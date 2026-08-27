from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

# Системное хранилище CA включает сертификаты Минцифры (нужны для platform-api2.max.ru)
SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"


class MaxApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _verify() -> str | bool:
    return SYSTEM_CA if Path(SYSTEM_CA).exists() else True


class MaxClient:
    def __init__(self, token: str | None = None, base: str | None = None) -> None:
        settings = get_settings()
        self.token = token or settings.max_bot_token
        self.base = (base or settings.max_api_base).rstrip("/")
        self._client = httpx.Client(
            base_url=self.base,
            headers={"Authorization": self.token},
            timeout=httpx.Timeout(60.0, connect=15.0),
            verify=_verify(),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MaxClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(5):
            r = self._client.request(method, path, **kwargs)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except Exception:
                    body = r.text
                raise MaxApiError(f"MAX API {r.status_code}: {body}", r.status_code, body)
            if r.status_code == 204 or not r.content:
                return None
            return r.json()
        raise MaxApiError("MAX API rate limit")

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/me")

    def list_chats(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/chats")
        if isinstance(data, dict):
            return list(data.get("chats") or [])
        return []

    def get_chat(self, chat_id: int) -> dict[str, Any]:
        return self._request("GET", f"/chats/{chat_id}")

    def patch_chat(
        self,
        chat_id: int,
        *,
        title: str | None = None,
        notify: bool = False,
        icon_url: str | None = None,
        pin: str | None = None,
    ) -> dict[str, Any]:
        """Изменить канал/чат. notify=False — без системного уведомления."""
        body: dict[str, Any] = {"notify": bool(notify)}
        if title is not None:
            body["title"] = (title or "")[:200]
        if icon_url is not None:
            body["icon"] = {"url": icon_url}
        if pin is not None:
            body["pin"] = pin
        return self._request("PATCH", f"/chats/{chat_id}", json=body)

    def delete_message(self, message_id: str) -> dict[str, Any] | None:
        """Удалить сообщение/пост по mid."""
        mid = (message_id or "").strip()
        if not mid:
            raise ValueError("message_id пуст")
        return self._request("DELETE", "/messages", params={"message_id": mid})

    def pin_message(
        self,
        chat_id: int,
        message_id: str,
        *,
        notify: bool = False,
    ) -> dict[str, Any] | None:
        """Закрепить пост. notify=False — без системного уведомления."""
        mid = (message_id or "").strip()
        if not mid:
            raise ValueError("message_id пуст")
        return self._request(
            "PUT",
            f"/chats/{chat_id}/pin",
            json={"message_id": mid, "notify": bool(notify)},
        )

    def get_updates(
        self,
        marker: int | None = None,
        limit: int = 100,
        timeout: int = 30,
        types: list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "timeout": timeout}
        if marker is not None:
            params["marker"] = marker
        if types:
            params["types"] = ",".join(types)
        return self._request("GET", "/updates", params=params)

    def send_message(
        self,
        chat_id: int,
        text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        notify: bool = True,
        disable_link_preview: bool = True,
        format: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"notify": notify}
        if text is not None:
            body["text"] = text[:4000]
        if attachments is not None:
            body["attachments"] = attachments
        if format:
            body["format"] = format
        return self._request(
            "POST",
            "/messages",
            params={"chat_id": chat_id, "disable_link_preview": str(disable_link_preview).lower()},
            json=body,
        )

    def upload_image_bytes(self, content: bytes, filename: str = "image.jpg", ctype: str = "image/jpeg") -> dict[str, Any] | None:
        """Загрузить байты изображения в MAX."""
        up = self._request("POST", "/uploads", params={"type": "image"})
        upload_url = up.get("url")
        if not upload_url:
            return None
        files = {"data": (filename, content, ctype)}
        with httpx.Client(timeout=120.0, verify=_verify()) as c:
            ur = c.post(upload_url, files=files, headers={"Authorization": self.token})
            if ur.status_code >= 400:
                print(f"[max] upload fail: {ur.status_code} {ur.text[:200]}", flush=True)
                return None
            try:
                payload = ur.json()
            except Exception:
                return None

        token = payload.get("token")
        if token:
            return {"type": "image", "payload": {"token": token}}
        photos = payload.get("photos") or payload.get("payload")
        if isinstance(photos, dict):
            for v in photos.values():
                if isinstance(v, dict) and v.get("token"):
                    return {"type": "image", "payload": {"token": v["token"]}}
        if isinstance(payload, dict):
            for v in payload.values():
                if isinstance(v, dict) and v.get("token"):
                    return {"type": "image", "payload": {"token": v["token"]}}
        print(f"[max] upload unknown payload: {payload}", flush=True)
        return None

    def upload_image_from_url(self, image_url: str, watermark_text: str = "") -> dict[str, Any] | None:
        """Скачать картинку, опционально наложить вотермарку и загрузить в MAX."""
        from app.http_util import scraper_proxy
        from app.watermark import apply_text_watermark

        proxy = scraper_proxy()
        try:
            with httpx.Client(
                timeout=60.0,
                follow_redirects=True,
                verify=_verify(),
                proxy=proxy,
            ) as c:
                img = c.get(image_url)
                img.raise_for_status()
                content = img.content
        except Exception as e:
            print(f"[max] image download fail: {e}", flush=True)
            return None

        if (watermark_text or "").strip():
            try:
                content = apply_text_watermark(content, watermark_text)
            except Exception as e:
                print(f"[max] watermark fail: {e}", flush=True)

        return self.upload_image_bytes(content, filename="image.jpg", ctype="image/jpeg")

    def upload_video_bytes(
        self,
        content: bytes,
        filename: str = "video.mp4",
        ctype: str = "video/mp4",
    ) -> dict[str, Any] | None:
        """Загрузить байты видео в MAX (лимит API ~250 MB)."""
        if not content:
            return None
        max_bytes = 250 * 1024 * 1024
        if len(content) > max_bytes:
            print(f"[max] video too large for MAX: {len(content)} bytes", flush=True)
            return None

        up = self._request("POST", "/uploads", params={"type": "video"})
        upload_url = up.get("url")
        token = up.get("token")
        if not upload_url:
            return None

        files = {"data": (filename, content, ctype or "video/mp4")}
        with httpx.Client(timeout=300.0, verify=_verify()) as c:
            ur = c.post(upload_url, files=files, headers={"Authorization": self.token})
            if ur.status_code >= 400:
                print(f"[max] video upload fail: {ur.status_code} {ur.text[:200]}", flush=True)
                return None
            try:
                payload = ur.json() if ur.content else {}
            except Exception:
                payload = {}

        token = token or (payload.get("token") if isinstance(payload, dict) else None)
        if not token:
            print(f"[max] video upload no token: up={up} payload={payload}", flush=True)
            return None
        return {"type": "video", "payload": {"token": token}}

    def upload_video_from_path(self, path: str | Path) -> dict[str, Any] | None:
        p = Path(path)
        if not p.is_file():
            print(f"[max] video path missing: {p}", flush=True)
            return None
        ctype = mimetypes.guess_type(str(p))[0] or "video/mp4"
        try:
            content = p.read_bytes()
        except Exception as e:
            print(f"[max] video read fail: {e}", flush=True)
            return None
        return self.upload_video_bytes(content, filename=p.name, ctype=ctype)

    def upload_video_from_url(self, video_url: str) -> dict[str, Any] | None:
        """Скачать видео и загрузить в MAX (без вотермарки). Поддерживает file://."""
        raw = (video_url or "").strip()
        if raw.startswith("file://"):
            from urllib.parse import unquote, urlparse

            return self.upload_video_from_path(unquote(urlparse(raw).path))
        if raw.startswith("/") and Path(raw).is_file():
            return self.upload_video_from_path(raw)

        from app.http_util import scraper_proxy

        proxy = scraper_proxy()
        try:
            with httpx.Client(
                timeout=180.0,
                follow_redirects=True,
                verify=_verify(),
                proxy=proxy,
            ) as c:
                vid = c.get(raw)
                vid.raise_for_status()
                content = vid.content
                ctype = (vid.headers.get("content-type") or "video/mp4").split(";")[0].strip()
        except Exception as e:
            print(f"[max] video download fail: {e}", flush=True)
            return None

        return self.upload_video_bytes(content, ctype=ctype or "video/mp4")
