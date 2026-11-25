import os
import unittest
from unittest.mock import Mock, patch

from embit import bip32, networks
from sheets import (
    get_btc_cold_balance,
    update_current_allocations_in_sheet,
    update_sdca_cash_in_sheet,
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
        ):
            update_current_allocations_in_sheet()

        worksheet.update.assert_any_call("E5", [[0.45]], value_input_option="RAW")
        worksheet.update.assert_any_call(
            "E8:E8", [[22_500.0]], value_input_option="RAW"
        )
        worksheet.update.assert_any_call("I8:I8", [[400.0]], value_input_option="RAW")
        worksheet.batch_update.assert_called_once_with(
            [
                {"range": "Q7", "values": [[0.05]]},
                {"range": "Q8", "values": [[0.25]]},
                {"range": "Q10", "values": [[15_100.0]]},
            ],
            value_input_option="RAW",
        )


class SdcaSheetTests(unittest.TestCase):
    def test_cash_update_writes_only_q3(self):
        worksheet = Mock()
        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = worksheet

        with (
            patch.dict(os.environ, {"GOOGLE_DOCS_SHEET_ID": "test-sheet"}),
            patch("sheets.get_gspread_client", return_value=client),
        ):
            update_sdca_cash_in_sheet(100.0)

        worksheet.update.assert_called_once_with(
            "Q3", [[100.0]], value_input_option="RAW"
        )

    def test_missing_trezor_public_key_means_zero_cold_btc(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_btc_cold_balance(), 0.0)
        with patch.dict(os.environ, {"TREZOR_BTC_PUB_KEY": ""}, clear=False):
            self.assertEqual(get_btc_cold_balance(), 0.0)

    def test_xpub_derivation_uses_prefix_to_choose_address_type(self):
        account = bip32.HDKey.from_seed(bytes(range(16))).derive("m/84h/0h/0h")
        network = networks.NETWORKS["main"]
        expected_prefixes = {
            "xpub": "1",
            "ypub": "3",
            "zpub": "bc1q",
        }

        from sheets import _derive_trezor_receive_address

        for prefix, version in (
            ("xpub", "xpub"),
            ("ypub", "ypub"),
            ("zpub", "zpub"),
        ):
            key = account.to_public(version=network[version])
            address = _derive_trezor_receive_address(key, 0)
            self.assertTrue(address.startswith(expected_prefixes[prefix]))

    def test_cold_balance_scans_until_gap_of_twenty_unused_addresses(self):
        account = bip32.HDKey.from_seed(bytes(range(16))).derive("m/84h/0h/0h")
        xpub = account.to_public(
            version=networks.NETWORKS["main"]["zpub"]
        ).to_base58()

        def address_stats(address):
            index = addresses.index(address)
            return {
                "chain_stats": {
                    "funded_txo_sum": 1_000 if index == 1 else 0,
                    "spent_txo_sum": 0,
                    "tx_count": 1 if index == 1 else 0,
                },
                "mempool_stats": {
                    "funded_txo_sum": 500 if index == 1 else 0,
                    "spent_txo_sum": 0,
                    "tx_count": 0,
                },
            }

        from sheets import _derive_trezor_receive_address

        addresses = [
            _derive_trezor_receive_address(
                account.to_public(version=networks.NETWORKS["main"]["zpub"]),
                index,
            )
            for index in range(25)
        ]
        with (
            patch.dict(os.environ, {"TREZOR_BTC_PUB_KEY": xpub}),
            patch("sheets._fetch_mempool_address", side_effect=address_stats) as fetch,
        ):
            self.assertAlmostEqual(get_btc_cold_balance(), 0.000015)

        self.assertEqual(fetch.call_count, 25)

    def test_sdca_kraken_btc_is_capped_at_actual_kraken_total(self):
        worksheet = Mock()
        worksheet.get_values.return_value = [["BTC"]]
        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = worksheet
        portfolio = (
            {"BTC": 25_000.0},
            {},
            {},
            {},
            25_000.0,
            {"USD": 0.0, "USDC": 0.0, "USDG": 0.0},
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
                    "kraken_btc": 1.0,
                    "cold_btc": 0.0,
                },
            ),
            patch("sheets.get_btc_cold_balance", return_value=0.0),
            patch("sheets.get_btc_usd_price", return_value=50_000.0),
            patch("sheets.get_kraken_btc_balance", return_value=0.5),
        ):
            update_current_allocations_in_sheet()

        worksheet.batch_update.assert_called_once_with(
            [
                {"range": "Q7", "values": [[0.5]]},
                {"range": "Q8", "values": [[0.0]]},
                {"range": "Q10", "values": [[25_000.0]]},
            ],
            value_input_option="RAW",
        )
        worksheet.update.assert_any_call("E5", [[0.0]], value_input_option="RAW")


if __name__ == "__main__":
    unittest.main()
