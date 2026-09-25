from datetime import time, date
from sqlalchemy import select
from config import logger
from database import FACULTY_SESSIONS, work_session_maker
from models import FacultyUser, FacultyLesson, WorkUser, UserBusySlot

# Официальная сетка пар БГУ
BSU_TIMESLOTS: dict[int, tuple[time, time]] = {
    1: (time(8, 30), time(9, 50)),
    2: (time(10, 5), time(11, 25)),
    3: (time(11, 40), time(13, 0)),
    4: (time(13, 30), time(14, 50)),
    5: (time(15, 5), time(16, 25)),
    6: (time(16, 40), time(18, 0)),
    7: (time(18, 15), time(19, 35)),
    8: (time(19, 50), time(21, 10)),
}


def get_conflicting_slots(
    shift_start: time, shift_end: time, travel_buffer_minutes: int = 15
) -> set[int]:
    conflicting_slots = set()
    shift_start_m = shift_start.hour * 60 + shift_start.minute
    shift_end_m = shift_end.hour * 60 + shift_end.minute

    for slot_id, (s_start, s_end) in BSU_TIMESLOTS.items():
        slot_start_m = (s_start.hour * 60 + s_start.minute) - travel_buffer_minutes
        slot_end_m = (s_end.hour * 60 + s_end.minute) + travel_buffer_minutes

        # Проверка пересечения двух временных интервалов
        if max(slot_start_m, shift_start_m) < min(slot_end_m, shift_end_m):
            conflicting_slots.add(slot_id)

    return conflicting_slots


async def find_available_candidates(
    target_date: date,
    day_of_week: int,
    shift_start: time,
    shift_end: time,
) -> tuple[int, list[int]]:
    conflicting_slots = get_conflicting_slots(shift_start, shift_end)
    all_free_student_ids: set[int] = set()

    # ==================== ЭТАП 1: СКАНИРОВАНИЕ БАЗ ФАКУЛЬТЕТОВ ====================
    for faculty_code, session_maker in FACULTY_SESSIONS.items():
        try:
            async with session_maker() as session:
                # 1. Получаем студентов факультета с привязанной группой
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

                # 2. Если конфликтных пар нет (например, смена поздним вечером) — свободны все
                if not conflicting_slots:
                    for s_id, _, _ in faculty_students:
                        all_free_student_ids.add(s_id)
                    continue

                # 3. Ищем группы, у которых в этот день есть пары в конфликтные слоты
                busy_res = await session.execute(
                    select(FacultyLesson.group_id, FacultyLesson.subgroup)
                    .where(FacultyLesson.day == day_of_week)
                    .where(FacultyLesson.slot_id.in_(conflicting_slots))
                )
                busy_pairs = set(busy_res.all())

                # 4. Вычисляем свободных студентов
                for s_id, g_id, sub_g in faculty_students:
                    is_busy = (
                        (g_id, None) in busy_pairs       # Пара у всей группы
                        or (g_id, sub_g) in busy_pairs   # Пара у этой подгруппы
                        or (g_id, 0) in busy_pairs       # Пара у общего потока
                    )
                    if not is_busy:
                        all_free_student_ids.add(s_id)

        except Exception as e:
            logger.error(
                f"⚠️ Ошибка при чтении базы факультета '{faculty_code}': {e}"
            )

    total_potential_count = len(all_free_student_ids)

    if not all_free_student_ids:
        return 0, []

    # ==================== ЭТАП 2: ФИЛЬТРАЦИЯ ПО БАЗЕ AVESWORK ====================
    ready_user_ids: list[int] = []

    async with work_session_maker() as work_session:
        # Ищем зарегистрированных соискателей среди свободных студентов
        active_users_res = await work_session.execute(
            select(WorkUser.telegram_id)
            .where(WorkUser.telegram_id.in_(all_free_student_ids))
            .where(WorkUser.is_active == True)
            .where(WorkUser.notifications_enabled == True)
        )
        registered_candidate_ids = set(active_users_res.scalars().all())

        if not registered_candidate_ids:
            return total_potential_count, []

        # Проверяем личные занятые часы студентов (спортзал, курсы, сон)
        busy_slots_res = await work_session.execute(
            select(UserBusySlot.user_id)
            .where(UserBusySlot.user_id.in_(registered_candidate_ids))
            .where(UserBusySlot.day_of_week == day_of_week)
            .where(UserBusySlot.start_time < shift_end)
            .where(UserBusySlot.end_time > shift_start)
        )
        personally_busy_ids = set(busy_slots_res.scalars().all())

        # Финальный пул адресатов для отправки пуша
        final_targets = registered_candidate_ids - personally_busy_ids
        ready_user_ids = list(final_targets)

    return total_potential_count, ready_user_ids