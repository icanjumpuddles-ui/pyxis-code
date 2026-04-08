import unittest
from unittest.mock import patch, mock_open
import json

import adsb_worker

class TestAdsbWorker(unittest.TestCase):
    @patch('adsb_worker.requests.get')
    @patch('adsb_worker.os.path.exists')
    def test_read_vessel_pos_local_file_read_error(self, mock_exists, mock_get):
        """
        Test that read_vessel_pos correctly falls back to DEFAULT_LAT and DEFAULT_LON
        when the local position file exists but reading it raises an exception
        (e.g., due to invalid JSON).
        """
        # 1. Skip the proxy fetch logic by raising an exception in requests.get
        mock_get.side_effect = Exception("Mocked API error")

        # 2. Force the local file fallback path
        mock_exists.return_value = True

        # 3. Mock open to raise an exception, simulating a bad file read/parse
        #    Alternatively, we can mock json.load, but mocking open to raise an error
        #    directly tests the `except Exception as e:` block cleanly.
        with patch('builtins.open', mock_open()) as mocked_file:
            mocked_file.side_effect = Exception("Mocked file read error")

            # Call the function
            lat, lon = adsb_worker.read_vessel_pos()

            # Assert that the function fell back to the defaults
            self.assertAlmostEqual(lat, adsb_worker.DEFAULT_LAT)
            self.assertAlmostEqual(lon, adsb_worker.DEFAULT_LON)

    @patch('adsb_worker.requests.get')
    @patch('adsb_worker.os.path.exists')
    def test_read_vessel_pos_local_file_invalid_json(self, mock_exists, mock_get):
        """
        Test that read_vessel_pos correctly falls back to DEFAULT_LAT and DEFAULT_LON
        when the local position file contains invalid JSON.
        """
        # Skip the proxy fetch logic by raising an exception
        mock_get.side_effect = Exception("Mocked API error")

        # Force the local file fallback path
        mock_exists.return_value = True

        # Mock open to return invalid JSON content
        with patch('builtins.open', mock_open(read_data="{invalid_json}")):
            lat, lon = adsb_worker.read_vessel_pos()

            # The json.load will fail, caught by `except Exception`, and fallback happens
            self.assertAlmostEqual(lat, adsb_worker.DEFAULT_LAT)
            self.assertAlmostEqual(lon, adsb_worker.DEFAULT_LON)

if __name__ == '__main__':
    unittest.main()
