"""Measure model-facing MCP bytes for the same native edit with either interface."""

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image
from starlette.testclient import TestClient

from photocraft_gateway import NativeGateway
from editing_efficiency import Usage
from test_photocraft_gateway import isolated_server


BACKGROUND = [
    {"id":"shape.create","params":{"name":"Moon","kind":"ellipse","rect":[740,96,380,380],"fill":"#f9aa70"}},
    {"id":"layer.layerStyle.outerGlow","params":{"color":"#f47958","size":85,"opacity":80}},
    {"id":"shape.create","params":{"name":"Atmosphere","kind":"ellipse","rect":[820,66,348,364],"fill":"#162438"}},
]
TYPE = {"name":"Headline","text":"NIGHT SIGNAL","x":76,"y":318,"size":108,"color":"#f9ecd8","font":"Impact"}
STYLE = {"tracking":22}
SHADOW = {"color":"#090d14","opacity":80,"distance":6,"size":20}
LETTERING = [
    {"id":"type.create","params":TYPE}, {"id":"type.setStyle","params":STYLE},
    {"id":"layer.layerStyle.dropShadow","params":SHADOW},
    {"id":"type.create","params":{"name":"Subtitle","text":"A light beyond the ridge","x":78,"y":394,
                                 "size":33,"color":"#aec0db","font":"Segoe UI"}},
]


def run(compact=False, batched=False):
    counters = {"calls":0,"request_text_bytes":0,"response_text_bytes":0,"image_bytes":0,"image_pixels":0}
    by_operation = {}
    with TemporaryDirectory(prefix="photo-edit-measure-") as folder:
        root = Path(folder)
        studio, _ = isolated_server(root)
        gateway = NativeGateway(studio.CLI, [root], root, mode="bridge", timeout=65)
        usage = Usage(root/"usage.sqlite3")
        with patch.multiple(studio, NATIVE=gateway, EDIT_USAGE=usage), TestClient(studio.app, base_url=studio.PUBLIC_URL) as client:
            headers = {"Authorization":"Bearer "+studio.TOKEN, "Accept":"application/json, text/event-stream"}

            def call(name, **args):
                body = {"jsonrpc":"2.0","id":counters["calls"]+1,"method":"tools/call",
                        "params":{"name":name,"arguments":args}}
                payload = json.dumps(body, separators=(",",":")).encode()
                response = client.post("/mcp",headers={**headers,"Content-Type":"application/json"},content=payload)
                result = response.json()["result"]
                assert not result.get("isError"), result.get("content")
                counters["calls"] += 1
                counters["request_text_bytes"] += len(payload)
                measured = by_operation.setdefault(args.get("tool_name",name),{"text_bytes":0,"image_bytes":0,"calls":0})
                measured["calls"] += 1
                for block in result["content"]:
                    if block["type"] == "text":
                        size = len(block["text"].encode())
                        counters["response_text_bytes"] += size
                        measured["text_bytes"] += size
                    elif block["type"] == "image":
                        data=base64.b64decode(block["data"],validate=True)
                        counters["image_bytes"] += len(data)
                        measured["image_bytes"] += len(data)
                        with Image.open(io.BytesIO(data)) as image:
                            counters["image_pixels"] += image.width*image.height
                return result["content"]

            def native(tool, **args):
                return call("native_tool_call",tool_name=tool,arguments_json=json.dumps(args,separators=(",",":")))

            if compact:
                call("native_command_help",query="type.create",limit=1,job_id="night-signal")
            else:
                call("native_tool_catalog")
                native("command_list")
            native("doc_new",name="Night Signal",width=1280,height=720,background="#162438")
            if compact:
                result=call("native_edit_batch",job_id="night-signal",actions_json=json.dumps(
                    [*BACKGROUND,{"use":"title","args":{**TYPE,"style":STYLE,"shadow":SHADOW}},LETTERING[-1]],
                    separators=(",",":")),preview="layout")
                preview_id=json.loads(result[0]["text"])["preview_id"]
                unchanged=call("native_edit_batch",job_id="night-signal",actions_json="[]",
                               preview="layout",known_preview_id=preview_id)
                assert all(block["type"]=="text" for block in unchanged)
                stats=usage.stats("night-signal")
                assert stats["calls"]==3
                assert sum(item["native_steps"] for item in stats["operations"])==len(BACKGROUND)+len(LETTERING)
            else:
                if batched:
                    native("command_batch",steps=BACKGROUND)
                else:
                    for command in BACKGROUND:
                        native("command_run",**command)
                native("doc_inspect")
                if batched:
                    native("command_batch",steps=LETTERING)
                else:
                    for command in LETTERING:
                        native("command_run",**command)
                native("doc_inspect")
                native("render_preview")
                native("render_preview")
            native("doc_export",path="night-signal.png")
            native("doc_close")
            with Image.open(root/"night-signal.png") as image:
                counters["pixels_sha256"]=hashlib.sha256(image.convert("RGBA").tobytes()).hexdigest()
    counters["total_text_bytes"]=counters["request_text_bytes"]+counters["response_text_bytes"]
    counters["by_operation"]=by_operation
    return counters


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode",choices=("raw","raw-batched","compact"),default="raw-batched")
    mode=parser.parse_args().mode
    print(json.dumps(run(compact=mode=="compact",batched=mode=="raw-batched")))
