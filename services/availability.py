"""
Свободное время студентов по расписанию факультета.

Расписание в базах ботов хранится по неделям (weeks → lessons). Для конкретной
даты берём неделю этой даты, а если её ещё не опубликовали — последнюю
опубликованную за три недели до неё (как делает сам бот расписания).
"""
from __future__ import annotations

import time as _clock
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, time, timedelta

from sqlalchemy import select

from config import TRAVEL_BUFFER_MIN, logger
from database import FACULTY_SESSIONS
from models import FacultyGroup, FacultyLesson, FacultyUser, FacultyWeek

# ==================== СЕТКИ ЗВОНКОВ ====================

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

WEEK_LOOKBACK_DAYS = 21
_WEEK_CACHE_TTL = 300
_STUDENT_CACHE_TTL = 120


def grid(faculty: str) -> dict[int, tuple[time, time]]:
    return FACULTY_TIMESLOTS.get(faculty, DEFAULT_TIMESLOTS)


def to_min(t: time) -> int:
    return t.hour * 60 + t.minute


def hhmm(t: time) -> str:
    return t.strftime("%H:%M")


def get_conflicting_slots(
    faculty_code: str,
    shift_start: time,
    shift_end: time,
    travel_buffer_minutes: int = TRAVEL_BUFFER_MIN,
) -> set[int]:
    """Номера пар, которые мешают выйти на смену (с запасом на дорогу)."""
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

    rows = (await session.execute(
        select(FacultyLesson.group_id, FacultyWeek.id, FacultyWeek.start_date)
        .join(FacultyWeek, FacultyWeek.id == FacultyLesson.week_id)
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


async def lessons_on(
    session,
    faculty: str,
    target: date,
    slot_ids: set[int] | None = None,
    group_ids: set[int] | None = None,
) -> dict[int, list[tuple[int | None, int]]]:
    """group_id → [(подгруппа, номер пары)] на конкретную дату."""
    weeks = await best_weeks(session, faculty, target)
    if group_ids is not None:
        weeks = {g: w for g, w in weeks.items() if g in group_ids}
    if not weeks:
        return {}

    q = (
        select(FacultyLesson.group_id, FacultyLesson.week_id, FacultyLesson.subgroup, FacultyLesson.slot_id)
        .where(FacultyLesson.week_id.in_(set(weeks.values())))
        .where(FacultyLesson.day == target.weekday())
    )
    if slot_ids is not None:
        q = q.where(FacultyLesson.slot_id.in_(slot_ids))
    if group_ids is not None:
        q = q.where(FacultyLesson.group_id.in_(group_ids))

    out: dict[int, list[tuple[int | None, int]]] = defaultdict(list)
    for gid, wid, sub, slot in (await session.execute(q)).all():
        if weeks.get(gid) == wid:
            out[gid].append((sub, slot))
    return out


# ==================== СТУДЕНТЫ ====================

@dataclass(slots=True)
class StudentRef:
    uid: int
    faculty: str
    group_id: int
    subgroup: int | None
    name: str | None
    username: str | None
    notifications: bool


def _ref(faculty: str, row) -> StudentRef:
    return StudentRef(
        uid=row.telegram_id,
        faculty=faculty,
        group_id=row.group_id,
        subgroup=row.subgroup,
        name=row.first_name,
        username=row.username,
        notifications=row.notifications_enabled is not False,
    )


_USER_COLS = (
    FacultyUser.telegram_id, FacultyUser.group_id, FacultyUser.subgroup,
    FacultyUser.first_name, FacultyUser.username, FacultyUser.notifications_enabled,
)


async def free_students(faculty: str, target: date, start: time, end: time) -> dict[int, StudentRef]:
    """Все студенты факультета с выбранной группой, у которых нет пар, мешающих смене."""
    session_maker = FACULTY_SESSIONS.get(faculty)
    if not session_maker:
        return {}
    conflicts = get_conflicting_slots(faculty, start, end)
    try:
        async with session_maker() as session:
            users = (await session.execute(
                select(*_USER_COLS).where(FacultyUser.group_id.is_not(None))
            )).all()
            if not users:
                return {}
            busy = await lessons_on(session, faculty, target, conflicts) if conflicts else {}
    except Exception as e:
        logger.error(f"⚠️ Ошибка базы факультета '{faculty}': {e}")
        return {}

    out: dict[int, StudentRef] = {}
    for row in users:
        lessons = busy.get(row.group_id)
        if lessons and blocking_slots(row.subgroup, lessons):
            continue
        out[row.telegram_id] = _ref(faculty, row)
    return out


_student_cache: dict[int, tuple[float, StudentRef | None]] = {}


async def find_student(uid: int, prefer: str | None = None) -> StudentRef | None:
    """В каком боте расписания студент выбрал группу. prefer — подсказка сайта (?f=bio)."""
    hit = _student_cache.get(uid)
    if hit and _clock.monotonic() - hit[0] < _STUDENT_CACHE_TTL:
        return hit[1]

    order = sorted(FACULTY_SESSIONS, key=lambda f: f != prefer)
    found: StudentRef | None = None
    for faculty in order:
        try:
            async with FACULTY_SESSIONS[faculty]() as session:
                row = (await session.execute(
                    select(*_USER_COLS).where(FacultyUser.telegram_id == uid)
                )).first()
        except Exception as e:
            logger.warning(f"⚠️ Не удалось проверить студента в '{faculty}': {e}")
            continue
        if row and row.group_id:
            found = _ref(faculty, row)
            break

    if len(_student_cache) > 5000:
        _student_cache.clear()
    _student_cache[uid] = (_clock.monotonic(), found)
    return found


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


async def student_schedule(ref: StudentRef, dates: list[date]) -> dict[date, list[int]]:
    """Номера пар студента на каждую из дат (с учётом подгруппы)."""
    if not dates:
        return {}
    session_maker = FACULTY_SESSIONS.get(ref.faculty)
    if not session_maker:
        return {d: [] for d in dates}

    async with session_maker() as session:
        week_for: dict[date, int | None] = {}
        for d in dates:
            weeks = await best_weeks(session, ref.faculty, d)
            week_for[d] = weeks.get(ref.group_id)
        week_ids = {w for w in week_for.values() if w is not None}
        rows = []
        if week_ids:
            rows = (await session.execute(
                select(FacultyLesson.week_id, FacultyLesson.day, FacultyLesson.subgroup, FacultyLesson.slot_id)
                .where(FacultyLesson.group_id == ref.group_id)
                .where(FacultyLesson.week_id.in_(week_ids))
            )).all()

    by_week_day: dict[tuple[int, int], list[tuple[int | None, int]]] = defaultdict(list)
    for wid, day, sub, slot in rows:
        by_week_day[(wid, day)].append((sub, slot))

    return {
        d: blocking_slots(ref.subgroup, by_week_day.get((week_for[d], d.weekday()), []))
        if week_for[d] is not None else []
        for d in dates
    }
