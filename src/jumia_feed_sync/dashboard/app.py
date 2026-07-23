"""FastAPI dashboard: Run screen + Review Grid. See Readme.md #4, #10.

Every request opens its own short-lived SQLite connection (WAL mode,
db.py) rather than sharing one across requests/background tasks -- the
same pattern the CLI already uses, and it sidesteps sqlite3's
same-thread restrictions without needing a connection pool for a
single-operator local tool.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from jumia_feed_sync import config, db, pipeline

app = FastAPI(title="Jumia Feed Sync")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

FILTERS = [
    ("all", "All"),
    ("blocked", "Blocked"),
    ("warned", "Warnings"),
    ("passed", "Passed"),
    ("unresolved_category", "Unresolved category"),
    ("missing_image", "Missing image"),
]


def _connect() -> sqlite3.Connection:
    return db.get_connection(config.DB_PATH)


def _latest_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        """
        SELECT id, started_at, finished_at, status, error_message, feed_item_count, passed, failed
        FROM validation_runs ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    keys = ["id", "started_at", "finished_at", "status", "error_message", "feed_item_count", "passed", "failed"]
    return dict(zip(keys, row))


def _run_in_progress(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM validation_runs WHERE status = 'running' LIMIT 1").fetchone() is not None


def _do_run(run_id: int) -> None:
    conn = _connect()
    try:
        pipeline.run_ingest_and_validate(conn, run_id=run_id)
    except Exception:
        pass  # already recorded on validation_runs by run_ingest_and_validate; nothing else to do
    finally:
        conn.close()


def _fetch_grid_rows(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT rp.sku, rp.title, rp.status, rp.human_override, rp.category_resolved, rp.brand_resolved,
               p.image_link, ic.status_code, ic.width, ic.height, ic.format
        FROM run_products rp
        JOIN products p ON p.sku = rp.sku
        LEFT JOIN image_cache ic ON ic.url = p.image_link
        WHERE rp.run_id = ?
        ORDER BY rp.sku
        """,
        (run_id,),
    ).fetchall()
    issue_counts = dict(
        conn.execute("SELECT sku, COUNT(*) FROM row_issues WHERE run_id = ? GROUP BY sku", (run_id,)).fetchall()
    )
    keys = [
        "sku", "title", "status", "human_override", "category_resolved", "brand_resolved",
        "image_link", "image_status_code", "width", "height", "format",
    ]
    result = []
    for row in rows:
        item = dict(zip(keys, row))
        item["issue_count"] = issue_counts.get(item["sku"], 0)
        result.append(item)
    return result


def _apply_filter(rows: list[dict], active_filter: str) -> list[dict]:
    if active_filter == "unresolved_category":
        return [r for r in rows if not r["category_resolved"]]
    if active_filter == "missing_image":
        return [r for r in rows if not r["image_link"]]
    if active_filter in ("blocked", "warned", "passed"):
        return [r for r in rows if r["status"] == active_filter]
    return rows


def _grid_counts(rows: list[dict]) -> dict[str, int]:
    return {
        "all": len(rows),
        "blocked": sum(1 for r in rows if r["status"] == "blocked"),
        "warned": sum(1 for r in rows if r["status"] == "warned"),
        "passed": sum(1 for r in rows if r["status"] == "passed"),
        "unresolved_category": sum(1 for r in rows if not r["category_resolved"]),
        "missing_image": sum(1 for r in rows if not r["image_link"]),
    }


def _resolve_run_id(conn: sqlite3.Connection, run_id_param: str) -> int | None:
    if run_id_param == "latest":
        row = conn.execute(
            "SELECT id FROM validation_runs WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    return int(run_id_param)


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse("/run")


@app.get("/run")
def run_page(request: Request):
    conn = _connect()
    try:
        run = _latest_run(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "run.html", {"run": run})


@app.get("/run/status")
def run_status(request: Request):
    conn = _connect()
    try:
        run = _latest_run(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "_run_status.html", {"run": run})


@app.post("/run/start")
def run_start(request: Request, background_tasks: BackgroundTasks):
    conn = _connect()
    try:
        if not _run_in_progress(conn):
            run_id = pipeline.start_run(conn)
            background_tasks.add_task(_do_run, run_id)
        run = _latest_run(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "_run_status.html", {"run": run})


@app.get("/review/{run_id}")
def review_page(request: Request, run_id: str, filter: str = "all"):
    conn = _connect()
    try:
        resolved_run_id = _resolve_run_id(conn, run_id)
        if resolved_run_id is None:
            return templates.TemplateResponse(request, "review.html", {"run_id": None, "rows": [], "counts": {}, "filters": FILTERS, "active_filter": filter})
        all_rows = _fetch_grid_rows(conn, resolved_run_id)
        counts = _grid_counts(all_rows)
        rows = _apply_filter(all_rows, filter)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "review.html",
        {"run_id": resolved_run_id, "rows": rows, "counts": counts, "filters": FILTERS, "active_filter": filter},
    )


@app.get("/review/{run_id}/grid")
def review_grid(request: Request, run_id: int, filter: str = "all"):
    conn = _connect()
    try:
        all_rows = _fetch_grid_rows(conn, run_id)
        counts = _grid_counts(all_rows)
        rows = _apply_filter(all_rows, filter)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "_grid.html", {"run_id": run_id, "rows": rows, "counts": counts, "filters": FILTERS, "active_filter": filter},
    )


@app.get("/review/{run_id}/detail/{sku}")
def review_detail(request: Request, run_id: int, sku: str):
    conn = _connect()
    try:
        issues = conn.execute(
            "SELECT severity, rule_id, field, message FROM row_issues WHERE run_id = ? AND sku = ? ORDER BY severity",
            (run_id, sku),
        ).fetchall()
    finally:
        conn.close()
    keys = ["severity", "rule_id", "field", "message"]
    return templates.TemplateResponse(
        request, "_detail.html", {"issues": [dict(zip(keys, i)) for i in issues]},
    )


@app.post("/review/{run_id}/override")
def review_override(request: Request, run_id: int, action: str = Form(...), sku: list[str] = Form(default=[])):
    conn = _connect()
    try:
        if action in ("approved", "excluded") and sku:
            placeholders = ",".join("?" for _ in sku)
            conn.execute(
                f"UPDATE run_products SET human_override = ? WHERE run_id = ? AND sku IN ({placeholders})",
                (action, run_id, *sku),
            )
            conn.commit()
        all_rows = _fetch_grid_rows(conn, run_id)
        counts = _grid_counts(all_rows)
        rows = _apply_filter(all_rows, "all")
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "_grid.html", {"run_id": run_id, "rows": rows, "counts": counts, "filters": FILTERS, "active_filter": "all"},
    )
