from __future__ import annotations

import sys
from pathlib import Path

# This project intentionally keeps the requested local folder name "mcp/".
# The official Python SDK is also imported as "mcp", so temporarily remove
# both the project root and this script directory while importing the SDK.
ROOT = Path(__file__).resolve().parents[1]
LOCAL_MCP_DIR = Path(__file__).resolve().parent

_original_sys_path = list(sys.path)
_filtered_sys_path: list[str] = []
for entry in sys.path:
    try:
        resolved = Path(entry or ".").resolve()
    except OSError:
        _filtered_sys_path.append(entry)
        continue
    if resolved not in {ROOT, LOCAL_MCP_DIR}:
        _filtered_sys_path.append(entry)

sys.path = _filtered_sys_path
try:
    from mcp.server import MCPServer
finally:
    sys.path = _original_sys_path

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.selection_queries import (
    get_hot_clusters as query_hot_clusters,
    get_new_entries as query_new_entries,
    get_rank_history as query_rank_history,
    get_repeat_products as query_repeat_products,
    get_rising_products as query_rising_products,
    get_selection_candidates as query_selection_candidates,
)


mcp = MCPServer("chenyu-amazon-new-releases")


@mcp.tool()
def get_new_entries(
    marketplace: str,
    days: int = 1,
    category: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return products first seen in the local New Releases history window."""
    return query_new_entries(marketplace, days, category, limit)


@mcp.tool()
def get_repeat_products(
    marketplace: str,
    days: int = 10,
    min_days: int = 3,
    category: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return ASINs repeatedly present across recent New Releases snapshots."""
    return query_repeat_products(marketplace, days, min_days, category, limit)


@mcp.tool()
def get_rising_products(
    marketplace: str,
    days: int = 7,
    min_improvement: int = 5,
    category: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return products whose New Releases rank is improving over recent snapshots."""
    return query_rising_products(marketplace, days, min_improvement, category, limit)


@mcp.tool()
def get_rank_history(
    asin: str,
    marketplace: str,
    days: int = 10,
    category: str | None = None,
) -> list[dict]:
    """Return the recent rank history for one ASIN."""
    return query_rank_history(asin, marketplace, days, category)


@mcp.tool()
def get_hot_clusters(
    marketplace: str,
    days: int = 10,
    min_unique_asins: int = 2,
    category: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return repeated normalized product concepts in recent New Releases data."""
    return query_hot_clusters(marketplace, days, min_unique_asins, category, limit)


@mcp.tool()
def get_selection_candidates(
    marketplace: str,
    category: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Merge NEW, REPEAT and RISING signals into a selection candidate pool."""
    return query_selection_candidates(marketplace, category, limit)


if __name__ == "__main__":
    mcp.run("stdio")
