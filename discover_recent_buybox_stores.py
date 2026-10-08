"""Discover IT BuyBox stores across SellerSprite's capped research results."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from decimal import Decimal
from datetime import datetime
from pathlib import Path

from crawler.seller_vat import (DEFAULT_URL, ROOT, SELLER, TZ, configure_research,
                                price_split, restore_sellersprite_session, save_json)
from merge_store_vat_database import save_discoveries

PROFILE = ROOT / "data" / "sellersprite-profile"
OUTPUT = ROOT / "outputs" / "seller-vat" / f"eu-store-{datetime.now(TZ).date().isoformat()}"
ENDPOINT = "https://www.sellersprite.com/v3/api/product-research"
# The UI stops at page 34 even when the API reports hundreds of pages.
RESULT_CAP = 2000
REQUEST_DELAY = 0.5


def save_candidates(candidates: dict, pages: int, reported_total: int, output: Path = OUTPUT) -> None:
    output.mkdir(parents=True, exist_ok=True)
    save_json(output / "店铺候选.json", {
        "source": "SellerSprite IT, listing date past 30 days, BuyBox seller",
        "pages_checked": pages, "reported_result_total": reported_total,
        "stores": sorted(candidates.values(), key=lambda item: item["seller_id"]),
    })


def add_page_candidates(items: list[dict], candidates: dict, pages: int, db_path: Path) -> None:
    """Commit each newly found store before moving to the next result."""
    for item in items:
        seller_id = str(item.get("sellerId") or "").upper()
        if not SELLER.fullmatch(seller_id) or seller_id in candidates:
            continue
        dto = item.get("sellerDto") or {}
        candidate = {
            "seller_id": seller_id,
            "seller_name": str(item.get("sellerName") or dto.get("shortName") or ""),
            "company_snapshot": str(dto.get("businessName") or ""),
            "first_page": pages,
        }
        save_discoveries(db_path, [candidate])
        candidates[seller_id] = candidate


def initial_checkpoint() -> dict:
    return {"pages_checked": 0, "reported_result_total": 0,
            "pending": [{"low": None, "high": None, "depth": 0, "page": 1, "fingerprints": []}],
            "complete": False}


def load_progress(output: Path) -> tuple[dict, dict]:
    path = output / "发现进度.json"
    checkpoint = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else initial_checkpoint()
    candidates_path = output / "店铺候选.json"
    saved = json.loads(candidates_path.read_text(encoding="utf-8")) if candidates_path.is_file() else {}
    candidates = {item["seller_id"]: item for item in saved.get("stores", [])}
    return checkpoint, candidates


def payload_for(base: dict, partition: dict) -> dict:
    payload = {key: value for key, value in base.items() if key not in ("minPrice", "maxPrice")}
    payload["page"] = partition["page"]
    if partition["low"] is not None:
        payload["minPrice"] = partition["low"]
    if partition["high"] is not None:
        payload["maxPrice"] = partition["high"]
    return payload


def consume_partition_page(checkpoint: dict, candidates: dict, data: dict,
                           output: Path, db_path: Path, max_pages: int = 0) -> bool:
    """Process one API response and persist a restartable page boundary."""
    part = checkpoint["pending"][0]
    total = int(data.get("total") or 0)
    page = part["page"]
    items = data.get("items") or []
    if page == 1 and part["low"] is None and part["high"] is None:
        checkpoint["reported_result_total"] = total
    if total > RESULT_CAP and page == 1:
        bounds = price_split(
            Decimal(part["low"]) if part["low"] is not None else None,
            Decimal(part["high"]) if part["high"] is not None else None,
        ) if part["depth"] < 24 else None
        if bounds is None:
            raise RuntimeError(f"价格区间 {part['low']}–{part['high']} 有 {total} 条，无法继续拆分")
        children = [{"low": str(low) if low is not None else None,
                     "high": str(high) if high is not None else None,
                     "depth": part["depth"] + 1, "page": 1, "fingerprints": []}
                    for low, high in bounds]
        checkpoint["pending"][:1] = children
        save_json(output / "发现进度.json", checkpoint)
        print(f"split={part['low']}:{part['high']} total={total}", flush=True)
        return True
    if total and not items:
        raise RuntimeError(f"价格区间 {part['low']}–{part['high']} 第 {page} 页为空，停止以免漏店铺")
    if not items:
        checkpoint["pending"].pop(0)
        save_json(output / "发现进度.json", checkpoint)
        return True
    fingerprint = hashlib.sha256(json.dumps([item.get("asin") for item in items]).encode()).hexdigest()
    if fingerprint in part["fingerprints"]:
        raise RuntimeError(f"价格区间 {part['low']}–{part['high']} 第 {page} 页重复")
    part["fingerprints"].append(fingerprint)
    checkpoint["pages_checked"] += 1
    pages = checkpoint["pages_checked"]
    add_page_candidates(items, candidates, pages, db_path)
    save_candidates(candidates, pages, checkpoint["reported_result_total"], output)
    if page * int(data.get("size") or len(items)) >= total or len(items) < int(data.get("size") or len(items)):
        checkpoint["pending"].pop(0)
    else:
        part["page"] += 1
    save_json(output / "发现进度.json", checkpoint)
    print(f"page={pages} partition={part['low']}:{part['high']}:{page} "
          f"sellers={len(candidates)} rows={len(items)} total={total}", flush=True)
    return not max_pages or pages < max_pages


def scan(context, base: dict, headers: dict, output: Path, db_path: Path, max_pages: int = 0) -> None:
    checkpoint, candidates = load_progress(output)
    if checkpoint.get("complete"):
        return
    while checkpoint["pending"]:
        part = checkpoint["pending"][0]
        payload = payload_for(base, part)
        for attempt in range(3):
            try:
                response = context.request.post(ENDPOINT, data=json.dumps(payload), headers=headers, timeout=30000)
                result = response.json()
                if response.status != 200 or result.get("code") != "OK" or not isinstance(result.get("data"), dict):
                    raise RuntimeError(f"卖家精灵查询失败：HTTP {response.status}, code={result.get('code')}")
                data = result["data"]
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        if not consume_partition_page(checkpoint, candidates, data, output, db_path, max_pages):
            return
        time.sleep(REQUEST_DELAY)
    checkpoint["complete"] = True
    save_json(output / "发现进度.json", checkpoint)
    print(f"complete pages={checkpoint['pages_checked']} sellers={len(candidates)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        context = runtime.chromium.launch_persistent_context(str(PROFILE), headless=True,
                                                             viewport={"width": 1440, "height": 1000})
        try:
            restore_sellersprite_session(context, PROFILE)
            page = context.pages[0] if context.pages else context.new_page()
            captured = []
            def capture(response):
                if response.url.split("?")[0] == ENDPOINT and response.request.method == "POST":
                    captured.append(response.request)
            page.on("response", capture)
            page.goto(DEFAULT_URL, wait_until="domcontentloaded", timeout=60000)
            configure_research(page)
            if not captured:
                raise RuntimeError("卖家精灵未返回商品研究数据")
            request = captured[-1]
            headers = {key: value for key, value in request.headers.items()
                       if key not in ("cookie", "content-length", "host", "origin", "referer",
                                      "accept-encoding", "connection")}
            scan(context, request.post_data_json, headers, args.output_dir,
                 ROOT / "data" / "amazon_it.sqlite3", args.max_pages)
        finally:
            context.close()


if __name__ == "__main__":
    main()
