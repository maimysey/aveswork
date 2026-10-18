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
from dataclasses import dataclass
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
    FACULTY_BOT,
    FACULTY_SHORT,
    StudentRef,
    combined_schedule,
    find_students,
    group_label,
    m2s,
    overlaps,
    to_min,
)
from services.matcher import considered_faculties, relay_allowed_for, relay_channel, split_off
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

@dataclass
class Who:
    """Кто смотрит раздел: все его расписания, учитываемые расписания и настройки."""
    uid: int
    refs: list[StudentRef]          # все боты расписания, где выбрана группа
    used: list[StudentRef]          # те, что учитываем (без выключенных)
    primary: StudentRef | None      # для имени и @username в отклике
    pref: WorkPref | None
    off: set[str]


async def _whoami(request: web.Request) -> Who:
    uid = _uid(request)
    refs = await find_students(uid)
    async with work_session_maker() as session:
        pref = await session.get(WorkPref, uid)
    off = split_off(pref.schedules_off if pref else "")
    allowed = set(considered_faculties({r.faculty: r for r in refs}, off))
    used = [r for r in refs if r.faculty in allowed]
    prefer = request.query.get("f") or None
    primary = next((r for r in refs if r.faculty == prefer), refs[0] if refs else None)
    return Who(uid=uid, refs=refs, used=used, primary=primary, pref=pref, off=off)


def _t(v: time) -> str:
    return v.strftime("%H:%M")


def _parse_hhmm(raw: Any) -> time | None:
    try:
        h, m = str(raw).strip().split(":")[:2]
        return time(int(h), int(m))
    except Exception:
        return None


def _group_busy(rows: list[UserBusySlot]) -> list[dict]:
    """Одинаковые интервалы на разные дни показываем одной строкой."""
    merged: dict[tuple, dict] = {}
    for r in sorted(rows, key=lambda x: (x.start_time, x.day_of_week)):
        key = (r.start_time, r.end_time, r.title)
        item = merged.setdefault(key, {"ids": [], "days": [], "start": _t(r.start_time),
                                       "end": _t(r.end_time), "title": r.title})
        item["ids"].append(r.id)
        if r.day_of_week not in item["days"]:
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
    return [{"start": m2s(a), "end": m2s(b)} for a, b in out if b > a]


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
    who = await _whoami(request)
    uid = who.uid
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

    by_fac = {r.faculty: r for r in who.refs}
    channel = relay_channel(by_fac, who.pref.relay_faculty if who.pref else None)
    schedules = []
    for r in who.refs:
        schedules.append({
            "faculty": r.faculty,
            "title": FACULTY_SHORT.get(r.faculty, r.faculty),
            "bot": FACULTY_BOT.get(r.faculty, r.faculty),
            "group": await group_label(r),
            "used": r.faculty not in who.off,
            "relay": r.faculty in WORK_RELAY_BOTS,
        })

    return ok({
        "mode": WORK_MODE,
        "tester": is_tester(uid),
        "bot": await _bot_username(request),
        "employer": is_employer,
        "student": {
            "faculty": who.primary.faculty,
            "group": await group_label(who.primary),
            "subgroup": who.primary.subgroup or 0,
        } if who.primary else None,
        "schedules": schedules,
        "settings": {
            "search_active": wu.is_active if wu else True,
            "bot_connected": bool(wu and wu.bot_started),
            "bot_notifications": wu.notifications_enabled if wu else True,
            "relay_enabled": who.pref.relay_enabled if who.pref else True,
            "relay_available": bool(channel and relay_allowed_for(uid)),
            "relay_faculty": channel,
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

    if any(k in body for k in ("relay_enabled", "schedules_off", "relay_faculty")):
        refs = {r.faculty for r in await find_students(uid)}
        async with work_session_maker() as session:
            pref = await session.get(WorkPref, uid)
            if not pref:
                pref = WorkPref(telegram_id=uid, relay_enabled=True, schedules_off="")
                session.add(pref)
            if "relay_enabled" in body:
                pref.relay_enabled = bool(body["relay_enabled"])
            if "schedules_off" in body:
                off = {str(f) for f in (body["schedules_off"] or []) if str(f) in refs}
                pref.schedules_off = ",".join(sorted(off))
            if "relay_faculty" in body:
                fac = body["relay_faculty"]
                if fac is not None and (fac not in refs or fac not in WORK_RELAY_BOTS):
                    return fail(400, "bad_faculty", "Через этого бота присылать нельзя.")
                pref.relay_faculty = fac
            await session.commit()

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
        same = set((await session.execute(
            select(UserBusySlot.day_of_week).where(UserBusySlot.user_id == uid, UserBusySlot.start_time == start,
                                                   UserBusySlot.end_time == end, UserBusySlot.title == title)
        )).scalars().all())
        for d in days:
            if d not in same:   # повторное «Добавить» не плодит дубли
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
    who = await _whoami(request)
    uid = who.uid
    now = get_minsk_now()
    today = now.date()
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
    # Все учитываемые расписания сразу: человек в двух ботах свободен, только если свободен в обоих
    schedule = await combined_schedule(who.used, dates_needed) if who.used else {}

    def lesson_conflict(v: Vacancy) -> str | None:
        b = overlaps(schedule.get(v.target_date, []), v.start_time, v.end_time)
        return b[2] if b else None

    def personal_conflict(v: Vacancy) -> str | None:
        for b in personal:
            if b.day_of_week == v.day_of_week and b.start_time < v.end_time and b.end_time > v.start_time:
                return f"{b.title}, {_t(b.start_time)}–{_t(b.end_time)}"
        return None

    taken = [v for v in vacs if my.get(v.id) == "applied"]

    def shift_conflict(v: Vacancy) -> str | None:
        # уже взятая смена в то же время (другая публикация)
        for t in taken:
            if t.id == v.id or (v.batch_key and t.batch_key == v.batch_key):
                continue
            if t.target_date == v.target_date and t.start_time < v.end_time and t.end_time > v.start_time:
                return f"{t.title or 'Смена'}, {_t(t.start_time)}–{_t(t.end_time)}"
        return None

    def conflict_of(v: Vacancy) -> dict | None:
        c = lesson_conflict(v)
        if c:
            return {"kind": "lesson", "label": c}
        c = shift_conflict(v)
        if c:
            return {"kind": "shift", "label": c}
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
        lessons = [{"start": m2s(s), "end": m2s(e), "label": label} for s, e, label in schedule.get(d, [])]
        busy = [
            {"start": _t(b.start_time), "end": _t(b.end_time), "title": b.title}
            for b in personal if b.day_of_week == d.weekday()
        ]
        shifts = [{"id": t.id, "start": _t(t.start_time), "end": _t(t.end_time), "title": t.title or "Смена"}
                  for t in taken if t.target_date == d]
        blocks = [(s, e) for s, e, _ in schedule.get(d, [])]
        blocks += [(to_min(b.start_time), to_min(b.end_time)) for b in personal if b.day_of_week == d.weekday()]
        blocks += [(to_min(t.start_time), to_min(t.end_time)) for t in taken if t.target_date == d]
        if d == today:
            blocks.append((DAY_FROM, max(DAY_FROM, now.hour * 60 + now.minute)))
        week.append({"date": d.isoformat(), "lessons": lessons, "busy": busy, "shifts": shifts,
                     "free": _free_windows(blocks)})

    return ok({
        "mode": WORK_MODE,
        "today": today.isoformat(),
        "now": now.strftime("%H:%M"),
        "has_schedule": bool(who.used),
        "schedules_used": [FACULTY_SHORT.get(r.faculty, r.faculty) for r in who.used],
        "search_active": wu.is_active if wu else True,
        "week": week,
        "items": items,
    })


# ───────────────────────── «Мои часы» ─────────────────────────

HOURS_MIN_GAP = 30   # свободным считаем окно от 30 минут: перемены между парами — не время


def _union(iv: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(x for x in iv if x[1] > x[0]):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _clip(iv: list[tuple[int, int]], lo: int, hi: int) -> list[tuple[int, int]]:
    return [(max(a, lo), min(b, hi)) for a, b in iv if min(b, hi) > max(a, lo)]


def _total(iv: list[tuple[int, int]]) -> int:
    return sum(b - a for a, b in _union(iv))


def _hours_bounds(request: web.Request) -> tuple[int, int]:
    raw_to = (request.query.get("to") or "").strip()
    lo, hi = _parse_hhmm(request.query.get("from")), _parse_hhmm(raw_to)
    lo_m = to_min(lo) if lo else DAY_FROM
    hi_m = 24 * 60 if raw_to in ("24:00", "00:00") else (to_min(hi) if hi else DAY_TO)
    if not (0 <= lo_m < hi_m <= 24 * 60) or hi_m - lo_m < 4 * 60:
        return DAY_FROM, DAY_TO
    return lo_m, hi_m


async def api_hours(request: web.Request) -> web.Response:
    """Свободные часы недели: пары (все учитываемые расписания), взятые смены и личные занятые часы."""
    who = await _whoami(request)
    uid = who.uid
    now = get_minsk_now()
    today = now.date()
    now_min = now.hour * 60 + now.minute
    try:
        offset = max(-1, min(4, int(request.query.get("week", "0"))))
    except ValueError:
        offset = 0
    lo, hi = _hours_bounds(request)
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    days = [monday + timedelta(days=i) for i in range(7)]

    async with work_session_maker() as session:
        personal = (await session.execute(
            select(UserBusySlot).where(UserBusySlot.user_id == uid)
        )).scalars().all()
        taken_rows = (await session.execute(
            select(Vacancy, Employer.company_name)
            .join(VacancyDelivery, VacancyDelivery.vacancy_id == Vacancy.id)
            .join(Employer, Employer.telegram_id == Vacancy.employer_id, isouter=True)
            .where(VacancyDelivery.user_id == uid)
            .where(VacancyDelivery.status == "applied")
            .where(Vacancy.status.in_(("active", "closed")))
            .where(Vacancy.target_date >= days[0])
            .where(Vacancy.target_date <= days[-1])
            .order_by(Vacancy.target_date, Vacancy.start_time)
        )).all()
        wu = await session.get(WorkUser, uid)

    schedule = await combined_schedule(who.used, days) if who.used else {}

    out_days = []
    tot = {"free": 0, "free_left": 0, "study": 0, "shifts": 0, "busy": 0}
    for d in days:
        study = [(s, e) for s, e, _ in schedule.get(d, [])]
        shifts = [(v, comp) for v, comp in taken_rows if v.target_date == d]
        shift_iv = [(to_min(v.start_time), to_min(v.end_time)) for v, _ in shifts]
        mine = [b for b in personal if b.day_of_week == d.weekday()]
        busy_iv = [(to_min(b.start_time), to_min(b.end_time)) for b in mine]

        occupied = _union(_clip(study + shift_iv + busy_iv, lo, hi))
        windows, cur = [], lo
        for a, b in occupied + [(hi, hi)]:
            if a - cur >= HOURS_MIN_GAP:
                windows.append((cur, a))
            cur = max(cur, b)

        past_until = hi if d < today else (now_min if d == today else lo)
        win_json, free, free_left = [], 0, 0
        for a, b in windows:
            free += b - a
            left = max(0, b - max(a, past_until))
            if left >= HOURS_MIN_GAP:
                free_left += left
            win_json.append({"start": m2s(a), "end": m2s(b), "minutes": b - a,
                             "past": b <= past_until, "now": a < past_until < b})
        day = {
            "date": d.isoformat(),
            "weekday": d.weekday(),
            "is_today": d == today,
            "is_past": d < today,
            "lessons": [{"start": m2s(s), "end": m2s(e), "label": label} for s, e, label in schedule.get(d, [])],
            "shifts": [{"id": v.id, "start": _t(v.start_time), "end": _t(v.end_time), "title": v.title or "Смена",
                        "company": comp or "", "place": detect_place(v.raw_text), "closed": v.status != "active"}
                       for v, comp in shifts],
            "busy": [{"id": b.id, "start": _t(b.start_time), "end": _t(b.end_time), "title": b.title} for b in mine],
            "free": win_json,
            "minutes": {"free": free, "free_left": free_left,
                        "study": _total(_clip(study, lo, hi)), "shifts": _total(_clip(shift_iv, lo, hi)),
                        "busy": _total(_clip(busy_iv, lo, hi))},
        }
        for k in tot:
            tot[k] += day["minutes"][k]
        out_days.append(day)

    return ok({
        "today": today.isoformat(),
        "now": now.strftime("%H:%M"),
        "week": offset,
        "monday": days[0].isoformat(),
        "sunday": days[-1].isoformat(),
        "day_from": m2s(lo),
        "day_to": m2s(hi) if hi < 24 * 60 else "24:00",
        "min_gap": HOURS_MIN_GAP,
        "has_schedule": bool(who.used),
        "schedules_used": [FACULTY_SHORT.get(r.faculty, r.faculty) for r in who.used],
        "search_active": wu.is_active if wu else True,
        "totals": tot,
        "days": out_days,
    })


# ───────────────────────── отклик ─────────────────────────

_APPLY_ERRORS = {
    "gone": (410, "Смену уже сняли с публикации."),
    "past": (410, "Эта смена уже началась."),
    "own": (400, "Это ваша собственная смена."),
}


async def _who(request: web.Request) -> tuple[str | None, str | None]:
    refs = await find_students(_uid(request))
    name = next((r.name for r in refs if r.name), None) or request["aves_user"].get("name")
    username = next((r.username for r in refs if r.username), None)
    return name, username


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
    r.add_get("/api/work/hours", api_hours)
    r.add_post("/api/work/busy", api_busy_add)
    r.add_delete("/api/work/busy", api_busy_delete)
    r.add_post("/api/work/busy/delete", api_busy_delete)
    r.add_post("/api/work/shifts/{vid:\\d+}/apply", api_apply)
    r.add_post("/api/work/shifts/{vid:\\d+}/withdraw", api_withdraw)

    logger.info(f"💼 [WorkAPI] API подработок для сайта подключён: mode={WORK_MODE}, "
                f"access={WORK_WEB_ACCESS}, origins={sorted(AVES_WEB_ORIGINS)}")
