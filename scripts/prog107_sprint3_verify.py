"""PROG-107 Sprint 3 — seller profile, follow, credit (A29-A31, A45-A47, A48-A49)."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE = os.environ.get("HEYMARKET_API_BASE", "http://127.0.0.1:8000/v1")
HEALTH_URL = BASE.replace("/v1", "") + "/health"
results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def request(method: str, path: str, *, query: dict | None = None, body: dict | None = None, token: str | None = None):
    url = f"{BASE}{path}"
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode()
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as err:
        text = err.read().decode()
        try:
            return err.code, json.loads(text) if text else None
        except json.JSONDecodeError:
            return err.code, {"message": text}


def login() -> tuple[str | None, dict | None]:
    status, data = request("POST", "/auth/login", body={"phone": "0400000000", "password": "demo123"})
    if status == 200 and isinstance(data, dict):
        return data.get("accessToken"), data.get("user")
    return None, None


def assert_credit(d: dict) -> bool:
    return all(k in d for k in ("score", "trades", "completionRate", "violations", "rating"))


def assert_review_summary(d: dict) -> bool:
    return "score" in d and "pendingCount" in d and "receivedCount" in d


def assert_verification(d: dict) -> bool:
    return all(k in d for k in ("phoneVerified", "wechatBound", "alipayBound", "identityVerified", "businessVerified"))


def assert_public_profile(d: dict) -> bool:
    keys = (
        "id", "nickname", "memberSince", "rating", "reviewCount", "listingCount", "followerCount",
        "phoneVerified", "identityVerified", "businessVerified", "wechatLinked", "alipayLinked",
    )
    return all(k in d for k in keys)


def assert_follow_item(d: dict) -> bool:
    return all(k in d for k in ("userId", "nickname", "followedAt"))


def main() -> int:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            record("Backend health", resp.status == 200)
    except Exception as exc:
        record("Backend health", False, str(exc))
        return 1

    token, me = login()
    record("Demo login", bool(token))
    if not token:
        return 1

    status, credit = request("GET", "/users/me/credit", token=token)
    record("A29 GET /users/me/credit", status == 200 and isinstance(credit, dict) and assert_credit(credit), str(credit.get("score") if isinstance(credit, dict) else status))

    status, reviews = request("GET", "/users/me/reviews/summary", token=token)
    record("A30 GET /users/me/reviews/summary", status == 200 and isinstance(reviews, dict) and assert_review_summary(reviews), str(reviews.get("pendingCount") if isinstance(reviews, dict) else status))

    status, ver = request("GET", "/users/me/verification", token=token)
    record("A31 GET /users/me/verification", status == 200 and isinstance(ver, dict) and assert_verification(ver), str(ver.get("phoneVerified") if isinstance(ver, dict) else status))

    status, feed = request("GET", "/catalog/feed", query={"regionState": "VIC", "regionCity": "Melbourne", "pageSize": 1})
    seller_id = None
    if isinstance(feed, dict) and feed.get("items"):
        seller_id = feed["items"][0].get("seller", {}).get("id")
    record("Resolve seller id from feed", bool(seller_id), seller_id or "none")

    if seller_id:
        status, profile = request("GET", f"/users/{urllib.parse.quote(seller_id, safe='')}/profile")
        record("A48 GET /users/:id/profile", status == 200 and isinstance(profile, dict) and assert_public_profile(profile), profile.get("nickname", "")[:30] if isinstance(profile, dict) else str(status))

        status, listings = request("GET", f"/users/{urllib.parse.quote(seller_id, safe='')}/listings", query={"pageSize": 10})
        first = (listings or {}).get("items", [{}])[0] if isinstance(listings, dict) else {}
        record(
            "A49 GET /users/:id/listings",
            status == 200 and isinstance(listings, dict) and isinstance(listings.get("items"), list),
            f"{len(listings.get('items', []))} listings" if isinstance(listings, dict) else str(status),
        )
    else:
        record("A48 GET /users/:id/profile", False, "no seller id")
        record("A49 GET /users/:id/listings", False, "no seller id")

    if seller_id and me and seller_id != me.get("id"):
        request("DELETE", f"/follows/{urllib.parse.quote(seller_id, safe='')}", token=token)
        status, _ = request("POST", f"/follows/{urllib.parse.quote(seller_id, safe='')}", token=token)
        record("A46 POST /follows/:userId", status == 204, str(status))

        status, follows = request("GET", "/follows", query={"pageSize": 100}, token=token)
        items = follows.get("items", []) if isinstance(follows, dict) else []
        has = any(item.get("userId") == seller_id for item in items)
        record("A45 GET /follows", status == 200 and isinstance(follows, dict) and (not items or assert_follow_item(items[0])), f"count={len(items)} has_target={has}")

        status, _ = request("DELETE", f"/follows/{urllib.parse.quote(seller_id, safe='')}", token=token)
        record("A47 DELETE /follows/:userId", status == 204, str(status))
    else:
        record("A46 POST /follows/:userId", False, "no distinct seller target")
        record("A45 GET /follows", False, "skipped")
        record("A47 DELETE /follows/:userId", False, "skipped")

    status, missing = request("GET", "/users/not-a-real-user/profile")
    record("Public profile 404", status == 404 and isinstance(missing, dict) and missing.get("code") == "NOT_FOUND", str(status))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} Sprint 3 checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())