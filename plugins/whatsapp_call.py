"""
WhatsApp Call plugin for JARVIS (Mark LVI).

Places and ends WhatsApp voice or video calls using the shared verified driver
in `plugins/_whatsapp_core.py`.

Calling returns `outcome.started(...)` immediately and dials on a background
thread so JARVIS speaks before the phone starts ringing; failures or contact
ambiguity are reported back via `player.request_say(...)`.
"""
from __future__ import annotations

import threading
from typing import Any

from core import confirm, outcome
from memory.config_manager import get_plugin_config


def _load_settings() -> dict:
    cfg = get_plugin_config("whatsapp_call")
    confirm_raw = cfg.get("confirm_before_call", False)
    if isinstance(confirm_raw, str):
        confirm_flag = confirm_raw.strip().lower() not in ("0", "false", "no", "off")
    else:
        confirm_flag = bool(confirm_raw)
    return {"confirm_before_call": confirm_flag}


def _log(player: Any, text: str) -> None:
    if player and hasattr(player, "write_log"):
        try:
            player.write_log(text)
        except Exception:
            pass


def _speak_outcome(player: Any, instruction: str) -> None:
    if player and hasattr(player, "request_say"):
        try:
            player.request_say(instruction)
        except Exception:
            pass


def _dial_and_report(player: Any, contact: str, video: bool) -> None:
    call_kind = "video" if video else "voice"
    try:
        from plugins import _whatsapp_core as wa
    except Exception as exc:
        msg = f"WhatsApp driver is unavailable: {exc}"
        _log(player, f"[call] {msg}")
        _speak_outcome(
            player,
            f"Tell the user in one short sentence in their own language that the WhatsApp {call_kind} call to {contact} could not start: {msg}",
        )
        return

    transport, why = wa.get()
    if transport is None:
        _log(player, f"[call] {why}")
        _speak_outcome(
            player,
            f"Tell the user in one short sentence in their own language that the WhatsApp {call_kind} call to {contact} could not start: {why}",
        )
        return

    ok, err = transport.place_call(contact, video=video)
    if ok:
        # Silence on success: the WhatsApp call window is already on screen.
        _log(player, f"[call] Dialing {contact} ({call_kind})")
        return

    _log(player, f"[call] Failed to call {contact}: {err}")
    if err.startswith("Multiple contacts match"):
        _speak_outcome(
            player,
            f"Tell the user in their own language that multiple WhatsApp contacts matched '{contact}' and ask which one they meant: {err}",
        )
    else:
        _speak_outcome(
            player,
            f"Tell the user in one short sentence in their own language that the WhatsApp {call_kind} call to {contact} did not go through, and state why: {err}",
        )


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters if isinstance(parameters, dict) else {}
    action = str(params.get("action") or "call").strip().lower()
    contact = str(params.get("contact") or "").strip()
    call_type = str(params.get("call_type") or "voice").strip().lower()
    video = call_type in ("video", "vid")

    try:
        if action in ("end", "hangup", "hang_up", "stop"):
            from plugins import _whatsapp_core as wa

            transport, why = wa.get(launch_if_closed=False)
            if transport is None:
                return outcome.failed("whatsapp_call", why, action="end")
            ok, msg = transport.end_call()
            if ok:
                _log(player, "[call] Ended WhatsApp call.")
                return outcome.done("whatsapp_call", action="end", verified=True)
            return outcome.failed("whatsapp_call", msg, action="end")

        if action in ("status", "check"):
            from plugins import _whatsapp_core as wa

            transport, why = wa.get(launch_if_closed=False)
            if transport is None:
                return outcome.failed("whatsapp_call", why, action="status")
            info = transport.has_active_call()
            if not info:
                return outcome.done("whatsapp_call", action="status", active=False)
            return outcome.done(
                "whatsapp_call",
                action="status",
                active=True,
                state=info.get("state", "active"),
                caller=info.get("caller", "Unknown"),
            )

        if action in ("call", "dial", "voice", "video"):
            if action == "video":
                video = True
            if not contact:
                return outcome.ask("whatsapp_call", "Who should I call on WhatsApp?")

            call_label = "video" if video else "voice"
            settings = _load_settings()

            def _start_dial() -> str:
                _log(player, f"[call] Starting WhatsApp {call_label} call to {contact}...")
                threading.Thread(
                    target=_dial_and_report,
                    args=(player, contact, video),
                    daemon=True,
                    name="whatsapp-call",
                ).start()
                return f"Starting WhatsApp {call_label} call to {contact}."

            if settings["confirm_before_call"]:
                return confirm.request(
                    "whatsapp_call",
                    f"Call {contact} on WhatsApp ({call_label})?",
                    f"Contact: {contact}\nType: {call_label} call",
                    _start_dial,
                )

            _start_dial()
            return outcome.started("whatsapp_call", contact=contact, call_type=call_label)

        return outcome.failed(
            "whatsapp_call",
            f"Unknown action '{action}'. Valid actions: call, end, status.",
        )

    except Exception as exc:
        _log(player, f"[call] Error: {exc}")
        return outcome.failed("whatsapp_call", str(exc), action=action)


# ── Tool & Settings declarations (auto-discovered by core/plugin_loader.py) ──

PLUGIN = {
    "name": "whatsapp_call",
    "description": (
        "Place or end WhatsApp voice and video calls on WhatsApp Desktop. "
        "Use action='call' with contact (and optional call_type='voice'|'video') to dial; "
        "this returns status=started immediately and dials in the background — say briefly that "
        "you are calling them, never claim the call connected until it rings. "
        "Use action='end' to hang up an active call, or action='status' to check if a call is active."
    ),
    "brief": "WhatsApp calls: call(contact, call_type), end, status.",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: call | end | status (default: call)",
            },
            "contact": {
                "type": "STRING",
                "description": "Contact name to call (for action='call')",
            },
            "call_type": {
                "type": "STRING",
                "description": "'voice' (default) or 'video'",
            },
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "whatsapp_call",
    "title": "WHATSAPP CALL",
    "fields": [
        {
            "key": "confirm_before_call",
            "label": "Require HUD confirmation before placing a call",
            "type": "toggle",
            "default": False,
        },
    ],
}
