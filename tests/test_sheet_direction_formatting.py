import os
import unittest
from unittest.mock import Mock, patch

from sheets import update_targets_from_signal


class UpdateTargetsDirectionFormattingTests(unittest.TestCase):
    def test_existing_and_new_directions_copy_their_matching_format(self):
        worksheet = Mock()
        worksheet.id = 123
        worksheet.get_values.return_value = [["BTC"]]

        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = worksheet

        with (
            patch.dict(os.environ, {"GOOGLE_DOCS_SHEET_ID": "test-sheet"}),
            patch("sheets.get_gspread_client", return_value=client),
        ):
            update_targets_from_signal("- 40% BTC SHORT\n- 60% ETH LONG")

        format_requests = worksheet.spreadsheet.batch_update.call_args_list[-1].args[
            0
        ]["requests"]
        requests_by_destination_row = {
            request["copyPaste"]["destination"]["startRowIndex"]: request[
                "copyPaste"
            ]["source"]["startRowIndex"]
            for request in format_requests
        }

        self.assertEqual(requests_by_destination_row, {7: 2, 8: 1})


if __name__ == "__main__":
    unittest.main()
