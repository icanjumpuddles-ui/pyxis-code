import unittest
from adsb_worker import icao_to_country

class TestAdsbWorker(unittest.TestCase):

    def test_icao_to_country_empty_or_none(self):
        self.assertEqual(icao_to_country(None), "Unknown")
        self.assertEqual(icao_to_country(""), "Unknown")

    def test_icao_to_country_short_string(self):
        self.assertEqual(icao_to_country("A"), "Unknown")
        self.assertEqual(icao_to_country("7"), "Unknown")

    def test_icao_to_country_known_prefixes(self):
        self.assertEqual(icao_to_country("7C"), "Australia")
        self.assertEqual(icao_to_country("7c"), "Australia")
        self.assertEqual(icao_to_country("7C1234"), "Australia")
        self.assertEqual(icao_to_country("AE"), "USA Military")
        self.assertEqual(icao_to_country("A0"), "USA")
        self.assertEqual(icao_to_country("40"), "United Kingdom")
        self.assertEqual(icao_to_country("3C"), "Germany")
        self.assertEqual(icao_to_country("F0"), "France")
        self.assertEqual(icao_to_country("C8"), "Canada")
        self.assertEqual(icao_to_country("86"), "China")
        self.assertEqual(icao_to_country("73"), "Russia")

    def test_icao_to_country_unknown_prefix(self):
        self.assertEqual(icao_to_country("ZZ"), "Unknown")
        self.assertEqual(icao_to_country("00"), "Unknown")
        self.assertEqual(icao_to_country("999999"), "Unknown")

if __name__ == '__main__':
    unittest.main()
