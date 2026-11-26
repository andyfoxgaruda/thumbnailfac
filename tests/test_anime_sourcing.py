import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from anime_sourcing import Candidate, Danbooru, Sourcing, SourceError, _near, queries


def picture(color):
    image = Image.new("RGB", (900, 700), color)
    draw = ImageDraw.Draw(image)
    for i in range(0, 800, 13):
        draw.line((i, 0, 899-i, 699), fill=(i%255, 100, 200), width=3)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class Provider:
    def __init__(self, name, urls=(), broken=False):
        self.name = name
        self.urls = urls
        self.broken = broken
        self.cooldown_until = 0
        self.last_error = ""
        self.calls = 0

    def search(self, tags, page, limit):
        self.calls += 1
        if self.broken:
            self.last_error = "rate_limited"
            self.cooldown_until = float("inf")
            raise SourceError("rate_limited")
        return [Candidate(self.name,str(i),url,"https://example.org/post/"+str(i),"","artist",
                          "uzumaki_naruto solo action_pose","g",900,700,"png",10,"")
                for i,url in enumerate(self.urls)]


class AnimeSourcingTests(unittest.TestCase):
    def test_planner_and_near_hash(self):
        self.assertEqual(queries("Naruto", "dramatic fighting", "danbooru", "foreground"),["uzumaki_naruto"])
        self.assertTrue(any("solo" in q for q in queries("Naruto", "fighting", "yandere", "foreground")))
        self.assertTrue(_near("0000000000000000","0000000000000001"))
        self.assertFalse(_near("0000000000000000","ffffffffffffffff"))

    def test_fallback_cache_and_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            a,b = "https://example.org/a.png","https://example.org/b.png"
            payload = picture("navy")
            fetches = []
            def fetch(url, **kwargs):
                fetches.append(url)
                return payload,url
            failed = Provider("blocked", broken=True)
            healthy = Provider("good",[a,b])
            source = Sourcing(Path(directory),[failed,healthy],fetch)
            first = source.source("Naruto action",["Naruto"],asset_roles=["foreground"],
                                  minimum_quality=25,max_candidates_per_role=2)
            self.assertEqual(first["status"],"READY")  # both URLs have identical bytes
            self.assertEqual(len(first["roles"]["foreground"]),1)
            self.assertEqual(first["diagnostics"]["provider_errors"]["blocked"],"rate_limited")
            self.assertEqual(len(list((Path(directory)/"Assets"/"Downloaded").glob("anime-*"))),1)
            second = source.source("Naruto action",["Naruto"],asset_roles=["foreground"],
                                   minimum_quality=25,max_candidates_per_role=2)
            self.assertTrue(second["diagnostics"]["cache_hit"])
            self.assertEqual(fetches.count(a),1)
            picked = source.pack_assets(first["pack_id"])
            self.assertEqual(picked[0]["role"],"foreground")
            self.assertLess(len(str(first)),1500)

    def test_corrupt_and_tiny_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            url = "https://example.org/bad.png"
            p = Provider("good",[url])
            source = Sourcing(Path(directory),[p],lambda u,**kw:(b"not-an-image",u))
            result = source.source("Naruto",["Naruto"],asset_roles=["foreground"],max_candidates_per_role=1)
            self.assertEqual(result["status"],"INSUFFICIENT_QUALITY")
            self.assertEqual(result["diagnostics"]["rejected"].get("invalid_image"),1)

    def test_rate_limit_cooldown_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            def limited(url, **kwargs):
                raise SourceError("The source returned HTTP 429.")
            source = Sourcing(Path(directory),[Danbooru(limited)],limited)
            with self.assertRaisesRegex(SourceError,"rate_limited"):
                source.providers[0].search("uzumaki_naruto")
            restarted = Sourcing(Path(directory),[Danbooru(limited)],limited)
            self.assertGreater(restarted.providers[0].health()["cooldown_s"],0)
            self.assertEqual(restarted.health()["providers"][0]["calls"],1)


if __name__ == "__main__":
    unittest.main()
