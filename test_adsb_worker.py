import unittest
from adsb_worker import geo_range_bearing

class TestAdsbWorker(unittest.TestCase):
    def test_geo_range_bearing_identical(self):
        """Test identical coordinates return 0 distance and 0 bearing."""
        dist, brg = geo_range_bearing(0.0, 0.0, 0.0, 0.0)
        self.assertAlmostEqual(dist, 0.0, places=1)
        self.assertAlmostEqual(brg, 0.0, places=1)

        dist, brg = geo_range_bearing(45.0, -120.0, 45.0, -120.0)
        self.assertAlmostEqual(dist, 0.0, places=1)
        self.assertAlmostEqual(brg, 0.0, places=1)

    def test_geo_range_bearing_due_north(self):
        """Test moving due north."""
        # 1 degree of latitude is 60 nautical miles
        dist, brg = geo_range_bearing(0.0, 0.0, 1.0, 0.0)
        self.assertAlmostEqual(dist, 60.0, places=1)
        self.assertAlmostEqual(brg, 0.0, places=1)

    def test_geo_range_bearing_due_south(self):
        """Test moving due south."""
        dist, brg = geo_range_bearing(1.0, 0.0, 0.0, 0.0)
        self.assertAlmostEqual(dist, 60.0, places=1)
        self.assertAlmostEqual(brg, 180.0, places=1)

    def test_geo_range_bearing_due_east(self):
        """Test moving due east."""
        # At equator, cos(0) = 1, so 1 degree longitude = 60 nm
        dist, brg = geo_range_bearing(0.0, 0.0, 0.0, 1.0)
        self.assertAlmostEqual(dist, 60.0, places=1)
        self.assertAlmostEqual(brg, 90.0, places=1)

    def test_geo_range_bearing_due_west(self):
        """Test moving due west."""
        dist, brg = geo_range_bearing(0.0, 1.0, 0.0, 0.0)
        self.assertAlmostEqual(dist, 60.0, places=1)
        self.assertAlmostEqual(brg, 270.0, places=1)

    def test_geo_range_bearing_cross_hemisphere(self):
        """Test crossing the equator."""
        # From -1, 0 to 1, 0 is 2 degrees north -> 120 nm, 0 deg
        dist, brg = geo_range_bearing(-1.0, 0.0, 1.0, 0.0)
        self.assertAlmostEqual(dist, 120.0, places=1)
        self.assertAlmostEqual(brg, 0.0, places=1)

        # From 0, -1 to 0, 1 is 2 degrees east -> 120 nm, 90 deg
        dist, brg = geo_range_bearing(0.0, -1.0, 0.0, 1.0)
        self.assertAlmostEqual(dist, 120.0, places=1)
        self.assertAlmostEqual(brg, 90.0, places=1)

    def test_geo_range_bearing_arbitrary(self):
        """Test an arbitrary known coordinate pair."""
        # 1 degree north, 1 degree east from 0,0
        # dist = sqrt(60^2 + 60^2) = sqrt(7200) = 84.8528...
        # brg = 45 degrees
        dist, brg = geo_range_bearing(0.0, 0.0, 1.0, 1.0)
        self.assertAlmostEqual(dist, 84.9, places=1)
        self.assertAlmostEqual(brg, 45.0, places=1)

if __name__ == '__main__':
    unittest.main()
