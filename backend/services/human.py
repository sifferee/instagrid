"""
InstaGrid — Имитация человеческого поведения (HumanInteractor).

Порт находок из SparkGrid ig_human.py с точными параметрами:
- Три профиля: balanced/fast/careful (60/20/20%), детерминированы по SHA256 аккаунта
- Кубические Безье с smoothstep, overshoot 14%, bend 5.5-17%
- Клик: mouse.down → пауза 45-135мс → mouse.up (НЕ .click())
- Ввод: посимвольно, тайпо 1% на соседнюю клавишу → Backspace, burst 2-7 символов
- Скролл: 5-10 wheel-импульсов, синусоидальное распределение, коррекция 18%
- Dwell: микродвижения ±8px
- Клик по элементу: beta(2.6, 2.6) — не в центр
- Move steps: 14-48 (balanced), масштаб по расстоянию
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import string
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page, ElementHandle, Mouse


# ─── Профили поведения ────────────────────────────────────────────────────────

class BehaviorProfile(Enum):
    BALANCED = "balanced"
    FAST = "fast"
    CAREFUL = "careful"


@dataclass(frozen=True)
class ProfileParams:
    """Числовые параметры для конкретного профиля поведения."""
    # Движение мыши
    move_steps_min: int
    move_steps_max: int
    overshoot_probability: float    # вероятность промаха
    overshoot_factor: float         # на сколько промахивается (доля расстояния)
    bend_min: float                 # мин. отклонение кривой (доля расстояния)
    bend_max: float                 # макс. отклонение кривой
    # Клик
    click_down_delay_min: int       # мс между mouse.down и mouse.up
    click_down_delay_max: int
    # Ввод текста
    char_delay_min: int             # мс между символами (в burst)
    char_delay_max: int
    burst_min: int                  # символов в серии
    burst_max: int
    burst_pause_min: int            # мс пауза между burst'ами
    burst_pause_max: int
    typo_rate: float                # вероятность опечатки
    # Скролл
    scroll_impulses_min: int
    scroll_impulses_max: int
    scroll_correction_prob: float   # вероятность случайной коррекции
    # Dwell
    dwell_duration_min: float       # сек
    dwell_duration_max: float
    dwell_micro_px: int             # ±px микродвижений


# Точные параметры из SparkGrid
PROFILES: dict[BehaviorProfile, ProfileParams] = {
    BehaviorProfile.BALANCED: ProfileParams(
        move_steps_min=14, move_steps_max=48,
        overshoot_probability=0.14, overshoot_factor=0.14,
        bend_min=0.055, bend_max=0.17,
        click_down_delay_min=45, click_down_delay_max=135,
        char_delay_min=28, char_delay_max=78,
        burst_min=2, burst_max=7,
        burst_pause_min=120, burst_pause_max=380,
        typo_rate=0.01,
        scroll_impulses_min=5, scroll_impulses_max=10,
        scroll_correction_prob=0.18,
        dwell_duration_min=0.8, dwell_duration_max=2.5,
        dwell_micro_px=8,
    ),
    BehaviorProfile.FAST: ProfileParams(
        move_steps_min=10, move_steps_max=32,
        overshoot_probability=0.18, overshoot_factor=0.16,
        bend_min=0.04, bend_max=0.12,
        click_down_delay_min=35, click_down_delay_max=95,
        char_delay_min=18, char_delay_max=52,
        burst_min=3, burst_max=7,
        burst_pause_min=70, burst_pause_max=220,
        typo_rate=0.015,
        scroll_impulses_min=4, scroll_impulses_max=8,
        scroll_correction_prob=0.22,
        dwell_duration_min=0.4, dwell_duration_max=1.2,
        dwell_micro_px=6,
    ),
    BehaviorProfile.CAREFUL: ProfileParams(
        move_steps_min=20, move_steps_max=60,
        overshoot_probability=0.08, overshoot_factor=0.10,
        bend_min=0.07, bend_max=0.20,
        click_down_delay_min=60, click_down_delay_max=165,
        char_delay_min=42, char_delay_max=110,
        burst_min=2, burst_max=5,
        burst_pause_min=200, burst_pause_max=550,
        typo_rate=0.005,
        scroll_impulses_min=6, scroll_impulses_max=12,
        scroll_correction_prob=0.14,
        dwell_duration_min=1.2, dwell_duration_max=3.8,
        dwell_micro_px=10,
    ),
}


# ─── Раскладка соседних клавиш для тайпо ─────────────────────────────────────

ADJACENT_KEYS: dict[str, str] = {
    "q": "wa", "w": "qeas", "e": "wrds", "r": "etdf", "t": "ryfg",
    "y": "tugh", "u": "yijh", "i": "uojk", "o": "iplk", "p": "ol",
    "a": "qwsz", "s": "wedxza", "d": "erfcxs", "f": "rtgvcd",
    "g": "tyhbvf", "h": "yujnbg", "j": "uikmnh", "k": "iolmj",
    "l": "opk", "z": "asx", "x": "zsdc", "c": "xdfv", "v": "cfgb",
    "b": "vghn", "n": "bhjm", "m": "njk",
    "1": "2q", "2": "13qw", "3": "24we", "4": "35er", "5": "46rt",
    "6": "57ty", "7": "68yu", "8": "79ui", "9": "80io", "0": "9p",
}


# ─── Утилиты Безье ────────────────────────────────────────────────────────────

def _smoothstep(t: float) -> float:
    """Smoothstep: t² × (3 - 2t). S-curve ускорение/замедление."""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _cubic_bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    """Вычисляет точку на кубической кривой Безье для параметра t ∈ [0, 1]."""
    u = 1.0 - t
    x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
    return (x, y)


def _generate_control_points(
    start: tuple[float, float],
    end: tuple[float, float],
    bend_min: float,
    bend_max: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """
    Генерирует контрольные точки для кубической Безье.
    bend — случайное отклонение 5.5-17% от расстояния, в случайную сторону.
    """
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    dist = math.hypot(dx, dy) or 1.0

    # Нормаль к вектору движения
    nx, ny = -dy / dist, dx / dist

    # Случайное отклонение (bend) для каждой контрольной точки
    bend1 = random.uniform(bend_min, bend_max) * dist * random.choice([-1, 1])
    bend2 = random.uniform(bend_min, bend_max) * dist * random.choice([-1, 1])

    # Контрольные точки примерно в 1/3 и 2/3 пути
    cp1 = (
        start[0] + dx * 0.33 + nx * bend1,
        start[1] + dy * 0.33 + ny * bend1,
    )
    cp2 = (
        start[0] + dx * 0.67 + nx * bend2,
        start[1] + dy * 0.67 + ny * bend2,
    )
    return cp1, cp2


# ─── Основной класс ──────────────────────────────────────────────────────────

class HumanInteractor:
    """
    Имитирует человеческое взаимодействие с Playwright Page.

    Профиль поведения детерминирован по SHA256 хешу username аккаунта:
    60% balanced, 20% fast, 20% careful. Один аккаунт = одна «личность».
    """

    def __init__(self, page: Page, username: str) -> None:
        self.page = page
        self.username = username
        self.profile = self._determine_profile(username)
        self.params = PROFILES[self.profile]
        self._current_x: float = 0.0
        self._current_y: float = 0.0

    # ── Детерминированный выбор профиля ───────────────────────────────────

    @staticmethod
    def _determine_profile(username: str) -> BehaviorProfile:
        """
        SHA256 хеш username → число 0-99 → профиль.
        balanced 0-59 (60%), fast 60-79 (20%), careful 80-99 (20%).
        """
        h = hashlib.sha256(username.encode("utf-8")).hexdigest()
        bucket = int(h[:8], 16) % 100
        if bucket < 60:
            return BehaviorProfile.BALANCED
        elif bucket < 80:
            return BehaviorProfile.FAST
        else:
            return BehaviorProfile.CAREFUL

    # ── Движение мыши ─────────────────────────────────────────────────────

    async def move_to(self, x: float, y: float) -> None:
        """
        Перемещает мышь по кубической Безье с smoothstep.
        С шансом overshoot_probability промахивается на overshoot_factor
        и делает корректирующее движение.
        """
        start = (self._current_x, self._current_y)
        target = (x, y)
        dist = math.hypot(x - start[0], y - start[1])

        # Масштабируем шаги по расстоянию (base: 300px → steps_max)
        scale = max(0.4, min(2.0, dist / 300.0))
        steps = int(random.uniform(
            self.params.move_steps_min,
            self.params.move_steps_max,
        ) * scale)
        steps = max(8, steps)

        # Overshoot: промах мимо цели
        do_overshoot = random.random() < self.params.overshoot_probability
        if do_overshoot:
            overshoot_dist = dist * self.params.overshoot_factor
            angle = math.atan2(y - start[1], x - start[0])
            angle += random.uniform(-0.3, 0.3)  # небольшое отклонение
            overshoot_target = (
                x + math.cos(angle) * overshoot_dist,
                y + math.sin(angle) * overshoot_dist,
            )
            # Движение к промаху
            await self._bezier_move(start, overshoot_target, int(steps * 0.75))
            # Корректировка к цели
            await self._bezier_move(overshoot_target, target, int(steps * 0.35))
        else:
            await self._bezier_move(start, target, steps)

        self._current_x = x
        self._current_y = y

    async def _bezier_move(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        steps: int,
    ) -> None:
        """Двигает мышь по кубической Безье от start к end за N шагов."""
        cp1, cp2 = _generate_control_points(
            start, end,
            self.params.bend_min,
            self.params.bend_max,
        )

        prev_x, prev_y = start
        for i in range(1, steps + 1):
            t = _smoothstep(i / steps)
            px, py = _cubic_bezier(start, cp1, cp2, end, t)

            # Добавляем микро-шум ±1px для естественности
            jitter = 0.8
            px += random.uniform(-jitter, jitter)
            py += random.uniform(-jitter, jitter)

            await self.page.mouse.move(px, py)

            # Задержка между шагами: ~1-4мс для плавности
            await asyncio.sleep(random.uniform(0.001, 0.004))

            prev_x, prev_y = px, py

    # ── Клик ──────────────────────────────────────────────────────────────

    async def click_at(self, x: float, y: float) -> None:
        """
        Перемещает мышь к точке и кликает: mouse.down → пауза 45-135мс → mouse.up.
        НЕ использует page.click() — это палево.
        """
        await self.move_to(x, y)
        await self.page.mouse.down()
        delay_ms = random.uniform(
            self.params.click_down_delay_min,
            self.params.click_down_delay_max,
        )
        await asyncio.sleep(delay_ms / 1000.0)
        await self.page.mouse.up()

    async def click_element(self, element: ElementHandle) -> None:
        """
        Кликает по элементу. Точка внутри элемента выбирается
        через beta-distribution(2.6, 2.6) — НЕ в центр.
        """
        box = await element.bounding_box()
        if not box:
            raise ValueError("Element has no bounding box (not visible?)")

        # Beta(2.6, 2.6) → значения тяготеют к центру, но не точно в центр
        rx = random.betavariate(2.6, 2.6)
        ry = random.betavariate(2.6, 2.6)

        x = box["x"] + box["width"] * rx
        y = box["y"] + box["height"] * ry

        await self.click_at(x, y)

    async def click_selector(self, selector: str) -> None:
        """Находит элемент по селектору и кликает по нему."""
        element = await self.page.wait_for_selector(selector, timeout=10_000)
        if not element:
            raise ValueError(f"Selector not found: {selector}")
        await self.click_element(element)

    # ── Ввод текста ───────────────────────────────────────────────────────

    async def type_text(self, text: str) -> None:
        """
        Вводит текст посимвольно с burst-паттерном.
        - Серия из 2-7 символов быстро (char_delay мс)
        - Пауза между сериями (burst_pause мс)
        - Тайпо 1%: нажимает соседнюю клавишу → Backspace → правильную
        """
        i = 0
        while i < len(text):
            # Определяем длину следующего burst
            burst_len = random.randint(self.params.burst_min, self.params.burst_max)
            burst_end = min(i + burst_len, len(text))

            # Печатаем burst
            for j in range(i, burst_end):
                char = text[j]

                # Тайпо: 1% шанс (только для букв/цифр)
                if (
                    random.random() < self.params.typo_rate
                    and char.lower() in ADJACENT_KEYS
                ):
                    wrong_char = random.choice(ADJACENT_KEYS[char.lower()])
                    if char.isupper():
                        wrong_char = wrong_char.upper()

                    # Печатаем неправильный символ
                    await self.page.keyboard.press(wrong_char)
                    await asyncio.sleep(random.uniform(
                        self.params.char_delay_min / 1000,
                        self.params.char_delay_max / 1000,
                    ))

                    # "Замечаем" ошибку — небольшая пауза
                    await asyncio.sleep(random.uniform(0.15, 0.45))

                    # Backspace
                    await self.page.keyboard.press("Backspace")
                    await asyncio.sleep(random.uniform(0.04, 0.12))

                # Печатаем правильный символ
                await self.page.keyboard.press(char)
                await asyncio.sleep(random.uniform(
                    self.params.char_delay_min / 1000,
                    self.params.char_delay_max / 1000,
                ))

            i = burst_end

            # Пауза между burst'ами (если не конец текста)
            if i < len(text):
                await asyncio.sleep(random.uniform(
                    self.params.burst_pause_min / 1000,
                    self.params.burst_pause_max / 1000,
                ))

    async def type_into(self, selector: str, text: str) -> None:
        """Кликает по полю ввода и печатает текст с имитацией человека."""
        await self.click_selector(selector)
        await asyncio.sleep(random.uniform(0.1, 0.35))
        await self.type_text(text)

    async def clear_and_type(self, selector: str, text: str) -> None:
        """Очищает поле (Ctrl+A → Backspace) и вводит текст."""
        await self.click_selector(selector)
        await asyncio.sleep(random.uniform(0.08, 0.2))
        await self.page.keyboard.press("Control+a")
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await self.page.keyboard.press("Backspace")
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await self.type_text(text)

    # ── Скролл ────────────────────────────────────────────────────────────

    async def scroll(self, delta_y: int = 300) -> None:
        """
        Скролл через 5-10 wheel-импульсов с синусоидальным распределением.
        + случайная коррекция 18% (обратный скролл).
        """
        impulses = random.randint(
            self.params.scroll_impulses_min,
            self.params.scroll_impulses_max,
        )

        # Синусоидальное распределение: больше скролла в середине
        weights = [math.sin(math.pi * (i + 0.5) / impulses) for i in range(impulses)]
        total_weight = sum(weights)
        deltas = [delta_y * w / total_weight for w in weights]

        for i, d in enumerate(deltas):
            # 18% шанс обратной коррекции
            if random.random() < self.params.scroll_correction_prob:
                correction = d * random.uniform(0.1, 0.35) * -1
                await self.page.mouse.wheel(0, correction)
                await asyncio.sleep(random.uniform(0.03, 0.08))

            await self.page.mouse.wheel(0, d)
            await asyncio.sleep(random.uniform(0.02, 0.07))

    async def scroll_to_element(self, selector: str) -> None:
        """Скроллит к элементу несколькими импульсами."""
        element = await self.page.query_selector(selector)
        if not element:
            return

        box = await element.bounding_box()
        if not box:
            return

        viewport = self.page.viewport_size
        if not viewport:
            return

        # Сколько нужно проскроллить
        target_y = box["y"] - viewport["height"] * 0.3
        if abs(target_y) < 50:
            return

        # Скроллим порциями
        remaining = target_y
        while abs(remaining) > 30:
            chunk = remaining * random.uniform(0.3, 0.6)
            await self.scroll(int(chunk))
            remaining -= chunk
            await asyncio.sleep(random.uniform(0.08, 0.2))

    # ── Dwell — «задумался» ───────────────────────────────────────────────

    async def dwell(self, x: float | None = None, y: float | None = None) -> None:
        """
        Имитация «задумался»: стоит на месте с микродвижениями ±8px.
        Если координаты не переданы — используются текущие.
        """
        cx = x if x is not None else self._current_x
        cy = y if y is not None else self._current_y

        duration = random.uniform(
            self.params.dwell_duration_min,
            self.params.dwell_duration_max,
        )
        end_time = asyncio.get_event_loop().time() + duration
        px = self.params.dwell_micro_px

        while asyncio.get_event_loop().time() < end_time:
            mx = cx + random.uniform(-px, px)
            my = cy + random.uniform(-px, px)
            await self.page.mouse.move(mx, my)
            await asyncio.sleep(random.uniform(0.05, 0.2))

        self._current_x = cx
        self._current_y = cy

    # ── Утилиты высокого уровня ───────────────────────────────────────────

    async def random_pause(self, min_sec: float = 0.5, max_sec: float = 2.0) -> None:
        """Случайная пауза."""
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    async def human_scroll_feed(self, scroll_count: int = 3) -> None:
        """
        Имитация листания ленты: скролл → пауза (чтение) → иногда dwell.
        """
        for _ in range(scroll_count):
            delta = random.randint(200, 600)
            await self.scroll(delta)

            # "Читаем" контент
            await asyncio.sleep(random.uniform(1.0, 4.0))

            # Иногда "задумываемся"
            if random.random() < 0.2:
                await self.dwell()

    async def move_to_random_spot(self) -> None:
        """Перемещает мышь в случайное место на странице (idle движение)."""
        viewport = self.page.viewport_size
        if not viewport:
            return
        x = random.uniform(50, viewport["width"] - 50)
        y = random.uniform(50, viewport["height"] - 50)
        await self.move_to(x, y)

    def __repr__(self) -> str:
        return (
            f"HumanInteractor(username={self.username!r}, "
            f"profile={self.profile.value})"
        )
