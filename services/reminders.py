"""
Напоминания и справка для всех ботов Aves (AvesBio, AvesFlow, AvesGlobe, AvesStudy, AvesWork).

Один и тот же файл кладётся в services/ каждого бота и подключается в main.py одной строкой:

    from services.reminders import setup_reminders
    setup_reminders(app, dp, bot, async_session_maker, bot_code="bio", help_text=lambda m: FAQ_TEXT)

Что умеет:
  • «Напомни завтра в 19:30 сдать лабу», «напоминание на чт к 8 утра забрать справку»,
    «напомни через 2 часа позвонить маме», «каждую среду в 18 тренировка», «remind me tomorrow at 7pm to …»;
  • «мои напоминания» / /reminders — список с кнопками удаления;
  • в пришедшем напоминании: ✅ Сделано, ⏰ +15 мин, ⏰ +1 час, для повторяющихся — 🔕 Отключить;
  • «помощь», «справка», «что ты умеешь», «help»… и /help — справка бота + блок про напоминания;
  • GET/POST /api/reminders для мини-аппа (вкладка «Смены → Мои часы»).

Свои данные — в таблице aves_reminders базы этого бота (создаётся сама). Колонка bot отделяет напоминания
ботов с общей базой (AvesBio и AvesStudy): каждый бот отправляет только свои.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import time as _clock
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import BigInteger, DateTime, Integer, SmallInteger, String, func, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

try:
    from config import logger  # логгер бота, если есть
except Exception:  # pragma: no cover
    logger = logging.getLogger("AvesReminders")

MINSK = ZoneInfo("Europe/Minsk")
SECRET = os.getenv("AVES_SESSION_SECRET", "")
MAX_ACTIVE = 50
MAX_TEXT = 300
POLL_SECONDS = 20


def now_minsk() -> datetime:
    return datetime.now(MINSK).replace(tzinfo=None, microsecond=0)


# ════════════════════════════════════════════════════════════════════════════
#                                   ПАРСЕР
# ════════════════════════════════════════════════════════════════════════════

TRIGGER = re.compile(
    r"^\s*(?:/remind(?:@\w+)?\s*|(?:бот[,!\s]+)?(?:пожалуйста[,\s]+)?(?:"
    r"(?:поставь|создай|сделай|добавь|запиши)\s+(?:мне\s+)?напоминани[еяй]"
    r"|напомни(?:ть)?(?:-ка)?|напоминалк[ау]|напоминани[ея]"
    r"|remind(?:\s+me)?|set\s+(?:a\s+)?reminder|reminder)\b)"
    r"[\s,:;—–-]*(?:(?:мне|me|пожалуйста|please)\b[\s,:;—–-]*)*",
    re.I,
)

WD_FORMS: list[tuple[int, str]] = [
    (0, r"понедельник(?:а|у|ом)?|пон|пн|monday|mon"),
    (1, r"вторник(?:а|у|ом)?|вт|tuesday|tue|tues"),
    (2, r"сред(?:а|у|ы|е)|ср|wednesday|wed"),
    (3, r"четверг(?:а|у|ом)?|чт|thursday|thu|thurs"),
    (4, r"пятниц(?:а|у|ы|е)|пт|friday|fri"),
    (5, r"суббот(?:а|у|ы|е)|сб|saturday|sat"),
    (6, r"воскресень(?:е|я|ю)|вс|вск|sunday|sun"),
]
WD_PLURAL: list[tuple[int, str]] = [
    (0, r"понедельникам"), (1, r"вторникам"), (2, r"средам"), (3, r"четвергам"),
    (4, r"пятницам"), (5, r"субботам"), (6, r"воскресеньям"),
]
_WD_ANY = "|".join(f"(?:{p})" for _, p in WD_FORMS)
_WD_SHORT_EN = {"mon", "tue", "tues", "wed", "thu", "thurs", "fri", "sat", "sun"}
_WD_SHORT = {"пон", "пн", "вт", "ср", "чт", "пт", "сб", "вс", "вск", "mon", "tue", "tues", "wed", "thu", "thurs",
             "fri", "sat", "sun"}

MONTHS = {
    "январ": 1, "янв": 1, "феврал": 2, "фев": 2, "март": 3, "мар": 3, "апрел": 4, "апр": 4, "ма": 5,
    "июн": 6, "июл": 7, "август": 8, "авг": 8, "сентябр": 9, "сен": 9, "сент": 9, "октябр": 10, "окт": 10,
    "ноябр": 11, "ноя": 11, "декабр": 12, "дек": 12,
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "april": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10,
    "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_RX = (r"(январ[яь]|янв|феврал[яь]|фев|марта?|мар|апрел[яь]|апр|ма[яй]|июн[яь]|июн|июл[яь]|июл"
             r"|августа?|авг|сентябр[яь]|сент?|октябр[яь]|окт|ноябр[яь]|ноя|декабр[яь]|дек"
             r"|january|jan|february|feb|march|april|may|june|jun|july|jul|august|aug|september|sept?"
             r"|october|oct|november|nov|december|dec)\.?")

NUM_WORDS = {
    "ноль": 0, "один": 1, "одну": 1, "одна": 1, "час": 1, "два": 2, "две": 2, "пару": 2, "пара": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11,
    "двенадцать": 12, "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "сорок": 40, "сорок пять": 45,
    "полтора": 1.5, "полторы": 1.5, "несколько": 3, "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "five": 5,
    "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "half an": 0.5,
}
# «половина восьмого» — родительный порядковый: часа
ORD_GEN = {"первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "пятого": 5, "шестого": 6,
           "седьмого": 7, "восьмого": 8, "девятого": 9, "десятого": 10, "одиннадцатого": 11, "двенадцатого": 12}
HOUR_WORDS = {"час": 1, "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
              "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12}
MIN_WORDS = {"пяти": 5, "десяти": 10, "пятнадцати": 15, "четверти": 15, "двадцати": 20, "двадцати пяти": 25}

PART_OF_DAY = [
    (r"(?:в\s+)?полдень|(?:at\s+)?noon|midday", time(12, 0)),
    (r"(?:в\s+)?полночь|(?:at\s+)?midnight", time(0, 0)),
    (r"(?:с\s+)?утр(?:ом|а)|к\s+утру|in\s+the\s+morning|morning", time(9, 0)),
    (r"(?:в\s+)?обед|днём|днем|in\s+the\s+afternoon|afternoon", time(13, 0)),
    (r"к\s+вечеру", time(18, 0)),
    (r"вечером|in\s+the\s+evening|evening|tonight", time(19, 0)),
    (r"ночью|перед\s+сном|at\s+night", time(23, 0)),
]
# уточнители к «в 7»: «завтра вечером в 7» → 19:00
DAY_MARKERS = [
    (r"(?:с\s+)?утр(?:ом|а)|in\s+the\s+morning", time(9, 0)),
    (r"днём|днем|in\s+the\s+afternoon", time(13, 0)),
    (r"вечером|in\s+the\s+evening|tonight", time(19, 0)),
    (r"ночью|at\s+night", time(23, 0)),
]

UNIT_MIN = [
    (r"мин(?:ут[уы]?|\.)?|minutes?|mins?", 1),
    (r"час(?:а|ов)?|ч\.?|hours?|hrs?|h", 60),
    (r"сут(?:ки|ок)|дн(?:я|ей)|день|days?", 1440),
    (r"недел[юиь]|нед\.?|weeks?", 10080),
]


@dataclass
class Parsed:
    due: datetime | None = None
    text: str = ""
    repeat: str = ""            # "", "daily", "weekdays", "weekly:0,2"
    error: str | None = None
    has_time: bool = False
    more: list[datetime] = field(default_factory=list)   # «в среду и пятницу» — ещё даты с тем же временем

    @property
    def dues(self) -> list[datetime]:
        return [self.due, *self.more] if self.due else []


@dataclass
class _Ctx:
    s: str
    orig: str = ""
    spans: list[tuple[int, int]] = field(default_factory=list)

    def take(self, m: re.Match, group: int = 0) -> None:
        self.spans.append((m.start(group), m.end(group)))

    def free(self, a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in self.spans)

    def finditer(self, rx: str):
        for m in re.finditer(rx, self.s, re.I):
            if self.free(m.start(), m.end()):
                yield m

    def search(self, rx: str):
        return next(self.finditer(rx), None)


def _wd_of(word: str) -> int | None:
    w = word.lower().strip(".")
    for idx, pat in WD_FORMS:
        if re.fullmatch(pat, w):
            return idx
    return None


def _num(word: str | None) -> float | None:
    if word is None or word == "":
        return 1
    w = word.lower().replace(",", ".").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", w):
        return float(w)
    return NUM_WORDS.get(w)


def _mk_date(d: int, m: int, y: int | None, today: date) -> date | None:
    try:
        yy = y if y else today.year
        if yy < 100:
            yy += 2000
        out = date(yy, m, d)
        if not y and out < today - timedelta(days=1):
            out = date(yy + 1, m, d)
        return out
    except ValueError:
        return None


def _month_num(word: str) -> int | None:
    w = word.lower().strip(".")
    for key in sorted(MONTHS, key=len, reverse=True):
        if w.startswith(key):
            return MONTHS[key]
    return None


def parse_reminder(raw: str, now: datetime | None = None) -> Parsed | None:
    """None — это не просьба о напоминании. Parsed.error — просьба есть, но понять не удалось."""
    now = (now or now_minsk()).replace(second=0, microsecond=0)
    today = now.date()
    m = TRIGGER.match(raw or "")
    if not m:
        return None
    body = raw[m.end():].strip()
    if not body:
        return Parsed(error="empty")
    s = body.replace("ё", "е").replace("Ё", "Е")
    c = _Ctx(s, body)

    the_date: date | None = None
    t: time | None = None
    explicit_ampm = False
    hour_only: int | None = None      # «в 7» без уточнения — решаем ниже
    repeat_days: list[int] = []
    repeat = ""
    rel = timedelta(0)
    next_week = False
    default_t: time | None = None
    from_weekday = False
    extra_dates: list[date] = []
    bad_date = False

    # ── повторы ───────────────────────────────────────────────────────────
    r = c.search(r"\b(каждый\s+день|ежедневно|every\s*day|daily)\b")
    if r:
        repeat = "daily"; c.take(r)
    r = c.search(r"\b(каждое\s+утро)\b")
    if r:
        repeat, default_t = "daily", time(9, 0); c.take(r)
    r = c.search(r"\b(каждый\s+вечер)\b")
    if r:
        repeat, default_t = "daily", time(19, 0); c.take(r)
    r = c.search(r"\b(по\s+будням|в\s+будни|каждый\s+будний\s+день|по\s+рабочим\s+дням|on\s+weekdays|weekdays)\b")
    if r:
        repeat = "weekdays"; c.take(r)
    r = c.search(r"\b(по\s+выходным|каждые\s+выходные|on\s+weekends|every\s+weekend)\b")
    if r:
        repeat, repeat_days = "weekly", [5, 6]; c.take(r)
    r = c.search(rf"\b(?:каждый|каждую|каждое|every)\s+((?:{_WD_ANY})(?:(?:\s*,\s*|\s+(?:и|and)\s+)(?:{_WD_ANY}))*)\b")
    if r:
        repeat = "weekly"
        repeat_days = sorted({d for w in re.split(r"\s*,\s*|\s+(?:и|and)\s+", r.group(1)) if (d := _wd_of(w)) is not None})
        c.take(r)
    pl = "|".join(p for _, p in WD_PLURAL)
    r = c.search(rf"\bпо\s+((?:{pl})(?:(?:\s*,\s*|\s+и\s+)(?:{pl}))*)\b")
    if r:
        repeat = "weekly"
        repeat_days = sorted({i for w in re.split(r"\s*,\s*|\s+и\s+", r.group(1)) for i, p in WD_PLURAL
                              if re.fullmatch(p, w.lower())})
        c.take(r)

    # ── через N минут / часов / дней ──────────────────────────────────────
    unit_rx = "|".join(f"(?:{u})" for u, _ in UNIT_MIN)
    num_rx = r"\d+(?:[.,]\d+)?|полтора|полторы|пару|пара|один|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|пятнадцать|двадцать|тридцать|сорок|несколько|an?|one|two|three|five|ten|fifteen|twenty|thirty"
    one = rf"(?:(?:{num_rx})\s*)?(?:{unit_rx})(?![а-яa-z])"
    r = c.search(rf"\b(?:через|in)\s+(полчаса|пол\s*часа|half\s+an\s+hour|{one}(?:(?:\s*,\s*|\s+и\s+|\s+and\s+|\s+){one})*)")
    if r:
        chunk = r.group(1).lower()
        if re.fullmatch(r"полчаса|пол\s*часа|half\s+an\s+hour", chunk):
            rel = timedelta(minutes=30)
        elif chunk in ("час",):
            rel = timedelta(hours=1)
        elif chunk in ("сутки", "день"):
            rel = timedelta(days=1)
        elif chunk == "неделю":
            rel = timedelta(weeks=1)
        elif chunk == "минуту":
            rel = timedelta(minutes=1)
        else:
            for nm in re.finditer(rf"(?:({num_rx})\s*)?({unit_rx})(?![а-яa-z])", chunk):
                n = _num(nm.group(1)) if nm.group(1) else 1
                mult = next(k for u, k in UNIT_MIN if re.fullmatch(u, nm.group(2)))
                rel += timedelta(minutes=(n or 1) * mult)
        c.take(r)

    # ── дата ──────────────────────────────────────────────────────────────
    r = c.search(r"\b(?:на\s+)?(послезавтра|day\s+after\s+tomorrow)\b")
    if r:
        the_date = today + timedelta(days=2); c.take(r)
    r = c.search(r"\b(?:на\s+)?(завтра|tomorrow)\b")
    if r:
        the_date = today + timedelta(days=1); c.take(r)
    r = c.search(r"\b(?:на\s+)?(сегодня|today|tonight)\b")
    if r:
        the_date = today
        if r.group(1).lower() == "tonight":
            default_t = default_t or time(19, 0)
        c.take(r)
    r = c.search(r"\b(?:на\s+)?следующей\s+неделе|next\s+week\b")
    if r:
        next_week = True; c.take(r)
    r = c.search(r"\b(?:на|в)\s+(?:эти\s+)?выходны[хе]\b|\bthis\s+weekend\b")
    if r and the_date is None:
        the_date = today + timedelta(days=(5 - today.weekday()) % 7); c.take(r)

    # день недели
    if not repeat:
        r = c.search(rf"\b((?:в|во|на|к|ко|on|next|this)\s+)?((?:эт(?:от|у|о)|ближайш(?:ий|ую|ее)|следующ(?:ий|ую|ее))\s+)?({_WD_ANY})\b\.?")
        if r and (r.group(1) or r.group(2) or r.group(3).lower().strip(".") not in _WD_SHORT_EN):
            wd = _wd_of(r.group(3))
            nxt = bool(r.group(2) and r.group(2).lower().startswith("следующ")) or (r.group(1) or "").lower().strip() == "next"
            delta = (wd - today.weekday()) % 7
            the_date = today + timedelta(days=delta)
            if nxt or next_week:
                monday_next = today + timedelta(days=7 - today.weekday())
                the_date = monday_next + timedelta(days=wd)
            c.take(r)
            from_weekday = True
            # «в среду и пятницу», «пн, ср, пт»
            pos = r.end()
            while True:
                m2 = re.compile(rf"(?:\s*,\s*|\s+(?:и|and)\s+)(?:(?:в|во|на|on)\s+)?({_WD_ANY})\b\.?", re.I).match(c.s, pos)
                if not m2 or not c.free(m2.start(), m2.end()):
                    break
                wd2 = _wd_of(m2.group(1))
                d2 = today + timedelta(days=(wd2 - today.weekday()) % 7)
                if nxt or next_week:
                    d2 = today + timedelta(days=7 - today.weekday()) + timedelta(days=wd2)
                if d2 != the_date and d2 not in extra_dates:
                    extra_dates.append(d2)
                c.take(m2)
                pos = m2.end()
    if next_week and the_date is None:
        the_date = today + timedelta(days=7 - today.weekday())

    # дата словами: «25 октября», «1-го ноября», «on 5 nov»
    r = c.search(rf"\b(?:на\s+|к\s+|до\s+|on\s+)?(\d{{1,2}})(?:-?(?:го|е|ое|th|st|nd|rd))?\s+{_MONTH_RX}(?:\s+(\d{{4}}))?(?=\W|$)")
    if r:
        mo = _month_num(r.group(2))
        d = _mk_date(int(r.group(1)), mo, int(r.group(3)) if r.group(3) else None, today) if mo else None
        if d:
            the_date = d; c.take(r)
        else:
            bad_date = True; c.take(r)
    # 25.10, 31.12.2026, 5/11 — не «в 19.30» (это время)
    for r in c.finditer(r"(?<![\d:])(?:(в|во|к|ко|at|by|на|до|on)\s+)?(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?![\d:])"):
        prep = (r.group(1) or "").lower()
        if prep in ("в", "во", "к", "ко", "at", "by") and not r.group(4):
            continue
        dd = _mk_date(int(r.group(2)), int(r.group(3)), int(r.group(4)) if r.group(4) else None, today)
        if dd and the_date is None:
            the_date = dd
            c.take(r)
            break
    # «25-го», «25 числа»
    r = c.search(r"\b(?:на\s+|к\s+)?(\d{1,2})(?:-?го|\s+числа)\b")
    if r and the_date is None:
        d = int(r.group(1))
        cand = _mk_date(d, today.month, today.year, today)
        if cand and cand < today:
            cand = _mk_date(d, today.month % 12 + 1, today.year + (today.month == 12), today)
        if cand:
            the_date = cand; c.take(r)

    # ── время ────────────────────────────────────────────────────────────
    def apply_marker(h: int, marker: str | None) -> tuple[int, bool]:
        if not marker:
            return h, False
        mk = marker.lower().replace(".", "")
        if mk in ("вечера", "дня", "pm") and h < 12:
            return h + 12, True
        if mk in ("утра", "am") and h == 12:
            return 0, True
        if mk == "ночи" and h == 12:
            return 0, True
        if mk == "ночи" and 6 <= h < 12:
            return h + 12, True
        return h, True

    marker_rx = r"(?:\s*(утра|дня|вечера|ночи|am|pm|a\.m\.|p\.m\.))?"

    # «в половине восьмого», «в четверть восьмого», «без четверти восемь», «без 10 восемь»
    r = c.search(r"\b(?:в\s+)?половин[еуa]\s+(" + "|".join(ORD_GEN) + r")" + marker_rx)
    if r:
        h = ORD_GEN[r.group(1)] - 1
        h, explicit_ampm = apply_marker(h, r.group(2))
        t = time(h % 24, 30); hour_only = None if explicit_ampm else h; c.take(r)
    if t is None:
        r = c.search(r"\b(?:в\s+)?четверть\s+(" + "|".join(ORD_GEN) + r")" + marker_rx)
        if r:
            h = ORD_GEN[r.group(1)] - 1
            h, explicit_ampm = apply_marker(h, r.group(2))
            t = time(h % 24, 15); hour_only = None if explicit_ampm else h; c.take(r)
    if t is None:
        r = c.search(r"\bбез\s+(\d{1,2}|" + "|".join(MIN_WORDS) + r")\s+(?:минут\s+)?(\d{1,2}|" + "|".join(HOUR_WORDS) + r")" + marker_rx)
        if r:
            mins = int(r.group(1)) if r.group(1).isdigit() else MIN_WORDS[r.group(1)]
            hh = int(r.group(2)) if r.group(2).isdigit() else HOUR_WORDS[r.group(2)]
            h = (hh - 1) % 24
            h, explicit_ampm = apply_marker(h, r.group(3))
            if 0 < mins < 60:
                t = time(h, 60 - mins); hour_only = None if explicit_ampm or hh > 12 else h; c.take(r)

    # 19:30, 19.30, 7:30 pm, «в 19 30», «19ч30», «в 19 часов 30 минут»
    if t is None:
        for r in c.finditer(r"(?<![\d.:/])(?:(в|во|к|ко|на|at|by|до)\s+)?([01]?\d|2[0-3])\s*[:.]\s*([0-5]\d)(?![\d.:/])" + marker_rx):
            prep = (r.group(1) or "").lower()
            h, mi = int(r.group(2)), int(r.group(3))
            if ":" not in r.group(0) and prep not in ("в", "во", "к", "ко", "at", "by"):
                # 10.10 без «в»/«к» — скорее дата; 19.30 (минуты > 12) — время
                if mi <= 12 and the_date is None and _mk_date(h, mi, None, today):
                    continue
            h, explicit_ampm = apply_marker(h, r.group(4))
            t = time(h, mi)
            hour_only = None
            c.take(r)
            break
    if t is None:
        r = c.search(r"\b(?:в|во|к|ко|на)\s+([01]?\d|2[0-3])\s+([0-5]\d)\b(?!\s*(?:" + unit_rx + r"))" + marker_rx)
        if r:
            h, explicit_ampm = apply_marker(int(r.group(1)), r.group(3))
            t = time(h, int(r.group(2))); c.take(r)
    if t is None:
        r = c.search(r"\b(?:(?:в|во|к|ко|на|at)\s+)?([01]?\d|2[0-3])\s*(?:ч(?:ас(?:а|ов)?)?\.?)(?:\s*(\d{1,2})\s*(?:м(?:ин(?:ут[аы]?)?)?\.?)?)?(?![а-я])" + marker_rx)
        if r and not re.search(r"через\s*$", c.s[:r.start()], re.I):
            h, explicit_ampm = apply_marker(int(r.group(1)), r.group(3))
            mi = int(r.group(2)) if r.group(2) else 0
            if mi < 60:
                t = time(h, mi); hour_only = None if explicit_ampm or r.group(2) or h > 12 else h; c.take(r)
    if t is None:
        # «в 7», «к 8 утра», «at 7pm», «7pm»
        r = c.search(r"\b(?:(?:в|во|к|ко|at|by)\s+)([01]?\d|2[0-3])(?![\d:.])" + marker_rx + r"(?!\s*(?:" + unit_rx + r"|" + _MONTH_RX + r"|-?го\b|числа))")
        if not r:
            r = c.search(r"\b([01]?\d)\s*(am|pm|a\.m\.|p\.m\.)(?=\W|$)")
            if r:
                h, explicit_ampm = apply_marker(int(r.group(1)), r.group(2))
                t = time(h % 24, 0); c.take(r)
        else:
            h, explicit_ampm = apply_marker(int(r.group(1)), r.group(2))
            t = time(h % 24, 0)
            hour_only = None if explicit_ampm or h >= 13 or h == 0 else h
            c.take(r)
    if t is None:
        r = c.search(r"\b(?:в\s+)?(" + "|".join(HOUR_WORDS) + r")\s+(утра|дня|вечера|ночи)\b")
        if r:
            h, explicit_ampm = apply_marker(HOUR_WORDS[r.group(1)], r.group(2))
            t = time(h % 24, 0); c.take(r)
    # части дня
    if t is None:
        for rx, tt in PART_OF_DAY:
            r = c.search(rf"\b(?:{rx})\b")
            if r:
                t = tt; c.take(r)
                if tt == time(0, 0) and the_date is None:
                    the_date = today + timedelta(days=1)
                break
    elif default_t is None:
        # «завтра вечером в 7» — маркер части дня уточняет «в 7»
        for rx, tt in DAY_MARKERS:
            r = c.search(rf"\b(?:{rx})\b")
            if r:
                c.take(r)
                if hour_only is not None and tt.hour >= 13 and hour_only < 12:
                    t = time(hour_only + 12, t.minute); hour_only = None
                elif hour_only is not None and tt.hour < 12:
                    hour_only = None
                break

    # ── текст напоминания ────────────────────────────────────────────────
    text = _clean_text(c)

    # ── сборка ───────────────────────────────────────────────────────────
    if repeat:
        tt = t or default_t or time(9, 0)
        if repeat == "weekly" and not repeat_days:
            return Parsed(error="no_days")
        rep = repeat if repeat != "weekly" else "weekly:" + ",".join(map(str, repeat_days))
        start = the_date or today
        due = next_occurrence(rep, tt, datetime.combine(start, time(0, 0)) - timedelta(minutes=1), now)
        return Parsed(due=due, text=text, repeat=rep, has_time=t is not None)

    if rel:
        due = now + rel
        if t and rel >= timedelta(days=1):
            due = datetime.combine(due.date(), t)
        return Parsed(due=due, text=text, has_time=True)

    if t is None and default_t is not None:
        t = default_t
    if bad_date and the_date is None:
        return Parsed(text=text, error="bad_date")
    if t is None and the_date is None:
        return Parsed(text=text, error="no_when")
    if t is None:
        if the_date == today:
            return Parsed(text=text, error="no_time_today")
        t = time(9, 0)
        has_time = False
    else:
        has_time = True

    d = the_date
    if hour_only is not None and 1 <= hour_only <= 11:
        if d is None or d == today:
            am = datetime.combine(today, t)
            pm = datetime.combine(today, time(hour_only + 12, t.minute))
            if am > now:
                due = am
            elif pm > now:
                due = pm
            else:
                due = am + timedelta(days=1) if d is None else None
            if due is None and not from_weekday:
                return Parsed(text=text, error="past")
            if due is not None and not extra_dates:
                return Parsed(due=due, text=text, has_time=True)
            if due is not None:
                t = due.time()
            elif hour_only <= 6:
                t = time(hour_only + 12, t.minute)
        elif hour_only <= 6:
            t = time(hour_only + 12, t.minute)
    if d is None:
        due = datetime.combine(today, t)
        if due <= now:
            due += timedelta(days=1)
    else:
        due = datetime.combine(d, t)
        if due <= now and from_weekday:
            due += timedelta(days=7)        # «в воскресенье в 10», а сегодня воскресенье 14:00 — следующее
        if due <= now:
            return Parsed(text=text, error="past")
    if due > now + timedelta(days=366):
        return Parsed(text=text, error="too_far")
    more = []
    for d2 in extra_dates:
        x = datetime.combine(d2, t)
        if x <= now:
            x += timedelta(days=7)
        more.append(x)
    allx = sorted({due, *more})
    return Parsed(due=allx[0], text=text, has_time=has_time, more=allx[1:])


_LEADING = re.compile(
    r"^(?:[\s,.:;!—–-]+|(?:что(?:бы)?|о\s+том(?:,)?\s+что(?:бы)?|о|об|про|to|that|about|мне|пожалуйста|please|"
    r"и|а|and)\b)+",
    re.I,
)
_TRAILING = re.compile(r"(?:[\s,.:;—–-]+|\b(?:в|во|на|к|ко|at|on|by|и|а|до|and|to)\b)+$", re.I)


def _clean_text(c: _Ctx) -> str:
    out, pos = [], 0
    for a, b in sorted(c.spans):
        if a > pos:
            out.append((c.orig or c.s)[pos:a])
        out.append(" ")
        pos = max(pos, b)
    out.append((c.orig or c.s)[pos:])
    text = re.sub(r"\s+", " ", "".join(out)).strip()
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    # предлоги, оставшиеся без своих слов посреди фразы: «сделать в в» → «сделать»
    text = re.sub(r"\b(?:в|во|на|к|ко)\s+(?=(?:в|во|на|к|ко)\b)", "", text, flags=re.I)
    for _ in range(3):
        text = _LEADING.sub("", text).strip()
        text = _TRAILING.sub("", text).strip()
    text = text[:MAX_TEXT].strip()
    return text[:1].upper() + text[1:] if text else ""


def next_occurrence(repeat: str, at: time, after: datetime, now: datetime | None = None) -> datetime:
    """Ближайший момент повтора строго после after (и не в прошлом)."""
    now = now or now_minsk()
    base = max(after, now)
    for i in range(0, 15):
        d = base.date() + timedelta(days=i)
        cand = datetime.combine(d, at)
        if cand <= base:
            continue
        wd = d.weekday()
        if repeat == "daily":
            return cand
        if repeat == "weekdays" and wd < 5:
            return cand
        if repeat.startswith("weekly:") and str(wd) in repeat.split(":", 1)[1].split(","):
            return cand
    return datetime.combine(base.date() + timedelta(days=1), at)


# ════════════════════════════════════════════════════════════════════════════
#                                ТЕКСТЫ
# ════════════════════════════════════════════════════════════════════════════

DAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
DAYS_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]

HELP_RU = (
    "⏰ <b>Напоминания</b> — просто напишите боту:\n"
    "• <i>«Напомни завтра в 19:30 сдать лабу»</i>\n"
    "• <i>«Напоминание на чт к 8 утра забрать справку»</i>\n"
    "• <i>«Напомни в среду вечером позвонить маме»</i>\n"
    "• <i>«Напомни через 2 часа выключить духовку»</i>\n"
    "• <i>«Напомни 25 октября в 18:00 оплатить общежитие»</i>\n"
    "• <i>«Напоминай каждую пятницу в 18 сдавать отчёт»</i> (или «по будням», «каждый день»)\n"
    "Список и удаление — <i>«мои напоминания»</i> или /reminders. Время — минское. "
    "Без времени «завтра» и дни недели — в 9:00, «утром» — 9:00, «вечером» — 19:00."
)
HELP_EN = (
    "⏰ <b>Reminders</b> — just type:\n"
    "• <i>«Remind me tomorrow at 7:30pm to submit the essay»</i>\n"
    "• <i>«Remind me on Thursday at 8am to pick up the certificate»</i>\n"
    "• <i>«Remind me in 2 hours to call mom»</i>\n"
    "• <i>«Remind me every Friday at 18:00 to send the report»</i>\n"
    "Russian works too: <i>«напомни завтра в 19:30 …»</i>. List: <i>«my reminders»</i> or /reminders. Minsk time."
)


def is_en(locale: str | None) -> bool:
    return (locale or "ru").lower().startswith("en")


def when_label(due: datetime, now: datetime | None = None, locale: str | None = "ru") -> str:
    now = now or now_minsk()
    d = (due.date() - now.date()).days
    hm = due.strftime("%H:%M")
    if is_en(locale):
        if d == 0:
            return f"today at {hm}"
        if d == 1:
            return f"tomorrow at {hm}"
        return f"{DAYS_EN[due.weekday()]}, {due.day} {due.strftime('%b')} at {hm}"
    if d == 0:
        return f"сегодня в {hm}"
    if d == 1:
        return f"завтра в {hm}"
    if d == 2:
        return f"послезавтра в {hm}"
    return f"{DAYS_SHORT[due.weekday()]}, {due.day} {MONTHS_GEN[due.month - 1]} в {hm}"


def repeat_label(rep: str, locale: str | None = "ru") -> str:
    if not rep:
        return ""
    if rep == "daily":
        return "every day" if is_en(locale) else "каждый день"
    if rep == "weekdays":
        return "on weekdays" if is_en(locale) else "по будням"
    days = [int(x) for x in rep.split(":", 1)[1].split(",") if x]
    if is_en(locale):
        return "every " + ", ".join(DAYS_EN[d] for d in days)
    return "по " + ", ".join(DAYS_SHORT[d] for d in days)


ERRORS_RU = {
    "empty": "Что и когда напомнить? Например: <i>«напомни завтра в 19:30 сдать лабу»</i>",
    "no_when": "Когда напомнить? Добавьте день и время, например: <i>«напомни завтра в 19:30 …»</i> "
               "или <i>«напомни через 2 часа …»</i>",
    "no_time_today": "Во сколько напомнить сегодня? Например: <i>«напомни сегодня в 21:00 …»</i>",
    "past": "Это время уже прошло 🙂 Укажите время позже текущего.",
    "too_far": "Напоминать можно не дальше чем на год вперёд.",
    "no_days": "Не понял, по каким дням повторять. Например: <i>«каждую среду в 18 …»</i>",
    "bad_date": "Такой даты нет в календаре 🙂 Проверьте число и месяц.",
    "limit": f"У вас уже {MAX_ACTIVE} активных напоминаний — удалите ненужные в «мои напоминания».",
}
ERRORS_EN = {
    "empty": "What and when should I remind you? E.g. <i>«remind me tomorrow at 7pm to …»</i>",
    "no_when": "When should I remind you? E.g. <i>«remind me tomorrow at 19:30 to …»</i> or <i>«in 2 hours»</i>",
    "no_time_today": "What time today? E.g. <i>«remind me today at 9pm to …»</i>",
    "past": "That time has already passed 🙂",
    "too_far": "Reminders can be at most a year ahead.",
    "no_days": "Which days should it repeat on? E.g. <i>«every Wednesday at 18:00 …»</i>",
    "bad_date": "That date doesn't exist 🙂",
    "limit": f"You already have {MAX_ACTIVE} active reminders — delete some in «my reminders».",
}


def error_text(code: str, locale: str | None = "ru") -> str:
    return (ERRORS_EN if is_en(locale) else ERRORS_RU).get(code, ERRORS_RU["no_when"])


# ════════════════════════════════════════════════════════════════════════════
#                               ХРАНЕНИЕ
# ════════════════════════════════════════════════════════════════════════════

class RemindersBase(DeclarativeBase):
    pass


class Reminder(RemindersBase):
    __tablename__ = "aves_reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot: Mapped[str] = mapped_column(String(16), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(String(MAX_TEXT + 20))
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)      # минское время
    repeat: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(12), default="active", index=True)   # active / done / cancelled
    source: Mapped[str] = mapped_column(String(8), default="bot")       # bot / web
    sent_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_minsk)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)


def item_json(r: Reminder) -> dict[str, Any]:
    return {
        "id": r.id, "text": r.text, "due": r.due_at.strftime("%Y-%m-%dT%H:%M"),
        "date": r.due_at.date().isoformat(), "time": r.due_at.strftime("%H:%M"),
        "repeat": r.repeat, "repeat_label": repeat_label(r.repeat), "when": when_label(r.due_at),
        "source": r.source,
    }


class ReminderService:
    def __init__(self, session_maker, bot_code: str):
        self.sm = session_maker
        self.bot = bot_code

    async def create(self, uid: int, p: Parsed, source: str = "bot") -> list[Reminder] | str:
        dues = p.dues
        async with self.sm() as s:
            n = await s.scalar(select(func.count(Reminder.id)).where(
                Reminder.bot == self.bot, Reminder.user_id == uid, Reminder.status == "active")) or 0
            if n + len(dues) > MAX_ACTIVE:
                return "limit"
            out = [Reminder(bot=self.bot, user_id=uid, text=(p.text or "Напоминание")[:MAX_TEXT], due_at=due,
                            repeat=p.repeat, status="active", source=source, sent_count=0, created_at=now_minsk())
                   for due in dues]
            s.add_all(out)
            await s.commit()
            for r in out:
                await s.refresh(r)
            return out

    async def active(self, uid: int) -> list[Reminder]:
        async with self.sm() as s:
            return list((await s.execute(
                select(Reminder).where(Reminder.bot == self.bot, Reminder.user_id == uid, Reminder.status == "active")
                .order_by(Reminder.due_at)
            )).scalars().all())

    async def cancel(self, uid: int, rid: int) -> bool:
        async with self.sm() as s:
            res = await s.execute(update(Reminder).where(
                Reminder.id == rid, Reminder.user_id == uid, Reminder.bot == self.bot, Reminder.status == "active"
            ).values(status="cancelled"))
            await s.commit()
            return bool(res.rowcount)

    async def snooze(self, uid: int, rid: int, minutes: int) -> Reminder | None:
        async with self.sm() as s:
            r = await s.get(Reminder, rid)
            if not r or r.user_id != uid or r.bot != self.bot:
                return None
            due = now_minsk() + timedelta(minutes=minutes)
            if r.repeat:
                # повторяющееся не трогаем — отложенную копию делаем разовым напоминанием
                copy = Reminder(bot=self.bot, user_id=uid, text=r.text, due_at=due, repeat="", status="active",
                                source=r.source, sent_count=0, created_at=now_minsk())
                s.add(copy)
                await s.commit()
                return copy
            r.due_at, r.status = due, "active"
            await s.commit()
            return r

    async def done(self, uid: int, rid: int) -> None:
        async with self.sm() as s:
            await s.execute(update(Reminder).where(
                Reminder.id == rid, Reminder.user_id == uid, Reminder.bot == self.bot, Reminder.repeat == ""
            ).values(status="done"))
            await s.commit()

    async def due(self, limit: int = 50) -> list[Reminder]:
        async with self.sm() as s:
            return list((await s.execute(
                select(Reminder).where(Reminder.bot == self.bot, Reminder.status == "active",
                                       Reminder.due_at <= now_minsk())
                .order_by(Reminder.due_at).limit(limit)
            )).scalars().all())

    async def mark_sent(self, r: Reminder, ok: bool, blocked: bool = False) -> None:
        now = now_minsk()
        async with self.sm() as s:
            values: dict[str, Any] = {"last_sent_at": now, "sent_count": (r.sent_count or 0) + 1}
            if blocked:
                values["status"] = "cancelled"
            elif r.repeat:
                values["due_at"] = next_occurrence(r.repeat, r.due_at.time(), max(r.due_at, now), now)
            elif ok:
                values["status"] = "done"
            else:
                values["due_at"] = now + timedelta(minutes=2 * ((r.sent_count or 0) + 1))
                if (r.sent_count or 0) >= 4:
                    values["status"] = "cancelled"
            await s.execute(update(Reminder).where(Reminder.id == r.id, Reminder.status == "active").values(**values))
            await s.commit()


# ════════════════════════════════════════════════════════════════════════════
#                          КНОПКИ И ОБРАБОТЧИКИ В БОТЕ
# ════════════════════════════════════════════════════════════════════════════

HELP_WORDS = {
    "помощь", "помоги", "помогите", "справка", "хелп", "help", "faq", "инструкция", "инструкции", "команды",
    "список команд", "что ты умеешь", "что умеешь", "что ты можешь", "что умеет бот", "как пользоваться",
    "как пользоваться ботом", "как это работает", "меню помощи", "нужна помощь", "/помощь", "?", "??",
    "what can you do", "commands", "how to use",
}
LIST_RX = re.compile(r"^\s*(?:бот[,\s]+)?(?:/reminders|мои\s+напоминани[яй]|напоминани[яй]|список\s+напоминаний|"
                     r"какие\s+(?:у\s+меня\s+)?напоминани[яй]|покажи\s+напоминани[яй]|(?:удалить|удали|отмени(?:ть)?)\s+"
                     r"напоминани[ея]|my\s+reminders|reminders)\s*[?!.]*\s*$", re.I)


def _norm(text: str) -> str:
    t = (text or "").lower().replace("ё", "е").strip()
    t = re.sub(r"^бот[,!\s]+", "", t)
    return re.sub(r"[!.\s]+$", "", re.sub(r"\s+", " ", t))


def is_help_request(text: str) -> bool:
    t = _norm(text)
    return t in HELP_WORDS or t.rstrip("?").strip() in HELP_WORDS


def reminder_kb(r: Reminder, locale: str | None = "ru") -> InlineKeyboardMarkup:
    en = is_en(locale)
    row = [InlineKeyboardButton(text="✅ Done" if en else "✅ Сделано", callback_data=f"rmd:ok:{r.id}"),
           InlineKeyboardButton(text="⏰ +15 min" if en else "⏰ +15 мин", callback_data=f"rmd:sn:{r.id}:15"),
           InlineKeyboardButton(text="⏰ +1 h" if en else "⏰ +1 час", callback_data=f"rmd:sn:{r.id}:60")]
    rows = [row]
    if r.repeat:
        rows.append([InlineKeyboardButton(text="🔕 Stop repeating" if en else "🔕 Больше не повторять",
                                          callback_data=f"rmd:off:{r.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def created_kb(r: Reminder, locale: str | None = "ru") -> InlineKeyboardMarkup:
    en = is_en(locale)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖️ Cancel" if en else "✖️ Отменить", callback_data=f"rmd:del:{r.id}"),
        InlineKeyboardButton(text="📋 My reminders" if en else "📋 Мои напоминания", callback_data="rmd:list"),
    ]])


def list_view(items: list[Reminder], locale: str | None = "ru") -> tuple[str, InlineKeyboardMarkup | None]:
    en = is_en(locale)
    if not items:
        return (("You have no active reminders.\n\n" + HELP_EN) if en else
                ("У вас нет активных напоминаний.\n\n" + HELP_RU)), None
    lines = ["⏰ <b>Your reminders:</b>\n" if en else "⏰ <b>Ваши напоминания:</b>\n"]
    rows = []
    for i, r in enumerate(items[:30], 1):
        rep = f" · 🔁 {repeat_label(r.repeat, locale)}" if r.repeat else ""
        lines.append(f"{i}. <b>{when_label(r.due_at, locale=locale)}</b>{rep}\n    {html.escape(r.text)}")
        rows.append([InlineKeyboardButton(text=f"🗑 {i}. {r.text[:40]}", callback_data=f"rmd:del:{r.id}")])
    lines.append("\n<i>Tap to delete:</i>" if en else "\n<i>Нажмите, чтобы удалить:</i>")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


HelpText = Callable[[Message], "str | Awaitable[str]"]
LocaleFn = Callable[[int], "str | Awaitable[str]"]


async def _maybe(v):
    return await v if asyncio.iscoroutine(v) or isinstance(v, Awaitable) else v


class _NoState(BaseFilter):
    """Обычный текст перехватываем, только если пользователь не заполняет форму бота (имя, заметку…)."""

    async def __call__(self, message: Message, state: FSMContext | None = None) -> bool:
        if state is None:
            return True
        return await state.get_state() is None


def _make_router(svc: ReminderService, help_text: HelpText | None, help_markup, locale_fn: LocaleFn | None,
                 help_extra: str | None) -> Router:
    router = Router(name="aves_reminders")

    async def loc(uid: int) -> str:
        if not locale_fn:
            return "ru"
        try:
            return await _maybe(locale_fn(uid)) or "ru"
        except Exception:
            return "ru"

    async def send_help(message: Message) -> None:
        locale = await loc(message.from_user.id) if message.from_user else "ru"
        base = ""
        if help_text:
            base = await _maybe(help_text(message)) or ""
        block = (help_extra or "") + (HELP_EN if is_en(locale) else HELP_RU)
        markup = help_markup(message) if callable(help_markup) else help_markup
        if base and len(base) + len(block) + 4 < 4096:
            await message.answer(base + "\n\n━━━━━━━━━━━━━━━━━━━━\n" + block, reply_markup=markup,
                                 disable_web_page_preview=True)
        else:
            if base:
                await message.answer(base, disable_web_page_preview=True)
            await message.answer(block, reply_markup=markup, disable_web_page_preview=True)

    @router.message(Command("help", "faq", "reminders_help", "помощь"))
    async def cmd_help(message: Message):
        await send_help(message)

    @router.callback_query(F.data == "show_faq")
    async def cb_help(callback: CallbackQuery):
        await callback.answer()
        await send_help(callback.message.model_copy(update={"from_user": callback.from_user}))

    @router.message(F.text, F.chat.type == "private", _NoState(), F.text.func(is_help_request))
    async def text_help(message: Message):
        await send_help(message)

    @router.message(F.text, F.chat.type != "private", F.text.regexp(r"(?i)^\s*бот[,!\s]+"),
                    F.text.func(is_help_request))
    async def group_help(message: Message):
        await send_help(message)

    @router.message(Command("reminders"))
    @router.message(F.text, F.chat.type == "private", _NoState(), F.text.regexp(LIST_RX))
    async def list_reminders(message: Message):
        locale = await loc(message.from_user.id)
        text, kb = list_view(await svc.active(message.from_user.id), locale)
        await message.answer(text, reply_markup=kb)

    @router.message(Command("remind"))
    @router.message(F.text, F.chat.type == "private", _NoState(), F.text.regexp(TRIGGER))
    async def create_reminder(message: Message):
        locale = await loc(message.from_user.id)
        raw = message.text or ""
        if raw.startswith("/remind") and not raw.strip().split(maxsplit=1)[1:]:
            await message.answer(HELP_EN if is_en(locale) else HELP_RU)
            return
        p = parse_reminder(raw)
        if p is None:
            return
        if p.error:
            hint = "" if p.error in ("limit", "past", "too_far") else "\n\n" + (HELP_EN if is_en(locale) else HELP_RU)
            await message.answer("🤔 " + error_text(p.error, locale) + (hint if p.error == "empty" else ""))
            return
        made = await svc.create(message.from_user.id, p)
        if isinstance(made, str):
            await message.answer(error_text(made, locale))
            return
        r = made[0]
        rep = f"\n🔁 {repeat_label(r.repeat, locale)}" if r.repeat else ""
        note = ""
        if not p.has_time and not r.repeat:
            note = ("\n<i>No time given — I'll remind you at 9:00</i>" if is_en(locale)
                    else "\n<i>Время не указано — напомню в 9:00</i>")
        if not r.text or r.text == "Напоминание":
            note += ("\n<i>Tip: add what to remind about — «remind me tomorrow at 10 to call Ann»</i>" if is_en(locale)
                     else "\n<i>Совет: добавьте, о чём напомнить — «напомни завтра в 10 позвонить Ане»</i>")
        title = "✅ I'll remind you" if is_en(locale) else "✅ Напомню"
        whens = ", ".join(f"<b>{when_label(x.due_at, locale=locale)}</b>" for x in made)
        kb = created_kb(r, locale) if len(made) == 1 else InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📋 My reminders" if is_en(locale) else "📋 Мои напоминания", callback_data="rmd:list")]])
        await message.answer(f"{title} {whens}{rep}\n📝 {html.escape(r.text)}{note}", reply_markup=kb)

    @router.callback_query(F.data.startswith("rmd:"))
    async def cb(callback: CallbackQuery):
        parts = callback.data.split(":")
        uid = callback.from_user.id
        locale = await loc(uid)
        en = is_en(locale)
        action = parts[1]
        if action == "list":
            text, kb = list_view(await svc.active(uid), locale)
            await callback.message.answer(text, reply_markup=kb)
            await callback.answer()
            return
        rid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        if action == "del":
            ok = await svc.cancel(uid, rid)
            await callback.answer(("Deleted" if en else "Удалено") if ok else ("Already gone" if en else "Уже нет"))
            if callback.message and callback.message.reply_markup and any(
                    b.callback_data == f"rmd:del:{rid}" for row in callback.message.reply_markup.inline_keyboard for b in row):
                if len(callback.message.reply_markup.inline_keyboard) > 1 or callback.message.text.startswith("⏰"):
                    text, kb = list_view(await svc.active(uid), locale)
                    try:
                        await callback.message.edit_text(text, reply_markup=kb)
                    except TelegramBadRequest:
                        pass
                else:
                    try:
                        await callback.message.edit_text(
                            callback.message.html_text + ("\n\n<i>Cancelled</i>" if en else "\n\n<i>Отменено</i>"))
                    except TelegramBadRequest:
                        pass
            return
        if action == "ok":
            await svc.done(uid, rid)
            await callback.answer("👍")
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
            return
        if action == "sn":
            minutes = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 15
            r = await svc.snooze(uid, rid, minutes)
            if not r:
                await callback.answer("Already gone" if en else "Напоминания уже нет", show_alert=True)
                return
            await callback.answer((f"Snoozed until {r.due_at:%H:%M}") if en else f"Напомню в {r.due_at:%H:%M}")
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
            return
        if action == "off":
            await svc.cancel(uid, rid)
            await callback.answer("Repeating stopped" if en else "Повтор отключён")
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass

    return router


async def _deliver(bot: Bot, svc: ReminderService, locale_fn: LocaleFn | None) -> int:
    sent = 0
    for r in await svc.due():
        locale = "ru"
        if locale_fn:
            try:
                locale = await _maybe(locale_fn(r.user_id)) or "ru"
            except Exception:
                pass
        late = now_minsk() - r.due_at
        note = ""
        if late > timedelta(minutes=10):
            note = (f"\n<i>Was due at {r.due_at:%H:%M} — the bot was asleep</i>" if is_en(locale)
                    else f"\n<i>Должно было прийти в {r.due_at:%H:%M} — бот просыпался</i>")
        title = "⏰ <b>Reminder</b>" if is_en(locale) else "⏰ <b>Напоминание</b>"
        text = f"{title}\n{html.escape(r.text)}{note}"
        try:
            await bot.send_message(r.user_id, text, reply_markup=reminder_kb(r, locale))
            await svc.mark_sent(r, ok=True)
            sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(e.retry_after, 30))
        except (TelegramForbiddenError,):
            await svc.mark_sent(r, ok=False, blocked=True)
        except TelegramBadRequest as e:
            await svc.mark_sent(r, ok=False, blocked="chat not found" in str(e).lower())
        except Exception as e:
            logger.warning(f"⏰ [Reminders] Не отправилось {r.user_id}: {e}")
            await svc.mark_sent(r, ok=False)
        await asyncio.sleep(0.05)
    return sent


async def _loop(bot: Bot, svc: ReminderService, locale_fn: LocaleFn | None) -> None:
    while True:
        try:
            n = await _deliver(bot, svc, locale_fn)
            if n:
                logger.info(f"⏰ [Reminders] Отправлено напоминаний: {n}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"⏰ [Reminders] Ошибка цикла: {e}", exc_info=True)
        await asyncio.sleep(POLL_SECONDS)


# ════════════════════════════════════════════════════════════════════════════
#                          API ДЛЯ МИНИ-АППА / САЙТА
# ════════════════════════════════════════════════════════════════════════════

def _read_token(token: str) -> dict | None:
    try:
        p64, s64 = token.split(".", 1)
        pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
        payload = pad(p64)
        if not hmac.compare_digest(hmac.new(SECRET.encode(), payload, hashlib.sha256).digest(), pad(s64)):
            return None
        data = json.loads(payload)
        return data if int(data.get("exp", 0)) >= _clock.time() else None
    except Exception:
        return None


def _uid(request) -> int | None:
    user = request.get("aves_user")
    if user:
        return int(user["uid"])
    h = request.headers.get("Authorization", "")
    data = _read_token(h[7:]) if h.startswith("Bearer ") and SECRET else None
    return int(data["uid"]) if data else None


def _json(data: Any, status: int = 200):
    from aiohttp import web
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


def _setup_api(app, svc: ReminderService) -> None:
    async def api_list(request):
        uid = _uid(request)
        if not uid:
            return _json({"error": "unauthorized", "message": "Войдите через Telegram."}, 401)
        items = await svc.active(uid)
        return _json({"items": [item_json(r) for r in items], "now": now_minsk().strftime("%Y-%m-%dT%H:%M")})

    async def api_create(request):
        uid = _uid(request)
        if not uid:
            return _json({"error": "unauthorized", "message": "Войдите через Telegram."}, 401)
        body = await request.json()
        raw = str(body.get("text") or "").strip()[:400]
        if body.get("date") and body.get("time"):
            # структурный ввод: дата, время, текст
            try:
                due = datetime.fromisoformat(f"{body['date']}T{body['time']}")
            except ValueError:
                return _json({"error": "bad_time", "message": "Неверная дата или время."}, 400)
            if due <= now_minsk():
                return _json({"error": "past", "message": error_text("past")}, 400)
            p = Parsed(due=due, text=raw or "Напоминание", has_time=True)
        else:
            if not TRIGGER.match(raw):
                raw = "напомни " + raw
            p = parse_reminder(raw)
            if p is None or p.error:
                code = p.error if p else "no_when"
                return _json({"error": code, "message": re.sub(r"<[^>]+>", "", error_text(code))}, 400)
        made = await svc.create(uid, p, source="web")
        if isinstance(made, str):
            return _json({"error": made, "message": error_text(made)}, 400)
        return _json({"item": item_json(made[0]), "items": [item_json(x) for x in made], "has_time": p.has_time})

    async def api_delete(request):
        uid = _uid(request)
        if not uid:
            return _json({"error": "unauthorized", "message": "Войдите через Telegram."}, 401)
        ok = await svc.cancel(uid, int(request.match_info["rid"]))
        return _json({"ok": ok})

    app.router.add_get("/api/reminders", api_list)
    app.router.add_post("/api/reminders", api_create)
    app.router.add_post("/api/reminders/{rid:\\d+}/delete", api_delete)


# ════════════════════════════════════════════════════════════════════════════
#                                ПОДКЛЮЧЕНИЕ
# ════════════════════════════════════════════════════════════════════════════

def setup_reminders(
    app,
    dp: Dispatcher,
    bot: Bot,
    session_maker,
    *,
    bot_code: str,
    help_text: HelpText | None = None,
    help_markup=None,
    locale_fn: LocaleFn | None = None,
    help_extra: str | None = None,
    web_api: bool = True,
) -> ReminderService:
    """
    app          — aiohttp-приложение бота (None — без API для сайта)
    session_maker — async_sessionmaker базы этого бота
    bot_code     — bio / fsk / fmo / study / work: отделяет напоминания ботов с общей базой
    help_text    — функция message → текст справки бота (FAQ), к нему добавится блок про напоминания
    help_markup  — клавиатура под справкой (или функция message → клавиатура)
    locale_fn    — функция user_id → "ru"/"en" (для англоязычного потока AvesGlobe)
    """
    svc = ReminderService(session_maker, bot_code)
    router = _make_router(svc, help_text, help_markup, locale_fn, help_extra)
    dp.include_router(router)
    dp.sub_routers.remove(router)
    dp.sub_routers.insert(0, router)        # раньше «всеядных» текстовых обработчиков бота

    tasks: list[asyncio.Task] = []

    async def _start(*_a, **_k):
        try:
            engine = session_maker.kw["bind"]
            async with engine.begin() as conn:
                await conn.run_sync(RemindersBase.metadata.create_all)
        except Exception as e:
            logger.error(f"⏰ [Reminders] Не удалось создать таблицу: {e}")
        tasks.append(asyncio.create_task(_loop(bot, svc, locale_fn)))
        logger.info(f"⏰ [Reminders] Напоминания включены (bot={bot_code})")

    async def _stop(*_a, **_k):
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    dp.startup.register(_start)
    dp.shutdown.register(_stop)
    if app is not None and web_api:
        _setup_api(app, svc)
    return svc
