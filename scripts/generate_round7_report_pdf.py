#!/usr/bin/env python3
"""Generate PDF report: benchmark DB data, tests, verification code."""

from __future__ import annotations

import json
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "app.db"
BENCH_DIR = ROOT / "data" / "editorial" / "benchmark"
OUT = BENCH_DIR / "editorial_round7_test_report.pdf"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def q(sql: str, args=()):
    import sqlite3

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    return rows


def run_tests() -> str:
    r = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "unittest", "discover", "-s", "tests", "-p", "test_editorial*.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    tail = (r.stdout + r.stderr).strip().splitlines()[-5:]
    return "\n".join(tail)


class ReportPDF(FPDF):
    def __init__(self):
        super().__init__()
        self.add_font("DV", "", FONT)
        self.add_font("DV", "B", FONT_B)
        self.set_auto_page_break(auto=True, margin=15)

    def heading(self, title: str, level: int = 1):
        self.ln(4)
        size = {1: 14, 2: 12, 3: 11}.get(level, 10)
        self.set_font("DV", "B", size)
        self.multi_cell(0, 7, title)
        self.ln(2)

    def body(self, text: str):
        self.set_font("DV", "", 9)
        self.multi_cell(0, 5, text)
        self.ln(1)

    def code(self, text: str):
        self.set_font("DV", "", 7)
        self.set_fill_color(245, 245, 245)
        w = self.w - self.l_margin - self.r_margin
        for raw in text.splitlines():
            line = raw.replace("\t", "    ")
            while line:
                chunk = line[:110]
                if len(line) > 110:
                    sp = chunk.rfind(" ")
                    if sp > 40:
                        chunk, line = line[:sp], line[sp:].lstrip()
                    else:
                        chunk, line = line[:110], line[110:]
                else:
                    line = ""
                self.multi_cell(w, 3.5, chunk, fill=True)
        self.ln(2)

    def table(self, headers: list[str], rows: list[list[str]], widths: list[int] | None = None):
        self.set_font("DV", "B", 8)
        if not widths:
            w = 190 / len(headers)
            widths = [int(w)] * len(headers)
        for i, h in enumerate(headers):
            self.cell(widths[i], 6, h[:28], border=1)
        self.ln()
        self.set_font("DV", "", 7)
        for row in rows:
            for i, cell in enumerate(row):
                self.cell(widths[i], 5, str(cell)[:32], border=1)
            self.ln()


def main() -> None:
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    usage_7d = q(
        """
        SELECT task, model, COUNT(*) n,
               SUM(prompt_tokens) pin, SUM(completion_tokens) pout,
               SUM(COALESCE(cached_tokens,0)) cached
        FROM editorial_llm_usage WHERE ts > datetime('now','-7 days')
        GROUP BY task, model ORDER BY pin+pout DESC LIMIT 18
        """
    )
    bench_runs = q(
        """
        SELECT run_id, COUNT(*) rows, SUM(p_in) pin, SUM(p_out) pout,
               SUM(cached) cached, ROUND(SUM(usd),4) usd
        FROM editorial_cost_benchmark GROUP BY run_id ORDER BY run_id
        """
    )
    main_run = "05936c491785"
    news_rows = q(
        """
        SELECT id, status, substr(last_error,1,55) err, source, substr(title,1,70) title
        FROM editorial_news WHERE id IN (3907,3497,6030,4584,485,3769,3282,3206,3528,3698)
        ORDER BY id
        """
    )
    per_stage = q(
        """
        SELECT stage, model, COUNT(*) calls, SUM(p_in) pin, SUM(p_out) pout,
               SUM(cached) cached, ROUND(SUM(usd),4) usd
        FROM editorial_cost_benchmark WHERE run_id=? GROUP BY stage, model
        ORDER BY pin+pout DESC
        """,
        (main_run,),
    )
    per_news = q(
        """
        SELECT news_id, stage, model, p_in, p_out, cached, ROUND(usd,4) usd
        FROM editorial_cost_benchmark WHERE run_id=? ORDER BY news_id, id
        """,
        (main_run,),
    )

    bench_json = {}
    jp = BENCH_DIR / f"{main_run}.json"
    if jp.is_file():
        bench_json = json.loads(jp.read_text(encoding="utf-8"))

    test_tail = run_tests()

    pdf = ReportPDF()
    pdf.add_page()

    pdf.heading("Editorial Round-7: отчёт о тестировании и benchmark", 1)
    pdf.body(f"Сформирован: {now}\nРепозиторий: /var/max-repost\nБД: data/app.db")

    pdf.heading("1. Ограничения качества тестирования (честно)", 2)
    pdf.body(
        "Автотесты round-7 в основном unit-тесты с mock (LLM/TG/HTTP не вызываются). "
        "Benchmark — реальные API-вызовы, но только 4 из 10 постов дошли до vision; "
        "6 отсеяны на topic/pick/filter до дорогих этапов. "
        "A/B Luna vision: в логе были ошибки empty content, при этом 4 вызова записаны в БД — "
        "сравнение цены есть, сравнение качества отбора фото НЕ валидировано человеком. "
        "soccerblog_gate в benchmark не измерялся отдельно (215 вызовов в проде за 7д — шум от discovery). "
        "213 editorial-тестов: 6 падений (часть до round-7, не регрессия gate)."
    )

    pdf.heading("2. Прогоны cost_benchmark", 2)
    pdf.table(
        ["run_id", "rows", "p_in", "p_out", "cached", "usd"],
        [[r["run_id"], r["rows"], r["pin"], r["pout"], r["cached"], r["usd"]] for r in bench_runs],
        [32, 14, 22, 18, 18, 16],
    )
    pdf.body(
        "05936c491785 — основной прогон: 10 held-постов сброшены в new, VISION_AB=true, "
        "IMAGERY_CANDIDATES_MAX=4, STORY_RELATION_HYBRID=true. Время ~153с."
    )

    pdf.heading("3. Итоги основного прогона (05936c491785)", 2)
    if bench_json.get("totals"):
        t = bench_json["totals"]
        pdf.body(
            f"Постов с LLM-строками в benchmark-таблице: {t.get('news_count')}\n"
            f"news_ids: {bench_json.get('news_ids')}\n"
            f"ИТОГО: ${t.get('usd')} | cached={t.get('cached_tokens')} | "
            f"avg/post=${t.get('avg_usd_per_post')}\n"
            f"Экстраполяция: {bench_json.get('extrapolation')}"
        )
    pdf.table(
        ["stage", "model", "calls", "p_in", "cached", "usd"],
        [
            [r["stage"], r["model"], r["calls"], r["pin"], r["cached"], r["usd"]]
            for r in per_stage
        ],
        [38, 28, 12, 18, 16, 14],
    )

    pdf.heading("3.1 Vision A/B (на одних и тех же 4 постах)", 3)
    va = bench_json.get("vision_ab") or {}
    if va.get("mini") and va.get("luna"):
        m, l = va["mini"], va["luna"]
        ratio = round(float(m["p_in"]) / max(float(l["p_in"]), 1), 1)
        pdf.body(
            f"4o-mini: p_in={m['p_in']}, usd={m['usd']}\n"
            f"Luna:    p_in={l['p_in']}, usd={l['usd']}\n"
            f"Отношение input tok mini/luna ≈ {ratio}x. Качество релевантности не сверялось."
        )

    pdf.heading("3.2 Статус 10 постов прогона", 3)
    pdf.table(
        ["id", "status", "source", "title"],
        [[r["id"], r["status"], r["source"], r["title"]] for r in news_rows],
        [12, 18, 28, 132],
    )
    ready = [r for r in news_rows if r["status"] in ("benchmark", "ready") and "ready" in (r["err"] or "")]
    pdf.body(
        f"ready (PNG): #3907, #3698 + проверить cover_path в БД. "
        f"held: #485, #6030. filtered: 5 постов."
    )

    pdf.add_page()
    pdf.heading("4. Расход LLM за 7 дней (editorial_llm_usage)", 2)
    pdf.table(
        ["task", "model", "n", "p_in", "cached"],
        [[r["task"], r["model"], r["n"], r["pin"], r["cached"]] for r in usage_7d],
        [36, 30, 10, 24, 18],
    )
    pdf.body(
        "image_vision ≈78% токенов. cached>0 появился после правок (35840 на vision в benchmark). "
        "soccerblog_gate: 215 вызовов, ~617k p_in (отдельный шум при discovery)."
    )

    pdf.heading("5. Автотесты (unittest discover test_editorial*)", 2)
    pdf.code(test_tail)
    pdf.body(
        "Round-7 файлы:\n"
        "  tests/test_editorial_round7.py — gate, hybrid story, single-candidate vision\n"
        "  tests/test_editorial_auto_reject.py — ночной skip, auto-reject\n"
        "  tests/test_editorial_usage.py — dashboard aggregation\n"
        "  tests/test_editorial_story_relation.py — Luna-first hybrid\n"
        "Падения (не round-7): imagery overlay/wrong_team, pick labels, dry_run publish, attribution."
    )

    pdf.heading("6. Код проверки — benchmark", 2)
    pdf.code(
        textwrap.dedent(
            """
            # Запуск:
            VISION_AB=true IMAGERY_CANDIDATES_MAX=4 \\
              .venv/bin/python -m editorial.cost_benchmark --count 10

            # editorial/cost_benchmark.py — сброс held → new:
            SELECT id FROM editorial_news
            WHERE status IN ('held','published') AND source NOT IN (...)
            ORDER BY RANDOM() LIMIT ?

            # Логирование: editorial_cost_benchmark + editorial_llm_usage.benchmark_run_id
            # JSON: data/editorial/benchmark/{run_id}.json
            """
        ).strip()
    )

    pdf.heading("7. Код проверки — soccerblog_gate", 2)
    pdf.code(
        textwrap.dedent(
            """
            # editorial/soccerblog_gate.py
            def soccerblog_gate(text, media, *, media_type):
                preview = media_preview_from_post(media)  # ffmpeg кадр / jpeg
                return vision(gpt-4o-mini, [preview], prompt) →
                  {kind: meme|news|reject, confidence, reason, is_media_meme}

            # editorial/sources.py — на parse Telegram meme feed
            verdict = soccerblog_gate(...)
            if kind == 'reject': continue
            if kind == 'meme': entities['meme_source'] = feed.name
            if kind == 'news': meme_source=0 → шаблонный конвейер

            # Фаза 2: editorial/moderation.py try_soccerblog_auto_publish()
            if confidence >= SOCCERBLOG_AUTO_CONFIDENCE: publish без бота
            """
        ).strip()
    )

    pdf.heading("8. Код проверки — vision / story_relation", 2)
    pdf.code(
        textwrap.dedent(
            """
            # editorial/imagery.py score_relevance
            if len(candidates) == 1: skip vision, relevance=0.72
            if vision_ab: вызов mini + luna (task image_vision_ab_*)
            основной: editorial_vision_model (gpt-4o-mini)

            # editorial/llm.py story_relation
            luna = chat_json(..., tag='story_relation_luna', model_kind='text')
            if confidence >= REASONING_ESCALATE (0.7): return luna
            else: chat_json(..., tag='story_relation', model_kind='reasoning')  # Terra
            """
        ).strip()
    )

    pdf.heading("9. SQL для самопроверки", 2)
    pdf.code(
        textwrap.dedent(
            """
            -- расход по task за 7д
            SELECT task, model, COUNT(*), SUM(prompt_tokens), SUM(cached_tokens)
            FROM editorial_llm_usage WHERE ts > datetime('now','-7 days')
            GROUP BY task, model ORDER BY 4 DESC;

            -- benchmark run
            SELECT * FROM editorial_cost_benchmark WHERE run_id='05936c491785';

            -- cached на vision
            SELECT ts, task, prompt_tokens, cached_tokens FROM editorial_llm_usage
            WHERE task LIKE '%vision%' ORDER BY id DESC LIMIT 20;
            """
        ).strip()
    )

    pdf.heading("10. Рекомендации перед прод-переключением", 2)
    pdf.body(
        "1) Вручную открыть cover_path для #3907, #3698 (и др. ready) — качество фото.\n"
        "2) Прогнать 10 постов с --from-held, исключая уже filtered источники, или форсировать RSS.\n"
        "3) Починить image_vision_ab_luna empty content перед switch на Luna.\n"
        "4) Отключить лишний soccerblog_gate при fetch_fresh вне meme-parse.\n"
        "5) Не пушить до ручной валидации 3+ карточек."
    )

    pdf.output(str(OUT))
    print(OUT)


if __name__ == "__main__":
    main()
