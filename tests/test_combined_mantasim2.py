import unittest
import numpy as np
import math
from combined_mantasim2 import euler_matrix

class TestEulerMatrix(unittest.TestCase):
    def test_identity(self):
        # 0 yaw, 0 pitch, 0 roll should be identity matrix
        mat = euler_matrix(0, 0, 0)
        expected = np.eye(4, dtype=np.float32)
        np.testing.assert_array_almost_equal(mat, expected)

    def test_yaw_90(self):
        # 90 degrees yaw (rotation around Y axis)
        # Ry = [[0, 0, 1], [0, 1, 0], [-1, 0, 0]]
        mat = euler_matrix(math.pi/2, 0, 0)
        expected = np.array([
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [-1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]
        ], dtype=np.float32)
        np.testing.assert_array_almost_equal(mat, expected)

    def test_pitch_90(self):
        # 90 degrees pitch (rotation around X axis)
        # Rx = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]
        mat = euler_matrix(0, math.pi/2, 0)
        expected = np.array([
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, -1.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]
        ], dtype=np.float32)
        np.testing.assert_array_almost_equal(mat, expected)

    def test_roll_90(self):
        # 90 degrees roll (rotation around Z axis)
        # Rz = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
        mat = euler_matrix(0, 0, math.pi/2)
        expected = np.array([
            [0.0, -1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]
        ], dtype=np.float32)
        np.testing.assert_array_almost_equal(mat, expected)

    def test_combination(self):
        # Test a combination of yaw, pitch, roll
        # E.g., yaw=pi/2, pitch=pi/2, roll=0
        # Ry * Rx = [[0, 0, 1], [0, 1, 0], [-1, 0, 0]] * [[1, 0, 0], [0, 0, -1], [0, 1, 0]]
        # = [[0, 1, 0], [0, 0, -1], [-1, 0, 0]]
        mat = euler_matrix(math.pi/2, math.pi/2, 0)
        expected = np.array([
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, -1.0, 0.0],
            [-1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]
        ], dtype=np.float32)
        np.testing.assert_array_almost_equal(mat, expected)

if __name__ == '__main__':
    unittest.main()
