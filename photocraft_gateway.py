"""Own the native MCP session and, in bridge mode, its PhotoCraft desktop."""

import asyncio
import base64
import io
import json
import logging
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image
from studio_stages import STAGE_DIRECTORY, StageError, StageStore
from editing_efficiency import EditingError, command_help, document_summary, fingerprint


LOG = logging.getLogger(__name__)

# The desktop process also has interactive commands. Only forward reviewed
# editing commands and read-only raw control methods from the HTTP gateway.
EDIT_COMMANDS = frozenset("""
    document.inspect edit.undo edit.redo edit.transform edit.fill file.new file.close file.placeEmbedded
    image.crop image.imageSize image.canvasSize
    layer.list layer.new.layer layer.new.group layer.delete layer.duplicate
    layer.select layer.translate layer.setProps layer.mergeDown layer.mergeVisible
    layer.arrange.bringForward layer.arrange.sendBackward
    layer.arrange.bringToFront layer.arrange.sendToBack
    layer.layerMask.revealSelection layer.layerMask.hideSelection layer.layerMask.revealAll
    layer.layerMask.edit layer.layerMask.enabled
    layer.layerStyle.dropShadow layer.layerStyle.outerGlow layer.layerStyle.innerGlow
    layer.layerStyle.stroke layer.layerStyle.gradientOverlay
    layer.newFillLayer.gradient layer.newAdjustmentLayer.curves
    layer.newAdjustmentLayer.hueSaturation
    type.create type.edit type.setStyle type.info type.fonts type.rasterize
    shape.create shape.edit shape.info shape.rasterize
    select.rect select.deselect select.inverse
    filter.blur.gaussianBlur filter.blur.motionBlur filter.sharpen.unsharpMask
""".split())
CONTROL_METHODS = {"ui.inspect", "ui.menu.list"}
NATIVE_TOOLS = frozenset("""
    command_batch command_list command_run control_call
    doc_close doc_export doc_export_data_sets doc_export_layers
    doc_import_data_sets doc_inspect doc_new doc_open doc_save doc_select
    jobs_cancel jobs_list render_preview session_list
    ui_inspect ui_menu_invoke ui_pointer ui_screenshot ui_set
""".split())


class NativeError(RuntimeError):
    """An anticipated gateway failure safe to report over MCP."""


class ExportQualityError(NativeError):
    """The active document did not pass publication checks."""


@dataclass
class Request:
    operation: str
    future: asyncio.Future
    name: str | None = None
    arguments: dict | None = None
    started: bool = False


class NativeGateway:
    def __init__(self, cli, read_roots, write_root, *, mode="headless",
                 desktop_executable=None, timeout=180):
        if mode not in {"bridge", "headless"}:
            raise ValueError("PHOTOCRAFT_NATIVE_MODE must be bridge or headless.")
        self.cli = cli
        self.mode = mode
        self.read_roots = tuple(Path(path).resolve() for path in read_roots)
        self.write_root = Path(write_root).resolve()
        self.stages = StageStore(self.write_root)
        self.desktop_executable = desktop_executable
        self.timeout = timeout
        self._queue = asyncio.Queue()
        self._worker = None
        self._desktop = None
        self._desktop_job = None
        self._desktop_config = None
        self._bridge_parameters = None
        self._closing = False
        self._catalog_cache = None
        self._command_cache = None
        self._native_version = None
        self._command_cache_binary = None
        self._session_stamp = None
        self._preview_cache = None

    @classmethod
    def from_environment(cls, cli, read_roots, write_root):
        mode = os.environ.get("PHOTOCRAFT_NATIVE_MODE", "bridge").strip().lower()
        return cls(cli, read_roots, write_root, mode=mode)

    def _root_args(self):
        args = []
        for root in self.read_roots:
            args.extend(("--automation-read-root", str(root)))
        return [*args, "--automation-write-root", str(self.write_root)]

    async def _parameters(self):
        if self.mode == "headless":
            # Preserve the supported renderer switches without accepting a local
            # override that broadens file capabilities or redirects the bridge.
            overrides = shlex.split(os.environ.get("PHOTOCRAFT_NATIVE_MCP_ARGS", ""), posix=False)
            renderer = [flag for flag in ("--gpu", "--no-gpu") if flag in overrides]
            return StdioServerParameters(command=self.cli, args=["mcp", *renderer, *self._root_args()])

        if self._desktop is not None and self._desktop.poll() is None:
            return self._bridge_parameters
        await self._stop_desktop()
        desktop = self.desktop_executable or shutil.which("photocraft")
        if not desktop:
            raise NativeError("PhotoCraft desktop was not found on PATH.")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        token = secrets.token_hex(32)
        self._desktop_config = tempfile.TemporaryDirectory(prefix="photocraft-automation-")
        env = get_default_environment()
        env.update(PHOTOCRAFT_CONTROL_TOKEN=token, PHOTOCRAFT_CONFIG_DIR=self._desktop_config.name)
        self._desktop = subprocess.Popen(
            [desktop, "--new-instance", "--control", str(port), *self._root_args()],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False,
        )
        if os.name == "nt":
            self._own_windows_desktop()
        self._bridge_parameters = StdioServerParameters(
            command=self.cli, args=["mcp", "--bridge", f"127.0.0.1:{port}"],
            env={"PHOTOCRAFT_CONTROL_TOKEN": token},
        )
        # TCP readiness alone is insufficient. _serve also authenticates with
        # session_list before giving any queued operation to the bridge.
        for _ in range(100):
            if self._desktop.poll() is not None:
                raise NativeError("PhotoCraft desktop exited during startup.")
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.25)
                writer.close()
                await writer.wait_closed()
                return self._bridge_parameters
            except (OSError, TimeoutError):
                await asyncio.sleep(0.1)
        raise NativeError("PhotoCraft desktop did not open its local control port.")

    async def _stop_desktop(self):
        process, self._desktop = self._desktop, None
        config, self._desktop_config = self._desktop_config, None
        job, self._desktop_job = self._desktop_job, None
        self._bridge_parameters = None
        try:
            if job is not None:
                job.Close()
            elif process is not None and process.poll() is None:
                with suppress(ProcessLookupError):
                    process.terminate()
            if process is not None:
                await asyncio.to_thread(process.wait, 5)
        finally:
            if config is not None:
                config.cleanup()

    def _own_windows_desktop(self):
        # Close the dedicated desktop if the gateway process exits abruptly,
        # including when normal ASGI shutdown hooks cannot run.
        import win32api
        import win32con
        import win32job

        job = win32job.CreateJobObject(None, "")
        try:
            limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
            handle = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, self._desktop.pid,
            )
            try:
                win32job.AssignProcessToJobObject(job, handle)
            finally:
                handle.Close()
        except BaseException:
            job.Close()
            raise
        self._desktop_job = job

    async def request(self, operation, *, name=None, arguments=None):
        if self._closing:
            raise NativeError("Native PhotoCraft gateway is shutting down.")
        if operation not in {"catalog", "call", "stage", "checked_export", "command_help", "compact_edit"}:
            raise ValueError("Unknown gateway operation")
        if operation == "command_help" and self._command_cache is not None:
            stamp = self._binary_stamp()
            if stamp != self._command_cache_binary:
                self._catalog_cache = self._command_cache = None
            else:
                return command_help(self._command_cache, **arguments)
        if operation == "call":
            arguments = self._authorize(name, arguments)
        elif operation == "stage":
            arguments = self._stage_plan(arguments)
            if result := self.stages.replay(name, arguments):
                return result
        elif operation == "compact_edit":
            self._authorize("command_batch", {"steps":arguments["steps"]})
            if any(step["id"].startswith("file.") for step in arguments["steps"]):
                raise EditingError("Open or create the document first. Compact edits operate on that canvas.")
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._serve(), name="photocraft-native-mcp")
        request = Request(operation, asyncio.get_running_loop().create_future(), name, arguments)
        self._queue.put_nowait(request)
        try:
            return await asyncio.wait_for(request.future, self.timeout)
        except TimeoutError:
            if request.started:
                if operation == "stage":
                    raise NativeError("The stage timed out after dispatch. Check native_stage_status before continuing.") from None
                raise NativeError("PhotoCraft timed out after dispatch. Check the document before retrying.") from None
            raise NativeError("PhotoCraft timed out before dispatch. The request was skipped.") from None

    async def _serve(self):
        current = None
        failure = None
        ready = False
        existing_config = self._desktop_config
        try:
            async with asyncio.timeout(45) as startup:
                parameters = await self._parameters()
                async with stdio_client(parameters) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        identity = await session.initialize()
                        version = identity.server_info.version
                        stamp = self._binary_stamp()
                        if (version,stamp) != (self._native_version,self._command_cache_binary):
                            self._catalog_cache = self._command_cache = None
                            self._native_version = version
                            self._command_cache_binary = stamp
                        self._session_stamp = secrets.token_hex(8)
                        self._preview_cache = None
                        if self.mode == "bridge":
                            probe = await session.call_tool("session_list", {})
                            if probe.is_error:
                                raise NativeError("PhotoCraft desktop control authentication or readiness check failed.")
                        startup.reschedule(None)
                        ready = True
                        LOG.info("Native PhotoCraft MCP connected in %s mode", self.mode)
                        while True:
                            current = await self._queue.get()
                            if current.future.cancelled():
                                current = None
                                continue
                            if self.mode == "bridge" and self._desktop.poll() is not None:
                                failure = NativeError("PhotoCraft desktop exited before dispatch. Call again to restart it.")
                                return
                            async with asyncio.timeout(5):
                                await session.send_ping()
                            if current.future.cancelled():
                                current = None
                                continue
                            async with asyncio.timeout(self.timeout):
                                try:
                                    if current.operation == "catalog":
                                        result = await self._catalog(session)
                                    else:
                                        current.started = True
                                        result = await self._dispatch(session, current)
                                except (StageError, ExportQualityError, EditingError) as exc:
                                    self._fail(current, exc)
                                else:
                                    if not current.future.done():
                                        current.future.set_result(result)
                            current = None
        except Exception as exc:
            # Subprocess and SDK exceptions can include command or environment
            # details. Report exception classes and use controlled client text.
            LOG.warning("Native PhotoCraft MCP stopped (%s)", type(exc).__name__)
            failure = exc if isinstance(exc, NativeError) else NativeError(
                "Native PhotoCraft connection failed before dispatch. Call again to reconnect."
            )
        finally:
            if not ready and self.mode == "bridge" and self._desktop_config is not existing_config:
                try:
                    await self._stop_desktop()
                except (OSError, subprocess.TimeoutExpired):
                    LOG.warning("Could not clean up the failed PhotoCraft desktop startup")
            if current is not None:
                message = failure or NativeError("PhotoCraft stopped before dispatch.")
                if current.started:
                    message = NativeError("PhotoCraft disconnected after dispatch. Check the document before retrying.")
                    if current.operation == "stage":
                        message = NativeError(
                            "PhotoCraft disconnected after dispatch. Check native_stage_status for the checkpoint."
                        )
                self._fail(current, message)
            while not self._queue.empty():
                self._fail(self._queue.get_nowait(), failure or NativeError("PhotoCraft stopped before dispatch."))

    async def _dispatch(self, session, request):
        if request.operation == "call":
            return await self._call(session, request.name, request.arguments)
        if request.operation == "checked_export":
            return await self._checked_export(session, request.arguments)
        if request.operation == "command_help":
            if self._command_cache is None:
                listed = await self._call(session, "command_list", {})
                if listed.is_error:
                    raise EditingError("PhotoCraft did not return its command descriptions.")
                self._command_cache = json.loads(listed.content[0].text)
            return command_help(self._command_cache, **request.arguments)
        if request.operation == "compact_edit":
            return await self._compact_edit(session, request.arguments)

        async def call(name, args):
            return await self._call(session, name, args)

        def resolve_project(path):
            try:
                return self._path(path, self.read_roots, read=True, document=True)
            except NativeError as exc:
                raise StageError(str(exc)) from None

        return await self.stages.execute(request.name, request.arguments, call, resolve_project)

    def _binary_stamp(self):
        try:
            stat = Path(self.cli).stat()
            return stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    async def _compact_edit(self, session, arguments):
        async def inspect():
            current = await self._call(session, "doc_inspect", {})
            if current.is_error:
                raise EditingError("Create or open a PhotoCraft document first.")
            document = json.loads(current.content[0].text)
            return document, fingerprint([self._session_stamp, document])

        document, state_id = await inspect()
        if arguments["expected_state_id"] and arguments["expected_state_id"] != state_id:
            raise EditingError("The active document changed. Read the latest summary and review before editing.")

        steps = arguments["steps"]
        report = {"completed":0}
        if steps:
            result = await self._call(session, "command_batch", {"steps":steps,"stop_on_error":True})
            if result.is_error:
                error = next((block.text for block in result.content if block.type=="text"), "Native batch failed.")
                raise EditingError(error[:350])
            outcome = json.loads(result.content[0].text)
            report["completed"] = outcome["completed"]
            if outcome.get("failed"):
                failures = [row for row in outcome.get("results", []) if not row.get("ok")]
                if failures:
                    report["error"] = str(failures[0].get("error", "A native step failed."))[:350]
                report["failed_step"] = outcome["completed"]+1
            document, state_id = await inspect()
        report["state"] = document_summary(document, arguments["detail"])
        report["state_id"] = state_id
        if not arguments["edge"] or "failed_step" in report:
            return report, None

        key = (state_id, arguments["edge"])
        if self._preview_cache and self._preview_cache[0] == key:
            _, preview_id, image = self._preview_cache
        else:
            if self.mode == "bridge":
                rendered = await self._canvas_preview(session, {"max_side":arguments["edge"]}, document)
            else:
                rendered = await self._call(session, "render_preview", {"max_side":arguments["edge"]})
            if rendered.is_error:
                report["preview_error"] = "PhotoCraft could not render this preview."
                return report, None
            image = next((block for block in rendered.content if block.type=="image"), None)
            if image is None:
                report["preview_error"] = "PhotoCraft returned no preview image."
                return report, None
            preview_id = fingerprint(image.data)
            self._preview_cache = (key, preview_id, image)
        report["preview_id"] = preview_id
        if preview_id == arguments["known_preview_id"]:
            report["preview_unchanged"] = True
            return report, None
        return report, image

    async def _checked_export(self, session, arguments):
        result = await self._call(session, "doc_inspect", {})
        if result.is_error:
            raise ExportQualityError("Open a document before exporting to the portal.")
        document = json.loads(result.content[0].text)
        size = document["width"], document["height"]
        expected = arguments["expected"]
        if expected is not None and size != expected:
            raise ExportQualityError(f"Canvas is {size[0]}×{size[1]}; expected {expected[0]}×{expected[1]}.")
        if size[0] * size[1] > 67_108_864:
            raise ExportQualityError("This canvas is too large for portal export. Resize it first.")

        visible_text = set()
        clipped_text = set()

        def inspect_layers(layers):
            for layer in layers:
                if layer.get("visible") is False:
                    continue
                if layer.get("kind", "").lower() in {"type", "text"} and layer.get("bounds"):
                    x, y, width, height = layer["bounds"]
                    if width > 0 and height > 0 and x < size[0] and y < size[1] and x+width > 0 and y+height > 0:
                        visible_text.add(layer["name"])
                    if x < 0 or y < 0 or x+width > size[0] or y+height > size[1]:
                        clipped_text.add(layer["name"])
                inspect_layers(layer.get("children") or ())

        inspect_layers(document["layers"])
        missing = set(arguments["required_text"]) - visible_text
        if missing:
            names = ", ".join(sorted(missing))
            raise ExportQualityError(f"Required text layers are missing, hidden, or outside the canvas: {names}.")
        result = await self._call(session, "doc_export", {"path": arguments["path"]})
        if result.is_error:
            raise ExportQualityError("PhotoCraft could not export the active document.")
        return size, sorted(clipped_text)

    def _stage_plan(self, plan):
        if not isinstance(plan, dict) or plan.keys() - {"document", "project", "checkpoint", "steps"}:
            raise StageError("Use a JSON object with steps and an optional document, project or checkpoint.")
        starting_points = [key for key in ("document", "project", "checkpoint") if key in plan]
        if len(starting_points) > 1:
            raise StageError("Choose one starting point: document, project or checkpoint.")
        document = plan.get("document", {})
        if not isinstance(document, dict) or document.keys() - {"name", "width", "height", "background"}:
            raise StageError("A new stage document accepts name, width, height and background.")
        for key in ("width", "height"):
            size = document.get(key, 1920 if key == "width" else 1080)
            if type(size) is not int or not 1 <= size <= 4096:
                raise StageError("Stage canvas dimensions must be between 1 and 4096 pixels.")
        if "project" in plan and (not isinstance(plan["project"], str) or not plan["project"]):
            raise StageError("Use a workspace project path.")
        if "checkpoint" in plan:
            self.stages.directory(plan["checkpoint"])
        steps = plan.get("steps")
        if not isinstance(steps, list) or len(steps) > 64:
            raise StageError("Supply an array of zero to 64 editing steps.")
        for step in steps:
            if not isinstance(step, dict) or step.keys() - {"id", "params"}:
                raise StageError("Each editing step accepts id and params.")
            command = step.get("id")
            if not isinstance(command, str) or command not in EDIT_COMMANDS or command.startswith("file."):
                raise StageError("Use a reviewed editing command. File commands cannot run inside a stage.")
            params = step.get("params", {})
            if not isinstance(params, dict) or "document" in params:
                raise StageError("Step params must target the stage's active document.")
        return plan

    async def _call(self, session, name, arguments):
        if name not in {"catalog","command_list","doc_inspect","session_list","render_preview",
                        "ui_inspect","ui_screenshot","jobs_list"}:
            self._preview_cache = None
        if self.mode == "bridge" and name == "doc_close":
            # The native wrapper exposes doc_close in both catalogs, but its
            # implementation delegates only in headless mode.
            params = {"save": "discard"}
            if arguments.get("index") is not None:
                params["document"] = arguments["index"]
            return await session.call_tool("command_run", {"id": "file.close", "params": params})
        if self.mode == "bridge" and name == "render_preview":
            # The upstream bridge renders a screenshot for this tool. Keep the
            # gateway's canvas preview usable in both modes. ui_screenshot still
            # exposes the actual desktop window and its panels.
            return await self._canvas_preview(session, arguments)
        result = await session.call_tool(name, arguments)
        if name == "command_list" and not result.is_error:
            filtered = [row for row in json.loads(result.content[0].text) if row["id"] in EDIT_COMMANDS]
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(filtered))])
        return result

    async def _catalog(self, session):
        if self._catalog_cache is not None:
            return self._catalog_cache
        result = await session.list_tools()
        tools = []
        for tool in result.tools:
            if tool.name not in NATIVE_TOOLS:
                continue
            if tool.name in {"doc_save", "doc_export"}:
                tool.input_schema["required"] = ["path"]
            if tool.name == "render_preview" and self.mode == "bridge":
                tool.description = "Render a bounded PNG of the active canvas. Use ui_screenshot for the desktop window."
                tool.input_schema.get("properties", {}).pop("index", None)
            tools.append(tool)
        result.tools[:] = tools
        self._catalog_cache = result
        return result

    async def _canvas_preview(self, session, arguments, state=None):
        max_side = arguments.get("max_side")
        if state is None:
            info = await session.call_tool("doc_inspect", {})
            if info.is_error:
                return info
            state = json.loads(info.content[0].text)
        width, height = state["width"], state["height"]
        if width * height > 67_108_864 or (max_side == 0 and max(width, height) > 2048):
            return CallToolResult(
                isError=True, content=[TextContent(type="text", text="Canvas exceeds the requested preview limits.")],
            )
        max_side = 1024 if max_side is None else (max_side or 2048)
        with tempfile.TemporaryDirectory(prefix=".preview-", dir=self.write_root) as directory:
            path = Path(directory) / "canvas.png"
            result = await session.call_tool("doc_export", {"path": str(path)})
            if result.is_error:
                return result
            image = self._preview_content(path, max_side)
        return CallToolResult(content=[image])

    @staticmethod
    def _preview_content(path, max_side):
        with Image.open(path) as original, io.BytesIO() as output:
            original.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            original.save(output, format="PNG")
            data = base64.b64encode(output.getvalue()).decode("ascii")
        return ImageContent(type="image", data=data, mimeType="image/png")

    @staticmethod
    def _fail(request, error):
        if not request.future.done():
            request.future.set_exception(error)

    async def shutdown(self):
        self._closing = True
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
        await self._stop_desktop()

    def _authorize(self, name, arguments):
        if name not in NATIVE_TOOLS:
            raise NativeError("Unknown native PhotoCraft tool. Call native_tool_catalog first.")
        args = dict(arguments)
        if name in {"command_run", "ui_menu_invoke"}:
            self._check_command(args.get("id"), args.get("params"), menu=name == "ui_menu_invoke")
        elif name == "command_batch":
            steps = args.get("steps")
            if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
                raise NativeError("command_batch requires an array of command objects.")
            for index, step in enumerate(steps, 1):
                try:
                    self._check_command(step.get("id"), step.get("params"))
                except NativeError as exc:
                    raise NativeError(f"command_batch step {index}: {exc}") from None
        elif name == "control_call":
            method = args.get("method")
            if not isinstance(method, str) or method not in CONTROL_METHODS or args.get("params") not in (None, {}):
                raise NativeError("This raw control method is unavailable through the HTTP gateway.")
        elif name == "render_preview":
            max_side = args.get("max_side")
            if max_side is not None and (type(max_side) is not int or not 0 <= max_side <= 2048):
                raise NativeError("max_side must be a whole number from 0 to 2048.")
            if self.mode == "bridge" and args.get("index") is not None:
                raise NativeError("Bridge previews use the active document. Indexed previews require headless mode.")
        elif name in {"doc_open", "doc_import_data_sets"}:
            args["path"] = self._path(args.get("path"), self.read_roots, read=True, document=name == "doc_open")
        elif name in {"doc_save", "doc_export"}:
            args["path"] = self._path(args.get("path"), (self.write_root,))
        elif name in {"doc_export_layers", "doc_export_data_sets"}:
            args["dir"] = self._path(args.get("dir"), (self.write_root,))
            for key in ("prefix", "naming", "format"):
                if key in args and args[key] is not None:
                    value = args[key]
                    if not isinstance(value, str) or any(char in value for char in "/\\:"):
                        raise NativeError("Export names and format must be filename components.")
        return args

    def _check_command(self, command, params, *, menu=False):
        if not isinstance(command, str) or command not in EDIT_COMMANDS:
            raise NativeError("This engine command is unavailable through the HTTP gateway.")
        if menu and command.startswith("file."):
            raise NativeError("Use the document tools for file operations.")
        if params is not None and not isinstance(params, dict):
            raise NativeError("Engine command params must be a JSON object.")
        if command == "file.placeEmbedded" and not menu:
            if not params or not params.get("path"):
                raise NativeError("file.placeEmbedded requires a workspace image path.")
            params["path"] = self._path(params["path"], self.read_roots, read=True)
        if command == "file.new" and params and "profile" in params:
            raise NativeError("Use the default color profile for remote document creation.")

    def _path(self, value, roots, *, read=False, document=False):
        if not isinstance(value, str) or not value:
            raise NativeError("A workspace file path is required.")
        path = Path(value)
        # Reject drive-relative paths, device names and NTFS alternate streams.
        if (path.anchor and not path.is_absolute()) or any(
            ":" in part or part.rstrip(" .") != part or os.path.isreserved(part)
            for part in path.parts if part != path.anchor and part not in {".", ".."}
        ):
            raise NativeError("Use a regular file path inside the PhotoCraft workspace.")
        for root in roots:
            candidate = path
            if not path.is_absolute():
                relative = path.parts[1:] if path.parts and path.parts[0].lower() == root.name.lower() else path.parts
                candidate = root.joinpath(*relative)
            resolved = candidate.resolve()
            if resolved.is_relative_to(self.write_root / STAGE_DIRECTORY):
                raise NativeError("Use the editing-stage tools to access a studio checkpoint.")
            if resolved.is_relative_to(root):
                if not read or resolved.is_file() or (
                    document and resolved.suffix.lower() == ".pcraft" and resolved.is_dir()
                ):
                    return str(resolved)
        raise NativeError("The path is outside the PhotoCraft workspace or the input file is missing.")
