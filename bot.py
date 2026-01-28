#!/usr/bin/env python3
"""
SSH Server Monitoring Bot for Bothost
Полный мониторинг серверов через SSH с Telegram интерфейсом
"""

import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import asyncssh
import aiosqlite

# ============= КОНФИГУРАЦИЯ =============

class Config:
    BOT_TOKEN = os.getenv('BOT_TOKEN', '')
    ADMIN_IDS = list(filter(None, map(str.strip, os.getenv('ADMIN_IDS', '').split(','))))
    ADMIN_IDS = [int(x) for x in ADMIN_IDS if x.isdigit()]
    
    DB_PATH = '/app/data/agent.db'  # Bothost сохраняет /app/data
    CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '120'))
    
    CPU_WARNING = 80
    CPU_CRITICAL = 95
    MEM_WARNING = 85
    MEM_CRITICAL = 95
    DISK_WARNING = 85
    DISK_CRITICAL = 95

# ============= ЛОГИРОВАНИЕ =============

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============= БАЗА ДАННЫХ =============

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # Создаём директорию если не существует
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
    async def init(self):
        """Инициализация базы данных"""
        async with aiosqlite.connect(self.db_path) as db:
            # Таблица серверов
            await db.execute('''
                CREATE TABLE IF NOT EXISTS servers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER DEFAULT 22,
                    username TEXT NOT NULL,
                    password TEXT,
                    enabled INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица метрик
            await db.execute('''
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id INTEGER NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    cpu_usage REAL,
                    mem_usage REAL,
                    disk_usage REAL,
                    load_avg TEXT,
                    uptime INTEGER,
                    status TEXT,
                    FOREIGN KEY (server_id) REFERENCES servers(id)
                )
            ''')
            
            # Таблица алертов
            await db.execute('''
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    sent INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (server_id) REFERENCES servers(id)
                )
            ''')
            
            await db.commit()
            logger.info(f"Database initialized at {self.db_path}")
            
    async def add_server(self, name: str, host: str, port: int, username: str, password: str) -> int:
        """Добавить сервер"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'INSERT INTO servers (name, host, port, username, password) VALUES (?, ?, ?, ?, ?)',
                (name, host, port, username, password)
            )
            await db.commit()
            return cursor.lastrowid
            
    async def get_servers(self, enabled_only: bool = True):
        """Получить все серверы"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = 'SELECT * FROM servers'
            if enabled_only:
                query += ' WHERE enabled = 1'
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
                
    async def get_server(self, server_id: int):
        """Получить сервер по ID"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('SELECT * FROM servers WHERE id = ?', (server_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
                
    async def delete_server(self, server_id: int):
        """Удалить сервер"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('DELETE FROM servers WHERE id = ?', (server_id,))
            await db.execute('DELETE FROM metrics WHERE server_id = ?', (server_id,))
            await db.execute('DELETE FROM alerts WHERE server_id = ?', (server_id,))
            await db.commit()
            
    async def save_metrics(self, server_id: int, cpu: float, mem: float, disk: float, 
                          load: str, uptime: int, status: str):
        """Сохранить метрики"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                '''INSERT INTO metrics (server_id, cpu_usage, mem_usage, disk_usage, 
                   load_avg, uptime, status) VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (server_id, cpu, mem, disk, load, uptime, status)
            )
            # Удаляем старые метрики (храним последние 1000)
            await db.execute(
                '''DELETE FROM metrics WHERE server_id = ? AND id NOT IN (
                   SELECT id FROM metrics WHERE server_id = ? 
                   ORDER BY timestamp DESC LIMIT 1000)''',
                (server_id, server_id)
            )
            await db.commit()
            
    async def get_latest_metrics(self, server_id: int):
        """Получить последние метрики"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM metrics WHERE server_id = ? ORDER BY timestamp DESC LIMIT 1',
                (server_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
                
    async def add_alert(self, server_id: int, level: str, message: str):
        """Добавить алерт"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'INSERT INTO alerts (server_id, level, message) VALUES (?, ?, ?)',
                (server_id, level, message)
            )
            await db.commit()
            
    async def get_unsent_alerts(self):
        """Получить неотправленные алерты"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                '''SELECT a.*, s.name as server_name FROM alerts a
                   JOIN servers s ON a.server_id = s.id
                   WHERE a.sent = 0 ORDER BY a.created_at ASC LIMIT 10'''
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
                
    async def mark_alert_sent(self, alert_id: int):
        """Отметить алерт отправленным"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE alerts SET sent = 1 WHERE id = ?', (alert_id,))
            await db.commit()

# ============= SSH МЕНЕДЖЕР =============

class SSHManager:
    async def execute(self, server: dict, command: str, timeout: int = 30):
        """Выполнить команду на сервере"""
        try:
            async with asyncssh.connect(
                server['host'],
                port=server['port'],
                username=server['username'],
                password=server['password'],
                known_hosts=None,
                connect_timeout=timeout
            ) as conn:
                result = await asyncio.wait_for(conn.run(command), timeout=timeout)
                return result.stdout or '', result.stderr or '', result.exit_status
        except asyncio.TimeoutError:
            return '', 'Timeout', -1
        except Exception as e:
            logger.error(f"SSH error for {server['name']}: {e}")
            return '', str(e), -1
            
    async def get_metrics(self, server: dict):
        """Получить метрики сервера"""
        try:
            # CPU
            cpu_cmd = "top -bn1 | grep 'Cpu(s)' | awk '{print $2}' | sed 's/%us,//'"
            cpu_out, _, _ = await self.execute(server, cpu_cmd)
            cpu_usage = float(cpu_out.strip() or 0)
            
            # Memory
            mem_cmd = "free | grep Mem | awk '{print ($3/$2) * 100.0}'"
            mem_out, _, _ = await self.execute(server, mem_cmd)
            mem_usage = float(mem_out.strip() or 0)
            
            # Disk
            disk_cmd = "df -h / | tail -1 | awk '{print $5}' | sed 's/%//'"
            disk_out, _, _ = await self.execute(server, disk_cmd)
            disk_usage = float(disk_out.strip() or 0)
            
            # Load
            load_cmd = "cat /proc/loadavg | cut -d' ' -f1-3"
            load_out, _, _ = await self.execute(server, load_cmd)
            load_avg = load_out.strip()
            
            # Uptime
            uptime_cmd = "cat /proc/uptime | cut -d' ' -f1"
            uptime_out, _, _ = await self.execute(server, uptime_cmd)
            uptime = int(float(uptime_out.strip() or 0))
            
            # Определяем статус
            if cpu_usage > 95 or mem_usage > 95 or disk_usage > 95:
                status = 'critical'
            elif cpu_usage > 80 or mem_usage > 85 or disk_usage > 85:
                status = 'warning'
            else:
                status = 'healthy'
                
            return {
                'cpu_usage': cpu_usage,
                'mem_usage': mem_usage,
                'disk_usage': disk_usage,
                'load_avg': load_avg,
                'uptime': uptime,
                'status': status
            }
        except Exception as e:
            logger.error(f"Failed to get metrics: {e}")
            return None

# ============= КЛАВИАТУРЫ =============

def main_kb():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Серверы", callback_data="list"),
        InlineKeyboardButton(text="➕ Добавить", callback_data="add")
    )
    builder.row(
        InlineKeyboardButton(text="📈 Статистика", callback_data="stats"),
        InlineKeyboardButton(text="🔔 Алерты", callback_data="alerts")
    )
    builder.row(InlineKeyboardButton(text="❓ Помощь", callback_data="help"))
    return builder.as_markup()

def servers_kb(servers: list):
    builder = InlineKeyboardBuilder()
    for s in servers:
        emoji = "🟢" if s['enabled'] else "🔴"
        builder.row(InlineKeyboardButton(
            text=f"{emoji} {s['name']}",
            callback_data=f"srv_{s['id']}"
        ))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu"))
    return builder.as_markup()

def server_kb(server_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Метрики", callback_data=f"met_{server_id}"),
        InlineKeyboardButton(text="💻 Команда", callback_data=f"cmd_{server_id}")
    )
    builder.row(
        InlineKeyboardButton(text="ℹ️ Инфо", callback_data=f"inf_{server_id}"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"ref_{server_id}")
    )
    builder.row(
        InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_{server_id}"),
        InlineKeyboardButton(text="🔙 К списку", callback_data="list")
    )
    return builder.as_markup()

def confirm_kb(server_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да", callback_data=f"confirm_{server_id}"),
        InlineKeyboardButton(text="❌ Нет", callback_data=f"srv_{server_id}")
    )
    return builder.as_markup()

# ============= FSM STATES =============

class AddServer(StatesGroup):
    name = State()
    host = State()
    port = State()
    username = State()
    password = State()

class ExecCommand(StatesGroup):
    waiting = State()

# ============= ИНИЦИАЛИЗАЦИЯ =============

bot = Bot(token=Config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

db = Database(Config.DB_PATH)
ssh = SSHManager()
scheduler = AsyncIOScheduler()

# ============= HANDLERS =============

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    
    if message.from_user.id not in Config.ADMIN_IDS:
        await message.answer("❌ Доступ запрещен")
        return
        
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "🖥 SSH Server Monitoring Agent\n\n"
        "Я помогу тебе мониторить твои серверы через SSH.\n"
        "Все данные сохраняются на Bothost!",
        reply_markup=main_kb()
    )

@router.callback_query(F.data == "menu")
async def show_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🏠 Главное меню:",
        reply_markup=main_kb()
    )
    await callback.answer()

@router.callback_query(F.data == "list")
async def show_servers(callback: CallbackQuery):
    servers = await db.get_servers(enabled_only=False)
    
    if not servers:
        await callback.message.edit_text(
            "📭 Нет серверов\n\nДобавьте сервер кнопкой ➕",
            reply_markup=main_kb()
        )
        await callback.answer()
        return
        
    text = "🖥 <b>Ваши серверы:</b>\n\n"
    for s in servers:
        metrics = await db.get_latest_metrics(s['id'])
        status = "🟢"
        if metrics:
            if metrics['status'] == 'warning':
                status = "🟡"
            elif metrics['status'] == 'critical':
                status = "🔴"
            text += f"{status} <b>{s['name']}</b> - {s['host']}\n"
            if metrics:
                text += f"   CPU: {metrics['cpu_usage']:.1f}% | RAM: {metrics['mem_usage']:.1f}%\n"
        else:
            text += f"⚪️ <b>{s['name']}</b> - {s['host']}\n   Метрики не собраны\n"
        text += "\n"
        
    await callback.message.edit_text(text, reply_markup=servers_kb(servers), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("srv_"))
async def show_server(callback: CallbackQuery):
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    
    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return
        
    metrics = await db.get_latest_metrics(server_id)
    
    text = f"🖥 <b>{server['name']}</b>\n\n"
    text += f"📍 {server['host']}:{server['port']}\n"
    text += f"👤 {server['username']}\n\n"
    
    if metrics:
        uptime_d = metrics['uptime'] // 86400
        uptime_h = (metrics['uptime'] % 86400) // 3600
        
        text += "📊 <b>Метрики:</b>\n"
        text += f"💻 CPU: {metrics['cpu_usage']:.1f}%\n"
        text += f"💾 RAM: {metrics['mem_usage']:.1f}%\n"
        text += f"💿 Диск: {metrics['disk_usage']:.1f}%\n"
        text += f"📈 Load: {metrics['load_avg']}\n"
        text += f"⏱ Uptime: {uptime_d}д {uptime_h}ч\n"
        text += f"🕐 {metrics['timestamp'][:19]}\n"
    else:
        text += "⚠️ Метрики пока не собраны"
        
    await callback.message.edit_text(text, reply_markup=server_kb(server_id), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("met_"))
async def refresh_metrics(callback: CallbackQuery):
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    
    await callback.answer("🔄 Обновляю метрики...")
    
    metrics = await ssh.get_metrics(server)
    
    if not metrics:
        await callback.answer("❌ Не удалось получить метрики", show_alert=True)
        return
        
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
    await show_server(callback)

@router.callback_query(F.data.startswith("cmd_"))
async def start_exec(callback: CallbackQuery, state: FSMContext):
    server_id = int(callback.data.split("_")[1])
    await state.update_data(server_id=server_id)
    await state.set_state(ExecCommand.waiting)
    
    await callback.message.answer(
        "💻 Введите команду:\n\n"
        "Например: <code>df -h</code>\n\n"
        "/cancel для отмены",
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(ExecCommand.waiting)
async def exec_command(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено", reply_markup=main_kb())
        return
        
    data = await state.get_data()
    server = await db.get_server(data['server_id'])
    
    msg = await message.answer("⏳ Выполняю...")
    
    stdout, stderr, code = await ssh.execute(server, message.text)
    
    result = f"💻 <code>{message.text}</code>\n"
    result += f"📤 Exit: {code}\n\n"
    if stdout:
        result += f"<pre>{stdout[:3000]}</pre>\n"
    if stderr:
        result += f"<b>Error:</b>\n<pre>{stderr[:1000]}</pre>"
        
    await msg.edit_text(result, parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data.startswith("inf_"))
async def show_info(callback: CallbackQuery):
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    
    await callback.answer("⏳ Получаю инфо...")
    
    commands = [
        ("hostname", "Hostname"),
        ("uname -a", "Kernel"),
        ("cat /etc/os-release | grep PRETTY_NAME | cut -d'\"' -f2", "OS"),
        ("nproc", "CPU Cores"),
    ]
    
    text = f"ℹ️ <b>Информация {server['name']}</b>\n\n"
    
    for cmd, label in commands:
        stdout, _, code = await ssh.execute(server, cmd)
        if code == 0:
            text += f"<b>{label}:</b> {stdout.strip()}\n"
            
    await callback.message.answer(text, parse_mode="HTML")

@router.callback_query(F.data.startswith("ref_"))
async def refresh_server(callback: CallbackQuery):
    # Обновляем метрики и показываем сервер
    server_id = int(callback.data.split("_")[1])
    await refresh_metrics(callback)
    
@router.callback_query(F.data.startswith("del_"))
async def delete_confirm(callback: CallbackQuery):
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    
    await callback.message.edit_text(
        f"⚠️ Удалить <b>{server['name']}</b>?\n\nЭто нельзя отменить!",
        reply_markup=confirm_kb(server_id),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("confirm_"))
async def delete_server(callback: CallbackQuery):
    server_id = int(callback.data.split("_")[1])
    await db.delete_server(server_id)
    await callback.answer("✅ Удалено", show_alert=True)
    await show_servers(callback)

# === ДОБАВЛЕНИЕ СЕРВЕРА ===

@router.callback_query(F.data == "add")
async def start_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddServer.name)
    await callback.message.edit_text(
        "➕ <b>Добавление сервера</b>\n\n"
        "Шаг 1/5: Введите имя сервера\n"
        "Например: <code>my-vps</code>\n\n"
        "/cancel для отмены",
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(AddServer.name)
async def add_name(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено", reply_markup=main_kb())
        return
        
    await state.update_data(name=message.text)
    await state.set_state(AddServer.host)
    await message.answer(
        "Шаг 2/5: IP или домен\n"
        "Например: <code>94.156.131.47</code>",
        parse_mode="HTML"
    )

@router.message(AddServer.host)
async def add_host(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено", reply_markup=main_kb())
        return
        
    await state.update_data(host=message.text)
    await state.set_state(AddServer.port)
    await message.answer("Шаг 3/5: Порт SSH\nОбычно <code>22</code>", parse_mode="HTML")

@router.message(AddServer.port)
async def add_port(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено", reply_markup=main_kb())
        return
        
    try:
        port = int(message.text)
        await state.update_data(port=port)
        await state.set_state(AddServer.username)
        await message.answer("Шаг 4/5: Username\nНапример: <code>root</code>", parse_mode="HTML")
    except:
        await message.answer("❌ Введите число")

@router.message(AddServer.username)
async def add_username(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено", reply_markup=main_kb())
        return
        
    await state.update_data(username=message.text)
    await state.set_state(AddServer.password)
    await message.answer(
        "Шаг 5/5: Пароль\n\n"
        "⚠️ Сообщение будет удалено после обработки"
    )

@router.message(AddServer.password)
async def add_password(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено", reply_markup=main_kb())
        return
        
    data = await state.get_data()
    
    # Удаляем сообщение с паролем
    await message.delete()
    
    test_msg = await message.answer("⏳ Проверяю подключение...")
    
    test_server = {
        'name': data['name'],
        'host': data['host'],
        'port': data['port'],
        'username': data['username'],
        'password': message.text
    }
    
    # Тестируем
    _, _, code = await ssh.execute(test_server, 'echo OK')
    
    if code != 0:
        await test_msg.edit_text(
            "❌ Не удалось подключиться!\n\n"
            "Проверьте данные и попробуйте снова",
            reply_markup=main_kb()
        )
        await state.clear()
        return
        
    # Сохраняем
    server_id = await db.add_server(
        data['name'],
        data['host'],
        data['port'],
        data['username'],
        message.text
    )
    
    await test_msg.edit_text(
        f"✅ Сервер <b>{data['name']}</b> добавлен!\n\n"
        "Собираю первые метрики...",
        parse_mode="HTML"
    )
    
    # Собираем метрики
    metrics = await ssh.get_metrics(test_server)
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
    await message.answer("🎉 Готово!", reply_markup=main_kb())

@router.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    servers = await db.get_servers()
    
    healthy = warning = offline = 0
    for s in servers:
        m = await db.get_latest_metrics(s['id'])
        if not m:
            offline += 1
        elif m['status'] == 'healthy':
            healthy += 1
        else:
            warning += 1
            
    text = "📈 <b>Статистика</b>\n\n"
    text += f"🖥 Всего: {len(servers)}\n"
    text += f"🟢 OK: {healthy}\n"
    text += f"🟡 Warning: {warning}\n"
    text += f"🔴 Offline: {offline}\n"
    
    await callback.message.edit_text(text, reply_markup=main_kb(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "alerts")
async def show_alerts(callback: CallbackQuery):
    alerts = await db.get_unsent_alerts()
    
    if not alerts:
        await callback.message.edit_text(
            "✅ Нет активных алертов",
            reply_markup=main_kb()
        )
        await callback.answer()
        return
        
    text = "🔔 <b>Алерты:</b>\n\n"
    for a in alerts[:10]:
        emoji = "⚠️" if a['level'] == 'warning' else "🚨"
        text += f"{emoji} <b>{a['server_name']}</b>\n{a['message']}\n\n"
        
    await callback.message.edit_text(text, reply_markup=main_kb(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "help")
async def show_help(callback: CallbackQuery):
    text = """
❓ <b>Справка</b>

<b>Возможности:</b>
• Мониторинг CPU, RAM, диска
• Выполнение команд по SSH
• Автоматические алерты
• История метрик

<b>Использование:</b>
1. Добавьте сервер (IP, порт, пароль)
2. Бот автоматически собирает метрики
3. При проблемах получите уведомление

<b>Пороги:</b>
⚠️ Warning: CPU>80%, RAM>85%, Disk>85%
🚨 Critical: CPU>95%, RAM>95%, Disk>95%

Все данные сохраняются на Bothost!
"""
    await callback.message.edit_text(text, reply_markup=main_kb(), parse_mode="HTML")
    await callback.answer()

# ============= ФОНОВЫЙ МОНИТОРИНГ =============

async def monitor_all_servers():
    """Фоновая проверка всех серверов"""
    logger.info("Running monitoring...")
    
    servers = await db.get_servers()
    
    for server in servers:
        try:
            metrics = await ssh.get_metrics(server)
            
            if not metrics:
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
                    f"CPU: {metrics['cpu_usage']:.1f}% (критично!)"
                )
            elif metrics['cpu_usage'] > Config.CPU_WARNING:
                await db.add_alert(
                    server['id'],
                    'warning',
                    f"CPU: {metrics['cpu_usage']:.1f}% (высокая нагрузка)"
                )
                
            if metrics['mem_usage'] > Config.MEM_CRITICAL:
                await db.add_alert(
                    server['id'],
                    'critical',
                    f"RAM: {metrics['mem_usage']:.1f}% (критично!)"
                )
                
            if metrics['disk_usage'] > Config.DISK_CRITICAL:
                await db.add_alert(
                    server['id'],
                    'critical',
                    f"Диск: {metrics['disk_usage']:.1f}% (почти заполнен!)"
                )
                
        except Exception as e:
            logger.error(f"Error monitoring {server['name']}: {e}")
            
    # Отправляем алерты
    await send_alerts()

async def send_alerts():
    """Отправка алертов админам"""
    alerts = await db.get_unsent_alerts()
    
    for alert in alerts:
        emoji = "⚠️" if alert['level'] == 'warning' else "🚨"
        text = f"{emoji} <b>{alert['server_name']}</b>\n\n{alert['message']}"
        
        for admin_id in Config.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
                await db.mark_alert_sent(alert['id'])
            except Exception as e:
                logger.error(f"Failed to send alert: {e}")

# ============= ЗАПУСК =============

async def on_startup():
    logger.info("Bot starting...")
    await db.init()
    
    # Запускаем планировщик
    scheduler.add_job(monitor_all_servers, 'interval', seconds=Config.CHECK_INTERVAL)
    scheduler.start()
    
    logger.info(f"Bot started! Monitoring interval: {Config.CHECK_INTERVAL}s")

async def on_shutdown():
    logger.info("Bot shutting down...")
    scheduler.shutdown()

async def main():
    try:
        await on_startup()
        await dp.start_polling(bot)
    finally:
        await on_shutdown()

if __name__ == '__main__':
    asyncio.run(main())
