#!/usr/bin/env python3
import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import Config, validate_config
from database import Database
from ssh_manager import SSHManager, ServerMetrics
from keyboards import *

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Инициализация
validate_config()
bot = Bot(token=Config.BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# Компоненты
db = Database(Config.DB_PATH)
ssh = SSHManager()
metrics_collector = ServerMetrics(ssh)
scheduler = AsyncIOScheduler()


# FSM States
class AddServerStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_host = State()
    waiting_for_port = State()
    waiting_for_username = State()
    waiting_for_auth_type = State()
    waiting_for_password = State()
    waiting_for_key_path = State()


class ExecuteCommandState(StatesGroup):
    waiting_for_command = State()


# Проверка доступа
def is_admin(user_id: int) -> bool:
    return user_id in Config.ADMIN_IDS


# === HANDLERS ===

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Команда /start"""
    await state.clear()
    
    # Регистрируем пользователя
    await db.add_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or "",
        is_admin=is_admin(message.from_user.id)
    )
    
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этому боту")
        return
        
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "🖥 Я помогу тебе мониторить серверы через SSH\n\n"
        "Используй меню ниже для управления:",
        reply_markup=main_menu()
    )


@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    """Вернуться в главное меню"""
    await state.clear()
    await callback.message.edit_text(
        "🏠 Главное меню:",
        reply_markup=main_menu()
    )


@router.callback_query(F.data == "servers_list")
async def show_servers(callback: CallbackQuery):
    """Показать список серверов"""
    servers = await db.get_servers(enabled_only=False)
    
    if not servers:
        await callback.message.edit_text(
            "📭 Нет добавленных серверов\n\n"
            "Используйте кнопку 'Добавить' для добавления сервера",
            reply_markup=main_menu()
        )
        return
        
    text = "🖥 <b>Ваши серверы:</b>\n\n"
    
    for server in servers:
        # Получаем последние метрики
        metrics = await db.get_latest_metrics(server['id'])
        status_emoji = "🟢"
        
        if metrics:
            if metrics['status'] == 'warning':
                status_emoji = "🟡"
            elif metrics['status'] == 'error':
                status_emoji = "🔴"
                
        text += f"{status_emoji} <b>{server['name']}</b>\n"
        text += f"   📍 {server['host']}:{server['port']}\n"
        
        if metrics:
            text += f"   💻 CPU: {metrics['cpu_usage']:.1f}% | "
            text += f"💾 RAM: {metrics['mem_usage']:.1f}%\n"
        
        text += "\n"
        
    await callback.message.edit_text(
        text,
        reply_markup=servers_list_kb(servers),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("server_"))
async def show_server_details(callback: CallbackQuery):
    """Показать детали сервера"""
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    
    if not server:
        await callback.answer("Сервер не найден")
        return
        
    metrics = await db.get_latest_metrics(server_id)
    
    text = f"🖥 <b>{server['name']}</b>\n\n"
    text += f"📍 Адрес: <code>{server['host']}:{server['port']}</code>\n"
    text += f"👤 Пользователь: <code>{server['username']}</code>\n"
    text += f"🔐 Аутентификация: {server['auth_type']}\n\n"
    
    if metrics:
        uptime_days = metrics['uptime'] // 86400
        uptime_hours = (metrics['uptime'] % 86400) // 3600
        
        text += "📊 <b>Последние метрики:</b>\n"
        text += f"💻 CPU: {metrics['cpu_usage']:.1f}%\n"
        text += f"💾 RAM: {metrics['mem_usage']:.1f}%\n"
        text += f"💿 Диск: {metrics['disk_usage']:.1f}%\n"
        text += f"📈 Load: {metrics['load_avg']}\n"
        text += f"⏱ Uptime: {uptime_days}д {uptime_hours}ч\n"
        text += f"🕐 Обновлено: {metrics['timestamp'][:19]}\n"
    else:
        text += "⚠️ Метрики еще не собраны"
        
    await callback.message.edit_text(
        text,
        reply_markup=server_actions_kb(server_id),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("metrics_"))
async def refresh_metrics(callback: CallbackQuery):
    """Обновить метрики сервера"""
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    
    if not server:
        await callback.answer("Сервер не найден")
        return
        
    await callback.answer("🔄 Получаю метрики...")
    
    metrics = await metrics_collector.get_all_metrics(server)
    
    if not metrics:
        await callback.message.answer("❌ Не удалось получить метрики")
        return
        
    # Сохраняем
    await db.save_metrics(
        server_id,
        metrics['cpu_usage'],
        metrics['mem_usage'],
        metrics['disk_usage'],
        metrics['load_avg'],
        metrics['uptime'],
        metrics['status']
    )
    
    # Показываем обновленные данные
    await show_server_details(callback)


@router.callback_query(F.data.startswith("info_"))
async def show_system_info(callback: CallbackQuery):
    """Показать системную информацию"""
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    
    if not server:
        await callback.answer("Сервер не найден")
        return
        
    await callback.answer("⏳ Получаю информацию...")
    
    info = await metrics_collector.get_system_info(server)
    
    text = f"ℹ️ <b>Информация о {server['name']}</b>\n\n"
    text += f"<code>{info}</code>"
    
    await callback.message.answer(
        text,
        parse_mode="HTML",
        reply_markup=server_actions_kb(server_id)
    )


@router.callback_query(F.data.startswith("top_"))
async def show_top_processes(callback: CallbackQuery):
    """Показать топ процессов"""
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    
    if not server:
        await callback.answer("Сервер не найден")
        return
        
    await callback.answer("⏳ Получаю процессы...")
    
    processes = await metrics_collector.get_top_processes(server, limit=10)
    
    text = f"📊 <b>Топ процессов на {server['name']}</b>\n\n"
    text += f"<code>{processes}</code>"
    
    await callback.message.answer(
        text,
        parse_mode="HTML",
        reply_markup=server_actions_kb(server_id)
    )


@router.callback_query(F.data.startswith("exec_"))
async def start_execute_command(callback: CallbackQuery, state: FSMContext):
    """Начать выполнение команды"""
    server_id = int(callback.data.split("_")[1])
    
    await state.update_data(server_id=server_id)
    await state.set_state(ExecuteCommandState.waiting_for_command)
    
    await callback.message.answer(
        "💻 Введите команду для выполнения:\n\n"
        "Например: <code>df -h</code> или <code>free -m</code>\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="HTML"
    )


@router.message(ExecuteCommandState.waiting_for_command)
async def execute_command(message: Message, state: FSMContext):
    """Выполнить команду"""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено", reply_markup=main_menu())
        return
        
    data = await state.get_data()
    server_id = data['server_id']
    server = await db.get_server(server_id)
    
    await message.answer("⏳ Выполняю команду...")
    
    stdout, stderr, code = await ssh.execute(server, message.text)
    
    result = f"💻 <b>Команда:</b> <code>{message.text}</code>\n"
    result += f"🖥 <b>Сервер:</b> {server['name']}\n"
    result += f"📤 <b>Код выхода:</b> {code}\n\n"
    
    if stdout:
        result += f"<b>Вывод:</b>\n<code>{stdout[:3000]}</code>\n\n"
    
    if stderr:
        result += f"<b>Ошибки:</b>\n<code>{stderr[:1000]}</code>"
        
    await message.answer(
        result,
        parse_mode="HTML",
        reply_markup=server_actions_kb(server_id)
    )
    
    await state.clear()


@router.callback_query(F.data.startswith("delete_"))
async def delete_server(callback: CallbackQuery):
    """Удалить сервер"""
    parts = callback.data.split("_")
    
    if len(parts) == 3 and parts[1] == "confirm":
        # Подтверждено - удаляем
        server_id = int(parts[2])
        await db.delete_server(server_id)
        await callback.answer("✅ Сервер удален")
        await show_servers(callback)
    else:
        # Запрос подтверждения
        server_id = int(parts[1])
        server = await db.get_server(server_id)
        
        await callback.message.edit_text(
            f"⚠️ Вы уверены, что хотите удалить сервер <b>{server['name']}</b>?\n\n"
            "Это действие нельзя отменить!",
            reply_markup=confirm_delete_kb(server_id),
            parse_mode="HTML"
        )


# === ДОБАВЛЕНИЕ СЕРВЕРА ===

@router.callback_query(F.data == "server_add")
async def start_add_server(callback: CallbackQuery, state: FSMContext):
    """Начать добавление сервера"""
    await state.set_state(AddServerStates.waiting_for_name)
    await callback.message.edit_text(
        "➕ <b>Добавление нового сервера</b>\n\n"
        "Шаг 1/5: Введите имя сервера\n"
        "Например: <code>production</code> или <code>my-vps</code>\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="HTML"
    )


@router.message(AddServerStates.waiting_for_name)
async def add_server_name(message: Message, state: FSMContext):
    """Получить имя сервера"""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление отменено", reply_markup=main_menu())
        return
        
    await state.update_data(name=message.text)
    await state.set_state(AddServerStates.waiting_for_host)
    
    await message.answer(
        "Шаг 2/5: Введите IP адрес или домен\n"
        "Например: <code>94.156.131.47</code> или <code>example.com</code>",
        parse_mode="HTML"
    )


@router.message(AddServerStates.waiting_for_host)
async def add_server_host(message: Message, state: FSMContext):
    """Получить хост сервера"""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление отменено", reply_markup=main_menu())
        return
        
    await state.update_data(host=message.text)
    await state.set_state(AddServerStates.waiting_for_port)
    
    await message.answer(
        "Шаг 3/5: Введите порт SSH\n"
        "Обычно это <code>22</code>. Просто отправьте 22 или другой порт:",
        parse_mode="HTML"
    )


@router.message(AddServerStates.waiting_for_port)
async def add_server_port(message: Message, state: FSMContext):
    """Получить порт"""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление отменено", reply_markup=main_menu())
        return
        
    try:
        port = int(message.text)
        await state.update_data(port=port)
        await state.set_state(AddServerStates.waiting_for_username)
        
        await message.answer(
            "Шаг 4/5: Введите имя пользователя\n"
            "Например: <code>root</code> или <code>admin</code>",
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer("❌ Введите корректный номер порта (число)")


@router.message(AddServerStates.waiting_for_username)
async def add_server_username(message: Message, state: FSMContext):
    """Получить username"""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление отменено", reply_markup=main_menu())
        return
        
    await state.update_data(username=message.text)
    
    await message.answer(
        "Шаг 5/5: Выберите тип аутентификации:",
        reply_markup=auth_type_kb()
    )


@router.callback_query(F.data == "auth_password", AddServerStates.waiting_for_username)
async def choose_password_auth(callback: CallbackQuery, state: FSMContext):
    """Выбрана аутентификация по паролю"""
    await state.update_data(auth_type='password')
    await state.set_state(AddServerStates.waiting_for_password)
    
    await callback.message.edit_text(
        "🔑 Введите пароль для подключения:\n\n"
        "⚠️ Пароль будет сохранен в зашифрованном виде\n\n"
        "Для отмены отправьте /cancel"
    )


@router.message(AddServerStates.waiting_for_password)
async def add_server_password(message: Message, state: FSMContext):
    """Получить пароль"""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление отменено", reply_markup=main_menu())
        return
        
    data = await state.get_data()
    
    # Удаляем сообщение с паролем
    await message.delete()
    
    # Тестируем подключение
    test_msg = await message.answer("⏳ Проверяю подключение...")
    
    test_server = {
        'name': data['name'],
        'host': data['host'],
        'port': data['port'],
        'username': data['username'],
        'auth_type': 'password',
        'password': message.text,
        'key_path': None
    }
    
    success, msg = await ssh.test_connection(test_server)
    
    if not success:
        await test_msg.edit_text(
            f"❌ Не удалось подключиться:\n{msg}\n\n"
            "Проверьте данные и попробуйте снова",
            reply_markup=main_menu()
        )
        await state.clear()
        return
        
    # Сохраняем сервер
    server_id = await db.add_server(
        name=data['name'],
        host=data['host'],
        username=data['username'],
        auth_type='password',
        port=data['port'],
        password=message.text
    )
    
    await test_msg.edit_text(
        f"✅ Сервер <b>{data['name']}</b> успешно добавлен!\n\n"
        "Сейчас соберу первые метрики...",
        parse_mode="HTML"
    )
    
    # Собираем первые метрики
    metrics = await metrics_collector.get_all_metrics(test_server)
    if metrics:
        await db.save_metrics(
            server_id,
            metrics['cpu_usage'],
            metrics['mem_usage'],
            metrics['disk_usage'],
            metrics['load_avg'],
            metrics['uptime'],
            metrics['status']
        )
    
    await state.clear()
    await message.answer(
        "🎉 Готово! Сервер добавлен и мониторится",
        reply_markup=main_menu()
    )


@router.callback_query(F.data == "auth_key", AddServerStates.waiting_for_username)
async def choose_key_auth(callback: CallbackQuery, state: FSMContext):
    """Выбрана аутентификация по ключу"""
    await state.update_data(auth_type='key')
    await state.set_state(AddServerStates.waiting_for_key_path)
    
    await callback.message.edit_text(
        "🔐 Введите путь к SSH ключу:\n\n"
        "Например: <code>~/.ssh/id_rsa</code>\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="HTML"
    )


@router.message(AddServerStates.waiting_for_key_path)
async def add_server_key_path(message: Message, state: FSMContext):
    """Получить путь к ключу"""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление отменено", reply_markup=main_menu())
        return
        
    data = await state.get_data()
    
    test_msg = await message.answer("⏳ Проверяю подключение...")
    
    test_server = {
        'name': data['name'],
        'host': data['host'],
        'port': data['port'],
        'username': data['username'],
        'auth_type': 'key',
        'password': None,
        'key_path': message.text
    }
    
    success, msg = await ssh.test_connection(test_server)
    
    if not success:
        await test_msg.edit_text(
            f"❌ Не удалось подключиться:\n{msg}\n\n"
            "Проверьте путь к ключу и попробуйте снова",
            reply_markup=main_menu()
        )
        await state.clear()
        return
        
    # Сохраняем сервер
    server_id = await db.add_server(
        name=data['name'],
        host=data['host'],
        username=data['username'],
        auth_type='key',
        port=data['port'],
        key_path=message.text
    )
    
    await test_msg.edit_text(
        f"✅ Сервер <b>{data['name']}</b> успешно добавлен!",
        parse_mode="HTML",
        reply_markup=main_menu()
    )
    
    await state.clear()


@router.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    """Показать общую статистику"""
    servers = await db.get_servers()
    
    total_servers = len(servers)
    healthy = 0
    warning = 0
    offline = 0
    
    for server in servers:
        metrics = await db.get_latest_metrics(server['id'])
        if not metrics:
            offline += 1
        elif metrics['status'] == 'healthy':
            healthy += 1
        else:
            warning += 1
            
    text = "📈 <b>Общая статистика</b>\n\n"
    text += f"🖥 Всего серверов: {total_servers}\n"
    text += f"🟢 Здоровых: {healthy}\n"
    text += f"🟡 Предупреждений: {warning}\n"
    text += f"🔴 Недоступных: {offline}\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=main_menu(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "alerts")
async def show_alerts(callback: CallbackQuery):
    """Показать алерты"""
    alerts = await db.get_unsent_alerts()
    
    if not alerts:
        await callback.message.edit_text(
            "✅ Нет активных алертов",
            reply_markup=main_menu()
        )
        return
        
    text = "🔔 <b>Активные алерты:</b>\n\n"
    
    for alert in alerts[:10]:
        emoji = "⚠️" if alert['level'] == 'warning' else "🚨"
        text += f"{emoji} <b>{alert['server_name']}</b>\n"
        text += f"   {alert['message']}\n"
        text += f"   🕐 {alert['created_at'][:19]}\n\n"
        
    await callback.message.edit_text(
        text,
        reply_markup=main_menu(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "help")
async def show_help(callback: CallbackQuery):
    """Показать помощь"""
    text = """
❓ <b>Справка</b>

<b>Основные функции:</b>
📊 Серверы - список всех серверов
➕ Добавить - добавить новый сервер
📈 Статистика - общая статистика
🔔 Алерты - активные предупреждения

<b>Действия с сервером:</b>
📊 Метрики - текущие метрики
💻 Команда - выполнить команду
ℹ️ Инфо - системная информация
📈 Топ - топ процессов
🔄 Обновить - обновить данные
❌ Удалить - удалить сервер

<b>Автоматический мониторинг:</b>
Бот автоматически проверяет серверы каждую минуту и отправляет алерты при проблемах.

<b>Пороги алертов:</b>
⚠️ Warning: CPU > 80%, RAM > 85%, Disk > 85%
🚨 Critical: CPU > 95%, RAM > 95%, Disk > 95%
"""
    
    await callback.message.edit_text(
        text,
        reply_markup=main_menu(),
        parse_mode="HTML"
    )


# === МОНИТОРИНГ В ФОНЕ ===

async def monitor_servers():
    """Фоновый мониторинг серверов"""
    logger.info("Running scheduled monitoring...")
    
    servers = await db.get_servers()
    
    for server in servers:
        try:
            metrics = await metrics_collector.get_all_metrics(server)
            
            if not metrics:
                # Сервер недоступен
                await db.add_alert(
                    server['id'],
                    'critical',
                    f"Сервер {server['name']} недоступен!"
                )
                continue
                
            # Сохраняем метрики
            await db.save_metrics(
                server['id'],
                metrics['cpu_usage'],
                metrics['mem_usage'],
                metrics['disk_usage'],
                metrics['load_avg'],
                metrics['uptime'],
                metrics['status']
            )
            
            # Проверяем пороги
            if metrics['cpu_usage'] > Config.CPU_CRITICAL:
                await db.add_alert(
                    server['id'],
                    'critical',
                    f"🚨 CPU: {metrics['cpu_usage']:.1f}% (критично!)"
                )
            elif metrics['cpu_usage'] > Config.CPU_WARNING:
                await db.add_alert(
                    server['id'],
                    'warning',
                    f"⚠️ CPU: {metrics['cpu_usage']:.1f}% (высокая нагрузка)"
                )
                
            if metrics['mem_usage'] > Config.MEM_CRITICAL:
                await db.add_alert(
                    server['id'],
                    'critical',
                    f"🚨 RAM: {metrics['mem_usage']:.1f}% (критично!)"
                )
            elif metrics['mem_usage'] > Config.MEM_WARNING:
                await db.add_alert(
                    server['id'],
                    'warning',
                    f"⚠️ RAM: {metrics['mem_usage']:.1f}% (высокое использование)"
                )
                
            if metrics['disk_usage'] > Config.DISK_CRITICAL:
                await db.add_alert(
                    server['id'],
                    'critical',
                    f"🚨 Диск: {metrics['disk_usage']:.1f}% (почти заполнен!)"
                )
            elif metrics['disk_usage'] > Config.DISK_WARNING:
                await db.add_alert(
                    server['id'],
                    'warning',
                    f"⚠️ Диск: {metrics['disk_usage']:.1f}% (заканчивается место)"
                )
                
        except Exception as e:
            logger.error(f"Error monitoring {server['name']}: {e}")
            
    # Отправляем неотправленные алерты
    await send_pending_alerts()


async def send_pending_alerts():
    """Отправить накопившиеся алерты"""
    alerts = await db.get_unsent_alerts()
    
    for alert in alerts:
        emoji = "⚠️" if alert['level'] == 'warning' else "🚨"
        
        text = f"{emoji} <b>Алерт: {alert['server_name']}</b>\n\n"
        text += f"{alert['message']}\n"
        text += f"🕐 {alert['created_at'][:19]}"
        
        for admin_id in Config.ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    text,
                    parse_mode="HTML"
                )
                await db.mark_alert_sent(alert['id'])
            except Exception as e:
                logger.error(f"Failed to send alert to {admin_id}: {e}")


async def on_startup():
    """При запуске бота"""
    logger.info("Bot starting...")
    
    # Инициализация БД
    await db.init()
    
    # Добавляем админов
    for admin_id in Config.ADMIN_IDS:
        await db.add_user(admin_id, "", "Admin", is_admin=True)
    
    # Запускаем планировщик
    scheduler.add_job(
        monitor_servers,
        'interval',
        seconds=Config.CHECK_INTERVAL,
        id='monitor'
    )
    scheduler.start()
    
    logger.info("Bot started successfully!")


async def on_shutdown():
    """При остановке бота"""
    logger.info("Bot shutting down...")
    scheduler.shutdown()


async def main():
    """Главная функция"""
    try:
        await on_startup()
        await dp.start_polling(bot)
    finally:
        await on_shutdown()


if __name__ == '__main__':
    asyncio.run(main())