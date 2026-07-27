"""InstaGrid — запуск сервера одной командой."""
import subprocess
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    # Установка зависимостей если нужно
    try:
        import fastapi
    except ImportError:
        print("[InstaGrid] Устанавливаю зависимости...")
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "-r",
            str(ROOT / "requirements.txt"), "--quiet"
        ])

    # Проверяем есть ли билд фронтенда
    frontend_dist = ROOT / "frontend" / "dist"
    if not frontend_dist.exists():
        print("[InstaGrid] Фронтенд не собран. Собираю...")
        frontend_dir = ROOT / "frontend"
        if (frontend_dir / "package.json").exists():
            npm = "npm.cmd" if os.name == "nt" else "npm"
            subprocess.check_call([npm, "install"], cwd=str(frontend_dir))
            subprocess.check_call([npm, "run", "build"], cwd=str(frontend_dir))
        else:
            print("[InstaGrid] ВНИМАНИЕ: frontend/package.json не найден")

    # Запуск сервера
    print()
    print("=" * 50)
    print("  InstaGrid запущен!")
    print("  Открой в браузере: http://127.0.0.1:8000")
    print("=" * 50)
    print()

    import uvicorn
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
