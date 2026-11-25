import os
import unittest
from unittest.mock import Mock, patch

from sheets import update_current_allocations_in_sheet


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
        ):
            update_current_allocations_in_sheet()

        worksheet.update.assert_any_call("E8:E8", [[36.0]], value_input_option="RAW")


if __name__ == "__main__":
    unittest.main()
