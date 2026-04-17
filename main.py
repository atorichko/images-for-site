from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from config import DEFAULT_MAX_REVIEWS
from models import CardMatch, ReviewRecord
from scraper import YandexMapsScraper
from utils import (
    ask_yes_no,
    load_queries_from_file,
    prompt_choice,
    prompt_multiline_queries,
    setup_logging,
    write_reviews_csv,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Сбор отзывов конкурентов по ЖК с Яндекс Карт"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Запуск браузера в headless-режиме. По умолчанию браузер видимый.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Путь к итоговому CSV-файлу.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_MAX_REVIEWS,
        help=f"Сколько последних отзывов собирать с одной карточки. По умолчанию {DEFAULT_MAX_REVIEWS}.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Уровень логирования.",
    )
    return parser.parse_args()


def choose_input_source() -> list[str]:
    print("Выберите способ ввода списка ЖК:")
    print("[1] Ввести построчно в консоли")
    print("[2] Указать путь к txt-файлу")

    choice = prompt_choice("Введите 1 или 2", {"1", "2"})

    if choice == "1":
        return prompt_multiline_queries()

    while True:
        file_path = input("Введите путь к txt-файлу: ").strip().strip('"').strip("'")
        try:
            queries = load_queries_from_file(Path(file_path))
            if not queries:
                print("Файл пустой или не содержит валидных строк. Попробуйте снова.")
                continue
            return queries
        except Exception as exc:
            print(f"Не удалось прочитать файл: {exc}")


def display_candidate(candidate: CardMatch) -> None:
    print("\n" + "=" * 80)
    print(f"Исходное название ЖК: {candidate.residential_complex_input}")
    print(f"Поисковый запрос:      {candidate.search_query}")
    print(f"Найденная карточка:    {candidate.ymaps_card_name or '[не найдено]'}")
    print(f"Адрес карточки:        {candidate.ymaps_card_address or '[не найден]'}")
    print(f"Ссылка на карточку:    {candidate.ymaps_card_url or '[не найдена]'}")
    print("=" * 80)


def confirm_cards(scraper: YandexMapsScraper, queries: list[str]) -> list[CardMatch]:
    confirmed: list[CardMatch] = []

    for idx, original_query in enumerate(queries, start=1):
        print(f"\n[{idx}/{len(queries)}] Обработка: {original_query}")
        current_query = original_query

        while True:
            candidate = scraper.search_card(
                residential_complex_input=original_query,
                search_query=current_query,
            )

            if candidate is None:
                print("\nПо данному запросу карточка не найдена или не удалось извлечь данные.")
                print("[2] Изменить запрос и повторить поиск")
                print("[3] Исключить из списка")
                action = prompt_choice("Введите 2 или 3", {"2", "3"})

                if action == "2":
                    current_query = input("Введите новый поисковый запрос: ").strip()
                    if not current_query:
                        print("Пустой запрос недопустим.")
                        current_query = original_query
                    continue

                break

            display_candidate(candidate)

            print("[1] Подтвердить")
            print("[2] Изменить запрос и повторить поиск")
            print("[3] Исключить из списка")
            action = prompt_choice("Введите 1, 2 или 3", {"1", "2", "3"})

            if action == "1":
                confirmed.append(candidate)
                break

            if action == "2":
                current_query = input("Введите новый поисковый запрос: ").strip()
                if not current_query:
                    print("Пустой запрос недопустим. Будет использован исходный запрос.")
                    current_query = original_query
                continue

            if action == "3":
                break

    return confirmed


def print_confirmed_cards(cards: list[CardMatch]) -> None:
    print("\nИтоговый список подтвержденных карточек:")
    if not cards:
        print("— Нет подтвержденных карточек.")
        return

    for idx, card in enumerate(cards, start=1):
        print(f"\n[{idx}] {card.residential_complex_input}")
        print(f"    Карточка: {card.ymaps_card_name}")
        print(f"    Адрес:    {card.ymaps_card_address or '[не найден]'}")
        print(f"    URL:      {card.ymaps_card_url}")


def build_output_path(user_path: Path | None) -> Path:
    if user_path is not None:
        return user_path

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path.cwd() / f"ymaps_reviews_{timestamp}.csv"


def collect_reviews(
    scraper: YandexMapsScraper,
    cards: list[CardMatch],
    limit: int,
) -> list[ReviewRecord]:
    all_reviews: list[ReviewRecord] = []

    for idx, card in enumerate(cards, start=1):
        print(f"\nСбор отзывов [{idx}/{len(cards)}]: {card.ymaps_card_name}")
        reviews = scraper.collect_reviews(card=card, limit=limit)
        all_reviews.extend(reviews)
        print(f"Собрано отзывов по карточке: {len(reviews)}")

    return all_reviews


def main() -> int:
    args = parse_args()
    logger = setup_logging(args.log_level)

    print("=== Сбор отзывов по ЖК конкурентов из Яндекс Карт ===\n")
    queries = choose_input_source()

    if not queries:
        print("Список ЖК пуст. Завершение.")
        return 1

    print(f"\nПолучено ЖК: {len(queries)}")

    confirmed_cards: list[CardMatch] = []
    all_reviews: list[ReviewRecord] = []

    try:
        with YandexMapsScraper(headless=args.headless, logger=logger) as scraper:
            confirmed_cards = confirm_cards(scraper, queries)
            print_confirmed_cards(confirmed_cards)

            if not confirmed_cards:
                print("\nНет подтвержденных карточек. Завершение без выгрузки.")
                return 0

            if not ask_yes_no("\nНачать сбор отзывов? [y/n]: "):
                print("Операция отменена пользователем.")
                return 0

            all_reviews = collect_reviews(scraper, confirmed_cards, args.limit)

    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except Exception as exc:
        logger.exception("Критическая ошибка: %s", exc)
        print(f"\nПроизошла критическая ошибка: {exc}")
        return 1

    output_path = build_output_path(args.output)
    write_reviews_csv(all_reviews, output_path)

    print("\n=== Готово ===")
    print(f"CSV сохранен: {output_path}")
    print(f"ЖК введено: {len(queries)}")
    print(f"Карточек подтверждено: {len(confirmed_cards)}")
    print(f"Отзывов собрано всего: {len(all_reviews)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
