"""Отклик на смену — общий для кнопки в боте и сайта."""
from __future__ import annotations

import html

from aiogram import Bot
from sqlalchemy import select

from config import ADMIN_IDS, OWNER_ID, logger
from database import work_session_maker
from models import Employer, Vacancy, VacancyDelivery
from services.metrics import metrics_service
from services.shift_format import DAYS_NAMES, is_upcoming, time_range


async def _batch(session, vac: Vacancy, whole: bool) -> list[Vacancy]:
    if not (whole and vac.batch_key):
        return [vac]
    rows = (await session.execute(
        select(Vacancy)
        .where(Vacancy.batch_key == vac.batch_key)
        .where(Vacancy.status == "active")
        .order_by(Vacancy.target_date.asc())
    )).scalars().all()
    return [v for v in rows if is_upcoming(v)] or [vac]


def _who(uid: int, name: str | None, username: str | None) -> str:
    if username:
        return f"@{html.escape(username)}"
    return f"<a href='tg://user?id={uid}'>{html.escape(name or 'Студент')}</a>"


async def _notify(bot: Bot, emp: Employer | None, vacs: list[Vacancy], text: str, is_demo: bool) -> None:
    targets: list[int] = []
    if is_demo:
        targets = [OWNER_ID] if OWNER_ID else list(ADMIN_IDS[:1])
        text = "🧪 <b>[Демо-смена]</b>\n" + text
    elif emp and emp.is_active:
        targets = [emp.telegram_id]
    for chat_id in targets:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning(f"Не удалось доставить уведомление об отклике {chat_id}: {e}")


async def apply_to_shift(
    bot: Bot,
    vac_id: int,
    uid: int,
    *,
    name: str | None = None,
    username: str | None = None,
    source: str = "bot",
) -> tuple[str, list[Vacancy]]:
    """
    Статусы: ok | already | gone (снята) | past (уже прошла) | own (своя смена).
    Если работодателю нужен человек на все дни сразу — отклик идёт на всю публикацию.
    """
    async with work_session_maker() as session:
        vac = await session.get(Vacancy, vac_id)
        if not vac or vac.status != "active":
            return "gone", []
        if not is_upcoming(vac):
            return "past", [vac]
        if vac.employer_id == uid:
            return "own", [vac]

        vacs = await _batch(session, vac, whole=vac.require_all_days)
        ids = [v.id for v in vacs]
        existing = {
            d.vacancy_id: d
            for d in (await session.execute(
                select(VacancyDelivery)
                .where(VacancyDelivery.vacancy_id.in_(ids))
                .where(VacancyDelivery.user_id == uid)
            )).scalars().all()
        }
        if ids and all(existing.get(i) and existing[i].status == "applied" for i in ids):
            return "already", vacs

        for v in vacs:
            d = existing.get(v.id)
            if d:
                d.status = "applied"
            else:
                session.add(VacancyDelivery(vacancy_id=v.id, user_id=uid, status="applied"))
        await session.commit()
        emp = await session.get(Employer, vac.employer_id)

    metrics_service.track_application()

    dates = ", ".join(f"{DAYS_NAMES[v.day_of_week]} {v.target_date.strftime('%d.%m')}" for v in vacs)
    via = " через сайт" if source == "web" else ""
    who = _who(uid, name, username)
    hint = "" if username else "\n<i>У студента нет публичного @username — нажмите на имя, чтобы написать</i>"
    text = (
        f"🔔 <b>Новый отклик на смену{via}!</b>\n\n"
        f"🗓 Смена: <b>{dates} ({time_range(vacs[0])})</b>\n"
        f"👤 Соискатель: <b>{who}</b> (ID: <code>{uid}</code>)\n\n"
        f"<i>Свяжитесь со студентом для подтверждения выхода:</i> {who}{hint}"
    )
    await _notify(bot, emp, vacs, text, vac.is_demo)
    return "ok", vacs


async def withdraw_application(
    bot: Bot,
    vac_id: int,
    uid: int,
    *,
    name: str | None = None,
    username: str | None = None,
) -> str:
    async with work_session_maker() as session:
        vac = await session.get(Vacancy, vac_id)
        if not vac:
            return "gone"
        vacs = await _batch(session, vac, whole=vac.require_all_days)
        rows = (await session.execute(
            select(VacancyDelivery)
            .where(VacancyDelivery.vacancy_id.in_([v.id for v in vacs]))
            .where(VacancyDelivery.user_id == uid)
            .where(VacancyDelivery.status == "applied")
        )).scalars().all()
        if not rows:
            return "not_applied"
        for d in rows:
            d.status = "withdrawn"
        await session.commit()
        emp = await session.get(Employer, vac.employer_id)

    if vac.status == "active" and is_upcoming(vac):
        text = (
            f"↩️ <b>Студент отозвал отклик</b>\n\n"
            f"🗓 Смена: <b>{vac.target_date.strftime('%d.%m')} ({time_range(vac)})</b>\n"
            f"👤 {_who(uid, name, username)} (ID: <code>{uid}</code>)"
        )
        await _notify(bot, emp, vacs, text, vac.is_demo)
    return "ok"
