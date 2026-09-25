import asyncio
import html
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import BaseFilter
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter
from sqlalchemy import select

from config import logger
from database import work_session_maker
from models import Employer, Vacancy, VacancyDelivery
from services.nlp_parser import parse_vacancy_card
from services.matcher import find_available_candidates
from services.metrics import metrics_service
from keyboards import employer_preview_kb, student_vacancy_card_kb

router = Router()

DAYS_NAMES = [
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"
]


# ==================== ФИЛЬТР: ТОЛЬКО АВТОРИЗОВАННЫЙ РАБОТОДАТЕЛЬ ====================

class IsEmployerFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        async with work_session_maker() as session:
            emp = await session.get(Employer, message.from_user.id)
            return bool(emp and emp.is_active)


# ==================== ПРИЕМ И ПАРСИНГ ТЕКСТА СМЕНЫ ====================

@router.message(F.text, ~F.text.startswith("/"), IsEmployerFilter())
async def handle_employer_vacancy_post(message: Message):
    async with work_session_maker() as session:
        employer = await session.get(Employer, message.from_user.id)

    parsed = parse_vacancy_card(message.text)
    if not parsed:
        await message.reply(
            "⚠️ <b>Не удалось определить временной интервал смены!</b>\n\n"
            "Пожалуйста, обязательно укажите часы работы, например:\n"
            "• <code>с 17:00 до 21:00</code>\n"
            "• <code>12:00 - 16:30</code>\n"
            "• <code>с 18 до 22 завтра</code>"
        )
        return

    # Сохраняем проект смены в базу
    async with work_session_maker() as session:
        vacancy = Vacancy(
            employer_id=employer.telegram_id,
            raw_text=parsed["raw_text"],
            target_date=parsed["target_date"],
            day_of_week=parsed["day_of_week"],
            start_time=parsed["start_time"],
            end_time=parsed["end_time"],
            pay_amount=parsed["pay_amount"],
            pay_unit=parsed["pay_unit"],
        )
        session.add(vacancy)
        await session.commit()
        vac_id = vacancy.id

    metrics_service.track_vacancy_created()

    # Предварительный расчет свободной аудитории в Supabase
    total_potential, ready_candidates = await find_available_candidates(
        target_date=parsed["target_date"],
        day_of_week=parsed["day_of_week"],
        shift_start=parsed["start_time"],
        shift_end=parsed["end_time"],
    )

    day_title = DAYS_NAMES[parsed["day_of_week"]]
    date_str = parsed["target_date"].strftime("%d.%m.%Y")
    time_str = f"{parsed['start_time'].strftime('%H:%M')} — {parsed['end_time'].strftime('%H:%M')}"
    pay_str = f"<b>{parsed['pay_amount']} {parsed['pay_unit']}</b>" if parsed["pay_amount"] else "<i>По договоренности</i>"

    preview_text = (
        f"📋 <b>Параметры смены распознаны:</b>\n\n"
        f"🏢 Компания: <b>{html.escape(employer.company_name)}</b>\n"
        f"🗓 День: <b>{day_title} ({date_str})</b>\n"
        f"⏰ Время: <b>{time_str}</b>\n"
        f"💰 Оплата: {pay_str}\n\n"
        f"📊 <b>Аналитика аудитории под эти часы:</b>\n"
        f"• 👥 Всего студентов БГУ свободно в это время: <b>{total_potential} чел.</b>\n"
        f"• ⚡ Активных соискателей в AvesWork получат пуш: <b>{len(ready_candidates)} чел.</b>\n\n"
        "<i>Подтвердите публикацию для запуска адресного поиска:</i>"
    )

    await message.answer(preview_text, reply_markup=employer_preview_kb(vac_id))


# ==================== ОТМЕНА СМЕНЫ ====================

@router.callback_query(F.data.startswith("cancel_vac_"))
async def cb_cancel_vacancy(callback: CallbackQuery):
    vac_id = int(callback.data.split("_")[2])

    async with work_session_maker() as session:
        vac = await session.get(Vacancy, vac_id)
        if vac and vac.employer_id == callback.from_user.id:
            await session.delete(vac)
            await session.commit()

    await callback.message.edit_text("❌ <b>Публикация смены отменена.</b> Вы можете прислать новый текст")
    await callback.answer()


# ==================== ЗАПУСК АДРЕСНОЙ РАССЫЛКИ ====================

@router.callback_query(F.data.startswith("send_vac_"))
async def cb_broadcast_vacancy(callback: CallbackQuery, bot: Bot):
    vac_id = int(callback.data.split("_")[2])

    async with work_session_maker() as session:
        vac = await session.get(Vacancy, vac_id)
        if not vac:
            await callback.answer("Смена не найдена или уже удалена.", show_alert=True)
            return

        emp = await session.get(Employer, vac.employer_id)
        company_name = emp.company_name if emp else "Компания"

    await callback.message.edit_text("⏳ <b>Идет подбор кандидатов и рассылка пушей...</b>")

    total_potential, ready_candidate_ids = await find_available_candidates(
        target_date=vac.target_date,
        day_of_week=vac.day_of_week,
        shift_start=vac.start_time,
        shift_end=vac.end_time,
    )

    day_title = DAYS_NAMES[vac.day_of_week]
    date_str = vac.target_date.strftime("%d.%m")
    time_str = f"{vac.start_time.strftime('%H:%M')} – {vac.end_time.strftime('%H:%M')}"
    pay_line = f"💰 Оплата: <b>{vac.pay_amount} {vac.pay_unit}</b>\n" if vac.pay_amount else ""

    student_card_text = (
        f"⚡ <b>Новая смена под твое расписание!</b>\n\n"
        f"🏢 Работодатель: <b>{html.escape(company_name)}</b>\n"
        f"🗓 Дата: <b>{day_title} ({date_str})</b>\n"
        f"⏰ Время: <b>{time_str}</b>\n"
        f"{pay_line}\n"
        f"📝 <b>Описание:</b>\n"
        f"{html.escape(vac.raw_text)}"
    )

    sent_count = 0
    async with work_session_maker() as session:
        for uid in ready_candidate_ids:
            try:
                await bot.send_message(
                    chat_id=uid,
                    text=student_card_text,
                    reply_markup=student_vacancy_card_kb(vac.id),
                )
                session.add(VacancyDelivery(vacancy_id=vac.id, user_id=uid, status="sent"))
                sent_count += 1
            except TelegramForbiddenError:
                pass
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                try:
                    await bot.send_message(
                        chat_id=uid,
                        text=student_card_text,
                        reply_markup=student_vacancy_card_kb(vac.id),
                    )
                    session.add(VacancyDelivery(vacancy_id=vac.id, user_id=uid, status="sent"))
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
        f"🌐 Всего свободно студентов в этот день: <b>{total_potential} чел.</b>\n\n"
        f"<i>Как только появятся первые отклики, бот сразу пришлет контакты студентов в этот чат!</i>"
    )
    await callback.answer()