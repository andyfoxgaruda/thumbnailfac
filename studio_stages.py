"""Persistent checkpoints for editing stages executed by the native worker."""

import hashlib
import json
import os
import re
import zipfile
from contextlib import suppress


STAGE_DIRECTORY = ".studio-stages"


class StageError(RuntimeError):
    """An editing-stage error safe to return to an MCP client."""


class StageStore:
    def __init__(self, project_root):
        self.root = project_root / STAGE_DIRECTORY

    def directory(self, stage_id):
        if not isinstance(stage_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", stage_id):
            raise StageError("Use a stage ID of up to 64 lowercase letters, digits and hyphens.")
        path = self.root / stage_id
        if path.resolve() != path:
            raise StageError("The checkpoint directory must be inside the studio workspace.")
        return path

    def record(self, stage_id):
        directory = self.directory(stage_id)
        try:
            return json.loads((directory / "state.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"state": "started" if directory.is_dir() else "missing"}

    def checkpoint(self, stage_id):
        path = self.directory(stage_id) / "checkpoint.pcraft"
        if path.resolve() != path or not path.is_file() or not zipfile.is_zipfile(path):
            raise StageError("The checkpoint file is missing or unreadable.")
        return path

    def status(self, stage_id):
        state = self.record(stage_id)["state"]
        if state == "complete":
            self.checkpoint(stage_id)
        return {"stage_id": stage_id, "state": state}

    def replay(self, stage_id, plan):
        record = self.record(stage_id)
        if record["state"] == "missing":
            return None
        if record.get("fingerprint") not in (None, self._fingerprint(plan)):
            raise StageError("This stage ID belongs to a different edit plan.")
        if record["state"] == "complete":
            return self.status(stage_id)
        raise StageError(
            "This stage may already have applied edits. Check its status and use a new stage ID "
            "from the last completed checkpoint."
        )

    @staticmethod
    def _fingerprint(plan):
        encoded = json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _write(directory, state, fingerprint):
        staging = directory / "state.tmp"
        with staging.open("w", encoding="utf-8") as file:
            json.dump({"state": state, "fingerprint": fingerprint}, file)
            file.flush()
            os.fsync(file.fileno())
        staging.replace(directory / "state.json")

    async def execute(self, stage_id, plan, call, resolve_project):
        if result := self.replay(stage_id, plan):
            return result
        project = plan.get("project")
        previous = plan.get("checkpoint")
        if previous:
            if self.status(previous)["state"] != "complete":
                raise StageError("Resume from a completed checkpoint.")
            start = str(self.checkpoint(previous))
        else:
            start = resolve_project(project) if project else None

        session = await self._checked(call, "session_list", {})
        documents = session.get("session", session).get("documents")
        if documents != []:
            raise StageError("Close open PhotoCraft documents before running an isolated edit stage.")

        directory = self.directory(stage_id)
        directory.parent.mkdir(exist_ok=True)
        try:
            directory.mkdir()
        except FileExistsError:
            return self.replay(stage_id, plan)
        fingerprint = self._fingerprint(plan)
        self._write(directory, "started", fingerprint)
        opened = False
        try:
            if start:
                await self._checked(call, "doc_open", {"path": start})
            else:
                await self._checked(call, "doc_new", plan.get("document", {}))
            opened = True
            steps = plan["steps"]
            if steps:
                batch = await self._checked(call, "command_batch", {"steps": steps, "stop_on_error": True})
                if batch.get("completed") != len(steps) or batch.get("failed"):
                    raise StageError(
                        f"PhotoCraft completed {batch.get('completed', 0)} of {len(steps)} editing steps. "
                        "The stage has no new checkpoint."
                    )
            await self._checked(call, "doc_save", {"path": str(directory / "checkpoint.pcraft")})
            self.checkpoint(stage_id)
            self._write(directory, "complete", fingerprint)
        except StageError:
            self._write(directory, "failed", fingerprint)
            raise
        finally:
            if opened:
                with suppress(StageError):
                    await self._checked(call, "doc_close", {})
        return self.status(stage_id)

    @staticmethod
    async def _checked(call, name, arguments):
        result = await call(name, arguments)
        if result.is_error:
            raise StageError(f"PhotoCraft {name} failed. Check the last completed checkpoint.")
        try:
            return json.loads(result.content[0].text)
        except (AttributeError, IndexError, json.JSONDecodeError):
            raise StageError(f"PhotoCraft {name} returned an unexpected response.") from None
