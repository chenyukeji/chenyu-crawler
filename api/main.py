from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="chenyu-crawler", version="0.1.0")


@app.get("/")
def health():
    return {"service": "chenyu-crawler", "status": "running"}


@app.get("/api/status")
def status():
    return {"crawler": "ready", "scheduler": "not_configured"}
