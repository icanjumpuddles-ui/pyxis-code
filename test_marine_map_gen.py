import unittest
from marine_map_gen import nearest_cmems_current

class TestMarineMapGen(unittest.TestCase):

    def test_nearest_cmems_current_empty(self):
        self.assertEqual(nearest_cmems_current([], 10.0, 20.0), (0.0, 0.0))
        self.assertEqual(nearest_cmems_current(None, 10.0, 20.0), (0.0, 0.0))

    def test_nearest_cmems_current_single_point(self):
        grid = [{'lat': 10.0, 'lon': 20.0, 'speed_kn': 1.5, 'dir_deg': 90.0}]
        self.assertEqual(nearest_cmems_current(grid, 10.5, 20.5), (1.5, 90.0))

    def test_nearest_cmems_current_multiple_points(self):
        grid = [
            {'lat': 10.0, 'lon': 20.0, 'speed_kn': 1.5, 'dir_deg': 90.0},
            {'lat': 11.0, 'lon': 21.0, 'speed_kn': 2.0, 'dir_deg': 180.0},
            {'lat': 12.0, 'lon': 22.0, 'speed_kn': 2.5, 'dir_deg': 270.0},
        ]

        # Closest to first point
        self.assertEqual(nearest_cmems_current(grid, 10.1, 20.1), (1.5, 90.0))
        # Closest to second point
        self.assertEqual(nearest_cmems_current(grid, 10.8, 20.8), (2.0, 180.0))
        # Closest to third point
        self.assertEqual(nearest_cmems_current(grid, 12.5, 22.5), (2.5, 270.0))

    def test_nearest_cmems_current_equidistant(self):
        # When equidistant, the first one encountered with the strict less-than minimal distance is chosen
        grid = [
            {'lat': 10.0, 'lon': 20.0, 'speed_kn': 1.5, 'dir_deg': 90.0},
            {'lat': 12.0, 'lon': 20.0, 'speed_kn': 2.0, 'dir_deg': 180.0},
        ]

        # Point exactly in the middle between lat 10.0 and 12.0 (11.0)
        self.assertEqual(nearest_cmems_current(grid, 11.0, 20.0), (1.5, 90.0))

if __name__ == '__main__':
    unittest.main()
