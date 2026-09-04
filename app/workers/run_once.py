from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import sys
import time
from typing import Any, Dict, List, Optional
import structlog
from sqlalchemy import select

from app.config import load_watchlist, settings
from app.database.models import SourceScanStatus
from app.database.session import async_session_factory, engine, init_db
from app.matcher.equipment_matcher import EquipmentMatcher
from app.providers.machinerytrader import MachineryTraderProvider
from app.schemas.listing import MatchClassification, MatchResult, RawListing
from app.services.listing_service import ListingService, UpsertOutcome

logger = structlog.get_logger(__name__)


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def run_scan_once(
    query: Optional[str] = None,
    max_pages: int = 1,
    headless: bool = True,
    persist: bool = True,
) -> Dict[str, Any]:
    """
    Executes a single end-to-end scan cycle:
    1. Fetches listings from MachineryTrader.
    2. Runs each listing through EquipmentMatcher.
    3. Persists matches via ListingService with atomic deduplication.
    4. Updates SourceScanStatus with runtime metrics.
    """
    watchlist = load_watchlist()
    matcher = EquipmentMatcher(watchlist)

    # Use specified query or default from watchlist
    search_query = query or (
        watchlist.search_queries[0]
        if watchlist.search_queries
        else "CAT wheel loader"
    )

    started_at = get_utc_now()
    start_perf = time.perf_counter()
    provider = MachineryTraderProvider(headless=headless)

    metrics: Dict[str, Any] = {
        "source": provider.source,
        "query": search_query,
        "max_pages": max_pages,
        "started_at": started_at.isoformat(),
        "total_scraped": 0,
        "accepted": 0,
        "review": 0,
        "rejected": 0,
        "new_inserted": 0,
        "duplicates": 0,
        "price_changed": 0,
        "duration_ms": 0,
        "examples": {
            "accepted": [],
            "review": [],
            "rejected": [],
        },
        "limitations": [],
    }

    scan_error: Optional[str] = None

    try:
        # Initialize DB schema if persisting
        if persist:
            await init_db()

        print(f"\n[SCAN] Starting live scan for query: '{search_query}' (max_pages={max_pages})...")
        raw_listings: List[RawListing] = await provider.search(
            query=search_query,
            max_pages=max_pages,
        )
        metrics["total_scraped"] = len(raw_listings)
        print(f"[SCAN] Discovered and parsed {len(raw_listings)} listings from {provider.source}.")

        evaluated_pairs: List[tuple[RawListing, MatchResult]] = []
        for raw in raw_listings:
            match_res = matcher.match(raw)
            evaluated_pairs.append((raw, match_res))

            if match_res.classification == MatchClassification.ACCEPT:
                metrics["accepted"] += 1
                if len(metrics["examples"]["accepted"]) < 3:
                    metrics["examples"]["accepted"].append({
                        "id": raw.external_id,
                        "title": raw.title,
                        "model": match_res.model,
                        "confidence": match_res.confidence,
                        "price": str(raw.price) if raw.price else "None",
                        "location": raw.location,
                        "seller": raw.seller_name,
                        "serial_number": raw.serial_number,
                        "url": raw.url,
                    })
            elif match_res.classification == MatchClassification.REVIEW:
                metrics["review"] += 1
                if len(metrics["examples"]["review"]) < 3:
                    metrics["examples"]["review"].append({
                        "id": raw.external_id,
                        "title": raw.title,
                        "model": match_res.model,
                        "confidence": match_res.confidence,
                        "reason": match_res.rejection_reason or "Review threshold met",
                        "price": str(raw.price) if raw.price else "None",
                        "url": raw.url,
                    })
            else:
                metrics["rejected"] += 1
                if len(metrics["examples"]["rejected"]) < 3:
                    metrics["examples"]["rejected"].append({
                        "id": raw.external_id,
                        "title": raw.title,
                        "reason": match_res.rejection_reason or "Target model not matched",
                        "price": str(raw.price) if raw.price else "None",
                    })

        # Persist qualified (ACCEPT & REVIEW) listings to database
        if persist and evaluated_pairs:
            async with async_session_factory() as session:
                listing_service = ListingService(session)
                for raw, match_res in evaluated_pairs:
                    # Persist accepted and review candidates
                    if match_res.classification in {
                        MatchClassification.ACCEPT,
                        MatchClassification.REVIEW,
                    }:
                        _, outcome = await listing_service.upsert_listing(
                            raw=raw,
                            match=match_res,
                            auto_commit=True,
                        )
                        if outcome == UpsertOutcome.NEW:
                            metrics["new_inserted"] += 1
                        elif outcome == UpsertOutcome.PRICE_CHANGED:
                            metrics["price_changed"] += 1
                        else:
                            metrics["duplicates"] += 1

    except Exception as exc:
        scan_error = str(exc)
        metrics["limitations"].append(f"Scan exception: {scan_error}")
        logger.error("run_scan_once_failed", error=scan_error)
        raise
    finally:
        await provider.close()
        duration_ms = int((time.perf_counter() - start_perf) * 1000)
        metrics["duration_ms"] = duration_ms
        completed_at = get_utc_now()

        # Update SourceScanStatus table
        if persist:
            try:
                async with async_session_factory() as session:
                    stmt = select(SourceScanStatus).where(
                        SourceScanStatus.source == provider.source
                    )
                    res = await session.execute(stmt)
                    status_row = res.scalar_one_or_none()
                    if status_row is None:
                        status_row = SourceScanStatus(source=provider.source)
                        session.add(status_row)

                    status_row.last_scan_started_at = started_at
                    status_row.last_scan_completed_at = completed_at
                    status_row.duration_ms = duration_ms
                    status_row.listings_received = metrics["total_scraped"]
                    status_row.new_matches = metrics["accepted"] + metrics["review"]
                    status_row.error = scan_error
                    if not scan_error:
                        status_row.last_successful_scan_at = completed_at

                    await session.commit()
            except Exception as e:
                logger.warning("failed_to_update_source_scan_status", error=str(e))

    return metrics


def print_scan_report(metrics: Dict[str, Any]) -> None:
    """Print readable report of live scan metrics and examples."""
    print("=" * 70)
    print(f"  MACHINERYTRADER LIVE SCAN RESULTS")
    print("=" * 70)
    print(f"Source:           {metrics['source']}")
    print(f"Query:            '{metrics['query']}'")
    print(f"Duration:         {metrics['duration_ms']} ms")
    print(f"Total Scraped:    {metrics['total_scraped']}")
    print(f"Accepted:         {metrics['accepted']}")
    print(f"Review:           {metrics['review']}")
    print(f"Rejected:         {metrics['rejected']}")
    print(f"New Inserted:     {metrics['new_inserted']}")
    print(f"Price Changed:    {metrics['price_changed']}")
    print(f"Duplicates:       {metrics['duplicates']}")
    print("-" * 70)

    print("\n[ACCEPTED EXAMPLES]")
    for item in metrics["examples"]["accepted"]:
        print(f"  * ID: {item['id']} | Model: {item['model']} | Conf: {item['confidence']:.2f} | Price: ${item['price']}")
        print(f"    Title:  {item['title']}")
        if item.get("serial_number"):
            print(f"    S/N:    {item['serial_number']}")
        if item.get("seller"):
            print(f"    Seller: {item['seller']}")
        print(f"    URL:    {item['url']}")

    print("\n[REVIEW EXAMPLES]")
    for item in metrics["examples"]["review"]:
        print(f"  * ID: {item['id']} | Conf: {item['confidence']:.2f} | Reason: {item['reason']}")
        print(f"    Title: {item['title']}")
        print(f"    URL:   {item['url']}")

    print("\n[REJECTED EXAMPLES]")
    for item in metrics["examples"]["rejected"]:
        print(f"  * ID: {item['id']} | Reason: {item['reason']}")
        print(f"    Title: {item['title']}")

    if metrics["limitations"]:
        print("\n[LIMITATIONS FOUND]")
        for lim in metrics["limitations"]:
            print(f"  - {lim}")
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one live marketplace scan.")
    parser.add_argument("--query", type=str, default="CAT wheel loader", help="Search keyword")
    parser.add_argument("--max-pages", type=int, default=1, help="Max search pages")
    parser.add_argument("--no-headless", action="store_true", help="Run browser with visible UI")
    parser.add_argument("--no-save", action="store_true", help="Skip DB persistence")

    args = parser.parse_args()

    metrics = asyncio.run(
        run_scan_once(
            query=args.query,
            max_pages=args.max_pages,
            headless=not args.no_headless,
            persist=not args.no_save,
        )
    )
    print_scan_report(metrics)


if __name__ == "__main__":
    main()
