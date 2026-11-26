import unittest

from sheets import parse_sdca_signal_update, parse_signal_update


class ParseSignalUpdateTests(unittest.TestCase):
    def test_cash_only_signal_has_no_targets(self):
        text = """
📈 **RSPS Signal:** 📈
- **100.0% CASH** 💵

📉 **Short Module:** 📉
- **No short positions** (flat)
"""
        self.assertEqual(parse_signal_update(text), {})

    def test_spot_allocations_are_read_from_rsps_section(self):
        text = """
📈 **RSPS Signal:** 📈
- **38.6% BTC LONG** 🟠
- **18.2% SUI LONG** 💧
- **11.4% ZRO LONG** ⚪
- **31.8% CASH** 💵

📉 **Short Module:** 📉
- **No short positions** (flat)
"""
        self.assertEqual(
            parse_signal_update(text),
            {
                "BTC": (0.386, "Long"),
                "SUI": (0.182, "Long"),
                "ZRO": (0.114, "Long"),
            },
        )

    def test_short_allocations_ignore_net_positions_section(self):
        text = """
📈 **RSPS Signal:** 📈
- **100.0% CASH** 💵

📉 **Short Module:** 📉
- **0.1% PEPE SHORT** 🐸
- **0.1% ETH SHORT** 🔷
- **0.0% ENA SHORT** 💠
- Total short exposure: **0.2%**

⚖️ **Net Positions:** ⚖️
- **0.1% PEPE SHORT** 🐸 ⬇️
- **0.0% ENA FLAT** 💠 ➖
- **100.0% CASH** 💵
"""
        self.assertEqual(
            parse_signal_update(text),
            {
                "PEPE": (0.001, "Short"),
                "ETH": (0.001, "Short"),
                "ENA": (0.0, "Short"),
            },
        )


class ParseSDCASignalUpdateTests(unittest.TestCase):
    def test_explicit_buy_signal_returns_positive_fraction(self):
        text = """
⚡ **SDCA Signal Update** ⚡
📈 **Today's Accumulation/Distribution Rate:** 📈
- **BUY 8.05% :bitcoin: WITH CASH RESERVE** 💵
"""
        self.assertAlmostEqual(parse_sdca_signal_update(text), 0.0805)

    def test_default_threshold_signal_returns_positive_one_percent(self):
        text = """
⚡ **SDCA Signal Update** ⚡
📈 **Today's Accumulation/Distribution Rate:** 📈
- **SDCA Accelerated Accumulation Threshold not passed -
  Default to ~1% per-day perpetually** ♻️
"""
        self.assertAlmostEqual(parse_sdca_signal_update(text), 0.01)

    def test_sell_and_distribution_signal_variants_return_negative_fraction(self):
        self.assertAlmostEqual(
            parse_sdca_signal_update("⚡ SDCA Signal Update ⚡\n- SELL 25%"),
            -0.25,
        )
        self.assertAlmostEqual(
            parse_sdca_signal_update("⚡ SDCA Signal Update ⚡\n- DISTRIBUTE 10%"),
            -0.1,
        )

    def test_standalone_negative_percentage_is_a_sell(self):
        self.assertAlmostEqual(
            parse_sdca_signal_update("SDCA Signal Update\nChange: -12.5%"),
            -0.125,
        )

    def test_signal_without_extractable_percentage_raises(self):
        with self.assertRaisesRegex(ValueError, "No SDCA change percentage"):
            parse_sdca_signal_update("SDCA Signal Update\nNo allocation today")


if __name__ == "__main__":
    unittest.main()
