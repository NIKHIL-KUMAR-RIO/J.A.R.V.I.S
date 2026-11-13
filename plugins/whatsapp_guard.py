"""
WhatsApp Guard plugin for JARVIS (Mark LVI).

Answer, decline, or auto-reply to incoming WhatsApp calls, and optionally
watch in the background for incoming WhatsApp calls using `plugins/_whatsapp_core.py`.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Optional

from core import outcome
from memory.config_manager import get_plugin_config, save_plugin_config

_NS = "whatsapp_guard"
_DEFAULT_REPLY = "Sorry, I can't take a call right now. I'll get back to you shortly."
_POLL_INTERVAL = 2.0
_ALERT_COOLDOWN = 25.0

_WATCH_LOCK = threading.Lock()
_watch_thread: Optional[threading.Thread] = None
_watch_stop = threading.Event()
_last_alert_caller = ""
_last_alert_at = 0.0


def _load_settings(overrides: dict | None = None) -> dict:
    cfg = get_plugin_config(_NS)
    if overrides:
        cfg.update(overrides)
    mode = str(cfg.get("guard_mode") or "announce").strip().lower()
    if mode not in ("announce", "auto_decline", "vip_only"):
        mode = "announce"
    watch_raw = cfg.get("watch_enabled", False)
    if isinstance(watch_raw, str):
        watch_flag = watch_raw.strip().lower() not in ("0", "false", "no", "off", "")
    else:
        watch_flag = bool(watch_raw)
    vip_raw = str(cfg.get("vip_contacts") or "").strip()
    vips = [v.strip() for v in vip_raw.split(",") if v.strip()]
    reply = str(cfg.get("auto_reply_text") or _DEFAULT_REPLY).strip() or _DEFAULT_REPLY
    return {
        "guard_mode": mode,
        "vip_contacts": vips,
        "auto_reply_text": reply,
        "watch_enabled": watch_flag,
    }


def _safe_caller(raw: str) -> str:
    cleaned = re.sub(r"[\r\n\x00-\x1f]+", " ", str(raw or "Unknown caller")).strip()
    return cleaned[:80] or "Unknown caller"


def _log(player: Any, text: str) -> None:
    if player and hasattr(player, "write_log"):
        try:
            player.write_log(text)
        except Exception:
            pass


def _speak(player: Any, instruction: str) -> None:
    if player and hasattr(player, "request_say"):
        try:
            player.request_say(instruction)
        except Exception:
            pass


def _is_vip(caller: str, vips: list[str]) -> bool:
    if not caller or not vips:
        return False
    try:
        from plugins import _whatsapp_core as wa

        return any(wa.contact_matches(v, caller) for v in vips)
    except Exception:
        low = caller.lower()
        return any(v.lower() in low for v in vips)


def _handle_incoming_call(transport: Any, caller: str, settings: dict, player: Any) -> None:
    safe = _safe_caller(caller)
    mode = settings["guard_mode"]
    vip = _is_vip(safe, settings["vip_contacts"])

    if mode == "auto_decline" or (mode == "vip_only" and not vip):
        ok_dec, dec_msg = transport.decline_call()
        if ok_dec:
            _log(player, f"[guard] Auto-declined WhatsApp call from {safe}")
            reply = settings["auto_reply_text"]
            if reply and safe != "Unknown caller":
                ok_msg, err_msg = transport.send_message_to(safe, reply)
                if ok_msg:
                    _log(player, f"[guard] Sent auto-reply to {safe}")
                    _speak(
                        player,
                        f"Tell the user briefly in their own language that you declined an incoming WhatsApp call from '{safe}' and sent the auto-reply.",
                    )
                    return
                _log(player, f"[guard] Auto-reply failed for {safe}: {err_msg}")
            _speak(
                player,
                f"Tell the user briefly in their own language that you declined an incoming WhatsApp call from '{safe}'.",
            )
            return
        _log(player, f"[guard] Could not auto-decline call from {safe}: {dec_msg}")

    _log(player, f"[guard] Incoming WhatsApp call from {safe}")
    _speak(
        player,
        f"Tell the user in one short sentence in their own language that '{safe}' is calling on WhatsApp, and ask if they want to answer or decline.",
    )


def _watch_loop(player: Any) -> None:
    global _last_alert_caller, _last_alert_at
    while not _watch_stop.is_set():
        try:
            settings = _load_settings()
            if not settings["watch_enabled"]:
                break
            from plugins import _whatsapp_core as wa

            transport, _ = wa.get(launch_if_closed=False)
            if transport is not None:
                info = transport.has_active_call()
                if info and info.get("state") == "incoming":
                    caller = _safe_caller(info.get("caller", "Unknown caller"))
                    now = time.monotonic()
                    if caller != _last_alert_caller or (now - _last_alert_at) >= _ALERT_COOLDOWN:
                        _last_alert_caller = caller
                        _last_alert_at = now
                        _handle_incoming_call(transport, caller, settings, player)
        except Exception:
            pass
        _watch_stop.wait(_POLL_INTERVAL)


def start_watcher(player: Any = None) -> bool:
    global _watch_thread
    with _WATCH_LOCK:
        if _watch_thread is not None and _watch_thread.is_alive():
            return True
        _watch_stop.clear()
        _watch_thread = threading.Thread(
            target=_watch_loop,
            args=(player,),
            daemon=True,
            name="whatsapp-guard",
        )
        _watch_thread.start()
        return True


def stop_watcher() -> None:
    global _watch_thread
    with _WATCH_LOCK:
        _watch_stop.set()
        _watch_thread = None


# ── Settings UI action callback ──────────────────────────────────────────────

def apply_and_check(values: dict | None = None) -> tuple[bool, str]:
    """Called by Plugin Settings button: saves guard settings, toggles watcher, and checks call status."""
    try:
        if isinstance(values, dict):
            save_plugin_config(_NS, values)
        settings = _load_settings(values)
        if settings["watch_enabled"]:
            start_watcher(None)
        else:
            stop_watcher()

        from plugins import _whatsapp_core as wa

        transport, why = wa.get(launch_if_closed=False)
        if transport is None:
            return True, f"Guard saved (mode={settings['guard_mode']}, watch={'ON' if settings['watch_enabled'] else 'OFF'}). WhatsApp not open ({why})"
        info = transport.has_active_call()
        if info:
            return True, f"Guard saved. Call detected: {info.get('state')} from {_safe_caller(info.get('caller', ''))}"
        return True, f"Guard active (mode={settings['guard_mode']}, watch={'ON' if settings['watch_enabled'] else 'OFF'}). No incoming call right now."
    except Exception as exc:
        return False, str(exc)


def on_startup(player: Any = None) -> None:
    """Auto-start incoming WhatsApp call guard on JARVIS launch when watch_enabled is ON."""
    settings = _load_settings()
    if settings["watch_enabled"]:
        start_watcher(player)
        _log(player, f"[guard] Incoming WhatsApp call guard auto-started ({settings['guard_mode']}).")


# ── Main plugin entry point ──────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters if isinstance(parameters, dict) else {}
    action = str(params.get("action") or "status").strip().lower()
    reply_msg = str(params.get("reply_message") or "").strip()
    video = bool(params.get("video", False))

    try:
        settings = _load_settings()

        if action in ("enable_guard", "enable", "start_watch"):
            save_plugin_config(_NS, {"watch_enabled": True})
            start_watcher(player)
            _log(player, f"[guard] Incoming WhatsApp call guard enabled ({settings['guard_mode']}).")
            return outcome.done(
                "whatsapp_guard",
                op="enable_guard",
                mode=settings["guard_mode"],
                watching=True,
                verified=True,
            )

        if action in ("disable_guard", "disable", "stop_watch"):
            save_plugin_config(_NS, {"watch_enabled": False})
            stop_watcher()
            _log(player, "[guard] Incoming WhatsApp call guard disabled.")
            return outcome.done("whatsapp_guard", op="disable_guard", watching=False, verified=True)

        # Ensure background watcher runs if user previously enabled it in settings
        if settings["watch_enabled"]:
            start_watcher(player)

        from plugins import _whatsapp_core as wa

        transport, why = wa.get(launch_if_closed=False)
        if transport is None:
            return outcome.failed("whatsapp_guard", why, op=action)

        if action in ("status", "check"):
            info = transport.has_active_call()
            if not info:
                return outcome.done(
                    "whatsapp_guard",
                    op="status",
                    incoming=False,
                    watching=settings["watch_enabled"],
                    mode=settings["guard_mode"],
                )
            caller = _safe_caller(info.get("caller", "Unknown"))
            return outcome.done(
                "whatsapp_guard",
                op="status",
                incoming=(info.get("state") == "incoming"),
                state=info.get("state", "active"),
                caller=caller,
                watching=settings["watch_enabled"],
            )

        if action in ("answer", "accept"):
            ok, msg = transport.answer_call(video=video)
            if ok:
                _log(player, f"[guard] {msg}")
                return outcome.done("whatsapp_guard", op="answer", summary=msg, verified=True)
            return outcome.failed("whatsapp_guard", msg, op="answer")

        if action in ("decline", "reject", "auto_reply", "busy"):
            info = transport.has_active_call()
            caller = _safe_caller(info.get("caller", "")) if info else ""
            ok_dec, dec_msg = transport.decline_call()
            if not ok_dec:
                return outcome.failed("whatsapp_guard", dec_msg, op=action)
            _log(player, f"[guard] {dec_msg}")

            text_to_send = reply_msg or (settings["auto_reply_text"] if action in ("auto_reply", "busy") else "")
            if text_to_send and caller and caller != "Unknown caller":
                ok_send, send_err = transport.send_message_to(caller, text_to_send)
                if ok_send:
                    _log(player, f"[guard] Auto-replied to {caller}")
                    return outcome.done(
                        "whatsapp_guard",
                        op=action,
                        caller=caller,
                        replied=True,
                        verified=True,
                    )
                return outcome.unverified(
                    "whatsapp_guard",
                    f"Call from {caller} was declined, but auto-reply failed: {send_err}",
                    op=action,
                    caller=caller,
                    replied=False,
                )

            return outcome.done("whatsapp_guard", op=action, caller=caller or "unknown", replied=False, verified=True)

        return outcome.failed(
            "whatsapp_guard",
            f"Unknown action '{action}'. Valid actions: status, answer, decline, auto_reply, enable_guard, disable_guard.",
        )

    except Exception as exc:
        _log(player, f"[guard] Error: {exc}")
        return outcome.failed("whatsapp_guard", str(exc), op=action)


# ── Tool & Settings declarations ─────────────────────────────────────────────

PLUGIN = {
    "name": "whatsapp_guard",
    "description": (
        "Handle incoming WhatsApp calls: answer, decline, or decline and auto-reply with a message. "
        "Use action='answer' to pick up an incoming call, 'decline' to reject it, "
        "'auto_reply' with optional reply_message to decline and text the caller back, "
        "'status' to check who is calling right now, or 'enable_guard'/'disable_guard' to toggle "
        "background incoming-call monitoring."
    ),
    "brief": "WhatsApp incoming calls: status, answer, decline, auto_reply, enable_guard, disable_guard.",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: status | answer | decline | auto_reply | enable_guard | disable_guard",
            },
            "reply_message": {
                "type": "STRING",
                "description": "Optional custom message to send after declining (for decline/auto_reply)",
            },
            "video": {
                "type": "BOOLEAN",
                "description": "True if answering with video (default: false)",
            },
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": _NS,
    "title": "WHATSAPP CALL GUARD",
    "fields": [
        {
            "key": "guard_mode",
            "label": "Guard mode when watching in background",
            "type": "choice",
            "options": ["announce", "auto_decline", "vip_only"],
            "default": "announce",
        },
        {
            "key": "vip_contacts",
            "label": "VIP contacts (comma-separated; never auto-declined)",
            "type": "text",
            "placeholder": "e.g. Mayank, Kannu",
        },
        {
            "key": "auto_reply_text",
            "label": "Default auto-reply message when declining",
            "type": "text",
            "default": _DEFAULT_REPLY,
            "placeholder": _DEFAULT_REPLY,
        },
        {
            "key": "watch_enabled",
            "label": "Monitor incoming WhatsApp calls in background",
            "type": "toggle",
            "default": False,
        },
    ],
    "action": {
        "label": "APPLY GUARD & CHECK CALL",
        "run": apply_and_check,
    },
}
