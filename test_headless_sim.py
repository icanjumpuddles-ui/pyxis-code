import unittest
from headless_sim import generate_sonar_grid

class TestHeadlessSim(unittest.TestCase):

    def test_generate_sonar_grid_shape(self):
        """Verify the output is a 10x10 matrix (nested list)."""
        grid = generate_sonar_grid(0)
        self.assertEqual(len(grid), 10)
        for row in grid:
            self.assertEqual(len(row), 10)

    def test_generate_sonar_grid_bounds(self):
        """Verify all generated depth values fall within the expected min/max boundaries (30.0 to 50.0)."""
        grid = generate_sonar_grid(100)
        for row in grid:
            for depth in row:
                self.assertGreaterEqual(depth, 30.0)
                self.assertLessEqual(depth, 50.0)

    def test_generate_sonar_grid_determinism(self):
        """Verify that consecutive calls with the same tick value generate the exact same grid."""
        grid1 = generate_sonar_grid(42)
        grid2 = generate_sonar_grid(42)
        self.assertEqual(grid1, grid2)

    def test_generate_sonar_grid_variance(self):
        """Verify that calling the function with different tick values generates different grids."""
        grid1 = generate_sonar_grid(0)
        grid2 = generate_sonar_grid(10)
        self.assertNotEqual(grid1, grid2)

if __name__ == '__main__':
    unittest.main()
