from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from config import WORK_DB_URL, BIO_DB_URL, FSK_DB_URL, DB_SSL, logger

_CONNECT_ARGS = {} if DB_SSL == "disable" else {"ssl": "require"}

# ==================== 1. ОСНОВНАЯ БАЗА AVESWORK (READ/WRITE) ====================
work_engine = create_async_engine(
    WORK_DB_URL,
    echo=False,
    connect_args=_CONNECT_ARGS,
    pool_size=5,
    max_overflow=10,
    pool_timeout=15,
    pool_pre_ping=True,
    pool_recycle=300,
)

work_session_maker = async_sessionmaker(
    work_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ==================== 2. БАЗА БИОФАКА (READ-ONLY) ====================
bio_engine = create_async_engine(
    BIO_DB_URL,
    echo=False,
    connect_args=_CONNECT_ARGS,
    pool_size=2,
    max_overflow=5,
    pool_timeout=15,
    pool_pre_ping=True,
    pool_recycle=300,
    execution_options={"isolation_level": "AUTOCOMMIT"},
)

bio_session_maker = async_sessionmaker(
    bio_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ==================== 3. БАЗА ФСК (READ-ONLY) ====================
fsk_engine = create_async_engine(
    FSK_DB_URL,
    echo=False,
    connect_args=_CONNECT_ARGS,
    pool_size=2,
    max_overflow=5,
    pool_timeout=15,
    pool_pre_ping=True,
    pool_recycle=300,
    execution_options={"isolation_level": "AUTOCOMMIT"},
)

fsk_session_maker = async_sessionmaker(
    fsk_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ==================== РЕЕСТР ФАКУЛЬТЕТСКИХ БАЗ (на будующее) ====================\
FACULTY_SESSIONS: dict[str, async_sessionmaker[AsyncSession]] = {
    "bio": bio_session_maker,
    "fsk": fsk_session_maker,
}


# ==================== МИГРАЦИИ ====================
# create_all создаёт только новые таблицы. Колонки в старых таблицах добавляем здесь —
# каждая команда идемпотентна, повторный запуск ничего не ломает.
_MIGRATIONS = [
    "ALTER TABLE work_users ADD COLUMN IF NOT EXISTS bot_started BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS title VARCHAR(120)",
    "ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS category VARCHAR(20) NOT NULL DEFAULT 'other'",
    # Старые смены уже разосланы — считаем их опубликованными
    "ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'active'",
    "ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS batch_key VARCHAR(32)",
    "ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS require_all_days BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS published_at TIMESTAMP",
    "CREATE INDEX IF NOT EXISTS ix_vacancies_status ON vacancies (status)",
    "CREATE INDEX IF NOT EXISTS ix_vacancies_batch_key ON vacancies (batch_key)",
]


async def migrate_work_schema(base_metadata) -> None:
    async with work_engine.begin() as conn:
        await conn.run_sync(base_metadata.create_all)
        for sql in _MIGRATIONS:
            await conn.execute(text(sql))
    logger.info("✅ Схема AvesWork актуальна (таблицы и новые колонки)")


async def dispose_all_engines() -> None:
    logger.info("🛑 Закрытие сетевых пулов всех баз данных...")
    await work_engine.dispose()
    await bio_engine.dispose()
    await fsk_engine.dispose()
    logger.info("✅ Все пулы Supabase успешно закрыты")
