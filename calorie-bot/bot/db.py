from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS weights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weighed_at TEXT NOT NULL,
    weight_kg REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    name TEXT NOT NULL,
    kcal REAL NOT NULL DEFAULT 0,
    protein REAL NOT NULL DEFAULT 0,
    fat REAL NOT NULL DEFAULT 0,
    carbs REAL NOT NULL DEFAULT 0,
    amount REAL,
    amount_unit TEXT,
    source TEXT NOT NULL DEFAULT 'text',
    raw TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_photos (
    chat_id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS day_settings (
    day TEXT PRIMARY KEY,
    activity_factor REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    name TEXT NOT NULL,
    kcal REAL NOT NULL DEFAULT 0,
    duration_min REAL,
    raw TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS body_measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT UNIQUE,
    weighed_at TEXT NOT NULL,
    weight_kg REAL NOT NULL,
    body_fat REAL,
    bmi REAL,
    visceral_fat REAL,
    muscle_pct REAL,
    body_age INTEGER,
    bone_mass REAL,
    bmr INTEGER,
    water_pct REAL,
    skeletal_muscle REAL,
    subcutaneous_fat REAL,
    source TEXT NOT NULL DEFAULT 'picooc',
    raw TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class MealTotals:
    kcal: float = 0
    protein: float = 0
    fat: float = 0
    carbs: float = 0
    count: int = 0


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        cur = await self.db.execute("PRAGMA table_info(meals)")
        cols = {row["name"] for row in await cur.fetchall()}
        if "meal_slot" not in cols:
            await self.db.execute("ALTER TABLE meals ADD COLUMN meal_slot TEXT")
        if "tg_message_id" not in cols:
            await self.db.execute("ALTER TABLE meals ADD COLUMN tg_message_id INTEGER")
        cur = await self.db.execute("PRAGMA table_info(activities)")
        act_cols = {row["name"] for row in await cur.fetchall()}
        if "tg_message_id" not in act_cols:
            await self.db.execute(
                "ALTER TABLE activities ADD COLUMN tg_message_id INTEGER"
            )
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS meal_preps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                servings_total REAL NOT NULL,
                servings_left REAL NOT NULL,
                kcal_total REAL NOT NULL,
                protein_total REAL NOT NULL,
                fat_total REAL NOT NULL,
                carbs_total REAL NOT NULL,
                ingredients_json TEXT,
                tg_message_id INTEGER,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if not self._db:
            raise RuntimeError("Database is not connected")
        return self._db

    async def add_weight(self, weight_kg: float, weighed_at: date | None = None) -> None:
        day = (weighed_at or date.today()).isoformat()
        now = datetime.now().isoformat(timespec="seconds")
        await self.db.execute(
            "INSERT INTO weights (weighed_at, weight_kg, created_at) VALUES (?, ?, ?)",
            (day, weight_kg, now),
        )
        await self.db.commit()

    async def latest_weight(self) -> float | None:
        cur = await self.db.execute(
            "SELECT weight_kg FROM weights ORDER BY weighed_at DESC, id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return float(row["weight_kg"]) if row else None

    async def add_meal(
        self,
        *,
        day: date,
        name: str,
        kcal: float,
        protein: float,
        fat: float,
        carbs: float,
        amount: float | None = None,
        amount_unit: str | None = None,
        source: str = "text",
        raw: str | None = None,
        meal_slot: str | None = None,
    ) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        cur = await self.db.execute(
            """
            INSERT INTO meals (
                day, name, kcal, protein, fat, carbs,
                amount, amount_unit, source, raw, created_at, meal_slot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                day.isoformat(),
                name,
                kcal,
                protein,
                fat,
                carbs,
                amount,
                amount_unit,
                source,
                raw,
                now,
                meal_slot,
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid)

    async def set_meal_tg_message(self, meal_id: int, tg_message_id: int) -> None:
        await self.db.execute(
            "UPDATE meals SET tg_message_id = ? WHERE id = ?",
            (tg_message_id, meal_id),
        )
        await self.db.commit()

    async def add_meal_prep(
        self,
        *,
        name: str,
        servings: float,
        kcal_total: float,
        protein_total: float,
        fat_total: float,
        carbs_total: float,
        ingredients: list[dict[str, Any]] | None = None,
    ) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        cur = await self.db.execute(
            """
            INSERT INTO meal_preps (
                name, servings_total, servings_left,
                kcal_total, protein_total, fat_total, carbs_total,
                ingredients_json, created_at, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                name,
                servings,
                servings,
                kcal_total,
                protein_total,
                fat_total,
                carbs_total,
                json.dumps(ingredients or [], ensure_ascii=False),
                now,
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid)

    async def set_prep_tg_message(self, prep_id: int, tg_message_id: int) -> None:
        await self.db.execute(
            "UPDATE meal_preps SET tg_message_id = ? WHERE id = ?",
            (tg_message_id, prep_id),
        )
        await self.db.commit()

    async def get_prep_by_id(self, prep_id: int) -> aiosqlite.Row | None:
        cur = await self.db.execute(
            "SELECT * FROM meal_preps WHERE id = ?", (prep_id,)
        )
        return await cur.fetchone()

    async def find_prep_by_tg_message(self, tg_message_id: int) -> aiosqlite.Row | None:
        cur = await self.db.execute(
            "SELECT * FROM meal_preps WHERE tg_message_id = ? AND active = 1",
            (tg_message_id,),
        )
        return await cur.fetchone()

    async def latest_active_prep(self) -> aiosqlite.Row | None:
        cur = await self.db.execute(
            """
            SELECT * FROM meal_preps
            WHERE active = 1 AND servings_left > 0.01
            ORDER BY id DESC LIMIT 1
            """
        )
        return await cur.fetchone()

    async def find_active_prep_by_name(self, hint: str) -> aiosqlite.Row | None:
        cur = await self.db.execute(
            """
            SELECT * FROM meal_preps
            WHERE active = 1 AND servings_left > 0.01
            ORDER BY id DESC LIMIT 20
            """
        )
        rows = await cur.fetchall()
        needle = hint.lower().strip()
        tokens = {t for t in re.findall(r"[а-яёa-z0-9]+", needle) if len(t) > 2}
        best = None
        best_score = 0
        for r in rows:
            name = str(r["name"]).lower()
            if needle in name or name in needle:
                return r
            ntok = {t for t in re.findall(r"[а-яёa-z0-9]+", name) if len(t) > 2}
            score = len(tokens & ntok)
            if score > best_score:
                best_score = score
                best = r
        return best if best_score >= 1 else None

    async def consume_prep_servings(
        self, prep_id: int, servings: float
    ) -> aiosqlite.Row | None:
        row = await self.get_prep_by_id(prep_id)
        if not row or not row["active"]:
            return None
        left = float(row["servings_left"])
        if servings <= 0 or left <= 0:
            return None
        take = min(servings, left)
        new_left = left - take
        active = 0 if new_left < 0.05 else 1
        await self.db.execute(
            """
            UPDATE meal_preps
            SET servings_left = ?, active = ?
            WHERE id = ?
            """,
            (new_left if active else 0.0, active, prep_id),
        )
        await self.db.commit()
        cur = await self.db.execute("SELECT * FROM meal_preps WHERE id = ?", (prep_id,))
        return await cur.fetchone()

    async def cancel_prep(self, prep_id: int) -> str | None:
        cur = await self.db.execute(
            "SELECT id, name FROM meal_preps WHERE id = ?", (prep_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        await self.db.execute(
            """
            UPDATE meal_preps
            SET active = 0, servings_left = 0
            WHERE id = ?
            """,
            (prep_id,),
        )
        await self.db.commit()
        return str(row["name"])

    async def delete_meal_by_id(self, meal_id: int) -> str | None:
        cur = await self.db.execute(
            "SELECT id, name FROM meals WHERE id = ?", (meal_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        await self.db.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
        await self.db.commit()
        return str(row["name"])

    async def find_meal_by_tg_message(self, tg_message_id: int) -> int | None:
        cur = await self.db.execute(
            "SELECT id FROM meals WHERE tg_message_id = ?",
            (tg_message_id,),
        )
        row = await cur.fetchone()
        return int(row["id"]) if row else None

    async def find_meal_by_name(self, day: date, name: str) -> int | None:
        # Точное совпадение, иначе последнее «похожее» за день
        cur = await self.db.execute(
            "SELECT id FROM meals WHERE day = ? AND name = ? ORDER BY id DESC LIMIT 1",
            (day.isoformat(), name),
        )
        row = await cur.fetchone()
        if row:
            return int(row["id"])
        cur = await self.db.execute(
            "SELECT id, name FROM meals WHERE day = ? ORDER BY id DESC",
            (day.isoformat(),),
        )
        rows = await cur.fetchall()
        needle = name.lower().strip()
        for r in rows:
            n = str(r["name"]).lower()
            if needle in n or n in needle:
                return int(r["id"])
        return None

    async def get_meal_by_id(self, meal_id: int) -> aiosqlite.Row | None:
        cur = await self.db.execute("SELECT * FROM meals WHERE id = ?", (meal_id,))
        return await cur.fetchone()

    async def find_meal_by_name_recent(
        self, name: str, *, within_days: int = 21
    ) -> aiosqlite.Row | None:
        """Последнее блюдо с похожим именем за recent days (любой день)."""
        cur = await self.db.execute(
            """
            SELECT * FROM meals
            WHERE day >= date('now', ?)
            ORDER BY id DESC
            LIMIT 200
            """,
            (f"-{int(within_days)} days",),
        )
        rows = await cur.fetchall()
        needle = name.lower().strip()
        if not needle:
            return None
        exact = None
        fuzzy = None
        for r in rows:
            n = str(r["name"]).lower()
            if n == needle:
                return r
            if exact is None and (needle in n or n in needle):
                fuzzy = r
        return fuzzy

    async def day_meals(self, day: date) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM meals WHERE day = ? ORDER BY id",
            (day.isoformat(),),
        )
        return await cur.fetchall()

    async def day_totals(self, day: date) -> MealTotals:
        cur = await self.db.execute(
            """
            SELECT
                COALESCE(SUM(kcal), 0) AS kcal,
                COALESCE(SUM(protein), 0) AS protein,
                COALESCE(SUM(fat), 0) AS fat,
                COALESCE(SUM(carbs), 0) AS carbs,
                COUNT(*) AS count
            FROM meals WHERE day = ?
            """,
            (day.isoformat(),),
        )
        row = await cur.fetchone()
        return MealTotals(
            kcal=float(row["kcal"]),
            protein=float(row["protein"]),
            fat=float(row["fat"]),
            carbs=float(row["carbs"]),
            count=int(row["count"]),
        )

    async def weight_on_or_before(self, day: date) -> float | None:
        cur = await self.db.execute(
            """
            SELECT weight_kg FROM weights
            WHERE weighed_at <= ?
            ORDER BY weighed_at DESC, id DESC LIMIT 1
            """,
            (day.isoformat(),),
        )
        row = await cur.fetchone()
        return float(row["weight_kg"]) if row else None

    async def set_pending(self, chat_id: int, payload: dict[str, Any]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        await self.db.execute(
            """
            INSERT INTO pending_photos (chat_id, payload, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at
            """,
            (chat_id, json.dumps(payload, ensure_ascii=False), now),
        )
        await self.db.commit()

    async def get_pending(self, chat_id: int) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT payload FROM pending_photos WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cur.fetchone()
        return json.loads(row["payload"]) if row else None

    async def clear_pending(self, chat_id: int) -> None:
        await self.db.execute("DELETE FROM pending_photos WHERE chat_id = ?", (chat_id,))
        await self.db.commit()

    async def undo_last_meal(self, day: date) -> str | None:
        cur = await self.db.execute(
            "SELECT id, name FROM meals WHERE day = ? ORDER BY id DESC LIMIT 1",
            (day.isoformat(),),
        )
        row = await cur.fetchone()
        if not row:
            return None
        await self.db.execute("DELETE FROM meals WHERE id = ?", (row["id"],))
        await self.db.commit()
        return str(row["name"])

    async def set_day_factor(self, day: date, factor: float) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        await self.db.execute(
            """
            INSERT INTO day_settings (day, activity_factor, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                activity_factor = excluded.activity_factor,
                updated_at = excluded.updated_at
            """,
            (day.isoformat(), factor, now),
        )
        await self.db.commit()

    async def get_day_factor(self, day: date) -> float | None:
        cur = await self.db.execute(
            "SELECT activity_factor FROM day_settings WHERE day = ?",
            (day.isoformat(),),
        )
        row = await cur.fetchone()
        if not row or row["activity_factor"] is None:
            return None
        return float(row["activity_factor"])

    async def add_activity(
        self,
        *,
        day: date,
        name: str,
        kcal: float,
        duration_min: float | None = None,
        raw: str | None = None,
    ) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        cur = await self.db.execute(
            """
            INSERT INTO activities (day, name, kcal, duration_min, raw, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (day.isoformat(), name, kcal, duration_min, raw, now),
        )
        await self.db.commit()
        return int(cur.lastrowid)

    async def set_activity_tg_message(self, activity_id: int, tg_message_id: int) -> None:
        await self.db.execute(
            "UPDATE activities SET tg_message_id = ? WHERE id = ?",
            (tg_message_id, activity_id),
        )
        await self.db.commit()

    async def find_activity_by_tg_message(self, tg_message_id: int) -> int | None:
        cur = await self.db.execute(
            "SELECT id FROM activities WHERE tg_message_id = ?",
            (tg_message_id,),
        )
        row = await cur.fetchone()
        return int(row["id"]) if row else None

    async def find_activity_by_name(self, day: date, name: str) -> int | None:
        cur = await self.db.execute(
            "SELECT id FROM activities WHERE day = ? AND name = ? ORDER BY id DESC LIMIT 1",
            (day.isoformat(), name),
        )
        row = await cur.fetchone()
        if row:
            return int(row["id"])
        cur = await self.db.execute(
            "SELECT id, name FROM activities WHERE day = ? ORDER BY id DESC",
            (day.isoformat(),),
        )
        needle = name.lower().strip()
        for r in await cur.fetchall():
            n = str(r["name"]).lower()
            if needle in n or n in needle:
                return int(r["id"])
        return None

    async def delete_activity_by_id(self, activity_id: int) -> str | None:
        cur = await self.db.execute(
            "SELECT id, name FROM activities WHERE id = ?", (activity_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        await self.db.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
        await self.db.commit()
        return str(row["name"])

    async def day_activities(self, day: date) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM activities WHERE day = ? ORDER BY id",
            (day.isoformat(),),
        )
        return await cur.fetchall()

    async def day_activity_kcal(self, day: date) -> float:
        cur = await self.db.execute(
            "SELECT COALESCE(SUM(kcal), 0) AS kcal FROM activities WHERE day = ?",
            (day.isoformat(),),
        )
        row = await cur.fetchone()
        return float(row["kcal"])

    async def undo_last_entry(self, day: date) -> tuple[str, str] | None:
        """Удаляет последнюю еду или активность. Возвращает (тип, имя)."""
        day_s = day.isoformat()
        cur = await self.db.execute(
            """
            SELECT 'meal' AS kind, id, name, created_at FROM meals WHERE day = ?
            UNION ALL
            SELECT 'activity' AS kind, id, name, created_at FROM activities WHERE day = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (day_s, day_s),
        )
        row = await cur.fetchone()
        if not row:
            return None
        table = "meals" if row["kind"] == "meal" else "activities"
        await self.db.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
        await self.db.commit()
        return str(row["kind"]), str(row["name"])

    async def add_body_measurement(
        self,
        *,
        external_id: str,
        weighed_at: datetime,
        weight_kg: float,
        body_fat: float | None = None,
        bmi: float | None = None,
        visceral_fat: float | None = None,
        muscle_pct: float | None = None,
        body_age: int | None = None,
        bone_mass: float | None = None,
        bmr: int | None = None,
        water_pct: float | None = None,
        skeletal_muscle: float | None = None,
        subcutaneous_fat: float | None = None,
        source: str = "picooc",
        raw: str | None = None,
    ) -> bool:
        """True если запись новая."""
        now = datetime.now().isoformat(timespec="seconds")
        try:
            await self.db.execute(
                """
                INSERT INTO body_measurements (
                    external_id, weighed_at, weight_kg, body_fat, bmi, visceral_fat,
                    muscle_pct, body_age, bone_mass, bmr, water_pct, skeletal_muscle,
                    subcutaneous_fat, source, raw, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    external_id,
                    weighed_at.isoformat(timespec="seconds"),
                    weight_kg,
                    body_fat,
                    bmi,
                    visceral_fat,
                    muscle_pct,
                    body_age,
                    bone_mass,
                    bmr,
                    water_pct,
                    skeletal_muscle,
                    subcutaneous_fat,
                    source,
                    raw,
                    now,
                ),
            )
            await self.db.commit()
        except aiosqlite.IntegrityError:
            return False

        # также кладём вес в таблицу weights
        await self.add_weight(weight_kg, weighed_at.date())
        return True

    async def latest_body_measurement(self) -> aiosqlite.Row | None:
        cur = await self.db.execute(
            "SELECT * FROM body_measurements ORDER BY weighed_at DESC, id DESC LIMIT 1"
        )
        return await cur.fetchone()

    async def has_body_measurement(self, external_id: str) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM body_measurements WHERE external_id = ? LIMIT 1",
            (external_id,),
        )
        return await cur.fetchone() is not None

    async def get_meta(self, key: str) -> str | None:
        cur = await self.db.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cur.fetchone()
        return str(row["value"]) if row else None

    async def weight_on_day(self, day: date) -> float | None:
        cur = await self.db.execute(
            """
            SELECT weight_kg FROM weights
            WHERE weighed_at = ?
            ORDER BY id DESC LIMIT 1
            """,
            (day.isoformat(),),
        )
        row = await cur.fetchone()
        return float(row["weight_kg"]) if row else None

    async def has_meals_on(self, day: date) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM meals WHERE day = ? LIMIT 1",
            (day.isoformat(),),
        )
        return await cur.fetchone() is not None

    async def set_meta(self, key: str, value: str) -> None:
        await self.db.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self.db.commit()
