"""Phone pairing: one-time codes shown as a QR on the laptop, exchanged for a device token.

* A pairing code is random (128 bits), single-use and expires after 10 minutes.
* The phone receives a 256-bit device secret in an HttpOnly, Secure, SameSite=Strict cookie.
  Only its SHA-256 hash is stored (devices.json in the Torino home folder).
* Devices can be revoked from the laptop.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

CODE_TTL = 600
CONNECTED_WINDOW = 120


def _h(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class Pairing:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._codes: dict[str, float] = {}  # code hash -> expiry
        self._seen: dict[str, float] = {}   # device_id -> last seen (memory; persisted lazily)

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"devices": {}}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def new_code(self) -> tuple[str, float]:
        code = secrets.token_urlsafe(16)
        exp = time.time() + CODE_TTL
        with self._lock:
            now = time.time()
            self._codes = {k: v for k, v in self._codes.items() if v > now}
            self._codes[_h(code)] = exp
        return code, exp

    def redeem(self, code: str, name: str) -> tuple[str, str] | None:
        """Exchange a valid code for (device_id, cookie_value). Codes work once."""
        if not code:
            return None
        with self._lock:
            exp = self._codes.pop(_h(code), None)
            if not exp or exp < time.time():
                return None
            device_id = "D" + secrets.token_hex(4).upper()
            secret = secrets.token_urlsafe(32)
            data = self._load()
            data["devices"][device_id] = {"name": name[:80] or "Phone", "secret_sha256": _h(secret),
                                          "paired_at": time.strftime("%Y-%m-%d %H:%M"), "last_seen": time.time()}
            self._save(data)
            self._seen[device_id] = time.time()
        return device_id, f"{device_id}.{secret}"

    def check(self, cookie: str | None) -> str | None:
        if not cookie or "." not in cookie:
            return None
        device_id, secret = cookie.split(".", 1)
        with self._lock:
            dev = self._load()["devices"].get(device_id)
            if not dev or not hmac.compare_digest(dev["secret_sha256"], _h(secret)):
                return None
            now = time.time()
            if now - self._seen.get(device_id, 0) > 60:  # persist last_seen at most once a minute
                data = self._load()
                if device_id in data["devices"]:
                    data["devices"][device_id]["last_seen"] = now
                    self._save(data)
            self._seen[device_id] = now
        return device_id

    def devices(self) -> list[dict[str, Any]]:
        with self._lock:
            data = self._load()
        now = time.time()
        out = []
        for did, d in data["devices"].items():
            seen = max(d.get("last_seen", 0), self._seen.get(did, 0))
            out.append({"device_id": did, "name": d["name"], "paired_at": d["paired_at"],
                        "last_seen_s": int(now - seen) if seen else None,
                        "connected": bool(seen) and now - seen < CONNECTED_WINDOW})
        return out

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            data = self._load()
            if data["devices"].pop(device_id, None) is None:
                return False
            self._save(data)
            self._seen.pop(device_id, None)
        return True

    def name_of(self, device_id: str) -> str:
        return self._load()["devices"].get(device_id, {}).get("name", "phone")
