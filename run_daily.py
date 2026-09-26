from __future__ import annotations

import argparse
import json
import sys
import signal
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from crawler.amazon import AccessControlBlocked, BrowserSettings, ParseError
from crawler.config import load_sources, source_id
from crawler.retry import collect_with_retry
from database.repository import SnapshotStore
from contextlib import closing
from crawler.lifecycle import CollectorLock, CollectorBusy, recover_interrupted, due_sources, latest_runs, now_shanghai, queued_sources, cancel_unavailable_queue


ROOT = Path(__file__).resolve().parent


def codex_notification(summary: dict[str, Any]) -> str:
    if summary["status"] == "COMPLETE":
        return (
            "CODEX_NOTIFY: Amazon 新品榜采集成功；"
            f"{summary['sources_succeeded']}/{summary['sources_total']} 个数据源，"
            f"{summary['observations_total']} 条唯一记录。数据库：{summary['database']}"
        )
    return (
        "CODEX_NOTIFY: Amazon 新品榜采集未完整完成；"
        f"完整 {summary['sources_succeeded']}，不足目标 {summary['sources_partial']}，"
        f"风控阻断 {summary['sources_blocked']}，"
        f"失败 {summary['sources_failed']}，未请求 {summary['sources_skipped']}。详情见当前运行日志。"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Amazon New Releases through a visible browser UI")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "sources.json")
    parser.add_argument("--browser-config", type=Path, default=ROOT / "env" / "browser.json")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "new_releases.db")
    parser.add_argument("--date", default=now_shanghai().date().isoformat())
    parser.add_argument("--limit", type=int)
    parser.add_argument("--source-limit", type=int, help="Run only the first N configured sources for diagnostics")
    parser.add_argument("--channel", choices=("msedge", "chrome", "chromium"))
    parser.add_argument("--headless", action="store_true", help="Diagnostic option; normal runs use a visible browser")
    parser.add_argument("--queued", action="store_true", help="Run pending per-source requests")
    parser.add_argument("--source-id", action="append", help="Run only the selected enabled source ids")
    parser.add_argument("--retry-due", action="store_true", help="Only collect enabled sources whose delayed retry is due")
    parser.add_argument("--scheduled", action="store_true", help="Skip initial collection if this date already has runs")
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    args = build_parser().parse_args(argv)
    config, sources = load_sources(args.config)

    if args.source_id:
        selected = set(args.source_id)
        if not selected <= {source_id(s) for s in sources}:
            raise ValueError("Unknown or disabled source id")
        sources = [s for s in sources if source_id(s) in selected]

    if args.source_limit is not None:
        if args.source_limit < 1:
            raise ValueError("--source-limit must be at least 1")
        sources = sources[: args.source_limit]

    settings = BrowserSettings.from_file(args.browser_config)
    if args.channel:
        settings.channel = args.channel
    if args.headless:
        settings.headless = True

    policy = config.get("collection_policy", {})
    limit = args.limit or int(
        policy.get("max_items_per_source", policy.get("max_visible_items_per_source", 100))
    )
    if limit < 1:
        raise ValueError("--limit must be at least 1")

    if args.validate_only:
        print(
            json.dumps(
                {
                    "status": "VALID",
                    "sources": len(sources),
                    "browser": settings.channel,
                    "database": str(args.db),
                    "snapshot_retention_days": int(policy.get("retention_days", 7)),
                },
                ensure_ascii=False,
            )
        )
        return 0

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is missing. Run: .\\env\\setup.ps1") from exc

    with CollectorLock(args.db.parent / "collector.lock"):
        store = SnapshotStore(args.db, retention_days=int(policy.get("retention_days", 7)))
        recover_interrupted(store)
        cancel_unavailable_queue(store, config["sources"], now_shanghai())
        with closing(store.connect()) as connection:
            previous = latest_runs(connection, args.date)
            if args.queued:
                sources = queued_sources(connection, sources, now_shanghai())
            elif args.retry_due:
                if args.date != now_shanghai().date().isoformat():
                    raise ValueError("Delayed retries are only allowed for today")
                sources = due_sources(connection, sources, now_shanghai())
            elif args.scheduled:
                sources = [s for s in sources if s["url"] not in previous]
        if not sources:
            print("No delayed retries are due", flush=True)
            return 0
        old_term = signal.getsignal(signal.SIGTERM)
        def terminate(signum, frame):
            raise KeyboardInterrupt("Collector terminated")
        signal.signal(signal.SIGTERM, terminate)
        try:
            return execute_collection(args, sources, settings, limit, store, previous, PlaywrightError, sync_playwright)
        finally:
            signal.signal(signal.SIGTERM, old_term)


def execute_collection(args, sources, settings, limit, store, previous, PlaywrightError, sync_playwright):
    results: list[dict[str, Any]] = []
    observations_total = 0
    evidence_root = ROOT / "data" / "diagnostics" / datetime.now().strftime("%Y%m%d-%H%M%S")
    started_at = now_shanghai().isoformat(timespec="microseconds")
    run_ids: dict[str, str] = {}
    finished_run_ids: set[str] = set()
    browsers = []

    try:
        store.prune(date.fromisoformat(args.date))
        for source in sources:
            current_source_id = source_id(source)
            run_ids[current_source_id] = store.enqueue_run(
                source_url=source["url"],
                marketplace=source["marketplace"],
                category=source["category"],
                snapshot_date=args.date,
                started_at=started_at,
                retry_round=(previous.get(source["url"], {}).get("retry_round", 0) + (1 if args.retry_due else 0)),
            )

        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "headless": settings.headless,
                "slow_mo": settings.slow_mo_ms,
            }
            if settings.channel != "chromium":
                launch_options["channel"] = settings.channel

            for index, source in enumerate(sources):
                current_source_id = source_id(source)
                if not store.mark_running(run_ids[current_source_id]):
                    continue

                source_browsers = []

                def open_next_page():
                    # A fresh process also isolates the network/session state
                    # after a fully loaded rank page; a new tab alone did not.
                    new_browser = playwright.chromium.launch(**launch_options)
                    browsers.append(new_browser)
                    source_browsers.append(new_browser)
                    new_context = new_browser.new_context(locale="en-US", timezone_id="Asia/Shanghai")
                    new_page = new_context.new_page()
                    new_page.set_default_timeout(settings.timeout_ms)
                    return new_page

                page = open_next_page()
                try:
                    snapshot = collect_with_retry(
                        page, source, settings, limit, evidence_root / current_source_id,
                        open_next_page=open_next_page,
                    )
                    snapshot["snapshot_date"] = args.date

                    changed = store.ingest(
                        snapshot["items"],
                        source_url=source["url"],
                        marketplace=source["marketplace"],
                        category=source["category"],
                        snapshot_date=args.date,
                        preserve_more_complete=True,
                    )
                    observations_total += len(snapshot["items"])
                    source_status = str(snapshot["status"])
                    run_status = "COMPLETE" if source_status == "ok" else "PARTIAL"
                    run_error = None
                    if run_status == "PARTIAL":
                        run_error = snapshot["error_message"]
                    store.finish_run(
                        run_ids[current_source_id],
                        status=run_status,
                        item_count=len(snapshot["items"]),
                        error_message=run_error,
                    )
                    finished_run_ids.add(run_ids[current_source_id])
                    results.append(
                        {
                            "source": current_source_id,
                            "status": source_status,
                            "items": len(snapshot["items"]),
                            "target_items": snapshot.get("target_items", limit),
                            "top_list_complete": snapshot["top_list_complete"],
                            "pages_visited": snapshot["pages_visited"],
                            "attempts": snapshot["attempts"],
                            "page_diagnostics": snapshot["page_diagnostics"],
                            "ingested_or_updated": changed,
                        }
                    )
                except AccessControlBlocked as exc:
                    retained = (exc.partial_snapshot or {}).get("items", [])
                    if retained:
                        store.ingest(
                            retained, source_url=source["url"], marketplace=source["marketplace"],
                            category=source["category"], snapshot_date=args.date,
                            preserve_more_complete=True,
                        )
                        observations_total += len(retained)
                    store.finish_run(
                        run_ids[current_source_id],
                        status="BLOCKED",
                        item_count=len(retained),
                        error_message=str(exc),
                    )
                    finished_run_ids.add(run_ids[current_source_id])
                    results.append(
                        {
                            "source": current_source_id,
                            "status": "blocked",
                            "items": len(retained),
                            "reason": str(exc),
                        }
                    )
                except (PlaywrightError, RuntimeError, OSError) as exc:
                    store.finish_run(
                        run_ids[current_source_id],
                        status="PARSE_ERROR" if isinstance(exc, ParseError) else "FAILED",
                        item_count=0,
                        error_message=str(exc),
                    )
                    finished_run_ids.add(run_ids[current_source_id])
                    results.append(
                        {
                            "source": current_source_id,
                            "status": "failed",
                            "reason": str(exc),
                        }
                    )

                for source_browser in source_browsers:
                    source_browser.close()

                if index + 1 < len(sources):
                    time.sleep(settings.between_sources_seconds)

    except BaseException as exc:
        for open_browser in browsers:
            try:
                open_browser.close()
            except Exception:
                pass
        for run_id in run_ids.values():
            with closing(store.connect()) as c:
                active = c.execute("SELECT status FROM collection_runs WHERE run_id=?", (run_id,)).fetchone()
            if run_id not in finished_run_ids and active and active[0] == "RUNNING":
                store.finish_run(
                    run_id,
                    status="INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "FAILED",
                    item_count=0,
                    error_message=f"{type(exc).__name__}: {exc}",
                )
                finished_run_ids.add(run_id)
        raise

    status_counts = Counter(result["status"] for result in results)
    succeeded = status_counts["ok"]
    partial = status_counts["partial"]
    blocked = status_counts["blocked"]
    failed = status_counts["failed"]

    if succeeded == len(sources):
        status = "COMPLETE"
    elif blocked:
        status = "BLOCKED"
    else:
        status = "PARTIAL"

    summary = {
        "snapshot_date": args.date,
        "started_at": started_at,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": status,
        "sources_total": len(sources),
        "sources_succeeded": succeeded,
        "sources_partial": partial,
        "sources_blocked": blocked,
        "sources_failed": failed,
        "sources_skipped": status_counts["skipped"],
        "observations_total": observations_total,
        "browser_channel": settings.channel,
        "browser_headless": settings.headless,
        "database": str(args.db),
        "snapshot_retention_days": store.retention_days,
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(codex_notification(summary))
    return 0 if status == "COMPLETE" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"CODEX_NOTIFY: Amazon 新品榜采集启动失败：{exc}", file=sys.stderr)
        raise SystemExit(2)
