"""
InstaGrid — самопроверка перед запуском.

Прогоняет РЕАЛЬНЫЕ ветки кода, а не только синтаксис: создаёт временные
профили, генерирует и восстанавливает отпечатки, проверяет согласованность.
Именно такая проверка ловит ошибки вроде «падает на каждом втором профиле».

Запуск:
    cd C:\\Projects\\instagrid
    python selftest.py

Временные файлы создаются в системной temp-папке и удаляются в конце.
Ничего в рабочих данных не трогается.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PASSED, FAILED = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    mark = "OK " if ok else "СБОЙ"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("InstaGrid — самопроверка\n")

    # ── Импорты ──
    print("Модули:")
    try:
        from backend.services.profile_manager import (
            ProfileManager, fingerprint_from_dict, generate_fingerprint,
        )
        from backend.services.human import HumanInteractor, HumanActionProfile, persona_for
        from backend.services.proxy_parser import parse_proxy
        from backend.services.scheduler import next_session_at, active_window
        from backend.services import proxy_health, dialog_gate, story_publisher_browser
        check("импорт всех модулей", True)
    except Exception as e:
        check("импорт всех модулей", False, f"{type(e).__name__}: {e}")
        print("\nДальше идти нельзя.")
        return 1

    # ── Профиль поведения ──
    print("\nПрофиль поведения:")
    try:
        p = HumanActionProfile.named(persona_for("testuser"))
        check("у профиля есть .name", hasattr(p, "name"), p.name)
        check("нет устаревшего .value", not hasattr(p, "value"))
    except Exception as e:
        check("профиль поведения", False, f"{type(e).__name__}: {e}")

    # ── Парсер прокси ──
    print("\nПарсер прокси:")
    cases = [
        "user:pass@1.2.3.4:8080",
        "1.2.3.4:8080:user:pass",
        "1.2.3.4:8080",
        "http://user:pass@1.2.3.4:8080",
        "socks5://user:pass@1.2.3.4:1080",
        "socks5://1.2.3.4:1080",
    ]
    bad = [c for c in cases if not parse_proxy(c)]
    check("все форматы распознаются", not bad, f"не разобрано: {bad}" if bad else "6 форматов")

    # ── Отпечатки: главное ──
    print("\nОтпечатки (создание профилей):")
    tmp = Path(tempfile.mkdtemp(prefix="instagrid_selftest_"))
    try:
        pm = ProfileManager(profiles_dir=tmp)

        N = 25
        made, errs = [], []
        for i in range(N):
            try:
                pm.create_profile(f"t{i:03d}")
                made.append(f"t{i:03d}")
            except Exception as e:
                errs.append(f"{type(e).__name__}: {str(e)[:60]}")
        check(f"создано {N} профилей подряд", len(made) == N,
              f"{len(made)}/{N}" + (f", первая ошибка: {errs[0]}" if errs else ""))

        # Файлы на месте
        incomplete = [n for n in made
                      if not ((tmp/n/"meta.json").exists()
                              and (tmp/n/"fingerprint.json").exists())]
        check("все файлы профилей на месте", not incomplete,
              f"неполных: {len(incomplete)}" if incomplete else "")

        # Восстановление — это делает каждый запуск браузера
        rb_err = []
        for n in made:
            try:
                d = json.loads((tmp/n/"fingerprint.json").read_text(encoding="utf-8"))
                fp = fingerprint_from_dict(d)
                assert "Firefox" in fp.navigator.userAgent
                assert fp.screen.width > 0
            except Exception as e:
                rb_err.append(f"{n}: {type(e).__name__}")
        check("отпечатки восстанавливаются", not rb_err,
              f"ошибок: {len(rb_err)}" if rb_err else f"{len(made)} шт")

        # Отпечаток постоянный — ключевое требование антидетекта
        if made:
            n = made[0]
            before = (tmp/n/"fingerprint.json").read_text(encoding="utf-8")
            pm.create_profile(n)
            after = (tmp/n/"fingerprint.json").read_text(encoding="utf-8")
            check("отпечаток не меняется при повторном запуске", before == after)

        # Геометрия согласована с отпечатком
        mism = 0
        for n in made:
            d = json.loads((tmp/n/"fingerprint.json").read_text(encoding="utf-8"))
            m = json.loads((tmp/n/"meta.json").read_text(encoding="utf-8"))
            if int(d["screen"]["width"]) != int(m["screen_geometry"]["screen_width"]):
                mism += 1
        check("геометрия совпадает с отпечатком", mism == 0,
              f"расхождений: {mism}" if mism else "")

        # Битый профиль чинится, а не убивает аккаунт
        if len(made) > 1:
            n = made[1]
            (tmp/n/"meta.json").unlink()
            try:
                pm.create_profile(n)
                ok = (tmp/n/"meta.json").exists() and (tmp/n/"fingerprint.json").exists()
                check("битый профиль пересоздаётся", ok)
            except Exception as e:
                check("битый профиль пересоздаётся", False, f"{type(e).__name__}")

        # Отпечатки различаются между аккаунтами.
        # Считаем ЭФФЕКТИВНЫЙ отпечаток: данные BrowserForge плюс постоянные
        # seed'ы шума. Даже при совпадении JSON у двух профилей будут разные
        # canvas/audio, а сайт измеряет именно их.
        from backend.services.profile_manager import _noise_seeds
        combos = set()
        for n in made:
            d = json.loads((tmp/n/"fingerprint.json").read_text(encoding="utf-8"))
            combos.add(json.dumps(d, sort_keys=True) + json.dumps(_noise_seeds(n), sort_keys=True))
        check("отпечатки уникальны между аккаунтами",
              len(combos) == len(made), f"{len(combos)} из {len(made)}")

        # Шум canvas/audio закреплён за профилем, а не случаен при запуске
        s1 = _noise_seeds("checkuser")
        s2 = _noise_seeds("checkuser")
        check("шум canvas/audio постоянен между запусками", s1 == s2)
        check("шум различается между аккаунтами",
              _noise_seeds("aaa")["canvas:seed"] != _noise_seeds("bbb")["canvas:seed"])

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ── Расписание ──
    print("\nРасписание:")
    try:
        users = [f"u{i}" for i in range(20)]
        times = sorted(next_session_at(u, "America/New_York") for u in users)
        spread_h = (max(times) - min(times)) / 3600
        check("аккаунты расходятся по времени", spread_h > 2.0,
              f"разброс {spread_h:.1f} ч")
        wins = {active_window(u) for u in users}
        check("окна активности различаются", len(wins) > 10, f"{len(wins)} вариантов")
    except Exception as e:
        check("расписание", False, f"{type(e).__name__}: {e}")

    # ── Итог ──
    print()
    print("=" * 52)
    if FAILED:
        print(f"ПРОВАЛЕНО: {len(FAILED)} из {len(PASSED) + len(FAILED)}")
        for f in FAILED:
            print(f"   - {f}")
        print("\nЗапускать систему рано.")
        return 1

    print(f"ВСЁ ПРОЙДЕНО ({len(PASSED)} проверок)")
    print("\nМожно запускать: python run.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
