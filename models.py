from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CardMatch:
    residential_complex_input: str
    search_query: str
    ymaps_card_name: str
    ymaps_card_address: str
    ymaps_card_url: str


@dataclass(slots=True)
class ReviewRecord:
    residential_complex_input: str
    ymaps_card_name: str
    ymaps_card_address: str
    ymaps_card_url: str
    review_date: str
    user_name: str
    review_text: str
    ai_review_check: str = ""
    ai_review_reason: str = ""
    ai_review_confidence: float | None = None
