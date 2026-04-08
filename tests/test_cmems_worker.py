import unittest
from unittest.mock import patch, mock_open
import cmems_worker

class TestCmemsWorkerLoadEnv(unittest.TestCase):

    def test_load_env_success(self):
        # Test normal parsing
        mock_env_content = "CMEMS_USER=testuser\nCMEMS_PASS=\"testpass\"\n"
        with patch('builtins.open', mock_open(read_data=mock_env_content)):
            env = cmems_worker.load_env()
            self.assertEqual(env, {"CMEMS_USER": "testuser", "CMEMS_PASS": "testpass"})

    def test_load_env_comments_and_empty_lines(self):
        # Test ignoring comments and empty lines
        mock_env_content = "# This is a comment\n\nCMEMS_USER=testuser\n# Another comment\n"
        with patch('builtins.open', mock_open(read_data=mock_env_content)):
            env = cmems_worker.load_env()
            self.assertEqual(env, {"CMEMS_USER": "testuser"})

    def test_load_env_strip_quotes_and_spaces(self):
        # Test stripping spaces and double quotes
        mock_env_content = "  CMEMS_USER  =  \" testuser \"  \n"
        with patch('builtins.open', mock_open(read_data=mock_env_content)):
            env = cmems_worker.load_env()
            self.assertEqual(env, {"CMEMS_USER": " testuser "})

    def test_load_env_exception(self):
        # Test exception handling
        with patch('builtins.open', side_effect=Exception("Test Error")):
            env = cmems_worker.load_env()
            self.assertEqual(env, {})

if __name__ == '__main__':
    unittest.main()
