"""InstaGrid — API логина аккаунтов."""
import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database import query, query_one, execute, run_sync
from backend.services.profile_manager import ProfileManager
from backend.services.login import create_orchestrator

router = APIRouter(prefix="/api/login", tags=["login"])
logger = logging.getLogger("instagrid.login_api")

# Глобальное состояние логина
_login_state = {
    "running": False,
    "total": 0,
    "done": 0,
    "success": 0,
    "failed": 0,
    "results": [],
}


class LoginRequest(BaseModel):
    account_ids: list[int]
    pool_id: int | None = None  # если не указан — берётся из ниши аккаунта


@router.get("/status")
def login_status():
    """Текущий статус логина."""
    return _login_state


@router.post("/start")
async def start_login(body: LoginRequest):
    """Запускает логин для выбранных аккаунтов."""
    if _login_state["running"]:
        raise HTTPException(409, "Логин уже запущен")

    # Определяем pool_id: из запроса или из ниши аккаунтов
    pool_id = body.pool_id

    if not pool_id:
        # Пробуем взять пул из ниши первого аккаунта
        for aid in body.account_ids:
            acc = await run_sync(
                query_one,
                """SELECT a.niche_id, n.proxy_pool_id
                   FROM accounts a
                   LEFT JOIN niches n ON n.id = a.niche_id
                   WHERE a.id = ?""",
                (aid,),
            )
            if acc and acc.get("proxy_pool_id"):
                pool_id = acc["proxy_pool_id"]
                break

    if not pool_id:
        # Fallback: первый статический пул
        first_pool = await run_sync(
            query_one,
            "SELECT id FROM proxy_pools WHERE pool_type = 'static' ORDER BY id ASC LIMIT 1",
            (),
        )
        if first_pool:
            pool_id = first_pool["id"]

    if not pool_id:
        raise HTTPException(400, "Нет пулов прокси. Создай на странице Прокси.")

    # Проверяем что пул существует
    pool = await run_sync(
        query_one,
        "SELECT id FROM proxy_pools WHERE id = ?",
        (pool_id,),
    )
    if not pool:
        raise HTTPException(404, "Пул прокси не найден")

    # Проверяем аккаунты
    accounts = []
    for aid in body.account_ids:
        acc = await run_sync(
            query_one,
            "SELECT id, username, password, totp_secret, status FROM accounts WHERE id = ?",
            (aid,),
        )
        if acc:
            accounts.append(acc)

    if not accounts:
        raise HTTPException(400, "Нет аккаунтов для логина")

    # Разделяем: уже залогиненные пропускаем, остальные сбрасываем на 'new'
    to_login = []
    already_ok = []
    for acc in accounts:
        if acc["status"] == "logged_in":
            already_ok.append(acc)
        else:
            to_login.append(acc)
            await run_sync(
                execute,
                "UPDATE accounts SET status = 'new', static_proxy_id = NULL WHERE id = ?",
                (acc["id"],),
            )

    if not to_login and already_ok:
        return {"started": False, "message": f"Все {len(already_ok)} аккаунтов уже залогинены"}

    if not to_login:
        raise HTTPException(400, "Нет аккаунтов для логина")

    # Запускаем в фоне
    already_results = [
        {"account_id": a["id"], "username": a["username"], "success": True, "status": "logged_in", "message": "Уже залогинен"}
        for a in already_ok
    ]

    _login_state.update({
        "running": True,
        "total": len(to_login),
        "done": 0,
        "success": len(already_ok),
        "failed": 0,
        "results": already_results,
    })

    asyncio.create_task(_run_login(to_login, pool_id))
    return {"started": True, "accounts": len(to_login), "skipped": len(already_ok)}


@router.post("/stop")
def stop_login():
    """Останавливает текущий логин."""
    _login_state["running"] = False
    return {"stopped": True}


async def _run_login(accounts: list[dict], pool_id: int):
    """Фоновая задача логина."""
    try:
        pm = ProfileManager()
        orch = create_orchestrator(pm)

        for acc in accounts:
            if not _login_state["running"]:
                break

            logger.info("Login starting: %s", acc["username"])

            try:
                result = await orch.login_with_rotation(account=acc, proxy_pool_id=pool_id)

                entry = {
                    "account_id": acc["id"],
                    "username": acc["username"],
                    "success": result.success,
                    "status": result.status.value if hasattr(result.status, 'value') else str(result.status),
                    "message": result.message,
                }

                _login_state["done"] += 1
                if result.success:
                    _login_state["success"] += 1
                else:
                    _login_state["failed"] += 1
                _login_state["results"].append(entry)

                logger.info(
                    "Login %s: %s — %s",
                    "OK" if result.success else "FAIL",
                    acc["username"],
                    result.message,
                )
            except Exception as e:
                _login_state["done"] += 1
                _login_state["failed"] += 1
                _login_state["results"].append({
                    "account_id": acc["id"],
                    "username": acc["username"],
                    "success": False,
                    "status": "error",
                    "message": str(e),
                })
                logger.error("Login error %s: %s", acc["username"], e)

    finally:
        _login_state["running"] = False
        logger.info(
            "Login batch done: %d/%d success",
            _login_state["success"], _login_state["total"],
        )
