"""
Свободное время студентов по расписанию факультетов.

Расписание в базах ботов хранится по неделям (weeks → lessons). Для конкретной
даты берём неделю этой даты, а если её ещё не опубликовали — последнюю
опубликованную за три недели до неё (как делает сам бот расписания).

Факультеты бывают двух видов:
  • по сетке пар (биофак, ФСК): у пары номер, время берём из сетки звонков;
  • с точным временем (ФМО / AvesGlobe): у пары своё time_start/time_end,
    языковые подгруппы (track + label_key) и дисциплины по выбору (choice_key).

Один человек может быть в нескольких ботах расписания (как в AvesStudy).
Тогда он свободен, только если свободен по КАЖДОМУ своему расписанию;
лишнее расписание можно выключить в «Мои часы» на сайте (WorkPref.schedules_off).
"""
from __future__ import annotations

import json
import time as _clock
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, time, timedelta

from sqlalchemy import select

from config import TRAVEL_BUFFER_MIN, logger
from database import FACULTY_SESSIONS
from models import (
    FacultyGroup, FacultyLesson, FacultyUser, FacultyWeek, FmoLesson, FmoUser,
)

# ==================== ФАКУЛЬТЕТЫ ====================

FACULTY_TIMESLOTS: dict[str, dict[int, tuple[time, time]]] = {
    # Биофак — начало пар в 09:00
    "bio": {
        1: (time(9, 0), time(10, 25)),
        2: (time(10, 35), time(12, 0)),
        3: (time(12, 10), time(13, 35)),
        4: (time(14, 0), time(15, 25)),
        5: (time(15, 35), time(17, 0)),
        6: (time(17, 10), time(18, 35)),
        7: (time(18, 45), time(20, 10)),
        8: (time(20, 30), time(21, 55)),
    },
    # ФСК — стандартная сетка БГУ с 08:30
    "fsk": {
        1: (time(8, 30), time(9, 50)),
        2: (time(10, 5), time(11, 25)),
        3: (time(11, 40), time(13, 0)),
        4: (time(13, 30), time(14, 50)),
        5: (time(15, 5), time(16, 25)),
        6: (time(16, 40), time(18, 0)),
        7: (time(18, 15), time(19, 35)),
        8: (time(19, 50), time(21, 10)),
    },
}

# Сетка по умолчанию для новых факультетов
DEFAULT_TIMESLOTS = FACULTY_TIMESLOTS["fsk"]

# У этих факультетов у каждой пары своё точное время
EXACT_TIME_FACULTIES = {"fmo"}

FACULTY_SHORT = {"bio": "Биофак", "fsk": "ФСК", "fmo": "ФМО"}
FACULTY_BOT = {"bio": "AvesBio", "fsk": "AvesFlow", "fmo": "AvesGlobe"}
# Порядок, в котором выбираем «основной» факультет, если человек есть в нескольких ботах
FACULTY_ORDER = ["bio", "fsk", "fmo"]

WEEK_LOOKBACK_DAYS = 21
_WEEK_CACHE_TTL = 300
_STUDENT_CACHE_TTL = 120


def grid(faculty: str) -> dict[int, tuple[time, time]]:
    return FACULTY_TIMESLOTS.get(faculty, DEFAULT_TIMESLOTS)


def to_min(t: time) -> int:
    return t.hour * 60 + t.minute


def hhmm(t: time) -> str:
    return t.strftime("%H:%M")


def m2s(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _parse_min(raw) -> int | None:
    """«9:30», «09:30», «09:30:00» или time → минуты от полуночи."""
    if raw is None:
        return None
    if isinstance(raw, time):
        return to_min(raw)
    try:
        h, m = str(raw).strip().split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return None


def faculty_order(faculties) -> list[str]:
    return sorted(faculties, key=lambda f: (FACULTY_ORDER.index(f) if f in FACULTY_ORDER else 99, f))


def get_conflicting_slots(
    faculty_code: str,
    shift_start: time,
    shift_end: time,
    travel_buffer_minutes: int = TRAVEL_BUFFER_MIN,
) -> set[int]:
    """Номера пар сетки, которые мешают выйти на смену (с запасом на дорогу)."""
    s, e = to_min(shift_start), to_min(shift_end)
    out = set()
    for slot_id, (a, b) in grid(faculty_code).items():
        if max(to_min(a) - travel_buffer_minutes, s) < min(to_min(b) + travel_buffer_minutes, e):
            out.add(slot_id)
    return out


def blocking_slots(subgroup: int | None, lessons: list[tuple[int | None, int]]) -> list[int]:
    """Какие из пар группы касаются студента. Без подгруппы считаем, что касаются все."""
    mine = subgroup or 0
    return sorted({slot for sub, slot in lessons if not sub or not mine or sub == mine})


# ==================== СТУДЕНТ ====================

@dataclass(slots=True)
class StudentRef:
    uid: int
    faculty: str
    group_id: int
    subgroup: int | None
    name: str | None
    username: str | None
    notifications: bool
    tracks: dict = field(default_factory=dict)    # ФМО: {"lang1": ["Англ.3"]}
    choices: list = field(default_factory=list)   # ФМО: выбранные ДВС


def _json(v, default):
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def _ref(faculty: str, row) -> StudentRef:
    return StudentRef(
        uid=row.telegram_id,
        faculty=faculty,
        group_id=row.group_id,
        subgroup=getattr(row, "subgroup", None),
        name=row.first_name,
        username=row.username,
        notifications=row.notifications_enabled is not False,
        tracks=_json(getattr(row, "tracks", None), {}),
        choices=_json(getattr(row, "choices", None), []),
    )


def _user_query(faculty: str):
    if faculty in EXACT_TIME_FACULTIES:
        return select(
            FmoUser.telegram_id, FmoUser.group_id, FmoUser.first_name, FmoUser.username,
            FmoUser.notifications_enabled, FmoUser.tracks, FmoUser.choices,
        ), FmoUser
    return select(
        FacultyUser.telegram_id, FacultyUser.group_id, FacultyUser.subgroup,
        FacultyUser.first_name, FacultyUser.username, FacultyUser.notifications_enabled,
    ), FacultyUser


# ==================== ВЫБОР НЕДЕЛИ ====================

_week_cache: dict[tuple[str, date], tuple[float, dict[int, int]]] = {}


def _week_end(d: date) -> date:
    return d + timedelta(days=6 - d.weekday())


async def best_weeks(session, faculty: str, target: date) -> dict[int, int]:
    """group_id → week_id: неделя этой даты или последняя опубликованная до неё."""
    sunday = _week_end(target)
    key = (faculty, sunday)
    hit = _week_cache.get(key)
    if hit and _clock.monotonic() - hit[0] < _WEEK_CACHE_TTL:
        return hit[1]

    L = FmoLesson if faculty in EXACT_TIME_FACULTIES else FacultyLesson
    rows = (await session.execute(
        select(L.group_id, FacultyWeek.id, FacultyWeek.start_date)
        .join(FacultyWeek, FacultyWeek.id == L.week_id)
        .where(FacultyWeek.start_date <= sunday)
        .where(FacultyWeek.start_date > sunday - timedelta(days=WEEK_LOOKBACK_DAYS))
        .distinct()
    )).all()

    best: dict[int, tuple[date, int]] = {}
    for gid, wid, start in rows:
        if gid not in best or start > best[gid][0]:
            best[gid] = (start, wid)
    result = {gid: wid for gid, (_, wid) in best.items()}

    if len(_week_cache) > 64:
        _week_cache.clear()
    _week_cache[key] = (_clock.monotonic(), result)
    return result


async def _lesson_rows(session, faculty: str, weeks: dict[int, int], days: set[int] | None,
                       group_ids: set[int] | None) -> list:
    """Сырые строки пар нужных групп и недель. Возвращает (group_id, week_id, day, …данные пары)."""
    if not weeks:
        return []
    if faculty in EXACT_TIME_FACULTIES:
        L = FmoLesson
        q = select(L.group_id, L.week_id, L.day, L.time_start, L.time_end, L.track, L.label_key, L.choice_key)
    else:
        L = FacultyLesson
        q = select(L.group_id, L.week_id, L.day, L.subgroup, L.slot_id)
    q = q.where(L.week_id.in_(set(weeks.values())))
    if days is not None:
        q = q.where(L.day.in_(days))
    if group_ids is not None:
        q = q.where(L.group_id.in_(group_ids))
    return [r for r in (await session.execute(q)).all() if weeks.get(r[0]) == r[1]]


# ==================== КАКИЕ ПАРЫ КАСАЮТСЯ СТУДЕНТА ====================

Block = tuple[int, int, str]   # (начало, конец в минутах, подпись)


def student_blocks(ref: StudentRef, rows: list) -> list[Block]:
    """Пары одного дня одной группы → интервалы, которые действительно заняты у студента."""
    if ref.faculty in EXACT_TIME_FACULTIES:
        return _fmo_blocks(ref, rows)
    g = grid(ref.faculty)
    slots = blocking_slots(ref.subgroup, [(r[3], r[4]) for r in rows])
    return [(to_min(g[s][0]), to_min(g[s][1]), f"{s} пара, {hhmm(g[s][0])}–{hhmm(g[s][1])}") for s in slots if s in g]


def _fmo_blocks(ref: StudentRef, rows: list) -> list[Block]:
    """Логика как в боте ФМО (filter_for_user), но с перестраховкой:
       • пара без подгруппы — общая;
       • языковая — если её подгруппа выбрана в этом треке; трек не настроен — считаем занятым;
       • ДВС — если выбрана она или не выбрана ни одна из альтернатив в это же время."""
    picked = set(ref.choices or [])
    chosen_at: set[tuple[int, str]] = {(r[2], str(r[3])) for r in rows if r[7] and r[7] in picked}
    out: list[Block] = []
    for gid, wid, day, ts, te, track, label_key, choice_key in rows:
        if track and label_key:
            mine = (ref.tracks or {}).get(track)
            if mine and label_key not in mine:
                continue
        if choice_key and picked and choice_key not in picked and (day, str(ts)) in chosen_at:
            continue
        s, e = _parse_min(ts), _parse_min(te)
        if s is None:
            continue
        e = e if e and e > s else s + 80
        out.append((s, e, f"пара {m2s(s)}–{m2s(e)}"))
    return sorted(set(out))


def overlaps(blocks: list[Block], start: time, end: time, buffer: int = TRAVEL_BUFFER_MIN) -> Block | None:
    s, e = to_min(start), to_min(end)
    for b in blocks:
        if max(b[0] - buffer, s) < min(b[1] + buffer, e):
            return b
    return None


# ==================== МАССОВЫЙ ПОДБОР ====================

async def free_students(faculty: str, target: date, start: time, end: time) -> tuple[dict[int, StudentRef], set[int]]:
    """(все студенты факультета с группой, кто из них свободен в эту смену)."""
    session_maker = FACULTY_SESSIONS.get(faculty)
    if not session_maker:
        return {}, set()
    q, U = _user_query(faculty)
    try:
        async with session_maker() as session:
            users = (await session.execute(q.where(U.group_id.is_not(None)))).all()
            if not users:
                return {}, set()
            weeks = await best_weeks(session, faculty, target)
            rows = await _lesson_rows(session, faculty, weeks, {target.weekday()}, None)
    except Exception as e:
        logger.error(f"⚠️ Ошибка базы факультета '{faculty}': {e}")
        return {}, set()

    by_group: dict[int, list] = defaultdict(list)
    for r in rows:
        by_group[r[0]].append(r)

    registered: dict[int, StudentRef] = {}
    free: set[int] = set()
    for row in users:
        ref = _ref(faculty, row)
        registered[ref.uid] = ref
        if not overlaps(student_blocks(ref, by_group.get(ref.group_id, [])), start, end):
            free.add(ref.uid)
    return registered, free


# ==================== ОДИН СТУДЕНТ ====================

_student_cache: dict[int, tuple[float, list[StudentRef]]] = {}


async def find_students(uid: int) -> list[StudentRef]:
    """Во всех ботах расписания, где у человека выбрана группа (биофак → ФСК → ФМО)."""
    hit = _student_cache.get(uid)
    if hit and _clock.monotonic() - hit[0] < _STUDENT_CACHE_TTL:
        return hit[1]

    found: list[StudentRef] = []
    for faculty in faculty_order(FACULTY_SESSIONS):
        q, U = _user_query(faculty)
        try:
            async with FACULTY_SESSIONS[faculty]() as session:
                row = (await session.execute(q.where(U.telegram_id == uid))).first()
        except Exception as e:
            logger.warning(f"⚠️ Не удалось проверить студента в '{faculty}': {e}")
            continue
        if row and row.group_id:
            found.append(_ref(faculty, row))

    if len(_student_cache) > 5000:
        _student_cache.clear()
    _student_cache[uid] = (_clock.monotonic(), found)
    return found


async def find_student(uid: int, prefer: str | None = None) -> StudentRef | None:
    """Основной факультет: подсказка сайта (?f=bio), иначе первый по порядку."""
    refs = await find_students(uid)
    for r in refs:
        if r.faculty == prefer:
            return r
    return refs[0] if refs else None


def forget_student(uid: int) -> None:
    _student_cache.pop(uid, None)


async def group_label(ref: StudentRef) -> dict | None:
    try:
        async with FACULTY_SESSIONS[ref.faculty]() as session:
            g = await session.get(FacultyGroup, ref.group_id)
    except Exception:
        return None
    if not g:
        return None
    num = str(g.number).strip()
    tag = num if num.startswith(f"{g.course}-") else f"{g.course}-{num}"
    if g.study_mode == "Магистратура":
        tag = f"М{g.course}-{num}"
    return {"id": g.id, "course": g.course, "tag": tag}


async def student_schedule(ref: StudentRef, dates: list[date]) -> dict[date, list[Block]]:
    """Занятые парами интервалы студента на каждую из дат."""
    if not dates:
        return {}
    session_maker = FACULTY_SESSIONS.get(ref.faculty)
    if not session_maker:
        return {d: [] for d in dates}

    try:
        async with session_maker() as session:
            week_for: dict[date, int | None] = {}
            for d in dates:
                week_for[d] = (await best_weeks(session, ref.faculty, d)).get(ref.group_id)
            rows = []
            for wid in {w for w in week_for.values() if w is not None}:
                rows += await _lesson_rows(session, ref.faculty, {ref.group_id: wid}, None, {ref.group_id})
    except Exception as e:
        logger.warning(f"⚠️ Расписание '{ref.faculty}' недоступно: {e}")
        return {d: [] for d in dates}

    by_week_day: dict[tuple[int, int], list] = defaultdict(list)
    for r in rows:
        by_week_day[(r[1], r[2])].append(r)
    return {
        d: student_blocks(ref, by_week_day.get((week_for[d], d.weekday()), [])) if week_for[d] is not None else []
        for d in dates
    }


async def combined_schedule(refs: list[StudentRef], dates: list[date]) -> dict[date, list[Block]]:
    """Пары со всех учитываемых расписаний. При нескольких — подпись с факультетом."""
    out: dict[date, list[Block]] = {d: [] for d in dates}
    many = len(refs) > 1
    for ref in refs:
        for d, blocks in (await student_schedule(ref, dates)).items():
            for s, e, label in blocks:
                out[d].append((s, e, f"{FACULTY_SHORT.get(ref.faculty, ref.faculty)}: {label}" if many else label))
    return {d: sorted(v) for d, v in out.items()}
