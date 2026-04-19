from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any

from openai import OpenAI

from config import (
    AI_ANALYSIS_BATCH_SIZE,
    AI_ANALYSIS_MAX_REVIEW_CHARS,
    AI_ANALYSIS_MAX_WORKERS,
    AI_ANALYSIS_REQUEST_TIMEOUT_SECONDS,
    AI_SUMMARY_MAX_INPUT_CHARS,
    AI_SUMMARY_MAX_REVIEW_CHARS,
    AI_SUMMARY_MAX_REVIEWS,
    DEFAULT_AI_MODEL,
    POLZA_AI_BASE_URL,
)
from models import CompanyReviewSummary, ReviewRecord
from utils import normalize_whitespace, retry_call

SYSTEM_PROMPT = """
Ты проверяешь текст отзыва на естественность.

Нужно вернуть один из трех классов:
- "естественный"
- "подозрительный"
- "искусственный"

Критерии:
1. "естественный" — отзыв выглядит как обычный пользовательский опыт:
   - есть конкретные детали;
   - бытовой, неровный, живой стиль;
   - могут быть нюансы, смешанная оценка, частные замечания;
   - нет ощущения рекламного шаблона.

2. "подозрительный" — отзыв выглядит сомнительно, но не полностью явно:
   - слишком гладкий, общий, шаблонный;
   - мало конкретики, много общих слов;
   - слишком позитивный или маркетинговый тон;
   - похоже на полу-шаблонный хвалебный отзыв;
   - может напоминать ИИ-текст, но без полной уверенности.

3. "искусственный" — отзыв явно похож на:
   - текст, сгенерированный ИИ;
   - покупной, заказной, рекламный хвалебный отзыв;
   - шаблонный текст без личного опыта, но с выраженной похвалой;
   - чрезмерно универсальный, вылизанный, неестественно структурированный текст.

Важно:
- оценивай только текст отзыва;
- не додумывай факты о компании;
- если явных признаков искусственности нет, предпочитай "естественный";
- если признаки есть, но уверенность средняя, выбирай "подозрительный";
- поле reason должно быть коротким, по-русски, максимум 18 слов;
- confidence — число от 0 до 1.
""".strip()

BATCH_SYSTEM_PROMPT = """
Ты проверяешь естественность нескольких отзывов независимо друг от друга.

Для КАЖДОГО отзыва нужно вернуть:
- id
- label: "естественный" | "подозрительный" | "искусственный"
- reason: короткая причина по-русски, максимум 18 слов
- confidence: число от 0 до 1

Правила:
- оценивай только текст каждого отзыва;
- не сравнивай отзывы между собой;
- если явных признаков искусственности нет, предпочитай "естественный";
- если признаки есть, но уверенность средняя, выбирай "подозрительный";
- верни строго JSON-массив без markdown и без пояснений.

Пример:
[
  {"id":"r1","label":"естественный","reason":"есть бытовые детали","confidence":0.93},
  {"id":"r2","label":"подозрительный","reason":"слишком общий шаблонный тон","confidence":0.74}
]
""".strip()

COMPANY_SUMMARY_SYSTEM_PROMPT = """
Ты делаешь краткую сводку по отзывам одной компании.

Сначала отдели отзывы покупателей/клиентов от нерелевантных отзывов.
Нерелевантные отзывы — это, например:
- жалобы соседей или прохожих на шум стройки;
- жалобы на пыль, грязные окна, перекрытия, парковку вокруг стройки;
- жалобы жителей соседних домов на дискомфорт рядом с объектом;
- любые отзывы не про реальный опыт покупки, обращения или взаимодействия с компанией.

Такие отзывы считай "соседскими" и НЕ учитывай в выводах.

Дальше:
1. используй в первую очередь отзывы с меткой "естественный";
2. если их мало, можно осторожно опираться на "подозрительный";
3. "искусственный" учитывай только если иначе данных совсем мало;
4. positives и negatives — короткие тезисы на русском, максимум по 5 пунктов;
5. conclusion — один короткий абзац на русском, максимум 40 слов;
6. не выдумывай факты и не используй рекламный стиль.

Верни строго JSON без markdown и без пояснений:
{
  "used_review_ids":["r1","r4"],
  "excluded_neighbor_review_ids":["r2"],
  "positives":["хвалят качество отделки"],
  "negatives":["жалуются на долгую обратную связь"],
  "conclusion":"Покупатели чаще позитивно оценивают качество объекта, но часть клиентов недовольна скоростью коммуникации."
}
""".strip()


def _strip_code_fences(text: str) -> str:
    value = text.strip()
    value = re.sub(r"^```json\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^```\s*", "", value)
    value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _safe_confidence(value: object) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None

    if num < 0:
        return 0.0
    if num > 1:
        return 1.0
    return round(num, 2)


def _normalize_string_list(
    value: object,
    *,
    max_items: int = 5,
    max_item_chars: int = 220,
) -> list[str]:
    if not isinstance(value, list):
        return []

    result: list[str] = []

    for item in value[:max_items]:
        text = normalize_whitespace(str(item))
        if text:
            result.append(text[:max_item_chars])

    return result


def _normalize_id_list(value: object, allowed_ids: set[str]) -> list[str]:
    if not isinstance(value, list):
        return []

    result: list[str] = []
    seen: set[str] = set()

    for item in value:
        text = normalize_whitespace(str(item))
        if not text or text in seen or text not in allowed_ids:
            continue
        seen.add(text)
        result.append(text)

    return result


class ReviewAIAnalyzer:
    def __init__(
        self,
        *,
        api_key: str,
        logger: logging.Logger,
        model: str = DEFAULT_AI_MODEL,
        base_url: str = POLZA_AI_BASE_URL,
    ) -> None:
        self.logger = logger
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    def _create_client(self) -> OpenAI:
        return OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=AI_ANALYSIS_REQUEST_TIMEOUT_SECONDS,
        )

    def analyze_reviews(
        self,
        reviews: list[ReviewRecord],
        *,
        progress_callback: Callable[[int, int, ReviewRecord], None] | None = None,
    ) -> list[ReviewRecord]:
        total = len(reviews)
        result_rows: list[ReviewRecord | None] = [None] * total
        done = 0

        unique_entries: list[dict[str, Any]] = []
        dedupe_map: dict[str, dict[str, Any]] = {}

        for idx, review in enumerate(reviews):
            review_text = normalize_whitespace(review.review_text)

            if not review_text:
                result_rows[idx] = replace(
                    review,
                    ai_review_check="не определено",
                    ai_review_reason="пустой текст отзыва",
                    ai_review_confidence=None,
                )
                done += 1
                if progress_callback is not None:
                    progress_callback(done, total, review)
                continue

            dedupe_key = self._make_review_dedupe_key(review_text)

            entry = dedupe_map.get(dedupe_key)
            if entry is None:
                entry = {
                    "id": f"r{len(unique_entries) + 1}",
                    "text": review_text[:AI_ANALYSIS_MAX_REVIEW_CHARS],
                    "sample_review": review,
                    "indexes": [],
                }
                dedupe_map[dedupe_key] = entry
                unique_entries.append(entry)

            entry["indexes"].append(idx)

        if unique_entries:
            batches = [
                unique_entries[i : i + AI_ANALYSIS_BATCH_SIZE]
                for i in range(0, len(unique_entries), AI_ANALYSIS_BATCH_SIZE)
            ]

            max_workers = max(1, min(AI_ANALYSIS_MAX_WORKERS, len(batches)))

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_batch = {
                    executor.submit(self._classify_batch, batch): batch
                    for batch in batches
                }

                for future in as_completed(future_to_batch):
                    batch = future_to_batch[future]

                    try:
                        batch_result = future.result()
                    except Exception as exc:
                        self.logger.warning("Ошибка batch AI-анализа: %s", exc)
                        batch_result = {}

                    for entry in batch:
                        label, reason, confidence = batch_result.get(
                            entry["id"],
                            ("не определено", "ошибка AI-анализа батча", None),
                        )

                        for idx in entry["indexes"]:
                            review = reviews[idx]
                            result_rows[idx] = replace(
                                review,
                                ai_review_check=label,
                                ai_review_reason=reason,
                                ai_review_confidence=confidence,
                            )
                            done += 1
                            if progress_callback is not None:
                                progress_callback(done, total, review)

        analyzed: list[ReviewRecord] = []

        for idx, review in enumerate(reviews):
            row = result_rows[idx]
            if row is None:
                row = replace(
                    review,
                    ai_review_check="не определено",
                    ai_review_reason="результат AI-анализа не получен",
                    ai_review_confidence=None,
                )
            analyzed.append(row)

        return analyzed

    def summarize_companies(
        self,
        reviews: list[ReviewRecord],
        *,
        progress_callback: Callable[[int, int, CompanyReviewSummary], None] | None = None,
    ) -> list[CompanyReviewSummary]:
        grouped: dict[tuple[str, str, str, str], list[ReviewRecord]] = {}

        for review in reviews:
            key = (
                review.residential_complex_input,
                review.ymaps_card_name,
                review.ymaps_card_address,
                review.ymaps_card_url,
            )
            grouped.setdefault(key, []).append(review)

        company_groups = list(grouped.values())
        total = len(company_groups)

        if total == 0:
            return []

        result_rows: list[CompanyReviewSummary | None] = [None] * total
        max_workers = max(1, min(AI_ANALYSIS_MAX_WORKERS, total))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(self._summarize_company_reviews, company_reviews): idx
                for idx, company_reviews in enumerate(company_groups)
            }

            done = 0
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                company_reviews = company_groups[idx]

                try:
                    summary = future.result()
                except Exception as exc:
                    first = company_reviews[0]
                    self.logger.warning(
                        "Ошибка AI-сводки по компании '%s': %s",
                        first.ymaps_card_name or first.residential_complex_input,
                        exc,
                    )
                    summary = self._build_company_summary_record(
                        company_reviews,
                        summary_input_reviews=0,
                        source_reviews_used=0,
                        neighbor_reviews_excluded=0,
                        positives=[],
                        negatives=[],
                        conclusion="Не удалось сформировать AI-сводку по отзывам.",
                    )

                result_rows[idx] = summary
                done += 1
                if progress_callback is not None:
                    progress_callback(done, total, summary)

        return [item for item in result_rows if item is not None]

    def _make_review_dedupe_key(self, review_text: str) -> str:
        return normalize_whitespace(review_text).casefold()

    def _classify_batch(self, batch: list[dict[str, Any]]) -> dict[str, tuple[str, str, float | None]]:
        payload = [
            {
                "id": item["id"],
                "text": item["text"],
            }
            for item in batch
        ]

        user_prompt = (
            "Проанализируй каждый отзыв независимо и верни строго JSON-массив.\n\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )

        def action() -> str:
            client = self._create_client()
            response = client.chat.completions.create(
                model=self.model,
                temperature=0,
                messages=[
                    {"role": "system", "content": BATCH_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=900,
            )
            return response.choices[0].message.content or ""

        raw_response = retry_call(
            action,
            attempts=3,
            delay_seconds=2.0,
            backoff=2.0,
            logger=self.logger,
        )

        expected_ids = {str(item["id"]) for item in batch}
        return self._parse_batch_response(raw_response, expected_ids)

    def _parse_batch_response(
        self,
        raw_response: str,
        expected_ids: set[str],
    ) -> dict[str, tuple[str, str, float | None]]:
        cleaned = _strip_code_fences(raw_response)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            self.logger.warning("Не удалось распарсить AI batch-ответ как JSON: %s", raw_response)
            return {}

        if isinstance(data, dict) and isinstance(data.get("items"), list):
            items = data["items"]
        elif isinstance(data, list):
            items = data
        else:
            items = []

        result: dict[str, tuple[str, str, float | None]] = {}

        for item in items:
            if not isinstance(item, dict):
                continue

            item_id = normalize_whitespace(str(item.get("id", "")))
            if not item_id or item_id not in expected_ids:
                continue

            result[item_id] = self._parse_single_item_payload(item)

        return result

    def _parse_single_item_payload(self, item: dict[str, Any]) -> tuple[str, str, float | None]:
        label_raw = normalize_whitespace(str(item.get("label", ""))).lower()
        reason = normalize_whitespace(str(item.get("reason", "")))
        confidence = _safe_confidence(item.get("confidence"))

        if "искусствен" in label_raw:
            label = "искусственный"
        elif "подозр" in label_raw:
            label = "подозрительный"
        elif "естествен" in label_raw:
            label = "естественный"
        else:
            label = "не определено"

        if not reason:
            reason = "причина не указана"

        return label, reason, confidence

    def _summarize_company_reviews(self, reviews: list[ReviewRecord]) -> CompanyReviewSummary:
        if not reviews:
            raise ValueError("Пустой список отзывов для сводки")

        selected_reviews = self._select_reviews_for_summary(reviews)

        if not selected_reviews:
            return self._build_company_summary_record(
                reviews,
                summary_input_reviews=0,
                source_reviews_used=0,
                neighbor_reviews_excluded=0,
                positives=[],
                negatives=[],
                conclusion="Недостаточно текстов для формирования сводки.",
            )

        payload_items: list[dict[str, str]] = []
        total_chars = 0

        for idx, review in enumerate(selected_reviews, start=1):
            text = normalize_whitespace(review.review_text)[:AI_SUMMARY_MAX_REVIEW_CHARS]
            item = {
                "id": f"r{idx}",
                "ai_label": review.ai_review_check or "не определено",
                "date": review.review_date or "—",
                "text": text,
            }
            payload_json = json.dumps(item, ensure_ascii=False)

            if payload_items and total_chars + len(payload_json) + 2 > AI_SUMMARY_MAX_INPUT_CHARS:
                break

            payload_items.append(item)
            total_chars += len(payload_json) + 2

        if not payload_items:
            return self._build_company_summary_record(
                reviews,
                summary_input_reviews=0,
                source_reviews_used=0,
                neighbor_reviews_excluded=0,
                positives=[],
                negatives=[],
                conclusion="Недостаточно текстов для формирования сводки.",
            )

        first = reviews[0]
        natural_count = sum(1 for review in reviews if review.ai_review_check == "естественный")
        allowed_ids = {item["id"] for item in payload_items}

        user_prompt = f"""
Сделай краткую сводку по отзывам одной компании и верни только JSON.

Компания: {first.ymaps_card_name or first.residential_complex_input or "—"}
Адрес: {first.ymaps_card_address or "—"}
Всего собранных отзывов: {len(reviews)}
Естественных отзывов: {natural_count}

Важно:
- сначала отдели отзывы покупателей/клиентов от соседских жалоб;
- соседские жалобы в итог не включай;
- используй в первую очередь отзывы с меткой "естественный".

Вот JSON-массив отзывов:
{json.dumps(payload_items, ensure_ascii=False)}
""".strip()

        def action() -> str:
            client = self._create_client()
            response = client.chat.completions.create(
                model=self.model,
                temperature=0,
                messages=[
                    {"role": "system", "content": COMPANY_SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=500,
            )
            return response.choices[0].message.content or ""

        raw_response = retry_call(
            action,
            attempts=3,
            delay_seconds=2.0,
            backoff=2.0,
            logger=self.logger,
        )

        (
            used_review_ids,
            excluded_neighbor_review_ids,
            positives,
            negatives,
            conclusion,
        ) = self._parse_company_summary_response(raw_response, allowed_ids)

        source_reviews_used = len(used_review_ids)
        neighbor_reviews_excluded = len(excluded_neighbor_review_ids)

        if source_reviews_used == 0 and payload_items:
            source_reviews_used = max(0, len(payload_items) - neighbor_reviews_excluded)

        return self._build_company_summary_record(
            reviews,
            summary_input_reviews=len(payload_items),
            source_reviews_used=source_reviews_used,
            neighbor_reviews_excluded=neighbor_reviews_excluded,
            positives=positives,
            negatives=negatives,
            conclusion=conclusion,
        )

    def _select_reviews_for_summary(self, reviews: list[ReviewRecord]) -> list[ReviewRecord]:
        dedup: dict[str, ReviewRecord] = {}

        for review in reviews:
            text = normalize_whitespace(review.review_text)
            if not text:
                continue

            key = text.casefold()
            existing = dedup.get(key)

            if existing is None:
                dedup[key] = review
                continue

            if self._summary_sort_key(review) < self._summary_sort_key(existing):
                dedup[key] = review

        unique_reviews = list(dedup.values())
        unique_reviews.sort(key=self._summary_sort_key)

        return unique_reviews[:AI_SUMMARY_MAX_REVIEWS]

    def _summary_sort_key(self, review: ReviewRecord) -> tuple[int, int]:
        label_priority = {
            "естественный": 0,
            "подозрительный": 1,
            "не определено": 2,
            "искусственный": 3,
        }
        text_len = len(normalize_whitespace(review.review_text))
        priority = label_priority.get(review.ai_review_check or "", 2)
        return (priority, -text_len)

    def _build_company_summary_record(
        self,
        reviews: list[ReviewRecord],
        *,
        summary_input_reviews: int,
        source_reviews_used: int,
        neighbor_reviews_excluded: int,
        positives: list[str],
        negatives: list[str],
        conclusion: str,
    ) -> CompanyReviewSummary:
        first = reviews[0]

        return CompanyReviewSummary(
            residential_complex_input=first.residential_complex_input,
            ymaps_card_name=first.ymaps_card_name,
            ymaps_card_address=first.ymaps_card_address,
            ymaps_card_url=first.ymaps_card_url,
            total_reviews=len(reviews),
            natural_reviews=sum(1 for review in reviews if review.ai_review_check == "естественный"),
            suspicious_reviews=sum(1 for review in reviews if review.ai_review_check == "подозрительный"),
            artificial_reviews=sum(1 for review in reviews if review.ai_review_check == "искусственный"),
            summary_input_reviews=summary_input_reviews,
            source_reviews_used=source_reviews_used,
            neighbor_reviews_excluded=neighbor_reviews_excluded,
            positives=positives,
            negatives=negatives,
            conclusion=conclusion,
        )

    def _parse_company_summary_response(
        self,
        raw_response: str,
        allowed_ids: set[str],
    ) -> tuple[list[str], list[str], list[str], list[str], str]:
        cleaned = _strip_code_fences(raw_response)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            self.logger.warning("Не удалось распарсить AI-сводку как JSON: %s", raw_response)
            return [], [], [], [], "Некорректный формат ответа модели при формировании сводки."

        used_review_ids = _normalize_id_list(data.get("used_review_ids"), allowed_ids)
        excluded_neighbor_review_ids = _normalize_id_list(
            data.get("excluded_neighbor_review_ids"),
            allowed_ids,
        )
        positives = _normalize_string_list(data.get("positives"))
        negatives = _normalize_string_list(data.get("negatives"))
        conclusion = normalize_whitespace(str(data.get("conclusion", "")))

        if not conclusion:
            conclusion = "Краткий вывод по отзывам не сформирован."

        return (
            used_review_ids,
            excluded_neighbor_review_ids,
            positives,
            negatives,
            conclusion,
        )
