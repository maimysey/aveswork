import asyncio
import html
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import BaseFilter
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from sqlalchemy import select, func

from config import logger, OWNER_USERNAME
from database import work_session_maker
from models import Employer, Vacancy, VacancyDelivery
from services.nlp_parser import parse_vacancy_card
from services.matcher import find_available_candidates_batch
from services.metrics import metrics_service
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

EMPLOYER_MENU_BUTTONS = {
    "📝 Инструкция к публикации",
    "📊 Мои смены",
    "ℹ️ О сервисе",
    "💬 Связь с поддержкой",
}


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
        "Бот автоматически распознает даты, проверит расписание студентов БГУ и покажет аудиторию перед отправкой"
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
            .order_by(Vacancy.target_date.desc(), Vacancy.start_time.asc())
        )).scalars().all()

        if not vacancies:
            return "📋 <b>У вас пока нет активных смен.</b>\nОтправьте текст смены в чат, чтобы создать новую!", None

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


@router.message(F.text == "📊 Мои смены", IsEmployerFilter())
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

        total_sent = await session.scalar(
            select(func.count(VacancyDelivery.id))
            .where(VacancyDelivery.vacancy_id == vac.id)
        ) or 0

        applied_deliveries = (await session.execute(
            select(VacancyDelivery.user_id)
            .where(VacancyDelivery.vacancy_id == vac.id)
            .where(VacancyDelivery.status == "applied")
        )).scalars().all()

    day_title = DAYS_NAMES[vac.day_of_week]
    date_str = vac.target_date.strftime("%d.%m.%Y")
    time_str = f"{vac.start_time.strftime('%H:%M')} — {vac.end_time.strftime('%H:%M')}"
    pay_str = f"{vac.pay_amount} {vac.pay_unit}" if vac.pay_amount else "По договоренности"

    lines = [
        f"📋 <b>Управление сменой #{vac.id}</b>\n",
        f"🗓 <b>Дата:</b> {day_title} ({date_str})",
        f"⏰ <b>Время:</b> {time_str}",
        f"💰 <b>Оплата:</b> {pay_str}",
        f"📬 <b>Доставлено пушей студентам:</b> {total_sent} чел.",
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

def _build_preview_text(
    company_name: str,
    vacancies: list[Vacancy],
    breakdown: list[dict],
    total_potential: int,
    ready_count: int,
    require_all_days: bool,
) -> str:
    first_vac = vacancies[0]
    time_str = f"{first_vac.start_time.strftime('%H:%M')} — {first_vac.end_time.strftime('%H:%M')}"
    pay_str = f"<b>{first_vac.pay_amount} {first_vac.pay_unit}</b>" if first_vac.pay_amount else "<i>По договоренности</i>"

    lines = [
        "📋 <b>Параметры смены распознаны:</b>\n",
        f"🏢 Компания: <b>{html.escape(company_name)}</b>",
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
        for b in breakdown:
            d_title = DAYS_NAMES[b["day_of_week"]]
            d_str = b["target_date"].strftime("%d.%m")
            lines.append(
                f"• <b>{d_title} ({d_str}):</b> свободно {b['potential_count']} студ. "
                f"(пуш получат: {b['ready_count']})"
            )
        lines.append("")

        if require_all_days:
            mode_desc = "🎯 <b>Режим: Нужен на ВСЕ дни сразу</b>\n<i>(Пуш получат только те, кто свободен во все указанные дни)</i>"
        else:
            mode_desc = "🔀 <b>Режим: Можно по отдельным дням</b>\n<i>(Пуш получат те, кто свободен хотя бы в один из дней)</i>"
        lines.append(f"{mode_desc}\n")

    lines.extend([
        "📊 <b>Аналитика аудитории:</b>",
        f"• 👥 Всего подходит студентов БГУ: <b>{total_potential} чел.</b>",
        f"• ⚡ Активных соискателей получат пуш: <b>{ready_count} чел.</b>\n",
        "<i>Подтвердите публикацию для запуска адресного поиска:</i>"
    ])

    return "\n".join(lines)


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
            )
            session.add(vac)
            created_vacancies.append(vac)

        await session.commit()
        for v in created_vacancies:
            await session.refresh(v)

    metrics_service.track_vacancy_created()

    shifts_data = [{"target_date": v.target_date, "day_of_week": v.day_of_week} for v in created_vacancies]
    total_potential, ready_candidates, breakdown = await find_available_candidates_batch(
        shifts=shifts_data,
        shift_start=parsed["start_time"],
        shift_end=parsed["end_time"],
        require_all_days=False,
    )

    vac_ids = [v.id for v in created_vacancies]
    is_multi = len(created_vacancies) > 1

    text = _build_preview_text(
        company_name=employer.company_name,
        vacancies=created_vacancies,
        breakdown=breakdown,
        total_potential=total_potential,
        ready_count=len(ready_candidates),
        require_all_days=False,
    )

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

    async with work_session_maker() as session:
        vacancies = (await session.execute(
            select(Vacancy).where(Vacancy.id.in_(vac_ids)).order_by(Vacancy.target_date.asc())
        )).scalars().all()

        if not vacancies:
            await callback.answer("Смены устарели или были удалены", show_alert=True)
            return

        emp = await session.get(Employer, vacancies[0].employer_id)
        company_name = emp.company_name if emp else "Компания"

    shifts_data = [{"target_date": v.target_date, "day_of_week": v.day_of_week} for v in vacancies]
    first_v = vacancies[0]

    total_potential, ready_candidates, breakdown = await find_available_candidates_batch(
        shifts=shifts_data,
        shift_start=first_v.start_time,
        shift_end=first_v.end_time,
        require_all_days=new_require_all,
    )

    text = _build_preview_text(
        company_name=company_name,
        vacancies=list(vacancies),
        breakdown=breakdown,
        total_potential=total_potential,
        ready_count=len(ready_candidates),
        require_all_days=new_require_all,
    )

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

@router.callback_query(F.data.startswith("send_vac_"))
async def cb_broadcast_vacancy(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    ids_str = parts[2]
    require_all_days = bool(int(parts[3])) if len(parts) > 3 else False

    vac_ids = [int(x) for x in ids_str.split(",") if x.isdigit()]

    async with work_session_maker() as session:
        vacancies = (await session.execute(
            select(Vacancy).where(Vacancy.id.in_(vac_ids)).order_by(Vacancy.target_date.asc())
        )).scalars().all()

        if not vacancies:
            await callback.answer("Смены не найдены", show_alert=True)
            return

        emp = await session.get(Employer, vacancies[0].employer_id)
        company_name = emp.company_name if emp else "Компания"

    await callback.message.edit_text("⏳ <b>Идет подбор кандидатов и рассылка пушей...</b>")

    shifts_data = [{"target_date": v.target_date, "day_of_week": v.day_of_week} for v in vacancies]
    first_v = vacancies[0]

    total_potential, ready_candidate_ids, _ = await find_available_candidates_batch(
        shifts=shifts_data,
        shift_start=first_v.start_time,
        shift_end=first_v.end_time,
        require_all_days=require_all_days,
    )

    time_str = f"{first_v.start_time.strftime('%H:%M')} – {first_v.end_time.strftime('%H:%M')}"
    pay_line = f"💰 Оплата: <b>{first_v.pay_amount} {first_v.pay_unit}</b>\n" if first_v.pay_amount else ""

    if len(vacancies) == 1:
        v = vacancies[0]
        date_header = f"🗓 Дата: <b>{DAYS_NAMES[v.day_of_week]} ({v.target_date.strftime('%d.%m')})</b>"
    else:
        days_list = [f"{DAYS_NAMES[v.day_of_week]} ({v.target_date.strftime('%d.%m')})" for v in vacancies]
        days_joined = ", ".join(days_list)
        mode_note = " <i>(нужен человек на все дни)</i>" if require_all_days else ""
        date_header = f"🗓 Даты: <b>{days_joined}</b>{mode_note}"

    student_card_text = (
        f"⚡ <b>Новая смена под твое расписание!</b>\n\n"
        f"🏢 Работодатель: <b>{html.escape(company_name)}</b>\n"
        f"{date_header}\n"
        f"⏰ Время: <b>{time_str}</b>\n"
        f"{pay_line}\n"
        f"📝 <b>Описание:</b>\n"
        f"{html.escape(first_v.raw_text)}"
    )

    sent_count = 0
    primary_vac_id = vacancies[0].id

    async with work_session_maker() as session:
        for uid in ready_candidate_ids:
            try:
                await bot.send_message(
                    chat_id=uid,
                    text=student_card_text,
                    reply_markup=student_vacancy_card_kb(primary_vac_id),
                )
                for v in vacancies:
                    session.add(VacancyDelivery(vacancy_id=v.id, user_id=uid, status="sent"))
                sent_count += 1
            except TelegramForbiddenError:
                pass
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                try:
                    await bot.send_message(
                        chat_id=uid,
                        text=student_card_text,
                        reply_markup=student_vacancy_card_kb(primary_vac_id),
                    )
                    for v in vacancies:
                        session.add(VacancyDelivery(vacancy_id=v.id, user_id=uid, status="sent"))
                    sent_count += 1
                except Exception:
                    pass
            except Exception:
                pass

            await asyncio.sleep(0.04)

        await session.commit()

    metrics_service.track_deliveries_sent(sent_count)

    await callback.message.edit_text(
        f"✅ <b>Рассылка успешно выполнена!</b>\n\n"
        f"📬 Доставлено активным кандидатам: <b>{sent_count} чел.</b>\n"
        f"🌐 Всего свободно студентов в эти дни: <b>{total_potential} чел.</b>\n\n"
        f"<i>Как только появятся первые отклики, бот сразу пришлет контакты студентов в этот чат!</i>"
    )
    await callback.answer()