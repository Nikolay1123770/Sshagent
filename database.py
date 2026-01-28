import aiosqlite
from typing import List, Optional, Dict
from datetime import datetime
import json


class Database:
    def __init__(self, db_path: str = 'agent.db'):
        self.db_path = db_path
        
    async def init(self):
        """Инициализация БД"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS servers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER DEFAULT 22,
                    username TEXT NOT NULL,
                    auth_type TEXT NOT NULL,
                    password TEXT,
                    key_path TEXT,
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
            
            await db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    is_admin INTEGER DEFAULT 0,
                    notifications INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await db.commit()
            
    async def add_server(
        self, 
        name: str,
        host: str,
        username: str,
        auth_type: str,
        port: int = 22,
        password: Optional[str] = None,
        key_path: Optional[str] = None
    ) -> int:
        """Добавить сервер"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                '''INSERT INTO servers 
                   (name, host, port, username, auth_type, password, key_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (name, host, port, username, auth_type, password, key_path)
            )
            await db.commit()
            return cursor.lastrowid
            
    async def get_servers(self, enabled_only: bool = True) -> List[Dict]:
        """Получить список серверов"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = 'SELECT * FROM servers'
            if enabled_only:
                query += ' WHERE enabled = 1'
            
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
                
    async def get_server(self, server_id: int) -> Optional[Dict]:
        """Получить сервер по ID"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM servers WHERE id = ?', 
                (server_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
                
    async def delete_server(self, server_id: int):
        """Удалить сервер"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('DELETE FROM servers WHERE id = ?', (server_id,))
            await db.commit()
            
    async def save_metrics(
        self,
        server_id: int,
        cpu_usage: float,
        mem_usage: float,
        disk_usage: float,
        load_avg: str,
        uptime: int,
        status: str
    ):
        """Сохранить метрики"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                '''INSERT INTO metrics 
                   (server_id, cpu_usage, mem_usage, disk_usage, load_avg, uptime, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (server_id, cpu_usage, mem_usage, disk_usage, load_avg, uptime, status)
            )
            await db.commit()
            
    async def get_latest_metrics(self, server_id: int) -> Optional[Dict]:
        """Получить последние метрики сервера"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                '''SELECT * FROM metrics 
                   WHERE server_id = ? 
                   ORDER BY timestamp DESC 
                   LIMIT 1''',
                (server_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
                
    async def add_alert(
        self,
        server_id: int,
        level: str,
        message: str
    ) -> int:
        """Добавить алерт"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'INSERT INTO alerts (server_id, level, message) VALUES (?, ?, ?)',
                (server_id, level, message)
            )
            await db.commit()
            return cursor.lastrowid
            
    async def get_unsent_alerts(self) -> List[Dict]:
        """Получить неотправленные алерты"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                '''SELECT a.*, s.name as server_name 
                   FROM alerts a
                   JOIN servers s ON a.server_id = s.id
                   WHERE a.sent = 0
                   ORDER BY a.created_at ASC'''
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
                
    async def mark_alert_sent(self, alert_id: int):
        """Отметить алерт как отправленный"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'UPDATE alerts SET sent = 1 WHERE id = ?',
                (alert_id,)
            )
            await db.commit()
            
    async def add_user(self, user_id: int, username: str, first_name: str, is_admin: bool = False):
        """Добавить пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                '''INSERT OR REPLACE INTO users 
                   (user_id, username, first_name, is_admin)
                   VALUES (?, ?, ?, ?)''',
                (user_id, username, first_name, 1 if is_admin else 0)
            )
            await db.commit()
            
    async def is_user_allowed(self, user_id: int) -> bool:
        """Проверить доступ пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                'SELECT is_admin FROM users WHERE user_id = ?',
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return bool(row and row[0])