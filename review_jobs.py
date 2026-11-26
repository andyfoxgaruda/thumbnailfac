"""Explicit result slots and a filesystem-backed approval queue."""

import json
import os
import re
import secrets
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory


class ReviewError(ValueError):
    pass


class ReviewJobs:
    def __init__(self, root):
        self.root = Path(root)

    def create(self, title, modes):
        modes = modes if modes is not None else ["reference"]
        if not title.strip() or len(title)>120 or not 1<=len(modes)<=8:
            raise ReviewError("Use a title and one to eight explicitly requested results.")
        if any(mode not in {"reference","free_will","hybrid"} for mode in modes):
            raise ReviewError("Choose reference, free_will, or hybrid for each result.")
        job = secrets.token_hex(8)
        folder = self.root/job
        folder.mkdir(parents=True)
        manifest = {"job_id":job,"title":title,"modes":modes}
        temp = folder/"job.tmp"
        temp.write_text(json.dumps(manifest), encoding="utf-8")
        temp.replace(folder/"job.json")
        return manifest

    def read(self, job):
        if not re.fullmatch(r"[a-f0-9]{16}",job):
            raise ReviewError("Job not found.")
        path = self.root/job/"job.json"
        if not path.is_file():
            raise ReviewError("Job not found.")
        return json.loads(path.read_text(encoding="utf-8"))

    def pending(self, job, number):
        if not 1<=number<=len(self.read(job)["modes"]):
            raise ReviewError("Result number is outside this job.")
        return self.root/job/f"{number:02}.png"

    def approved(self, exports, job, number):
        return exports/f"studio-{job}-{number:02}.png"

    def approve(self, exports, job, numbers):
        if not numbers:
            raise ReviewError("Select at least one result.")
        sources = [(number,self.pending(job,number)) for number in dict.fromkeys(numbers)]
        if any(not source.is_file() for _,source in sources):
            raise ReviewError("A selected result is still being edited.")
        for number,source in sources:
            destination = self.approved(exports,job,number)
            if destination.exists():
                continue
            with TemporaryDirectory(prefix=".approval-",dir=exports) as work:
                staged = Path(work)/"approved.png"
                shutil.copyfile(source,staged)
                try:
                    os.link(staged,destination)
                except FileExistsError:
                    pass

    def jobs(self):
        for item in sorted(self.root.glob("*/job.json"),key=lambda p:p.stat().st_mtime_ns,reverse=True):
            yield self.read(item.parent.name)
