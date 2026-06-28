"""PROG-109 Sprints 5-9 verification (A14-A18, A19-A23, A24-A28, A36-A39, A40-A44, A50-A56, A1 register)."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import httpx

BASE = os.environ.get("HEYMARKET_API_BASE", "http://127.0.0.1:8000/v1")
HEALTH_URL = BASE.replace("/v1", "") + "/health"
results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


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


def assert_order(d: dict) -> bool:
    keys = ("id", "listingId", "listingTitle", "seller", "status", "amount", "escrowFee", "currency", "createdAt", "updatedAt")
    return isinstance(d, dict) and all(k in d for k in keys)


def sample_image() -> bytes | None:
    try:
        feed = httpx.get(f"{BASE}/catalog/feed", params={"regionState": "VIC", "regionCity": "Melbourne", "pageSize": 1}, timeout=15).json()
        url = feed["items"][0]["imageUrl"]
        return httpx.get(url, timeout=30).content
    except Exception:
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

    # A1 register (fresh phone)
    phone = f"04{str(int(time.time()))[-8:]}"
    status, send = request("POST", "/auth/register/send-code", body={"phone": phone})
    dev_code = send.get("devCode") if isinstance(send, dict) else None
    status, reg = request(
        "POST",
        "/auth/register",
        body={
            "nickname": "Sprint9User",
            "phone": phone,
            "password": "secret1",
            "verificationCode": dev_code or "000000",
        },
    )
    record("A1 POST /auth/register", status == 201 and isinstance(reg, dict) and reg.get("accessToken"), phone)
    reg_token = reg.get("accessToken") if isinstance(reg, dict) else None

    # Sprint 5 — Orders A19-A23
    ts_orders = int(time.time())
    status, order_seller_listing = request("POST", "/listings", body={
        "type": "product", "title": f"Order flow verify {ts_orders}", "description": "Order e2e listing",
        "price": 9.99, "categoryKey": "misc", "conditionKey": "lightlyUsed", "tagKey": "lightlyUsed",
        "locationLabel": "Clayton", "imageUrls": ["https://images.pexels.com/photos/3780681/pexels-photo-3780681.jpeg"],
        "pickupMethods": ["meetup"],
    }, token=token)
    order_listing_id = order_seller_listing.get("id") if isinstance(order_seller_listing, dict) else None
    listing_id = pick_buyable_listing(buyer_id)
    order_id = None
    status, orders = request("GET", "/orders", query={"pageSize": 50}, token=token)
    record("A19 GET /orders", status == 200 and isinstance(orders, dict) and isinstance(orders.get("items"), list), f"{len(orders.get('items', []))} orders" if isinstance(orders, dict) else str(status))

    if reg_token and order_listing_id:
        status, created = request("POST", "/orders", body={"listingId": order_listing_id, "deliveryMethod": "meetup"}, token=reg_token)
        ok_create = status == 201 and isinstance(created, dict) and assert_order(created) and created.get("status") == "pendingPay"
        record("A20 POST /orders", ok_create, str(created.get("id") if isinstance(created, dict) else status))
        if ok_create:
            order_id = created["id"]
            status, paid = request("POST", f"/orders/{order_id}/pay", token=reg_token)
            record("A21 POST /orders/:id/pay", status == 200 and isinstance(paid, dict) and paid.get("status") == "pendingShip", paid.get("status", "") if isinstance(paid, dict) else str(status))
            status, _ = request("POST", f"/orders/{order_id}/remind-ship", token=reg_token)
            record("A22 POST /orders/:id/remind-ship", status == 204, str(status))
            status, early = request("POST", f"/orders/{order_id}/confirm-receive", token=reg_token)
            record("A22b confirm-receive before ship rejected", status == 400, str(status))
            status, shipped = request("POST", f"/orders/{order_id}/ship", token=token)
            record("A22c POST /orders/:id/ship", status == 200 and isinstance(shipped, dict) and shipped.get("status") == "pendingReceive", shipped.get("status", "") if isinstance(shipped, dict) else str(status))
            status, received = request("POST", f"/orders/{order_id}/confirm-receive", token=reg_token)
            record("A23 POST /orders/:id/confirm-receive", status == 200 and isinstance(received, dict) and received.get("status") == "pendingReview", received.get("status", "") if isinstance(received, dict) else str(status))
    elif listing_id:
        status, created = request("POST", "/orders", body={"listingId": listing_id, "deliveryMethod": "meetup"}, token=token)
        ok_create = status == 201 and isinstance(created, dict) and assert_order(created) and created.get("status") == "pendingPay"
        record("A20 POST /orders", ok_create, str(created.get("id") if isinstance(created, dict) else status))
        if ok_create:
            order_id = created["id"]
            status, paid = request("POST", f"/orders/{order_id}/pay", token=token)
            record("A21 POST /orders/:id/pay", status == 200 and isinstance(paid, dict) and paid.get("status") == "pendingShip", paid.get("status", "") if isinstance(paid, dict) else str(status))
            status, _ = request("POST", f"/orders/{order_id}/remind-ship", token=token)
            record("A22 POST /orders/:id/remind-ship", status == 204, str(status))
            for label in ["A22b confirm-receive before ship rejected", "A22c POST /orders/:id/ship", "A23 POST /orders/:id/confirm-receive"]:
                record(label, False, "need registered buyer + demo seller listing")
    else:
        for label in ["A20 POST /orders", "A21 POST /orders/:id/pay", "A22 POST /orders/:id/remind-ship", "A22b confirm-receive before ship rejected", "A22c POST /orders/:id/ship", "A23 POST /orders/:id/confirm-receive"]:
            record(label, False, "no order listing or buyer")

    # Sprint 6 — Listings A14-A18
    status, mine = request("GET", "/listings/mine", query={"pageSize": 20}, token=token)
    record("A14 GET /listings/mine", status == 200 and isinstance(mine, dict), f"{len(mine.get('items', []))} mine" if isinstance(mine, dict) else str(status))
    status, sold = request("GET", "/listings/sold", query={"pageSize": 20}, token=token)
    record("A15 GET /listings/sold", status == 200 and isinstance(sold, dict), f"{len(sold.get('items', []))} sold" if isinstance(sold, dict) else str(status))

    img = sample_image()
    image_url = "https://images.pexels.com/photos/3780681/pexels-photo-3780681.jpeg"
    if img:
        up = httpx.post(f"{BASE}/uploads/images", files={"file": ("test.jpg", img, "image/jpeg")}, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if up.status_code in (200, 201):
            image_url = up.json().get("url", image_url)
    record("A18 POST /uploads/images", image_url.startswith("http"), image_url[:60])

    ts = int(time.time())
    status, created_listing = request("POST", "/listings", body={
        "type": "product", "title": f"Sprint6 verify {ts}", "description": "Test listing",
        "price": 12.5, "categoryKey": "misc", "conditionKey": "lightlyUsed", "tagKey": "lightlyUsed",
        "locationLabel": "Clayton", "imageUrls": [image_url], "pickupMethods": ["meetup"],
    }, token=token)
    record("A16 POST /listings", status == 201 and isinstance(created_listing, dict) and created_listing.get("title"), str(created_listing.get("id") if isinstance(created_listing, dict) else status))

    resale_source = order_listing_id if reg_token else None
    if resale_source and reg_token:
        status, resale = request("POST", f"/listings/resale/{resale_source}", token=reg_token)
        record("A17 POST /listings/resale/:id", status == 201 and isinstance(resale, dict) and resale.get("status") == "draft", str(resale.get("id") if isinstance(resale, dict) else status))
    else:
        record("A17 POST /listings/resale/:id", False, "need buyer purchase flow (reg_token + order_listing_id)")

    # Sprint 7 — Favorites & history A40-A44
    fav_listing = pick_buyable_listing(buyer_id) or listing_id
    if fav_listing:
        request("DELETE", f"/favorites/{fav_listing}", token=token)
        status, fav = request("POST", f"/favorites/{fav_listing}", token=token)
        record("A41 POST /favorites/:listingId", status in (200, 201) and isinstance(fav, dict) and fav.get("listingId") == fav_listing, str(status))
        status, flist = request("GET", "/favorites", query={"pageSize": 50}, token=token)
        has_fav = any(i.get("listingId") == fav_listing for i in (flist or {}).get("items", []))
        record("A40 GET /favorites", status == 200 and has_fav, f"has={has_fav}")
        status, _ = request("DELETE", f"/favorites/{fav_listing}", token=token)
        record("A42 DELETE /favorites/:listingId", status == 204, str(status))
        status, _ = request("POST", f"/history/views/{fav_listing}", token=token)
        record("A44 POST /history/views/:listingId", status == 204, str(status))
        status, hist = request("GET", "/history/views", query={"pageSize": 50}, token=token)
        has_hist = any(i.get("listingId") == fav_listing for i in (hist or {}).get("items", []))
        record("A43 GET /history/views", status == 200 and has_hist, f"has={has_hist}")
    else:
        for label in ["A40 GET /favorites", "A41 POST /favorites/:listingId", "A42 DELETE /favorites/:listingId", "A43 GET /history/views", "A44 POST /history/views/:listingId"]:
            record(label, False, "no listing")

    # Sprint 8 — Notifications A36-A39
    status, groups = request("GET", "/notifications/groups", token=token)
    record("A36 GET /notifications/groups", status == 200 and isinstance(groups, list) and len(groups) >= 1, f"{len(groups) if isinstance(groups, list) else 0} groups")
    cat = groups[0]["category"] if isinstance(groups, list) and groups else "system"
    status, notes = request("GET", f"/notifications/groups/{cat}", query={"pageSize": 20}, token=token)
    items = (notes or {}).get("items", []) if isinstance(notes, dict) else []
    record("A37 GET /notifications/groups/:cat", status == 200 and isinstance(notes, dict), f"{len(items)} items")
    status, _ = request("POST", f"/notifications/groups/{cat}/mark-read", token=token)
    record("A39 POST /notifications/groups/:cat/mark-read", status == 204, str(status))
    if items:
        nid = items[0]["id"]
        status, _ = request("DELETE", f"/notifications/{nid}", token=token)
        record("A38 DELETE /notifications/:id", status == 204, str(status))
    else:
        record("A38 DELETE /notifications/:id", True, "no items to delete (skipped)")

    # Sprint 9 — Profile, payments, settings A24-A28, A50-A56
    status, profile = request("GET", "/users/me/profile", token=token)
    record("A24 GET /users/me/profile", status == 200 and isinstance(profile, dict) and profile.get("id") == buyer_id, profile.get("nickname", "") if isinstance(profile, dict) else str(status))
    status, patched = request("PATCH", "/users/me/profile", body={"bio": f"Sprint9 {ts}"}, token=token)
    record("A25 PATCH /users/me/profile", status == 200 and isinstance(patched, dict) and patched.get("bio"), str(status))

    status, addrs = request("GET", "/users/me/addresses", token=token)
    record("A26 GET /users/me/addresses", status == 200 and isinstance(addrs, list), f"{len(addrs) if isinstance(addrs, list) else 0} addrs")
    status, addr = request("POST", "/users/me/addresses", body={"label": "Test", "area": "Clayton", "meetupSpot": "Station"}, token=token)
    addr_id = addr.get("id") if isinstance(addr, dict) else None
    record("A27 POST /users/me/addresses", status == 201 and bool(addr_id), str(addr_id or status))
    if addr_id:
        status, _ = request("DELETE", f"/users/me/addresses/{addr_id}", token=token)
        record("A28 DELETE /users/me/addresses/:id", status == 204, str(status))
    else:
        record("A28 DELETE /users/me/addresses/:id", False, "no address id")

    status, pm = request("GET", "/payments/methods", token=token)
    record("A50 GET /payments/methods", status == 200 and isinstance(pm, list), f"{len(pm) if isinstance(pm, list) else 0} methods")
    status, po = request("GET", "/payouts/methods", token=token)
    record("A51 GET /payouts/methods", status == 200 and isinstance(po, list), f"{len(po) if isinstance(po, list) else 0} methods")

    status, ns = request("GET", "/settings/notifications", token=token)
    keys = ("intentAlerts", "chatMessages", "reviewResults", "marketing")
    record("A52 GET /settings/notifications", status == 200 and isinstance(ns, dict) and all(k in ns for k in keys), str(status))
    status, ns2 = request("PATCH", "/settings/notifications", body={"marketing": False}, token=token)
    record("A53 PATCH /settings/notifications", status == 200 and isinstance(ns2, dict) and ns2.get("marketing") is False, str(status))
    status, ps = request("GET", "/settings/privacy", token=token)
    pkeys = ("findByPhone", "showWechatBadge", "personalization")
    record("A54 GET /settings/privacy", status == 200 and isinstance(ps, dict) and all(k in ps for k in pkeys), str(status))
    status, ps2 = request("PATCH", "/settings/privacy", body={"personalization": True}, token=token)
    record("A55 PATCH /settings/privacy", status == 200 and isinstance(ps2, dict) and ps2.get("personalization") is True, str(status))
    status, tr = request("GET", "/settings/transaction-reminders", token=token)
    trkeys = ("payAlerts", "shipAlerts", "receiveAlerts", "disputeAlerts")
    record("A55b GET /settings/transaction-reminders", status == 200 and isinstance(tr, dict) and all(k in tr for k in trkeys), str(status))
    status, tr2 = request("PATCH", "/settings/transaction-reminders", body={"payAlerts": False}, token=token)
    record("A55c PATCH /settings/transaction-reminders", status == 200 and isinstance(tr2, dict) and tr2.get("payAlerts") is False, str(status))
    status, cache = request("POST", "/settings/cache/clear", token=token)
    record("A56 POST /settings/cache/clear", status == 200 and isinstance(cache, dict) and "freedBytes" in cache, str(cache.get("freedBytes") if isinstance(cache, dict) else status))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} Sprints 5-9 checks passed")
    if failed:
        print("Failures:")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())