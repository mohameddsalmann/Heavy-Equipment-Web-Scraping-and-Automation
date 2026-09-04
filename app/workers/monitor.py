from __future__ import annotations

import argparse
import asyncio
import signal
from typing import Optional
import structlog

from app.config import settings
from app.workers.facebook_run_once import run_facebook_scan

logger = structlog.get_logger(__name__)


class ContinuousMonitor:
    """
    Continuous Facebook Marketplace monitoring worker.
    Runs non-overlapping polling cycles at configured interval (e.g. 120s),
    isolates region errors, backs off gracefully on consecutive provider failures,
    and handles SIGINT / SIGTERM for zero-data-loss graceful shutdowns.
    """

    def __init__(
        self,
        interval_seconds: Optional[int] = None,
        provider_name: Optional[str] = None,
        max_cycles: Optional[int] = None,
        regions: Optional[list[str]] = None,
        queries: Optional[list[str]] = None,
        max_results_per_query: int = 10,
    ) -> None:
        self.interval_seconds = interval_seconds or settings.MONITOR_INTERVAL_SECONDS or 120
        self.provider_name = provider_name or settings.FACEBOOK_PROVIDER
        self.max_cycles = max_cycles
        self.regions = regions
        self.queries = queries
        self.max_results_per_query = max_results_per_query
        self._stop_event = asyncio.Event()
        self._scan_lock = asyncio.Lock()
        self._consecutive_errors: int = 0
        self._max_backoff_seconds: int = 60

    def stop(self) -> None:
        """Signal monitor to cleanly shut down."""
        logger.info("continuous_monitor_shutdown_signaled")
        self._stop_event.set()

    async def run(self) -> None:
        """Main monitoring loop with non-overlapping execution and exponential backoff."""
        cycle_count = 0
        logger.info(
            "continuous_monitor_started",
            interval_seconds=self.interval_seconds,
            provider=self.provider_name,
            max_cycles=self.max_cycles,
            regions=self.regions,
            queries=self.queries,
        )

        while not self._stop_event.is_set():
            if self.max_cycles is not None and cycle_count >= self.max_cycles:
                logger.info("max_cycles_reached", cycles=cycle_count)
                break

            cycle_count += 1
            cycle_started = asyncio.get_event_loop().time()

            if self._scan_lock.locked():
                logger.warning("previous_scan_still_running_skipping_cycle", cycle=cycle_count)
            else:
                async with self._scan_lock:
                    try:
                        logger.info("starting_scan_cycle", cycle=cycle_count)
                        metrics = await run_facebook_scan(
                            queries=self.queries,
                            region_ids=self.regions,
                            max_results_per_query=self.max_results_per_query,
                            provider_name=self.provider_name,
                            persist=True,
                        )
                        self._consecutive_errors = 0

                        logger.info(
                            "scan_cycle_completed",
                            cycle=cycle_count,
                            duration=metrics["latency"]["scan_duration_seconds"],
                            total_raw=metrics["total_raw_listings"],
                            accepted=metrics["accepted"],
                            new=metrics["new_inserted"],
                            price_changed=metrics["price_changed"],
                            alerts=metrics["alerts_created"],
                        )
                    except Exception as exc:
                        self._consecutive_errors += 1
                        backoff = min(
                            self._max_backoff_seconds,
                            5 * (2 ** (self._consecutive_errors - 1)),
                        )
                        logger.error(
                            "scan_cycle_failed",
                            cycle=cycle_count,
                            consecutive_errors=self._consecutive_errors,
                            backoff_seconds=backoff,
                            error=str(exc),
                        )
                        # Back off before retrying
                        try:
                            await asyncio.wait_for(self._stop_event.wait(), timeout=float(backoff))
                        except asyncio.TimeoutError:
                            pass
                        continue

            if self.max_cycles is not None and cycle_count >= self.max_cycles:
                logger.info("max_cycles_reached", cycles=cycle_count)
                break

            # Sleep remaining time until next scheduled interval
            elapsed = asyncio.get_event_loop().time() - cycle_started
            sleep_time = max(0.1, float(self.interval_seconds) - elapsed)

            logger.info("waiting_next_cycle", sleep_seconds=round(sleep_time, 1))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_time)
            except asyncio.TimeoutError:
                pass

        logger.info("continuous_monitor_stopped", total_cycles=cycle_count)


def main() -> None:
    parser = argparse.ArgumentParser(description="Continuous Facebook Marketplace monitor worker.")
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Polling interval in seconds (default: 120)",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        help="Facebook provider to use (apify or brightdata)",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Max cycles to run before exiting (useful for testing)",
    )
    parser.add_argument(
        "--regions",
        nargs="+",
        default=None,
        help="Region IDs to monitor (e.g. us_texas ca_ontario)",
    )
    parser.add_argument(
        "--queries",
        nargs="+",
        default=None,
        help="Specific search queries to monitor",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=5,
        help="Max results per query per region",
    )
    args = parser.parse_args()

    monitor = ContinuousMonitor(
        interval_seconds=args.interval,
        provider_name=args.provider,
        max_cycles=args.max_cycles,
        regions=args.regions,
        queries=args.queries,
        max_results_per_query=args.max_results,
    )


    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Register OS signals
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, monitor.stop)
        except (NotImplementedError, AttributeError):
            # Windows signal handling fallback
            signal.signal(sig, lambda s, f: monitor.stop())

    try:
        loop.run_until_complete(monitor.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
