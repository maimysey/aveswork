import html
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import CommandStart, CommandObject
from aiogram.enums import ChatType

from config import OWNER_USERNAME, logger
from database import work_session_maker
from models import Employer, Vacancy, WorkUser
from keyboards import student_main_menu_kb, employer_main_menu_kb, open_feed_kb, student_vacancy_card_kb
from services.availability import find_student, forget_student
from services.shift_format import is_upcoming

router = Router()


async def _send_shift_card(message: Message, vac_id: int) -> None:
    """Глубокая ссылка t.me/<бот>?start=v<id> — с сайта или из короткого уведомления."""
    from handlers.employer import student_card_text

    async with work_session_maker() as session:
        vac = await session.get(Vacancy, vac_id)
        emp = await session.get(Employer, vac.employer_id) if vac else None
    if not vac or vac.status != "active" or not is_upcoming(vac):
        await message.answer("⌛ Эта смена уже закрыта. Остальные — в ленте:", reply_markup=open_feed_kb())
        return
    await message.answer(
        student_card_text(emp.company_name if emp else "Компания", [vac], vac.require_all_days),
        reply_markup=student_vacancy_card_kb(vac.id),
    )


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, command: CommandObject):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "Студент"
    safe_name = html.escape(first_name)
    payload = (command.args or "").strip()

    # 1. Проверяем, является ли пользователь авторизованным работодателем
    async with work_session_maker() as session:
        employer = await session.get(Employer, user_id)

    if employer and employer.is_active:
        safe_comp = html.escape(employer.company_name)
        text = (
            f"💼 <b>Кабинет работодателя: {safe_comp}</b>\n\n"
            "Вы авторизованы как менеджер компании. В этом чате вы можете создавать смены "
            "и моментально находить сотрудников среди студентов БГУ\n\n"
            "📝 <b>Как опубликовать смену:</b>\n"
            "Просто отправьте сообщение с описанием работы, часами и днями, например:\n"
            "• <code>Бариста в четверг и пятницу с 16:00 до 21:00, 45 руб. Немига</code>\n"
            "• <code>Курьер завтра с 12 до 18, 50 BYN</code>\n\n"
            "<i>Используйте кнопки меню ниже для управления сервисом:</i>"
        )
        # Устанавливаем отдельную клавиатуру для работодателя
        await message.answer(text, reply_markup=employer_main_menu_kb())
        return

    # 2. Логика соискателя (студента): регистрация в базе AvesWork
    async with work_session_maker() as session:
        user = await session.get(WorkUser, user_id)
        if not user:
            user = WorkUser(
                telegram_id=user_id,
                is_active=True,
                notifications_enabled=True,
                bot_started=True,
            )
            session.add(user)
            logger.info(f"✨ Зарегистрирован соискатель: ID {user_id}")
        elif not user.bot_started:
            # Профиль создавался на сайте — теперь бот может писать сам
            user.bot_started = True
        await session.commit()

    # 3. Проверка наличия расписания в базах факультетов (Биофак, ФСК)
    forget_student(user_id)
    is_synced = (await find_student(user_id)) is not None

    # 4. Формирование подсказки о синхронизации с учебой
    if is_synced:
        sync_badge = (
            "🟢 <b>Расписание БГУ подключено:</b>\n"
            "Бот знает ваши пары на факультете и будет присылать только те смены, "
            "которые подходят под ваши свободные часы (с учетом дороги)"
        )
    else:
        sync_badge = (
            "💡 <b>Подсказка:</b>\n"
            "Вы пока не выбрали группу в ботах расписания (AvesBio / AvesFlow) "
            "Запустите их, чтобы AvesWork автоматически видел сетку ваших занятий"
        )

    greeting_text = (
        f"👋 <b>Рады видеть вас, {safe_name}!</b>\n\n"
        "<b>AvesWork</b> — умный сервис подработки для студентов БГУ.\n\n"
        f"{sync_badge}\n\n"
        "⏳ Чтобы бот не беспокоил вас во время тренировок, курсов или сна — "
        "нажмите <b>«⏳ Мои занятые часы»</b> и заблокируйте личное время\n\n"
        f"🤝 <i>Вы работодатель? Напишите владельцу для подключения: @{OWNER_USERNAME}</i>"
    )

    await message.answer(greeting_text, reply_markup=student_main_menu_kb())

    if payload.startswith("v") and payload[1:].isdigit():
        await _send_shift_card(message, int(payload[1:]))
    else:
        await message.answer("Все актуальные смены под ваше расписание:", reply_markup=open_feed_kb())
