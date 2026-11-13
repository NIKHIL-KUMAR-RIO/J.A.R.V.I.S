"""
Self-Healing CodeAct Agent Engine for J.A.R.V.I.S Mark LVI.

Borrowed from OpenJarvis CodeAct and NativeOpenHands self-repair concepts:
When an action or code execution fails:
1. Intercepts exceptions, tracebacks, and tool execution failures.
2. Identifies failure mode:
   - Dependency errors (ModuleNotFoundError): automatically installs via pip.
   - Parameter errors (invalid types/keys): auto-corrects using Gemini FAST.
   - Path errors: resolves desktop/upload/relative paths.
3. Retries execution up to MAX_HEALING_ATTEMPTS.
4. Returns seamless healed result without crashing or frustrating the user.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

MAX_HEALING_ATTEMPTS = 2


def _classify_error(error_str: str) -> str:
    """Classify the root cause of an action failure."""
    low = (error_str or "").lower()
    if any(k in low for k in ("no module named", "modulenotfounderror", "importerror")):
        return "missing_dependency"
    if any(k in low for k in ("filenotfounderror", "no such file or directory", "path does not exist")):
        return "file_not_found"
    if any(k in low for k in ("typeerror", "unexpected keyword argument", "missing required positional argument")):
        return "parameter_error"
    if any(k in low for k in ("jsondecodeerror", "invalid json")):
        return "json_syntax"
    if any(k in low for k in ("permissionerror", "access is denied")):
        return "permission_denied"
    return "general_runtime_error"


def _extract_missing_module(error_str: str) -> Optional[str]:
    """Extract missing module name from error string."""
    pat = re.compile(r"no module named ['\"]([a-zA-Z0-9_\-\.]+)['\"]", re.IGNORECASE)
    m = pat.search(error_str)
    if m:
        raw = m.group(1).split(".")[0]
        # Common package name mappings
        pkg_map = {
            "cv2": "opencv-python",
            "PIL": "pillow",
            "bs4": "beautifulsoup4",
            "sklearn": "scikit-learn",
            "yaml": "pyyaml",
        }
        return pkg_map.get(raw, raw.replace("_", "-"))
    return None


def _try_pip_install(pkg: str) -> bool:
    """Attempt auto-installing a missing package."""
    if not pkg:
        return False
    print(f"[SelfHealing] 📦 Auto-installing missing dependency: {pkg}...")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg],
            capture_output=True,
            text=True,
            timeout=90,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 0:
            print(f"[SelfHealing] ✓ Successfully installed: {pkg}")
            return True
        print(f"[SelfHealing] ⚠️ pip install failed for {pkg}: {proc.stderr[:180]}")
    except Exception as exc:
        print(f"[SelfHealing] ⚠️ pip install exception: {exc}")
    return False


def _heal_parameters_with_llm(
    action_name: str,
    failed_params: dict,
    error_msg: str,
    handler: Callable,
) -> Optional[dict]:
    """Ask Gemini FAST to repair mismatched or invalid action arguments."""
    try:
        from core import gemini

        sig = inspect.signature(handler)
        valid_params = list(sig.parameters.keys())

        prompt = f"""You are a self-healing AI execution engine.
An action named '{action_name}' failed with the following error:
Error: {error_msg}

Arguments provided:
{json.dumps(failed_params, indent=2)}

Callable signature parameters: {valid_params}

Fix the arguments so they match what the function expects and resolve the error.
Return ONLY valid JSON containing the corrected dictionary of arguments.
No markdown, no explanation, no backticks.
JSON:"""

        resp = gemini.call(prompt, tier=gemini.FAST, timeout_ms=10000)
        if not resp:
            return None
        raw = (resp.text or "").strip()
        raw = re.sub(r"^```[a-zA-Z]*\r?\n?", "", raw)
        raw = re.sub(r"\r?\n?```\s*$", "", raw).strip()
        corrected = json.loads(raw)
        if isinstance(corrected, dict):
            return corrected
    except Exception as exc:
        print(f"[SelfHealing] ⚠️ Parameter repair skipped: {exc}")
    return None


def heal_action(
    action_name: str,
    parameters: dict,
    error_message: str,
    handler: Callable,
    ctx: dict,
    max_attempts: int = MAX_HEALING_ATTEMPTS,
) -> Tuple[bool, str]:
    """
    Attempt to self-heal an action execution failure.

    Returns:
        (healed: bool, result_or_error: str)
    """
    err_type = _classify_error(error_message)
    print(f"[SelfHealing] 🩺 Diagnosed '{action_name}' failure as: {err_type}")

    current_params = dict(parameters or {})

    for attempt in range(1, max_attempts + 1):
        repaired = False

        # Strategy 1: Missing dependency
        if err_type == "missing_dependency":
            missing_pkg = _extract_missing_module(error_message)
            if missing_pkg and _try_pip_install(missing_pkg):
                repaired = True

        # Strategy 2: Parameter repair
        elif err_type in ("parameter_error", "json_syntax"):
            corrected = _heal_parameters_with_llm(action_name, current_params, error_message, handler)
            if corrected and corrected != current_params:
                print(f"[SelfHealing] 🩹 Adjusted parameters: {corrected}")
                current_params = corrected
                repaired = True

        # Strategy 3: File not found recovery
        elif err_type == "file_not_found":
            player = ctx.get("player")
            current_file = getattr(player, "current_file", None) if player else None
            for k in ("file_path", "folder", "path"):
                if k in current_params and not Path(str(current_params[k])).exists():
                    if current_file and Path(str(current_file)).exists():
                        current_params[k] = str(current_file)
                        print(f"[SelfHealing] 🩹 Auto-routed to current file: {current_file}")
                        repaired = True
                        break

        # Fallback: Attempt general parameter correction if not repaired yet
        if not repaired and attempt == 1:
            corrected = _heal_parameters_with_llm(action_name, current_params, error_message, handler)
            if corrected and corrected != current_params:
                current_params = corrected
                repaired = True

        if not repaired:
            break

        # Re-invoke the handler with repaired state
        try:
            print(f"[SelfHealing] 🔄 Re-trying '{action_name}' (Attempt {attempt}/{max_attempts})...")
            # Introspect context keys
            sig = inspect.signature(handler)
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            kwargs = {}
            for k in ("player", "speak", "response", "session_memory"):
                if has_var_kw or k in sig.parameters:
                    kwargs[k] = ctx.get(k)

            res = handler(parameters=current_params, **kwargs)
            res_str = str(res or "Done.")

            # Check if second execution succeeded
            if not any(res_str.lower().startswith(p) for p in ("tool '", "error:", "failed:")):
                print(f"[SelfHealing] ✅ Successfully healed '{action_name}'!")
                return True, res_str
            else:
                error_message = res_str
                err_type = _classify_error(error_message)
        except Exception as retry_exc:
            error_message = str(retry_exc)
            err_type = _classify_error(error_message)

    return False, f"Tool '{action_name}' failed after self-healing attempts: {error_message}"
