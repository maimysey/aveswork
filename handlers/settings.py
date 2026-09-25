import html
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from sqlalchemy import select, func

from config import OWNER_USERNAME
from database import work_session_maker
from models import WorkUser, UserBusySlot
from keyboards import student_settings_kb, business_contact_kb

router = Router()

HELP_TEXT = (
    "📖 <b>Справка и возможности платформы AvesWork</b>\n\n"
    "<b>AvesWork</b> — это сервис умной подработки для студентов БГУ, "
    "который подбирает смены и вакансии <b>в строгом соответствии с вашим официальным расписанием</b> "
    "без конфликтов с парами, лабораторными и отработками\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "👨‍🎓 <b>КАК ЭТО РАБОТАЕТ ДЛЯ СТУДЕНТА:</b>\n\n"
    "1. <b>Автоматическая синхронизация с парами:</b>\n"
    "Вам не нужно вручную вбивать расписание. Бот напрямую обращается к базам факультетов (Биофак, ФСК и др.) "
    "и знает, когда у вас заканчиваются пары (с учетом 15–20 минут на дорогу).\n\n"
    "2. <b>Личные блокировки времени:</b>\n"
    "Если вы заняты тренировками, сном или курсами — нажмите <b>«⏳ Мои занятые часы»</b> "
    "или напишите боту в чат обычным текстом:\n"
    "• <code>с 19 до 21 в пт</code>\n"
    "• <code>пт 18:00 - 20:30 тренировка</code>\n"
    "• <code>завтра с 14 до 17</code>\n"
    "В эти часы предложения о работе приходить не будут\n\n"
    "3. <b>Отклик в один клик:</b>\n"
    "Когда появляется подходящая смена, вам приходит карточка с датой, временем и оплатой "
    "Нажмите <b>«🙋‍♂️ Откликнуться на смену»</b> — и менеджер компании сразу получит ваш Telegram для связи\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🏢 <b>ДЛЯ РАБОТОДАТЕЛЕЙ И БИЗНЕСА:</b>\n\n"
    "• Если вам нужны сотрудники на пиковые часы (бариста, официанты, курьеры, промоутеры, помощники на склад) — "
    f"напишите владельцу платформы: @{OWNER_USERNAME}\n"
    "• После подключения менеджер просто отправляет в чат бота текст смены: "
    "<i>«Нужен бариста в четверг с 16 до 21, 45 руб...»</i>. Бот мгновенно покажет, сколько студентов свободно, "
    "и адресно доставит предложение"
)


def render_settings_text(user: WorkUser, busy_count: int) -> str:
    search_str = "В активном поиске 🟢" if user.is_active else "Поиск на паузе 🔴"
    notif_str = "Включены 🟢" if user.notifications_enabled else "Выключены 🔴"

    return (
        "⚙️ <b>Личный кабинет и настройки</b>\n\n"
        f"🔍 <b>Статус:</b> {search_str}\n"
        f"🔔 <b>Пуш-уведомления:</b> {notif_str}\n"
        f"⏳ <b>Личных ограничений по времени:</b> {busy_count} шт.\n\n"
        "<i>Используйте кнопки ниже для управления:</i>"
    )


# ==================== СПРАВКА И ПОМОЩЬ ====================

@router.message(Command("help"))
@router.message(Command("faq"))
@router.message(F.text == "ℹ️ О сервисе")
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, reply_markup=business_contact_kb(OWNER_USERNAME))


@router.message(F.text == "💼 Для работодателей")
async def cmd_for_business(message: Message):
    text = (
        "💼 <b>Сотрудничество с бизнесом</b>\n\n"
        "Платформа AvesWork позволяет закрывать смены за считанные минуты благодаря доступу "
        "к расписанию сотен студентов БГУ\n\n"
        "• Закрытие пиковых часов (вечера, ланчи, выходные);\n"
        "• Мгновенный расчет доступных кандидатов перед рассылкой;\n"
        "• Оплата только за реальные выходы\n\n"
        f"Для подключения вашего бизнеса и получения доступа напишите владельцу: @{OWNER_USERNAME}"
    )
    await message.answer(text, reply_markup=business_contact_kb(OWNER_USERNAME))


# ==================== МЕНЮ НАСТРОЕК ====================

@router.message(Command("settings"))
@router.message(F.text == "⚙️ Настройки")
async def open_settings(message: Message):
    async with work_session_maker() as session:
        user = await session.get(WorkUser, message.from_user.id)
        if not user:
            user = WorkUser(telegram_id=message.from_user.id)
            session.add(user)
            await session.commit()

        busy_count = await session.scalar(
            select(func.count(UserBusySlot.id)).where(UserBusySlot.user_id == user.telegram_id)
        ) or 0

    text = render_settings_text(user, busy_count)
    await message.answer(
        text,
        reply_markup=student_settings_kb(user.is_active, user.notifications_enabled),
    )


# ==================== ПЕРЕКЛЮЧАТЕЛИ НАСТРОЕК ====================

@router.callback_query(F.data == "toggle_search_status")
async def cb_toggle_search(callback: CallbackQuery):
    async with work_session_maker() as session:
        user = await session.get(WorkUser, callback.from_user.id)
        if not user:
            await callback.answer()
            return

        user.is_active = not user.is_active
        await session.commit()

        busy_count = await session.scalar(
            select(func.count(UserBusySlot.id)).where(UserBusySlot.user_id == user.telegram_id)
        ) or 0

        text = render_settings_text(user, busy_count)
        kb = student_settings_kb(user.is_active, user.notifications_enabled)

    await callback.message.edit_text(text, reply_markup=kb)
    status_str = "возобновлен 🟢" if user.is_active else "поставлен на паузу 🔴"
    await callback.answer(f"Поиск работы {status_str}!")


@router.callback_query(F.data == "toggle_notif_status")
async def cb_toggle_notifications(callback: CallbackQuery):
    async with work_session_maker() as session:
        user = await session.get(WorkUser, callback.from_user.id)
        if not user:
            await callback.answer()
            return

        user.notifications_enabled = not user.notifications_enabled
        await session.commit()

        busy_count = await session.scalar(
            select(func.count(UserBusySlot.id)).where(UserBusySlot.user_id == user.telegram_id)
        ) or 0

        text = render_settings_text(user, busy_count)
        kb = student_settings_kb(user.is_active, user.notifications_enabled)

    await callback.message.edit_text(text, reply_markup=kb)
    status_str = "включены 🟢" if user.notifications_enabled else "отключены 🔴"
    await callback.answer(f"Оповещения {status_str}!")