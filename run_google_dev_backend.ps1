$env:DATABASE_URL = "sqlite:///./heishi_google_dev.db"
$env:GOOGLE_DEV_AUTH_FALLBACK = "true"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8002
