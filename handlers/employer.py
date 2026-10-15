import asyncio
import html
import uuid
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import BaseFilter
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from sqlalchemy import select, func, update

from config import logger, OWNER_USERNAME, WORK_MODE, get_minsk_now
from database import work_session_maker
from models import Employer, Vacancy, VacancyDelivery, WorkUser
from services.nlp_parser import parse_vacancy_card
from services.matcher import Audience, build_audience
from services.metrics import metrics_service
from services.relay import enqueue as enqueue_relay
from services.shift_format import pay_text
from services.shift_meta import describe
from keyboards import (
    employer_preview_kb,
    student_vacancy_card_kb,
    business_contact_kb,
    employer_vacancies_list_kb,
    employer_vacancy_manage_kb,
)

router = Router()

DAYS_NAMES = [
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"
]

MY_SHIFTS_BUTTONS = {"📊 Мои смены", "📊 Мои публикации"}

EMPLOYER_MENU_BUTTONS = {
    "📝 Инструкция к публикации",
    "ℹ️ О сервисе",
    "💬 Связь с поддержкой",
} | MY_SHIFTS_BUTTONS


# ==================== ФИЛЬТР: ТОЛЬКО АВТОРИЗОВАННЫЙ РАБОТОДАТЕЛЬ ====================

class IsEmployerFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        async with work_session_maker() as session:
            emp = await session.get(Employer, message.from_user.id)
            return bool(emp and emp.is_active)


# ==================== КНОПКИ НИЖНЕГО МЕНЮ РАБОТОДАТЕЛЯ ====================

@router.message(F.text == "📝 Инструкция к публикации", IsEmployerFilter())
async def cmd_instruction(message: Message):
    text = (
        "📝 <b>Как публиковать смены в AvesWork</b>\n\n"
        "Отправьте в этот чат сообщение в свободной форме с описанием работы, часами и днями\n\n"
        "<b>Примеры:</b>\n"
        "• <i>«Ищем бариста 29.09 с 16:00 до 21:00. Оплата 45 руб. Кофейня на Немиге»</i>\n"
        "• <i>«Курьер в четверг и пятницу с 17 до 21. 50 BYN за смену»</i>\n"
        "• <i>«Помощник на склад с 28.09 по 30.09 с 12 до 18, 10 руб/час.»</i>\n\n"
        "Бот автоматически распознает даты, проверит расписание студентов БГУ и покажет аудиторию перед отправкой. "
        "Смена сразу появится в ленте «Смены» на сайте у тех, кому она подходит"
    )
    await message.answer(text)


@router.message(F.text == "ℹ️ О сервисе", IsEmployerFilter())
async def cmd_employer_help(message: Message):
    from handlers.settings import HELP_TEXT
    await message.answer(HELP_TEXT, reply_markup=business_contact_kb(OWNER_USERNAME))


@router.message(F.text == "💬 Связь с поддержкой", IsEmployerFilter())
async def cmd_support(message: Message):
    await message.answer(
        "💬 <b>Поддержка и вопросы сотрудничества:</b>\n"
        f"Напишите владельцу платформы: @{OWNER_USERNAME}",
        reply_markup=business_contact_kb(OWNER_USERNAME),
    )


# ==================== УПРАВЛЕНИЕ И УДАЛЕНИЕ СМЕН (МОИ СМЕНЫ) ====================

async def _render_vacancies_list(user_id: int) -> tuple[str, any]:
    async with work_session_maker() as session:
        vacancies = (await session.execute(
            select(Vacancy)
            .where(Vacancy.employer_id == user_id)
            .where(Vacancy.status != "draft")
            .order_by(Vacancy.target_date.desc(), Vacancy.start_time.asc())
            .limit(40)
        )).scalars().all()

        if not vacancies:
            return "📋 <b>У вас пока нет опубликованных смен.</b>\nОтправьте текст смены в чат, чтобы создать новую!", None

        # Считаем отклики для каждой смены
        app_counts = (await session.execute(
            select(VacancyDelivery.vacancy_id, func.count(VacancyDelivery.id))
            .where(VacancyDelivery.vacancy_id.in_([v.id for v in vacancies]))
            .where(VacancyDelivery.status == "applied")
            .group_by(VacancyDelivery.vacancy_id)
        )).all()
        app_dict = dict(app_counts)

        vacancies_data = [(v, app_dict.get(v.id, 0)) for v in vacancies]

    text = (
        "📊 <b>Ваши смены в системе:</b>\n"
        "<i>Нажмите на смену ниже, чтобы посмотреть отклики или снять её с публикации:</i>"
    )
    kb = employer_vacancies_list_kb(vacancies_data)
    return text, kb


@router.message(F.text.in_(MY_SHIFTS_BUTTONS), IsEmployerFilter())
async def cmd_my_vacancies(message: Message):
    text, kb = await _render_vacancies_list(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "back_to_my_vacs")
async def cb_back_to_my_vacs(callback: CallbackQuery):
    text, kb = await _render_vacancies_list(callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("manage_vac_"))
async def cb_manage_vacancy(callback: CallbackQuery):
    vac_id = int(callback.data.split("_")[2])

    async with work_session_maker() as session:
        vac = await session.get(Vacancy, vac_id)
        if not vac or vac.employer_id != callback.from_user.id:
            await callback.answer("Смена не найдена или уже удалена", show_alert=True)
            return

        sent_rows = (await session.execute(
            select(VacancyDelivery.status, func.count(VacancyDelivery.id))
            .where(VacancyDelivery.vacancy_id == vac.id)
            .group_by(VacancyDelivery.status)
        )).all()
        by_status = dict(sent_rows)

        applied_deliveries = (await session.execute(
            select(VacancyDelivery.user_id)
            .where(VacancyDelivery.vacancy_id == vac.id)
            .where(VacancyDelivery.status == "applied")
        )).scalars().all()

    total_sent = sum(by_status.values())
    day_title = DAYS_NAMES[vac.day_of_week]
    date_str = vac.target_date.strftime("%d.%m.%Y")
    time_str = f"{vac.start_time.strftime('%H:%M')} — {vac.end_time.strftime('%H:%M')}"
    pay_str = pay_text(vac.pay_amount, vac.pay_unit) or "По договоренности"
    status_str = {"active": "🟢 Опубликована", "closed": "⚪ Снята", "draft": "📝 Черновик"}.get(vac.status, vac.status)

    lines = [
        f"📋 <b>Управление сменой #{vac.id}</b>\n",
        f"📌 <b>Статус:</b> {status_str}",
        f"🗓 <b>Дата:</b> {day_title} ({date_str})",
        f"⏰ <b>Время:</b> {time_str}",
        f"💰 <b>Оплата:</b> {pay_str}",
        f"📬 <b>Узнали о смене:</b> {total_sent} чел. "
        f"(карточка в AvesWork: {by_status.get('sent', 0)}, коротко в боте расписания: {by_status.get('relay', 0)})",
        f"🙋‍♂️ <b>Откликов:</b> {len(applied_deliveries)} чел.\n",
        f"📝 <b>Текст вакансии:</b>\n<i>{html.escape(vac.raw_text)}</i>\n",
    ]

    if applied_deliveries:
        lines.append("👥 <b>Откликнувшиеся соискатели:</b>")
        for idx, uid in enumerate(applied_deliveries, 1):
            lines.append(f"{idx}. <a href='tg://user?id={uid}'>Кандидат #{uid}</a> (ID: <code>{uid}</code>)")
    else:
        lines.append("<i>Откликов пока нет. Ожидайте уведомлений в чате</i>")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=employer_vacancy_manage_kb(vac.id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_vac_"))
async def cb_delete_vacancy(callback: CallbackQuery):
    vac_id = int(callback.data.split("_")[2])

    async with work_session_maker() as session:
        vac = await session.get(Vacancy, vac_id)
        if vac and vac.employer_id == callback.from_user.id:
            await session.delete(vac)
            await session.commit()

    await callback.answer("Смена успешно снята с публикации!", show_alert=True)

    text, kb = await _render_vacancies_list(callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


# ==================== ВСПОМОГАТЕЛЬНЫЙ ФОРМАТТЕР ПРЕВЬЮ ====================

def _relay_note() -> str:
    if WORK_MODE == "live":
        return "коротко в боте расписания"
    return "коротко в боте расписания — <i>тестовый режим, только тестерам</i>"


def _build_preview_text(
    company_name: str,
    vacancies: list[Vacancy],
    aud: Audience,
    require_all_days: bool,
) -> str:
    first_vac = vacancies[0]
    time_str = f"{first_vac.start_time.strftime('%H:%M')} — {first_vac.end_time.strftime('%H:%M')}"
    pay = pay_text(first_vac.pay_amount, first_vac.pay_unit)
    pay_str = f"<b>{pay}</b>" if pay else "<i>По договоренности</i>"

    lines = [
        "📋 <b>Параметры смены распознаны:</b>\n",
        f"🏢 Компания: <b>{html.escape(company_name)}</b>",
        f"🏷 Должность: <b>{html.escape(first_vac.title or 'Смена')}</b>",
        f"⏰ Время: <b>{time_str}</b>",
        f"💰 Оплата: {pay_str}\n",
    ]

    if len(vacancies) == 1:
        v = vacancies[0]
        day_title = DAYS_NAMES[v.day_of_week]
        date_str = v.target_date.strftime("%d.%m.%Y")
        lines.append(f"🗓 Дата: <b>{day_title} ({date_str})</b>\n")
    else:
        lines.append(f"🗓 <b>Смены на {len(vacancies)} дня:</b>")
        for b in aud.breakdown:
            d_title = DAYS_NAMES[b["day_of_week"]]
            d_str = b["target_date"].strftime("%d.%m")
            lines.append(
                f"• <b>{d_title} ({d_str}):</b> свободно {b['potential_count']} студ. "
                f"(карточка: {b['ready_count']}, коротко: {b['relay_count']})"
            )
        lines.append("")

        if require_all_days:
            mode_desc = "🎯 <b>Режим: Нужен на ВСЕ дни сразу</b>\n<i>(Узнают только те, кто свободен во все указанные дни)</i>"
        else:
            mode_desc = "🔀 <b>Режим: Можно по отдельным дням</b>\n<i>(Узнают те, кто свободен хотя бы в один из дней)</i>"
        lines.append(f"{mode_desc}\n")

    lines.extend([
        "📊 <b>Аналитика аудитории:</b>",
        f"• 👥 Свободно по расписанию: <b>{len(aud.potential)} чел.</b>",
        f"• ⚡ Получат карточку в AvesWork: <b>{len(aud.push)} чел.</b>",
        f"• 💬 Получат {_relay_note()}: <b>до {len(aud.relay)} чел.</b>",
        "• 🌐 Смена появится в ленте на сайте у всех, кому подходит по времени\n",
        "<i>Подтвердите публикацию для запуска адресного поиска:</i>"
    ])

    return "\n".join(lines)


async def _load_vacancies(vac_ids: list[int]) -> tuple[list[Vacancy], str]:
    async with work_session_maker() as session:
        vacancies = (await session.execute(
            select(Vacancy).where(Vacancy.id.in_(vac_ids)).order_by(Vacancy.target_date.asc())
        )).scalars().all()
        if not vacancies:
            return [], ""
        emp = await session.get(Employer, vacancies[0].employer_id)
    return list(vacancies), (emp.company_name if emp else "Компания")


def _shifts(vacancies: list[Vacancy]) -> list[dict]:
    return [{"target_date": v.target_date, "day_of_week": v.day_of_week} for v in vacancies]


# ==================== ПРИЕМ И ПАРСИНГ ТЕКСТА СМЕНЫ ====================

# Фильтр исключает кнопки нижнего меню работодателя
@router.message(F.text, ~F.text.startswith("/"), ~F.text.in_(EMPLOYER_MENU_BUTTONS), IsEmployerFilter())
async def handle_employer_vacancy_post(message: Message):
    async with work_session_maker() as session:
        employer = await session.get(Employer, message.from_user.id)

    parsed = parse_vacancy_card(message.text)
    if not parsed or not parsed["shifts"]:
        await message.reply(
            "⚠️ <b>Не удалось определить временной интервал!</b>\n\n"
            "Пожалуйста, укажите часы работы, например:\n"
            "• <code>29.09 с 17:00 до 21:00</code>\n"
            "• <code>с 28.09 по 30.09 с 12:00 до 16:30</code>\n"
            "• <code>в чт и пт с 18 до 22</code>"
        )
        return

    meta = describe(parsed["raw_text"])
    batch_key = uuid.uuid4().hex[:16]

    created_vacancies: list[Vacancy] = []
    async with work_session_maker() as session:
        for s in parsed["shifts"]:
            vac = Vacancy(
                employer_id=employer.telegram_id,
                raw_text=parsed["raw_text"],
                target_date=s["target_date"],
                day_of_week=s["day_of_week"],
                start_time=parsed["start_time"],
                end_time=parsed["end_time"],
                pay_amount=parsed["pay_amount"],
                pay_unit=parsed["pay_unit"],
                title=meta["title"],
                category=meta["category"],
                status="draft",
                batch_key=batch_key,
            )
            session.add(vac)
            created_vacancies.append(vac)

        await session.commit()
        for v in created_vacancies:
            await session.refresh(v)

    metrics_service.track_vacancy_created()

    aud = await build_audience(_shifts(created_vacancies), parsed["start_time"], parsed["end_time"], False)

    vac_ids = [v.id for v in created_vacancies]
    is_multi = len(created_vacancies) > 1

    text = _build_preview_text(employer.company_name, created_vacancies, aud, require_all_days=False)
    await message.answer(
        text,
        reply_markup=employer_preview_kb(vac_ids, require_all_days=False, is_multi_day=is_multi)
    )


# ==================== ПЕРЕКЛЮЧЕНИЕ РЕЖИМА СТРОГИЙ / ГИБКИЙ ====================

@router.callback_query(F.data.startswith("toggle_mode_"))
async def cb_toggle_mode(callback: CallbackQuery):
    parts = callback.data.split("_")
    ids_str = parts[2]
    new_require_all = bool(int(parts[3]))

    vac_ids = [int(x) for x in ids_str.split(",") if x.isdigit()]
    vacancies, company_name = await _load_vacancies(vac_ids)
    if not vacancies:
        await callback.answer("Смены устарели или были удалены", show_alert=True)
        return

    first_v = vacancies[0]
    aud = await build_audience(_shifts(vacancies), first_v.start_time, first_v.end_time, new_require_all)
    text = _build_preview_text(company_name, vacancies, aud, new_require_all)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=employer_preview_kb(vac_ids, require_all_days=new_require_all, is_multi_day=True)
        )
    except TelegramBadRequest:
        pass

    await callback.answer()


# ==================== ОТМЕНА ЧЕРНОВИКА СМЕНЫ ====================

@router.callback_query(F.data.startswith("cancel_vac_"))
async def cb_cancel_vacancy(callback: CallbackQuery):
    ids_str = callback.data.replace("cancel_vac_", "")
    vac_ids = [int(x) for x in ids_str.split(",") if x.isdigit()]

    async with work_session_maker() as session:
        vacancies = (await session.execute(
            select(Vacancy).where(Vacancy.id.in_(vac_ids))
        )).scalars().all()

        for v in vacancies:
            if v.employer_id == callback.from_user.id:
                await session.delete(v)

        await session.commit()

    await callback.message.edit_text("❌ <b>Публикация смен отменена.</b> Вы можете прислать новый текст")
    await callback.answer()


# ==================== ЗАПУСК РАССЫЛКИ ====================

def student_card_text(company_name: str, vacancies: list[Vacancy], require_all_days: bool) -> str:
    first_v = vacancies[0]
    time_str = f"{first_v.start_time.strftime('%H:%M')} – {first_v.end_time.strftime('%H:%M')}"
    pay = pay_text(first_v.pay_amount, first_v.pay_unit)
    pay_line = f"💰 Оплата: <b>{pay}</b>\n" if pay else ""

    if len(vacancies) == 1:
        v = vacancies[0]
        date_header = f"🗓 Дата: <b>{DAYS_NAMES[v.day_of_week]} ({v.target_date.strftime('%d.%m')})</b>"
    else:
        days_list = [f"{DAYS_NAMES[v.day_of_week]} ({v.target_date.strftime('%d.%m')})" for v in vacancies]
        mode_note = " <i>(нужен человек на все дни)</i>" if require_all_days else ""
        date_header = f"🗓 Даты: <b>{', '.join(days_list)}</b>{mode_note}"

    return (
        f"⚡ <b>Новая смена под твое расписание!</b>\n\n"
        f"🏷 <b>{html.escape(first_v.title or 'Смена')}</b>\n"
        f"🏢 Работодатель: <b>{html.escape(company_name)}</b>\n"
        f"{date_header}\n"
        f"⏰ Время: <b>{time_str}</b>\n"
        f"{pay_line}\n"
        f"📝 <b>Описание:</b>\n"
        f"{html.escape(first_v.raw_text)}"
    )


async def _send_card(bot: Bot, uid: int, text: str, vac_id: int) -> str:
    """sent | blocked | error"""
    for attempt in range(2):
        try:
            await bot.send_message(chat_id=uid, text=text, reply_markup=student_vacancy_card_kb(vac_id))
            return "sent"
        except TelegramForbiddenError:
            return "blocked"
        except TelegramRetryAfter as e:
            if attempt == 0:
                await asyncio.sleep(e.retry_after)
                continue
            return "error"
        except Exception:
            return "error"
    return "error"


@router.callback_query(F.data.startswith("send_vac_"))
async def cb_broadcast_vacancy(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    ids_str = parts[2]
    require_all_days = bool(int(parts[3])) if len(parts) > 3 else False

    vac_ids = [int(x) for x in ids_str.split(",") if x.isdigit()]
    vacancies, company_name = await _load_vacancies(vac_ids)
    if not vacancies:
        await callback.answer("Смены не найдены", show_alert=True)
        return
    if vacancies[0].status == "active":
        await callback.answer("Эта смена уже опубликована", show_alert=True)
        return

    await callback.message.edit_text("⏳ <b>Идет подбор кандидатов и рассылка...</b>")

    # 1. Публикуем: смена сразу появляется в ленте на сайте
    async with work_session_maker() as session:
        await session.execute(
            update(Vacancy)
            .where(Vacancy.id.in_(vac_ids))
            .values(status="active", published_at=get_minsk_now(), require_all_days=require_all_days)
        )
        await session.commit()
    for v in vacancies:
        v.status, v.require_all_days = "active", require_all_days

    first_v = vacancies[0]
    aud = await build_audience(_shifts(vacancies), first_v.start_time, first_v.end_time, require_all_days)

    # 2. Полные карточки в AvesWork
    card = student_card_text(company_name, vacancies, require_all_days)
    sent_count = 0
    blocked: list[int] = []
    async with work_session_maker() as session:
        for uid in aud.push:
            res = await _send_card(bot, uid, card, first_v.id)
            if res == "sent":
                for v in vacancies:
                    session.add(VacancyDelivery(vacancy_id=v.id, user_id=uid, status="sent"))
                sent_count += 1
            elif res == "blocked":
                blocked.append(uid)
            await asyncio.sleep(0.04)

        if blocked:
            # Остановил бота — дальше сообщаем ему через бота расписания
            await session.execute(
                update(WorkUser).where(WorkUser.telegram_id.in_(blocked)).values(bot_started=False)
            )
        await session.commit()

    metrics_service.track_deliveries_sent(sent_count)

    # 3. Короткие уведомления через ботов расписания (очередь, лимиты, тихие часы)
    relay_targets = dict(aud.relay)
    relay_stats = await enqueue_relay(vacancies, relay_targets)

    relay_line = f"💬 Коротко в боте расписания: <b>{relay_stats['queued']} чел.</b>"
    if relay_stats["deferred"] and relay_stats["queued"]:
        relay_line += " <i>(сейчас ночь — уйдут утром)</i>"
    elif relay_stats["queued"]:
        relay_line += " <i>(уходят в течение пары минут)</i>"
    if relay_stats["limited"]:
        relay_line += f"\n<i>Ещё {relay_stats['limited']} чел. уже получили сегодня максимум уведомлений</i>"
    if WORK_MODE != "live":
        relay_line += "\n<i>Тестовый режим: короткие уведомления получают только тестеры</i>"

    await callback.message.edit_text(
        f"✅ <b>Смена опубликована!</b>\n\n"
        f"📬 Карточка в AvesWork: <b>{sent_count} чел.</b>\n"
        f"{relay_line}\n"
        f"🌐 Свободно по расписанию: <b>{len(aud.potential)} чел.</b> — они видят смену в ленте на сайте\n\n"
        f"<i>Как только появятся первые отклики, бот сразу пришлет контакты студентов в этот чат!</i>"
    )
    await callback.answer()
    logger.info(
        f"💼 Смена {vac_ids} опубликована: push={sent_count}, relay={relay_stats}, potential={len(aud.potential)}"
    )
