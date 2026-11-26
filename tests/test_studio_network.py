import json
import tempfile
import unittest
from pathlib import Path

from studio_network import DEFAULTS, load_network


class NetworkConfigTests(unittest.TestCase):
    def test_fresh_checkout_uses_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_network(Path(directory)), DEFAULTS)

    def test_local_config_is_used_and_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "studio_network.json"
            custom = {**DEFAULTS, "public_url": "https://studio.example.test"}
            path.write_text(json.dumps(custom), encoding="utf-8")
            self.assertEqual(load_network(Path(directory))["public_url"], custom["public_url"])
            path.write_text(json.dumps({"public_url": "https://studio.example.test"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_network(Path(directory))
