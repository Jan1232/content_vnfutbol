"""GoodPaper: documents_only только у wlpr8k, не у всего канала."""

from __future__ import annotations

from app.rules import reload_rules, resolve_rules

GP = dict(
    chat_id=-77250724273564,
    title="GoodPaper | Обои на телефон",
    link="https://max.ru/channel_goodpaper",
)


def test_wlpr_documents_only() -> None:
    reload_rules()
    wlpr = resolve_rules(**GP, source_url="https://t.me/s/wlpr8k")
    assert wlpr.documents_only is True
    assert wlpr.fixed_caption.startswith("Ставь")

    oboiz = resolve_rules(**GP, source_url="https://t.me/s/oboiz")
    assert oboiz.documents_only is False
    assert oboiz.require_media is True
    assert oboiz.fixed_caption.startswith("Ставь")


if __name__ == "__main__":
    test_wlpr_documents_only()
    print("OK goodpaper documents_only scoped to wlpr8k")
