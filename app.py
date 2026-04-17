from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

import streamlit as st

from config import DEFAULT_MAX_REVIEWS
from models import CardMatch, ReviewRecord
from scraper import CaptchaRequiredError, YandexMapsScraper
from utils import (
    decode_uploaded_text_file,
    reviews_to_csv_bytes,
    setup_logging,
    unique_non_empty,
)

st.set_page_config(
    page_title="Yandex Maps ЖК Reviews Scraper",
    page_icon="🏙️",
    layout="wide",
)


def init_state() -> None:
    defaults: dict[str, Any] = {
        "items": [],
        "reviews": [],
        "stats": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def load_queries_to_state(queries: list[str]) -> None:
    st.session_state.items = [
        {
            "id": f"item_{idx}",
            "original_query": query,
            "search_query": query,
            "candidate": None,
            "status": "pending",  # pending | confirmed | excluded
            "last_error": "",
        }
        for idx, query in enumerate(queries, start=1)
    ]
    st.session_state.reviews = []
    st.session_state.stats = None

    for item in st.session_state.items:
        state_key = f"search_query_{item['id']}"
        st.session_state[state_key] = item["search_query"]


def parse_queries(text_input: str, uploaded_text: str) -> list[str]:
    combined: list[str] = []

    if text_input.strip():
        combined.extend(text_input.splitlines())

    if uploaded_text.strip():
        combined.extend(uploaded_text.splitlines())

    return unique_non_empty(combined)


def card_from_dict(data: dict[str, Any]) -> CardMatch:
    return CardMatch(
        residential_complex_input=data["residential_complex_input"],
        search_query=data["search_query"],
        ymaps_card_name=data["ymaps_card_name"],
        ymaps_card_address=data["ymaps_card_address"],
        ymaps_card_url=data["ymaps_card_url"],
    )


def review_from_dict(data: dict[str, Any]) -> ReviewRecord:
    return ReviewRecord(
        residential_complex_input=data["residential_complex_input"],
        ymaps_card_name=data["ymaps_card_name"],
        ymaps_card_address=data["ymaps_card_address"],
        ymaps_card_url=data["ymaps_card_url"],
        review_date=data["review_date"],
        user_name=data["user_name"],
        review_text=data["review_text"],
    )


def build_logger(log_level: str):
    return setup_logging(log_level)


def search_single_item(item_index: int, headless: bool, log_level: str) -> None:
    item = st.session_state.items[item_index]
    item_id = item["id"]
    query_key = f"search_query_{item_id}"
    item["search_query"] = st.session_state.get(query_key, item["search_query"]).strip()

    if not item["search_query"]:
        item["last_error"] = "Поисковый запрос не может быть пустым."
        st.session_state.items[item_index] = item
        return

    logger = build_logger(log_level)
    status_box = st.empty()
    messages: list[str] = []

    def status_callback(message: str) -> None:
        messages.append(message)
        rendered = "\n".join(f"- {msg}" for msg in messages[-8:])
        status_box.info(rendered)

    try:
        with YandexMapsScraper(
            headless=headless,
            logger=logger,
            status_callback=status_callback,
        ) as scraper:
            candidate = scraper.search_card(
                residential_complex_input=item["original_query"],
                search_query=item["search_query"],
            )

        if candidate is None:
            item["candidate"] = None
            item["status"] = "pending"
            item["last_error"] = "Карточка не найдена или не удалось извлечь данные."
            status_box.warning(item["last_error"])
        else:
            item["candidate"] = asdict(candidate)
            item["status"] = "pending"
            item["last_error"] = ""
            status_box.success("Поиск завершен. Проверьте найденную карточку ниже.")

    except CaptchaRequiredError as exc:
        item["last_error"] = str(exc)
        status_box.error(str(exc))
    except Exception as exc:
        logger.exception("Ошибка поиска карточки")
        item["last_error"] = str(exc)
        status_box.error(f"Ошибка поиска: {exc}")

    st.session_state.items[item_index] = item


def search_all_pending(headless: bool, log_level: str) -> None:
    items = st.session_state.items
    total = len(items)

    if total == 0:
        return

    progress = st.progress(0, text="Поиск карточек...")
    status_text = st.empty()

    for idx, item in enumerate(items):
        if item["status"] == "excluded":
            progress.progress((idx + 1) / total, text="Поиск карточек...")
            continue

        status_text.write(f"**Поиск [{idx + 1}/{total}]**: {item['original_query']}")
        search_single_item(idx, headless=headless, log_level=log_level)
        progress.progress((idx + 1) / total, text="Поиск карточек...")

    status_text.success("Поиск по списку завершен.")


def collect_reviews_for_confirmed(headless: bool, log_level: str, limit: int) -> None:
    confirmed_items = [
        item
        for item in st.session_state.items
        if item["status"] == "confirmed" and item["candidate"] is not None
    ]

    if not confirmed_items:
        st.warning("Нет подтвержденных карточек для сбора отзывов.")
        return

    logger = build_logger(log_level)
    progress = st.progress(0, text="Подготовка к сбору отзывов...")
    status_box = st.empty()
    all_reviews: list[ReviewRecord] = []

    messages: list[str] = []

    def status_callback(message: str) -> None:
        messages.append(message)
        rendered = "\n".join(f"- {msg}" for msg in messages[-10:])
        status_box.info(rendered)

    try:
        with YandexMapsScraper(
            headless=headless,
            logger=logger,
            status_callback=status_callback,
        ) as scraper:
            total = len(confirmed_items)

            for idx, item in enumerate(confirmed_items, start=1):
                card = card_from_dict(item["candidate"])
                progress.progress(
                    (idx - 1) / total,
                    text=f"Сбор отзывов [{idx}/{total}]: {card.ymaps_card_name}",
                )

                try:
                    reviews = scraper.collect_reviews(card=card, limit=limit)
                    all_reviews.extend(reviews)
                except CaptchaRequiredError as exc:
                    logger.warning("Капча не решена для '%s': %s", card.ymaps_card_name, exc)
                    st.warning(
                        f"Карточка '{card.ymaps_card_name}' пропущена из-за капчи: {exc}"
                    )
                except Exception as exc:
                    logger.exception("Ошибка при сборе отзывов по '%s'", card.ymaps_card_name)
                    st.warning(
                        f"Ошибка при сборе отзывов по '{card.ymaps_card_name}': {exc}"
                    )

                progress.progress(
                    idx / total,
                    text=f"Сбор отзывов [{idx}/{total}]: {card.ymaps_card_name}",
                )

    except Exception as exc:
        logger.exception("Критическая ошибка при сборе отзывов")
        st.error(f"Критическая ошибка при сборе отзывов: {exc}")
        return

    st.session_state.reviews = [asdict(review) for review in all_reviews]
    st.session_state.stats = {
        "complexes_total": len(st.session_state.items),
        "cards_confirmed": len(confirmed_items),
        "reviews_total": len(all_reviews),
    }
    status_box.success("Сбор отзывов завершен.")


def render_candidate(item: dict[str, Any]) -> None:
    candidate = item["candidate"]
    if candidate is None:
        st.info("Карточка пока не найдена.")
        return

    st.markdown("**Найденная карточка**")
    st.write(f"**Исходный ЖК:** {candidate['residential_complex_input']}")
    st.write(f"**Поисковый запрос:** {candidate['search_query']}")
    st.write(f"**Название карточки:** {candidate['ymaps_card_name'] or '—'}")
    st.write(f"**Адрес:** {candidate['ymaps_card_address'] or '—'}")
    st.write(f"**URL:** {candidate['ymaps_card_url'] or '—'}")


def render_items(headless: bool, log_level: str) -> None:
    if not st.session_state.items:
        st.info("Список ЖК пока не загружен.")
        return

    st.subheader("Карточки для проверки")

    for idx, item in enumerate(st.session_state.items):
        title = f"{idx + 1}. {item['original_query']} — статус: {item['status']}"
        expanded = item["status"] in {"pending"}
        with st.expander(title, expanded=expanded):
            search_key = f"search_query_{item['id']}"
            if search_key not in st.session_state:
                st.session_state[search_key] = item["search_query"]

            st.text_input(
                "Поисковый запрос",
                key=search_key,
                help="Можно изменить запрос и заново выполнить поиск.",
            )

            col1, col2, col3 = st.columns(3)

            if col1.button("Найти / повторить поиск", key=f"search_btn_{item['id']}"):
                search_single_item(idx, headless=headless, log_level=log_level)
                st.rerun()

            if col2.button("Подтвердить карточку", key=f"confirm_btn_{item['id']}"):
                if item["candidate"] is None:
                    st.warning("Сначала выполните поиск и получите карточку.")
                else:
                    item["status"] = "confirmed"
                    item["last_error"] = ""
                    st.session_state.items[idx] = item
                    st.rerun()

            if col3.button("Исключить из списка", key=f"exclude_btn_{item['id']}"):
                item["status"] = "excluded"
                st.session_state.items[idx] = item
                st.rerun()

            if item["last_error"]:
                st.error(item["last_error"])

            render_candidate(item)


def render_confirmed_summary() -> None:
    confirmed = [
        item for item in st.session_state.items
        if item["status"] == "confirmed" and item["candidate"] is not None
    ]

    st.subheader("Подтвержденные карточки")

    if not confirmed:
        st.info("Пока нет подтвержденных карточек.")
        return

    rows = []
    for item in confirmed:
        candidate = item["candidate"]
        rows.append(
            {
                "residential_complex_input": candidate["residential_complex_input"],
                "ymaps_card_name": candidate["ymaps_card_name"],
                "ymaps_card_address": candidate["ymaps_card_address"],
                "ymaps_card_url": candidate["ymaps_card_url"],
            }
        )

    st.dataframe(rows, use_container_width=True)


def render_reviews_result() -> None:
    reviews_data = st.session_state.reviews
    stats = st.session_state.stats

    st.subheader("Результат")

    if stats:
        c1, c2, c3 = st.columns(3)
        c1.metric("ЖК обработано", stats["complexes_total"])
        c2.metric("Карточек подтверждено", stats["cards_confirmed"])
        c3.metric("Отзывов собрано", stats["reviews_total"])

    if not reviews_data:
        st.info("Отзывы пока не собраны.")
        return

    filename = f"ymaps_reviews_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    csv_bytes = reviews_to_csv_bytes([review_from_dict(item) for item in reviews_data])

    st.download_button(
        label="Скачать CSV",
        data=csv_bytes,
        file_name=filename,
        mime="text/csv",
    )

    st.dataframe(reviews_data, use_container_width=True, height=500)


def main() -> None:
    init_state()

    st.title("🏙️ Сбор отзывов по ЖК конкурентов из Яндекс Карт")
    st.caption("Streamlit + Playwright")

    with st.sidebar:
        st.header("Настройки")

        headless = st.checkbox(
            "Headless-режим",
            value=False,
            help=(
                "Для локальной работы лучше выключить, чтобы видеть браузер "
                "и при необходимости пройти капчу вручную."
            ),
        )

        review_limit = st.number_input(
            "Лимит отзывов на карточку",
            min_value=1,
            max_value=300,
            value=DEFAULT_MAX_REVIEWS,
            step=1,
        )

        log_level = st.selectbox(
            "Уровень логирования",
            options=["INFO", "DEBUG", "WARNING", "ERROR"],
            index=0,
        )

        st.markdown("---")
        st.markdown(
            """
            **Рекомендация:**  
            Запускайте локально и с выключенным headless, если возможна капча.
            """
        )

    st.subheader("Шаг 1. Загрузка списка ЖК")

    input_col1, input_col2 = st.columns(2)

    with input_col1:
        text_input = st.text_area(
            "Вставьте список ЖК построчно",
            height=220,
            placeholder="ЖК Clever Park Екатеринбург\nЖК Макаровский Екатеринбург\nЖК Нагорный Екатеринбург",
        )

    with input_col2:
        uploaded_file = st.file_uploader(
            "Или загрузите TXT-файл",
            type=["txt"],
            accept_multiple_files=False,
        )
        uploaded_text = decode_uploaded_text_file(uploaded_file)

    action_col1, action_col2 = st.columns([1, 1])

    if action_col1.button("Загрузить список ЖК", type="primary"):
        queries = parse_queries(text_input=text_input, uploaded_text=uploaded_text)
        if not queries:
            st.warning("Не найдено ни одного валидного запроса.")
        else:
            load_queries_to_state(queries)
            st.success(f"Загружено ЖК: {len(queries)}")
            st.rerun()

    if action_col2.button("Очистить текущий список"):
        st.session_state.items = []
        st.session_state.reviews = []
        st.session_state.stats = None
        st.rerun()

    if st.session_state.items:
        top_col1, top_col2 = st.columns([1, 2])
        if top_col1.button("Искать карточки для всех не исключенных ЖК"):
            search_all_pending(headless=headless, log_level=log_level)
            st.rerun()

        pending_count = sum(1 for item in st.session_state.items if item["status"] == "pending")
        confirmed_count = sum(1 for item in st.session_state.items if item["status"] == "confirmed")
        excluded_count = sum(1 for item in st.session_state.items if item["status"] == "excluded")

        top_col2.info(
            f"Всего: {len(st.session_state.items)} | "
            f"Pending: {pending_count} | "
            f"Confirmed: {confirmed_count} | "
            f"Excluded: {excluded_count}"
        )

    st.markdown("---")
    render_items(headless=headless, log_level=log_level)

    st.markdown("---")
    render_confirmed_summary()

    confirmed_exists = any(
        item["status"] == "confirmed" and item["candidate"] is not None
        for item in st.session_state.items
    )

    if confirmed_exists:
        st.subheader("Шаг 2. Сбор отзывов")
        if st.button("Собрать отзывы по подтвержденным карточкам", type="primary"):
            collect_reviews_for_confirmed(
                headless=headless,
                log_level=log_level,
                limit=int(review_limit),
            )
            st.rerun()

    st.markdown("---")
    render_reviews_result()


if __name__ == "__main__":
    main()
