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
    free_students,
    get_conflicting_slots,
)


@dataclass
class Audience:
    potential: set[int] = field(default_factory=set)   # свободны по расписанию
    push: set[int] = field(default_factory=set)        # полная карточка в AvesWork
    relay: dict[int, str] = field(default_factory=dict)  # uid → факультет для короткого уведомления
    breakdown: list[dict] = field(default_factory=list)


def relay_allowed_for(uid: int) -> bool:
    return WORK_MODE == "live" or is_tester(uid)


async def build_audience(
    shifts: list[dict],
    shift_start: time,
    shift_end: time,
    require_all_days: bool = False,
) -> Audience:
    if not shifts:
        return Audience()

    # 1. Кто свободен по расписанию — отдельно на каждый день смены
    per_day: list[dict[int, StudentRef]] = []
    for s in shifts:
        day_map: dict[int, StudentRef] = {}
        for faculty in FACULTY_SESSIONS:
            for uid, ref in (await free_students(faculty, s["target_date"], shift_start, shift_end)).items():
                day_map.setdefault(uid, ref)
        per_day.append(day_map)

    refs: dict[int, StudentRef] = {}
    for d in per_day:
        for uid, ref in d.items():
            refs.setdefault(uid, ref)
    everyone = set(refs)
    if not everyone:
        return Audience(breakdown=[
            {"target_date": s["target_date"], "day_of_week": s["day_of_week"],
             "potential_count": 0, "ready_count": 0, "relay_count": 0}
            for s in shifts
        ])

    # 2. Данные AvesWork: работодатели, профили, личные блокировки, отписки
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
        relay_off = set((await session.execute(
            select(WorkPref.telegram_id)
            .where(WorkPref.telegram_id.in_(everyone))
            .where(WorkPref.relay_enabled == False)  # noqa: E712
        )).scalars().all())

        personally_busy: list[set[int]] = []
        for s in shifts:
            personally_busy.append(set((await session.execute(
                select(UserBusySlot.user_id)
                .where(UserBusySlot.user_id.in_(everyone))
                .where(UserBusySlot.day_of_week == s["day_of_week"])
                .where(UserBusySlot.start_time < shift_end)
                .where(UserBusySlot.end_time > shift_start)
            )).scalars().all()))

    def wants_push(uid: int) -> bool:
        p = profiles.get(uid)
        return bool(p and p.is_active and p.notifications_enabled and p.bot_started)

    def wants_relay(uid: int) -> bool:
        p = profiles.get(uid)
        if p and not (p.is_active and p.notifications_enabled):
            return False  # поставил поиск на паузу или выключил уведомления о работе
        ref = refs[uid]
        return (
            uid not in relay_off
            and ref.notifications
            and ref.faculty in WORK_RELAY_BOTS
            and relay_allowed_for(uid)
        )

    potential_days = [set(d) - employer_ids for d in per_day]
    ready_days = [pot - busy for pot, busy in zip(potential_days, personally_busy)]

    combine = set.intersection if require_all_days else set.union
    potential = combine(*potential_days)
    ready = combine(*ready_days)

    push = {uid for uid in ready if wants_push(uid)}
    relay = {uid: refs[uid].faculty for uid in ready if uid not in push and wants_relay(uid)}

    breakdown = []
    for s, pot, rdy in zip(shifts, potential_days, ready_days):
        breakdown.append({
            "target_date": s["target_date"],
            "day_of_week": s["day_of_week"],
            "potential_count": len(pot),
            "ready_count": sum(1 for uid in rdy if wants_push(uid)),
            "relay_count": sum(1 for uid in rdy if not wants_push(uid) and wants_relay(uid)),
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
