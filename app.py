from __future__ import annotations

from pathlib import Path
import os
from dataclasses import asdict
from datetime import datetime
from typing import Any

import streamlit as st

st.sidebar.caption(
    f"POLZA_AI_API_KEY на сервере: {'найден' if os.environ.get('POLZA_AI_API_KEY') else 'не найден'}"
)

from ai_analyzer import ReviewAIAnalyzer
from config import DEFAULT_AI_MODEL, DEFAULT_MAX_REVIEWS
from models import CardMatch, ReviewRecord
from scraper import CaptchaRequiredError, YandexMapsScraper
from utils import (
    decode_uploaded_text_file,
    reviews_to_xlsx_bytes,
    setup_logging,
    unique_non_empty,
)



st.set_page_config(
    page_title="Yandex Maps ЖК Reviews Scraper",
    page_icon="🏙️",
    layout="wide",
)

STATE_SEARCH_ITEMS = "search_items"
STATE_REVIEW_ROWS = "review_rows"
STATE_RUN_STATS = "run_stats"


def is_streamlit_cloud() -> bool:
    return (
        bool(os.environ.get("STREAMLIT_SHARING_MODE"))
        or bool(os.environ.get("STREAMLIT_CLOUD"))
        or os.environ.get("HOME") == "/home/appuser"
        or Path("/mount/src").exists()
    )


def init_state() -> None:
    defaults: dict[str, Any] = {
        STATE_SEARCH_ITEMS: [],
        STATE_REVIEW_ROWS: [],
        STATE_RUN_STATS: None,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def get_search_items() -> list[dict[str, Any]]:
    return st.session_state[STATE_SEARCH_ITEMS]


def set_search_items(items: list[dict[str, Any]]) -> None:
    st.session_state[STATE_SEARCH_ITEMS] = items


def parse_queries(text_input: str, uploaded_text: str) -> list[str]:
    raw_lines: list[str] = []

    if text_input.strip():
        raw_lines.extend(text_input.splitlines())

    if uploaded_text.strip():
        raw_lines.extend(uploaded_text.splitlines())

    return unique_non_empty(raw_lines)


def load_queries_to_state(queries: list[str]) -> None:
    search_items = [
        {
            "id": f"rc_{idx}",
            "original_query": query,
            "search_query": query,
            "candidate": None,
            "status": "pending",  # pending | confirmed | excluded
            "last_error": "",
        }
        for idx, query in enumerate(queries, start=1)
    ]

    set_search_items(search_items)
    st.session_state[STATE_REVIEW_ROWS] = []
    st.session_state[STATE_RUN_STATS] = None

    for item in search_items:
        state_key = f"search_query_{item['id']}"
        st.session_state[state_key] = item["search_query"]


def clear_state() -> None:
    for item in get_search_items():
        key = f"search_query_{item['id']}"
        if key in st.session_state:
            del st.session_state[key]

    st.session_state[STATE_SEARCH_ITEMS] = []
    st.session_state[STATE_REVIEW_ROWS] = []
    st.session_state[STATE_RUN_STATS] = None


def build_logger(log_level: str):
    return setup_logging(log_level)

def get_default_polza_api_key() -> str:
    env_key = os.environ.get("POLZA_AI_API_KEY", "").strip()
    if env_key:
        return env_key

    try:
        secret_key = str(st.secrets.get("POLZA_AI_API_KEY", "")).strip()
        return secret_key
    except Exception:
        return ""

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
        ai_review_check=data.get("ai_review_check", ""),
        ai_review_reason=data.get("ai_review_reason", ""),
        ai_review_confidence=data.get("ai_review_confidence"),
    )



def get_confirmed_items() -> list[dict[str, Any]]:
    return [
        item
        for item in get_search_items()
        if item["status"] == "confirmed" and item["candidate"] is not None
    ]


def search_single_item(item_index: int, headless: bool, log_level: str) -> None:
    search_items = get_search_items()
    item = search_items[item_index]
    item_id = item["id"]
    query_state_key = f"search_query_{item_id}"

    item["search_query"] = st.session_state.get(query_state_key, item["search_query"]).strip()

    if not item["search_query"]:
        item["last_error"] = "Поисковый запрос не может быть пустым."
        search_items[item_index] = item
        set_search_items(search_items)
        return

    logger = build_logger(log_level)
    status_placeholder = st.empty()
    messages: list[str] = []

    def status_callback(message: str) -> None:
        messages.append(message)
        rendered = "\n".join(f"- {msg}" for msg in messages[-8:])
        status_placeholder.info(rendered)

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
            status_placeholder.warning(item["last_error"])
        else:
            item["candidate"] = asdict(candidate)
            item["status"] = "pending"
            item["last_error"] = ""
            status_placeholder.success("Поиск завершен. Проверьте найденную карточку.")

    except CaptchaRequiredError as exc:
        item["candidate"] = None
        item["last_error"] = str(exc)
        status_placeholder.error(str(exc))

        if is_streamlit_cloud():
            st.warning(
                "Приложение запущено на Streamlit Cloud. "
                "Если Яндекс показал капчу/антибот-проверку, "
                "в облаке такой сценарий обычно не обрабатывается стабильно. "
                "Для надежной работы лучше запускать приложение локально."
            )

    except Exception as exc:
        logger.exception("Ошибка поиска карточки")
        item["last_error"] = str(exc)
        status_placeholder.error(f"Ошибка поиска: {exc}")

    search_items[item_index] = item
    set_search_items(search_items)


def search_all_non_excluded(headless: bool, log_level: str) -> None:
    search_items = get_search_items()
    total = len(search_items)

    if total == 0:
        return

    progress = st.progress(0, text="Поиск карточек...")
    progress_text = st.empty()

    for idx, item in enumerate(search_items):
        if item["status"] == "excluded":
            progress.progress((idx + 1) / total, text="Поиск карточек...")
            continue

        progress_text.write(f"**Поиск [{idx + 1}/{total}]**: {item['original_query']}")
        search_single_item(idx, headless=headless, log_level=log_level)
        progress.progress((idx + 1) / total, text="Поиск карточек...")

    progress_text.success("Поиск по списку завершен.")


def collect_reviews_for_confirmed(
    headless: bool,
    log_level: str,
    limit: int,
    analyze_with_ai: bool,
    polza_api_key: str,
    ai_model: str,
) -> None:
    confirmed_items = get_confirmed_items()

    if not confirmed_items:
        st.warning("Нет подтвержденных карточек для сбора отзывов.")
        return

    logger = build_logger(log_level)
    progress = st.progress(0, text="Подготовка к сбору отзывов...")
    status_placeholder = st.empty()
    all_reviews: list[ReviewRecord] = []
    messages: list[str] = []

    def status_callback(message: str) -> None:
        messages.append(message)
        rendered = "\n".join(f"- {msg}" for msg in messages[-10:])
        status_placeholder.info(rendered)

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
                    text=f"Сбор отзывов [{idx}/{total}]: {card.ymaps_card_name or card.ymaps_card_url}",
                )

                try:
                    reviews = scraper.collect_reviews(card=card, limit=limit)
                    all_reviews.extend(reviews)
                except CaptchaRequiredError as exc:
                    logger.warning("Капча по карточке '%s': %s", card.ymaps_card_name, exc)
                    st.warning(
                        f"Карточка '{card.ymaps_card_name or card.ymaps_card_url}' "
                        f"пропущена из-за капчи/антибота: {exc}"
                    )
                except Exception as exc:
                    logger.exception("Ошибка при сборе отзывов по '%s'", card.ymaps_card_name)
                    st.warning(
                        f"Ошибка при сборе отзывов по "
                        f"'{card.ymaps_card_name or card.ymaps_card_url}': {exc}"
                    )

                progress.progress(
                    idx / total,
                    text=f"Сбор отзывов [{idx}/{total}]: {card.ymaps_card_name or card.ymaps_card_url}",
                )

    except Exception as exc:
        logger.exception("Критическая ошибка при сборе отзывов")
        st.error(f"Критическая ошибка при сборе отзывов: {exc}")
        return

    if analyze_with_ai and all_reviews:
        if not polza_api_key.strip():
            st.warning(
                "AI-анализ включен, но ключ Polza.ai не указан. "
                "Отзывы будут выгружены без AI-разметки."
            )
        else:
            analyzer = ReviewAIAnalyzer(
                api_key=polza_api_key.strip(),
                model=ai_model.strip() or DEFAULT_AI_MODEL,
                logger=logger,
            )

            analysis_progress = st.progress(0, text="AI-анализ отзывов...")

            def ai_progress_callback(done: int, total: int, review: ReviewRecord) -> None:
                title = review.ymaps_card_name or review.residential_complex_input or "—"
                analysis_progress.progress(
                    done / total,
                    text=f"AI-анализ [{done}/{total}]: {title}",
                )

            try:
                all_reviews = analyzer.analyze_reviews(
                    all_reviews,
                    progress_callback=ai_progress_callback,
                )
                status_placeholder.success("Сбор отзывов и AI-анализ завершены.")
            except Exception as exc:
                logger.exception("Ошибка AI-анализа отзывов")
                st.warning(f"Отзывы собраны, но AI-анализ завершился ошибкой: {exc}")
            finally:
                analysis_progress.empty()
    else:
        status_placeholder.success("Сбор отзывов завершен.")

    st.session_state[STATE_REVIEW_ROWS] = [asdict(review) for review in all_reviews]
    st.session_state[STATE_RUN_STATS] = {
        "complexes_total": len(get_search_items()),
        "cards_confirmed": len(confirmed_items),
        "reviews_total": len(all_reviews),
        "reviews_ai_checked": sum(
            1 for review in all_reviews if review.ai_review_check and review.ai_review_check != "не определено"
        ),
        "reviews_suspicious": sum(
            1 for review in all_reviews if review.ai_review_check in {"подозрительный", "искусственный"}
        ),
    }


def render_candidate(item: dict[str, Any]) -> None:
    candidate = item["candidate"]

    if candidate is None:
        st.info("Карточка пока не найдена.")
        return

    st.markdown("**Найденная карточка**")
    st.write(f"**Исходный ЖК:** {candidate.get('residential_complex_input', '—')}")
    st.write(f"**Поисковый запрос:** {candidate.get('search_query', '—')}")
    st.write(f"**Название карточки:** {candidate.get('ymaps_card_name', '') or '—'}")
    st.write(f"**Адрес:** {candidate.get('ymaps_card_address', '') or '—'}")
    st.write(f"**URL:** {candidate.get('ymaps_card_url', '') or '—'}")


def render_search_items(headless: bool, log_level: str) -> None:
    search_items = get_search_items()

    if not search_items:
        st.info("Список ЖК пока не загружен.")
        return

    st.subheader("Карточки для проверки")

    for idx, item in enumerate(search_items):
        expander_title = f"{idx + 1}. {item['original_query']} — статус: {item['status']}"
        expanded = item["status"] == "pending"

        with st.expander(expander_title, expanded=expanded):
            query_state_key = f"search_query_{item['id']}"
            if query_state_key not in st.session_state:
                st.session_state[query_state_key] = item["search_query"]

            st.text_input(
                "Поисковый запрос",
                key=query_state_key,
                help="Можно изменить запрос и заново выполнить поиск.",
            )

            col1, col2, col3 = st.columns(3)

            if col1.button("Найти / повторить поиск", key=f"search_btn_{item['id']}"):
                search_single_item(idx, headless=headless, log_level=log_level)
                st.rerun()

            if col2.button("Подтвердить карточку", key=f"confirm_btn_{item['id']}"):
                latest_items = get_search_items()
                latest_item = latest_items[idx]

                if latest_item["candidate"] is None:
                    st.warning("Сначала выполните поиск и получите карточку.")
                else:
                    latest_item["status"] = "confirmed"
                    latest_item["last_error"] = ""
                    latest_items[idx] = latest_item
                    set_search_items(latest_items)
                    st.rerun()

            if col3.button("Исключить из списка", key=f"exclude_btn_{item['id']}"):
                latest_items = get_search_items()
                latest_item = latest_items[idx]
                latest_item["status"] = "excluded"
                latest_items[idx] = latest_item
                set_search_items(latest_items)
                st.rerun()

            if item["last_error"]:
                st.error(item["last_error"])

            render_candidate(item)


def render_confirmed_summary() -> None:
    confirmed_items = get_confirmed_items()

    st.subheader("Подтвержденные карточки")

    if not confirmed_items:
        st.info("Пока нет подтвержденных карточек.")
        return

    rows: list[dict[str, Any]] = []
    for item in confirmed_items:
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


def render_results() -> None:
    review_rows = st.session_state[STATE_REVIEW_ROWS]
    run_stats = st.session_state[STATE_RUN_STATS]

    st.subheader("Результат")

    if run_stats:
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("ЖК обработано", run_stats["complexes_total"])
        col2.metric("Карточек подтверждено", run_stats["cards_confirmed"])
        col3.metric("Отзывов собрано", run_stats["reviews_total"])
        col4.metric("AI-проверено", run_stats.get("reviews_ai_checked", 0))
        col5.metric("Подозрительных", run_stats.get("reviews_suspicious", 0))

    if not review_rows:
        st.info("Отзывы пока не собраны.")
        return

    review_objects = [review_from_dict(row) for row in review_rows]
    xlsx_bytes = reviews_to_xlsx_bytes(review_objects)
    filename = f"ymaps_reviews_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    st.download_button(
        label="Скачать XLSX",
        data=xlsx_bytes,
        file_name=filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.dataframe(review_rows, use_container_width=True, height=500)




def render_environment_notice(cloud_mode: bool) -> None:
    if cloud_mode:
        st.warning(
            "Приложение запущено на Streamlit Cloud. "
            "Браузер работает только в headless-режиме. "
            "Яндекс Карты могут показывать капчу или антибот-проверку, "
            "а в облаке это часто делает сбор нестабильным."
        )
        st.info(
            "Если поиск/сбор регулярно ломается, рекомендуется запускать это приложение локально."
        )


def render_sidebar() -> tuple[bool, int, str, bool, str]:
    cloud_mode = is_streamlit_cloud()

    with st.sidebar:
        st.header("Настройки")

        if cloud_mode:
            headless = True
            st.checkbox(
                "Headless-режим",
                value=True,
                disabled=True,
                help="На Streamlit Cloud headless включен принудительно.",
            )
            st.caption("На Streamlit Cloud headed-режим недоступен.")
        else:
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
            max_value=50,
            value=DEFAULT_MAX_REVIEWS,
            step=1,
            help="Для снижения риска капчи лимит жестко ограничен 50 отзывами.",
        )

        log_level = st.selectbox(
            "Уровень логирования",
            options=["INFO", "DEBUG", "WARNING", "ERROR"],
            index=0,
        )

        st.markdown("---")
        st.subheader("AI-анализ отзывов")

        if "polza_ai_model_input" not in st.session_state:
            st.session_state["polza_ai_model_input"] = DEFAULT_AI_MODEL

        server_polza_api_key = get_default_polza_api_key()
        server_key_configured = bool(server_polza_api_key)

        analyze_with_ai = st.checkbox(
            "Проверять отзывы через Polza.ai",
            value=server_key_configured,
            disabled=not server_key_configured,
            help="Ключ берется с сервера, пользователю вводить его не нужно.",
        )

        ai_model = st.text_input(
            "Модель Polza.ai",
            key="polza_ai_model_input",
            disabled=not analyze_with_ai,
        )

        if server_key_configured:
            st.success("Polza.ai API key загружен с сервера.")
        else:
            st.warning("Polza.ai API key не найден на сервере. AI-анализ недоступен.")

        st.markdown("---")

        if cloud_mode:
            st.markdown(
                """
                **Режим запуска:** Streamlit Cloud  
                **Headless:** принудительно включен  
                **Риск:** капча/антибот Яндекса  
                **Рекомендация:**  
                Для снижения риска капчи сбор ограничен последними 50 отзывами.
                """
            )
        else:
            st.markdown(
                """
                **Рекомендация:**  
                Если возможна капча, запускайте локально и с выключенным headless.
                """
            )

    return headless, int(review_limit), log_level, analyze_with_ai, ai_model



def render_input_section() -> None:
    st.subheader("Шаг 1. Загрузка списка ЖК")

    col1, col2 = st.columns(2)

    with col1:
        text_input = st.text_area(
            "Вставьте список ЖК построчно в формате ""ЖК Название Город""",
            height=220,
            placeholder=(
                "ЖК Clever Park Екатеринбург\n"
                "ЖК Макаровский Екатеринбург\n"
                "ЖК Нагорный Екатеринбург"
            ),
        )

    with col2:
        uploaded_file = st.file_uploader(
            "Или загрузите TXT-файл",
            type=["txt"],
            accept_multiple_files=False,
        )
        uploaded_text = decode_uploaded_text_file(uploaded_file)

    action_col1, action_col2 = st.columns(2)

    if action_col1.button("Загрузить список ЖК", type="primary"):
        queries = parse_queries(text_input=text_input, uploaded_text=uploaded_text)

        if not queries:
            st.warning("Не найдено ни одного валидного запроса.")
        else:
            load_queries_to_state(queries)
            st.success(f"Загружено ЖК: {len(queries)}")
            st.rerun()

    if action_col2.button("Очистить текущий список"):
        clear_state()
        st.rerun()


def render_top_actions(headless: bool, log_level: str) -> None:
    search_items = get_search_items()

    if not search_items:
        return

    left_col, right_col = st.columns([1, 2])

    if left_col.button("Искать карточки для всех не исключенных ЖК"):
        search_all_non_excluded(headless=headless, log_level=log_level)
        st.rerun()

    pending_count = sum(1 for item in search_items if item["status"] == "pending")
    confirmed_count = sum(1 for item in search_items if item["status"] == "confirmed")
    excluded_count = sum(1 for item in search_items if item["status"] == "excluded")

    right_col.info(
        f"Всего: {len(search_items)} | "
        f"Pending: {pending_count} | "
        f"Confirmed: {confirmed_count} | "
        f"Excluded: {excluded_count}"
    )


def main() -> None:
    init_state()

    cloud_mode = is_streamlit_cloud()

    st.title("🏙️ Сбор отзывов по ЖК конкурентов из Яндекс Карт")
    st.caption("Streamlit + Playwright")

    render_environment_notice(cloud_mode)
    headless, review_limit, log_level, analyze_with_ai, ai_model = render_sidebar()

    polza_api_key = get_default_polza_api_key()

    render_input_section()
    render_top_actions(headless=headless, log_level=log_level)

    st.markdown("---")
    render_search_items(headless=headless, log_level=log_level)

    st.markdown("---")
    render_confirmed_summary()

    if get_confirmed_items():
        st.subheader("Шаг 2. Сбор отзывов")
        button_label = (
            "Собрать отзывы и выполнить AI-анализ"
            if analyze_with_ai
            else "Собрать отзывы по подтвержденным карточкам"
        )

        if st.button(button_label, type="primary"):
            collect_reviews_for_confirmed(
                headless=headless,
                log_level=log_level,
                limit=review_limit,
                analyze_with_ai=analyze_with_ai,
                polza_api_key=polza_api_key,
                ai_model=ai_model,
            )
            st.rerun()

    st.markdown("---")
    render_results()


if __name__ == "__main__":
    main()
