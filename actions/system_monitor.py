"""
System Monitor — background metric checks with voice alert support.
Zero subprocess calls on all platforms — uses ctypes/pynvml/psutil/wmi only.
"""
import ctypes
import platform
import threading
import time

import psutil

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

DEFAULT_THRESHOLDS = {
    "cpu":  90.0,
    "ram":  90.0,
    "temp": 85.0,
    "gpu":  95.0,
}

_COOLDOWN   = 300
_CPU_STREAK = 3

# ── NVML DLL cache (Windows: nvml.dll, Linux: libnvidia-ml.so.1) ─────────────
_nvml_lib: object = None
_nvml_ok:  object = None   # None=untested  True=works  False=unavailable
_pynvml_mod: object = None
_pynvml_h: object = None
_pynvml_ok: object = None

# ── Windows PDH cache (Intel / AMD / NVIDIA GPU + ACPI Thermal Zone, non-admin)
_PDH_LOCK = threading.Lock()
_pdh_ok: object = None      # None=untested, True=ready, False=unavailable
_pdh_query: object = None
_pdh_gpu_ctr: object = None
_pdh_tmp_ctr: object = None
_pdh_last_refresh: float = 0.0
_pdh_last_sample_at: float = 0.0
_pdh_cached_sample: tuple[float, float] = (-1.0, -1.0)
_PDH_REFRESH_INTERVAL = 30.0
_PDH_CACHE_TTL = 0.5
_wmi_tz_ok: object = None
_wmi_tz_conn: object = None


def _nvml_gpu() -> float:
    """NVIDIA GPU utilisation via pynvml or nvml.dll — zero subprocess."""
    global _nvml_lib, _nvml_ok, _pynvml_mod, _pynvml_h, _pynvml_ok

    if _pynvml_ok is not False:
        try:
            if _pynvml_h is None:
                import pynvml  # type: ignore
                pynvml.nvmlInit()
                _pynvml_mod = pynvml
                _pynvml_h = pynvml.nvmlDeviceGetHandleByIndex(0)
                _pynvml_ok = True
            return float(_pynvml_mod.nvmlDeviceGetUtilizationRates(_pynvml_h).gpu)
        except Exception:
            _pynvml_ok = False

    if _nvml_ok is False:
        return -1.0
    try:
        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            if _OS == "Windows":
                candidates = ("nvml", r"C:\Windows\System32\nvml.dll")
                _load = ctypes.WinDLL
            else:
                candidates = (
                    "libnvidia-ml.so.1",
                    "libnvidia-ml.so",
                    "libnvidia-ml.dylib",
                )
                _load = ctypes.CDLL
            for name in candidates:
                try:
                    lib = _load(name)
                    if lib.nvmlInit_v2() == 0:
                        _nvml_lib = lib
                        break
                except Exception:
                    continue

        if _nvml_lib is None:
            _nvml_ok = False
            return -1.0

        dev = ctypes.c_void_p()
        if _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev)) != 0:
            _nvml_ok = False
            return -1.0
        u = _Util()
        if _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u)) != 0:
            _nvml_ok = False
            return -1.0
        _nvml_ok = True
        return float(u.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


def _ensure_pdh(force_refresh: bool = False) -> bool:
    """Initialise or periodically refresh Windows PDH GPU/Thermal counters."""
    global _pdh_ok, _pdh_query, _pdh_gpu_ctr, _pdh_tmp_ctr, _pdh_last_refresh
    if _OS != "Windows" or _pdh_ok is False:
        return False

    now = time.monotonic()
    if (
        not force_refresh
        and _pdh_query is not None
        and (now - _pdh_last_refresh) < _PDH_REFRESH_INTERVAL
    ):
        return True

    try:
        import win32pdh  # type: ignore

        if _pdh_query is not None:
            try:
                win32pdh.CloseQuery(_pdh_query)
            except Exception:
                pass
            _pdh_query = None
            _pdh_gpu_ctr = None
            _pdh_tmp_ctr = None

        add_ctr = getattr(win32pdh, "AddEnglishCounter", win32pdh.AddCounter)
        q = win32pdh.OpenQuery()
        gpu_ctr = None
        tmp_ctr = None
        for path, slot in (
            (r"\GPU Engine(*)\Utilization Percentage", "gpu"),
            (r"\Thermal Zone Information(*)\Temperature", "tmp"),
        ):
            try:
                ctr = add_ctr(q, path)
                if slot == "gpu":
                    gpu_ctr = ctr
                else:
                    tmp_ctr = ctr
            except Exception:
                pass

        if gpu_ctr is None and tmp_ctr is None:
            try:
                win32pdh.CloseQuery(q)
            except Exception:
                pass
            _pdh_ok = False
            return False

        win32pdh.CollectQueryData(q)
        time.sleep(0.06)
        _pdh_query = q
        _pdh_gpu_ctr = gpu_ctr
        _pdh_tmp_ctr = tmp_ctr
        _pdh_last_refresh = time.monotonic()
        _pdh_ok = True
        return True
    except Exception:
        _pdh_ok = False
        return False


def _pdh_sample() -> tuple[float, float]:
    """Return (gpu_percent, temp_celsius) via Windows PDH; -1.0 when unavailable."""
    global _pdh_last_sample_at, _pdh_cached_sample
    if _OS != "Windows":
        return -1.0, -1.0
    with _PDH_LOCK:
        now = time.monotonic()
        if (now - _pdh_last_sample_at) < _PDH_CACHE_TTL and _pdh_cached_sample != (-1.0, -1.0):
            return _pdh_cached_sample
        if not _ensure_pdh():
            return -1.0, -1.0
        try:
            import win32pdh  # type: ignore

            try:
                win32pdh.CollectQueryData(_pdh_query)
            except Exception:
                if not _ensure_pdh(force_refresh=True):
                    return -1.0, -1.0
                win32pdh.CollectQueryData(_pdh_query)

            gpu_val = -1.0
            if _pdh_gpu_ctr is not None:
                try:
                    raw_gpu = win32pdh.GetFormattedCounterArray(_pdh_gpu_ctr, win32pdh.PDH_FMT_DOUBLE)
                    if isinstance(raw_gpu, dict) and raw_gpu:
                        by_eng: dict[str, float] = {}
                        for name, val in raw_gpu.items():
                            if val is None or val < 0:
                                continue
                            eng = name.split("engtype_")[-1] if "engtype_" in name else "3D"
                            by_eng[eng] = by_eng.get(eng, 0.0) + float(val)
                        if by_eng:
                            gpu_val = min(100.0, max(0.0, max(by_eng.values())))
                except Exception:
                    pass

            tmp_val = -1.0
            if _pdh_tmp_ctr is not None:
                try:
                    raw_tmp = win32pdh.GetFormattedCounterArray(_pdh_tmp_ctr, win32pdh.PDH_FMT_DOUBLE)
                    if isinstance(raw_tmp, dict) and raw_tmp:
                        c_vals: list[float] = []
                        for val in raw_tmp.values():
                            if val is None:
                                continue
                            v = float(val)
                            if 200.0 < v < 450.0:
                                v -= 273.15
                            if 5.0 <= v <= 125.0:
                                c_vals.append(v)
                        if c_vals:
                            tmp_val = max(c_vals)
                except Exception:
                    pass

            _pdh_cached_sample = (gpu_val, tmp_val)
            _pdh_last_sample_at = time.monotonic()
            return _pdh_cached_sample
        except Exception:
            return -1.0, -1.0


def _get_gpu_usage() -> float:
    nv = _nvml_gpu()
    if nv >= 0:
        return nv
    if _OS == "Windows":
        gpu_val, _ = _pdh_sample()
        if gpu_val >= 0:
            return gpu_val
    return -1.0


def _get_cpu_temp() -> float:
    global _wmi_tz_ok, _wmi_tz_conn
    # psutil — works on Linux; occasionally Windows with driver support
    try:
        temps = psutil.sensors_temperatures()
        for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                     "cpu-thermal", "zenpower", "it8688"]:
            if name in temps and temps[name]:
                return temps[name][0].current
        for entries in temps.values():
            if entries:
                return entries[0].current
    except Exception:
        pass

    if _OS == "Windows":
        _, tmp_val = _pdh_sample()
        if tmp_val >= 0:
            return tmp_val

        # WMI fallback (often requires elevated rights on newer Windows)
        if _wmi_tz_ok is not False:
            try:
                try:
                    import pythoncom  # type: ignore
                    pythoncom.CoInitialize()
                except Exception:
                    pass
                if _wmi_tz_conn is None:
                    import wmi  # type: ignore
                    _wmi_tz_conn = wmi.WMI(namespace="root/wmi")
                tz = _wmi_tz_conn.MSAcpi_ThermalZoneTemperature()
                if tz:
                    _wmi_tz_ok = True
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
                _wmi_tz_ok = False
            except Exception:
                _wmi_tz_ok = False
                _wmi_tz_conn = None

    return -1.0


def get_system_status() -> dict:
    """Snapshot of current system metrics for the system_status tool."""
    cpu  = psutil.cpu_percent(interval=0.2)
    ram  = psutil.virtual_memory()
    temp = _get_cpu_temp()
    gpu  = _get_gpu_usage()

    boot_time   = psutil.boot_time()
    uptime_secs = time.time() - boot_time
    uptime_h    = int(uptime_secs // 3600)
    uptime_m    = int((uptime_secs % 3600) // 60)

    return {
        "cpu_percent":   round(cpu, 1),
        "ram_percent":   round(ram.percent, 1),
        "ram_used_gb":   round(ram.used   / 1024 ** 3, 1),
        "ram_total_gb":  round(ram.total  / 1024 ** 3, 1),
        "cpu_temp_c":    round(temp, 1) if temp > 0 else None,
        "gpu_percent":   round(gpu,  1) if gpu  >= 0 else None,
        "uptime":        f"{uptime_h}h {uptime_m}m",
        "process_count": len(psutil.pids()),
    }


class SystemMonitor:
    """
    Stateful monitor — cooldown state persists across session reconnections.
    Call check() periodically; returns a [SYSTEM_ALERT] string or None.
    """

    def __init__(self, thresholds: dict | None = None):
        self.thresholds   = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._last_alert: dict[str, float] = {}
        self._cpu_streak  = 0

    def _can_alert(self, key: str) -> bool:
        return (time.monotonic() - self._last_alert.get(key, 0)) > _COOLDOWN

    def _record(self, key: str):
        self._last_alert[key] = time.monotonic()

    def check(self) -> str | None:
        try:
            cpu  = psutil.cpu_percent(interval=None)
            ram  = psutil.virtual_memory().percent
            temp = _get_cpu_temp()
            gpu  = _get_gpu_usage()
        except Exception:
            return None

        alerts: list[str] = []

        if cpu >= self.thresholds["cpu"]:
            self._cpu_streak += 1
            if self._cpu_streak >= _CPU_STREAK and self._can_alert("cpu"):
                alerts.append(
                    f"[SYSTEM_ALERT] CPU usage has been critically high ({cpu:.0f}%) "
                    "for several seconds. Warn the user in their language and suggest "
                    "closing heavy applications."
                )
                self._record("cpu")
                self._cpu_streak = 0
        else:
            self._cpu_streak = 0

        if ram >= self.thresholds["ram"] and self._can_alert("ram"):
            alerts.append(
                f"[SYSTEM_ALERT] RAM is at {ram:.0f}% — nearly exhausted. "
                "Warn the user in their language and suggest freeing memory."
            )
            self._record("ram")

        if temp > 0 and temp >= self.thresholds["temp"] and self._can_alert("temp"):
            alerts.append(
                f"[SYSTEM_ALERT] CPU temperature is {temp:.0f}°C — above the safe limit. "
                "Warn the user in their language and advise reducing system load "
                "or checking cooling."
            )
            self._record("temp")

        if gpu >= 0 and gpu >= self.thresholds["gpu"] and self._can_alert("gpu"):
            alerts.append(
                f"[SYSTEM_ALERT] GPU load is at {gpu:.0f}%. "
                "Briefly inform the user in their language."
            )
            self._record("gpu")

        return " ".join(alerts) if alerts else None
