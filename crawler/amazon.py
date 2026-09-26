from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from playwright.sync_api import Error as PlaywrightError
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
    "unauthorized ai agent",
)
class AccessControlBlocked(RuntimeError):
    """Amazon showed login, CAPTCHA, rate limiting, or another access-control page."""

    def __init__(self, message: str, partial_snapshot: dict | None = None, diagnostics: dict | None = None):
        super().__init__(message)
        self.partial_snapshot = partial_snapshot
        self.diagnostics = diagnostics or {}



class ParseError(RuntimeError):
    """A page/card cannot be interpreted without inventing product facts."""


class PageCollectionError(RuntimeError):
    """A recoverable page failure, retaining only prior successful pages."""

    def __init__(self, message, resume_from, *, parse_error=False):
        super().__init__(message)
        self.resume_from = resume_from
        self.parse_error = parse_error


def actual_rank(value: Any) -> int | None:
    text = str(value).strip()
    return int(text) if re.fullmatch(r"[0-9]+", text) and int(text) > 0 else None


@dataclass(slots=True)
class BrowserSettings:
    channel: str = "msedge"
    headless: bool = False
    slow_mo_ms: int = 75
    timeout_ms: int = 60_000
    between_sources_seconds: float = 3.0
    between_pages_seconds: float = 0.0
    scroll_steps: int = 12
    scroll_pause_ms: int = 700

    @classmethod
    def from_file(cls, path: Path) -> "BrowserSettings":
        defaults = cls()
        if not path.exists():
            return defaults
        payload = read_json(path)
        page_delay = float(payload.get("between_pages_seconds", defaults.between_pages_seconds))
        if not 0 <= page_delay <= 300:
            raise ValueError("between_pages_seconds must be between 0 and 300")
        return cls(
            channel=str(payload.get("channel", defaults.channel)),
            headless=bool(payload.get("headless", defaults.headless)),
            slow_mo_ms=int(payload.get("slow_mo_ms", defaults.slow_mo_ms)),
            timeout_ms=int(payload.get("timeout_ms", defaults.timeout_ms)),
            between_sources_seconds=float(payload.get("between_sources_seconds", defaults.between_sources_seconds)),
            between_pages_seconds=page_delay,
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
    """Keep the first occurrence of each ASIN and preserve the actual Amazon rank."""
    ordered = sorted(items, key=lambda item: int(parse_number(item.get("rank"), 9999)))
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in ordered:
        asin = str(item.get("asin", "")).strip().upper()
        title = str(item.get("title", "")).strip()
        rank = actual_rank(item.get("rank"))
        if not asin or not title or asin in seen or rank is None:
            continue
        seen.add(asin)
        unique.append(
            {
                "rank": rank,
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


def _extract_card(card: Any, page_url: str = "") -> dict[str, Any] | None:
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
    if not rank_text:
        rank_text = _read_text(card.locator("xpath=ancestor::div[.//span[contains(@class, 'zg-bdg-text')]][1]").locator(".zg-bdg-text"))
    rank_match = re.fullmatch(r"#?\s*([0-9]+|[0-9]{1,3}(?:[,.][0-9]{3})+)", rank_text.strip())
    rank = actual_rank(re.sub(r"[,.]", "", rank_match.group(1))) if rank_match else None
    if rank is None:
        raise ParseError("MISSING_RANK: 商品卡片缺少真实榜单排名")
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


def _click_next_page(page: Any, next_url: str, settings: BrowserSettings) -> Any:
    """Click the real pagination link, preferring its numbered button."""
    links = page.locator(".a-pagination a, li.a-last:not(.a-disabled) a, a[aria-label='Next page']")
    candidates = []
    for index in range(links.count()):
        link = links.nth(index)
        if urljoin(page.url, link.get_attribute("href") or "") == next_url and link.is_visible():
            candidates.append(link)
    if not candidates:
        raise ParseError(f"PAGINATION_LINK_MISSING: 页面没有可点击的下一页链接; URL={next_url}")
    link = next((item for item in candidates if (item.inner_text() or "").strip().isdigit()), candidates[0])
    # Scroll the ordinary page until pagination is in view.
    for _ in range(40):
        box = link.bounding_box()
        height = page.evaluate("window.innerHeight")
        if box and 0 <= box['y'] and box['y'] + box['height'] <= height:
            break
        page.mouse.wheel(0, 850 if not box or box['y'] > 0 else -850)
        page.wait_for_timeout(settings.scroll_pause_ms)
    link.scroll_into_view_if_needed(timeout=settings.timeout_ms)
    print(f"点击分页按钮 {link.inner_text().strip()}: {next_url}", flush=True)
    with page.expect_navigation(wait_until="domcontentloaded", timeout=settings.timeout_ms) as navigation:
        link.click(timeout=settings.timeout_ms)
    return navigation.value


def _check_page_access(page: Any, response: Any = None, *, stage: str = "首次打开", page_number: int = 1) -> None:
    status_code = response.status if response is not None else 0
    body_text = page.locator("body").inner_text(timeout=15_000)
    blocker = detect_access_blocker(body_text, page.url, page.title())
    if blocker or status_code in {403, 429}:
        if blocker == "unauthorized ai agent":
            kind, action = "AGENT_RESTRICTED", "站点明确限制自动化访问，需恢复访问授权后再采集"
        elif blocker == "Amazon login page":
            kind, action = "LOGIN_REQUIRED", "页面要求登录，需人工确认访问权限"
        elif status_code == 429:
            kind, action = "RATE_LIMITED", "请求频率受限，停止当前采集并查看 Retry-After"
        elif blocker:
            kind, action = "VERIFICATION_REQUIRED", "页面要求验证或提示异常访问，需人工检查"
        else:
            kind, action = "HTTP_FORBIDDEN", "服务器拒绝访问，需检查访问权限"
        headers = response.headers if response is not None else {}
        # Never persist cookies, authorization headers, or complete request headers.
        safe_headers = {k: v for k, v in headers.items()
                        if k.lower() in {"retry-after", "content-type", "date", "x-amz-rid", "x-amzn-requestid"}} if isinstance(headers, dict) else {}
        diagnostic = {"kind": kind, "stage": stage, "page_number": page_number,
                      "http_status": status_code, "url": page.url,
                      "marker": blocker, "response_headers": safe_headers, "action": action}
        raise AccessControlBlocked(
            f"ACCESS_BLOCKED: {kind}; Amazon 访问受限 ({blocker or status_code}); "
            f"阶段={stage}; 第{page_number}页; HTTP={status_code}; URL={page.url}; "
            f"处理建议={action}; 页面={body_text[:350]}", diagnostics=diagnostic)
    if status_code >= 400:
        raise RuntimeError(f"HTTP_ERROR: HTTP {status_code}; URL={page.url}; 页面={body_text[:350]}")



def collect_source(page: Any, source: dict[str, Any], settings: BrowserSettings, limit: int, *, on_checkpoint: Any = None, resume_from: dict | None = None) -> dict[str, Any]:
    retry_state = resume_from or {"url": source["url"], "items": [], "page_diagnostics": [], "visited_page_urls": [], "pages_visited": 0}
    checkpoint = (_build_snapshot(source, retry_state["items"], limit, retry_state["page_diagnostics"], retry_state["pages_visited"], "正在恢复失败页")
                  if retry_state["items"] else None)

    def remember_cursor(state):
        nonlocal retry_state
        retry_state = state

    def remember(snapshot):
        nonlocal checkpoint
        checkpoint = snapshot
        if on_checkpoint:
            on_checkpoint(snapshot)

    try:
        return _collect_source(page, source, settings, limit, on_checkpoint=remember, resume_from=retry_state, on_cursor=remember_cursor)
    except AccessControlBlocked as exc:
        # A restriction can be embedded in a partly rendered product page.
        # Read only the DOM already returned: no scrolling, clicking or requests.
        retained = list(retry_state.get("items", []))
        candidates = list(retained)
        try:
            cards = page.locator("div.zg-grid-general-faceout")
            if cards.count() == 0:
                cards = page.locator("div.p13n-sc-uncoverable-faceout")
            rejected = 0
            for index in range(cards.count()):
                try:
                    item = _extract_card(cards.nth(index), page.url)
                    if item and item["image_url"]:
                        candidates.append(item)
                    else:
                        rejected += 1
                except ParseError:
                    rejected += 1
            items = deduplicate_items(candidates, limit)
            if len(items) > len(retained):
                diagnostics = list(retry_state.get("page_diagnostics", []))
                diagnostics.append({"url": page.url, "http_status": exc.diagnostics.get("http_status"),
                                    "cards": cards.count(), "rejected_cards": rejected,
                                    "new_unique_items": len(items) - len(retained),
                                    "access_blocked": True, "load_stable": False})
                snapshot = _build_snapshot(source, items, limit, diagnostics,
                                           exc.diagnostics.get("page_number", len(diagnostics)), "页面访问受限，只保留已返回商品")
                snapshot.update(status="partial", top_list_complete=False, error_message=str(exc))
                if checkpoint is None or len(snapshot["items"]) >= len(checkpoint["items"]):
                    exc.partial_snapshot = snapshot
                    remember(snapshot)
        except Exception as salvage_error:
            # Preserve the original access restriction if local extraction fails.
            exc.diagnostics["salvage_error"] = str(salvage_error)
        raise
    except (PlaywrightError, ParseError, RuntimeError, OSError) as exc:
        raise PageCollectionError(str(exc), retry_state, parse_error=isinstance(exc, ParseError)) from exc


def _wait_for_page_settle(page: Any, *, max_polls: int = 60) -> dict:
    """Require document load and three seconds of unchanged product content.

    A finite deadline avoids hanging on slow third-party resources. Callers
    retain parsed products but must not paginate when the deadline is reached.
    """
    previous = None
    stable_polls = 0
    last_state = {}
    for poll in range(max_polls):
        last_state = page.evaluate("""() => ({
            ready: document.readyState,
            products: [...document.querySelectorAll('div.zg-grid-general-faceout, div.p13n-sc-uncoverable-faceout')]
                .map(e => [e.querySelector('a[href*="/dp/"]')?.getAttribute('href'),
                           e.querySelector('img')?.getAttribute('src'), e.textContent].join('|'))
        })""")
        if last_state['ready'] == 'complete' and last_state == previous:
            stable_polls += 1
        else:
            stable_polls = 0
        if stable_polls >= 6:
            return {"settled": True, "ready_state": last_state['ready'], "waited_ms": poll * 500}
        previous = last_state
        if poll + 1 < max_polls:
            page.wait_for_timeout(500)
    return {"settled": False, "ready_state": last_state.get('ready'), "waited_ms": (max_polls - 1) * 500}


def _collect_source(page: Any, source: dict[str, Any], settings: BrowserSettings, limit: int, *, on_checkpoint: Any = None, resume_from: dict | None = None, on_cursor: Any = None) -> dict[str, Any]:
    state = resume_from or {"url": source["url"], "items": [], "page_diagnostics": [], "visited_page_urls": [], "pages_visited": 0}
    extracted = list(state["items"])
    visited_page_urls = set(state["visited_page_urls"])
    pages_visited = state["pages_visited"]
    page_diagnostics = list(state["page_diagnostics"])
    stop_reason = ""
    response = page.goto(state["url"], wait_until="domcontentloaded", timeout=settings.timeout_ms)
    page.wait_for_timeout(600)
    _check_page_access(page, response, stage="恢复失败页" if pages_visited else "首次打开", page_number=pages_visited + 1)
    while len(deduplicate_items(extracted, limit)) < limit:
        current_url = page.url
        if current_url in visited_page_urls:
            break
        visited_page_urls.add(current_url)
        pages_visited += 1
        # Cookie controls may appear again after a locale redirect or pagination.
        for selector in ("#sp-cc-rejectall-link", "#sp-cc-accept"):
            button = page.locator(selector)
            if button.count() and button.first.is_visible():
                button.first.click(timeout=5_000)
                page.wait_for_timeout(300)
                break
        # Fixed wheel counts stop short on long grids. Reach the bottom and allow
        # lazy content to settle; bound the work even when a page never fills.
        stable_bottom = 0
        previous_count = -1
        for _ in range(max(40, settings.scroll_steps)):
            count = page.locator("div.zg-grid-general-faceout").count()
            if not count:
                count = page.locator("div.p13n-sc-uncoverable-faceout").count()
            at_bottom = page.evaluate("window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 10")
            if count != previous_count:
                stable_bottom = 0
            elif at_bottom:
                stable_bottom += 1
            if count >= 50 or stable_bottom >= 4:
                break
            if at_bottom and count:
                # Amazon may leave the lazy-load trigger outside the viewport.
                # Wheel events at the bottom do not move it; revisit the last
                # viewport to trigger normal page loading before continuing.
                page.mouse.wheel(0, -1400)
                page.wait_for_timeout(settings.scroll_pause_ms)
            previous_count = count
            page.mouse.wheel(0, 850)
            page.wait_for_timeout(settings.scroll_pause_ms)
        settlement = _wait_for_page_settle(page)
        _check_page_access(page, response, stage="滚动后", page_number=pages_visited)
        cards = page.locator("div.zg-grid-general-faceout")
        if cards.count() == 0:
            cards = page.locator("div.p13n-sc-uncoverable-faceout")
        if cards.count() == 0:
            raise ParseError(f"NO_PRODUCT_CARDS: 第{pages_visited}页; HTTP={response.status if response else 0}; URL={page.url}; title={page.title()}; body={page.locator('body').inner_text()[:500]}")
        unique_before = len(deduplicate_items(extracted, limit))
        rejected = 0
        missing_rank_cards = 0
        for index in range(cards.count()):
            try:
                item = _extract_card(cards.nth(index), current_url)
            except ParseError:
                missing_rank_cards += 1
                rejected += 1
                continue
            if item and item["image_url"]:
                extracted.append(item)
            else:
                rejected += 1
        items_so_far = deduplicate_items(extracted, limit)
        page_diagnostics.append({"url": current_url, "http_status": response.status if response else None, "cards": cards.count(), "rejected_cards": rejected, "missing_rank_cards": missing_rank_cards, "new_unique_items": len(items_so_far) - unique_before, "load_stable": (count >= 50 or stable_bottom >= 4) and settlement["settled"], "load_wait": settlement, "collected_at": datetime.now(timezone.utc).isoformat()})
        if on_checkpoint:
            on_checkpoint(_build_snapshot(source, items_so_far, limit, page_diagnostics, pages_visited, "后续分页尚未完成"))
        if not settlement["settled"]:
            raise ParseError(f"PAGE_NOT_SETTLED: 第{pages_visited}页加载未稳定，直接重试当前页")
        if rejected:
            raise ParseError(f"PAGE_PARSE_ERROR: 第{pages_visited}页有 {rejected} 张无效商品卡片，直接重试当前页")
        if len(items_so_far) >= limit:
            break
        if len(items_so_far) == unique_before:
            stop_reason = "分页没有新增有效 ASIN"
            break
        next_url = _next_page_url(page)
        if not next_url or next_url in visited_page_urls:
            stop_reason = "已到末页，页面没有可用下一页链接" if not next_url else "下一页链接重复"
            break
        if on_cursor:
            on_cursor({"url": next_url, "items": list(items_so_far),
                       "page_diagnostics": list(page_diagnostics),
                       "visited_page_urls": sorted(visited_page_urls), "pages_visited": pages_visited})
        if settings.between_pages_seconds:
            print(f"{source['marketplace']}/{source['category']}: 页面加载稳定，额外等待 {settings.between_pages_seconds:g} 秒后访问第 {pages_visited + 1} 页", flush=True)
            page.wait_for_timeout(settings.between_pages_seconds * 1000)
            _check_page_access(page, response, stage="翻页前等待后", page_number=pages_visited)
        response = _click_next_page(page, next_url, settings)
        page.wait_for_timeout(600)
        _check_page_access(page, response, stage="翻页后", page_number=pages_visited + 1)
    items = deduplicate_items(extracted, limit)
    if not items:
        raise ParseError("PARSE_ERROR: 无可用商品，ASIN、标题、图片或真实排名缺失；分页=" + json.dumps(page_diagnostics, ensure_ascii=False))
    return _build_snapshot(source, items, limit, page_diagnostics, pages_visited, stop_reason)


def _build_snapshot(source, items, limit, page_diagnostics, pages_visited, stop_reason):
    missing_ranks = sorted(set(range(1, limit + 1)) - {item["rank"] for item in items})
    reached_limit = (len(items) >= limit and not missing_ranks and bool(page_diagnostics)
                     and all(p.get("load_stable", False) and not p.get("rejected_cards", 0)
                             and not p.get("access_blocked", False) for p in page_diagnostics)
                     and stop_reason != "页面加载未稳定，保留结果并延迟补抓")
    contiguous = bool(items) and {item["rank"] for item in items} == set(range(1, len(items) + 1))
    clean_pages = bool(page_diagnostics) and all(
        p.get("rejected_cards", 0) == 0 and p.get("missing_rank_cards", 0) == 0
        and p.get("cards") == p.get("new_unique_items") for p in page_diagnostics)
    exhausted = (stop_reason == "已到末页，页面没有可用下一页链接"
                 and page_diagnostics[-1].get("load_stable", False)) if page_diagnostics else False
    complete = reached_limit or (exhausted and contiguous and clean_pages)
    effective_target = len(items) if complete and len(items) < limit else limit
    return {
        "url": source["url"],
        "marketplace": str(source["marketplace"]).upper(),
        "category": source["category"],
        "snapshot_date": "",
        "status": "ok" if complete else "partial",
        "error_message": "" if complete else f"INCOMPLETE: {len(items)}/{limit} 条；{stop_reason or '排名不完整'}；缺失排名={missing_ranks}；分页={json.dumps(page_diagnostics, ensure_ascii=False)}",
        "page_diagnostics": list(page_diagnostics),
        "target_items": effective_target,
        "max_items": limit,
        "completion_reason": ("榜单已采集至末页" if not reached_limit else "已达到采集上限") if complete else "",
        "top_list_complete": complete,
        "pages_visited": pages_visited,
        "items": items,
    }


