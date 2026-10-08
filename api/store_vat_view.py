"""Read-only, paginated views of the saved seller and VAT database."""
from __future__ import annotations

import csv
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode

PAGE_SIZE = 50
COUNTRIES = ("IT", "FR", "DE", "PL", "ES", "AT", "BE", "BG", "CY", "CZ", "DK", "EE", "EL", "FI", "HR", "HU", "IE", "LT", "LU", "LV", "MT", "NL", "PT", "RO", "SE", "SI", "SK", "XI", "GB")
TABLE_LABELS = {
    "sellers": "店铺税号", "seller_vat_evidence": "逐站税号证据",
    "seller_site_checks": "站点核查", "seller_discoveries": "店铺发现",
    "vat_checks": "VIES 查询",
}
LABELS = {
    "id": "ID", "marketplace": "站点", "seller_id": "卖家 ID", "seller_name": "店铺名称",
    "company_name": "店铺公开公司名", "company_snapshot": "卖家精灵公司名",
    "vat_number": "税号", "vat_country": "税号国家", "vies_company_name": "VIES 公司名",
    "vies_address": "VIES 地址", "vies_valid": "VIES 有效", "business_address": "公司地址",
    "checked_at": "核查时间", "first_seen_at": "首次发现", "last_seen_at": "最近发现",
    "discovered_at": "发现时间", "first_page": "来源页码", "source": "来源",
    "status": "核查状态", "error": "错误信息", "country": "国家",
    "source_url": "来源页面", "created_at": "入库时间", "updated_at": "更新时间",
}
REPORT_COLUMNS = [
    "卖家ID", "店铺名称", "公司名称", "公司地址", "IT税号", "FR税号", "DE税号", "PL税号", "ES税号",
    "其他税号", "意大利店铺页", "法国店铺页", "德国店铺页", "波兰店铺页", "西班牙店铺页", "核查状态", "核查时间",
]


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _page_url(query: str, country: str, table: str, page: int) -> str:
    return "/seller-vat?" + urlencode({"q": query, "country": country, "table": table, "page": page})


def _base(mode: str, query: str, country: str, table: str) -> dict:
    return {
        "mode": mode, "available": True, "error": "", "query": query, "country": country,
        "table": table, "tables": [], "countries": COUNTRIES, "supports_country": True,
        "columns": [], "rows": [], "total": 0, "page": 1, "pages": 1,
        "previous_url": "", "next_url": "", "summary": {"records": 0, "stores": 0, "vats": 0, "vies_names": 0},
        "source_label": "", "source_note": "",
    }


def _database_summary(db: sqlite3.Connection) -> dict:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sellers'").fetchone():
        return {"records": 0, "stores": 0, "vats": 0, "vies_names": 0}
    row = db.execute("""SELECT COUNT(*) AS records, COUNT(DISTINCT NULLIF(seller_id,'')) AS stores,
        SUM(CASE WHEN NULLIF(TRIM(COALESCE(vat_number,'')),'') IS NOT NULL THEN 1 ELSE 0 END) AS vats,
        SUM(CASE WHEN NULLIF(TRIM(COALESCE(vies_company_name,'')),'') IS NOT NULL
             AND TRIM(vies_company_name) NOT IN ('---','—') THEN 1 ELSE 0 END) AS vies_names
        FROM sellers""").fetchone()
    return {key: row[key] or 0 for key in ("records", "stores", "vats", "vies_names")}


def load_store_overview(path: Path) -> dict:
    """Return small, read-only totals for the collection overview."""
    result = {"available": False, "stores": 0, "vats": 0, "sites": 0,
              "checks": 0, "last_checked_at": ""}
    if not path.is_file():
        return result
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sellers','seller_site_checks')"
            )}
            if "sellers" not in tables:
                return result
            stores, vats, sites = db.execute("""
                SELECT COUNT(DISTINCT NULLIF(seller_id,'')),
                       COUNT(DISTINCT NULLIF(TRIM(vat_number),'')),
                       COUNT(DISTINCT marketplace)
                FROM sellers
            """).fetchone()
            result.update(available=True, stores=stores, vats=vats, sites=sites)
            if "seller_site_checks" in tables:
                checks, last_checked_at = db.execute(
                    "SELECT COUNT(*), MAX(checked_at) FROM seller_site_checks"
                ).fetchone()
                result.update(checks=checks, last_checked_at=last_checked_at or "")
    except (sqlite3.DatabaseError, OSError, ValueError):
        return {"available": False, "stores": 0, "vats": 0, "sites": 0,
                "checks": 0, "last_checked_at": ""}
    return result


def load_amazon_db(
    path: Path, *, query: str = "", country: str = "", page: int = 1, table: str = "sellers",
) -> dict | None:
    """Read the uploaded SQLite database without creating or changing it."""
    if not path.is_file():
        return None
    query = query.strip()[:100]
    country = country.upper() if country.upper() in COUNTRIES else ""
    view = _base("database", query, country, table)
    view["source_label"] = path.name
    view["source_note"] = "直接显示已保存数据库中的原始字段。店铺公司名与 VIES 公司名分列，空白表示数据库尚无该值。"
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            known = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            available_tables = [name for name in TABLE_LABELS if name in known]
            if "sellers" not in available_tables:
                view.update(available=False, error="数据库中没有 sellers 表。")
                return view
            view["tables"] = [
                {"name": name, "label": TABLE_LABELS[name],
                 "url": _page_url("", "", name, 1)} for name in available_tables
            ]
            table = table if table in available_tables else "sellers"
            view["table"] = table
            table_sql = _quote(table)
            columns = [row["name"] for row in db.execute(f"PRAGMA table_info({table_sql})")]
            view["supports_country"] = "vat_number" in columns
            if not view["supports_country"]:
                country = ""
                view["country"] = ""
            where = []
            args: list[str] = []
            searchable = [field for field in (
                "seller_id", "seller_name", "company_name", "company_snapshot", "vat_number",
                "vies_company_name", "marketplace", "source_url", "error",
            ) if field in columns]
            if query and searchable:
                where.append("(" + " OR ".join(
                    f"CAST({_quote(field)} AS TEXT) LIKE ? ESCAPE '\\'" for field in searchable
                ) + ")")
                args.extend([_like(query)] * len(searchable))
            if country:
                where.append("UPPER(SUBSTR(COALESCE(vat_number,''),1,2))=?")
                args.append(country)
            suffix = " WHERE " + " AND ".join(where) if where else ""
            view["total"] = db.execute(f"SELECT COUNT(*) FROM {table_sql}{suffix}", args).fetchone()[0]
            view["pages"] = max(1, (view["total"] + PAGE_SIZE - 1) // PAGE_SIZE)
            view["page"] = min(max(1, page), view["pages"])
            order_by = _quote("id") if "id" in columns else "rowid"
            view["rows"] = [dict(row) for row in db.execute(
                f"SELECT * FROM {table_sql}{suffix} ORDER BY {order_by} LIMIT ? OFFSET ?",
                [*args, PAGE_SIZE, (view["page"] - 1) * PAGE_SIZE],
            )]
            view["summary"] = _database_summary(db)
            view["columns"] = [{"key": field, "label": LABELS.get(field, field)} for field in columns]
    except (sqlite3.DatabaseError, OSError, ValueError) as exc:
        view.update(available=False, error=f"数据库读取失败：{type(exc).__name__}")
        return view
    view["previous_url"] = _page_url(query, country, table, view["page"] - 1) if view["page"] > 1 else ""
    view["next_url"] = _page_url(query, country, table, view["page"] + 1) if view["page"] < view["pages"] else ""
    return view


def load_store_report(
    root: Path, *, query: str = "", country: str = "", page: int = 1,
) -> dict | None:
    files = sorted(root.glob("eu-store-*/欧洲店铺税号.csv"))
    if not files:
        return None
    path = files[-1]
    query = query.strip()[:100]
    country = country.upper() if country.upper() in COUNTRIES else ""
    view = _base("report", query, country, "report")
    view["source_label"] = path.parent.name
    view["source_note"] = "当前展示店铺报告。上传数据库后可查看全部历史记录及 VIES 公司名。"
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            all_rows = list(csv.DictReader(stream))
        view["summary"] = {
            "records": len(all_rows), "stores": len(all_rows),
            "vats": sum(bool(row.get(c + "税号", "")) for row in all_rows for c in COUNTRIES),
            "vies_names": 0,
        }
        filtered = all_rows
        if query:
            needle = query.casefold()
            filtered = [row for row in filtered if any(needle in str(row.get(field, "")).casefold()
                        for field in ("卖家ID", "店铺名称", "公司名称", "IT税号", "FR税号", "DE税号", "PL税号", "ES税号", "其他税号"))]
        if country:
            filtered = [row for row in filtered if row.get(country + "税号")]
        view["total"] = len(filtered)
        view["pages"] = max(1, (view["total"] + PAGE_SIZE - 1) // PAGE_SIZE)
        view["page"] = min(max(1, page), view["pages"])
        view["rows"] = filtered[(view["page"] - 1) * PAGE_SIZE:view["page"] * PAGE_SIZE]
        view["columns"] = [{"key": field, "label": field} for field in REPORT_COLUMNS]
    except (OSError, UnicodeError, csv.Error) as exc:
        view.update(available=False, error=f"店铺报告读取失败：{type(exc).__name__}")
        return view
    view["previous_url"] = _page_url(query, country, "report", view["page"] - 1) if view["page"] > 1 else ""
    view["next_url"] = _page_url(query, country, "report", view["page"] + 1) if view["page"] < view["pages"] else ""
    return view


def load_store_view(
    db_path: Path, report_root: Path, *, query: str = "", country: str = "", page: int = 1, table: str = "sellers",
) -> dict | None:
    return load_amazon_db(db_path, query=query, country=country, page=page, table=table) or load_store_report(
        report_root, query=query, country=country, page=page,
    )
