import html
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import CommandStart
from aiogram.enums import ChatType
from sqlalchemy import select

from config import OWNER_USERNAME, logger
from database import work_session_maker, FACULTY_SESSIONS
from models import Employer, WorkUser, FacultyUser
from keyboards import student_main_menu_kb, employer_main_menu_kb

router = Router()


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "Студент"
    safe_name = html.escape(first_name)

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
            )
            session.add(user)
            await session.commit()
            logger.info(f"✨ Зарегистрирован соискатель: ID {user_id}")

    # 3. Проверка наличия расписания в базах факультетов (Биофак, ФСК)
    is_synced = False
    for fac_code, session_maker in FACULTY_SESSIONS.items():
        try:
            async with session_maker() as fac_session:
                fac_user = (await fac_session.execute(
                    select(FacultyUser)
                    .where(FacultyUser.telegram_id == user_id)
                    .where(FacultyUser.group_id.is_not(None))
                )).scalar_one_or_none()

                if fac_user:
                    is_synced = True
                    break
        except Exception:
            pass

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