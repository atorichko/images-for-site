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
from models import ReviewRecord
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
