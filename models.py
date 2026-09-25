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
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ==================== БАЗОВЫЕ КЛАССЫ ====================

class WorkBase(DeclarativeBase):
    pass


class FacultyBase(DeclarativeBase):
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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
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
    status: Mapped[str] = mapped_column(String(20), default="sent", index=True)  # sent, applied
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    vacancy: Mapped["Vacancy"] = relationship(back_populates="deliveries")

    __table_args__ = (
        Index("idx_delivery_lookup", "vacancy_id", "user_id"),
    )


# ==================== ЗЕРКАЛА ФАКУЛЬТЕТСКИХ ТАБЛИЦ (READ-ONLY) ====================

class FacultyUser(FacultyBase):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subgroup: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)


class FacultyLesson(FacultyBase):
    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer)
    day: Mapped[int] = mapped_column(SmallInteger)
    slot_id: Mapped[int] = mapped_column(SmallInteger)
    subgroup: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)


class FacultyGroup(FacultyBase):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course: Mapped[int] = mapped_column(SmallInteger)
    number: Mapped[str] = mapped_column(String)