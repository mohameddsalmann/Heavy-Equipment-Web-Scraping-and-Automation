from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock
import pytest

from app.config import load_watchlist
from app.matcher.equipment_matcher import EquipmentMatcher
from app.providers.machinerytrader import MachineryTraderProvider
from app.schemas.listing import MatchClassification, RawListing

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "machinerytrader" / "sample_wheel_loaders.html"
)


@pytest.fixture
def sample_html() -> str:
    """Loads the real saved MachineryTrader HTML fixture."""
    assert FIXTURE_PATH.exists(), f"Missing HTML fixture at {FIXTURE_PATH}"
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def provider() -> MachineryTraderProvider:
    return MachineryTraderProvider()


def test_machinerytrader_parse_fixture_extracts_listings(provider, sample_html):
    """Verify parser extracts multiple listings from saved HTML fixture."""
    listings = provider.parse_page(sample_html)
    assert len(listings) >= 20, f"Expected at least 20 listings, got {len(listings)}"

    for item in listings:
        assert isinstance(item, RawListing)
        assert item.source == "machinerytrader"
        assert item.external_id, "external_id must not be empty"
        assert item.title, "title must not be empty"
        assert item.url.startswith("https://"), f"URL must be canonical https: {item.url}"
        assert "?" not in item.external_id, "external_id must be stripped of query tokens"
        assert "#" not in item.external_id, "external_id must be stripped of fragments"
        if item.price is not None:
            assert isinstance(item.price, Decimal), "Price must be Decimal instance"
            assert item.price > 0


def test_machinerytrader_provider_contract_deduplication(provider, sample_html):
    """Ensure in-page listings are strictly deduplicated by external_id."""
    listings = provider.parse_page(sample_html)
    ids = [item.external_id for item in listings]
    assert len(ids) == len(set(ids)), "Provider output must not contain duplicate external_ids"


def test_machinerytrader_parse_rich_attributes(provider, sample_html):
    """Verify extraction of serial number, seller name, and location."""
    listings = provider.parse_page(sample_html)

    # Check for known listing with serial number and seller
    target = next((item for item in listings if item.external_id == "258894117"), None)
    assert target is not None, "Expected listing 258894117 to be present"
    assert target.price == Decimal("59900")
    assert target.currency == "USD"
    assert "420F2" in target.title
    assert target.serial_number == "CAT0420FHHWD00209"
    assert target.seller_name == "BROOKSIDE EQUIPMENT"
    assert "Massachusetts" in (target.location or "")

    # Check that across all listings, multiple have serial numbers and sellers
    serial_count = sum(1 for item in listings if item.serial_number)
    seller_count = sum(1 for item in listings if item.seller_name)
    assert serial_count >= 5, f"Expected at least 5 listings with serial numbers, got {serial_count}"
    assert seller_count >= 5, f"Expected at least 5 listings with seller names, got {seller_count}"


def test_machinerytrader_parse_empty_or_malformed_html(provider):
    """Test handling of edge case inputs."""
    assert provider.parse_page("") == []
    assert provider.parse_page("<html><body><h1>No listings</h1></body></html>") == []

    # Malformed JSON-LD with valid DOM card fallback
    malformed_json_ld = """
    <html>
      <head>
        <script type="application/ld+json">
          { "broken": json without closing ...
        </script>
      </head>
      <body>
        <div class="listing-card-grid">
          <a class="list-listing-title-link" href="/listing/for-sale/99912345/caterpillar-950m">
            2018 CATERPILLAR 950M
          </a>
          <span class="price">$125,000</span>
          <div class="machine-location">Location: Austin, Texas</div>
        </div>
      </body>
    </html>
    """
    listings = provider.parse_page(malformed_json_ld)
    assert len(listings) == 1
    item = listings[0]
    assert item.external_id == "99912345"
    assert item.title == "2018 CATERPILLAR 950M"
    assert item.price == Decimal("125000")
    assert item.location == "Austin, Texas"


def test_machinerytrader_matcher_integration(provider, sample_html):
    """
    Pass real parsed listings through the EquipmentMatcher pipeline
    to verify classification behavior.
    """
    watchlist = load_watchlist()
    matcher = EquipmentMatcher(watchlist)

    listings = provider.parse_page(sample_html)
    classified_results = [matcher.match(item) for item in listings]

    classifications = {r.classification for r in classified_results}
    assert len(classifications) > 0

    # Ensure match results conform to contract
    for result in classified_results:
        assert result.classification in {
            MatchClassification.ACCEPT,
            MatchClassification.REVIEW,
            MatchClassification.REJECT,
        }
        assert 0.0 <= result.confidence <= 1.0


@pytest.mark.asyncio
async def test_base_provider_lifecycle():
    from unittest.mock import AsyncMock
    from app.providers.base import BaseProvider

    class DummyProvider(BaseProvider):
        @property
        def source(self) -> str:
            return "dummy"

        async def search(self, query: str, max_pages: int = 1):
            return []

        def parse_page(self, html: str, base_url: str = ""):
            return []

        async def fetch_page(self, url: str):
            return ""

    async with DummyProvider() as p:
        assert p.source == "dummy"
        res = await p.search("test")
        assert res == []


@pytest.mark.asyncio
async def test_machinerytrader_provider_lifecycle_and_search_mock(sample_html):
    provider = MachineryTraderProvider(headless=True)
    assert provider.source == "machinerytrader"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(provider, "fetch_page", AsyncMock(return_value=sample_html))
        results = await provider.search("CAT wheel loader", max_pages=1)
        assert len(results) >= 20

    await provider.close()
