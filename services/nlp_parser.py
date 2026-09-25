import re
from datetime import date, time, datetime, timedelta
from typing import Any
from config import get_minsk_now

# Словарь дней недели и частых сокращений
DAY_ALIASES = {
    "пн": 0, "пон": 0, "понедельник": 0, "понедельника": 0, "понедельнику": 0,
    "вт": 1, "втор": 1, "вторник": 1, "вторника": 1, "вторнику": 1,
    "ср": 2, "сред": 2, "среда": 2, "среду": 2, "среды": 2,
    "чт": 3, "чет": 3, "четверг": 3, "четверга": 3, "четвергу": 3,
    "пт": 4, "пят": 4, "пятница": 4, "пятницу": 4, "пятницы": 4,
    "сб": 5, "суб": 5, "суббота": 5, "субботу": 5, "субботы": 5,
    "вс": 6, "вск": 6, "воскресенье": 6, "воскресенья": 6, "воскресенью": 6,
}

# Регулярка для ДЕНЯГ: 40 руб, 45 bun, 50 byn, 12 р/час
MONEY_REGEX = re.compile(
    r"(?P<amount>\d+[\d\s.,]*)\s*(?P<currency>руб(?:л[ейяь]+)?|byn|bun|р\b|бел(?:\.|\s*)?руб)(?:\s*(?:/|за)?\s*(?P<unit>смен[ауеы]|час[а]?))?",
    re.IGNORECASE,
)

# Регулярка для ВРЕМЕНИ: "с 17 до 21", "17:00 - 21:30", "с 12:30 до 16:00"
TIME_RANGE_REGEX = re.compile(
    r"(?:с|c)?\s*(\b[0-2]?\d(?::[0-5]\d)?)\s*(?:до|-|–|—)\s*([0-2]?\d(?::[0-5]\d)?)\b",
    re.IGNORECASE,
)

# Регулярка для ДАТ: 26.09 или 26.09.2026
DATE_REGEX = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")


def _parse_time_str(t_str: str) -> time | None:
    t_str = t_str.strip()
    try:
        if ":" in t_str:
            h, m = map(int, t_str.split(":"))
        else:
            h, m = int(t_str), 0
        if 0 <= h <= 23 and 0 <= m <= 59:
            return time(hour=h, minute=m)
    except ValueError:
        pass
    return None


def parse_vacancy_card(text: str) -> dict[str, Any] | None:
    working_text = text.lower()
    now_dt = get_minsk_now()
    today_date = now_dt.date()

    # 1. Извлекаем оплату и маскируем её плейсхолдером [PAYMENT]
    pay_amount = None
    pay_unit = "BYN"

    money_match = MONEY_REGEX.search(working_text)
    if money_match:
        raw_val = money_match.group("amount").replace(" ", "").replace(",", ".")
        try:
            pay_amount = int(float(raw_val))
            unit = money_match.group("unit")
            pay_unit = f"BYN/{unit}" if unit else "BYN"
        except ValueError:
            pay_amount = None

        # Вырезаем найденную сумму из текста, чтобы она не мешала парсингу времени
        working_text = (
            working_text[: money_match.start()]
            + " [PAYMENT] "
            + working_text[money_match.end() :]
        )

    # 2. Извлекаем интервал времени смены
    time_match = TIME_RANGE_REGEX.search(working_text)
    if not time_match:
        return None  # Без времени смена не может быть сопоставлена с расписанием

    start_time = _parse_time_str(time_match.group(1))
    end_time = _parse_time_str(time_match.group(2))

    if not start_time or not end_time or start_time >= end_time:
        return None

    # 3. Извлекаем дату и день недели
    target_date = today_date
    target_day = None

    date_match = DATE_REGEX.search(working_text)
    if date_match:
        d = int(date_match.group(1))
        m = int(date_match.group(2))
        y = int(date_match.group(3)) if date_match.group(3) else today_date.year
        if y < 100:
            y += 2000
        try:
            target_date = date(y, m, d)
            target_day = target_date.weekday()
        except ValueError:
            pass

    if target_day is None:
        if "послезавтра" in working_text:
            target_date = today_date + timedelta(days=2)
            target_day = target_date.weekday()
        elif "завтра" in working_text:
            target_date = today_date + timedelta(days=1)
            target_day = target_date.weekday()
        elif "сегодня" in working_text:
            target_date = today_date
            target_day = target_date.weekday()
        else:
            # Поиск по дням недели
            for alias, day_idx in DAY_ALIASES.items():
                if re.search(rf"\b{alias}\b", working_text):
                    target_day = day_idx
                    days_ahead = (day_idx - today_date.weekday()) % 7
                    # Если день сегодня, но смена уже началась или скоро начнется — переносим на след. неделю
                    if days_ahead == 0 and now_dt.hour >= start_time.hour:
                        days_ahead = 7
                    target_date = today_date + timedelta(days=days_ahead)
                    break

    # Если день не был явно указан — ставим сегодня или завтра
    if target_day is None:
        if now_dt.hour >= start_time.hour:
            target_date = today_date + timedelta(days=1)
        else:
            target_date = today_date
        target_day = target_date.weekday()

    return {
        "raw_text": text.strip(),
        "target_date": target_date,
        "day_of_week": target_day,
        "start_time": start_time,
        "end_time": end_time,
        "pay_amount": pay_amount,
        "pay_unit": pay_unit,
    }


def parse_user_busy_input(text: str) -> dict[str, Any] | None:
    working_text = text.lower().strip()
    today_date = get_minsk_now().date()

    # 1. Извлекаем время
    time_match = TIME_RANGE_REGEX.search(working_text)
    if not time_match:
        return None

    start_time = _parse_time_str(time_match.group(1))
    end_time = _parse_time_str(time_match.group(2))

    if not start_time or not end_time or start_time >= end_time:
        return None

    # Очищаем время из строки для поиска названия
    working_text_clean = (
        working_text[: time_match.start()] + " " + working_text[time_match.end() :]
    )

    # 2. Извлекаем день недели
    target_day = None

    if "завтра" in working_text_clean:
        target_day = (today_date + timedelta(days=1)).weekday()
        working_text_clean = working_text_clean.replace("завтра", "")
    elif "сегодня" in working_text_clean:
        target_day = today_date.weekday()
        working_text_clean = working_text_clean.replace("сегодня", "")
    else:
        for alias, day_idx in DAY_ALIASES.items():
            if re.search(rf"\b{alias}\b", working_text_clean):
                target_day = day_idx
                working_text_clean = re.sub(
                    rf"\b{alias}\b", "", working_text_clean
                )
                break

    if target_day is None:
        return None

    # 3. Извлекаем оставшийся комментарий (если есть: "зал", "курсы")
    clean_title = re.sub(r"[^\w\s-]", "", working_text_clean).strip()
    clean_title = re.sub(r"\b(в|во|на|с|со|до)\b", "", clean_title).strip()
    title = clean_title.capitalize() if len(clean_title) >= 3 else "Личные дела"

    return {
        "day_of_week": target_day,
        "start_time": start_time,
        "end_time": end_time,
        "title": title,
    }