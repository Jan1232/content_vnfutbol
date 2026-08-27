from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger("calorie-bot.picooc")

API = "https://api2.picooc-int.com/v1/api/"
APP_VER = "i4.1.11.0"


def _upper_md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest().upper()


@dataclass
class PicoocMeasurement:
    weighed_at: datetime
    weight_kg: float
    body_fat: float | None = None
    bmi: float | None = None
    visceral_fat: float | None = None
    muscle_pct: float | None = None
    body_age: int | None = None
    bone_mass: float | None = None
    bmr: int | None = None
    water_pct: float | None = None
    skeletal_muscle: float | None = None
    subcutaneous_fat: float | None = None
    mac: str | None = None
    server_id: int | None = None
    role_name: str = ""

    @property
    def external_id(self) -> str:
        if self.server_id:
            return f"picooc:{self.server_id}"
        ts = int(self.weighed_at.timestamp())
        return f"picooc:{ts}:{self.weight_kg}"


class PicoocClient:
    """Неофициальный клиент облака Picooc (как SmartScaleConnect)."""

    def __init__(
        self,
        email: str,
        password: str,
        *,
        proxy: str | None = None,
        device_id: str | None = None,
        role_name: str = "",
    ) -> None:
        self.email = email
        self.password = password
        self.proxy = proxy or None
        self.device_id = (device_id or uuid.uuid4().hex).upper()
        self.role_name = role_name
        self.user_id: str | None = None
        self.role_ids: dict[str, str] = {}

    def _values(self, method: str) -> dict[str, str]:
        timestamp = str(int(datetime.now().timestamp()))
        sign = _upper_md5(
            self.device_id
            + _upper_md5(timestamp + _upper_md5(method) + _upper_md5(APP_VER))
        )
        return {
            "appver": APP_VER,
            "timestamp": timestamp,
            "lang": "en",
            "method": method,
            "timezone": "",
            "sign": sign,
            "push_token": f"android::{self.device_id}",
            "device_id": self.device_id,
        }

    async def login(self) -> None:
        form = self._values("user_login_new")
        req_payload = {
            "appver": form["appver"],
            "timestamp": form["timestamp"],
            "lang": form["lang"],
            "method": form["method"],
            "timezone": form["timezone"],
            "sign": form["sign"],
            "push_token": form["push_token"],
            "device_id": form["device_id"],
            "req": {
                "app_channel": "",
                "app_version": form["appver"],
                "email": self.email,
                "password": self.password,
                "phone": "",
                "phone_system": "",
                "phone_type": "",
            },
        }
        import json

        data = dict(form)
        data["reqData"] = json.dumps(req_payload, separators=(",", ":"))

        async with httpx.AsyncClient(proxy=self.proxy, timeout=60) as client:
            r = await client.post(
                API + "account/login",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            r.raise_for_status()
            body = r.json()

        if body.get("code") != 0:
            raise RuntimeError(f"Picooc login failed: {body.get('msg')}")

        resp = body.get("resp") or {}
        self.user_id = str(resp.get("user_id") or "")
        role_id = str(resp.get("role_id") or "")
        self.role_ids = {"": role_id}
        for role in resp.get("roles") or []:
            self.role_ids[str(role.get("role_name") or "")] = str(role.get("role_id") or "")
        log.info("Picooc login ok user_id=%s roles=%s", self.user_id, list(self.role_ids))

    async def fetch_measurements(self, *, page_size: int = 100) -> list[PicoocMeasurement]:
        if not self.user_id:
            await self.login()

        role_id = self.role_ids.get(self.role_name)
        if not role_id:
            raise RuntimeError(f"Picooc unknown role: {self.role_name!r}")

        out: list[PicoocMeasurement] = []
        params = self._values("bodyIndexList")
        params.update(
            {
                "pageSize": str(page_size),
                "time": params["timestamp"],
                "userId": self.user_id or "",
                "roleId": role_id,
            }
        )

        async with httpx.AsyncClient(proxy=self.proxy, timeout=60) as client:
            while True:
                r = await client.get(API + "bodyIndex/bodyIndexList", params=params)
                r.raise_for_status()
                body = r.json()
                resp = body.get("resp") or {}
                for rec in resp.get("records") or []:
                    m = self._parse_record(rec)
                    if m:
                        out.append(m)
                if not resp.get("continue"):
                    break
                params["time"] = str(resp.get("lastTime") or params["time"])
                # refresh sign for next page
                fresh = self._values("bodyIndexList")
                params.update(
                    {
                        "timestamp": fresh["timestamp"],
                        "sign": fresh["sign"],
                        "userId": self.user_id or "",
                        "roleId": role_id,
                        "pageSize": str(page_size),
                    }
                )

        out.sort(key=lambda x: x.weighed_at)
        return out

    def _parse_record(self, rec: dict[str, Any]) -> PicoocMeasurement | None:
        if int(rec.get("abnormal_flag") or 0) != 0:
            return None
        if int(rec.get("is_del") or 0) != 0:
            return None
        weight = float(rec.get("weight") or 0)
        if weight <= 0:
            return None
        body_time = int(rec.get("bodyTime") or 0)
        if not body_time:
            return None

        def f(key: str) -> float | None:
            v = rec.get(key)
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        def i(key: str) -> int | None:
            v = rec.get(key)
            if v is None:
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        return PicoocMeasurement(
            weighed_at=datetime.fromtimestamp(body_time),
            weight_kg=weight,
            body_fat=f("body_fat"),
            bmi=f("bmi"),
            visceral_fat=f("visceral_fat_level"),
            muscle_pct=f("muscle_race"),
            body_age=i("body_age"),
            bone_mass=f("bone_mass"),
            bmr=i("basic_metabolism"),
            water_pct=f("water_race"),
            skeletal_muscle=f("skeletal_muscle"),
            subcutaneous_fat=f("subcutaneous_fat"),
            mac=rec.get("mac"),
            server_id=i("server_id") or i("body_index_id"),
            role_name=self.role_name,
        )
