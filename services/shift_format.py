"""Единый текстовый вид смены: для карточек в боте, коротких уведомлений и сайта."""
from __future__ import annotations

import html
from datetime import date, datetime

from config import get_minsk_now

DAYS_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
DAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def time_range(v) -> str:
    return f"{v.start_time.strftime('%H:%M')}–{v.end_time.strftime('%H:%M')}"


def date_short(d: date) -> str:
    return f"{DAYS_SHORT[d.weekday()]} {d.strftime('%d.%m')}"


def day_word(d: date, today: date | None = None) -> str:
    today = today or get_minsk_now().date()
    delta = (d - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    return date_short(d)


def pay_text(amount: int | None, unit: str | None) -> str | None:
    if not amount:
        return None
    unit = (unit or "BYN").lower()
    if "/час" in unit:
        return f"{amount} BYN/час"
    if "/смен" in unit:
        return f"{amount} BYN за смену"
    return f"{amount} BYN"


def shift_hours(v) -> float:
    s = v.start_time.hour * 60 + v.start_time.minute
    e = v.end_time.hour * 60 + v.end_time.minute
    return round(max(0, e - s) / 60, 1)


def relay_text(vacancies: list, company: str, title: str | None, first_time: bool = False) -> str:
    """Максимально коротко: что, когда, сколько. Три строки."""
    v = vacancies[0]
    if len(vacancies) == 1:
        when = day_word(v.target_date)
    else:
        when = ", ".join(date_short(x.target_date) for x in vacancies[:3])
        if len(vacancies) > 3:
            when += f" +{len(vacancies) - 3}"
    parts = [when, time_range(v)]
    pay = pay_text(v.pay_amount, v.pay_unit)
    if pay:
        parts.append(pay)
    who = html.escape(title or "Смена")
    if company:
        who += f" · {html.escape(company)}"
    text = f"💼 <b>Смена в твоё окно</b>\n{' · '.join(parts)}\n{who}"
    if first_time:
        text += "\n<i>Подбираю только то, что не пересекается с парами</i>"
    return text


def is_upcoming(v, now: datetime | None = None) -> bool:
    now = now or get_minsk_now()
    if v.target_date > now.date():
        return True
    return v.target_date == now.date() and v.start_time > now.time()
