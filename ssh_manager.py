import asyncssh
import asyncio
from typing import Optional, Tuple, Dict
import logging

logger = logging.getLogger(__name__)


class SSHManager:
    def __init__(self):
        self._connections: Dict[int, asyncssh.SSHClientConnection] = {}
        
    async def execute(
        self,
        server: Dict,
        command: str,
        timeout: int = 30
    ) -> Tuple[str, str, int]:
        """Выполнить команду на сервере"""
        try:
            # Параметры подключения
            connect_opts = {
                'host': server['host'],
                'port': server['port'],
                'username': server['username'],
                'known_hosts': None,
                'connect_timeout': timeout,
            }
            
            # Аутентификация
            if server['auth_type'] == 'password' and server['password']:
                connect_opts['password'] = server['password']
            elif server['auth_type'] == 'key' and server['key_path']:
                connect_opts['client_keys'] = [server['key_path']]
                
            # Подключение
            async with asyncssh.connect(**connect_opts) as conn:
                result = await asyncio.wait_for(
                    conn.run(command),
                    timeout=timeout
                )
                return (
                    result.stdout or '',
                    result.stderr or '',
                    result.exit_status
                )
                
        except asyncio.TimeoutError:
            return '', 'Command timeout', -1
        except Exception as e:
            logger.error(f"SSH error for {server['name']}: {e}")
            return '', str(e), -1
            
    async def test_connection(self, server: Dict) -> Tuple[bool, str]:
        """Проверить подключение"""
        stdout, stderr, code = await self.execute(server, 'echo "OK"', timeout=10)
        if code == 0 and 'OK' in stdout:
            return True, "Connection successful"
        return False, stderr or "Connection failed"


class ServerMetrics:
    """Получение метрик сервера"""
    
    def __init__(self, ssh: SSHManager):
        self.ssh = ssh
        
    async def get_all_metrics(self, server: Dict) -> Optional[Dict]:
        """Получить все метрики"""
        try:
            # CPU
            cpu_out, _, cpu_code = await self.ssh.execute(
                server,
                "top -bn1 | grep 'Cpu(s)' | awk '{print $2}' | cut -d'%' -f1"
            )
            cpu_usage = float(cpu_out.strip() or 0)
            
            # Memory
            mem_out, _, _ = await self.ssh.execute(server, "free | grep Mem")
            mem_parts = mem_out.split()
            if len(mem_parts) >= 3:
                mem_total = int(mem_parts[1])
                mem_used = int(mem_parts[2])
                mem_usage = (mem_used / mem_total) * 100
            else:
                mem_usage = 0
                
            # Disk
            disk_out, _, _ = await self.ssh.execute(
                server,
                "df -h / | tail -1 | awk '{print $5}' | cut -d'%' -f1"
            )
            disk_usage = float(disk_out.strip() or 0)
            
            # Load average
            load_out, _, _ = await self.ssh.execute(
                server,
                "cat /proc/loadavg | cut -d' ' -f1-3"
            )
            load_avg = load_out.strip()
            
            # Uptime
            uptime_out, _, _ = await self.ssh.execute(
                server,
                "cat /proc/uptime | cut -d' ' -f1"
            )
            uptime = int(float(uptime_out.strip() or 0))
            
            # Hostname
            hostname_out, _, _ = await self.ssh.execute(server, "hostname")
            hostname = hostname_out.strip()
            
            return {
                'cpu_usage': cpu_usage,
                'mem_usage': mem_usage,
                'disk_usage': disk_usage,
                'load_avg': load_avg,
                'uptime': uptime,
                'hostname': hostname,
                'status': 'healthy' if cpu_usage < 90 and mem_usage < 90 else 'warning'
            }
            
        except Exception as e:
            logger.error(f"Failed to get metrics: {e}")
            return None
            
    async def get_system_info(self, server: Dict) -> str:
        """Получить информацию о системе"""
        commands = [
            ("OS", "cat /etc/os-release | grep PRETTY_NAME | cut -d'\"' -f2"),
            ("Kernel", "uname -r"),
            ("CPU", "lscpu | grep 'Model name' | cut -d':' -f2 | xargs"),
            ("Cores", "nproc"),
        ]
        
        info = []
        for label, cmd in commands:
            stdout, _, code = await self.ssh.execute(server, cmd)
            if code == 0:
                info.append(f"{label}: {stdout.strip()}")
                
        return "\n".join(info)
        
    async def get_top_processes(self, server: Dict, limit: int = 5) -> str:
        """Получить топ процессов"""
        stdout, _, code = await self.ssh.execute(
            server,
            f"ps aux --sort=-%cpu | head -{limit+1}"
        )
        if code == 0:
            return stdout
        return "Failed to get processes"