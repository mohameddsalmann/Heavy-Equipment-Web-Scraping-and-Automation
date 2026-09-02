from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.database.models import Base, Listing, PriceHistory, SourceScanStatus
from app.schemas.listing import MatchClassification, MatchResult, RawListing
from app.services.listing_service import ListingService, UpsertOutcome
from app.utils.url import canonicalize_url, extract_stable_external_id

TEST_POSTGRES_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://test_user:test_password@localhost:5433/equipment_monitor_test",
)


# Database fixtures (pg_engine, test_db_session, session_factory) are provided by tests/conftest.py



def make_raw_listing(
    source: str = "machinerytrader",
    external_id: str = "item_001",
    title: str = "2015 CAT 966H Wheel Loader",
    price: Decimal | float | int | None = Decimal("110000.00"),
    currency: str = "USD",
    url: str | None = None,
    serial_number: str | None = None,
    seller_name: str | None = None,
) -> RawListing:
    dec_price = Decimal(str(price)) if price is not None else None
    return RawListing(
        source=source,
        external_id=external_id,
        title=title,
        price=dec_price,
        currency=currency,
        location="Houston, TX",
        url=url or f"https://{source}.com/item/{external_id}",
        serial_number=serial_number,
        seller_name=seller_name,
    )


def make_match_result() -> MatchResult:
    return MatchResult(
        classification=MatchClassification.ACCEPT,
        is_target_equipment=True,
        manufacturer="Caterpillar",
        model="966H",
        equipment_type="Wheel Loader",
        confidence=0.95,
        matched_rules=["model_in_title:966H", "manufacturer_in_title:Caterpillar"],
        extracted_price=Decimal("110000.00"),
    )


# ---------------------------------------------------------------------------
# 1. PostgreSQL Confirmation & Dialect Verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_postgresql_confirmation(test_db_session):
    """Proves: tests run against a real PostgreSQL 16 instance, NOT SQLite or in-memory."""
    # Verify dialect name
    dialect_name = test_db_session.bind.dialect.name
    assert dialect_name == "postgresql"

    # Query real PostgreSQL version
    result = await test_db_session.execute(text("SELECT version();"))
    full_version = result.scalar()
    assert "PostgreSQL 16" in full_version, f"Unexpected version: {full_version}"


# ---------------------------------------------------------------------------
# 2. Deduplication Requirements
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_source_and_external_id_does_not_create_another_listing(
    test_db_session,
):
    """Proves: same source + same external_id updates existing record rather than creating duplicates."""
    service = ListingService(test_db_session)
    raw = make_raw_listing(source="machinerytrader", external_id="item_100")
    match = make_match_result()

    # 1. First insert
    listing1, outcome1 = await service.upsert_listing(raw, match)
    assert outcome1 == UpsertOutcome.NEW
    initial_id = listing1.id
    initial_first_seen = listing1.first_seen_at

    # 2. Duplicate insert with same source and external_id
    listing2, outcome2 = await service.upsert_listing(raw, match)
    assert outcome2 == UpsertOutcome.DUPLICATE
    assert listing2.id == initial_id
    assert listing2.first_seen_at == initial_first_seen

    # Verify total count in database is exactly 1
    count_stmt = select(func.count(Listing.id))
    total_listings = (await test_db_session.execute(count_stmt)).scalar()
    assert total_listings == 1


@pytest.mark.asyncio
async def test_same_listing_does_not_generate_another_new_event(test_db_session):
    """Proves: a previously seen listing yields DUPLICATE, never a second NEW outcome."""
    service = ListingService(test_db_session)
    raw = make_raw_listing(source="machinio", external_id="mach_555")
    match = make_match_result()

    # First event is NEW
    _, first_outcome = await service.upsert_listing(raw, match)
    assert first_outcome == UpsertOutcome.NEW

    # Subsequent identical scans must be DUPLICATE
    _, second_outcome = await service.upsert_listing(raw, match)
    assert second_outcome == UpsertOutcome.DUPLICATE

    _, third_outcome = await service.upsert_listing(raw, match)
    assert third_outcome == UpsertOutcome.DUPLICATE


@pytest.mark.asyncio
async def test_same_external_id_different_sources_allowed(test_db_session):
    """Proves: same external_id on different sources is treated as separate listings."""
    service = ListingService(test_db_session)
    shared_ext_id = "EQUIP_777"
    match = make_match_result()

    raw_source1 = make_raw_listing(source="machinerytrader", external_id=shared_ext_id)
    raw_source2 = make_raw_listing(source="machinio", external_id=shared_ext_id)

    listing1, outcome1 = await service.upsert_listing(raw_source1, match)
    listing2, outcome2 = await service.upsert_listing(raw_source2, match)

    assert outcome1 == UpsertOutcome.NEW
    assert outcome2 == UpsertOutcome.NEW
    assert listing1.id != listing2.id
    assert listing1.source == "machinerytrader"
    assert listing2.source == "machinio"

    count_stmt = select(func.count(Listing.id))
    total_listings = (await test_db_session.execute(count_stmt)).scalar()
    assert total_listings == 2


# ---------------------------------------------------------------------------
# 3. Monetary Decimal / PostgreSQL NUMERIC Precision & Price Change Rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monetary_exact_same_price_duplicate(test_db_session):
    """Rule: 85000 -> 85000 = DUPLICATE (Decimal precision)."""
    service = ListingService(test_db_session)
    raw1 = make_raw_listing(external_id="price_p1", price=Decimal("85000.00"))
    match = make_match_result()

    listing1, outcome1 = await service.upsert_listing(raw1, match)
    assert outcome1 == UpsertOutcome.NEW
    assert isinstance(listing1.price, Decimal)
    assert listing1.price == Decimal("85000.00")

    # Rescan with identical 85000
    raw2 = make_raw_listing(external_id="price_p1", price=Decimal("85000.00"))
    listing2, outcome2 = await service.upsert_listing(raw2, match)
    assert outcome2 == UpsertOutcome.DUPLICATE
    assert listing2.price == Decimal("85000.00")

    # Only initial price history record
    ph_stmt = select(PriceHistory).where(PriceHistory.listing_id == listing1.id)
    history = (await test_db_session.execute(ph_stmt)).scalars().all()
    assert len(history) == 1
    assert history[0].price == Decimal("85000.00")


@pytest.mark.asyncio
async def test_monetary_price_decrease_changed(test_db_session):
    """Rule: 85000 -> 82000 = PRICE_CHANGED."""
    service = ListingService(test_db_session)
    raw1 = make_raw_listing(external_id="price_p2", price=Decimal("85000.00"))
    match = make_match_result()

    listing1, outcome1 = await service.upsert_listing(raw1, match)
    assert outcome1 == UpsertOutcome.NEW

    # Price dropped to 82000
    raw2 = make_raw_listing(external_id="price_p2", price=Decimal("82000.00"))
    listing2, outcome2 = await service.upsert_listing(raw2, match)
    assert outcome2 == UpsertOutcome.PRICE_CHANGED
    assert listing2.price == Decimal("82000.00")

    # Two price history rows
    ph_stmt = select(PriceHistory).where(PriceHistory.listing_id == listing1.id)
    history = (await test_db_session.execute(ph_stmt)).scalars().all()
    assert len(history) == 2
    prices = [h.price for h in history]
    assert Decimal("85000.00") in prices
    assert Decimal("82000.00") in prices


@pytest.mark.asyncio
async def test_monetary_none_to_price_changed(test_db_session):
    """Rule: None -> 85000 = PRICE_CHANGED."""
    service = ListingService(test_db_session)
    raw_no_price = make_raw_listing(external_id="price_p3", price=None)
    match = make_match_result()

    listing1, outcome1 = await service.upsert_listing(raw_no_price, match)
    assert outcome1 == UpsertOutcome.NEW
    assert listing1.price is None

    # No price history for initial unpriced listing
    ph_stmt = select(PriceHistory).where(PriceHistory.listing_id == listing1.id)
    history1 = (await test_db_session.execute(ph_stmt)).scalars().all()
    assert len(history1) == 0

    # Rescan now publishes price of 85000
    raw_priced = make_raw_listing(external_id="price_p3", price=Decimal("85000.00"))
    listing2, outcome2 = await service.upsert_listing(raw_priced, match)
    assert outcome2 == UpsertOutcome.PRICE_CHANGED
    assert listing2.price == Decimal("85000.00")

    history2 = (await test_db_session.execute(ph_stmt)).scalars().all()
    assert len(history2) == 1
    assert history2[0].price == Decimal("85000.00")


@pytest.mark.asyncio
async def test_monetary_currency_changed(test_db_session):
    """Rule: 85000 USD -> 85000 CAD must be treated as a meaningful pricing/currency change."""
    service = ListingService(test_db_session)
    raw_usd = make_raw_listing(
        external_id="price_p4", price=Decimal("85000.00"), currency="USD"
    )
    match = make_match_result()

    listing1, outcome1 = await service.upsert_listing(raw_usd, match)
    assert outcome1 == UpsertOutcome.NEW
    assert listing1.currency == "USD"

    # Rescan with CAD
    raw_cad = make_raw_listing(
        external_id="price_p4", price=Decimal("85000.00"), currency="CAD"
    )
    listing2, outcome2 = await service.upsert_listing(raw_cad, match)
    assert outcome2 == UpsertOutcome.PRICE_CHANGED
    assert listing2.currency == "CAD"
    assert listing2.price == Decimal("85000.00")

    ph_stmt = select(PriceHistory).where(PriceHistory.listing_id == listing1.id)
    history = (await test_db_session.execute(ph_stmt)).scalars().all()
    assert len(history) == 2
    currencies = [h.currency for h in history]
    assert "USD" in currencies
    assert "CAD" in currencies


@pytest.mark.asyncio
async def test_monetary_missing_price_does_not_drop(test_db_session):
    """
    Rule: 85000 -> None
    Explicit definition: Do not interpret missing price as a price drop.
    The existing known price ($85,000) must be preserved, and the event must be DUPLICATE.
    """
    service = ListingService(test_db_session)
    raw_initial = make_raw_listing(external_id="price_p5", price=Decimal("85000.00"))
    match = make_match_result()

    listing1, outcome1 = await service.upsert_listing(raw_initial, match)
    assert outcome1 == UpsertOutcome.NEW
    assert listing1.price == Decimal("85000.00")

    # Rescan where provider omitted price (None)
    raw_missing_price = make_raw_listing(external_id="price_p5", price=None)
    listing2, outcome2 = await service.upsert_listing(raw_missing_price, match)

    assert outcome2 == UpsertOutcome.DUPLICATE
    # Crucial assertion: price is NOT overwritten to None or 0
    assert listing2.price == Decimal("85000.00")

    # No spurious second price history record
    ph_stmt = select(PriceHistory).where(PriceHistory.listing_id == listing1.id)
    history = (await test_db_session.execute(ph_stmt)).scalars().all()
    assert len(history) == 1


# ---------------------------------------------------------------------------
# 4. Stable External ID & URL Canonicalization Contract
# ---------------------------------------------------------------------------


def test_url_canonicalization_and_stable_id():
    """Proves: tracking params and URL fragments are stripped and do not alter identity."""
    url_with_tracking = "https://example.com/listing/123?utm_source=x&utm_campaign=summer&fbclid=abc#details"
    url_clean = "https://example.com/listing/123"

    canon1 = canonicalize_url(url_with_tracking)
    canon2 = canonicalize_url(url_clean)
    assert canon1 == "https://example.com/listing/123"
    assert canon2 == "https://example.com/listing/123"

    id1 = extract_stable_external_id(url_with_tracking)
    id2 = extract_stable_external_id(url_clean)
    assert id1 == "123"
    assert id2 == "123"


@pytest.mark.asyncio
async def test_provider_contract_url_and_id_stability(test_db_session):
    """Proves: raw listings differing only by tracking params resolve to same database listing."""
    service = ListingService(test_db_session)
    match = make_match_result()

    raw1 = RawListing(
        source="machinerytrader",
        external_id="123?utm_source=x",
        title="CAT 966H Wheel Loader",
        url="https://example.com/listing/123?utm_source=x",
        price=Decimal("95000.00"),
    )

    raw2 = RawListing(
        source="machinerytrader",
        external_id="123",
        title="CAT 966H Wheel Loader",
        url="https://example.com/listing/123",
        price=Decimal("95000.00"),
    )

    listing1, outcome1 = await service.upsert_listing(raw1, match)
    listing2, outcome2 = await service.upsert_listing(raw2, match)

    assert outcome1 == UpsertOutcome.NEW
    assert outcome2 == UpsertOutcome.DUPLICATE
    assert listing1.id == listing2.id


# ---------------------------------------------------------------------------
# 5. Future Cross-Source Deduplication Schema Attributes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_source_entity_fields_persisted(test_db_session):
    """Proves: serial_number and seller_name are persisted and indexed in PostgreSQL."""
    service = ListingService(test_db_session)
    raw = make_raw_listing(
        external_id="cross_source_001",
        serial_number="CAT0966HKL98210",
        seller_name="Empire Heavy Machinery LLC",
    )
    match = make_match_result()

    listing, outcome = await service.upsert_listing(raw, match)
    assert outcome == UpsertOutcome.NEW
    assert listing.serial_number == "CAT0966HKL98210"
    assert listing.seller_name == "Empire Heavy Machinery LLC"

    # Query via PostgreSQL index
    stmt = select(Listing).where(Listing.serial_number == "CAT0966HKL98210")
    found = (await test_db_session.execute(stmt)).scalar_one_or_none()
    assert found is not None
    assert found.id == listing.id


# ---------------------------------------------------------------------------
# 6. Concurrency Safety on Real PostgreSQL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_attempts_cannot_create_duplicates(session_factory):
    """Proves: 5 concurrent workers attempting to insert the exact same listing on PostgreSQL 16."""
    raw = make_raw_listing(
        source="machinerytrader",
        external_id="CONCURRENT_RACE_001",
        price=Decimal("85000.00"),
    )
    match = make_match_result()

    async def worker_upsert():
        async with session_factory() as session:
            service = ListingService(session)
            listing, outcome = await service.upsert_listing(raw, match, auto_commit=True)
            return listing.id, outcome

    # Execute 5 concurrent upsert attempts simultaneously
    tasks = [worker_upsert() for _ in range(5)]
    results = await asyncio.gather(*tasks)

    # All returned IDs must be identical
    ids = [r[0] for r in results]
    assert len(set(ids)) == 1

    # Exactly one task should get NEW, others must get DUPLICATE
    outcomes = [r[1] for r in results]
    assert outcomes.count(UpsertOutcome.NEW) == 1
    assert outcomes.count(UpsertOutcome.DUPLICATE) == 4

    # Verify database has exactly 1 listing row
    async with session_factory() as verify_session:
        count_stmt = select(func.count(Listing.id)).where(
            Listing.source == "machinerytrader",
            Listing.external_id == "CONCURRENT_RACE_001",
        )
        total_count = (await verify_session.execute(count_stmt)).scalar()
        assert total_count == 1


# ---------------------------------------------------------------------------
# 7. Status & Scan Status Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_classification_persisted_correctly(test_db_session):
    """Proves: listings classified as REVIEW are stored with status='REVIEW' and not ACCEPT."""
    service = ListingService(test_db_session)
    raw = make_raw_listing(source="machinerytrader", external_id="review_item_001")
    review_match = MatchResult(
        classification=MatchClassification.REVIEW,
        is_target_equipment=False,
        confidence=0.55,
        matched_rules=["model_in_title:966H"],
        rejection_reason="Confidence (0.55) requires human/AI review (threshold 0.70)",
    )
    listing, outcome = await service.upsert_listing(raw, review_match)
    assert outcome == UpsertOutcome.NEW
    assert listing.status == "REVIEW"
    assert listing.confidence == Decimal("0.55")


@pytest.mark.asyncio
async def test_source_scan_status_lifecycle(test_db_session):
    """Proves: SourceScanStatus records provider health, durations, and match counts."""
    now = datetime.now(timezone.utc)
    status_entry = SourceScanStatus(
        source="machinerytrader",
        last_scan_started_at=now,
        last_scan_completed_at=now,
        last_successful_scan_at=now,
        duration_ms=1850,
        listings_received=50,
        new_matches=4,
        error=None,
    )
    test_db_session.add(status_entry)
    await test_db_session.commit()

    retrieved = await test_db_session.get(SourceScanStatus, "machinerytrader")
    assert retrieved is not None
    assert retrieved.listings_received == 50
    assert retrieved.new_matches == 4
    assert retrieved.duration_ms == 1850
    assert retrieved.error is None
