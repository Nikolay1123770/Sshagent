#!/usr/bin/env python3
"""
SSH Server Monitoring Agent - FIXED VERSION
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
from fastapi import FastAPI, Request, HTTPException
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
    BOT_TOKEN = os.getenv('BOT_TOKEN', '')
    ADMIN_IDS = list(filter(None, map(str.strip, os.getenv('ADMIN_IDS', '').split(','))))
    ADMIN_IDS = [int(x) for x in ADMIN_IDS if x.isdigit()]
    WEB_PORT = int(os.getenv('PORT', '8000'))
    DB_PATH = os.getenv('DB_PATH', '/app/data/agent.db')
    CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '120'))


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
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
                server['host'], port=server['port'], username=server['username'],
                password=server['password'], known_hosts=None, connect_timeout=timeout
            ) as conn:
                result = await asyncio.wait_for(conn.run(command), timeout=timeout)
                return result.stdout or '', result.stderr or '', result.exit_status
        except Exception as e:
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
                'cpu_usage': cpu_usage, 'mem_usage': mem_usage, 'disk_usage': disk_usage,
                'load_avg': load_avg, 'uptime': uptime, 'status': status
            }
        except Exception as e:
            logger.error(f"Failed to get metrics: {e}")
            return None


# ============= ИНИЦИАЛИЗАЦИЯ =============

db = Database(Config.DB_PATH)
ssh = SSHManager()
scheduler = AsyncIOScheduler()

if Config.BOT_TOKEN:
    bot = Bot(token=Config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    router = Router()
    dp.include_router(router)
else:
    bot = None

app = FastAPI(title="SSH Agent", docs_url=None, redoc_url=None)


# ============= TELEGRAM BOT =============

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
            builder.row(InlineKeyboardButton(text=f"{emoji} {s['name']}", callback_data=f"srv_{s['id']}"))
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
            "👋 Привет!\n\n🖥 SSH Server Agent\n📱 Telegram + 🌐 Web интерфейс\n\n🌐 Web: https://sshagent.bothost.ru",
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
            await callback.message.edit_text("📭 Нет серверов", reply_markup=main_kb())
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
        text = f"🖥 <b>{server['name']}</b>\n\n📍 {server['host']}:{server['port']}\n👤 {server['username']}\n\n"
        if m:
            text += f"💻 CPU: {m['cpu_usage']:.1f}%\n💾 RAM: {m['mem_usage']:.1f}%\n💿 Disk: {m['disk_usage']:.1f}%\n📈 Load: {m['load_avg']}\n"
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
                metrics['disk_usage'], metrics['load_avg'], metrics['uptime'], metrics['status']
            )
        await show_server(callback)

    @router.callback_query(F.data.startswith("cmd_"))
    async def start_exec(callback: CallbackQuery, state: FSMContext):
        server_id = int(callback.data.split("_")[1])
        await state.update_data(server_id=server_id)
        await state.set_state(ExecCommand.waiting)
        await callback.message.answer("💻 Введите команду:\n\n/cancel для отмены")
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
            reply_markup=confirm_kb(server_id), parse_mode="HTML"
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
        await callback.message.edit_text("➕ Добавление сервера\n\nШаг 1/5: Имя сервера", parse_mode="HTML")
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
        await message.answer("Шаг 5/5: Пароль")

    @router.message(AddServer.password)
    async def add_password(message: Message, state: FSMContext):
        if message.text == "/cancel":
            await state.clear()
            await message.answer("❌ Отменено", reply_markup=main_kb())
            return
        data = await state.get_data()
        await message.delete()
        test_msg = await message.answer("⏳ Проверяю...")
        test_server = {
            'name': data['name'], 'host': data['host'], 'port': data['port'],
            'username': data['username'], 'password': message.text
        }
        _, _, code = await ssh.execute(test_server, 'echo OK')
        if code != 0:
            await test_msg.edit_text("❌ Не удалось подключиться!", reply_markup=main_kb())
            await state.clear()
            return
        server_id = await db.add_server(data['name'], data['host'], data['port'], data['username'], message.text)
        await test_msg.edit_text(f"✅ Сервер <b>{data['name']}</b> добавлен!", parse_mode="HTML")
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
        text = f"📈 Статистика\n\n🖥 Всего: {len(servers)}\n🟢 OK: {healthy}\n🟡 Warning: {warning}\n🔴 Offline: {offline}\n"
        await callback.message.edit_text(text, reply_markup=main_kb())
        await callback.answer()

    @router.callback_query(F.data == "web")
    async def show_web_link(callback: CallbackQuery):
        text = "🌐 Web интерфейс\n\nURL: https://sshagent.bothost.ru\n\n• 📊 Dashboard\n• 💻 Терминал\n• 📁 Файлы"
        await callback.message.edit_text(text, reply_markup=main_kb())
        await callback.answer()


# ============= WEB INTERFACE =============

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

HTML_STYLE = """
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: system-ui; background: #f5f5f5; color: #333; line-height: 1.6; }
.navbar { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 1rem 2rem; }
.navbar h1 { font-size: 1.5rem; }
.container { max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
.stat-card { background: white; padding: 1.5rem; border-radius: 10px; text-align: center; }
.stat-value { font-size: 2.5rem; font-weight: bold; color: #667eea; }
.stat-label { color: #666; margin-top: 0.5rem; }
.servers { display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 1.5rem; }
.server { background: white; padding: 1.5rem; border-radius: 10px; border-left: 4px solid #667eea; }
.server.warning { border-left-color: #f59e0b; }
.server.critical { border-left-color: #ef4444; }
.server h3 { margin-bottom: 0.5rem; }
.server .host { color: #666; font-size: 0.9rem; }
.metrics { margin: 1rem 0; }
.metric { margin: 0.5rem 0; display: flex; justify-content: space-between; align-items: center; }
.metric-label { font-size: 0.85rem; color: #666; }
.metric-value { font-weight: bold; }
.metric-bar { background: #e5e7eb; height: 8px; border-radius: 4px; flex: 1; margin: 0 0.5rem; }
.metric-fill { background: linear-gradient(90deg, #667eea, #764ba2); height: 100%; }
.btn { display: inline-block; padding: 0.5rem 1rem; background: #667eea; color: white; text-decoration: none; border-radius: 5px; margin: 0.25rem; border: none; cursor: pointer; }
.btn:hover { background: #5568d3; }
.btn-secondary { background: #6b7280; }
.terminal { background: #1e1e1e; color: #0f0; border-radius: 10px; padding: 1rem; font-family: monospace; min-height: 400px; }
.terminal-input { background: transparent; border: none; color: #0f0; font-family: monospace; width: 80%; outline: none; }
.alert { padding: 1rem; border-radius: 5px; margin: 1rem 0; border-left: 4px solid #667eea; }
.alert-info { background: #e1f5fe; color: #0277bd; border-color: #03a9f4; }
</style>
"""

@app.get("/health")
async def health():
    servers = await db.get_servers()
    return {"status": "healthy", "timestamp": datetime.now().isoformat(), "servers": len(servers)}

@app.get("/", response_class=HTMLResponse)
async def dashboard():
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
                    <div class="metric-bar"><div class="metric-fill" style="width: {min(m['cpu_usage'], 100)}%"></div></div>
                    <span class="metric-value">{m['cpu_usage']:.1f}%</span>
                </div>
                <div class="metric">
                    <span class="metric-label">RAM</span>
                    <div class="metric-bar"><div class="metric-fill" style="width: {min(m['mem_usage'], 100)}%"></div></div>
                    <span class="metric-value">{m['mem_usage']:.1f}%</span>
                </div>
                <div class="metric">
                    <span class="metric-label">Disk</span>
                    <div class="metric-bar"><div class="metric-fill" style="width: {min(m['disk_usage'], 100)}%"></div></div>
                    <span class="metric-value">{m['disk_usage']:.1f}%</span>
                </div>
                <div style="color: #666; font-size: 0.85rem; margin-top: 0.5rem;">
                    Load: {m['load_avg']} | Uptime: {uptime_days}d {uptime_hours}h
                </div>
            </div>
            """
        else:
            stats['offline'] += 1
            metrics_html = '<div class="alert alert-info">Метрики не доступны</div>'
        
        servers_html += f"""
        <div class="server {status_class}">
            <h3>{server['name']}</h3>
            <div class="host">{server['host']}:{server['port']}</div>
            {metrics_html}
            <div style="margin-top: 1rem;">
                <a href="/terminal/{server['id']}" class="btn">Terminal</a>
                <a href="/files/{server['id']}" class="btn btn-secondary">Files</a>
            </div>
        </div>
        """
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>SSH Agent</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        {HTML_STYLE}
    </head>
    <body>
        <div class="navbar"><h1>🖥 SSH Agent Dashboard</h1></div>
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
                {servers_html if servers_html else '<div class="alert alert-info">Добавьте серверы через Telegram бота</div>'}
            </div>
        </div>
        <script>setTimeout(() => location.reload(), 60000);</script>
    </body>
    </html>
    """

@app.get("/terminal/{server_id}", response_class=HTMLResponse)
async def terminal_page(server_id: int):
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(404)
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Terminal - {server['name']}</title>
        {HTML_STYLE}
    </head>
    <body>
        <div class="navbar"><h1>💻 Terminal: {server['name']}</h1></div>
        <div class="container">
            <div class="terminal">
                <div id="output">Welcome to Terminal<br><br></div>
                <div><span>$ </span><input type="text" class="terminal-input" id="input" autocomplete="off"></div>
            </div>
            <div style="margin-top: 1rem;">
                <button class="btn" onclick="runCmd('df -h')">Disk</button>
                <button class="btn" onclick="runCmd('free -m')">Memory</button>
                <button class="btn" onclick="runCmd('uptime')">Uptime</button>
                <button class="btn btn-secondary" onclick="document.getElementById('output').innerHTML='Cleared<br><br>'">Clear</button>
            </div>
        </div>
        <script>
            const output = document.getElementById('output');
            const input = document.getElementById('input');
            async function runCmd(cmd) {{ input.value = cmd; await exec(); }}
            async function exec() {{
                const cmd = input.value.trim();
                if (!cmd) return;
                output.innerHTML += `$ ${{cmd}}<br>`;
                input.value = '';
                try {{
                    const res = await fetch('/api/exec/{server_id}', {{
                        method: 'POST', headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{command: cmd}})
                    }});
                    const data = await res.json();
                    if (data.stdout) output.innerHTML += `${{data.stdout.replace(/</g, '&lt;').replace(/\\n/g, '<br>')}}`;
                    if (data.stderr) output.innerHTML += `<span style="color: red;">${{data.stderr}}</span><br>`;
                }} catch (e) {{
                    output.innerHTML += `<span style="color: red;">Error: ${{e}}</span><br>`;
                }}
            }}
            input.addEventListener('keydown', e => {{ if (e.key === 'Enter') exec(); }});
            input.focus();
        </script>
    </body>
    </html>
    """

@app.get("/files/{server_id}", response_class=HTMLResponse)
async def files_page(server_id: int):
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(404)
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Files - {server['name']}</title>
        {HTML_STYLE}
        <style>
        .files-table {{ width: 100%; border-collapse: collapse; background: white; }}
        .files-table th {{ background: #f9fafb; padding: 0.75rem; text-align: left; }}
        .files-table td {{ padding: 0.75rem; border-bottom: 1px solid #e5e7eb; }}
        .files-table tr:hover {{ background: #f9fafb; }}
        </style>
    </head>
    <body>
        <div class="navbar"><h1>📁 Files: {server['name']}</h1></div>
        <div class="container">
            <div style="background: white; padding: 1rem; border-radius: 10px;">
                <div style="margin-bottom: 1rem;">
                    <button class="btn" onclick="load('/')">Home</button>
                    <button class="btn" onclick="goUp()">Up</button>
                    <input type="text" id="path" value="/" readonly style="width: 300px; padding: 0.5rem;">
                    <button class="btn" onclick="load(document.getElementById('path').value)">Refresh</button>
                </div>
                <table class="files-table">
                    <thead><tr><th>Name</th><th>Size</th><th>Modified</th></tr></thead>
                    <tbody id="files"><tr><td colspan="3">Loading...</td></tr></tbody>
                </table>
            </div>
        </div>
        <script>
            let currentPath = '/';
            async function load(path) {{
                currentPath = path;
                document.getElementById('path').value = path;
                try {{
                    const res = await fetch('/api/files/{server_id}/list?path=' + encodeURIComponent(path));
                    const data = await res.json();
                    const tbody = document.getElementById('files');
                    if (!data.files || data.files.length === 0) {{
                        tbody.innerHTML = '<tr><td colspan="3">Empty directory</td></tr>';
                        return;
                    }}
                    tbody.innerHTML = data.files.map(f => `
                        <tr>
                            <td><a href="#" onclick="${{f.is_dir ? `load('${{f.path}}')` : `editFile('${{f.path}}')`}}; return false;">
                                ${{f.is_dir ? '📁' : '📄'}} ${{f.name}}</a></td>
                            <td>${{f.size}}</td>
                            <td>${{f.date}}</td>
                        </tr>
                    `).join('');
                }} catch (e) {{
                    document.getElementById('files').innerHTML = '<tr><td colspan="3">Error: ' + e + '</td></tr>';
                }}
            }}
            function goUp() {{
                if (currentPath === '/') return;
                const parts = currentPath.split('/').filter(p => p);
                parts.pop();
                load(parts.length ? '/' + parts.join('/') : '/');
            }}
            async function editFile(path) {{
                try {{
                    const res = await fetch('/api/files/{server_id}/read?path=' + encodeURIComponent(path));
                    const data = await res.json();
                    alert('File content:\\n\\n' + data.content.substring(0, 1000) + (data.content.length > 1000 ? '...' : ''));
                }} catch (e) {{
                    alert('Error reading file: ' + e);
                }}
            }}
            load('/');
        </script>
    </body>
    </html>
    """

@app.post("/api/exec/{server_id}")
async def api_exec(server_id: int, request: Request):
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(404)
    data = await request.json()
    stdout, stderr, code = await ssh.execute(server, data.get('command', ''))
    return {'stdout': stdout, 'stderr': stderr, 'exit_code': code}

@app.get("/api/files/{server_id}/list")
async def api_list_files(server_id: int, path: str = "/"):
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(404)
    cmd = f"ls -lAh '{path}' 2>/dev/null || ls -lAh '{path}'"
    stdout, stderr, code = await ssh.execute(server, cmd)
    if code != 0:
        raise HTTPException(400, stderr)
    files = []
    for line in stdout.strip().split('\n')[1:]:
        if not line.strip():
            continue
        parts = line.split(None, 8)
        if len(parts) >= 9:
            files.append({
                'name': parts[8], 'size': parts[4], 'date': f"{parts[5]} {parts[6]}",
                'is_dir': parts[0].startswith('d'), 'path': f"{path.rstrip('/')}/{parts[8]}"
            })
    return {'path': path, 'files': files}

@app.get("/api/files/{server_id}/read")
async def api_read_file(server_id: int, path: str):
    server = await db.get_server(server_id)
    if not server:
        raise HTTPException(404)
    stdout, stderr, code = await ssh.execute(server, f"cat '{path}'")
    if code != 0:
        raise HTTPException(400, stderr)
    return {'content': stdout, 'path': path}

async def monitor_all_servers():
    logger.info("🔍 Running monitoring...")
    try:
        servers = await db.get_servers()
        for server in servers:
            try:
                metrics = await ssh.get_metrics(server)
                if metrics:
                    await db.save_metrics(
                        server['id'], metrics['cpu_usage'], metrics['mem_usage'],
                        metrics['disk_usage'], metrics['load_avg'], metrics['uptime'], metrics['status']
                    )
                    logger.info(f"✅ {server['name']}: CPU {metrics['cpu_usage']:.1f}%")
            except Exception as e:
                logger.error(f"❌ Error monitoring {server['name']}: {e}")
        if bot:
            await send_alerts()
    except Exception as e:
        logger.error(f"❌ Monitoring error: {e}")

async def send_alerts():
    alerts = await db.get_unsent_alerts()
    for alert in alerts:
        emoji = "⚠️" if alert['level'] == 'warning' else "🚨"
        text = f"{emoji} <b>{alert['server_name']}</b>\n\n{alert['message']}"
        for admin_id in Config.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
                await db.mark_alert_sent(alert['id'])
            except Exception as e:
                logger.error(f"❌ Alert send error: {e}")

@app.on_event("startup")
async def startup():
    await db.init()
    logger.info("🌐 FastAPI ready!")

async def start_telegram():
    if not bot:
        return
    logger.info("🤖 Starting Telegram...")
    scheduler.add_job(monitor_all_servers, 'interval', seconds=Config.CHECK_INTERVAL)
    scheduler.start()
    await dp.start_polling(bot)

async def start_web():
    logger.info(f"🌐 Starting web on {Config.WEB_PORT}...")
    config = uvicorn.Config(app, host="0.0.0.0", port=Config.WEB_PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    logger.info("🚀 SSH AGENT STARTING")
    await asyncio.gather(start_telegram(), start_web(), return_exceptions=True)

if __name__ == '__main__':
    asyncio.run(main()
