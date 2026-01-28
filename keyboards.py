from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu() -> InlineKeyboardMarkup:
    """Главное меню"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Серверы", callback_data="servers_list"),
        InlineKeyboardButton(text="➕ Добавить", callback_data="server_add")
    )
    builder.row(
        InlineKeyboardButton(text="📈 Статистика", callback_data="stats"),
        InlineKeyboardButton(text="🔔 Алерты", callback_data="alerts")
    )
    builder.row(
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
        InlineKeyboardButton(text="❓ Помощь", callback_data="help")
    )
    return builder.as_markup()


def servers_list_kb(servers: list) -> InlineKeyboardMarkup:
    """Список серверов"""
    builder = InlineKeyboardBuilder()
    
    for server in servers:
        status_emoji = "🟢" if server.get('enabled') else "🔴"
        builder.row(
            InlineKeyboardButton(
                text=f"{status_emoji} {server['name']}",
                callback_data=f"server_{server['id']}"
            )
        )
        
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")
    )
    return builder.as_markup()


def server_actions_kb(server_id: int) -> InlineKeyboardMarkup:
    """Действия с сервером"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Метрики", callback_data=f"metrics_{server_id}"),
        InlineKeyboardButton(text="💻 Команда", callback_data=f"exec_{server_id}")
    )
    builder.row(
        InlineKeyboardButton(text="ℹ️ Инфо", callback_data=f"info_{server_id}"),
        InlineKeyboardButton(text="📈 Топ", callback_data=f"top_{server_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_{server_id}"),
        InlineKeyboardButton(text="❌ Удалить", callback_data=f"delete_{server_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 К списку", callback_data="servers_list")
    )
    return builder.as_markup()


def confirm_delete_kb(server_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"delete_confirm_{server_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"server_{server_id}")
    )
    return builder.as_markup()


def auth_type_kb() -> InlineKeyboardMarkup:
    """Выбор типа аутентификации"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔑 Пароль", callback_data="auth_password"),
        InlineKeyboardButton(text="🔐 SSH ключ", callback_data="auth_key")
    )
    builder.row(
        InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main")
    )
    return builder.as_markup()