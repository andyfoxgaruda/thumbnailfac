"""Optional real-model regression using a known alpha mask and busy background."""

import io
import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch

from PIL import Image, ImageDraw
from starlette.testclient import TestClient

from segmentation import remove_background
from test_photocraft_gateway import isolated_server


ROOT = Path(__file__).resolve().parents[1]


@skipUnless(os.environ.get("PHOTOCRAFT_RUN_SEGMENTATION") == "1", "set PHOTOCRAFT_RUN_SEGMENTATION=1")
class LocalSegmentationQualityTest(TestCase):
    def test_both_models_remove_a_busy_scene_against_known_alpha(self):
        source = Image.open(ROOT / "Assets" / "Downloaded" / "wikipe-tan-cutout.png").convert("RGBA")
        size = (900, 1280)
        pixels = []
        colors = [(28, 41, 75), (100, 34, 81), (31, 97, 103),
                  (174, 88, 45), (70, 54, 105), (30, 103, 65)]
        for y in range(size[1]):
            for x in range(size[0]):
                color = colors[((x // 30) + (y // 30)) % len(colors)]
                shade = int(18 * math.sin(x / 37) + 14 * math.cos(y / 51))
                pixels.append(tuple(max(0, min(255, channel + shade)) for channel in color))
        background = Image.new("RGB", size)
        background.putdata(pixels)
        draw = ImageDraw.Draw(background)
        for index in range(42):
            x, y = (index * 173 + 35) % size[0], (index * 257 + 20) % size[1]
            draw.ellipse((x-36, y-24, x+36, y+24), outline=(230, 190, 100), width=3)
            draw.line((x-55, y+35, x+60, y-40), fill=(190, 220, 230), width=2)

        scale = min(700 / source.width, 1120 / source.height)
        character = source.resize((round(source.width * scale), round(source.height * scale)),
                                  Image.Resampling.LANCZOS)
        offset = ((size[0] - character.width) // 2, (size[1] - character.height) // 2)
        truth = Image.new("L", size, 0)
        truth.paste(character.getchannel("A"), offset)
        composite = background.convert("RGBA")
        composite.alpha_composite(character, offset)
        encoded = io.BytesIO()
        composite.save(encoded, format="PNG")

        expected = [alpha >= 128 for alpha in truth.get_flattened_data()]
        for model, minimum_iou in (("anime", 0.90), ("general", 0.88)):
            with self.subTest(model=model):
                output = Image.open(io.BytesIO(remove_background(encoded.getvalue(), model))).convert("RGBA")
                self.assertEqual(output.size, size)
                actual = [alpha >= 128 for alpha in output.getchannel("A").get_flattened_data()]
                intersection = sum(a and b for a, b in zip(expected, actual))
                union = sum(a or b for a, b in zip(expected, actual))
                iou = intersection / max(1, union)
                self.assertGreaterEqual(iou, minimum_iou, f"{model} mask IoU was {iou:.3f}")

        with TemporaryDirectory() as directory:
            studio, _ = isolated_server(Path(directory))
            downloads = Path(directory) / "Downloaded"
            downloads.mkdir()
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "download_image", "arguments": {
                    "download_url": "https://example.org/opaque.png", "filename": "segmented-test",
                    "cutout": True, "segmentation_model": "anime", "min_subject_edge": 300,
                },
            }}
            with patch.object(studio, "DOWNLOADED", downloads), \
                 patch.object(studio.image_mcp, "public_fetch",
                              return_value=(encoded.getvalue(), "https://example.org/opaque.png")), \
                 TestClient(studio.app, base_url=studio.PUBLIC_URL) as client:
                response = client.post("/mcp", headers={
                    "Authorization": "Bearer " + studio.TOKEN,
                    "Accept": "application/json, text/event-stream",
                }, json=request)
            self.assertEqual(response.status_code, 200)
            reply = response.json()["result"]
            self.assertFalse(reply.get("isError"), reply)
            metadata = json.loads(reply["content"][0]["text"])
            self.assertTrue(metadata["cutout"])
            self.assertTrue(metadata["transparent"])
            self.assertTrue((downloads / metadata["file"]).is_file())
