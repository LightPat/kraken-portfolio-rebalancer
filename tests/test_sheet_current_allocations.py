import os
import unittest
from unittest.mock import Mock, patch

from sheets import (
    get_btc_cold_balance,
    update_current_allocations_in_sheet,
    update_sdca_values,
)


class UpdateCurrentAllocationsTests(unittest.TestCase):
    def test_writes_short_used_margin_as_positive_current_allocation(self):
        worksheet = Mock()
        worksheet.get_values.return_value = [["BTC"]]

        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = worksheet

        portfolio = (
            {},
            {},
            {"BTC": 36.0},
            {},
            36.0,
            {},
        )
        with (
            patch.dict(os.environ, {"GOOGLE_DOCS_SHEET_ID": "test-sheet"}),
            patch("sheets.fetch_portfolio", return_value=portfolio),
            patch("sheets.get_gspread_client", return_value=client),
            patch(
                "sheets.get_sdca_sheet_values",
                return_value={
                    "cash_usd": 0.0,
                    "buy_pct": 0.1,
                    "kraken_btc": 0.0,
                    "cold_btc": 0.0,
                },
            ),
            patch("sheets.get_btc_cold_balance", return_value=0.0),
            patch("sheets.get_btc_usd_price", return_value=50_000.0),
            patch("sheets.get_kraken_btc_balance", return_value=0.0),
            patch("sheets.update_sdca_values"),
        ):
            update_current_allocations_in_sheet()

        worksheet.update.assert_any_call("E8:E8", [[36.0]], value_input_option="RAW")
        worksheet.update.assert_any_call("E5", [[0.0]], value_input_option="RAW")

    def test_separates_sdca_btc_from_rsps_btc_and_refreshes_sdca_cells(self):
        worksheet = Mock()
        worksheet.get_values.side_effect = lambda cell_range: (
            [["BTC"]] if cell_range == "A8:A" else [["USD"]]
        )
        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = worksheet
        portfolio = (
            {"BTC": 25_000.0},
            {},
            {},
            {},
            30_000.0,
            {"USD": 500.0, "USDC": 0.0, "USDG": 0.0},
        )

        with (
            patch.dict(os.environ, {"GOOGLE_DOCS_SHEET_ID": "test-sheet"}),
            patch("sheets.fetch_portfolio", return_value=portfolio),
            patch("sheets.get_gspread_client", return_value=client),
            patch(
                "sheets.get_sdca_sheet_values",
                return_value={
                    "cash_usd": 100.0,
                    "buy_pct": 0.1,
                    "kraken_btc": 0.1,
                    "cold_btc": 0.2,
                },
            ),
            patch("sheets.get_btc_cold_balance", return_value=0.25),
            patch("sheets.get_btc_usd_price", return_value=50_000.0),
            patch("sheets.get_kraken_btc_balance", return_value=0.5),
            patch("sheets.update_sdca_values") as update_sdca,
        ):
            update_current_allocations_in_sheet()

        update_sdca.assert_called_once_with(100.0, 0.05, 0.25, 50_000.0)
        worksheet.update.assert_any_call("E5", [[0.45]], value_input_option="RAW")
        worksheet.update.assert_any_call(
            "E8:E8", [[22_500.0]], value_input_option="RAW"
        )
        worksheet.update.assert_any_call("I8:I8", [[400.0]], value_input_option="RAW")


class SdcaSheetTests(unittest.TestCase):
    def test_sdca_updates_only_owned_balance_cells_and_equity(self):
        worksheet = Mock()
        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = worksheet

        with (
            patch.dict(os.environ, {"GOOGLE_DOCS_SHEET_ID": "test-sheet"}),
            patch("sheets.get_gspread_client", return_value=client),
        ):
            equity = update_sdca_values(100.0, 0.1, 0.2, 50_000.0)

        self.assertEqual(equity, 15_100.0)
        worksheet.batch_update.assert_called_once_with(
            [
                {"range": "Q3", "values": [[100.0]]},
                {"range": "Q7", "values": [[0.1]]},
                {"range": "Q8", "values": [[0.2]]},
                {"range": "Q10", "values": [[15_100.0]]},
            ],
            value_input_option="RAW",
        )

    def test_cold_wallet_balance_sums_deduplicated_addresses(self):
        first_address = Mock()
        first_address.json.return_value = {
            "chain_stats": {"funded_txo_sum": 100_000_000, "spent_txo_sum": 10_000_000},
            "mempool_stats": {"funded_txo_sum": 3_000_000, "spent_txo_sum": 2_000_000},
        }
        second_address = Mock()
        second_address.json.return_value = {
            "chain_stats": {"funded_txo_sum": 20_000_000, "spent_txo_sum": 0},
            "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0},
        }

        with (
            patch.dict(
                os.environ,
                {"BTC_COLD_ADDRESS": "bc1first, bc1second,bc1first"},
            ),
            patch(
                "sheets.httpx.get",
                side_effect=[first_address, second_address],
            ) as request,
        ):
            balance = get_btc_cold_balance()

        self.assertAlmostEqual(balance, 1.11)
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
