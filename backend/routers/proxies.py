"""InstaGrid — CRUD прокси-пулов и прокси."""
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from backend.database import query, query_one, execute, get_db

router = APIRouter(prefix="/api/proxies", tags=["proxies"])


# === Модели ===

class PoolCreate(BaseModel):
    name: str
    pool_type: str  # 'static' или 'mobile'
    # mobile-only:
    rotation_url: str | None = None
    proxy_host: str | None = None
    proxy_port: int | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None


class PoolUpdate(BaseModel):
    name: str | None = None
    rotation_url: str | None = None
    proxy_host: str | None = None
    proxy_port: int | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None


class ProxyBulkAdd(BaseModel):
    pool_id: int
    data: str  # любой формат прокси, по строке


# === Пулы прокси ===

@router.get("/pools")
def list_pools():
    rows = query("""
        SELECT pp.*,
            (SELECT COUNT(*) FROM static_proxies sp WHERE sp.pool_id = pp.id) as proxy_count,
            (SELECT COUNT(*) FROM static_proxies sp WHERE sp.pool_id = pp.id AND sp.status = 'available') as available_count,
            (SELECT COUNT(*) FROM static_proxies sp WHERE sp.pool_id = pp.id AND sp.status = 'bound') as bound_count
        FROM proxy_pools pp
        ORDER BY pp.name
    """)
    return rows


@router.post("/pools", status_code=201)
def create_pool(body: PoolCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Имя пула не может быть пустым")
    if body.pool_type not in ("static", "mobile"):
        raise HTTPException(400, "Тип пула: static или mobile")
    existing = query_one("SELECT id FROM proxy_pools WHERE name = ?", (name,))
    if existing:
        raise HTTPException(409, f"Пул «{name}» уже существует")

    pool_id = execute(
        """INSERT INTO proxy_pools
           (name, pool_type, rotation_url, proxy_host, proxy_port, proxy_username, proxy_password)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (name, body.pool_type, body.rotation_url, body.proxy_host,
         body.proxy_port, body.proxy_username, body.proxy_password)
    )
    return query_one("SELECT * FROM proxy_pools WHERE id = ?", (pool_id,))


@router.put("/pools/{pool_id}")
def update_pool(pool_id: int, body: PoolUpdate):
    pool = query_one("SELECT * FROM proxy_pools WHERE id = ?", (pool_id,))
    if not pool:
        raise HTTPException(404, "Пул не найден")

    fields, params = [], []
    for f in ("name", "rotation_url", "proxy_host", "proxy_port", "proxy_username", "proxy_password"):
        v = getattr(body, f, None)
        if v is not None:
            fields.append(f"{f} = ?")
            params.append(v)
    if not fields:
        raise HTTPException(400, "Нечего обновлять")

    fields.append("updated_at = ?")
    params.extend([time.time(), pool_id])
    execute(f"UPDATE proxy_pools SET {', '.join(fields)} WHERE id = ?", tuple(params))
    return query_one("SELECT * FROM proxy_pools WHERE id = ?", (pool_id,))


@router.delete("/pools/{pool_id}")
def delete_pool(pool_id: int):
    pool = query_one("SELECT * FROM proxy_pools WHERE id = ?", (pool_id,))
    if not pool:
        raise HTTPException(404, "Пул не найден")
    # CASCADE удалит все прокси пула
    execute("DELETE FROM proxy_pools WHERE id = ?", (pool_id,))
    return {"ok": True}


# === Статические прокси ===

@router.get("/pools/{pool_id}/proxies")
def list_proxies(pool_id: int):
    pool = query_one("SELECT * FROM proxy_pools WHERE id = ?", (pool_id,))
    if not pool:
        raise HTTPException(404, "Пул не найден")
    rows = query("""
        SELECT sp.*, a.username as bound_account
        FROM static_proxies sp
        LEFT JOIN accounts a ON a.id = sp.account_id
        WHERE sp.pool_id = ?
        ORDER BY sp.id
    """, (pool_id,))
    return rows


@router.post("/bulk-add", status_code=201)
def bulk_add_proxies(body: ProxyBulkAdd):
    """
    Массовый импорт прокси — любой формат:
      login:password@hostname:port
      hostname:port:login:password
      hostname:port
      http://login:password@hostname:port
      https://login:password@hostname:port
      socks5://login:password@hostname:port
      socks5://hostname:port
    """
    from backend.services.proxy_parser import parse_proxy

    pool = query_one("SELECT * FROM proxy_pools WHERE id = ? AND pool_type = 'static'",
                     (body.pool_id,))
    if not pool:
        raise HTTPException(404, "Статический пул не найден")

    lines = [l.strip() for l in body.data.strip().splitlines() if l.strip()]
    if not lines:
        raise HTTPException(400, "Нет данных для импорта")

    imported = 0
    errors = []
    with get_db() as conn:
        for i, line in enumerate(lines, 1):
            parsed = parse_proxy(line)
            if not parsed:
                errors.append(f"Строка {i}: нераспознанный формат")
                continue

            conn.execute(
                """INSERT INTO static_proxies
                   (pool_id, host, port, username, password, protocol)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (body.pool_id, parsed["host"], parsed["port"],
                 parsed.get("username"), parsed.get("password"),
                 parsed.get("protocol", "http")),
            )
            imported += 1

    return {"imported": imported, "errors": errors, "total_lines": len(lines)}


@router.delete("/proxy/{proxy_id}")
def delete_proxy(proxy_id: int):
    proxy = query_one("SELECT * FROM static_proxies WHERE id = ?", (proxy_id,))
    if not proxy:
        raise HTTPException(404, "Прокси не найден")
    if proxy["status"] == "bound" and proxy["account_id"]:
        # Отвязать от аккаунта
        execute("UPDATE accounts SET static_proxy_id = NULL, updated_at = ? WHERE id = ?",
                (time.time(), proxy["account_id"]))
    execute("DELETE FROM static_proxies WHERE id = ?", (proxy_id,))
    return {"ok": True}
