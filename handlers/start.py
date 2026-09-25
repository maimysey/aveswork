import html
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import CommandStart
from aiogram.enums import ChatType
from sqlalchemy import select

from config import OWNER_USERNAME, logger
from database import work_session_maker, FACULTY_SESSIONS
from models import Employer, WorkUser, FacultyUser
from keyboards import student_main_menu_kb, business_contact_kb

router = Router()


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "Студент"
    safe_name = html.escape(first_name)

    # 1. Проверяем, не является ли пользователь работодателем
    async with work_session_maker() as session:
        employer = await session.get(Employer, user_id)
        if employer and employer.is_active:
            safe_comp = html.escape(employer.company_name)
            await message.answer(
                f"💼 <b>Кабинет работодателя: {safe_comp}</b>\n\n"
                "Вы можете публиковать смены прямо в этот чат обычным текстом\n\n"
                "<i>Пример текста:</i>\n"
                "<code>Ищем бариста на смену в четверг с 16:00 до 21:00. Оплата 45 руб. Кофейня на Немиге</code>\n\n"
                "Бот автоматически рассчитает свободные часы студентов и предложит запустить поиск"
            )
            return

    # 2. Регистрируем соискателя в базе AvesWork
    async with work_session_maker() as session:
        user = await session.get(WorkUser, user_id)
        if not user:
            user = WorkUser(
                telegram_id=user_id,
                is_active=True,
                notifications_enabled=True,
            )
            session.add(user)
            await session.commit()
            logger.info(f"✨ Зарегистрирован новый соискатель: ID {user_id}")

    # 3. Проверяем наличие студента в базах факультетов (Биофак, ФСК)
    is_synced_with_faculty = False
    for fac_code, session_maker in FACULTY_SESSIONS.items():
        try:
            async with session_maker() as fac_session:
                fac_user = (await fac_session.execute(
                    select(FacultyUser)
                    .where(FacultyUser.telegram_id == user_id)
                    .where(FacultyUser.group_id.is_not(None))
                )).scalar_one_or_none()

                if fac_user:
                    is_synced_with_faculty = True
                    break
        except Exception:
            pass

    # 4. Формируем приветственное сообщение
    if is_synced_with_faculty:
        sync_badge = (
            "🟢 <b>Расписание БГУ подключено:</b>\n"
            "Мы знаем сетку ваших пар и будем предлагать только те смены, "
            "которые не пересекаются с парами и лабами!"
        )
    else:
        sync_badge = (
            "💡 <b>Подсказка:</b>\n"
            "Вы пока не зарегистрированы в ботах расписания факультетов (AvesBio / AvesFlow). "
            "Запустите их и выберите вашу группу, чтобы AvesWork автоматически видел ваши свободные часы"
        )

    greeting_text = (
        f"👋 <b>Рады видеть вас, {safe_name}!</b>\n\n"
        "<b>AvesWork</b> — сервис гибкой занятости для студентов БГУ. "
        "Мы присылаем проверенные смены от бизнеса точно под ваши свободные часы.\n\n"
        f"{sync_badge}\n\n"
        "⏳ Если вы заняты тренировками или курсами, нажмите <b>«⏳ Мои занятые часы»</b> "
        "и укажите интервалы, в которые вас нельзя беспокоить.\n\n"
        f"🤝 <i>Представитель бизнеса? Свяжитесь со мной для размещения смен: @{OWNER_USERNAME}</i>"
    )

    await message.answer(greeting_text, reply_markup=student_main_menu_kb())