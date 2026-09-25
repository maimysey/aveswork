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

# ==================== БАЗЫ ДАННЫХ (SUPABASE) ====================
# 1. Собственная база сервиса AvesWork (Read/Write)
WORK_DB_URL = os.getenv("WORK_DB_URL", "")

# 2. База Биофака (Read-Only)
BIO_DB_URL = os.getenv("BIO_DB_URL", "")

# 3. База ФСК (Read-Only)
FSK_DB_URL = os.getenv("FSK_DB_URL", "")

# ==================== ВРЕМЯ И ТАЙМЗОНА ====================
MINSK_TZ = ZoneInfo("Europe/Minsk")

def get_minsk_now() -> datetime:
    return datetime.now(MINSK_TZ).replace(tzinfo=None)

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