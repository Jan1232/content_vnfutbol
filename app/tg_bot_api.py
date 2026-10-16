"""Telegram Bot API клиент — публикация постов в TG-каналы."""

from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any

import httpx

from app.http_util import scraper_proxy

TG_API_BASE = "https://api.telegram.org"


class TgBotApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class TgBotClient:
    """Минимальный клиент Telegram Bot API для публикации в каналы."""

    def __init__(self, token: str) -> None:
        self.token = token.strip()
        proxy = scraper_proxy()
        self._client = httpx.Client(
            base_url=f"{TG_API_BASE}/bot{self.token}",
            timeout=httpx.Timeout(120.0, connect=30.0),
            proxy=proxy,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TgBotClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _request(self, method: str, **kwargs: Any) -> Any:
        for attempt in range(3):
            r = self._client.post(f"/{method}", **kwargs)
            if r.status_code == 429:
                retry_after = 1.5 * (attempt + 1)
                try:
                    retry_after = float(r.json().get("parameters", {}).get("retry_after", retry_after))
                except Exception:
                    pass
                time.sleep(retry_after)
                continue
            try:
                data = r.json()
            except Exception:
                data = {"ok": False, "description": r.text}
            if not data.get("ok"):
                raise TgBotApiError(
                    f"TG Bot API error: {data.get('description', r.text)}",
                    status=r.status_code,
                    body=data,
                )
            return data.get("result")
        raise TgBotApiError("TG Bot API rate limit exceeded")

    def get_me(self) -> dict[str, Any]:
        return self._request("getMe")

    def get_chat(self, chat_id: int | str) -> dict[str, Any]:
        return self._request("getChat", json={"chat_id": chat_id})

    def get_chat_member(self, chat_id: int | str, user_id: int) -> dict[str, Any]:
        return self._request("getChatMember", json={"chat_id": chat_id, "user_id": int(user_id)})

    def leave_chat(self, chat_id: int | str) -> bool:
        return bool(self._request("leaveChat", json={"chat_id": chat_id}))

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 1,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            body["offset"] = offset
        if allowed_updates:
            body["allowed_updates"] = allowed_updates
        result = self._request("getUpdates", json=body)
        return list(result or [])

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
        disable_notification: bool = False,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4096],
            "disable_web_page_preview": disable_web_page_preview,
            "disable_notification": disable_notification,
        }
        if parse_mode:
            body["parse_mode"] = parse_mode
        if reply_markup:
            body["reply_markup"] = reply_markup
        return self._request("sendMessage", json=body)

    def send_photo(
        self,
        chat_id: int | str,
        photo: bytes | str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        filename: str = "photo.jpg",
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Отправить одну фотографию. photo — bytes или URL."""
        if isinstance(photo, str):
            body: dict[str, Any] = {"chat_id": chat_id, "photo": photo}
            if caption:
                body["caption"] = caption[:1024]
            if parse_mode:
                body["parse_mode"] = parse_mode
            if reply_markup:
                body["reply_markup"] = reply_markup
            return self._request("sendPhoto", json=body)
        files = {"photo": (filename, photo, "image/jpeg")}
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1024]
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup:
            import json as json_mod

            data["reply_markup"] = json_mod.dumps(reply_markup, ensure_ascii=False)
        return self._request("sendPhoto", data=data, files=files)

    def send_video(
        self,
        chat_id: int | str,
        video: bytes | str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        filename: str = "video.mp4",
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(video, str):
            body: dict[str, Any] = {"chat_id": chat_id, "video": video}
            if caption:
                body["caption"] = caption[:1024]
            if parse_mode:
                body["parse_mode"] = parse_mode
            if reply_markup:
                body["reply_markup"] = reply_markup
            return self._request("sendVideo", json=body)
        files = {"video": (filename, video, "video/mp4")}
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1024]
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup:
            import json as json_mod

            data["reply_markup"] = json_mod.dumps(reply_markup, ensure_ascii=False)
        return self._request("sendVideo", data=data, files=files)

    def send_media_group(
        self,
        chat_id: int | str,
        media: list[dict[str, Any]],
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        """Отправить альбом (до 10 фото/видео).

        media — список dict: {"type": "photo"/"video", "url": "...", "bytes": b"...", "filename": "..."}
        caption ставится на первый элемент.
        """
        import json as json_mod

        input_media: list[dict[str, Any]] = []
        files: dict[str, tuple[str, bytes, str]] = {}

        for i, m in enumerate(media[:10]):
            mtype = m.get("type", "photo")
            entry: dict[str, Any] = {"type": mtype}
            if i == 0 and caption:
                entry["caption"] = caption[:1024]
                if parse_mode:
                    entry["parse_mode"] = parse_mode

            raw_bytes = m.get("bytes")
            url = m.get("url", "")
            if raw_bytes:
                fname = m.get("filename", f"media_{i}.jpg")
                attach_key = f"media_{i}"
                ctype = mimetypes.guess_type(fname)[0] or ("image/jpeg" if mtype == "photo" else "video/mp4")
                files[attach_key] = (fname, raw_bytes, ctype)
                entry["media"] = f"attach://{attach_key}"
            elif url:
                entry["media"] = url
            else:
                continue
            input_media.append(entry)

        if not input_media:
            raise TgBotApiError("No media to send")

        data: dict[str, Any] = {
            "chat_id": str(chat_id),
            "media": json_mod.dumps(input_media, ensure_ascii=False),
        }
        return self._request("sendMediaGroup", data=data, files=files or None)

    def send_document(
        self,
        chat_id: int | str,
        document: bytes | str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        filename: str = "file.bin",
    ) -> dict[str, Any]:
        if isinstance(document, str):
            body: dict[str, Any] = {"chat_id": chat_id, "document": document}
            if caption:
                body["caption"] = caption[:1024]
            if parse_mode:
                body["parse_mode"] = parse_mode
            return self._request("sendDocument", json=body)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        files = {"document": (filename, document, ctype)}
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1024]
        if parse_mode:
            data["parse_mode"] = parse_mode
        return self._request("sendDocument", data=data, files=files)
