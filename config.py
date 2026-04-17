from __future__ import annotations

BASE_URL = "https://yandex.ru/maps/"

DEFAULT_TIMEOUT_MS = 15_000
SEARCH_TIMEOUT_MS = 25_000
CARD_TIMEOUT_MS = 20_000

DEFAULT_MAX_REVIEWS = 100
MAX_SCROLL_STAGNATION = 5

MIN_DELAY_SECONDS = 0.6
MAX_DELAY_SECONDS = 1.4

BROWSER_WIDTH = 1440
BROWSER_HEIGHT = 980

CAPTCHA_SELECTORS = [
    'text="Я не робот"',
    'text="Введите символы"',
    'text="Подтвердите, что запросы отправляли вы"',
    'iframe[src*="captcha"]',
    'input[name="rep"]',
]

RESULT_SELECTORS = [
    'a[href*="/org/"]',
    '[class*="search-business-snippet-view"] a',
    '[class*="search-snippet-view"] a',
    '[class*="search-business-snippet-view"]',
    '[class*="search-snippet-view"]',
]

CARD_TITLE_SELECTORS = [
    ".orgpage-header-view__header",
    ".card-title-view__title",
    ".business-header-title-view__title",
    '[class*="orgpage-header-view__header"]',
    '[class*="title-view__title"]',
    'h1[class*="header"]',
    "h1",
]

CARD_ADDRESS_SELECTORS = [
    ".business-contacts-view__address-link",
    ".card-address-view__address",
    ".card-address-view",
    '[class*="contacts-view__address"]',
    '[class*="address-view"]',
    '[class*="address"]',
]

OPEN_REVIEWS_SELECTORS = [
    'a:has-text("Все отзывы")',
    'button:has-text("Все отзывы")',
    'text="Все отзывы"',
    '[role="tab"]:has-text("Отзывы")',
    'a:has-text("Отзывы")',
    'button:has-text("Отзывы")',
    'text="Отзывы"',
]

NO_REVIEWS_SELECTORS = [
    'text="Нет отзывов"',
    'text="Отзывов пока нет"',
]

SORT_BUTTON_SELECTORS = [
    'button:has-text("По умолчанию")',
    'button:has-text("Сначала полезные")',
    'button:has-text("Сначала новые")',
    'text="Сортировка"',
]

NEWEST_OPTION_SELECTORS = [
    '[role="menuitem"]:has-text("Сначала новые")',
    'button:has-text("Сначала новые")',
    'text="Сначала новые"',
]

REVIEW_ITEM_SELECTORS = [
    ".business-review-view",
    '[class*="business-review-view"]',
    '[class*="review-snippet-view"]',
    "[data-review-id]",
]

REVIEW_USER_SELECTORS = [
    ".business-review-view__author",
    '[class*="author"]',
    '[class*="user-name"]',
    '[itemprop="author"]',
]

REVIEW_DATE_SELECTORS = [
    ".business-review-view__date",
    "time",
    '[class*="date"]',
]

REVIEW_TEXT_SELECTORS = [
    ".business-review-view__body-text",
    ".spoiler-view__text-container",
    '[class*="review-view__body"]',
    '[class*="review-text"]',
    '[itemprop="reviewBody"]',
]

REVIEW_EXPAND_BUTTON_SELECTORS = [
    'button:has-text("Читать целиком")',
    'button:has-text("ещё")',
    'text="Читать целиком"',
    'text="ещё"',
]

REVIEW_SCROLL_CONTAINER_SELECTORS = [
    '[class*="business-reviews-card-view__scroll"]',
    '[class*="reviews-view__scroll"]',
    '[class*="scroll__container"]',
]
