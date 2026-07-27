"""InstaGrid — настройки проекта."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "instagrid.db"
PROFILES_DIR = ROOT / "profiles"
LOGS_DIR = ROOT / "logs"

for d in (DATA_DIR, PROFILES_DIR, LOGS_DIR):
    d.mkdir(exist_ok=True)

HOST = os.environ.get("INSTAGRID_HOST", "127.0.0.1")
PORT = int(os.environ.get("INSTAGRID_PORT", "8000"))
