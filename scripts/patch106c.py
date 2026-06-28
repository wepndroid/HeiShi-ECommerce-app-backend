from pathlib import Path
p = Path("prog106_catalog_verify.py")
t = p.read_text(encoding="utf-8")
if "import httpx" not in t:
    t = t.replace("import json", "import json\nimport httpx")
if "def sample_image_bytes" not in t:
    helper = """

def sample_image_bytes() -> bytes | None:
    uploads = Path(__file__).resolve().parents[1] / \"uploads\"
    image_path = uploads / \"43e818ad004e46d6bf5235533dc9c0d3.jpg\"
    if image_path.is_file() and image_path.stat().st_size > 1000:
        return image_path.read_bytes()
    try:
        feed = httpx.get(f\"{BASE}/catalog/feed\", params={**REGION, \"pageSize\": 1}, timeout=15).json()
        img_url = feed[\"items\"][0][\"imageUrl\"]
        return httpx.get(img_url, timeout=30).content
    except Exception:
        return None
"""
    t = t.replace("\ndef main() -> int:", helper + "\ndef main() -> int:")
old = """    uploads = Path(__file__).resolve().parents[1] / \"uploads\"
    image_path = uploads / \"43e818ad004e46d6bf5235533dc9c0d3.jpg\"
    if not image_path.is_file():
        c = list(uploads.glob(\"*.jpg\")) if uploads.is_dir() else []
        image_path = c[0] if c else None
    if image_path and Path(image_path).is_file():
        boundary = \"----HeyMarketCatalogVerify\"
        file_bytes = Path(image_path).read_bytes()
        body = (f\"--{boundary}\\r\\nContent-Disposition: form-data; name=\\\"file\\\"; filename=\\\"test.jpg\\\"\\r\\nContent-Type: image/jpeg\\r\\n\\r\\n\").encode() + file_bytes + f\"\\r\\n--{boundary}--\\r\\n\".encode()
        url = f\"{BASE}/catalog/search/image?\" + urllib.parse.urlencode({**REGION, \"pageSize\": 5})
        req = urllib.request.Request(url, data=body, headers={\"Accept\": \"application/json\", \"Content-Type\": f\"multipart/form-data; boundary={boundary}\"}, method=\"POST\")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                img_data = json.loads(resp.read().decode())
                img_status = resp.status
        except urllib.error.HTTPError as err:
            img_status = err.code
            raw = err.read().decode()
            img_data = json.loads(raw) if raw else None
        record(\"A9 POST /catalog/search/image\", img_status == 200 and isinstance(img_data, dict) and \"matchCount\" in img_data, f\"matchCount={img_data.get('matchCount') if isinstance(img_data, dict) else img_status}\")
    else:
        record(\"A9 POST /catalog/search/image\", False, \"no sample image\")"""
new = """    file_bytes = sample_image_bytes()
    if file_bytes:
        try:
            resp = httpx.post(
                f\"{BASE}/catalog/search/image\",
                params={**REGION, \"pageSize\": 5},
                files={\"file\": (\"test.jpg\", file_bytes, \"image/jpeg\")},
                timeout=120,
            )
            img_status = resp.status_code
            img_data = resp.json() if resp.content else None
        except Exception as exc:
            img_status = 0
            img_data = None
            record(\"A9 POST /catalog/search/image\", False, str(exc))
            img_status = -1
        if img_status != -1:
            record(
                \"A9 POST /catalog/search/image\",
                img_status == 200 and isinstance(img_data, dict) and \"matchCount\" in img_data,
                f\"matchCount={img_data.get('matchCount') if isinstance(img_data, dict) else img_status}\",
            )
    else:
        record(\"A9 POST /catalog/search/image\", False, \"no sample image\")"""
if old in t:
    t = t.replace(old, new)
    p.write_text(t, encoding="utf-8")
    print("patched image block")
else:
    print("old block not found")
