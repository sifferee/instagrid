"""InstaGrid — автосборщик селекторов.

При каждой навигации собирает все input, button, div[role=button], a, form
и сохраняет в logs/selectors/ с таймстемпом и URL.

Использование:
    collector = SelectorCollector(page)
    await collector.start()  # начинает слушать навигации
    # ... работаем с page ...
    collector.stop()  # останавливаем
"""
import json
import time
import logging
import asyncio
from pathlib import Path

logger = logging.getLogger("instagrid.selectors")

SELECTORS_DIR = Path(__file__).resolve().parent.parent.parent / "logs" / "selectors"
SELECTORS_DIR.mkdir(parents=True, exist_ok=True)


class SelectorCollector:
    """Автоматически собирает селекторы со всех страниц."""

    def __init__(self, page, username: str = "unknown"):
        self.page = page
        self.username = username
        self._running = False
        self._collected_urls = set()
        self._task = None

    async def start(self):
        """Начинает мониторинг навигаций."""
        self._running = True
        self.page.on("load", lambda: asyncio.ensure_future(self._on_page_load()))
        logger.info("[%s] Selector collector started", self.username)

    def stop(self):
        """Останавливает мониторинг."""
        self._running = False
        logger.info("[%s] Selector collector stopped", self.username)

    async def _on_page_load(self):
        """Вызывается при каждой загрузке страницы."""
        if not self._running:
            return

        try:
            await asyncio.sleep(2)  # даём JS отрендериться
            url = self.page.url
            # Не дублируем один и тот же URL
            url_key = url.split("?")[0]
            if url_key in self._collected_urls:
                return
            self._collected_urls.add(url_key)

            await self.collect_now(url)
        except Exception as e:
            logger.debug("[%s] Selector collection failed: %s", self.username, e)

    async def collect_now(self, url: str = None):
        """Собирает все элементы с текущей страницы и сохраняет в файл."""
        if url is None:
            url = self.page.url

        try:
            elements = await self.page.evaluate("""() => {
                const results = [];
                const selectors = 'input, button, [role="button"], [type="submit"], a[href], select, textarea, form, [aria-label], [data-testid]';
                document.querySelectorAll(selectors).forEach(el => {
                    const rect = el.getBoundingClientRect();
                    results.push({
                        tag: el.tagName,
                        type: el.type || '',
                        name: el.name || '',
                        id: el.id || '',
                        className: (el.className || '').toString().slice(0, 80),
                        ariaLabel: el.getAttribute('aria-label') || '',
                        placeholder: el.placeholder || '',
                        href: el.href || '',
                        text: el.textContent?.trim()?.slice(0, 60) || '',
                        autocomplete: el.autocomplete || '',
                        dataTestId: el.getAttribute('data-testid') || '',
                        role: el.getAttribute('role') || '',
                        visible: el.offsetParent !== null,
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        w: Math.round(rect.width),
                        h: Math.round(rect.height),
                    });
                });
                return results;
            }""")

            # Формируем имя файла из URL
            url_clean = url.split("?")[0].replace("https://", "").replace("http://", "")
            url_clean = url_clean.replace("/", "_").replace(".", "_")[:60]
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"{timestamp}_{self.username}_{url_clean}.json"

            filepath = SELECTORS_DIR / filename

            data = {
                "url": url,
                "username": self.username,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "elements_count": len(elements),
                "elements": elements,
            }

            filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info(
                "[%s] Collected %d elements from %s → %s",
                self.username, len(elements), url[:60], filename,
            )

            # Также выводим читаемую сводку
            self._print_summary(elements, url)

            return elements

        except Exception as e:
            logger.debug("[%s] Failed to collect selectors: %s", self.username, e)
            return []

    def _print_summary(self, elements: list, url: str):
        """Печатает читаемую сводку элементов."""
        visible = [e for e in elements if e["visible"]]
        hidden = [e for e in elements if not e["visible"]]

        logger.info("─── Selectors: %s ───", url.split("?")[0][:50])
        logger.info("  Total: %d (visible: %d, hidden: %d)", len(elements), len(visible), len(hidden))

        for el in visible:
            parts = [f"{el['tag']:8}"]
            if el["type"]:
                parts.append(f"type={el['type']}")
            if el["name"]:
                parts.append(f"name={el['name']}")
            if el["ariaLabel"]:
                parts.append(f"aria='{el['ariaLabel']}'")
            if el["placeholder"]:
                parts.append(f"ph='{el['placeholder']}'")
            if el["dataTestId"]:
                parts.append(f"testid='{el['dataTestId']}'")
            if el["text"] and el["tag"] in ("BUTTON", "DIV", "A", "SPAN"):
                parts.append(f"text='{el['text'][:30]}'")
            if el["href"] and el["tag"] == "A":
                parts.append(f"href='{el['href'][:40]}'")

            logger.info("  [VIS] %s", " | ".join(parts))


async def collect_page_selectors(page, username: str = "test") -> list:
    """Одноразовый сбор селекторов с текущей страницы."""
    collector = SelectorCollector(page, username)
    return await collector.collect_now()
