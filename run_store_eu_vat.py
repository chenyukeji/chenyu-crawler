"""Check public European Amazon seller profiles and export one row per store."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from crawler.seller_vat import ROOT, TZ, parse_seller
from check_vies_vats import run as run_vies
from merge_store_vat_database import merge

SITES = ("it", "fr", "de", "pl", "es")
SITE_LABELS = {"it": "意大利", "fr": "法国", "de": "德国", "pl": "波兰", "es": "西班牙"}
STATUS_LABELS = {"local_vat": "已公开本站税号", "other_vat": "仅其他国税号",
                 "no_public_vat": "未见公开税号", "failed": "访问失败", "pending": "待核查"}
OUTPUT = ROOT / "outputs" / "seller-vat" / f"eu-store-{datetime.now(TZ).date().isoformat()}"
STORE_DB = ROOT / "data" / "amazon_it.sqlite3"
COLUMNS = ["卖家ID", "店铺名称", "公司名称", "公司地址", "IT税号", "FR税号", "DE税号", "PL税号", "ES税号",
           "其他税号", "意大利店铺页", "法国店铺页", "德国店铺页", "波兰店铺页", "西班牙店铺页", "核查状态", "核查时间"]


def safe(value: str) -> str:
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def load_candidates() -> dict:
    path = OUTPUT / "店铺候选.json"
    try:
        return {item["seller_id"]: item for item in json.loads(path.read_text(encoding="utf-8"))["stores"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def load_records(path: Path) -> dict:
    records = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                records[(item["seller_id"], item["site"])] = item
            except (ValueError, KeyError, TypeError):
                continue
    return records


def export(sellers: list[str], candidates: dict, records: dict) -> Path:
    rows = []
    for seller_id in sellers:
        checks = [records.get((seller_id, site), {}) for site in SITES]
        if not any(checks):
            continue
        candidate = candidates.get(seller_id, {})
        row = {"卖家ID": seller_id}
        row["店铺名称"] = candidate.get("seller_name", "")
        row["公司名称"] = candidate.get("company_snapshot", "")
        row["公司地址"] = ""
        vats = {site.upper(): set() for site in SITES}
        others = set()
        for site, check in zip(SITES, checks):
            row[f"{SITE_LABELS[site]}店铺页"] = f"https://www.amazon.{site}/sp?seller={seller_id}"
            title = check.get("title", "")
            if title and not row["店铺名称"]:
                row["店铺名称"] = re.split(r"[:：]", title, maxsplit=1)[-1].strip()
            if check.get("company"):
                row["公司名称"] = check["company"]
            if check.get("address"):
                row["公司地址"] = check["address"]
            for vat in check.get("vats", []):
                number = vat.get("number", "")
                country = vat.get("country", "")
                if not number:
                    continue
                if country in vats:
                    vats[country].add(number)
                else:
                    others.add(number)
        for country, values in vats.items():
            row[f"{country}税号"] = "; ".join(sorted(values))
        row["其他税号"] = "; ".join(sorted(others))
        row["核查状态"] = "; ".join(
            f"{site}:{STATUS_LABELS.get(check.get('status', 'pending'), '待核查')}"
            for site, check in zip(SITES, checks)
        )
        row["核查时间"] = max((check.get("checked_at", "") for check in checks), default="")
        rows.append(row)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    target = OUTPUT / "欧洲店铺税号.csv"
    temporary = target.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: safe(str(row.get(column, ""))) for column in COLUMNS})
    temporary.replace(target)
    return target


def save_results(sellers: list[str], candidates: dict, records: dict) -> Path:
    target = export(sellers, candidates, records)
    merged = merge(STORE_DB, OUTPUT)
    print(f"database={STORE_DB} sellers={merged['sellers_total']} vat_evidence={merged['vat_evidence']}", flush=True)
    return target


async def main_async(limit_new: int, concurrency: int, skip_sites: set[str], check_vies: bool = True) -> None:
    from playwright.async_api import async_playwright
    OUTPUT.mkdir(parents=True, exist_ok=True)
    evidence_path = OUTPUT / "店铺公开信息_逐站证据.jsonl"
    records = load_records(evidence_path)
    candidates = load_candidates()
    with closing(sqlite3.connect(STORE_DB.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        sellers = sorted({row[0] for row in db.execute("SELECT seller_id FROM seller_discoveries")} | set(candidates))
    pending = [(seller_id, site) for seller_id in sellers for site in SITES
               if site not in skip_sites and
               ((seller_id, site) not in records or records[(seller_id, site)].get("status") == "failed")]
    if limit_new:
        pending = pending[:limit_new * len(SITES)]
    print(f"stores={len(sellers)} pending_site_checks={len(pending)}", flush=True)
    if not pending:
        print(save_results(sellers, candidates, records), flush=True)
        if check_vies:
            print(f"VIES: {run_vies(STORE_DB)}", flush=True)
        return
    semaphore = asyncio.Semaphore(concurrency)
    write_lock = asyncio.Lock()
    completed = 0
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 1000})
        try:
            with evidence_path.open("a", encoding="utf-8") as evidence:
                async def check(seller_id: str, site: str) -> None:
                    nonlocal completed
                    url = f"https://www.amazon.{site}/sp?seller={seller_id}"
                    item = {"seller_id": seller_id, "site": site, "url": url,
                            "checked_at": datetime.now(TZ).isoformat(timespec="seconds")}
                    async with semaphore:
                        page = await context.new_page()
                        try:
                            response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                            if not response or response.status != 200:
                                raise RuntimeError(f"HTTP {response.status if response else 'unknown'}")
                            await page.wait_for_timeout(500)
                            body = await page.locator("body").inner_text(timeout=10000)
                            if any(marker in body.casefold() for marker in (
                                "enter the characters you see below", "robot check", "inserisci i caratteri",
                                "captcha", "unusual traffic", "verifica che tu sia umano",
                            )):
                                raise RuntimeError("访问验证")
                            details = parse_seller(await page.content())
                            if not details["company"] and not details["address"] and not details["vats"]:
                                raise RuntimeError("没有识别到公开商业信息")
                            status = ("local_vat" if any(v["country"] == site.upper() for v in details["vats"])
                                      else "other_vat" if details["vats"] else "no_public_vat")
                            item.update(status=status, company=details["company"], address=details["address"],
                                        vats=details["vats"], title=await page.title())
                        except Exception as error:
                            item.update(status="failed", error=f"{type(error).__name__}: {error}"[:180])
                        finally:
                            await page.close()
                        await asyncio.sleep(0.7)
                    async with write_lock:
                        records[(seller_id, site)] = item
                        evidence.write(json.dumps(item, ensure_ascii=False) + "\n")
                        evidence.flush()
                        completed += 1
                        if completed % 10 == 0 or completed == len(pending):
                            save_results(sellers, candidates, records)
                            print(f"site_checks={completed}/{len(pending)} stores_with_results={len({s for s, _ in records})}", flush=True)
                await asyncio.gather(*(check(seller_id, site) for seller_id, site in pending))
        finally:
            await context.close()
            await browser.close()
    print(save_results(sellers, candidates, records), flush=True)
    if check_vies:
        print(f"VIES: {run_vies(STORE_DB)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit-new", type=int, default=0, help="Limit number of pending stores, 0=all")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--skip-sites", default="", help="Comma-separated site codes to defer")
    parser.add_argument("--skip-vies", action="store_true", help="Skip official VAT number checks")
    args = parser.parse_args()
    skip_sites = {site.strip() for site in args.skip_sites.split(",") if site.strip()}
    if args.limit_new < 0 or not 1 <= args.concurrency <= 5 or not skip_sites <= set(SITES):
        parser.error("limit-new must be nonnegative; concurrency must be 1..5; skip-sites must be EU site codes")
    asyncio.run(main_async(args.limit_new, args.concurrency, skip_sites, not args.skip_vies))


if __name__ == "__main__":
    main()
