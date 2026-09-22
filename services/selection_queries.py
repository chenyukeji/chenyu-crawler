from __future__ import annotations

from pathlib import Path
from typing import Any

from analysis.new_entries import find_new_entries
from analysis.product_clusters import find_hot_clusters
from analysis.rank_trends import find_rising_products, get_rank_history as query_rank_history
from analysis.repeat_products import find_repeat_products


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "data" / "new_releases.db"


def get_new_entries(
    marketplace: str,
    days: int = 1,
    category: str | None = None,
    limit: int = 100,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    return find_new_entries(
        db_path,
        marketplace=marketplace,
        days=days,
        category=category,
        limit=limit,
    )


def get_repeat_products(
    marketplace: str,
    days: int = 10,
    min_days: int = 3,
    category: str | None = None,
    limit: int = 100,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    return find_repeat_products(
        db_path,
        marketplace=marketplace,
        days=days,
        min_days=min_days,
        category=category,
        limit=limit,
    )


def get_rising_products(
    marketplace: str,
    days: int = 7,
    min_improvement: int = 5,
    category: str | None = None,
    limit: int = 100,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    return find_rising_products(
        db_path,
        marketplace=marketplace,
        days=days,
        min_improvement=min_improvement,
        category=category,
        limit=limit,
    )


def get_rank_history(
    asin: str,
    marketplace: str,
    days: int = 10,
    category: str | None = None,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    return query_rank_history(
        db_path,
        asin=asin,
        marketplace=marketplace,
        days=days,
        category=category,
    )


def get_hot_clusters(
    marketplace: str,
    days: int = 10,
    min_unique_asins: int = 2,
    category: str | None = None,
    limit: int = 50,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    return find_hot_clusters(
        db_path,
        marketplace=marketplace,
        days=days,
        min_unique_asins=min_unique_asins,
        category=category,
        limit=limit,
    )


def get_selection_candidates(
    marketplace: str,
    category: str | None = None,
    limit: int = 100,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Merge deterministic NEW / REPEAT / RISING signals without asking the LLM to do SQL."""
    merged: dict[str, dict[str, Any]] = {}

    signal_sets = (
        ("NEW", get_new_entries(marketplace, days=3, category=category, limit=limit, db_path=db_path)),
        (
            "REPEAT",
            get_repeat_products(
                marketplace,
                days=10,
                min_days=3,
                category=category,
                limit=limit,
                db_path=db_path,
            ),
        ),
        (
            "RISING",
            get_rising_products(
                marketplace,
                days=7,
                min_improvement=5,
                category=category,
                limit=limit,
                db_path=db_path,
            ),
        ),
    )

    for signal, rows in signal_sets:
        for row in rows:
            asin = str(row["asin"])
            candidate = merged.setdefault(asin, {"asin": asin, "signals": []})
            if signal not in candidate["signals"]:
                candidate["signals"].append(signal)
            for key, value in row.items():
                if key == "asin" or value is None:
                    continue
                candidate[key] = value

    results = list(merged.values())
    results.sort(
        key=lambda item: (
            len(item["signals"]),
            item.get("consecutive_snapshots", 0),
            item.get("period_rank_change", 0),
            -(item.get("current_rank") or 9999),
        ),
        reverse=True,
    )
    return results[:limit]
