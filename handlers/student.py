import html
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from config import logger
from database import work_session_maker
from models import WorkUser, UserBusySlot, Vacancy, VacancyDelivery, Employer
from services.nlp_parser import parse_user_busy_input
from services.metrics import metrics_service
from keyboards import (
    student_busy_menu_kb,
    busy_slots_list_kb,
    student_applied_kb,
)

router = Router()

DAYS_FULL = [
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"
]


class StudentBusyFSM(StatesGroup):
    waiting_for_busy_text = State()


# ==================== МЕНЮ ЗАНЯТЫХ ЧАСОВ ====================

@router.message(F.text == "⏳ Мои занятые часы")
async def cmd_busy_hours_menu(message: Message):
    text = (
        "⏳ <b>Управление личными занятыми часами</b>\n\n"
        "Бот автоматически сверяется с вашим расписанием пар в БГУ, но если вы заняты "
        "<b>тренировками, курсами или сном</b> — добавьте эти часы сюда\n\n"
        "<i>В заблокированные часы бот никогда не будет присылать вам смены</i>"
    )
    await message.answer(text, reply_markup=student_busy_menu_kb())


@router.callback_query(F.data == "add_busy_slot")
async def cb_start_add_busy(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "✍️ <b>Отправьте время, когда вы заняты:</b>\n\n"
        "<i>Примеры:</i>\n"
        "• <code>с 19 до 21 в пт</code>\n"
        "• <code>пт 18:00 - 20:30 тренировка</code>\n"
        "• <code>завтра с 14 до 17</code>\n"
        "• <code>суббота 10-13</code>"
    )
    await state.set_state(StudentBusyFSM.waiting_for_busy_text)
    await callback.answer()


@router.message(StudentBusyFSM.waiting_for_busy_text, F.text)
async def process_busy_slot_input(message: Message, state: FSMContext):
    parsed = parse_user_busy_input(message.text)
    if not parsed:
        await message.reply(
            "⚠️ <b>Не удалось распознать время и день!</b>\n\n"
            "Попробуйте написать проще, например:\n"
            "• <code>с 19 до 21 в пт</code>\n"
            "• <code>завтра с 15:00 до 18:30</code>"
        )
        return

    async with work_session_maker() as session:
        slot = UserBusySlot(
            user_id=message.from_user.id,
            day_of_week=parsed["day_of_week"],
            start_time=parsed["start_time"],
            end_time=parsed["end_time"],
            title=parsed["title"],
        )
        session.add(slot)
        await session.commit()

    await state.clear()

    day_name = DAYS_FULL[parsed["day_of_week"]]
    time_str = f"{parsed['start_time'].strftime('%H:%M')} — {parsed['end_time'].strftime('%H:%M')}"

    await message.answer(
        f"✅ <b>Время успешно заблокировано!</b>\n\n"
        f"🗓 День: <b>{day_name}</b>\n"
        f"⏰ Часы: <b>{time_str}</b>\n"
        f"📝 Метка: <b>{html.escape(parsed['title'])}</b>\n\n"
        "В эти часы предложения о работе приходить не будут",
        reply_markup=student_busy_menu_kb(),
    )


# ==================== ПРОСМОТР И УДАЛЕНИЕ БЛОКИРОВОК ====================

@router.callback_query(F.data == "list_busy_slots")
async def cb_list_busy_slots(callback: CallbackQuery):
    async with work_session_maker() as session:
        slots = (await session.execute(
            select(UserBusySlot)
            .where(UserBusySlot.user_id == callback.from_user.id)
            .order_by(UserBusySlot.day_of_week.asc(), UserBusySlot.start_time.asc())
        )).scalars().all()

    if not slots:
        await callback.message.edit_text(
            "📋 <b>У вас пока нет добавленных блокировок времени</b>\n\n"
            "Вы можете добавить их кнопкой ниже:",
            reply_markup=student_busy_menu_kb(),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        "📋 <b>Ваши персональные ограничения:</b>\n"
        "<i>(Нажмите на кнопку с красным крестиком, чтобы удалить интервал)</i>",
        reply_markup=busy_slots_list_kb(list(slots)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("del_busy_"))
async def cb_delete_busy_slot(callback: CallbackQuery):
    slot_id = int(callback.data.split("_")[2])

    async with work_session_maker() as session:
        slot = await session.get(UserBusySlot, slot_id)
        if slot and slot.user_id == callback.from_user.id:
            await session.delete(slot)
            await session.commit()

        # Обновляем список оставшихся
        remaining_slots = (await session.execute(
            select(UserBusySlot)
            .where(UserBusySlot.user_id == callback.from_user.id)
            .order_by(UserBusySlot.day_of_week.asc(), UserBusySlot.start_time.asc())
        )).scalars().all()

    await callback.answer("Интервал удален!")

    if remaining_slots:
        await callback.message.edit_text(
            "📋 <b>Ваши персональные ограничения:</b>\n"
            "<i>(Нажмите на кнопку с красным крестиком, чтобы удалить интервал)</i>",
            reply_markup=busy_slots_list_kb(list(remaining_slots)),
        )
    else:
        await callback.message.edit_text(
            "✅ <b>Все личные блокировки удалены</b>",
            reply_markup=student_busy_menu_kb(),
        )


# ==================== ОТКЛИК НА СМЕНУ ====================

@router.callback_query(F.data.startswith("apply_"))
async def cb_apply_to_vacancy(callback: CallbackQuery, bot: Bot):
    vac_id = int(callback.data.split("_")[2])
    student = callback.from_user

    async with work_session_maker() as session:
        vac = await session.get(Vacancy, vac_id)
        if not vac:
            await callback.answer("К сожалению, эта смена уже закрыта или отменена", show_alert=True)
            return

        emp = await session.get(Employer, vac.employer_id)

        # Фиксируем статус отклика в базе
        delivery = (await session.execute(
            select(VacancyDelivery)
            .where(VacancyDelivery.vacancy_id == vac.id)
            .where(VacancyDelivery.user_id == student.id)
        )).scalar_one_or_none()

        if delivery:
            delivery.status = "applied"
        else:
            session.add(VacancyDelivery(vacancy_id=vac.id, user_id=student.id, status="applied"))

        await session.commit()

    metrics_service.track_application()

    # Меняем кнопку под карточкой студента
    try:
        await callback.message.edit_reply_markup(reply_markup=student_applied_kb())
    except Exception:
        pass

    await callback.answer("🎉 Отклик отправлен работодателю!", show_alert=True)

    # Моментальное уведомление менеджеру компании
    if emp and emp.is_active:
        user_mention = f"@{student.username}" if student.username else f"<a href='tg://user?id={student.id}'>{html.escape(student.first_name)}</a>"
        shift_date_str = vac.target_date.strftime("%d.%m")
        shift_time_str = f"{vac.start_time.strftime('%H:%M')}–{vac.end_time.strftime('%H:%M')}"

        notif_text = (
            f"🔔 <b>Новый отклик на смену!</b>\n\n"
            f"🗓 Смена: <b>{shift_date_str} ({shift_time_str})</b>\n"
            f"👤 Соискатель: <b>{user_mention}</b> (ID: <code>{student.id}</code>)\n\n"
            f"<i>Свяжитесь со студентом для подтверждения выхода:</i> {user_mention}"
        )

        try:
            await bot.send_message(chat_id=emp.telegram_id, text=notif_text)
        except Exception as e:
            logger.warning(f"Не удалось доставить уведомление об отклике менеджеру {emp.telegram_id}: {e}")