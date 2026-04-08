import unittest
import math
import sys
import os

# Adjust path to import adsb_worker from the parent directory
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from adsb_worker import geo_range_bearing

class TestGeoRangeBearing(unittest.TestCase):

    def test_same_point(self):
        dist, brg = geo_range_bearing(0, 0, 0, 0)
        self.assertEqual(dist, 0.0)
        self.assertEqual(brg, 0.0)

    def test_due_north(self):
        dist, brg = geo_range_bearing(0, 0, 1, 0)
        self.assertEqual(dist, 60.0)
        self.assertEqual(brg, 0.0)

    def test_due_south(self):
        dist, brg = geo_range_bearing(0, 0, -1, 0)
        self.assertEqual(dist, 60.0)
        self.assertEqual(brg, 180.0)

    def test_due_east(self):
        dist, brg = geo_range_bearing(0, 0, 0, 1)
        self.assertEqual(dist, 60.0)
        self.assertEqual(brg, 90.0)

    def test_due_west(self):
        dist, brg = geo_range_bearing(0, 0, 0, -1)
        self.assertEqual(dist, 60.0)
        self.assertEqual(brg, 270.0)

    def test_non_zero_latitude_scaling(self):
        # At 60 degrees latitude, cos(60) = 0.5
        # 1 degree of longitude should be ~30 nautical miles
        dist, brg = geo_range_bearing(60, 0, 60, 1)
        self.assertEqual(dist, 30.0)
        self.assertEqual(brg, 90.0)

    def test_diagonal_movement(self):
        # South-East at the equator
        # dLat = -60, dLon = 60 -> dist = sqrt(60^2 + 60^2) = 84.8528... -> 84.9
        # brg = atan2(60, -60) = 135 deg
        dist, brg = geo_range_bearing(0, 0, -1, 1)
        self.assertEqual(dist, 84.9)
        self.assertEqual(brg, 135.0)
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
