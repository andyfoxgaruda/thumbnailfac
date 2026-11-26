"""Shared, atomic image index used by the portal and MCP analysis tools."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def build_reference_index(reference, index_file):
    root = Path(reference).resolve()
    destination = Path(index_file)
    records = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            continue
        stat = resolved.stat()
        records.append({
            "id": len(records),
            "file": resolved.relative_to(root).as_posix(),
            "bytes": stat.st_size,
            "modified_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        })
    payload = {"created_utc": datetime.now(timezone.utc).isoformat(),
               "count": len(records), "images": records}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=destination.parent,
                               prefix=".image-index-", suffix=".tmp", delete=False) as temp:
            temp_name = temp.name
            json.dump(payload, temp, indent=2, ensure_ascii=False)
            temp.write("\n")
        os.replace(temp_name, destination)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return payload


def load_reference_index(index_file):
    path = Path(index_file)
    if not path.exists():
        raise RuntimeError("No reference index yet. Run scan_reference_library first.")
    return json.loads(path.read_text(encoding="utf-8"))
