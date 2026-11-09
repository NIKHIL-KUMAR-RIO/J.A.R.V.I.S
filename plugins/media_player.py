"""
Spotify + VLC control plugin for JARVIS (drop into plugins/).

No new dependencies - standard library only, no `core.gemini` needed.

HOW EACH APP IS CONTROLLED
    VLC      VLC's built-in HTTP interface. If VLC is closed, this plugin starts it
             with that interface switched on (no settings to touch). If VLC is
             already open WITHOUT it, basic play/next/previous still work on
             Linux and macOS; on Windows you must close VLC and let JARVIS open it.
             Optional env vars: JARVIS_VLC_PORT (8080), JARVIS_VLC_PASSWORD (jarvis).
    Spotify  The desktop app, controlled locally (no login, no API key):
               Windows  media keys + spotify: links
               macOS    AppleScript
               Linux    playerctl if installed, otherwise D-Bus (MPRIS)

KNOWN LIMIT
    "Play <song name>" cannot auto-press play without Spotify's Web API (needs an
    app registration + login). Here it opens Spotify's search for the query, and a
    spotify: link / open.spotify.com link plays or opens directly.
"""

import base64
import csv
import io
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

PLUGIN = {
    "name": "media_player",
    "description": (
        "Controls the Spotify and VLC desktop apps: play, pause, stop, next, "
        "previous, volume up/down/set, what's playing, open a file or URL in VLC, "
        "or open a Spotify search / spotify: link. Use for 'pause Spotify', "
        "'next song', 'play this video in VLC', 'what's playing'. If the user "
        "names no app, use spotify. Do NOT use youtube_video for Spotify or VLC, "
        "and do NOT use computer_settings for the volume of a specific player "
        "(only for overall system volume)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app": {
                "type": "STRING",
                "description": "Which player: 'spotify' or 'vlc'. Defaults to spotify.",
            },
            "action": {
                "type": "STRING",
                "description": (
                    "One of: play, pause, toggle, stop, next, previous, volume_up, "
                    "volume_down, set_volume, now_playing, open, search."
                ),
            },
            "value": {
                "type": "STRING",
                "description": (
                    "Optional. For set_volume: a number 0-100. For VLC open/play: a "
                    "file path or URL. For Spotify search/play: a song or artist to "
                    "search for, or a spotify: / open.spotify.com link."
                ),
            },
        },
        "required": ["action"],
    },
}

_IS_WIN = sys.platform.startswith("win")
_IS_MAC = sys.platform == "darwin"
_STEP = 10  # volume step in percent

_ALIASES = {
    "play": "play", "resume": "play", "start": "play", "unpause": "play",
    "pause": "pause", "hold": "pause",
    "toggle": "toggle", "playpause": "toggle", "play_pause": "toggle",
    "stop": "stop", "halt": "stop",
    "next": "next", "skip": "next", "next_track": "next", "forward": "next",
    "previous": "previous", "prev": "previous", "back": "previous",
    "previous_track": "previous",
    "volume_up": "volume_up", "louder": "volume_up", "up": "volume_up",
    "volume_down": "volume_down", "quieter": "volume_down", "down": "volume_down",
    "set_volume": "set_volume", "volume": "set_volume",
    "now_playing": "now_playing", "current": "now_playing",
    "whats_playing": "now_playing", "what_is_playing": "now_playing",
    "open": "open", "launch": "open", "play_file": "open",
    "search": "search",
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _sh(cmd, timeout=6):
    """Run a command. Returns (returncode, stdout). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except Exception:
        return 1, ""


def _proc_running(name):
    if _IS_WIN:
        _, out = _sh(["tasklist", "/fi", f"IMAGENAME eq {name}.exe", "/nh"])
        return f"{name}.exe".lower() in out.lower()
    rc, _ = _sh(["pgrep", "-ix", name])
    return rc == 0


def _detach(cmd):
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL}
    if _IS_WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)


def _clamp_pct(n):
    return max(0, min(100, int(n)))


def _parse_pct(value):
    digits = re.sub(r"[^\d]", "", value or "")
    return _clamp_pct(digits) if digits else None


def _osa(script):
    return _sh(["osascript", "-e", script])


def _mpris(app, method):
    rc, _ = _sh(["dbus-send", "--print-reply",
                 f"--dest=org.mpris.MediaPlayer2.{app}", "/org/mpris/MediaPlayer2",
                 f"org.mpris.MediaPlayer2.Player.{method}"])
    return rc == 0


# --------------------------------------------------------------------------- #
# VLC
# --------------------------------------------------------------------------- #
def _vlc_port():
    try:
        return int(os.environ.get("JARVIS_VLC_PORT", "8080"))
    except ValueError:
        return 8080


def _vlc_password():
    return os.environ.get("JARVIS_VLC_PASSWORD", "jarvis")


def _vlc_req(**params):
    url = f"http://127.0.0.1:{_vlc_port()}/requests/status.xml"
    if params:
        url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    req = urllib.request.Request(url)
    token = base64.b64encode((":" + _vlc_password()).encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    with urllib.request.urlopen(req, timeout=3) as r:
        return ET.fromstring(r.read())


def _vlc_up():
    try:
        _vlc_req()
        return True
    except Exception:
        return False


def _vlc_exe():
    found = shutil.which("vlc")
    if found:
        return found
    candidates = [
        r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        "/Applications/VLC.app/Contents/MacOS/VLC",
    ]
    return next((c for c in candidates if os.path.exists(c)), None)


def _vlc_launch(target=None):
    """Start VLC with its HTTP interface on. True once the interface answers."""
    exe = _vlc_exe()
    if not exe:
        return False
    cmd = [exe, "--extraintf=http", "--http-host=127.0.0.1",
           f"--http-port={_vlc_port()}", f"--http-password={_vlc_password()}"]
    if target:
        cmd.append(target)
    try:
        _detach(cmd)
    except Exception:
        return False
    for _ in range(16):
        time.sleep(0.5)
        if _vlc_up():
            return True
    return False


def _vlc_target(value):
    """File path or URL -> something VLC accepts. None if a path doesn't exist."""
    if "://" in value:
        return value
    p = Path(value).expanduser()
    if not p.exists():
        return None
    return p.resolve().as_uri()


def _vlc_fallback(action):
    """Basic transport on a VLC that has no HTTP interface (Linux/macOS only)."""
    if _IS_MAC:
        verbs = {"play": "play", "pause": "play", "toggle": "play",
                 "stop": "stop", "next": "next", "previous": "previous"}
        if action in verbs:
            rc, _ = _osa(f'tell application "VLC" to {verbs[action]}')
            return rc == 0
    elif not _IS_WIN:
        methods = {"play": "Play", "pause": "Pause", "toggle": "PlayPause",
                   "stop": "Stop", "next": "Next", "previous": "Previous"}
        if action in methods:
            return _mpris("vlc", methods[action])
    return False


def _vlc_describe(root):
    title = root.findtext(".//info[@name='title']") or root.findtext(
        ".//info[@name='filename']")
    artist = root.findtext(".//info[@name='artist']")
    if not title:
        return ""
    return f"{title} by {artist}" if artist else title


def _vlc(action, value):
    if action in ("open", "search") and value:
        target = _vlc_target(value)
        if target is None:
            return f"Sir, I couldn't find '{value}' on this machine."
        if _vlc_up():
            _vlc_req(command="in_play", input=target)
            return "Playing that in VLC, sir."
        if _proc_running("vlc"):
            return ("Sir, VLC is open without remote control. Close it and ask me "
                    "again, and I'll start it the right way.")
        if _vlc_launch(target):
            return "Opening it in VLC, sir."
        return "Sir, I couldn't start VLC. Is it installed?"

    if not _vlc_up():
        running = _proc_running("vlc")
        if not running:
            if action in ("play", "toggle", "open"):
                if _vlc_launch():
                    return "VLC is open, sir. Tell me what to play."
                return "Sir, I couldn't start VLC. Is it installed?"
            return "VLC isn't open, sir."
        if _vlc_fallback(action):
            return "Done, sir."
        return ("Sir, VLC is open without remote control, so I can't do that. "
                "Close it and ask me to open it again, and I'll enable it.")

    root = _vlc_req()
    state = root.findtext("state") or ""

    if action == "open":
        return "VLC is already open, sir."
    if action == "play":
        if state == "playing":
            return "It's already playing, sir."
        _vlc_req(command="pl_pause" if state == "paused" else "pl_play")
        return "Playing, sir."
    if action == "pause":
        if state != "playing":
            return "Nothing is playing in VLC, sir."
        _vlc_req(command="pl_pause")
        return "Paused, sir."
    if action == "toggle":
        _vlc_req(command="pl_pause" if state in ("playing", "paused") else "pl_play")
        return "Done, sir."
    if action == "stop":
        _vlc_req(command="pl_stop")
        return "Stopped, sir."
    if action == "next":
        _vlc_req(command="pl_next")
        return "Next one, sir."
    if action == "previous":
        _vlc_req(command="pl_previous")
        return "Going back, sir."
    if action in ("volume_up", "volume_down", "set_volume"):
        cur = round(int(root.findtext("volume") or 256) / 2.56)
        if action == "set_volume":
            pct = _parse_pct(value)
            if pct is None:
                return "What volume level, sir?"
        else:
            pct = _clamp_pct(cur + (_STEP if action == "volume_up" else -_STEP))
        _vlc_req(command="volume", val=int(pct * 2.56))
        return f"VLC volume at {pct} percent, sir."
    if action == "now_playing":
        what = _vlc_describe(root)
        if state == "playing" and what:
            return f"VLC is playing {what}, sir."
        if state == "paused" and what:
            return f"VLC is paused on {what}, sir."
        return "Nothing is playing in VLC, sir."
    return f"Sir, VLC can't do '{action}'."


# --------------------------------------------------------------------------- #
# Spotify
# --------------------------------------------------------------------------- #
_VK = {"toggle": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}


def _win_key(vk):
    import ctypes
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)


def _win_spotify_title():
    """Spotify's window title is 'Artist - Song' only while a song is playing."""
    _, out = _sh(["tasklist", "/v", "/fi", "IMAGENAME eq Spotify.exe",
                  "/fo", "csv", "/nh"])
    for row in csv.reader(io.StringIO(out)):
        if row:
            title = row[-1].strip()
            if " - " in title and not title.startswith("GDI+"):
                return title
    return ""


def _spotify_uri(value):
    """spotify: URI for a link, else None."""
    if value.startswith("spotify:"):
        return value
    if "open.spotify.com/" in value:
        parts = [p for p in urllib.parse.urlparse(value).path.split("/") if p]
        parts = [p for p in parts if not p.startswith("intl-")]
        if len(parts) >= 2:
            return f"spotify:{parts[0]}:{parts[1]}"
    return None


def _spotify_launch():
    if _IS_WIN:
        os.startfile("spotify:")  # noqa
        return True
    if _IS_MAC:
        return _sh(["open", "-a", "Spotify"])[0] == 0
    exe = shutil.which("spotify")
    if exe:
        _detach([exe])
        return True
    if shutil.which("flatpak"):
        _detach(["flatpak", "run", "com.spotify.Client"])
        return True
    return False


def _spotify_open_uri(uri):
    if _IS_WIN:
        os.startfile(uri)  # noqa
        return True
    if _IS_MAC:
        return _osa(f'open location "{uri}"')[0] == 0
    opener = shutil.which("xdg-open")
    if opener:
        _detach([opener, uri])
        return True
    return False


def _spotify(action, value):
    name = "Spotify" if _IS_MAC else "spotify"
    if not _proc_running("Spotify" if _IS_WIN else name):
        if action in ("play", "toggle", "open", "search"):
            if not _spotify_launch():
                return "Sir, I couldn't start Spotify. Is it installed?"
            time.sleep(4)  # let the app (and its media session) come up
            if action == "open" and not value:
                return "Spotify is open, sir."
        else:
            return "Spotify isn't open, sir."

    if action in ("open", "search"):
        if not value:
            return "Spotify is open, sir." if action == "open" else \
                "What should I search for, sir?"
        uri = _spotify_uri(value)
        if uri:
            if _IS_MAC and uri.split(":")[1] in ("track", "album", "playlist", "artist"):
                _osa(f'tell application "Spotify" to play track "{uri}"')
            else:
                _spotify_open_uri(uri)
            return "Opening that in Spotify, sir."
        _spotify_open_uri("spotify:search:" + urllib.parse.quote(value))
        return "I've opened Spotify's search for that, sir. Press play on the result."

    if _IS_WIN:
        return _spotify_windows(action)
    if _IS_MAC:
        return _spotify_mac(action, value)
    return _spotify_linux(action, value)


def _spotify_windows(action):
    if action in ("volume_up", "volume_down", "set_volume"):
        return ("Sir, I can't reach Spotify's own volume on Windows. "
                "I can change the system volume instead.")
    playing = bool(_win_spotify_title())
    if action == "now_playing":
        title = _win_spotify_title()
        return f"Spotify is playing {title}, sir." if title else \
            "Nothing is playing on Spotify, sir."
    if action == "play":
        if playing:
            return "It's already playing, sir."
        _win_key(_VK["toggle"])
        return "Playing, sir."
    if action == "pause":
        if not playing:
            return "Nothing is playing on Spotify, sir."
        _win_key(_VK["toggle"])
        return "Paused, sir."
    if action == "stop":
        if playing:
            _win_key(_VK["toggle"])
        return "Stopped, sir."
    if action in ("toggle", "next", "previous"):
        _win_key(_VK[action])
        return "Done, sir."
    return f"Sir, Spotify can't do '{action}'."


def _spotify_mac(action, value):
    def tell(cmd):
        return _osa(f'tell application "Spotify" to {cmd}')

    simple = {"play": "play", "pause": "pause", "toggle": "playpause",
              "stop": "pause", "next": "next track", "previous": "previous track"}
    if action in simple:
        tell(simple[action])
        return "Done, sir."
    if action in ("volume_up", "volume_down", "set_volume"):
        if action == "set_volume":
            pct = _parse_pct(value)
            if pct is None:
                return "What volume level, sir?"
        else:
            rc, out = tell("sound volume")
            try:
                cur = int(float(out))
            except ValueError:
                return "Sir, I couldn't read Spotify's volume."
            pct = _clamp_pct(cur + (_STEP if action == "volume_up" else -_STEP))
        tell(f"set sound volume to {pct}")
        return f"Spotify volume at {pct} percent, sir."
    if action == "now_playing":
        rc, out = tell('(name of current track) & " by " & (artist of current track)')
        return f"Spotify is playing {out}, sir." if rc == 0 and out else \
            "Nothing is playing on Spotify, sir."
    return f"Sir, Spotify can't do '{action}'."


def _spotify_linux(action, value):
    pc = shutil.which("playerctl")

    def ctl(*args):
        return _sh(["playerctl", "-p", "spotify", *args])

    if action in ("volume_up", "volume_down", "set_volume", "now_playing") and not pc:
        return "Sir, install playerctl and I can do that on Spotify."

    if action == "now_playing":
        rc, out = ctl("metadata", "--format", "{{title}} by {{artist}}")
        return f"Spotify is playing {out}, sir." if rc == 0 and out else \
            "Nothing is playing on Spotify, sir."
    if action in ("volume_up", "volume_down"):
        ctl("volume", f"{_STEP / 100:.2f}" + ("+" if action == "volume_up" else "-"))
        return "Done, sir."
    if action == "set_volume":
        pct = _parse_pct(value)
        if pct is None:
            return "What volume level, sir?"
        ctl("volume", f"{pct / 100:.2f}")
        return f"Spotify volume at {pct} percent, sir."

    cmds = {"play": ("play", "Play"), "pause": ("pause", "Pause"),
            "toggle": ("play-pause", "PlayPause"), "stop": ("stop", "Stop"),
            "next": ("next", "Next"), "previous": ("previous", "Previous")}
    if action in cmds:
        verb, method = cmds[action]
        ok = (ctl(verb)[0] == 0) if pc else _mpris("spotify", method)
        return "Done, sir." if ok else "Sir, Spotify didn't respond."
    return f"Sir, Spotify can't do '{action}'."


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        raw_app = str(parameters.get("app", "") or "spotify").strip().lower()
        raw_action = str(parameters.get("action", "") or "").strip().lower()
        value = str(parameters.get("value", "") or "").strip()

        if "vlc" in raw_app:
            app = "vlc"
        elif "spot" in raw_app:
            app = "spotify"
        else:
            return f"Sir, I can only control Spotify and VLC, not '{raw_app}'."

        action = _ALIASES.get(raw_action.replace(" ", "_").replace("-", "_"))
        if action is None:
            return f"Sir, I don't know how to '{raw_action}' in {app}."
        if action == "play" and value:
            action = "open" if app == "vlc" else "search"

        msg = _vlc(action, value) if app == "vlc" else _spotify(action, value)
    except Exception as e:
        msg = f"Sir, media_player failed: {e}"

    if player:
        try:
            player.write_log(f"JARVIS: {msg}")
        except Exception:
            pass
    return msg
