"""Check public European Amazon seller profiles and export one row per store."""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from crawler.seller_vat import ROOT, TZ, parse_seller

SITES = ("it", "fr", "de", "pl", "es")
OUTPUT = ROOT / "outputs" / "seller-vat" / f"eu-store-{datetime.now(TZ).date().isoformat()}"
DB = ROOT / "data" / "seller_vat.sqlite3"


def safe(value: str) -> str:
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def export(rows: list[dict]) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    columns = ["卖家ID", "店铺名称", "公司名称", "公司地址", "IT税号", "FR税号", "DE税号", "PL税号", "ES税号",
               "其他税号", "意大利店铺页", "法国店铺页", "德国店铺页", "波兰店铺页", "西班牙店铺页", "核查状态", "核查时间"]
    target = OUTPUT / "欧洲店铺税号.csv"
    with target.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: safe(str(row.get(column, ""))) for column in columns})
    print(target, flush=True)


def main() -> None:
    with sqlite3.connect(DB) as db:
        sellers = [row[0] for row in db.execute("SELECT seller_id FROM auto_sellers ORDER BY seller_id")]
    if not sellers:
        raise SystemExit("没有可核查的店铺 ID")
    from playwright.sync_api import sync_playwright
    OUTPUT.mkdir(parents=True, exist_ok=True)
    evidence_path = OUTPUT / "店铺公开信息_逐站证据.jsonl"
    results = []
    with sync_playwright() as runtime, evidence_path.open("w", encoding="utf-8") as evidence:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 1000})
        page = context.new_page()
        try:
            for index, seller_id in enumerate(sellers, 1):
                row = {"卖家ID": seller_id}
                statuses = []
                others = set()
                for site, label in zip(SITES, ("意大利", "法国", "德国", "波兰", "西班牙")):
                    url = f"https://www.amazon.{site}/sp?seller={seller_id}"
                    row[f"{label}店铺页"] = url
                    checked_at = datetime.now(TZ).isoformat(timespec="seconds")
                    record = {"seller_id": seller_id, "site": site, "url": url, "checked_at": checked_at}
                    try:
                        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(600)
                        body = page.locator("body").inner_text(timeout=10000)
                        if not response or response.status >= 400:
                            raise RuntimeError(f"HTTP {response.status if response else 'unknown'}")
                        if any(marker in body.casefold() for marker in (
                            "enter the characters you see below", "robot check", "inserisci i caratteri",
                            "captcha", "unusual traffic", "verifica che tu sia umano",
                        )):
                            raise RuntimeError("访问验证")
                        details = parse_seller(page.content())
                        title = page.title()
                        if not row.get("店铺名称"):
                            row["店铺名称"] = re.split(r"[:：]", title, maxsplit=1)[-1].strip() if ":" in title or "：" in title else title
                        if details["company"] and not row.get("公司名称"):
                            row["公司名称"] = details["company"]
                        if details["address"] and not row.get("公司地址"):
                            row["公司地址"] = details["address"]
                        for vat in details["vats"]:
                            country = vat["country"]
                            if country in ("IT", "FR", "DE", "PL", "ES"):
                                field = f"{country}税号"
                                existing = set(filter(None, row.get(field, "").split("; ")))
                                existing.add(vat["number"])
                                row[field] = "; ".join(sorted(existing))
                            else:
                                others.add(vat["number"])
                        status = ("local_vat" if any(v["country"] == site.upper() for v in details["vats"])
                                  else "other_vat" if details["vats"] else "no_public_vat")
                        record.update(status=status, company=details["company"],
                                      address=details["address"], vats=details["vats"], title=title)
                    except Exception as error:
                        record.update(status="failed", error=f"{type(error).__name__}: {error}"[:180])
                    statuses.append(f"{site}:{record['status']}")
                    evidence.write(json.dumps(record, ensure_ascii=False) + "\n")
                    evidence.flush()
                    time.sleep(1.5)
                row["其他税号"] = "; ".join(sorted(others))
                row["核查状态"] = "; ".join(statuses)
                row["核查时间"] = datetime.now(TZ).isoformat(timespec="seconds")
                results.append(row)
                export(results)
                print(f"{index}/{len(sellers)} {seller_id} {statuses}", flush=True)
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
