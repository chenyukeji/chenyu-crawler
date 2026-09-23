from __future__ import annotations

from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta
from pathlib import Path
import re
from threading import Thread

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from crawler.config import (
    load_config,
    save_config,
    source_id,
    validate_daily_schedule,
)
from crawler.amazon import validate_source
from database.connection import connect_database
from scheduler_loop import SCHEDULE_TIMEZONE, loop, start_manual_run


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "sources.json"


@asynccontextmanager
async def lifespan(_: FastAPI):
    Thread(target=loop, name="daily-collector", daemon=True).start()
    yield


app = FastAPI(
    title="Amazon New Releases Collector",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
templates = Jinja2Templates(directory=str(ROOT / "web" / "templates"))


def scheduler_state() -> dict[str, str]:
    with closing(connect_database()) as connection:
        latest = connection.execute(
            """
            SELECT started_at
            FROM collection_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        if not latest:
            return {}
        rows = connection.execute(
            """
            SELECT snapshot_date, status, item_count, error_message,
                   COALESCE(finished_at, started_at) AS updated_at
            FROM collection_runs
            WHERE started_at = ?
            ORDER BY marketplace, category
            """,
            (str(latest[0]),),
        ).fetchall()
    if not rows:
        return {}
    statuses = {str(row[1]) for row in rows}
    if "RUNNING" in statuses:
        status = "running"
    elif "FAILED" in statuses:
        status = "failed"
    else:
        status = "success"
    errors = [str(row[3]) for row in rows if row[3]]
    completed = sum(1 for row in rows if str(row[1]) == "COMPLETE")
    message = (
        "\n".join(errors)
        if errors
        else f"完成 {completed}/{len(rows)} 个数据源，共采集 {sum(int(row[2]) for row in rows)} 条。"
    )
    return {
        "last_attempt_date": str(rows[0][0]),
        "last_status": status,
        "last_message": message,
        "updated_at": max(str(row[4]) for row in rows),
    }


def database_stats() -> dict:
    today = datetime.now(SCHEDULE_TIMEZONE).date()
    cutoff = (today - timedelta(days=6)).isoformat()
    with closing(connect_database()) as connection:
        summary = connection.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT asin), COUNT(DISTINCT source_url),
                   COALESCE(MAX(snapshot_date), '')
            FROM observations
            """
        ).fetchone()
        counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """
                SELECT snapshot_date, COUNT(*)
                FROM observations
                WHERE snapshot_date >= ?
                GROUP BY snapshot_date
                """,
                (cutoff,),
            ).fetchall()
        }
    chart = []
    maximum = max(counts.values(), default=0)
    for offset in range(6, -1, -1):
        current = today - timedelta(days=offset)
        count = counts.get(current.isoformat(), 0)
        chart.append(
            {
                "date": current.isoformat(),
                "label": current.strftime("%m-%d"),
                "count": count,
                "height": round(count / maximum * 100) if maximum else 0,
            }
        )
    return {
        "observations": int(summary[0]),
        "unique_asins": int(summary[1]),
        "active_snapshots": int(summary[2]),
        "latest_date": str(summary[3]),
        "chart": chart,
    }


def page_context(
    request: Request,
    *,
    saved: bool = False,
    deleted: bool = False,
    run_started: bool = False,
    run_busy: bool = False,
    error: str = "",
) -> dict:
    config = load_config(CONFIG_PATH)
    sources = [dict(source, source_id=source_id(source)) for source in config["sources"]]
    enabled_count = sum(1 for source in sources if source["enabled"])
    return {
        "request": request,
        "daily_schedule": config["daily_schedule"],
        "schedule_timezone": "Asia/Shanghai",
        "retention_days": config["collection_policy"]["retention_days"],
        "sources": sources,
        "enabled_count": enabled_count,
        "marketplace_count": len({source["marketplace"] for source in sources}),
        "state": scheduler_state(),
        "stats": database_stats(),
        "saved": saved,
        "deleted": deleted,
        "run_started": run_started,
        "run_busy": run_busy,
        "error": error,
    }


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    saved: bool = False,
    deleted: bool = False,
    run_started: bool = False,
    run_busy: bool = False,
):
    return templates.TemplateResponse(
        request,
        "index.html",
        page_context(
            request,
            saved=saved,
            deleted=deleted,
            run_started=run_started,
            run_busy=run_busy,
        ),
    )


@app.post("/run")
def run_now():
    started = start_manual_run()
    return RedirectResponse(
        "/?run_started=true" if started else "/?run_busy=true",
        status_code=303,
    )


def remove_source(target_id: str, config_path: Path) -> None:
    config = load_config(config_path)
    remaining = [
        source for source in config["sources"] if source_id(source) != target_id
    ]
    if len(remaining) == len(config["sources"]):
        raise ValueError("要删除的数据源不存在")
    if not remaining:
        raise ValueError("至少需要保留一个数据源")
    if not any(source["enabled"] for source in remaining):
        raise ValueError("请先启用另一个数据源，再删除当前数据源")
    config["sources"] = remaining
    save_config(config_path, config)


@app.post("/source/delete", response_class=HTMLResponse)
async def delete_source(request: Request):
    form = await request.form()
    target_id = str(form.get("source_id", "")).strip()
    try:
        remove_source(target_id, CONFIG_PATH)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            page_context(request, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse("/?deleted=true", status_code=303)


@app.post("/config", response_class=HTMLResponse)
async def update_config(request: Request):
    form = await request.form()
    try:
        schedule = validate_daily_schedule(str(form.get("daily_schedule", "")))
        enabled_ids = {str(value) for value in form.getlist("enabled_sources")}
        config = load_config(CONFIG_PATH)
        new_marketplace = str(form.get("new_marketplace", "")).strip().upper()
        new_category = str(form.get("new_category", "")).strip().lower()
        new_url = str(form.get("new_url", "")).strip()
        if any((new_marketplace, new_category, new_url)):
            if not all((new_marketplace, new_category, new_url)):
                raise ValueError("新增类目时请完整填写站点、类目名称和 URL")
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", new_category):
                raise ValueError("类目名称只能使用小写字母、数字和连字符")
            new_source = {
                "marketplace": new_marketplace,
                "category": new_category,
                "url": new_url,
                "enabled": True,
            }
            validate_source(new_source)
            new_source_id = source_id(new_source)
            if new_source_id in {source_id(source) for source in config["sources"]}:
                raise ValueError("该站点和类目已经存在")
            config["sources"].append(new_source)
            enabled_ids.add(new_source_id)
        if not enabled_ids:
            raise ValueError("至少需要启用一个数据源")
        known_ids = {source_id(source) for source in config["sources"]}
        if not enabled_ids <= known_ids:
            raise ValueError("包含未知数据源")
        config["daily_schedule"] = schedule
        for source in config["sources"]:
            source["enabled"] = source_id(source) in enabled_ids
        save_config(CONFIG_PATH, config)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            page_context(request, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse("/?saved=true", status_code=303)
