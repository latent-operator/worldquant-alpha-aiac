import unittest
from unittest.mock import patch
from ops.launch import validate_release

class ReleaseTests(unittest.TestCase):
    def test_worker_cannot_be_enabled_by_pointer(self):
        with self.assertRaises(ValueError):
            validate_release({'worker_enabled':True})

    def test_release_requires_exact_clean_source(self):
        release={'source':'.','commit':'expected','worker_enabled':False}
        with patch('ops.launch.git',side_effect=['different','']):
            with self.assertRaises(ValueError): validate_release(release)
        with patch('ops.launch.git',side_effect=['expected',' M backend/main.py']):
            with self.assertRaises(ValueError): validate_release(release)
        with patch('ops.launch.git',side_effect=['expected','']):
            validate_release(release)

if __name__=='__main__': unittest.main()
