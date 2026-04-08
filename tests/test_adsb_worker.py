import unittest
from unittest.mock import patch, mock_open
import os
import sys

# Add the parent directory to sys.path to import adsb_worker
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import adsb_worker

class TestAdsbWorker(unittest.TestCase):
    @patch('adsb_worker.requests.get')
    @patch('adsb_worker.os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_read_vessel_pos_file_error(self, mock_open_file, mock_exists, mock_get):
        # Setup mock_get to raise an exception so it falls back to the file
        mock_get.side_effect = Exception("Network error")

        # Setup mock_exists to return True so it tries to read the file
        mock_exists.return_value = True

        # Setup open to raise an exception when reading the file
        mock_open_file.side_effect = Exception("File read error")

        # Call the function
        lat, lon = adsb_worker.read_vessel_pos()

        # Assert that it returns the default fallback position when file read fails
        self.assertEqual(lat, adsb_worker.DEFAULT_LAT)
        self.assertEqual(lon, adsb_worker.DEFAULT_LON)

if __name__ == '__main__':
    unittest.main()
