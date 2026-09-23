from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from crawler.amazon import read_json, validate_source


DEFAULT_DAILY_SCHEDULE = "06:00"


def source_id(source: dict[str, Any]) -> str:
    return f"{str(source['marketplace']).upper()}_{source['category']}"


def validate_daily_schedule(value: str) -> str:
    schedule = value.strip()
    try:
        parsed = datetime.strptime(schedule, "%H:%M")
    except ValueError as exc:
        raise ValueError("Daily schedule must use HH:MM in 24-hour time") from exc
    return parsed.strftime("%H:%M")


def load_config(config_path: Path) -> dict[str, Any]:
    config = read_json(config_path)
    config["daily_schedule"] = validate_daily_schedule(
        str(config.get("daily_schedule", DEFAULT_DAILY_SCHEDULE))
    )
    sources = list(config.get("sources", []))
    if not sources:
        raise ValueError("No sources were configured")
    seen: set[str] = set()
    for source in sources:
        validate_source(source)
        current_id = source_id(source)
        if current_id in seen:
            raise ValueError(f"Duplicate source: {current_id}")
        seen.add(current_id)
        source["enabled"] = bool(source.get("enabled", True))
    config["sources"] = sources
    return config


def load_sources(config_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = load_config(config_path)
    sources = [source for source in config["sources"] if source["enabled"]]
    if not sources:
        raise ValueError("At least one source must be enabled")
    return config, sources


def save_config(config_path: Path, config: dict[str, Any]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = config_path.with_suffix(f"{config_path.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary_path.replace(config_path)
