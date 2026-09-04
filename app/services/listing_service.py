from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AlertEvent, Listing, PriceHistory
from app.schemas.listing import MatchClassification, MatchResult, RawListing


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UpsertOutcome(str, Enum):
    NEW = "NEW"
    DUPLICATE = "DUPLICATE"
    PRICE_CHANGED = "PRICE_CHANGED"


class ListingService:
    """
    Handles atomic persistence, deduplication, and price tracking for equipment listings.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_listing(
        self,
        raw: RawListing,
        match: Optional[MatchResult] = None,
        auto_commit: bool = True,
    ) -> Tuple[Listing, UpsertOutcome]:
        """
        Atomically upsert a listing by (source, external_id).
        Guarantees deduplication under concurrent execution and logs price history on change.
        """
        now = get_utc_now()
        status_val = (
            match.classification.value
            if match
            else MatchClassification.ACCEPT.value
        )
        confidence_val = (
            Decimal(str(round(match.confidence, 2)))
            if match
            else Decimal("1.00")
        )

        # Attempt to find existing listing
        stmt = (
            select(Listing)
            .where(Listing.source == raw.source, Listing.external_id == raw.external_id)
        )
        result = await self.session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is not None:
            return await self._handle_existing(
                existing, raw, match, status_val, confidence_val, now, auto_commit
            )

        # Create new listing
        new_price = Decimal(str(raw.price)) if raw.price is not None else None
        new_currency = (raw.currency or "USD").upper()

        new_listing = Listing(
            id=uuid.uuid4(),
            source=raw.source,
            external_id=raw.external_id,
            url=raw.url,
            title=raw.title,
            description=raw.description,
            manufacturer=match.manufacturer if match else None,
            model=match.model if match else None,
            equipment_type=match.equipment_type if match else None,
            price=new_price,
            currency=new_currency,
            location=raw.location,
            image_url=raw.image_url,
            serial_number=raw.serial_number,
            seller_name=raw.seller_name,
            first_seen_at=now,
            last_seen_at=now,
            published_at=raw.published_at,
            confidence=confidence_val,
            status=status_val,
            created_at=now,
            updated_at=now,
        )

        try:
            self.session.add(new_listing)

            if new_price is not None:
                initial_price = PriceHistory(
                    id=uuid.uuid4(),
                    listing_id=new_listing.id,
                    price=new_price,
                    currency=new_listing.currency,
                    detected_at=now,
                )
                self.session.add(initial_price)

            # Only confirmed ACCEPT listings trigger alert outbox events
            if match and match.classification == MatchClassification.ACCEPT:
                alert_event = AlertEvent(
                    id=uuid.uuid4(),
                    event_type="NEW_LISTING",
                    listing_id=new_listing.id,
                    payload_json={
                        "event_type": "NEW_LISTING",
                        "listing_id": str(new_listing.id),
                        "external_id": new_listing.external_id,
                        "source": new_listing.source,
                        "title": new_listing.title,
                        "manufacturer": new_listing.manufacturer,
                        "model": new_listing.model,
                        "equipment_type": new_listing.equipment_type,
                        "price": float(new_listing.price) if new_listing.price is not None else None,
                        "currency": new_listing.currency,
                        "location": new_listing.location,
                        "url": new_listing.url,
                        "image_url": new_listing.image_url,
                        "serial_number": new_listing.serial_number,
                        "seller_name": new_listing.seller_name,
                        "confidence": float(match.confidence),
                        "detected_at": now.isoformat(),
                    },
                    created_at=now,
                )
                self.session.add(alert_event)

            if auto_commit:
                await self.session.commit()
            else:
                await self.session.flush()

            return new_listing, UpsertOutcome.NEW

        except IntegrityError:
            # Concurrent insert detected on (source, external_id)
            await self.session.rollback()

            # Poll until competing transaction commits
            refetched = None
            for _ in range(25):
                await asyncio.sleep(0.04)
                refetch_stmt = select(Listing).where(
                    Listing.source == raw.source, Listing.external_id == raw.external_id
                )
                refetch_res = await self.session.execute(refetch_stmt)
                refetched = refetch_res.scalar_one_or_none()
                if refetched is not None:
                    break

            if refetched is None:
                raise

            return await self._handle_existing(
                refetched, raw, match, status_val, confidence_val, now, auto_commit
            )

    async def _handle_existing(
        self,
        existing: Listing,
        raw: RawListing,
        match: Optional[MatchResult],
        status_val: str,
        confidence_val: float,
        now: datetime,
        auto_commit: bool,
    ) -> Tuple[Listing, UpsertOutcome]:
        """
        Update existing listing attributes, track last_seen_at, and record price changes.
        Rules:
        - 85000 -> 85000: DUPLICATE (no new history record)
        - 85000 -> 82000: PRICE_CHANGED (new PriceHistory record)
        - None -> 85000: PRICE_CHANGED (new PriceHistory record)
        - 85000 USD -> 85000 CAD: PRICE_CHANGED (currency shift recorded in PriceHistory)
        - 85000 -> None: DUPLICATE (missing price is NOT interpreted as a price drop;
          existing price is retained, no history record generated).
        """
        existing.last_seen_at = now
        existing.url = raw.url
        existing.title = raw.title
        if raw.description:
            existing.description = raw.description
        if raw.image_url:
            existing.image_url = raw.image_url
        if raw.location:
            existing.location = raw.location
        if raw.serial_number:
            existing.serial_number = raw.serial_number
        if raw.seller_name:
            existing.seller_name = raw.seller_name

        existing.status = status_val
        existing.confidence = confidence_val

        outcome = UpsertOutcome.DUPLICATE
        old_price = existing.price
        new_price = Decimal(str(raw.price)) if raw.price is not None else None
        old_currency = (existing.currency or "USD").upper()
        new_currency = (raw.currency or existing.currency or "USD").upper()

        if new_price is not None:
            price_changed = (old_price is None) or (new_price != old_price)
            currency_changed = (new_currency != old_currency)

            if price_changed or currency_changed:
                existing.price = new_price
                existing.currency = new_currency
                price_record = PriceHistory(
                    id=uuid.uuid4(),
                    listing_id=existing.id,
                    price=new_price,
                    currency=new_currency,
                    detected_at=now,
                )
                self.session.add(price_record)
                outcome = UpsertOutcome.PRICE_CHANGED

                # Only confirmed ACCEPT listings trigger alert outbox events
                if match and match.classification == MatchClassification.ACCEPT:
                    alert_event = AlertEvent(
                        id=uuid.uuid4(),
                        event_type="PRICE_CHANGED",
                        listing_id=existing.id,
                        payload_json={
                            "event_type": "PRICE_CHANGED",
                            "listing_id": str(existing.id),
                            "external_id": existing.external_id,
                            "source": existing.source,
                            "title": existing.title,
                            "manufacturer": existing.manufacturer,
                            "model": existing.model,
                            "equipment_type": existing.equipment_type,
                            "old_price": float(old_price) if old_price is not None else None,
                            "price": float(new_price) if new_price is not None else None,
                            "currency": new_currency,
                            "location": existing.location,
                            "url": existing.url,
                            "image_url": existing.image_url,
                            "serial_number": existing.serial_number,
                            "seller_name": existing.seller_name,
                            "confidence": float(match.confidence),
                            "detected_at": now.isoformat(),
                        },
                        created_at=now,
                    )
                    self.session.add(alert_event)
        else:
            # 85000 -> None: Do not interpret missing price as a price drop.
            # Preserve existing known price and currency, keeping outcome as DUPLICATE.
            pass

        if auto_commit:
            await self.session.commit()
        else:
            await self.session.flush()

        return existing, outcome
