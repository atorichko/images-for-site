from __future__ import annotations

BASE_URL = "https://yandex.ru/maps/"

DEFAULT_TIMEOUT_MS = 20_000
SEARCH_TIMEOUT_MS = 35_000
CARD_TIMEOUT_MS = 25_000

DEFAULT_MAX_REVIEWS = 100
MAX_SCROLL_STAGNATION = 12

MIN_DELAY_SECONDS = 0.8
MAX_DELAY_SECONDS = 1.8

BROWSER_WIDTH = 1440
BROWSER_HEIGHT = 980

CAPTCHA_WAIT_TIMEOUT_SECONDS = 180
CAPTCHA_POLL_INTERVAL_SECONDS = 2

CAPTCHA_SELECTORS = [
    'text="Я не робот"',
    'text="Введите символы"',
    'text="Подтвердите, что запросы отправляли вы"',
    'text="Проверьте, что вы не робот"',
    'iframe[src*="captcha"]',
    'input[name="rep"]',
    'input[name="smart-token"]',
]

RESULT_SELECTORS = [
    'a[href*="/org/"]',
    '[class*="search-business-snippet-view"] a',
    '[class*="search-snippet-view"] a',
    '[class*="search-business-snippet-view"]',
    '[class*="search-snippet-view"]',
    '[class*="search-list-view"] a',
    '[class*="search-list-view"] [href*="/org/"]',
]

CARD_TITLE_SELECTORS = [
    ".orgpage-header-view__header",
    ".card-title-view__title",
    ".business-header-title-view__title",
    '[class*="orgpage-header-view__header"]',
    '[class*="title-view__title"]',
    '[class*="business-header-title"]',
    'h1[class*="header"]',
    'h1[class*="title"]',
    "h1",
]

CARD_ADDRESS_SELECTORS = [
    ".business-contacts-view__address-link",
    ".card-address-view__address",
    ".card-address-view",
    '[class*="contacts-view__address"]',
    '[class*="business-contacts-view__address"]',
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
    'text="Будьте первым, кто оставит отзыв"',
]

SORT_BUTTON_SELECTORS = [
    'button:has-text("По умолчанию")',
    'button:has-text("Сначала полезные")',
    'button:has-text("Сначала новые")',
    'button:has-text("Сортировка")',
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
    '[class*="reviews-list-view__review"]',
    '[class*="review-business-item"]',
    '[data-review-id]',
    'article',
]

REVIEW_USER_SELECTORS = [
    ".business-review-view__author",
    '[class*="business-review-view__author"]',
    '[class*="author"]',
    '[class*="user-name"]',
    '[class*="name-view"]',
    '[itemprop="author"]',
]

REVIEW_DATE_SELECTORS = [
    ".business-review-view__date",
    '[class*="business-review-view__date"]',
    "time",
    '[datetime]',
    '[class*="date"]',
]

REVIEW_TEXT_SELECTORS = [
    ".business-review-view__body-text",
    ".spoiler-view__text-container",
    '[class*="business-review-view__body-text"]',
    '[class*="review-view__body"]',
    '[class*="review-text"]',
    '[class*="comment"]',
    '[itemprop="reviewBody"]',
]

REVIEW_EXPAND_BUTTON_SELECTORS = [
    'button:has-text("Читать целиком")',
    'button:has-text("ещё")',
    'button:has-text("Ещё")',
    'text="Читать целиком"',
    'text="ещё"',
    'text="Ещё"',
]

REVIEW_SCROLL_CONTAINER_SELECTORS = [
    '[class*="business-reviews-card-view__scroll"]',
    '[class*="business-reviews-card-view__reviews-container"]',
    '[class*="reviews-view__scroll"]',
    '[class*="reviews-list-view"]',
    '[class*="scroll__container"]',
    '[class*="scrollbar__container"]',
    '[class*="tabs-select-view__content"]',
]
