"""SellerSprite IT recent listings -> unique BuyBox seller URLs -> public VATs."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urljoin
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))
ASIN = re.compile(r"\bB[A-Z0-9]{9}\b")
ASIN_VALUE = re.compile(r'^[A-Z0-9]{10}$')
SELLER = re.compile(r"^[A-Z0-9]{8,20}$")
DEFAULT_URL = "https://cn.sellersprite.com/v3/"
LABELS = {
    "company": re.compile(r"^(?:Ragione sociale|Nome (?:dell.?azienda|azienda|impresa)|Business name|Legal business name|Nombre de empresa|Nom commercial|Nom de l'entreprise|Unternehmensname)\s*[:：]", re.I),
    "vat": re.compile(r"^(?:Partita IVA|Numero (?:di partita )?IVA|Numero di identificazione IVA|VAT(?: registration)? (?:number|ID)|Tax (?:number|ID)|Número de IVA|Num[eé]ro (?:de TVA|TVA)|Umsatzsteuer[^:]*|USt[^:]*)\s*[:：]", re.I),
    "address": re.compile(r"^(?:Indirizzo (?:aziendale|dell.?azienda|commerciale)|Business address|Dirección empresarial|Adresse professionnelle|Geschäftsadresse)\s*[:：]", re.I),
}


class StopCollection(RuntimeError):
    pass


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def seller_id_from_url(url):
    parsed = urlparse(url)
    if parsed.hostname not in {"amazon.it", "www.amazon.it"}:
        return ""
    query = parse_qs(parsed.query)
    value = (query.get("seller") or query.get("me") or [""])[0].upper()
    return value if SELLER.fullmatch(value) else ""


def seller_url(seller_id):
    if not SELLER.fullmatch(seller_id):
        raise ValueError("Invalid seller ID")
    return f"https://www.amazon.it/sp?seller={seller_id}"


def parse_seller(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    result = {"company": "", "address": "", "vats": []}
    # Amazon publishes labelled rows. Do not search the entire page for arbitrary
    # numbers: company registration numbers and phone numbers are not VATs.
    for node in soup.select(".a-text-bold, dt, th, strong, b"):
        label = node.get_text(" ", strip=True)
        kind = next((k for k, pattern in LABELS.items() if pattern.match(label)), None)
        if not kind:
            continue
        row = node.find_parent(class_="a-row") or node.parent
        text = row.get_text(" ", strip=True)
        value = LABELS[kind].sub("", text, count=1).strip()
        if not value or value == text:
            continue
        if kind == "vat":
            # Keep country prefix if published; a bare 11-digit number on an
            # Italian marketplace does NOT prove its issuing country.
            tokens = re.findall(r"(?<![A-Z0-9])(?:[A-Z]{2}\s*)?[A-Z0-9][A-Z0-9 .-]{5,24}[A-Z0-9](?![A-Z0-9])", value.upper())
            for token in tokens:
                number = re.sub(r"[ .-]", "", token)
                if re.search(r"\d", number) and 7 <= len(number) <= 20:
                    country = number[:2] if number[:2].isalpha() else ""
                    item = {"number": number, "country": country, "raw": value}
                    if number not in [v["number"] for v in result["vats"]]:
                        result["vats"].append(item)
        elif not result[kind]:
            result[kind] = value
    return result


def check_page(page, amazon=False):
    body = page.locator("body").inner_text(timeout=15000)
    lower = body.lower()
    markers = ("enter the characters you see below", "robot check", "inserisci i caratteri", "unauthorized ai agent", "unusual traffic", "输入验证码", "滑块验证", "安全验证", "验证您是真人")
    if any(m in lower for m in markers) or page.locator("input#captchacharacters").count():
        raise StopCollection("遇到验证码或访问验证，已保存进度。请人工处理后续跑。")
    if not amazon and (re.search(r"未登录\s*(游客)?", body) or "/user/login" in page.url):
        raise StopCollection("卖家精灵尚未登录。先运行 run_seller_vat.py login，登录后再运行采集。")
    if amazon and any(m in lower for m in ("sorry! something went wrong", "service unavailable", "api-services-support@amazon")):
        raise StopCollection("Amazon 页面暂时不可用，已保存进度。")
    return body


def navigate(page, url):
    response = page.goto(url, wait_until='domcontentloaded', timeout=60000)
    if response and response.status in (401, 403, 429, 503):
        raise StopCollection(f'HTTP {response.status}，停止采集并保存进度；请检查登录、限流或网站服务。')
    return response


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS auto_products (
          run_id TEXT, asin TEXT, seller_id TEXT, seller_name TEXT, available_date TEXT,
          product_url TEXT, source_ref TEXT, raw_json TEXT, PRIMARY KEY(run_id,asin,seller_id));
        CREATE TABLE IF NOT EXISTS auto_sellers (
          seller_id TEXT PRIMARY KEY, source_url TEXT, company_name TEXT, business_address TEXT,
          status TEXT, error TEXT, checked_at TEXT);
        CREATE TABLE IF NOT EXISTS auto_vats (
          seller_id TEXT, vat_number TEXT, country TEXT, raw_value TEXT,
          PRIMARY KEY(seller_id,vat_number));
        """)

    def products(self, run_id, items, source):
        with self.db:
            for item in items:
                asin = str(item.get("asin") or "").upper()
                if not ASIN_VALUE.fullmatch(asin):
                    continue
                sid = str(item.get("sellerId") or "").upper()
                if not SELLER.fullmatch(sid):
                    sid = ""
                self.db.execute("INSERT OR REPLACE INTO auto_products VALUES (?,?,?,?,?,?,?,?)", (
                    run_id, asin, sid, str(item.get("sellerName") or ""), str(item.get("availableDate") or ""),
                    f"https://www.amazon.it/dp/{asin}", source, json.dumps(item, ensure_ascii=False)))
                if sid:
                    self.db.execute("INSERT OR IGNORE INTO auto_sellers(seller_id,source_url,status) VALUES (?,?,'pending')", (sid, seller_url(sid)))

    def save_seller(self, sid, result, status, error=""):
        with self.db:
            self.db.execute("UPDATE auto_sellers SET company_name=?,business_address=?,status=?,error=?,checked_at=? WHERE seller_id=?",
                            (result.get("company", ""), result.get("address", ""), status, error, now(), sid))
            self.db.execute("DELETE FROM auto_vats WHERE seller_id=?", (sid,))
            for vat in result.get("vats", []):
                self.db.execute("INSERT OR REPLACE INTO auto_vats VALUES (?,?,?,?)", (sid, vat["number"], vat["country"], vat["raw"]))

    def export(self, run_id, folder):
        folder.mkdir(parents=True, exist_ok=True)
        rows = self.db.execute("""SELECT DISTINCT p.seller_id,s.source_url,s.company_name,s.business_address,
          v.vat_number,v.country,s.status,s.error,s.checked_at FROM auto_products p
          JOIN auto_sellers s ON s.seller_id=p.seller_id LEFT JOIN auto_vats v ON v.seller_id=s.seller_id
          WHERE p.run_id=? ORDER BY p.seller_id,v.vat_number""", (run_id,)).fetchall()
        products = self.db.execute("SELECT asin,seller_id,product_url,available_date,source_ref FROM auto_products WHERE run_id=? ORDER BY asin,seller_id", (run_id,)).fetchall()
        outputs = [
            ("店铺税号.csv", ["卖家ID", "店铺链接", "公司名称", "公司地址", "税号", "税号国家前缀", "状态", "错误", "核查时间"], rows),
            ("商品来源.csv", ["ASIN", "卖家ID", "商品链接", "上架时间原值", "数据来源"], products),
        ]
        for name, headers, data in outputs:
            with (folder / name).open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                # CSV formula injection prevention; keep VAT leading zeros as text.
                for row in data:
                    writer.writerow(["'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v for v in row])
        return {"products": len(products), "sellers": len({r[0] for r in rows}), "vat_records": sum(bool(r[4]) for r in rows),
                "unresolved_products": sum(not r[1] for r in products), "pending_or_failed": len({r[0] for r in rows if r[6] not in ("ok", "no_public_vat")})}


def select_dropdown(page, current_pattern, option_pattern):
    controls = page.locator("input[readonly]:visible")
    for i in range(controls.count()):
        control = controls.nth(i)
        if re.search(current_pattern, control.input_value()):
            control.click()
            option = page.locator(".el-select-dropdown__item:visible").filter(has_text=re.compile(option_pattern)).first
            if not option.count():
                option = page.get_by_text(re.compile(option_pattern), exact=True).filter(visible=True).first
            option.click()
            if not re.search(option_pattern, control.input_value()):
                raise StopCollection("筛选值未生效，请核对网页界面。")
            return
    raise StopCollection(f"找不到筛选下拉框：{current_pattern}，网站界面可能更新。")


def configure_research(page):
    check_page(page)
    # Reset avoids accidentally inheriting category/sales filters from old sessions.
    page.get_by_text("重置条件", exact=True).first.click()
    select_dropdown(page, r"美国|日本|英国|德国|法国|意大利|西班牙|加拿大|印度|墨西哥", r"^意大利(?:站)?$")
    select_dropdown(page, r"最近30天|^20\d{2}-\d{2}$", r"^最近30天$")
    # Several inputs say 不限; choose only the one whose menu contains 近30天.
    controls = page.locator("input[readonly]:visible")
    for i in range(controls.count()):
        control = controls.nth(i)
        if control.input_value().strip() != "不限":
            continue
        control.click()
        option = page.locator(".el-select-dropdown__item:visible").filter(has_text=re.compile(r"^近30天$")).first
        if option.count():
            option.click()
            if control.input_value().strip() != "近30天":
                raise StopCollection("上架时间筛选未生效")
            break
        page.keyboard.press("Escape")
    else:
        raise StopCollection("找不到上架时间的近30天选项")
    page.get_by_text("开始筛选", exact=True).first.click()
    page.wait_for_timeout(2000)
    check_page(page)


def read_products(page):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page.content(), "html.parser")
    tables = []
    for table in soup.select("table"):
        rows = []
        previous = None
        for tr in table.select("tbody tr"):
            text = tr.get_text(" ", strip=True)
            links = [a.get("href", "") for a in tr.select("a[href]")]
            linked_asins = []
            for link in links:
                parsed = urlparse(link)
                if parsed.hostname in {'amazon.it', 'www.amazon.it'}:
                    match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)', parsed.path, re.I)
                    if match:
                        linked_asins.append(match.group(1).upper())
            match = re.search(r'\bASIN\s*[:：]?\s*([A-Z0-9]{10})\b', text, re.I) or ASIN.search(text)
            if not linked_asins and not match:
                continue
            asin = linked_asins[0] if linked_asins else (match.group(1).upper() if match.lastindex else match.group())
            ids = list(dict.fromkeys(filter(None, (seller_id_from_url(url) for url in links))))
            if previous and previous["asin"] == asin and not ids:
                continue
            # Table contains BuyBox seller; do not infer all offer sellers.
            date = re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", text)
            item = {"asin": asin, "sellerId": ids[0] if ids else "", "availableDate": date.group() if date else "", "row_text": text, "links": links}
            if previous and previous["asin"] == asin and ids and not previous["sellerId"]:
                previous["sellerId"] = ids[0]
            else:
                rows.append(item)
                previous = item
        tables.append(rows)
    return max(tables, key=len, default=[])


def price_split(low, high):
    """Inclusive overlapping boundaries prevent fractional-price gaps."""
    from decimal import Decimal
    if low is not None and high is not None:
        if high - low <= Decimal('0.01'):
            return None
        middle = ((low + high) / 2).quantize(Decimal('0.01'))
        if middle <= low or middle >= high:
            return None
    elif high is not None:
        if high <= Decimal('0.01'):
            return None
        middle = (high / 2).quantize(Decimal('0.01'))
    else:
        middle = max(Decimal('100'), (low or Decimal('0')) * 2)
        if low is not None and middle <= low:
            return None
    return (low, middle), (middle, high)


def set_price_range(page, low, high):
    label = page.get_by_text('价格', exact=True).first
    field = label.locator('xpath=ancestor::*[count(.//input[not(@readonly)])=2][1]')
    inputs = field.locator('input:not([readonly])')
    if inputs.count() != 2:
        raise StopCollection('找不到价格范围输入框，无法自动拆分超过上限的结果')
    for control, value in ((inputs.nth(0), low), (inputs.nth(1), high)):
        text = '' if value is None else str(value)
        control.fill(text)
        if control.input_value() != text:
            raise StopCollection('价格分区条件未生效')
    page.get_by_text('开始筛选', exact=True).first.click()
    page.wait_for_timeout(2000)
    check_page(page)


def collect_browser(page, store, run_id, folder, manifest, args):
    navigate(page, args.url)
    configure_research(page)
    completed_pages = 0
    unique = set()
    root_body = check_page(page)
    total_match = re.search(r'搜索结果数\s*[:：]?\s*([\d,]+)', root_body)
    expected = int(total_match.group(1).replace(',', '')) if total_match else None
    manifest['expected_products'] = expected
    queue = [(None, None, 0)]
    partition_index = 0
    truncated = False
    while queue:
        low, high, depth = queue.pop(0)
        if partition_index:
            set_price_range(page, low, high)
        partition_index += 1
        body = check_page(page)
        total = re.search(r"搜索结果数\s*[:：]?\s*([\d,]+)", body)
        partition_total = int(total.group(1).replace(',', '')) if total else None
        if partition_total is not None and partition_total > args.partition_cap:
            split = price_split(low, high) if depth < 24 else None
            if split:
                queue[0:0] = [(a, b, depth + 1) for a, b in split]
                print(f'[自动拆分] {partition_total} 条，价格区间 {low} ~ {high}', flush=True)
                continue
            truncated = True
            manifest['warnings'].append(f'价格区间 {low} ~ {high} 有 {partition_total} 条，无法继续按价格细分；仍可能截断，需要类目分区。')
        page_number = 0
        seen = set()
        while True:
            body = check_page(page)
            items = read_products(page)
            if not items:
                if partition_total == 0 and any(m in body for m in ('暂无相关数据', '暂无数据')):
                    break
                raise StopCollection('没有读取到商品表，不能确认是否采集完成。请检查会员权限或界面。')
            fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
            if fingerprint in seen:
                raise StopCollection('翻页返回重复页面，已停止，避免误判全部采集完成。')
            seen.add(fingerprint)
            completed_pages += 1
            page_number += 1
            unique.update(item['asin'] for item in items)
            source = f'{page.url}#partition={partition_index}&page={page_number}&price={low}:{high}'
            save_json(folder / 'pages' / f'browser-{completed_pages:05d}.json', {'source': source, 'items': items})
            store.products(run_id, items, source)
            manifest.update(pages=completed_pages, observed_unique_products=len(unique), price_partition=[str(low), str(high)])
            save_json(folder / 'manifest.json', manifest)
            print(f'[卖家精灵] 分区 {partition_index} 第 {page_number} 页，累计 {len(unique)} 个 ASIN，总数 {expected}', flush=True)
            next_button = page.get_by_text('下一页', exact=True).first
            if not next_button.count():
                raise StopCollection('未找到下一页按钮，无法确认最后一页')
            disabled = next_button.is_disabled() or next_button.get_attribute('aria-disabled') == 'true'
            classes = (next_button.get_attribute('class') or '') + ' ' + (next_button.locator('..').get_attribute('class') or '')
            disabled = disabled or 'disabled' in classes
            if disabled:
                break
            if args.max_pages and completed_pages >= args.max_pages:
                manifest['discovery_status'] = 'partial'
                manifest['warnings'].append('达到本次页数限制')
                return
            next_button.click()
            page.wait_for_timeout(int(args.delay * 1000))
    manifest['discovery_status'] = 'complete' if not truncated and expected is not None and len(unique) == expected else 'partial'
    if manifest['discovery_status'] == 'partial':
        manifest['warnings'].append('采集数量未能证明覆盖结果总数；可能有会员限制、重复变体、空价格或结果截断。')


def api_page(key, filters, number):
    body = {"marketplace": "IT", "month": "nearly", "availableMonth": 1, "variation": "N", **filters, "page": number, "size": 100}
    request = Request("https://api.sellersprite.com/v1/product/research", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", "secret-key": key}, method="POST")
    with urlopen(request, timeout=60) as response:
        value = json.load(response)
    if value.get("code") != "OK" or not isinstance(value.get("data"), dict):
        raise StopCollection(f"卖家精灵接口拒绝查询，状态码：{value.get('code')}。检查权限和额度。")
    return value["data"]


def collect_api(store, run_id, folder, manifest, args):
    key = os.environ.get("SELLERSPRITE_API_KEY", "")
    if not key:
        raise StopCollection("未设置 SELLERSPRITE_API_KEY。网页会员登录不等于 API 权限。")
    filters = json.loads(Path(args.partitions).read_text(encoding="utf-8-sig")) if args.partitions else [{}]
    if not isinstance(filters, list) or not filters:
        raise StopCollection("分区配置必须为非空 JSON 数组")
    manifest["discovery_status"] = "complete"
    for index, partition in enumerate(filters, 1):
        if not isinstance(partition, dict) or any(k in partition for k in ("marketplace", "month", "availableMonth", "page", "size", "variation")):
            raise StopCollection("分区配置不允许改变意大利站、时间范围或分页参数")
        number = 1
        seen = set()
        while True:
            data = api_page(key, partition, number)
            items = data.get("items")
            if not isinstance(items, list) or not isinstance(data.get("total"), int):
                raise StopCollection("API 返回结构改变，缺少 items 或 total")
            total = data["total"]
            fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
            if items and fingerprint in seen:
                raise StopCollection("API 翻页重复，停止并保留进度")
            seen.add(fingerprint)
            save_json(folder / "pages" / f"api-{index:04d}-{number:05d}.json", {"filter": partition, "data": data})
            store.products(run_id, items, f"SellerSprite API partition={index} page={number}")
            manifest["pages"] = manifest.get("pages", 0) + 1
            save_json(folder / "manifest.json", manifest)
            print(f"[API] 分区 {index} 第 {number} 页，结果总数 {total}", flush=True)
            if total > 2000:
                manifest["discovery_status"] = "partial"
                warning = f"分区 {index} 有 {total} 条，超过官方 2000 条上限，需细分类目/价格分区。"
                if warning not in manifest["warnings"]:
                    manifest["warnings"].append(warning)
            if number * 100 >= min(total, 2000):
                break
            if not items:
                raise StopCollection("API 在到达结果总数前返回空页")
            if args.max_pages and manifest["pages"] >= args.max_pages:
                manifest["discovery_status"] = "partial"
                manifest["warnings"].append("达到本次 API 页数限制")
                return
            number += 1
            time.sleep(args.delay)
    if args.partitions:
        manifest["discovery_status"] = "partial"
        manifest["warnings"].append("已完成给定分区；未证明分区覆盖全站，需要核对覆盖范围。")


def crawl_vats(context, store, run_id, folder, args):
    page = context.new_page()
    try:
        # Product rows lacking BuyBox IDs are preserved for review. A current
        # product page can resolve its currently displayed seller, with provenance.
        unresolved = store.db.execute("SELECT asin,product_url FROM auto_products WHERE run_id=? AND seller_id=''", (run_id,)).fetchall()
        for row in unresolved:
            try:
                navigate(page, row['product_url'])
                check_page(page, amazon=True)
            except StopCollection:
                raise
            except Exception as exc:
                print(f'[商品卖家回填失败] {row["asin"]}: {type(exc).__name__}', flush=True)
                continue
            links = page.locator("#sellerProfileTriggerId, #merchant-info a[href], #tabular-buybox a[href]")
            sid = ""
            for i in range(links.count()):
                sid = seller_id_from_url(urljoin(page.url, links.nth(i).get_attribute("href") or ""))
                if sid:
                    break
            if sid:
                with store.db:
                    store.db.execute("UPDATE auto_products SET seller_id=?,source_ref=source_ref || ' ; current Amazon BuyBox' WHERE run_id=? AND asin=? AND seller_id=''", (sid, run_id, row["asin"]))
                    store.db.execute("INSERT OR IGNORE INTO auto_sellers(seller_id,source_url,status) VALUES (?,?,'pending')", (sid, seller_url(sid)))
            time.sleep(args.delay)
        sellers = store.db.execute("SELECT DISTINCT s.* FROM auto_sellers s JOIN auto_products p ON p.seller_id=s.seller_id WHERE p.run_id=? ORDER BY s.seller_id", (run_id,)).fetchall()
        processed = 0
        for row in sellers:
            if not args.refresh and row["status"] in ("ok", "no_public_vat"):
                continue
            if args.limit and processed >= args.limit:
                break
            sid = row["seller_id"]
            try:
                response = navigate(page, row['source_url'])
                body = check_page(page, amazon=True)
                if not response or response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status if response else 'unknown'}")
                html = page.content()
                parsed = parse_seller(html)
                if not parsed["company"] and not parsed["address"] and not parsed["vats"]:
                    raise RuntimeError("未识别到卖家商业信息，不能判定没有税号")
                if args.snapshots:
                    target = folder / "seller-pages" / f"{sid}.html"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(html, encoding="utf-8")
                store.save_seller(sid, parsed, "ok" if parsed["vats"] else "no_public_vat")
                print(f"[税号] {sid}: {', '.join(v['number'] for v in parsed['vats']) or '页面未公开税号'}", flush=True)
            except StopCollection:
                raise
            except Exception as exc:
                store.save_seller(sid, {}, "failed", type(exc).__name__ + ": " + str(exc)[:200])
                print(f"[失败] {sid}: {type(exc).__name__}", flush=True)
            processed += 1
            store.export(run_id, folder)
            time.sleep(args.delay)
    finally:
        page.close()


def parser():
    p = argparse.ArgumentParser(description="意大利站近30天商品 → BuyBox店铺链接 → 页面公开税号")
    p.add_argument("action", choices=["login", "run", "export"], nargs="?", default="run")
    p.add_argument("--source", choices=["browser", "api"], default="browser")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--run-id", default=datetime.now(TZ).strftime("%Y-%m-%d"))
    p.add_argument("--db", type=Path, default=ROOT / "data" / "seller_vat.sqlite3")
    p.add_argument("--profile", type=Path, default=ROOT / "data" / "sellersprite-profile")
    p.add_argument("--channel", choices=["chrome", "msedge", "chromium"], default="chrome")
    p.add_argument("--headless", action="store_true")
    p.add_argument('--login-wait', type=int, default=0, help='登录模式等待人工登录秒数；0=终端按Enter确认')
    p.add_argument("--max-pages", type=int, default=0, help="0=全部可访问页面")
    p.add_argument('--partition-cap', type=int, default=2000, help='超过此数量自动按价格拆分筛选；可按会员限制调整')
    p.add_argument("--limit", type=int, default=0, help="本次采集卖家数量，0=全部")
    p.add_argument("--delay", type=float, default=3)
    p.add_argument("--refresh", action="store_true", help="重新核查已完成卖家")
    p.add_argument("--resume", action="store_true", help="从已入库ASIN继续税号采集，不重新查询卖家精灵")
    p.add_argument("--snapshots", action="store_true")
    p.add_argument("--partitions", help="API分区筛选JSON文件")
    return p


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    args = parser().parse_args(argv)
    if args.max_pages < 0 or args.limit < 0 or args.delay < 1 or args.partition_cap < 1:
        raise SystemExit("页数/数量不能为负，间隔至少1秒")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.run_id):
        raise SystemExit("run-id只能包含字母、数字、下划线和短横线")
    parsed_url = urlparse(args.url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in {"www.sellersprite.com", "cn.sellersprite.com", "sellersprite.com"} or parsed_url.username or parsed_url.password:
        raise SystemExit("卖家精灵URL必须为官方HTTPS地址")
    folder = ROOT / "outputs" / "seller-vat" / args.run_id
    folder.mkdir(parents=True, exist_ok=True)
    store = Store(args.db)
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if args.resume and manifest_path.exists() else {
        "run_id": args.run_id, "started_at": now(), "marketplace": "IT", "listing_window_days": 30,
        "seller_scope": "SellerSprite BuyBox sellers; not all offer sellers or newly registered shops",
        "source": args.source, "discovery_status": "pending", "warnings": []}
    if args.action == "export":
        manifest["counts"] = store.export(args.run_id, folder)
        save_json(manifest_path, manifest)
        print(folder)
        store.db.close()
        return 0
    lock = ROOT / "data" / "auto-collection.lock"
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        store.db.close()
        print("已有采集进程或上次异常退出留下锁。确认没有进程运行后删除 data/auto-collection.lock。")
        return 2
    os.write(lock_fd, str(os.getpid()).encode())
    os.close(lock_fd)
    exit_code = 0
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as runtime:
            context = runtime.chromium.launch_persistent_context(str(args.profile),
                channel=None if args.channel == "chromium" else args.channel,
                headless=args.headless and args.action != "login", viewport={"width": 1440, "height": 1000})
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if args.action == "login":
                    page.goto("https://cn.sellersprite.com/cn/w/user/login", wait_until="domcontentloaded", timeout=60000)
                    print("请在打开的浏览器中登录卖家精灵。登录成功后回到此终端按 Enter。")
                    if args.login_wait:
                        print('正在等待浏览器人工登录；请登录后进入选产品页面。', flush=True)
                        deadline = time.monotonic() + args.login_wait
                        while time.monotonic() < deadline:
                            if '/user/login' not in page.url and '/v3' in page.url:
                                try:
                                    check_page(page)
                                    break
                                except StopCollection:
                                    pass
                            time.sleep(2)
                        else:
                            raise StopCollection('等待人工登录超时。请重新运行登录入口。')
                    else:
                        input()
                    page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
                    check_page(page)
                    print("已确认登录，浏览器登录状态保存在本机专用目录。")
                else:
                    manifest["status"] = "running"
                    save_json(manifest_path, manifest)
                    if not args.resume:
                        with store.db:
                            store.db.execute('DELETE FROM auto_products WHERE run_id=?', (args.run_id,))
                        if args.source == "browser":
                            collect_browser(page, store, args.run_id, folder, manifest, args)
                        else:
                            collect_api(store, args.run_id, folder, manifest, args)
                    elif not store.db.execute("SELECT 1 FROM auto_products WHERE run_id=? LIMIT 1", (args.run_id,)).fetchone():
                        raise StopCollection("此 run-id 没有已采集商品，不能续跑")
                    crawl_vats(context, store, args.run_id, folder, args)
                    counts = store.export(args.run_id, folder)
                    manifest["counts"] = counts
                    manifest["status"] = "complete" if manifest["discovery_status"] == "complete" and not counts["pending_or_failed"] and not counts["unresolved_products"] else "partial"
                    exit_code = 0 if manifest["status"] == "complete" else 3
            finally:
                context.close()
    except KeyboardInterrupt:
        manifest.update(status="interrupted", error="用户中断")
        exit_code = 130
    except Exception as exc:
        manifest.update(status="blocked", error=type(exc).__name__ + ": " + str(exc)[:600])
        print(manifest["error"], file=sys.stderr)
        exit_code = 2
    finally:
        if args.action != "login":
            manifest["counts"] = store.export(args.run_id, folder)
            manifest["updated_at"] = now()
            save_json(manifest_path, manifest)
            print(f"结果目录：{folder}")
        store.db.close()
        lock.unlink(missing_ok=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
