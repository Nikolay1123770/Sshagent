#!/usr/bin/env python3
"""
SSH Server Monitoring Agent
Telegram Bot + Web Interface - Optimized for Bothost
"""

import os
import sys
import asyncio
import logging
import base64
import mimetypes
from datetime import datetime
from typing import Optional, Dict, List
from pathlib import Path
import json
import signal

# Telegram
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

# Web
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
import uvicorn
from threading import Thread
import multiprocessing

# Scheduler
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# SSH & DB
import asyncssh
import aiosqlite


# ============= КОНФИГУРАЦИЯ =============

class Config:
    # Telegram
    BOT_TOKEN = os.getenv('BOT_TOKEN', '')
    ADMIN_IDS = list(filter(None, map(str.strip, os.getenv('ADMIN_IDS', '').split(','))))
    ADMIN_IDS = [int(x) for x in ADMIN_IDS if x.isdigit()]
    
    # Web
    WEB_HOST = os.getenv('WEB_HOST', '0.0.0.0')
    WEB_PORT = int(os.getenv('PORT', '8000'))
    
    # Database
    DB_PATH = os.getenv('DB_PATH', '/app/data/agent.db')
    
    # Monitoring
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
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/app/data/agent.log')
    ]
)
logger = logging.getLogger(__name__)


# ============= БАЗА ДАННЫХ =============

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
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
            
    async def add_server(self, name, host, port, username, password) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'INSERT INTO servers (name, host, port, username, password) VALUES (?, ?, ?, ?, ?)',
                (name, host, port, username, password)
            )
            await db.commit()
            return cursor.lastrowid
            
    async def get_servers(self, enabled_only=True):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = 'SELECT * FROM servers'
            if enabled_only:
                query += ' WHERE enabled = 1'
            query += ' ORDER BY name'
            async with db.execute(query) as cursor:
                return [dict(row) for row in await cursor.fetchall()]
                
    async def get_server(self, server_id):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('SELECT * FROM servers WHERE id = ?', (server_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
                
    async def delete_server(self, server_id):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('DELETE FROM servers WHERE id = ?', (server_id,))
            await db.execute('DELETE FROM metrics WHERE server_id = ?', (server_id,))
            await db.execute('DELETE FROM alerts WHERE server_id = ?', (server_id,))
            await db.commit()
            
    async def save_metrics(self, server_id, cpu, mem, disk, load, uptime, status):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                '''INSERT INTO metrics (server_id, cpu_usage, mem_usage, disk_usage, 
                   load_avg, uptime, status) VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (server_id, cpu, mem, disk, load, uptime, status)
            )
            # Удаляем старые записи, оставляем последние 1000
            await db.execute(
                '''DELETE FROM metrics WHERE server_id = ? AND id NOT IN (
                   SELECT id FROM metrics WHERE server_id = ? 
                   ORDER BY timestamp DESC LIMIT 1000)''',
                (server_id, server_id)
            )
            await db.commit()
            
    async def get_latest_metrics(self, server_id):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM metrics WHERE server_id = ? ORDER BY timestamp DESC LIMIT 1',
                (server_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
                
    async def add_alert(self, server_id, level, message):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'INSERT INTO alerts (server_id, level, message) VALUES (?, ?, ?)',
                (server_id, level, message)
            )
            await db.commit()
            
    async def get_unsent_alerts(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                '''SELECT a.*, s.name as server_name FROM alerts a
                   JOIN servers s ON a.server_id = s.id
                   WHERE a.sent = 0 ORDER BY a.created_at ASC LIMIT 10'''
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]
                
    async def mark_alert_sent(self, alert_id):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE alerts SET sent = 1 WHERE id = ?', (alert_id,))
            await db.commit()


# ============= SSH МЕНЕДЖЕР =============

class SSHManager:
    async def execute(self, server, command, timeout=30):
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
                return result.stdout.strip() if result.stdout else '', result.stderr.strip() if result.stderr else '', result.exit_status
        except asyncio.TimeoutError:
            logger.error(f"SSH timeout for {server['host']}")
            return '', 'Timeout', -1
        except Exception as e:
            logger.error(f"SSH error for {server['host']}: {e}")
            return '', str(e), -1
            
    async def get_metrics(self, server):
        try:
            # CPU usage
            cpu_cmd = "top -bn1 | grep 'Cpu(s)' | awk '{print $2}' | sed 's/%us,//'"
            cpu_out, cpu_err, cpu_code = await self.execute(server, cpu_cmd)
            if cpu_code != 0:
                return None
            try:
                cpu_usage = float(cpu_out.strip()) if cpu_out.strip() else 0.0
            except:
                cpu_usage = 0.0
            
            # Memory usage
            mem_cmd = "free | grep Mem | awk '{print ($3/$2) * 100.0}'"
            mem_out, mem_err, mem_code = await self.execute(server, mem_cmd)
            if mem_code != 0:
                return None
            try:
                mem_usage = float(mem_out.strip()) if mem_out.strip() else 0.0
            except:
                mem_usage = 0.0
            
            # Disk usage
            disk_cmd = "df -h / | tail -1 | awk '{print $5}' | sed 's/%//'"
            disk_out, disk_err, disk_code = await self.execute(server, disk_cmd)
            if disk_code != 0:
                return None
            try:
                disk_usage = float(disk_out.strip()) if disk_out.strip() else 0.0
            except:
                disk_usage = 0.0
            
            # Load average
            load_cmd = "cat /proc/loadavg | cut -d' ' -f1-3"
            load_out, load_err, load_code = await self.execute(server, load_cmd)
            load_avg = load_out.strip() if load_out else "0 0 0"
            
            # Uptime
            uptime_cmd = "cat /proc/uptime | cut -d' ' -f1"
            uptime_out, uptime_err, uptime_code = await self.execute(server, uptime_cmd)
            try:
                uptime = int(float(uptime_out.strip())) if uptime_out.strip() else 0
            except:
                uptime = 0
            
            # Determine status
            if cpu_usage > Config.CPU_CRITICAL or mem_usage > Config.MEM_CRITICAL or disk_usage > Config.DISK_CRITICAL:
                status = 'critical'
            elif cpu_usage > Config.CPU_WARNING or mem_usage > Config.MEM_WARNING or disk_usage > Config.DISK_WARNING:
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
            logger.error(f"Failed to get metrics for {server.get('name', 'unknown')}: {e}")
            return None


# ============= ИНИЦИАЛИЗАЦИЯ =============

db = Database(Config.DB_PATH)
ssh = SSHManager()
scheduler = AsyncIOScheduler(timezone="UTC")

# Telegram Bot
bot = Bot(token=Config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# FastAPI Web
app = FastAPI(title="SSH Agent", docs_url=None, redoc_url=None)


# ============= TELEGRAM BOT =============

class AddServer(StatesGroup):
    name = State()
    host = State()
    port = State()
    username = State()
    password = State()

class ExecCommand(StatesGroup):
    waiting = State()

def main_kb():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Серверы", callback_data="list"),
        InlineKeyboardButton(text="➕ Добавить", callback_data="add")
    )
    builder.row(
        InlineKeyboardButton(text="📈 Статистика", callback_data="stats"),
        InlineKeyboardButton(text="🌐 Web", callback_data="web")
    )
    return builder.as_markup()

def servers_kb(servers):
    builder = InlineKeyboardBuilder()
    for s in servers:
        emoji = "🟢" if s['enabled'] else "🔴"
        builder.row(InlineKeyboardButton(
            text=f"{emoji} {s['name']}",
            callback_data=f"srv_{s['id']}"
        ))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu"))
    return builder.as_markup()

def server_kb(server_id):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Метрики", callback_data=f"met_{server_id}"),
        InlineKeyboardButton(text="💻 Команда", callback_data=f"cmd_{server_id}")
    )
    builder.row(
        InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_{server_id}"),
        InlineKeyboardButton(text="🔙 Назад", callback_data="list")
    )
    return builder.as_markup()

def confirm_kb(server_id):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да", callback_data=f"confirm_{server_id}"),
        InlineKeyboardButton(text="❌ Нет", callback_data=f"srv_{server_id}")
    )
    return builder.as_markup()

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id not in Config.ADMIN_IDS:
        await message.answer("❌ Доступ запрещен")
        return
    
    web_url = f"https://sshagent.bothost.ru"
    
    await message.answer(
        f"👋 Привет!\n\n"
        "🖥 SSH Server Agent\n"
        "📱 Telegram + 🌐 Web интерфейс\n\n"
        f"🌐 Web: {web_url}",
        reply_markup=main_kb()
    )

@router.callback_query(F.data == "menu")
async def show_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🏠 Главное меню:", reply_markup=main_kb())
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
    text = "🖥 <b>Серверы:</b>\n\n"
    for s in servers:
        m = await db.get_latest_metrics(s['id'])
        status = "🟢" if m and m['status'] == 'healthy' else "🔴"
        text += f"{status} <b>{s['name']}</b> - {s['host']}\n"
        if m:
            text += f"   CPU: {m['cpu_usage']:.1f}% | RAM: {m['mem_usage']:.1f}%\n"
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
    m = await db.get_latest_metrics(server_id)
    text = f"🖥 <b>{server['name']}</b>\n\n"
    text += f"📍 {server['host']}:{server['port']}\n"
    text += f"👤 {server['username']}\n\n"
    if m:
        text += f"💻 CPU: {m['cpu_usage']:.1f}%\n"
        text += f"💾 RAM: {m['mem_usage']:.1f}%\n"
        text += f"💿 Disk: {m['disk_usage']:.1f}%\n"
        text += f"📈 Load: {m['load_avg']}\n"
        text += f"⏱ Uptime: {m['uptime']} sec\n"
        status_emoji = "🟢" if m['status'] == 'healthy' else "🟡" if m['status'] == 'warning' else "🔴"
        text += f"📊 Status: {status_emoji} {m['status']}\n"
    else:
        text += "⚠️ Метрики не собраны"
    await callback.message.edit_text(text, reply_markup=server_kb(server_id), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("met_"))
async def refresh_metrics(callback: CallbackQuery):
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    await callback.answer("🔄 Обновляю...")
    metrics = await ssh.get_metrics(server)
    if metrics:
        await db.save_metrics(
            server_id, metrics['cpu_usage'], metrics['mem_usage'],
            metrics['disk_usage'], metrics['load_avg'],
            metrics['uptime'], metrics['status']
        )
    await show_server(callback)

@router.callback_query(F.data.startswith("cmd_"))
async def start_exec(callback: CallbackQuery, state: FSMContext):
    server_id = int(callback.data.split("_")[1])
    await state.update_data(server_id=server_id)
    await state.set_state(ExecCommand.waiting)
    await callback.message.answer(
        "💻 Введите команду:\n\nНапример: <code>df -h</code>\n\n/cancel для отмены",
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
    if not server:
        await message.answer("❌ Сервер не найден")
        await state.clear()
        return
    msg = await message.answer("⏳ Выполняю...")
    stdout, stderr, code = await ssh.execute(server, message.text)
    result = f"💻 <code>{message.text}</code>\n📤 Exit: {code}\n\n"
    if stdout:
        # Обрезаем слишком длинный вывод
        if len(stdout) > 3000:
            stdout = stdout[:3000] + "\n... (output truncated)"
        result += f"<pre>{stdout}</pre>"
    if stderr:
        # Обрезаем слишком длинный вывод ошибок
        if len(stderr) > 1000:
            stderr = stderr[:1000] + "\n... (error truncated)"
        result += f"\n<b>Error:</b>\n<pre>{stderr}</pre>"
    await msg.edit_text(result, parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data.startswith("del_"))
async def delete_confirm(callback: CallbackQuery):
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    await callback.message.edit_text(
        f"⚠️ Удалить <b>{server['name']}</b>?",
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

@router.callback_query(F.data == "add")
async def start_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddServer.name)
    await callback.message.edit_text(
        "➕ <b>Добавление сервера</b>\n\n"
        "Шаг 1/5: Имя сервера\n\n/cancel для отмены",
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
    await message.answer("Шаг 2/5: IP или домен")

@router.message(AddServer.host)
async def add_host(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено", reply_markup=main_kb())
        return
    await state.update_data(host=message.text)
    await state.set_state(AddServer.port)
    await message.answer("Шаг 3/5: Порт SSH (обычно 22)")

@router.message(AddServer.port)
async def add_port(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено", reply_markup=main_kb())
        return
    try:
        port = int(message.text)
        if port < 1 or port > 65535:
            await message.answer("❌ Порт должен быть от 1 до 65535")
            return
        await state.update_data(port=port)
        await state.set_state(AddServer.username)
        await message.answer("Шаг 4/5: Username")
    except ValueError:
        await message.answer("❌ Введите число")

@router.message(AddServer.username)
async def add_username(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено", reply_markup=main_kb())
        return
    await state.update_data(username=message.text)
    await state.set_state(AddServer.password)
    await message.answer("Шаг 5/5: Пароль\n\n⚠️ Сообщение будет удалено")

@router.message(AddServer.password)
async def add_password(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено", reply_markup=main_kb())
        return
    data = await state.get_data()
    try:
        await message.delete()
    except:
        pass  # Игнорируем ошибку удаления сообщения
    
    test_msg = await message.answer("⏳ Проверяю подключение...")
    test_server = {
        'name': data['name'], 
        'host': data['host'],
        'port': data['port'], 
        'username': data['username'],
        'password': message.text
    }
    stdout, stderr, code = await ssh.execute(test_server, 'echo OK')
    if code != 0:
        await test_msg.edit_text(
            f"❌ Не удалось подключиться!\n\nОшибка: {stderr or stdout}",
            reply_markup=main_kb()
        )
        await state.clear()
        return
    
    try:
        server_id = await db.add_server(
            data['name'], data['host'], data['port'],
            data['username'], message.text
        )
        await test_msg.edit_text(f"✅ Сервер <b>{data['name']}</b> добавлен!", parse_mode="HTML")
        
        # Сразу собираем метрики
        metrics = await ssh.get_metrics(test_server)
        if metrics:
            await db.save_metrics(
                server_id, metrics['cpu_usage'], metrics['mem_usage'],
                metrics['disk_usage'], metrics['load_avg'],
                metrics['uptime'], metrics['status']
            )
    except Exception as e:
        await test_msg.edit_text(f"❌ Ошибка добавления: {e}")
    
    await state.clear()
    await message.answer("🎉 Готово!", reply_markup=main_kb())

@router.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    servers = await db.get_servers()
    healthy = warning = critical = 0
    for s in servers:
        m = await db.get_latest_metrics(s['id'])
        if not m:
            critical += 1
        elif m['status'] == 'healthy':
            healthy += 1
        elif m['status'] == 'warning':
            warning += 1
        else:
            critical += 1
    
    text = "📈 <b>Статистика</b>\n\n"
    text += f"🖥 Всего: {len(servers)}\n"
    text += f"🟢 Healthy: {healthy}\n"
    text += f"🟡 Warning: {warning}\n"
    text += f"🔴 Critical/Offline: {critical}\n"
    await callback.message.edit_text(text, reply_markup=main_kb(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "web")
async def show_web_link(callback: CallbackQuery):
    web_url = f"https://sshagent.bothost.ru"
    text = f"🌐 <b>Web интерфейс</b>\n\n"
    text += f"URL: <code>{web_url}</code>\n\n"
    text += "Откройте в браузере для доступа к:\n"
    text += "• Dashboard с метриками\n"
    text += "• Терминал для команд\n"
    text += "• Файловый менеджер\n"
    await callback.message.edit_text(text, reply_markup=main_kb(), parse_mode="HTML")
    await callback.answer()


# ============= WEB INTERFACE =============

HTML_STYLE = """
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { 
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f5f5f5;
    color: #333;
}
.navbar {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    padding: 1rem 2rem;
    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.navbar h1 { font-size: 1.5rem; }
.container { max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
.stat-card {
    background: white;
    padding: 1.5rem;
    border-radius: 10px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.05);
    text-align: center;
}
.stat-value { font-size: 2.5rem; font-weight: bold; color: #667eea; }
.stat-label { color: #666; margin-top: 0.5rem; }
.servers { display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 1.5rem; }
.server {
    background: white;
    padding: 1.5rem;
    border-radius: 10px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.05);
    border-left: 4px solid #667eea;
}
.server.warning { border-left-color: #f59e0b; }
.server.critical { border-left-color: #ef4444; }
.server h3 { margin-bottom: 0.5rem; }
.server .host { color: #666; font-size: 0.9rem; }
.metrics { margin: 1rem 0; }
.metric { margin: 0.5rem 0; }
.metric-label { font-size: 0.85rem; color: #666; margin-bottom: 0.25rem; }
.metric-bar {
    background: #e5e7eb;
    height: 8px;
    border-radius: 4px;
    overflow: hidden;
}
.metric-fill {
    background: linear-gradient(90deg, #667eea, #764ba2);
    height: 100%;
    transition: width 0.3s;
}
.btn {
    display: inline-block;
    padding: 0.5rem 1rem;
    background: #667eea;
    color: white;
    text-decoration: none;
    border-radius: 5px;
    border: none;
    cursor: pointer;
    margin: 0.25rem;
    font-size: 0.9rem;
}
.btn:hover { background: #5568d3; }
.btn-secondary { background: #6b7280; }
.btn-secondary:hover { background: #4b5563; }
textarea { 
    width: 100%; 
    min-height: 400px; 
    font-family: monospace; 
    padding: 1rem; 
    border: 1px solid #ddd;
    border-radius: 5px;
}
</style>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    servers = await db.get_servers()
    stats = {'total': len(servers), 'healthy': 0, 'warning': 0, 'critical': 0}
    
    servers_html = ""
    for server in servers:
        m = await db.get_latest_metrics(server['id'])
        status_class = ""
        status_text = ""
        
        if m:
            if m['status'] == 'healthy':
                stats['healthy'] += 1
                status_class = ""
                status_text = "🟢 Healthy"
            elif m['status'] == 'warning':
                stats['warning'] += 1
                status_class = "warning"
                status_text = "🟡 Warning"
            else:
                stats['critical'] += 1
                status_class = "critical"
                status_text = "🔴 Critical"
            
            metrics_html = f"""
            <div class="metrics">
                <div class="metric">
                    <div class="metric-label">CPU: {m['cpu_usage']:.1f}%</div>
                    <div class="metric-bar">
                        <div class="metric-fill" style="width: {min(m['cpu_usage'], 100)}%"></div>
                    </div>
                </div>
                <div class="metric">
                    <div class="metric-label">RAM: {m['mem_usage']:.1f}%</div>
                    <div class="metric-bar">
                        <div class="metric-fill" style="width: {min(m['mem_usage'], 100)}%"></div>
                    </div>
                </div>
                <div class="metric">
                    <div class="metric-label">Disk: {m['disk_usage']:.1f}%</div>
                    <div class="metric-bar">
                        <div class="metric-fill" style="width: {min(m['disk_usage'], 100)}%"></div>
                    </div>
                </div>
                <div style="margin-top: 0.5rem; font-size: 0.85rem; color: #666;">
                    Load: {m['load_avg']} | Status: {status_text}
                </div>
            </div>
            """
        else:
            stats['critical'] += 1
            status_class = "critical"
            metrics_html = "<p style='color: #666;'>Метрики не доступны</p>"
        
        servers_html += f"""
        <div class="server {status_class}">
            <h3>{server['name']}</h3>
            <div class="host">{server['host']}:{server['port']} • {server['username']}</div>
            {metrics_html}
            <div>
                <a href="/terminal/{server['id']}" class="btn">Terminal</a>
                <a href="/files/{server['id']}" class="btn btn-secondary">Files</a>
            </div>
        </div>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>SSH Agent</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        {HTML_STYLE}
    </head>
    <body>
        <div class="navbar">
            <h1>🖥 SSH Agent</h1>
            <div style="font-size: 0.9rem;">Auto refresh: 60s</div>
        </div>
        
        <div class="container">
            <div class="stats">
                <div class="stat-card">
                    <div class="stat-value">{stats['healthy']}</div>
                    <div class="stat-label">🟢 Healthy</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{stats['warning']}</div>
                    <div class="stat-label">🟡 Warning</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{stats['critical']}</div>
                    <div class="stat-label">🔴 Critical</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{stats['total']}</div>
                    <div class="stat-label">📊 Total</div>
                </div>
            </div>
            
            <h2>Servers ({stats['total']})</h2>
            <div class="servers">
                {servers_html if servers_html else '<p>No servers yet. Add via Telegram bot!</p>'}
            </div>
        </div>
        
        <script>
            setTimeout(() => location.reload(), 60000);
        </script>
    </body>
    </html>
    """
    return html

@app.get("/terminal/{server_id}", response_class=HTMLResponse)
async def terminal_page(server_id: int):
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Terminal - {server['name']}</title>
        {HTML_STYLE}
    </head>
    <body>
        <div class="navbar">
            <h1>💻 Terminal: {server['name']}</h1>
            <a href="/" class="btn" style="background: transparent; border: 1px solid white;">← Back</a>
        </div>
        
        <div class="container">
            <div style="background: #1e1e1e; color: #0f0; padding: 1rem; border-radius: 10px; min-height: 400px; font-family: 'Courier New', monospace;">
                <div id="output"></div>
                <div style="display: flex; align-items: center; margin-top: 1rem;">
                    <span style="color: #0f0;">$ </span>
                    <input type="text" id="input" style="flex: 1; background: transparent; border: none; color: #0f0; font-family: monospace; padding: 0.5rem; outline: none;" autocomplete="off" placeholder="Type command...">
                </div>
            </div>
            
            <div style="margin-top: 1rem;">
                <button class="btn" onclick="runCmd('df -h')">Disk Usage</button>
                <button class="btn" onclick="runCmd('free -m')">Memory</button>
                <button class="btn" onclick="runCmd('uptime')">Uptime</button>
                <button class="btn" onclick="runCmd('top -bn1 | head -20')">Processes</button>
                <button class="btn btn-secondary" onclick="clearOutput()">Clear</button>
            </div>
        </div>
        
        <script>
            const output = document.getElementById('output');
            const input = document.getElementById('input');
            
            function addOutput(text, color = '#0f0') {{
                const div = document.createElement('div');
                div.style.color = color;
                div.textContent = text;
                output.appendChild(div);
                output.scrollTop = output.scrollHeight;
            }}
            
            async function runCmd(cmd) {{
                input.value = cmd;
                await exec();
            }}
            
            async function exec() {{
                const cmd = input.value.trim();
                if (!cmd) return;
                
                addOutput('$ ' + cmd);
                input.value = '';
                
                try {{
                    const res = await fetch('/api/exec/{server_id}', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{command: cmd}})
                    }});
                    const data = await res.json();
                    if (data.stdout) {{
                        addOutput(data.stdout);
                    }}
                    if (data.stderr) {{
                        addOutput(data.stderr, '#ff4444');
                    }}
                    if (!data.stdout && !data.stderr) {{
                        addOutput('(no output)');
                    }}
                }} catch (e) {{
                    addOutput('Error: ' + e, '#ff4444');
                }}
            }}
            
            function clearOutput() {{
                output.innerHTML = '';
            }}
            
            input.addEventListener('keydown', e => {{
                if (e.key === 'Enter') exec();
                if (e.key === 'Escape') input.value = '';
            }});
            input.focus();
        </script>
    </body>
    </html>
    """
    return html

@app.get("/files/{server_id}", response_class=HTMLResponse)
async def files_page(server_id: int):
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Files - {server['name']}</title>
        {HTML_STYLE}
    </head>
    <body>
        <div class="navbar">
            <h1>📁 Files: {server['name']}</h1>
            <a href="/" class="btn" style="background: transparent; border: 1px solid white;">← Back</a>
        </div>
        
        <div class="container">
            <div style="background: white; padding: 1.5rem; border-radius: 10px;">
                <div style="margin-bottom: 1rem; display: flex; gap: 0.5rem; align-items: center;">
                    <button class="btn" onclick="load('/')">Home</button>
                    <button class="btn" onclick="goUp()">Up</button>
                    <input type="text" id="path" value="/" style="flex: 1; padding: 0.5rem; border: 1px solid #ddd; border-radius: 5px;" onkeydown="if(event.key==='Enter') load(this.value)">
                    <button class="btn" onclick="load(document.getElementById('path').value)">Go</button>
                    <button class="btn" onclick="load(document.getElementById('path').value)">Refresh</button>
                </div>
                
                <div id="loading" style="text-align: center; padding: 2rem; color: #666;">
                    Loading...
                </div>
                
                <table id="filesTable" style="width: 100%; border-collapse: collapse; display: none;">
                    <thead>
                        <tr style="background: #f5f5f5;">
                            <th style="padding: 0.75rem; text-align: left;">Name</th>
                            <th style="padding: 0.75rem; text-align: left;">Size</th>
                            <th style="padding: 0.75rem; text-align: left;">Modified</th>
                            <th style="padding: 0.75rem; text-align: left;">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="files">
                    </tbody>
                </table>
            </div>
            
            <div id="editor" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 1000;">
                <div style="background: white; margin: 5% auto; padding: 2rem; max-width: 90%; max-height: 90%; overflow: auto; border-radius: 10px; box-shadow: 0 10px 30px rgba(0,0,0,0.3);">
                    <h3 id="filename">Edit File</h3>
                    <textarea id="content" style="width: 100%; min-height: 300px; margin: 1rem 0;"></textarea>
                    <div style="margin-top: 1rem; display: flex; gap: 0.5rem;">
                        <button class="btn" onclick="save()">Save</button>
                        <button class="btn btn-secondary" onclick="closeEditor()">Cancel</button>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
            let currentPath = '/';
            let editingFile = null;
            
            async function load(path) {{
                currentPath = path;
                document.getElementById('path').value = path;
                document.getElementById('filesTable').style.display = 'none';
                document.getElementById('loading').style.display = 'block';
                
                try {{
                    const res = await fetch('/api/files/{server_id}/list?path=' + encodeURIComponent(path));
                    if (!res.ok) throw new Error('HTTP ' + res.status);
                    const data = await res.json();
                    
                    const tbody = document.getElementById('files');
                    if (!data.files || data.files.length === 0) {{
                        tbody.innerHTML = '<tr><td colspan="4" style="padding: 2rem; text-align: center; color: #666;">Empty directory</td></tr>';
                    }} else {{
                        tbody.innerHTML = data.files.map(f => `
                            <tr style="border-bottom: 1px solid #eee;">
                                <td style="padding: 0.75rem;">
                                    <a href="#" onclick="${{f.is_dir ? `load('${{f.path}}')` : `edit('${{f.path}}')`}}; return false;" style="text-decoration: none; color: #333;">
                                        ${{f.is_dir ? '📁' : '📄'}} ${{f.name}}
                                    </a>
                                </td>
                                <td style="padding: 0.75rem;">${{f.size}}</td>
                                <td style="padding: 0.75rem;">${{f.date}}</td>
                                <td style="padding: 0.75rem;">
                                    ${{!f.is_dir ? `<button class="btn" onclick="edit('${{f.path}}')" style="padding: 0.25rem 0.5rem; font-size: 0.8rem;">Edit</button>` : ''}}
                                </td>
                            </tr>
                        `).join('');
                    }}
                    
                    document.getElementById('loading').style.display = 'none';
                    document.getElementById('filesTable').style.display = 'table';
                }} catch (e) {{
                    document.getElementById('loading').innerHTML = `<div style="color: red; padding: 1rem;">Error: ${{e.message}}</div>`;
                }}
            }}
            
            function goUp() {{
                const parts = currentPath.split('/').filter(p => p);
                parts.pop();
                load('/' + parts.join('/') || '/');
            }}
            
            async function edit(path) {{
                editingFile = path;
                const filename = path.split('/').pop();
                document.getElementById('filename').textContent = 'Edit: ' + filename;
                document.getElementById('editor').style.display = 'block';
                
                try {{
                    const res = await fetch('/api/files/{server_id}/read?path=' + encodeURIComponent(path));
                    if (!res.ok) throw new Error('HTTP ' + res.status);
                    const data = await res.json();
                    document.getElementById('content').value = data.content;
                }} catch (e) {{
                    alert('Error reading file: ' + e.message);
                    closeEditor();
                }}
            }}
            
            async function save() {{
                if (!editingFile) return;
                
                try {{
                    const res = await fetch('/api/files/{server_id}/write', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{
                            path: editingFile,
                            content: document.getElementById('content').value
                        }})
                    }});
                    if (!res.ok) throw new Error('HTTP ' + res.status);
                    alert('File saved successfully!');
                    closeEditor();
                }} catch (e) {{
                    alert('Error saving file: ' + e.message);
                }}
            }}
            
            function closeEditor() {{
                document.getElementById('editor').style.display = 'none';
                editingFile = null;
                document.getElementById('content').value = '';
            }}
            
            load('/');
        </script>
    </body>
    </html>
    """
    return html

# API Endpoints

@app.post("/api/exec/{server_id}")
async def exec_api(server_id: int, request: Request):
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    try:
        data = await request.json()
        command = data.get('command', '')
        if not command:
            raise HTTPException(status_code=400, detail="Command is required")
        
        stdout, stderr, code = await ssh.execute(server, command)
        return {
            'stdout': stdout,
            'stderr': stderr,
            'exit_code': code,
            'server': server['name']
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/files/{server_id}/list")
async def list_files_api(server_id: int, path: str = "/"):
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    try:
        # Безопасная проверка пути
        safe_path = path.strip() or "/"
        cmd = f"ls -lAh --time-style=long-iso '{safe_path}' 2>/dev/null || ls -lAh '{safe_path}' 2>/dev/null"
        stdout, stderr, code = await ssh.execute(server, cmd)
        
        if code != 0:
            raise HTTPException(status_code=400, detail=f"Failed to list directory: {stderr}")
        
        files = []
        lines = stdout.strip().split('\n')
        
        for line in lines[1:]:  # Пропускаем первую строку (total)
            if not line.strip():
                continue
            
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            
            file_name = parts[8]
            # Пропускаем . и ..
            if file_name in ['.', '..']:
                continue
            
            is_dir = parts[0].startswith('d')
            files.append({
                'name': file_name,
                'size': parts[4],
                'date': f"{parts[5]} {parts[6]}",
                'permissions': parts[0],
                'is_dir': is_dir,
                'path': f"{safe_path.rstrip('/')}/{file_name}"
            })
        
        return {'path': safe_path, 'files': files}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/files/{server_id}/read")
async def read_file_api(server_id: int, path: str):
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    try:
        # Проверяем, что файл существует и это не директория
        check_cmd = f"[ -f '{path}' ] && echo 'FILE' || echo 'NOT_FOUND'"
        check_out, check_err, check_code = await ssh.execute(server, check_cmd)
        
        if check_out.strip() != 'FILE':
            raise HTTPException(status_code=404, detail="File not found")
        
        # Читаем файл
        read_cmd = f"head -c 1048576 '{path}'"  # Ограничиваем 1MB
        stdout, stderr, code = await ssh.execute(server, read_cmd, timeout=60)
        
        if code != 0:
            raise HTTPException(status_code=400, detail=f"Failed to read file: {stderr}")
        
        return {'content': stdout, 'path': path, 'size': len(stdout)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/files/{server_id}/write")
async def write_file_api(server_id: int, request: Request):
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    try:
        data = await request.json()
        path = data.get('path', '')
        content = data.get('content', '')
        
        if not path:
            raise HTTPException(status_code=400, detail="Path is required")
        
        # Экранируем контент
        escaped_content = content.replace("'", "'\"'\"'")
        
        # Записываем файл через временный файл
        temp_file = f"/tmp/sshagent_{os.urandom(4).hex()}"
        cmd = f"echo -n '{escaped_content}' > '{temp_file}' && mv '{temp_file}' '{path}'"
        
        _, stderr, code = await ssh.execute(server, cmd, timeout=30)
        
        if code != 0:
            raise HTTPException(status_code=400, detail=f"Failed to write file: {stderr}")
        
        return {'success': True, 'path': path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============= МОНИТОРИНГ =============

async def monitor_all_servers():
    logger.info("Running monitoring...")
    servers = await db.get_servers()
    
    if not servers:
        logger.info("No servers to monitor")
        return
    
    for server in servers:
        try:
            logger.info(f"Checking server: {server['name']} ({server['host']})")
            metrics = await ssh.get_metrics(server)
            
            if not metrics:
                await db.add_alert(server['id'], 'critical', f"Сервер {server['name']} недоступен!")
                await db.save_metrics(
                    server['id'], 0, 0, 0, 
                    "0 0 0", 0, 'critical'
                )
                continue
            
            await db.save_metrics(
                server['id'], metrics['cpu_usage'], metrics['mem_usage'],
                metrics['disk_usage'], metrics['load_avg'],
                metrics['uptime'], metrics['status']
            )
            
            # Проверяем критические уровни
            alerts_added = False
            if metrics['cpu_usage'] > Config.CPU_CRITICAL:
                await db.add_alert(server['id'], 'critical', f"CPU: {metrics['cpu_usage']:.1f}% (критично!)")
                alerts_added = True
            elif metrics['cpu_usage'] > Config.CPU_WARNING:
                await db.add_alert(server['id'], 'warning', f"CPU: {metrics['cpu_usage']:.1f}% (высокая нагрузка)")
                alerts_added = True
            
            if metrics['mem_usage'] > Config.MEM_CRITICAL:
                await db.add_alert(server['id'], 'critical', f"RAM: {metrics['mem_usage']:.1f}% (критично!)")
                alerts_added = True
            elif metrics['mem_usage'] > Config.MEM_WARNING:
                await db.add_alert(server['id'], 'warning', f"RAM: {metrics['mem_usage']:.1f}% (высокая нагрузка)")
                alerts_added = True
            
            if metrics['disk_usage'] > Config.DISK_CRITICAL:
                await db.add_alert(server['id'], 'critical', f"Диск: {metrics['disk_usage']:.1f}% (почти заполнен!)")
                alerts_added = True
            elif metrics['disk_usage'] > Config.DISK_WARNING:
                await db.add_alert(server['id'], 'warning', f"Диск: {metrics['disk_usage']:.1f}% (высокая заполненность)")
                alerts_added = True
            
            if alerts_added:
                logger.warning(f"Alerts generated for {server['name']}")
            else:
                logger.info(f"Server {server['name']} is healthy")
                
        except Exception as e:
            logger.error(f"Error monitoring {server['name']}: {e}")
            await db.add_alert(server['id'], 'critical', f"Ошибка мониторинга: {str(e)[:100]}")
    
    await send_alerts()

async def send_alerts():
    alerts = await db.get_unsent_alerts()
    if not alerts:
        return
    
    logger.info(f"Sending {len(alerts)} alerts")
    for alert in alerts:
        try:
            emoji = "⚠️" if alert['level'] == 'warning' else "🚨"
            text = f"{emoji} <b>{alert['server_name']}</b>\n\n{alert['message']}"
            
            for admin_id in Config.ADMIN_IDS:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            
            await db.mark_alert_sent(alert['id'])
            logger.info(f"Alert sent: {alert['id']}")
            
        except Exception as e:
            logger.error(f"Failed to send alert {alert['id']}: {e}")


# ============= ЗАПУСК =============

def run_web():
    """Запуск веб-сервера в отдельном процессе"""
    logger.info(f"Starting web server on {Config.WEB_HOST}:{Config.WEB_PORT}")
    
    try:
        uvicorn.run(
            app,
            host=Config.WEB_HOST,
            port=Config.WEB_PORT,
            log_level="warning",
            access_log=False,
            loop="asyncio"
        )
    except Exception as e:
        logger.error(f"Web server error: {e}")

async def main():
    logger.info("=== SSH Agent Starting ===")
    logger.info(f"Admin IDs: {Config.ADMIN_IDS}")
    logger.info(f"Web server: {Config.WEB_HOST}:{Config.WEB_PORT}")
    logger.info(f"Database: {Config.DB_PATH}")
    logger.info(f"Check interval: {Config.CHECK_INTERVAL}s")
    
    # Проверка конфигурации
    if not Config.BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is required!")
        sys.exit(1)
    
    if not Config.ADMIN_IDS:
        logger.warning("No ADMIN_IDS specified - bot will be inaccessible")
    
    # Инициализация БД
    await db.init()
    
    # Запуск планировщика
    scheduler.add_job(monitor_all_servers, 'interval', seconds=Config.CHECK_INTERVAL)
    scheduler.start()
    logger.info(f"Scheduler started (interval: {Config.CHECK_INTERVAL}s)")
    
    # Запуск мониторинга сразу
    asyncio.create_task(monitor_all_servers())
    
    # Запуск веб-сервера в отдельном процессе
    from multiprocessing import Process
    web_process = Process(target=run_web, daemon=True)
    web_process.start()
    logger.info(f"Web server started on {Config.WEB_HOST}:{Config.WEB_PORT}")
    
    # Запуск Telegram бота
    logger.info("Starting Telegram bot...")
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        scheduler.shutdown()
        web_process.terminate()
        await bot.session.close()

if __name__ == '__main__':
    # Упрощенный запуск для Bothost
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Agent stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
