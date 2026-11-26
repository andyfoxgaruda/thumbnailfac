"""Local, model-independent style memory for the thumbnail studio."""

import argparse
import hashlib
import io
import json
import math
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps
from pydantic import BaseModel, ConfigDict, Field, ValidationError


EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
QUERY_STOP_WORDS = set("a an the for of in with and or make create design poster thumbnail anime".split())
SCHEMA = """
CREATE TABLE IF NOT EXISTS images(id TEXT PRIMARY KEY, features TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files(
    path TEXT PRIMARY KEY, image_id TEXT NOT NULL REFERENCES images(id),
    size INTEGER NOT NULL, modified INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS files_image ON files(image_id);
CREATE TABLE IF NOT EXISTS topics(image_id TEXT PRIMARY KEY REFERENCES images(id), title TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations(
    image_id TEXT NOT NULL REFERENCES images(id), analyst TEXT NOT NULL,
    observed TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY (image_id, analyst)
);
CREATE TABLE IF NOT EXISTS profiles(
    id TEXT PRIMARY KEY, created TEXT NOT NULL, snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS human_feedback(
    job_id TEXT NOT NULL, result_number INTEGER NOT NULL, decision TEXT NOT NULL,
    mode TEXT NOT NULL, title TEXT NOT NULL, note TEXT NOT NULL,
    image_hash TEXT NOT NULL, palette TEXT NOT NULL, observed TEXT NOT NULL,
    PRIMARY KEY(job_id, result_number)
);
"""


class ArtDNAError(ValueError):
    """An input or data availability problem safe to report to an MCP client."""


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^[0-9a-f]{20}$")
    topic: str = Field(min_length=1, max_length=75)
    composition: str = Field(min_length=1, max_length=200)
    typography: str = Field(min_length=1, max_length=200)
    color_lighting: str = Field(min_length=1, max_length=180)
    editing: str = Field(min_length=1, max_length=180)
    attention: str = Field(min_length=1, max_length=160)
    takeaway: str = Field(min_length=1, max_length=180)
    tags: list[str] = Field(min_length=1, max_length=10)


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def words(value):
    return set(re.findall(r"[^\W_]+", value.casefold()))


def image_features(path):
    with Image.open(path) as source:
        if source.width * source.height > 67_108_864:
            raise ArtDNAError("Reference exceeds the image pixel limit.")
        orientation = source.getexif().get(274)
        width, height = source.size
        if orientation in {5, 6, 7, 8}:
            width, height = height, width
        source.thumbnail((144, 144), Image.Resampling.LANCZOS)
        rgba = ImageOps.exif_transpose(source).convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, "#171717")
        canvas.alpha_composite(rgba)
        thumb = canvas.convert("RGB")
        palette = thumb.quantize(colors=5)
        colors = palette.getpalette()
        swatches = ["#" + bytes(colors[i * 3:i * 3 + 3]).hex()
                    for count, i in sorted(palette.getcolors(), reverse=True)]

        hsv = thumb.convert("HSV")
        hues = [0] * 13
        for hue, sat, val in hsv.get_flattened_data():
            hues[12 if val < 46 or sat < 48 else hue * 12 // 256] += 1
        light = thumb.convert("L")
        histogram = light.histogram()
        pixels = thumb.width * thumb.height
        bands = [sum(histogram[i:i+64]) / pixels for i in range(0, 256, 64)]
        edges = light.filter(ImageFilter.FIND_EDGES)
        edge_grid = [v / 255 for v in edges.crop((1, 1, edges.width-1, edges.height-1))
                     .resize((3, 3), Image.Resampling.BOX).get_flattened_data()] if min(edges.size) > 2 else [0]*9
        return {
            "size": [width, height],
            "palette": swatches,
            "signature": [round(v / pixels, 4) for v in hues] + [round(v, 4) for v in bands + edge_grid],
        }


def similarity(first, second):
    return sum(math.sqrt(a*b) for a, b in zip(first[:17], second[:17])) / 2 - (
        sum(abs(a-b) for a, b in zip(first[17:], second[17:])) / 9
    )


class ArtDNA:
    def __init__(self, db_path, library):
        self.db_path = Path(db_path)
        self.library = Path(library).resolve()

    def _connect(self):
        if not self.db_path.is_file():
            raise ArtDNAError("Art DNA has not been indexed on this PC.")
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SCHEMA)
        return connection

    def index(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.touch(exist_ok=True)
        paths = [p for p in self.library.rglob("*") if p.suffix.lower() in EXTENSIONS
                 and p.is_file() and p.resolve().is_relative_to(self.library)]
        cached = processed = reused = 0
        errors = []
        with closing(self._connect()) as con, con:
            con.executescript(SCHEMA)
            known = {row["path"]: row for row in con.execute("SELECT * FROM files")}
            present = {p.relative_to(self.library).as_posix() for p in paths}
            for name in known.keys() - present:
                con.execute("DELETE FROM files WHERE path=?", (name,))
            for path in paths:
                name = path.relative_to(self.library).as_posix()
                old = known.get(name)
                try:
                    before = path.stat()
                    if old and (before.st_size, before.st_mtime_ns) == (old["size"], old["modified"]):
                        cached += 1
                        continue
                    with path.open("rb") as stream:
                        image_id = hashlib.file_digest(stream, "sha256").hexdigest()[:20]
                    exists = con.execute("SELECT 1 FROM images WHERE id=?", (image_id,)).fetchone()
                    features = None if exists else image_features(path)
                    after = path.stat()
                    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                        raise ArtDNAError("Reference changed during indexing.")
                    if features:
                        con.execute("INSERT INTO images VALUES (?,?)", (image_id, compact(features)))
                        processed += 1
                    else:
                        reused += 1
                    con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
                                (name, image_id, before.st_size, before.st_mtime_ns))
                except (OSError, ArtDNAError, Image.DecompressionBombError):
                    errors.append(name)
                    con.execute("DELETE FROM files WHERE path=?", (name,))
        return {"new_content": processed, "unchanged_paths": cached, "existing_content": reused, "errors": errors}

    def status(self):
        with closing(self._connect()) as con:
            return dict(con.execute("""
                SELECT (SELECT count(*) FROM files) AS files,
                       (SELECT count(DISTINCT image_id) FROM files) AS unique_images,
                       (SELECT count(DISTINCT o.image_id) FROM observations o
                         JOIN files f ON f.image_id=o.image_id) AS annotated_images,
                       (SELECT count(DISTINCT t.image_id) FROM topics t
                         JOIN files f ON f.image_id=t.image_id) AS labeled_images,
                       (SELECT count(*) FROM human_feedback WHERE decision='approve') AS human_approved,
                       (SELECT count(*) FROM human_feedback WHERE decision='reject') AS human_rejected,
                       (SELECT value FROM settings WHERE key='profile') AS version
            """).fetchone())

    def record_feedback(self, job_id, result_number, decision, mode, title, note, image_path):
        if not re.fullmatch(r"[a-f0-9]{16}", job_id or "") or not 1 <= result_number <= 8:
            raise ArtDNAError("The review result ID is invalid.")
        if decision not in {"approve", "reject"} or mode not in {"reference", "free_will", "hybrid"}:
            raise ArtDNAError("Choose an explicit approve or reject decision and a valid mode.")
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise ArtDNAError("The review title is invalid.")
        if not isinstance(note, str) or len(note.strip()) > 240:
            raise ArtDNAError("Keep feedback notes under 240 characters.")
        path = Path(image_path)
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            raise ArtDNAError("The reviewed image is missing or unsupported.")
        try:
            features = image_features(path)
            with path.open("rb") as stream:
                image_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        except (OSError, Image.DecompressionBombError, ArtDNAError):
            raise ArtDNAError("Could not read the reviewed image safely.") from None
        with closing(self._connect()) as con, con:
            con.execute("""
                INSERT INTO human_feedback VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,result_number) DO UPDATE SET
                    decision=excluded.decision, mode=excluded.mode, title=excluded.title,
                    note=excluded.note, image_hash=excluded.image_hash,
                    palette=excluded.palette, observed=excluded.observed
            """, (job_id, result_number, decision, mode, title.strip(), note.strip(), image_hash,
                  compact(features["palette"][:3]), datetime.now(timezone.utc).isoformat()))
        return {"saved": True, "decision": decision}

    def feedback_for(self, job_id, result_number):
        with closing(self._connect()) as con:
            row = con.execute("""SELECT decision,note FROM human_feedback
                WHERE job_id=? AND result_number=?""", (job_id, result_number)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _feedback_brief(con):
        counts = {row["decision"]: row["n"] for row in con.execute(
            "SELECT decision,count(*) AS n FROM human_feedback GROUP BY decision")}
        rows = con.execute("""SELECT decision,mode,title,note,palette FROM human_feedback
            ORDER BY observed DESC LIMIT 4""").fetchall()
        cues = []
        for row in rows:
            palette = json.loads(row["palette"])
            cue = row["note"] or ("palette " + ", ".join(palette) if palette else "no written note")
            cues.append({"decision": row["decision"], "mode": row["mode"],
                         "title": row["title"], "cue": cue})
        return {"approved": counts.get("approve", 0), "rejected": counts.get("reject", 0),
                "recent_cues": cues}

    def _images(self, con):
        rows = con.execute("""
            SELECT i.id, i.features, min(f.path) AS path, count(*) AS copies,
                (SELECT t.title FROM topics t WHERE t.image_id=i.id) AS title,
                (SELECT o.record FROM observations o WHERE o.image_id=i.id
                 ORDER BY (o.analyst='codex-reference-review') DESC, o.observed DESC, o.analyst LIMIT 1) AS observation
            FROM files f JOIN images i ON f.image_id=i.id GROUP BY i.id
        """)
        return {r["id"]: {"file": r["path"], "title": r["title"], "features": json.loads(r["features"]),
                          "note": json.loads(r["observation"]) if r["observation"] else None}
                for r in rows}

    def _save(self, con, analyst, records):
        if not re.fullmatch(r"[A-Za-z0-9 ._:/-]{1,60}", analyst):
            raise ArtDNAError("Use a short analyst/model label.")
        if not isinstance(records, list) or not 1 <= len(records) <= 64:
            raise ArtDNAError("Supply one to 64 structured observations.")
        inserted = 0
        for record in records:
            try:
                note = Observation.model_validate(record)
            except ValidationError:
                raise ArtDNAError("An observation does not match the Art DNA schema.") from None
            if any(not tag.strip() or len(tag) > 32 for tag in note.tags):
                raise ArtDNAError("Each observation tag must have 1 to 32 characters.")
            if not con.execute("SELECT 1 FROM files WHERE image_id=?", (note.id,)).fetchone():
                raise ArtDNAError("An observation ID is outside the indexed reference library.")
            payload = compact(note.model_dump())
            saved = con.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?)",
                                (note.id, analyst, datetime.now(timezone.utc).isoformat(), payload))
            if saved.rowcount:
                inserted += 1
            elif con.execute("SELECT record FROM observations WHERE image_id=? AND analyst=?",
                             (note.id, analyst)).fetchone()["record"] != payload:
                raise ArtDNAError("This analyst already recorded that image. Use a new versioned analyst label for a revision.")
        return {"saved": inserted, "already_cached": len(records)-inserted}

    def record(self, analyst, records):
        with closing(self._connect()) as con, con:
            return self._save(con, analyst, records)

    def install_profile(self, seed):
        with closing(self._connect()) as con, con:
            by_file = {row["path"]: row["image_id"] for row in con.execute("SELECT path,image_id FROM files")}
            records = [{**row, "id": by_file[row["file"]]} for row in seed["observations"]]
            for row in records:
                del row["file"]
            self._save(con, seed["analyst"], records)
            profile = {**seed["profile"], "evidence": [row["id"] for row in records]}
            snapshot = compact(profile)
            if len(snapshot) > 1900:
                raise ArtDNAError("Keep the shared style profile under 1,900 characters.")
            version = hashlib.sha256(snapshot.encode()).hexdigest()[:12]
            con.execute("INSERT OR IGNORE INTO profiles VALUES (?,?,?)",
                        (version, datetime.now(timezone.utc).isoformat(), snapshot))
            con.execute("INSERT OR IGNORE INTO settings VALUES ('profile',?)", (version,))
        return version

    def install_topics(self, topics):
        with closing(self._connect()) as con, con:
            by_file = {row["path"]: row["image_id"] for row in con.execute("SELECT path,image_id FROM files")}
            labels = {by_file[path]: title for path, title in topics.items() if path in by_file}
            con.executemany("INSERT OR REPLACE INTO topics VALUES (?,?)", labels.items())
        return len(labels)

    def activate(self, version):
        with closing(self._connect()) as con, con:
            if not con.execute("SELECT 1 FROM profiles WHERE id=?", (version,)).fetchone():
                raise ArtDNAError("Unknown Art DNA version.")
            con.execute("INSERT OR REPLACE INTO settings VALUES ('profile',?)", (version,))

    def brief(self, mode, query="", reference_ids=(), reference_percent=None, similar_to=None,
              core_only=False):
        if mode not in {"reference", "free_will", "hybrid"}:
            raise ArtDNAError("Choose reference, free_will, or hybrid.")
        if len(query) > 400 or len(reference_ids) > 3:
            raise ArtDNAError("Use a short brief and up to three reference IDs.")
        if reference_percent is not None and (mode != "hybrid" or not 0 <= reference_percent <= 100):
            raise ArtDNAError("Reference percentage applies to hybrid mode and runs from 0 to 100.")
        weight = {"reference": 100, "free_will": 0, "hybrid": 50}[mode]
        if reference_percent is not None:
            weight = reference_percent
        if not weight and (reference_ids or similar_to):
            raise ArtDNAError("Choose a reference-led mode to anchor the brief to specific images.")
        if core_only and (reference_ids or similar_to):
            raise ArtDNAError("Core-only mode uses the shared style profile without individual references.")
        with closing(self._connect()) as con:
            feedback = self._feedback_brief(con)
            selected = con.execute("""
                SELECT p.id,p.snapshot FROM profiles p JOIN settings s ON s.value=p.id WHERE s.key='profile'
            """).fetchone()
            if not selected:
                raise ArtDNAError("The reference index exists, but no style profile is active.")
            images = self._images(con) if weight and not core_only else {}
        profile = json.loads(selected["snapshot"])
        examples = self._examples(images, query, reference_ids, similar_to, weight, mode)
        if core_only and weight:
            direction = [
                "Apply the shared style rules to pose, subject scale and lighting while sourcing original assets.",
                "Carry the shared hierarchy and depth into an original layout; vary the crop and title position.",
                "Balance the shared contrast and edge treatment with a palette and effects invented for this brief.",
            ]
        elif weight == 0:
            direction = ["Search outside the familiar pose, scene and color family.",
                         "Invent a different framing and title relationship from the collection.",
                         "Keep the shared contrast and hierarchy. Vary effects and palette freely."]
        elif weight < 65:
            direction = ["Use one relevant reference cue in search and also collect a contrasting asset option.",
                         "Borrow one visual relationship, then change subject geometry and title placement.",
                         "Preserve either lighting or type character; invent the other."]
        else:
            direction = ["Search for references' subject scale, pose energy and light direction on a new scene.",
                         "Translate the relevant hierarchy and depth cues into a different layout.",
                         "Carry the reference's contrast, edge treatment and type weight into a fresh palette or framing."]
        return {
            "version": selected["id"], "mode": mode, "reference_percent": weight,
            "style": {key: profile[key] for key in ("identity", "rules", "variation")},
            "human_feedback": feedback,
            "apply": dict(zip(("sourcing", "composition", "finishing"), direction)),
            "examples": examples,
        }

    @staticmethod
    def _examples(images, query, chosen, similar_to, weight, mode):
        ids = list(dict.fromkeys(chosen))
        if not images and not ids and not similar_to:
            return []
        if any(image_id not in images for image_id in ids) or (similar_to and similar_to not in images):
            raise ArtDNAError("An example ID is outside the active reference index.")
        anchor = images[similar_to]["features"]["signature"] if similar_to else None
        query_words = words(query) - QUERY_STOP_WORDS

        def rank(image_id):
            item = images[image_id]
            note = item["note"] or {}
            tags = words(" ".join(note.get("tags", [])))
            title = words(note.get("topic") or item["title"] or Path(item["file"]).stem) - QUERY_STOP_WORDS
            semantic = 3*len(tags & query_words) + 4*len(title & query_words)
            if len(title) > 1 and title <= query_words:
                semantic += 8
            visual = similarity(item["features"]["signature"], anchor) if anchor else 0
            return semantic + 2*visual, bool(note), image_id

        limit = min(3, max(len(ids), 3 if mode == "reference" else math.ceil(weight/35)))
        candidates = sorted((i for i in images if i not in ids and i != similar_to), key=rank, reverse=True)
        ids.extend(candidates[:max(0, limit-len(ids))])
        output = []
        for image_id in ids:
            item = images[image_id]
            note = item["note"]
            output.append({"id": image_id, "topic": note["topic"] if note else (item["title"] or Path(item["file"]).stem)[:75],
                           "cue": note["takeaway"] if note else "Measured palette only; visual notes pending.",
                           "colors": item["features"]["palette"][:3]})
        return output

    def batch(self, image_ids=(), limit=2):
        if not 1 <= limit <= 2 or len(image_ids) > limit:
            raise ArtDNAError("Request at most two images to keep visual context small.")
        with closing(self._connect()) as con:
            images = self._images(con)
            if image_ids:
                ids = list(dict.fromkeys(image_ids))
                if any(i not in images for i in ids):
                    raise ArtDNAError("An image ID is outside the active reference index.")
            else:
                ids = sorted((i for i in images if not images[i]["note"]), key=lambda i: images[i]["file"])[:limit]
            output = []
            for image_id in ids:
                item = images[image_id]
                match = con.execute("SELECT size,modified FROM files WHERE path=?", (item["file"],)).fetchone()
                path = (self.library / item["file"]).resolve()
                if not path.is_relative_to(self.library) or not path.is_file():
                    raise ArtDNAError("Reference is unavailable. Refresh the local reference index.")
                stat = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (match["size"], match["modified"]):
                    raise ArtDNAError("Reference changed. Refresh the local reference index.")
                with Image.open(path) as source, io.BytesIO() as image:
                    source.thumbnail((720, 720), Image.Resampling.LANCZOS)
                    ImageOps.exif_transpose(source).convert("RGB").save(image, "JPEG", quality=84)
                    output.append((image_id, item["file"], image.getvalue()))
        return output


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("index", "install-profile", "install-topics", "status", "activate", "versions"))
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    store = ArtDNA(root/"ArtDNA"/"art_dna.db", root/"ReferenceLibrary")
    if args.command == "index":
        result = store.index()
    elif args.command == "install-profile":
        seed = Path(args.value) if args.value else root/"ArtDNA"/"initial_review.json"
        result = {"installed": store.install_profile(json.loads(seed.read_text(encoding="utf-8")))}
    elif args.command == "install-topics":
        topics = Path(args.value) if args.value else root/"ArtDNA"/"reference_topics.json"
        result = {"topics": store.install_topics(json.loads(topics.read_text(encoding="utf-8")))}
    elif args.command == "activate":
        store.activate(args.value)
        result = store.status()
    elif args.command == "versions":
        with closing(store._connect()) as con:
            result = [dict(row) for row in con.execute("SELECT id,created FROM profiles ORDER BY created")]
    else:
        result = store.status()
    print(compact(result))


if __name__ == "__main__":
    main()
