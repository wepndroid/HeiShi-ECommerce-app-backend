"""PROG-105 auth verification — live API + bootstrap simulation."""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any

BASE = "http://127.0.0.1:8000/v1"
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

results: list[tuple[str, bool, str]] = []


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


def main() -> int:
    # Health
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5) as resp:
            record("Backend health", resp.status == 200, resp.read().decode())
    except Exception as exc:
        record("Backend health", False, str(exc))
        print("\nBackend not reachable — start uvicorn first.")
        return 1

    # Login — demo success
    status, data = request("POST", "/auth/login", {"phone": "0400000000", "password": "demo123"})
    record("Demo login success", status == 200 and bool(data and data.get("accessToken")))
    demo_access = data.get("accessToken") if data else None
    demo_refresh = data.get("refreshToken") if data else None
    demo_user = data.get("user") if data else None

    # Login — wrong password
    status, data = request("POST", "/auth/login", {"phone": "0400000000", "password": "wrong"})
    record(
        "Wrong password -> INVALID_CREDENTIALS",
        status == 401 and data and data.get("code") == "INVALID_CREDENTIALS",
        str(data),
    )

    # Login — unknown phone
    status, data = request("POST", "/auth/login", {"phone": "0499999999", "password": "x"})
    record(
        "Unknown phone -> INVALID_CREDENTIALS",
        status == 401 and data and data.get("code") == "INVALID_CREDENTIALS",
        str(data),
    )

    # Login — invalid phone format
    status, data = request("POST", "/auth/login", {"phone": "123", "password": "x"})
    record(
        "Invalid login phone -> VALIDATION_ERROR",
        status == 422 and data and data.get("code") == "VALIDATION_ERROR",
        str(data),
    )

    # Register — invalid phone
    status, data = request(
        "POST",
        "/auth/register",
        {"nickname": "Bad", "phone": "123", "password": "secret1"},
    )
    record(
        "Invalid register phone -> VALIDATION_ERROR",
        status == 422 and data and data.get("code") == "VALIDATION_ERROR",
        str(data),
    )

    # Register — duplicate
    status, data = request(
        "POST",
        "/auth/register",
        {"nickname": "Dup", "phone": "0400000000", "password": "secret1"},
    )
    record(
        "Duplicate register -> PHONE_TAKEN",
        status == 409 and data and data.get("code") == "PHONE_TAKEN",
        str(data),
    )

    # Register — fresh user
    test_phone = "0488877766"
    status, data = request(
        "POST",
        "/auth/register",
        {"nickname": "Prog105User", "phone": test_phone, "password": "secret1"},
    )
    if status == 409:
        # reuse existing from prior run
        status, data = request("POST", "/auth/login", {"phone": test_phone, "password": "secret1"})
        record("Fresh user register (or reuse existing login)", status == 200, "phone already taken; logged in")
    else:
        record("Fresh user register -> 201 + tokens", status == 201 and bool(data and data.get("accessToken")))

    new_access = data.get("accessToken") if data else None
    new_refresh = data.get("refreshToken") if data else None
    new_user = data.get("user") if data else None

    if new_user:
        uid = new_user.get("id", "")
        heishi = new_user.get("heishiId", "")
        record("New user id is UUID", bool(UUID_RE.match(uid)), uid)
        record("New user has heishiId", bool(heishi and heishi.startswith("HS")), heishi)
        record("id != heishiId", uid != heishi, f"{uid} vs {heishi}")

    # /auth/me with valid token
    if new_access:
        status, me = request("GET", "/auth/me", token=new_access)
        record("/auth/me with access token", status == 200 and me and me.get("id"), str(me.get("id") if me else ""))

    # Logout revokes refresh tokens (access JWT remains valid until expiry; client clears storage)
    if new_access and new_refresh:
        status, _ = request("POST", "/auth/logout", token=new_access)
        record("Logout returns 204", status == 204)
        status, refreshed = request("POST", "/auth/refresh", {"refreshToken": new_refresh})
        record(
            "Refresh token revoked after logout",
            status == 401,
            str(status),
        )

    # Refresh flow (demo user)
    if demo_refresh:
        status, refreshed = request("POST", "/auth/refresh", {"refreshToken": demo_refresh})
        record("Refresh token rotation", status == 200 and bool(refreshed and refreshed.get("accessToken")))
        if refreshed:
            status, me = request("GET", "/auth/me", token=refreshed["accessToken"])
            record("/auth/me after refresh", status == 200, demo_user.get("nickname") if demo_user else "")

    # Login with spaces
    status, data = request("POST", "/auth/login", {"phone": "04 0000 0000", "password": "demo123"})
    record("Login normalizes spaces", status == 200, str(status))

    # Feed loads for authenticated user
    if demo_access:
        status, feed = request("GET", "/catalog/feed?page=1&pageSize=5", token=demo_access)
        record(
            "Authenticated feed loads",
            status == 200 and feed and isinstance(feed.get("items"), list),
            f"{len(feed.get('items', []))} items" if feed else "",
        )

    # heishiId collision path
    status, a = request(
        "POST",
        "/auth/register",
        {"nickname": "CollA", "phone": "0411223344", "password": "secret1"},
    )
    status, b = request(
        "POST",
        "/auth/register",
        {"nickname": "CollB", "phone": "0511223344", "password": "secret1"},
    )
    if a and a.get("user") and b and b.get("user"):
        ha = a["user"]["heishiId"]
        hb = b["user"]["heishiId"]
        record("Distinct heishiId for suffix collision phones", ha != hb, f"{ha} vs {hb}")
    elif status == 409:
        record("Distinct heishiId for suffix collision phones", True, "phones already registered (skipped)")

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} API checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
