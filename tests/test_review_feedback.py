"""Small end-to-end check for portal feedback and automatic analysis indexing."""

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

import portal
from art_dna import ArtDNA
from review_jobs import ReviewJobs


class ReviewFeedbackTest(TestCase):
    def test_human_decisions_feed_art_dna_and_analysis_uploads_index(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "ReferenceLibrary"
            reference.mkdir()
            exports = root / "Exports"
            exports.mkdir()
            assets = root / "Assets"
            assets.mkdir()
            incoming = root / "Incoming"
            incoming.mkdir()
            store = ArtDNA(root / "ArtDNA" / "art_dna.db", reference)
            store.db_path.parent.mkdir()
            store.db_path.touch()
            review = ReviewJobs(root / "Jobs")
            job = review.create("Signal test", ["hybrid", "free_will"])["job_id"]
            for number, color in ((1, "blue"), (2, "red")):
                Image.new("RGB", (96, 54), color).save(review.pending(job, number))

            password = "test-portal-password"
            with patch.multiple(
                portal,
                PASSWORD=password,
                REVIEW_JOBS=review,
                EXPORTS=exports,
                REFERENCE=reference,
                ASSETS=assets,
                INCOMING=incoming,
                ART_DNA=store,
                ANALYSIS_INDEX=root / "ReferenceAnalysis" / "image_index.json",
            ), TestClient(portal.app, follow_redirects=False) as client:
                client.cookies.set("thumb_auth", password)
                csrf = portal.approval_token(job)
                approved = client.post(f"/review/{job}/approve", data={
                    "csrf": csrf, "numbers": "1", "feedback_note": "Readable title and clear subject",
                })
                self.assertEqual(approved.status_code, 303)
                self.assertEqual(store.feedback_for(job, 1)["decision"], "approve")
                self.assertEqual(store.feedback_for(job, 1)["note"], "Readable title and clear subject")
                self.assertTrue(review.approved(exports, job, 1).is_file())

                rejected = client.post(f"/review/{job}/reject", data={
                    "csrf": csrf, "reject_number": "2", "feedback_note": "Too much visual clutter",
                })
                self.assertEqual(rejected.status_code, 303)
                self.assertEqual(store.feedback_for(job, 2)["decision"], "reject")
                self.assertFalse(review.approved(exports, job, 2).exists())
                self.assertEqual(store.status()["human_approved"], 1)
                self.assertEqual(store.status()["human_rejected"], 1)

                upload = io.BytesIO()
                Image.new("RGB", (80, 45), "green").save(upload, format="PNG")
                response = client.post("/upload", data={"destination": "ReferenceLibrary"}, files={
                    "files": ("new-reference.png", upload.getvalue(), "image/png"),
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(store.status()["files"], 1)
                self.assertTrue((reference / "new-reference.png").is_file())
                index = json.loads((root / "ReferenceAnalysis" / "image_index.json").read_text())
                self.assertEqual(index["count"], 1)
                self.assertEqual(index["images"][0]["file"], "new-reference.png")
