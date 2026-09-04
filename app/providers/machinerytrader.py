from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup, Tag
import structlog
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from app.providers.base import BaseProvider
from app.schemas.listing import RawListing

logger = structlog.get_logger(__name__)


class MachineryTraderProvider(BaseProvider):
    """
    Marketplace provider for MachineryTrader (machinerytrader.com).
    Uses Playwright for dynamic Cloudflare-resilient retrieval and BeautifulSoup
    for multi-strategy extraction (combining Schema.org JSON-LD and HTML DOM cards).
    """

    BASE_URL: str = "https://www.machinerytrader.com"
    SEARCH_PATH: str = "/listings/for-sale/search"

    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int = 30000,
        user_agent: Optional[str] = None,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.user_agent = (
            user_agent
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    @property
    def source(self) -> str:
        return "machinerytrader"

    async def _ensure_browser(self) -> BrowserContext:
        """Lazily initialize Playwright browser and stealth-oriented context."""
        if self._context is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            self._context = await self._browser.new_context(
                user_agent=self.user_agent,
                viewport={"width": 1366, "height": 768},
                locale="en-US",
                timezone_id="America/New_York",
            )
        return self._context

    async def fetch_page(self, url: str) -> str:
        """Fetch page HTML using Playwright context with anti-bot resistance."""
        context = await self._ensure_browser()
        page = await context.new_page()
        try:
            logger.info("machinerytrader_fetch_started", url=url)
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            # Brief pause to allow hydration and anti-bot challenge clearance
            await page.wait_for_timeout(2000)

            status_code = response.status if response else None
            logger.info("machinerytrader_fetch_completed", url=url, status=status_code)
            html = await page.content()
            return html
        finally:
            await page.close()

    def parse_page(self, html: str, base_url: str = "") -> List[RawListing]:
        """
        Parse raw HTML content into standardized RawListing instances.
        Utilizes a dual strategy:
        1. Parse structured JSON-LD (Schema.org Product & Offer specs).
        2. Parse and correlate DOM cards (.listing-card-grid) to extract
           future cross-source deduplication attributes (serial number, seller name).
        """
        if not html:
            return []

        resolved_base = base_url or self.BASE_URL
        soup = BeautifulSoup(html, "html.parser")
        items_by_id: Dict[str, Dict[str, Any]] = {}

        # 1. Strategy A: Schema.org Product / Offer JSON-LD scripts
        self._parse_json_ld(soup, resolved_base, items_by_id)

        # 2. Strategy B: DOM cards (.listing-card-grid / .ListingParentElement)
        self._parse_dom_cards(soup, resolved_base, items_by_id)

        # 3. Build and validate RawListing instances
        listings: List[RawListing] = []
        for ext_id, item_dict in items_by_id.items():
            try:
                listing = RawListing(**item_dict)
                listings.append(listing)
            except Exception as e:
                logger.warning(
                    "machinerytrader_listing_validation_failed",
                    external_id=ext_id,
                    error=str(e),
                )

        logger.info(
            "machinerytrader_parsed_listings",
            count=len(listings),
            source=self.source,
        )
        return listings

    def _parse_json_ld(
        self,
        soup: BeautifulSoup,
        base_url: str,
        items_by_id: Dict[str, Dict[str, Any]],
    ) -> None:
        """Extract structured listings from application/ld+json scripts."""
        for script_tag in soup.find_all("script", type="application/ld+json"):
            content = script_tag.string
            if not content:
                continue
            try:
                data = json.loads(content)
            except Exception:
                continue

            # Support single dictionary or list of schemas
            schema_items = data if isinstance(data, list) else [data]
            for schema in schema_items:
                if not isinstance(schema, dict):
                    continue

                # Look for Product schema containing offers
                if schema.get("@type") == "Product" and "offers" in schema:
                    offers_obj = schema.get("offers", {})
                    offers_list = (
                        offers_obj.get("offers", [])
                        if isinstance(offers_obj, dict)
                        else []
                    )
                    for offer in offers_list:
                        if not isinstance(offer, dict):
                            continue
                        item = offer.get("itemOffered", {})
                        if not isinstance(item, dict):
                            item = {}

                        raw_id = str(item.get("@id") or offer.get("@id") or "").strip()
                        raw_href = offer.get("url") or item.get("url") or ""
                        if not raw_href and not raw_id:
                            continue

                        full_url = urljoin(base_url, raw_href) if raw_href else ""
                        if not raw_id and full_url:
                            id_match = re.search(r"/(\d+)(?:[/?#]|$)", full_url)
                            raw_id = id_match.group(1) if id_match else full_url

                        name = item.get("name") or offer.get("name") or schema.get("name") or ""
                        desc = item.get("description") or offer.get("description") or ""

                        # Extract price
                        price_num = offer.get("price")
                        price_dec: Optional[Decimal] = None
                        if price_num is not None:
                            try:
                                price_dec = Decimal(str(price_num))
                            except Exception:
                                price_dec = None

                        currency = offer.get("priceCurrency") or "USD"
                        image = item.get("image") or offer.get("image")

                        # Extract location from address
                        location: Optional[str] = None
                        addr = offer.get("availableAtOrFrom", {}).get("address", {})
                        if isinstance(addr, dict):
                            parts = [
                                addr.get("addressLocality"),
                                addr.get("addressRegion"),
                                addr.get("addressCountry"),
                            ]
                            loc_str = ", ".join([str(p).strip() for p in parts if p])
                            location = loc_str or None

                        if raw_id:
                            items_by_id[raw_id] = {
                                "source": self.source,
                                "external_id": raw_id,
                                "title": name,
                                "description": desc or None,
                                "price": price_dec,
                                "currency": currency,
                                "location": location,
                                "url": full_url,
                                "image_url": image or None,
                            }

    def _parse_dom_cards(
        self,
        soup: BeautifulSoup,
        base_url: str,
        items_by_id: Dict[str, Dict[str, Any]],
    ) -> None:
        """Extract or enrich listings from HTML listing cards."""
        cards = soup.find_all(class_=["listing-card-grid", "ListingParentElement"])
        if not cards:
            cards = soup.find_all(class_=re.compile(r"listing-card-grid|ListingParentElement"))

        for card in cards:
            if not isinstance(card, Tag):
                continue

            link = card.find("a", class_=["list-listing-title-link", "view-listing-details-link"])
            if not link or not isinstance(link, Tag):
                # Fallback: any link pointing to /listing/for-sale/
                link = card.find("a", href=re.compile(r"/listing/for-sale/\d+"))
                if not link or not isinstance(link, Tag):
                    continue

            href = str(link.get("href", "")).strip()
            if not href:
                continue

            full_url = urljoin(base_url, href)

            # Derive external ID from URL pattern
            id_match = re.search(r"/(?:listing/for-sale/|auctions/online/)(\d+)", href)
            ext_id = id_match.group(1) if id_match else None
            if not ext_id:
                # Fallback numeric matcher
                num_match = re.search(r"/(\d+)(?:[/?#]|$)", href)
                ext_id = num_match.group(1) if num_match else full_url

            title = link.get_text(" ", strip=True)
            if not title:
                title_elem = card.find(class_=["listing-portion-title", "listing-title"])
                if title_elem:
                    title = title_elem.get_text(" ", strip=True)

            # Extract specs (serial number, hours, description)
            serial_number: Optional[str] = None
            description: Optional[str] = None
            specs_divs = card.find_all(class_=["list-spec", "spec"])
            for spec_div in specs_divs:
                lbl = spec_div.find(class_=["spec-label"])
                val = spec_div.find(class_=["spec-value"])
                if lbl and val:
                    key = lbl.get_text(" ", strip=True).rstrip(":").strip().lower()
                    val_str = val.get_text(" ", strip=True)
                    if key == "serial number":
                        serial_number = val_str
                    elif key == "description":
                        description = val_str

            # Fallback regex for serial number from entire card text
            if not serial_number:
                card_text = card.get_text(separator=" | ", strip=True)
                sn_match = re.search(r"Serial Number\s*[:\|]\s*([A-Za-z0-9\-_]+)", card_text)
                if sn_match:
                    serial_number = sn_match.group(1).strip()

            # Extract Seller
            seller_name: Optional[str] = None
            seller_elem = card.find(class_=["seller"]) or card.find(class_=["dealer-name", "company-name"])
            if seller_elem:
                seller_text = seller_elem.get_text(strip=True)
                seller_name = re.sub(r"^Seller\s*:\s*", "", seller_text, flags=re.IGNORECASE).strip() or None

            # Extract Location
            location: Optional[str] = None
            loc_elem = card.find(class_=["machine-location"])
            if loc_elem:
                loc_text = loc_elem.get_text(strip=True)
                location = re.sub(r"^Location\s*:\s*", "", loc_text, flags=re.IGNORECASE).strip() or None

            # Extract Price & Currency
            price_dec: Optional[Decimal] = None
            currency_code = "USD"
            price_elem = card.find(class_=["price", "retail-price-container", "price-contain"])
            if price_elem:
                p_text = price_elem.get_text(strip=True)
                p_match = re.search(r"([A-Z]{3})?\s*\$([\d,]+(?:\.\d{2})?)", p_text)
                if p_match:
                    if p_match.group(1):
                        currency_code = p_match.group(1).upper()
                    raw_num = p_match.group(2).replace(",", "")
                    try:
                        price_dec = Decimal(raw_num)
                    except Exception:
                        price_dec = None

            # Extract Image URL
            image_url: Optional[str] = None
            img_elem = card.find("img", class_=["listing-main-image", "listing-image"]) or card.find("img")
            if img_elem and isinstance(img_elem, Tag):
                image_url = (
                    img_elem.get("src")
                    or img_elem.get("data-uc-src")
                    or img_elem.get("data-src")
                )

            # Correlate or insert
            if ext_id in items_by_id:
                existing = items_by_id[ext_id]
                if serial_number and not existing.get("serial_number"):
                    existing["serial_number"] = serial_number
                if seller_name and not existing.get("seller_name"):
                    existing["seller_name"] = seller_name
                if location and not existing.get("location"):
                    existing["location"] = location
                if description and not existing.get("description"):
                    existing["description"] = description
                if price_dec is not None and existing.get("price") is None:
                    existing["price"] = price_dec
                    existing["currency"] = currency_code
                if image_url and not existing.get("image_url"):
                    existing["image_url"] = image_url
            else:
                items_by_id[ext_id] = {
                    "source": self.source,
                    "external_id": ext_id,
                    "title": title,
                    "description": description,
                    "price": price_dec,
                    "currency": currency_code,
                    "location": location,
                    "url": full_url,
                    "image_url": image_url,
                    "serial_number": serial_number,
                    "seller_name": seller_name,
                }

    async def search(self, query: str, max_pages: int = 1) -> List[RawListing]:
        """Search MachineryTrader across given query up to max_pages."""
        all_listings: List[RawListing] = []
        seen_ids = set()

        for page in range(1, max_pages + 1):
            url = f"{self.BASE_URL}{self.SEARCH_PATH}?keywords={quote_plus(query)}"
            if page > 1:
                url += f"&Page={page}"

            logger.info("machinerytrader_searching", query=query, page=page, url=url)
            html = await self.fetch_page(url)
            page_listings = self.parse_page(html, base_url=self.BASE_URL)

            for item in page_listings:
                if item.external_id not in seen_ids:
                    seen_ids.add(item.external_id)
                    all_listings.append(item)

        return all_listings

    async def close(self) -> None:
        """Close browser resources and terminate Playwright."""
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("machinerytrader_provider_closed")
