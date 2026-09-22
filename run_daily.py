from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from crawler.amazon import AccessControlBlocked, BrowserSettings, collect_source, write_json
from crawler.categories import load_sources, source_id
from database.repository import TrendStore


ROOT = Path(__file__).resolve().parent


def codex_notification(summary: dict[str, Any]) -> str:
    if summary["status"] in {"COMPLETE", "BASELINE_COMPLETE"}:
        return (
            "CODEX_NOTIFY: Amazon 新品榜采集成功；"
            f"{summary['sources_succeeded']}/{summary['sources_total']} 个数据源，"
            f"{summary['observations_total']} 条唯一记录。汇总：{summary['summary_path']}"
        )
    return (
        "CODEX_NOTIFY: Amazon 新品榜采集未完整完成；"
        f"完整 {summary['sources_succeeded']}，不足目标 {summary['sources_partial']}，"
        f"风控阻断 {summary['sources_blocked']}，"
        f"失败 {summary['sources_failed']}。请查看：{summary['summary_path']}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Amazon New Releases through a visible browser UI")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "sources.json")
    parser.add_argument("--browser-config", type=Path, default=ROOT / "env" / "browser.json")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "new_releases.db")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "daily")
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
                    "snapshot_retention_days": int(policy.get("retention_days", 10)),
                    "identity_retention_days": int(policy.get("identity_retention_days", 90)),
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

    run_dir = args.output_root / args.date
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    lock_path = args.db.parent / "collector.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_handle = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise RuntimeError(f"Another collector run is active: {lock_path}") from exc

    store = TrendStore(
        args.db,
        retention_days=int(policy.get("retention_days", 10)),
        identity_retention_days=int(policy.get("identity_retention_days", 90)),
    )

    results: list[dict[str, Any]] = []
    observations_total = 0
    stopped = False
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")

    try:
        lock_handle.write(started_at)
        lock_handle.close()

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
                raw_path = raw_dir / f"{current_source_id}.json"

                try:
                    snapshot = collect_source(page, source, settings, limit)
                    snapshot["snapshot_date"] = args.date
                    write_json(raw_path, snapshot)

                    changed = store.ingest(
                        snapshot["items"],
                        source_url=source["url"],
                        marketplace=source["marketplace"],
                        category=source["category"],
                        snapshot_date=args.date,
                    )
                    observations_total += len(snapshot["items"])
                    source_status = str(snapshot["status"])
                    results.append(
                        {
                            "source": current_source_id,
                            "status": source_status,
                            "items": len(snapshot["items"]),
                            "target_items": limit,
                            "top_list_complete": snapshot["top_list_complete"],
                            "pages_visited": snapshot["pages_visited"],
                            "ingested_or_updated": changed,
                            "raw_path": str(raw_path),
                        }
                    )
                except AccessControlBlocked as exc:
                    write_json(
                        raw_path,
                        {
                            "url": source["url"],
                            "marketplace": source["marketplace"],
                            "category": source["category"],
                            "snapshot_date": args.date,
                            "status": "blocked",
                            "reason": str(exc),
                            "items": [],
                        },
                    )
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
    finally:
        lock_path.unlink(missing_ok=True)

    processed_ids = {result["source"] for result in results}
    if stopped:
        for source in sources:
            current_source_id = source_id(source)
            if current_source_id not in processed_ids:
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

    successful_ids = {
        result["source"]
        for result in results
        if result["status"] in {"ok", "partial"}
    }
    successful_urls = [
        source["url"]
        for source in sources
        if source_id(source) in successful_ids
    ]
    history_sufficient = bool(successful_urls) and all(
        len(store.snapshot_dates(url)) >= 2 for url in successful_urls
    )

    if succeeded == len(sources):
        status = "COMPLETE" if history_sufficient else "BASELINE_COMPLETE"
    elif blocked:
        status = "BLOCKED"
    else:
        status = "PARTIAL"

    summary_path = run_dir / "run_summary.json"
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
        "history_sufficient": history_sufficient,
        "browser_channel": settings.channel,
        "browser_headless": settings.headless,
        "database": str(args.db),
        "snapshot_retention_days": store.retention_days,
        "identity_retention_days": store.identity_retention_days,
        "results": results,
        "summary_path": str(summary_path),
    }
    write_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(codex_notification(summary))
    return 0 if status in {"COMPLETE", "BASELINE_COMPLETE"} else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"CODEX_NOTIFY: Amazon 新品榜采集启动失败：{exc}", file=sys.stderr)
        raise SystemExit(2)
