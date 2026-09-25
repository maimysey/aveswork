from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from models import UserBusySlot

DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ==================== ГЛАВНОЕ МЕНЮ СТУДЕНТА ====================

def student_main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏳ Мои занятые часы"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="💼 Для работодателей"), KeyboardButton(text="ℹ️ О сервисе")],
        ],
        resize_keyboard=True,
    )


# ==================== УПРАВЛЕНИЕ ЗАНЯТЫМИ ЧАСАМИ ====================

def student_busy_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Заблокировать время", callback_data="add_busy_slot")],
            [InlineKeyboardButton(text="📋 Мой список ограничений", callback_data="list_busy_slots")],
        ]
    )


def busy_slots_list_kb(slots: list[UserBusySlot]) -> InlineKeyboardMarkup:
    buttons = []
    for slot in slots:
        day_str = DAYS_SHORT[slot.day_of_week]
        start_str = slot.start_time.strftime("%H:%M")
        end_str = slot.end_time.strftime("%H:%M")
        btn_label = f"❌ {day_str} {start_str}–{end_str} ({slot.title})"
        buttons.append([
            InlineKeyboardButton(text=btn_label, callback_data=f"del_busy_{slot.id}")
        ])

    buttons.append([
        InlineKeyboardButton(text="➕ Добавить еще", callback_data="add_busy_slot")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ==================== НАСТРОЙКИ СОИСКАТЕЛЯ ====================

def student_settings_kb(is_active: bool, notifications_enabled: bool) -> InlineKeyboardMarkup:
    search_label = "🔍 Поиск работы: ВКЛ 🟢" if is_active else "🔍 Поиск работы: ПАУЗА 🔴"
    notif_label = "🔔 Уведомления: ВКЛ 🟢" if notifications_enabled else "🔔 Уведомления: ВЫКЛ 🔴"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=search_label, callback_data="toggle_search_status")],
            [InlineKeyboardButton(text=notif_label, callback_data="toggle_notif_status")],
            [InlineKeyboardButton(text="⏳ Настроить занятые часы", callback_data="add_busy_slot")],
        ]
    )


# ==================== РАБОТОДАТЕЛЬ: ПРЕВЬЮ СМЕНЫ ====================

def employer_preview_kb(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Запустить рассылку кандидатам", callback_data=f"send_vac_{vacancy_id}")],
            [InlineKeyboardButton(text="🗑 Отменить смену", callback_data=f"cancel_vac_{vacancy_id}")],
        ]
    )


# ==================== КАРТОЧКА СМЕНЫ ДЛЯ СТУДЕНТА ====================

def student_vacancy_card_kb(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🙋‍♂️ Откликнуться на смену", callback_data=f"apply_{vacancy_id}")
        ]]
    )


def student_applied_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Вы откликнулись", callback_data="already_applied")
        ]]
    )


# ==================== СВЯЗЬ С ВЛАДЕЛЬЦЕМ ДЛЯ БИЗНЕСА ====================

def business_contact_kb(owner_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="💬 Написать владельцу (@" + owner_username + ")",
                url=f"https://t.me/{owner_username}",
            )
        ]]
    )