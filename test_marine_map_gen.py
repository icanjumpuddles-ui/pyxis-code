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
import math
from marine_map_gen import _nm_distance

class TestMarineMapGen(unittest.TestCase):

    def test_nm_distance_same_point(self):
        """Distance between the same point should be 0."""
        self.assertAlmostEqual(_nm_distance(0, 0, 0, 0), 0.0)
        self.assertAlmostEqual(_nm_distance(45, -120, 45, -120), 0.0)
        self.assertAlmostEqual(_nm_distance(-30, 150, -30, 150), 0.0)

    def test_nm_distance_latitude_only(self):
        """Distance of 1 degree latitude should be approximately 60 nautical miles."""
        # Note: 1 degree latitude is exactly 60 nautical miles based on the historic definition,
        # but depending on the Earth radius assumed in the function, it might vary slightly.
        # The function uses R = 3440.065, so 1 degree = (math.pi/180) * 3440.065 * 2??
        # Let's check: dlat = math.radians(1), a = math.sin(dlat/2)**2
        # return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a)) -> essentially arc length = R * theta
        # 3440.065 * math.pi / 180 = 60.0405

        expected_1_deg = 3440.065 * math.pi / 180
        self.assertAlmostEqual(_nm_distance(0, 0, 1, 0), expected_1_deg, places=3)
        self.assertAlmostEqual(_nm_distance(10, 20, 11, 20), expected_1_deg, places=3)

    def test_nm_distance_equator_longitude(self):
        """Distance of 1 degree longitude at the equator should be approximately 60 nm."""
        expected_1_deg = 3440.065 * math.pi / 180
        self.assertAlmostEqual(_nm_distance(0, 0, 0, 1), expected_1_deg, places=3)
        self.assertAlmostEqual(_nm_distance(0, 179, 0, -180), expected_1_deg, places=3)

    def test_nm_distance_symmetry(self):
        """Distance from A to B should equal B to A."""
        p1 = (37.7749, -122.4194) # SF
        p2 = (34.0522, -118.2437) # LA
        self.assertAlmostEqual(
            _nm_distance(p1[0], p1[1], p2[0], p2[1]),
            _nm_distance(p2[0], p2[1], p1[0], p1[1])
        )

    def test_nm_distance_real_world(self):
        """Check distance against known real-world value."""
        # San Francisco to Los Angeles is approx 300-350 nm (straight line)
        # Using a reliable calculator: ~302 nm
        p1 = (37.7749, -122.4194) # SF
        p2 = (34.0522, -118.2437) # LA
        dist = _nm_distance(p1[0], p1[1], p2[0], p2[1])
        self.assertTrue(290 < dist < 315, f"Distance {dist} is out of expected range")

if __name__ == '__main__':
    unittest.main()
