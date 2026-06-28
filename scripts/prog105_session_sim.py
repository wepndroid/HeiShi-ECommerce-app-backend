"""PROG-105 client session simulation (bootstrap + mock mode)."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/v1"
results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"{mark}  {name}{suffix}")


def post(path: str, body: dict, token: str | None = None) -> tuple[int, dict | None]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode()
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as err:
        text = err.read().decode()
        return err.code, json.loads(text) if text else None


def get(path: str, token: str) -> tuple[int, dict | None]:
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode()
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as err:
        text = err.read().decode()
        return err.code, json.loads(text) if text else None


def bootstrap_api_mode(storage: dict[str, str]) -> dict | None:
    """Mirror Frontend bootstrapAuth when API_USE_MOCK_FALLBACK=false."""
    access = storage.get("authAccessToken")
    if access:
        status, me = get("/auth/me", access)
        if status == 200 and me:
            storage["authSession"] = json.dumps(
                {
                    "id": me["id"],
                    "heishiId": me["heishiId"],
                    "nickname": me["nickname"],
                    "phone": me["phone"],
                }
            )
            return me
        if status == 401:
            refresh = storage.get("authRefreshToken")
            if refresh:
                code, tokens = post("/auth/refresh", {"refreshToken": refresh})
                if code == 200 and tokens:
                    storage["authAccessToken"] = tokens["accessToken"]
                    storage["authRefreshToken"] = tokens["refreshToken"]
                    user = tokens["user"]
                    storage["authSession"] = json.dumps(
                        {
                            "id": user["id"],
                            "heishiId": user["heishiId"],
                            "nickname": user["nickname"],
                            "phone": user["phone"],
                        }
                    )
                    return user
            storage.pop("authAccessToken", None)
            storage.pop("authRefreshToken", None)
            storage.pop("authSession", None)
        return None
    return None


def main() -> int:
    storage: dict[str, str] = {}

    # Stale local session + bad token
    storage["authSession"] = json.dumps(
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "heishiId": "HS12345678",
            "nickname": "StaleDemo",
            "phone": "0400000000",
        }
    )
    storage["authAccessToken"] = "invalid-token"
    record(
        "Bootstrap ignores stale session when token invalid",
        bootstrap_api_mode(storage) is None,
    )

    # Login + relaunch
    storage.clear()
    code, tokens = post("/auth/login", {"phone": "0400000000", "password": "demo123"})
    record("Live login stores tokens", code == 200 and bool(tokens))
    if tokens:
        storage["authAccessToken"] = tokens["accessToken"]
        storage["authRefreshToken"] = tokens["refreshToken"]
        user = bootstrap_api_mode(storage)
        record("Bootstrap restores session after relaunch", user and user["nickname"] == "Holden")
        record(
            "Bootstrap user id present and distinct from heishiId",
            bool(user and user.get("id") and user["id"] != user.get("heishiId")),
            f"{user['id']} vs {user.get('heishiId')}" if user else "",
        )
        record(
            "Bootstrap heishiId present",
            bool(user and user["heishiId"].startswith("HS")),
            user["heishiId"] if user else "",
        )

        # Logout + relaunch
        post("/auth/logout", {}, token=storage["authAccessToken"])
        storage.pop("authAccessToken", None)
        storage.pop("authRefreshToken", None)
        storage.pop("authSession", None)
        record("Bootstrap null after logout relaunch", bootstrap_api_mode(storage) is None)
        record("Tokens cleared from storage", "authAccessToken" not in storage)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} session simulation checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
