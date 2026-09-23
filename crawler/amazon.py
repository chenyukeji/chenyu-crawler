from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

BLOCKED_MARKERS = (
    "enter the characters you see below",
    "robot check",
    "sorry, we just need to make sure you're not a robot",
    "geben sie die zeichen",
    "captcha",
    "automatisierte zugriffe",
    "unusual traffic",
)
class AccessControlBlocked(RuntimeError):
    """Amazon showed login, CAPTCHA, rate limiting, or another access-control page."""


@dataclass(slots=True)
class BrowserSettings:
    channel: str = "msedge"
    headless: bool = False
    slow_mo_ms: int = 75
    timeout_ms: int = 60_000
    between_sources_seconds: float = 3.0
    scroll_steps: int = 12
    scroll_pause_ms: int = 700

    @classmethod
    def from_file(cls, path: Path) -> "BrowserSettings":
        defaults = cls()
        if not path.exists():
            return defaults
        payload = read_json(path)
        return cls(
            channel=str(payload.get("channel", defaults.channel)),
            headless=bool(payload.get("headless", defaults.headless)),
            slow_mo_ms=int(payload.get("slow_mo_ms", defaults.slow_mo_ms)),
            timeout_ms=int(payload.get("timeout_ms", defaults.timeout_ms)),
            between_sources_seconds=float(payload.get("between_sources_seconds", defaults.between_sources_seconds)),
            scroll_steps=int(payload.get("scroll_steps", defaults.scroll_steps)),
            scroll_pause_ms=int(payload.get("scroll_pause_ms", defaults.scroll_pause_ms)),
        )


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def parse_number(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\u20ac", "").replace("\u00a3", "").replace("$", "")
    text = text.replace("\u00a5", "").replace("\uffe5", "").replace("%", "").replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif text.count(",") == 1 and len(text.rsplit(",", 1)[1]) in {1, 2}:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return default


def parse_first_number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    match = re.search(r"\d[\d.,]*", str(value))
    return parse_number(match.group(0), default) if match else default


def detect_access_blocker(body_text: str, current_url: str = "", page_title: str = "") -> str | None:
    combined = f"{body_text}\n{page_title}".casefold()
    for marker in BLOCKED_MARKERS:
        if marker in combined:
            return marker
    url_text = current_url.casefold()
    title_text = page_title.casefold()
    if "/ap/signin" in url_text or "amazon sign-in" in title_text or "amazon anmeldung" in title_text:
        return "Amazon login page"
    return None


def validate_source(source: dict[str, Any]) -> None:
    missing = [key for key in ("marketplace", "category", "url") if not str(source.get(key, "")).strip()]
    if missing:
        raise ValueError(f"Source is missing required fields: {', '.join(missing)}")
    parsed = urlparse(str(source["url"]))
    marketplace = str(source["marketplace"]).strip().upper()
    expected_hosts = {
        "US": {"www.amazon.com", "amazon.com"},
        "DE": {"www.amazon.de", "amazon.de"},
    }
    if marketplace not in expected_hosts:
        raise ValueError(f"Unsupported marketplace: {marketplace}")
    if parsed.scheme != "https" or parsed.netloc.casefold() not in expected_hosts[marketplace]:
        raise ValueError(f"Only configured Amazon US/DE HTTPS pages are allowed: {source['url']}")
    if "/gp/new-releases/" not in parsed.path:
        raise ValueError(f"Not an Amazon New Releases page: {source['url']}")


def deduplicate_items(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Keep the first occurrence of each ASIN in rank order and rebuild ranks 1..limit."""
    ordered = sorted(items, key=lambda item: int(parse_number(item.get("rank"), 9999)))
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in ordered:
        asin = str(item.get("asin", "")).strip().upper()
        title = str(item.get("title", "")).strip()
        if not asin or not title or asin in seen:
            continue
        seen.add(asin)
        unique.append(
            {
                "rank": len(unique) + 1,
                "asin": asin,
                "title": title,
                "review_count": int(parse_number(item.get("review_count"), 0)),
                "price": parse_number(item.get("price"), 0),
                "price_text": str(item.get("price_text") or "").strip(),
                "rating": parse_number(item.get("rating"), 0),
                "product_url": str(item.get("product_url") or "").strip(),
                "image_url": str(item.get("image_url") or "").strip(),
            }
        )
        if len(unique) >= limit:
            break
    return unique


def _read_text(locator: Any, *, attribute: str | None = None) -> str:
    try:
        if locator.count() == 0:
            return ""
        value = locator.first.get_attribute(attribute) if attribute else locator.first.text_content()
        return str(value or "").strip()
    except Exception:
        return ""


def _extract_card(card: Any, fallback_rank: int, page_url: str = "") -> dict[str, Any] | None:
    link = card.locator("a[href*='/dp/'], a[href*='/gp/product/']")
    href = _read_text(link, attribute="href")
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", href, flags=re.IGNORECASE)
    if not match:
        return None
    title_candidates = [
        _read_text(card.locator("[class*='p13n-sc-css-line-clamp']")),
        _read_text(card.locator("a[role='link'] span div")),
        _read_text(card.locator("h2 span")),
        _read_text(card.locator("img[alt]"), attribute="alt"),
    ]
    title = max((candidate for candidate in title_candidates if candidate), key=len, default="")
    if not title:
        return None
    rank_text = _read_text(card.locator(".zg-bdg-text"))
    rank_match = re.search(r"([\d,.]+)", rank_text)
    rank = int(parse_number(rank_match.group(1), fallback_rank)) if rank_match else fallback_rank
    review_text = _read_text(card.locator("a[href*='/product-reviews/'] .a-size-small"))
    review_digits = re.sub(r"\D", "", review_text)
    price_text = _read_text(card.locator(".p13n-sc-price, .a-price .a-offscreen"))
    rating_text = _read_text(card.locator(".a-icon-alt"))
    image_url = _read_text(card.locator("img[src]"), attribute="src")
    parsed_page_url = urlparse(page_url)
    if parsed_page_url.scheme and parsed_page_url.netloc:
        product_url = f"{parsed_page_url.scheme}://{parsed_page_url.netloc}/dp/{match.group(1).upper()}"
    else:
        product_url = urljoin(page_url, href)
    return {
        "rank": rank,
        "asin": match.group(1).upper(),
        "title": title,
        "review_count": int(review_digits or 0),
        "price": parse_number(price_text, 0),
        "price_text": price_text,
        "rating": parse_first_number(rating_text, 0),
        "product_url": product_url,
        "image_url": image_url,
    }


def _next_page_url(page: Any) -> str:
    selectors = (
        ".a-pagination .a-last:not(.a-disabled) a",
        "li.a-last:not(.a-disabled) a",
        "a[aria-label='Next page']",
    )
    for selector in selectors:
        href = _read_text(page.locator(selector), attribute="href")
        if href and not href.casefold().startswith("javascript:"):
            return urljoin(page.url, href)
    return ""


def _check_page_access(page: Any, response: Any = None) -> None:
    status_code = response.status if response is not None else 0
    if status_code in {403, 429, 503}:
        raise AccessControlBlocked(f"Amazon returned HTTP {status_code}")
    body_text = page.locator("body").inner_text(timeout=15_000)
    blocker = detect_access_blocker(body_text, page.url, page.title())
    if blocker:
        raise AccessControlBlocked(f"Amazon access-control page detected: {blocker}")


def collect_source(page: Any, source: dict[str, Any], settings: BrowserSettings, limit: int) -> dict[str, Any]:
    response = page.goto(source["url"], wait_until="domcontentloaded", timeout=settings.timeout_ms)
    page.wait_for_timeout(600)
    _check_page_access(page, response)
    cookie_button = page.locator("#sp-cc-accept")
    if cookie_button.count() and cookie_button.first.is_visible():
        cookie_button.first.click(timeout=5_000)
        page.wait_for_timeout(300)
    extracted: list[dict[str, Any]] = []
    visited_page_urls: set[str] = set()
    pages_visited = 0
    while len(deduplicate_items(extracted, limit)) < limit:
        current_url = page.url
        if current_url in visited_page_urls:
            break
        visited_page_urls.add(current_url)
        pages_visited += 1
        if pages_visited > 1:
            _check_page_access(page)
        for _ in range(max(0, settings.scroll_steps)):
            page.mouse.wheel(0, 850)
            page.wait_for_timeout(settings.scroll_pause_ms)
        cards = page.locator("div.zg-grid-general-faceout")
        if cards.count() == 0:
            cards = page.locator("div.p13n-sc-uncoverable-faceout")
        if cards.count() == 0:
            if pages_visited == 1:
                raise RuntimeError("Amazon loaded, but no visible New Releases product cards were found")
            break
        unique_before = len(deduplicate_items(extracted, limit))
        remaining = limit - unique_before
        for index in range(min(cards.count(), remaining)):
            item = _extract_card(cards.nth(index), unique_before + index + 1, current_url)
            if item:
                extracted.append(item)
        items_so_far = deduplicate_items(extracted, limit)
        if len(items_so_far) >= limit or len(items_so_far) == unique_before:
            break
        next_url = _next_page_url(page)
        if not next_url or next_url in visited_page_urls:
            break
        response = page.goto(next_url, wait_until="domcontentloaded", timeout=settings.timeout_ms)
        page.wait_for_timeout(600)
        _check_page_access(page, response)
    items = deduplicate_items(extracted, limit)
    if not items:
        raise RuntimeError("Amazon product cards were present, but no valid ASIN/title pairs were readable")
    return {
        "url": source["url"],
        "marketplace": str(source["marketplace"]).upper(),
        "category": source["category"],
        "snapshot_date": "",
        "status": "ok" if len(items) >= limit else "partial",
        "target_items": limit,
        "top_list_complete": len(items) >= limit,
        "pages_visited": pages_visited,
        "items": items,
    }


