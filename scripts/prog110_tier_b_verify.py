"""PROG-110 Tier B wire-then-verify: coupons, address PATCH, order review."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("HEYMARKET_API_BASE", "http://127.0.0.1:8000/v1")
HEALTH_URL = BASE.replace("/v1", "") + "/health"
results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f" - {detail}" if detail else ""))


def request(method: str, path: str, *, query=None, body=None, token=None):
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
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode()
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as err:
        text = err.read().decode()
        try:
            return err.code, json.loads(text) if text else None
        except json.JSONDecodeError:
            return err.code, {"message": text}


def login():
    status, data = request("POST", "/auth/login", body={"phone": "0400000000", "password": "demo123"})
    if status == 200 and isinstance(data, dict):
        return data.get("accessToken"), data.get("user")
    return None, None


def pick_buyable_listing(buyer_id: str) -> int | None:
    status, feed = request("GET", "/catalog/feed", query={"regionState": "VIC", "regionCity": "Melbourne", "pageSize": 30})
    if status != 200 or not isinstance(feed, dict):
        return None
    for item in feed.get("items", []):
        seller_id = (item.get("seller") or {}).get("id")
        if seller_id and seller_id != buyer_id and item.get("status") == "active":
            return item.get("id")
    return None


def main() -> int:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            record("Backend health", resp.status == 200)
    except Exception as exc:
        record("Backend health", False, str(exc))
        return 1

    token, me = login()
    record("Demo login", bool(token and me))
    if not token or not me:
        return 1
    buyer_id = me["id"]

    status, coupons = request("GET", "/coupons", query={"pageSize": 20}, token=token)
    items = (coupons or {}).get("items", []) if isinstance(coupons, dict) else []
    available = [c for c in items if c.get("status") == "available"]
    record(
        "B1 GET /coupons",
        status == 200 and isinstance(coupons, dict) and len(items) >= 1,
        f"{len(items)} total, {len(available)} available",
    )
    if available:
        coupon_id = available[0]["id"]
        status, _ = request("POST", f"/coupons/{coupon_id}/redeem", token=token)
        record("B1 POST /coupons/:id/redeem", status == 204, str(status))
        status, after = request("GET", "/coupons", query={"pageSize": 20}, token=token)
        redeemed = next((c for c in (after or {}).get("items", []) if c.get("id") == coupon_id), None)
        record(
            "B1 redeem keeps status=available",
            status == 200 and redeemed and redeemed.get("status") == "available",
            redeemed.get("status") if redeemed else "missing",
        )
    else:
        record("B1 POST /coupons/:id/redeem", False, "no available coupon")
        record("B1 redeem keeps status=available", False, "skipped")

    status, addresses = request("GET", "/users/me/addresses", token=token)
    addr_list = addresses if isinstance(addresses, list) else []
    record("B2 GET /users/me/addresses", status == 200, f"{len(addr_list)} addresses")

    addr_id = None
    if not addr_list:
        status, created = request(
            "POST",
            "/users/me/addresses",
            body={"label": "Verify spot", "area": "Melbourne CBD", "meetupSpot": "State Library"},
            token=token,
        )
        ok_create = status == 201 and isinstance(created, dict) and created.get("id")
        record("B2 POST /users/me/addresses (setup)", ok_create, str(created.get("id") if isinstance(created, dict) else status))
        if ok_create:
            addr_id = created["id"]
    else:
        addr_id = addr_list[0]["id"]

    if addr_id:
        status, updated = request(
            "PATCH",
            f"/users/me/addresses/{addr_id}",
            body={"area": "Clayton"},
            token=token,
        )
        record(
            "B2 PATCH /users/me/addresses/:id",
            status == 200 and isinstance(updated, dict) and updated.get("area") == "Clayton",
            updated.get("area") if isinstance(updated, dict) else str(status),
        )
    else:
        record("B2 PATCH /users/me/addresses/:id", False, "no address to patch")

    pending_review_id = None
    status, pending = request("GET", "/orders", query={"status": "pendingReview", "pageSize": 20}, token=token)
    if status == 200 and isinstance(pending, dict):
        for order in pending.get("items", []):
            pending_review_id = order.get("id")
            if pending_review_id:
                break

    if not pending_review_id:
        listing_id = pick_buyable_listing(buyer_id)
        if listing_id:
            status, created = request("POST", "/orders", body={"listingId": listing_id, "deliveryMethod": "meetup"}, token=token)
            if status == 201 and isinstance(created, dict):
                oid = created["id"]
                request("POST", f"/orders/{oid}/pay", token=token)
                request("POST", f"/orders/{oid}/confirm-receive", token=token)
                status, after = request("GET", "/orders", query={"status": "pendingReview", "pageSize": 20}, token=token)
                if status == 200 and isinstance(after, dict) and after.get("items"):
                    pending_review_id = after["items"][0]["id"]

    if pending_review_id:
        status, _ = request(
            "POST",
            f"/orders/{pending_review_id}/review",
            body={
                "criteria": {
                    "quality": 5,
                    "communication": 5,
                    "trustement": 5,
                },
                "comment": "Tier B verify",
            },
            token=token,
        )
        record("B3 POST /orders/:id/review", status == 204, str(status))
        status, completed = request("GET", "/orders", query={"status": "completed", "pageSize": 50}, token=token)
        done = any(o.get("id") == pending_review_id for o in (completed or {}).get("items", []))
        record("B3 order status=completed", status == 200 and done, f"order={pending_review_id}")
    else:
        record("B3 POST /orders/:id/review", False, "no pendingReview order")
        record("B3 order status=completed", False, "skipped")

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} Tier B checks passed")
    if failed:
        print("Failures:")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())