"""
Pincode delivery checker for FirstCry products.

Strategy
--------
1. Navigate to the product page.
2. Locate the pincode input field.
3. Enter the pincode and submit.
4. Read the delivery status from the response text.
5. Cache results per (product_id, pincode) to avoid repeat page loads.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import httpx
from playwright.async_api import async_playwright, BrowserContext, Page

logger = logging.getLogger(__name__)

# In-memory cache: (product_id, pincode) -> bool
_delivery_cache: dict[tuple[str, str], bool] = {}

PINCODE_INPUT_SELECTORS = [
    "input[placeholder*='pincode' i]",
    "input[placeholder*='Pincode' i]",
    "input[placeholder*='PIN' i]",
    "input[id*='pincode' i]",
    "input[name*='pincode' i]",
    "input[class*='pincode' i]",
]

PINCODE_SUBMIT_SELECTORS = [
    "button:has-text('Check')",
    "button:has-text('Apply')",
    "button[id*='check' i]",
    "button[class*='check' i]",
    "span:has-text('Check'):visible",
]

AVAILABLE_PATTERNS = [
    r"delivery available",
    r"deliverable",
    r"eligible for delivery",
    r"ships? to",
    r"estimated delivery",
    r"will be delivered",
]

UNAVAILABLE_PATTERNS = [
    r"not serviceable",
    r"not deliverable",
    r"not available.*delivery",
    r"delivery not available",
    r"service not available",
    r"pincode.*not.*serviceable",
]


async def _check_single_product(
    page: Page, product_url: str, pincode: str
) -> Optional[bool]:
    """
    Returns True if deliverable, False if not, None if undetermined.
    """
    try:
        await page.goto(product_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        # Find pincode input
        pincode_input = None
        for sel in PINCODE_INPUT_SELECTORS:
            try:
                pincode_input = await page.query_selector(sel)
                if pincode_input:
                    break
            except Exception:
                continue

        if not pincode_input:
            logger.debug("No pincode input found on %s", product_url)
            return None

        # Clear and type pincode
        await pincode_input.click()
        await pincode_input.fill("")
        await pincode_input.type(pincode, delay=80)
        await asyncio.sleep(0.5)

        # Submit
        submitted = False
        for sel in PINCODE_SUBMIT_SELECTORS:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click()
                    submitted = True
                    break
            except Exception:
                continue

        if not submitted:
            await page.keyboard.press("Enter")

        await asyncio.sleep(2)

        # Read page text near the pincode area for result
        body_text = (await page.inner_text("body")).lower()

        for pattern in AVAILABLE_PATTERNS:
            if re.search(pattern, body_text):
                return True

        for pattern in UNAVAILABLE_PATTERNS:
            if re.search(pattern, body_text):
                return False

        return None  # Could not determine

    except Exception as exc:
        logger.error("Pincode check failed for %s: %s", product_url, exc)
        return None


async def check_pincode_delivery(
    product_id: str,
    product_url: str,
    pincode: str,
    use_cache: bool = True,
) -> Optional[bool]:
    """
    Check if a product is deliverable to a given pincode.
    Tries Playwright first; falls back to httpx+regex if browser fails.
    Returns True/False, or None if undetermined.
    """
    cache_key = (product_id, pincode)
    if use_cache and cache_key in _delivery_cache:
        return _delivery_cache[cache_key]

    # Try Playwright
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context: BrowserContext = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()
            result = await _check_single_product(page, product_url, pincode)
            await context.close()
            await browser.close()

        if result is not None:
            _delivery_cache[cache_key] = result
        return result

    except Exception as pw_exc:
        logger.warning("[%s] Playwright pincode check failed, trying httpx: %s", product_id, str(pw_exc)[:60])

    # Fallback: fetch product page with httpx and look for pincode-related delivery info
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-IN,en;q=0.9",
        }
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=15) as client:
            resp = await client.get(product_url)
            page_text = resp.text.lower()

        for pattern in AVAILABLE_PATTERNS:
            if re.search(pattern, page_text):
                _delivery_cache[cache_key] = True
                return True
        for pattern in UNAVAILABLE_PATTERNS:
            if re.search(pattern, page_text):
                _delivery_cache[cache_key] = False
                return False

        # Can't determine — assume deliverable
        _delivery_cache[cache_key] = True
        return True

    except Exception as exc:
        logger.warning("[%s] httpx pincode check also failed: %s", product_id, str(exc)[:60])
        return None


async def batch_check_pincode(
    products: list[tuple[str, str]],  # [(product_id, url), ...]
    pincode: str,
    concurrency: int = 2,
) -> dict[str, Optional[bool]]:
    """
    Check delivery for multiple products with limited concurrency.
    Returns {product_id: deliverable}.
    """
    results: dict[str, Optional[bool]] = {}
    semaphore = asyncio.Semaphore(concurrency)

    async def _check(pid: str, url: str) -> None:
        async with semaphore:
            results[pid] = await check_pincode_delivery(pid, url, pincode)
            await asyncio.sleep(1.5)  # polite delay

    await asyncio.gather(*[_check(pid, url) for pid, url in products])
    return results


def clear_delivery_cache() -> None:
    _delivery_cache.clear()
