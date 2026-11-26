"""Local, compact anime asset sourcing for PhotoCraft. No browser scraping."""

from __future__ import annotations

import concurrent.futures
from contextlib import contextmanager
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import statistics
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from PIL import Image, ImageFilter, ImageOps, ImageStat

import image_mcp


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    provider: str
    post_id: str
    file_url: str
    page_url: str
    source_url: str
    artist: str
    tags: str
    rating: str
    width: int
    height: int
    file_ext: str
    score: int
    md5: str


class Adapter:
    name = ""
    endpoint = ""
    page_base = ""
    safe_ratings = frozenset()
    min_interval = 1.0  # Conservative sustained rate; independent per provider.

    def __init__(self, fetch=image_mcp.public_fetch):
        self.fetch = fetch
        self._lock = threading.Lock()
        self._next = 0.0
        self.failures = 0
        self.cooldown_until = 0.0
        self.last_error = ""
        self.calls = 0
        self.successes = 0
        self.latencies = []
        self.on_event = None

    def params(self, tags: str, page: int, limit: int) -> dict:
        raise NotImplementedError

    def parse(self, row: dict) -> Candidate:
        raise NotImplementedError

    def search(self, tags: str, page=1, limit=12) -> list[Candidate]:
        with self._lock:
            now = time.monotonic()
            if now < self.cooldown_until:
                raise SourceError("provider_cooldown")
            delay = max(0.0, self._next-now)
            self._next = max(now, self._next)+self.min_interval
        if delay:
            time.sleep(delay)
        started = time.monotonic()
        url = self.endpoint + "?" + urlencode(self.params(tags, page, limit))
        self.calls += 1
        try:
            raw, _ = self.fetch(url, limit=2_000_000, timeout=15, accept="application/json")
            rows = json.loads(raw)
            if not isinstance(rows, list):
                raise SourceError("unexpected_response")
            found = []
            for row in rows:
                try:
                    item = self.parse(row)
                    if (item.rating in self.safe_ratings and item.file_url.startswith("https://")
                            and item.width > 0 and item.height > 0):
                        found.append(item)
                except (KeyError, TypeError, ValueError):
                    continue
            self.failures = 0
            self.last_error = ""
            self.successes += 1
            self.latencies.append(round(time.monotonic()-started, 3))
            self.latencies = self.latencies[-30:]
            if self.on_event:
                self.on_event(self.name, "ok", self.latencies[-1])
            return found
        except Exception as exc:
            message = str(exc).lower()
            self.failures += 1
            self.last_error = ("rate_limited" if "429" in message else
                               "access_denied" if "403" in message or "401" in message else
                               "unavailable" if "50" in message or "timed out" in message else
                               "invalid_response")
            duration = 300 if self.last_error == "access_denied" else min(120, 5*2**min(self.failures, 4))
            self.cooldown_until = time.monotonic()+duration
            if self.on_event:
                self.on_event(self.name, self.last_error, round(time.monotonic()-started,3))
            raise SourceError(self.last_error) from None

    def health(self):
        return {"provider": self.name, "calls": self.calls, "successes": self.successes,
                "latency_ms": round(1000*statistics.mean(self.latencies)) if self.latencies else None,
                "cooldown_s": max(0, round(self.cooldown_until-time.monotonic())),
                "last_error": self.last_error or None}


class Danbooru(Adapter):
    name = "danbooru"
    endpoint = "https://danbooru.donmai.us/posts.json"
    page_base = "https://danbooru.donmai.us/posts/"
    safe_ratings = frozenset({"g"})

    def params(self, tags, page, limit):
        return {"tags": tags+" rating:g", "page": page, "limit": limit,
                "only": "id,file_url,source,tag_string,tag_string_artist,rating,image_width,image_height,file_ext,score,md5"}

    def parse(self, row):
        return Candidate(self.name, str(row["id"]), str(row.get("file_url") or ""),
                         self.page_base+str(row["id"]), str(row.get("source") or ""),
                         str(row.get("tag_string_artist") or ""), str(row.get("tag_string") or ""),
                         str(row.get("rating") or ""), int(row.get("image_width") or 0),
                         int(row.get("image_height") or 0), str(row.get("file_ext") or ""),
                         int(row.get("score") or 0), str(row.get("md5") or ""))


class Safebooru(Adapter):
    name = "safebooru"
    endpoint = "https://safebooru.org/index.php"
    page_base = "https://safebooru.org/index.php?page=post&s=view&id="
    safe_ratings = frozenset({"safe", "s"})

    def params(self, tags, page, limit):
        return {"page": "dapi", "s": "post", "q": "index", "json": 1,
                "tags": tags+" rating:safe", "pid": page-1, "limit": limit}

    def parse(self, row):
        return Candidate(self.name, str(row["id"]), str(row.get("file_url") or ""),
                         self.page_base+str(row["id"]), str(row.get("source") or ""),
                         str(row.get("owner") or ""), str(row.get("tags") or ""),
                         str(row.get("rating") or ""), int(row.get("width") or 0),
                         int(row.get("height") or 0), Path(urlsplit(str(row.get("file_url") or "")).path).suffix.lstrip("."),
                         int(row.get("score") or 0), str(row.get("hash") or ""))


class Yandere(Adapter):
    name = "yandere"
    endpoint = "https://yande.re/post.json"
    page_base = "https://yande.re/post/show/"
    safe_ratings = frozenset({"s"})

    def params(self, tags, page, limit):
        return {"tags": tags+" rating:s", "page": page, "limit": limit}

    def parse(self, row):
        return Candidate(self.name, str(row["id"]), str(row.get("file_url") or ""),
                         self.page_base+str(row["id"]), str(row.get("source") or ""),
                         str(row.get("author") or ""), str(row.get("tags") or ""),
                         str(row.get("rating") or ""), int(row.get("width") or 0),
                         int(row.get("height") or 0), str(row.get("file_ext") or ""),
                         int(row.get("score") or 0), str(row.get("md5") or ""))


ALIASES = {
    "naruto": {"danbooru": "uzumaki_naruto", "safebooru": "uzumaki_naruto", "yandere": "uzumaki_naruto"},
    "sasuke": {"danbooru": "uchiha_sasuke", "safebooru": "uchiha_sasuke", "yandere": "uchiha_sasuke"},
    "light yagami": {"danbooru": "yagami_light", "safebooru": "yagami_light", "yandere": "yagami_light"},
    "eren": {"danbooru": "eren_yeager", "safebooru": "eren_yeager", "yandere": "eren_yeager"},
}
HINTS = {"fighting": "fighting_stance", "action": "action_pose", "dynamic": "dynamic_pose",
         "angry": "angry", "aggressive": "angry", "smiling": "smile", "smile": "smile",
         "red": "red_background", "blue": "blue_background", "dramatic": "dramatic_lighting",
         "dark": "dark_background", "backlit": "backlighting", "glowing": "glowing",
         "full body": "full_body", "closeup": "close-up", "close-up": "close-up",
         "portrait": "portrait", "looking at viewer": "looking_at_viewer",
         "transparent": "transparent_background", "white background": "white_background"}


def queries(character: str, brief: str, provider: str, role: str) -> list[str]:
    name = re.sub(r"[^a-z0-9 ]", "", character.lower()).strip()
    base = ALIASES.get(name, {}).get(provider, name.replace(" ", "_"))
    if not base:
        raise SourceError("Supply a character or series for targeted sourcing.")
    hints = [v for k, v in HINTS.items() if re.search(r"\b"+re.escape(k)+r"\b",brief.lower())]
    if role in {"foreground", "secondary"}:
        hints = ["solo"] + hints
    elif role == "background":
        hints = ["scenery", "landscape"] + hints
    elif role in {"overlay", "effect"}:
        hints = ["visual_effects"] + hints
    # Danbooru's anonymous search is limited to two tags, including rating.
    if provider == "danbooru":
        return [base]
    return [base+" "+h for h in hints[:2]] + [base]


def _inspect(raw: bytes) -> dict:
    try:
        with Image.open(io.BytesIO(raw)) as im:
            image_format = im.format
            if image_format == "GIF":
                raise SourceError("animated_or_gif_unsupported")
            im.verify()
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            w, h = im.size
            if w*h > 32_000_000:
                raise SourceError("too_large")
            thumb = ImageOps.exif_transpose(im).convert("RGBA")
            thumb.thumbnail((256, 256))
            opaque = thumb.getchannel("A").getextrema()[0] == 255
            gray = ImageOps.grayscale(thumb)
            edges = gray.filter(ImageFilter.FIND_EDGES)
            sharpness = round(ImageStat.Stat(edges).stddev[0], 2)
            tiny = gray.resize((9, 8))
            px = list(tiny.get_flattened_data())
            dhash = sum((1 << (y*8+x)) for y in range(8) for x in range(8)
                        if px[y*9+x] > px[y*9+x+1])
            alpha_bbox = thumb.getchannel("A").getbbox()
            coverage = round(((alpha_bbox[2]-alpha_bbox[0])*(alpha_bbox[3]-alpha_bbox[1])) /
                             max(1, thumb.width*thumb.height), 3) if alpha_bbox else 0
            return {"width": w, "height": h, "format": image_format, "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(), "dhash": f"{dhash:016x}",
                    "sharpness": sharpness, "transparent": not opaque, "coverage": coverage}
    except (OSError, ValueError, Image.DecompressionBombError):
        raise SourceError("invalid_image") from None


def _near(a: str, b: str) -> bool:
    return (int(a, 16)^int(b, 16)).bit_count() <= 5


def _score(tags_text: str, width: int, height: int, sharpness: float,
           transparent: bool, minimum: int, role: str, terms: list[str]) -> tuple[float,int,int]:
    tags = set(tags_text.lower().split())
    relevance = sum(1 for term in terms if any(term in tag for tag in tags))
    role_good = ({"solo","1boy","1girl","full_body","upper_body","white_background","transparent_background"}
                 if role in {"foreground","secondary"} else
                 {"scenery","landscape","no_humans","cityscape","sky","outdoors"})
    role_bad = ({"multiple_boys","multiple_girls","comic","manga","text","watermark","collage"}
                if role in {"foreground","secondary"} else {"solo","portrait"})
    suitability = min(12, 4*len(tags & role_good)) - min(15, 5*len(tags & role_bad))
    quality = round(max(0,min(100, 30 + 12*relevance + suitability
                        + 8*math.log2(max(1,min(width,height)/minimum))
                        + min(20,sharpness/3)
                        + (8 if transparent and role in {"foreground","secondary"} else 0))), 1)
    return quality,relevance,suitability


def _role_eligible(candidate: Candidate, role: str) -> bool:
    tags = set(candidate.tags.lower().split())
    if role in {"foreground","secondary"}:
        if tags & {"2boys","3boys","4boys","2girls","3girls","multiple_boys","multiple_girls",
                   "group_picture","scenery","landscape","comic","manga","collage"}:
            return False
        return bool(tags & {"solo","1boy","1girl"})
    if role == "background":
        return bool(tags & {"scenery","landscape","no_humans","cityscape","outdoors","sky","forest"})
    return True


class Sourcing:
    def __init__(self, root: Path, providers=None, fetch=image_mcp.public_fetch):
        self.root = Path(root).resolve()
        self.folder = self.root/"Assets"/"Downloaded"
        self.db = self.root/"Logs"/"anime_sourcing.sqlite3"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.fetch = fetch
        self.providers = providers if providers is not None else [Danbooru(fetch), Safebooru(fetch), Yandere(fetch)]
        with self._con() as con:
            con.executescript("""
              CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, path TEXT NOT NULL, provider TEXT NOT NULL,
                post_id TEXT, file_url TEXT UNIQUE, page_url TEXT, source_url TEXT, artist TEXT, tags TEXT,
                rating TEXT, width INTEGER, height INTEGER, file_type TEXT, sha256 TEXT, dhash TEXT,
                quality REAL, quality_notes TEXT, rights TEXT, created REAL, used INTEGER DEFAULT 0);
              CREATE TABLE IF NOT EXISTS packs(id TEXT PRIMARY KEY, request_hash TEXT, created REAL,
                selection_json TEXT NOT NULL, status TEXT NOT NULL, diagnostics_json TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS idx_packs_request ON packs(request_hash,created);
              CREATE INDEX IF NOT EXISTS idx_assets_post ON assets(provider,post_id);
              CREATE TABLE IF NOT EXISTS provider_events(provider TEXT, status TEXT, seconds REAL, created REAL);
              CREATE TABLE IF NOT EXISTS provider_state(provider TEXT PRIMARY KEY, cooldown_until REAL,
                failures INTEGER, last_error TEXT);
              CREATE TABLE IF NOT EXISTS source_calls(pack_id TEXT, cache_hit INTEGER, response_bytes INTEGER,
                seconds REAL, created REAL);
            """)
        with self._con() as con:
            states = {r[0]:r for r in con.execute("SELECT * FROM provider_state")}
        for provider in self.providers:
            provider.on_event = self._event
            if provider.name in states:
                row = states[provider.name]
                provider.cooldown_until = time.monotonic()+max(0,(row[1] or 0)-time.time())
                provider.failures = row[2] or 0
                provider.last_error = row[3] or ""

    @contextmanager
    def _con(self):
        con = sqlite3.connect(self.db, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def health(self):
        with self._con() as con:
            rows = con.execute("SELECT provider,COUNT(*),SUM(status='ok'),AVG(CASE WHEN status='ok' THEN seconds END) "
                               "FROM provider_events GROUP BY provider").fetchall()
            call = con.execute("SELECT COUNT(*),SUM(cache_hit),SUM(response_bytes),AVG(seconds) FROM source_calls").fetchone()
        history = {r[0]:r for r in rows}
        providers = []
        for provider in self.providers:
            status = provider.health()
            past = history.get(provider.name)
            if past:
                status.update({"calls":past[1],"successes":past[2],
                               "latency_ms":round(1000*past[3]) if past[3] else None})
            providers.append(status)
        return {"providers":providers,"mcp_sourcing_calls":call[0],"cache_hits":call[1] or 0,
                "response_bytes":call[2] or 0,"average_seconds":round(call[3],2) if call[3] else None}

    def _event(self, provider, status, seconds):
        active = next((p for p in self.providers if p.name == provider),None)
        remaining = max(0,(active.cooldown_until-time.monotonic())) if active else 0
        with self._con() as con:
            con.execute("INSERT INTO provider_events VALUES (?,?,?,?)",(provider,status,seconds,time.time()))
            con.execute("INSERT OR REPLACE INTO provider_state VALUES (?,?,?,?)",
                        (provider,time.time()+remaining,active.failures if active else 0,
                         active.last_error if active else ""))

    def log_call(self, pack_id, cached, response_bytes, seconds):
        with self._con() as con:
            con.execute("INSERT INTO source_calls VALUES (?,?,?,?,?)",
                        (pack_id,int(cached),response_bytes,seconds,time.time()))

    def _cached(self, request_hash):
        with self._con() as con:
            row = con.execute("SELECT * FROM packs WHERE request_hash=? AND created>? ORDER BY created DESC LIMIT 1",
                              (request_hash,time.time()-7*86400)).fetchone()
            if row:
                selection = json.loads(row["selection_json"])
                if all((self.root/x["path"]).is_file() for group in selection.values() for x in group):
                    return self._summary(row["id"], selection, row["status"], {"cache_hit": True}, 0)
        return None

    def _asset_cached(self, url):
        with self._con() as con:
            row = con.execute("SELECT * FROM assets WHERE file_url=?", (url,)).fetchone()
            if row and (self.root/row["path"]).is_file():
                return dict(row)
        return None

    def _save_asset(self, candidate: Candidate, minimum: int, role: str, terms: list[str]):
        prior = self._asset_cached(candidate.file_url)
        if prior:
            return prior, "cache"
        raw, _ = self.fetch(candidate.file_url, limit=24*1024*1024, timeout=25)
        info = _inspect(raw)
        if min(info["width"],info["height"]) < minimum:
            raise SourceError("below_resolution")
        if info["sharpness"] < 9:
            raise SourceError("low_detail")
        with self._con() as con:
            rows = con.execute("SELECT * FROM assets WHERE sha256=?", (info["sha256"],)).fetchall()
            for row in rows:
                if (self.root/row["path"]).is_file():
                    return dict(row), "exact_duplicate"
            for row in con.execute("SELECT * FROM assets WHERE dhash IS NOT NULL AND width>=? AND height>=?",
                                   (minimum,minimum)):
                if _near(row["dhash"], info["dhash"]) and (self.root/row["path"]).is_file():
                    return dict(row), "near_duplicate"
        ext = image_mcp.FORMATS.get(info["format"])
        if not ext:
            raise SourceError("unsupported_format")
        asset_id = info["sha256"][:20]
        path = self.folder/("anime-"+asset_id+ext)
        if not path.exists():
            import tempfile
            with tempfile.NamedTemporaryFile(dir=self.folder, prefix=".anime-", delete=False) as handle:
                temp = Path(handle.name)
                try:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                except Exception:
                    temp.unlink(missing_ok=True)
                    raise
            os.replace(temp,path)
        quality,relevance,suitability = _score(candidate.tags,info["width"],info["height"],
            info["sharpness"],info["transparent"],minimum,role,terms)
        notes = {"tag_matches":relevance,"role_tag_score":suitability,
                 "sharpness":info["sharpness"],"transparent":info["transparent"],
                 "coverage":info["coverage"],"semantic_visual_check":"not_performed"}
        record = {"id":asset_id,"path":str(path.relative_to(self.root)).replace("\\","/"),
                  "provider":candidate.provider,"post_id":candidate.post_id,"file_url":candidate.file_url,
                  "page_url":candidate.page_url,"source_url":candidate.source_url,"artist":candidate.artist,
                  "tags":candidate.tags[:3000],"rating":candidate.rating,"width":info["width"],
                  "height":info["height"],"file_type":info["format"],"sha256":info["sha256"],
                  "dhash":info["dhash"],"quality":quality,"quality_notes":json.dumps(notes),
                  "rights":"unknown", "created":time.time(),"used":0}
        with self._con() as con:
            con.execute("INSERT OR IGNORE INTO assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",tuple(record.values()))
        return record, "download"

    def _summary(self, pack_id, selected, status, diagnostics, duration):
        return {"pack_id":pack_id,"status":status,
                "roles":{role:[{"asset_id":a["id"],"path":a["path"],"size":[a["width"],a["height"]],
                                "provider":a["provider"],"page":a["page_url"],"artist":a["artist"],
                                "quality":a["quality"],"rights":a["rights"]} for a in group]
                         for role,group in selected.items()},
                "missing":[role for role,group in selected.items() if not group],
                "diagnostics":diagnostics,"seconds":round(duration,2)}

    def source(self, creative_brief: str, characters: list[str], series: str="", asset_roles=None,
               target_width=1280, target_height=720, minimum_quality=45,
               max_candidates_per_role=2, preferred_visual_style="", previous_pack_id="", replace_role=""):
        started = time.monotonic()
        if (not isinstance(creative_brief,str) or len(creative_brief)>2000 or not isinstance(characters,list)
                or len(characters)>8 or any(not isinstance(c,str) or len(c)>100 for c in characters)
                or not 1<=target_width<=4096 or not 1<=target_height<=4096
                or not 1<=max_candidates_per_role<=4 or not 0<=minimum_quality<=100):
            raise SourceError("Invalid brief, character list, canvas size, quality, or candidate count.")
        roles = asset_roles or ["foreground","background"]
        valid_roles = {"foreground","secondary","background","overlay","effect"}
        if not isinstance(roles,list) or not roles or len(roles)>5 or any(r not in valid_roles for r in roles):
            raise SourceError("Roles must be foreground, secondary, background, overlay, or effect.")
        if replace_role and (replace_role not in roles or not previous_pack_id):
            raise SourceError("A replacement role needs a previous pack ID and requested role.")
        request = {"planner_version":3,"brief":creative_brief,"characters":characters,"series":series,"roles":roles,
                   "size":[target_width,target_height],"quality":minimum_quality,
                   "max":max_candidates_per_role,"style":preferred_visual_style,
                   "previous":previous_pack_id,"replace":replace_role}
        request_hash = hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
        if not replace_role:
            hit = self._cached(request_hash)
            if hit:
                return hit
        selected = {role:[] for role in roles}
        exclude = set()
        if previous_pack_id:
            with self._con() as con:
                row = con.execute("SELECT selection_json FROM packs WHERE id=?",(previous_pack_id,)).fetchone()
            if not row:
                raise SourceError("Previous pack not found.")
            old = json.loads(row["selection_json"])
            exclude = {x["id"] for group in old.values() for x in group}
            if replace_role:
                selected.update({role:group for role,group in old.items() if role != replace_role and role in roles})
        diagnostics = {"provider_errors":{},"searched":0,"downloaded":0,"reused":0,"rejected":{},"cache_hit":False}
        target_roles = [replace_role] if replace_role else roles
        for role in target_roles:
            subject = (characters[1] if role == "secondary" and len(characters)>1 else
                       characters[0] if characters else series)
            if not subject:
                diagnostics["rejected"][role] = "no_character_or_series"
                continue
            terms = [t for t in re.sub(r"[^a-z0-9 ]","",subject.lower()).split() if len(t)>2]
            terms += [tag for hint,tag in HINTS.items()
                      if re.search(r"\b"+re.escape(hint)+r"\b",(creative_brief+" "+preferred_visual_style).lower())]
            minimum = max(480, int(min(target_width,target_height)*(.8 if role in {"foreground","secondary"} else .45)))
            if role in {"overlay","effect"}:
                minimum = 320
            found = []
            plans = {p.name:queries(subject,creative_brief+" "+preferred_visual_style,p.name,role) for p in self.providers}
            # First pass checks all healthy providers concurrently. One expansion per weak role.
            for expansion in range(2):
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.providers) or 1) as pool:
                    futures = {pool.submit(p.search,plans[p.name][0] if expansion == 0 else plans[p.name][-1],
                                           1 if expansion == 0 or plans[p.name][0] != plans[p.name][-1] else 2,12):p
                               for p in self.providers if p.cooldown_until<=time.monotonic()}
                    for future,p in futures.items():
                        try:
                            rows = future.result()
                            diagnostics["searched"] += len(rows)
                            found.extend(rows)
                        except Exception:
                            diagnostics["provider_errors"][p.name]=p.last_error or "unavailable"
                unique = list({(c.provider,c.post_id):c for c in found}.values())
                viable = [c for c in unique if min(c.width,c.height)>=minimum and _role_eligible(c,role)]
                if len(viable)>=max_candidates_per_role*2:
                    break
            candidate_list = sorted((c for c in {(c.provider,c.post_id):c for c in found}.values()
                                     if _role_eligible(c,role) and min(c.width,c.height)>=minimum),
                                    key=lambda c:(_score(c.tags,c.width,c.height,18,False,minimum,role,terms)[0],
                                                  min(c.width,c.height),c.score),reverse=True)
            seen = {a["id"] for group in selected.values() for a in group}
            for candidate in candidate_list[:max(6,max_candidates_per_role*5)]:
                if len(selected[role])>=max_candidates_per_role:
                    break
                if min(candidate.width,candidate.height)<minimum:
                    diagnostics["rejected"]["below_resolution"] = diagnostics["rejected"].get("below_resolution",0)+1
                    continue
                if not _role_eligible(candidate,role):
                    diagnostics["rejected"]["role_mismatch"] = diagnostics["rejected"].get("role_mismatch",0)+1
                    continue
                try:
                    asset, how = self._save_asset(candidate,minimum,role,terms)
                    asset = dict(asset)
                    notes = json.loads(asset["quality_notes"])
                    asset["quality"] = _score(asset["tags"],asset["width"],asset["height"],
                        notes["sharpness"],notes["transparent"],minimum,role,terms)[0]
                    if asset["id"] in seen or asset["id"] in exclude:
                        continue
                    seen.add(asset["id"])
                    if asset["quality"] < minimum_quality:
                        diagnostics["rejected"]["below_quality"] = diagnostics["rejected"].get("below_quality",0)+1
                        continue
                    selected[role].append(asset)
                    diagnostics["reused" if how != "download" else "downloaded"] += 1
                except Exception as exc:
                    reason = str(exc) if isinstance(exc,SourceError) else "download_failed"
                    diagnostics["rejected"][reason] = diagnostics["rejected"].get(reason,0)+1
            selected[role].sort(key=lambda a:a["quality"],reverse=True)
        pack_id = uuid.uuid4().hex[:16]
        status = "READY" if all(selected.values()) else "INSUFFICIENT_QUALITY"
        with self._con() as con:
            con.execute("INSERT INTO packs VALUES (?,?,?,?,?,?)",
                        (pack_id,request_hash,time.time(),json.dumps(selected),status,json.dumps(diagnostics)))
        return self._summary(pack_id,selected,status,diagnostics,time.monotonic()-started)

    def pack_assets(self, pack_id: str, choices=None):
        with self._con() as con:
            row = con.execute("SELECT selection_json FROM packs WHERE id=?",(pack_id,)).fetchone()
        if not row:
            raise SourceError("Asset pack not found.")
        groups = json.loads(row["selection_json"])
        choices = choices or {}
        if not isinstance(choices,dict) or any(k not in groups or not isinstance(v,str) for k,v in choices.items()):
            raise SourceError("Choices must map pack roles to selected asset IDs.")
        order = ["background","effect","overlay","secondary","foreground"]
        selected = []
        for role in order:
            items = groups.get(role,[])
            if items:
                chosen = next((a for a in items if a["id"]==choices.get(role)),None) if role in choices else items[0]
                if not chosen:
                    raise SourceError("Selected asset is not in this pack.")
                path = (self.root/chosen["path"]).resolve()
                if (not path.is_relative_to(self.folder.resolve()) or not path.is_file() or path.is_symlink()):
                    raise SourceError("A selected file is missing or outside Downloads.")
                selected.append({"role":role,"asset":chosen,"relative":str(path.relative_to((self.root/"Assets").resolve())).replace("\\","/")})
        if not selected:
            raise SourceError("Asset pack contains no usable files.")
        return selected
