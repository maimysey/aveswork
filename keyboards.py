from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from models import UserBusySlot, Vacancy

DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ==================== КЛАВИАТУРЫ ГЛАВНОГО МЕНЮ ====================

def student_main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏳ Мои занятые часы"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="💼 Для работодателей"), KeyboardButton(text="ℹ️ О сервисе")],
        ],
        resize_keyboard=True,
    )


def employer_main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Инструкция к публикации"), KeyboardButton(text="📊 Мои публикации")],
            [KeyboardButton(text="ℹ️ О сервисе"), KeyboardButton(text="💬 Связь с поддержкой")],
        ],
        resize_keyboard=True,
    )


# ==================== УПРАВЛЕНИЕ ЗАНЯТЫМИ ЧАСАМИ СТУДЕНТА ====================

def student_busy_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Заблокировать время", callback_data="add_busy_slot")],
            [InlineKeyboardButton(text="📋 Мой список ограничений", callback_data="list_busy_slots")],
        ]
    )


def cancel_busy_input_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отменить ввод", callback_data="cancel_busy_fsm")
        ]]
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


# ==================== РАБОТОДАТЕЛЬ: ПРЕВЬЮ ПРИ СОЗДАНИИ ====================

def employer_preview_kb(
    vacancy_ids: list[int],
    require_all_days: bool = False,
    is_multi_day: bool = False,
) -> InlineKeyboardMarkup:
    ids_str = ",".join(map(str, vacancy_ids))
    keyboard = []

    if is_multi_day:
        if require_all_days:
            mode_text = "🎯 Режим: Нужен на ВСЕ дни сразу"
            next_val = 0
        else:
            mode_text = "🔀 Режим: Можно по отдельным дням"
            next_val = 1

        keyboard.append([
            InlineKeyboardButton(
                text=mode_text,
                callback_data=f"toggle_mode_{ids_str}_{next_val}"
            )
        ])

    mode_int = 1 if require_all_days else 0
    keyboard.extend([
        [InlineKeyboardButton(text="🚀 Запустить рассылку кандидатам", callback_data=f"send_vac_{ids_str}_{mode_int}")],
        [InlineKeyboardButton(text="🗑 Отменить публикацию", callback_data=f"cancel_vac_{ids_str}")],
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


# ==================== РАБОТОДАТЕЛЬ: УПРАВЛЕНИЕ И УДАЛЕНИЕ СМЕН ====================

def employer_vacancies_list_kb(vacancies_data: list[tuple[Vacancy, int]]) -> InlineKeyboardMarkup:
    buttons = []
    for vac, app_count in vacancies_data:
        day_str = DAYS_SHORT[vac.day_of_week]
        date_str = vac.target_date.strftime("%d.%m")
        start_str = vac.start_time.strftime("%H:%M")
        end_str = vac.end_time.strftime("%H:%M")
        label = f"📅 {day_str} {date_str} ({start_str}–{end_str}) • 🙋‍♂️ {app_count}"
        buttons.append([
            InlineKeyboardButton(text=label, callback_data=f"manage_vac_{vac.id}")
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def employer_vacancy_manage_kb(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Снять с публикации", callback_data=f"delete_vac_{vacancy_id}")],
            [InlineKeyboardButton(text="⬅️ Назад к списку смен", callback_data="back_to_my_vacs")],
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


# ==================== СВЯЗЬ С ВЛАДЕЛЬЦЕМ ====================

def business_contact_kb(owner_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="💬 Написать владельцу (@" + owner_username + ")",
                url=f"https://t.me/{owner_username}",
            )
        ]]
    )