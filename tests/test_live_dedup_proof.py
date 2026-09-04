from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List
import pytest
from sqlalchemy import select, func

from app.database.models import AlertEvent, Listing, PriceHistory
from app.providers.facebook.base import BaseFacebookProvider
from app.providers.facebook.models import FacebookRegion
from app.schemas.listing import MatchClassification, RawListing
from app.workers.facebook_run_once import run_facebook_scan


class ControlledEmissionProvider(BaseFacebookProvider):
    """
    Mock provider that emits controlled listings across cycles to test:
    1. Scan #1: Inserts new listings and alert outbox events.
    2. Scan #2: Proves existing listings are DUPLICATE, 0 duplicate NEW alert events created.
    3. Scan #3: Proves price change creates PRICE_CHANGED and 1 price alert.
    4. Multi-region in same cycle: Proves same external ID creates only one listing.
    """
    def __init__(self, items: List[RawListing]):
        super().__init__()
        self.items_to_emit = items

    async def search_region(
        self, query: str, region: FacebookRegion, max_results: int = 20
    ) -> List[RawListing]:
        return self.items_to_emit

    def normalize_item(self, item: Dict[str, Any], region: FacebookRegion = None) -> RawListing:
        return item


@pytest.mark.asyncio
async def test_full_dedup_lifecycle_proof(session_factory, test_db_session):
    """
    PHASE 6 PROOF:
    - existing listings are DUPLICATE
    - no duplicate NEW alert events are created
    - changed prices create PRICE_CHANGED only
    - same Facebook ID returned through multiple searches creates one listing
    """
    # 1. First Scan: A target CAT 966H wheel loader
    item_v1 = RawListing(
        source="facebook_marketplace",
        external_id="fb_live_dedup_966h",
        title="2012 Caterpillar 966H Wheel Loader",
        price=Decimal("85000.00"),
        currency="USD",
        location="Dallas, TX",
        url="https://www.facebook.com/marketplace/item/fb_live_dedup_966h",
    )

    # Also include duplicate item with same external ID across multiple searches in same cycle
    scan1_items = [
        item_v1,
        RawListing(
            source="facebook_marketplace",
            external_id="fb_live_dedup_966h",
            title="2012 Caterpillar 966H Wheel Loader",
            price=Decimal("85000.00"),
            currency="USD",
            location="Dallas, TX",
            url="https://www.facebook.com/marketplace/item/fb_live_dedup_966h",
        ),
    ]

    prov1 = ControlledEmissionProvider(scan1_items)
    metrics1 = await run_facebook_scan(
        queries=["CAT 966H"],
        region_ids=["us_texas"],
        custom_provider=prov1,
        session_factory=session_factory,
    )

    # Verification of Scan #1
    assert metrics1["accepted"] == 1
    assert metrics1["new_inserted"] == 1       # Exactly 1 new inserted
    assert metrics1["alerts_created"] == 1     # Exactly 1 NEW_LISTING alert created


    # Check DB listings count
    listings_q = await test_db_session.execute(
        select(Listing).where(Listing.external_id == "fb_live_dedup_966h")
    )
    listings = listings_q.scalars().all()
    assert len(listings) == 1
    assert listings[0].price == Decimal("85000.00")

    # Check alert events count
    alerts_q = await test_db_session.execute(
        select(AlertEvent).where(AlertEvent.listing_id == listings[0].id)
    )
    alerts = alerts_q.scalars().all()
    assert len(alerts) == 1
    assert alerts[0].event_type == "NEW_LISTING"

    # -------------------------------------------------------------
    # 2. Second Scan: Exact same listing emitted again
    # -------------------------------------------------------------
    prov2 = ControlledEmissionProvider([item_v1])
    metrics2 = await run_facebook_scan(
        queries=["CAT 966H"],
        region_ids=["us_texas"],
        custom_provider=prov2,
        session_factory=session_factory,
    )

    # Verification of Scan #2: MUST BE DUPLICATE
    assert metrics2["accepted"] == 1
    assert metrics2["new_inserted"] == 0       # 0 NEW inserted!
    assert metrics2["duplicates"] == 1         # Marked DUPLICATE!
    assert metrics2["price_changed"] == 0      # No price change
    assert metrics2["alerts_created"] == 0     # ZERO duplicate alerts created!

    # Verify no new alert event in DB
    alerts_q2 = await test_db_session.execute(
        select(AlertEvent).where(AlertEvent.listing_id == listings[0].id)
    )
    alerts2 = alerts_q2.scalars().all()
    assert len(alerts2) == 1  # Still exactly 1!

    # -------------------------------------------------------------
    # 3. Third Scan: Price Drops from $85,000 to $79,000
    # -------------------------------------------------------------
    item_v2_price_drop = RawListing(
        source="facebook_marketplace",
        external_id="fb_live_dedup_966h",
        title="2012 Caterpillar 966H Wheel Loader",
        price=Decimal("79000.00"),
        currency="USD",
        location="Dallas, TX",
        url="https://www.facebook.com/marketplace/item/fb_live_dedup_966h",
    )

    prov3 = ControlledEmissionProvider([item_v2_price_drop])
    metrics3 = await run_facebook_scan(
        queries=["CAT 966H"],
        region_ids=["us_texas"],
        custom_provider=prov3,
        session_factory=session_factory,
    )

    # Verification of Scan #3: MUST BE PRICE_CHANGED
    assert metrics3["accepted"] == 1
    assert metrics3["new_inserted"] == 0
    assert metrics3["duplicates"] == 0
    assert metrics3["price_changed"] == 1      # Price changed detected!
    assert metrics3["alerts_created"] == 1     # Exactly 1 PRICE_CHANGED alert created!

    # Verify alert events now has 2 entries (NEW_LISTING and PRICE_CHANGED)
    alerts_q3 = await test_db_session.execute(
        select(AlertEvent).where(AlertEvent.listing_id == listings[0].id).order_by(AlertEvent.created_at)
    )
    alerts3 = alerts_q3.scalars().all()
    assert len(alerts3) == 2
    assert alerts3[0].event_type == "NEW_LISTING"
    assert alerts3[1].event_type == "PRICE_CHANGED"
    assert alerts3[1].payload_json["old_price"] == 85000.0
    assert alerts3[1].payload_json["price"] == 79000.0

    # Verify PriceHistory table recorded both prices
    ph_q = await test_db_session.execute(
        select(PriceHistory).where(PriceHistory.listing_id == listings[0].id)
    )
    history = ph_q.scalars().all()
    assert len(history) == 2
