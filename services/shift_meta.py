"""Короткое название, категория и место смены из свободного текста работодателя."""
from __future__ import annotations

import re

# (корень слова, как показать, категория)
_ROLES: list[tuple[str, str, str]] = [
    ("бариста", "Бариста", "food"),
    ("официант", "Официант", "food"),
    ("бармен", "Бармен", "food"),
    ("хостес", "Хостес", "food"),
    ("повар", "Помощник повара", "food"),
    ("кухн", "Работа на кухне", "food"),
    ("раздач", "Раздача", "food"),
    ("посудомо", "Мойщик посуды", "food"),
    ("кассир", "Кассир", "retail"),
    ("продав", "Продавец", "retail"),
    ("консультант", "Консультант", "retail"),
    ("мерчендайз", "Мерчендайзер", "retail"),
    ("выкладк", "Выкладка товара", "retail"),
    ("инвентаризац", "Инвентаризация", "retail"),
    ("курьер", "Курьер", "delivery"),
    ("доставк", "Доставка", "delivery"),
    ("промоутер", "Промоутер", "events"),
    ("листовк", "Раздача листовок", "events"),
    ("аниматор", "Аниматор", "events"),
    ("гардероб", "Гардеробщик", "events"),
    ("волонт", "Волонтёр", "events"),
    ("мероприят", "Помощь на мероприятии", "events"),
    ("грузчик", "Грузчик", "warehouse"),
    ("комплектовщ", "Комплектовщик", "warehouse"),
    ("сборщик", "Сборщик заказов", "warehouse"),
    ("упаковщ", "Упаковщик", "warehouse"),
    ("склад", "Помощник на складе", "warehouse"),
    ("оператор", "Оператор", "office"),
    ("администратор", "Администратор", "office"),
    ("ассистент", "Ассистент", "office"),
    ("репетитор", "Репетитор", "other"),
]

CATEGORY_TITLES = {
    "retail": "Магазины",
    "food": "Кафе и еда",
    "delivery": "Доставка",
    "events": "Мероприятия",
    "warehouse": "Склад",
    "office": "Офис",
    "other": "Другое",
}

_NOISE = re.compile(
    r"\b(ищем|ищу|нужен|нужна|нужны|нужно|требуется|требуются|срочно|в\s+кофейню|в\s+магазин)\b",
    re.IGNORECASE,
)
_TIMEISH = re.compile(r"\d{1,2}[:.]\d{2}|\d{1,2}\s*(?:-|–|—|до)\s*\d{1,2}|\d+\s*(?:руб|byn|р\b)", re.IGNORECASE)

_PLACE_PATTERNS = [
    re.compile(r"\b((?:ТЦ|ТРЦ|ТРК)\s+[«\"]?[А-ЯЁA-Z][\w\-«»\" ]{1,25})", re.UNICODE),
    re.compile(r"\b((?:м\.|метро)\s*[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)"),
    re.compile(r"\b((?:ул\.|улица|пр\.|пр-т|проспект)\s*[А-ЯЁ][\w\-]+(?:\s*\d+[а-я]?)?)"),
    re.compile(r"\b(на\s+[А-ЯЁ][а-яё]{3,}(?:\s+[А-ЯЁ][а-яё]+)?)"),
]


def detect_role(text: str) -> tuple[str | None, str]:
    low = (text or "").lower()
    best: tuple[int, str, str] | None = None
    for stem, title, cat in _ROLES:
        pos = low.find(stem)
        if pos != -1 and (best is None or pos < best[0]):
            best = (pos, title, cat)
    if best:
        return best[1], best[2]
    return None, "other"


def detect_place(text: str) -> str | None:
    for rx in _PLACE_PATTERNS:
        m = rx.search(text or "")
        if m:
            place = m.group(1).strip(" ,.;:«»\"")
            if 3 <= len(place) <= 40:
                return place
    return None


def make_title(text: str) -> str:
    role, _ = detect_role(text)
    if role:
        return role
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    first = _NOISE.sub(" ", first)
    first = _TIMEISH.split(first)[0]
    first = re.sub(r"\s+", " ", first).strip(" ,.;:-—")
    words = first.split(" ")[:5]
    title = " ".join(words).strip()
    if len(title) < 3:
        return "Смена"
    return title[:1].upper() + title[1:60]


def describe(text: str) -> dict:
    role, category = detect_role(text)
    return {
        "title": role or make_title(text),
        "category": category,
        "place": detect_place(text),
    }
