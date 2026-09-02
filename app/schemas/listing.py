from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

from app.utils.url import canonicalize_url, extract_stable_external_id


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MatchClassification(str, Enum):
    ACCEPT = "ACCEPT"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


class RawListing(BaseModel):
    """
    Raw listing returned by any marketplace provider.
    Enforces the Provider Contract:
    - Stable external_id independent of query parameters, tracking tokens, and fragments.
    - Canonicalized clean URL.
    - Monetary values represented using Decimal / NUMERIC.
    - Future-ready cross-source deduplication attributes (serial_number, seller_name).
    """

    source: str
    external_id: str
    title: str
    description: Optional[str] = None
    price: Optional[Decimal] = None
    currency: Optional[str] = "USD"
    location: Optional[str] = None
    url: str
    image_url: Optional[str] = None
    published_at: Optional[datetime] = None
    scraped_at: datetime = Field(default_factory=get_utc_now)

    # Future cross-source deduplication attributes
    serial_number: Optional[str] = None
    seller_name: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def enforce_provider_contract(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_url = str(data.get("url") or "")
            # Canonicalize listing URL
            canon_url = canonicalize_url(raw_url)
            data["url"] = canon_url

            # Enforce stable external ID independent of query/tracking parameters or URL fragments
            raw_id = data.get("external_id")
            data["external_id"] = extract_stable_external_id(canon_url, raw_id)

            # Ensure price is converted to Decimal if provided as float/int/str
            raw_price = data.get("price")
            if raw_price is not None:
                if isinstance(raw_price, (int, float, str)):
                    data["price"] = Decimal(str(raw_price))
        return data


class MatchResult(BaseModel):
    """Result of passing a listing through the equipment matcher engine."""

    classification: MatchClassification
    is_target_equipment: bool
    manufacturer: Optional[str] = None
    manufacturer_inferred: bool = False
    model: Optional[str] = None
    equipment_type: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    matched_rules: List[str] = Field(default_factory=list)
    rejection_reason: Optional[str] = None
    extracted_year: Optional[int] = None
    extracted_hours: Optional[float] = None
    extracted_price: Optional[Decimal] = None

    @field_validator("extracted_price", mode="before")
    @classmethod
    def convert_extracted_price(cls, v):
        if v is not None and not isinstance(v, Decimal):
            return Decimal(str(v))
        return v
