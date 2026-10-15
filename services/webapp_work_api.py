"""
API раздела «Смены» для сайта microcosmos-bio.me и Mini App ботов расписания.

    from services.webapp_work_api import setup_work_api
    setup_work_api(app, bot)

Вход отдельно не нужен: сайт уже получил сессионный токен у бота расписания,
подпись проверяется тем же общим секретом AVES_SESSION_SECRET.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time as _clock
from datetime import date, datetime, time, timedelta
from typing import Any, Awaitable, Callable

from aiogram import Bot
from aiohttp import web
from sqlalchemy import delete, func, select, text

from config import (
    AVES_SESSION_SECRET,
    AVES_WEB_ORIGINS,
    FEED_DAYS_AHEAD,
    WORK_MODE,
    WORK_RELAY_BOTS,
    WORK_WEB_ACCESS,
    get_minsk_now,
    is_tester,
    logger,
)
from database import FACULTY_SESSIONS, work_session_maker
from models import Employer, UserBusySlot, Vacancy, VacancyDelivery, WorkPref, WorkUser
from services.applications import apply_to_shift, withdraw_application
from services.availability import (
    StudentRef,
    find_student,
    get_conflicting_slots,
    grid,
    group_label,
    hhmm,
    student_schedule,
    to_min,
)
from services.matcher import relay_allowed_for
from services.relay import get_relay_pref, set_relay_pref
from services.shift_format import is_upcoming, pay_text, shift_hours
from services.shift_meta import CATEGORY_TITLES, detect_place

BOT_KEY = web.AppKey("work_bot", Bot)

SESSION_TTL = 60 * 60 * 24 * 30
INIT_DATA_TTL = 60 * 60 * 24
PUBLIC_PATHS = {"/api/work/auth/webapp"}

DAY_FROM = 8 * 60    # окна дня на сайте рисуем с 08:00
DAY_TO = 22 * 60     # до 22:00
MIN_WINDOW = 60      # окна короче часа не показываем


# ───────────────────────── токены (формат ботов расписания) ─────────────────────────

def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def issue_token(uid: int, name: str | None) -> str:
    payload = json.dumps(
        {"uid": uid, "name": name or "", "exp": int(_clock.time()) + SESSION_TTL}, separators=(",", ":")
    ).encode()
    sig = hmac.new(AVES_SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def read_token(token: str) -> dict[str, Any] | None:
    try:
        p64, s64 = token.split(".", 1)
        payload = _b64d(p64)
        expected = hmac.new(AVES_SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(s64)):
            return None
        data = json.loads(payload)
        return data if int(data.get("exp", 0)) >= _clock.time() else None
    except Exception:
        return None


def ok(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


def fail(status: int, code: str, message: str, **extra) -> web.Response:
    return ok({"error": code, "message": message, **extra}, status=status)


def _uid(request: web.Request) -> int:
    return int(request["aves_user"]["uid"])


# ───────────────────────── баны (таблица bans в базе биофака) ─────────────────────────

_bans: tuple[float, set[int]] = (0.0, set())


async def _banned_ids() -> set[int]:
    global _bans
    if _clock.monotonic() - _bans[0] < 60:
        return _bans[1]
    ids: set[int] = set()
    maker = FACULTY_SESSIONS.get("bio")
    if maker:
        try:
            async with maker() as session:
                ids = set((await session.execute(text("SELECT telegram_id FROM bans"))).scalars().all())
        except Exception:
            ids = _bans[1]
    _bans = (_clock.monotonic(), ids)
    return ids


# ───────────────────────── middleware ─────────────────────────

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@web.middleware
async def work_cors(request: web.Request, handler: Handler) -> web.StreamResponse:
    if not request.path.startswith("/api/work"):
        return await handler(request)
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if request.method == "OPTIONS":
        resp: web.StreamResponse = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = fail(e.status, "http_error", e.reason)
        except Exception as e:
            logger.error(f"💼 [WorkAPI] {request.method} {request.path}: {e}", exc_info=True)
            resp = fail(500, "server_error", "Внутренняя ошибка. Попробуйте ещё раз через минуту.")
    if origin in AVES_WEB_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


@web.middleware
async def work_auth(request: web.Request, handler: Handler) -> web.StreamResponse:
    if not request.path.startswith("/api/work") or request.method == "OPTIONS" or request.path in PUBLIC_PATHS:
        return await handler(request)
    header = request.headers.get("Authorization", "")
    data = read_token(header[7:]) if header.startswith("Bearer ") else None
    if not data:
        return fail(401, "unauthorized", "Сессия истекла. Войдите через Telegram заново.")
    uid = int(data["uid"])
    if uid in await _banned_ids():
        return fail(403, "banned", "Доступ ограничен администратором за нарушение правил использования.")
    if WORK_WEB_ACCESS == "testers" and not is_tester(uid):
        return fail(403, "closed_beta", "Раздел подработок сейчас в закрытом тесте. Скоро откроем для всех.")
    request["aves_user"] = data
    return await handler(request)


# ───────────────────────── помощники ─────────────────────────

async def _student(request: web.Request) -> StudentRef | None:
    return await find_student(_uid(request), request.query.get("f") or None)


def _t(v: time) -> str:
    return v.strftime("%H:%M")


def _parse_hhmm(raw: Any) -> time | None:
    try:
        h, m = str(raw).strip().split(":")[:2]
        return time(int(h), int(m))
    except Exception:
        return None


def _slot_label(faculty: str, slot: int) -> str:
    a, b = grid(faculty)[slot]
    return f"{slot} пара, {hhmm(a)}–{hhmm(b)}"


def _group_busy(rows: list[UserBusySlot]) -> list[dict]:
    """Одинаковые интервалы на разные дни показываем одной строкой."""
    merged: dict[tuple, dict] = {}
    for r in sorted(rows, key=lambda x: (x.start_time, x.day_of_week)):
        key = (r.start_time, r.end_time, r.title)
        item = merged.setdefault(key, {"ids": [], "days": [], "start": _t(r.start_time),
                                       "end": _t(r.end_time), "title": r.title})
        item["ids"].append(r.id)
        item["days"].append(r.day_of_week)
    return sorted(merged.values(), key=lambda x: (min(x["days"]), x["start"]))


def _free_windows(busy: list[tuple[int, int]]) -> list[dict]:
    out, cur = [], DAY_FROM
    for s, e in sorted(busy):
        if s - cur >= MIN_WINDOW:
            out.append((cur, min(s, DAY_TO)))
        cur = max(cur, e)
    if DAY_TO - cur >= MIN_WINDOW:
        out.append((cur, DAY_TO))
    return [{"start": f"{a // 60:02d}:{a % 60:02d}", "end": f"{b // 60:02d}:{b % 60:02d}"}
            for a, b in out if b > a]


async def _bot_username(request: web.Request) -> str | None:
    try:
        me = await request.app[BOT_KEY].me()
        return me.username
    except Exception:
        return None


# ───────────────────────── вход из Mini App самого AvesWork ─────────────────────────

async def api_auth_webapp(request: web.Request) -> web.Response:
    """Кнопка «Лента смен» в AvesWork: initData подписан токеном AvesWork, выдаём общую сессию Aves."""
    from aiogram.utils.web_app import safe_parse_webapp_init_data
    from config import BOT_TOKEN

    body = await request.json()
    try:
        parsed = safe_parse_webapp_init_data(token=BOT_TOKEN, init_data=str(body.get("init_data") or ""))
    except ValueError:
        return fail(401, "bad_init_data", "Telegram не подтвердил вход. Откройте ленту из бота ещё раз.")
    auth_ts = parsed.auth_date.timestamp() if hasattr(parsed.auth_date, "timestamp") else int(parsed.auth_date)
    if _clock.time() - auth_ts > INIT_DATA_TTL:
        return fail(401, "stale_init_data", "Сессия Telegram устарела. Закройте и откройте ленту снова.")
    if not parsed.user:
        return fail(401, "no_user", "Telegram не передал пользователя.")
    uid = parsed.user.id
    if uid in await _banned_ids():
        return fail(403, "banned", "Доступ ограничен администратором за нарушение правил использования.")
    return ok({"token": issue_token(uid, parsed.user.first_name)})


# ───────────────────────── профиль ─────────────────────────

async def api_me(request: web.Request) -> web.Response:
    uid = _uid(request)
    ref = await _student(request)
    async with work_session_maker() as session:
        wu = await session.get(WorkUser, uid)
        busy = (await session.execute(
            select(UserBusySlot).where(UserBusySlot.user_id == uid)
        )).scalars().all()
        applied = await session.scalar(
            select(func.count(func.distinct(VacancyDelivery.vacancy_id)))
            .where(VacancyDelivery.user_id == uid)
            .where(VacancyDelivery.status == "applied")
        ) or 0
        is_employer = bool(await session.get(Employer, uid))
    relay_enabled = await get_relay_pref(uid)

    return ok({
        "mode": WORK_MODE,
        "tester": is_tester(uid),
        "bot": await _bot_username(request),
        "employer": is_employer,
        "student": {
            "faculty": ref.faculty,
            "group": await group_label(ref),
            "subgroup": ref.subgroup or 0,
        } if ref else None,
        "settings": {
            "search_active": wu.is_active if wu else True,
            "bot_connected": bool(wu and wu.bot_started),
            "bot_notifications": wu.notifications_enabled if wu else True,
            "relay_enabled": relay_enabled,
            "relay_available": bool(ref and ref.faculty in WORK_RELAY_BOTS and relay_allowed_for(uid)),
        },
        "busy": _group_busy(list(busy)),
        "stats": {"applied": applied},
    })


async def _ensure_work_user(session, uid: int) -> WorkUser:
    wu = await session.get(WorkUser, uid)
    if not wu:
        # Профиль с сайта: бот AvesWork этому человеку писать не может, пока тот не нажмёт «Старт»
        wu = WorkUser(telegram_id=uid, is_active=True, notifications_enabled=True, bot_started=False)
        session.add(wu)
        await session.flush()
    return wu


async def api_me_update(request: web.Request) -> web.Response:
    uid = _uid(request)
    body = await request.json()
    if "relay_enabled" in body:
        await set_relay_pref(uid, bool(body["relay_enabled"]))
    if "search_active" in body or "bot_notifications" in body:
        async with work_session_maker() as session:
            wu = await _ensure_work_user(session, uid)
            if "search_active" in body:
                wu.is_active = bool(body["search_active"])
            if "bot_notifications" in body:
                wu.notifications_enabled = bool(body["bot_notifications"])
            await session.commit()
    return await api_me(request)


async def api_busy_add(request: web.Request) -> web.Response:
    uid = _uid(request)
    body = await request.json()
    days = sorted({int(d) for d in (body.get("days") or []) if str(d).isdigit() and 0 <= int(d) <= 6})
    start, end = _parse_hhmm(body.get("start")), _parse_hhmm(body.get("end"))
    title = str(body.get("title") or "").strip()[:60] or "Личные дела"
    if not days:
        return fail(400, "no_days", "Выберите хотя бы один день.")
    if not start or not end or start >= end:
        return fail(400, "bad_time", "Время «до» должно быть позже времени «с».")
    async with work_session_maker() as session:
        await _ensure_work_user(session, uid)
        count = await session.scalar(select(func.count(UserBusySlot.id)).where(UserBusySlot.user_id == uid)) or 0
        if count + len(days) > 60:
            return fail(400, "too_many", "Слишком много интервалов. Удалите старые.")
        for d in days:
            session.add(UserBusySlot(user_id=uid, day_of_week=d, start_time=start, end_time=end, title=title))
        await session.commit()
    return await api_me(request)


async def api_busy_delete(request: web.Request) -> web.Response:
    uid = _uid(request)
    ids = [int(x) for x in (request.query.get("ids") or "").split(",") if x.strip().isdigit()][:30]
    if ids:
        async with work_session_maker() as session:
            await session.execute(
                delete(UserBusySlot).where(UserBusySlot.user_id == uid).where(UserBusySlot.id.in_(ids))
            )
            await session.commit()
    return await api_me(request)


# ───────────────────────── лента ─────────────────────────

def _vacancy_json(v: Vacancy, company: str, batch: list[Vacancy], now: datetime) -> dict[str, Any]:
    return {
        "id": v.id,
        "date": v.target_date.isoformat(),
        "start": _t(v.start_time),
        "end": _t(v.end_time),
        "hours": shift_hours(v),
        "title": v.title or "Смена",
        "company": company,
        "category": v.category or "other",
        "category_title": CATEGORY_TITLES.get(v.category or "other", "Другое"),
        "place": detect_place(v.raw_text),
        "pay": pay_text(v.pay_amount, v.pay_unit),
        "pay_amount": v.pay_amount,
        "text": v.raw_text,
        "demo": bool(v.is_demo),
        "new": bool(v.published_at and now - v.published_at < timedelta(hours=24)),
        "batch": {
            "key": v.batch_key or f"v{v.id}",
            "dates": [b.target_date.isoformat() for b in batch],
            "require_all": bool(v.require_all_days and len(batch) > 1),
        },
    }


async def api_feed(request: web.Request) -> web.Response:
    uid = _uid(request)
    now = get_minsk_now()
    today = now.date()
    ref = await _student(request)
    tester = is_tester(uid)

    async with work_session_maker() as session:
        vacs = (await session.execute(
            select(Vacancy)
            .where(Vacancy.status == "active")
            .where(Vacancy.target_date >= today)
            .where(Vacancy.target_date <= today + timedelta(days=FEED_DAYS_AHEAD))
            .order_by(Vacancy.target_date, Vacancy.start_time)
        )).scalars().all()
        vacs = [v for v in vacs if is_upcoming(v, now) and v.employer_id != uid and (tester or not v.is_demo)]

        companies = {
            e.telegram_id: e.company_name
            for e in (await session.execute(
                select(Employer).where(Employer.telegram_id.in_(list({v.employer_id for v in vacs}) or [0]))
            )).scalars().all()
        }
        personal = (await session.execute(
            select(UserBusySlot).where(UserBusySlot.user_id == uid)
        )).scalars().all()
        my = {
            vid: st for vid, st in (await session.execute(
                select(VacancyDelivery.vacancy_id, VacancyDelivery.status)
                .where(VacancyDelivery.user_id == uid)
                .where(VacancyDelivery.vacancy_id.in_([v.id for v in vacs] or [0]))
                .where(VacancyDelivery.status.in_(("applied", "withdrawn")))
            )).all()
        }
        wu = await session.get(WorkUser, uid)

    days = [today + timedelta(days=i) for i in range(7)]
    dates_needed = sorted({v.target_date for v in vacs} | set(days))
    schedule = await student_schedule(ref, dates_needed) if ref else {}

    def lesson_conflict(v: Vacancy) -> str | None:
        if not ref:
            return None
        clash = sorted(set(schedule.get(v.target_date, [])) & get_conflicting_slots(ref.faculty, v.start_time, v.end_time))
        return _slot_label(ref.faculty, clash[0]) if clash else None

    def personal_conflict(v: Vacancy) -> str | None:
        for b in personal:
            if b.day_of_week == v.day_of_week and b.start_time < v.end_time and b.end_time > v.start_time:
                return f"{b.title}, {_t(b.start_time)}–{_t(b.end_time)}"
        return None

    def conflict_of(v: Vacancy) -> dict | None:
        c = lesson_conflict(v)
        if c:
            return {"kind": "lesson", "label": c}
        c = personal_conflict(v)
        return {"kind": "personal", "label": c} if c else None

    batches: dict[str, list[Vacancy]] = {}
    for v in vacs:
        if v.batch_key:
            batches.setdefault(v.batch_key, []).append(v)

    items = []
    for v in vacs:
        batch = batches.get(v.batch_key, [v]) if v.batch_key else [v]
        item = _vacancy_json(v, companies.get(v.employer_id, ""), batch, now)
        conflict = conflict_of(v)
        if not conflict and item["batch"]["require_all"]:
            for other in batch:
                c = conflict_of(other)
                if c:
                    conflict = {"kind": c["kind"], "label": f"{other.target_date.strftime('%d.%m')}: {c['label']}"}
                    break
        item["fits"] = conflict is None
        item["conflict"] = conflict
        item["applied"] = my.get(v.id) == "applied"
        items.append(item)

    week = []
    for d in days:
        lessons = [] if not ref else [
            {"slot": s, "start": hhmm(grid(ref.faculty)[s][0]), "end": hhmm(grid(ref.faculty)[s][1])}
            for s in schedule.get(d, []) if s in grid(ref.faculty)
        ]
        busy = [
            {"start": _t(b.start_time), "end": _t(b.end_time), "title": b.title}
            for b in personal if b.day_of_week == d.weekday()
        ]
        blocks = [(to_min(_parse_hhmm(x["start"])), to_min(_parse_hhmm(x["end"]))) for x in lessons + busy]
        if d == today:
            blocks.append((DAY_FROM, max(DAY_FROM, now.hour * 60 + now.minute)))
        week.append({"date": d.isoformat(), "lessons": lessons, "busy": busy, "free": _free_windows(blocks)})

    return ok({
        "mode": WORK_MODE,
        "today": today.isoformat(),
        "now": now.strftime("%H:%M"),
        "has_schedule": ref is not None,
        "search_active": wu.is_active if wu else True,
        "week": week,
        "items": items,
    })


# ───────────────────────── отклик ─────────────────────────

_APPLY_ERRORS = {
    "gone": (410, "Смену уже сняли с публикации."),
    "past": (410, "Эта смена уже началась."),
    "own": (400, "Это ваша собственная смена."),
}


async def _who(request: web.Request) -> tuple[str | None, str | None]:
    ref = await _student(request)
    name = (ref.name if ref else None) or request["aves_user"].get("name")
    return name, (ref.username if ref else None)


async def api_apply(request: web.Request) -> web.Response:
    vid = int(request.match_info["vid"])
    name, username = await _who(request)
    status, vacs = await apply_to_shift(
        request.app[BOT_KEY], vid, _uid(request), name=name, username=username, source="web"
    )
    if status in _APPLY_ERRORS:
        code, msg = _APPLY_ERRORS[status]
        return fail(code, status, msg)
    return ok({"status": status, "applied_ids": [v.id for v in vacs], "has_username": bool(username)})


async def api_withdraw(request: web.Request) -> web.Response:
    vid = int(request.match_info["vid"])
    name, username = await _who(request)
    status = await withdraw_application(request.app[BOT_KEY], vid, _uid(request), name=name, username=username)
    if status == "gone":
        return fail(410, "gone", "Смену уже сняли с публикации.")
    return ok({"status": status})


# ───────────────────────── регистрация ─────────────────────────

def setup_work_api(app: web.Application, bot: Bot) -> None:
    if not AVES_SESSION_SECRET or len(AVES_SESSION_SECRET) < 32:
        logger.error("💼 [WorkAPI] AVES_SESSION_SECRET не задан или короче 32 символов — API сайта выключен")
        return

    app[BOT_KEY] = bot
    app.middlewares.append(work_cors)
    app.middlewares.append(work_auth)

    r = app.router
    r.add_post("/api/work/auth/webapp", api_auth_webapp)
    r.add_get("/api/work/me", api_me)
    r.add_patch("/api/work/me", api_me_update)
    r.add_get("/api/work/feed", api_feed)
    r.add_post("/api/work/busy", api_busy_add)
    r.add_delete("/api/work/busy", api_busy_delete)
    r.add_post("/api/work/shifts/{vid:\\d+}/apply", api_apply)
    r.add_post("/api/work/shifts/{vid:\\d+}/withdraw", api_withdraw)

    logger.info(f"💼 [WorkAPI] API подработок для сайта подключён: mode={WORK_MODE}, "
                f"access={WORK_WEB_ACCESS}, origins={sorted(AVES_WEB_ORIGINS)}")
