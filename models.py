from datetime import datetime, date, time
from sqlalchemy import (
    BigInteger,
    String,
    Boolean,
    Integer,
    SmallInteger,
    ForeignKey,
    Time,
    Date,
    Text,
    func,
    Index,
    UniqueConstraint,
    JSON,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import get_minsk_now


# ==================== БАЗОВЫЕ КЛАССЫ ====================

class WorkBase(DeclarativeBase):
    pass


class FacultyBase(DeclarativeBase):
    pass


class FmoBase(DeclarativeBase):
    """Зеркала базы ФМО (AvesGlobe): у пар точное время, языковые подгруппы и ДВС."""
    pass


# ==================== МОДЕЛИ AVESWORK (WRITE/READ) ====================

class Employer(WorkBase):
    __tablename__ = "employers"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(150), index=True)
    contact_name: Mapped[str | None] = mapped_column(String(100), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    vacancies: Mapped[list["Vacancy"]] = relationship(
        back_populates="employer", cascade="all, delete-orphan"
    )


class WorkUser(WorkBase):
    __tablename__ = "work_users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # is_active — ищет подработку; False = пауза: ни карточек, ни коротких уведомлений
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # Полные карточки смен в самом AvesWork
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # Нажимал «Старт» в AvesWork. Профиль, созданный с сайта, — False: писать ему бот не может
    bot_started: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    registered_at: Mapped[datetime] = mapped_column(server_default=func.now())

    busy_slots: Mapped[list["UserBusySlot"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserBusySlot(WorkBase):
    __tablename__ = "user_busy_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("work_users.telegram_id", ondelete="CASCADE"), index=True
    )
    day_of_week: Mapped[int] = mapped_column(SmallInteger, index=True)  # 0 = Пн, 6 = Вс
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    title: Mapped[str] = mapped_column(String(100), default="Личные дела")

    user: Mapped["WorkUser"] = relationship(back_populates="busy_slots")

    __table_args__ = (
        Index("idx_user_busy_lookup", "user_id", "day_of_week"),
    )


class Vacancy(WorkBase):
    """Одна смена = один день. Многодневная публикация — несколько строк с общим batch_key."""
    __tablename__ = "vacancies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employer_id: Mapped[int] = mapped_column(
        ForeignKey("employers.telegram_id", ondelete="CASCADE"), index=True
    )
    raw_text: Mapped[str] = mapped_column(Text)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    day_of_week: Mapped[int] = mapped_column(SmallInteger, index=True)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    pay_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pay_unit: Mapped[str] = mapped_column(String(20), default="BYN")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Добавлено для ленты на сайте (старые строки получают значения по умолчанию)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True, default=None)
    category: Mapped[str] = mapped_column(String(20), default="other", server_default="other")
    # draft — превью у работодателя, active — опубликована, closed — снята
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="active", index=True)
    batch_key: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None, index=True)
    require_all_days: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    published_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)

    employer: Mapped["Employer"] = relationship(back_populates="vacancies")
    deliveries: Mapped[list["VacancyDelivery"]] = relationship(
        back_populates="vacancy", cascade="all, delete-orphan"
    )


class VacancyDelivery(WorkBase):
    __tablename__ = "vacancy_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vacancy_id: Mapped[int] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # sent — карточка в AvesWork, relay — короткое уведомление в боте факультета,
    # applied — отклик, withdrawn — отклик отозван
    status: Mapped[str] = mapped_column(String(20), default="sent", index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    vacancy: Mapped["Vacancy"] = relationship(back_populates="deliveries")

    __table_args__ = (
        Index("idx_delivery_lookup", "vacancy_id", "user_id"),
    )


class WorkPref(WorkBase):
    """Настройки студента, которому необязательно запускать AvesWork (пишет сайт и кнопка в боте факультета)."""
    __tablename__ = "work_prefs"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    relay_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Человек в нескольких ботах расписания: какие расписания НЕ учитывать (через запятую: "fmo")
    schedules_off: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # Через какого бота присылать короткие уведомления (bio / fsk / fmo); пусто — первый по порядку
    relay_faculty: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class RelayOutbox(WorkBase):
    """Очередь коротких уведомлений через ботов факультетов."""
    __tablename__ = "work_relay_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    faculty: Mapped[str] = mapped_column(String(16))
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"), index=True)
    # pending → sent | blocked | skipped | failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0)
    # Все времена очереди — минское «наивное» время, как get_minsk_now()
    not_before: Mapped[datetime] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(default=get_minsk_now, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True, default=None)

    __table_args__ = (
        UniqueConstraint("user_id", "vacancy_id", name="uq_relay_user_vacancy"),
    )


# ==================== ЗЕРКАЛА ФАКУЛЬТЕТСКИХ ТАБЛИЦ (READ-ONLY) ====================

class FacultyUser(FacultyBase):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subgroup: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    notifications_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class FacultyWeek(FacultyBase):
    __tablename__ = "weeks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_date: Mapped[date] = mapped_column(Date)
    course: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)


class FacultyLesson(FacultyBase):
    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer)
    week_id: Mapped[int] = mapped_column(Integer)
    day: Mapped[int] = mapped_column(SmallInteger)
    slot_id: Mapped[int] = mapped_column(SmallInteger)
    subgroup: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)


class FacultyGroup(FacultyBase):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course: Mapped[int] = mapped_column(SmallInteger)
    number: Mapped[str] = mapped_column(String)
    study_mode: Mapped[str | None] = mapped_column(String, nullable=True)


class FmoUser(FmoBase):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tracks: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    choices: Mapped[list | None] = mapped_column(JSON, nullable=True)
    notifications_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class FmoLesson(FmoBase):
    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer)
    week_id: Mapped[int] = mapped_column(Integer)
    day: Mapped[int] = mapped_column(SmallInteger)
    time_start: Mapped[str] = mapped_column(String(5))
    time_end: Mapped[str] = mapped_column(String(5))
    track: Mapped[str | None] = mapped_column(String(16), nullable=True)
    label_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    choice_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
