from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from config import (
    BASE_URL,
    BROWSER_HEIGHT,
    BROWSER_WIDTH,
    CAPTCHA_POLL_INTERVAL_SECONDS,
    CAPTCHA_SELECTORS,
    CAPTCHA_WAIT_TIMEOUT_SECONDS,
    CARD_ADDRESS_SELECTORS,
    CARD_TIMEOUT_MS,
    CARD_TITLE_SELECTORS,
    DEFAULT_MAX_REVIEWS,
    DEFAULT_TIMEOUT_MS,
    MAX_DELAY_SECONDS,
    MAX_SCROLL_STAGNATION,
    MIN_DELAY_SECONDS,
    NEWEST_OPTION_SELECTORS,
    NO_REVIEWS_SELECTORS,
    OPEN_REVIEWS_SELECTORS,
    RESULT_SELECTORS,
    REVIEW_DATE_SELECTORS,
    REVIEW_EXPAND_BUTTON_SELECTORS,
    REVIEW_ITEM_SELECTORS,
    REVIEW_SCROLL_CONTAINER_SELECTORS,
    REVIEW_TEXT_SELECTORS,
    REVIEW_USER_SELECTORS,
    SEARCH_TIMEOUT_MS,
    SORT_BUTTON_SELECTORS,
)
from models import CardMatch, ReviewRecord
from utils import normalize_whitespace, retry_call, sleep_random


class CaptchaRequiredError(RuntimeError):
    pass


def _get_playwright_cache_dir() -> Path:
    custom_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom_path:
        return Path(custom_path)
    return Path.home() / ".cache" / "ms-playwright"


def _browser_glob_patterns() -> list[str]:
    return [
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-linux64/chrome",
        "chromium-*/chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-win/chrome.exe",
    ]


def _playwright_browser_exists() -> bool:
    cache_dir = _get_playwright_cache_dir()

    if not cache_dir.exists():
        return False

    for pattern in _browser_glob_patterns():
        if next(cache_dir.glob(pattern), None) is not None:
            return True

    return False


def ensure_playwright_browser_installed(logger: logging.Logger) -> None:
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        str(Path.home() / ".cache" / "ms-playwright"),
    )

    if _playwright_browser_exists():
        logger.info("Playwright Chromium уже установлен")
        return

    logger.warning("Playwright Chromium не найден. Запускаем playwright install chromium ...")

    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error("Установка Chromium завершилась с ошибкой")
        logger.error("stdout: %s", result.stdout)
        logger.error("stderr: %s", result.stderr)
        raise RuntimeError(
            "Не удалось установить Chromium для Playwright. "
            f"stderr: {result.stderr.strip() or result.stdout.strip()}"
        )

    if not _playwright_browser_exists():
        raise RuntimeError(
            "playwright install chromium завершился без ошибки, "
            "но исполняемый файл Chromium не найден."
        )

    logger.info("Playwright Chromium успешно установлен")


class YandexMapsScraper:
    def __init__(
        self,
        *,
        headless: bool,
        logger: logging.Logger,
        status_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.headless = headless
        self.effective_headless = headless
        self.logger = logger
        self.status_callback = status_callback

        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def __enter__(self) -> "YandexMapsScraper":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _emit_status(self, message: str) -> None:
        self.logger.info(message)
        if self.status_callback is not None:
            self.status_callback(message)

    def start(self) -> None:
        self._emit_status("Проверка браузера Chromium для Playwright")
        ensure_playwright_browser_installed(self.logger)

        self._emit_status("Запуск Playwright")
        self.playwright = sync_playwright().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]

        if platform.system().lower() == "linux":
            launch_args.extend(
                [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ]
            )

        self.effective_headless = self.headless

        if platform.system().lower() == "linux" and not os.environ.get("DISPLAY"):
            self.effective_headless = True
            self._emit_status("DISPLAY не найден. Принудительно включен headless-режим.")

        self.browser = self.playwright.chromium.launch(
            headless=self.effective_headless,
            slow_mo=50,
            args=launch_args,
        )

        self.context = self.browser.new_context(
            locale="ru-RU",
            viewport={"width": BROWSER_WIDTH, "height": BROWSER_HEIGHT},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        )
        self.page = self.context.new_page()
        self.page.set_default_timeout(DEFAULT_TIMEOUT_MS)

    def close(self) -> None:
        self.logger.info("Закрытие браузера")
        if self.context is not None:
            self.context.close()
        if self.browser is not None:
            self.browser.close()
        if self.playwright is not None:
            self.playwright.stop()

    def search_card(
        self,
        *,
        residential_complex_input: str,
        search_query: str,
    ) -> CardMatch | None:
        page = self._require_page()
        self._emit_status(f"Поиск карточки: {search_query}")

        search_url = f"{BASE_URL}?text={quote(search_query)}"
        self._goto(search_url)
        self._maybe_handle_captcha(page)
        sleep_random(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)

        self._wait_for_results_or_card(page)

        if not self._has_open_card(page):
            clicked = self._open_first_search_result(page)
            if not clicked:
                self.logger.warning(
                    "Не удалось открыть карточку из результатов поиска: %s",
                    search_query,
                )
                return None

        self._maybe_handle_captcha(page)
        self._wait_for_card(page)

        title = self._extract_text_from_selectors(page, CARD_TITLE_SELECTORS)
        address = self._extract_text_from_selectors(page, CARD_ADDRESS_SELECTORS)
        card_url = self._extract_card_url(page)

        if not title and not card_url:
            self.logger.warning("Карточка не распознана после поиска: %s", search_query)
            return None

        self._emit_status(f"Карточка найдена: {title or card_url}")

        return CardMatch(
            residential_complex_input=residential_complex_input,
            search_query=search_query,
            ymaps_card_name=title,
            ymaps_card_address=address,
            ymaps_card_url=card_url,
        )

    def collect_reviews(
        self,
        *,
        card: CardMatch,
        limit: int = DEFAULT_MAX_REVIEWS,
    ) -> list[ReviewRecord]:
        page = self._require_page()
        self._emit_status(f"Открытие карточки: {card.ymaps_card_name or card.ymaps_card_url}")

        if not card.ymaps_card_url:
            self.logger.warning("У карточки отсутствует URL, пропуск")
            return []

        self._goto(card.ymaps_card_url)
        self._maybe_handle_captcha(page)
        self._wait_for_card(page)

        if not self._open_reviews_section(page):
            self._emit_status(
                f"Отзывов нет или раздел отзывов не найден: {card.ymaps_card_name or card.ymaps_card_url}"
            )
            return []

        self._sort_reviews_by_newest(page)

        collected: dict[tuple[str, str, str], ReviewRecord] = {}
        stagnant_iterations = 0

        while len(collected) < limit and stagnant_iterations < MAX_SCROLL_STAGNATION:
            self._maybe_handle_captcha(page)
            self._expand_visible_review_texts(page)

            before_count = len(collected)
            batch = self._extract_reviews_from_dom(page, card)

            for review in batch:
                key = (review.review_date, review.user_name, review.review_text)
                if key not in collected:
                    collected[key] = review

            after_count = len(collected)
            self._emit_status(
                f"Собрано {after_count}/{limit} отзывов по '{card.ymaps_card_name or card.ymaps_card_url}'"
            )

            if after_count >= limit:
                break

            scrolled = self._scroll_reviews(page)
            sleep_random(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)

            if after_count == before_count:
                stagnant_iterations += 1
            elif not scrolled:
                stagnant_iterations += 1
            else:
                stagnant_iterations = 0

        result = list(collected.values())[:limit]
        self._emit_status(
            f"Сбор завершен: {len(result)} отзывов по '{card.ymaps_card_name or card.ymaps_card_url}'"
        )
        return result

    def _require_page(self) -> Page:
        if self.page is None:
            raise RuntimeError("Браузер не инициализирован")
        return self.page

    def _goto(self, url: str) -> None:
        page = self._require_page()

        def action() -> None:
            page.goto(url, wait_until="domcontentloaded", timeout=SEARCH_TIMEOUT_MS)

        retry_call(
            action,
            attempts=3,
            delay_seconds=2.0,
            backoff=1.8,
            logger=self.logger,
            retry_exceptions=(PlaywrightTimeoutError, PlaywrightError),
        )

    def _wait_for_results_or_card(self, page: Page, timeout_ms: int = SEARCH_TIMEOUT_MS) -> None:
        end_time = time.time() + timeout_ms / 1000

        while time.time() < end_time:
            if self._has_open_card(page) or self._has_search_results(page):
                return
            time.sleep(0.3)

        self.logger.warning("Истекло ожидание результатов поиска/карточки")

    def _wait_for_card(self, page: Page, timeout_ms: int = CARD_TIMEOUT_MS) -> None:
        end_time = time.time() + timeout_ms / 1000

        while time.time() < end_time:
            title = self._extract_text_from_selectors(page, CARD_TITLE_SELECTORS)
            if title or "/org/" in page.url:
                return
            time.sleep(0.3)

        self.logger.warning("Истекло ожидание открытия карточки")

    def _has_open_card(self, page: Page) -> bool:
        title = self._extract_text_from_selectors(page, CARD_TITLE_SELECTORS)
        return bool(title) or "/org/" in page.url

    def _has_search_results(self, page: Page) -> bool:
        return self._find_first_visible(page, RESULT_SELECTORS, timeout_ms=0) is not None

    def _open_first_search_result(self, page: Page) -> bool:
        self._emit_status("Пробуем открыть первый результат поиска")

        for selector in RESULT_SELECTORS:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 10)
            except PlaywrightError:
                continue

            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if not item.is_visible():
                        continue
                    item.click(timeout=5_000)
                    sleep_random(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
                    return True
                except PlaywrightError:
                    continue

        return False

    def _extract_card_url(self, page: Page) -> str:
        current_url = page.url
        if "/org/" in current_url:
            return current_url

        locator = page.locator('a[href*="/org/"]')
        try:
            count = min(locator.count(), 5)
        except PlaywrightError:
            count = 0

        for idx in range(count):
            item = locator.nth(idx)
            try:
                href = item.get_attribute("href")
                if href and "/org/" in href:
                    return urljoin(BASE_URL, href)
            except PlaywrightError:
                continue

        return current_url

    def _open_reviews_section(self, page: Page) -> bool:
        if self._find_first_visible(page, NO_REVIEWS_SELECTORS, timeout_ms=1_000) is not None:
            return False

        if self._find_first_visible(page, REVIEW_ITEM_SELECTORS, timeout_ms=1_000) is not None:
            return True

        for selector in OPEN_REVIEWS_SELECTORS:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 5)
            except PlaywrightError:
                continue

            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if not item.is_visible():
                        continue
                    item.click(timeout=5_000)
                    sleep_random(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
                    if self._find_first_visible(page, REVIEW_ITEM_SELECTORS, timeout_ms=5_000) is not None:
                        return True
                except PlaywrightError:
                    continue

        if self._find_first_visible(page, NO_REVIEWS_SELECTORS, timeout_ms=1_000) is not None:
            return False

        return self._find_first_visible(page, REVIEW_ITEM_SELECTORS, timeout_ms=3_000) is not None

    def _sort_reviews_by_newest(self, page: Page) -> None:
        self._emit_status("Пробуем включить сортировку 'Сначала новые'")

        newest_visible = self._find_first_visible(page, NEWEST_OPTION_SELECTORS, timeout_ms=1_000)
        if newest_visible is not None:
            try:
                newest_visible.click(timeout=2_000)
                sleep_random(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
                return
            except PlaywrightError:
                pass

        sort_button = self._find_first_visible(page, SORT_BUTTON_SELECTORS, timeout_ms=3_000)
        if sort_button is None:
            self.logger.warning("Не найдена кнопка сортировки отзывов")
            return

        try:
            sort_button.click(timeout=3_000)
            sleep_random(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)

            newest_option = self._find_first_visible(page, NEWEST_OPTION_SELECTORS, timeout_ms=3_000)
            if newest_option is None:
                self.logger.warning("Не найден пункт 'Сначала новые'")
                return

            newest_option.click(timeout=3_000)
            sleep_random(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
        except PlaywrightError as exc:
            self.logger.warning("Не удалось изменить сортировку отзывов: %s", exc)

    def _extract_reviews_from_dom(self, page: Page, card: CardMatch) -> list[ReviewRecord]:
        selector = self._pick_first_selector_with_matches(page, REVIEW_ITEM_SELECTORS)
        if selector is None:
            return []

        items = page.locator(selector)
        try:
            count = min(items.count(), 150)
        except PlaywrightError:
            return []

        result: list[ReviewRecord] = []

        for idx in range(count):
            item = items.nth(idx)
            try:
                if not item.is_visible():
                    continue
            except PlaywrightError:
                continue

            user_name = self._extract_text_from_selectors(item, REVIEW_USER_SELECTORS)
            review_date = self._extract_text_from_selectors(item, REVIEW_DATE_SELECTORS)
            review_text = self._extract_text_from_selectors(item, REVIEW_TEXT_SELECTORS)

            if not any([user_name, review_date, review_text]):
                continue

            result.append(
                ReviewRecord(
                    residential_complex_input=card.residential_complex_input,
                    ymaps_card_name=card.ymaps_card_name,
                    ymaps_card_address=card.ymaps_card_address,
                    ymaps_card_url=card.ymaps_card_url,
                    review_date=review_date,
                    user_name=user_name,
                    review_text=review_text,
                )
            )

        return result

    def _expand_visible_review_texts(self, page: Page) -> None:
        for selector in REVIEW_EXPAND_BUTTON_SELECTORS:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 10)
            except PlaywrightError:
                continue

            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if item.is_visible():
                        item.click(timeout=1_000)
                        time.sleep(0.1)
                except PlaywrightError:
                    continue

    def _scroll_reviews(self, page: Page) -> bool:
        container = self._find_review_scroll_container(page)

        if container is not None:
            try:
                before = container.evaluate("(el) => el.scrollTop")
                container.evaluate(
                    "(el) => { el.scrollBy(0, Math.max(900, el.clientHeight * 0.9)); }"
                )
                time.sleep(0.5)
                after = container.evaluate("(el) => el.scrollTop")
                return bool(after > before)
            except PlaywrightError:
                pass

        try:
            before = page.evaluate("window.scrollY")
            page.mouse.wheel(0, 1500)
            time.sleep(0.5)
            after = page.evaluate("window.scrollY")
            return bool(after > before)
        except PlaywrightError:
            return False

    def _find_review_scroll_container(self, page: Page) -> Locator | None:
        for selector in REVIEW_SCROLL_CONTAINER_SELECTORS:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 10)
            except PlaywrightError:
                continue

            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if not item.is_visible():
                        continue

                    for review_selector in REVIEW_ITEM_SELECTORS:
                        if item.locator(review_selector).count() > 0:
                            return item
                except PlaywrightError:
                    continue

        return None

    def _pick_first_selector_with_matches(self, root: Page | Locator, selectors: list[str]) -> str | None:
        for selector in selectors:
            locator = root.locator(selector)
            try:
                if locator.count() > 0:
                    return selector
            except PlaywrightError:
                continue
        return None

    def _find_first_visible(
        self,
        root: Page | Locator,
        selectors: list[str],
        *,
        timeout_ms: int,
    ) -> Locator | None:
        end_time = time.time() + timeout_ms / 1000 if timeout_ms > 0 else None

        while True:
            for selector in selectors:
                locator = root.locator(selector)
                try:
                    count = min(locator.count(), 10)
                except PlaywrightError:
                    continue

                for idx in range(count):
                    candidate = locator.nth(idx)
                    try:
                        if candidate.is_visible():
                            return candidate
                    except PlaywrightError:
                        continue

            if end_time is None or time.time() >= end_time:
                return None

            time.sleep(0.25)

    def _extract_text_from_selectors(self, root: Page | Locator, selectors: list[str]) -> str:
        for selector in selectors:
            locator = root.locator(selector)
            try:
                count = min(locator.count(), 10)
            except PlaywrightError:
                continue

            for idx in range(count):
                candidate = locator.nth(idx)
                try:
                    if not candidate.is_visible():
                        continue
                    text = normalize_whitespace(candidate.inner_text(timeout=1_500))
                    if text:
                        return text
                except PlaywrightError:
                    continue

        return ""

    def _maybe_handle_captcha(self, page: Page) -> None:
        if not self._is_captcha_present(page):
            return

        if self.effective_headless:
            raise CaptchaRequiredError(
                "Обнаружена капча или антибот-проверка. "
                "В headless/cloud-режиме продолжить автоматически не удалось."
            )

        self._emit_status("Обнаружена капча. Решите ее в окне браузера. Ожидание...")

        deadline = time.time() + CAPTCHA_WAIT_TIMEOUT_SECONDS
        while time.time() < deadline:
            if not self._is_captcha_present(page):
                self._emit_status("Капча, вероятно, решена. Продолжаем.")
                return
            time.sleep(CAPTCHA_POLL_INTERVAL_SECONDS)

        raise CaptchaRequiredError(
            "Не удалось дождаться решения капчи. Попробуйте повторить действие."
        )

    def _is_captcha_present(self, page: Page) -> bool:
        for selector in CAPTCHA_SELECTORS:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 5)
            except PlaywrightError:
                continue

            for idx in range(count):
                try:
                    if locator.nth(idx).is_visible():
                        return True
                except PlaywrightError:
                    continue

        try:
            page_text = page.locator("body").inner_text(timeout=1_500).lower()
            keywords = ["капча", "captcha", "не робот", "подтвердите"]
            return any(keyword in page_text for keyword in keywords)
        except PlaywrightError:
            return False
