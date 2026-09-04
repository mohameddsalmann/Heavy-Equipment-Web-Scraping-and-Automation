from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from typing import Any, Dict, List, Optional, Set
import structlog

from app.config import load_watchlist, settings
from app.providers.base import BaseProvider
from app.providers.facebook.models import FacebookRegion, load_facebook_regions
from app.schemas.listing import RawListing

logger = structlog.get_logger(__name__)


class BaseFacebookProvider(BaseProvider, ABC):
    """
    Abstract base provider for Facebook Marketplace integrations.
    Supports managed public-data scraping vendors (Apify, Bright Data)
    with concurrent regional grid searches, isolated failures, and in-run deduplication.
    """

    def __init__(
        self,
        max_concurrency: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
    ) -> None:
        self.max_concurrency = max_concurrency or settings.FACEBOOK_MAX_CONCURRENCY
        self.timeout_seconds = timeout_seconds or settings.FACEBOOK_REQUEST_TIMEOUT_SECONDS
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    @property
    def source(self) -> str:
        return "facebook_marketplace"

    @abstractmethod
    async def search_region(
        self, query: str, region: FacebookRegion, max_results: int = 20
    ) -> List[RawListing]:
        """Search Facebook Marketplace in a specific region for a query."""
        pass

    @abstractmethod
    def normalize_item(
        self, item: Dict[str, Any], region: Optional[FacebookRegion] = None
    ) -> Optional[RawListing]:
        """Normalize vendor-specific JSON/payload into a canonical RawListing."""
        pass

    async def fetch_latest(
        self,
        queries: Optional[List[str]] = None,
        regions: Optional[List[FacebookRegion]] = None,
        max_results_per_query: int = 20,
    ) -> List[RawListing]:
        """
        High-throughput concurrent discovery across configured US/Canada regions.
        Executes bounded concurrent queries, isolates individual regional errors,
        and deduplicates listing IDs inside the provider run.
        """
        # Load default queries from watchlist if not supplied
        if not queries:
            wl = load_watchlist()
            queries = wl.search_queries or ["CAT wheel loader", "CASE 580"]

        # Load active regions from grid config if not supplied
        if not regions:
            regions = load_facebook_regions()
            if not regions:
                logger.warning("no_facebook_regions_configured_using_defaults")
                regions = [
                    FacebookRegion(
                        id="us_texas",
                        name="Texas",
                        country="US",
                        location_id="dallas",
                        search_url="https://www.facebook.com/marketplace/dallas/search",
                        enabled=True,
                        priority=1,
                    )
                ]

        logger.info(
            "facebook_discovery_started",
            query_count=len(queries),
            region_count=len(regions),
            concurrency_limit=self.max_concurrency,
        )

        async def _bounded_search(query: str, region: FacebookRegion) -> List[RawListing]:
            async with self._semaphore:
                try:
                    return await asyncio.wait_for(
                        self.search_region(
                            query=query,
                            region=region,
                            max_results=max_results_per_query,
                        ),
                        timeout=float(self.timeout_seconds),
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "facebook_region_search_timeout",
                        query=query,
                        region_id=region.id,
                        region_name=region.name,
                        timeout=self.timeout_seconds,
                    )
                    return []
                except Exception as exc:
                    logger.warning(
                        "facebook_region_search_failed",
                        query=query,
                        region_id=region.id,
                        region_name=region.name,
                        error=str(exc),
                    )
                    return []

        tasks = [
            _bounded_search(query=q, region=r)
            for q in queries
            for r in regions
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        deduped_listings: List[RawListing] = []
        seen_ids: Set[str] = set()

        for res in results:
            if isinstance(res, list):
                for listing in res:
                    if listing.external_id not in seen_ids:
                        seen_ids.add(listing.external_id)
                        deduped_listings.append(listing)
            elif isinstance(res, Exception):
                logger.error("facebook_task_unhandled_exception", error=str(res))

        logger.info(
            "facebook_discovery_completed",
            raw_count=sum(len(r) for r in results if isinstance(r, list)),
            deduped_count=len(deduped_listings),
            unique_ids=len(seen_ids),
        )
        return deduped_listings

    async def search(self, query: str, max_pages: int = 1) -> List[RawListing]:
        """Implements BaseProvider interface by querying latest listings for the query."""
        return await self.fetch_latest(queries=[query], max_results_per_query=max_pages * 20)

    def parse_page(self, html: str, base_url: str = "") -> List[RawListing]:
        """Not directly used for managed API providers."""
        return []

    async def fetch_page(self, url: str) -> str:
        """Not directly used for managed API providers."""
        return ""

    async def close(self) -> None:
        """Clean up HTTP client sessions or connections."""
        pass
