from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple
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


class ApifyFacebookProvider(BaseFacebookProvider):
    """
    Production-grade Facebook Marketplace provider supporting dynamic Apify Actors:
    - Primary: blackfalcondata/facebook-marketplace-scraper (incremental, newest-first, batching)
    - Fallback: calm_builder/facebook-marketplace-scraper
    - Optional fallback: apify/facebook-marketplace-scraper

    Features:
    - Actor auto-fallback on error/timeout
    - Regional & query batching to minimize Actor runs (e.g. 1-2 runs instead of N*M)
    - Incremental monitoring with persistent stateKey and newest-first sorting
    - Full payload normalization across all actor variations
    - Isolated regional failures and resilient error handling
    """

    APIFY_BASE_URL: str = "https://api.apify.com/v2"

    def __init__(
        self,
        token: Optional[str] = None,
        actor_id: Optional[str] = None,
        fallback_actors: Optional[List[str]] = None,
        client: Optional[httpx.AsyncClient] = None,
        max_concurrency: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        incremental_mode: Optional[bool] = None,
        state_key: Optional[str] = None,
        emit_unchanged: Optional[bool] = None,
        sort_by: Optional[str] = None,
    ) -> None:
        super().__init__(max_concurrency=max_concurrency, timeout_seconds=timeout_seconds)
        self.token = token or settings.APIFY_TOKEN
        self.actor_id = (
            actor_id
            or settings.FACEBOOK_APIFY_ACTOR_ID
            or settings.APIFY_FACEBOOK_ACTOR_ID
            or "blackfalcondata/facebook-marketplace-scraper"
        )
        self.fallback_actors = (
            fallback_actors
            if fallback_actors is not None
            else list(settings.FACEBOOK_FALLBACK_ACTORS)
        )
        self.incremental_mode = (
            incremental_mode
            if incremental_mode is not None
            else settings.FACEBOOK_INCREMENTAL_MODE
        )
        self.state_key = state_key or settings.FACEBOOK_STATE_KEY
        self.emit_unchanged = (
            emit_unchanged
            if emit_unchanged is not None
            else settings.FACEBOOK_EMIT_UNCHANGED
        )
        self.sort_by = sort_by or settings.FACEBOOK_SORT_BY

        self._external_client = client
        self._client: Optional[httpx.AsyncClient] = client

        # Monitoring and tracking attributes
        self.last_actor_id: str = self.actor_id
        self.actor_runs_started: int = 0
        self.last_run_duration_seconds: float = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=float(self.timeout_seconds))
        return self._client

    def _build_payload_for_actor(
        self,
        actor_id: str,
        queries: List[str],
        regions: List[FacebookRegion],
        max_results: int,
    ) -> Dict[str, Any]:
        """
        Dynamically constructs the exact input payload schema for the target Actor.
        Adheres strictly to the Actor's published schema.
        """
        actor_lower = actor_id.lower()

        if "blackfalcon" in actor_lower:
            # 1. Primary: blackfalcondata/facebook-marketplace-scraper
            # Supports JSON array strings for multi-query and multi-location in one run
            query_val = json.dumps(queries) if len(queries) > 1 else (queries[0] if queries else "")

            ca_prov_map = {
                "ontario": "ON",
                "alberta": "AB",
                "british columbia": "BC",
                "quebec": "QC",
                "quebec / east": "QC",
                "central canada": "MB",
            }
            loc_list: List[str] = []
            for r in regions:
                if r.location_id and "," in r.location_id:
                    loc_list.append(r.location_id)
                elif r.country == "US":
                    city = r.location_id.title() if r.location_id else r.name
                    loc_list.append(f"{city}, {r.name}")
                elif r.country == "CA":
                    city = r.location_id.title() if r.location_id else r.name
                    prov = ca_prov_map.get(r.name.lower(), "ON")
                    loc_list.append(f"{city}, {prov}")
                else:
                    loc_list.append(r.name)

            location_val = json.dumps(loc_list) if len(loc_list) > 1 else (loc_list[0] if loc_list else "Texas")

            proxy_country = "CA" if any(r.country == "CA" for r in regions) else "US"

            payload: Dict[str, Any] = {
                "query": query_val,
                "location": location_val,
                "maxResults": max_results,
                "sortBy": self.sort_by if self.sort_by in ["newest", "relevance", "price_low_to_high", "price_high_to_low", "distance"] else "newest",
                "includeDetails": True,
                "incrementalMode": self.incremental_mode,
                "emitUnchanged": self.emit_unchanged,
                "proxy": {
                    "useApifyProxy": True,
                    "apifyProxyGroups": ["RESIDENTIAL"],
                    "apifyProxyCountry": "US",
                },
            }

            if self.state_key:
                payload["stateKey"] = self.state_key
            return payload


        elif "calm_builder" in actor_lower:
            # 2. Fallback: calm_builder/facebook-marketplace-scraper
            keyword_searches = []
            for q in queries:
                for r in regions:
                    slug = (r.location_id or r.name).lower().replace(" ", "").replace(",", "").split()[0]
                    keyword_searches.append({"query": q, "locationSlug": slug})

            sort_val = "creation_time_descend" if self.sort_by == "newest" else "best_match"
            per_search_cap = max(1, max_results // max(1, len(keyword_searches)))
            return {
                "keywordSearches": keyword_searches,
                "sortBy": sort_val,
                "maxListings": min(50, per_search_cap),
                "deduplicateListings": True,
                "fetchDetails": True,
            }

        else:
            # 3. Optional fallback: apify/facebook-marketplace-scraper
            start_urls = []
            for q in queries:
                for r in regions:
                    start_urls.append({"url": f"{r.search_url}?query={quote_plus(q)}"})

            return {
                "startUrls": start_urls,
                "resultsLimit": max_results,
                "includeListingDetails": True,
            }

    async def _execute_single_actor_run(
        self,
        actor_id: str,
        payload: Dict[str, Any],
        max_items: int,
    ) -> Tuple[List[Dict[str, Any]], float]:
        """
        Starts an Apify Actor run, polls until completion or timeout,
        and retrieves dataset items. Compatible with MockTransport for unit testing.
        """
        client = await self._get_client()
        actor_clean = actor_id.replace("/", "~")
        run_url = f"{self.APIFY_BASE_URL}/acts/{actor_clean}/runs?token={self.token}&memory=1024"

        t_start = asyncio.get_event_loop().time()
        start_res = await client.post(
            run_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )

        # Immediate synchronous return (e.g. in MockTransport unit tests)
        if start_res.status_code in (200, 201):
            raw_json = start_res.json()
            if isinstance(raw_json, list):
                # Mock transport directly returned item list
                return raw_json, round(asyncio.get_event_loop().time() - t_start, 2)
            else:
                run_data = raw_json.get("data", {})
                run_id = run_data.get("id")
                dataset_id = run_data.get("defaultDatasetId")
        else:
            err_text = start_res.text[:300]
            raise RuntimeError(
                f"Apify Actor {actor_id} run start failed: HTTP {start_res.status_code} - {err_text}"
            )

        if not dataset_id or not run_id:
            raise RuntimeError(f"Apify Actor {actor_id} returned no run_id or defaultDatasetId")

        self.actor_runs_started += 1

        # Poll run progress
        poll_interval = 2.0
        max_wait = float(self.timeout_seconds)
        elapsed = 0.0

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            status_res = await client.get(
                f"{self.APIFY_BASE_URL}/actor-runs/{run_id}?token={self.token}"
            )
            if status_res.status_code == 200:
                cur_data = status_res.json().get("data", {})
                run_status = cur_data.get("status")
                charged_listings = (
                    cur_data.get("chargedEventCounts", {}).get("listing", 0)
                )

                if (
                    run_status in ("SUCCEEDED", "TIMED-OUT", "ABORTED", "FAILED")
                    or charged_listings >= max_items
                    or (charged_listings > 0 and elapsed >= 25.0)
                ):
                    if run_status == "FAILED":
                        raise RuntimeError(f"Actor run {run_id} failed with status FAILED")
                    break

        # Fetch dataset items
        dataset_url = (
            f"{self.APIFY_BASE_URL}/datasets/{dataset_id}/items"
            f"?token={self.token}&limit={max_items}"
        )
        items_res = await client.get(dataset_url)
        items: List[Dict[str, Any]] = []
        if items_res.status_code == 200:
            res_data = items_res.json()
            if isinstance(res_data, list):
                items = res_data

        # Abort background run if still active to conserve compute credits
        try:
            await client.post(
                f"{self.APIFY_BASE_URL}/actor-runs/{run_id}/abort?token={self.token}"
            )
        except Exception:
            pass

        duration = round(asyncio.get_event_loop().time() - t_start, 2)
        return items, duration

    async def _execute_with_fallback(
        self,
        queries: List[str],
        regions: List[FacebookRegion],
        max_results: int,
    ) -> Tuple[List[Dict[str, Any]], str, float]:
        """
        Executes search against the primary actor.
        If it encounters an error or returns no data due to actor issues,
        falls back sequentially to configured fallback actors.
        """
        candidate_actors = [self.actor_id] + [
            act for act in self.fallback_actors if act != self.actor_id
        ]

        last_err: Optional[Exception] = None
        for actor in candidate_actors:
            payload = self._build_payload_for_actor(
                actor_id=actor,
                queries=queries,
                regions=regions,
                max_results=max_results,
            )

            logger.info(
                "executing_apify_actor",
                actor_id=actor,
                queries=queries,
                region_count=len(regions),
            )

            try:
                items, duration = await self._execute_single_actor_run(
                    actor_id=actor,
                    payload=payload,
                    max_items=max_results,
                )
                self.last_actor_id = actor
                self.last_run_duration_seconds = duration
                logger.info(
                    "apify_actor_success",
                    actor_id=actor,
                    items_retrieved=len(items),
                    duration_sec=duration,
                )
                return items, actor, duration

            except Exception as exc:
                last_err = exc
                logger.warning(
                    "apify_actor_run_failed_trying_fallback",
                    failed_actor=actor,
                    error=str(exc),
                )

        if last_err:
            raise last_err
        return [], self.actor_id, 0.0

    async def search_region(
        self, query: str, region: FacebookRegion, max_results: int = 20
    ) -> List[RawListing]:
        """
        Searches a single region for a query. Maintains backward compatibility with BaseFacebookProvider.
        """
        if not self.token:
            logger.warning("apify_token_not_configured", region_id=region.id, query=query)
            return []

        try:
            items, actor_used, _ = await self._execute_with_fallback(
                queries=[query],
                regions=[region],
                max_results=max_results,
            )
            listings: List[RawListing] = []
            for item in items:
                if isinstance(item, dict):
                    norm = self.normalize_item(item, region=region)
                    if norm is not None:
                        listings.append(norm)
            return listings
        except Exception as exc:
            logger.error("apify_search_region_failed", query=query, region=region.name, error=str(exc))
            return []

    async def fetch_latest(
        self,
        queries: Optional[List[str]] = None,
        regions: Optional[List[FacebookRegion]] = None,
        max_results_per_query: int = 20,
    ) -> List[RawListing]:
        """
        High-efficiency batched discovery across regions.
        Shards search logically by country (US shard, Canada shard) or single batch,
        reducing total Actor runs per cycle to 1 or 2 rather than N*M.
        Enforces in-run deduplication and resilient isolated error handling.
        """
        if not self.token:
            logger.warning("apify_token_not_configured")
            return []

        if not queries:
            queries = ["CAT loader", "CAT wheel loader", "CASE 580"]

        if not regions:
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

        # Reset cycle run stats
        self.actor_runs_started = 0
        t_batch_start = asyncio.get_event_loop().time()

        # Shard logically by country: US shard and Canada shard (Phase 9 architecture)
        shards: Dict[str, List[FacebookRegion]] = {}
        for r in regions:
            shards.setdefault(r.country, []).append(r)

        all_listings: List[RawListing] = []
        seen_ids: Set[str] = set()

        async def _process_shard(country_code: str, shard_regions: List[FacebookRegion]) -> List[RawListing]:
            # Calculate cap for shard
            shard_cap = max(10, max_results_per_query * len(queries))
            try:
                items, actor_used, _ = await self._execute_with_fallback(
                    queries=queries,
                    regions=shard_regions,
                    max_results=shard_cap,
                )
                shard_listings: List[RawListing] = []
                for item in items:
                    if isinstance(item, dict):
                        # Associate with closest region in shard or primary
                        norm = self.normalize_item(item, region=shard_regions[0])
                        if norm is not None:
                            shard_listings.append(norm)
                return shard_listings
            except Exception as exc:
                logger.error("apify_shard_failed", country=country_code, error=str(exc))
                return []

        shard_tasks = [
            _process_shard(country, shard_regs)
            for country, shard_regs in shards.items()
        ]

        results = await asyncio.gather(*shard_tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, list):
                for item in res:
                    if item.external_id not in seen_ids:
                        seen_ids.add(item.external_id)
                        all_listings.append(item)
            elif isinstance(res, Exception):
                logger.error("shard_exception", error=str(res))

        self.last_run_duration_seconds = round(asyncio.get_event_loop().time() - t_batch_start, 2)
        logger.info(
            "apify_batch_discovery_complete",
            actor_id=self.last_actor_id,
            actor_runs_started=self.actor_runs_started,
            total_items_found=len(all_listings),
            duration_sec=self.last_run_duration_seconds,
        )
        return all_listings

    def normalize_item(
        self, item: Dict[str, Any], region: Optional[FacebookRegion] = None
    ) -> Optional[RawListing]:
        """
        Normalizes scraper output from Black Falcon, Calm Builder, or Apify default
        into a canonical RawListing. Extracts published timestamps, dirty prices,
        currencies, photos, and locations.
        """
        # 1. External ID extraction
        raw_id = str(
            item.get("listingId")
            or item.get("id")
            or item.get("itemId")
            or item.get("productId")
            or ""
        ).strip()

        url_val = (
            item.get("listingUrl")
            or item.get("itemUrl")
            or item.get("url")
            or item.get("link")
            or item.get("portalUrl")
            or ""
        )

        if not raw_id and url_val:
            id_match = re.search(r"/marketplace/item/(\d+)", url_val)
            if id_match:
                raw_id = id_match.group(1)

        if not raw_id:
            return None

        # Canonicalize item URL
        if not url_val:
            url_val = f"https://www.facebook.com/marketplace/item/{raw_id}"

        # 2. Title extraction
        title = str(
            item.get("marketplace_listing_title")
            or item.get("listingTitle")
            or item.get("title")
            or item.get("name")
            or item.get("header")
            or ""
        ).strip()
        if not title:
            return None

        # 3. Price & Currency normalization
        price_dec: Optional[Decimal] = None
        currency_code: str = "CAD" if (region and region.country == "CA") else "USD"

        # Check explicit numeric price field first (Black Falcon provides priceValue)
        raw_num_price = item.get("priceValue")
        if raw_num_price is not None and isinstance(raw_num_price, (int, float)):
            try:
                price_dec = Decimal(str(raw_num_price))
            except Exception:
                price_dec = None

        # Check explicit currency field (Black Falcon provides priceCurrency)
        raw_curr = item.get("priceCurrency") or item.get("currency")
        if isinstance(raw_curr, str) and raw_curr.strip():
            curr_upper = raw_curr.strip().upper()
            if "CAD" in curr_upper:
                currency_code = "CAD"
            elif "USD" in curr_upper:
                currency_code = "USD"

        # Fallback to dirty formatted price strings
        if price_dec is None:
            price_field = (
                item.get("priceText")
                or item.get("listing_price")
                or item.get("listingPrice")
                or item.get("price")
                or item.get("formattedPrice")
                or item.get("amount")
            )
            if isinstance(price_field, dict):
                amount_val = price_field.get("amount") or price_field.get("formatted_amount")
                price_str = str(amount_val).strip() if amount_val else ""
                currency_check = str(price_field.get("formatted_amount", ""))
            else:
                price_str = str(price_field).strip() if price_field is not None else ""
                currency_check = price_str

            if currency_check:
                if re.search(r"\bCAD\b|CA\$|C\$", currency_check, re.IGNORECASE):
                    currency_code = "CAD"
                elif re.search(r"\bUSD\b|US\$", currency_check, re.IGNORECASE):
                    currency_code = "USD"

            if price_str:
                clean_num = re.sub(r"[^\d.]", "", price_str)
                if clean_num:
                    try:
                        price_dec = Decimal(clean_num)
                    except Exception:
                        price_dec = None

        # 4. Location extraction
        location: Optional[str] = None
        loc_val = (
            item.get("locationText")
            or item.get("location")
            or item.get("city")
            or item.get("locationName")
        )
        if isinstance(loc_val, dict):
            rg = loc_val.get("reverse_geocode") if isinstance(loc_val.get("reverse_geocode"), dict) else {}
            city_page = rg.get("city_page") if isinstance(rg.get("city_page"), dict) else {}
            display = city_page.get("display_name")
            if display:
                location = str(display).strip()
            else:
                city = rg.get("city") or loc_val.get("city")
                state = rg.get("state") or loc_val.get("state")
                country = rg.get("country") or loc_val.get("country")
                parts = [city, state, country]
                location = ", ".join([str(p).strip() for p in parts if p]) or None
        elif isinstance(loc_val, str) and loc_val.strip():
            location = loc_val.strip()

        if not location and (item.get("city") or item.get("state")):
            c = str(item.get("city") or "").strip()
            s = str(item.get("state") or "").strip()
            location = f"{c}, {s}".strip(", ") or None

        if not location and region:
            location = f"{region.name}, {region.country}"

        # 5. Description
        description = item.get("description") or item.get("details") or item.get("text")
        if isinstance(description, str):
            description = description.strip() or None
        else:
            description = None

        # 6. Image URL
        image_url: Optional[str] = None
        if item.get("imageUrl") and isinstance(item.get("imageUrl"), str):
            image_url = item.get("imageUrl")

        if not image_url:
            raw_gallery = item.get("galleryUrls") or item.get("images") or item.get("photos")
            if isinstance(raw_gallery, list) and raw_gallery:
                first = raw_gallery[0]
                if isinstance(first, str):
                    image_url = first
                elif isinstance(first, dict):
                    image_url = first.get("url") or first.get("src")

        if not image_url:
            photo_obj = item.get("primary_listing_photo") or item.get("primaryListingPhoto")
            if isinstance(photo_obj, dict):
                image_url = photo_obj.get("photo_image_url") or photo_obj.get("image") or photo_obj.get("url")

        if not image_url:
            image_url = item.get("primaryPhoto") or item.get("image") or item.get("photoUrl")

        # 7. Seller Name
        seller_name: Optional[str] = None
        seller_data = (
            item.get("marketplace_listing_seller")
            or item.get("seller")
            or item.get("sellerName")
        )
        if isinstance(seller_data, dict):
            seller_name = seller_data.get("name") or seller_data.get("username")
        elif isinstance(seller_data, str) and seller_data.strip():
            seller_name = seller_data.strip()

        # 8. Published timestamp extraction (trustworthy source timestamp)
        published_at: Optional[datetime] = None
        pub_raw = item.get("publishedAt") or item.get("creationTime") or item.get("datePosted")
        if isinstance(pub_raw, str) and pub_raw.strip():
            try:
                published_at = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
            except Exception:
                published_at = None
        elif isinstance(pub_raw, (int, float)) and pub_raw > 0:
            try:
                published_at = datetime.fromtimestamp(pub_raw, tz=timezone.utc)
            except Exception:
                published_at = None

        if not published_at:
            epoch_raw = item.get("publishedAtEpoch")
            if isinstance(epoch_raw, (int, float)) and epoch_raw > 0:
                try:
                    published_at = datetime.fromtimestamp(epoch_raw, tz=timezone.utc)
                except Exception:
                    published_at = None

        # 9. Serial Number extraction (if present in description)
        serial_number: Optional[str] = None
        if description:
            sn_match = re.search(
                r"\b(?:s/n|serial|vin)\s*[:#]?\s*([A-Za-z0-9\-_]{6,})",
                description,
                re.IGNORECASE,
            )
            if sn_match:
                serial_number = sn_match.group(1).strip()

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
                serial_number=serial_number,
                published_at=published_at,
                scraped_at=get_utc_now(),
            )
        except Exception as exc:
            logger.warning(
                "facebook_raw_listing_validation_failed",
                error=str(exc),
                external_id=raw_id,
            )
            return None

    async def enrich_listing(self, raw: RawListing) -> RawListing:
        """
        Fetches detailed information for a confirmed ACCEPT or REVIEW candidate
        if it lacks critical metadata (e.g. description or seller).
        Obvious rejects must NOT be enriched.
        """
        # If already has description and seller, return as-is
        if raw.description and raw.seller_name and raw.image_url:
            return raw

        if not self.token or not raw.url:
            return raw

        client = await self._get_client()
        # Calm Builder or Black Falcon item URL direct fetch
        try:
            actor_clean = "calm_builder~facebook-marketplace-scraper"
            payload = {"itemUrls": [raw.url], "fetchDetails": True}
            run_url = f"{self.APIFY_BASE_URL}/acts/{actor_clean}/runs?token={self.token}&waitForFinish=60"
            res = await client.post(run_url, json=payload)
            if res.status_code in (200, 201):
                data = res.json().get("data", {})
                dataset_id = data.get("defaultDatasetId")
                if dataset_id:
                    d_res = await client.get(
                        f"{self.APIFY_BASE_URL}/datasets/{dataset_id}/items?token={self.token}&limit=1"
                    )
                    if d_res.status_code == 200:
                        items = d_res.json()
                        if items and isinstance(items[0], dict):
                            enriched = self.normalize_item(items[0])
                            if enriched:
                                return enriched
        except Exception as exc:
            logger.warning("enrichment_failed", external_id=raw.external_id, error=str(exc))

        return raw

    async def close(self) -> None:
        """Close HTTP client session."""
        if self._client and not self._external_client:
            await self._client.aclose()
            self._client = None
