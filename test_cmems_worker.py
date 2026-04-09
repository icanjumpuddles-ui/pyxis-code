import unittest
import math
from cmems_worker import uv_to_speed_dir

class TestCMEMSWorker(unittest.TestCase):
    def test_uv_to_speed_dir_zero(self):
        speed, direction = uv_to_speed_dir(0, 0)
        self.assertEqual(speed, 0.0)
        self.assertEqual(direction, 0.0)

    def test_uv_to_speed_dir_positive_u(self):
        speed, direction = uv_to_speed_dir(1, 0)
        self.assertAlmostEqual(speed, 1.944, places=3)
        self.assertAlmostEqual(direction, 90.0, places=1)

    def test_uv_to_speed_dir_positive_v(self):
        speed, direction = uv_to_speed_dir(0, 1)
        self.assertAlmostEqual(speed, 1.944, places=3)
        self.assertAlmostEqual(direction, 0.0, places=1)

    def test_uv_to_speed_dir_negative_u(self):
        speed, direction = uv_to_speed_dir(-1, 0)
        self.assertAlmostEqual(speed, 1.944, places=3)
        self.assertAlmostEqual(direction, 270.0, places=1)

    def test_uv_to_speed_dir_negative_v(self):
        speed, direction = uv_to_speed_dir(0, -1)
        self.assertAlmostEqual(speed, 1.944, places=3)
        self.assertAlmostEqual(direction, 180.0, places=1)

    def test_uv_to_speed_dir_mixed(self):
        speed, direction = uv_to_speed_dir(1, 1)
        expected_speed = math.sqrt(2) * 1.944
        self.assertAlmostEqual(speed, expected_speed, places=3)
        self.assertAlmostEqual(direction, 45.0, places=1)

if __name__ == '__main__':
    unittest.main()
