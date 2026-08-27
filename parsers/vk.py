from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx

from parsers.telegram import ParsedPost

VK_RE = re.compile(
    r"(?:https?://)?(?:m\.)?vk\.(?:com|ru)/(?:club|public|wall)?(-?\d+|[A-Za-z0-9_.]+)",
    re.I,
)
WALL_RE = re.compile(r"wall(-?\d+)_(\d+)", re.I)


def resolve_vk_owner(url: str, token: str = "") -> tuple[int, str]:
    """Возвращает (owner_id, title). owner_id отрицательный для сообществ."""
    url = url.strip()
    m = WALL_RE.search(url)
    if m:
        return int(m.group(1)), ""

    path = urlparse(url if "://" in url else f"https://{url}").path.strip("/")
    screen = path.split("/")[0]
    if screen.startswith("club"):
        return -int(screen[4:]), ""
    if screen.startswith("public"):
        return -int(screen[6:]), ""
    if screen.startswith("id") and screen[2:].isdigit():
        return int(screen[2:]), ""

    # screen name via API
    params: dict[str, Any] = {
        "v": "5.199",
        "screen_name": screen,
    }
    if token:
        params["access_token"] = token
    else:
        # публичный метод иногда требует токен — пробуем без / с service
        params["access_token"] = token or "anonymous"

    with httpx.Client(timeout=30.0) as client:
        # utils.resolveScreenName
        r = client.get("https://api.vk.com/method/utils.resolveScreenName", params={
            "v": "5.199",
            "screen_name": screen,
            **({"access_token": token} if token else {}),
        })
        data = r.json()
        resp = data.get("response")
        if resp and resp.get("type") in {"group", "page", "event"}:
            return -int(resp["object_id"]), screen
        if resp and resp.get("type") == "user":
            return int(resp["object_id"]), screen

        # fallback: groups.getById
        if token:
            r2 = client.get(
                "https://api.vk.com/method/groups.getById",
                params={"v": "5.199", "group_id": screen, "access_token": token},
            )
            d2 = r2.json()
            groups = d2.get("response")
            if isinstance(groups, dict):
                groups = groups.get("groups") or []
            if groups:
                g = groups[0]
                return -int(g["id"]), g.get("name") or screen

    raise ValueError(
        "Не удалось определить сообщество VK. Укажите ссылку вида https://vk.com/public123 "
        "или задайте VK_ACCESS_TOKEN в .env"
    )


def parse_vk(url: str, since_id: str = "", token: str = "") -> tuple[str, list[ParsedPost]]:
    owner_id, title_hint = resolve_vk_owner(url, token=token)
    params: dict[str, Any] = {
        "v": "5.199",
        "owner_id": owner_id,
        "count": 20,
        "filter": "owner",
    }
    if token:
        params["access_token"] = token

    with httpx.Client(timeout=30.0) as client:
        r = client.get("https://api.vk.com/method/wall.get", params=params)
        data = r.json()

    if "error" in data:
        err = data["error"]
        # Without token public wall may fail
        raise ValueError(f"VK API: {err.get('error_msg') or err}. Нужен VK_ACCESS_TOKEN для надёжного парсинга.")

    response = data.get("response") or {}
    items = response.get("items") or []
    title = title_hint or f"VK {owner_id}"

    since_num = None
    if since_id.startswith("vk:"):
        try:
            since_num = int(since_id.split("_")[-1])
        except ValueError:
            since_num = None

    posts: list[ParsedPost] = []
    for item in reversed(items):  # wall.get отдаёт новые первыми
        post_id = int(item["id"])
        if since_num is not None and post_id <= since_num:
            continue
        if item.get("marked_as_ads") or item.get("post_type") == "suggest":
            # всё равно положим как пост — фильтр рекламы отдельно; marked_as_ads пометим в тексте
            pass

        text = item.get("text") or ""
        media: list[dict[str, Any]] = []
        for att in item.get("attachments") or []:
            if att.get("type") == "photo":
                sizes = (att.get("photo") or {}).get("sizes") or []
                if sizes:
                    best = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
                    if best.get("url"):
                        media.append({"type": "image", "url": best["url"]})
            elif att.get("type") == "link":
                # наличие внешней ссылки — попадёт в фильтр через URL в тексте
                link = att.get("link") or {}
                if link.get("url"):
                    text = f"{text}\n{link['url']}".strip()

        external_id = f"vk:{owner_id}_{post_id}"
        posts.append(
            ParsedPost(
                external_id=external_id,
                text=text,
                media=media,
                source_url=f"https://vk.com/wall{owner_id}_{post_id}",
                title=title,
            )
        )

    return title, posts
