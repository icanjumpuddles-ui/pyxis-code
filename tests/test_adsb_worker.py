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

if __name__ == '__main__':
    unittest.main()
