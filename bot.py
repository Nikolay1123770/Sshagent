#!/usr/bin/env python3
"""
SSH Server Monitoring Agent
Telegram Bot + Web Interface - Single File for Bothost
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

# Telegram
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Web
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware

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
    WEB_PORT = int(os.getenv('PORT', '8000'))
    
    # Database
    DB_PATH = os.getenv('DB_PATH', '/app/data/agent.db')
    
    # Monitoring
    CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '120'))


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
                return result.stdout or '', result.stderr or '', result.exit_status
        except asyncio.TimeoutError:
            return '', 'Timeout', -1
        except Exception as e:
            logger.error(f"SSH error: {e}")
            return '', str(e), -1
            
    async def get_metrics(self, server):
        try:
            cpu_cmd = "top -bn1 | grep 'Cpu(s)' | awk '{print $2}' | sed 's/%us,//'"
            cpu_out, _, _ = await self.execute(server, cpu_cmd)
            cpu_usage = float(cpu_out.strip() or 0)
            
            mem_cmd = "free | grep Mem | awk '{print ($3/$2) * 100.0}'"
            mem_out, _, _ = await self.execute(server, mem_cmd)
            mem_usage = float(mem_out.strip() or 0)
            
            disk_cmd = "df -h / | tail -1 | awk '{print $5}' | sed 's/%//'"
            disk_out, _, _ = await self.execute(server, disk_cmd)
            disk_usage = float(disk_out.strip() or 0)
            
            load_cmd = "cat /proc/loadavg | cut -d' ' -f1-3"
            load_out, _, _ = await self.execute(server, load_cmd)
            load_avg = load_out.strip()
            
            uptime_cmd = "cat /proc/uptime | cut -d' ' -f1"
            uptime_out, _, _ = await self.execute(server, uptime_cmd)
            uptime = int(float(uptime_out.strip() or 0))
            
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


# ============= ИНИЦИАЛИЗАЦИЯ =============

db = Database(Config.DB_PATH)
ssh = SSHManager()
scheduler = AsyncIOScheduler()

# Telegram Bot
if Config.BOT_TOKEN:
    bot = Bot(token=Config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    router = Router()
    dp.include_router(router)
else:
    bot = None
    dp = None

# FastAPI Web
app = FastAPI(title="SSH Agent", docs_url=None, redoc_url=None)


# ============= WEB MIDDLEWARE =============

class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = datetime.now()
        logger.info(f"🌐 {request.method} {request.url}")
        
        try:
            response = await call_next(request)
            process_time = (datetime.now() - start_time).total_seconds()
            logger.info(f"✅ {response.status_code} ({process_time:.3f}s)")
            return response
        except Exception as e:
            logger.error(f"❌ HTTP Error: {e}")
            raise

app.add_middleware(LoggingMiddleware)


# ============= TELEGRAM BOT HANDLERS =============

if bot:
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
        
        await message.answer(
            f"👋 Привет!\n\n"
            "🖥 SSH Server Agent\n"
            "📱 Telegram + 🌐 Web интерфейс\n\n"
            f"🌐 Web: https://sshagent.bothost.ru",
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
        msg = await message.answer("⏳ Выполняю...")
        stdout, stderr, code = await ssh.execute(server, message.text)
        result = f"💻 <code>{message.text}</code>\n📤 Exit: {code}\n\n"
        if stdout:
            result += f"<pre>{stdout[:3000]}</pre>"
        if stderr:
            result += f"\n<b>Error:</b>\n<pre>{stderr[:1000]}</pre>"
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
            await state.update_data(port=port)
            await state.set_state(AddServer.username)
            await message.answer("Шаг 4/5: Username")
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
        await message.answer("Шаг 5/5: Пароль\n\n⚠️ Сообщение будет удалено")

    @router.message(AddServer.password)
    async def add_password(message: Message, state: FSMContext):
        if message.text == "/cancel":
            await state.clear()
            await message.answer("❌ Отменено", reply_markup=main_kb())
            return
        data = await state.get_data()
        await message.delete()
        test_msg = await message.answer("⏳ Проверяю подключение...")
        test_server = {
            'name': data['name'], 'host': data['host'],
            'port': data['port'], 'username': data['username'],
            'password': message.text
        }
        _, _, code = await ssh.execute(test_server, 'echo OK')
        if code != 0:
            await test_msg.edit_text(
                "❌ Не удалось подключиться!",
                reply_markup=main_kb()
            )
            await state.clear()
            return
        server_id = await db.add_server(
            data['name'], data['host'], data['port'],
            data['username'], message.text
        )
        await test_msg.edit_text(f"✅ Сервер <b>{data['name']}</b> добавлен!", parse_mode="HTML")
        metrics = await ssh.get_metrics(test_server)
        if metrics:
            await db.save_metrics(
                server_id, metrics['cpu_usage'], metrics['mem_usage'],
                metrics['disk_usage'], metrics['load_avg'],
                metrics['uptime'], metrics['status']
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

    @router.callback_query(F.data == "web")
    async def show_web_link(callback: CallbackQuery):
        web_url = "https://sshagent.bothost.ru"
        text = f"🌐 <b>Web интерфейс</b>\n\n"
        text += f"URL: <code>{web_url}</code>\n\n"
        text += "Функции:\n"
        text += "• 📊 Dashboard с метриками\n"
        text += "• 💻 Терминал для команд\n"
        text += "• 📁 Файловый менеджер\n"
        await callback.message.edit_text(text, reply_markup=main_kb(), parse_mode="HTML")
        await callback.answer()


# ============= WEB INTERFACE =============

HTML_STYLE = """
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { 
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f5f5f5; color: #333; line-height: 1.6;
}
.navbar {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white; padding: 1rem 2rem; box-shadow: 0 2px 10px rgba(0,0,0,0.1);
}
.navbar h1 { font-size: 1.5rem; }
.container { max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }
.stats { 
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); 
    gap: 1rem; margin-bottom: 2rem; 
}
.stat-card {
    background: white; padding: 1.5rem; border-radius: 10px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.05); text-align: center;
}
.stat-value { font-size: 2.5rem; font-weight: bold; color: #667eea; }
.stat-label { color: #666; margin-top: 0.5rem; }
.servers { 
    display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); 
    gap: 1.5rem; 
}
.server {
    background: white; padding: 1.5rem; border-radius: 10px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.05); border-left: 4px solid #667eea;
}
.server.warning { border-left-color: #f59e0b; }
.server.critical { border-left-color: #ef4444; }
.server h3 { margin-bottom: 0.5rem; }
.server .host { color: #666; font-size: 0.9rem; }
.metrics { margin: 1rem 0; }
.metric { margin: 0.5rem 0; display: flex; justify-content: space-between; align-items: center; }
.metric-label { font-size: 0.85rem; color: #666; }
.metric-value { font-weight: bold; }
.metric-bar {
    background: #e5e7eb; height: 8px; border-radius: 4px; overflow: hidden;
    flex: 1; margin: 0 0.5rem;
}
.metric-fill {
    background: linear-gradient(90deg, #667eea, #764ba2); height: 100%;
    transition: width 0.3s;
}
.metric-fill.warning { background: #f59e0b; }
.metric-fill.critical { background: #ef4444; }
.btn {
    display: inline-block; padding: 0.5rem 1rem; background: #667eea;
    color: white; text-decoration: none; border-radius: 5px; border: none;
    cursor: pointer; margin: 0.25rem; transition: background 0.3s;
}
.btn:hover { background: #5568d3; }
.btn-secondary { background: #6b7280; }
.btn-secondary:hover { background: #4b5563; }
.terminal {
    background: #1e1e1e; color: #0f0; border-radius: 10px; padding: 1rem;
    font-family: 'Courier New', monospace; min-height: 400px;
}
.terminal-input {
    background: transparent; border: none; color: #0f0; 
    font-family: 'Courier New', monospace; width: 80%; outline: none;
}
.alert { 
    padding: 1rem; border-radius: 5px; margin: 1rem 0; 
    border-left: 4px solid #667eea;
}
.alert-info { background: #e1f5fe; color: #0277bd; border-color: #03a9f4; }
.alert-success { background: #e8f5e8; color: #2e7d32; border-color: #4caf50; }
.alert-warning { background: #fff8e1; color: #f57c00; border-color: #ff9800; }
.alert-error { background: #ffebee; color: #c62828; border-color: #f44336; }
</style>
"""

@app.get("/health")
async def health():
    """Health check"""
    servers = await db.get_servers()
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "servers": len(servers),
        "message": "SSH Agent is running"
    }

@app.get("/ping")
async def ping():
    """Simple ping"""
    return {"ping": "pong", "time": datetime.now().isoformat()}

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Главная страница"""
    servers = await db.get_servers()
    stats = {'total': len(servers), 'online': 0, 'warning': 0, 'offline': 0}
    
    servers_html = ""
    for server in servers:
        m = await db.get_latest_metrics(server['id'])
        status_class = ""
        metrics_html = ""
        
        if m:
            if m['status'] == 'healthy':
                stats['online'] += 1
                status_class = ""
            elif m['status'] == 'warning':
                stats['warning'] += 1
                status_class = "warning"
            else:
                stats['offline'] += 1
                status_class = "critical"
            
            uptime_days = m['uptime'] // 86400
            uptime_hours = (m['uptime'] % 86400) // 3600
            
            metrics_html = f"""
            <div class="metrics">
                <div class="metric">
                    <span class="metric-label">CPU</span>
                    <div class="metric-bar">
                        <div class="metric-fill {'warning' if m['cpu_usage'] > 80 else 'critical' if m['cpu_usage'] > 95 else ''}" 
                             style="width: {min(m['cpu_usage'], 100)}%"></div>
                    </div>
                    <span class="metric-value">{m['cpu_usage']:.1f}%</span>
                </div>
                <div class="metric">
                    <span class="metric-label">RAM</span>
                    <div class="metric-bar">
                        <div class="metric-fill {'warning' if m['mem_usage'] > 85 else 'critical' if m['mem_usage'] > 95 else ''}" 
                             style="width: {min(m['mem_usage'], 100)}%"></div>
                    </div>
                    <span class="metric-value">{m['mem_usage']:.1f}%</span>
                </div>
                <div class="metric">
                    <span class="metric-label">Disk</span>
                    <div class="metric-bar">
                        <div class="metric-fill {'warning' if m['disk_usage'] > 85 else 'critical' if m['disk_usage'] > 95 else ''}" 
                             style="width: {min(m['disk_usage'], 100)}%"></div>
                    </div>
                    <span class="metric-value">{m['disk_usage']:.1f}%</span>
                </div>
                <div style="color: #666; font-size: 0.85rem; margin-top: 0.5rem;">
                    Load: {m['load_avg']} | Uptime: {uptime_days}d {uptime_hours}h
                </div>
                <div style="color: #999; font-size: 0.8rem;">
                    Last update: {m['timestamp'][:16]}
                </div>
            </div>
            """
        else:
            stats['offline'] += 1
            status_class = "critical"
            metrics_html = """
            <div class="alert alert-warning">
                <strong>Метрики не доступны</strong><br>
                Сервер может быть недоступен или ещё не проверялся
            </div>
            """
        
        servers_html += f"""
        <div class="server {status_class}">
            <h3>{server['name']}</h3>
            <div class="host">{server['host']}:{server['port']}</div>
            {metrics_html}
            <div style="margin-top: 1rem;">
                <a href="/terminal/{server['id']}" class="btn">💻 Terminal</a>
                <a href="/files/{server['id']}" class="btn btn-secondary">📁 Files</a>
                <button onclick="location.reload()" class="btn btn-secondary">🔄 Refresh</button>
            </div>
        </div>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <title>SSH Agent Dashboard</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        {HTML_STYLE}
    </head>
    <body>
        <div class="navbar">
            <h1>🖥 SSH Agent Dashboard</h1>
        </div>
        
        <div class="container">
            <div class="stats">
                <div class="stat-card">
                    <div class="stat-value">{stats['online']}</div>
                    <div class="stat-label">🟢 Online</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{stats['warning']}</div>
                    <div class="stat-label">🟡 Warning</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{stats['offline']}</div>
                    <div class="stat-label">🔴 Offline</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{stats['total']}</div>
                    <div class="stat-label">📊 Total</div>
                </div>
            </div>
            
            <h2>Servers</h2>
            <div class="servers">
                {servers_html if servers_html else '''
                <div class="alert alert-info">
                    <strong>Нет серверов</strong><br>
                    Добавьте серверы через Telegram бота: <a href="https://t.me/sshAgent_bothost_bot" target="_blank">@sshAgent_bothost_bot</a>
                </div>
                '''}
            </div>
        </div>
        
        <script>
            // Auto-refresh every minute
            setTimeout(() => location.reload(), 60000);
            
            // Show loading indicator
            document.addEventListener('DOMContentLoaded', () => {
                console.log('SSH Agent Dashboard loaded');
                console.log('Servers: {stats["total"]}');
            });
        </script>
    </body>
    </html>
    """
    return html

@app.get("/terminal/{server_id}", response_class=HTMLResponse)
async def terminal_page(server_id: int):
    """Страница терминала"""
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Terminal - {server['name']}</title>
        <meta charset="utf-8">
        {HTML_STYLE}
    </head>
    <body>
        <div class="navbar">
            <h1>💻 Terminal: {server['name']}</h1>
            <span style="float: right;">
                <a href="/" style="color: white; text-decoration: none;">🏠 Dashboard</a>
            </span>
        </div>
        
        <div class="container">
            <div class="alert alert-info">
                <strong>Connected to:</strong> {server['username']}@{server['host']}:{server['port']}
            </div>
            
            <div class="terminal" id="terminal">
                <div id="output">Welcome to SSH Agent Terminal<br>Type commands and press Enter<br><br></div>
                <div style="display: flex; align-items: center;">
                    <span style="color: #0f0;">$ </span>
                    <input type="text" class="terminal-input" id="input" autocomplete="off" autofocus>
                </div>
            </div>
            
            <div style="margin-top: 1rem; background: white; padding: 1rem; border-radius: 10px;">
                <h3>Quick Commands</h3>
                <button class="btn" onclick="runCommand('df -h')">📊 Disk Usage</button>
                <button class="btn" onclick="runCommand('free -m')">💾 Memory</button>
                <button class="btn" onclick="runCommand('top -bn1 | head -20')">📈 Processes</button>
                <button class="btn" onclick="runCommand('uptime')">⏱ Uptime</button>
                <button class="btn" onclick="runCommand('ps aux --sort=-%cpu | head -10')">🔥 Top CPU</button>
                <button class="btn btn-secondary" onclick="clearTerminal()">🗑 Clear</button>
            </div>
        </div>
        
        <script>
            const serverId = {server_id};
            const output = document.getElementById('output');
            const input = document.getElementById('input');
            
            async function runCommand(cmd) {{
                input.value = cmd;
                await executeCommand();
            }}
            
            async function executeCommand() {{
                const command = input.value.trim();
                if (!command) return;
                
                output.innerHTML += `<span style="color: #00ff00;">$ ${{command}}</span><br>`;
                input.value = '';
                
                try {{
                    const response = await fetch(`/api/exec/${{serverId}}`, {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{command}})
                    }});
                    
                    const data = await response.json();
                    
                    if (data.stdout) {{
                        output.innerHTML += `<span style="color: #ccc;">${{escapeHtml(data.stdout)}}</span><br>`;
                    }}
                    if (data.stderr) {{
                        output.innerHTML += `<span style="color: #ff5555;">${{escapeHtml(data.stderr)}}</span><br>`;
                    }}
                    output.innerHTML += `<span style="color: #666;">Exit code: ${{data.exit_code}}</span><br><br>`;
                    
                }} catch (e) {{
                    output.innerHTML += `<span style="color: #ff5555;">Error: ${{e.message}}</span><br><br>`;
                }}
                
                document.getElementById('terminal').scrollTop = document.getElementById('terminal').scrollHeight;
            }}
            
            function clearTerminal() {{
                output.innerHTML = 'Terminal cleared<br><br>';
            }}
            
            function escapeHtml(text) {{
                const div = document.createElement('div');
                div.textContent = text;
                return div.innerHTML.replace(/\\n/g, '<br>');
            }}
            
            input.addEventListener('keydown', (e) => {{
                if (e.key === 'Enter') {{
                    executeCommand();
                }}
            }});
            
            // Focus input
            input.focus();
        </script>
    </body>
    </html>
    """
    return html

@app.get("/files/{server_id}", response_class=HTMLResponse)
async def files_page(server_id: int):
    """Страница файлового менеджера"""
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Files - {server['name']}</title>
        <meta charset="utf-8">
        {HTML_STYLE}
        <style>
            .files-table {{ width: 100%; border-collapse: collapse; background: white; }}
            .files-table th {{ background: #f9fafb; padding: 0.75rem; text-align: left; border-bottom: 2px solid #e5e7eb; }}
            .files-table td {{ padding: 0.75rem; border-bottom: 1px solid #e5e7eb; }}
            .files-table tr:hover {{ background: #f9fafb; }}
            .file-icon {{ margin-right: 0.5rem; }}
            .modal {{
                display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                background: rgba(0,0,0,0.5); z-index: 1000;
            }}
            .modal.active {{ display: flex; align-items: center; justify-content: center; }}
            .modal-content {{
                background: white; padding: 2rem; border-radius: 10px;
                max-width: 90%; max-height: 90%; overflow: auto;
            }}
            .modal-content textarea {{
                width: 100%; min-height: 400px; font-family: 'Courier New', monospace;
                padding: 1rem; border: 1px solid #ddd; border-radius: 5px;
            }}
        </style>
    </head>
    <body>
        <div class="navbar">
            <h1>📁 Files: {server['name']}</h1>
            <span style="float: right;">
                <a href="/" style="color: white; text-decoration: none;">🏠 Dashboard</a>
            </span>
        </div>
        
        <div class="container">
            <div style="background: white; border-radius: 10px; padding: 1.5rem;">
                <div style="display: flex; justify-content: space-between; margin-bottom: 1rem; align-items: center;">
                    <div style="display: flex; gap: 0.5rem; align-items: center;">
                        <button class="btn" onclick="navigate('/')">🏠 Home</button>
                        <button class="btn" onclick="goUp()">⬆️ Up</button>
                        <input type="text" id="current-path" value="/" readonly 
                               style="padding: 0.5rem; border: 1px solid #ddd; border-radius: 5px; width: 300px;">
                        <button class="btn" onclick="loadFiles(document.getElementById('current-path').value)">🔄 Refresh</button>
                    </div>
                </div>
                
                <table class="files-table">
                    <thead>
                        <tr>
                            <th>📄 Name</th>
                            <th>📏 Size</th>
                            <th>📅 Modified</th>
                            <th>🔐 Permissions</th>
                            <th>⚙️ Actions</th>
                        </tr>
                    </thead>
                    <tbody id="files-body">
                        <tr><td colspan="5" style="text-align: center; padding: 2rem;">Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
        
        <div id="editor-modal" class="modal">
            <div class="modal-content">
                <h3 id="editor-title">Edit File</h3>
                <textarea id="editor-content"></textarea>
                <div style="margin-top: 1rem;">
                    <button class="btn" onclick="saveFile()">💾 Save</button>
                    <button class="btn btn-secondary" onclick="closeEditor()">❌ Cancel</button>
                </div>
            </div>
        </div>
        
        <script>
            const serverId = {server_id};
            let currentPath = '/';
            let editingFile = null;
            
            async function loadFiles(path = currentPath) {{
                currentPath = path;
                document.getElementById('current-path').value = currentPath;
                
                const tbody = document.getElementById('files-body');
                tbody.innerHTML = '<tr><td colspan="5" style="text-align: center;">⏳ Loading...</td></tr>';
                
                try {{
                    const response = await fetch(`/api/files/${{serverId}}/list?path=${{encodeURIComponent(currentPath)}}`);
                    const data = await response.json();
                    
                    if (!data.files || data.files.length === 0) {{
                        tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #666;">📭 Empty directory</td></tr>';
                        return;
                    }}
                    
                    tbody.innerHTML = data.files.map(file => `
                        <tr>
                            <td>
                                <span class="file-icon">${{file.is_dir ? '📁' : '📄'}}</span>
                                <a href="#" onclick="${{file.is_dir ? `navigate('${{file.path}}')` : `viewFile('${{file.path}}')`}}; return false;"
                                   style="text-decoration: none; color: #667eea;">
                                    ${{file.name}}
                                </a>
                            </td>
                            <td>${{file.size}}</td>
                            <td>${{file.date}}</td>
                            <td><code style="font-size: 0.8rem;">${{file.permissions}}</code></td>
                            <td>
                                ${{!file.is_dir ? `<button class="btn" style="padding: 0.25rem 0.5rem; font-size: 0.8rem;" onclick="editFile('${{file.path}}')">✏️ Edit</button>` : ''}}
                                <button class="btn btn-secondary" style="padding: 0.25rem 0.5rem; font-size: 0.8rem;" onclick="downloadFile('${{file.path}}', '${{file.name}}')">⬇️</button>
                            </td>
                        </tr>
                    `).join('');
                }} catch (e) {{
                    tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: red;">❌ Error: ${{e.message}}</td></tr>`;
                }}
            }}
            
            function navigate(path) {{
                loadFiles(path);
            }}
            
            function goUp() {{
                if (currentPath === '/') return;
                const parts = currentPath.split('/').filter(p => p);
                parts.pop();
                navigate(parts.length ? '/' + parts.join('/') : '/');
            }}
            
            async function editFile(path) {{
                editingFile = path;
                document.getElementById('editor-title').textContent = 'Edit: ' + path.split('/').pop();
                
                try {{
                    const response = await fetch(`/api/files/${{serverId}}/read?path=${{encodeURIComponent(path)}}`);
                    const data = await response.json();
                    document.getElementById('editor-content').value = data.content;
                    document.getElementById('editor-modal').classList.add('active');
                }} catch (e) {{
                    alert('❌ Error reading file: ' + e.message);
                }}
            }}
            
            function viewFile(path) {{
                editFile(path);
            }}
            
            async function saveFile() {{
                if (!editingFile) return;
                
                const content = document.getElementById('editor-content').value;
                
                try {{
                    await fetch(`/api/files/${{serverId}}/write`, {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{path: editingFile, content}})
                    }});
                    
                    alert('✅ File saved!');
                    closeEditor();
                }} catch (e) {{
                    alert('❌ Error saving: ' + e.message);
                }}
            }}
            
            function closeEditor() {{
                document.getElementById('editor-modal').classList.remove('active');
                editingFile = null;
            }}
            
            async function downloadFile(path, name) {{
                try {{
                    const response = await fetch(`/api/files/${{serverId}}/download?path=${{encodeURIComponent(path)}}`);
                    const blob = await response.blob();
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = name;
                    a.click();
                    window.URL.revokeObjectURL(url);
                }} catch (e) {{
                    alert('❌ Error downloading: ' + e.message);
                }}
            }}
            
            // Close modal on background click
            document.getElementById('editor-modal').addEventListener('click', (e) => {{
                if (e.target.id === 'editor-modal') closeEditor();
            }});
            
            // Initial load
            loadFiles();
        </script>
    </body>
    </html>
    """
    return html

# API Endpoints

@app.post("/api/exec/{server_id}")
async def api_exec(server_id: int, request: Request):
    """Выполнить команду"""
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    
    data = await request.json()
    command = data.get('command', '')
    
    if not command:
        raise HTTPException(400, "Command required")
    
    stdout, stderr, code = await ssh.execute(server, command)
    return {
        'stdout': stdout,
        'stderr': stderr,
        'exit_code': code
    }

@app.get("/api/files/{server_id}/list")
async def api_list_files(server_id: int, path: str = "/"):
    """Список файлов"""
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    
    cmd = f"ls -lAh --time-style=long-iso '{path}' 2>/dev/null || ls -lAh '{path}'"
    stdout, stderr, code = await ssh.execute(server, cmd)
    
    if code != 0:
        raise HTTPException(400, stderr or "Failed to list directory")
    
    files = []
    lines = stdout.strip().split('\n')
    if len(lines) > 1:  # Skip header line if present
        for line in lines[1:]:
            if not line.strip():
                continue
            parts = line.split(None, 8)
            if len(parts) >= 9:
                files.append({
                    'name': parts[8],
                    'size': parts[4],
                    'date': f"{parts[5]} {parts[6]}",
                    'permissions': parts[0],
                    'is_dir': parts[0].startswith('d'),
                    'path': f"{path.rstrip('/')}/{parts[8]}"
                })
    
    return {'path': path, 'files': files}

@app.get("/api/files/{server_id}/read")
async def api_read_file(server_id: int, path: str):
    """Прочитать файл"""
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    
    # Check file size first
    size_cmd = f"stat -c %s '{path}' 2>/dev/null || stat -f %z '{path}' 2>/dev/null"
    size_out, _, _ = await ssh.execute(server, size_cmd)
    
    try:
        file_size = int(size_out.strip())
        if file_size > 10 * 1024 * 1024:  # 10MB limit
            raise HTTPException(400, "File too large (max 10MB)")
    except:
        pass  # Ignore size check errors
    
    stdout, stderr, code = await ssh.execute(server, f"cat '{path}'", timeout=60)
    if code != 0:
        raise HTTPException(400, stderr or "Failed to read file")
    
    return {'content': stdout, 'path': path}

@app.post("/api/files/{server_id}/write")
async def api_write_file(server_id: int, request: Request):
    """Записать файл"""
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    
    data = await request.json()
    path = data.get('path')
    content = data.get('content', '')
    
    if not path:
        raise HTTPException(400, "Path required")
    
    # Escape content for shell
    content_escaped = content.replace("'", "'\\''")
    
    # Write using a temporary file approach
    tmp_file = f"/tmp/ssh_agent_{os.urandom(8).hex()}"
    cmd = f"echo -n '{content_escaped}' > {tmp_file} && mv {tmp_file} '{path}'"
    
    _, stderr, code = await ssh.execute(server, cmd)
    if code != 0:
        raise HTTPException(400, stderr or "Failed to write file")
    
    return {'success': True}

@app.get("/api/files/{server_id}/download")
async def api_download_file(server_id: int, path: str):
    """Скачать файл"""
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    
    # Get file content via base64 to handle binary files
    cmd = f"cat '{path}' | base64"
    stdout, stderr, code = await ssh.execute(server, cmd, timeout=120)
    
    if code != 0:
        raise HTTPException(400, stderr or "Failed to read file")
    
    try:
        content = base64.b64decode(stdout.strip())
    except Exception:
        raise HTTPException(500, "Failed to decode file")
    
    # Determine content type
    mime_type, _ = mimetypes.guess_type(path)
    
    return StreamingResponse(
        iter([content]),
        media_type=mime_type or 'application/octet-stream',
        headers={
            'Content-Disposition': f'attachment; filename="{Path(path).name}"'
        }
    )


# ============= МОНИТОРИНГ =============

async def monitor_all_servers():
    """Фоновый мониторинг всех серверов"""
    logger.info("🔍 Running monitoring check...")
    
    try:
        servers = await db.get_servers()
        logger.info(f"📊 Monitoring {len(servers)} servers")
        
        for server in servers:
            try:
                metrics = await ssh.get_metrics(server)
                
                if not metrics:
                    await db.add_alert(
                        server['id'],
                        'critical',
                        f"Сервер {server['name']} недоступен!"
                    )
                    logger.warning(f"❌ {server['name']} is unreachable")
                    continue
                
                # Save metrics
                await db.save_metrics(
                    server['id'],
                    metrics['cpu_usage'],
                    metrics['mem_usage'],
                    metrics['disk_usage'],
                    metrics['load_avg'],
                    metrics['uptime'],
                    metrics['status']
                )
                
                logger.info(f"✅ {server['name']}: CPU {metrics['cpu_usage']:.1f}% | RAM {metrics['mem_usage']:.1f}% | Disk {metrics['disk_usage']:.1f}%")
                
                # Check thresholds and create alerts
                if metrics['cpu_usage'] > 95:
                    await db.add_alert(
                        server['id'],
                        'critical',
                        f"CPU: {metrics['cpu_usage']:.1f}% (критично!)"
                    )
                elif metrics['cpu_usage'] > 80:
                    await db.add_alert(
                        server['id'],
                        'warning',
                        f"CPU: {metrics['cpu_usage']:.1f}% (высокая нагрузка)"
                    )
                
                if metrics['mem_usage'] > 95:
                    await db.add_alert(
                        server['id'],
                        'critical',
                        f"RAM: {metrics['mem_usage']:.1f}% (критично!)"
                    )
                elif metrics['mem_usage'] > 85:
                    await db.add_alert(
                        server['id'],
                        'warning',
                        f"RAM: {metrics['mem_usage']:.1f}% (высокое использование)"
                    )
                
                if metrics['disk_usage'] > 95:
                    await db.add_alert(
                        server['id'],
                        'critical',
                        f"Диск: {metrics['disk_usage']:.1f}% (почти заполнен!)"
                    )
                elif metrics['disk_usage'] > 85:
                    await db.add_alert(
                        server['id'],
                        'warning',
                        f"Диск: {metrics['disk_usage']:.1f}% (заканчивается место)"
                    )
                    
            except Exception as e:
                logger.error(f"❌ Error monitoring {server['name']}: {e}")
                
        # Send pending alerts
        if bot:
            await send_alerts()
            
    except Exception as e:
        logger.error(f"❌ Error in monitoring loop: {e}")

async def send_alerts():
    """Отправка алертов админам"""
    try:
        alerts = await db.get_unsent_alerts()
        
        for alert in alerts:
            emoji = "⚠️" if alert['level'] == 'warning' else "🚨"
            text = f"{emoji} <b>Алерт: {alert['server_name']}</b>\n\n"
            text += f"{alert['message']}\n"
            text += f"🕐 {alert['created_at'][:19]}"
            
            for admin_id in Config.ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, text, parse_mode="HTML")
                    await db.mark_alert_sent(alert['id'])
                    logger.info(f"📤 Alert sent to {admin_id}")
                except Exception as e:
                    logger.error(f"❌ Failed to send alert to {admin_id}: {e}")
                    
    except Exception as e:
        logger.error(f"❌ Error sending alerts: {e}")


# ============= STARTUP EVENTS =============

@app.on_event("startup")
async def startup():
    """FastAPI startup"""
    logger.info("🌐 FastAPI starting up...")
    
    # Initialize database
    await db.init()
    
    # Check servers
    servers = await db.get_servers()
    logger.info(f"📊 Found {len(servers)} servers in database")
    
    logger.info("🎉 FastAPI startup complete!")


# ============= ГЛАВНАЯ ФУНКЦИЯ ЗАПУСКА =============

async def start_telegram_bot():
    """Запуск Telegram бота"""
    if not bot:
        logger.warning("⚠️ Telegram bot disabled (no BOT_TOKEN)")
        return
    
    try:
        logger.info("🤖 Starting Telegram bot...")
        
        # Start scheduler
        scheduler.add_job(
            monitor_all_servers,
            'interval',
            seconds=Config.CHECK_INTERVAL,
            id='monitor'
        )
        scheduler.start()
        logger.info(f"⏰ Scheduler started (interval: {Config.CHECK_INTERVAL}s)")
        
        # Start polling
        await dp.start_polling(bot)
        
    except Exception as e:
        logger.error(f"❌ Telegram bot error: {e}")

async def start_web_server():
    """Запуск веб-сервера"""
    try:
        logger.info(f"🌐 Starting web server on 0.0.0.0:{Config.WEB_PORT}")
        
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=Config.WEB_PORT,
            log_level="info",
            access_log=True
        )
        server = uvicorn.Server(config)
        await server.serve()
        
    except Exception as e:
        logger.error(f"❌ Web server error: {e}")

async def main():
    """Главная функция - запуск всех компонентов"""
    logger.info("=" * 50)
    logger.info("🚀 SSH AGENT STARTING")
    logger.info("=" * 50)
    logger.info(f"💾 Database: {Config.DB_PATH}")
    logger.info(f"🌐 Web port: {Config.WEB_PORT}")
    logger.info(f"🤖 Bot token: {Config.BOT_TOKEN[:10] + '...' if Config.BOT_TOKEN else 'NOT SET'}")
    logger.info(f"👥 Admin IDs: {Config.ADMIN_IDS}")
    logger.info(f"⏰ Check interval: {Config.CHECK_INTERVAL}s")
    logger.info(f"🌍 Web URL: https://sshagent.bothost.ru")
    logger.info("=" * 50)
    
    try:
        # Run both services concurrently
        await asyncio.gather(
            start_telegram_bot(),
            start_web_server(),
            return_exceptions=True
        )
    except KeyboardInterrupt:
        logger.info("👋 Shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Goodbye!")
