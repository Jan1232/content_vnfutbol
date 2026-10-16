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
        # GET /chats удалён из MAX API в июне 2026. Каналы уже хранятся
        # локально, а новые/удалённые отслеживаются через GET /updates.
        try:
            data = self._request("GET", "/chats")
        except MaxApiError as e:
            body = e.body if isinstance(e.body, dict) else {}
            if e.status == 404 and body.get("code") == "method.not.found":
                return []
            raise
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
        chat_id: int | None = None,
        text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        notify: bool = True,
        disable_link_preview: bool = True,
        format: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"notify": notify}
        if text is not None:
            body["text"] = text[:4000]
        if attachments is not None:
            body["attachments"] = attachments
        if format:
            body["format"] = format
        params: dict[str, Any] = {
            "disable_link_preview": str(disable_link_preview).lower(),
        }
        if user_id is not None:
            params["user_id"] = int(user_id)
        elif chat_id is not None:
            params["chat_id"] = int(chat_id)
        else:
            raise ValueError("нужен chat_id или user_id")
        return self._request("POST", "/messages", params=params, json=body)

    def list_messages(
        self,
        chat_id: int,
        *,
        count: int = 50,
        from_ms: int | None = None,
        to_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "chat_id": int(chat_id),
            "count": max(1, min(100, int(count))),
        }
        if from_ms is not None:
            params["from"] = int(from_ms)
        if to_ms is not None:
            params["to"] = int(to_ms)
        data = self._request("GET", "/messages", params=params)
        msgs = data.get("messages") if isinstance(data, dict) else None
        return [m for m in (msgs or []) if isinstance(m, dict)]

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

    def upload_image_from_path(
        self,
        path: str | Path,
        watermark_text: str = "",
        filename: str | None = None,
    ) -> dict[str, Any] | None:
        """Загрузить локальный файл как image (опционально с вотермаркой)."""
        from app.watermark import apply_text_watermark

        p = Path(path)
        if not p.is_file():
            print(f"[max] image path missing: {p}", flush=True)
            return None
        try:
            content = p.read_bytes()
        except Exception as e:
            print(f"[max] image read fail: {e}", flush=True)
            return None
        if (watermark_text or "").strip():
            try:
                content = apply_text_watermark(content, watermark_text)
            except Exception as e:
                print(f"[max] watermark fail: {e}", flush=True)
        name = filename or p.name or "image.jpg"
        ctype = mimetypes.guess_type(name)[0] or "image/jpeg"
        return self.upload_image_bytes(content, filename=name, ctype=ctype)

    def upload_image_from_url(self, image_url: str, watermark_text: str = "") -> dict[str, Any] | None:
        """Скачать картинку, опционально наложить вотермарку и загрузить в MAX.

        Поддерживает file:// и абсолютные локальные пути (после Telethon).
        """
        from app.http_util import scraper_proxy
        from app.watermark import apply_text_watermark

        raw = (image_url or "").strip()
        if raw.startswith("file://"):
            from urllib.parse import unquote, urlparse

            return self.upload_image_from_path(
                unquote(urlparse(raw).path), watermark_text=watermark_text
            )
        if raw.startswith("/") and Path(raw).is_file():
            return self.upload_image_from_path(raw, watermark_text=watermark_text)

        proxy = scraper_proxy()
        try:
            with httpx.Client(
                timeout=60.0,
                follow_redirects=True,
                verify=_verify(),
                proxy=proxy,
            ) as c:
                img = c.get(raw)
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

    def upload_file_bytes(
        self,
        content: bytes,
        filename: str = "file.bin",
        ctype: str = "application/octet-stream",
    ) -> dict[str, Any] | None:
        """Загрузить произвольный файл в MAX (type=file)."""
        if not content:
            return None
        up = self._request("POST", "/uploads", params={"type": "file"})
        upload_url = up.get("url")
        if not upload_url:
            return None
        files = {"data": (filename or "file.bin", content, ctype or "application/octet-stream")}
        with httpx.Client(timeout=300.0, verify=_verify()) as c:
            ur = c.post(upload_url, files=files, headers={"Authorization": self.token})
            if ur.status_code >= 400:
                print(f"[max] file upload fail: {ur.status_code} {ur.text[:200]}", flush=True)
                return None
            try:
                payload = ur.json() if ur.content else {}
            except Exception:
                payload = {}

        token = None
        if isinstance(payload, dict):
            token = payload.get("token")
            if not token:
                for v in payload.values():
                    if isinstance(v, dict) and v.get("token"):
                        token = v["token"]
                        break
        if not token:
            print(f"[max] file upload no token: payload={payload}", flush=True)
            return None
        return {"type": "file", "payload": {"token": token}}

    def upload_file_from_path(
        self,
        path: str | Path,
        filename: str | None = None,
    ) -> dict[str, Any] | None:
        p = Path(path)
        if not p.is_file():
            print(f"[max] file path missing: {p}", flush=True)
            return None
        name = filename or p.name or "file.bin"
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            content = p.read_bytes()
        except Exception as e:
            print(f"[max] file read fail: {e}", flush=True)
            return None
        return self.upload_file_bytes(content, filename=name, ctype=ctype)

    def upload_file_from_url(
        self,
        file_url: str,
        filename: str | None = None,
    ) -> dict[str, Any] | None:
        """Загрузить файл по URL / file:// / локальному пути как type=file."""
        raw = (file_url or "").strip()
        if raw.startswith("file://"):
            from urllib.parse import unquote, urlparse

            return self.upload_file_from_path(
                unquote(urlparse(raw).path), filename=filename
            )
        if raw.startswith("/") and Path(raw).is_file():
            return self.upload_file_from_path(raw, filename=filename)

        from app.http_util import scraper_proxy

        proxy = scraper_proxy()
        try:
            with httpx.Client(
                timeout=180.0,
                follow_redirects=True,
                verify=_verify(),
                proxy=proxy,
            ) as c:
                resp = c.get(raw)
                resp.raise_for_status()
                content = resp.content
                ctype = (resp.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
        except Exception as e:
            print(f"[max] file download fail: {e}", flush=True)
            return None
        name = filename or "file.bin"
        return self.upload_file_bytes(content, filename=name, ctype=ctype or "application/octet-stream")

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


def extract_max_message_id(resp: dict[str, Any] | None) -> str:
    """mid опубликованного сообщения из ответа POST /messages."""
    if not isinstance(resp, dict):
        return ""

    def _pick(d: dict[str, Any]) -> str:
        for key in ("mid", "message_id", "id"):
            val = d.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, int) and val:
                return str(val)
        return ""

    found = _pick(resp)
    if found:
        return found
    msg = resp.get("message")
    if isinstance(msg, dict):
        body = msg.get("body") if isinstance(msg.get("body"), dict) else msg
        if isinstance(body, dict):
            found = _pick(body)
            if found:
                return found
        found = _pick(msg)
        if found:
            return found
    return ""
