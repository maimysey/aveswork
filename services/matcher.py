from datetime import time, date
from sqlalchemy import select
from config import logger
from database import FACULTY_SESSIONS, work_session_maker
from models import FacultyUser, FacultyLesson, WorkUser, UserBusySlot, Employer

# ==================== ФАКУЛЬТЕТСКИЕ СЕТКИ ЗВОНКОВ ====================

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


def get_conflicting_slots(
    faculty_code: str,
    shift_start: time,
    shift_end: time,
    travel_buffer_minutes: int = 15,
) -> set[int]:
    grid = FACULTY_TIMESLOTS.get(faculty_code, DEFAULT_TIMESLOTS)
    conflicting_slots = set()

    shift_start_m = shift_start.hour * 60 + shift_start.minute
    shift_end_m = shift_end.hour * 60 + shift_end.minute

    for slot_id, (s_start, s_end) in grid.items():
        slot_start_m = (s_start.hour * 60 + s_start.minute) - travel_buffer_minutes
        slot_end_m = (s_end.hour * 60 + s_end.minute) + travel_buffer_minutes

        if max(slot_start_m, shift_start_m) < min(slot_end_m, shift_end_m):
            conflicting_slots.add(slot_id)

    return conflicting_slots


async def _get_single_day_free_candidates(
    day_of_week: int,
    shift_start: time,
    shift_end: time,
) -> tuple[set[int], set[int]]:
    faculty_free_ids: set[int] = set()

    # 1. Сканируем базы факультетов БГУ
    for faculty_code, session_maker in FACULTY_SESSIONS.items():
        # Считаем конфликтные слоты именно для ЭТОГО факультета
        conflicting_slots = get_conflicting_slots(faculty_code, shift_start, shift_end)

        try:
            async with session_maker() as session:
                users_res = await session.execute(
                    select(
                        FacultyUser.telegram_id,
                        FacultyUser.group_id,
                        FacultyUser.subgroup,
                    ).where(FacultyUser.group_id.is_not(None))
                )
                faculty_students = users_res.all()

                if not faculty_students:
                    continue

                if not conflicting_slots:
                    for s_id, _, _ in faculty_students:
                        faculty_free_ids.add(s_id)
                    continue

                busy_res = await session.execute(
                    select(FacultyLesson.group_id, FacultyLesson.subgroup)
                    .where(FacultyLesson.day == day_of_week)
                    .where(FacultyLesson.slot_id.in_(conflicting_slots))
                )
                busy_pairs = set(busy_res.all())

                for s_id, g_id, sub_g in faculty_students:
                    is_busy = (
                        (g_id, None) in busy_pairs
                        or (g_id, sub_g) in busy_pairs
                        or (g_id, 0) in busy_pairs
                    )
                    if not is_busy:
                        faculty_free_ids.add(s_id)

        except Exception as e:
            logger.error(f"⚠️ Ошибка базы факультета '{faculty_code}': {e}")

    if not faculty_free_ids:
        return set(), set()

    # 2. Фильтруем зарегистрированных соискателей AvesWork
    async with work_session_maker() as work_session:
        active_res = await work_session.execute(
            select(WorkUser.telegram_id)
            .where(WorkUser.telegram_id.in_(faculty_free_ids))
            .where(WorkUser.is_active == True)
            .where(WorkUser.notifications_enabled == True)
        )
        candidates = set(active_res.scalars().all())

        if not candidates:
            return faculty_free_ids, set()

        # Находим всех работодателей, чтобы исключить их из рассылки соискателей
        employers_res = await work_session.execute(
            select(Employer.telegram_id).where(Employer.is_active == True)
        )
        employer_ids = set(employers_res.scalars().all())

        # Исключаем личные блокировки времени
        busy_res = await work_session.execute(
            select(UserBusySlot.user_id)
            .where(UserBusySlot.user_id.in_(candidates))
            .where(UserBusySlot.day_of_week == day_of_week)
            .where(UserBusySlot.start_time < shift_end)
            .where(UserBusySlot.end_time > shift_start)
        )
        personally_busy = set(busy_res.scalars().all())

        # Вычитаем и личные дела, и аккаунты работодателей
        ready_ids = candidates - personally_busy - employer_ids
        faculty_free_ids = faculty_free_ids - employer_ids

    return faculty_free_ids, ready_ids


async def find_available_candidates_batch(
    shifts: list[dict],
    shift_start: time,
    shift_end: time,
    require_all_days: bool = False,
) -> tuple[int, list[int], list[dict]]:
    if not shifts:
        return 0, [], []

    per_day_potential: list[set[int]] = []
    per_day_ready: list[set[int]] = []
    breakdown: list[dict] = []

    for s in shifts:
        pot_set, ready_set = await _get_single_day_free_candidates(
            day_of_week=s["day_of_week"],
            shift_start=shift_start,
            shift_end=shift_end,
        )
        per_day_potential.append(pot_set)
        per_day_ready.append(ready_set)

        breakdown.append({
            "target_date": s["target_date"],
            "day_of_week": s["day_of_week"],
            "potential_count": len(pot_set),
            "ready_count": len(ready_set),
        })

    if require_all_days:
        final_potential = set.intersection(*per_day_potential) if per_day_potential else set()
        final_ready = set.intersection(*per_day_ready) if per_day_ready else set()
    else:
        final_potential = set.union(*per_day_potential) if per_day_potential else set()
        final_ready = set.union(*per_day_ready) if per_day_ready else set()

    return len(final_potential), list(final_ready), breakdown


async def find_available_candidates(
    target_date: date,
    day_of_week: int,
    shift_start: time,
    shift_end: time,
) -> tuple[int, list[int]]:
    pot_set, ready_set = await _get_single_day_free_candidates(
        day_of_week, shift_start, shift_end
    )
    return len(pot_set), list(ready_set)