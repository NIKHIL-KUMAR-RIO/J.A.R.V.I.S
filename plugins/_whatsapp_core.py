"""
Shared WhatsApp driver for JARVIS (Mark LVI).

Leading underscore means `core/plugin_loader.py` skips this file as a standalone
tool; instead, `actions/send_message.py`, `plugins/whatsapp_call.py`, and
`plugins/whatsapp_guard.py` import it via:

    from plugins import _whatsapp_core as wa
    transport, why = wa.get()

Supports:
  1. Windows WhatsApp Desktop via UI Automation (`pywinauto` UIA backend)
  2. Cross-platform WhatsApp Web via Playwright persistent profile
     (`config/whatsapp_web/`, git-ignored)

Every action verifies what happened before reporting success:
  - `open_conversation` verifies the active chat header matches the contact
  - `send_message_to` verifies the message composer emptied after Enter
  - `place_call` verifies an outgoing/active call UI appeared
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import threading
import time
import unicodedata
from typing import Any, Optional

from core.lazyimport import LazyModule, installed

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

_PYWINAUTO = installed("pywinauto")
_PLAYWRIGHT = installed("playwright")
_PYAUTOGUI = installed("pyautogui")
_PYPERCLIP = installed("pyperclip")


def _configure_pyautogui(mod: Any) -> None:
    mod.FAILSAFE = True
    mod.PAUSE = 0.0  # _whatsapp_core manages its own explicit settling delays


pyautogui = LazyModule("pyautogui", _configure_pyautogui)
pyperclip = LazyModule("pyperclip")

_DRIVER_LOCK = threading.RLock()
_PUNCT_RE = re.compile(r"[^\w]|_", re.UNICODE)


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _web_profile_dir() -> Path:
    p = _base_dir() / "config" / "whatsapp_web"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _labels_cache_path() -> Path:
    return _base_dir() / "config" / "whatsapp_labels.json"


# ── Contact name normalization & matching ────────────────────────────────────

def fold(text: str) -> str:
    """Reduce a contact name to its canonical form (mirrors core/appidentity.fold)."""
    norm = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(ch for ch in norm if not unicodedata.combining(ch))
    return _PUNCT_RE.sub("", stripped).casefold()


def score_contact_match(query: str, candidate: str) -> int:
    """Return a match score (0 = no match, 100 = exact folded match)."""
    qf = fold(query)
    cf = fold(candidate)
    if not qf or not cf:
        return 0
    if qf == cf:
        return 100
    if cf.startswith(qf) or qf.startswith(cf):
        return 85
    q_tokens = [fold(t) for t in str(query or "").split() if fold(t)]
    c_tokens = [fold(t) for t in str(candidate or "").split() if fold(t)]
    if q_tokens and all(any(qt in ct for ct in c_tokens) for qt in q_tokens):
        return 75
    if qf in cf or (len(cf) >= 3 and cf in qf):
        return 60
    return 0


def contact_matches(query: str, candidate: str) -> bool:
    """Return True only when *candidate* convincingly matches *query*."""
    return score_contact_match(query, candidate) >= 60 or _digits_match(query, candidate)


def _phone_digits(text: str) -> str:
    return re.sub(r"\D", "", str(text or ""))


def _is_phone_query(query: str) -> bool:
    raw = str(query or "").strip()
    return bool(raw and re.fullmatch(r"[\d\s+\-().]+", raw) and len(_phone_digits(raw)) >= 7)


def _digits_match(query: str, candidate: str) -> bool:
    qd = _phone_digits(query)
    cd = _phone_digits(candidate)
    if len(qd) < 7 or len(cd) < 7:
        return False
    return qd == cd or qd.endswith(cd) or cd.endswith(qd)


def _is_self_alias(query: str) -> bool:
    return fold(query) in {"me", "myself", "mynumber", "self", "you", "mywhatsapp"}


def _is_self_text(text: str) -> bool:
    low = str(text or "").lower()
    return "(you)" in low or "message yourself" in low or "(siz)" in low or "kendinize" in low


def _fallback_search_term(query: str) -> str:
    """Extract the bare person-name token when a prefixed query like 'cg piyush'
    returns 0 literal hits in WhatsApp's search box (because contact is saved as '(Cg) Piyush')."""
    tokens = [re.sub(r"^[^\w]+|[^\w]+$", "", t) for t in str(query or "").split()]
    tokens = [t for t in tokens if len(t) >= 2]
    if len(tokens) < 2:
        return ""
    # Prefer the last token (usually the person's name after prefix tags like '(Cg) Piyush')
    return tokens[-1] if len(tokens[-1]) >= 2 else max(tokens, key=len)


# ── Multilingual UI label sets + persistent Gemini cache ─────────────────────

_BUILTIN_LABELS: dict[str, tuple[str, ...]] = {
    "search_box": (
        "search or start a new chat",
        "search or start new chat",
        "search",
        "ara veya yeni sohbet başlat",
        "ara",
        "buscar un chat o iniciar uno nuevo",
        "buscar",
        "rechercher ou démarrer une nouvelle discussion",
        "suchen oder neuen chat beginnen",
        "pesquisar ou começar uma nova conversa",
        "البحث أو بدء دردشة جديدة",
    ),
    "message_box": (
        "type a message",
        "write a message",
        "bir mesaj yazın",
        "mesaj yazın",
        "escribe un mensaje",
        "écrire un message",
        "nachricht eingeben",
        "digite uma mensagem",
        "اكتب رسالة",
    ),
    "voice_call": (
        "voice call",
        "audio call",
        "sesli arama",
        "llamada de voz",
        "appel vocal",
        "sprach­anruf",
        "sprachanruf",
        "ligação de voz",
        "مكالمة صوتية",
    ),
    "video_call": (
        "video call",
        "görüntülü arama",
        "videollamada",
        "appel vidéo",
        "videoanruf",
        "chamada de vídeo",
        "مكالمة فيديو",
    ),
    "end_call": (
        "end call",
        "hang up",
        "leave call",
        "aramayı sonlandır",
        "aramayı bitir",
        "kapat",
        "finalizar llamada",
        "colgar",
        "terminer l'appel",
        "anruf beenden",
        "encerrar chamada",
        "إنهاء المكالمة",
    ),
    "accept_call": (
        "accept",
        "answer",
        "accept call",
        "accept with video",
        "join call",
        "kabul et",
        "yanıtla",
        "cevapla",
        "aceptar",
        "contestar",
        "accepter",
        "répondre",
        "annehmen",
        "aceitar",
        "atender",
        "قبول",
        "رد",
    ),
    "decline_call": (
        "decline",
        "reject",
        "ignore",
        "dismiss",
        "reddet",
        "yoksay",
        "rechazar",
        "colgar",
        "refuser",
        "ablehnen",
        "recusar",
        "رفض",
    ),
}


def _load_cached_labels() -> dict[str, list[str]]:
    p = _labels_cache_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cached_labels(extra: dict[str, list[str]]) -> None:
    p = _labels_cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(extra, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def get_labels(role: str) -> tuple[str, ...]:
    base = list(_BUILTIN_LABELS.get(role, ()))
    cached = _load_cached_labels().get(role) or []
    for item in cached:
        s = str(item or "").strip().lower()
        if s and s not in base:
            base.append(s)
    return tuple(base)


def learn_labels_from_controls(observed_names: list[str]) -> dict[str, list[str]]:
    """Optionally ask core.gemini once to classify unfamiliar UI control labels and cache them."""
    clean = [str(n).strip() for n in observed_names if str(n or "").strip()]
    if not clean:
        return {}
    cache = _load_cached_labels()
    signature = "|".join(sorted(set(clean))[:20])
    if cache.get("_last_sig") == signature:
        return cache

    try:
        from core import gemini

        prompt = (
            "Classify any matching WhatsApp UI button/input labels from this list into JSON keys "
            "(search_box, message_box, voice_call, video_call, end_call, accept_call, decline_call). "
            "Return ONLY a JSON object mapping keys to lists of exact lowercase labels found in the input.\n\n"
            f"Labels: {json.dumps(clean[:40], ensure_ascii=False)}"
        )
        parsed = gemini.as_json(prompt, tier=gemini.FAST, timeout_ms=10_000)
        if isinstance(parsed, dict):
            for role in _BUILTIN_LABELS:
                vals = parsed.get(role)
                if isinstance(vals, list):
                    existing = [str(x).lower() for x in cache.get(role, [])]
                    for v in vals:
                        s = str(v or "").strip().lower()
                        if s and s not in existing:
                            existing.append(s)
                    if existing:
                        cache[role] = existing
            cache["_last_sig"] = signature
            _save_cached_labels(cache)
    except Exception:
        pass
    return cache


def _label_matches(name: str, role: str) -> bool:
    low = (name or "").strip().lower()
    if not low:
        return False
    for pat in get_labels(role):
        if pat == low or pat in low:
            return True
    return False


# ── Base Transport Contract ──────────────────────────────────────────────────

class WhatsAppTransport:
    """Common interface implemented by Desktop and Web WhatsApp drivers."""

    backend_name: str = "base"

    def is_ready(self) -> tuple[bool, str]:
        raise NotImplementedError

    def open_conversation(self, contact: str) -> tuple[bool, str]:
        raise NotImplementedError

    def send_message_to(self, contact: str, text: str) -> tuple[bool, str]:
        raise NotImplementedError

    def place_call(self, contact: str, video: bool = False) -> tuple[bool, str]:
        raise NotImplementedError

    def end_call(self) -> tuple[bool, str]:
        raise NotImplementedError

    def has_active_call(self) -> Optional[dict]:
        raise NotImplementedError

    def answer_call(self, video: bool = False) -> tuple[bool, str]:
        raise NotImplementedError

    def decline_call(self) -> tuple[bool, str]:
        raise NotImplementedError

    def read_last_messages(self, contact: str = "", limit: int = 5) -> tuple[list[str], str]:
        raise NotImplementedError


# ── Windows Desktop Transport (pywinauto UIA) ────────────────────────────────

def _paste_via_clipboard(text: str) -> None:
    if _PYPERCLIP and _PYAUTOGUI:
        pyperclip.copy(text)
        time.sleep(0.08)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.12)
    elif _PYAUTOGUI:
        pyautogui.write(text, interval=0.02)
    else:
        raise RuntimeError("Neither pyperclip nor pyautogui is available for pasting text.")


class DesktopWhatsAppTransport(WhatsAppTransport):
    """Drives WhatsApp Desktop on Windows via UI Automation (pywinauto)."""

    backend_name = "desktop"

    def _desktop(self):
        from pywinauto import Desktop

        return Desktop(backend="uia")

    def _find_windows(self) -> list[Any]:
        try:
            wins = self._desktop().windows(title_re=r".*WhatsApp.*", visible_only=True)
            return list(wins or [])
        except Exception:
            return []

    def _main_window(self) -> Any:
        wins = self._find_windows()
        if not wins:
            return None
        # Prefer the main window (largest or titled "WhatsApp") over call popups
        for w in wins:
            try:
                title = (w.window_text() or "").strip().lower()
                if title == "whatsapp":
                    return w
            except Exception:
                continue
        return wins[0]

    def _launch_desktop(self) -> bool:
        if _OS != "Windows":
            return False
        for cmd in (
            ["cmd", "/c", "start", "", "whatsapp:"],
            ["explorer.exe", r"shell:AppsFolder\5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"],
        ):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline:
                    if self._main_window() is not None:
                        time.sleep(0.6)
                        return True
                    time.sleep(0.4)
            except Exception:
                continue
        return False

    def _ensure_window(self, launch_if_closed: bool = True) -> tuple[Any, str]:
        win = self._main_window()
        if win is None and launch_if_closed:
            self._launch_desktop()
            win = self._main_window()
        if win is None:
            return None, "WhatsApp Desktop window is not open and could not be launched."
        try:
            if win.is_minimized():
                win.restore()
            win.set_focus()
            time.sleep(0.2)
        except Exception:
            pass
        return win, ""

    def _find_control(self, win: Any, role: str, control_types: tuple[str, ...]) -> Any:
        try:
            for ctype in control_types:
                for ctrl in win.descendants(control_type=ctype):
                    try:
                        name = ctrl.window_text() or ""
                        if _label_matches(name, role):
                            return ctrl
                    except Exception:
                        continue
        except Exception:
            pass
        return None

    def _header_matches(
        self,
        win: Any,
        contact: str,
        *,
        allow_self_for_phone: bool = False,
    ) -> tuple[bool, str]:
        """Inspect top-pane text/button elements to verify the opened chat belongs to *contact*."""
        try:
            rect = win.rectangle()
            top_cutoff = rect.top + int((rect.bottom - rect.top) * 0.24)
            left_cutoff = rect.left + int((rect.right - rect.left) * 0.22)
        except Exception:
            top_cutoff = 10**9
            left_cutoff = 0

        candidates: list[str] = []
        try:
            for ctype in ("Text", "Button", "Heading"):
                for ctrl in win.descendants(control_type=ctype):
                    try:
                        r = ctrl.rectangle()
                        if r.top <= top_cutoff and r.left >= left_cutoff:
                            txt = (ctrl.window_text() or "").strip()
                            if txt:
                                candidates.append(txt)
                                if contact_matches(contact, txt):
                                    return True, txt
                    except Exception:
                        continue
        except Exception:
            pass
        joined_header = " ".join(candidates)
        if _is_self_text(joined_header) and (
            _is_self_alias(contact) or (allow_self_for_phone and _is_phone_query(contact))
        ):
            return True, candidates[0] if candidates else "self-chat"
        observed = ", ".join(candidates[:4]) or "unknown conversation"
        return False, observed

    def is_ready(self) -> tuple[bool, str]:
        if _OS != "Windows" or not _PYWINAUTO:
            return False, "Windows UI Automation (pywinauto) is unavailable."
        win = self._main_window()
        if win is None:
            return False, "WhatsApp Desktop is not currently running."
        return True, "WhatsApp Desktop is open and reachable."

    def _listitem_label(self, item: Any) -> str:
        """Extract the primary contact name from a WhatsApp chat ListItem."""
        raw = (item.window_text() or "").strip()
        if raw:
            # Strip trailing preview/timestamp if UIA packs "Name, 12:30 PM, Last msg"
            first_line = raw.splitlines()[0].strip()
            return first_line.split(",", 1)[0].strip() if "," in first_line else first_line
        try:
            for child in item.descendants(control_type="Text"):
                txt = (child.window_text() or "").strip()
                if txt:
                    return txt
        except Exception:
            pass
        return ""

    def _type_search(self, win: Any, term: str) -> None:
        search_box = self._find_control(win, "search_box", ("Edit",))
        if search_box is not None:
            try:
                search_box.click_input()
                time.sleep(0.12)
            except Exception:
                search_box = None

        if search_box is None:
            if not _PYAUTOGUI:
                raise RuntimeError("Could not locate WhatsApp search box.")
            pyautogui.hotkey("ctrl", "f")
            time.sleep(0.2)

        if _PYAUTOGUI:
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.05)
            pyautogui.press("backspace")
            time.sleep(0.05)
        _paste_via_clipboard(term)
        time.sleep(0.75)

    def _collect_matches(self, win: Any, contact: str) -> list[tuple[int, str, Any]]:
        matches: list[tuple[int, str, Any]] = []
        seen_folded: set[str] = set()
        want_self = _is_self_alias(contact) or _is_phone_query(contact)
        for item in win.descendants(control_type="ListItem"):
            try:
                raw_text = (item.window_text() or "").strip()
                name = self._listitem_label(item)
                sc = score_contact_match(contact, name)
                if _digits_match(contact, raw_text) or _digits_match(contact, name):
                    sc = max(sc, 95)
                elif want_self and (_is_self_text(raw_text) or _is_self_text(name)):
                    sc = max(sc, 90)
                key = fold(name or raw_text)
                if sc >= 60 and key and key not in seen_folded:
                    seen_folded.add(key)
                    matches.append((sc, name or raw_text, item))
            except Exception:
                continue
        matches.sort(key=lambda m: m[0], reverse=True)
        return matches

    def open_conversation(self, contact: str) -> tuple[bool, str]:
        contact = (contact or "").strip()
        if not contact:
            return False, "No contact name was provided."

        with _DRIVER_LOCK:
            win, err = self._ensure_window(launch_if_closed=True)
            if win is None:
                return False, err

            # Check if the right chat is already open before searching
            ok_now, current_hdr = self._header_matches(win, contact, allow_self_for_phone=False)
            if ok_now:
                return True, current_hdr

            search_term = "" if _is_self_alias(contact) else contact
            try:
                self._type_search(win, "(You)" if not search_term else search_term)
            except RuntimeError as exc:
                return False, str(exc)

            matches = self._collect_matches(win, contact)
            if not matches:
                # If a prefixed query like 'cg piyush' found nothing literally in WhatsApp
                # because the saved name has punctuation like '(Cg) Piyush', search by name token.
                fb = _fallback_search_term(contact)
                if fb:
                    try:
                        self._type_search(win, fb)
                        matches = self._collect_matches(win, contact)
                    except Exception:
                        matches = []

            clicked = False
            if matches:
                top_score = matches[0][0]
                if len(matches) > 1 and top_score < 100:
                    names = ", ".join(m[1] for m in matches[:4])
                    return False, f"Multiple contacts match '{contact}': {names}. Specify which one you mean."
                try:
                    matches[0][2].click_input()
                    clicked = True
                except Exception:
                    clicked = False

            if not clicked and _PYAUTOGUI:
                pyautogui.press("down")
                time.sleep(0.15)
                pyautogui.press("enter")

            time.sleep(0.55)
            ok_hdr, hdr = self._header_matches(win, contact, allow_self_for_phone=True)
            if not ok_hdr:
                return False, (
                    f"Could not verify conversation for '{contact}' "
                    f"(active header: '{hdr}')."
                )
            return True, hdr

    def _find_message_box(self, win: Any) -> Any:
        """Locate the chat composer across WinUI 3, UWP, and WebView2 WhatsApp Desktop builds."""
        try:
            wr = win.rectangle()
            w_h = max(1, wr.bottom - wr.top)
            w_w = max(1, wr.right - wr.left)
            min_top = wr.top + int(w_h * 0.58)
            min_left = wr.left + int(w_w * 0.20)
            search_cutoff_top = wr.top + int(w_h * 0.35)
        except Exception:
            min_top = 0
            min_left = 0
            search_cutoff_top = 0

        candidates: list[tuple[int, Any]] = []
        try:
            ctrls = win.descendants()
        except Exception:
            ctrls = []
        for ctrl in ctrls:
            try:
                ctype = str(getattr(getattr(ctrl, "element_info", None), "control_type", "") or "")
                name = (ctrl.window_text() or "").strip()
                low = name.lower()
                auto_id = (getattr(getattr(ctrl, "element_info", None), "automation_id", "") or "").lower()
                is_text_input = ctype in ("Edit", "Document") or not ctype
                is_named_input = (
                    _label_matches(name, "message_box")
                    or low.startswith("message ")
                    or any(tok in auto_id for tok in ("inputbar", "chatinput", "messageinput", "composer"))
                )
                if not is_text_input and not is_named_input:
                    continue
                rect = ctrl.rectangle()
                width = rect.right - rect.left
                height = rect.bottom - rect.top
            except Exception:
                continue

            # Never mistake the top search bar for the chat composer
            if search_cutoff_top and rect.bottom <= search_cutoff_top:
                continue
            if width < 80 or height < 14:
                continue

            score = 0
            if _label_matches(name, "message_box") or low.startswith("message "):
                score += 100
            if any(tok in auto_id for tok in ("inputbar", "chatinput", "messageinput", "composer")):
                score += 90
            elif "textbox" in auto_id and rect.top >= min_top:
                score += 60
            if is_text_input and rect.top >= min_top and rect.left >= min_left:
                score += 50 + min(30, rect.bottom // 50)
            if score > 0:
                candidates.append((score, ctrl))

        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates[0][1]

    def _composer_text(self, box: Any) -> str:
        try:
            val = box.get_value()
            if val is not None:
                return str(val)
        except Exception:
            pass
        try:
            txt = str(box.window_text() or "")
            if txt:
                return txt
        except Exception:
            pass
        try:
            parts = [str(t) for t in (box.texts() or []) if t]
            return " ".join(parts).strip()
        except Exception:
            return ""

    def send_message_to(self, contact: str, text: str) -> tuple[bool, str]:
        text = (text or "").strip()
        if not text:
            return False, "Message text cannot be empty."

        with _DRIVER_LOCK:
            ok, info = self.open_conversation(contact)
            if not ok:
                return False, info

            win = self._main_window()
            if win is None:
                return False, "WhatsApp Desktop window disappeared."

            msg_box = self._find_message_box(win)
            if msg_box is None:
                return False, "Could not locate WhatsApp message input box."

            try:
                msg_box.click_input()
            except Exception as exc:
                return False, f"Could not focus WhatsApp message box: {exc}"
            time.sleep(0.12)

            _paste_via_clipboard(text)
            time.sleep(0.15)

            if _PYAUTOGUI:
                pyautogui.press("enter")
            else:
                try:
                    msg_box.type_keys("{ENTER}")
                except Exception as exc:
                    return False, f"Could not press Enter to send: {exc}"

            # Verify the input box emptied after Enter
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                remaining = self._composer_text(msg_box).strip()
                # Ignore placeholder text if UIA exposes placeholder as value
                if not remaining or _label_matches(remaining, "message_box"):
                    return True, ""
                time.sleep(0.15)

            return False, "Message remained in the WhatsApp input box and could not be confirmed sent."

    def place_call(self, contact: str, video: bool = False) -> tuple[bool, str]:
        with _DRIVER_LOCK:
            ok, info = self.open_conversation(contact)
            if not ok:
                return False, info

            win = self._main_window()
            if win is None:
                return False, "WhatsApp Desktop window disappeared."

            role = "video_call" if video else "voice_call"
            btn = self._find_control(win, role, ("Button",))
            if btn is None:
                return False, f"Could not find the {'Video' if video else 'Voice'} call button in WhatsApp."

            try:
                btn.click_input()
            except Exception:
                try:
                    btn.invoke()
                except Exception as exc:
                    return False, f"Could not click the WhatsApp call button: {exc}"

            # Verify a call state/window appeared
            deadline = time.monotonic() + 4.5
            while time.monotonic() < deadline:
                state = self.has_active_call()
                if state is not None:
                    return True, ""
                time.sleep(0.25)

            return False, "Clicked the call button, but no active/ringing WhatsApp call window appeared."

    def has_active_call(self) -> Optional[dict]:
        with _DRIVER_LOCK:
            for win in self._find_windows():
                accept_btn = self._find_control(win, "accept_call", ("Button",))
                decline_btn = self._find_control(win, "decline_call", ("Button",))
                end_btn = self._find_control(win, "end_call", ("Button",))

                if accept_btn is not None or decline_btn is not None:
                    caller = self._extract_caller_name(win)
                    return {"state": "incoming", "caller": caller, "window": win}
                if end_btn is not None:
                    caller = self._extract_caller_name(win)
                    return {"state": "active", "caller": caller, "window": win}
            return None

    def _extract_caller_name(self, win: Any) -> str:
        try:
            title = (win.window_text() or "").strip()
            if title and title.lower() not in ("whatsapp", "whatsapp call"):
                return title
            for ctrl in win.descendants(control_type="Text"):
                txt = (ctrl.window_text() or "").strip()
                if txt and not any(
                    _label_matches(txt, r)
                    for r in ("accept_call", "decline_call", "end_call", "voice_call", "video_call")
                ):
                    return txt
        except Exception:
            pass
        return "Unknown caller"

    def answer_call(self, video: bool = False) -> tuple[bool, str]:
        with _DRIVER_LOCK:
            info = self.has_active_call()
            if not info or info.get("state") != "incoming":
                return False, "There is no incoming WhatsApp call right now."
            win = info["window"]
            btn = self._find_control(win, "accept_call", ("Button",))
            if btn is None:
                return False, "Could not locate the Accept button on the incoming WhatsApp call."
            try:
                btn.click_input()
            except Exception:
                btn.invoke()
            time.sleep(0.5)
            return True, f"Answered call from {info.get('caller', 'Unknown')}."

    def decline_call(self) -> tuple[bool, str]:
        with _DRIVER_LOCK:
            info = self.has_active_call()
            if not info or info.get("state") != "incoming":
                return False, "There is no incoming WhatsApp call right now."
            win = info["window"]
            btn = self._find_control(win, "decline_call", ("Button",))
            if btn is None:
                return False, "Could not locate the Decline button on the incoming WhatsApp call."
            try:
                btn.click_input()
            except Exception:
                btn.invoke()
            time.sleep(0.5)
            return True, f"Declined call from {info.get('caller', 'Unknown')}."

    def end_call(self) -> tuple[bool, str]:
        with _DRIVER_LOCK:
            info = self.has_active_call()
            if not info:
                return False, "There is no active WhatsApp call to end."
            win = info["window"]
            btn = self._find_control(win, "end_call", ("Button",)) or self._find_control(
                win, "decline_call", ("Button",)
            )
            if btn is None:
                return False, "Could not locate the End call button."
            try:
                btn.click_input()
            except Exception:
                btn.invoke()
            time.sleep(0.5)
            return True, "WhatsApp call ended."

    def read_last_messages(self, contact: str = "", limit: int = 5) -> tuple[list[str], str]:
        with _DRIVER_LOCK:
            if contact:
                ok, info = self.open_conversation(contact)
                if not ok:
                    return [], info
            win = self._main_window()
            if win is None:
                return [], "WhatsApp Desktop window is not open."
            lines: list[str] = []
            try:
                for item in win.descendants(control_type="ListItem"):
                    txt = (item.window_text() or "").strip()
                    if txt:
                        lines.append(txt)
            except Exception as exc:
                return [], str(exc)
            return lines[-max(1, min(20, limit)):], ""


# ── Cross-Platform Web Transport (Playwright persistent context) ─────────────

class WebWhatsAppTransport(WhatsAppTransport):
    """Drives web.whatsapp.com via a persistent Playwright browser profile."""

    backend_name = "web"

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="WhatsAppWeb")
        self._thread.start()
        self._ready.wait(timeout=20)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def _run(self, coro: Any, timeout: float = 40.0) -> Any:
        self._ensure_thread()
        if not self._loop:
            raise RuntimeError("WhatsApp Web event loop failed to start.")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    async def _ensure_page(self) -> tuple[Any, str]:
        if not _PLAYWRIGHT:
            return None, "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        from playwright.async_api import async_playwright

        if self._pw is None:
            self._pw = await async_playwright().start()
        if self._context is None:
            profile_dir = str(_web_profile_dir())
            self._context = await self._pw.chromium.launch_persistent_context(
                profile_dir,
                headless=False,
                viewport=None,
                args=["--no-first-run", "--disable-blink-features=AutomationControlled"],
            )
            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
        if self._page.is_closed():
            self._page = await self._context.new_page()
        if "web.whatsapp.com" not in (self._page.url or ""):
            await self._page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1.5)

        # Check whether QR code login is waiting
        qr = await self._page.locator('canvas[aria-label*="Scan" i], div[data-ref]').count()
        if qr > 0:
            return None, "WhatsApp Web needs QR login — scan the QR code in the browser window first."
        return self._page, ""

    def is_ready(self) -> tuple[bool, str]:
        if not _PLAYWRIGHT:
            return False, "Playwright is not installed."
        try:
            return self._run(self._async_is_ready())
        except Exception as exc:
            return False, str(exc)

    async def _async_is_ready(self) -> tuple[bool, str]:
        page, err = await self._ensure_page()
        if page is None:
            return False, err
        return True, "WhatsApp Web session is active."

    def open_conversation(self, contact: str) -> tuple[bool, str]:
        contact = (contact or "").strip()
        if not contact:
            return False, "No contact name was provided."
        with _DRIVER_LOCK:
            try:
                return self._run(self._async_open_conversation(contact))
            except concurrent.futures.TimeoutError:
                return False, "Timed out opening conversation in WhatsApp Web."
            except Exception as exc:
                return False, str(exc)

    async def _async_open_conversation(self, contact: str) -> tuple[bool, str]:
        page, err = await self._ensure_page()
        if page is None:
            return False, err

        search = page.locator('div[contenteditable="true"][data-tab="3"], div[role="textbox"]').first
        if await search.count() == 0:
            return False, "WhatsApp Web search box is not visible yet (is it still loading?)."
        want_self = _is_self_alias(contact) or _is_phone_query(contact)

        async def _query_rows(term: str):
            await search.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await search.type(term, delay=35)
            await asyncio.sleep(0.9)
            rows = page.locator('div[role="listitem"] span[title]')
            count = await rows.count()
            found: list[tuple[int, str, int]] = []
            seen_folded: set[str] = set()
            for idx in range(min(count, 10)):
                title = (await rows.nth(idx).get_attribute("title")) or ""
                sc = score_contact_match(contact, title)
                if _digits_match(contact, title):
                    sc = max(sc, 95)
                elif want_self and _is_self_text(title):
                    sc = max(sc, 90)
                key = fold(title)
                if sc >= 60 and key and key not in seen_folded:
                    seen_folded.add(key)
                    found.append((sc, title, idx))
            found.sort(key=lambda m: m[0], reverse=True)
            return rows, found

        rows, matches = await _query_rows("(You)" if _is_self_alias(contact) else contact)
        if not matches:
            fb = _fallback_search_term(contact)
            if fb:
                rows, matches = await _query_rows(fb)
        if not matches:
            return False, f"No WhatsApp chat matching '{contact}' was found."
        top_score = matches[0][0]
        if len(matches) > 1 and top_score < 100:
            names = ", ".join(m[1] for m in matches[:4])
            return False, f"Multiple contacts match '{contact}': {names}. Specify which one you mean."
        chosen_title = matches[0][1]
        await rows.nth(matches[0][2]).click()

        await asyncio.sleep(0.4)
        header = page.locator("header span[title]").first
        hdr_title = ((await header.get_attribute("title")) or "").strip() if await header.count() else chosen_title
        hdr_full = (await page.locator("header").first.inner_text()) if await page.locator("header").count() else hdr_title
        if not contact_matches(contact, hdr_title) and not (want_self and (_is_self_text(hdr_title) or _is_self_text(hdr_full))):
            return False, f"Opened chat header '{hdr_title}' did not match '{contact}'."
        return True, hdr_title

    def send_message_to(self, contact: str, text: str) -> tuple[bool, str]:
        text = (text or "").strip()
        if not text:
            return False, "Message text cannot be empty."
        with _DRIVER_LOCK:
            try:
                return self._run(self._async_send(contact, text))
            except Exception as exc:
                return False, str(exc)

    async def _async_send(self, contact: str, text: str) -> tuple[bool, str]:
        ok, info = await self._async_open_conversation(contact)
        if not ok:
            return False, info
        page = self._page
        composer = page.locator('footer div[contenteditable="true"][role="textbox"]').last
        if await composer.count() == 0:
            return False, "WhatsApp Web message box not found."
        await composer.click()
        await composer.type(text, delay=20)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.4)
        remaining = (await composer.inner_text()).strip()
        if not remaining:
            return True, ""
        return False, "Message stayed in WhatsApp Web composer after pressing Enter."

    def place_call(self, contact: str, video: bool = False) -> tuple[bool, str]:
        return (
            False,
            "WhatsApp Web in Chromium does not support voice/video calls; install and open WhatsApp Desktop.",
        )

    def end_call(self) -> tuple[bool, str]:
        return False, "No active WhatsApp call on WhatsApp Web."

    def has_active_call(self) -> Optional[dict]:
        return None

    def answer_call(self, video: bool = False) -> tuple[bool, str]:
        return False, "WhatsApp Web cannot answer calls."

    def decline_call(self) -> tuple[bool, str]:
        return False, "WhatsApp Web cannot decline calls."

    def read_last_messages(self, contact: str = "", limit: int = 5) -> tuple[list[str], str]:
        with _DRIVER_LOCK:
            try:
                return self._run(self._async_read_last(contact, limit))
            except Exception as exc:
                return [], str(exc)

    async def _async_read_last(self, contact: str, limit: int) -> tuple[list[str], str]:
        if contact:
            ok, info = await self._async_open_conversation(contact)
            if not ok:
                return [], info
        page = self._page
        msgs = page.locator("div.message-in span.selectable-text, div.message-out span.selectable-text")
        count = await msgs.count()
        start = max(0, count - max(1, min(20, limit)))
        out: list[str] = []
        for idx in range(start, count):
            out.append((await msgs.nth(idx).inner_text()).strip())
        return out, ""


# ── Singleton factory ────────────────────────────────────────────────────────

_desktop_singleton: Optional[DesktopWhatsAppTransport] = None
_web_singleton: Optional[WebWhatsAppTransport] = None


def get(prefer_web: bool = False, launch_if_closed: bool = True) -> tuple[Optional[WhatsAppTransport], str]:
    """Return `(transport, reason)` matching `actions/send_message.py` contract.

    Tries Windows WhatsApp Desktop (pywinauto UIA) first unless `prefer_web=True`,
    falling back to WhatsApp Web (Playwright) when available.
    """
    global _desktop_singleton, _web_singleton

    with _DRIVER_LOCK:
        if not prefer_web and _OS == "Windows" and _PYWINAUTO:
            if _desktop_singleton is None:
                _desktop_singleton = DesktopWhatsAppTransport()
            ready, reason = _desktop_singleton.is_ready()
            if ready:
                return _desktop_singleton, ""
            if launch_if_closed and _desktop_singleton._launch_desktop():
                ready, reason = _desktop_singleton.is_ready()
                if ready:
                    return _desktop_singleton, ""
            desktop_why = reason
        elif not prefer_web and _OS == "Windows":
            desktop_why = "pywinauto is not installed for WhatsApp Desktop automation."
        else:
            desktop_why = "WhatsApp Desktop automation is only supported on Windows."

        if _PLAYWRIGHT:
            if _web_singleton is None:
                _web_singleton = WebWhatsAppTransport()
            return _web_singleton, ""

        return None, f"{desktop_why} WhatsApp Web fallback also unavailable (playwright not installed)."
