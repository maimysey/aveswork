import asyncio
import os
import html
import uuid
from datetime import datetime, time, timedelta
from aiogram import Router, Bot
from aiogram.filters import Command, CommandObject, BaseFilter
from aiogram.types import Message, FSInputFile
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter
from sqlalchemy import select, func, delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import (
    ADMIN_IDS, WORK_MODE, WORK_RELAY_BOTS, WORK_TESTERS, WORK_WEB_ACCESS,
    RELAY_MAX_PER_DAY, RELAY_MAX_PER_WEEK, QUIET_FROM, QUIET_TO, get_minsk_now, logger,
)
from database import work_session_maker
from models import Employer, WorkUser, Vacancy, VacancyDelivery, UserBusySlot, RelayOutbox, WorkPref
from services.availability import find_students, forget_student
from services.matcher import relay_channel
from services.metrics import metrics_service
from services.relay import queue_stats
from services.shift_meta import describe

router = Router()

DEMO_EMPLOYER_ID = 0


class IsAdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        return message.from_user.id in ADMIN_IDS


router.message.filter(IsAdminFilter())


# ==================== СПРАВКА АДМИНИСТРАТОРА ====================

@router.message(Command("admin"))
@router.message(Command("ahelp"))
async def cmd_admin_help(message: Message):
    text = (
        "👑 <b>Панель управления AvesWork</b>\n\n"
        "<b>Управление бизнесом:</b>\n"
        "• <code>/add_biz &lt;tg_id&gt; &lt;Компания&gt;</code> — авторизовать менеджера\n"
        "• <code>/del_biz &lt;tg_id&gt;</code> — отозвать доступ у менеджера\n"
        "• <code>/employers</code> — список подключенных партнеров\n\n"
        "<b>Аналитика и система:</b>\n"
        "• <code>/stats</code> — полная статистика платформы и DAU\n"
        "• <code>/tech Текст</code> — рассылка оповещения всем соискателям\n"
        "• <code>/logs</code> — скачать лог-файл (work_bot.log)\n\n"
        "<b>Тест раздела «Смены»:</b>\n"
        "• <code>/work</code> — режим, тестеры, очередь коротких уведомлений\n"
        "• <code>/demo</code> — 3 демо-смены (видят только тестеры)\n"
        "• <code>/demo_clear</code> — удалить демо-смены\n"
        "• <code>/relay_test</code> — прислать себе короткое уведомление через бота факультета"
    )
    await message.answer(text)


# ==================== АВТОРИЗАЦИЯ РАБОТОДАТЕЛЕЙ ====================

@router.message(Command("add_biz"))
@router.message(Command("add_employer"))
async def cmd_add_business(message: Message, command: CommandObject, bot: Bot):
    args = (command.args or "").split(maxsplit=1)
    if len(args) < 2 or not args[0].isdigit():
        await message.reply(
            "⚠️ <b>Формат команды:</b>\n"
            "<code>/add_biz &lt;telegram_id&gt; &lt;Название Компании&gt;</code>\n\n"
            "<i>Пример:</i> <code>/add_biz 987654321 Кофейня Varka</code>"
        )
        return

    manager_id = int(args[0])
    company_name = args[1].strip()

    async with work_session_maker() as session:
        emp = await session.get(Employer, manager_id)
        if not emp:
            emp = Employer(
                telegram_id=manager_id,
                company_name=company_name,
                is_active=True
            )
            session.add(emp)
        else:
            emp.company_name = company_name
            emp.is_active = True

        await session.commit()

    safe_company = html.escape(company_name)
    await message.answer(
        f"✅ <b>Работодатель успешно авторизован!</b>\n\n"
        f"🏢 Компания: <b>{safe_company}</b>\n"
        f"🆔 Telegram ID: <code>{manager_id}</code>"
    )

    # Отправка приветствия менеджеру
    try:
        await bot.send_message(
            chat_id=manager_id,
            text=(
                f"🎉 <b>Добро пожаловать в AvesWork!</b>\n\n"
                f"Ваш аккаунт подтвержден для компании <b>{safe_company}</b>.\n\n"
                "Теперь вы можете публиковать смены / вакансии прямо в этот чат простым текстом\n"
                "<i>Пример:</i>\n"
                "<code>Ищем бариста на смену в четверг с 16:00 до 21:00. Оплата 45 руб. Кофейня на Немиге</code>"
            )
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление менеджеру {manager_id}: {e}")


@router.message(Command("del_biz"))
async def cmd_delete_business(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg or not arg.isdigit():
        await message.reply("⚠️ Формат: <code>/del_biz &lt;telegram_id&gt;</code>")
        return

    manager_id = int(arg)
    async with work_session_maker() as session:
        emp = await session.get(Employer, manager_id)
        if not emp:
            await message.reply("Работодатель с таким ID не найден.")
            return

        emp.is_active = False
        await session.commit()

    await message.answer(f"🔒 Доступ для менеджера <code>{manager_id}</code> деактивирован.")


@router.message(Command("employers"))
async def cmd_list_employers(message: Message):
    async with work_session_maker() as session:
        employers = (await session.execute(
            select(Employer)
            .where(Employer.telegram_id != DEMO_EMPLOYER_ID)
            .order_by(Employer.created_at.desc())
        )).scalars().all()

    if not employers:
        await message.answer("Партнеры пока не зарегистрированы.")
        return

    text_lines = ["🏢 <b>Список работодателей в базе:</b>\n"]
    for emp in employers:
        status_tag = "🟢 Активен" if emp.is_active else "🔴 Отключен"
        safe_name = html.escape(emp.company_name)
        text_lines.append(
            f"• <b>{safe_name}</b> | ID: <code>{emp.telegram_id}</code> ({status_tag})"
        )

    await message.answer("\n".join(text_lines))


# ==================== СТАТИСТИКА И МЕТРИКИ ====================

@router.message(Command("stats"))
@router.message(Command("metrics"))
async def cmd_admin_stats(message: Message):
    # 1. Метрики в оперативной памяти
    m = metrics_service.get_stats()

    # 2. Метрики из базы данных PostgreSQL
    async with work_session_maker() as session:
        total_users = await session.scalar(select(func.count(WorkUser.telegram_id))) or 0
        active_users = await session.scalar(
            select(func.count(WorkUser.telegram_id)).where(WorkUser.is_active == True)
        ) or 0
        notif_users = await session.scalar(
            select(func.count(WorkUser.telegram_id)).where(WorkUser.notifications_enabled == True)
        ) or 0
        web_only = await session.scalar(
            select(func.count(WorkUser.telegram_id)).where(WorkUser.bot_started == False)
        ) or 0
        total_busy_slots = await session.scalar(select(func.count(UserBusySlot.id))) or 0

        total_employers = await session.scalar(
            select(func.count(Employer.telegram_id)).where(Employer.is_active == True)
        ) or 0
        total_vacancies = await session.scalar(
            select(func.count(Vacancy.id)).where(Vacancy.status != "draft").where(Vacancy.is_demo == False)
        ) or 0
        total_deliveries = await session.scalar(select(func.count(VacancyDelivery.id))) or 0
        total_applications = await session.scalar(
            select(func.count(VacancyDelivery.id)).where(VacancyDelivery.status == "applied")
        ) or 0

    conversion = (
        f"{(total_applications / total_deliveries * 100):.1f}%"
        if total_deliveries > 0 else "0.0%"
    )

    text = (
        f"📊 <b>Аналитика платформы AvesWork</b>\n"
        f"<i>Сервер активен с: {m['boot_time']}</i>\n"
        f"<i>Режим раздела «Смены»: {WORK_MODE}</i>\n\n"
        f"📈 <b>Активность за сегодня ({m['today_date']}):</b>\n"
        f"• 👥 DAU (активных соискателей): <b>{m['dau']}</b>\n"
        f"• ⚡ Запросов за сутки: <b>{m['daily_requests']}</b>\n"
        f"• 🕒 Пиковый час: <b>{m['peak_hour']}</b>\n"
        f"• 💼 Новых смен создано: <b>{m['daily_vacancies']}</b>\n"
        f"• 📬 Уведомлений отправлено: <b>{m['daily_deliveries']}</b>\n"
        f"• 🙋‍♂️ Откликов на смены: <b>{m['daily_applications']}</b>\n\n"
        f"👤 <b>База соискателей:</b>\n"
        f"• Всего зарегистрировано: <b>{total_users}</b> (только с сайта: {web_only})\n"
        f"• В активном поиске: <b>{active_users}</b>\n"
        f"• С включенными пушами: <b>{notif_users}</b>\n"
        f"• Задано личных занятых часов: <b>{total_busy_slots}</b>\n\n"
        f"🏢 <b>Бизнес и конверсия:</b>\n"
        f"• Активных работодателей: <b>{total_employers}</b>\n"
        f"• Всего смен в системе: <b>{total_vacancies}</b>\n"
        f"• Всего доставлено уведомлений: <b>{total_deliveries}</b>\n"
        f"• Всего откликов на работу: <b>{total_applications}</b>\n"
        f"• Средняя конверсия в отклик: <b>{conversion}</b>"
    )
    await message.answer(text)


# ==================== РАЗДЕЛ «СМЕНЫ»: РЕЖИМ И ОЧЕРЕДЬ ====================

@router.message(Command("work"))
async def cmd_work_status(message: Message):
    q = await queue_stats()
    bots = ", ".join(f"{k} → {v}" for k, v in WORK_RELAY_BOTS.items()) or "не заданы"
    testers = ", ".join(f"<code>{t}</code>" for t in sorted(WORK_TESTERS)) or "—"
    mode = "🧪 тест — короткие уведомления только тестерам" if WORK_MODE != "live" else "🟢 боевой — всем подходящим"
    await message.answer(
        "💼 <b>Раздел «Смены»</b>\n\n"
        f"• Режим: <b>{mode}</b>\n"
        f"• Сайт виден: <b>{'всем' if WORK_WEB_ACCESS == 'all' else 'только тестерам'}</b>\n"
        f"• Тестеры: {testers}\n"
        f"• Боты факультетов: {html.escape(bots)}\n"
        f"• Лимиты: {RELAY_MAX_PER_DAY}/день, {RELAY_MAX_PER_WEEK}/неделю, тихие часы {QUIET_FROM}:00–{QUIET_TO}:00\n\n"
        "📨 <b>Короткие уведомления за сутки:</b>\n"
        f"• Отправлено: <b>{q.get('sent', 0)}</b>\n"
        f"• В очереди: <b>{q.get('pending', 0)}</b>\n"
        f"• Бот заблокирован у студента: <b>{q.get('blocked', 0)}</b>\n"
        f"• Не дошло после повторов: <b>{q.get('failed', 0)}</b>\n"
        f"• Пропущено (смену сняли / началась): <b>{q.get('skipped', 0)}</b>\n"
        f"• Всего отписались «Не присылать»: <b>{q.get('opted_out_total', 0)}</b>\n\n"
        "<i>Режим и тестеры меняются переменными WORK_MODE и WORK_TESTERS на Render</i>"
    )


_DEMO = [
    ("Ищем бариста в кофейню на Немиге, {d1} с 17:00 до 21:00. Оплата 45 руб за смену. Научим всему на месте", 1, (17, 0), (21, 0), 45, "BYN/смену"),
    ("Промоутер в ТЦ Galleria Minsk, {d1} с 12 до 16, 10 руб/час. Раздаём дегустацию нового чая", 2, (12, 0), (16, 0), 10, "BYN/час"),
    ("Помощник на склад книжного магазина, инвентаризация {d1}–{d2} с 10 до 15, 60 BYN за смену", 5, (10, 0), (15, 0), 60, "BYN/смену"),
]


@router.message(Command("demo"))
async def cmd_demo(message: Message):
    today = get_minsk_now().date()
    now = get_minsk_now()
    created = 0
    async with work_session_maker() as session:
        if not await session.get(Employer, DEMO_EMPLOYER_ID):
            session.add(Employer(telegram_id=DEMO_EMPLOYER_ID, company_name="Демо-компания Aves", is_active=False))
            await session.flush()
        for tmpl, offset, (sh, sm), (eh, em), pay, unit in _DEMO:
            first = today + timedelta(days=offset)
            days = [first, first + timedelta(days=1)] if "{d2}" in tmpl else [first]
            raw = tmpl.format(d1=first.strftime("%d.%m"), d2=days[-1].strftime("%d.%m"))
            meta = describe(raw)
            key = "demo" + uuid.uuid4().hex[:12]
            for d in days:
                session.add(Vacancy(
                    employer_id=DEMO_EMPLOYER_ID, raw_text=raw, target_date=d, day_of_week=d.weekday(),
                    start_time=time(sh, sm), end_time=time(eh, em), pay_amount=pay, pay_unit=unit,
                    title=meta["title"], category=meta["category"], status="active", batch_key=key,
                    require_all_days=len(days) > 1, is_demo=True, published_at=now,
                ))
                created += 1
        await session.commit()
    await message.answer(
        f"🧪 Создано демо-смен: <b>{created}</b>\n"
        "Их видят только тестеры — откройте раздел «Смены» на сайте.\n"
        "Отклики на демо-смены приходят владельцу. Удалить: /demo_clear"
    )


@router.message(Command("demo_clear"))
async def cmd_demo_clear(message: Message):
    async with work_session_maker() as session:
        res = await session.execute(delete(Vacancy).where(Vacancy.is_demo == True))
        await session.commit()
    await message.answer(f"🧹 Удалено демо-смен: <b>{res.rowcount or 0}</b>")


@router.message(Command("relay_test"))
async def cmd_relay_test(message: Message):
    uid = message.from_user.id
    forget_student(uid)
    refs = await find_students(uid)
    if not refs:
        await message.answer("⚠️ Вы не выбрали группу ни в одном боте расписания — короткое уведомление некуда отправить.")
        return
    async with work_session_maker() as session:
        pref = await session.get(WorkPref, uid)
    faculty = relay_channel({r.faculty: r for r in refs}, pref.relay_faculty if pref else None)
    if not faculty:
        await message.answer("⚠️ Для ваших факультетов не задан бот в WORK_RELAY_BOTS.")
        return

    async with work_session_maker() as session:
        vac = (await session.execute(
            select(Vacancy)
            .where(Vacancy.status == "active")
            .where(Vacancy.target_date >= get_minsk_now().date())
            .order_by(Vacancy.is_demo.desc(), Vacancy.target_date)
            .limit(1)
        )).scalar_one_or_none()
        if not vac:
            await message.answer("Нет активных смен. Сначала создайте демо: /demo")
            return
        now = get_minsk_now()
        await session.execute(
            pg_insert(RelayOutbox).values(
                user_id=uid, faculty=faculty, vacancy_id=vac.id, status="pending",
                attempts=0, not_before=now, created_at=now,
            ).on_conflict_do_update(
                constraint="uq_relay_user_vacancy",
                set_={"status": "pending", "attempts": 0, "not_before": now, "error": None},
            )
        )
        await session.commit()
    await message.answer(
        f"📨 Поставлено в очередь: смена #{vac.id} → бот факультета <b>{faculty}</b>.\n"
        "Придёт в течение ~20 секунд (если бот факультета спит — до минуты). Статус: /work"
    )


# ==================== ТЕХНИЧЕСКАЯ РАССЫЛКА ====================

@router.message(Command("tech"))
async def cmd_tech_broadcast(message: Message, command: CommandObject, bot: Bot):
    broadcast_text = command.args
    if not broadcast_text:
        await message.reply("⚠️ Формат: <code>/tech Текст системного сообщения</code>")
        return

    async with work_session_maker() as session:
        user_ids = (await session.execute(
            select(WorkUser.telegram_id).where(WorkUser.is_active == True).where(WorkUser.bot_started == True)
        )).scalars().all()

    total = len(user_ids)
    sent = 0
    blocked_count = 0
    errors = 0

    status_msg = await message.answer(f"⏳ Рассылка на {total} соискателей запущена...")

    safe_text = html.escape(broadcast_text)
    for uid in user_ids:
        try:
            await bot.send_message(
                chat_id=uid,
                text=f"📢 <b>СИСТЕМНОЕ ОПОВЕЩЕНИЕ</b>\n\n{safe_text}"
            )
            sent += 1
        except TelegramForbiddenError:
            blocked_count += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(
                    chat_id=uid,
                    text=f"📢 <b>СИСТЕМНОЕ ОПОВЕЩЕНИЕ</b>\n\n{safe_text}"
                )
                sent += 1
            except Exception:
                errors += 1
        except Exception:
            errors += 1

        await asyncio.sleep(0.04)

    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"👥 Всего адресатов: <b>{total}</b>\n"
        f"📬 Доставлено: <b>{sent}</b>\n"
        f"🚫 Заблокировали бота: <b>{blocked_count}</b>\n"
        f"❌ Ошибок: <b>{errors}</b>"
    )


# ==================== ЛОГИРОВАНИЕ ====================

@router.message(Command("logs"))
async def cmd_get_logs(message: Message):
    log_path = "work_bot.log"
    if not os.path.exists(log_path) or os.path.getsize(log_path) == 0:
        await message.reply("Лог-файл пуст или еще не создан.")
        return

    try:
        await message.reply_document(
            document=FSInputFile(log_path, filename="work_bot_logs.txt"),
            caption="📄 Логи работы AvesWork"
        )
    except Exception as e:
        await message.reply(f"❌ Не удалось отправить логи: {e}")
