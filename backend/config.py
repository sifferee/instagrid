"""InstaGrid — настройки проекта."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "instagrid.db"
PROFILES_DIR = ROOT / "profiles"
LOGS_DIR = ROOT / "logs"
CONTENT_DIR = ROOT / "content"

for d in (DATA_DIR, PROFILES_DIR, LOGS_DIR, CONTENT_DIR):
    d.mkdir(exist_ok=True)

HOST = os.environ.get("INSTAGRID_HOST", "127.0.0.1")
PORT = int(os.environ.get("INSTAGRID_PORT", "8000"))

# Claude API для автогенерации описаний (опционально)
# Можно задать через config.env или переменную окружения
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")

# Загружаем config.env если есть
_env_file = ROOT / "config.env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key == "CLAUDE_API_KEY" and val:
                CLAUDE_API_KEY = val
