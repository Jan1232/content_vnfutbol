"""CLI: проверка / чистка аудитории канала от имени бота предложки.

Примеры:
  python -m predlozka.audit --chat-id -79631652207322
  python -m predlozka.audit --chat-id -79631652207322 --purge --yes
  python -m predlozka.audit --chat-id -79631652207322 --dm-owner 44784432
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from predlozka.audience import remove_members, scan_channel
from predlozka.client import PredlozkaClient
from predlozka.config import bot_token, load_dotenv


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="Аудит аудитории MAX-канала (накрутка)")
    p.add_argument("--chat-id", type=int, required=True)
    p.add_argument("--dm-owner", type=int, default=0, help="user_id владельца для отчёта в ЛС")
    p.add_argument("--purge", action="store_true", help="удалить burst-подписчиков")
    p.add_argument("--block", action="store_true", default=True, help="блокировать при удалении")
    p.add_argument("--no-block", action="store_true", help="удалить без блока")
    p.add_argument("--limit", type=int, default=0, help="лимит удалений (0 = все burst)")
    p.add_argument("--yes", action="store_true", help="без подтверждения на purge")
    p.add_argument("--save", type=str, default="", help="сохранить JSON-отчёт")
    args = p.parse_args()

    with PredlozkaClient(token=bot_token()) as client:
        report = scan_channel(client, args.chat_id)
        text = report.summary_text()
        print(text.replace("<b>", "").replace("</b>", ""))

        save_path = Path(args.save) if args.save else Path("data") / f"audit_{args.chat_id}.json"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(
            json.dumps(
                {
                    "chat_id": report.chat_id,
                    "title": report.title,
                    "fetched": report.fetched,
                    "participants_count": report.participants_count,
                    "burst_count": report.burst_count,
                    "organic_count": report.organic_count,
                    "burst_windows": report.burst_windows,
                    "burst_user_ids": report.burst_user_ids,
                    "organic": [
                        {
                            "user_id": m.get("user_id"),
                            "first_name": m.get("first_name"),
                            "join_time": m.get("join_time"),
                            "last_activity_time": m.get("last_activity_time"),
                        }
                        for m in report.organic
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n[saved] {save_path}")

        if args.dm_owner:
            ok = False
            try:
                client.send_to_user(int(args.dm_owner), text=text, format="html")
                ok = True
            except Exception as e:
                print(f"[dm] fail: {e}")
            print(f"[dm] owner={args.dm_owner} ok={ok}")

        if args.purge:
            if not report.burst_user_ids:
                print("[purge] некого удалять")
                return
            if not args.yes:
                print(
                    f"[purge] отказ: нужно --yes чтобы удалить {len(report.burst_user_ids)} user_id"
                )
                sys.exit(2)
            block = not args.no_block
            limit = int(args.limit) or None
            print(
                f"[purge] start chat={args.chat_id} n={len(report.burst_user_ids)} "
                f"limit={limit or 'all'} block={block}"
            )
            stats = remove_members(
                client,
                args.chat_id,
                report.burst_user_ids,
                block=block,
                limit=limit,
            )
            print(f"[purge] done {stats}")
            if args.dm_owner:
                try:
                    client.send_to_user(
                        int(args.dm_owner),
                        text=(
                            f"🧹 Чистка «{_plain(report.title)}»: "
                            f"удалено {stats['ok']}, ошибок {stats['fail']} "
                            f"из {stats['total']}."
                        ),
                        format="html",
                    )
                except Exception as e:
                    print(f"[dm] purge notify fail: {e}")


def _plain(s: str) -> str:
    return str(s).replace("<", "").replace(">", "")


if __name__ == "__main__":
    main()
