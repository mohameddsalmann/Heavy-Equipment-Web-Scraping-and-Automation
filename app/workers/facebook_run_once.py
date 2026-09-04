from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional
import structlog
from sqlalchemy import select

from app.config import load_watchlist, settings
from app.database.models import AlertEvent, SourceScanStatus
from app.database.session import async_session_factory, init_db
from app.matcher.equipment_matcher import EquipmentMatcher
from app.providers.facebook import (
    BaseFacebookProvider,
    FacebookRegion,
    get_facebook_provider,
    load_facebook_regions,
)
from app.schemas.listing import MatchClassification, MatchResult, RawListing
from app.services.listing_service import ListingService, UpsertOutcome

logger = structlog.get_logger(__name__)


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def percentile(data: List[float], p: float) -> float:
    """Calculate percentile from a list of floats."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    d0 = sorted_data[int(f)] * (c - k)
    d1 = sorted_data[int(c)] * (k - f)
    return d0 + d1


from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

async def run_facebook_scan(
    queries: Optional[List[str]] = None,
    region_ids: Optional[List[str]] = None,
    max_results_per_query: int = 20,
    provider_name: Optional[str] = None,
    actor_id: Optional[str] = None,
    persist: bool = True,
    custom_provider: Optional[BaseFacebookProvider] = None,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> Dict[str, Any]:
    """
    Executes a single Facebook Marketplace discovery and matching cycle:
    1. Runs regional search grid via managed provider (Apify / Bright Data).
    2. Batches queries & regions to minimize Actor runs (e.g. 1-2 runs total).
    3. Evaluates every item through deterministic EquipmentMatcher with post-match anomaly gate.
    4. Persists ACCEPT & REVIEW listings with atomic deduplication.
    5. Dispatches confirmed alerts to alert_events outbox (NEW_LISTING, PRICE_CHANGED).
    6. Measures end-to-end latency breakdowns (provider run, internal processing, source-to-first-seen).
    """
    scan_started_at = get_utc_now()
    t0 = asyncio.get_event_loop().time()

    # 1. Prepare regions
    all_regions = load_facebook_regions()
    if region_ids:
        target_regions = [r for r in all_regions if r.id in region_ids]
    else:
        target_regions = all_regions

    if not target_regions:
        target_regions = [
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

    # 2. Prepare search queries
    watchlist = load_watchlist()
    if queries:
        target_queries = queries
    else:
        # Default priority queries from watchlist
        target_queries = (
            watchlist.search_queries[:4]
            if watchlist.search_queries
            else ["CAT loader", "CAT wheel loader", "CASE 580"]
        )

    # 3. Initialize Provider & Matcher
    provider = custom_provider or get_facebook_provider(provider_name, actor_id=actor_id)
    matcher = EquipmentMatcher(watchlist)

    actual_actor_id = (
        getattr(provider, "actor_id", None)
        or actor_id
        or settings.active_facebook_actor_id
    )

    metrics: Dict[str, Any] = {
        "source": provider.source,
        "provider": provider.__class__.__name__,
        "actor_id": actual_actor_id,
        "actor_runs_started": 0,
        "queries_executed": target_queries,
        "regions_scanned": [f"{r.name} ({r.country})" for r in target_regions],
        "scan_started_at": scan_started_at.isoformat(),
        "scan_completed_at": None,
        "total_raw_listings": 0,
        "unique_facebook_ids": 0,
        "accepted": 0,
        "review": 0,
        "rejected": 0,
        "new_inserted": 0,
        "duplicates": 0,
        "price_changed": 0,
        "price_anomaly": 0,
        "alerts_created": 0,
        "errors": [],
        "latency": {
            "full_scan_duration_seconds": 0.0,
            "provider_run_duration_seconds": 0.0,
            "internal_processing_latency_seconds": 0.0,
            "scan_duration_seconds": 0.0,
            "median_detection_latency_seconds": 0.0,
            "p95_detection_latency_seconds": 0.0,
            "max_detection_latency_seconds": 0.0,
            "source_to_first_seen_seconds": None,
        },
        "examples": {
            "accepted": [],
            "review": [],
            "rejected": [],
            "all_sample": [],
        },
    }

    scan_error: Optional[str] = None
    detection_latencies: List[float] = []
    source_to_first_seen_latencies: List[float] = []

    db_factory = session_factory or async_session_factory

    try:
        if persist and session_factory is None:
            await init_db()

        # Execute regional search
        t_provider_start = asyncio.get_event_loop().time()
        raw_listings = await provider.fetch_latest(
            queries=target_queries,
            regions=target_regions,
            max_results_per_query=max_results_per_query,
        )
        provider_duration = round(asyncio.get_event_loop().time() - t_provider_start, 2)
        metrics["latency"]["provider_run_duration_seconds"] = getattr(
            provider, "last_run_duration_seconds", provider_duration
        )
        metrics["actor_id"] = getattr(provider, "last_actor_id", metrics["actor_id"])
        metrics["actor_runs_started"] = getattr(provider, "actor_runs_started", 1)

        metrics["total_raw_listings"] = len(raw_listings)
        metrics["unique_facebook_ids"] = len({l.external_id for l in raw_listings})

        # Match listings through deterministic matcher + post-match anomaly gate
        evaluated: List[tuple[RawListing, MatchResult]] = []

        for item in raw_listings:
            match = matcher.match(item)

            # Optional detail enrichment for suspicious/incomplete ACCEPT or REVIEW candidates only
            # Obvious REJECT items are NEVER enriched to conserve cost
            if (
                match.classification in {MatchClassification.ACCEPT, MatchClassification.REVIEW}
                and (not item.description or not item.seller_name)
                and hasattr(provider, "enrich_listing")
            ):
                try:
                    enriched_item = await provider.enrich_listing(item)
                    if enriched_item and enriched_item.description:
                        item = enriched_item
                        # Re-evaluate with enriched details
                        match = matcher.match(item)
                except Exception:
                    pass

            evaluated.append((item, match))

            # Measure source-to-first-seen latency if published timestamp is available
            if item.published_at:
                source_delay = (item.scraped_at - item.published_at).total_seconds()
                if source_delay >= 0:
                    source_to_first_seen_latencies.append(source_delay)

            # Measure internal processing detection delay
            delay_sec = max(0.0, (get_utc_now() - item.scraped_at).total_seconds())
            detection_latencies.append(delay_sec)

            sample_item = {
                "id": item.external_id,
                "title": item.title,
                "classification": match.classification.value,
                "model": match.model,
                "confidence": match.confidence,
                "price": str(item.price) if item.price is not None else None,
                "currency": item.currency,
                "location": item.location,
                "url": item.url,
                "reason": match.rejection_reason,
            }

            if len(metrics["examples"]["all_sample"]) < 10:
                metrics["examples"]["all_sample"].append(sample_item)

            if "price_anomaly" in match.matched_rules:
                metrics["price_anomaly"] += 1

            if match.classification == MatchClassification.ACCEPT:
                metrics["accepted"] += 1
                if len(metrics["examples"]["accepted"]) < 5:
                    metrics["examples"]["accepted"].append(sample_item)
            elif match.classification == MatchClassification.REVIEW:
                metrics["review"] += 1
                if len(metrics["examples"]["review"]) < 5:
                    metrics["examples"]["review"].append(sample_item)
            else:
                metrics["rejected"] += 1
                if len(metrics["examples"]["rejected"]) < 5:
                    metrics["examples"]["rejected"].append(sample_item)

        # Persist listings and alert outbox events
        if persist and evaluated:
            async with db_factory() as session:
                listing_service = ListingService(session)
                for raw, match in evaluated:
                    # Persist only ACCEPT and REVIEW candidates
                    if match.classification in {
                        MatchClassification.ACCEPT,
                        MatchClassification.REVIEW,
                    }:
                        _, outcome = await listing_service.upsert_listing(
                            raw=raw, match=match, auto_commit=True
                        )
                        if outcome == UpsertOutcome.NEW:
                            metrics["new_inserted"] += 1
                            if match.classification == MatchClassification.ACCEPT:
                                metrics["alerts_created"] += 1
                        elif outcome == UpsertOutcome.PRICE_CHANGED:
                            metrics["price_changed"] += 1
                            if match.classification == MatchClassification.ACCEPT:
                                metrics["alerts_created"] += 1
                        else:
                            metrics["duplicates"] += 1

    except Exception as exc:
        scan_error = str(exc)
        metrics["errors"].append(scan_error)
        logger.error("facebook_scan_error", error=scan_error)
        raise
    finally:
        await provider.close()
        scan_completed_at = get_utc_now()
        total_duration_sec = round(asyncio.get_event_loop().time() - t0, 2)
        metrics["scan_completed_at"] = scan_completed_at.isoformat()
        metrics["latency"]["full_scan_duration_seconds"] = total_duration_sec
        metrics["latency"]["scan_duration_seconds"] = total_duration_sec

        prov_dur = metrics["latency"]["provider_run_duration_seconds"]
        internal_dur = round(max(0.0, total_duration_sec - prov_dur), 2)
        metrics["latency"]["internal_processing_latency_seconds"] = internal_dur

        if detection_latencies:
            metrics["latency"]["median_detection_latency_seconds"] = round(
                percentile(detection_latencies, 0.50), 2
            )
            metrics["latency"]["p95_detection_latency_seconds"] = round(
                percentile(detection_latencies, 0.95), 2
            )
            metrics["latency"]["max_detection_latency_seconds"] = round(
                max(detection_latencies), 2
            )

        if source_to_first_seen_latencies:
            metrics["latency"]["source_to_first_seen_seconds"] = {
                "median": round(percentile(source_to_first_seen_latencies, 0.50), 1),
                "p95": round(percentile(source_to_first_seen_latencies, 0.95), 1),
                "min": round(min(source_to_first_seen_latencies), 1),
                "max": round(max(source_to_first_seen_latencies), 1),
            }

        # Update SourceScanStatus
        if persist:
            try:
                async with db_factory() as session:
                    stmt = select(SourceScanStatus).where(
                        SourceScanStatus.source == provider.source
                    )
                    res = await session.execute(stmt)
                    status_row = res.scalar_one_or_none()
                    if status_row is None:
                        status_row = SourceScanStatus(source=provider.source)
                        session.add(status_row)

                    status_row.last_scan_started_at = scan_started_at
                    status_row.last_scan_completed_at = scan_completed_at
                    status_row.duration_ms = int(total_duration_sec * 1000)
                    status_row.listings_received = metrics["total_raw_listings"]
                    status_row.new_matches = metrics["accepted"]
                    status_row.error = scan_error
                    if not scan_error:
                        status_row.last_successful_scan_at = scan_completed_at

                    await session.commit()
            except Exception as e:
                logger.warning("source_scan_status_update_failed", error=str(e))

    return metrics


def print_facebook_report(metrics: Dict[str, Any]) -> None:
    """Print clean, detailed summary of the Facebook Marketplace scan."""
    print("\n" + "=" * 76)
    print("      FACEBOOK MARKETPLACE DISCOVERY REPORT")
    print("=" * 76)
    print(f"Provider:              {metrics['provider']}")
    print(f"Actor ID:              {metrics.get('actor_id', 'N/A')}")
    print(f"Actor Runs Started:    {metrics.get('actor_runs_started', 1)}")
    print(f"Queries Executed:      {', '.join(metrics['queries_executed'])}")
    print(f"Regions Scanned:       {', '.join(metrics['regions_scanned'])}")
    print("-" * 76)
    print("DISCOVERY & MATCHING CLASSIFICATION:")
    print(f"Total Raw Listings:    {metrics['total_raw_listings']}")
    print(f"Unique Facebook IDs:   {metrics['unique_facebook_ids']}")
    print(f"Accepted Matches:      {metrics['accepted']}")
    print(f"Review Pending:        {metrics['review']}")
    print(f"Rejected:              {metrics['rejected']}")
    print(f"Price Anomaly:         {metrics.get('price_anomaly', 0)}")
    print(f"New Inserted:          {metrics['new_inserted']}")
    print(f"Duplicates:            {metrics['duplicates']}")
    print(f"Price Changed:         {metrics['price_changed']}")
    print(f"Alert Events Created:  {metrics['alerts_created']}")
    print("-" * 76)
    print("LATENCY METRICS:")
    lat = metrics.get("latency", {})
    print(f"  * Median Latency:     {lat.get('median_detection_latency_seconds', 0.0)}s")
    print(f"  * P95 Latency:        {lat.get('p95_detection_latency_seconds', 0.0)}s")
    print(f"  * Actor Run Duration: {lat.get('provider_run_duration_seconds', 0.0)}s")
    print(f"  * Internal Latency:   {lat.get('internal_processing_latency_seconds', 0.0)}s")
    print(f"  * Total Scan Duration: {lat.get('full_scan_duration_seconds', 0.0)}s")
    if lat.get("source_to_first_seen_seconds"):
        s2f = lat["source_to_first_seen_seconds"]
        print(f"  * Source-to-Seen (Med): {s2f['median']}s ({round(s2f['median'] / 60, 1)}m)")
        print(f"  * Source-to-Seen (P95): {s2f['p95']}s ({round(s2f['p95'] / 60, 1)}m)")
    print("-" * 76)


    print("\n[SAMPLE LISTINGS EVALUATED (UP TO 10)]")
    samples = metrics["examples"]["all_sample"]
    if not samples:
        print("  (No listings returned)")
    for i, s in enumerate(samples, 1):
        price_str = f"${s['price']} {s['currency']}" if s['price'] else "Unpriced"
        print(f"{i:2d}. [{s['classification']}] {s['title']} - {price_str} | ID: {s['id']}")
        if s.get("location"):
            print(f"    Location: {s['location']}")
        if s.get("reason"):
            print(f"    Note:     {s['reason']}")
        print(f"    URL:      {s['url']}")

    if metrics["errors"]:
        print("\n[ERRORS ENCOUNTERED]")
        for err in metrics["errors"]:
            print(f"  - {err}")

    print("=" * 76)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Facebook Marketplace scan cycle.")
    parser.add_argument(
        "--queries",
        nargs="+",
        default=["CAT 966H", "CAT wheel loader", "CASE 580SL"],
        help="Search queries",
    )
    parser.add_argument(
        "--regions",
        nargs="+",
        default=None,
        help="Region IDs (e.g. us_texas ca_ontario)",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=20,
        help="Max results per query per region",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        help="Facebook provider: apify or brightdata",
    )
    parser.add_argument(
        "--actor-id",
        type=str,
        default=None,
        help="Apify Actor ID (e.g. blackfalcondata/facebook-marketplace-scraper)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Skip database writes",
    )

    args = parser.parse_args()

    metrics = asyncio.run(
        run_facebook_scan(
            queries=args.queries,
            region_ids=args.regions,
            max_results_per_query=args.max_results,
            provider_name=args.provider,
            actor_id=args.actor_id,
            persist=not args.no_save,
        )
    )
    print_facebook_report(metrics)


if __name__ == "__main__":
    main()
