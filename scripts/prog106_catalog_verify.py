"""PROG-106 catalog read verification — live API (Sprint 2, A6–A13)."""
from __future__ import annotations

import json
import httpx
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BASE = os.environ.get("HEYMARKET_API_BASE", "http://127.0.0.1:8000/v1")
HEALTH_URL = BASE.replace("/v1", "") + "/health"
REGION = {"regionState": "VIC", "regionCity": "Melbourne"}
results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"{mark}  {name}{suffix}")


def request(method: str, path: str, *, query: dict | None = None, body: bytes | None = None, content_type: str | None = None):
    url = f"{BASE}{path}"
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    headers = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
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


def assert_listing_summary(item: dict) -> bool:
    keys = ("id", "type", "title", "price", "currency", "categoryKey", "tagKey", "locationLabel", "imageUrl", "seller", "status", "createdAt")
    if not all(k in item for k in keys):
        return False
    seller = item.get("seller") or {}
    return isinstance(seller.get("id"), str) and isinstance(seller.get("nickname"), str)


def assert_listing_detail(item: dict) -> bool:
    return assert_listing_summary(item) and isinstance(item.get("images"), list)


def assert_local_service(item: dict) -> bool:
    return all(k in item for k in ("id", "title", "description", "priceFrom", "currency", "area", "icon", "seller"))



def sample_image_bytes() -> bytes | None:
    uploads = Path(__file__).resolve().parents[1] / "uploads"
    image_path = uploads / "43e818ad004e46d6bf5235533dc9c0d3.jpg"
    if image_path.is_file() and image_path.stat().st_size > 1000:
        return image_path.read_bytes()
    try:
        feed = httpx.get(f"{BASE}/catalog/feed", params={**REGION, "pageSize": 1}, timeout=15).json()
        img_url = feed["items"][0]["imageUrl"]
        return httpx.get(img_url, timeout=30).content
    except Exception:
        return None

def main() -> int:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            record("Backend health", resp.status == 200)
    except Exception as exc:
        record("Backend health", False, str(exc))
        return 1

    status, data = request("GET", "/catalog/form-options")
    record("A6 GET /catalog/form-options", status == 200 and isinstance(data, dict) and "categories" in data, str(len(data.get("categories", []))) if isinstance(data, dict) else str(status))

    status, feed = request("GET", "/catalog/feed", query={**REGION, "tab": "recommended", "pageSize": 10})
    first = (feed or {}).get("items", [{}])[0] if isinstance(feed, dict) else {}
    record("A7 GET /catalog/feed", status == 200 and isinstance(feed, dict) and (not feed.get("items") or assert_listing_summary(first)), f"{len(feed.get('items', []))} items" if isinstance(feed, dict) else str(status))

    status, search = request("GET", "/catalog/search", query={**REGION, "q": "headphones", "sort": "relevance", "pageSize": 10})
    sfirst = (search or {}).get("items", [{}])[0] if isinstance(search, dict) else {}
    record("A8 GET /catalog/search", status == 200 and isinstance(search, dict) and (not search.get("items") or assert_listing_summary(sfirst)), f"{len(search.get('items', []))} hits" if isinstance(search, dict) else str(status))

    listing_id = first.get("id") if isinstance(first, dict) else None
    if listing_id:
        status, detail = request("GET", f"/catalog/listings/{listing_id}")
        record("A10 GET /catalog/listings/:id", status == 200 and isinstance(detail, dict) and assert_listing_detail(detail), str(listing_id))
        status, related = request("GET", f"/catalog/listings/{listing_id}/related", query={**REGION, "pageSize": 6})
        rfirst = (related or {}).get("items", [{}])[0] if isinstance(related, dict) else {}
        record("A11 GET /catalog/listings/:id/related", status == 200 and isinstance(related, dict) and (not related.get("items") or assert_listing_summary(rfirst)), f"{len(related.get('items', []))} related" if isinstance(related, dict) else str(status))
    else:
        record("A10 GET /catalog/listings/:id", False, "no listing id")
        record("A11 GET /catalog/listings/:id/related", False, "no listing id")

    status, services = request("GET", "/catalog/services", query={**REGION, "pageSize": 10})
    svfirst = (services or {}).get("items", [{}])[0] if isinstance(services, dict) else {}
    record("A12 GET /catalog/services", status == 200 and isinstance(services, dict) and (not services.get("items") or assert_local_service(svfirst)), f"{len(services.get('items', []))} services" if isinstance(services, dict) else str(status))

    status, suggestions = request("GET", "/catalog/suggestions", query=REGION)
    record("A13 GET /catalog/suggestions", status == 200 and isinstance(suggestions, list), f"{len(suggestions)} suggestions" if isinstance(suggestions, list) else str(status))

    file_bytes = sample_image_bytes()
    if file_bytes:
        try:
            resp = httpx.post(
                f"{BASE}/catalog/search/image",
                params={**REGION, "pageSize": 5},
                files={"file": ("test.jpg", file_bytes, "image/jpeg")},
                timeout=120,
            )
            img_status = resp.status_code
            img_data = resp.json() if resp.content else None
        except Exception as exc:
            img_status = 0
            img_data = None
            record("A9 POST /catalog/search/image", False, str(exc))
            img_status = -1
        if img_status != -1:
            record(
                "A9 POST /catalog/search/image",
                img_status == 200 and isinstance(img_data, dict) and "matchCount" in img_data,
                f"matchCount={img_data.get('matchCount') if isinstance(img_data, dict) else img_status}",
            )
    else:
        record("A9 POST /catalog/search/image", False, "no sample image")

    status, missing = request("GET", "/catalog/listings/999999")
    record("Listing 404 NOT_FOUND", status == 404 and isinstance(missing, dict) and missing.get("code") == "NOT_FOUND", str(status))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} catalog checks passed")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())