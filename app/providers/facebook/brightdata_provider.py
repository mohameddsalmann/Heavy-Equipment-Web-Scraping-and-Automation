from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus
import httpx
import structlog

from app.config import settings
from app.providers.facebook.base import BaseFacebookProvider
from app.providers.facebook.models import FacebookRegion
from app.schemas.listing import RawListing

logger = structlog.get_logger(__name__)


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BrightDataFacebookProvider(BaseFacebookProvider):
    """
    Facebook Marketplace provider powered by Bright Data Web Scraper API.
    Provides vendor interchangeability with Apify via the BaseFacebookProvider contract.
    """

    BRIGHTDATA_BASE_URL: str = "https://api.brightdata.com/datasets/v3"

    def __init__(
        self,
        api_key: Optional[str] = None,
        dataset_id: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
        max_concurrency: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
    ) -> None:
        super().__init__(max_concurrency=max_concurrency, timeout_seconds=timeout_seconds)
        self.api_key = api_key or settings.BRIGHTDATA_API_KEY
        self.dataset_id = dataset_id or settings.BRIGHTDATA_DATASET_ID
        self._external_client = client
        self._client: Optional[httpx.AsyncClient] = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=float(self.timeout_seconds))
        return self._client

    async def search_region(
        self, query: str, region: FacebookRegion, max_results: int = 20
    ) -> List[RawListing]:
        """Queries Bright Data dataset API for the search query and region."""
        if not self.api_key or not self.dataset_id:
            logger.warning(
                "brightdata_credentials_not_configured",
                region_id=region.id,
                query=query,
            )
            return []

        client = await self._get_client()
        search_target_url = f"{region.search_url}?query={quote_plus(query)}"

        endpoint = f"{self.BRIGHTDATA_BASE_URL}/trigger?dataset_id={self.dataset_id}&include_errors=true"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = [
            {
                "url": search_target_url,
                "country": region.country,
                "limit": max_results,
            }
        ]

        logger.info(
            "brightdata_search_started",
            query=query,
            region=region.name,
            country=region.country,
        )

        try:
            response = await client.post(endpoint, json=payload, headers=headers)
            if response.status_code not in (200, 201):
                logger.error(
                    "brightdata_api_error",
                    status_code=response.status_code,
                    response_text=response.text[:300],
                )
                return []

            items = response.json()
            if not isinstance(items, list):
                return []
        except Exception as exc:
            logger.error("brightdata_request_failed", error=str(exc))
            return []

        listings: List[RawListing] = []
        for item in items:
            if isinstance(item, dict):
                norm = self.normalize_item(item, region=region)
                if norm is not None:
                    listings.append(norm)

        return listings

    def normalize_item(
        self, item: Dict[str, Any], region: Optional[FacebookRegion] = None
    ) -> Optional[RawListing]:
        """Normalizes Bright Data dataset item into a canonical RawListing."""
        raw_id = str(item.get("id") or item.get("post_id") or item.get("item_id") or "").strip()
        url_val = item.get("url") or item.get("post_url") or ""

        if not raw_id and url_val:
            match = re.search(r"/marketplace/item/(\d+)", url_val)
            if match:
                raw_id = match.group(1)

        if not raw_id:
            return None

        if not url_val:
            url_val = f"https://www.facebook.com/marketplace/item/{raw_id}"

        title = str(item.get("title") or item.get("header") or "").strip()
        if not title:
            return None

        # Price & currency
        raw_price = item.get("price") or item.get("final_price")
        price_dec: Optional[Decimal] = None
        currency_code: str = (
            item.get("currency")
            or ("CAD" if (region and region.country == "CA") else "USD")
        ).upper()

        if raw_price is not None:
            clean_num = re.sub(r"[^\d.]", "", str(raw_price))
            if clean_num:
                try:
                    price_dec = Decimal(clean_num)
                except Exception:
                    price_dec = None

        location = item.get("location") or (f"{region.name}, {region.country}" if region else None)
        description = item.get("description")
        image_url = item.get("image") or item.get("image_url")
        seller_name = item.get("seller_name")

        try:
            return RawListing(
                source=self.source,
                external_id=raw_id,
                title=title,
                description=description,
                price=price_dec,
                currency=currency_code,
                location=location,
                url=url_val,
                image_url=image_url,
                seller_name=seller_name,
                scraped_at=get_utc_now(),
            )
        except Exception as exc:
            logger.warning("brightdata_listing_validation_failed", error=str(exc))
            return None

    async def close(self) -> None:
        """Close HTTP client session."""
        if self._client and not self._external_client:
            await self._client.aclose()
            self._client = None
