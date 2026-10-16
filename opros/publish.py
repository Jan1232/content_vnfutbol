"""Сборка текста поста и отправка в MAX."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.max_api import MaxClient
from opros.channels import chat_id_for, get_channel
from opros.football import FootballPair
from opros.google_images import fetch_first_image
from opros.quiz import QuizItem, format_quiz_text
from opros.quiz_images import pick_image


FOOTER = (
    "Отправь нужную реакцию:\n"
    "\n"
    "◯   Да (✅)\n"
    "⠀⠀⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
    "◯   Нет (🛑 )"
)


def format_post(question: str) -> str:
    q = (question or "").strip()
    text = f"{q}\n\n{FOOTER}".strip()
    if len(text) > 4000:
        budget = 4000 - len(FOOTER) - 2
        q = q[: max(0, budget - 3)].rstrip() + "..."
        text = f"{q}\n\n{FOOTER}".strip()
    return text


def format_football_post(channel_key: str, pair: FootballPair) -> tuple[str, str]:
    """Возвращает (question, full_text)."""
    ch = get_channel(channel_key)
    if ch.kind == "football_who":
        question = pair.question_who
        text = (
            f"{question}\n"
            f"\n"
            f"❤️ - {pair.option_b}\n"
            f"👑 - {pair.option_a}\n"
            f"\n"
            f"👇 поясни свой ответ в комментариях"
        )
    else:
        question = pair.question_or
        text = (
            f"{question}\n"
            f"\n"
            f"❤️ - {pair.option_b}\n"
            f"👑 - {pair.option_a}"
        )
    return question, text.strip()


def publish_poll(
    client: MaxClient,
    *,
    channel_key: str,
    question: str,
) -> dict[str, Any]:
    text = format_post(question)
    chat_id = chat_id_for(channel_key)
    resp = client.send_message(
        chat_id=chat_id,
        text=text,
        notify=True,
        disable_link_preview=True,
    )
    return {"response": resp, "text": text, "chat_id": chat_id}


def _upload_pair_images(
    client: MaxClient,
    pair: FootballPair,
) -> tuple[list[dict[str, Any]], str, str]:
    """Две картинки (A затем B). Возвращает attachments, url_a, url_b."""
    attachments: list[dict[str, Any]] = []
    urls: list[str] = []
    for label, name in (("a", pair.option_a), ("b", pair.option_b)):
        query = f"{name} футбол"
        url, path, reason = fetch_first_image(query, dest_stem=None)
        print(f"[opros] image {label} q={query!r} reason={reason} url={url!r}", flush=True)
        urls.append(url or "")
        att = None
        if path and Path(path).is_file():
            att = client.upload_image_from_path(path)
            time.sleep(0.4)
        elif url:
            att = client.upload_image_from_url(url)
            time.sleep(0.4)
        if att:
            attachments.append(att)
    return attachments, urls[0] if urls else "", urls[1] if len(urls) > 1 else ""


def publish_football(
    client: MaxClient,
    *,
    channel_key: str,
    pair: FootballPair,
) -> dict[str, Any]:
    question, text = format_football_post(channel_key, pair)
    chat_id = chat_id_for(channel_key)
    attachments, url_a, url_b = _upload_pair_images(client, pair)
    if len(attachments) < 2:
        raise RuntimeError(
            f"need 2 images, got {len(attachments)} for {pair.option_a!r} vs {pair.option_b!r}"
        )
    resp = client.send_message(
        chat_id=chat_id,
        text=text,
        attachments=attachments,
        notify=True,
        disable_link_preview=True,
    )
    return {
        "response": resp,
        "text": text,
        "question": question,
        "chat_id": chat_id,
        "image_a_url": url_a,
        "image_b_url": url_b,
    }


def publish_quiz(
    client: MaxClient,
    *,
    channel_key: str,
    item: QuizItem,
    used_image_names: set[str] | None = None,
) -> dict[str, Any]:
    text = format_quiz_text(item)
    chat_id = chat_id_for(channel_key)
    attachments: list[dict[str, Any]] = []
    image_path = pick_image(
        item.kind,
        channel_key=channel_key,
        used_names=used_image_names,
    )
    image_ref = ""
    if image_path is not None:
        att = client.upload_image_from_path(image_path)
        if att:
            attachments.append(att)
            image_ref = str(image_path)
            print(f"[opros] quiz image kind={item.kind} channel={channel_key} path={image_path}", flush=True)
        else:
            print(f"[opros] quiz image upload fail path={image_path}", flush=True)
    else:
        print(
            f"[opros] quiz no local image for kind={item.kind} channel={channel_key}",
            flush=True,
        )

    resp = client.send_message(
        chat_id=chat_id,
        text=text,
        attachments=attachments or None,
        notify=True,
        disable_link_preview=True,
    )
    return {
        "response": resp,
        "text": text,
        "question": item.question,
        "chat_id": chat_id,
        "image_a_url": image_ref,
        "answer": item.answer,
        "kind": item.kind,
    }


def extract_message_id(resp: dict[str, Any] | None) -> str:
    if not isinstance(resp, dict):
        return ""
    for key in ("mid", "message_id", "id"):
        if isinstance(resp.get(key), str) and resp[key]:
            return resp[key]
    msg = resp.get("message")
    if isinstance(msg, dict):
        body = msg.get("body") if isinstance(msg.get("body"), dict) else msg
        if isinstance(body, dict):
            for key in ("mid", "message_id", "id"):
                if isinstance(body.get(key), str) and body[key]:
                    return body[key]
        for key in ("mid", "message_id", "id"):
            if isinstance(msg.get(key), str) and msg[key]:
                return msg[key]
    return ""
