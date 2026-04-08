import unittest
from adsb_worker import icao_to_country

class TestICAOToCountry(unittest.TestCase):
    def test_empty_and_none(self):
        self.assertEqual(icao_to_country(""), "Unknown")
        self.assertEqual(icao_to_country(None), "Unknown")

    def test_short_inputs(self):
        self.assertEqual(icao_to_country("A"), "Unknown")
        self.assertEqual(icao_to_country("7"), "Unknown")
        self.assertEqual(icao_to_country("A0"), "USA")

    def test_valid_mappings(self):
        self.assertEqual(icao_to_country("7C1234"), "Australia")
        self.assertEqual(icao_to_country("AE5678"), "USA Military")
        self.assertEqual(icao_to_country("40ABCD"), "United Kingdom")
        self.assertEqual(icao_to_country("8A9999"), "Australia (RAAF)")
        self.assertEqual(icao_to_country("A0FFFF"), "USA")

    def test_case_insensitivity(self):
        self.assertEqual(icao_to_country("7c1234"), "Australia")
        self.assertEqual(icao_to_country("ae5678"), "USA Military")

    def test_unmapped_prefixes(self):
        self.assertEqual(icao_to_country("99ABCD"), "Unknown")
        self.assertEqual(icao_to_country("ZZ1234"), "Unknown")

if __name__ == '__main__':
    unittest.main()
