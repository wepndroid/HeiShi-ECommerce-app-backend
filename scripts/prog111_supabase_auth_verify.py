"""PROG-111 Supabase Auth verification — JWT sync-profile + legacy regression."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jose import jwt

BASE = "http://127.0.0.1:8000/v1"
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

results: list[tuple[str, bool, str]] = []


def load_jwt_secret() -> str:
    secret = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
    if secret:
        return secret
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("SUPABASE_JWT_SECRET="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def fresh_phone(prefix: str = "04") -> str:
    return f"{prefix}{int(time.time() * 1000) % 100000000:08d}"


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"{mark}  {name}{suffix}")


def request(
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict[str, Any] | None]:
    url = f"{BASE}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode()
            payload = json.loads(text) if text else None
            return resp.status, payload
    except urllib.error.HTTPError as err:
        text = err.read().decode()
        try:
            payload = json.loads(text) if text else None
        except json.JSONDecodeError:
            payload = {"message": text}
        return err.code, payload


def make_supabase_jwt(sub: str, phone: str, secret: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "aud": "authenticated",
        "role": "authenticated",
        "phone": phone,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def main() -> int:
    secret = load_jwt_secret()
    if not secret:
        record(
            "SUPABASE_JWT_SECRET configured",
            False,
            "Set SUPABASE_JWT_SECRET in Backend/.env to run Supabase Auth tests",
        )
        print("\nSkipping Supabase JWT tests — configure Backend/.env first.")
        return 1

    record("SUPABASE_JWT_SECRET configured", True)

    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5) as resp:
            record("Backend health", resp.status == 200, resp.read().decode())
    except Exception as exc:
        record("Backend health", False, str(exc))
        print("\nBackend not reachable — start uvicorn first.")
        return 1

    phone = fresh_phone()
    e164 = f"+61{phone[1:]}"
    user_id = str(uuid.uuid4())
    token = make_supabase_jwt(user_id, e164, secret)

    status, data = request(
        "POST",
        "/auth/sync-profile",
        {"nickname": "Supabase Test", "phone": phone},
        token=token,
    )
    record(
        "Supabase sync-profile creates user",
        status == 200 and bool(data and data.get("id") == user_id),
        f"status={status}",
    )

    status, me = request("GET", "/auth/me", token=token)
    record(
        "Supabase JWT -> /auth/me",
        status == 200 and bool(me and me.get("phone") == phone),
        f"status={status}",
    )

    status, _ = request("GET", "/auth/me", token="not-a-valid-token")
    record("Invalid Supabase JWT rejected", status == 401, f"status={status}")

    status, dup = request(
        "POST",
        "/auth/sync-profile",
        {"nickname": "Other User", "phone": phone},
        token=make_supabase_jwt(str(uuid.uuid4()), e164, secret),
    )
    record(
        "Duplicate phone on sync-profile -> PHONE_TAKEN",
        status == 409 and isinstance(dup, dict) and dup.get("code") == "PHONE_TAKEN",
        f"status={status}",
    )

    status, legacy = request("POST", "/auth/login", {"phone": "0400000000", "password": "demo123"})
    record(
        "Legacy demo login still works",
        status == 200 and bool(legacy and legacy.get("accessToken")),
        f"status={status}",
    )
    legacy_token = legacy.get("accessToken") if legacy else None
    if legacy_token:
        status, legacy_me = request("GET", "/auth/me", token=legacy_token)
        record(
            "Legacy JWT -> /auth/me",
            status == 200 and bool(legacy_me and legacy_me.get("phone")),
            f"status={status}",
        )

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())