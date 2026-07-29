"""InstaGrid — настройки проекта (единственный источник путей и конфигурации)."""
import logging
import logging.handlers
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "instagrid.db"
PROFILES_DIR = ROOT / "profiles"
LOGS_DIR = ROOT / "logs"
CONTENT_DIR = ROOT / "content"
VIDEOS_DIR = CONTENT_DIR / "videos"
STORY_PHOTOS_DIR = CONTENT_DIR / "story_photos"

for d in (DATA_DIR, PROFILES_DIR, LOGS_DIR, CONTENT_DIR, VIDEOS_DIR, STORY_PHOTOS_DIR):
    d.mkdir(parents=True, exist_ok=True)

HOST = os.environ.get("INSTAGRID_HOST", "127.0.0.1")
PORT = int(os.environ.get("INSTAGRID_PORT", "8000"))

# Claude API для автогенерации описаний (опционально)
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")

# Human interactor speed multiplier (INSTAGRID_SPEED=0.5 → всё вдвое быстрее, для тестов)
SPEED_MULTIPLIER = float(os.environ.get("INSTAGRID_SPEED", "1.0"))

# Мобильный прокси: макс аккаунтов на один пул
MOBILE_POOL_MAX_ACCOUNTS = 45

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


# ─── Логирование: консоль + файл с ротацией ─────────────────────────────────

def setup_logging():
    """Настраивает логирование: консоль INFO + файл DEBUG с ротацией 10MB × 5."""
    root = logging.getLogger()
    if root.handlers:
        return  # уже настроено
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Консоль — INFO+
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    # Файл — DEBUG+, ротация 10MB × 5 файлов
    log_file = LOGS_DIR / "instagrid.log"
    file_h = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(fmt)
    root.addHandler(file_h)
