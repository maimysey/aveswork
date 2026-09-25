from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from config import WORK_DB_URL, BIO_DB_URL, FSK_DB_URL, logger

# ==================== 1. ОСНОВНАЯ БАЗА AVESWORK (READ/WRITE) ====================
work_engine = create_async_engine(
    WORK_DB_URL,
    echo=False,
    connect_args={"ssl": "require"},
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
    connect_args={"ssl": "require"},
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
    connect_args={"ssl": "require"},
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


async def dispose_all_engines() -> None:
    logger.info("🛑 Закрытие сетевых пулов всех баз данных...")
    await work_engine.dispose()
    await bio_engine.dispose()
    await fsk_engine.dispose()
    logger.info("✅ Все пулы Supabase успешно закрыты")