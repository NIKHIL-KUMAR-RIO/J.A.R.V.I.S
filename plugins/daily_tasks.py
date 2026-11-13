"""
Daily Tasks & Automated WhatsApp Briefing plugin for JARVIS (Mark LVI).

Stores your daily tasks locally in JARVIS config (`plugin_config["daily_tasks"]`),
lets you add/list/complete/remove tasks by voice, and sends a daily WhatsApp
summary of your remaining tasks (optionally combined with today's Google
Calendar events and unread Gmail highlights) either on demand or on a daily
OS schedule.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import platform
import re
import subprocess
import sys
from typing import Any, Optional

from core import outcome
from memory.config_manager import get_plugin_config, save_plugin_config

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"
_TASK_NAME = "JARVISDailyTaskBriefing"
_NS = "daily_tasks"


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _load_state(overrides: dict | None = None) -> dict:
    cfg = get_plugin_config(_NS)
    if overrides:
        cfg.update(overrides)
    tasks = cfg.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
    # Normalize task dicts
    clean_tasks: list[dict] = []
    for item in tasks:
        if isinstance(item, dict) and str(item.get("title") or "").strip():
            clean_tasks.append({
                "title": str(item["title"]).strip(),
                "done": bool(item.get("done", False)),
                "due": str(item.get("due") or "").strip(),
                "priority": str(item.get("priority") or "normal").strip().lower(),
                "added": str(item.get("added") or datetime.now().strftime("%Y-%m-%d")),
            })
        elif isinstance(item, str) and item.strip():
            clean_tasks.append({
                "title": item.strip(),
                "done": False,
                "due": "",
                "priority": "normal",
                "added": datetime.now().strftime("%Y-%m-%d"),
            })

    def _bool(key: str, default: bool) -> bool:
        val = cfg.get(key, default)
        if isinstance(val, str):
            return val.strip().lower() not in ("0", "false", "no", "off", "")
        return bool(val)

    return {
        "my_whatsapp_contact": str(cfg.get("my_whatsapp_contact") or "").strip(),
        "send_time": str(cfg.get("send_time") or "09:00").strip() or "09:00",
        "auto_send_enabled": _bool("auto_send_enabled", False),
        "include_calendar": _bool("include_calendar", True),
        "include_gmail": _bool("include_gmail", True),
        "last_sent_date": str(cfg.get("last_sent_date") or "").strip(),
        "tasks": clean_tasks,
    }


def _save_tasks(tasks: list[dict]) -> None:
    save_plugin_config(_NS, {"tasks": tasks})


def _valid_hhmm(text: str) -> Optional[str]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (text or "").strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"


def _format_tasks_text(tasks: list[dict]) -> str:
    pending = [t for t in tasks if not t.get("done")]
    completed = [t for t in tasks if t.get("done")]
    if not tasks:
        return "No daily tasks saved yet."

    lines: list[str] = []
    if pending:
        lines.append(f"Pending ({len(pending)}):")
        for idx, t in enumerate(tasks, 1):
            if t.get("done"):
                continue
            tag = "[HIGH] " if t.get("priority") == "high" else ""
            due = f" (due {t['due']})" if t.get("due") else ""
            lines.append(f"{idx}. [ ] {tag}{t['title']}{due}")
    else:
        lines.append("All tasks are completed!")

    if completed:
        lines.append(f"\nCompleted ({len(completed)}):")
        for idx, t in enumerate(tasks, 1):
            if not t.get("done"):
                continue
            lines.append(f"{idx}. [x] {t['title']}")
    return "\n".join(lines)


def _optional_calendar_summary() -> str:
    try:
        from plugins import calendar_control as cc

        st = cc._load_settings()
        if st["auth_mode"] == "ics_url" and not st["ics_url"]:
            return ""
        if st["auth_mode"] == "google_oauth" and not cc._default_token_path().is_file():
            return ""
        raw = cc.run({"action": "today"}, player=None)
        if raw.startswith("status=failed") or raw.startswith("status=ask"):
            return ""
        clean = outcome.unwrap(raw).strip()
        if "no events scheduled" in clean.lower():
            return "Calendar: No events scheduled today."
        return clean
    except Exception:
        return ""


def _optional_gmail_summary() -> str:
    try:
        from plugins import gmail_control as gc

        st = gc._load_settings()
        if st["auth_mode"] == "app_password" and (not st["email_address"] or not st["app_password"]):
            return ""
        if st["auth_mode"] == "oauth" and not gc._default_token_path().is_file():
            return ""
        raw = gc.run({"action": "list_unread", "limit": 3}, player=None)
        if raw.startswith("status=failed") or raw.startswith("status=ask"):
            return ""
        clean = outcome.unwrap(raw).strip()
        if "no matching emails" in clean.lower():
            return "Gmail: No unread emails."
        return clean
    except Exception:
        return ""


def compose_daily_briefing(state: dict | None = None) -> str:
    st = state or _load_state()
    today_label = datetime.now().strftime("%A, %b %d, %Y")
    tasks = st["tasks"]
    pending = [t for t in tasks if not t.get("done")]
    completed = [t for t in tasks if t.get("done")]

    sections: list[str] = [f"JARVIS Daily Update — {today_label}"]

    if pending:
        task_lines = [f"Today's Remaining Tasks ({len(pending)}):"]
        for i, t in enumerate(pending, 1):
            tag = "[HIGH] " if t.get("priority") == "high" else ""
            due = f" (by {t['due']})" if t.get("due") else ""
            task_lines.append(f"{i}. {tag}{t['title']}{due}")
        if completed:
            task_lines.append(f"Completed so far: {len(completed)}")
        sections.append("\n".join(task_lines))
    elif completed:
        sections.append(f"Daily Tasks: All {len(completed)} task(s) completed today!")
    else:
        sections.append("Daily Tasks: No tasks added yet. Tell JARVIS your tasks anytime.")

    if st["include_calendar"]:
        cal = _optional_calendar_summary()
        if cal:
            sections.append(cal)

    if st["include_gmail"]:
        mail = _optional_gmail_summary()
        if mail:
            sections.append(mail)

    return "\n\n".join(sections)


def send_daily_briefing(
    recipient: str = "",
    *,
    force: bool = True,
    state: dict | None = None,
) -> tuple[bool, str]:
    st = state or _load_state()
    target = (recipient or st["my_whatsapp_contact"]).strip()
    if not target:
        return False, (
            "No WhatsApp recipient configured for daily task updates. "
            "Set 'Your WhatsApp contact' in Plugin Settings -> DAILY TASKS or tell me your contact name."
        )

    today_iso = datetime.now().strftime("%Y-%m-%d")
    if not force and st["last_sent_date"] == today_iso:
        return True, f"Already sent today's briefing ({today_iso})."

    message = compose_daily_briefing(st)

    from plugins import _whatsapp_core as wa

    transport, why = wa.get()
    if transport is None:
        return False, why

    ok, err = transport.send_message_to(target, message)
    if ok:
        save_plugin_config(_NS, {"last_sent_date": today_iso})
        return True, f"Sent daily tasks update to {target}."
    return False, err


# ── OS daily scheduler helpers ───────────────────────────────────────────────

def _runner_script_path() -> Path:
    return _base_dir() / "config" / "run_daily_briefing.py"


def _write_runner_script() -> Path:
    path = _runner_script_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    root_literal = repr(str(_base_dir()))
    code = (
        "import os, sys\n"
        f"os.chdir({root_literal})\n"
        f"sys.path.insert(0, {root_literal})\n"
        "from plugins.daily_tasks import run_scheduled_briefing\n"
        "run_scheduled_briefing()\n"
    )
    path.write_text(code, encoding="utf-8")
    return path


def _windows_pythonw() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        pw = exe.with_name("pythonw.exe")
        if pw.is_file():
            return str(pw)
    return str(exe)


def install_daily_schedule(send_time: str, enabled: bool = True) -> tuple[bool, str]:
    hhmm = _valid_hhmm(send_time)
    if not hhmm:
        return False, f"Invalid send_time '{send_time}'. Use 24-hour HH:MM format (e.g. 09:00)."

    if _OS == "Windows":
        if not enabled:
            res = subprocess.run(
                ["schtasks", "/Delete", "/TN", _TASK_NAME, "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            save_plugin_config(_NS, {"auto_send_enabled": False, "send_time": hhmm})
            if res.returncode == 0 or "cannot find" in (res.stderr or "").lower():
                return True, "Daily WhatsApp briefing schedule disabled."
            return False, (res.stderr or res.stdout or "Failed to remove scheduled task.").strip()

        script = _write_runner_script()
        py = _windows_pythonw()
        tr_cmd = f'"{py}" "{script}"'
        res = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/F",
                "/SC",
                "DAILY",
                "/ST",
                hhmm,
                "/TN",
                _TASK_NAME,
                "/TR",
                tr_cmd,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "schtasks failed").strip()
            return False, f"Could not create daily schedule: {err}"
        save_plugin_config(_NS, {"auto_send_enabled": True, "send_time": hhmm})
        return True, f"Daily WhatsApp update scheduled at {hhmm} every day."

    return False, f"Automatic OS daily scheduling is currently supported on Windows (found {_OS})."


def run_scheduled_briefing() -> None:
    """Entry point executed by the daily OS task."""
    st = _load_state()
    if not st["auto_send_enabled"]:
        return
    send_daily_briefing(force=False, state=st)


# ── UI helpers & Settings action callback ────────────────────────────────────

def _show_panel(player: Any, title: str, text: str) -> None:
    if player and hasattr(player, "show_content"):
        try:
            player.show_content(title, text)
        except Exception:
            pass


def _log(player: Any, text: str) -> None:
    if player and hasattr(player, "write_log"):
        try:
            player.write_log(text)
        except Exception:
            pass


def apply_and_test(values: dict | None = None) -> tuple[bool, str]:
    """Called by Plugin Settings button: saves settings, updates daily schedule, and sends test."""
    try:
        if isinstance(values, dict):
            save_plugin_config(_NS, values)
        st = _load_state(values)
        if st["auto_send_enabled"]:
            if not st["my_whatsapp_contact"]:
                return False, "Enter 'Your WhatsApp contact' first."
            ok_sched, msg_sched = install_daily_schedule(st["send_time"], enabled=True)
            if not ok_sched:
                return False, msg_sched
        else:
            install_daily_schedule(st["send_time"], enabled=False)

        if st["my_whatsapp_contact"]:
            ok_send, msg_send = send_daily_briefing(force=True, state=st)
            if not ok_send:
                return False, msg_send
            return True, f"{msg_send} (Daily time: {st['send_time']}, auto={'ON' if st['auto_send_enabled'] else 'OFF'})"
        return True, "Settings saved. Enter 'Your WhatsApp contact' to send a test update."
    except Exception as exc:
        return False, str(exc)


# ── Main plugin entry point ──────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters if isinstance(parameters, dict) else {}
    action = str(params.get("action") or "list").strip().lower()
    task_text = str(params.get("task") or "").strip()
    due = str(params.get("due") or "").strip()
    priority = str(params.get("priority") or "normal").strip().lower()
    if priority not in ("high", "normal"):
        priority = "normal"
    recipient = str(params.get("contact") or "").strip()
    send_time = str(params.get("send_time") or "").strip()

    try:
        st = _load_state()
        tasks = list(st["tasks"])

        if action == "add":
            if not task_text:
                return outcome.ask("daily_tasks", "What task should I add to today's list?")
            items = [p.strip() for p in re.split(r"[\n;]+", task_text) if p.strip()]
            today_iso = datetime.now().strftime("%Y-%m-%d")
            for item in items:
                tasks.append({
                    "title": item,
                    "done": False,
                    "due": due,
                    "priority": priority,
                    "added": today_iso,
                })
            _save_tasks(tasks)
            formatted = _format_tasks_text(tasks)
            _show_panel(player, "DAILY TASKS", formatted)
            _log(player, f"[tasks] Added {len(items)} task(s).")
            return outcome.done("daily_tasks", action="add", added=len(items), pending=sum(1 for t in tasks if not t["done"]), verified=True)

        if action in ("list", "pending", "status"):
            formatted = _format_tasks_text(tasks)
            _show_panel(player, "DAILY TASKS", formatted)
            return outcome.untrusted("daily_tasks", formatted)

        if action in ("complete", "done", "finish"):
            idx_val = params.get("index")
            matched_idx: Optional[int] = None
            if idx_val not in (None, ""):
                try:
                    n = int(idx_val) - 1
                    if 0 <= n < len(tasks):
                        matched_idx = n
                except (TypeError, ValueError):
                    pass
            if matched_idx is None and task_text:
                needle = task_text.lower()
                for i, t in enumerate(tasks):
                    if not t["done"] and needle in t["title"].lower():
                        matched_idx = i
                        break
            if matched_idx is None:
                return outcome.ask("daily_tasks", "Which task number or name did you complete?")
            tasks[matched_idx]["done"] = True
            _save_tasks(tasks)
            _show_panel(player, "DAILY TASKS", _format_tasks_text(tasks))
            return outcome.done(
                "daily_tasks",
                action="complete",
                task=tasks[matched_idx]["title"],
                remaining=sum(1 for t in tasks if not t["done"]),
                verified=True,
            )

        if action in ("remove", "delete"):
            idx_val = params.get("index")
            matched_idx = None
            if idx_val not in (None, ""):
                try:
                    n = int(idx_val) - 1
                    if 0 <= n < len(tasks):
                        matched_idx = n
                except (TypeError, ValueError):
                    pass
            if matched_idx is None and task_text:
                needle = task_text.lower()
                for i, t in enumerate(tasks):
                    if needle in t["title"].lower():
                        matched_idx = i
                        break
            if matched_idx is None:
                return outcome.ask("daily_tasks", "Which task number or name should I remove?")
            removed = tasks.pop(matched_idx)
            _save_tasks(tasks)
            _show_panel(player, "DAILY TASKS", _format_tasks_text(tasks))
            return outcome.done("daily_tasks", action="remove", task=removed["title"], verified=True)

        if action in ("clear_completed", "clear"):
            if action == "clear":
                tasks = []
            else:
                tasks = [t for t in tasks if not t["done"]]
            _save_tasks(tasks)
            _show_panel(player, "DAILY TASKS", _format_tasks_text(tasks))
            return outcome.done("daily_tasks", action=action, remaining=len(tasks), verified=True)

        if action == "set_recipient":
            if not recipient:
                return outcome.ask("daily_tasks", "What is your WhatsApp contact name for daily updates?")
            save_plugin_config(_NS, {"my_whatsapp_contact": recipient})
            return outcome.done("daily_tasks", action="set_recipient", contact=recipient, verified=True)

        if action in ("send_now", "send", "briefing"):
            if recipient:
                save_plugin_config(_NS, {"my_whatsapp_contact": recipient})
            ok, msg = send_daily_briefing(recipient=recipient, force=True, state=st)
            if ok:
                _log(player, f"[tasks] {msg}")
                return outcome.done("daily_tasks", action="send_now", summary=msg, verified=True)
            return outcome.failed("daily_tasks", msg, action="send_now")

        if action in ("schedule_daily", "schedule", "disable_schedule"):
            enabled = action != "disable_schedule"
            if recipient:
                save_plugin_config(_NS, {"my_whatsapp_contact": recipient})
                st["my_whatsapp_contact"] = recipient
            if enabled and not st["my_whatsapp_contact"]:
                return outcome.ask("daily_tasks", "What WhatsApp contact name should I send your daily task update to?")
            target_time = _valid_hhmm(send_time or st["send_time"]) or (send_time or st["send_time"])
            ok, msg = install_daily_schedule(target_time, enabled=enabled)
            if ok:
                _log(player, f"[tasks] {msg}")
                return outcome.done("daily_tasks", op=action, time=target_time, enabled=enabled, verified=True)
            return outcome.failed("daily_tasks", msg, op=action)

        return outcome.failed(
            "daily_tasks",
            f"Unknown action '{action}'. Valid actions: add, list, complete, remove, clear_completed, clear, set_recipient, send_now, schedule_daily, disable_schedule.",
        )

    except Exception as exc:
        _log(player, f"[tasks] Error: {exc}")
        return outcome.failed("daily_tasks", str(exc), action=action)


# ── Tool & Settings declarations ─────────────────────────────────────────────

PLUGIN = {
    "name": "daily_tasks",
    "description": (
        "Manage today's daily tasks and automatically send WhatsApp updates to the user about what is left today. "
        "Use action='add' to save tasks the user gives you, 'list' to check remaining tasks, "
        "'complete' to mark a task done, 'remove'/'clear_completed'/'clear' to clean up, "
        "'send_now' to WhatsApp the user a briefing of remaining tasks + today's calendar + unread emails, "
        "or 'schedule_daily' with send_time ('HH:MM') and contact to send the WhatsApp update automatically every day."
    ),
    "brief": "Daily tasks & auto-WhatsApp briefing: add, list, complete, send_now, schedule_daily.",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: add | list | complete | remove | clear_completed | clear | set_recipient | send_now | schedule_daily | disable_schedule",
            },
            "task": {
                "type": "STRING",
                "description": "Task text for add, or partial task name for complete/remove",
            },
            "index": {
                "type": "INTEGER",
                "description": "1-based task number from list for complete/remove",
            },
            "due": {
                "type": "STRING",
                "description": "Optional due time (e.g. '17:00') when adding a task",
            },
            "priority": {
                "type": "STRING",
                "description": "'high' or 'normal' (default: normal)",
            },
            "contact": {
                "type": "STRING",
                "description": "Your WhatsApp contact name to receive daily task updates",
            },
            "send_time": {
                "type": "STRING",
                "description": "Daily send time in 24h HH:MM format (e.g. '09:00')",
            },
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": _NS,
    "title": "DAILY TASKS & AUTO-WHATSAPP",
    "fields": [
        {
            "key": "my_whatsapp_contact",
            "label": "Your WhatsApp number or contact name",
            "type": "text",
            "placeholder": "e.g. 9876543210 or Your Name",
        },
        {
            "key": "send_time",
            "label": "Daily send time (24h HH:MM)",
            "type": "text",
            "default": "09:00",
            "placeholder": "09:00",
        },
        {
            "key": "auto_send_enabled",
            "label": "Always send daily WhatsApp briefing automatically",
            "type": "toggle",
            "default": False,
        },
        {
            "key": "include_calendar",
            "label": "Include today's Google Calendar events",
            "type": "toggle",
            "default": True,
        },
        {
            "key": "include_gmail",
            "label": "Include unread Gmail summary",
            "type": "toggle",
            "default": True,
        },
    ],
    "action": {
        "label": "APPLY SCHEDULE & SEND TEST",
        "run": apply_and_test,
    },
}
