"""Local network identity for OAuth and MCP transport security."""

import json
from pathlib import Path
from urllib.parse import urlsplit


DEFAULTS = {
    "public_url": "http://127.0.0.1:8000",
    "allowed_hosts": ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"],
    "allowed_origins": ["http://127.0.0.1:8000", "http://localhost:8000"],
}


def load_network(root: Path) -> dict:
    path = Path(root) / "studio_network.json"
    values = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else DEFAULTS
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise ValueError("studio_network.json must define public_url, allowed_hosts, and allowed_origins.")
    public_url = values["public_url"]
    if (not isinstance(public_url, str) or not public_url or public_url.endswith("/")
            or urlsplit(public_url).scheme not in {"https", "http"}
            or not urlsplit(public_url).hostname):
        raise ValueError("studio_network.json has an invalid public_url.")
    for key in ("allowed_hosts", "allowed_origins"):
        if (not isinstance(values[key], list) or not 1 <= len(values[key]) <= 20
                or any(not isinstance(item, str) or not item or len(item) > 255 for item in values[key])):
            raise ValueError(f"studio_network.json has invalid {key}.")
    return values
