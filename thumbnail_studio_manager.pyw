"""Small local control panel for the managed Thumbnail Studio services."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, time
from pathlib import Path
from tkinter import messagebox, ttk


ROOT = Path(__file__).resolve().parent
TASK_SCRIPT = ROOT / "studio_manager_tasks.ps1"
SCHEDULE_SCRIPT = ROOT / "studio_schedule_tasks.ps1"
SCHEDULE_FILE = ROOT / "studio_schedule.json"
SERVICES = {
    "MCP server": ("MCP", "http://127.0.0.1:8000/health"),
    "Upload portal": ("Portal", "http://127.0.0.1:8001/health"),
}
POWERSHELL = "powershell.exe"
SERVICE_KEYS = ("MCP", "Portal")


def hidden_subprocess_options() -> dict:
    """Prevent a console flash when the windowless manager calls PowerShell."""
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def task_call(action: str, service: str) -> dict:
    if action not in {"Status", "Start", "Stop", "Restart", "Enable", "Disable"}:
        raise ValueError("Unsupported service action")
    if service not in {"MCP", "Portal"}:
        raise ValueError("Unsupported service")
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(TASK_SCRIPT),
            "-Action",
            action,
            "-Service",
            service,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
        **hidden_subprocess_options(),
    )
    if result.returncode:
        # Error text only; PowerShell output is never used as an executable command.
        raise RuntimeError((result.stderr or result.stdout or "Windows could not complete that action.").strip())
    return json.loads(result.stdout)


def schedule_task_call(action: str) -> dict:
    if action not in {"Install", "Remove"}:
        raise ValueError("Unsupported schedule action")
    result = subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(SCHEDULE_SCRIPT), "-Action", action],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
        **hidden_subprocess_options(),
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Windows could not update the schedule.").strip())
    return json.loads(result.stdout)


def read_schedule() -> dict:
    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        if data.get("enabled") and valid_schedule(data.get("start", ""), data.get("end", "")):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"enabled": False, "start": "14:00", "end": "15:00"}


def valid_schedule(start: str, end: str) -> bool:
    try:
        start_time = time.fromisoformat(start)
        end_time = time.fromisoformat(end)
        return start_time != end_time and start_time.second == end_time.second == 0
    except (TypeError, ValueError):
        return False


def schedule_active(schedule: dict, now: datetime | None = None) -> bool:
    if not schedule.get("enabled") or not valid_schedule(schedule.get("start", ""), schedule.get("end", "")):
        return False
    current = (now or datetime.now()).time().replace(second=0, microsecond=0)
    start = time.fromisoformat(schedule["start"])
    end = time.fromisoformat(schedule["end"])
    if start < end:
        return start <= current < end
    return current >= start or current < end


def schedule_tick() -> None:
    """Enforce the saved local time window; called by the hidden scheduled task."""
    schedule = read_schedule()
    desired = schedule_active(schedule)
    actions = []
    for service in SERVICE_KEYS:
        state = task_call("Status", service)
        online = health_check(SERVICES["MCP server" if service == "MCP" else "Upload portal"][1])
        if desired:
            if not state.get("enabled"):
                task_call("Enable", service)
                state = task_call("Status", service)
                actions.append(f"enabled {service}")
            if not online and state["task_state"] != "Running":
                task_call("Start", service)
                actions.append(f"started {service}")
        else:
            if state.get("enabled") or state["task_state"] == "Running":
                task_call("Disable", service)
                actions.append(f"paused {service}")
    if actions:
        log_path = ROOT / "logs" / "studio_schedule.log"
        log_path.parent.mkdir(exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now().isoformat(timespec='seconds')} schedule: {', '.join(actions)}\n")


def set_schedule_enabled(enabled: bool, start: str | None = None, end: str | None = None) -> None:
    current = read_schedule()
    new_schedule = {
        "enabled": enabled,
        "start": start or current["start"],
        "end": end or current["end"],
    }
    if not valid_schedule(new_schedule["start"], new_schedule["end"]):
        raise ValueError("Choose two different valid times.")
    previous = SCHEDULE_FILE.read_bytes() if SCHEDULE_FILE.exists() else None
    temporary = SCHEDULE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(new_schedule, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, SCHEDULE_FILE)
    try:
        schedule_task_call("Install" if enabled else "Remove")
    except Exception:
        if enabled:
            if previous is None:
                SCHEDULE_FILE.unlink(missing_ok=True)
            else:
                SCHEDULE_FILE.write_bytes(previous)
        raise


def health_check(url: str) -> bool:
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            body = json.loads(response.read(2048))
            return response.status == 200 and body.get("ok") is True
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False


class StudioManager(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Thumbnail Studio")
        self.geometry("760x660")
        self.minsize(690, 610)
        self.configure(bg="#111827")
        self._busy = False
        self.status_vars: dict[str, tk.StringVar] = {}
        self.detail_vars: dict[str, tk.StringVar] = {}
        self.status_labels: dict[str, ttk.Label] = {}
        self.service_buttons: list[ttk.Button] = []
        self.schedule = read_schedule()
        self._build()
        self.after(400, self.refresh)

    def _build(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#111827")
        style.configure("Card.TFrame", background="#1f2937")
        style.configure("Title.TLabel", background="#111827", foreground="#f9fafb", font=("Segoe UI", 23, "bold"))
        style.configure("Sub.TLabel", background="#111827", foreground="#9ca3af", font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background="#1f2937", foreground="#f3f4f6", font=("Segoe UI", 13, "bold"))
        style.configure("Status.TLabel", background="#1f2937", foreground="#d1d5db", font=("Segoe UI", 11))
        style.configure("Online.Status.TLabel", background="#1f2937", foreground="#4ade80", font=("Segoe UI", 11, "bold"))
        style.configure("Offline.Status.TLabel", background="#1f2937", foreground="#fbbf24", font=("Segoe UI", 11, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=(11, 8))
        style.configure("Accent.TButton", background="#2563eb", foreground="white")
        style.map("Accent.TButton", background=[("active", "#1d4ed8")])

        outer = ttk.Frame(self, padding=24)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Thumbnail Studio", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Your local control panel", style="Sub.TLabel").pack(anchor="w", pady=(2, 20))

        for label, (service, _url) in SERVICES.items():
            card = ttk.Frame(outer, style="Card.TFrame", padding=16)
            card.pack(fill="x", pady=6)
            row = ttk.Frame(card, style="Card.TFrame")
            row.pack(fill="x")
            ttk.Label(row, text=label, style="CardTitle.TLabel").pack(side="left")
            status = tk.StringVar(value="Checking…")
            detail = tk.StringVar(value="")
            self.status_vars[service] = status
            self.detail_vars[service] = detail
            status_label = ttk.Label(row, textvariable=status, style="Offline.Status.TLabel")
            status_label.pack(side="right")
            self.status_labels[service] = status_label
            ttk.Label(card, textvariable=detail, style="Status.TLabel", font=("Segoe UI", 9)).pack(anchor="w", pady=(7, 0))

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(19, 12))
        for title, action in (("Start", "Start"), ("Restart", "Restart"), ("Stop", "Stop")):
            button = ttk.Button(controls, text=title, style="Accent.TButton" if action == "Start" else "TButton",
                                command=lambda selected=action: self.run_action(selected))
            button.pack(side="left", padx=(0, 8))
            self.service_buttons.append(button)
        ttk.Button(controls, text="Refresh status", command=self.refresh).pack(side="right")

        schedule_card = ttk.Frame(outer, style="Card.TFrame", padding=14)
        schedule_card.pack(fill="x", pady=(5, 11))
        schedule_header = ttk.Frame(schedule_card, style="Card.TFrame")
        schedule_header.pack(fill="x")
        ttk.Label(schedule_header, text="Daily schedule", style="CardTitle.TLabel").pack(side="left")
        self.schedule_state = tk.StringVar(value="Schedule is off")
        ttk.Label(schedule_header, textvariable=self.schedule_state, style="Sub.TLabel").pack(side="right")
        ttk.Label(schedule_card, text="Run every day during the time window you choose (your PC’s local time).",
                  style="Status.TLabel", font=("Segoe UI", 9)).pack(anchor="w", pady=(5, 9))
        time_row = ttk.Frame(schedule_card, style="Card.TFrame")
        time_row.pack(fill="x")
        self.start_vars = self._make_time_picker(time_row, "Start", "14:00")
        self.end_vars = self._make_time_picker(time_row, "End", "15:00")
        schedule_buttons = ttk.Frame(schedule_card, style="Card.TFrame")
        schedule_buttons.pack(fill="x", pady=(9, 0))
        ttk.Button(schedule_buttons, text="Save schedule", style="Accent.TButton", command=self.save_schedule).pack(side="left", padx=(0, 7))
        ttk.Button(schedule_buttons, text="Turn off", command=self.disable_schedule).pack(side="left")

        links = ttk.Frame(outer)
        links.pack(fill="x", pady=(2, 10))
        ttk.Button(links, text="Open upload portal", command=lambda: webbrowser.open("http://127.0.0.1:8001/")).pack(side="left", padx=(0, 8))
        ttk.Button(links, text="Open project folder", command=lambda: self.open_path(ROOT)).pack(side="left", padx=(0, 8))
        ttk.Button(links, text="Open recent logs", command=lambda: self.open_path(ROOT / "logs")).pack(side="left")
        self.message = tk.StringVar(value="Controls stay on this PC. The schedule checks once each minute.")
        ttk.Label(outer, textvariable=self.message, style="Sub.TLabel", wraplength=650).pack(anchor="w", pady=(10, 0))

        self._show_schedule_state()

    @staticmethod
    def _make_time_picker(parent: ttk.Frame, label: str, default: str) -> tuple[tk.StringVar, tk.StringVar, tk.StringVar]:
        parsed = datetime.strptime(default, "%H:%M")
        hour_var = tk.StringVar(value=parsed.strftime("%I"))
        minute_var = tk.StringVar(value=parsed.strftime("%M"))
        period_var = tk.StringVar(value=parsed.strftime("%p"))
        ttk.Label(parent, text=label, style="Status.TLabel").pack(side="left", padx=(0, 5))
        ttk.Combobox(parent, textvariable=hour_var, values=[f"{n:02d}" for n in range(1, 13)], width=3, state="readonly").pack(side="left")
        ttk.Label(parent, text=":", style="Status.TLabel").pack(side="left")
        ttk.Combobox(parent, textvariable=minute_var, values=[f"{n:02d}" for n in range(60)], width=3, state="readonly").pack(side="left")
        ttk.Combobox(parent, textvariable=period_var, values=["AM", "PM"], width=4, state="readonly").pack(side="left", padx=(4, 16))
        return hour_var, minute_var, period_var

    @staticmethod
    def _picker_value(values: tuple[tk.StringVar, tk.StringVar, tk.StringVar]) -> str:
        hour, minute, period = (value.get() for value in values)
        return datetime.strptime(f"{hour}:{minute} {period}", "%I:%M %p").strftime("%H:%M")

    def _show_schedule_state(self) -> None:
        self.schedule = read_schedule()
        start = datetime.strptime(self.schedule["start"], "%H:%M").strftime("%I:%M %p").lstrip("0")
        end = datetime.strptime(self.schedule["end"], "%H:%M").strftime("%I:%M %p").lstrip("0")
        self.schedule_state.set(f"Daily: {start}–{end}" if self.schedule["enabled"] else "Schedule is off")
        for button in self.service_buttons:
            button.configure(state="disabled" if self.schedule["enabled"] else "normal")

    def save_schedule(self) -> None:
        try:
            start = self._picker_value(self.start_vars)
            end = self._picker_value(self.end_vars)
            if not valid_schedule(start, end):
                raise ValueError("Start and end times must be different.")
        except ValueError as exc:
            messagebox.showerror("Check the schedule", str(exc), parent=self)
            return
        self._change_schedule(True, start, end)

    def disable_schedule(self) -> None:
        self._change_schedule(False)

    def _change_schedule(self, enabled: bool, start: str | None = None, end: str | None = None) -> None:
        if self._busy:
            self.message.set("Please wait for the current check or action to finish.")
            return
        self._busy = True
        self.message.set("Saving the daily schedule…" if enabled else "Turning off the schedule and restoring normal startup…")
        threading.Thread(target=self._change_schedule_worker, args=(enabled, start, end), daemon=True).start()

    def _change_schedule_worker(self, enabled: bool, start: str | None, end: str | None) -> None:
        error = None
        try:
            set_schedule_enabled(enabled, start, end)
        except Exception as exc:
            error = str(exc)
        self.after(0, lambda: self._schedule_done(enabled, start, end, error))

    def _schedule_done(self, enabled: bool, start: str | None, end: str | None, error: str | None) -> None:
        self._busy = False
        if error:
            self.message.set(f"Could not update the schedule: {error[:220]}")
        else:
            self.schedule = {"enabled": enabled, "start": start or self.schedule["start"], "end": end or self.schedule["end"]}
            for (hour, minute, period), value in ((self.start_vars, self.schedule["start"]), (self.end_vars, self.schedule["end"])):
                parsed = datetime.strptime(value, "%H:%M")
                hour.set(parsed.strftime("%I"))
                minute.set(parsed.strftime("%M"))
                period.set(parsed.strftime("%p"))
            self._show_schedule_state()
            self.message.set("Daily schedule saved. The next change can take up to one minute.")
        self.after(300, self.refresh)

    @staticmethod
    def open_path(path: Path) -> None:
        path.mkdir(exist_ok=True) if path.name == "logs" else None
        subprocess.Popen(["explorer.exe", str(path)], cwd=ROOT)

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.message.set("Checking the local services…")
        threading.Thread(target=self._read_statuses, daemon=True).start()

    def _read_statuses(self) -> None:
        records = []
        for label, (service, url) in SERVICES.items():
            try:
                task = task_call("Status", service)
                online = health_check(url)
                records.append((service, task, online, None))
            except Exception as exc:  # surface actionable local task errors in the UI
                records.append((service, None, False, str(exc)))
        self.after(0, lambda: self._show_statuses(records))

    def _show_statuses(self, records: list) -> None:
        for service, task, online, error in records:
            if error:
                self.status_vars[service].set("Needs attention")
                self.detail_vars[service].set(error[:220])
            else:
                self.status_vars[service].set("● Online" if online else "○ Offline")
                self.status_labels[service].configure(style="Online.Status.TLabel" if online else "Offline.Status.TLabel")
                if task["task_state"] == "Running":
                    detail = "Windows service is running" if online else "Windows task is running; waiting for its local health check"
                elif self.schedule["enabled"] and not schedule_active(self.schedule):
                    end = datetime.strptime(self.schedule["start"], "%H:%M").strftime("%I:%M %p").lstrip("0")
                    detail = f"Paused by schedule · next start {end}"
                else:
                    detail = "Windows service is stopped"
                last_run = task.get("last_run", "")
                if last_run and last_run != "1601-01-01 00:00:00":
                    detail += f" · Last started {last_run}"
                self.detail_vars[service].set(detail)
        self.message.set(f"Last checked {datetime.now().strftime('%I:%M:%S %p')}")
        self._show_schedule_state()
        self._busy = False
        self.after(10_000, self.refresh)

    def run_action(self, action: str) -> None:
        if self._busy:
            self.message.set("Please wait for the current check or action to finish.")
            return
        self._busy = True
        self.message.set({"Start": "Starting", "Restart": "Restarting", "Stop": "Stopping"}[action] + " the MCP server and upload portal…")
        threading.Thread(target=self._run_action, args=(action,), daemon=True).start()

    def _run_action(self, action: str) -> None:
        errors = []
        for _label, (service, _url) in SERVICES.items():
            try:
                task_call(action, service)
            except Exception as exc:
                errors.append(str(exc))
        self.after(0, lambda: self._action_done(action, errors))

    def _action_done(self, action: str, errors: list[str]) -> None:
        self._busy = False
        if errors:
            self.message.set(f"Could not {action.lower()} everything: {errors[0][:220]}")
        else:
            self.message.set(f"{action} request sent to both services. Checking their status…")
        self.after(400, self.refresh)


if __name__ == "__main__":
    if "--schedule-tick" in sys.argv:
        schedule_tick()
    else:
        StudioManager().mainloop()
