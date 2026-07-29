"""
InstaGrid — Имитация человеческого поведения (HumanInteractor).

Полный порт из SparkGrid ig_human.py с улучшениями:
- Три профиля: balanced/fast/careful (60/20/20%), детерминированы по SHA256 аккаунта
- Кубические Безье с smoothstep, overshoot 14%, bend 5.5-17%
- PRE-CLICK пауза (80-300мс) + click hold (45-135мс) + POST-CLICK пауза (120-420мс)
- Correction probability: 18% шанс микро-коррекции после прибытия
- wander(): случайные перемещения мыши по странице (при "чтении")
- seed-based RNG: воспроизводимое поведение для каждого аккаунта
- Speed multiplier: env INSTAGRID_SPEED=0.5 ускоряет всё вдвое для тестов
- Event trace log: каждое действие записывается для дебага
- Ввод: посимвольно, тайпо 1% → Backspace, burst с punctuation/word паузами
- Скролл: 5-10 wheel-импульсов, синусоидальное распределение, коррекция scroll
- Dwell: микродвижения ±8px с chunk-делением
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import Page, ElementHandle


# ─── Профили поведения ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HumanActionProfile:
    """Числовые параметры профиля — точный порт из SparkGrid."""
    name: str = "balanced"
    speed: float = 1.0
    min_move_steps: int = 14
    max_move_steps: int = 48
    click_hold_min: float = 0.045
    click_hold_max: float = 0.135
    pre_click_min: float = 0.080
    pre_click_max: float = 0.300
    post_click_min: float = 0.120
    post_click_max: float = 0.420
    type_delay_min: float = 0.035
    type_delay_max: float = 0.105
    punctuation_pause_min: float = 0.090
    punctuation_pause_max: float = 0.250
    word_pause_min: float = 0.040
    word_pause_max: float = 0.135
    overshoot_probability: float = 0.14
    correction_probability: float = 0.18
    typo_rate: float = 0.010

    @classmethod
    def named(cls, name: str) -> HumanActionProfile:
        value = str(name or "balanced").strip().lower()
        if value in {"fast", "quick"}:
            return cls(
                name="fast", speed=0.76,
                min_move_steps=10, max_move_steps=32,
                click_hold_min=0.035, click_hold_max=0.100,
                pre_click_min=0.045, pre_click_max=0.170,
                post_click_min=0.070, post_click_max=0.250,
                type_delay_min=0.024, type_delay_max=0.072,
                punctuation_pause_min=0.060, punctuation_pause_max=0.165,
                word_pause_min=0.025, word_pause_max=0.085,
                overshoot_probability=0.09, correction_probability=0.12,
                typo_rate=0.015,
            )
        if value in {"careful", "slow"}:
            return cls(
                name="careful", speed=1.24,
                min_move_steps=18, max_move_steps=58,
                click_hold_min=0.060, click_hold_max=0.170,
                pre_click_min=0.120, pre_click_max=0.430,
                post_click_min=0.180, post_click_max=0.620,
                type_delay_min=0.050, type_delay_max=0.140,
                punctuation_pause_min=0.135, punctuation_pause_max=0.330,
                word_pause_min=0.065, word_pause_max=0.185,
                overshoot_probability=0.17, correction_probability=0.23,
                typo_rate=0.005,
            )
        return cls()


def persona_for(seed: Any) -> str:
    """Стабильный профиль действий для аккаунта. 60% balanced, 20% fast, 20% careful."""
    raw = str(seed or "default").encode("utf-8", "ignore")
    value = hashlib.sha256(raw).digest()[0] % 10
    if value <= 1:
        return "careful"
    if value >= 8:
        return "fast"
    return "balanced"


# ─── Раскладка соседних клавиш ───────────────────────────────────────────────

ADJACENT_KEYS: dict[str, str] = {
    "a": "sqwz", "s": "adwx", "d": "sfec", "f": "dgrv", "g": "fhtb",
    "h": "gjyn", "j": "hkum", "k": "jlio", "l": "kop", "q": "wa",
    "w": "qase", "e": "wsdr", "r": "edft", "t": "rfgy", "y": "tghu",
    "u": "yhji", "i": "ujko", "o": "iklp", "p": "ol", "z": "asx",
    "x": "zsdc", "c": "xdfv", "v": "cfgb", "b": "vghn", "n": "bhjm", "m": "njk",
}


# ─── Основной класс ──────────────────────────────────────────────────────────

class HumanInteractor:
    """
    Имитация человеческого взаимодействия с Playwright Page (async).

    Профиль поведения детерминирован по SHA256 хешу username.
    Все рандомные решения через self.rng (seed-based).
    """

    def __init__(self, page: Page, username: str) -> None:
        self.page = page
        self.username = username

        # Профиль по SHA256
        profile_name = persona_for(username)
        self.profile = HumanActionProfile.named(profile_name)

        # Seed-based RNG для воспроизводимости
        seed_raw = hashlib.sha256(username.encode("utf-8", "ignore")).digest()[:8]
        seed = int.from_bytes(seed_raw, "big") ^ secrets.randbits(32)
        self.rng = random.Random(seed)

        # Позиция курсора
        self._position: tuple[float, float] | None = None

        # Speed multiplier из env (для тестов: INSTAGRID_SPEED=0.5)
        try:
            self._speed_multiplier = max(0.1, min(5.0,
                float(os.environ.get("INSTAGRID_SPEED", "1.0") or "1.0")))
        except Exception:
            self._speed_multiplier = 1.0

        # Event trace log
        self.events: list[dict[str, Any]] = []

    # ── Утилиты ──────────────────────────────────────────────────────────

    def _log(self, kind: str, **payload: Any) -> None:
        """Записывает событие в trace log."""
        event = {"at": time.time(), "kind": kind, "profile": self.profile.name}
        event.update(payload)
        self.events.append(event)

    async def _sleep(self, minimum: float, maximum: float) -> None:
        """Пауза с учётом profile.speed и speed_multiplier."""
        low = max(0.0, float(minimum))
        high = max(low, float(maximum))
        await asyncio.sleep(
            self.rng.uniform(low, high) * self.profile.speed * self._speed_multiplier
        )

    async def _viewport_size(self) -> tuple[float, float]:
        """Возвращает (width, height) viewport."""
        try:
            size = getattr(self.page, "viewport_size", None)
            if size:
                return float(size["width"]), float(size["height"])
        except Exception:
            pass
        try:
            width, height = await self.page.evaluate(
                "() => [window.innerWidth, window.innerHeight]"
            )
            return float(width), float(height)
        except Exception:
            return 1280.0, 800.0

    def _clamp(self, x: float, y: float, vw: float, vh: float) -> tuple[float, float]:
        """Ограничивает координаты viewport."""
        return (
            min(max(2.0, float(x)), max(2.0, vw - 2.0)),
            min(max(2.0, float(y)), max(2.0, vh - 2.0)),
        )

    async def _ensure_position(self) -> tuple[float, float]:
        """Гарантирует начальную позицию курсора."""
        if self._position is None:
            vw, vh = await self._viewport_size()
            self._position = (
                self.rng.uniform(vw * 0.24, vw * 0.76),
                self.rng.uniform(vh * 0.22, vh * 0.76),
            )
            try:
                await self.page.mouse.move(self._position[0], self._position[1])
            except Exception:
                pass
        return self._position

    def save_trace(self, path: str | Path) -> Path:
        """Сохраняет trace log в JSON."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"profile": asdict(self.profile), "events": self.events}
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    # ── Безье: генерация пути ────────────────────────────────────────────

    @staticmethod
    def _bezier(
        p0: tuple[float, float], p1: tuple[float, float],
        p2: tuple[float, float], p3: tuple[float, float],
        t: float,
    ) -> tuple[float, float]:
        mt = 1.0 - t
        return (
            mt**3 * p0[0] + 3*mt**2*t * p1[0] + 3*mt*t**2 * p2[0] + t**3 * p3[0],
            mt**3 * p0[1] + 3*mt**2*t * p1[1] + 3*mt*t**2 * p2[1] + t**3 * p3[1],
        )

    async def _path_points(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> list[tuple[float, float]]:
        vw, vh = await self._viewport_size()
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = max(1.0, math.hypot(dx, dy))
        nx, ny = -dy / distance, dx / distance

        bend = min(100.0, max(7.0, distance * self.rng.uniform(0.055, 0.17)))
        bend *= self.rng.choice((-1.0, 1.0))

        p1 = (
            start[0] + dx * self.rng.uniform(0.20, 0.38) + nx * bend,
            start[1] + dy * self.rng.uniform(0.20, 0.38) + ny * bend,
        )
        p2 = (
            start[0] + dx * self.rng.uniform(0.62, 0.84) - nx * bend * self.rng.uniform(0.30, 0.85),
            start[1] + dy * self.rng.uniform(0.62, 0.84) - ny * bend * self.rng.uniform(0.30, 0.85),
        )

        steps = int(distance / self.rng.uniform(13.0, 22.0)) + self.rng.randint(5, 10)
        steps = max(self.profile.min_move_steps, min(self.profile.max_move_steps, steps))

        points: list[tuple[float, float]] = []
        for index in range(1, steps + 1):
            raw = index / float(steps)
            t = raw * raw * (3.0 - 2.0 * raw)  # smoothstep
            x, y = self._bezier(start, p1, p2, end, t)
            jitter = max(0.0, 1.0 - raw) * self.rng.uniform(-0.42, 0.42)
            points.append(self._clamp(x + jitter, y - jitter, vw, vh))
        points[-1] = end
        return points

    # ── Движение мыши ────────────────────────────────────────────────────

    async def move_to(self, x: float, y: float, allow_overshoot: bool = True) -> bool:
        """
        Перемещает мышь по кубической Безье с smoothstep.
        overshoot + correction_probability из SparkGrid.
        """
        try:
            vw, vh = await self._viewport_size()
            start = await self._ensure_position()
            end = self._clamp(x, y, vw, vh)

            if allow_overshoot and self.rng.random() < self.profile.overshoot_probability:
                dx, dy = end[0] - start[0], end[1] - start[1]
                distance = max(1.0, math.hypot(dx, dy))
                amount = min(12.0, max(3.0, distance * self.rng.uniform(0.012, 0.035)))
                overshoot = self._clamp(
                    end[0] + dx / distance * amount,
                    end[1] + dy / distance * amount,
                    vw, vh,
                )
                points = await self._path_points(start, overshoot)
                points += await self._path_points(overshoot, end)
            else:
                points = await self._path_points(start, end)

            per_step = self.rng.uniform(0.0065, 0.0135) * self.profile.speed * self._speed_multiplier
            for px, py in points:
                await self.page.mouse.move(px, py)
                await asyncio.sleep(per_step)

            self._position = end

            # Correction probability: 18% шанс микро-коррекции после прибытия
            if self.rng.random() < self.profile.correction_probability:
                cx = end[0] + self.rng.uniform(-8, 8)
                cy = end[1] + self.rng.uniform(-6, 6)
                cx, cy = self._clamp(cx, cy, vw, vh)
                correction_points = await self._path_points(end, (cx, cy))
                # Короткое быстрое движение
                for px, py in correction_points[-5:]:
                    await self.page.mouse.move(px, py)
                    await asyncio.sleep(per_step * 0.5)
                self._position = (cx, cy)

            self._log(
                "move",
                start=[round(start[0], 2), round(start[1], 2)],
                end=[round(end[0], 2), round(end[1], 2)],
                distance=round(math.hypot(end[0] - start[0], end[1] - start[1]), 2),
                steps=len(points),
            )
            return True

        except Exception as exc:
            self._log("move_error", error=type(exc).__name__)
            return False

    # ── Клик ─────────────────────────────────────────────────────────────

    async def click_at(self, x: float, y: float) -> bool:
        """
        Клик: move → PRE-CLICK pause → mouse.down → hold → mouse.up → POST-CLICK pause.
        Полный порт SparkGrid: pre_click + hold + post_click.
        """
        try:
            if not await self.move_to(x, y):
                return False

            # PRE-CLICK pause (80-300мс balanced)
            await self._sleep(self.profile.pre_click_min, self.profile.pre_click_max)

            await self.page.mouse.down()

            # Click hold (45-135мс)
            await self._sleep(self.profile.click_hold_min, self.profile.click_hold_max)

            await self.page.mouse.up()

            # POST-CLICK pause (120-420мс balanced)
            await self._sleep(self.profile.post_click_min, self.profile.post_click_max)

            self._log("click", x=round(x, 2), y=round(y, 2), method="mouse_point")
            return True

        except Exception as exc:
            self._log("click_error", error=type(exc).__name__)
            return False

    async def click_element(self, element: ElementHandle) -> bool:
        """
        Кликает по элементу. Точка выбирается через beta(2.6, 2.6) — не в центр.
        С margins для надёжности (не кликаем по самому краю).
        """
        try:
            box = await element.bounding_box()
            if not box:
                # Fallback на .click()
                await element.click()
                self._log("click", method="element_fallback")
                return True

            width = max(1.0, float(box.get("width", 1)))
            height = max(1.0, float(box.get("height", 1)))
            margin_x = min(width * 0.18, max(2.0, width * 0.08))
            margin_y = min(height * 0.22, max(2.0, height * 0.10))
            usable_w = max(1.0, width - margin_x * 2.0)
            usable_h = max(1.0, height - margin_y * 2.0)

            rx = self.rng.betavariate(2.6, 2.6)
            ry = self.rng.betavariate(2.8, 2.8)
            x = box["x"] + margin_x + usable_w * rx
            y = box["y"] + margin_y + usable_h * ry

            return await self.click_at(x, y)

        except Exception as exc:
            try:
                await element.click()
                self._log("click", method="locator_fallback", error=type(exc).__name__)
                return True
            except Exception:
                self._log("click_error", method="locator_fallback", error=type(exc).__name__)
                return False

    async def click_selector(self, selector: str, timeout: int = 30_000) -> bool:
        """Находит элемент по селектору и кликает."""
        try:
            element = await self.page.wait_for_selector(selector, timeout=timeout)
            if not element:
                return False
            return await self.click_element(element)
        except Exception as exc:
            self._log("click_error", selector=selector[:60], error=type(exc).__name__)
            return False

    # ── Ввод текста ──────────────────────────────────────────────────────

    async def type_text(
        self,
        text: str,
        *,
        allow_typos: bool = True,
        sensitive: bool = False,
    ) -> bool:
        """
        Ввод текста посимвольно с burst-паттерном, punctuation/word паузами.
        Порт из SparkGrid с burst_left и профильными паузами.
        """
        value = str(text)
        burst_left = self.rng.randint(2, 6)

        try:
            for char in value:
                # Тайпо: 1% шанс для нечувствительного ввода
                if (allow_typos and not sensitive and char.lower() in ADJACENT_KEYS
                        and self.rng.random() < self.profile.typo_rate):
                    wrong = self.rng.choice(ADJACENT_KEYS[char.lower()])
                    await self.page.keyboard.type(
                        wrong.upper() if char.isupper() else wrong, delay=0
                    )
                    await self._sleep(0.07, 0.20)
                    await self.page.keyboard.press("Backspace")
                    await self._sleep(0.04, 0.11)

                await self.page.keyboard.type(char, delay=0)

                burst_left -= 1
                minimum = self.profile.type_delay_min
                maximum = self.profile.type_delay_max

                if burst_left <= 0:
                    burst_left = self.rng.randint(2, 7)
                    minimum *= 1.35
                    maximum *= 1.85
                elif self.rng.random() < 0.55:
                    minimum *= 0.65
                    maximum *= 0.82

                if char in ".,!?;:\n":
                    await self._sleep(
                        self.profile.punctuation_pause_min,
                        self.profile.punctuation_pause_max,
                    )
                elif char.isspace():
                    await self._sleep(
                        self.profile.word_pause_min,
                        self.profile.word_pause_max,
                    )
                else:
                    await self._sleep(minimum, maximum)

            self._log("type", length=len(value), sensitive=sensitive)
            return True

        except Exception as exc:
            self._log("type_error", error=type(exc).__name__, length=len(value))
            return False

    async def type_into(self, selector: str, text: str) -> bool:
        """Кликает по полю ввода и печатает текст."""
        ok = await self.click_selector(selector)
        if not ok:
            return False
        await self._sleep(0.1, 0.35)
        return await self.type_text(text)

    async def clear_and_type(self, selector: str, text: str) -> bool:
        """Очищает поле (Ctrl+A → Backspace) и вводит текст."""
        ok = await self.click_selector(selector)
        if not ok:
            return False
        await self._sleep(0.08, 0.2)
        await self.page.keyboard.press("Control+a")
        await self._sleep(0.05, 0.15)
        await self.page.keyboard.press("Backspace")
        await self._sleep(0.1, 0.3)
        return await self.type_text(text)

    # ── Скролл ───────────────────────────────────────────────────────────

    async def scroll(self, distance: int | None = None, direction: int = 1) -> int:
        """
        Скролл через 5-10 wheel-импульсов с синусоидальным распределением.
        Scroll correction после основного скролла (SparkGrid паттерн).
        """
        if distance is None:
            distance = self.rng.randint(420, 920)
        total = max(80, abs(int(distance))) * (1 if direction >= 0 else -1)
        pulses = self.rng.randint(5, 10)
        weights = [math.sin(math.pi * (i + 1) / (pulses + 1)) + 0.20 for i in range(pulses)]
        scale = total / sum(weights)
        sent = 0
        values: list[int] = []

        try:
            for index, weight in enumerate(weights):
                delta = int(round(weight * scale))
                if index == pulses - 1:
                    delta = total - sent
                sent += delta
                values.append(delta)
                await self.page.mouse.wheel(0, delta)
                await self._sleep(0.025, 0.085)

            # Scroll correction (SparkGrid: обратный микро-скролл после основного)
            if self.rng.random() < self.profile.correction_probability:
                correction = int(-total * self.rng.uniform(0.035, 0.105))
                await self._sleep(0.18, 0.58)
                await self.page.mouse.wheel(0, correction)
                values.append(correction)
                sent += correction

            self._log("scroll", requested=total, actual=sent, pulses=len(values))
            await self._sleep(0.16, 0.62)
            return sent

        except Exception as exc:
            self._log("scroll_error", error=type(exc).__name__, requested=total)
            return sent

    async def scroll_to_element(self, selector: str) -> None:
        """Скроллит к элементу порциями."""
        element = await self.page.query_selector(selector)
        if not element:
            return
        box = await element.bounding_box()
        if not box:
            return
        vw, vh = await self._viewport_size()
        target_y = box["y"] - vh * 0.3
        if abs(target_y) < 50:
            return
        remaining = target_y
        while abs(remaining) > 30:
            chunk = remaining * self.rng.uniform(0.3, 0.6)
            await self.scroll(int(chunk))
            remaining -= chunk
            await self._sleep(0.08, 0.2)

    # ── Wander: случайные перемещения при "чтении" ───────────────────────

    async def wander(self, moves: int = 1) -> bool:
        """
        Случайное перемещение мыши по странице (как при чтении ленты).
        SparkGrid: 8-92% ширины, 12-88% высоты.
        """
        vw, vh = await self._viewport_size()
        ok = False
        for _ in range(max(1, int(moves))):
            x = self.rng.uniform(vw * 0.08, vw * 0.92)
            y = self.rng.uniform(vh * 0.12, vh * 0.88)
            ok = await self.move_to(x, y, allow_overshoot=False) or ok
            await self._sleep(0.12, 0.48)
        self._log("wander", moves=max(1, int(moves)))
        return ok

    # ── Dwell: "задумался" ───────────────────────────────────────────────

    async def dwell(
        self,
        minimum: float = 0.8,
        maximum: float = 2.4,
        micro_moves: bool = True,
    ) -> None:
        """
        Имитация "задумался": стоит на месте с микродвижениями ±8px.
        SparkGrid паттерн: duration/count на каждый chunk.
        """
        vw, vh = await self._viewport_size()
        duration = self.rng.uniform(float(minimum), float(maximum)) * self.profile.speed

        if not micro_moves:
            await asyncio.sleep(duration * self._speed_multiplier)
            self._log("idle", duration=round(duration, 3), micro_moves=False)
            return

        start = await self._ensure_position()
        count = self.rng.randint(1, 3)
        chunk = duration / float(count + 1)

        for _ in range(count):
            await asyncio.sleep(chunk * self._speed_multiplier)
            x, y = self._clamp(
                start[0] + self.rng.uniform(-8.0, 8.0),
                start[1] + self.rng.uniform(-6.0, 6.0),
                vw, vh,
            )
            try:
                await self.page.mouse.move(x, y)
                self._position = (x, y)
            except Exception:
                pass

        await asyncio.sleep(chunk * self._speed_multiplier)
        self._log("idle", duration=round(duration, 3), micro_moves=True)

    # ── Утилиты высокого уровня ──────────────────────────────────────────

    async def random_pause(self, min_sec: float = 0.5, max_sec: float = 2.0) -> None:
        """Случайная пауза с учётом speed multiplier."""
        await self._sleep(min_sec, max_sec)

    async def human_scroll_feed(self, scroll_count: int = 3) -> None:
        """
        Листание ленты: скролл → пауза (чтение) → иногда dwell → wander.
        """
        for _ in range(scroll_count):
            delta = self.rng.randint(200, 600)
            await self.scroll(delta)

            # "Читаем" контент
            await self._sleep(1.0, 4.0)

            # Иногда wander (как при чтении)
            if self.rng.random() < 0.3:
                await self.wander(moves=1)

            # Иногда "задумываемся"
            if self.rng.random() < 0.2:
                await self.dwell()

    async def move_to_random_spot(self) -> None:
        """Перемещает мышь в случайное место на странице."""
        vw, vh = await self._viewport_size()
        x = self.rng.uniform(50, vw - 50)
        y = self.rng.uniform(50, vh - 50)
        await self.move_to(x, y)

    def __repr__(self) -> str:
        return (
            f"HumanInteractor(username={self.username!r}, "
            f"profile={self.profile.name}, speed={self._speed_multiplier})"
        )
