import unittest
from watch_simulator import WindRoseCanvas

class TestWindRoseCanvas(unittest.TestCase):
    def setUp(self):
        self.canvas = WindRoseCanvas(None)

    def test_parse_brg(self):
        test_cases = [
            ("204", 204.0),
            ("SSW (204°)", 204.0),
            (204.0, 204.0),
            (None, -1.0),
            ("n/a", -1.0),
            ("None", -1.0),
            ("", -1.0),
            ("  180  ", 180.0),
            ("N (0°)", 0.0),
            ("NW (315°)", 315.0),
            ("INVALID", -1.0),
            ("-10", -1.0),
            (" (abc°) ", -1.0)
        ]

        for s, expected in test_cases:
            with self.subTest(s=s):
                self.assertEqual(self.canvas._parse_brg(s), expected)

if __name__ == "__main__":
    unittest.main()
