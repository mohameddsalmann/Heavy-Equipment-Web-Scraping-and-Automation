from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest
from sqlalchemy import select

from app.database.models import Listing, SourceScanStatus
from app.providers.machinerytrader import MachineryTraderProvider
from app.schemas.listing import MatchClassification
from app.workers.run_once import run_scan_once

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "machinerytrader" / "sample_wheel_loaders.html"
)


@pytest.mark.asyncio
async def test_run_scan_once_pipeline(test_db_session):
    """
    Test run_scan_once worker end-to-end using saved HTML fixture listings.
    Verifies matcher evaluation, database persistence, deduplication, and scan metrics.
    """
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        fixture_html = f.read()

    provider = MachineryTraderProvider()
    parsed_listings = provider.parse_page(fixture_html)
    assert len(parsed_listings) > 0

    # Mock provider search to return fixture listings without live network request
    with patch.object(MachineryTraderProvider, "search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = parsed_listings

        metrics = await run_scan_once(
            query="CAT wheel loader",
            max_pages=1,
            headless=True,
            persist=True,
        )

        assert metrics["total_scraped"] == len(parsed_listings)
        assert metrics["accepted"] + metrics["review"] + metrics["rejected"] == len(parsed_listings)
        assert metrics["new_inserted"] > 0
        assert metrics["duration_ms"] > 0

        # Verify listings are in the database
        stmt = select(Listing).where(Listing.source == "machinerytrader")
        res = await test_db_session.execute(stmt)
        saved_listings = res.scalars().all()
        assert len(saved_listings) == metrics["new_inserted"]

        # Verify accepted listings have valid statuses
        for item in saved_listings:
            assert item.status in {MatchClassification.ACCEPT.value, MatchClassification.REVIEW.value}

        # Verify SourceScanStatus was recorded
        status_stmt = select(SourceScanStatus).where(SourceScanStatus.source == "machinerytrader")
        status_res = await test_db_session.execute(status_stmt)
        status_row = status_res.scalar_one_or_none()
        assert status_row is not None
        assert status_row.listings_received == len(parsed_listings)
        assert status_row.new_matches == metrics["accepted"] + metrics["review"]
        assert status_row.error is None


def test_print_scan_report(capsys):
    from app.workers.run_once import print_scan_report

    sample_metrics = {
        "source": "machinerytrader",
        "query": "CAT wheel loader",
        "duration_ms": 1250,
        "total_scraped": 10,
        "accepted": 2,
        "review": 1,
        "rejected": 7,
        "new_inserted": 3,
        "price_changed": 0,
        "duplicates": 0,
        "examples": {
            "accepted": [
                {
                    "id": "123",
                    "model": "950M",
                    "confidence": 0.95,
                    "price": "150000",
                    "title": "2018 CAT 950M",
                    "serial_number": "CAT123",
                    "seller": "Acme Equipment",
                    "url": "https://example.com/123",
                }
            ],
            "review": [
                {
                    "id": "456",
                    "model": "950",
                    "confidence": 0.60,
                    "reason": "Manufacturer ambiguous",
                    "price": "120000",
                    "title": "CAT Loader",
                    "url": "https://example.com/456",
                }
            ],
            "rejected": [
                {
                    "id": "789",
                    "title": "CAT Filter Element",
                    "reason": "Negative keyword",
                    "price": "50",
                }
            ],
        },
        "limitations": ["Rate limit warning"],
    }
    print_scan_report(sample_metrics)
    captured = capsys.readouterr()
    assert "MACHINERYTRADER LIVE SCAN RESULTS" in captured.out
    assert "ACCEPTED EXAMPLES" in captured.out
    assert "REVIEW EXAMPLES" in captured.out
    assert "REJECTED EXAMPLES" in captured.out
    assert "LIMITATIONS FOUND" in captured.out


def test_main_cli(monkeypatch):
    from app.workers.run_once import main

    async def mock_run(*args, **kwargs):
        return {
            "source": "machinerytrader",
            "query": "test",
            "duration_ms": 10,
            "total_scraped": 0,
            "accepted": 0,
            "review": 0,
            "rejected": 0,
            "new_inserted": 0,
            "price_changed": 0,
            "duplicates": 0,
            "examples": {"accepted": [], "review": [], "rejected": []},
            "limitations": [],
        }

    monkeypatch.setattr("app.workers.run_once.run_scan_once", mock_run)
    monkeypatch.setattr("sys.argv", ["run_once.py", "--query", "test", "--no-save"])
    main()
