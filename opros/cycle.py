"""Одна публикация опроса и выбор due-канала."""

from __future__ import annotations

import time
from typing import Any

from app.max_api import MaxClient
from opros.channels import channel_keys, get_channel, is_football, is_quiz
from opros.comment import extract_comment_mid, post_quiz_answer_comment
from opros.db import (
    count_posts,
    db,
    get_next_publish_ts,
    init_db,
    insert_post,
    last_quiz_kind,
    make_pair_key,
    pair_exists,
    question_exists,
    recent_pairs,
    recent_questions,
    recent_quiz_answers,
    recent_quiz_image_names,
    set_last_error,
    set_next_publish_ts,
)
from opros.football import generate_football_pair
from opros.generate import generate_question
from opros.publish import extract_message_id, publish_football, publish_poll, publish_quiz
from opros.quiz import QuizKind, generate_quiz
from opros.quiz_images import ensure_quiz_asset_dirs
from opros.schedule import is_quiet, next_publish_at, next_window_start, now_local


def _pick_due_channel(
    *,
    force: bool = False,
    prefer: str | None = None,
) -> str | None:
    keys = channel_keys()
    if prefer:
        if prefer not in keys:
            raise KeyError(f"unknown channel: {prefer}")
        return prefer

    now_ts = time.time()
    due: list[tuple[float, str]] = []
    with db() as conn:
        for key in keys:
            ts = get_next_publish_ts(conn, key)
            if ts is None:
                due.append((0.0, key))
            elif force or ts <= now_ts:
                due.append((ts, key))
    if not due:
        return None
    due.sort(key=lambda x: (x[0], x[1]))
    return due[0][1]


def _earliest_next_ts() -> float | None:
    values: list[float] = []
    with db() as conn:
        for key in channel_keys():
            ts = get_next_publish_ts(conn, key)
            if ts is not None:
                values.append(ts)
    return min(values) if values else None


def _backoff(key: str, err: str) -> dict[str, Any]:
    nxt = next_publish_at(interval_sec=600)
    with db() as conn:
        set_last_error(conn, err)
        set_next_publish_ts(conn, key, nxt.timestamp())
    return {"action": "error", "channel": key, "error": err, "next": nxt.isoformat()}


def _publish_yesno(client: MaxClient, key: str) -> dict[str, Any]:
    ch = get_channel(key)
    with db() as conn:
        recent = recent_questions(conn, key, limit=30)

    try:
        question = generate_question(key, recent=recent)
        with db() as conn:
            if question_exists(conn, key, question):
                recent = [question] + recent
                question = generate_question(key, recent=recent)
                if question_exists(conn, key, question):
                    raise RuntimeError(f"duplicate after retry: {question}")
    except Exception as e:
        print(f"[opros] generate fail channel={key}: {e}", flush=True)
        return _backoff(key, str(e))

    try:
        sent = publish_poll(client, channel_key=key, question=question)
    except Exception as e:
        print(f"[opros] publish fail channel={key}: {e}", flush=True)
        return _backoff(key, str(e))

    mid = extract_message_id(sent.get("response") if isinstance(sent, dict) else None)
    nxt = next_publish_at()
    with db() as conn:
        post_id = insert_post(
            conn,
            channel_key=key,
            question=question,
            text=str(sent.get("text") or ""),
            message_id=mid,
            status="published",
        )
        set_next_publish_ts(conn, key, nxt.timestamp())
        set_last_error(conn, "")
        total = count_posts(conn, key)

    print(
        f"[opros] published channel={key} title={ch.title!r} id={post_id} "
        f"mid={mid or '-'} next={nxt.isoformat()} total={total} q={question!r}",
        flush=True,
    )
    return {
        "action": "published",
        "channel": key,
        "title": ch.title,
        "id": post_id,
        "question": question,
        "message_id": mid,
        "chat_id": sent.get("chat_id"),
        "next": nxt.isoformat(),
        "total": total,
    }


def _publish_football(client: MaxClient, key: str) -> dict[str, Any]:
    ch = get_channel(key)
    with db() as conn:
        recent = recent_pairs(conn, limit=40)

    try:
        pair = generate_football_pair(key, recent=recent)
        pkey = make_pair_key(pair.option_a, pair.option_b)
        with db() as conn:
            if pair_exists(conn, pkey):
                recent = [(pair.option_a, pair.option_b)] + recent
                pair = generate_football_pair(key, recent=recent)
                pkey = make_pair_key(pair.option_a, pair.option_b)
                if pair_exists(conn, pkey):
                    raise RuntimeError(
                        f"duplicate pair after retry: {pair.option_a} vs {pair.option_b}"
                    )
    except Exception as e:
        print(f"[opros] football generate fail channel={key}: {e}", flush=True)
        return _backoff(key, str(e))

    try:
        sent = publish_football(client, channel_key=key, pair=pair)
    except Exception as e:
        print(f"[opros] football publish fail channel={key}: {e}", flush=True)
        return _backoff(key, str(e))

    question = str(sent.get("question") or "")
    mid = extract_message_id(sent.get("response") if isinstance(sent, dict) else None)
    nxt = next_publish_at()
    pkey = make_pair_key(pair.option_a, pair.option_b)
    with db() as conn:
        post_id = insert_post(
            conn,
            channel_key=key,
            question=question,
            text=str(sent.get("text") or ""),
            message_id=mid,
            status="published",
            option_a=pair.option_a,
            option_b=pair.option_b,
            pair_key=pkey,
            image_a_url=str(sent.get("image_a_url") or ""),
            image_b_url=str(sent.get("image_b_url") or ""),
        )
        set_next_publish_ts(conn, key, nxt.timestamp())
        set_last_error(conn, "")
        total = count_posts(conn, key)

    print(
        f"[opros] published channel={key} title={ch.title!r} id={post_id} "
        f"mid={mid or '-'} next={nxt.isoformat()} total={total} "
        f"pair={pair.option_a!r} vs {pair.option_b!r}",
        flush=True,
    )
    return {
        "action": "published",
        "channel": key,
        "title": ch.title,
        "id": post_id,
        "question": question,
        "option_a": pair.option_a,
        "option_b": pair.option_b,
        "message_id": mid,
        "chat_id": sent.get("chat_id"),
        "next": nxt.isoformat(),
        "total": total,
    }


def _next_quiz_kind(channel_key: str) -> QuizKind:
    with db() as conn:
        last = last_quiz_kind(conn, channel_key)
    if last == "player":
        return "club"
    if last == "club":
        return "player"
    return "player"


def _publish_quiz(client: MaxClient, key: str) -> dict[str, Any]:
    ensure_quiz_asset_dirs()
    ch = get_channel(key)
    kind = _next_quiz_kind(key)
    with db() as conn:
        recent = recent_quiz_answers(conn, limit=40)
        used_images = recent_quiz_image_names(conn, kind, limit=80)

    try:
        item = generate_quiz(key, kind, recent_answers=recent)
        with db() as conn:
            if pair_exists(conn, item.answer_key):
                recent = [item.answer] + recent
                item = generate_quiz(key, kind, recent_answers=recent)
                if pair_exists(conn, item.answer_key):
                    raise RuntimeError(f"duplicate quiz answer after retry: {item.answer}")
    except Exception as e:
        print(f"[opros] quiz generate fail channel={key}: {e}", flush=True)
        return _backoff(key, str(e))

    try:
        sent = publish_quiz(
            client,
            channel_key=key,
            item=item,
            used_image_names=used_images,
        )
    except Exception as e:
        print(f"[opros] quiz publish fail channel={key}: {e}", flush=True)
        return _backoff(key, str(e))

    mid = extract_message_id(sent.get("response") if isinstance(sent, dict) else None)
    nxt = next_publish_at()
    comment_mid = ""
    chat_id = int(sent.get("chat_id") or 0)
    if mid and chat_id:
        try:
            cre = post_quiz_answer_comment(
                chat_id=chat_id,
                post_message_id=mid,
                answer=item.answer,
            )
            comment_mid = extract_comment_mid(
                cre.get("response") if isinstance(cre, dict) else None
            )
            print(
                f"[opros] quiz comment ok channel={key} mid={comment_mid or '-'} "
                f"answer={item.answer!r}",
                flush=True,
            )
        except Exception as e:
            print(f"[opros] quiz comment fail channel={key}: {e}", flush=True)

    with db() as conn:
        post_id = insert_post(
            conn,
            channel_key=key,
            question=str(sent.get("question") or item.question),
            text=str(sent.get("text") or ""),
            message_id=mid,
            status="published",
            option_a=item.answer,
            option_b=item.kind,
            pair_key=item.answer_key,
            image_a_url=str(sent.get("image_a_url") or ""),
            comment_message_id=comment_mid,
        )
        set_next_publish_ts(conn, key, nxt.timestamp())
        set_last_error(conn, "")
        total = count_posts(conn, key)

    print(
        f"[opros] published channel={key} title={ch.title!r} id={post_id} "
        f"mid={mid or '-'} next={nxt.isoformat()} total={total} "
        f"quiz={item.kind} answer={item.answer!r}",
        flush=True,
    )
    return {
        "action": "published",
        "channel": key,
        "title": ch.title,
        "id": post_id,
        "question": item.question,
        "quiz_kind": item.kind,
        "answer": item.answer,
        "message_id": mid,
        "comment_message_id": comment_mid,
        "chat_id": sent.get("chat_id"),
        "next": nxt.isoformat(),
        "total": total,
    }


def publish_one(
    client: MaxClient | None = None,
    *,
    force: bool = False,
    channel: str | None = None,
) -> dict[str, Any]:
    init_db()
    ensure_quiz_asset_dirs()
    now = now_local()
    if not force and is_quiet(now):
        nxt = next_window_start(now)
        with db() as conn:
            for key in channel_keys():
                cur = get_next_publish_ts(conn, key)
                if cur is None or cur < nxt.timestamp():
                    set_next_publish_ts(conn, key, nxt.timestamp())
        return {"action": "quiet", "until": nxt.isoformat()}

    key = _pick_due_channel(force=force, prefer=channel)
    if key is None:
        earliest = _earliest_next_ts()
        return {
            "action": "wait",
            "until_ts": earliest,
            "until": None
            if earliest is None
            else time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(earliest)),
        }

    own_client = client is None
    client = client or MaxClient()
    try:
        if is_quiz(key):
            return _publish_quiz(client, key)
        if is_football(key):
            return _publish_football(client, key)
        return _publish_yesno(client, key)
    finally:
        if own_client:
            client.close()


def seconds_until_next_action() -> float:
    now = now_local()
    if is_quiet(now):
        return max(30.0, (next_window_start(now) - now).total_seconds())
    earliest = _earliest_next_ts()
    if earliest is None:
        return 0.0
    return max(0.0, earliest - time.time())
