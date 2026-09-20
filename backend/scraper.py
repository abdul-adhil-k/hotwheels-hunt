"""
Scraper for FirstCry Hot Wheels products.

Strategy
--------
1. Primary path: FirstCry's own product listing is loaded via an internal
   JSON AJAX endpoint (`GetSearchResultProductsPaging`) — the same one the
   site's infinite-scroll uses. It returns exact stock counts (`CrntStock`)
   and the total catalog size, so we hit it directly over plain HTTP
   instead of guessing at HTML/CSS. This is far faster and more reliable
   than parsing rendered HTML, and it's the only way to see real pagination
   (a plain `?page=N` query string on the listing page is a no-op — FirstCry
   paginates client-side only).
2. Every page of the catalog is fetched concurrently — a top-down scan and
   a bottom-up scan run in parallel — and each page's products are streamed
   to the caller the instant that page finishes, so restocks can be
   diffed/notified without waiting for the slowest page in the catalog.
3. A full headless-browser (Playwright) scrape — which scrolls to trigger
   the same infinite-load — is used only as a fallback if the JSON API
   changes shape or stops responding.
4. A single-page BeautifulSoup fetch is the last-resort fallback if even
   the browser can't launch (e.g. sandboxed environments).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import (
    Browser, BrowserContext, Page, async_playwright
)

from backend.config import settings

# Callback invoked with the products found on a single page, as soon as that
# page finishes fetching — lets callers (the scheduler) notify instantly
# instead of waiting for every page in the catalog to be scraped.
OnPageCallback = Callable[[list["ScrapedProduct"]], Awaitable[None]]

_BS4_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Referer": "https://www.firstcry.com/",
}

logger = logging.getLogger(__name__)

# ── Series detection keywords ─────────────────────────────────────────────────
SERIES_PATTERNS: list[tuple[str, str]] = [
    (r"premium", "Premium"),
    (r"car culture", "Car Culture"),
    (r"fast\s*&?\s*furious", "Fast & Furious"),
    (r"mainline", "Mainline"),
    (r"id\s*cars?", "id Cars"),
    (r"track\s*builder", "Track Builder"),
    (r"hw\s*racing", "HW Racing"),
    (r"monster\s*trucks?", "Monster Trucks"),
    (r"mario\s*kart", "Mario Kart"),
    (r"mario", "Mario"),
    (r"star\s*wars", "Star Wars"),
    (r"marvel|dc\s*comics|batman|superman", "Pop Culture"),
]

# ── FirstCry product selectors (with fallbacks) ───────────────────────────────
# These selectors target FirstCry's React-rendered product grid.
PRODUCT_CARD_SELECTORS = [
    "div[data-pid]",
    "div.product-box",
    "div.product-card",
    "li.product-item",
    "div[class*='ProductCard']",
    "div[class*='product-card']",
    "div[class*='ProductBox']",
]

PRODUCT_LINK_SELECTORS = [
    "a[href*='/product-detail']",
    "a[href*='product-detail']",
    "a.product-link",
    "a[class*='ProductLink']",
]

NEXT_PAGE_SELECTORS = [
    "a[aria-label='Next']",
    "a.next-page",
    "button[aria-label='Next']",
    "li.next > a",
    "a[rel='next']",
]


@dataclass
class ScrapedProduct:
    product_id: str
    name: str
    url: str
    price: Optional[float] = None
    original_price: Optional[float] = None
    image_url: Optional[str] = None
    is_available: bool = True
    series: Optional[str] = None
    variant_group_id: Optional[str] = None

    # Keywords that indicate a set/track rather than an individual die-cast car
    _SET_KEYWORDS = re.compile(
        r"\b(track\s*set|playset|play\s*set|trackset|loop\s*set|"
        r"launcher|race\s*track|stunt\s*track|garage|city\s*set|"
        r"transporter|carry\s*case|storage|display\s*set|"
        r"track\s*and\s*\d|track\s*&\s*\d|\d+\s*track)\b",
        re.I,
    )

    def is_valid(self) -> bool:
        """Reject records missing required fields or with junk name/URL."""
        if not self.product_id or not self.product_id.isdigit():
            return False
        if not self.name or len(self.name) < 8:
            return False
        # Dimension strings like "L 5.5 x B 3 x H 1.5 cm" are not product names
        if re.match(r"^[LBHlbh]\s*\d", self.name):
            return False
        if not self.url or "firstcry.com" not in self.url:
            return False
        if not self.url.endswith("/product-detail") and "product-detail" not in self.url:
            return False
        if self.price is not None and self.price <= 0:
            return False
        if self._SET_KEYWORDS.search(self.name):
            return False
        return True


def _extract_product_id(url: str) -> Optional[str]:
    """Pull the numeric product ID from a FirstCry product URL."""
    m = re.search(r"/(\d{5,})/product-detail", url)
    if m:
        return m.group(1)
    # fallback: last path segment before query
    m = re.search(r"/(\d{5,})(?:[/?]|$)", url)
    return m.group(1) if m else None


def _detect_series(name: str) -> Optional[str]:
    lower = name.lower()
    for pattern, label in SERIES_PATTERNS:
        if re.search(pattern, lower):
            return label
    return "Mainline"  # default for Hot Wheels


def _parse_price(text: str) -> Optional[float]:
    m = re.search(r"[\d,]+(?:\.\d+)?", text.replace(",", ""))
    return float(m.group().replace(",", "")) if m else None


async def _random_delay(min_ms: int = 800, max_ms: int = 2500) -> None:
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


async def _scroll_to_bottom(page: Page) -> None:
    """Scroll incrementally to trigger lazy loading."""
    prev_height = 0
    for _ in range(20):
        await page.evaluate("window.scrollBy(0, document.body.scrollHeight / 5)")
        await _random_delay(400, 900)
        height = await page.evaluate("document.body.scrollHeight")
        if height == prev_height:
            break
        prev_height = height


async def _try_first_selector(page: Page, selectors: list[str]) -> Optional[str]:
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=3000)
            return sel
        except Exception:
            continue
    return None


async def _extract_products_from_page(page: Page) -> list[ScrapedProduct]:
    products: list[ScrapedProduct] = []

    # Scroll to ensure lazy-loaded cards are rendered
    await _scroll_to_bottom(page)

    # Find all product links — the most reliable anchor on FirstCry
    links = await page.query_selector_all("a[href*='product-detail']")
    if not links:
        links = await page.query_selector_all("a[href*='/product-detail']")

    seen_ids: set[str] = set()

    for link in links:
        try:
            href = await link.get_attribute("href")
            if not href:
                continue
            if not href.startswith("http"):
                href = "https://www.firstcry.com" + href

            pid = _extract_product_id(href)
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)

            # Navigate up to the card container for contextual info
            card = await link.evaluate_handle(
                """el => {
                    let n = el;
                    for (let i = 0; i < 8; i++) {
                        n = n.parentElement;
                        if (!n) break;
                        if (n.querySelector('img') && n.querySelector('img') !== el.querySelector('img')) break;
                    }
                    return n || el;
                }"""
            )

            # ── Name ──────────────────────────────────────────────────────
            name_el = await card.query_selector(
                "p[class*='name'], span[class*='name'], h3, h2, "
                "[class*='ProductName'], [class*='product-name'], "
                "[class*='Title'], [class*='title']"
            )
            name = (await name_el.inner_text()).strip() if name_el else ""

            if not name:
                name = (await link.inner_text()).strip()

            if not name or "hot wheel" not in name.lower():
                # Accept it anyway — user searched for hot wheels so all results qualify
                if not name:
                    continue

            # ── Image ─────────────────────────────────────────────────────
            img_el = await card.query_selector("img")
            image_url: Optional[str] = None
            if img_el:
                image_url = await img_el.get_attribute("src") or await img_el.get_attribute("data-src")

            # ── Price ─────────────────────────────────────────────────────
            price: Optional[float] = None
            original_price: Optional[float] = None

            price_el = await card.query_selector(
                "[class*='SalePrice'], [class*='sale-price'], "
                "[class*='DiscountedPrice'], [class*='discounted'], "
                "[class*='Price']:not([class*='Original']):not([class*='Mrp'])"
            )
            mrp_el = await card.query_selector(
                "[class*='MRP'], [class*='mrp'], [class*='OriginalPrice'], "
                "[class*='original-price'], [class*='Mrp']"
            )

            if price_el:
                price = _parse_price(await price_el.inner_text())
            if mrp_el:
                original_price = _parse_price(await mrp_el.inner_text())

            # Fallback: grab first price-like text in card
            if price is None:
                all_text = await card.inner_text()
                m = re.search(r"₹\s*([\d,]+)", all_text)
                if m:
                    price = _parse_price(m.group(1))

            # ── Availability ───────────────────────────────────────────────
            card_text_lower = (await card.inner_text()).lower()
            notify_me = "notify me" in card_text_lower or "out of stock" in card_text_lower
            add_to_cart = "add to cart" in card_text_lower or "buy now" in card_text_lower
            is_available = add_to_cart or (not notify_me)

            products.append(
                ScrapedProduct(
                    product_id=pid,
                    name=name,
                    url=href,
                    price=price,
                    original_price=original_price,
                    image_url=image_url,
                    is_available=is_available,
                    series=_detect_series(name),
                )
            )
        except Exception as exc:
            logger.debug("Error parsing product card: %s", exc)
            continue

    return products


async def _setup_context(browser: Browser) -> BrowserContext:
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 768},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        extra_http_headers={
            "Accept-Language": "en-IN,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        },
    )
    # Mask webdriver flag
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return context


async def _scrape_with_playwright(
    url: str,
    pages_limit: int,
    on_page: Optional[OnPageCallback] = None,
) -> list[ScrapedProduct]:
    """Fallback scraper using a headless Chromium browser (used only when
    the JSON API path can't be resolved). FirstCry's listing paginates via
    client-side infinite scroll — there's no real "next page" URL — so this
    loads the page once and scrolls until no new cards appear, streaming
    newly-revealed cards to `on_page` as they show up."""
    all_products: list[ScrapedProduct] = []
    seen_ids: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await _setup_context(browser)
        page = await context.new_page()

        await page.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in ("media", "font")
                else route.continue_()
            ),
        )

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _random_delay(800, 1500)

            stable_rounds = 0
            max_scrolls = 200 if pages_limit == 0 else max(10, pages_limit * 5)
            for _ in range(max_scrolls):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await _random_delay(500, 1000)

                page_products = await _extract_products_from_page(page)
                new_products = [p for p in page_products if p.product_id not in seen_ids]
                for p in new_products:
                    seen_ids.add(p.product_id)
                all_products.extend(new_products)
                if on_page and new_products:
                    await on_page(new_products)

                if not new_products:
                    stable_rounds += 1
                    if stable_rounds >= 3:  # a few consecutive empty scrolls = reached the end
                        break
                else:
                    stable_rounds = 0

            logger.info("[playwright] Found %d products total", len(all_products))

        except Exception as exc:
            logger.error("Playwright scrape failed: %s", exc)
        finally:
            await context.close()
            await browser.close()

    return all_products



def _fix_url(href: str) -> str:
    """Normalise protocol-relative and relative URLs to absolute https."""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://www.firstcry.com" + href
    return href


_TOTAL_ITEMS_RE = re.compile(r"\(\s*([\d,]+)\s*Items?\s*\)", re.I)


def _detect_total_pages(html: str, items_on_page1: int) -> Optional[int]:
    """Read the '(409 Items)' style header and derive how many pages exist."""
    m = _TOTAL_ITEMS_RE.search(html)
    if not m or items_on_page1 <= 0:
        return None
    total_items = int(m.group(1).replace(",", ""))
    if total_items <= 0:
        return None
    return max(1, math.ceil(total_items / items_on_page1))


def _extract_products_from_html(html: str, base_url: str) -> list[ScrapedProduct]:
    """Parse product cards from raw HTML using BeautifulSoup."""
    soup = BeautifulSoup(html, "lxml")
    products: list[ScrapedProduct] = []
    seen_ids: set[str] = set()

    # Each card is div.li_inner_block with class listingpg-{pid}
    for card in soup.find_all("div", class_=re.compile(r"li_inner_block")):
        try:
            # PID from class name "listingpg-{pid}"
            pid = next(
                (c.replace("listingpg-", "") for c in card.get("class", []) if c.startswith("listingpg-")),
                None,
            )
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)

            # URL
            a = card.find("a", href=re.compile(r"product-detail"))
            if not a:
                continue
            href = _fix_url(a["href"])

            # Name — div.li_txt1 contains the product title
            name_el = card.find("div", class_=re.compile(r"\bli_txt1\b"))
            name = name_el.get_text(strip=True) if name_el else a.get_text(strip=True)
            if not name:
                continue

            # Image — src uses // protocol-relative CDN URL
            img = card.find("img")
            image_url = _fix_url(img["src"]) if img and img.get("src") else None

            # Price — div.rupee text is like "1193.152435(51% Off)"
            # Two prices sometimes appear: sale price then MRP
            price: Optional[float] = None
            original_price: Optional[float] = None
            rupee_el = card.find("div", class_=re.compile(r"\brupee\b"))
            if rupee_el:
                nums = re.findall(r"[\d]+(?:\.\d+)?", rupee_el.get_text())
                if nums:
                    price = float(nums[0])
                if len(nums) >= 2 and float(nums[1]) > float(nums[0]):
                    original_price = float(nums[1])

            # Availability — check for ADD TO CART vs NOTIFY ME button
            btn = card.find("div", class_=re.compile(r"\bbn_btn\b"))
            btn_text = btn.get_text(strip=True).lower() if btn else ""
            is_available = "add to cart" in btn_text or "buy now" in btn_text

            products.append(
                ScrapedProduct(
                    product_id=pid,
                    name=name,
                    url=href,
                    price=price,
                    original_price=original_price,
                    image_url=image_url,
                    is_available=is_available,
                    series=_detect_series(name),
                )
            )
        except Exception as exc:
            logger.debug("BS4 card parse error: %s", exc)
            continue

    return products


# ── Fast path: FirstCry's internal JSON paging API ────────────────────────────
# Discovered by inspecting the XHR calls the site's own infinite-scroll makes:
#   GET /svcs/SearchResult.svc/GetSearchResultProductsPaging?PageNo=..&PageSize=20&...
# It returns exact stock counts per product and the true catalog size, and
# actually paginates (unlike a `?page=N` query string on the listing page,
# which FirstCry ignores server-side).
_PAGING_API_URL = "https://www.firstcry.com/svcs/SearchResult.svc/GetSearchResultProductsPaging"
_CATEGORY_PATH_RE = re.compile(r"/(\d+)/(\d+)/(\d+)(?:[/?]|$)")
_IMAGE_BASE = "https://cdn.fcglcdn.com/brainbees/images/products/219x265/"
_API_PAGE_SIZE = 20


def _slugify(name: str) -> str:
    """Best-effort SEO slug for the product URL. FirstCry redirects to the
    canonical URL from any non-empty slug as long as the product id matches,
    so this only needs to be readable, not exact."""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s/]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "hot-wheels"


def _build_paging_params(page_no: int, cat_id: str, master_brand: str) -> dict:
    params = {
        "PageNo": page_no,
        "PageSize": _API_PAGE_SIZE,
        "SortExpression": "bestseller",
        "OnSale": "",  # empty = full catalog, including out-of-stock items
        "SearchString": "brand",
        "SubCatId": "",
        "BrandId": "",
        "Price": "", "Age": "", "Color": "", "OptionalFilter": "", "OutOfStock": "",
        "combo": "", "discount": "", "searchwithincat": "", "ProductidQstr": "",
        "searchrank": "", "pmonths": "", "cgen": "", "PriceQstr": "", "DiscountQstr": "",
        "sorting": "", "MasterBrand": master_brand, "Rating": "", "Offer": "",
        "skills": "", "material": "", "curatedcollections": "", "measurement": "",
        "gender": "", "exclude": "", "premium": "", "pcode": 0, "isclub": 0,
        "deliverytype": "", "authors": "", "booktype": "", "character": "",
        "collections": "", "format": "", "genre": "", "booklanguage": "",
        "publication": "", "skill": "", "CatId": cat_id,
    }
    for i in range(1, 16):
        params[f"Type{i}"] = ""
    return params


def _map_api_product(raw: dict) -> Optional[ScrapedProduct]:
    pid = str(raw.get("PId") or "").strip()
    name = (raw.get("PNm") or "").strip()
    if not pid or not name:
        return None

    images = [i for i in (raw.get("Images") or "").split(";") if i]
    image_url = _IMAGE_BASE + images[0] if images else None

    def _num(key: str) -> Optional[float]:
        try:
            val = float(raw.get(key))
            return val if val > 0 else None
        except (TypeError, ValueError):
            return None

    price = _num("discprice") or _num("MRP")
    original_price = _num("MRP")

    try:
        stock = int(float(raw.get("CrntStock") or 0))
    except (TypeError, ValueError):
        stock = 0

    url = f"https://www.firstcry.com/hot-wheels/{_slugify(name)}/{pid}/product-detail"

    return ScrapedProduct(
        product_id=pid,
        name=name,
        url=url,
        price=price,
        original_price=original_price,
        image_url=image_url,
        is_available=stock > 0,
        series=_detect_series(name),
        variant_group_id=str(raw.get("P_Grp_ID") or pid),
    )


def _extract_color_variants(html: str) -> list[ScrapedProduct]:
    """Extract the color tiles embedded in a FirstCry product detail page."""
    match = re.search(
        r"CurrentProductDetailJSONColor=(\{.*?\}),CurrentProductDetailJSONOnlyColor=",
        html,
        re.S,
    )
    if not match:
        return []

    try:
        groups = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    variants: list[ScrapedProduct] = []
    seen_ids: set[str] = set()
    for entries in groups.values():
        for raw in entries:
            pid = str(raw.get("pid") or "").strip()
            name = (raw.get("pn") or "").strip()
            if not pid or not name or pid in seen_ids:
                continue
            seen_ids.add(pid)

            images = [item for item in (raw.get("Img") or "").split(";") if item]
            variants.append(
                ScrapedProduct(
                    product_id=pid,
                    name=name,
                    url=f"https://www.firstcry.com/hot-wheels/{_slugify(name)}/{pid}/product-detail",
                    price=float(raw["mrp"]) if raw.get("mrp") else None,
                    original_price=float(raw["mrp"]) if raw.get("mrp") else None,
                    image_url=_IMAGE_BASE + images[0] if images else None,
                    is_available=False,
                    series=_detect_series(name),
                )
            )
    return variants


async def _resolve_category(client: httpx.AsyncClient, search_url: str) -> Optional[tuple[str, str, str]]:
    """Follow the search URL to FirstCry's brand/category listing page and
    pull the CatId/MasterBrand ids the paging API needs out of its path,
    e.g. `/hotwheels/5/0/113` → CatId=5, MasterBrand=113."""
    resp = await client.get(search_url)
    resp.raise_for_status()
    real_url = str(resp.url)
    m = _CATEGORY_PATH_RE.search(httpx.URL(real_url).path)
    if not m:
        return None
    cat_id, _subcat_id, master_brand = m.groups()
    return cat_id, master_brand, real_url


async def _scrape_with_api(
    url: str,
    pages_limit: int,
    on_page: Optional[OnPageCallback] = None,
) -> list[ScrapedProduct]:
    """Fetch the whole catalog concurrently via FirstCry's internal JSON
    paging API — fast, exact stock counts, real pagination."""
    concurrency = max(1, settings.scrape_concurrency)
    headers = {**_BS4_HEADERS, "Accept": "application/json, text/plain, */*", "X-Requested-With": "XMLHttpRequest"}
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    all_products: list[ScrapedProduct] = []
    seen_ids: set[str] = set()
    catalog_by_id: dict[str, ScrapedProduct] = {}
    group_members: dict[str, set[str]] = {}

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20, limits=limits) as client:
        resolved = await _resolve_category(client, url)
        if not resolved:
            logger.info("[api] Could not resolve category ids from %s — skipping API path", url)
            return []
        cat_id, master_brand, real_url = resolved
        client.headers["Referer"] = real_url

        async def fetch_page(n: int) -> tuple[list[ScrapedProduct], int]:
            params = _build_paging_params(n, cat_id, master_brand)
            r = await client.get(_PAGING_API_URL, params=params)
            r.raise_for_status()
            inner = json.loads(r.json()["ProductResponse"])
            total = (inner.get("Count") or [0])[0]
            raw_products = inner.get("Products") or []
            mapped = [p for p in (_map_api_product(rp) for rp in raw_products) if p is not None]
            return mapped, total

        page1_products, total_items = await fetch_page(1)
        logger.info("[api] Page 1 → %d products (catalog size: %d)", len(page1_products), total_items)
        for p in page1_products:
            seen_ids.add(p.product_id)
            catalog_by_id[p.product_id] = p
            group_members.setdefault(p.variant_group_id or p.product_id, set()).add(p.product_id)
        all_products.extend(page1_products)
        if on_page and page1_products:
            await on_page(page1_products)

        detected_pages = max(1, math.ceil(total_items / _API_PAGE_SIZE)) if total_items else None
        if pages_limit > 0:
            total_pages = min(pages_limit, detected_pages) if detected_pages else pages_limit
        else:
            total_pages = detected_pages

        if total_pages and total_pages > 1:
            semaphore = asyncio.Semaphore(concurrency)

            async def bounded_fetch(n: int) -> list[ScrapedProduct]:
                async with semaphore:
                    try:
                        products, _ = await fetch_page(n)
                        logger.info("[api] page %d → %d products", n, len(products))
                        return products
                    except Exception as exc:
                        logger.error("[api] page %d failed: %s", n, exc)
                        return []

            page_numbers = _interleave_top_bottom(list(range(2, total_pages + 1)))
            tasks = [asyncio.create_task(bounded_fetch(n)) for n in page_numbers]
            for coro in asyncio.as_completed(tasks):
                page_products = await coro
                new_products = [p for p in page_products if p.product_id not in seen_ids]
                for p in new_products:
                    seen_ids.add(p.product_id)
                    catalog_by_id[p.product_id] = p
                    group_members.setdefault(p.variant_group_id or p.product_id, set()).add(p.product_id)
                all_products.extend(new_products)
                if on_page and new_products:
                    await on_page(new_products)

        # Detail pages contain the color-variation tiles shown in the user's
        # screenshot. Only request groups that already contain multiple
        # catalog items, avoiding hundreds of redundant detail requests.
        variant_groups = [
            group_id for group_id, members in group_members.items() if len(members) > 1
        ]
        if variant_groups:
            variant_semaphore = asyncio.Semaphore(min(concurrency, 8))

            async def fetch_variants(group_id: str) -> list[ScrapedProduct]:
                source_id = next(iter(group_members[group_id]))
                source = catalog_by_id[source_id]
                async with variant_semaphore:
                    try:
                        response = await client.get(source.url)
                        response.raise_for_status()
                        return _extract_color_variants(response.text)
                    except Exception as exc:
                        logger.debug("[variants] group %s failed: %s", group_id, exc)
                        return []

            variant_tasks = [asyncio.create_task(fetch_variants(group_id)) for group_id in variant_groups]
            variant_products: list[ScrapedProduct] = []
            for task in asyncio.as_completed(variant_tasks):
                for variant in await task:
                    catalog_product = catalog_by_id.get(variant.product_id)
                    if catalog_product:
                        variant.is_available = catalog_product.is_available
                        variant.price = catalog_product.price or variant.price
                        variant.original_price = catalog_product.original_price or variant.original_price
                        variant.image_url = catalog_product.image_url or variant.image_url
                        variant.variant_group_id = catalog_product.variant_group_id
                    if variant.product_id not in seen_ids:
                        seen_ids.add(variant.product_id)
                        variant_products.append(variant)

            if variant_products:
                logger.info("[variants] Discovered %d additional color variants", len(variant_products))
                all_products.extend(variant_products)
                if on_page:
                    await on_page(variant_products)

    valid = [p for p in all_products if p.is_valid()]
    logger.info("[api] %d valid / %d total products across catalog", len(valid), len(all_products))
    return valid


def _interleave_top_bottom(page_numbers: list[int]) -> list[int]:
    """Order pages so a top-down scan and a bottom-up scan run side by side.

    Both directions are dispatched together (as concurrent tasks), so
    whichever half of the catalog responds first gets processed first —
    nothing is left waiting on the slowest page.
    """
    ordered: list[int] = []
    lo, hi = 0, len(page_numbers) - 1
    while lo <= hi:
        ordered.append(page_numbers[lo])
        lo += 1
        if lo <= hi:
            ordered.append(page_numbers[hi])
            hi -= 1
    return ordered


async def _scrape_with_bs4(
    url: str,
    pages_limit: int,
    on_page: Optional[OnPageCallback] = None,
) -> list[ScrapedProduct]:
    """Fetch every page of the catalog concurrently (top-down + bottom-up)
    and stream each page's products to `on_page` the instant it lands, so
    restocks are diffed/notified without waiting for the whole catalog.
    """
    concurrency = max(1, settings.scrape_concurrency)
    all_products: list[ScrapedProduct] = []
    seen_ids: set[str] = set()

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        headers=_BS4_HEADERS, follow_redirects=True, timeout=20, limits=limits
    ) as client:

        # Fetch page 1 first — resolves redirects and tells us the real URL
        # plus (via the "(N Items)" header) how many pages exist in total.
        try:
            resp1 = await client.get(url)
            resp1.raise_for_status()
        except Exception as exc:
            logger.error("[bs4] Failed to fetch %s: %s", url, exc)
            return []

        real_url = str(resp1.url)
        page1_products = _extract_products_from_html(resp1.text, real_url)
        logger.info("[bs4] Page 1 (%s) → %d products", real_url, len(page1_products))

        for p in page1_products:
            seen_ids.add(p.product_id)
        all_products.extend(page1_products)
        if on_page and page1_products:
            await on_page(page1_products)

        def page_url(n: int) -> str:
            sep = "&" if "?" in real_url else "?"
            return real_url if n == 1 else f"{real_url}{sep}page={n}"

        detected_pages = _detect_total_pages(resp1.text, len(page1_products))
        if pages_limit > 0:
            total_pages = min(pages_limit, detected_pages) if detected_pages else pages_limit
        else:
            total_pages = detected_pages  # None if the header couldn't be parsed

        semaphore = asyncio.Semaphore(concurrency)

        async def fetch_page(n: int) -> list[ScrapedProduct]:
            purl = page_url(n)
            async with semaphore:
                try:
                    r = await client.get(purl)
                    r.raise_for_status()
                    products = _extract_products_from_html(r.text, purl)
                    logger.info("[bs4] page %d → %d products", n, len(products))
                    return products
                except Exception as exc:
                    logger.error("[bs4] Failed page %d (%s): %s", n, purl, exc)
                    return []

        async def stream_pages(numbers: list[int]) -> None:
            tasks = [asyncio.create_task(fetch_page(n)) for n in numbers]
            for coro in asyncio.as_completed(tasks):
                page_products = await coro
                new_products = [p for p in page_products if p.product_id not in seen_ids]
                for p in new_products:
                    seen_ids.add(p.product_id)
                all_products.extend(new_products)
                if on_page and new_products:
                    await on_page(new_products)

        if total_pages is not None:
            page_numbers = _interleave_top_bottom(list(range(2, total_pages + 1)))
            await stream_pages(page_numbers)
        else:
            # Couldn't read the item count — probe forward in concurrent
            # batches until a whole batch comes back empty.
            next_page = 2
            max_probe_pages = 60
            while next_page <= max_probe_pages:
                batch = list(range(next_page, min(next_page + concurrency, max_probe_pages + 1) + 1))
                before = len(all_products)
                await stream_pages(batch)
                if len(all_products) == before:
                    break
                next_page = batch[-1] + 1

    valid = [p for p in all_products if p.is_valid()]
    logger.info("[bs4] %d valid / %d total products across catalog", len(valid), len(all_products))
    return valid


async def scrape_hot_wheels(
    search_url: str | None = None,
    max_pages: int | None = None,
    on_page: Optional[OnPageCallback] = None,
) -> list[ScrapedProduct]:
    """
    Main entry point. Tries FirstCry's internal JSON paging API first (fast,
    exact stock counts, real pagination across the whole catalog). Falls
    back to a Playwright browser (scrolls to trigger the same infinite-load)
    if the API path can't be resolved, and finally to a single-page
    BeautifulSoup fetch as a last resort.

    `on_page`, if given, is awaited with each page's products as soon as
    that page is fetched — enabling instant diff/notify instead of waiting
    for the entire catalog to be scraped.
    """
    url = search_url or settings.firstcry_search_url
    pages_limit = max_pages if max_pages is not None else settings.max_pages

    try:
        products = await _scrape_with_api(url, pages_limit, on_page=on_page)
        if products:
            return products
        logger.warning("[api] Returned 0 products — falling back to Playwright")
    except Exception as exc:
        logger.warning("JSON API scraper failed (%s) — falling back to Playwright", str(exc)[:80])

    try:
        products = await _scrape_with_playwright(url, pages_limit, on_page=on_page)
        if products:
            logger.info("Playwright scrape complete. Products: %d", len(products))
            return products
        logger.warning("[playwright] Returned 0 products — falling back to BeautifulSoup")
    except Exception as pw_exc:
        logger.warning("Playwright fallback failed (%s) — falling back to BeautifulSoup", str(pw_exc)[:80])

    try:
        return await _scrape_with_bs4(url, pages_limit, on_page=on_page)
    except Exception as exc:
        logger.error("All scraping strategies failed: %s", exc)
        return []

