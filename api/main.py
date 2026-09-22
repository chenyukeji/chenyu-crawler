from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from api.categories import router as categories_router
from api.categories import list_categories, CategoryCreate, create_category
from api.tasks import router as tasks_router
from api.tasks import list_tasks, TaskCreate, create_task
from api.runs import router as runs_router
from api.runs import list_runs

app = FastAPI(title="chenyu-crawler", version="0.1.0")
app.include_router(categories_router)
app.include_router(tasks_router)
app.include_router(runs_router)
templates = Jinja2Templates(directory="web/templates")


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/categories", response_class=HTMLResponse)
def categories_page(request: Request):
    return templates.TemplateResponse(
        "categories.html",
        {"request": request, "categories": list_categories()},
    )


@app.post("/categories")
def categories_create_page(
    marketplace: str,
    node_id: str,
    name: str,
    level: int,
    parent_id: str = "",
):
    create_category(CategoryCreate(
        marketplace=marketplace,
        node_id=node_id,
        name=name,
        level=level,
        parent_id=parent_id,
    ))
    return RedirectResponse("/categories", status_code=303)


@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request):
    return templates.TemplateResponse(
        "tasks.html",
        {"request": request, "tasks": list_tasks()},
    )


@app.post("/tasks")
def tasks_create_page(name: str, marketplace: str, schedule: str = "", category_id: int | None = None):
    create_task(TaskCreate(
        name=name,
        marketplace=marketplace,
        schedule=schedule,
        category_id=category_id,
    ))
    return RedirectResponse("/tasks", status_code=303)


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    return templates.TemplateResponse(
        "runs.html",
        {"request": request, "runs": list_runs()},
    )


@app.get("/api/status")
def status():
    return {"crawler": "ready", "scheduler": "not_configured"}
