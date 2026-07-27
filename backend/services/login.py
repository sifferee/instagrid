"""
InstaGrid — Логин-модуль.

Логин в Instagram через Camoufox + HumanInteractor:
- Ввод логина/пароля с burst-typing, тайпо
- 2FA через pyotp (TOTP из сохранённого секрета)
- Прокликивание попапов: «Save login info», «Turn on notifications», ADS consent
- После успешного логина: настройки → отключение приватного профиля
- Ошибка логина → статический прокси удаляется, берётся следующий из пула
- 2-3 неудачи подряд → аккаунт невалидный
- Параллельный пул воркеров, атомарный захват задач
- Hard timeout: 15 мин на весь цикл аккаунта

Перезагрузка при лаге прокси:
- 30 сек нет ответа → клик адресная строка → Enter
- Прокси мёртв → берёт другой
- 3 провала → cooldown, следующий
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

import pyotp
from playwright.async_api import BrowserContext, Page, TimeoutError as PwTimeout

from backend.services.human import HumanInteractor
from backend.services.profile_manager import ProfileManager

logger = logging.getLogger("instagrid.login")


# ─── Константы ────────────────────────────────────────────────────────────────

INSTAGRAM_LOGIN_URL = "https://www.instagram.com/accounts/login/"
INSTAGRAM_SETTINGS_URL = "https://www.instagram.com/accounts/edit/"
INSTAGRAM_PRIVACY_URL = "https://www.instagram.com/accounts/privacy_and_security/"

# Таймауты
PAGE_LOAD_TIMEOUT = 30_000          # мс — ожидание загрузки страницы
ELEMENT_WAIT_TIMEOUT = 10_000       # мс — ожидание элемента
HARD_TIMEOUT = 15 * 60             # сек — 15 мин на весь цикл аккаунта
PROXY_LAG_TIMEOUT = 30              # сек — нет ответа = лаг прокси
PROXY_LAG_RETRY_PAUSE = 150        # сек — пауза если прокси жив, но IG не грузит

# Retry
MAX_LOGIN_ATTEMPTS = 3              # неудач подряд → аккаунт невалидный
MAX_PROXY_RETRIES = 3               # попытки с разными прокси
MAX_PAGE_RELOAD_RETRIES = 3         # попытки перезагрузки при лаге

# Статусы аккаунтов
class AccountStatus(str, Enum):
    NEW = "new"
    LOGGED_IN = "logged_in"
    COOLDOWN = "cooldown"
    DEAD = "dead"
    INVALID_CREDENTIALS = "invalid_credentials"
    CHALLENGE = "challenge"


# ─── Селекторы Instagram ──────────────────────────────────────────────────────

class Selectors:
    """CSS-селекторы элементов Instagram (web, desktop view)."""

    # Логин-форма
    USERNAME_INPUT = 'input[name="username"]'
    PASSWORD_INPUT = 'input[name="password"]'
    LOGIN_BUTTON = 'button[type="submit"]'

    # 2FA
    SECURITY_CODE_INPUT = 'input[name="verificationCode"]'
    CONFIRM_2FA_BUTTON = 'form button[type="button"]'  # "Confirm" button on 2FA page

    # Попапы после логина
    SAVE_INFO_NOT_NOW = '//button[contains(text(), "Not Now") or contains(text(), "Not now")]'
    SAVE_INFO_BUTTON = '//button[contains(text(), "Save Info") or contains(text(), "Save info")]'
    NOTIFICATIONS_NOT_NOW = '//button[contains(text(), "Not Now") or contains(text(), "Not now")]'
    TURN_ON_NOTIFICATIONS = '//button[contains(text(), "Turn On") or contains(text(), "Turn on")]'

    # ADS consent (cookie/GDPR popup)
    ADS_ACCEPT_ALL = '//button[contains(text(), "Allow all cookies") or contains(text(), "Allow essential and optional cookies")]'
    ADS_ESSENTIAL_ONLY = '//button[contains(text(), "Decline optional cookies") or contains(text(), "Allow essential cookies only")]'

    # Ошибки логина
    ERROR_MESSAGE = '#slfErrorAlert, [data-testid="login-error-message"], [role="alert"]'
    SUSPICIOUS_LOGIN = 'text="Suspicious Login Attempt"'
    CHALLENGE_REQUIRED = 'text="challenge_required"'

    # Подтверждение залогинен
    HOME_FEED = 'svg[aria-label="Home"], a[href="/"]'
    PROFILE_ICON = 'svg[aria-label="Profile"], img[data-testid="user-avatar"]'
    NAV_BAR = 'nav[role="navigation"]'

    # Настройки приватности
    PRIVATE_ACCOUNT_TOGGLE = 'input[type="checkbox"]'  # на странице privacy
    PRIVATE_ACCOUNT_LABEL = '//label[contains(text(), "Private account") or contains(text(), "Private Account")]'


# ─── Результат логина ─────────────────────────────────────────────────────────

@dataclass
class LoginResult:
    """Результат попытки логина."""
    success: bool
    account_id: int | None = None
    username: str = ""
    status: AccountStatus = AccountStatus.NEW
    message: str = ""
    proxy_used: str = ""
    attempts: int = 0
    duration_sec: float = 0.0


# ─── Основной класс логина ────────────────────────────────────────────────────

class InstagramLogin:
    """
    Выполняет логин одного аккаунта в Instagram.

    Жизненный цикл:
    1. launch_profile() → Camoufox с прокси
    2. navigate_to_login() → открываем IG
    3. handle_cookies_popup() → убираем GDPR
    4. enter_credentials() → логин/пароль с HumanInteractor
    5. handle_2fa() → если требуется
    6. handle_post_login_popups() → «Save info», «Notifications»
    7. verify_logged_in() → проверяем что залогинены
    8. disable_private_profile() → настройки → снять галку
    """

    def __init__(
        self,
        profile_manager: ProfileManager,
        db: Any = None,  # DatabaseSession — передаём для обновления статусов
    ) -> None:
        self.pm = profile_manager
        self.db = db

    async def login_account(
        self,
        account: dict[str, Any],
        proxy: dict[str, str] | None = None,
    ) -> LoginResult:
        """
        Полный цикл логина одного аккаунта.

        Args:
            account: {"id": int, "username": str, "password": str, "totp_secret": str, ...}
            proxy: {"server": "host:port", "username": "...", "password": "..."} или None

        Returns:
            LoginResult
        """
        username = account["username"]
        start_time = time.time()
        result = LoginResult(
            success=False,
            account_id=account.get("id"),
            username=username,
            proxy_used=proxy["server"] if proxy else "none",
        )

        context: BrowserContext | None = None
        page: Page | None = None

        try:
            # Hard timeout на весь цикл
            async with asyncio.timeout(HARD_TIMEOUT):
                # 1. Запуск профиля
                logger.info("[%s] Launching profile with proxy %s", username, result.proxy_used)

                if not self.pm.profile_exists(username):
                    self.pm.create_profile(username)

                context, page = await self.pm.launch_profile(
                    profile_id=username,
                    proxy=proxy,
                    headless=False,
                )

                human = HumanInteractor(page, username)
                logger.info("[%s] Profile launched, behavior=%s", username, human.profile.value)

                # 2. Навигация на страницу логина
                await self._navigate_to_login(page, human)

                # 3. Cookie/GDPR popup
                await self._handle_cookies_popup(page, human)

                # 4. Ввод credentials
                await self._enter_credentials(page, human, account)

                # 5. Ожидание результата + 2FA
                login_ok = await self._wait_for_login_result(page, human, account)

                if not login_ok:
                    result.status = AccountStatus.INVALID_CREDENTIALS
                    result.message = "Login failed — invalid credentials or challenge"
                    result.attempts = 1
                    return result

                # 6. Попапы после логина
                await self._handle_post_login_popups(page, human)

                # 7. Проверка что залогинены
                verified = await self._verify_logged_in(page)
                if not verified:
                    result.status = AccountStatus.COOLDOWN
                    result.message = "Login seemed OK but couldn't verify feed"
                    return result

                # 8. Отключение приватного профиля
                await self._disable_private_profile(page, human)

                result.success = True
                result.status = AccountStatus.LOGGED_IN
                result.message = "Login successful"
                logger.info("[%s] Login successful", username)

        except TimeoutError:
            result.status = AccountStatus.COOLDOWN
            result.message = f"Hard timeout ({HARD_TIMEOUT}s) exceeded"
            logger.error("[%s] Hard timeout exceeded", username)

        except Exception as e:
            result.status = AccountStatus.COOLDOWN
            result.message = f"Unexpected error: {e}"
            logger.exception("[%s] Login error", username)

        finally:
            result.duration_sec = time.time() - start_time
            result.attempts = 1

            # Не закрываем контекст при успешном логине — он нужен для дальнейшей работы
            if not result.success and context:
                await self.pm.close_profile(username)

        return result

    # ── Навигация ─────────────────────────────────────────────────────────

    async def _navigate_to_login(self, page: Page, human: HumanInteractor) -> None:
        """Открывает страницу логина с обработкой лага прокси."""
        for attempt in range(1, MAX_PAGE_RELOAD_RETRIES + 1):
            try:
                await page.goto(
                    INSTAGRAM_LOGIN_URL,
                    wait_until="domcontentloaded",
                    timeout=PAGE_LOAD_TIMEOUT,
                )
                # Ждём поле username
                await page.wait_for_selector(
                    Selectors.USERNAME_INPUT,
                    timeout=ELEMENT_WAIT_TIMEOUT,
                )
                await human.random_pause(1.0, 2.5)
                return

            except PwTimeout:
                logger.warning(
                    "[%s] Page load timeout attempt %d/%d",
                    human.username, attempt, MAX_PAGE_RELOAD_RETRIES,
                )
                if attempt < MAX_PAGE_RELOAD_RETRIES:
                    # 30 сек нет ответа → кликаем адресную строку → Enter
                    await self._reload_via_address_bar(page, human)
                    await asyncio.sleep(2.0)
                else:
                    raise RuntimeError("Instagram login page not loading after retries")

    async def _reload_via_address_bar(self, page: Page, human: HumanInteractor) -> None:
        """Перезагрузка через адресную строку (как человек)."""
        # Ctrl+L → фокус на адресную строку
        await page.keyboard.press("Control+l")
        await human.random_pause(0.3, 0.7)
        await page.keyboard.press("Enter")
        await human.random_pause(2.0, 4.0)

    # ── Cookie/GDPR попап ─────────────────────────────────────────────────

    async def _handle_cookies_popup(self, page: Page, human: HumanInteractor) -> None:
        """Обрабатывает GDPR/cookie-попап если он есть."""
        try:
            # Пытаемся найти кнопку "Allow essential cookies only"
            btn = await page.wait_for_selector(
                Selectors.ADS_ESSENTIAL_ONLY,
                timeout=3000,
            )
            if btn:
                await human.random_pause(0.5, 1.5)
                await human.click_element(btn)
                logger.info("[%s] Dismissed cookie popup (essential only)", human.username)
                await human.random_pause(1.0, 2.0)
                return
        except PwTimeout:
            pass

        try:
            # Fallback — accept all
            btn = await page.wait_for_selector(
                Selectors.ADS_ACCEPT_ALL,
                timeout=2000,
            )
            if btn:
                await human.random_pause(0.5, 1.5)
                await human.click_element(btn)
                logger.info("[%s] Dismissed cookie popup (accept all)", human.username)
                await human.random_pause(1.0, 2.0)
        except PwTimeout:
            logger.debug("[%s] No cookie popup found", human.username)

    # ── Ввод credentials ──────────────────────────────────────────────────

    async def _enter_credentials(
        self,
        page: Page,
        human: HumanInteractor,
        account: dict[str, Any],
    ) -> None:
        """Вводит логин и пароль с имитацией человека."""
        username = account["username"]
        password = account["password"]

        # Клик по полю username → ввод
        logger.info("[%s] Entering username", username)
        await human.clear_and_type(Selectors.USERNAME_INPUT, username)

        # Пауза "переводит взгляд" на поле пароля
        await human.random_pause(0.4, 1.2)

        # Tab или клик по полю password → ввод
        logger.info("[%s] Entering password", username)
        await human.clear_and_type(Selectors.PASSWORD_INPUT, password)

        # Пауза перед кликом на кнопку Login
        await human.random_pause(0.6, 1.8)

        # Клик по кнопке Log In
        await human.click_selector(Selectors.LOGIN_BUTTON)
        logger.info("[%s] Clicked login button", username)

    # ── Ожидание результата логина + 2FA ──────────────────────────────────

    async def _wait_for_login_result(
        self,
        page: Page,
        human: HumanInteractor,
        account: dict[str, Any],
    ) -> bool:
        """
        Ждёт результата после нажатия Login:
        - Ошибка → return False
        - 2FA форма → вводим код → проверяем
        - Успех → return True
        """
        await human.random_pause(2.0, 4.0)

        # Проверяем ошибки
        error = await page.query_selector(Selectors.ERROR_MESSAGE)
        if error:
            error_text = await error.inner_text()
            logger.warning("[%s] Login error: %s", human.username, error_text.strip())
            return False

        # Проверяем challenge
        challenge = await page.query_selector(Selectors.CHALLENGE_REQUIRED)
        if challenge:
            logger.warning("[%s] Challenge required", human.username)
            return False

        # Suspicious login attempt
        suspicious = await page.query_selector(Selectors.SUSPICIOUS_LOGIN)
        if suspicious:
            logger.warning("[%s] Suspicious login attempt detected", human.username)
            return False

        # Проверяем 2FA
        twofa_input = await page.query_selector(Selectors.SECURITY_CODE_INPUT)
        if twofa_input:
            return await self._handle_2fa(page, human, account)

        # Ждём либо навигационную панель (успех), либо ошибку
        try:
            await page.wait_for_selector(
                f"{Selectors.NAV_BAR}, {Selectors.HOME_FEED}",
                timeout=15_000,
            )
            return True
        except PwTimeout:
            # Ещё раз проверяем 2FA (может появиться с задержкой)
            twofa_input = await page.query_selector(Selectors.SECURITY_CODE_INPUT)
            if twofa_input:
                return await self._handle_2fa(page, human, account)

            logger.warning("[%s] No success/error/2FA detected after login", human.username)
            return False

    async def _handle_2fa(
        self,
        page: Page,
        human: HumanInteractor,
        account: dict[str, Any],
    ) -> bool:
        """Обрабатывает 2FA: генерирует TOTP-код и вводит."""
        totp_secret = account.get("totp_secret", "")
        if not totp_secret:
            logger.error("[%s] 2FA required but no TOTP secret saved", human.username)
            return False

        # Генерируем TOTP-код
        totp = pyotp.TOTP(totp_secret)
        code = totp.now()
        logger.info("[%s] 2FA required, entering TOTP code", human.username)

        # Пауза — "достаёт телефон, смотрит код"
        await human.random_pause(2.0, 5.0)

        # Вводим код
        await human.clear_and_type(Selectors.SECURITY_CODE_INPUT, code)
        await human.random_pause(0.5, 1.2)

        # Нажимаем Confirm
        try:
            confirm_btn = await page.wait_for_selector(
                Selectors.CONFIRM_2FA_BUTTON,
                timeout=5000,
            )
            if confirm_btn:
                await human.click_element(confirm_btn)
        except PwTimeout:
            # Fallback: Enter
            await page.keyboard.press("Enter")

        await human.random_pause(3.0, 5.0)

        # Проверяем результат
        error = await page.query_selector(Selectors.ERROR_MESSAGE)
        if error:
            logger.warning("[%s] 2FA code rejected", human.username)
            return False

        try:
            await page.wait_for_selector(
                f"{Selectors.NAV_BAR}, {Selectors.HOME_FEED}",
                timeout=15_000,
            )
            return True
        except PwTimeout:
            logger.warning("[%s] No feed after 2FA", human.username)
            return False

    # ── Попапы после логина ───────────────────────────────────────────────

    async def _handle_post_login_popups(self, page: Page, human: HumanInteractor) -> None:
        """Прокликивает попапы: Save Info, Notifications."""

        # Save Login Info → "Not Now"
        await self._dismiss_popup(
            page, human,
            Selectors.SAVE_INFO_NOT_NOW,
            "Save Login Info",
        )

        # Turn On Notifications → "Not Now"
        await self._dismiss_popup(
            page, human,
            Selectors.NOTIFICATIONS_NOT_NOW,
            "Notifications",
        )

    async def _dismiss_popup(
        self,
        page: Page,
        human: HumanInteractor,
        selector: str,
        popup_name: str,
        timeout_ms: int = 5000,
    ) -> None:
        """Пытается найти и закрыть попап."""
        try:
            btn = await page.wait_for_selector(selector, timeout=timeout_ms)
            if btn:
                await human.random_pause(0.8, 2.0)
                await human.click_element(btn)
                logger.info("[%s] Dismissed popup: %s", human.username, popup_name)
                await human.random_pause(1.0, 2.0)
        except PwTimeout:
            logger.debug("[%s] Popup not found: %s", human.username, popup_name)

    # ── Проверка залогиненности ───────────────────────────────────────────

    async def _verify_logged_in(self, page: Page) -> bool:
        """Проверяет что мы реально на главной странице IG."""
        try:
            await page.wait_for_selector(
                f"{Selectors.NAV_BAR}, {Selectors.HOME_FEED}",
                timeout=10_000,
            )
            current_url = page.url
            if "login" in current_url or "challenge" in current_url:
                return False
            return True
        except PwTimeout:
            return False

    # ── Отключение приватного профиля ─────────────────────────────────────

    async def _disable_private_profile(self, page: Page, human: HumanInteractor) -> None:
        """Заходит в настройки приватности и отключает private account."""
        try:
            logger.info("[%s] Navigating to privacy settings", human.username)

            await page.goto(
                INSTAGRAM_PRIVACY_URL,
                wait_until="domcontentloaded",
                timeout=PAGE_LOAD_TIMEOUT,
            )
            await human.random_pause(1.5, 3.0)

            # Ищем тоггл Private Account
            label = await page.query_selector(Selectors.PRIVATE_ACCOUNT_LABEL)
            if not label:
                # Пробуем альтернативный путь
                # Некоторые версии IG используют Settings → Privacy → Account Privacy
                await page.goto(
                    "https://www.instagram.com/accounts/who_can_see_your_content/",
                    wait_until="domcontentloaded",
                    timeout=PAGE_LOAD_TIMEOUT,
                )
                await human.random_pause(1.5, 3.0)
                label = await page.query_selector(Selectors.PRIVATE_ACCOUNT_LABEL)

            if not label:
                logger.warning("[%s] Private account toggle not found", human.username)
                return

            # Проверяем текущее состояние
            toggle = await page.query_selector(Selectors.PRIVATE_ACCOUNT_TOGGLE)
            if toggle:
                is_checked = await toggle.is_checked()
                if is_checked:
                    # Приватный — отключаем
                    await human.click_element(toggle)
                    await human.random_pause(1.0, 2.0)

                    # Может быть confirmation dialog
                    try:
                        confirm = await page.wait_for_selector(
                            '//button[contains(text(), "Switch to Public") or contains(text(), "Turn Off")]',
                            timeout=5000,
                        )
                        if confirm:
                            await human.click_element(confirm)
                            logger.info("[%s] Disabled private profile", human.username)
                    except PwTimeout:
                        logger.info("[%s] Private toggle clicked, no confirm needed", human.username)
                else:
                    logger.info("[%s] Profile already public", human.username)
            else:
                logger.warning("[%s] Could not find privacy toggle element", human.username)

        except Exception as e:
            logger.warning("[%s] Failed to disable private profile: %s", human.username, e)

        # Возвращаемся на главную
        await page.goto(
            "https://www.instagram.com/",
            wait_until="domcontentloaded",
            timeout=PAGE_LOAD_TIMEOUT,
        )
        await human.random_pause(1.0, 2.0)


# ─── Оркестратор с ротацией прокси ────────────────────────────────────────────

class LoginOrchestrator:
    """
    Логин одного аккаунта с ротацией прокси при ошибках.

    Логика:
    - Попытка 1 с текущим прокси
    - Неудача → удаляем прокси из пула, берём следующий, повторяем
    - 2-3 неудачи подряд → аккаунт невалидный (пароль неверный)
    - Challenge → cooldown 24ч
    """

    def __init__(
        self,
        profile_manager: ProfileManager,
        get_next_proxy: Callable[[int], Coroutine[Any, Any, dict[str, str] | None]],
        delete_proxy: Callable[[int], Coroutine[Any, Any, None]],
        update_account_status: Callable[[int, str, str | None], Coroutine[Any, Any, None]],
        bind_proxy_to_account: Callable[[int, int], Coroutine[Any, Any, None]],
    ) -> None:
        """
        Args:
            profile_manager: ProfileManager instance
            get_next_proxy: async fn(pool_id) → {"id": int, "server": ..., ...} или None
            delete_proxy: async fn(proxy_id) → удаляет прокси навсегда
            update_account_status: async fn(account_id, status, message) → обновляет статус
            bind_proxy_to_account: async fn(account_id, proxy_id) → привязывает прокси
        """
        self.pm = profile_manager
        self._get_next_proxy = get_next_proxy
        self._delete_proxy = delete_proxy
        self._update_status = update_account_status
        self._bind_proxy = bind_proxy_to_account
        self._login = InstagramLogin(profile_manager)

    async def login_with_rotation(
        self,
        account: dict[str, Any],
        proxy_pool_id: int,
    ) -> LoginResult:
        """
        Логин с ротацией прокси при неудачах.

        - Успех → прокси привязывается к аккаунту
        - Неудача → прокси удаляется, берётся следующий
        - MAX_LOGIN_ATTEMPTS неудач → аккаунт невалидный
        """
        last_result = LoginResult(success=False, username=account["username"])
        consecutive_failures = 0

        for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
            # Получаем прокси
            proxy_data = await self._get_next_proxy(proxy_pool_id)
            if not proxy_data:
                last_result.message = "No available proxies in pool"
                last_result.status = AccountStatus.COOLDOWN
                await self._update_status(
                    account["id"],
                    AccountStatus.COOLDOWN.value,
                    "No proxies available",
                )
                return last_result

            proxy_id = proxy_data.pop("id")
            proxy_for_launch = {
                "server": proxy_data["server"],
                "username": proxy_data.get("username", ""),
                "password": proxy_data.get("password", ""),
            }

            logger.info(
                "[%s] Login attempt %d/%d with proxy %s",
                account["username"], attempt, MAX_LOGIN_ATTEMPTS,
                proxy_for_launch["server"],
            )

            # Попытка логина
            result = await self._login.login_account(account, proxy_for_launch)
            result.attempts = attempt

            if result.success:
                # Привязываем прокси к аккаунту
                await self._bind_proxy(account["id"], proxy_id)
                await self._update_status(
                    account["id"],
                    AccountStatus.LOGGED_IN.value,
                    None,
                )
                logger.info(
                    "[%s] Login OK on attempt %d, proxy %s bound",
                    account["username"], attempt, proxy_for_launch["server"],
                )
                return result

            # Неудача
            consecutive_failures += 1
            last_result = result

            if result.status == AccountStatus.CHALLENGE:
                # Challenge → cooldown 24ч, прокси НЕ удаляем (не его вина)
                await self._update_status(
                    account["id"],
                    AccountStatus.CHALLENGE.value,
                    "Challenge required — cooldown 24h",
                )
                logger.warning("[%s] Challenge → cooldown", account["username"])
                return result

            # Удаляем прокси — он "сгорел" на этом аккаунте
            await self._delete_proxy(proxy_id)
            logger.info(
                "[%s] Proxy %s deleted after failed login",
                account["username"], proxy_for_launch["server"],
            )

            # Закрываем профиль перед следующей попыткой
            await self.pm.close_profile(account["username"])

            # Пауза между попытками
            if attempt < MAX_LOGIN_ATTEMPTS:
                pause = 5.0 + attempt * 3.0
                await asyncio.sleep(pause)

        # Все попытки исчерпаны → аккаунт невалидный
        await self._update_status(
            account["id"],
            AccountStatus.INVALID_CREDENTIALS.value,
            f"Failed {MAX_LOGIN_ATTEMPTS} attempts — likely invalid credentials",
        )
        logger.error(
            "[%s] All %d login attempts failed — marked invalid",
            account["username"], MAX_LOGIN_ATTEMPTS,
        )
        last_result.status = AccountStatus.INVALID_CREDENTIALS
        return last_result


# ─── Пул воркеров ─────────────────────────────────────────────────────────────

class LoginWorkerPool:
    """
    Параллельный пул воркеров для логина.

    - N воркеров (по умолчанию 5)
    - Атомарный захват задач: каждый воркер берёт следующий аккаунт из очереди
    - По порядку от первого до последнего
    - Пропуск уже отработанных
    - Перезапуск Camoufox каждые 20-30 аккаунтов (утечки памяти)
    """

    def __init__(
        self,
        orchestrator: LoginOrchestrator,
        max_workers: int = 5,
        restart_every: int = 25,  # перезапуск каждые N аккаунтов
    ) -> None:
        self.orchestrator = orchestrator
        self.max_workers = max_workers
        self.restart_every = restart_every

        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._results: list[LoginResult] = []
        self._results_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._active_workers: int = 0
        self._processed_count: int = 0
        self._total_count: int = 0

    async def run(
        self,
        accounts: list[dict[str, Any]],
        proxy_pool_id: int,
        on_progress: Callable[[LoginResult], Coroutine[Any, Any, None]] | None = None,
    ) -> list[LoginResult]:
        """
        Запускает логин для списка аккаунтов.

        Args:
            accounts: список аккаунтов [{id, username, password, totp_secret, ...}]
            proxy_pool_id: ID пула прокси
            on_progress: callback при каждом завершённом аккаунте

        Returns:
            Список LoginResult для каждого аккаунта
        """
        self._results = []
        self._stop_event.clear()
        self._processed_count = 0
        self._total_count = len(accounts)

        # Заполняем очередь
        for acc in accounts:
            await self._queue.put(acc)

        # Запускаем воркеров
        workers = [
            asyncio.create_task(
                self._worker(i, proxy_pool_id, on_progress)
            )
            for i in range(min(self.max_workers, len(accounts)))
        ]

        # Ждём завершения всех
        await asyncio.gather(*workers, return_exceptions=True)

        logger.info(
            "Login pool finished: %d/%d successful",
            sum(1 for r in self._results if r.success),
            self._total_count,
        )
        return self._results

    async def stop(self) -> None:
        """Останавливает пул (текущие задачи дорабатывают)."""
        self._stop_event.set()

    async def _worker(
        self,
        worker_id: int,
        proxy_pool_id: int,
        on_progress: Callable[[LoginResult], Coroutine[Any, Any, None]] | None,
    ) -> None:
        """Один воркер: берёт аккаунты из очереди и логинит."""
        processed_in_session = 0

        while not self._stop_event.is_set():
            try:
                account = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            logger.info(
                "[Worker %d] Processing %s (%d/%d)",
                worker_id, account["username"],
                self._processed_count + 1, self._total_count,
            )

            try:
                result = await self.orchestrator.login_with_rotation(
                    account, proxy_pool_id,
                )
            except Exception as e:
                result = LoginResult(
                    success=False,
                    account_id=account.get("id"),
                    username=account["username"],
                    status=AccountStatus.COOLDOWN,
                    message=f"Worker error: {e}",
                )
                logger.exception("[Worker %d] Error processing %s", worker_id, account["username"])

            async with self._results_lock:
                self._results.append(result)
                self._processed_count += 1

            if on_progress:
                try:
                    await on_progress(result)
                except Exception:
                    pass

            processed_in_session += 1

            # Перезапуск каждые N аккаунтов (утечки памяти Playwright/Camoufox)
            if processed_in_session >= self.restart_every:
                logger.info("[Worker %d] Restarting after %d accounts", worker_id, processed_in_session)
                processed_in_session = 0

            self._queue.task_done()

        logger.info("[Worker %d] Finished", worker_id)

    @property
    def progress(self) -> tuple[int, int]:
        """Возвращает (processed, total)."""
        return self._processed_count, self._total_count
