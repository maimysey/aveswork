"""
Подбор аудитории для смены.

Кому и как сообщаем о смене:
  push  — полная карточка в самом AvesWork (студент нажимал «Старт» и не выключал уведомления);
  relay — одна короткая строка через бота расписания его факультета
          (студент AvesWork не запускал или запускал только сайт). Лимиты и тихие часы — в services/relay.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time

from sqlalchemy import select

from config import WORK_MODE, WORK_RELAY_BOTS, is_tester
from database import FACULTY_SESSIONS, work_session_maker
from models import Employer, UserBusySlot, WorkPref, WorkUser
from services.availability import (  # noqa: F401 — реэкспорт для старого кода
    DEFAULT_TIMESLOTS,
    FACULTY_TIMESLOTS,
    StudentRef,
    faculty_order,
    free_students,
    get_conflicting_slots,
)


@dataclass
class Audience:
    potential: set[int] = field(default_factory=set)   # свободны по расписанию
    push: set[int] = field(default_factory=set)        # полная карточка в AvesWork
    relay: dict[int, str] = field(default_factory=dict)  # uid → факультет, чей бот пришлёт короткое уведомление
    breakdown: list[dict] = field(default_factory=list)


def relay_allowed_for(uid: int) -> bool:
    return WORK_MODE == "live" or is_tester(uid)


def split_off(raw: str | None) -> set[str]:
    return {x.strip() for x in (raw or "").split(",") if x.strip()}


def considered_faculties(registered: dict[str, StudentRef], schedules_off: set[str]) -> list[str]:
    """Чьи расписания проверяем: все, кроме выключенных студентом."""
    return [f for f in faculty_order(registered) if f not in schedules_off]


def relay_channel(registered: dict[str, StudentRef], preferred: str | None) -> str | None:
    """Через какого бота писать. Человек в нескольких ботах получает одно сообщение, не несколько."""
    options = [f for f in faculty_order(registered) if f in WORK_RELAY_BOTS]
    if preferred in options:
        return preferred
    return options[0] if options else None


async def load_prefs(session, uids) -> dict[int, WorkPref]:
    if not uids:
        return {}
    return {
        p.telegram_id: p
        for p in (await session.execute(select(WorkPref).where(WorkPref.telegram_id.in_(uids)))).scalars().all()
    }


async def build_audience(
    shifts: list[dict],
    shift_start: time,
    shift_end: time,
    require_all_days: bool = False,
) -> Audience:
    if not shifts:
        return Audience()

    # 1. Кто свободен по расписанию — на каждый день смены и по каждому факультету отдельно
    registered: dict[int, dict[str, StudentRef]] = {}
    free_by_day: list[dict[str, set[int]]] = []
    for s in shifts:
        day_free: dict[str, set[int]] = {}
        for faculty in FACULTY_SESSIONS:
            reg, free = await free_students(faculty, s["target_date"], shift_start, shift_end)
            for uid, ref in reg.items():
                registered.setdefault(uid, {})[faculty] = ref
            day_free[faculty] = free
        free_by_day.append(day_free)

    everyone = set(registered)
    if not everyone:
        return Audience(breakdown=[
            {"target_date": s["target_date"], "day_of_week": s["day_of_week"],
             "potential_count": 0, "ready_count": 0, "relay_count": 0}
            for s in shifts
        ])

    # 2. Данные AvesWork: работодатели, профили, личные блокировки, настройки
    async with work_session_maker() as session:
        employer_ids = set((await session.execute(
            select(Employer.telegram_id).where(Employer.is_active == True)  # noqa: E712
        )).scalars().all())

        profiles = {
            row.telegram_id: row
            for row in (await session.execute(
                select(WorkUser.telegram_id, WorkUser.is_active, WorkUser.notifications_enabled, WorkUser.bot_started)
                .where(WorkUser.telegram_id.in_(everyone))
            )).all()
        }
        prefs = await load_prefs(session, everyone)

        personally_busy: list[set[int]] = []
        for s in shifts:
            personally_busy.append(set((await session.execute(
                select(UserBusySlot.user_id)
                .where(UserBusySlot.user_id.in_(everyone))
                .where(UserBusySlot.day_of_week == s["day_of_week"])
                .where(UserBusySlot.start_time < shift_end)
                .where(UserBusySlot.end_time > shift_start)
            )).scalars().all()))

    def off(uid: int) -> set[str]:
        p = prefs.get(uid)
        return split_off(p.schedules_off) if p else set()

    def free_on(uid: int, day_free: dict[str, set[int]]) -> bool:
        # Несколько ботов расписания: свободен, только если свободен по каждому учитываемому
        return all(uid in day_free.get(f, set()) for f in considered_faculties(registered[uid], off(uid)))

    def wants_push(uid: int) -> bool:
        p = profiles.get(uid)
        return bool(p and p.is_active and p.notifications_enabled and p.bot_started)

    def channel(uid: int) -> str | None:
        p = profiles.get(uid)
        if p and not (p.is_active and p.notifications_enabled):
            return None  # поставил поиск на паузу или выключил уведомления о работе
        pref = prefs.get(uid)
        if pref and pref.relay_enabled is False:
            return None
        if not relay_allowed_for(uid):
            return None
        fac = relay_channel(registered[uid], pref.relay_faculty if pref else None)
        if not fac or not registered[uid][fac].notifications:
            return None
        return fac

    potential_days = [{uid for uid in everyone if free_on(uid, df)} - employer_ids for df in free_by_day]
    ready_days = [pot - busy for pot, busy in zip(potential_days, personally_busy)]

    combine = set.intersection if require_all_days else set.union
    potential = combine(*potential_days)
    ready = combine(*ready_days)

    push = {uid for uid in ready if wants_push(uid)}
    relay = {}
    for uid in ready - push:
        fac = channel(uid)
        if fac:
            relay[uid] = fac

    breakdown = []
    for s, pot, rdy in zip(shifts, potential_days, ready_days):
        breakdown.append({
            "target_date": s["target_date"],
            "day_of_week": s["day_of_week"],
            "potential_count": len(pot),
            "ready_count": sum(1 for uid in rdy if wants_push(uid)),
            "relay_count": sum(1 for uid in rdy if not wants_push(uid) and channel(uid)),
        })

    return Audience(potential=potential, push=push, relay=relay, breakdown=breakdown)


# ==================== СОВМЕСТИМОСТЬ СО СТАРЫМ КОДОМ ====================

async def find_available_candidates_batch(
    shifts: list[dict],
    shift_start: time,
    shift_end: time,
    require_all_days: bool = False,
) -> tuple[int, list[int], list[dict]]:
    aud = await build_audience(shifts, shift_start, shift_end, require_all_days)
    return len(aud.potential), list(aud.push), aud.breakdown


async def find_available_candidates(
    target_date: date,
    day_of_week: int,
    shift_start: time,
    shift_end: time,
) -> tuple[int, list[int]]:
    aud = await build_audience(
        [{"target_date": target_date, "day_of_week": day_of_week}], shift_start, shift_end
    )
    return len(aud.potential), list(aud.push)
