import unittest
import math
import numpy as np

# Assuming cmems_worker is in the parent directory
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from cmems_worker import nearest_ocean_val

class TestNearestOceanVal(unittest.TestCase):
    def setUp(self):
        # Create a simple 5x5 grid
        # Lats: 10, 11, 12, 13, 14
        # Lons: 20, 21, 22, 23, 24
        self.lats = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
        self.lons = np.array([20.0, 21.0, 22.0, 23.0, 24.0])
        self.arr = np.zeros((5, 5))

    def test_exact_target_valid(self):
        """Test when the exact target coordinate has a non-NaN value."""
        self.arr[2, 2] = 42.5  # target at lat=12, lon=22
        val = nearest_ocean_val(self.arr, self.lats, self.lons, 12.0, 22.0, search=2)
        self.assertEqual(val, 42.5)

    def test_target_nan_nearby_valid(self):
        """Test when target is NaN, but a nearby cell is valid."""
        # Fill grid with NaNs
        self.arr[:] = np.nan
        # Valid cell at index [1, 1] (lat=11, lon=21), adjacent to target [2, 2]
        self.arr[1, 1] = 99.9

        # Target lat=12, lon=22 is mapped to [2, 2] which is NaN
        val = nearest_ocean_val(self.arr, self.lats, self.lons, 12.0, 22.0, search=2)
        self.assertEqual(val, 99.9)

    def test_all_nan(self):
        """Test when all cells in search radius are NaN."""
        self.arr[:] = np.nan
        val = nearest_ocean_val(self.arr, self.lats, self.lons, 12.0, 22.0, search=2)
        self.assertTrue(math.isnan(val))

    def test_out_of_bounds_protection(self):
        """Test out-of-bounds protection during search near edges."""
        # Grid with all NaNs except one out-of-bounds index relative to search logic,
        # but within array if bounds checks were missing.
        self.arr[:] = np.nan
        # Valid cell at edge [0, 0] (lat=10, lon=20)
        self.arr[0, 0] = 7.7

        # Target is edge [0, 0] (lat=10, lon=20), which is valid and returns immediately.
        # But if target is [0, 1] and [0,1] is NaN, search will look around.
        # Ensure search doesn't go below 0 index.
        val = nearest_ocean_val(self.arr, self.lats, self.lons, 10.0, 21.0, search=3)
        self.assertEqual(val, 7.7)

        # Target is at the other edge [4, 4]
        self.arr[:] = np.nan
        self.arr[4, 4] = 8.8
        val = nearest_ocean_val(self.arr, self.lats, self.lons, 14.0, 23.0, search=3)
        self.assertEqual(val, 8.8)

if __name__ == '__main__':
    unittest.main()
