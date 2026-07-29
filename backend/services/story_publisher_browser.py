"""
InstaGrid — Публикация сторис ИЗНУТРИ браузера.

Зачем это нужно
---------------
Прежний путь (httpx) отправлял rupload_igphoto и configure_to_story из Python:
    - TLS/JA3-отпечаток — Python+OpenSSL, а не Firefox
    - Порядок и регистр HTTP/2-заголовков, HPACK, приоритеты фреймов — свои
    - Отдельное TCP-соединение, свой TLS-хендшейк
    - Куки копировались вручную, x-ig-www-claim мог устареть

Со стороны Instagram это выглядит так: весь сеанс идёт из Firefox, а публикация
сторис прилетает с тем же sessionid, тем же User-Agent — но с TLS-стеком Python.
Совпадение отпечатка клиента и заявленного UA у них проверяется.

Здесь запрос выполняется через page.evaluate() → fetch() внутри страницы:
    - Настоящий TLS/HTTP2 Firefox
    - Куки берёт сам браузер (credentials: 'include'), ничего не копируем
    - Тот же прокси, то же соединение, тот же connection pool
    - x-ig-www-claim браузер обновляет сам

По сути это тот же запрос, который делает веб-клиент Instagram, потому что
его и делает веб-клиент Instagram.
"""

from __future__ import annotations

import base64
import json
import logging
import random
from typing import Any

logger = logging.getLogger("instagrid.story_browser")

IG_APP_ID = "936619743392459"
IG_ASBD_ID = "129477"


# ─── JS: загрузка JPEG через rupload_igphoto ─────────────────────────────────

_UPLOAD_JS = """async ({b64, entityName, uploadId, appId, asbdId}) => {
  try {
    // base64 → Uint8Array (бинарь напрямую из Python передать нельзя)
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);

    const csrf = (document.cookie.split(';')
      .map(c => c.trim())
      .find(c => c.startsWith('csrftoken=')) || '').split('=')[1] || '';

    const res = await fetch('https://i.instagram.com/rupload_igphoto/' + entityName, {
      method: 'POST',
      credentials: 'include',
      headers: {
        'content-type': 'image/jpeg',
        'offset': '0',
        'x-entity-name': entityName,
        'x-entity-length': String(bytes.length),
        'x-entity-type': 'image/jpeg',
        'x-instagram-rupload-params': JSON.stringify({
          upload_id: uploadId,
          media_type: 1,
        }),
        'x-csrftoken': csrf,
        'x-ig-app-id': appId,
        'x-asbd-id': asbdId,
        'x-requested-with': 'XMLHttpRequest',
      },
      body: bytes,
    });

    const text = await res.text();
    return {status: res.status, body: text.slice(0, 500)};
  } catch (e) {
    return {status: 0, body: '', error: String(e)};
  }
}"""


# ─── JS: публикация через configure_to_story ─────────────────────────────────

_CONFIGURE_JS = """async ({uploadId, stickersJson, appId, asbdId}) => {
  try {
    const csrf = (document.cookie.split(';')
      .map(c => c.trim())
      .find(c => c.startsWith('csrftoken=')) || '').split('=')[1] || '';

    const form = new URLSearchParams();
    form.append('upload_id', uploadId);
    form.append('source_type', '4');
    if (stickersJson) form.append('story_link_stickers', stickersJson);

    const res = await fetch(
      'https://www.instagram.com/api/v1/web/create/configure_to_story/', {
      method: 'POST',
      credentials: 'include',
      headers: {
        'content-type': 'application/x-www-form-urlencoded',
        'x-csrftoken': csrf,
        'x-ig-app-id': appId,
        'x-asbd-id': asbdId,
        'x-requested-with': 'XMLHttpRequest',
      },
      body: form.toString(),
    });

    const text = await res.text();
    return {status: res.status, body: text.slice(0, 800)};
  } catch (e) {
    return {status: 0, body: '', error: String(e)};
  }
}"""


class BrowserStoryPublisher:
    """
    Публикует сторис через fetch() внутри страницы браузера.

    Требует открытую страницу на instagram.com (нужен правильный origin,
    иначе запрос уйдёт как cross-origin и куки не подставятся).
    """

    def __init__(self, page: Any) -> None:
        self.page = page

    async def _ensure_origin(self) -> None:
        """Гарантирует, что мы на instagram.com — иначе fetch пойдёт cross-origin."""
        try:
            url = str(getattr(self.page, "url", "") or "")
        except Exception:
            url = ""
        if "instagram.com" not in url:
            await self.page.goto(
                "https://www.instagram.com/",
                wait_until="domcontentloaded",
                timeout=45_000,
            )

    async def publish(
        self,
        image_bytes: bytes,
        upload_id: str,
        link_url: str = "",
        sticker_text: str = "",
        sticker_position: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """
        Публикует сторис. Возвращает {"success", "media_id", "error"}.
        """
        await self._ensure_origin()

        entity_name = f"{upload_id}_0_{random.randint(100000000, 999999999)}"
        b64 = base64.b64encode(image_bytes).decode("ascii")

        # ── Шаг 1: загрузка изображения ──
        up = await self.page.evaluate(_UPLOAD_JS, {
            "b64": b64,
            "entityName": entity_name,
            "uploadId": upload_id,
            "appId": IG_APP_ID,
            "asbdId": IG_ASBD_ID,
        })

        if not isinstance(up, dict) or up.get("status") != 200:
            return {
                "success": False, "media_id": "",
                "error": f"rupload failed: status={up.get('status')} "
                         f"{up.get('error') or up.get('body', '')[:200]}",
            }

        # ── Шаг 2: конфигурация в сторис ──
        stickers_json = ""
        if link_url:
            pos = sticker_position or {
                "x": 0.5, "y": 0.82, "width": 0.65, "height": 0.065, "rotation": 0.0,
            }
            stickers_json = json.dumps([{
                "x": pos["x"], "y": pos["y"], "z": 0,
                "width": pos["width"], "height": pos["height"],
                "rotation": pos.get("rotation", 0.0),
                "is_sticker": True,
                "link_type": "web_link",
                "url": link_url,
                "custom_cta": sticker_text or "Learn More",
            }])

        cfg = await self.page.evaluate(_CONFIGURE_JS, {
            "uploadId": upload_id,
            "stickersJson": stickers_json,
            "appId": IG_APP_ID,
            "asbdId": IG_ASBD_ID,
        })

        if not isinstance(cfg, dict) or cfg.get("status") != 200:
            return {
                "success": False, "media_id": "",
                "error": f"configure failed: status={cfg.get('status')} "
                         f"{cfg.get('error') or cfg.get('body', '')[:200]}",
            }

        try:
            data = json.loads(cfg.get("body") or "{}")
        except Exception:
            data = {}

        if data.get("status") == "ok":
            media_id = str((data.get("media") or {}).get("pk", ""))
            logger.info("Story published in-browser: media_id=%s", media_id)
            return {"success": True, "media_id": media_id, "error": ""}

        return {
            "success": False, "media_id": "",
            "error": f"configure status={data.get('status')} {data.get('message', '')}",
        }
