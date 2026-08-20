from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

try:
    from xauusd_technical_analysis_chart_v2_1 import ContractSpec, IBGateway
    from ibapi.contract import Contract
    IBAPI_AVAILABLE = True
except SystemExit:
    # ibapi (TWS API Python client) isn't installed in this environment; the
    # module raises SystemExit on import. These tests need the real IB
    # client/wrapper base classes, so skip rather than fail the whole suite.
    IBAPI_AVAILABLE = False


def make_spec(symbol: str = "GC") -> ContractSpec:
    return ContractSpec(display_symbol=symbol, contract=Contract(), what_to_show="TRADES")


def fake_df() -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
        index=pd.to_datetime(["2026-08-20T00:00:00Z"]),
    )


@unittest.skipUnless(IBAPI_AVAILABLE, "ibapi (TWS API Python client) is not installed")
class HistoricalRetryTests(unittest.TestCase):
    def test_recovers_after_transient_timeouts_within_budget(self):
        ib = IBGateway()
        attempts = []

        def flaky(spec, duration, bar_size, timeout):
            attempts.append(1)
            if len(attempts) < 3:
                raise TimeoutError(f"{spec.display_symbol}: historical data timeout ({timeout:.0f}s)")
            return fake_df()

        with patch.object(ib, "_request_historical_once", side_effect=flaky), \
             patch("xauusd_technical_analysis_chart_v2_1.time.sleep") as mock_sleep:
            df = ib.get_historical(make_spec(), "2 M", "1 hour")

        self.assertEqual(len(attempts), 3)
        self.assertTrue(df.equals(fake_df()))
        # two retries -> two backoff sleeps, using the configured schedule
        mock_sleep.assert_any_call(15.0)
        mock_sleep.assert_any_call(25.0)

    def test_raises_last_timeout_once_retry_budget_is_exhausted(self):
        ib = IBGateway()

        def always_times_out(spec, duration, bar_size, timeout):
            raise TimeoutError(f"{spec.display_symbol}: historical data timeout ({timeout:.0f}s)")

        with patch.object(ib, "_request_historical_once", side_effect=always_times_out), \
             patch("xauusd_technical_analysis_chart_v2_1.time.sleep") as mock_sleep:
            with self.assertRaises(TimeoutError):
                ib.get_historical(make_spec("ES"), "2 M", "1 hour", retry_attempts=2)

        self.assertEqual(mock_sleep.call_count, 2)

    def test_non_timeout_errors_are_not_retried(self):
        ib = IBGateway()
        attempts = []

        def permission_error(spec, duration, bar_size, timeout):
            attempts.append(1)
            raise RuntimeError("IB 354: Requested market data is not subscribed")

        with patch.object(ib, "_request_historical_once", side_effect=permission_error), \
             patch("xauusd_technical_analysis_chart_v2_1.time.sleep") as mock_sleep:
            with self.assertRaises(RuntimeError):
                ib.get_historical(make_spec("NQ"), "2 M", "1 hour")

        self.assertEqual(len(attempts), 1)
        mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
