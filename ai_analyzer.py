from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import replace

from openai import OpenAI

from config import (
    AI_ANALYSIS_MAX_REVIEW_CHARS,
    AI_ANALYSIS_REQUEST_TIMEOUT_SECONDS,
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

Верни строго JSON без markdown и без пояснений:
{"label":"естественный","reason":"есть конкретные бытовые детали","confidence":0.91}
""".strip()

COMPANY_SUMMARY_SYSTEM_PROMPT = """
Ты делаешь краткую сводку по отзывам покупателей одной компании.

Нужно:
1. выделить, что покупатели чаще хвалят;
2. выделить, что покупатели чаще критикуют;
3. дать короткий общий вывод.

Правила:
- опирайся только на переданные отзывы;
- не выдумывай факты;
- не используй рекламный стиль;
- если данных мало, формулируй осторожно;
- positives и negatives — массивы коротких тезисов на русском, максимум по 5 пунктов;
- conclusion — один короткий абзац на русском, максимум 35 слов.

Верни строго JSON без markdown и без пояснений:
{"positives":["хвалят удобное расположение"],"negatives":["жалуются на задержки"],"conclusion":"Отзывы в целом положительные, но часть покупателей регулярно отмечает проблемы со сроками."}
""".strip()

COMPANY_SUMMARY_MAX_INPUT_CHARS = 12_000
COMPANY_SUMMARY_MAX_REVIEWS = 25
COMPANY_SUMMARY_MAX_REVIEW_CHARS = 700


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


def _normalize_string_list(value: object, *, max_items: int = 5, max_item_chars: int = 220) -> list[str]:
    if not isinstance(value, list):
        return []

    result: list[str] = []

    for item in value[:max_items]:
        text = normalize_whitespace(str(item))
        if text:
            result.append(text[:max_item_chars])

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
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=AI_ANALYSIS_REQUEST_TIMEOUT_SECONDS,
        )

    def analyze_reviews(
        self,
        reviews: list[ReviewRecord],
        *,
        progress_callback: Callable[[int, int, ReviewRecord], None] | None = None,
    ) -> list[ReviewRecord]:
        analyzed: list[ReviewRecord] = []
        total = len(reviews)

        for idx, review in enumerate(reviews, start=1):
            try:
                label, reason, confidence = self._classify_review(review)
            except Exception as exc:
                self.logger.warning(
                    "Ошибка AI-анализа отзыва '%s' (%s): %s",
                    review.ymaps_card_name or review.residential_complex_input,
                    review.review_date,
                    exc,
                )
                label, reason, confidence = "не определено", "ошибка AI-анализа", None

            analyzed.append(
                replace(
                    review,
                    ai_review_check=label,
                    ai_review_reason=reason,
                    ai_review_confidence=confidence,
                )
            )

            if progress_callback is not None:
                progress_callback(idx, total, review)

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

        summaries: list[CompanyReviewSummary] = []
        total = len(grouped)

        for idx, company_reviews in enumerate(grouped.values(), start=1):
            try:
                summary = self._summarize_company_reviews(company_reviews)
            except Exception as exc:
                first = company_reviews[0]
                self.logger.warning(
                    "Ошибка AI-сводки по компании '%s': %s",
                    first.ymaps_card_name or first.residential_complex_input,
                    exc,
                )
                summary = self._build_company_summary_record(
                    company_reviews,
                    positives=[],
                    negatives=[],
                    conclusion="Не удалось сформировать AI-сводку по отзывам.",
                    source_reviews_used=0,
                )

            summaries.append(summary)

            if progress_callback is not None:
                progress_callback(idx, total, summary)

        return summaries

    def _classify_review(self, review: ReviewRecord) -> tuple[str, str, float | None]:
        review_text = normalize_whitespace(review.review_text)

        if not review_text:
            return "не определено", "пустой текст отзыва", None

        trimmed_text = review_text[:AI_ANALYSIS_MAX_REVIEW_CHARS]

        user_prompt = f"""
Проанализируй один отзыв и верни только JSON.

Карточка: {review.ymaps_card_name or review.residential_complex_input or "—"}
Дата: {review.review_date or "—"}
Автор: {review.user_name or "—"}

Текст отзыва:
\"\"\"{trimmed_text}\"\"\"
""".strip()

        def action() -> str:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=0,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=120,
            )
            return response.choices[0].message.content or ""

        raw_response = retry_call(
            action,
            attempts=3,
            delay_seconds=2.0,
            backoff=2.0,
            logger=self.logger,
        )

        return self._parse_response(raw_response)

    def _summarize_company_reviews(self, reviews: list[ReviewRecord]) -> CompanyReviewSummary:
        if not reviews:
            raise ValueError("Пустой список отзывов для сводки")

        natural_reviews = [
            review
            for review in reviews
            if review.ai_review_check == "естественный" and normalize_whitespace(review.review_text)
        ]
        fallback_reviews = [review for review in reviews if normalize_whitespace(review.review_text)]
        source_reviews = natural_reviews or fallback_reviews

        payload, source_reviews_used = self._build_summary_reviews_payload(source_reviews)

        if not payload:
            return self._build_company_summary_record(
                reviews,
                positives=[],
                negatives=[],
                conclusion="Недостаточно текстов для формирования сводки.",
                source_reviews_used=0,
            )

        first = reviews[0]
        natural_count = sum(1 for review in reviews if review.ai_review_check == "естественный")

        user_prompt = f"""
Сделай краткую сводку по отзывам одной компании и верни только JSON.

Компания: {first.ymaps_card_name or first.residential_complex_input or "—"}
Адрес: {first.ymaps_card_address or "—"}
Всего отзывов: {len(reviews)}
Естественных отзывов: {natural_count}

Используй только информацию из текстов ниже.
Если естественные отзывы есть, ориентируйся в первую очередь на них.

Отзывы:
\"\"\"{payload}\"\"\"
""".strip()

        def action() -> str:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=0,
                messages=[
                    {"role": "system", "content": COMPANY_SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=350,
            )
            return response.choices[0].message.content or ""

        raw_response = retry_call(
            action,
            attempts=3,
            delay_seconds=2.0,
            backoff=2.0,
            logger=self.logger,
        )

        positives, negatives, conclusion = self._parse_company_summary_response(raw_response)

        return self._build_company_summary_record(
            reviews,
            positives=positives,
            negatives=negatives,
            conclusion=conclusion,
            source_reviews_used=source_reviews_used,
        )

    def _build_summary_reviews_payload(self, reviews: list[ReviewRecord]) -> tuple[str, int]:
        parts: list[str] = []
        current_chars = 0
        used = 0

        for idx, review in enumerate(reviews, start=1):
            text = normalize_whitespace(review.review_text)
            if not text:
                continue

            fragment = (
                f"Отзыв {idx}\n"
                f"Дата: {review.review_date or '—'}\n"
                f"Автор: {review.user_name or '—'}\n"
                f"Текст: {text[:COMPANY_SUMMARY_MAX_REVIEW_CHARS]}"
            )

            if parts and current_chars + len(fragment) + 2 > COMPANY_SUMMARY_MAX_INPUT_CHARS:
                break

            parts.append(fragment)
            current_chars += len(fragment) + 2
            used += 1

            if used >= COMPANY_SUMMARY_MAX_REVIEWS:
                break

        return "\n\n".join(parts), used

    def _build_company_summary_record(
        self,
        reviews: list[ReviewRecord],
        *,
        positives: list[str],
        negatives: list[str],
        conclusion: str,
        source_reviews_used: int,
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
            source_reviews_used=source_reviews_used,
            positives=positives,
            negatives=negatives,
            conclusion=conclusion,
        )

    def _parse_response(self, raw_response: str) -> tuple[str, str, float | None]:
        cleaned = _strip_code_fences(raw_response)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            self.logger.warning("Не удалось распарсить AI-ответ как JSON: %s", raw_response)
            return "не определено", "некорректный формат ответа модели", None

        label_raw = normalize_whitespace(str(data.get("label", ""))).lower()
        reason = normalize_whitespace(str(data.get("reason", "")))
        confidence = _safe_confidence(data.get("confidence"))

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

    def _parse_company_summary_response(self, raw_response: str) -> tuple[list[str], list[str], str]:
        cleaned = _strip_code_fences(raw_response)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            self.logger.warning("Не удалось распарсить AI-сводку как JSON: %s", raw_response)
            return [], [], "Некорректный формат ответа модели при формировании сводки."

        positives = _normalize_string_list(data.get("positives"))
        negatives = _normalize_string_list(data.get("negatives"))
        conclusion = normalize_whitespace(str(data.get("conclusion", "")))

        if not conclusion:
            conclusion = "Краткий вывод по отзывам не сформирован."

        return positives, negatives, conclusion
