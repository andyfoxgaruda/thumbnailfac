"""Gateway regression tests. Set PHOTOCRAFT_RUN_LIVE=1 for installed-app tests."""

import asyncio
import base64
import hashlib
import importlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase, skipUnless
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client import stdio
from mcp.client.stdio import stdio_client
from PIL import Image, ImageChops, ImageDraw
from starlette.testclient import TestClient

from photocraft_gateway import NativeError, NativeGateway
from editing_efficiency import compile_plan


def isolated_server(directory):
    """Load the real HTTP application with synthetic credentials and an isolated DB."""
    read_text = Path.read_text
    connect = sqlite3.connect
    database = directory / "oauth.sqlite3"
    credentials = {"mcp_token": "test-gateway-only", "portal_password": "test-portal-only"}

    class TestConnection(sqlite3.Connection):
        def __exit__(self, *args):
            try:
                return super().__exit__(*args)
            finally:
                self.close()

    def read_test_credentials(path, *args, **kwargs):
        if path.name == "secrets.json":
            return json.dumps(credentials)
        return read_text(path, *args, **kwargs)

    def test_connection(path, **kwargs):
        return connect(database, factory=TestConnection, **kwargs)

    def load(name):
        return importlib.reload(sys.modules[name]) if name in sys.modules else importlib.import_module(name)

    with patch.object(Path, "read_text", read_test_credentials):
        portal = load("portal")
        with patch.object(sqlite3, "connect", test_connection):
            server = load("server")

    def db():
        con = test_connection(database, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    server.db = db
    return server, portal


class GatewayPolicyTests(IsolatedAsyncioTestCase):
    def test_short_edit_actions_preserve_native_commands(self):
        steps = json.loads(compile_plan(json.dumps([
            ["r", "Panel", 10, 12, 70, 30, "#B04020", 8],
            ["t", "Headline", "NIGHT SIGNAL", 20, 60, 28, "#FFFFFF", "Impact"],
            ["c", "layer.setProps", {"layer": 2, "opacity": 80}],
        ])))
        self.assertEqual([step["id"] for step in steps],
                         ["shape.create", "type.create", "layer.setProps"])
        self.assertEqual(steps[0]["params"]["radii"], 8)
        self.assertEqual(steps[1]["params"]["font"], "Impact")

    async def test_denied_operations_never_start_a_native_process(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "Projects"
            projects.mkdir()
            gateway = NativeGateway("not launched", (projects,), projects)
            for name, args in (
                ("doc_open", {"path": str(root / "outside.png")}),
                ("doc_save", {"path": "../outside.psd"}),
                ("doc_save", {}),
                ("doc_export", {"path": "image.png:stream"}),
                ("command_run", {"id": "file.scripts.browse"}),
                ("command_batch", {"steps": [{"id": "type.create"}, {"id": "file.scripts.browse"}]}),
                ("command_run", {"id": "file.placeEmbedded", "params": {"path": "../outside.png"}}),
                ("control_call", {"method": "ui.dialog.open"}),
                ("ui_menu_invoke", {"id": "file.automate.createDroplet"}),
                ("unknown", {}),
            ):
                with self.subTest(name=name, args=args):
                    with self.assertRaises(NativeError):
                        await gateway.request("call", name=name, arguments=args)
                    self.assertIsNone(gateway._worker)

    async def test_request_that_times_out_in_queue_is_skipped(self):
        class PausedGateway(NativeGateway):
            async def _serve(self):
                await release.wait()
                item = self._queue.get_nowait()
                self.skipped = item.future.cancelled()

        release = asyncio.Event()
        gateway = PausedGateway("not launched", (), Path.cwd(), timeout=0.05)
        with self.assertRaisesRegex(NativeError, "before dispatch"):
            await gateway.request("call", name="doc_new", arguments={})
        release.set()
        await gateway._worker
        self.assertTrue(gateway.skipped)
        await gateway.shutdown()


@skipUnless(os.environ.get("PHOTOCRAFT_RUN_LIVE") == "1", "set PHOTOCRAFT_RUN_LIVE=1")
class LiveGatewayTests(TestCase):
    def setUp(self):
        sandbox = self.enterContext(TemporaryDirectory(prefix="photocraft-mcp-test-"))
        self.workspace = Path(sandbox)
        self.server, self.portal = isolated_server(self.workspace)
        self.projects = self.workspace / "Projects"
        self.assets = self.workspace / "Assets"
        self.exports = self.workspace / "Exports"
        self.projects.mkdir()
        self.assets.mkdir()
        self.exports.mkdir()
        self.enterContext(patch.multiple(
            self.server, PROJECTS=self.projects, ASSETS=self.assets, EXPORTS=self.exports,
        ))
        self.sequence = 0
        self.headers = {
            "Authorization": "Bearer " + self.server.TOKEN,
            "Accept": "application/json, text/event-stream",
        }

    def connect(self, mode):
        self.gateway = NativeGateway(
            self.server.CLI, (self.projects, self.assets), self.projects, mode=mode, timeout=65,
        )
        self.enterContext(patch.object(self.server, "NATIVE", self.gateway))
        self.client = self.enterContext(TestClient(self.server.app, base_url=self.server.PUBLIC_URL))
        initialized = self.rpc("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "photocraft-integration", "version": "1"},
        })
        self.assertIn("protocolVersion", initialized)

    def rpc(self, method, params=None):
        self.sequence += 1
        response = self.client.post("/mcp", headers=self.headers, json={
            "jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params or {},
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn("error", body)
        return body["result"]

    def tool(self, name, args=None, *, error=None):
        result = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        if error:
            self.assertTrue(result["isError"])
            self.assertIn(error, result["content"][0]["text"])
        else:
            self.assertFalse(result.get("isError"), result.get("content"))
        return result["content"]

    def native(self, name, /, *, error=None, **args):
        return self.tool("native_tool_call", {"tool_name": name, "arguments_json": json.dumps(args)}, error=error)

    def native_json(self, name, /, **args):
        return json.loads(self.native(name, **args)[0]["text"])

    def native_image(self, name, /, **args):
        block = next(block for block in self.native(name, **args) if block["type"] == "image")
        self.assertEqual(block["mimeType"], "image/png")
        data = base64.b64decode(block["data"], validate=True)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.format, "PNG")
            return image.convert("RGB")

    def capture(self, name, image):
        if output := os.environ.get("PHOTOCRAFT_TEST_ARTIFACTS"):
            target = Path(output)
            target.mkdir(parents=True, exist_ok=True)
            image.save(target / name)

    def test_headless_http_session_and_preview(self):
        self.connect("headless")
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/.well-known/oauth-protected-resource/mcp").status_code, 200)
        response = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("resource_metadata", response.headers["www-authenticate"])
        tools = {tool["name"] for tool in self.rpc("tools/list")["tools"]}
        self.assertEqual(tools, {
            "workspace_status", "create_thumbnail", "add_text", "place_image", "move_layer",
            "inspect_project", "list_layers", "export_thumbnail", "native_tool_catalog",
            "native_tool_call", "scan_reference_library", "get_reference_image_batch",
            "save_reference_analysis", "get_reference_analysis",
            "download_image", "set_layer_properties", "transform_layer",
            "native_edit_stage", "native_stage_status", "native_export_to_portal",
            "get_art_dna_brief", "art_dna_status", "get_art_dna_reference_images",
            "record_art_dna_observations",
            "load_image_in_photocraft",
            "create_review_job", "submit_review_result",
            "native_command_help", "native_edit_batch", "native_compose",
            "get_editing_job_stats", "get_editing_context",
        })
        catalog = json.loads(self.tool("native_tool_catalog")[0]["text"])
        self.assertIn("render_preview", {tool["name"] for tool in catalog})
        commands = {command["id"] for command in self.native_json("command_list")}
        self.assertIn("type.create", commands)
        self.assertNotIn("file.scripts.browse", commands)
        self.native("doc_new", width=320, height=180, background="#13579B")
        self.native("command_run", id="layer.new.layer", params={"name": "Persistent layer"})
        self.assertIn("Persistent layer", self.layer_names())
        image = self.native_image("render_preview", max_side=320)
        self.assertEqual(image.size, (320, 180))
        self.assertEqual(image.getpixel((160, 90)), (19, 87, 155))
        self.native("doc_open", path=str(self.workspace / "outside.psd"), error="outside")
        self.native("doc_open", path="Projects", error="input file is missing")
        self.native("doc_select", index=999, error="document")
        self.assertIn("Persistent layer", self.layer_names())

    def test_native_compose_imports_and_edits_in_one_call(self):
        self.connect("headless")
        catalog=self.tool("native_tool_catalog")[0]["text"]
        self.assertLess(len(catalog.encode()),4000)
        schema=json.loads(self.tool("native_tool_catalog",{
            "tool_name":"doc_save","detailed":True,
        })[0]["text"])
        self.assertIn("input_schema",schema[0])
        folder=self.assets/"Generated"/"compose-check"
        folder.mkdir(parents=True)
        Image.new("RGBA",(160,90),(20,35,50,255)).save(folder/"back.png")
        Image.new("RGBA",(160,90),(200,70,20,100)).save(folder/"front.png")
        result=json.loads(self.tool("native_compose",{
            "output_name":"compose-check", "width":160,"height":90,
            "assets":["Generated/compose-check/back.png","Generated/compose-check/front.png"],
            "actions_json":json.dumps([
                ["r","Accent",10,10,70,20,"#F4A040"],
                ["t","Headline","NIGHT",14,58,28,"#FFFFFF","Impact"],
            ]),"job_id":"compose-check",
        })[0]["text"])
        self.assertEqual((result["imports"],result["steps"]),(2,2))
        saved=self.projects/"compose-check.pcraft"
        self.assertTrue(saved.is_file())
        self.native("doc_close")
        self.native("doc_open",path=str(saved))
        names=self.layer_names()
        self.assertIn("Headline",names)
        self.assertIn("Accent",names)
        self.tool("native_compose",{
            "output_name":"bad-compose","assets":["../ReferenceLibrary/anything.png"],
        },error="relative image path")

    def test_staged_edits_recover_from_a_checkpoint_without_replay(self):
        self.connect("bridge")
        def edit(stage_id, plan, *, error=None):
            result = self.tool("native_edit_stage", {
                "stage_id": stage_id, "plan_json": json.dumps(plan),
            }, error=error)
            return json.loads(result[0]["text"]) if not error else None

        def state(stage_id):
            return json.loads(self.tool("native_stage_status", {"stage_id": stage_id})[0]["text"])["state"]

        def layer(name):
            return {"id": "layer.new.layer", "params": {"name": name}}

        original = {"document": {"width": 96, "height": 64}, "steps": [layer("Checkpoint layer")]}
        self.native("doc_new", width=32, height=32)
        edit("first", original, error="Close open PhotoCraft documents")
        self.assertEqual(state("first"), "missing")
        self.native("doc_close")

        self.assertEqual(edit("first", original)["state"], "complete")
        checkpoint = self.gateway.stages.checkpoint("first")
        snapshot = checkpoint.stat().st_mtime_ns
        self.assertEqual(edit("first", original)["state"], "complete")
        self.assertEqual(checkpoint.stat().st_mtime_ns, snapshot)
        edit("first", {"steps": []}, error="different edit plan")
        self.native("doc_open", path=str(checkpoint), error="editing-stage tools")

        invoke = self.gateway._call

        async def crash_after_edit(session, name, args):
            result = await invoke(session, name, args)
            if name == "command_batch":
                self.gateway._desktop.terminate()
                self.gateway._desktop.wait(timeout=5)
                raise ConnectionError("test disconnect after editing")
            return result

        interrupted = {"checkpoint": "first", "steps": [layer("Never replay")]}
        with patch.object(self.gateway, "_call", crash_after_edit):
            edit("interrupted", interrupted, error="disconnected after dispatch")
        self.assertEqual(state("interrupted"), "started")
        edit("interrupted", interrupted, error="may already have applied")

        async def resume_in_parallel():
            return await asyncio.gather(*(
                self.gateway.request("stage", name=stage, arguments=plan)
                for stage, plan in (
                    ("resumed", {"checkpoint": "first", "steps": [layer("Recovered layer")]}),
                    ("unrelated", {"document": {"width": 48, "height": 32}, "steps": [layer("Other job")]}),
                )
            ))

        statuses = self.client.portal.call(resume_in_parallel)
        self.assertTrue(all(status["state"] == "complete" for status in statuses))
        for stage, expected, forbidden in (
            ("resumed", ("Checkpoint layer", "Recovered layer"), ("Never replay", "Other job")),
            ("unrelated", ("Other job",), ("Checkpoint layer", "Recovered layer")),
        ):
            output = subprocess.check_output([
                self.server.CLI, "info", "--compact", str(self.gateway.stages.checkpoint(stage)),
            ], text=True, timeout=10)
            for name in expected:
                self.assertIn(name, output)
            for name in forbidden:
                self.assertNotIn(name, output)

    def layer_names(self):
        return {layer["name"] for layer in self.native_json("doc_inspect")["layers"]}

    def test_oauth_and_upload_portal(self):
        self.connect("headless")
        self.client.follow_redirects = False
        redirect = "http://127.0.0.1:19999/callback"
        response = self.client.post("/register", json={"redirect_uris": [redirect]})
        self.assertEqual(response.status_code, 201)
        client_id = response.json()["client_id"]
        verifier = "test-verifier-" * 4
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        form = self.client.get("/authorize", params={
            "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "test-state",
        })
        self.assertEqual(form.status_code, 200)
        request_id = re.search(r'name="request_id" value="([^"]+)"', form.text).group(1)
        approved = self.client.post("/authorize", data={
            "request_id": request_id, "decision": "approve", "password": self.server.AUTH_PASSWORD,
        })
        self.assertEqual(approved.status_code, 303)
        callback = parse_qs(urlsplit(approved.headers["location"]).query)
        self.assertEqual(callback["state"], ["test-state"])
        tokens = self.client.post("/token", data={
            "grant_type": "authorization_code", "client_id": client_id, "redirect_uri": redirect,
            "code": callback["code"][0], "code_verifier": verifier,
        })
        self.assertEqual(tokens.status_code, 200)
        token = tokens.json()["access_token"]
        self.headers["Authorization"] = "Bearer " + token
        self.assertTrue(self.rpc("tools/list")["tools"])
        revoked = self.client.post("/revoke", data={"token": token, "client_id": client_id})
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(self.client.post("/mcp", headers=self.headers, json={}).status_code, 401)

        incoming = self.workspace / "Incoming"
        incoming.mkdir()
        self.enterContext(patch.object(self.portal, "INCOMING", incoming))
        portal = self.enterContext(TestClient(self.portal.app, base_url="https://testserver", follow_redirects=False))
        self.assertEqual(portal.get("/health").status_code, 200)
        login = portal.post("/login", data={"password": self.portal.PASSWORD})
        self.assertEqual(login.status_code, 303)
        png = io.BytesIO()
        Image.new("RGB", (24, 12), "#13579b").save(png, format="PNG")
        upload = portal.post("/upload", data={"destination": "Incoming"}, files={
            "files": ("test.png", png.getvalue(), "image/png"),
        })
        self.assertEqual(upload.status_code, 200)
        self.assertEqual((incoming / "test.png").read_bytes(), png.getvalue())

    def test_desktop_edit_save_export_and_restart(self):
        native_processes = []
        spawn = stdio._create_platform_compatible_process

        async def observe_spawn(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            native_processes.append(process)
            return process

        self.enterContext(patch.object(stdio, "_create_platform_compatible_process", observe_spawn))
        self.connect("bridge")
        self.assertIsNone(self.gateway._desktop)
        self.native("doc_new", name="Field Notes", width=640, height=360, background="#FFFFFF")
        self.native("command_run", id="edit.fill", params={"contents": "color", "color": "#17263D"})
        desktop = self.gateway._desktop
        self.assertIsNone(desktop.poll())
        async def reject_wrong_token():
            parameters = StdioServerParameters(
                command=self.server.CLI, args=self.gateway._bridge_parameters.args,
                env={"PHOTOCRAFT_CONTROL_TOKEN": "0" * 64},
            )
            async with asyncio.timeout(10):
                async with stdio_client(parameters) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        self.assertTrue((await session.call_tool("session_list", {})).is_error)

        self.client.portal.call(reject_wrong_token)
        self.assertNotIn("--control-token", self.gateway._bridge_parameters.args)
        star = Image.new("RGBA", (20, 20))
        ImageDraw.Draw(star).polygon(
            [(10, 0), (12, 8), (19, 10), (12, 12), (10, 19), (8, 12), (0, 10), (8, 8)],
            fill="#56E3CA",
        )
        star.save(self.assets / "star.png")
        self.native("doc_save", path="base.psd")
        self.native("doc_close")
        self.tool("place_image", {
            "project_filename": "base.psd", "image_filename": "star.png", "output_name": "placed",
        })
        self.native("doc_open", path="placed.psd")
        asset = self.native_json("doc_inspect")["layers"][0]
        self.assertIn("star", asset["name"].lower())
        self.native("command_run", id="layer.translate", params={
            "layer": asset["id"], "dx": 250, "dy": 73,
        })
        blueprint = [
            ("shape.create", {"name": "Accent", "kind": "roundedRect",
                              "rect": [44, 51, 74, 12], "radii": 6, "fill": "#56E3CA"}),
            ("shape.create", {"name": "Illustration", "kind": "ellipse",
                              "rect": [453, 86, 147, 147], "fill": "#F7AF66"}),
            ("shape.create", {"name": "Shadow", "kind": "ellipse",
                              "rect": [506, 71, 119, 119], "fill": "#17263D"}),
            ("type.create", {"name": "Title", "text": "FIELD NOTES", "x": 44,
                             "y": 161, "size": 52, "color": "#F4F5EE"}),
            ("type.create", {"name": "Subtitle", "text": "A guide to the night sky",
                             "x": 46, "y": 216, "size": 26, "color": "#A7C1D8"}),
        ]
        batch = self.native_json("command_batch", steps=[{"id": cmd, "params": args} for cmd, args in blueprint])
        self.assertEqual(batch["completed"], len(blueprint), batch)
        expected = {args["name"] for _, args in blueprint}
        self.assertLessEqual(expected, self.layer_names())
        preview = self.native_image("ui_screenshot", max_side=1000)
        self.assertGreaterEqual(preview.width, 640)
        self.assertGreater(len(preview.getcolors(10000) or [None] * 10000), 20)
        self.capture("desktop-preview.png", preview)
        self.native("render_preview", index=0, error="active document")
        canvas = self.native_image("render_preview", max_side=640)
        self.assertEqual(canvas.size, (640, 360))
        self.assertEqual(self.native_image("render_preview", max_side=320).size, (320, 180))
        self.assertEqual({path.name for path in self.projects.iterdir()}, {"base.psd", "placed.psd"})

        self.native("doc_save", path="Projects/field-notes.pcraft")
        self.native("doc_save", path="Projects/field-notes.psd", maximizeCompatibility=True)
        self.native("doc_export", path="Projects/field-notes.png")
        for filename in ("field-notes.pcraft", "field-notes.psd", "field-notes.png"):
            self.assertGreater((self.projects / filename).stat().st_size, 100)
        with Image.open(self.projects / "field-notes.png") as image:
            export = image.convert("RGB")
        self.assertEqual(export.size, (640, 360))
        self.assertEqual(export.getpixel((0, 0)), (23, 38, 61))
        self.assertEqual(export.getpixel((80, 57)), (86, 227, 202))
        self.assertEqual(export.getpixel((470, 158)), (247, 175, 102))
        self.assertEqual(export.getpixel((570, 253)), (86, 227, 202))
        self.assertIsNone(ImageChops.difference(export, canvas).getbbox())
        portal_result = self.tool("native_export_to_portal", {"output_name": "portal-active"})
        self.assertIn(":8443/download/portal-active.png", portal_result[0]["text"])
        self.tool("native_export_to_portal", {"output_name": "portal-active"}, error="exists")
        self.capture("field-notes.png", export)
        from review_jobs import ReviewJobs
        review = ReviewJobs(self.workspace/"pending")
        with patch.object(self.server,"REVIEW_JOBS",review), patch.multiple(
            self.portal,REVIEW_JOBS=review,EXPORTS=self.exports,
        ), TestClient(self.portal.app,base_url="https://testserver",follow_redirects=False) as client:
            single = json.loads(self.tool("create_review_job",{"title":"Single"})[0]["text"])
            self.assertEqual(len(single["modes"]),1)
            job = json.loads(self.tool("create_review_job",{
                "title":"Review test","modes":["reference","hybrid"],
            })[0]["text"])["job_id"]
            for number in (1,2):
                self.tool("submit_review_result",{"job_id":job,"number":number})
                self.assertFalse(review.approved(self.exports,job,number).exists())
            self.assertEqual(client.get(f"/review/{job}/1").status_code,401)
            client.post("/login",data={"password":self.portal.PASSWORD})
            self.assertIn("Image 2",client.get("/?tab=review").text)
            approval = f"/review/{job}/approve"
            self.assertEqual(client.post(approval,data={"csrf":"bad","numbers":"2"}).status_code,403)
            self.assertEqual(client.post(approval,data={
                "csrf":self.portal.approval_token(job),"numbers":"2",
            }).status_code,303)
            self.assertFalse(review.approved(self.exports,job,1).exists())
            self.assertEqual(review.approved(self.exports,job,2).read_bytes(),review.pending(job,2).read_bytes())

        self.native("doc_close")
        self.native("doc_open", path="field-notes.psd")
        self.assertLessEqual(expected, self.layer_names())
        self.tool("export_thumbnail", {"project_filename": "field-notes.psd", "output_name": "portal-copy"})
        self.enterContext(patch.object(self.portal, "EXPORTS", self.exports))
        with TestClient(self.portal.app, base_url="https://testserver", follow_redirects=False) as portal:
            self.assertEqual(portal.get("/download/portal-active.png?preview=1").status_code, 401)
            self.assertEqual(portal.post("/login", data={"password": self.portal.PASSWORD}).status_code, 303)
            self.assertIn("/download/portal-active.png?preview=1", portal.get("/").text)
            self.assertNotIn(">portal-active.png<",portal.get("/").text)
            active = portal.get("/download/portal-active.png?preview=1")
            self.assertEqual(active.status_code, 200)
            self.assertEqual(active.headers["content-type"], "image/png")
            download = portal.get("/download/portal-copy.png")
            self.assertEqual(download.status_code, 200)
        with Image.open(io.BytesIO(active.content)) as from_active:
            self.assertIsNone(ImageChops.difference(export, from_active.convert("RGB")).getbbox())
        with Image.open(io.BytesIO(download.content)) as downloaded:
            self.assertIsNone(ImageChops.difference(export, downloaded.convert("RGB")).getbbox())
        self.native("doc_close")
        self.native("doc_open", path="field-notes.pcraft")
        self.assertLessEqual(expected, self.layer_names())

        # Kill the actual MCP bridge subprocess while keeping the desktop alive.
        async def disconnect():
            native_processes[0].kill()
            await asyncio.wait_for(native_processes[0].wait(), 5)

        self.client.portal.call(disconnect)
        self.native("command_run", id="layer.new.layer", params={"name": "Never dispatched"}, error="before dispatch")
        self.assertLessEqual(expected, self.layer_names())
        self.assertNotIn("Never dispatched", self.layer_names())
        self.assertIs(self.gateway._desktop, desktop)
        self.assertIsNone(desktop.poll())

        # Kill only the owned automation instance to simulate a desktop crash.
        desktop.terminate()
        desktop.wait(timeout=5)
        self.native("command_run", id="layer.new.layer", params={"name": "Never replay"}, error="before dispatch")
        self.tool("native_tool_catalog")
        self.assertNotEqual(self.gateway._desktop.pid, desktop.pid)
        self.native("doc_open", path="field-notes.pcraft")
        self.assertLessEqual(expected, self.layer_names())
        self.assertNotIn("Never replay", self.layer_names())
        self.native("doc_export", path="after-restart.png")
        with Image.open(self.projects / "after-restart.png") as recovered:
            self.assertIsNone(ImageChops.difference(export, recovered.convert("RGB")).getbbox())
