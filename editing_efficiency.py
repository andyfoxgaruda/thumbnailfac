"""Compact native editing plans, context, and measured MCP usage."""

import base64
import hashlib
import io
import json
import math
import re
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from PIL import Image


class EditingError(ValueError):
    """An actionable error safe to return to the agent."""


PROCEDURES = {
    "title": ("type.create", {"style":"type.setStyle", "shadow":"layer.layerStyle.dropShadow",
                              "stroke":"layer.layerStyle.stroke", "gradient":"layer.layerStyle.gradientOverlay"}),
    "glow_shape": ("shape.create", {"outer":"layer.layerStyle.outerGlow", "inner":"layer.layerStyle.innerGlow"}),
    "layer_treatment": (None, {"properties":"layer.setProps", "transform":"edit.transform",
                               "shadow":"layer.layerStyle.dropShadow"}),
    "mask_region": ("select.rect", {"mask":"layer.layerMask.revealSelection", "finish":"layer.layerMask.edit"}),
}


def compact(value):
    return json.dumps(value,ensure_ascii=False,separators=(",",":"),allow_nan=False,sort_keys=True)


def fingerprint(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()[:24]


def validate_job(job_id):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", job_id):
        raise EditingError("Use a short job ID containing letters, numbers, dots, underscores or hyphens.")


@lru_cache(maxsize=64)
def compile_plan(source):
    """Cache validated recipe expansion without making native edits.

    Short forms: ["t",name,text,x,y,size,color,font?],
    ["r",name,x,y,width,height,fill,radius?], ["c",command,params?],
    and ["p",procedure,args]. The full object form remains available.
    """
    if len(source) > 65536:
        raise EditingError("The editing plan is too long. Split it into two batches.")
    try:
        actions = json.loads(source)
    except (ValueError, RecursionError):
        raise EditingError("Supply a JSON array of editing actions.") from None
    if not isinstance(actions, list) or len(actions) > 64:
        raise EditingError("Use an array of up to 64 actions.")
    steps = []
    for action in actions:
        if isinstance(action, list):
            if not action or not isinstance(action[0], str):
                raise EditingError("A short action needs a command letter.")
            kind = action[0]
            if kind == "t" and len(action) in {7, 8}:
                _, name, value, x, y, size, color, *font = action
                params = {"name":name,"text":value,"x":x,"y":y,"size":size,"color":color}
                if font:
                    params["font"] = font[0]
                action = {"id":"type.create","params":params}
            elif kind == "r" and len(action) in {7, 8}:
                _, name, x, y, width, height, fill, *radius = action
                params = {"name":name,"kind":"roundedRect" if radius else "rect",
                          "rect":[x,y,width,height],"fill":fill}
                if radius:
                    params["radii"] = radius[0]
                action = {"id":"shape.create","params":params}
            elif kind == "c" and len(action) in {2, 3}:
                action = {"id":action[1],"params":action[2] if len(action) == 3 else {}}
            elif kind == "p" and len(action) == 3:
                action = {"use":action[1],"args":action[2]}
            else:
                raise EditingError("Use t, r, c or p with the documented short fields.")
        if not isinstance(action, dict):
            raise EditingError("Each action must be an object.")
        if "id" in action:
            if action.keys() - {"id", "params"} or not isinstance(action["id"], str):
                raise EditingError("Native actions accept id and params.")
            params = action.get("params", {})
            if not isinstance(params, dict):
                raise EditingError("Native params must be an object.")
            steps.append({"id":action["id"], "params":params})
            continue
        recipe = action.get("use")
        if action.keys() - {"use","args"} or not isinstance(recipe, str) or recipe not in PROCEDURES:
            raise EditingError("Use a native action or a listed procedure.")
        args = action.get("args")
        if not isinstance(args, dict):
            raise EditingError("Procedure args must be an object.")
        primary, optional = PROCEDURES[recipe]
        shared = {key:value for key,value in args.items() if key not in optional}
        if primary is not None:
            steps.append({"id":primary, "params":shared})
        elif shared:
            raise EditingError(f"{recipe} uses only its named parameter objects.")
        for key, command in optional.items():
            if key in args or recipe == "mask_region" and key == "mask":
                params = args.get(key, {})
                if not isinstance(params, dict):
                    raise EditingError(f"Procedure {key} must be an object.")
                steps.append({"id":command,"params":params})
    if len(steps) > 64:
        raise EditingError("Expanded plan exceeds 64 native steps. Split the batch.")
    try:
        return compact(steps)
    except (ValueError, RecursionError):
        raise EditingError("Use finite JSON numbers in your edit plan.") from None


def command_help(rows, query, detailed, limit):
    terms = query.casefold().split()

    def rank(row):
        command = row["id"].casefold()
        label = row.get("label", "").casefold()
        doc = row.get("params", "")
        haystack = f"{command} {label} {doc.casefold()}"
        if not all(term in haystack for term in terms):
            return None
        return (command == query.casefold(), sum(term in command for term in terms), label)

    matches = [(score, row) for row in rows if (score := rank(row)) is not None]
    matches.sort(key=lambda entry: (-entry[0][0], -entry[0][1], entry[0][2]))
    output = []
    for _, row in matches[:limit]:
        params = row.get("params", "")
        item = {"id":row["id"],"label":row.get("label", "")}
        if detailed:
            item["params"] = params
        else:
            keys = re.findall(r'"([A-Za-z][A-Za-z0-9_]*)":', params)
            item["keys"] = list(dict.fromkeys(keys))[:12]
        output.append(item)
    return {"matches":len(matches),"commands":output}


def document_summary(document, detail):
    layers = []

    def walk(group):
        for layer in group:
            layers.append(layer)
            walk(layer.get("children") or ())

    walk(document.get("layers") or ())
    active = next((layer for layer in layers if layer["id"] == document.get("activeLayer")), None)
    def brief_layer(layer):
        return [layer["id"],layer.get("name", "")[:100],layer["kind"]]

    result = {"canvas":[document["width"],document["height"]],"name":str(document.get("name") or "")[:100],
              "revision":document.get("revision"),"layer_count":len(layers),
              "active":brief_layer(active) if active else None}
    if detail == "layers":
        result["layers"] = [brief_layer(layer) for layer in layers[:48]]
    return result


@lru_cache(maxsize=96)
def cached_asset(path, image_stamp, metadata_stamp):
    asset = Path(path)
    with Image.open(asset) as image:
        result = {"file":asset.name,"size":list(image.size),"format":image.format}
    if metadata_stamp:
        sidecar = asset.with_suffix(asset.suffix+".source.json")
        try:
            record = json.loads(sidecar.read_text(encoding="utf-8"))
            result["transparent"] = bool(record.get("transparent"))
            result["cutout"] = bool(record.get("cutout"))
        except (OSError, ValueError, AttributeError):
            pass
    return result


class Usage:
    def __init__(self, path):
        self.path = Path(path)

    def record(self, job, kind, input_data, blocks, elapsed_ms, steps=0, pattern=None, failed=False):
        if job is None:
            return
        text_bytes = image_bytes = image_pixels = 0
        for block in blocks:
            if block.type == "text":
                text_bytes += len(block.text.encode())
            elif block.type == "image":
                data = base64.b64decode(block.data)
                image_bytes += len(data)
                with Image.open(io.BytesIO(data)) as image:
                    image_pixels += image.width*image.height
        with closing(sqlite3.connect(self.path,timeout=3)) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS usage(
                job TEXT, operation TEXT, input_bytes INTEGER, output_bytes INTEGER,
                image_bytes INTEGER, image_pixels INTEGER, elapsed_ms INTEGER, steps INTEGER,
                pattern TEXT, failed INTEGER, at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            connection.execute("CREATE INDEX IF NOT EXISTS usage_job ON usage(job)")
            connection.execute("""INSERT INTO usage(job,operation,input_bytes,output_bytes,
                image_bytes,image_pixels,elapsed_ms,steps,pattern,failed) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (job,kind,len(compact(input_data).encode()),text_bytes,image_bytes,image_pixels,
                 elapsed_ms,steps,pattern,int(failed)))

    def stats(self, job):
        with closing(sqlite3.connect(self.path,timeout=3)) as con:
            con.row_factory = sqlite3.Row
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='usage'").fetchone():
                return {"job":job,"calls":0}
            rows = [dict(row) for row in con.execute("""SELECT operation,count(*) AS calls,
                sum(input_bytes) AS input_text_bytes, sum(output_bytes) AS output_text_bytes,
                sum(image_bytes) AS image_bytes, sum(image_pixels) AS image_pixels,
                sum(elapsed_ms) AS elapsed_ms, sum(steps) AS native_steps, sum(failed) AS failed
                FROM usage WHERE job=? GROUP BY operation ORDER BY output_text_bytes DESC""",(job,))]
            recurring = con.execute("""SELECT pattern,count(*) AS uses FROM usage
                WHERE job=? AND failed=0 AND pattern IS NOT NULL GROUP BY pattern
                HAVING uses>1 ORDER BY uses DESC LIMIT 4""",(job,)).fetchall()
        output = {"job":job,"calls":sum(row["calls"] for row in rows),"operations":rows}
        if recurring:
            output["repeated_patterns"] = [dict(row) for row in recurring]
        total = sum(row["input_text_bytes"]+row["output_text_bytes"] for row in rows)
        output["estimated_text_tokens"] = math.ceil(total/4)
        return output
