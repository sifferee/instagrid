"""InstaGrid — CRUD ниш (групп аккаунтов) + привязка пула прокси."""
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from backend.database import query, query_one, execute

router = APIRouter(prefix="/api/niches", tags=["niches"])


class NicheCreate(BaseModel):
    name: str
    proxy_pool_id: int | None = None

class NicheUpdate(BaseModel):
    name: str | None = None
    proxy_pool_id: int | None = None


@router.get("")
def list_niches():
    """Все ниши с количеством аккаунтов и привязанным пулом."""
    rows = query("""
        SELECT n.*, COUNT(a.id) as account_count,
               pp.name as pool_name, pp.pool_type
        FROM niches n
        LEFT JOIN accounts a ON a.niche_id = n.id
        LEFT JOIN proxy_pools pp ON pp.id = n.proxy_pool_id
        GROUP BY n.id
        ORDER BY n.name
    """)
    return rows


@router.post("", status_code=201)
def create_niche(body: NicheCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Имя ниши не может быть пустым")
    existing = query_one("SELECT id FROM niches WHERE name = ?", (name,))
    if existing:
        raise HTTPException(409, f"Ниша «{name}» уже существует")
    niche_id = execute(
        "INSERT INTO niches (name, proxy_pool_id) VALUES (?, ?)",
        (name, body.proxy_pool_id),
    )
    return query_one("SELECT * FROM niches WHERE id = ?", (niche_id,))


@router.put("/{niche_id}")
def update_niche(niche_id: int, body: NicheUpdate):
    niche = query_one("SELECT * FROM niches WHERE id = ?", (niche_id,))
    if not niche:
        raise HTTPException(404, "Ниша не найдена")

    fields, params = [], []

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "Имя ниши не может быть пустым")
        dup = query_one("SELECT id FROM niches WHERE name = ? AND id != ?", (name, niche_id))
        if dup:
            raise HTTPException(409, f"Ниша «{name}» уже существует")
        fields.append("name = ?")
        params.append(name)

    # proxy_pool_id может быть 0/None (сброс) или ID пула
    if "proxy_pool_id" in (body.model_fields_set if hasattr(body, 'model_fields_set') else set()):
        fields.append("proxy_pool_id = ?")
        params.append(body.proxy_pool_id if body.proxy_pool_id else None)

    if not fields:
        raise HTTPException(400, "Нечего обновлять")

    fields.append("updated_at = ?")
    params.extend([time.time(), niche_id])
    execute(f"UPDATE niches SET {', '.join(fields)} WHERE id = ?", tuple(params))
    return query_one("SELECT * FROM niches WHERE id = ?", (niche_id,))


@router.delete("/{niche_id}")
def delete_niche(niche_id: int):
    niche = query_one("SELECT * FROM niches WHERE id = ?", (niche_id,))
    if not niche:
        raise HTTPException(404, "Ниша не найдена")
    execute("DELETE FROM niches WHERE id = ?", (niche_id,))
    return {"ok": True}
