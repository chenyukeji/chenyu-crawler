from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from crawler.amazon import AccessControlBlocked, BrowserSettings, collect_source
from crawler.config import load_sources, source_id
from database.repository import SnapshotStore


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
        f"失败 {summary['sources_failed']}。详情见当前运行日志。"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Amazon New Releases through a visible browser UI")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "sources.json")
    parser.add_argument("--browser-config", type=Path, default=ROOT / "env" / "browser.json")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "new_releases.db")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--limit", type=int)
    parser.add_argument("--source-limit", type=int, help="Run only the first N configured sources for diagnostics")
    parser.add_argument("--channel", choices=("msedge", "chrome", "chromium"))
    parser.add_argument("--headless", action="store_true", help="Diagnostic option; normal runs use a visible browser")
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    args = build_parser().parse_args(argv)
    config, sources = load_sources(args.config)

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

    lock_path = args.db.parent / "collector.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_handle = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise RuntimeError(f"Another collector run is active: {lock_path}") from exc

    store = SnapshotStore(
        args.db,
        retention_days=int(policy.get("retention_days", 7)),
    )

    results: list[dict[str, Any]] = []
    observations_total = 0
    stopped = False
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    run_ids: dict[str, str] = {}
    finished_run_ids: set[str] = set()

    try:
        lock_handle.write(started_at)
        lock_handle.close()
        store.prune(date.fromisoformat(args.date))
        for source in sources:
            current_source_id = source_id(source)
            run_ids[current_source_id] = store.start_run(
                source_url=source["url"],
                marketplace=source["marketplace"],
                category=source["category"],
                snapshot_date=args.date,
                started_at=started_at,
            )

        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "headless": settings.headless,
                "slow_mo": settings.slow_mo_ms,
            }
            if settings.channel != "chromium":
                launch_options["channel"] = settings.channel

            browser = playwright.chromium.launch(**launch_options)
            context = browser.new_context(locale="en-US", timezone_id="Asia/Shanghai")
            page = context.new_page()
            page.set_default_timeout(settings.timeout_ms)

            for index, source in enumerate(sources):
                current_source_id = source_id(source)

                try:
                    snapshot = collect_source(page, source, settings, limit)
                    snapshot["snapshot_date"] = args.date

                    changed = store.ingest(
                        snapshot["items"],
                        source_url=source["url"],
                        marketplace=source["marketplace"],
                        category=source["category"],
                        snapshot_date=args.date,
                    )
                    observations_total += len(snapshot["items"])
                    source_status = str(snapshot["status"])
                    run_status = "COMPLETE" if source_status == "ok" else "FAILED"
                    run_error = None
                    if run_status == "FAILED":
                        run_error = (
                            f"只采集到 {len(snapshot['items'])}/{limit} 条，榜单结果不完整"
                        )
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
                            "target_items": limit,
                            "top_list_complete": snapshot["top_list_complete"],
                            "pages_visited": snapshot["pages_visited"],
                            "ingested_or_updated": changed,
                        }
                    )
                except AccessControlBlocked as exc:
                    store.finish_run(
                        run_ids[current_source_id],
                        status="FAILED",
                        item_count=0,
                        error_message=str(exc),
                    )
                    finished_run_ids.add(run_ids[current_source_id])
                    results.append(
                        {
                            "source": current_source_id,
                            "status": "blocked",
                            "reason": str(exc),
                        }
                    )
                    stopped = True
                    break
                except (PlaywrightError, RuntimeError, OSError) as exc:
                    store.finish_run(
                        run_ids[current_source_id],
                        status="FAILED",
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

                if index + 1 < len(sources):
                    time.sleep(settings.between_sources_seconds)

            context.close()
            browser.close()
    except Exception as exc:
        for run_id in run_ids.values():
            if run_id not in finished_run_ids:
                store.finish_run(
                    run_id,
                    status="FAILED",
                    item_count=0,
                    error_message=str(exc),
                )
                finished_run_ids.add(run_id)
        raise
    finally:
        lock_path.unlink(missing_ok=True)

    processed_ids = {result["source"] for result in results}
    if stopped:
        for source in sources:
            current_source_id = source_id(source)
            if current_source_id not in processed_ids:
                run_id = run_ids[current_source_id]
                if run_id not in finished_run_ids:
                    store.finish_run(
                        run_id,
                        status="FAILED",
                        item_count=0,
                        error_message="Run stopped after access control",
                    )
                    finished_run_ids.add(run_id)
                results.append(
                    {
                        "source": current_source_id,
                        "status": "skipped",
                        "reason": "Run stopped after access control",
                    }
                )

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
