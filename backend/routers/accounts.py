"""InstaGrid — CRUD аккаунтов + массовый импорт."""
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from backend.config import MOBILE_POOL_MAX_ACCOUNTS
from backend.database import query, query_one, execute, get_db

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class AccountCreate(BaseModel):
    niche_id: int | None = None
    username: str
    password: str
    totp_secret: str | None = None


class AccountUpdate(BaseModel):
    niche_id: int | None = None
    username: str | None = None
    password: str | None = None
    totp_secret: str | None = None
    status: str | None = None
    mobile_pool_id: int | None = None
    notes: str | None = None


class AccountBulkImport(BaseModel):
    niche_id: int | None = None
    data: str  # формат: login:password или login:password:2fa, по строке


class AccountMove(BaseModel):
    account_ids: list[int]
    niche_id: int | None


@router.get("")
def list_accounts(niche_id: int | None = None, status: str | None = None):
    """Список аккаунтов с фильтрами."""
    sql = """
        SELECT a.*,
               n.name as niche_name,
               sp.host as proxy_host, sp.port as proxy_port
        FROM accounts a
        LEFT JOIN niches n ON n.id = a.niche_id
        LEFT JOIN static_proxies sp ON sp.id = a.static_proxy_id
        WHERE 1=1
    """
    params = []
    if niche_id is not None:
        sql += " AND a.niche_id = ?"
        params.append(niche_id)
    if status:
        sql += " AND a.status = ?"
        params.append(status)
    sql += " ORDER BY a.id"
    return query(sql, tuple(params))


@router.get("/{account_id}")
def get_account(account_id: int):
    acc = query_one("""
        SELECT a.*, n.name as niche_name,
               sp.host as proxy_host, sp.port as proxy_port
        FROM accounts a
        LEFT JOIN niches n ON n.id = a.niche_id
        LEFT JOIN static_proxies sp ON sp.id = a.static_proxy_id
        WHERE a.id = ?
    """, (account_id,))
    if not acc:
        raise HTTPException(404, "Аккаунт не найден")
    return acc


@router.post("", status_code=201)
def create_account(body: AccountCreate):
    acc_id = execute(
        "INSERT INTO accounts (niche_id, username, password, totp_secret) VALUES (?, ?, ?, ?)",
        (body.niche_id, body.username.strip(), body.password, body.totp_secret)
    )
    return query_one("SELECT * FROM accounts WHERE id = ?", (acc_id,))


@router.post("/bulk-import", status_code=201)
def bulk_import(body: AccountBulkImport):
    """Массовый импорт: login:password или login:password:2fa, по строке."""
    lines = [l.strip() for l in body.data.strip().splitlines() if l.strip()]
    if not lines:
        raise HTTPException(400, "Нет данных для импорта")

    imported = 0
    errors = []
    with get_db() as conn:
        for i, line in enumerate(lines, 1):
            parts = line.split(":")
            if len(parts) < 2:
                errors.append(f"Строка {i}: неверный формат (нужно login:password)")
                continue
            username = parts[0].strip()
            password = parts[1].strip()
            totp = parts[2].strip() if len(parts) >= 3 else None
            if not username or not password:
                errors.append(f"Строка {i}: пустой логин или пароль")
                continue
            conn.execute(
                "INSERT INTO accounts (niche_id, username, password, totp_secret) VALUES (?, ?, ?, ?)",
                (body.niche_id, username, password, totp)
            )
            imported += 1

    return {"imported": imported, "errors": errors, "total_lines": len(lines)}


@router.put("/{account_id}")
def update_account(account_id: int, body: AccountUpdate):
    acc = query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not acc:
        raise HTTPException(404, "Аккаунт не найден")

    fields = []
    params = []
    for field_name in ("niche_id", "username", "password", "totp_secret", "status", "mobile_pool_id", "notes"):
        value = getattr(body, field_name, None)
        if value is not None:
            # Лимит 45 аккаунтов на мобильный пул
            if field_name == "mobile_pool_id":
                count = query_one(
                    "SELECT COUNT(*) as cnt FROM accounts WHERE mobile_pool_id = ? AND id != ?",
                    (value, account_id),
                )
                if count and count["cnt"] >= MOBILE_POOL_MAX_ACCOUNTS:
                    raise HTTPException(
                        400,
                        f"Мобильный пул уже содержит {count['cnt']} аккаунтов (лимит {MOBILE_POOL_MAX_ACCOUNTS})",
                    )
            fields.append(f"{field_name} = ?")
            params.append(value)

    if not fields:
        raise HTTPException(400, "Нечего обновлять")

    fields.append("updated_at = ?")
    params.append(time.time())
    params.append(account_id)

    execute(f"UPDATE accounts SET {', '.join(fields)} WHERE id = ?", tuple(params))
    return query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))


@router.post("/move")
def move_accounts(body: AccountMove):
    """Перемещение аккаунтов между нишами."""
    if not body.account_ids:
        raise HTTPException(400, "Не указаны аккаунты")
    placeholders = ",".join("?" for _ in body.account_ids)
    execute(
        f"UPDATE accounts SET niche_id = ?, updated_at = ? WHERE id IN ({placeholders})",
        (body.niche_id, time.time(), *body.account_ids)
    )
    return {"ok": True, "moved": len(body.account_ids)}


@router.delete("/{account_id}")
def delete_account(account_id: int):
    acc = query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not acc:
        raise HTTPException(404, "Аккаунт не найден")
    # При удалении аккаунта — привязанный статический прокси удаляется полностью
    if acc["static_proxy_id"]:
        execute("DELETE FROM static_proxies WHERE id = ?", (acc["static_proxy_id"],))
    execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return {"ok": True}


@router.delete("")
def delete_accounts_bulk(account_ids: str):
    """Удаление пачкой: ?account_ids=1,2,3"""
    ids = [int(x.strip()) for x in account_ids.split(",") if x.strip().isdigit()]
    if not ids:
        raise HTTPException(400, "Не указаны аккаунты")
    for aid in ids:
        acc = query_one("SELECT static_proxy_id FROM accounts WHERE id = ?", (aid,))
        if acc and acc["static_proxy_id"]:
            execute("DELETE FROM static_proxies WHERE id = ?", (acc["static_proxy_id"],))
    placeholders = ",".join("?" for _ in ids)
    execute(f"DELETE FROM accounts WHERE id IN ({placeholders})", tuple(ids))
    return {"ok": True, "deleted": len(ids)}
