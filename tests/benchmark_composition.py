"""Compare model-facing cost for the same six-layer PhotoCraft composition."""

import hashlib
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image, ImageDraw
from starlette.testclient import TestClient

from editing_efficiency import Usage
from photocraft_gateway import NativeGateway
from tests.test_photocraft_gateway import isolated_server


def run(composed):
    with TemporaryDirectory(prefix="photocraft-compose-benchmark-") as directory:
        root=Path(directory)
        projects=root/"Projects"
        assets=root/"Assets"
        projects.mkdir()
        assets.mkdir()
        for index in range(6):
            layer=Image.new("RGBA",(320,180),(0,0,0,0))
            draw=ImageDraw.Draw(layer)
            draw.rectangle((index*18,index*12,319-index*9,179-index*5),
                           fill=(35+index*25,28+index*12,55+index*14,180))
            layer.save(assets/f"layer-{index}.png")

        studio,_=isolated_server(root)
        gateway=NativeGateway(studio.CLI,(projects,assets),projects,mode="headless",timeout=65)
        usage=Usage(root/"usage.sqlite3")
        metrics={"calls":0,"request_text_bytes":0,"response_text_bytes":0}
        with patch.multiple(studio,PROJECTS=projects,ASSETS=assets,DOWNLOADED=assets,
                            NATIVE=gateway,EDIT_USAGE=usage), \
             TestClient(studio.app,base_url=studio.PUBLIC_URL) as client:
            headers={"Authorization":"Bearer "+studio.TOKEN,
                     "Accept":"application/json, text/event-stream"}

            def call(name,**args):
                body={"jsonrpc":"2.0","id":metrics["calls"]+1,"method":"tools/call",
                      "params":{"name":name,"arguments":args}}
                payload=json.dumps(body,separators=(",",":"),ensure_ascii=False).encode()
                result=client.post("/mcp",headers={**headers,"Content-Type":"application/json"},
                                   content=payload).json()["result"]
                assert not result.get("isError"),result.get("content")
                metrics["calls"]+=1
                metrics["request_text_bytes"]+=len(payload)
                metrics["response_text_bytes"]+=sum(len(block["text"].encode())
                    for block in result["content"] if block["type"]=="text")
                return result["content"]

            raw=[
                {"id":"shape.create","params":{"name":"Accent","kind":"rect",
                    "rect":[20,20,85,24],"fill":"#F4A040"}},
                {"id":"type.create","params":{"name":"Headline","text":"NIGHT SIGNAL",
                    "x":30,"y":132,"size":46,"color":"#FFFFFF","font":"Impact"}},
            ]
            if composed:
                short=[["r","Accent",20,20,85,24,"#F4A040"],
                       ["t","Headline","NIGHT SIGNAL",30,132,46,"#FFFFFF","Impact"]]
                call("native_compose",output_name="measure",width=320,height=180,
                     assets=[{"file":f"layer-{index}.png","scale":100,"center":[160,90]}
                             for index in range(6)],actions_json=json.dumps(short,separators=(",",":")),
                     job_id="measure")
            else:
                previous=None
                for index in range(6):
                    name=f"measure-stage-{index}"
                    args={"image_filename":f"layer-{index}.png","output_name":name,
                          "width":320,"height":180,"scale":100,"center_x":160,"center_y":90,
                          "job_id":"measure"}
                    if previous:
                        args["project_filename"]=previous
                    call("load_image_in_photocraft",**args)
                    previous=name+".pcraft"
                call("native_edit_batch",job_id="measure",
                     actions_json=json.dumps(raw,separators=(",",":")),preview="none")
                call("native_tool_call",tool_name="doc_save",job_id="measure",
                     arguments_json=json.dumps({"path":str(projects/"measure.pcraft")}))

            call("native_tool_call",tool_name="doc_export",
                 arguments_json=json.dumps({"path":str(projects/"measure.png")}),job_id="measure")
            with Image.open(projects/"measure.png") as image:
                metrics["pixels_sha256"]=hashlib.sha256(image.convert("RGBA").tobytes()).hexdigest()
            metrics["project_exists"]=(projects/"measure.pcraft").is_file()
            metrics["tracked_calls"]=usage.stats("measure")["calls"]
        metrics["estimated_text_tokens"]=math.ceil(
            (metrics["request_text_bytes"]+metrics["response_text_bytes"])/4)
        return metrics


if __name__=="__main__":
    old=run(False)
    new=run(True)
    assert old["pixels_sha256"]==new["pixels_sha256"]
    assert old["project_exists"] and new["project_exists"]
    print(json.dumps({"old":old,"new":new,"same_pixels":True},separators=(",",":")))
