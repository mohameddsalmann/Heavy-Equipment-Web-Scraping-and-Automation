"""Facebook Marketplace providers package."""
from __future__ import annotations

from typing import Optional
from app.config import settings
from app.providers.facebook.apify_provider import ApifyFacebookProvider
from app.providers.facebook.base import BaseFacebookProvider
from app.providers.facebook.brightdata_provider import BrightDataFacebookProvider
from app.providers.facebook.models import FacebookRegion, load_facebook_regions


def get_facebook_provider(
    provider_name: Optional[str] = None,
    actor_id: Optional[str] = None,
) -> BaseFacebookProvider:
    """
    Factory creating configured Facebook Marketplace provider instance.
    Selection is driven by FACEBOOK_PROVIDER setting ('apify' or 'brightdata').
    """
    chosen = (provider_name or settings.FACEBOOK_PROVIDER or "apify").lower().strip()
    if chosen == "brightdata":
        return BrightDataFacebookProvider()
    return ApifyFacebookProvider(actor_id=actor_id)



__all__ = [
    "BaseFacebookProvider",
    "ApifyFacebookProvider",
    "BrightDataFacebookProvider",
    "FacebookRegion",
    "load_facebook_regions",
    "get_facebook_provider",
]
