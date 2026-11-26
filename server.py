import asyncio, base64, json, os, re, shutil, subprocess, io, urllib.parse, secrets, hashlib, hmac, sqlite3, time, html
from pathlib import Path
from functools import lru_cache
from tempfile import TemporaryDirectory
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent, TextContent
from mcp.server.mcpserver.exceptions import ToolError
from photocraft_gateway import NativeError, NativeGateway
from studio_stages import StageError
from art_dna import ArtDNA, ArtDNAError, compact
from reference_analysis import build_reference_index as create_reference_index, load_reference_index as read_reference_index
from editing_efficiency import (
    EditingError, Usage, cached_asset, compile_plan, fingerprint, validate_job,
)
from review_jobs import ReviewJobs, ReviewError
from studio_network import load_network
import image_mcp
from anime_sourcing import Sourcing, SourceError
import zipfile
from PIL import Image, ImageOps
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse, Response
import uvicorn

ROOT=Path(__file__).resolve().parent
INCOMING=ROOT/"Incoming"; ASSETS=ROOT/"Assets"; PROJECTS=ROOT/"Projects"; EXPORTS=ROOT/"Exports"
REFERENCE=ROOT/"ReferenceLibrary"
DOWNLOADED=ASSETS/"Downloaded"
ANALYSIS=ROOT/"ReferenceAnalysis"
INDEX_FILE=ANALYSIS/"image_index.json"
GUIDE_FILE=ANALYSIS/"style_guide.md"
for p in (INCOMING,ASSETS,PROJECTS,EXPORTS,REFERENCE,ANALYSIS,DOWNLOADED): p.mkdir(parents=True,exist_ok=True)
NETWORK=load_network(ROOT)

SECRETS=json.loads((ROOT/"secrets.json").read_text(encoding="utf-8"))
TOKEN=SECRETS["mcp_token"]
CLI=shutil.which("photocraft-cli")
if not CLI:
    raise RuntimeError("photocraft-cli was not found on PATH. Open a new PowerShell and confirm photocraft-cli --help works.")
LOCK=asyncio.Lock()
ASSET_DOWNLOAD_SLOTS=asyncio.Semaphore(2)
mcp=MCPServer(
    "PhotoCraft Thumbnail Workshop",
    instructions=(
        "For a new creative job, call get_art_dna_brief once before sourcing and layout. "
        "Use create_review_job and submit_review_result for new artwork. Make one result unless "
        "the user explicitly requests multiple. The user approves permanent PNGs in the review portal. "
        "Use the requested mode and hybrid weight. Reuse that brief during editing. "
        "Use source_poster_assets once for anime artwork, or uploaded Assets and download_image for a known direct URL. "
        "Review the compact asset pack and use load_asset_pack_into_photocraft to import selected layers. "
        "For editing, search exact operations with native_command_help and send compatible changes "
        "in native_edit_batch. For a new layered canvas, import Assets and edit in one native_compose call. "
        "Short actions t/r/c/p reduce repeated JSON. Use summary and preview only at review points. "
        "Request up to two reference images only for a concrete visual decision. "
        "Use style cues to make an original layout."
    ),
)
IMAGE_EXTS={".png",".jpg",".jpeg",".webp",".bmp",".tif",".tiff"}
MAX_PREVIEW_EDGE=1600
MAX_BATCH=4

def safe_name(name):
    name=Path(str(name)).name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,120}",name):
        raise ValueError("Use a simple filename: letters, numbers, spaces, dot, dash or underscore.")
    return name

def source_path(name):
    n=safe_name(name)
    for folder in (INCOMING,ASSETS,DOWNLOADED):
        p=folder/n
        if p.exists() and p.is_file(): return p
    raise FileNotFoundError(f"{n} not found in Incoming or Assets. Upload it first.")

def project_path(name):
    n=safe_name(name)
    if Path(n).suffix.lower() not in (".psd",".psb"): raise ValueError("Project file must be PSD or PSB.")
    p=(PROJECTS/n).resolve()
    if p.parent != PROJECTS.resolve() or not p.exists(): raise FileNotFoundError(f"Project not found in Projects: {n}")
    return p

def output_stem(name):
    return safe_name(Path(name).stem)


def safe_download_name(name, default="downloaded-image"):
    stem=Path(str(name)).stem
    stem=re.sub(r"[^A-Za-z0-9 _.-]","_",stem).strip(" ._")[:80] or default
    return stem

def run_cli(args):
    cmd=[CLI]+[str(x) for x in args]
    r=subprocess.run(cmd,capture_output=True,text=True,timeout=240,shell=False)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout or "PhotoCraft command failed")[-5000:])
    return r.stdout[-8000:]

async def run_locked(args):
    async with LOCK:
        return await asyncio.to_thread(run_cli,args)

async def export_png(psd: Path, stem: str):
    out=EXPORTS/(stem+".png")
    msg=await run_locked(["run",str(psd),"--out",str(out),"--format","png"])
    if not out.exists(): raise RuntimeError("PhotoCraft reported success but the PNG was not created. "+msg)
    return out

@mcp.tool()
def workspace_status() -> str:
    """Show incoming images, reusable assets, reference library, projects and exported thumbnails."""
    result={}
    for label,folder in (("incoming",INCOMING),("assets",ASSETS),("downloaded_assets",DOWNLOADED),("reference_library",REFERENCE),("projects",PROJECTS),("exports",EXPORTS)):
        result[label]=[p.name for p in sorted(folder.iterdir()) if p.is_file()]
    return json.dumps(result,indent=2)

@mcp.tool()
async def create_thumbnail(image_filename: str, title: str, output_name: str,
                           x: int=70, y: int=130, font_size: int=76,
                           color: str="#FFFFFF") -> str:
    """Create an editable thumbnail project from an image in Incoming, Assets or Assets/Downloaded, add a title, and export a PNG."""
    src=source_path(image_filename); stem=output_stem(output_name)
    if not title.strip(): raise ValueError("Title cannot be empty.")
    if not (8 <= font_size <= 240): raise ValueError("font_size must be between 8 and 240.")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}",color): raise ValueError("Color must be a hex color.")
    psd=PROJECTS/(stem+".psd")
    params={"x":int(x),"y":int(y),"text":title[:180],"name":"Thumbnail title","size":int(font_size),"color":color}
    msg=await run_locked(["run",str(src),"--cmd","type.create","--params",json.dumps(params),"--out",str(psd)])
    if not psd.exists(): raise RuntimeError("PSD was not created. "+msg)
    png=await export_png(psd,stem)
    return f"Created editable project: Projects/{psd.name}\nExported thumbnail: Exports/{png.name}"

@mcp.tool()
async def add_text(project_filename: str, text: str, output_name: str,
                   x: int=70, y: int=130, font_size: int=72,
                   color: str="#FFFFFF") -> str:
    """Add a new editable text layer to an existing PSD project and export a PNG preview."""
    src=project_path(project_filename); stem=output_stem(output_name)
    if not text.strip(): raise ValueError("Text cannot be empty.")
    if not (8 <= font_size <= 240): raise ValueError("font_size must be between 8 and 240.")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}",color): raise ValueError("Color must be a hex color.")
    psd=PROJECTS/(stem+".psd")
    params={"x":int(x),"y":int(y),"text":text[:180],"name":"Text "+stem,"size":int(font_size),"color":color}
    msg=await run_locked(["run",str(src),"--cmd","type.create","--params",json.dumps(params),"--out",str(psd)])
    if not psd.exists(): raise RuntimeError("PSD was not created. "+msg)
    await export_png(psd,stem)
    return f"Saved Projects/{psd.name} and Exports/{stem}.png."

@mcp.tool()
async def place_image(project_filename: str, image_filename: str, output_name: str, scale: float=100) -> str:
    """Place an uploaded image as an embedded layer in an existing PSD project. Scale is percent."""
    src=project_path(project_filename); asset=source_path(image_filename); stem=output_stem(output_name)
    if not (1 <= scale <= 500): raise ValueError("scale must be from 1 to 500.")
    psd=PROJECTS/(stem+".psd")
    params={"path":str(asset),"scale":float(scale)}
    msg=await run_locked(["run",str(src),"--cmd","file.placeEmbedded","--params",json.dumps(params),"--out",str(psd)])
    if not psd.exists(): raise RuntimeError("PSD was not created. "+msg)
    await export_png(psd,stem)
    return f"Placed image. Saved Projects/{psd.name} and Exports/{stem}.png."

@mcp.tool()
async def move_layer(project_filename: str, layer_id: int, dx: int, dy: int, output_name: str) -> str:
    """Move a layer by pixel offsets and save a new editable PSD project."""
    src=project_path(project_filename); stem=output_stem(output_name); psd=PROJECTS/(stem+".psd")
    params={"layer":int(layer_id),"dx":int(dx),"dy":int(dy),"units":"px"}
    msg=await run_locked(["run",str(src),"--cmd","layer.translate","--params",json.dumps(params),"--out",str(psd)])
    if not psd.exists(): raise RuntimeError("PSD was not created. "+msg)
    await export_png(psd,stem)
    return f"Moved layer. Saved Projects/{psd.name} and Exports/{stem}.png."

@mcp.tool()
async def inspect_project(project_filename: str) -> str:
    """Inspect a PhotoCraft PSD document and return document information for planning edits."""
    src=project_path(project_filename)
    return await run_locked(["run",str(src),"--cmd","document.inspect","--out",str(PROJECTS/"_inspect_result.json")])

@mcp.tool()
async def list_layers(project_filename: str) -> str:
    """List layer names, IDs, visibility and bounds for a PSD project."""
    src=project_path(project_filename); tmp=PROJECTS/"_inspect_temp.psd"
    msg=await run_locked(["run",str(src),"--cmd","layer.list","--params","{}","--out",str(tmp)])
    if tmp.exists(): tmp.unlink()
    return msg or "No layer details returned."

@mcp.tool()
async def export_thumbnail(project_filename: str, output_name: str) -> str:
    """Export an existing PSD project as a PNG into Exports."""
    src=project_path(project_filename); stem=output_stem(output_name); png=EXPORTS/(stem+".png")
    msg=await run_locked(["run",str(src),"--out",str(png),"--format","png"])
    if not png.exists(): raise RuntimeError("PNG export failed. "+msg)
    return f"Exported Exports/{png.name}. Retrieve it from the portal."

# Keep one native editing session across independent HTTP requests.
NATIVE = NativeGateway.from_environment(CLI, (PROJECTS, INCOMING, ASSETS, DOWNLOADED, EXPORTS), PROJECTS)
EDIT_USAGE = Usage(ROOT / "Logs" / "editing_usage.sqlite3")
REVIEW_JOBS = ReviewJobs(ROOT/"Jobs"/"PendingReview")
ANIME_SOURCES = Sourcing(ROOT)


@mcp.tool()
async def source_poster_assets(creative_brief: str, characters: list[str], series: str="",
                               asset_roles: list[str] | None=None, target_width: int=1280,
                               target_height: int=720, minimum_quality: int=45,
                               max_candidates_per_role: int=2, preferred_visual_style: str="",
                               previous_pack_id: str="", replace_role: str="") -> str:
    """Find and verify anime originals using local cached searches, ranking, and safe public APIs.

    Returns a compact pack with local paths, dimensions, provenance, and missing roles.
    Use previous_pack_id + replace_role to find alternatives without redoing other roles.
    Quality is a technical/metadata score; inspect artwork visually before final use.
    """
    started = time.perf_counter()
    try:
        result = await asyncio.to_thread(
            ANIME_SOURCES.source, creative_brief, characters, series, asset_roles,
            target_width, target_height, minimum_quality, max_candidates_per_role,
            preferred_visual_style, previous_pack_id, replace_role)
        answer = compact(result)
        ANIME_SOURCES.log_call(result["pack_id"],result["diagnostics"].get("cache_hit",False),
                               len(answer.encode("utf-8")),time.perf_counter()-started)
        return answer
    except SourceError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool()
def get_anime_source_health() -> str:
    """Measured provider calls, successful searches, latency, cooldown, and last error this server run."""
    return compact(ANIME_SOURCES.health())


@mcp.tool()
async def load_asset_pack_into_photocraft(pack_id: str, output_name: str,
                                          choices: dict[str,str] | None=None,
                                          width: int=1280, height: int=720,
                                          actions_json: str="[]", preview: str="none",
                                          job_id: str | None=None) -> str:
    """Import chosen pack images as editable layers, bottom-to-top, with optional native edits.

    Choices maps role to an asset ID from the pack. Originals remain in Assets/Downloaded.
    Uses the same native_compose bridge and its secure command allowlist.
    """
    try:
        selected = ANIME_SOURCES.pack_assets(pack_id, choices)
    except SourceError as exc:
        raise ToolError(str(exc)) from None
    blocks = await native_compose(output_name, [item["relative"] for item in selected],
                                  actions_json, width, height, preview, job_id)
    summary = json.loads(blocks[0].text)
    with ANIME_SOURCES._con() as con:
        for item in selected:
            con.execute("UPDATE assets SET used=used+1 WHERE id=?", (item["asset"]["id"],))
    summary["pack_id"] = pack_id
    summary["layers"] = [{"role":item["role"],"asset_id":item["asset"]["id"]} for item in selected]
    return compact(summary)


@mcp.tool()
def create_review_job(title: str, modes: list[str] | None=None) -> str:
    """Start a review job. Default to one result; supply multiple modes only when explicitly requested."""
    try:
        return compact(REVIEW_JOBS.create(title,modes))
    except ReviewError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool()
async def submit_review_result(job_id: str, number: int) -> str:
    """Render the active canvas into a numbered temporary review slot. The portal owner approves the permanent PNG."""
    try:
        destination = REVIEW_JOBS.pending(job_id,number)
    except ReviewError as exc:
        raise ToolError(str(exc)) from None
    clipped = await render_portal_png(destination,None,None)
    return compact({"job_id":job_id,"result":number,"status":"pending_approval",
                    "clipped_text":clipped,"review_url":PUBLIC_URL+":8443/?tab=review"})


def usage_start(job_id):
    if job_id is not None:
        try:
            validate_job(job_id)
        except EditingError as exc:
            raise ToolError(str(exc)) from None
    return time.perf_counter()


def record_edit_usage(job_id, operation, inputs, blocks, started, **details):
    try:
        EDIT_USAGE.record(job_id,operation,inputs,blocks,int((time.perf_counter()-started)*1000),**details)
    except (OSError, ValueError, sqlite3.Error):
        # Telemetry must never turn an already-applied edit into a retry.
        return

def native_content(result):
    # Return actual MCP content blocks, not a JSON string containing an image
    # placeholder. MCPServer forwards ImageContent to Claude/ChatGPT as an image.
    content = []
    for item in result.content:
        if isinstance(item, ImageContent) or getattr(item, "type", None) == "image":
            # Normalize very large previews to keep remote payloads manageable.
            try:
                raw = base64.b64decode(item.data, validate=True)
                if len(raw) > 5 * 1024 * 1024:
                    with Image.open(io.BytesIO(raw)) as img:
                        img = ImageOps.exif_transpose(img).convert("RGB")
                        img.thumbnail((1600, 1600))
                        out = io.BytesIO()
                        img.save(out, format="JPEG", quality=88, optimize=True)
                        content.append(ImageContent(type="image", data=base64.b64encode(out.getvalue()).decode("ascii"), mimeType="image/jpeg"))
                else:
                    content.append(ImageContent(type="image", data=item.data, mimeType=getattr(item, "mime_type", None) or item.mimeType))
            except Exception as exc:
                content.append(TextContent(type="text", text="Could not forward native image: " + str(exc)))
        elif getattr(item, "type", None) == "text":
            content.append(TextContent(type="text", text=item.text))
        else:
            content.append(TextContent(type="text", text=str(item)))
    if not content:
        content.append(TextContent(type="text", text=json.dumps(getattr(result, "structured_content", None) or {"ok": True}, ensure_ascii=False)))
    return content

@mcp.tool()
async def native_tool_catalog(job_id: str | None=None, detailed: bool=False,
                              tool_name: str | None=None) -> str:
    """List compact native tool names by default; request one full schema with tool_name and detailed=true."""
    started = usage_start(job_id)
    if tool_name is not None and (not isinstance(tool_name,str) or len(tool_name)>100):
        raise ToolError("Use one short native tool name.")
    try:
        result = await NATIVE.request("catalog")
    except NativeError as exc:
        raise ToolError(str(exc)) from None
    selected=[tool for tool in result.tools if tool_name is None or tool.name==tool_name]
    if tool_name is not None and not selected:
        raise ToolError("No native tool has that name.")
    if detailed:
        rows=[{"name":tool.name,"description":tool.description,"input_schema":tool.input_schema}
              for tool in selected]
    else:
        rows=[{"name":tool.name,"about":tool.description.splitlines()[0][:90]}
              for tool in selected]
    text=compact(rows)
    record_edit_usage(job_id,"native_tool_catalog",[detailed,tool_name],
                      [TextContent(type="text",text=text)],started)
    return text

@mcp.tool()
async def native_tool_call(tool_name: str, arguments_json: str="{}", job_id: str | None=None) -> list:
    """Run a native PhotoCraft tool, including command_batch with reviewed steps. Use native_export_to_portal to deliver a finished PNG."""
    started = usage_start(job_id)
    try:
        args = json.loads(arguments_json)
    except json.JSONDecodeError:
        raise ToolError("arguments_json must be valid JSON") from None
    if not isinstance(args, dict):
        raise ToolError("arguments_json must be a JSON object")
    try:
        result = await NATIVE.request("call", name=tool_name, arguments=args)
    except NativeError as exc:
        raise ToolError(str(exc)) from None
    if result.is_error:
        details = " ".join(item.text for item in result.content if item.type == "text")
        raise ToolError(details[:2000] or "PhotoCraft reported a native tool error.")
    blocks = native_content(result)
    record_edit_usage(job_id, "native:"+tool_name, [tool_name,arguments_json], blocks, started)
    return blocks


@mcp.tool()
async def native_command_help(query: str="", detailed: bool=False, limit: int=4,
                              job_id: str | None=None) -> str:
    """Find PhotoCraft commands by name or keyword. Ask for detailed=true on one specific command."""
    if len(query)>140 or not 1<=limit<=(4 if detailed else 12):
        raise ToolError("Use a short query and a limit of 1–12, or 1–4 for detailed help.")
    started = usage_start(job_id)
    try:
        answer = await NATIVE.request("command_help",arguments={
            "query":query, "detailed":detailed, "limit":limit,
        })
    except (NativeError, EditingError) as exc:
        raise ToolError(str(exc)) from None
    text = compact(answer)
    record_edit_usage(job_id,"native_command_help",[query,detailed,limit],[TextContent(type="text",text=text)],started)
    return text


@mcp.tool()
async def native_edit_batch(job_id: str, actions_json: str, detail: str="summary",
                             preview: str="none", known_preview_id: str | None=None,
                             expected_state_id: str | None=None) -> list:
    """Edit the open PhotoCraft canvas in one call. Pass JSON actions [{id,params}] or [{use,args}].

    Short forms: ["t",name,text,x,y,size,color,font?], ["r",name,x,y,w,h,fill,radius?],
    ["c",command,params?], or ["p",procedure,args].

    Procedures (optional keys in parentheses): title uses type.create args (style, shadow, stroke,
    gradient); glow_shape uses shape.create args (outer, inner); layer_treatment takes properties,
    transform, or shadow; mask_region takes select.rect args (mask, finish). Each optional object
    uses native command params. Raw actions and procedures may be mixed freely.

    Use [] for state alone, detail="layers" for IDs and names, or the raw doc_inspect for full
    state. Preview is none, layout (480 px), or detail (960 px). Pass the last preview ID to
    skip an unchanged image. Optionally pass the last state ID to guard the next edit.
    """
    started = usage_start(job_id)
    edges = {"none":0,"layout":480,"detail":960}
    if detail not in {"summary","layers"} or preview not in edges:
        raise ToolError("Choose summary or layers, and none, layout or detail for the preview.")
    inputs = [actions_json,detail,preview,known_preview_id,expected_state_id]
    try:
        steps = json.loads(compile_plan(actions_json))
        report, image = await NATIVE.request("compact_edit",arguments={
            "steps":steps,"detail":detail,"edge":edges[preview],"known_preview_id":known_preview_id,
            "expected_state_id":expected_state_id,
        })
    except (NativeError, EditingError) as exc:
        record_edit_usage(job_id,"native_edit_batch",inputs,[],started,failed=True)
        raise ToolError(str(exc)) from None
    blocks = [TextContent(type="text",text=compact(report))]
    if image is not None:
        blocks.append(image)
    pattern = fingerprint([(step["id"],sorted(step["params"])) for step in steps]) if steps else None
    record_edit_usage(job_id,"native_edit_batch",inputs,blocks,started,steps=report["completed"],
                      pattern=pattern,failed="failed_step" in report)
    return blocks


@mcp.tool()
async def native_compose(output_name: str, assets: list, actions_json: str="[]",
                         width: int=1280, height: int=720, preview: str="none",
                         job_id: str | None=None) -> list:
    """Create one editable project from Assets images and native edits in a single MCP call.

    Assets are bottom-to-top paths relative to Assets, such as
    "Downloaded/subject.png" or "Generated/job/background.png". An asset may
    instead be {"file":path,"scale":percent,"center":[x,y]}. The edit plan
    accepts full native actions or short t/r/c/p actions from native_edit_batch.
    Preview is none (default), layout (480 px), or detail (960 px).
    """
    started=usage_start(job_id)
    if not isinstance(assets,list) or not 1<=len(assets)<=24:
        raise ToolError("Supply 1 to 24 image layers in bottom-to-top order.")
    if type(width) is not int or type(height) is not int or not 1<=width<=4096 or not 1<=height<=4096:
        raise ToolError("Canvas sides must be 1 to 4096 pixels.")
    edges={"none":0,"layout":480,"detail":960}
    if preview not in edges:
        raise ToolError("Preview must be none, layout, or detail.")
    try:
        stem=output_stem(output_name)
        steps=json.loads(compile_plan(actions_json))
        if any(step["id"].startswith("file.") for step in steps):
            raise EditingError("Put image imports in assets; editing steps cannot use file commands.")
        NATIVE._authorize("command_batch",{"steps":steps})
    except (ValueError,EditingError,NativeError) as exc:
        raise ToolError(str(exc)) from None
    destination=PROJECTS/(stem+".pcraft")
    if os.path.isreserved(destination.name) or destination.exists():
        raise ToolError("Choose a regular unused project name.")

    imports=[]
    asset_root=ASSETS.resolve()
    for entry in assets:
        if isinstance(entry,str):
            entry={"file":entry}
        if not isinstance(entry,dict) or entry.keys()-{"file","scale","center"}:
            raise ToolError("Each asset must be a path or a file/scale/center object.")
        name=entry.get("file")
        if not isinstance(name,str) or "\\" in name or len(name)>500:
            raise ToolError("Use a forward-slash path beneath Assets.")
        parts=name.split("/")
        if not 1<=len(parts)<=5 or any(part in {"",".",".."} or
                                         not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,120}",part)
                                         for part in parts):
            raise ToolError("Use a simple relative image path beneath Assets.")
        asset=(ASSETS.joinpath(*parts)).resolve()
        if not asset.is_relative_to(asset_root) or asset.suffix.lower() not in IMAGE_EXTS or not asset.is_file():
            raise ToolError("An asset is missing or outside Assets.")
        params={"path":str(asset)}
        if "scale" in entry:
            scale=entry["scale"]
            if type(scale) not in {int,float} or not 1<=scale<=500:
                raise ToolError("Asset scale must be 1 to 500 percent.")
            params["scale"]=scale
        if "center" in entry:
            center=entry["center"]
            if not isinstance(center,list) or len(center)!=2 or any(type(value) is not int for value in center):
                raise ToolError("Asset center must contain two pixel coordinates.")
            params["center"]=center
        imports.append(params)

    with TemporaryDirectory(prefix=".native-compose-",dir=PROJECTS) as directory:
        current=None
        for index,params in enumerate(imports,1):
            prepared=Path(directory)/f"layer-{index}.pcraft"
            start=(["--new",json.dumps({"name":stem,"width":width,"height":height,"background":"transparent"})]
                   if current is None else [str(current)])
            try:
                await run_locked(["run",*start,"--cmd","file.placeEmbedded",
                                  "--params",json.dumps(params),"--out",str(prepared)])
            except RuntimeError:
                raise ToolError(f"PhotoCraft could not import asset {index}.") from None
            if not prepared.is_file() or not zipfile.is_zipfile(prepared):
                raise ToolError(f"PhotoCraft did not save import stage {index}.")
            current=prepared
        try:
            os.link(current,destination)
        except FileExistsError:
            raise ToolError("Choose a regular unused project name.") from None

    try:
        opened=await NATIVE.request("call",name="doc_open",arguments={"path":str(destination)})
        if opened.is_error:
            raise ToolError(f"Imported layers into {destination.name}; PhotoCraft did not open it.")
        report,image=await NATIVE.request("compact_edit",arguments={
            "steps":steps,"detail":"summary","edge":edges[preview],
            "known_preview_id":None,"expected_state_id":None,
        })
        if "failed_step" in report:
            raise ToolError(f"Imported layers into {destination.name}; edit step {report['failed_step']} failed: {report.get('error','Unknown error')}")
        saved=await NATIVE.request("call",name="doc_save",arguments={"path":str(destination)})
        if saved.is_error:
            raise ToolError(f"Imported layers into {destination.name}; PhotoCraft could not save the edits.")
    except (NativeError,EditingError) as exc:
        raise ToolError(f"Imported layers into {destination.name}; {exc}") from None
    answer={"project":destination.name,"imports":len(imports),"steps":report["completed"],
            "state":report["state"],"state_id":report["state_id"]}
    if "preview_id" in report:
        answer["preview_id"]=report["preview_id"]
    blocks=[TextContent(type="text",text=compact(answer))]
    if image is not None:
        blocks.append(image)
    record_edit_usage(job_id,"native_compose",[output_name,assets,actions_json,width,height,preview],
                      blocks,started,steps=report["completed"])
    return blocks


@mcp.tool()
def get_editing_job_stats(job_id: str) -> str:
    """Show measured MCP text bytes, image bytes and pixels, calls, and native steps for a job."""
    usage_start(job_id)
    if not EDIT_USAGE.path.is_file():
        return compact({"job":job_id,"calls":0})
    return compact(EDIT_USAGE.stats(job_id))


@mcp.tool()
async def native_export_to_portal(output_name: str, expected_width: int | None=None,
                                  expected_height: int | None=None,
                                  required_text_layers: list[str] | None=None,
                                  job_id: str | None=None) -> str:
    """Export the active PhotoCraft canvas as a full-resolution PNG in Finished thumbnails.

    Call before closing the document. Return the protected download URL to the user.
    Optionally pass both expected canvas dimensions and the names of required editable
    text layers. Blank exports are blocked and clipped visible text is flagged.
    An existing export with the same name is preserved.
    """
    started = usage_start(job_id)
    try:
        filename = output_stem(output_name) + ".png"
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    if os.path.isreserved(filename):
        raise ToolError("Use a regular Windows filename.")
    expected = None
    if expected_width is not None or expected_height is not None:
        if expected_width is None or expected_height is None or min(expected_width, expected_height) <= 0:
            raise ToolError("Supply both expected canvas dimensions as positive numbers.")
        expected = (expected_width, expected_height)
    destination = EXPORTS / filename
    clipped = await render_portal_png(destination,expected,required_text_layers)
    public = urllib.parse.urlsplit(PUBLIC_URL)
    url = f"{public.scheme}://{public.hostname}:8443/download/{urllib.parse.quote(filename)}"
    message = f"Saved {filename} in Finished thumbnails. Download after unlocking the portal: {url}"
    if clipped:
        message += "\nCheck clipped text layers: " + ", ".join(clipped)
    record_edit_usage(job_id,"native_export_to_portal",[output_name,expected,required_text_layers],
                      [TextContent(type="text",text=message)],started)
    return message


async def render_portal_png(destination,expected,required_text_layers):
    filename = destination.name
    if destination.exists():
        raise ToolError("That export name exists. Choose a new name.")
    with TemporaryDirectory(prefix=".portal-export-", dir=NATIVE.write_root) as directory:
        rendered = Path(directory) / "canvas.png"
        try:
            size, clipped = await NATIVE.request("checked_export", arguments={
                "path": str(rendered), "expected": expected, "required_text": required_text_layers or [],
            })
        except NativeError as exc:
            raise ToolError(str(exc)) from None
        try:
            with Image.open(rendered) as png:
                if png.format != "PNG" or png.size != size:
                    raise ValueError("The export did not match its canvas.")
                png.verify()
            with Image.open(rendered) as png:
                # A small composite catches blank frames and ignores otherwise
                # invisible RGB values stored in transparent pixels.
                png.thumbnail((256, 256))
                rgba = png.convert("RGBA")
                composite = Image.new("RGBA", rgba.size, "black")
                composite.alpha_composite(rgba)
                if all(low == high for low, high in composite.convert("RGB").getextrema()):
                    raise ToolError("The exported canvas appears blank. Add visible artwork before publishing.")
        except (OSError, ValueError):
            raise ToolError("PhotoCraft did not produce a readable PNG at the expected canvas size.") from None
        with TemporaryDirectory(prefix=".publish-", dir=destination.parent) as publishing:
            staged = Path(publishing) / filename
            shutil.copyfile(rendered, staged)
            try:
                os.link(staged, destination)
            except FileExistsError:
                raise ToolError("That export name exists. Choose a new name.") from None
    return clipped


@mcp.tool()
async def native_edit_stage(stage_id: str, plan_json: str) -> str:
    """Run isolated PhotoCraft edits and save a checkpoint. Use a unique stage ID.

    Plan JSON contains steps [{id, params}] and one optional starting point:
    document {name, width, height, background}, project (workspace path), or
    checkpoint (completed stage ID). Stages wait for one another and require
    the native session to have no manually opened documents. Repeating the same
    completed stage returns its checkpoint status without applying edits again.
    """
    if len(plan_json) > 65536:
        raise ToolError("The editing plan exceeds 64,000 characters.")
    try:
        plan = json.loads(plan_json)
        json.dumps(plan, allow_nan=False)
    except (ValueError, RecursionError):
        raise ToolError("plan_json must be valid JSON with finite numbers.") from None
    try:
        return json.dumps(await NATIVE.request("stage", name=stage_id, arguments=plan))
    except (NativeError, StageError) as exc:
        raise ToolError(str(exc)) from None


@mcp.tool()
def native_stage_status(stage_id: str) -> str:
    """Read a stage's checkpoint status without starting PhotoCraft."""
    try:
        return json.dumps(NATIVE.stages.status(stage_id))
    except StageError as exc:
        raise ToolError(str(exc)) from None

# Phase 2: the reference library is deliberately separate from Incoming and Assets.
ART_DNA = ArtDNA(ROOT / "ArtDNA" / "art_dna.db", REFERENCE)

@lru_cache(maxsize=64)
def cached_art_brief(store, stamp, mode, query, references, percent, similar, core_only):
    return store.brief(mode, query, references, percent, similar, core_only)


def art_brief(query, mode, reference_percent, reference_ids=(), similar_to=None, core_only=False):
    try:
        stamp = ART_DNA.db_path.stat()
    except FileNotFoundError:
        raise ArtDNAError("Art DNA has not been indexed on this PC.") from None
    return cached_art_brief(ART_DNA,(stamp.st_mtime_ns,stamp.st_size),mode,query,tuple(reference_ids),
                            reference_percent,similar_to,core_only)


@mcp.tool()
def get_editing_context(job_id: str, query: str="", mode: str="reference",
                        reference_percent: int | None=None, asset_names: list[str] | None=None,
                        known_context_id: str | None=None, core_only: bool=False) -> str:
    """Fetch Art DNA and optional workspace asset metadata once. Send a prior context ID to skip unchanged data."""
    started = usage_start(job_id)
    names = asset_names or []
    if len(names)>6:
        raise ToolError("Request metadata for at most six assets.")
    try:
        style = art_brief(query,mode,reference_percent,core_only=core_only)
        assets = []
        roots = {folder.resolve() for folder in (INCOMING,ASSETS,DOWNLOADED)}
        for name in names:
            path = source_path(name).resolve()
            if path.parent not in roots or path.suffix.lower() not in IMAGE_EXTS:
                raise EditingError("Use a raster image inside a workspace asset folder.")
            stat = path.stat()
            sidecar = path.with_suffix(path.suffix+".source.json")
            meta = sidecar.stat() if sidecar.is_file() and not sidecar.is_symlink() else None
            assets.append(cached_asset(str(path),(stat.st_size,stat.st_mtime_ns),
                                       (meta.st_size,meta.st_mtime_ns) if meta else None))
        context = {"style":style,"assets":assets}
        context_id = fingerprint(context)
        result = {"context_id":context_id,"unchanged":True} if context_id == known_context_id else {
            "context_id":context_id,**context,
        }
    except (ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc,(ArtDNAError,EditingError)) else "An asset is missing or unreadable."
        raise ToolError(message) from None
    text = compact(result)
    record_edit_usage(job_id,"get_editing_context",[query,mode,reference_percent,names,known_context_id,core_only],
                      [TextContent(type="text",text=text)],started)
    return text


@mcp.tool()
def get_art_dna_brief(query: str="", mode: str="reference", reference_percent: int | None=None,
                      reference_ids: list[str] | None=None, similar_to: str | None=None,
                      core_only: bool=False) -> str:
    """Get a compact style guide and recent explicit human feedback before planning.
    Modes: reference, free_will, hybrid.

    Returns text only, with up to three reference IDs. Request images separately
    only if a visual decision needs them. Hybrid accepts an optional reference
    percentage. Use similar_to with a reference ID for local visual similarity.
    Approved and passed review results plus any short owner notes are included as feedback cues.
    Set core_only to omit individual references and analysis records in any mode.
    """
    try:
        return compact(art_brief(query, mode, reference_percent, reference_ids or [], similar_to, core_only))
    except ArtDNAError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool()
def art_dna_status() -> str:
    """Show indexed and annotated reference counts and the active style version."""
    try:
        return compact(ART_DNA.status())
    except ArtDNAError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool()
def get_art_dna_reference_images(image_ids: list[str] | None=None, limit: int=2) -> list:
    """Show up to two small reference images. Without IDs, pick references awaiting notes."""
    try:
        images = ART_DNA.batch(image_ids or [], limit)
    except ArtDNAError as exc:
        raise ToolError(str(exc)) from None
    if not images:
        return [TextContent(type="text", text="No references are waiting for visual notes.")]
    instructions = (
        "If recording analysis, return one JSON object per ID with topic, composition, typography, "
        "color_lighting, editing, attention, takeaway, and tags (1–10 short strings). Keep each "
        "observation to one short sentence. Describe visible evidence and uncertainties. "
        "Do not infer exact fonts, layer settings, licensing or click performance from raster art."
    )
    blocks = [TextContent(type="text", text=instructions)]
    for image_id, name, image in images:
        blocks.extend([
            TextContent(type="text", text=compact({"id": image_id, "file": name})),
            ImageContent(type="image", mimeType="image/jpeg", data=base64.b64encode(image).decode("ascii")),
        ])
    return blocks


@mcp.tool()
def record_art_dna_observations(analyst: str, observations_json: str) -> str:
    """Store structured visual notes for indexed references from a connected vision agent.

    Supply a JSON array and a short model/analyst label. Repeated identical
    observations are cached. This does not update the active style profile.
    """
    if len(observations_json) > 64000:
        raise ToolError("Keep observations under 64,000 characters.")
    if analyst.strip().casefold() == "codex-reference-review":
        raise ToolError("Use the name of your connected analyst or model.")
    try:
        records = json.loads(observations_json)
    except (ValueError, RecursionError):
        raise ToolError("observations_json must be a JSON array.") from None
    try:
        return compact(ART_DNA.record(analyst, records))
    except ArtDNAError as exc:
        raise ToolError(str(exc)) from None


def build_reference_index():
    return create_reference_index(REFERENCE,INDEX_FILE)

def load_reference_index():
    return read_reference_index(INDEX_FILE)

@mcp.tool()
def scan_reference_library() -> str:
    """Index image files in ReferenceLibrary only. Does not analyze image contents or include Incoming/Assets."""
    payload=build_reference_index()
    total=sum(x["bytes"] for x in payload["images"])
    return json.dumps({
        "indexed_images":payload["count"],
        "total_bytes":total,
        "index_file":"ReferenceAnalysis/image_index.json",
        "next_step":"Call get_reference_image_batch in small batches so you can visually inspect the images."
    },indent=2)

@mcp.tool()
def get_reference_image_batch(start_index: int=0, limit: int=2) -> list:
    """Return actual image previews from the indexed ReferenceLibrary for Claude to visually inspect. Use small batches; maximum 4."""
    if start_index < 0: raise ValueError("start_index cannot be negative.")
    if not (1 <= limit <= MAX_BATCH): raise ValueError(f"limit must be between 1 and {MAX_BATCH}.")
    payload=load_reference_index()
    images=payload["images"]
    selected=images[start_index:start_index+limit]
    if not selected: return [TextContent(type="text",text="No images at that index. The scan contains "+str(len(images))+" images.")]
    content=[TextContent(type="text",text=f"Reference image batch: indexes {start_index} to {start_index+len(selected)-1} of {len(images)}. Inspect each image and note recurring visual patterns.")]
    for item in selected:
        rel=Path(item["file"])
        path=(REFERENCE/rel).resolve()
        if REFERENCE.resolve() not in path.parents or not path.is_file():
            content.append(TextContent(type="text",text=f"Skipped missing or invalid file: {item['file']}"))
            continue
        try:
            with Image.open(path) as im:
                im=ImageOps.exif_transpose(im)
                im.thumbnail((MAX_PREVIEW_EDGE,MAX_PREVIEW_EDGE))
                if im.mode not in ("RGB","L"): im=im.convert("RGB")
                elif im.mode=="L": im=im.convert("RGB")
                buf=io.BytesIO()
                im.save(buf,format="JPEG",quality=82,optimize=True)
                data=base64.b64encode(buf.getvalue()).decode("ascii")
            content.append(TextContent(type="text",text=f"Image {item['id']}: {item['file']} ({item['bytes']} bytes)"))
            content.append(ImageContent(type="image",data=data,mimeType="image/jpeg"))
        except Exception as e:
            content.append(TextContent(type="text",text=f"Could not preview {item['file']}: {type(e).__name__}: {e}"))
    content.append(TextContent(type="text",text=f"Next batch starts at index {start_index+len(selected)}."))
    return content

@mcp.tool()
def save_reference_analysis(content: str, append: bool=False) -> str:
    """Save Claude's concise visual style findings locally in ReferenceAnalysis/style_guide.md. This is separate from Claude's built-in memory."""
    if not content.strip(): raise ValueError("Analysis content cannot be empty.")
    if len(content) > 50000: raise ValueError("Keep the stored style guide under 50,000 characters.")
    if append and GUIDE_FILE.exists():
        existing=GUIDE_FILE.read_text(encoding="utf-8")
        GUIDE_FILE.write_text(existing.rstrip()+"\n\n"+content.strip()+"\n",encoding="utf-8")
    else:
        GUIDE_FILE.write_text("# Reference image style guide\n\n"+content.strip()+"\n",encoding="utf-8")
    return f"Saved {GUIDE_FILE.relative_to(ROOT).as_posix()} ({GUIDE_FILE.stat().st_size} bytes)."

@mcp.tool()
def get_reference_analysis() -> str:
    """Retrieve the saved concise style guide from the one-time reference collection analysis."""
    if not GUIDE_FILE.exists(): return "No saved style guide yet. Analyze the reference images and call save_reference_analysis."
    return GUIDE_FILE.read_text(encoding="utf-8")[:50000]


@mcp.tool()
async def download_image(download_url: str, filename: str, source_page: str="", license_name: str="Unknown",
                         cutout: bool=False, min_subject_edge: int=320,
                         background_color: str | None=None, segmentation_model: str="general") -> str:
    """Download a direct public raster URL from any reachable host into Assets/Downloaded.

    Set cutout to make a transparent PNG with a local segmentation model when
    needed. Choose general or anime. A known plain backdrop can be keyed first.
    The models download once into the local project folder. Source and license
    metadata are retained. Private networks and non-image responses are refused.
    """
    if not 0 <= min_subject_edge <= 4096:
        raise ToolError("Minimum subject edge must be between 0 and 4096 pixels.")
    if background_color is not None and not cutout:
        raise ToolError("Use cutout=true when supplying a background color.")
    try:
        async with ASSET_DOWNLOAD_SLOTS:
            result = await asyncio.to_thread(image_mcp.download, download_url, DOWNLOADED,
                                             safe_download_name(filename), source_page, license_name,
                                             cutout, min_subject_edge, background_color, segmentation_model)
        return compact(result)
    except image_mcp.ImageSourceError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool()
async def load_image_in_photocraft(image_filename: str, output_name: str,
                                  project_filename: str | None=None, width: int=1280, height: int=720,
                                  scale: float | None=None, center_x: int | None=None,
                                  center_y: int | None=None, job_id: str | None=None) -> str:
    """Open a layered PhotoCraft project with a downloaded or uploaded raster as a Smart Object.

    Optionally start from a saved PSD, PSB, or PCRAFT in Projects. Creates a new
    editable copy and opens it in the native session. A scale of 100 uses the
    original image size. Omit scale for an automatic fit.
    """
    started=usage_start(job_id)
    if not (1 <= width <= 4096 and 1 <= height <= 4096 and (scale is None or 1 <= scale <= 500)):
        raise ToolError("Canvas sides must be 1–4096 pixels and scale must be 1–500 percent.")
    if (center_x is None) != (center_y is None):
        raise ToolError("Supply both center_x and center_y.")
    try:
        image_name = safe_name(image_filename)
        stem = output_stem(output_name)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    allowed = (INCOMING, ASSETS, DOWNLOADED)
    asset = next((directory/image_name for directory in allowed if (directory/image_name).is_file()
                  and (directory/image_name).resolve().parent == directory.resolve()), None)
    if not asset or asset.suffix.lower() not in IMAGE_EXTS:
        raise ToolError("Use an image in Incoming, Assets, or Assets/Downloaded.")
    if project_filename is not None:
        try:
            supplied = safe_name(project_filename)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        source = (PROJECTS/supplied).resolve()
        if source.parent != PROJECTS.resolve() or source.suffix.lower() not in {".psd",".psb",".pcraft"} or not source.is_file():
            raise ToolError("Use a saved PSD, PSB or PCRAFT in Projects.")
        start = [str(source)]
    else:
        start = ["--new", json.dumps({"name":stem, "width":width, "height":height, "background":"transparent"})]
    destination = PROJECTS/(stem+".pcraft")
    if os.path.isreserved(destination.name) or destination.exists():
        raise ToolError("Choose a regular unused project name.")
    params = {"path":str(asset)}
    if scale is not None:
        params["scale"] = scale
    if center_x is not None:
        params["center"] = [center_x, center_y]
    with TemporaryDirectory(prefix=".asset-stage-", dir=PROJECTS) as directory:
        prepared = Path(directory)/"layered.pcraft"
        try:
            await run_locked(["run", *start, "--cmd", "file.placeEmbedded",
                              "--params", json.dumps(params), "--out", str(prepared)])
        except RuntimeError:
            raise ToolError("PhotoCraft could not place this raster as an embedded layer.") from None
        if not prepared.is_file() or not zipfile.is_zipfile(prepared):
            raise ToolError("PhotoCraft did not produce a readable editable project.")
        try:
            os.link(prepared, destination)
        except FileExistsError:
            raise ToolError("That project name already exists.") from None
    try:
        result = await NATIVE.request("call", name="doc_open", arguments={"path":str(destination)})
    except NativeError:
        raise ToolError(f"Saved {destination.name}. Reopen it with doc_open after PhotoCraft reconnects.") from None
    if result.is_error:
        raise ToolError(f"Saved {destination.name}, but the desktop could not open it. Use doc_open to retry.")
    reply=compact({"project":destination.name,"asset":image_name,
                   "opened":json.loads(result.content[0].text)})
    record_edit_usage(job_id,"load_image_in_photocraft",
                      [image_filename,output_name,project_filename,width,height,scale,center_x,center_y],
                      [TextContent(type="text",text=reply)],started)
    return reply


@mcp.tool()
async def set_layer_properties(project_filename: str, layer_id: int, output_name: str,
                               visible: bool=None, opacity: float=None, blend: str=None,
                               locked: bool=None) -> str:
    """Change visibility, opacity, blend mode or lock state for one PhotoCraft layer and save a new PSD."""
    src=project_path(project_filename); stem=output_stem(output_name); psd=PROJECTS/(stem+".psd")
    params={"layer":int(layer_id)}
    if visible is not None: params["visible"]=bool(visible)
    if opacity is not None:
        if not (0 <= opacity <= 1): raise ValueError("opacity must be between 0 and 1.")
        params["opacity"]=float(opacity)
    if blend is not None: params["blend"]=blend
    if locked is not None: params["locked"]=bool(locked)
    if len(params)==1: raise ValueError("Provide at least one property to change.")
    msg=await run_locked(["run",str(src),"--cmd","layer.setProps","--params",json.dumps(params),"--out",str(psd)])
    if not psd.exists(): raise RuntimeError("Layer update did not create a PSD. "+msg)
    await export_png(psd,stem)
    return f"Updated layer {layer_id}. Saved Projects/{psd.name} and Exports/{stem}.png."

@mcp.tool()
async def transform_layer(project_filename: str, layer_id: int, output_name: str,
                          scale_x: float=None, scale_y: float=None, rotate: float=None,
                          dx: int=None, dy: int=None) -> str:
    """Resize, rotate or offset a PhotoCraft layer and save a new PSD plus PNG preview."""
    src=project_path(project_filename); stem=output_stem(output_name); psd=PROJECTS/(stem+".psd")
    params={"layer":int(layer_id)}
    if scale_x is not None:
        if not (1 <= scale_x <= 500): raise ValueError("scale_x must be between 1 and 500 percent.")
        params["scaleX"]=float(scale_x)
    if scale_y is not None:
        if not (1 <= scale_y <= 500): raise ValueError("scale_y must be between 1 and 500 percent.")
        params["scaleY"]=float(scale_y)
    if rotate is not None: params["rotate"]=float(rotate)
    if dx is not None: params["dx"]=int(dx)
    if dy is not None: params["dy"]=int(dy)
    if len(params)==1: raise ValueError("Provide at least one transform.")
    msg=await run_locked(["run",str(src),"--cmd","edit.transform","--params",json.dumps(params),"--out",str(psd)])
    if not psd.exists(): raise RuntimeError("Transform did not create a PSD. "+msg)
    await export_png(psd,stem)
    return f"Transformed layer {layer_id}. Saved Projects/{psd.name} and Exports/{stem}.png."

# OAuth 2.1-style authorization-code + PKCE, with persistent SQLite storage.
# This self-hosted authorization server uses the existing portal password to approve connections.
PUBLIC_URL=NETWORK["public_url"]
DB_PATH=ROOT/"oauth.sqlite3"
AUTH_PASSWORD=SECRETS["portal_password"]
OAUTH_SCOPE="photocraft"

def db():
    con=sqlite3.connect(str(DB_PATH),timeout=10)
    con.row_factory=sqlite3.Row
    return con

with db() as con:
    con.execute("CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, redirect_uris TEXT NOT NULL, created_at INTEGER NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS oauth_requests (request_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, state TEXT, challenge TEXT NOT NULL, scope TEXT, expires INTEGER NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS oauth_codes (code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, scope TEXT, expires INTEGER NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS oauth_tokens (token_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT, expires INTEGER NOT NULL)")

def hash_token(value): return hashlib.sha256(value.encode('utf-8')).hexdigest()
def oauth_error(message,status=400): return JSONResponse({"error":message},status_code=status,headers={"Cache-Control":"no-store"})
def no_store(response):
    response.headers['Cache-Control']='no-store'
    response.headers['Pragma']='no-cache'
    response.headers['X-Content-Type-Options']='nosniff'
    return response

def valid_redirect(uri):
    try:
        u=urllib.parse.urlsplit(uri)
        if u.fragment or u.username or u.password: return False
        # Gemini/Google Connected Apps redirects. No arbitrary external redirects.
        if u.scheme=='https' and u.hostname in ('oauth-redirect.googleusercontent.com','oauth-redirect-sandbox.googleusercontent.com','oauth-redirect-test.googleusercontent.com'):
            return u.path.startswith('/r/') or u.path.startswith('/a/')
        # ChatGPT's documented OAuth callbacks. Exact host and restricted paths only.
        if u.scheme=='https' and u.hostname=='chatgpt.com' and u.port is None:
            return u.path=='/connector_platform_oauth_redirect' or (u.path.startswith('/connector/oauth/') and len(u.path)>len('/connector/oauth/'))
        # Claude OAuth callbacks: only Anthropic-owned Claude HTTPS hosts.
        if u.scheme=='https' and u.hostname in ('claude.ai','www.claude.ai','claude.com','www.claude.com') and u.port is None:
            return bool(u.path) and (('oauth' in u.path.lower()) or ('callback' in u.path.lower()) or ('redirect' in u.path.lower()))
        # Local loopback redirects for desktop MCP clients.
        if u.scheme=='http' and u.hostname in ('127.0.0.1','localhost','[::1]') and u.port is not None:
            return True
    except ValueError: pass
    return False

def metadata(request):
    return no_store(JSONResponse({
        'issuer':PUBLIC_URL,
        'authorization_endpoint':PUBLIC_URL+'/authorize',
        'token_endpoint':PUBLIC_URL+'/token',
        'registration_endpoint':PUBLIC_URL+'/register',
        'revocation_endpoint':PUBLIC_URL+'/revoke',
        'response_types_supported':['code'],
        'grant_types_supported':['authorization_code','refresh_token'],
        'code_challenge_methods_supported':['S256'],
        'token_endpoint_auth_methods_supported':['none'],
        'scopes_supported':[OAUTH_SCOPE],
        'client_id_metadata_document_supported':False
    }))

def protected_resource_metadata(request):
    return no_store(JSONResponse({'resource':PUBLIC_URL+'/mcp','authorization_servers':[PUBLIC_URL],
                                  'scopes_supported':[OAUTH_SCOPE]}))

async def register(request):
    try:
        data=await request.json()
        uris=data.get('redirect_uris',[])
        if not isinstance(uris,list) or not uris or len(uris)>10 or any(not isinstance(u,str) or len(u)>2048 or not valid_redirect(u) for u in uris):
            print('OAuth DCR rejected redirect hosts/paths:', [(urllib.parse.urlsplit(u).hostname, urllib.parse.urlsplit(u).path) for u in uris if isinstance(u,str)], flush=True)
            return oauth_error('invalid_redirect_uri')
        methods=data.get('token_endpoint_auth_method','none')
        if methods!='none': return oauth_error('invalid_client_metadata')
        grants=data.get('grant_types',['authorization_code','refresh_token'])
        if not isinstance(grants,list) or 'authorization_code' not in grants or any(g not in ('authorization_code','refresh_token') for g in grants):
            return oauth_error('invalid_client_metadata')
        responses=data.get('response_types',['code'])
        if not isinstance(responses,list) or responses!=['code']:
            return oauth_error('invalid_client_metadata')
        client_id=secrets.token_urlsafe(24)
        with db() as con: con.execute('INSERT INTO oauth_clients VALUES (?,?,?)',(client_id,json.dumps(uris),int(time.time())))
        return no_store(JSONResponse({'client_id':client_id,'client_id_issued_at':int(time.time()),
                'redirect_uris':uris,'token_endpoint_auth_method':'none',
                'grant_types':['authorization_code','refresh_token'],'response_types':['code']},status_code=201))
    except Exception: return oauth_error('invalid_client_metadata')

def auth_form(req_id,client_id,error=''):
    return no_store(HTMLResponse('''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>PhotoCraft — authorize</title><style>body{background:#111827;color:#f3f4f6;font:16px system-ui;max-width:450px;margin:12vh auto;padding:24px}main{background:#1f2937;padding:28px;border-radius:14px}input,button{box-sizing:border-box;width:100%;padding:13px;margin-top:12px;border-radius:8px}button{background:#2563eb;border:0;color:white;font-weight:bold;cursor:pointer}input{border:1px solid #6b7280;background:#111827;color:white}small{color:#aab2c0}</style></head><body><main><h2>Connect to PhotoCraft</h2><p>Allow this app to use your PhotoCraft thumbnail tools and files on your PC.</p><small>Client: '''+html.escape(client_id)+'''</small><p style="color:#fca5a5">'''+html.escape(error)+'''</p><form method="post" action="/authorize"><input type="hidden" name="request_id" value="'''+html.escape(req_id,quote=True)+'''"><label>Enter your existing upload-portal password</label><input name="password" type="password" required autofocus><button type="submit" name="decision" value="approve">Authorize connection</button><button type="submit" name="decision" value="deny" style="background:#374151">Cancel</button></form></main></body></html>'''))

async def authorize_get(request):
    q=request.query_params
    client_id=q.get('client_id','')
    redirect_uri=q.get('redirect_uri','')
    challenge=q.get('code_challenge','')
    if q.get('response_type')!='code' or q.get('code_challenge_method')!='S256' or not re.fullmatch(r'[A-Za-z0-9_\-]{43,128}',challenge):
        return oauth_error('invalid_request')
    with db() as con: row=con.execute('SELECT redirect_uris FROM oauth_clients WHERE client_id=?',(client_id,)).fetchone()
    if row is None or redirect_uri not in json.loads(row['redirect_uris']): return oauth_error('invalid_client')
    scope=q.get('scope',OAUTH_SCOPE)
    if scope not in ('',OAUTH_SCOPE): return oauth_error('invalid_scope')
    rid=secrets.token_urlsafe(32)
    with db() as con:
        con.execute('DELETE FROM oauth_requests WHERE expires<?',(int(time.time()),))
        con.execute('INSERT INTO oauth_requests VALUES (?,?,?,?,?,?,?)',(rid,client_id,redirect_uri,q.get('state',''),challenge,OAUTH_SCOPE,int(time.time())+600))
    return auth_form(rid,client_id)

async def authorize_post(request):
    form=await request.form()
    rid=str(form.get('request_id',''))
    with db() as con: row=con.execute('SELECT * FROM oauth_requests WHERE request_id=? AND expires>?',(rid,int(time.time()))).fetchone()
    if row is None: return no_store(HTMLResponse('Authorization request expired. Return to your app and try again.',status_code=400))
    if form.get('decision')=='deny':
        with db() as con: con.execute('DELETE FROM oauth_requests WHERE request_id=?',(rid,))
        args={'error':'access_denied'}
    elif hmac.compare_digest(str(form.get('password','')),AUTH_PASSWORD):
        code=secrets.token_urlsafe(48)
        with db() as con:
            deleted=con.execute('DELETE FROM oauth_requests WHERE request_id=?',(rid,)).rowcount
            if deleted!=1: return oauth_error('invalid_request')
            con.execute('INSERT INTO oauth_codes VALUES (?,?,?,?,?,?)',(hash_token(code),row['client_id'],row['redirect_uri'],row['challenge'],row['scope'],int(time.time())+120))
        args={'code':code}
    else: return auth_form(rid,row['client_id'],'Incorrect password. Try again.')
    if row['state']: args['state']=row['state']
    separator='&' if '?' in row['redirect_uri'] else '?'
    return no_store(RedirectResponse(row['redirect_uri']+separator+urllib.parse.urlencode(args),status_code=303))

def token_response(client_id,scope,include_refresh=True,refresh=None):
    now=int(time.time())
    access=secrets.token_urlsafe(48)
    if include_refresh and refresh is None: refresh=secrets.token_urlsafe(48)
    with db() as con:
        con.execute('INSERT INTO oauth_tokens VALUES (?,?,?,?,?)',(hash_token(access),client_id,'access',scope,now+3600))
        if include_refresh and refresh:
            con.execute('INSERT OR REPLACE INTO oauth_tokens VALUES (?,?,?,?,?)',(hash_token(refresh),client_id,'refresh',scope,now+90*86400))
    result={'access_token':access,'token_type':'Bearer','expires_in':3600,'scope':scope}
    if include_refresh: result['refresh_token']=refresh
    return no_store(JSONResponse(result))

async def token_endpoint(request):
    form=await request.form()
    grant=form.get('grant_type')
    client_id=str(form.get('client_id',''))
    with db() as con: client=con.execute('SELECT client_id FROM oauth_clients WHERE client_id=?',(client_id,)).fetchone()
    if client is None: return oauth_error('invalid_client',401)
    if grant=='authorization_code':
        code=str(form.get('code',''))
        verifier=str(form.get('code_verifier',''))
        if not re.fullmatch(r'[A-Za-z0-9._~\-]{43,128}',verifier): return oauth_error('invalid_grant')
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        with db() as con:
            row=con.execute('SELECT * FROM oauth_codes WHERE code_hash=?',(hash_token(code),)).fetchone()
            if row is None or row['expires']<time.time() or row['client_id']!=client_id or row['redirect_uri']!=form.get('redirect_uri') or not hmac.compare_digest(row['challenge'],challenge):
                return oauth_error('invalid_grant')
            if con.execute('DELETE FROM oauth_codes WHERE code_hash=?',(hash_token(code),)).rowcount!=1: return oauth_error('invalid_grant')
        return token_response(client_id,row['scope'])
    if grant=='refresh_token':
        refresh=str(form.get('refresh_token',''))
        with db() as con:
            row=con.execute('SELECT * FROM oauth_tokens WHERE token_hash=? AND kind=?',(hash_token(refresh),'refresh')).fetchone()
        if row is None or row['expires']<time.time() or row['client_id']!=client_id: return oauth_error('invalid_grant')
        return token_response(client_id,row['scope'],refresh=refresh)
    return oauth_error('unsupported_grant_type')

async def revoke(request):
    form=await request.form()
    client_id=str(form.get('client_id',''))
    with db() as con:
        row=con.execute('SELECT 1 FROM oauth_clients WHERE client_id=?',(client_id,)).fetchone()
        if row is None: return oauth_error('invalid_client',401)
        con.execute('DELETE FROM oauth_tokens WHERE token_hash=? AND client_id=?',(hash_token(str(form.get('token',''))),client_id))
    return no_store(Response(status_code=200))

class OAuthGate:
    def __init__(self, app, legacy_token):
        self.app=app; self.legacy_token=legacy_token.encode()
    async def __call__(self, scope, receive, send):
        if scope['type']=='http' and scope.get('path','').rstrip('/')=='/mcp':
            headers=dict(scope.get('headers',[])); auth=headers.get(b'authorization',b'').strip()
            supplied=auth[7:].strip() if auth.lower().startswith(b'bearer ') else auth
            authorized=hmac.compare_digest(supplied,self.legacy_token)
            if not authorized and auth.lower().startswith(b'bearer '):
                try:
                    with db() as con: row=con.execute('SELECT 1 FROM oauth_tokens WHERE token_hash=? AND kind=? AND expires>?',(hash_token(supplied.decode('ascii')),'access',int(time.time()))).fetchone()
                    authorized=row is not None
                except (UnicodeDecodeError,sqlite3.Error): pass
            if not authorized:
                body=b'{"error":"unauthorized"}'
                await send({'type':'http.response.start','status':401,'headers':[(b'content-type',b'application/json'),(b'content-length',str(len(body)).encode()),(b'www-authenticate',('Bearer resource_metadata="'+PUBLIC_URL+'/.well-known/oauth-protected-resource/mcp"').encode())]})
                await send({'type':'http.response.body','body':body}); return
        await self.app(scope,receive,send)

async def health(request): return JSONResponse({"ok":True,"service":"PhotoCraft MCP"})
@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        try:
            yield
        finally:
            await NATIVE.shutdown()

inner=mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=NETWORK["allowed_hosts"],
        allowed_origins=NETWORK["allowed_origins"],
    )
)
inner.router.routes.append(Route("/health",health,methods=["GET"]))
app=Starlette(routes=[
    Route('/.well-known/oauth-authorization-server',metadata,methods=['GET']),
    Route('/.well-known/openid-configuration',metadata,methods=['GET']),
    Route('/.well-known/oauth-protected-resource',protected_resource_metadata,methods=['GET']),
    Route('/.well-known/oauth-protected-resource/mcp',protected_resource_metadata,methods=['GET']),
    Route('/register',register,methods=['POST']),
    Route('/authorize',authorize_get,methods=['GET']),
    Route('/authorize',authorize_post,methods=['POST']),
    Route('/token',token_endpoint,methods=['POST']),
    Route('/revoke',revoke,methods=['POST']),
    Mount('/',app=inner)
],lifespan=lifespan)
app=OAuthGate(app,TOKEN)

if __name__=="__main__":
    uvicorn.run(app,host="127.0.0.1",port=8000,log_level="info")
