from __future__ import annotations

from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta
from pathlib import Path
import re
from threading import Thread
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from api.auth import (
    ADMIN_ACCOUNT, AuthenticationUnavailable, SESSION_COOKIE,
    login_admin, verify_admin_session,
)

from crawler.config import (
    load_config,
    save_config,
    source_id,
    validate_daily_schedule,
)
from crawler.amazon import validate_source
from crawler.lifecycle import latest_runs, now_shanghai
from database.connection import connect_database
from scheduler_loop import SCHEDULE_TIMEZONE, loop, start_manual_run
from api.seller_vat_view import load_scheduler_status, load_seller_vat_page
from api.store_matrix_view import load_store_matrix
from api.store_vat_view import load_store_overview
import seller_vat_login
from seller_vat_loop import TRIGGER, profile_ready
from crawler.seller_vat import collection_lock_held, save_json


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "sources.json"
SELLER_VAT_DB_PATH = ROOT / "data" / "seller_vat.sqlite3"
AMAZON_IT_DB_PATH = ROOT / "data" / "amazon_it.sqlite3"
STORE_REPORT_ROOT = ROOT / "outputs" / "seller-vat"
SELLER_VAT_STATUS_PATH = ROOT / "data" / "seller-vat-scheduler.json"


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


@app.get("/favicon.png", include_in_schema=False)
def favicon():
    return FileResponse(ROOT / "web" / "static" / "favicon.png", media_type="image/png")


def safe_next_path(value: str) -> str:
    return value if value in {"/today", "/seller-vat"} else "/"


@app.middleware("http")
async def require_crawler_admin(request: Request, call_next):
    if request.url.path in {"/login", "/favicon.png"}:
        return await call_next(request)
    session = request.cookies.get(SESSION_COOKIE, "")
    if not await run_in_threadpool(verify_admin_session, session):
        if request.url.path == "/status" or "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"error": "请先以管理员登录"}, status_code=401, headers={"Cache-Control": "no-store"})
        next_path = safe_next_path(request.url.path)
        return RedirectResponse(f"/login?next={quote(next_path, safe='')}", status_code=303)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    next_path = safe_next_path(next)
    if await run_in_threadpool(verify_admin_session, request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse(next_path, status_code=303)
    response = templates.TemplateResponse(request, "login.html", {
        "request": request, "next_path": next_path, "error": "",
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/login", response_class=HTMLResponse)
async def submit_login(request: Request):
    form = await request.form()
    next_path = safe_next_path(str(form.get("next", "/")))
    account = str(form.get("account", "")).strip()
    password = str(form.get("password", ""))
    session = None
    error = "账号或密码不正确"
    status_code = 401
    if account == ADMIN_ACCOUNT and password:
        try:
            session = await run_in_threadpool(login_admin, password)
        except AuthenticationUnavailable:
            error = "晨玙网站认证服务暂不可用，请稍后再试"
            status_code = 503
    if session:
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            SESSION_COOKIE, session, httponly=True,
            secure=request.url.scheme == "https", samesite="lax", path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    response = templates.TemplateResponse(request, "login.html", {
        "request": request, "next_path": next_path, "error": error,
    }, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return response


def is_automated_access_restriction(reason: str) -> bool:
    lowered = reason.casefold()
    return any(marker in lowered for marker in (
        "unauthorized ai agent", "agent_restricted", "automated_access_restricted"
    ))


def public_error_message(reason: str) -> str:
    """Summarize old and new restriction errors without claiming an AI model was detected."""
    if not is_automated_access_restriction(reason):
        return reason
    details = []
    for pattern in (r"阶段=([^;]+)", r"第(\d+)页", r"HTTP=(\d+)", r"保留此前有效结果 (\d+) 条"):
        match = re.search(pattern, reason)
        if match:
            value = match.group(1)
            if pattern.startswith("第"):
                value = f"第{value}页"
            elif pattern.startswith("HTTP"):
                value = f"HTTP={value}"
            elif pattern.startswith("保留"):
                value = f"保留本次有效结果 {value} 条"
            details.append(value)
    evidence = re.search(r"证据=([^\s;]+)", reason)
    if evidence:
        details.append(f"诊断={evidence.group(1)}")
    return "自动化访问受限：Amazon 返回限制页，未提供该页商品；具体识别信号未知。" + ("；".join(details) if details else "")


def scheduler_state() -> dict[str, str]:
    with closing(connect_database()) as connection:
        latest = connection.execute(
            """
            SELECT snapshot_date
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
                   COALESCE(finished_at, started_at) AS updated_at, marketplace, category, retry_round, next_retry_at
            FROM collection_runs AS r
            WHERE snapshot_date = ? AND r.rowid = (
                SELECT newer.rowid FROM collection_runs AS newer
                WHERE newer.source_url = r.source_url AND newer.snapshot_date = r.snapshot_date
                ORDER BY newer.started_at DESC, newer.rowid DESC LIMIT 1
            )
            ORDER BY marketplace, category
            """,
            (str(latest[0]),),
        ).fetchall()
    if not rows:
        return {}
    statuses = {str(row[1]) for row in rows}
    if statuses & {"RUNNING", "QUEUED"}:
        status = "running"
    elif statuses != {"COMPLETE"}:
        status = "partial" if any(int(row[2]) > 0 for row in rows) else "failed"
    else:
        status = "success"
    labels = {"QUEUED": "等待中", "RUNNING": "采集中", "COMPLETE": "完整", "PARTIAL": "部分成功", "BLOCKED": "访问受限",
              "SKIPPED": "未请求", "PARSE_ERROR": "解析失败", "FAILED": "采集失败", "INTERRUPTED": "异常中断"}
    details = []
    for row in rows:
        retry = f"；下次补抓 {row[8]}" if row[8] else ""
        run_reason = public_error_message(str(row[3] or "")) if row[1] == "BLOCKED" else str(row[3] or "")
        run_label = "自动化访问受限" if row[1] == "BLOCKED" and is_automated_access_restriction(str(row[3] or "")) else labels.get(row[1], row[1])
        details.append(f"{row[5]}/{row[6]}: {run_reason} [{run_label}，{row[2]} 条，补抓轮次 {row[7]}/2{retry}]")
    completed = sum(1 for row in rows if str(row[1]) == "COMPLETE")
    message = f"完整 {completed}/{len(rows)} 个数据源，最近结果共采集 {sum(int(row[2]) for row in rows)} 条。"
    message += "\n" + "\n".join(details)
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


CATEGORY_NAMES = {"baby-products": "母婴用品", "fashion": "服饰", "beauty": "美容个护",
                  "home-garden": "家居生活", "handmade": "手工制品", "kitchen": "厨房用品",
                  "lawn-garden": "庭院园艺", "pet-supplies": "宠物用品", "sporting-goods": "运动户外"}
CARD_STATES = {"WAITING": ("等待中", "idle"), "QUEUED": ("等待中", "queued"),
               "RUNNING": ("采集中", "running"), "COMPLETE": ("已完成", "success"),
               "PARTIAL": ("部分完成", "partial"), "BLOCKED": ("访问受限", "failed"),
               "SKIPPED": ("未采集", "idle"), "PARSE_ERROR": ("解析失败", "failed"),
               "FAILED": ("采集失败", "failed"), "INTERRUPTED": ("已中断", "partial"),
               "DISABLED": ("已停用", "idle")}


def today_source_cards(config, now=None):
    now = now or now_shanghai()
    today = now.date().isoformat()
    with closing(connect_database()) as connection:
        rows = latest_runs(connection, today)
        saved = {r[0]: int(r[1]) for r in connection.execute(
            "SELECT source_url,COUNT(*) FROM observations WHERE snapshot_date=? GROUP BY source_url", (today,))}
    target = int(config.get("collection_policy", {}).get("max_items_per_source", 100))
    cards = []
    for source in config["sources"]:
        row = rows.get(source["url"], {})
        status = row.get("status", "WAITING") if source["enabled"] else "DISABLED"
        label, css = CARD_STATES[status]
        reason = row.get("error_message") or ""
        if status == "BLOCKED" and is_automated_access_restriction(reason):
            label = "自动化访问受限"
            reason = public_error_message(reason)
        if status == "QUEUED": reason = "已加入队列，轮到此类目时开始采集。"
        elif status == "WAITING": reason = f"今日尚未采集，计划 {config['daily_schedule']} 开始。"
        elif status == "DISABLED": reason = "已停用自动采集，启用并保存后可采集。"
        card_target = min(target, row["item_count"]) if status == "COMPLETE" and row.get("item_count", 0) > 0 else target
        cards.append(dict(source, source_id=source_id(source), name=CATEGORY_NAMES.get(source["category"],source["category"]),
                          today=today, today_status=status, status_label=label, status_class=css,
                          saved_count=saved.get(source["url"],0), target=card_target,
                          percent=100 if status == "COMPLETE" else min(100,round(saved.get(source["url"],0)/max(card_target,1)*100)),
                          last_count=row.get("item_count",0), reason=reason,
                          updated_at=row.get("finished_at") or row.get("started_at") or "",
                          next_retry_at=row.get("next_retry_at") or "",
                          can_run=source["enabled"] and status not in {"QUEUED","RUNNING"},
                          button_label="重新采集" if row else "立即采集"))
    return cards


def card_summary(cards):
    enabled = [s for s in cards if s['enabled']]
    complete = sum(s['today_status']=='COMPLETE' for s in enabled)
    queued = sum(s['today_status']=='QUEUED' for s in enabled)
    running = sum(s['today_status']=='RUNNING' for s in enabled)
    return f"今天已完成 {complete}/{len(enabled)} 个类目 · 采集中 {running} · 已排队 {queued}"


@app.get("/status")
def live_status():
    config = load_config(CONFIG_PATH)
    cards = today_source_cards(config)
    return {"sources": cards, "summary": card_summary(cards), "state": scheduler_state(),
            "stats": database_stats(), "store_vat": load_store_overview(AMAZON_IT_DB_PATH)}


@app.post("/source/run")
async def run_source(request: Request):
    form = await request.form()
    target_id = str(form.get("source_id", "")).strip()
    try:
        if not target_id:
            raise ValueError("请选择一个类目")
        start_manual_run(target_id)
    except ValueError as exc:
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"error": str(exc)}, status_code=400)
        return templates.TemplateResponse(request,"index.html",page_context(request,error=str(exc)),status_code=400)
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"accepted": True, "source_id": target_id}, status_code=202)
    return RedirectResponse("/?run_started=true", status_code=303)


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
    sources = today_source_cards(config)
    enabled_count = sum(1 for source in sources if source["enabled"])
    return {
        "request": request,
        "daily_schedule": config["daily_schedule"],
        "schedule_timezone": "Asia/Shanghai",
        "retention_days": config["collection_policy"]["retention_days"],
        "sources": sources,
        "enabled_count": enabled_count,
        "today_label": now_shanghai().date().isoformat(),
        "card_summary": card_summary(sources),
        "marketplace_count": len({source["marketplace"] for source in sources}),
        "state": scheduler_state(),
        "stats": database_stats(),
        "store_vat": load_store_overview(AMAZON_IT_DB_PATH),
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


@app.get("/today", response_class=HTMLResponse)
def today(
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


@app.get("/seller-vat", response_class=HTMLResponse)
def seller_vat_page(
    request: Request, run_id: str = "", tab: str = "sellers",
    q: str = "", status: str = "", page: int = 1,
    site: str = "", similarity: int = 0,
):
    store_view = load_store_matrix(
        AMAZON_IT_DB_PATH, query=q, site=site, similarity=similarity, page=page,
    )
    if store_view is not None:
        return templates.TemplateResponse(request, "seller_vat.html", {
            "request": request, "store_view": store_view,
        })
    data = load_seller_vat_page(
        SELLER_VAT_DB_PATH, run_id=run_id, tab=tab,
        query=q, status=status, page=page,
    )
    return templates.TemplateResponse(request, "seller_vat.html", {"request": request, "store_view": None, "scheduler": load_scheduler_status(SELLER_VAT_STATUS_PATH), "default_account": seller_vat_login.default_account(), **data})


def _secure_vat_action(request: Request):
    if request.url.scheme != "https" and request.headers.get("host", "").split(":")[0] not in {"127.0.0.1", "localhost"}:
        return JSONResponse({"error": "请通过 HTTPS 访问登录和采集操作"}, status_code=403)
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JSONResponse({"error": "无效的操作请求"}, status_code=403)
    return None


@app.post("/seller-vat/login/start")
def seller_vat_login_start(request: Request):
    if rejection := _secure_vat_action(request):
        return rejection
    started, message = seller_vat_login.start_from_config()
    return JSONResponse({"started": started, "message": message}, status_code=202 if started else 409)


@app.get("/seller-vat/login/status")
def seller_vat_login_status():
    return {"login": seller_vat_login.status(), "scheduler": load_scheduler_status(SELLER_VAT_STATUS_PATH)}


@app.post("/seller-vat/run")
def seller_vat_run(request: Request):
    if rejection := _secure_vat_action(request):
        return rejection
    if not profile_ready():
        return JSONResponse({"error": "请先登录卖家精灵"}, status_code=409)
    if collection_lock_held(seller_vat_login.LOCK):
        return JSONResponse({"error": "浏览器正在登录或采集，请稍后重试"}, status_code=409)
    save_json(TRIGGER, {"requested_at": datetime.now(SCHEDULE_TIMEZONE).isoformat(timespec="seconds")})
    return JSONResponse({"message": "已提交采集请求，后台即将启动"}, status_code=202)


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
    return RedirectResponse("/today?deleted=true", status_code=303)


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
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"error": str(exc)}, status_code=400)
        return templates.TemplateResponse(
            request,
            "index.html",
            page_context(request, error=str(exc)),
            status_code=400,
        )
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"saved": True})
    return RedirectResponse("/?saved=true", status_code=303)
