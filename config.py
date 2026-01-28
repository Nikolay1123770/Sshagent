import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Telegram
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(',')))
    
    # Database
    DB_PATH = os.getenv('DB_PATH', 'agent.db')
    
    # Monitoring
    CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '60'))
    
    # Alerts
    CPU_WARNING = int(os.getenv('CPU_WARNING', '80'))
    CPU_CRITICAL = int(os.getenv('CPU_CRITICAL', '95'))
    MEM_WARNING = int(os.getenv('MEM_WARNING', '85'))
    MEM_CRITICAL = int(os.getenv('MEM_CRITICAL', '95'))
    DISK_WARNING = int(os.getenv('DISK_WARNING', '85'))
    DISK_CRITICAL = int(os.getenv('DISK_CRITICAL', '95'))


# Проверка конфигурации
def validate_config():
    if not Config.BOT_TOKEN:
        raise ValueError("BOT_TOKEN не установлен!")
    if not Config.ADMIN_IDS:
        raise ValueError("ADMIN_IDS не установлен!")