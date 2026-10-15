import os
import logging
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ==================== БОТ И ВЛАДЕЛЕЦ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Твой Telegram ID (как главного администратора)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Твой публичный юзернейм без @ (выдается бизнесу на старте)
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "aves_admin").lstrip("@")

# Список админов (по умолчанию ты + доп. ID из .env при необходимости)
raw_admin_ids = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in raw_admin_ids.split(",") if x.strip().isdigit()]
if OWNER_ID and OWNER_ID not in ADMIN_IDS:
    ADMIN_IDS.append(OWNER_ID)

# ==================== БАЗЫ ДАННЫХ (SUPABASE, сука) ====================
def _ensure_asyncpg_driver(url: str) -> str:
    if not url:
        return ""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://") and not url.startswith("postgresql+"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url

# 1. Собственная база сервиса AvesWork (Read/Write)
WORK_DB_URL = _ensure_asyncpg_driver(os.getenv("WORK_DB_URL", ""))

# 2. База Биофака (Read-Only)
BIO_DB_URL = _ensure_asyncpg_driver(os.getenv("BIO_DB_URL", ""))

# 3. База ФСК (Read-Only)
FSK_DB_URL = _ensure_asyncpg_driver(os.getenv("FSK_DB_URL", ""))

# SSL к Supabase. Для локальных тестов без SSL: DB_SSL=disable
DB_SSL = os.getenv("DB_SSL", "require").strip().lower()

# ==================== ВРЕМЯ И ТАЙМЗОНА ====================
MINSK_TZ = ZoneInfo("Europe/Minsk")

def get_minsk_now() -> datetime:
    return datetime.now(MINSK_TZ).replace(tzinfo=None)


# ==================== САЙТ И СВЯЗКА С ДРУГИМИ БОТАМИ ====================

def _id_set(raw: str) -> set[int]:
    return {int(x.strip()) for x in (raw or "").split(",") if x.strip().lstrip("-").isdigit()}


# Тот же секрет, что у ботов расписания и AvesStudy: сайт входит один раз на все боты
AVES_SESSION_SECRET = os.getenv("AVES_SESSION_SECRET", "")
AVES_WEB_ORIGINS = {
    o.strip().rstrip("/")
    for o in os.getenv("AVES_WEB_ORIGINS", "https://microcosmos-bio.me,https://www.microcosmos-bio.me").split(",")
    if o.strip()
}
AVES_SITE_URL = os.getenv("AVES_SITE_URL", "https://microcosmos-bio.me").rstrip("/")

# test — короткие уведомления через другие боты получают только тестеры (WORK_TESTERS + админы).
# live — все подходящие студенты. Полные карточки в самом AvesWork работают в обоих режимах.
WORK_MODE = "live" if os.getenv("WORK_MODE", "test").strip().lower() == "live" else "test"
WORK_TESTERS = _id_set(os.getenv("WORK_TESTERS", "")) | set(ADMIN_IDS)

# Кто видит раздел на сайте: all — все вошедшие, testers — только тестеры
WORK_WEB_ACCESS = "testers" if os.getenv("WORK_WEB_ACCESS", "all").strip().lower() == "testers" else "all"

# Боты, через которых уходят короткие уведомления: код факультета → адрес сервиса бота
WORK_RELAY_BOTS: dict[str, str] = {}
for _pair in os.getenv(
    "WORK_RELAY_BOTS", "bio=https://biobotm.onrender.com,fsk=https://mambafsk.onrender.com"
).split(","):
    if "=" in _pair:
        _code, _url = _pair.split("=", 1)
        if _code.strip() and _url.strip():
            WORK_RELAY_BOTS[_code.strip()] = _url.strip().rstrip("/")

# Лимиты коротких уведомлений на одного студента (чтобы боты расписания не превратились в спам)
RELAY_MAX_PER_DAY = int(os.getenv("WORK_RELAY_MAX_PER_DAY", "1"))
RELAY_MAX_PER_WEEK = int(os.getenv("WORK_RELAY_MAX_PER_WEEK", "3"))

# Тихие часы: короткие уведомления, созданные ночью, уходят утром
_quiet = os.getenv("WORK_QUIET_HOURS", "22-9").split("-")
QUIET_FROM = int(_quiet[0]) if len(_quiet) == 2 and _quiet[0].isdigit() else 22
QUIET_TO = int(_quiet[1]) if len(_quiet) == 2 and _quiet[1].isdigit() else 9

# Сколько минут закладываем на дорогу между корпусом и работой
TRAVEL_BUFFER_MIN = int(os.getenv("WORK_TRAVEL_BUFFER_MIN", "15"))

# На сколько дней вперёд лента показывает смены
FEED_DAYS_AHEAD = int(os.getenv("WORK_FEED_DAYS", "14"))


def is_tester(uid: int) -> bool:
    return uid in WORK_TESTERS

# ==================== ЛОГИРОВАНИЕ ====================
LOG_FORMAT = "%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
logger = logging.getLogger("AvesWork")
logger.setLevel(logging.INFO)

# Ротация логов: макс 5 МБ, до 3 файлов архива
file_handler = RotatingFileHandler("work_bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(LOG_FORMAT))

logger.addHandler(file_handler)
logger.addHandler(console_handler)
