from __future__ import annotations

from pathlib import Path
from typing import Any

from crawler.amazon import read_json, validate_source


def source_id(source: dict[str, Any]) -> str:
    return f"{str(source['marketplace']).upper()}_{source['category']}"


def load_sources(config_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load and validate configured Amazon New Releases sources."""
    config = read_json(config_path)
    sources = list(config.get("sources", []))
    if not sources:
        raise ValueError("No sources were configured")
    for source in sources:
        validate_source(source)
    return config, sources


def select_sources(
    sources: list[dict[str, Any]],
    *,
    marketplace: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    selected = sources
    if marketplace:
        market = marketplace.upper()
        selected = [source for source in selected if str(source["marketplace"]).upper() == market]
    if category:
        selected = [source for source in selected if str(source["category"]) == category]
    return selected
