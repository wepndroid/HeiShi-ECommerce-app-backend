from contextlib import asynccontextmanager
import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings, validate_runtime_configuration
from app.database import Base, SessionLocal, engine
from app.routers import auth, catalog, listings, messages, orders, platform_features, region_safety, user_data, users
from app.routers import admin_routes
from app.payments.router import router as payments_router
from app.migrations import run_migrations
from app.conversation_inbox import cleanup_duplicate_empty_conversations
from app.messaging_read import backfill_read_watermarks
from app.seed import seed
from app.background_jobs import run_periodic_cycle, scheduler_owner_id


logger = logging.getLogger(__name__)
_SCHEDULER_OWNER_ID = scheduler_owner_id()


async def _auto_confirm_loop() -> None:
    while True:
        # Payment deadlines are measured in minutes, so an hourly worker can
        # expire an order before either reminder is ever evaluated.
        await asyncio.sleep(max(settings.background_jobs_interval_seconds, 10))
        db = SessionLocal()
        try:
            run_periodic_cycle(db, owner_id=_SCHEDULER_OWNER_ID)
        except Exception:  # noqa: BLE001 - never allow the periodic task to die
            db.rollback()
            logger.exception("Unexpected periodic scheduler-cycle failure")
        finally:
            db.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_runtime_configuration()
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    db = SessionLocal()
    try:
        backfill_read_watermarks(db)
        cleanup_duplicate_empty_conversations(db)
        seed(db)
        run_periodic_cycle(db, owner_id=_SCHEDULER_OWNER_ID)
    finally:
        db.close()
    task = asyncio.create_task(_auto_confirm_loop())
    yield
    task.cancel()


app = FastAPI(
    title="HeyMarket API",
    version="1.0.0",
    description="Backend API for HeyMarket mobile app — mirrors Frontend/src/api contracts",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def private_network_access(request: Request, call_next):
    """Chrome blocks localhost → 127.0.0.1 unless preflight allows private network."""
    response = await call_next(request)
    if (
        request.method == "OPTIONS"
        and request.headers.get("access-control-request-private-network") == "true"
    ):
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

upload_path = Path(settings.upload_dir)
upload_path.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(upload_path)), name="uploads")

API_PREFIX = "/v1"

app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(catalog.router, prefix=API_PREFIX)
app.include_router(listings.router, prefix=API_PREFIX)
app.include_router(listings.upload_router, prefix=API_PREFIX)
app.include_router(orders.router, prefix=API_PREFIX)
app.include_router(user_data.router, prefix=API_PREFIX)
app.include_router(messages.router, prefix=API_PREFIX)
app.include_router(messages.notifications_router, prefix=API_PREFIX)
app.include_router(users.router, prefix=API_PREFIX)
app.include_router(users.payments_router, prefix=API_PREFIX)
app.include_router(users.payouts_router, prefix=API_PREFIX)
app.include_router(users.settings_router, prefix=API_PREFIX)
app.include_router(region_safety.router, prefix=API_PREFIX)
app.include_router(payments_router, prefix=API_PREFIX)
app.include_router(admin_routes.router, prefix=API_PREFIX)
app.include_router(platform_features.router, prefix=API_PREFIX)
app.include_router(platform_features.admin_router, prefix=API_PREFIX)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "message" in exc.detail:
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, **exc.detail},
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "code": "ERROR",
            "message": str(exc.detail),
            "details": {},
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed",
            "details": exc.errors(),
        },
    )


@app.get("/health")
def health():
    from app.video_processing import video_processor_available

    return {
        "status": "ok",
        "version": "1.0.0",
        "capabilities": {
            "videoProcessing": video_processor_available(),
            "verifiedAndroidLinks": bool(
                settings.android_app_sha256_fingerprints.strip()
            ),
            "verifiedIosLinks": bool(settings.apple_team_id.strip()),
        },
    }


@app.get("/v1")
def v1_root():
    return {"service": "HeyMarket API", "version": "v1", "docs": "/docs"}


@app.get("/.well-known/assetlinks.json")
def android_asset_links():
    fingerprints = [
        value.strip()
        for value in settings.android_app_sha256_fingerprints.split(",")
        if value.strip()
    ]
    if not fingerprints:
        return JSONResponse(content=[])
    return JSONResponse(
        content=[
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": settings.android_app_package,
                    "sha256_cert_fingerprints": fingerprints,
                },
            }
        ]
    )


@app.get("/.well-known/apple-app-site-association")
def apple_app_site_association():
    app_id = ".".join(
        value
        for value in (settings.apple_team_id.strip(), settings.apple_bundle_id.strip())
        if value
    )
    details = [{"appID": app_id, "paths": ["/s/*"]}] if settings.apple_team_id.strip() else []
    return JSONResponse(
        content={"applinks": {"apps": [], "details": details}},
        media_type="application/json",
    )


@app.get("/s/{share_token}", response_class=HTMLResponse)
def public_share_landing(share_token: str):
    """Verified-link landing page with an explicit clipboard/install fallback."""
    safe_token = "".join(
        character for character in share_token if character.isalnum() or character in "_-"
    )[:256]
    if not safe_token or safe_token != share_token:
        raise HTTPException(status_code=404, detail="Share link not found")
    deep_link = f"heymarket://shares/{safe_token}"
    share_text = f"【HeyMarket】{safe_token}"
    play_store_url = (
        "https://play.google.com/store/apps/details"
        f"?id={quote(settings.android_app_package, safe='')}"
        f"&referrer={quote(f'share_token={safe_token}', safe='')}"
    )
    ios_install = (
        f"""<button type="button" onclick="copyAndInstall({json.dumps(settings.ios_app_store_url)})">
Install HeyMarket for iPhone</button>"""
        if settings.ios_app_store_url.strip().startswith("https://")
        else ""
    )
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Open in HeyMarket</title></head>
<body style="font-family:system-ui;margin:3rem auto;max-width:32rem;padding:1rem">
<h1>Open this product in HeyMarket</h1>
<p>If the app does not open, copy the share code, install HeyMarket, and open it again.</p>
<p><code id="code">{share_text}</code></p>
<button onclick="navigator.clipboard.writeText(document.getElementById('code').textContent)">
Copy share code</button>
<p><a href="{deep_link}">Open HeyMarket</a></p>
<p><a href="{play_store_url}">Install HeyMarket for Android</a></p>
{ios_install}
<script>
function copyAndInstall(destination) {{
  var shareCode = document.getElementById('code').textContent;
  var proceed = function () {{ window.location.assign(destination); }};
  if (navigator.clipboard && window.isSecureContext) {{
    navigator.clipboard.writeText(shareCode).then(proceed, proceed);
  }} else {{
    proceed();
  }}
}}
window.location.replace({json.dumps(deep_link)});
if (/Android/i.test(navigator.userAgent)) {{
  window.setTimeout(function () {{
    if (!document.hidden) window.location.replace({json.dumps(play_store_url)});
  }}, 1500);
}}
</script>
</body></html>"""
    )
