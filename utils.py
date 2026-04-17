from __future__ import annotations

import csv
import logging
import random
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from models import ReviewRecord

T = TypeVar("T")


def setup_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("ymaps_reviews")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger.propagate = False
    return logger


def sleep_random(min_seconds: float, max_seconds: float) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def normalize_whitespace(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(value.replace("\xa0", " ").split()).strip()


def prompt_choice(prompt: str, choices: set[str]) -> str:
    while True:
        value = input(f"{prompt}: ").strip()
        if value in choices:
            return value
        print(f"Некорректный ввод. Допустимые значения: {', '.join(sorted(choices))}")


def ask_yes_no(prompt: str) -> bool:
    while True:
        value = input(prompt).strip().lower()
        if value in {"y", "yes", "д", "да"}:
            return True
        if value in {"n", "no", "н", "нет"}:
            return False
        print("Введите y/n.")


def prompt_multiline_queries() -> list[str]:
    print(
        "Введите список ЖК построчно. "
        "Чтобы завершить ввод, отправьте пустую строку два раза подряд."
    )

    lines: list[str] = []
    empty_streak = 0

    while True:
        line = input().strip()
        if not line:
            empty_streak += 1
            if empty_streak >= 2:
                break
            continue

        empty_streak = 0
        lines.append(line)

    return unique_non_empty(lines)


def unique_non_empty(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []

    for item in items:
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)

    return result


def load_queries_from_file(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")

    text = path.read_text(encoding="utf-8")
    return unique_non_empty(text.splitlines())


def retry_call(
    func: Callable[[], T],
    *,
    attempts: int,
    delay_seconds: float,
    backoff: float,
    logger: logging.Logger | None = None,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    current_delay = delay_seconds
    last_exc: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return func()
        except retry_exceptions as exc:
            last_exc = exc
            if attempt >= attempts:
                break

            if logger is not None:
                logger.warning(
                    "Попытка %s/%s завершилась ошибкой: %s. Повтор через %.1f сек.",
                    attempt,
                    attempts,
                    exc,
                    current_delay,
                )
            time.sleep(current_delay)
            current_delay *= backoff

    assert last_exc is not None
    raise last_exc


def write_reviews_csv(records: list[ReviewRecord], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "residential_complex_input",
        "ymaps_card_name",
        "ymaps_card_address",
        "ymaps_card_url",
        "review_date",
        "user_name",
        "review_text",
    ]

    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for record in records:
            writer.writerow(asdict(record))

