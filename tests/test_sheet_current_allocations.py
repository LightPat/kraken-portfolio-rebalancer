import os
import unittest
from unittest.mock import Mock, patch

from embit import bip32, networks
from sheets import (
    clear_btc_cold_balance_cache,
    get_btc_cold_balance,
    get_sdca_cash_on_kraken,
    get_sdca_sheet_values,
    update_current_allocations_in_sheet,
    update_sdca_cash_after_fill,
    update_sdca_change_in_sheet,
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
                    "change_pct": 0.1,
                    "kraken_btc": 0.0,
                    "cold_btc": 0.0,
                },
            ),
            patch("sheets.get_sdca_cash_on_kraken", return_value=0.0),
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
                    "change_pct": 0.1,
                    "kraken_btc": 0.1,
                    "cold_btc": 0.2,
                },
            ),
            patch("sheets.get_sdca_cash_on_kraken", return_value=50.0),
            patch("sheets.get_btc_cold_balance", return_value=0.25),
            patch("sheets.get_btc_usd_price", return_value=50_000.0),
            patch("sheets.get_kraken_btc_balance", return_value=0.5),
        ):
            update_current_allocations_in_sheet()

        worksheet.update.assert_any_call("E5", [[0.45]], value_input_option="RAW")
        worksheet.update.assert_any_call(
            "E8:E8", [[22_500.0]], value_input_option="RAW"
        )
        worksheet.update.assert_any_call("I8:I8", [[450.0]], value_input_option="RAW")
        worksheet.batch_update.assert_called_once_with(
            [
                {"range": "Q7", "values": [[0.05]]},
                {"range": "Q8", "values": [[0.25]]},
                {"range": "Q10", "values": [[15_100.0]]},
            ],
            value_input_option="RAW",
        )


class SdcaSheetTests(unittest.TestCase):
    def test_sdca_buy_subtracts_filled_cost_from_both_cash_cells(self):
        worksheet = Mock()
        worksheet.acell.side_effect = lambda cell: Mock(
            value={"B4": "$100.00", "B5": "$40.00"}[cell]
        )
        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = worksheet
        with (
            patch.dict(os.environ, {"GOOGLE_DOCS_SHEET_ID": "test-sheet"}),
            patch("sheets.get_gspread_client", return_value=client),
        ):
            updated = update_sdca_cash_after_fill("buy", 12.5)

        self.assertEqual(updated, {"usd_left": 87.5, "cash_on_kraken": 27.5})
        worksheet.batch_update.assert_called_once_with(
            [
                {"range": "B4", "values": [[87.5]]},
                {"range": "B5", "values": [[27.5]]},
            ],
            value_input_option="RAW",
        )

    def test_sdca_sell_adds_net_proceeds_to_both_cash_cells(self):
        worksheet = Mock()
        worksheet.acell.side_effect = lambda cell: Mock(
            value={"B4": "100", "B5": "40"}[cell]
        )
        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = worksheet
        with (
            patch.dict(os.environ, {"GOOGLE_DOCS_SHEET_ID": "test-sheet"}),
            patch("sheets.get_gspread_client", return_value=client),
        ):
            updated = update_sdca_cash_after_fill("sell", 9.75)

        self.assertEqual(updated, {"usd_left": 109.75, "cash_on_kraken": 49.75})

    def test_sdca_cash_updates_are_floored_at_zero(self):
        worksheet = Mock()
        worksheet.acell.side_effect = lambda cell: Mock(
            value={"B4": "5", "B5": "0"}[cell]
        )
        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = worksheet
        with (
            patch.dict(os.environ, {"GOOGLE_DOCS_SHEET_ID": "test-sheet"}),
            patch("sheets.get_gspread_client", return_value=client),
        ):
            updated = update_sdca_cash_after_fill("buy", 10)

        self.assertEqual(updated, {"usd_left": 0.0, "cash_on_kraken": 0.0})

    def test_sdca_sheet_reader_accepts_signed_decimal_change(self):
        worksheet = Mock()
        worksheet.acell.side_effect = lambda cell: Mock(
            value={
                "Q3": "125.50",
                "Q4": "-0.25",
                "Q7": "0.1",
                "Q8": "0.2",
            }[cell]
        )

        with patch("sheets._get_signals_worksheet", return_value=worksheet):
            values = get_sdca_sheet_values()

        self.assertEqual(values["change_pct"], -0.25)
        self.assertEqual(values["cash_usd"], 125.5)

    def test_sdca_sheet_reader_converts_formatted_percentage_to_fraction(self):
        worksheet = Mock()
        worksheet.acell.side_effect = lambda cell: Mock(
            value={
                "Q3": "125.50",
                "Q4": "8.656%",
                "Q7": "0.1",
                "Q8": "0.2",
            }[cell]
        )

        with patch("sheets._get_signals_worksheet", return_value=worksheet):
            values = get_sdca_sheet_values()

        self.assertAlmostEqual(values["change_pct"], 0.08656)

    def test_sdca_sheet_reader_preserves_sign_on_formatted_percentage(self):
        worksheet = Mock()
        worksheet.acell.side_effect = lambda cell: Mock(
            value={
                "Q3": "125.50",
                "Q4": "-8.656%",
                "Q7": "0.1",
                "Q8": "0.2",
            }[cell]
        )

        with patch("sheets._get_signals_worksheet", return_value=worksheet):
            values = get_sdca_sheet_values()

        self.assertAlmostEqual(values["change_pct"], -0.08656)

    def test_sdca_cash_on_kraken_reader_treats_empty_cell_as_zero(self):
        worksheet = Mock()
        worksheet.acell.return_value.value = ""
        with patch("sheets._get_signals_worksheet", return_value=worksheet):
            self.assertEqual(get_sdca_cash_on_kraken(), 0.0)

    def test_sdca_cash_on_kraken_reader_returns_nonnegative_amount(self):
        worksheet = Mock()
        worksheet.acell.return_value.value = "$125.50"
        with patch("sheets._get_signals_worksheet", return_value=worksheet):
            self.assertEqual(get_sdca_cash_on_kraken(), 125.5)

    def test_sdca_cash_on_kraken_reader_rejects_negative_amount(self):
        worksheet = Mock()
        worksheet.acell.return_value.value = "-1"
        with patch("sheets._get_signals_worksheet", return_value=worksheet):
            with self.assertRaisesRegex(ValueError, "non-negative"):
                get_sdca_cash_on_kraken()

    def test_allocation_refresh_subtracts_q6_not_q3_and_does_not_write_either(self):
        worksheet = Mock()
        worksheet.get_values.side_effect = lambda cell_range: (
            [["USD"]] if cell_range in {"A8:A", "H8:H"} else []
        )
        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = worksheet
        portfolio = (
            {},
            {},
            {},
            {},
            1_000.0,
            {"USD": 400.0, "USDC": 0.0, "USDG": 0.0},
        )

        with (
            patch.dict(os.environ, {"GOOGLE_DOCS_SHEET_ID": "test-sheet"}),
            patch("sheets.fetch_portfolio", return_value=portfolio),
            patch("sheets.get_gspread_client", return_value=client),
            patch(
                "sheets.get_sdca_sheet_values",
                return_value={
                    "cash_usd": 250.0,
                    "change_pct": 0.0,
                    "kraken_btc": 0.0,
                    "cold_btc": 0.0,
                },
            ),
            patch("sheets.get_sdca_cash_on_kraken", return_value=100.0),
            patch("sheets.get_btc_cold_balance", return_value=0.0),
            patch("sheets.get_btc_usd_price", return_value=50_000.0),
            patch("sheets.get_kraken_btc_balance", return_value=0.0),
        ):
            update_current_allocations_in_sheet()

        worksheet.update.assert_any_call("E8:E8", [[0.0]], value_input_option="RAW")
        worksheet.update.assert_any_call("I8:I8", [[300.0]], value_input_option="RAW")
        for call in worksheet.update.call_args_list:
            self.assertNotIn(call.args[0], {"Q3", "Q6"})
        for batch in worksheet.batch_update.call_args_list:
            self.assertNotIn(
                "Q3", [item["range"] for item in batch.args[0]]
            )
            self.assertNotIn(
                "Q6", [item["range"] for item in batch.args[0]]
            )

    def test_change_update_writes_only_q4(self):
        worksheet = Mock()
        with patch("sheets._get_signals_worksheet", return_value=worksheet):
            update_sdca_change_in_sheet(-0.25)

        worksheet.update.assert_called_once_with(
            "Q4", [[-0.25]], value_input_option="RAW"
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

    def test_back_to_back_allocation_refreshes_scan_blockchain_once(self):
        account = bip32.HDKey.from_seed(bytes(range(16))).derive("m/84h/0h/0h")
        xpub = account.to_public(
            version=networks.NETWORKS["main"]["zpub"]
        ).to_base58()
        worksheet = Mock()
        worksheet.get_values.return_value = []
        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = worksheet
        portfolio = ({}, {}, {}, {}, 0.0, {"USD": 0.0, "USDC": 0.0, "USDG": 0.0})
        unused_address = {
            "chain_stats": {
                "funded_txo_sum": 0,
                "spent_txo_sum": 0,
                "tx_count": 0,
            },
            "mempool_stats": {
                "funded_txo_sum": 0,
                "spent_txo_sum": 0,
                "tx_count": 0,
            },
        }

        clear_btc_cold_balance_cache()
        try:
            with (
                patch.dict(
                    os.environ,
                    {
                        "GOOGLE_DOCS_SHEET_ID": "test-sheet",
                        "TREZOR_BTC_PUB_KEY": xpub,
                    },
                ),
                patch("sheets.fetch_portfolio", return_value=portfolio),
                patch("sheets.get_sdca_sheet_values", return_value={
                    "cash_usd": 0.0,
                    "change_pct": 0.1,
                    "kraken_btc": 0.0,
                    "cold_btc": 0.0,
                }),
                patch("sheets.get_sdca_cash_on_kraken", return_value=0.0),
                patch("sheets.get_btc_usd_price", return_value=50_000.0),
                patch("sheets.get_kraken_btc_balance", return_value=0.0),
                patch("sheets.get_gspread_client", return_value=client),
                patch(
                    "sheets._fetch_mempool_address", return_value=unused_address
                ) as fetch,
                patch("sheets.time.monotonic", return_value=1_000.0),
            ):
                update_current_allocations_in_sheet()
                update_current_allocations_in_sheet()

            self.assertEqual(fetch.call_count, 20)
        finally:
            clear_btc_cold_balance_cache()

    def test_expired_scan_rechecks_used_addresses_then_resumes_after_highest_index(
        self,
    ):
        account = bip32.HDKey.from_seed(bytes(range(16))).derive("m/84h/0h/0h")
        public_key = account.to_public(version=networks.NETWORKS["main"]["zpub"])
        xpub = public_key.to_base58()
        from sheets import _derive_trezor_receive_address

        addresses = {
            _derive_trezor_receive_address(public_key, index): index
            for index in range(25)
        }

        def address_stats(address):
            index = addresses[address]
            return {
                "chain_stats": {
                    "funded_txo_sum": 1_000 if index == 1 else 0,
                    "spent_txo_sum": 0,
                    "tx_count": 1 if index == 1 else 0,
                },
                "mempool_stats": {
                    "funded_txo_sum": 0,
                    "spent_txo_sum": 0,
                    "tx_count": 0,
                },
            }

        clear_btc_cold_balance_cache()
        try:
            with (
                patch.dict(os.environ, {"TREZOR_BTC_PUB_KEY": xpub}),
                patch("sheets._fetch_mempool_address", side_effect=address_stats) as fetch,
                patch("sheets.time.monotonic", side_effect=[0.0, 0.0, 91.0, 91.0]),
            ):
                self.assertAlmostEqual(get_btc_cold_balance(), 0.00001)
                self.assertAlmostEqual(get_btc_cold_balance(), 0.00001)

            second_scan_indices = [
                addresses[call.args[0]] for call in fetch.call_args_list[25:]
            ]
            self.assertEqual(len(second_scan_indices), 21)
            self.assertEqual(set(second_scan_indices), set(range(1, 22)))
        finally:
            clear_btc_cold_balance_cache()

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
                    "change_pct": 0.1,
                    "kraken_btc": 1.0,
                    "cold_btc": 0.0,
                },
            ),
            patch("sheets.get_sdca_cash_on_kraken", return_value=0.0),
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
