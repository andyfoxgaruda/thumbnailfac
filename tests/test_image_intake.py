"""Essential checks for direct image intake and local cutout preparation."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

import image_mcp


class ImageIntakeTests(unittest.TestCase):
    def test_gif_becomes_a_static_png(self):
        frames=[Image.new("RGBA",(96,64),color) for color in ("#D02030","#2040D0")]
        encoded=io.BytesIO()
        frames[0].save(encoded,"GIF",save_all=True,append_images=frames[1:],duration=100)
        converted,extension,details=image_mcp.validate_image(encoded.getvalue())
        self.assertEqual(extension,".png")
        self.assertEqual((details["width"],details["height"]),(96,64))
        with Image.open(io.BytesIO(converted)) as result:
            self.assertEqual(result.format,"PNG")

    def test_direct_download_keeps_source_metadata(self):
        raw=io.BytesIO()
        Image.new("RGB",(64,48),"#304050").save(raw,"PNG")
        url="https://example.org/art.png"
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(image_mcp,"public_fetch",return_value=(raw.getvalue(),url)):
            folder=Path(directory)
            record=image_mcp.download(url,folder,"art","https://example.org/post","Fixture")
            self.assertTrue((folder/record["file"]).is_file())
            sidecar=folder/(record["file"]+".source.json")
            self.assertEqual(json.loads(sidecar.read_text(encoding="utf-8"))["source_page"],
                             "https://example.org/post")

    def test_opaque_cutout_uses_selected_local_model(self):
        background=io.BytesIO()
        Image.new("RGB",(160,180),"#596070").save(background,"PNG")
        subject=Image.new("RGBA",(140,160),(0,0,0,0))
        ImageDraw.Draw(subject).ellipse((15,10,125,150),fill="#e05040")
        segmented=io.BytesIO()
        subject.save(segmented,"PNG")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(image_mcp,"public_fetch",return_value=(background.getvalue(),
                          "https://example.org/a.png")), \
             patch.object(image_mcp,"segment_background",return_value=segmented.getvalue()) as model:
            result=image_mcp.download("https://example.org/a.png",Path(directory),"cutout",
                                      "https://example.org/page","Fixture",cutout=True,
                                      min_subject_edge=50,segmentation_model="anime")
        self.assertTrue(result["cutout"])
        self.assertTrue(result["transparent"])
        model.assert_called_once_with(background.getvalue(),"anime")

    def test_flat_backdrop_is_removed_but_busy_edges_are_rejected(self):
        plain=Image.new("RGB",(600,600),"white")
        ImageDraw.Draw(plain).ellipse((100,70,500,530),fill="#c72535")
        encoded=io.BytesIO()
        plain.save(encoded,"PNG")
        cutout,_,details=image_mcp.validate_image(encoded.getvalue(),cutout=True,
            min_subject_edge=100,background_color="#FFFFFF")
        self.assertTrue(details["transparent"])
        with Image.open(io.BytesIO(cutout)) as isolated:
            self.assertEqual(isolated.getchannel("A").getpixel((0,0)),0)

        busy=Image.new("RGB",(600,600),"white")
        draw=ImageDraw.Draw(busy)
        for y in range(0,600,24):
            for x in range(0,600,24):
                if (x//24+y//24)%2:
                    draw.rectangle((x,y,x+23,y+23),fill="#294b72")
        draw.ellipse((150,90,450,520),fill="#c72535")
        encoded=io.BytesIO()
        busy.save(encoded,"PNG")
        with self.assertRaisesRegex(image_mcp.ImageSourceError,"background remains visible"):
            image_mcp.validate_image(encoded.getvalue(),cutout=True,min_subject_edge=100,
                                     background_color="#FFFFFF")


if __name__=="__main__":
    unittest.main()
