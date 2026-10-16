"""Автозащита каналов от пачковой накрутки подписчиков."""

from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any

from predlozka.audience import remove_members, scan_channel_full
from predlozka.client import PredlozkaClient
from predlozka.config import bot_token, load_dotenv
from predlozka.db import (
    bad_users_set,
    count_bad_users,
    db,
    get_meta,
    init_db,
    set_meta,
    upsert_bad_users,
)
from predlozka.protect import collect_protect_targets

ROOT = Path(__file__).resolve().parents[1]
SHARIY_PURGE_JSON = ROOT / "data" / "audit_shariy_purge.json"
SHARIY_MEMBERS_JSON = ROOT / "data" / "shariy_members.json"
SEED_META_KEY = "bad_users:seeded_shariy_v1"


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


def guard_chat_ids() -> list[int]:
    raw = (os.environ.get("PREDLOZKA_GUARD_CHATS") or "").strip()
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


def owner_id() -> int:
    return _env_int("PREDLOZKA_GUARD_OWNER", 0)


def _count_key(chat_id: int) -> str:
    return f"guard:last_count:{chat_id}"


def seed_shariy_bad_users(*, force: bool = False) -> dict[str, Any]:
    """Импорт 402 burst user_id из чистки Шария в bad_users."""
    init_db()
    with db() as conn:
        if not force and get_meta(conn, SEED_META_KEY):
            return {
                "seeded": False,
                "skipped": True,
                "bad_users": count_bad_users(conn),
            }
        if not SHARIY_PURGE_JSON.is_file():
            return {"seeded": False, "error": f"missing {SHARIY_PURGE_JSON}"}

        data = json.loads(SHARIY_PURGE_JSON.read_text(encoding="utf-8"))
        ids = [int(x) for x in (data.get("burst_user_ids") or []) if int(x) > 0]
        chat_id = int(data.get("chat_id") or -79631652207322)

        profiles: dict[int, dict[str, Any]] = {}
        if SHARIY_MEMBERS_JSON.is_file():
            try:
                members = json.loads(SHARIY_MEMBERS_JSON.read_text(encoding="utf-8"))
                if isinstance(members, list):
                    for m in members:
                        if not isinstance(m, dict):
                            continue
                        uid = int(m.get("user_id") or 0)
                        if uid in ids:
                            profiles[uid] = m
            except Exception:
                pass

        n = upsert_bad_users(
            conn,
            ids,
            reason="burst_shariy",
            source="audit_shariy_purge",
            source_chat_id=chat_id,
            profiles=profiles,
        )
        set_meta(conn, SEED_META_KEY, str(int(time.time())))
        total = count_bad_users(conn)
    return {"seeded": True, "imported": n, "bad_users": total, "chat_id": chat_id}


def _profiles_map(members: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for m in members:
        try:
            uid = int(m.get("user_id") or 0)
        except (TypeError, ValueError):
            continue
        if uid > 0:
            out[uid] = m
    return out


def _record_purge(
    user_ids: list[int],
    *,
    reason: str,
    chat_id: int,
    profiles: dict[int, dict[str, Any]],
) -> None:
    if not user_ids:
        return
    with db() as conn:
        upsert_bad_users(
            conn,
            user_ids,
            reason=reason,
            source="guard_purge",
            source_chat_id=chat_id,
            profiles=profiles,
        )


def guard_once(client: PredlozkaClient) -> list[dict]:
    init_db()
    seed_info = seed_shariy_bad_users()
    if seed_info.get("seeded"):
        print(f"[guard] seeded bad_users from shariy: {seed_info}", flush=True)

    chats = guard_chat_ids()
    if not chats:
        print("[guard] no PREDLOZKA_GUARD_CHATS configured", flush=True)
        return []

    threshold = max(3, _env_int("PREDLOZKA_GUARD_BURST_THRESHOLD", 5))
    recent_hours = max(1.0, _env_float("PREDLOZKA_GUARD_RECENT_HOURS", 48.0))
    spike = max(10, _env_int("PREDLOZKA_GUARD_SPIKE", 30))
    alert_thr = max(10, _env_int("PREDLOZKA_GUARD_ALERT_PER_MIN", 30))
    storm_thr = max(alert_thr, _env_int("PREDLOZKA_GUARD_STORM_PER_MIN", 50))
    dead_min = max(1.0, _env_float("PREDLOZKA_GUARD_DEAD_HOURS", 24.0))
    dead_max = max(dead_min, _env_float("PREDLOZKA_GUARD_DEAD_MAX_HOURS", 72.0))
    fp_score = max(2, _env_int("PREDLOZKA_GUARD_FP_MIN_SCORE", 3))
    enable_dead = _env_bool("PREDLOZKA_GUARD_DEAD_PURGE", True)
    enable_fp = _env_bool("PREDLOZKA_GUARD_FP_PURGE", True)
    auto_purge = _env_bool("PREDLOZKA_GUARD_AUTO_PURGE", True)
    owner = owner_id()
    results: list[dict] = []

    with db() as conn:
        known_bad = bad_users_set(conn)

    for chat_id in chats:
        try:
            chat, members, report = scan_channel_full(
                client,
                chat_id,
                burst_threshold=threshold,
                recent_hours=recent_hours,
            )
            title = str(chat.get("title") or chat_id)
            count = int(chat.get("participants_count") or 0)
            profiles = _profiles_map(members)

            with db() as conn:
                prev_raw = get_meta(conn, _count_key(chat_id))
            prev = int(float(prev_raw)) if prev_raw else count
            delta = count - prev

            targets = collect_protect_targets(
                members,
                bad_ids=known_bad,
                burst_user_ids=report.burst_user_ids,
                alert_threshold=alert_thr,
                storm_threshold=storm_thr,
                recent_hours=recent_hours,
                dead_min_hours=dead_min,
                dead_max_hours=dead_max,
                fingerprint_min_score=fp_score,
                enable_dead=enable_dead,
                enable_fingerprint=enable_fp,
            )
            purge_ids = targets.all_ids()
            spike_hit = delta >= spike
            has_storm = bool(targets.storm_windows)
            has_alert = bool(targets.alert_windows)
            action = "ok"
            purge_stats = None

            if purge_ids and auto_purge:
                print(
                    f"[guard] purge chat={chat_id} title={title!r} "
                    f"targets={targets.summary()} delta={delta}",
                    flush=True,
                )
                purge_stats = remove_members(
                    client,
                    chat_id,
                    purge_ids,
                    block=True,
                )
                # пишем в bad_users по причинам
                for reason, ids in targets.by_reason.items():
                    _record_purge(
                        ids,
                        reason=reason,
                        chat_id=chat_id,
                        profiles=profiles,
                    )
                # обновить in-memory set
                known_bad.update(purge_ids)
                action = "purged"
                if owner:
                    try:
                        parts = [
                            f"🛡 Анти-накрутка «{title}»",
                            f"Удалено {purge_stats['ok']} из {purge_stats['total']} "
                            f"(ошибок {purge_stats['fail']}).",
                            f"Было ~{count}, delta={delta:+d}.",
                            "Причины: "
                            + ", ".join(f"{k}={v}" for k, v in targets.summary().items()),
                        ]
                        if targets.storm_windows:
                            parts.append(
                                "Storm: "
                                + "; ".join(f"{w}×{n}" for w, n in targets.storm_windows[:5])
                            )
                        client.send_to_user(
                            owner,
                            text="\n".join(parts),
                            format="html",
                        )
                    except Exception as e:
                        print(f"[guard] dm fail: {e}", flush=True)
            elif has_storm and not auto_purge and owner:
                try:
                    client.send_to_user(
                        owner,
                        text=(
                            f"🌪 Storm «{title}»: "
                            + "; ".join(f"{w}×{n}" for w, n in targets.storm_windows[:5])
                            + " (auto_purge выключен)"
                        ),
                        format="html",
                    )
                except Exception as e:
                    print(f"[guard] storm dm fail: {e}", flush=True)
                action = "storm_alert"
            elif (has_alert or spike_hit) and not purge_ids and owner:
                try:
                    extra = ""
                    if targets.alert_windows:
                        extra = " Окна: " + "; ".join(
                            f"{w}×{n}" for w, n in targets.alert_windows[:5]
                        )
                    client.send_to_user(
                        owner,
                        text=(
                            f"⚠️ «{title}»: резкий рост "
                            f"+{delta} (было {prev} → {count}).{extra} "
                            f"Авто-целей на чистку нет — проверь вручную."
                        ),
                        format="html",
                    )
                except Exception as e:
                    print(f"[guard] spike dm fail: {e}", flush=True)
                action = "spike_alert"
            elif purge_ids and not auto_purge and owner:
                try:
                    client.send_to_user(
                        owner,
                        text=(
                            report.summary_text()
                            + "\n\nЦели: "
                            + ", ".join(f"{k}={v}" for k, v in targets.summary().items())
                        ),
                        format="html",
                    )
                except Exception as e:
                    print(f"[guard] report dm fail: {e}", flush=True)
                action = "report_only"

            try:
                chat2 = client.get_chat(chat_id)
                count2 = int(chat2.get("participants_count") or count)
            except Exception:
                count2 = count
            with db() as conn:
                set_meta(conn, _count_key(chat_id), str(count2))

            row = {
                "chat_id": chat_id,
                "title": title,
                "count": count2,
                "delta": delta,
                "burst": report.burst_count,
                "targets": targets.summary(),
                "storm": targets.storm_windows[:3],
                "alert": targets.alert_windows[:3],
                "action": action,
                "purge": purge_stats,
                "bad_db": len(known_bad),
            }
            print(f"[guard] {row}", flush=True)
            results.append(row)
        except Exception as e:
            print(f"[guard] fail chat={chat_id}: {e}", flush=True)
            traceback.print_exc()
            results.append({"chat_id": chat_id, "action": "error", "error": str(e)})
    return results


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Анти-накрутка: периодический guard")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--seed-shariy",
        action="store_true",
        help="Импортировать burst user_id Шария в bad_users и выйти",
    )
    parser.add_argument(
        "--force-seed",
        action="store_true",
        help="Перезаписать seed Шария даже если уже импортирован",
    )
    args = parser.parse_args()

    if args.seed_shariy:
        print(seed_shariy_bad_users(force=args.force_seed), flush=True)
        return

    interval = max(60, _env_int("PREDLOZKA_GUARD_INTERVAL_SEC", 300))
    with PredlozkaClient(token=bot_token()) as client:
        if args.once:
            print(guard_once(client), flush=True)
            return
        print(
            f"[guard] start chats={guard_chat_ids()} interval={interval}s "
            f"auto_purge={_env_bool('PREDLOZKA_GUARD_AUTO_PURGE', True)} "
            f"storm>={_env_int('PREDLOZKA_GUARD_STORM_PER_MIN', 50)} "
            f"dead={_env_bool('PREDLOZKA_GUARD_DEAD_PURGE', True)} "
            f"fp={_env_bool('PREDLOZKA_GUARD_FP_PURGE', True)}",
            flush=True,
        )
        while True:
            try:
                guard_once(client)
            except Exception:
                traceback.print_exc()
            time.sleep(interval)


if __name__ == "__main__":
    main()
