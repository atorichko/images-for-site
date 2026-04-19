from __future__ import annotations

from dataclasses import dataclass, field


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


@dataclass(slots=True)
class CompanyReviewSummary:
    residential_complex_input: str
    ymaps_card_name: str
    ymaps_card_address: str
    ymaps_card_url: str
    total_reviews: int = 0
    natural_reviews: int = 0
    suspicious_reviews: int = 0
    artificial_reviews: int = 0
    summary_input_reviews: int = 0
    source_reviews_used: int = 0
    neighbor_reviews_excluded: int = 0
    positives: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)
    conclusion: str = ""
