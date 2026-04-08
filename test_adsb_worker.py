import unittest
from adsb_worker import classify_type

class TestClassifyType(unittest.TestCase):

    def test_emergency_squawks(self):
        self.assertEqual(classify_type("123456", "CALL1", 0, "7700"), "MAYDAY")
        self.assertEqual(classify_type("123456", "CALL1", 0, "7500"), "HIJACK")
        self.assertEqual(classify_type("123456", "CALL1", 0, "7600"), "RADIO FAIL")
        self.assertEqual(classify_type("123456", "CALL1", 0, 7700), "MAYDAY")

    def test_categories(self):
        self.assertEqual(classify_type("123456", "CALL1", 1, "1234"), "Light GA")
        self.assertEqual(classify_type("123456", "CALL1", 2, "1234"), "GA Small")
        self.assertEqual(classify_type("123456", "CALL1", 3, "1234"), "GA Large")
        self.assertEqual(classify_type("123456", "CALL1", 4, "1234"), "VHVW")
        self.assertEqual(classify_type("123456", "CALL1", 5, "1234"), "Heavy")
        self.assertEqual(classify_type("123456", "CALL1", 6, "1234"), "High Perf")
        self.assertEqual(classify_type("123456", "CALL1", 7, "1234"), "Rotorcraft")
        self.assertEqual(classify_type("123456", "CALL1", 10, "1234"), "Glider")
        self.assertEqual(classify_type("123456", "CALL1", 11, "1234"), "Airship")
        self.assertEqual(classify_type("123456", "CALL1", 12, "1234"), "UAV")
        self.assertEqual(classify_type("123456", "CALL1", 13, "1234"), "Space")

    def test_military_callsigns(self):
        self.assertEqual(classify_type("123456", "RCH123", 0, "1234"), "Military")
        self.assertEqual(classify_type("123456", "RHC456", 0, "1234"), "Military")
        self.assertEqual(classify_type("123456", "EAGLE1", 0, "1234"), "Military")
        self.assertEqual(classify_type("123456", " VENOM2 ", 0, "1234"), "Military") # Test stripping
        self.assertEqual(classify_type("123456", "ghost3", 0, "1234"), "Military") # Test uppercase

    def test_icao_hex_ranges(self):
        # US Military: 0xAE0000 <= h <= 0xAFFFFF
        self.assertEqual(classify_type("AE0000", "TEST", 0, "1234"), "US Military")
        self.assertEqual(classify_type("AFFFFF", "TEST", 0, "1234"), "US Military")
        self.assertEqual(classify_type("AE1234", "TEST", 0, "1234"), "US Military")

        # RAF: 0x438000 <= h <= 0x43FFFF
        self.assertEqual(classify_type("438000", "TEST", 0, "1234"), "RAF")
        self.assertEqual(classify_type("43FFFF", "TEST", 0, "1234"), "RAF")
        self.assertEqual(classify_type("438ABC", "TEST", 0, "1234"), "RAF")

        # Luftwaffe: 0x3C4000 <= h <= 0x3CFFFF
        self.assertEqual(classify_type("3C4000", "TEST", 0, "1234"), "Luftwaffe")
        self.assertEqual(classify_type("3CFFFF", "TEST", 0, "1234"), "Luftwaffe")
        self.assertEqual(classify_type("3C4DEF", "TEST", 0, "1234"), "Luftwaffe")

    def test_commercial_callsigns(self):
        self.assertEqual(classify_type("123456", "QFA123", 0, "1234"), "Commercial")
        self.assertEqual(classify_type("123456", "UAE456", 0, "1234"), "Commercial")
        self.assertEqual(classify_type("123456", "JST", 0, "1234"), "Commercial")

    def test_unknown_and_edge_cases(self):
        self.assertEqual(classify_type("123456", "", 0, "1234"), "Unknown")
        self.assertEqual(classify_type("123456", None, 0, "1234"), "Unknown")
        self.assertEqual(classify_type("INVALID_HEX", "A1", 0, "1234"), "Unknown")
        self.assertEqual(classify_type("", "12", 0, "1234"), "Unknown")
        self.assertEqual(classify_type(None, "12", 0, "1234"), "Unknown")

if __name__ == '__main__':
    unittest.main()
