"""Essential regression for indexing, compact retrieval and version history."""

import hashlib
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from PIL import Image, ImageDraw

from art_dna import ArtDNA, ArtDNAError, compact


class ArtDNATest(TestCase):
    def test_cached_references_modes_observations_and_originals(self):
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            reference = root / "ReferenceLibrary"
            reference.mkdir()
            files = [reference / name for name in ("draft.png", "copy.png", "quiet.png")]
            for path, color in ((files[0], "orange"), (files[2], "blue")):
                sample = Image.new("RGB", (96, 54), color)
                ImageDraw.Draw(sample).rectangle((10, 14, 45, 32), fill="black")
                sample.save(path)
            shutil.copy2(files[0], files[1])
            fingerprints = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in files}
            store = ArtDNA(root/"ArtDNA"/"art_dna.db", reference)
            self.assertEqual(store.index()["new_content"], 2)
            self.assertEqual(store.index()["unchanged_paths"], 3)
            self.assertEqual(store.install_topics({"draft.png": "Sample orange hero", "quiet.png":"Blue portrait"}), 2)
            identity = {"identity":"A focused and readable poster", "rules":["Use a quiet title bed"],
                        "variation":["Vary the crop"]}
            note = {
                "file": "draft.png", "topic": "Sample orange hero", "composition": "Tight framing",
                "typography": "Strong title", "color_lighting": "Orange accent",
                "editing": "Soft background", "attention": "Hero gaze",
                "takeaway": "Echo orange in the light", "tags": ["orange", "action"],
            }
            initial = store.install_profile({"analyst":"codex-reference-review", "profile":identity,"observations":[note]})
            revised = store.install_profile({
                "analyst":"codex-reference-review", "profile":{**identity, "identity":"Revised"}, "observations":[note],
            })
            store.activate(revised)
            self.assertEqual(store.brief("free_will")["style"]["identity"], "Revised")
            store.activate(initial)
            active = store.brief("reference", "orange action")
            hero = hashlib.sha256(files[0].read_bytes()).hexdigest()[:20]
            self.assertEqual(active["examples"][0]["id"], hero)
            self.assertLess(len(compact(active)), 2200)
            self.assertEqual(store.brief("free_will")["examples"], [])
            hybrid = store.brief("hybrid", "orange action", reference_percent=35)
            self.assertEqual(len(hybrid["examples"]), 1)
            self.assertNotEqual(active["apply"]["sourcing"], hybrid["apply"]["sourcing"])
            self.assertEqual(len(store.batch([hero])), 1)
            blue = hashlib.sha256(files[2].read_bytes()).hexdigest()[:20]
            provider_record = {key: value for key, value in note.items() if key != "file"}
            provider_record.update(id=blue, topic="Provider observed blue")
            self.assertEqual(store.record("fixture-vision", [provider_record])["saved"], 1)
            self.assertEqual(store.record("fixture-vision", [provider_record])["already_cached"], 1)
            with self.assertRaises(ArtDNAError):
                store.record("fixture-vision", [{**provider_record, "id":"0"*20}])
            store.record_feedback("a"*16, 1, "approve", "hybrid", "Good example",
                                  "Readable title", files[0])
            store.record_feedback("a"*16, 2, "reject", "free_will", "Weak example",
                                  "Subject too small", files[2])
            learned = store.brief("hybrid")["human_feedback"]
            self.assertEqual((learned["approved"], learned["rejected"]), (1, 1))
            self.assertEqual({cue["cue"] for cue in learned["recent_cues"]},
                             {"Readable title", "Subject too small"})
            self.assertEqual(store.status()["annotated_images"], 2)
            for path, (modified, data) in fingerprints.items():
                self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()), (modified, data))
