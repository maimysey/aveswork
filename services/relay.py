"""
Короткие уведомления о сменах через ботов расписания факультетов.

AvesWork складывает уведомления в очередь (work_relay_outbox), фоновый цикл
раз в 20 секунд отдаёт их пачками боту факультета: POST {бот}/internal/work/notify.
Бот отправляет сообщение от своего имени и отвечает, кому дошло.

Защита от спама:
  • не больше RELAY_MAX_PER_DAY / RELAY_MAX_PER_WEEK уведомлений на человека;
  • ночью (WORK_QUIET_HOURS) не пишем — уведомление уходит утром, если смена ещё актуальна;
  • кнопка «Не присылать» в каждом сообщении, в тестовом режиме — только тестерам.

Запросы между сервисами подписываются ключом из AVES_SESSION_SECRET (он уже одинаковый во всех ботах).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time as _clock
from collections import defaultdict
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import (
    AVES_SESSION_SECRET,
    QUIET_FROM,
    QUIET_TO,
    RELAY_MAX_PER_DAY,
    RELAY_MAX_PER_WEEK,
    WORK_RELAY_BOTS,
    get_minsk_now,
    logger,
)
from database import work_session_maker
from models import Employer, RelayOutbox, Vacancy, VacancyDelivery, WorkPref
from services.metrics import metrics_service
from services.shift_format import is_upcoming, relay_text

BATCH_SIZE = 40
POLL_SECONDS = 20
MAX_ATTEMPTS = 4
# Не шлём уведомление, если до начала смены осталось меньше
MIN_LEAD = timedelta(minutes=45)


# ==================== ПОДПИСЬ ЗАПРОСОВ МЕЖДУ СЕРВИСАМИ ====================

def _key() -> bytes:
    return hmac.new(AVES_SESSION_SECRET.encode(), b"aves-internal-v1", hashlib.sha256).digest()


def sign(body: bytes, ts: int | None = None) -> str:
    ts = int(ts or _clock.time())
    mac = hmac.new(_key(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(header: str | None, body: bytes, max_age: int = 300) -> bool:
    if not AVES_SESSION_SECRET or not header:
        return False
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
        if abs(_clock.time() - ts) > max_age:
            return False
        expected = hmac.new(_key(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, parts["v1"])
    except Exception:
        return False


# ==================== ВРЕМЯ ОТПРАВКИ ====================

def is_quiet(dt: datetime) -> bool:
    h = dt.hour
    if QUIET_FROM > QUIET_TO:
        return h >= QUIET_FROM or h < QUIET_TO
    return QUIET_FROM <= h < QUIET_TO


def next_send_time(now: datetime) -> datetime:
    if not is_quiet(now):
        return now
    morning = now.replace(hour=QUIET_TO, minute=0, second=0, microsecond=0)
    return morning if morning > now else morning + timedelta(days=1)


def shift_start(v: Vacancy) -> datetime:
    return datetime.combine(v.target_date, v.start_time)


# ==================== НАСТРОЙКА «НЕ ПРИСЫЛАТЬ» ====================

async def set_relay_pref(uid: int, enabled: bool) -> None:
    async with work_session_maker() as session:
        pref = await session.get(WorkPref, uid)
        if pref:
            pref.relay_enabled = enabled
        else:
            session.add(WorkPref(telegram_id=uid, relay_enabled=enabled))
        await session.commit()


async def get_relay_pref(uid: int) -> bool:
    async with work_session_maker() as session:
        pref = await session.get(WorkPref, uid)
    return pref.relay_enabled if pref else True


# ==================== ПОСТАНОВКА В ОЧЕРЕДЬ ====================

async def enqueue(vacancies: list[Vacancy], relay: dict[int, str]) -> dict[str, int]:
    """relay: uid → факультет. Возвращает счётчики для отчёта работодателю."""
    stats = {"queued": 0, "limited": 0, "too_late": 0, "deferred": 0}
    if not vacancies or not relay:
        return stats

    now = get_minsk_now()
    send_at = next_send_time(now)
    first = min(vacancies, key=lambda v: (v.target_date, v.start_time))
    # Многодневную публикацию можно анонсировать, пока не прошёл хотя бы один из дней
    last = max(vacancies, key=lambda v: (v.target_date, v.start_time))
    if send_at > shift_start(last) - MIN_LEAD:
        stats["too_late"] = len(relay)
        return stats
    if send_at > now:
        stats["deferred"] = 1

    uids = list(relay)
    async with work_session_maker() as session:
        recent = (await session.execute(
            select(RelayOutbox.user_id, RelayOutbox.created_at)
            .where(RelayOutbox.user_id.in_(uids))
            .where(RelayOutbox.status.in_(("pending", "sent")))
            .where(RelayOutbox.created_at >= now - timedelta(days=7))
        )).all()
        day_cnt: dict[int, int] = defaultdict(int)
        week_cnt: dict[int, int] = defaultdict(int)
        for uid, created in recent:
            week_cnt[uid] += 1
            if created >= now - timedelta(days=1):
                day_cnt[uid] += 1

        rows = []
        for uid, faculty in relay.items():
            if day_cnt[uid] >= RELAY_MAX_PER_DAY or week_cnt[uid] >= RELAY_MAX_PER_WEEK:
                stats["limited"] += 1
                continue
            rows.append({
                "user_id": uid, "faculty": faculty, "vacancy_id": first.id,
                "status": "pending", "attempts": 0, "not_before": send_at, "created_at": now,
            })
        if rows:
            result = await session.execute(
                pg_insert(RelayOutbox).values(rows).on_conflict_do_nothing(constraint="uq_relay_user_vacancy")
            )
            stats["queued"] = result.rowcount if result.rowcount and result.rowcount > 0 else len(rows)
        await session.commit()
    return stats


# ==================== ОТПРАВКА ====================

async def _load_context(session, vac_ids: set[int]) -> tuple[dict[int, list[Vacancy]], dict[int, str]]:
    """vacancy_id → смены публикации (актуальные), employer_id → название компании."""
    primary = (await session.execute(select(Vacancy).where(Vacancy.id.in_(vac_ids)))).scalars().all()
    keys = {v.batch_key for v in primary if v.batch_key}
    siblings: dict[str, list[Vacancy]] = defaultdict(list)
    if keys:
        for v in (await session.execute(
            select(Vacancy).where(Vacancy.batch_key.in_(keys)).order_by(Vacancy.target_date, Vacancy.start_time)
        )).scalars().all():
            siblings[v.batch_key].append(v)

    batches: dict[int, list[Vacancy]] = {}
    for v in primary:
        group = siblings.get(v.batch_key, [v]) if v.batch_key else [v]
        batches[v.id] = [x for x in group if x.status == "active" and is_upcoming(x)]

    emp_ids = {v.employer_id for v in primary}
    companies = {
        e.telegram_id: e.company_name
        for e in (await session.execute(select(Employer).where(Employer.telegram_id.in_(emp_ids)))).scalars().all()
    }
    return batches, companies


async def _post(http: aiohttp.ClientSession, faculty: str, items: list[dict]) -> dict[str, str]:
    url = f"{WORK_RELAY_BOTS[faculty]}/internal/work/notify"
    body = json.dumps({"items": items}, ensure_ascii=False).encode()
    async with http.post(
        url, data=body,
        headers={"Content-Type": "application/json", "X-Aves-Signature": sign(body)},
        timeout=aiohttp.ClientTimeout(total=95),
    ) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}: {(await r.text())[:150]}")
        data = await r.json()
        return {str(k): str(v) for k, v in (data.get("results") or {}).items()}


async def process_once(http: aiohttp.ClientSession) -> int:
    now = get_minsk_now()
    async with work_session_maker() as session:
        rows = (await session.execute(
            select(RelayOutbox)
            .where(RelayOutbox.status == "pending")
            .where(RelayOutbox.not_before <= now)
            .order_by(RelayOutbox.id)
            .limit(BATCH_SIZE * 5)
        )).scalars().all()
        if not rows:
            return 0

        batches, companies = await _load_context(session, {r.vacancy_id for r in rows})
        seen_before = set((await session.execute(
            select(RelayOutbox.user_id)
            .where(RelayOutbox.user_id.in_({r.user_id for r in rows}))
            .where(RelayOutbox.status == "sent")
        )).scalars().all())

        work: dict[str, list[tuple[RelayOutbox, dict]]] = defaultdict(list)
        for r in rows:
            vacs = batches.get(r.vacancy_id) or []
            if r.faculty not in WORK_RELAY_BOTS:
                r.status, r.error = "skipped", "бот факультета не подключён"
                continue
            if not vacs or shift_start(vacs[-1]) - MIN_LEAD < now:
                r.status, r.error = "skipped", "смена снята или уже началась"
                continue
            v0 = vacs[0]
            work[r.faculty].append((r, {
                "uid": r.user_id,
                "vacancy_id": v0.id,
                "text": relay_text(vacs, companies.get(v0.employer_id, ""), v0.title,
                                   first_time=r.user_id not in seen_before),
            }))
        await session.commit()

    sent_total = 0
    for faculty, pairs in work.items():
        for i in range(0, len(pairs), BATCH_SIZE):
            chunk = pairs[i:i + BATCH_SIZE]
            try:
                results = await _post(http, faculty, [item for _, item in chunk])
                error = None
            except Exception as e:
                results, error = {}, str(e)[:200]
                logger.warning(f"📨 [Relay] Бот '{faculty}' не ответил: {error}")
            sent_total += await _apply_results([r for r, _ in chunk], results, error, batches)
    if sent_total:
        metrics_service.track_deliveries_sent(sent_total)
        logger.info(f"📨 [Relay] Короткие уведомления доставлены: {sent_total}")
    return sent_total


async def _apply_results(rows: list[RelayOutbox], results: dict[str, str], error: str | None,
                         batches: dict[int, list[Vacancy]]) -> int:
    now = get_minsk_now()
    sent = 0
    blocked: list[int] = []
    async with work_session_maker() as session:
        for r in rows:
            res = results.get(str(r.user_id))
            values: dict = {}
            if res == "sent":
                values = {"status": "sent", "sent_at": now, "error": None}
                sent += 1
                for v in batches.get(r.vacancy_id, []):
                    session.add(VacancyDelivery(vacancy_id=v.id, user_id=r.user_id, status="relay"))
            elif res in ("blocked", "banned"):
                values = {"status": "blocked", "error": res}
                blocked.append(r.user_id)
            else:
                attempts = (r.attempts or 0) + 1
                values = {
                    "attempts": attempts,
                    "error": (error or res or "нет ответа")[:200],
                    "status": "failed" if attempts >= MAX_ATTEMPTS else "pending",
                    "not_before": now + timedelta(minutes=2 * attempts),
                }
            await session.execute(update(RelayOutbox).where(RelayOutbox.id == r.id).values(**values))
        await session.commit()
    for uid in blocked:
        await set_relay_pref(uid, False)  # бот факультета у него заблокирован — больше не пробуем
    return sent


async def housekeeping() -> None:
    """Черновики, которые работодатель так и не отправил, и устаревшие хвосты очереди."""
    now = get_minsk_now()
    async with work_session_maker() as session:
        await session.execute(
            delete(Vacancy)
            .where(Vacancy.status == "draft")
            .where(Vacancy.target_date < now.date() - timedelta(days=1))
        )
        await session.execute(
            delete(RelayOutbox).where(RelayOutbox.created_at < now - timedelta(days=60))
        )
        await session.commit()


async def relay_worker() -> None:
    logger.info("📨 [Relay] Очередь коротких уведомлений запущена")
    last_cleanup = 0.0
    async with aiohttp.ClientSession() as http:
        while True:
            try:
                await process_once(http)
                if _clock.monotonic() - last_cleanup > 3600:
                    await housekeeping()
                    last_cleanup = _clock.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"📨 [Relay] Ошибка цикла: {e}", exc_info=True)
            await asyncio.sleep(POLL_SECONDS)


async def queue_stats() -> dict[str, int]:
    now = get_minsk_now()
    async with work_session_maker() as session:
        rows = (await session.execute(
            select(RelayOutbox.status, func.count())
            .where(RelayOutbox.created_at >= now - timedelta(days=1))
            .group_by(RelayOutbox.status)
        )).all()
        off = await session.scalar(
            select(func.count()).select_from(WorkPref).where(WorkPref.relay_enabled == False)  # noqa: E712
        ) or 0
    out = {k: v for k, v in rows}
    out["opted_out_total"] = off
    return out


# ==================== ВХОДЯЩИЙ ЗАПРОС ОТ БОТА ФАКУЛЬТЕТА ====================

async def internal_relay_pref(request: web.Request) -> web.Response:
    """Бот факультета сообщает: студент нажал «Не присылать» (или снова включил)."""
    body = await request.read()
    if not verify(request.headers.get("X-Aves-Signature"), body):
        return web.json_response({"error": "bad_signature"}, status=401)
    data = json.loads(body or b"{}")
    uid = int(data.get("uid") or 0)
    if not uid:
        return web.json_response({"error": "no_uid"}, status=400)
    await set_relay_pref(uid, bool(data.get("enabled", False)))
    return web.json_response({"ok": True})


def setup_relay_routes(app: web.Application) -> None:
    if not AVES_SESSION_SECRET:
        logger.error("📨 [Relay] AVES_SESSION_SECRET не задан — связка с ботами факультетов выключена")
        return
    app.router.add_post("/internal/relay/pref", internal_relay_pref)
