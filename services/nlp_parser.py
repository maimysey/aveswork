import re
from datetime import date, time, datetime, timedelta
from typing import Any
from config import get_minsk_now

DAY_ALIASES = {
    "пн": 0, "пон": 0, "понедельник": 0, "понедельника": 0, "понедельнику": 0,
    "вт": 1, "втор": 1, "вторник": 1, "вторника": 1, "вторнику": 1,
    "ср": 2, "сред": 2, "среда": 2, "среду": 2, "среды": 2,
    "чт": 3, "чет": 3, "четверг": 3, "четверга": 3, "четвергу": 3,
    "пт": 4, "пят": 4, "пятница": 4, "пятницу": 4, "пятницы": 4,
    "сб": 5, "суб": 5, "суббота": 5, "субботу": 5, "субботы": 5,
    "вс": 6, "вск": 6, "воскресенье": 6, "воскресенья": 6, "воскресенью": 6,
}

MONEY_REGEX = re.compile(
    r"(?P<amount>\d+[\d\s.,]*)\s*(?P<currency>руб(?:л[ейяь]+)?|byn|bun|р\b|бел(?:\.|\s*)?руб)(?:\s*(?:/|за)?\s*(?P<unit>смен[ауеы]|час[а]?))?",
    re.IGNORECASE,
)

TIME_RANGE_REGEX = re.compile(
    r"(?:с|c)?\s*(\b[0-2]?\d(?::[0-5]\d)?)\s*(?:до|-|–|—)\s*([0-2]?\d(?::[0-5]\d)?)\b",
    re.IGNORECASE,
)

DATE_REGEX = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")

DAY_RANGE_REGEX = re.compile(
    r"\b(?:с\s*)?([а-я]{2,12})\s*(?:по|-|–|—)\s*([а-я]{2,12})\b",
    re.IGNORECASE,
)


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


def _calculate_upcoming_date(day_idx: int, start_hour: int, now_dt: datetime) -> date:
    today_date = now_dt.date()
    days_ahead = (day_idx - today_date.weekday()) % 7
    if days_ahead == 0 and now_dt.hour >= start_hour:
        days_ahead = 7
    return today_date + timedelta(days=days_ahead)


def parse_vacancy_card(text: str) -> dict[str, Any] | None:
    working_text = text.lower()
    now_dt = get_minsk_now()
    today_date = now_dt.date()

    # 1. Извлекаем оплату и маскируем её
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

        working_text = (
            working_text[: money_match.start()]
            + " [PAYMENT] "
            + working_text[money_match.end() :]
        )

    # 2. Извлекаем интервал времени смены
    time_match = TIME_RANGE_REGEX.search(working_text)
    if not time_match:
        return None

    start_time = _parse_time_str(time_match.group(1))
    end_time = _parse_time_str(time_match.group(2))

    if not start_time or not end_time or start_time >= end_time:
        return None

    # Очищаем время из текста для надежного поиска дней
    working_text = (
        working_text[: time_match.start()] + " " + working_text[time_match.end() :]
    )

    # 3. Извлекаем дни (поддержка мульти-дней)
    target_dates_map: dict[date, int] = {}

    # 3.1. Точные календарные даты: 26.09, 27.09.2026
    for m in DATE_REGEX.finditer(working_text):
        d = int(m.group(1))
        month = int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today_date.year
        if y < 100:
            y += 2000
        try:
            exact_d = date(y, month, d)
            target_dates_map[exact_d] = exact_d.weekday()
        except ValueError:
            pass

    # 3.2. Относительные слова
    if "послезавтра" in working_text:
        d = today_date + timedelta(days=2)
        target_dates_map[d] = d.weekday()
    if "завтра" in working_text:
        d = today_date + timedelta(days=1)
        target_dates_map[d] = d.weekday()
    if "сегодня" in working_text:
        target_dates_map[today_date] = today_date.weekday()

    # 3.3. Диапазоны дней недели: «с пн по ср», «чт-сб»
    range_match = DAY_RANGE_REGEX.search(working_text)
    if range_match:
        w1, w2 = range_match.group(1), range_match.group(2)
        if w1 in DAY_ALIASES and w2 in DAY_ALIASES:
            start_d = DAY_ALIASES[w1]
            end_d = DAY_ALIASES[w2]
            if start_d <= end_d:
                for d_idx in range(start_d, end_d + 1):
                    calc_d = _calculate_upcoming_date(d_idx, start_time.hour, now_dt)
                    target_dates_map[calc_d] = d_idx

    # 3.4. Одиночные и перечисленные дни недели: «в четверг и пятницу», «пт, сб»
    words = re.findall(r"\b[а-яё]{2,12}\b", working_text)
    for w in words:
        if w in DAY_ALIASES:
            d_idx = DAY_ALIASES[w]
            calc_d = _calculate_upcoming_date(d_idx, start_time.hour, now_dt)
            target_dates_map[calc_d] = d_idx

    # Если дней не обнаружено вообще — берем сегодня или завтра
    if not target_dates_map:
        fallback_d = today_date if now_dt.hour < start_time.hour else today_date + timedelta(days=1)
        target_dates_map[fallback_d] = fallback_d.weekday()

    # Сортируем дни по календарю
    sorted_shifts = [
        {"target_date": d, "day_of_week": day_idx}
        for d, day_idx in sorted(target_dates_map.items(), key=lambda x: x[0])
    ]

    return {
        "raw_text": text.strip(),
        "start_time": start_time,
        "end_time": end_time,
        "pay_amount": pay_amount,
        "pay_unit": pay_unit,
        "shifts": sorted_shifts,
    }


def parse_user_busy_input(text: str) -> dict[str, Any] | None:
    working_text = text.lower().strip()
    today_date = get_minsk_now().date()

    time_match = TIME_RANGE_REGEX.search(working_text)
    if not time_match:
        return None

    start_time = _parse_time_str(time_match.group(1))
    end_time = _parse_time_str(time_match.group(2))

    if not start_time or not end_time or start_time >= end_time:
        return None

    clean_text = working_text[: time_match.start()] + " " + working_text[time_match.end() :]
    target_days: set[int] = set()

    # Проверка диапазона дней: «пн-ср», «с пн по пт»
    range_match = DAY_RANGE_REGEX.search(clean_text)
    if range_match:
        w1, w2 = range_match.group(1), range_match.group(2)
        if w1 in DAY_ALIASES and w2 in DAY_ALIASES:
            start_d = DAY_ALIASES[w1]
            end_d = DAY_ALIASES[w2]
            if start_d <= end_d:
                for d_idx in range(start_d, end_d + 1):
                    target_days.add(d_idx)
            clean_text = clean_text.replace(range_match.group(0), "")

    # Проверка одиночных и перечисленных дней
    if "завтра" in clean_text:
        target_days.add((today_date + timedelta(days=1)).weekday())
        clean_text = clean_text.replace("завтра", "")
    if "сегодня" in clean_text:
        target_days.add(today_date.weekday())
        clean_text = clean_text.replace("сегодня", "")

    for w in re.findall(r"\b[а-яё]{2,12}\b", clean_text):
        if w in DAY_ALIASES:
            target_days.add(DAY_ALIASES[w])
            clean_text = re.sub(rf"\b{w}\b", "", clean_text)

    if not target_days:
        return None

    # Заголовок (например «зал», «курсы»)
    clean_title = re.sub(r"[^\w\s-]", "", clean_text).strip()
    clean_title = re.sub(r"\b(в|во|на|с|со|до|по|и)\b", "", clean_title).strip()
    title = clean_title.capitalize() if len(clean_title) >= 3 else "Личные дела"

    return {
        "days": sorted(list(target_days)),
        "start_time": start_time,
        "end_time": end_time,
        "title": title,
    }