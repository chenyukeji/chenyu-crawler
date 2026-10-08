"""Discover unique IT BuyBox stores in SellerSprite's recent listing results."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

from crawler.seller_vat import DEFAULT_URL, ROOT, SELLER, TZ, configure_research, restore_sellersprite_session
from merge_store_vat_database import save_discoveries

PROFILE = ROOT / "data" / "sellersprite-profile"
OUTPUT = ROOT / "outputs" / "seller-vat" / f"eu-store-{datetime.now(TZ).date().isoformat()}"
ENDPOINT = "https://www.sellersprite.com/v3/api/product-research"


def save_candidates(candidates: dict, pages: int, reported_total: int, output: Path = OUTPUT,
                    new_candidates: list[dict] | None = None) -> None:
    output.mkdir(parents=True, exist_ok=True)
    path = output / "店铺候选.json"
    payload = {"source": "SellerSprite IT, listing date past 30 days, BuyBox seller", "pages_checked": pages,
               "reported_result_total": reported_total, "stores": sorted(candidates.values(), key=lambda x: x["seller_id"])}
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    if new_candidates:
        save_discoveries(ROOT / "data" / "amazon_it.sqlite3", new_candidates)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        context = runtime.chromium.launch_persistent_context(str(PROFILE), headless=True,
                                                             viewport={"width": 1440, "height": 1000})
        try:
            restore_sellersprite_session(context, PROFILE)
            page = context.pages[0] if context.pages else context.new_page()
            research = []
            def capture(response):
                if response.url.split("?")[0] == ENDPOINT:
                    try:
                        research.append(response.json()["data"])
                    except (KeyError, ValueError, TypeError):
                        pass
            page.on("response", capture)
            page.goto(DEFAULT_URL, wait_until="domcontentloaded", timeout=60000)
            configure_research(page)
            if not research:
                raise RuntimeError("卖家精灵未返回商品研究数据")
            candidates = {}
            fingerprints = set()
            pages = 0
            while research:
                data = research[-1]
                items = data.get("items") or []
                if not items:
                    break
                fingerprint = hashlib.sha256(json.dumps([x.get("asin") for x in items]).encode()).hexdigest()
                if fingerprint in fingerprints:
                    raise RuntimeError("卖家精灵翻页重复，停止以避免误判覆盖范围")
                fingerprints.add(fingerprint)
                pages += 1
                new_candidates = []
                for item in items:
                    seller_id = str(item.get("sellerId") or "").upper()
                    if not SELLER.fullmatch(seller_id):
                        continue
                    dto = item.get("sellerDto") or {}
                    if seller_id in candidates:
                        continue
                    candidate = {
                        "seller_id": seller_id, "seller_name": str(item.get("sellerName") or dto.get("shortName") or ""),
                        "company_snapshot": str(dto.get("businessName") or ""),
                        "first_page": pages,
                    }
                    candidates[seller_id] = candidate
                    new_candidates.append(candidate)
                save_candidates(candidates, pages, int(data.get("total") or 0), args.output_dir, new_candidates)
                print(f"page={pages} sellers={len(candidates)} rows={len(items)} reported_total={data.get('total')}", flush=True)
                next_button = page.get_by_text("下一页", exact=True).first
                if (args.max_pages and pages >= args.max_pages) or not next_button.count() or next_button.is_disabled():
                    break
                if next_button.get_attribute("aria-disabled") == "true" or "disabled" in (next_button.get_attribute("class") or ""):
                    break
                old_count = len(research)
                with page.expect_response(lambda response: response.url.split("?")[0] == ENDPOINT, timeout=30000):
                    next_button.click()
                deadline = time.monotonic() + 5
                while len(research) == old_count and time.monotonic() < deadline:
                    page.wait_for_timeout(100)
                if len(research) == old_count:
                    raise RuntimeError("翻页响应未返回数据")
                page.wait_for_timeout(800)
        finally:
            context.close()


if __name__ == "__main__":
    main()
