from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List, Optional
import structlog

from app.schemas.listing import RawListing

logger = structlog.get_logger(__name__)


class BaseProvider(ABC):
    """
    Abstract base class defining the marketplace provider contract.
    All providers (MachineryTrader, Machinio, Facebook, etc.) must implement this interface.
    """

    @property
    @abstractmethod
    def source(self) -> str:
        """Unique identifier of the provider source (e.g., 'machinerytrader')."""
        pass

    @abstractmethod
    async def search(self, query: str, max_pages: int = 1) -> List[RawListing]:
        """
        Execute search on the marketplace provider for a given query and return parsed RawListings.
        """
        pass

    @abstractmethod
    def parse_page(self, html: str, base_url: str = "") -> List[RawListing]:
        """
        Parse raw HTML content into a list of standardized RawListing instances.
        Must enforce the provider contract (canonical URL, stable external ID, decimal price).
        """
        pass

    @abstractmethod
    async def fetch_page(self, url: str) -> str:
        """
        Fetch HTML content from a given marketplace URL.
        """
        pass

    async def close(self) -> None:
        """Clean up network sessions, browser instances, or connection pools."""
        pass

    async def __aenter__(self) -> BaseProvider:
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        await self.close()
