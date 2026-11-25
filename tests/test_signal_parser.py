import unittest

from sheets import parse_signal_update


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


if __name__ == "__main__":
    unittest.main()
