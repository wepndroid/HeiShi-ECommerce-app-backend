"""PROG-108 Sprint 4 — messages and chat (A32-A35)."""
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


def assert_conversation(d: dict) -> bool:
    if not all(k in d for k in ("id", "counterpart", "unreadCount")):
        return False
    cp = d.get("counterpart") or {}
    return isinstance(cp.get("id"), str) and isinstance(cp.get("nickname"), str)


def assert_message(d: dict) -> bool:
    return all(k in d for k in ("id", "conversationId", "senderId", "text", "sentAt"))


def pick_listing_for_chat(token: str, buyer_id: str) -> int | None:
    status, feed = request("GET", "/catalog/feed", query={"regionState": "VIC", "regionCity": "Melbourne", "pageSize": 20})
    if status != 200 or not isinstance(feed, dict):
        return None
    for item in feed.get("items", []):
        seller_id = (item.get("seller") or {}).get("id")
        if seller_id and seller_id != buyer_id:
            return item.get("id")
    return None


def main() -> int:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            record("Backend health", resp.status == 200)
    except Exception as exc:
        record("Backend health", False, str(exc))
        return 1

    status, _ = request("GET", "/conversations")
    record("Conversations require auth (401)", status == 401, str(status))

    token, me = login()
    record("Demo login", bool(token and me))
    if not token or not me:
        return 1

    buyer_id = me.get("id")
    status, conv_list = request("GET", "/conversations", query={"pageSize": 50}, token=token)
    first = (conv_list or {}).get("items", [{}])[0] if isinstance(conv_list, dict) else {}
    record(
        "A32 GET /conversations",
        status == 200 and isinstance(conv_list, dict) and isinstance(conv_list.get("items"), list)
        and (not conv_list.get("items") or assert_conversation(first)),
        f"{len(conv_list.get('items', []))} conversations" if isinstance(conv_list, dict) else str(status),
    )

    listing_id = pick_listing_for_chat(token, buyer_id)
    record("Pick listing for chat", bool(listing_id), str(listing_id))

    conv_id = None
    if listing_id:
        status, opened = request("POST", "/conversations", body={"listingId": listing_id}, token=token)
        record(
            "A33 POST /conversations",
            status in (200, 201) and isinstance(opened, dict) and assert_conversation(opened),
            opened.get("id", "")[:36] if isinstance(opened, dict) else str(status),
        )
        if isinstance(opened, dict):
            conv_id = opened.get("id")
            status2, reopened = request("POST", "/conversations", body={"listingId": listing_id}, token=token)
            record(
                "A33 idempotent reopen same conversation",
                status2 in (200, 201) and isinstance(reopened, dict) and reopened.get("id") == conv_id,
                str(status2),
            )
    else:
        record("A33 POST /conversations", False, "no listing")
        record("A33 idempotent reopen same conversation", False, "skipped")

    if conv_id:
        status, msgs = request("GET", f"/conversations/{conv_id}/messages", query={"limit": 50}, token=token)
        msg_items = (msgs or {}).get("items", []) if isinstance(msgs, dict) else []
        mfirst = msg_items[0] if msg_items else {}
        record(
            "A34 GET /conversations/:id/messages",
            status == 200 and isinstance(msgs, dict) and isinstance(msgs.get("items"), list)
            and (not msgs.get("items") or assert_message(mfirst)),
            f"{len(msgs.get('items', []))} messages" if isinstance(msgs, dict) else str(status),
        )

        test_text = "Sprint4 verify ping"
        status, sent = request("POST", f"/conversations/{conv_id}/messages", body={"text": test_text}, token=token)
        record(
            "A35 POST /conversations/:id/messages",
            status == 201 and isinstance(sent, dict) and assert_message(sent) and sent.get("text") == test_text,
            sent.get("id", "")[:36] if isinstance(sent, dict) else str(status),
        )

        status, msgs2 = request("GET", f"/conversations/{conv_id}/messages", query={"limit": 50}, token=token)
        items = msgs2.get("items", []) if isinstance(msgs2, dict) else []
        has_sent = any(m.get("text") == test_text for m in items)
        record("A35 message appears in thread", has_sent, f"items={len(items)}")
    else:
        record("A34 GET /conversations/:id/messages", False, "no conversation id")
        record("A35 POST /conversations/:id/messages", False, "no conversation id")
        record("A35 message appears in thread", False, "skipped")

    status, bad = request("GET", "/conversations/not-a-real-id/messages", token=token)
    record("Messages 404 NOT_FOUND", status == 404 and isinstance(bad, dict) and bad.get("code") == "NOT_FOUND", str(status))

    if conv_id:
        status, empty = request("POST", f"/conversations/{conv_id}/messages", body={"text": "   "}, token=token)
        record("Empty message rejected (422)", status == 422, str(status))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} Sprint 4 checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())