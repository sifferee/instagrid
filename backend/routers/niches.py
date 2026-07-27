"""InstaGrid — CRUD ниш (групп аккаунтов)."""
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from backend.database import query, query_one, execute

router = APIRouter(prefix="/api/niches", tags=["niches"])


class NicheCreate(BaseModel):
    name: str

class NicheRename(BaseModel):
    name: str


@router.get("")
def list_niches():
    """Все ниши с количеством аккаунтов."""
    rows = query("""
        SELECT n.*, COUNT(a.id) as account_count
        FROM niches n
        LEFT JOIN accounts a ON a.niche_id = n.id
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
    niche_id = execute("INSERT INTO niches (name) VALUES (?)", (name,))
    return query_one("SELECT * FROM niches WHERE id = ?", (niche_id,))


@router.put("/{niche_id}")
def rename_niche(niche_id: int, body: NicheRename):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Имя ниши не может быть пустым")
    niche = query_one("SELECT * FROM niches WHERE id = ?", (niche_id,))
    if not niche:
        raise HTTPException(404, "Ниша не найдена")
    dup = query_one("SELECT id FROM niches WHERE name = ? AND id != ?", (name, niche_id))
    if dup:
        raise HTTPException(409, f"Ниша «{name}» уже существует")
    execute("UPDATE niches SET name = ?, updated_at = ? WHERE id = ?",
            (name, time.time(), niche_id))
    return query_one("SELECT * FROM niches WHERE id = ?", (niche_id,))


@router.delete("/{niche_id}")
def delete_niche(niche_id: int):
    niche = query_one("SELECT * FROM niches WHERE id = ?", (niche_id,))
    if not niche:
        raise HTTPException(404, "Ниша не найдена")
    # Аккаунты ниши не удаляются — у них niche_id станет NULL (ON DELETE SET NULL)
    execute("DELETE FROM niches WHERE id = ?", (niche_id,))
    return {"ok": True}
