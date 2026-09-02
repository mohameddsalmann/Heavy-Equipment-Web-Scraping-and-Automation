from __future__ import annotations

import pytest
from decimal import Decimal
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.database.session import get_db_session, init_db
from app.schemas.listing import RawListing
from app.services.listing_service import ListingService, UpsertOutcome
from app.utils.url import canonicalize_url, extract_stable_external_id
from tests.test_deduplication import TEST_POSTGRES_URL, make_match_result, make_raw_listing


@pytest.mark.asyncio
async def test_init_db_and_session_generator():
    """Test init_db with custom engine and get_db_session generator."""
    engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    await init_db(custom_engine=engine)

    # Test get_db_session generator success
    session_gen = get_db_session()
    session = await anext(session_gen)
    assert session is not None
    try:
        await anext(session_gen)
    except StopAsyncIteration:
        pass

    # Test get_db_session generator exception / rollback
    session_gen2 = get_db_session()
    session2 = await anext(session_gen2)
    with pytest.raises(RuntimeError):
        await session_gen2.athrow(RuntimeError("Simulated failure"))

    await engine.dispose()


def test_url_canonicalization_edge_cases():
    """Test URL utility edge cases: empty strings, root paths, full URL external IDs."""
    assert canonicalize_url("") == ""
    assert canonicalize_url("   ") == ""
    assert canonicalize_url("https://EXAMPLE.COM/") == "https://example.com/"

    # extract_stable_external_id when raw_external_id is a full URL
    id_from_full_url = extract_stable_external_id(
        url="https://example.com/listing/999",
        raw_external_id="https://example.com/listing/999?utm_source=facebook",
    )
    assert id_from_full_url == "999"

    # extract_stable_external_id with trailing fragment/param
    id_with_hash = extract_stable_external_id(
        url="https://example.com/item/456",
        raw_external_id="456#specs?foo=bar",
    )
    assert id_with_hash == "456"

    # Fallback when no path segment
    id_root = extract_stable_external_id("https://example.com")
    assert id_root == "https://example.com"


@pytest.mark.asyncio
async def test_listing_service_attribute_updates(test_db_session):
    """Test description, image_url, location updates when existing listing is rescanned."""
    service = ListingService(test_db_session)
    raw1 = make_raw_listing(
        external_id="attr_update_01",
        price=Decimal("50000.00"),
    )
    match = make_match_result()

    listing1, outcome1 = await service.upsert_listing(raw1, match)
    assert outcome1 == UpsertOutcome.NEW

    # Update with description, image_url, location, serial_number, seller_name
    raw2 = RawListing(
        source=raw1.source,
        external_id=raw1.external_id,
        title="Updated CAT Title",
        description="Brand new machine description",
        image_url="https://example.com/cat.jpg",
        location="Dallas, TX",
        serial_number="SN-CAT-999",
        seller_name="Texas CAT Dealer",
        url=raw1.url,
        price=Decimal("50000.00"),
    )

    listing2, outcome2 = await service.upsert_listing(raw2, match, auto_commit=False)
    assert outcome2 == UpsertOutcome.DUPLICATE
    assert listing2.description == "Brand new machine description"
    assert listing2.image_url == "https://example.com/cat.jpg"
    assert listing2.location == "Dallas, TX"
    assert listing2.serial_number == "SN-CAT-999"
    assert listing2.seller_name == "Texas CAT Dealer"
