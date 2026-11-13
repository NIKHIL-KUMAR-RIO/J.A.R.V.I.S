"""
Aviation Weather Plugin for JARVIS (Mark LVI).

Retrieves live, real-time METAR (current observation) and TAF (terminal forecast)
weather reports from AviationWeather.gov's public API. No API key or account
is required.

Supports:
- 4-letter ICAO codes (KJFK, EGLL, VIDP, LFPG, EDDF, OMDB, etc.)
- 3-letter IATA codes (JFK, LHR, DEL, DXB, CDG, FRA, LAX, SFO, etc.)
- Common airport and city names ("Heathrow", "Kennedy", "Dubai", "Tokyo Haneda", etc.)
- Flight categories: VFR (Visual), MVFR (Marginal VFR), IFR (Instrument), LIFR (Low IFR)
- Decoded winds, visibility, ceiling, temperature, dewpoint, pressure, and raw strings.
"""
from __future__ import annotations

import datetime
import math
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from core import outcome

_API_BASE = "https://aviationweather.gov/api/data"
_DEFAULT_TIMEOUT = 12.0

# ── Airport / Station Resolver (IATA / City / Name -> ICAO) ─────────────────
# Comprehensive lookup for major global airports to make voice commands seamless
_AIRPORT_RESOLVER: Dict[str, str] = {
    # US Major
    "JFK": "KJFK", "KENNEDY": "KJFK", "NEW YORK": "KJFK",
    "LGA": "KLGA", "LAGUARDIA": "KLGA",
    "EWR": "KEWR", "NEWARK": "KEWR",
    "LAX": "KLAX", "LOS ANGELES": "KLAX",
    "SFO": "KSFO", "SAN FRANCISCO": "KSFO",
    "ORD": "KORD", "OHARE": "KORD", "CHICAGO": "KORD",
    "MDW": "KMDW", "MIDWAY": "KMDW",
    "ATL": "KATL", "ATLANTA": "KATL",
    "DFW": "KDFW", "DALLAS": "KDFW", "FORT WORTH": "KDFW",
    "DEN": "KDEN", "DENVER": "KDEN",
    "MIA": "KMIA", "MIAMI": "KMIA",
    "SEA": "KSEA", "SEATTLE": "KSEA", "TACOMA": "KSEA",
    "BOS": "KBOS", "BOSTON": "KBOS", "LOGAN": "KBOS",
    "PHX": "KPHX", "PHOENIX": "KPHX",
    "IAH": "KIAH", "HOUSTON": "KIAH",
    "LAS": "KLAS", "VEGAS": "KLAS", "LAS VEGAS": "KLAS",
    "MCO": "KMCO", "ORLANDO": "KMCO",
    "CLT": "KCLT", "CHARLOTTE": "KCLT",
    "MSP": "KMSP", "MINNEAPOLIS": "KMSP",
    "DTW": "KDTW", "DETROIT": "KDTW",
    "PHL": "KPHL", "PHILADELPHIA": "KPHL",
    "SLC": "KSLC", "SALT LAKE": "KSLC", "SALT LAKE CITY": "KSLC",
    "SAN": "KSAN", "SAN DIEGO": "KSAN",
    "IAD": "KIAD", "DULLES": "KIAD", "WASHINGTON DULLES": "KIAD",
    "DCA": "KDCA", "REAGAN": "KDCA", "NATIONAL": "KDCA",
    "BWI": "KBWI", "BALTIMORE": "KBWI",
    "TPA": "KTPA", "TAMPA": "KTPA",
    "PDX": "KPDX", "PORTLAND": "KPDX",
    "AUS": "KAUS", "AUSTIN": "KAUS",
    "BNA": "KBNA", "NASHVILLE": "KBNA",
    "STL": "KSTL", "ST LOUIS": "KSTL",
    "HNL": "PHNL", "HONOLULU": "PHNL", "HAWAII": "PHNL",
    "ANC": "PANC", "ANCHORAGE": "PANC", "ALASKA": "PANC",

    # UK & Europe
    "LHR": "EGLL", "HEATHROW": "EGLL", "LONDON HEATHROW": "EGLL",
    "LGW": "EGKK", "GATWICK": "EGKK", "LONDON GATWICK": "EGKK",
    "STN": "EGSS", "STANSTED": "EGSS",
    "MAN": "EGCC", "MANCHESTER": "EGCC",
    "EDI": "EGPH", "EDINBURGH": "EGPH",
    "CDG": "LFPG", "CHARLES DE GAULLE": "LFPG", "PARIS": "LFPG",
    "ORY": "LFPO", "ORLY": "LFPO",
    "FRA": "EDDF", "FRANKFURT": "EDDF",
    "MUC": "EDDM", "MUNICH": "EDDM",
    "BER": "EDDB", "BERLIN": "EDDB",
    "AMS": "EHAM", "SCHIPHOL": "EHAM", "AMSTERDAM": "EHAM",
    "MAD": "LEMD", "MADRID": "LEMD", "BARAJAS": "LEMD",
    "BCN": "LEBL", "BARCELONA": "LEBL",
    "FCO": "LIRF", "FIUMICINO": "LIRF", "ROME": "LIRF",
    "ZRH": "LSZH", "ZURICH": "LSZH",
    "VIE": "LOWW", "VIENNA": "LOWW",
    "BRU": "EBBR", "BRUSSELS": "EBBR",
    "DUB": "EIDW", "DUBLIN": "EIDW",

    # Middle East & India & Asia
    "DXB": "OMDB", "DUBAI": "OMDB",
    "AUH": "OMAA", "ABU DHABI": "OMAA",
    "DOH": "OTHH", "DOHA": "OTHH", "HAMAD": "OTHH",
    "DEL": "VIDP", "DELHI": "VIDP", "NEW DELHI": "VIDP", "INDIRA GANDHI": "VIDP",
    "BOM": "VABB", "MUMBAI": "VABB", "BOMBAY": "VABB",
    "BLR": "VOBL", "BANGALORE": "VOBL", "BENGALURU": "VOBL",
    "SIN": "WSSS", "CHANGI": "WSSS", "SINGAPORE": "WSSS",
    "HKG": "VHHH", "HONG KONG": "VHHH",
    "HND": "RJTT", "HANEDA": "RJTT", "TOKYO HANEDA": "RJTT",
    "NRT": "RJAA", "NARITA": "RJAA", "TOKYO NARITA": "RJAA",
    "KIX": "RJBB", "KANSAI": "RJBB", "OSAKA": "RJBB",
    "ICN": "RKSI", "INCHEON": "RKSI", "SEOUL": "RKSI",
    "BKK": "VTBS", "BANGKOK": "VTBS", "SUVARNABHUMI": "VTBS",

    # Australasia & Americas
    "SYD": "YSSY", "SYDNEY": "YSSY",
    "MEL": "YMML", "MELBOURNE": "YMML",
    "BNE": "YBBN", "BRISBANE": "YBBN",
    "AKL": "NZAA", "AUCKLAND": "NZAA",
    "YYZ": "CYYZ", "TORONTO": "CYYZ", "PEARSON": "CYYZ",
    "YVR": "CYVR", "VANCOUVER": "CYVR",
    "YUL": "CYUL", "MONTREAL": "CYUL",
    "MEX": "MMMX", "MEXICO CITY": "MMMX",
    "GRU": "SBGR", "SAO PAULO": "SBGR",
}


def resolve_station_code(query: str) -> str:
    """Normalize input station into an upper-case 4-letter ICAO code."""
    cleaned = query.strip().upper()
    # Direct 4-letter ICAO check (e.g. KJFK, EGLL, VIDP)
    if re.fullmatch(r"[A-Z]{4}", cleaned):
        return cleaned

    # Check airport resolver table
    normalized_name = re.sub(r"[^A-Z0-9\s]", "", cleaned)
    if normalized_name in _AIRPORT_RESOLVER:
        return _AIRPORT_RESOLVER[normalized_name]

    # Check 3-letter IATA code: in US, prepend 'K'
    if re.fullmatch(r"[A-Z]{3}", cleaned):
        # Check resolver
        if cleaned in _AIRPORT_RESOLVER:
            return _AIRPORT_RESOLVER[cleaned]
        # Common US convention
        return f"K{cleaned}"

    # Search partial word matches in resolver
    for name, icao in _AIRPORT_RESOLVER.items():
        if name in normalized_name or normalized_name in name:
            return icao

    return cleaned


# ── Flight Category Badging ──────────────────────────────────────────────────

_FLIGHT_CAT_INFO = {
    "VFR": ("🟢 VFR (Visual Flight Rules)", "Ceiling > 3,000 ft and Visibility > 5 miles"),
    "MVFR": ("🔵 MVFR (Marginal VFR)", "Ceiling 1,000–3,000 ft and/or Visibility 3–5 miles"),
    "IFR": ("🔴 IFR (Instrument Flight Rules)", "Ceiling 500–<1,000 ft and/or Visibility 1–<3 miles"),
    "LIFR": ("🟣 LIFR (Low IFR)", "Ceiling < 500 ft and/or Visibility < 1 mile"),
}


# ── Weather Codes & Cloud Decoder ────────────────────────────────────────────

_WX_DESCRIPTIONS = {
    "TS": "Thunderstorm", "SH": "Showers", "FZ": "Freezing",
    "DZ": "Drizzle", "RA": "Rain", "SN": "Snow", "SG": "Snow Grains",
    "IC": "Ice Crystals", "PL": "Ice Pellets", "GR": "Hail", "GS": "Small Hail",
    "BR": "Mist", "FG": "Fog", "FU": "Smoke", "VA": "Volcanic Ash",
    "DU": "Dust", "SA": "Sand", "HZ": "Haze", "PY": "Spray",
    "SQ": "Squall", "FC": "Funnel Cloud/Tornado", "SS": "Sandstorm",
    "+": "Heavy ", "-": "Light ", "VC": "In the Vicinity: ",
}


def decode_wx_string(wx_code: str) -> str:
    """Decode aviation METAR weather phenomena code e.g. -TSRA BR."""
    if not wx_code or wx_code in ("NSW", "NONE"):
        return "None reported"
    tokens = wx_code.split()
    decoded = []
    for tok in tokens:
        desc = tok
        prefix = ""
        if tok.startswith("+"):
            prefix = "Heavy "
            tok = tok[1:]
        elif tok.startswith("-"):
            prefix = "Light "
            tok = tok[1:]
        elif tok.startswith("VC"):
            prefix = "Vicinity "
            tok = tok[2:]

        parts = []
        i = 0
        while i < len(tok):
            chunk = tok[i:i+2]
            if chunk in _WX_DESCRIPTIONS:
                parts.append(_WX_DESCRIPTIONS[chunk])
                i += 2
            else:
                parts.append(tok[i:])
                break
        decoded.append(prefix + " ".join(parts))
    return ", ".join(decoded) if decoded else wx_code


def decode_clouds(clouds_list: List[Dict[str, Any]]) -> str:
    """Decode cloud cover layers (FEW, SCT, BKN, OVC)."""
    if not clouds_list:
        return "Clear / None reported"
    layers = []
    cover_names = {
        "SKC": "Sky Clear", "CLR": "Clear", "NSC": "No Significant Clouds",
        "FEW": "Few", "SCT": "Scattered", "BKN": "Broken (Ceiling)", "OVC": "Overcast (Ceiling)",
        "VV": "Vertical Visibility",
    }
    for c in clouds_list:
        cov = c.get("cover", "")
        base = c.get("base")
        c_type = c.get("type", "")
        name = cover_names.get(cov, cov)
        type_str = f" ({c_type})" if c_type else ""
        if base is not None:
            layers.append(f"{name} at {base:,} ft AGL{type_str}")
        else:
            layers.append(f"{name}{type_str}")
    return ", ".join(layers)


# ── Core API Fetchers ────────────────────────────────────────────────────────

def fetch_metar(station_ids: List[str]) -> List[Dict[str, Any]]:
    """Query current METAR reports from AviationWeather.gov."""
    ids_param = ",".join(station_ids)
    url = f"{_API_BASE}/metar"
    try:
        resp = requests.get(
            url,
            params={"ids": ids_param, "format": "json"},
            headers={"User-Agent": "JARVIS-AviationWeather/MarkLVI"},
            timeout=_DEFAULT_TIMEOUT,
        )
        if resp.status_code in (400, 404):
            return []
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code in (400, 404):
            return []
        raise


def fetch_taf(station_ids: List[str]) -> List[Dict[str, Any]]:
    """Query TAF forecast reports from AviationWeather.gov."""
    ids_param = ",".join(station_ids)
    url = f"{_API_BASE}/taf"
    try:
        resp = requests.get(
            url,
            params={"ids": ids_param, "format": "json"},
            headers={"User-Agent": "JARVIS-AviationWeather/MarkLVI"},
            timeout=_DEFAULT_TIMEOUT,
        )
        if resp.status_code in (400, 404):
            return []
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code in (400, 404):
            return []
        raise


# ── Formatters ───────────────────────────────────────────────────────────────

def format_metar_report(item: Dict[str, Any]) -> str:
    """Format single METAR report into Markdown for display."""
    icao = item.get("icaoId", "UNKNOWN")
    name = item.get("name") or icao
    flt_cat = item.get("fltCat") or "UNKNOWN"
    cat_badge, cat_desc = _FLIGHT_CAT_INFO.get(flt_cat, (f"⚪ {flt_cat}", "Unknown"))

    # Temperature
    temp_c = item.get("temp")
    dewp_c = item.get("dewp")
    temp_str = f"{temp_c:.1f}°C ({temp_c * 9/5 + 32:.1f}°F)" if temp_c is not None else "N/A"
    dewp_str = f"{dewp_c:.1f}°C ({dewp_c * 9/5 + 32:.1f}°F)" if dewp_c is not None else "N/A"

    # Wind
    wdir = item.get("wdir")
    wspd = item.get("wspd")
    wgst = item.get("wgst")
    if wdir == 0 and wspd == 0:
        wind_str = "Calm (0 kts)"
    elif wdir is not None and wspd is not None:
        wind_str = f"{wdir:03d}° at {wspd} kts"
        if wgst:
            wind_str += f" (gusting to {wgst} kts)"
    else:
        wind_str = "Variable / Unknown"

    # Visibility
    visib = item.get("visib")
    visib_str = f"{visib} statute miles" if visib is not None else "N/A"

    # Altimeter / Pressure
    altim = item.get("altim")
    if altim is not None:
        inhg = altim * 0.02953
        altim_str = f"{altim:.1f} hPa ({inhg:.2f} inHg)"
    else:
        altim_str = "N/A"

    # Clouds & Weather
    clouds_str = decode_clouds(item.get("clouds", []))
    wx_code = item.get("wxString") or ""
    wx_desc = decode_wx_string(wx_code)

    raw_ob = item.get("rawOb") or "N/A"
    obs_time = item.get("reportTime") or item.get("obsTime") or "Recent"

    lines = [
        f"### ✈️ METAR: `{icao}` — {name}",
        f"**Flight Category:** {cat_badge} *({cat_desc})*",
        "",
        "| Metric | Current Observation |",
        "| :--- | :--- |",
        f"| **Wind** | `{wind_str}` |",
        f"| **Visibility** | `{visib_str}` |",
        f"| **Clouds / Ceiling** | `{clouds_str}` |",
        f"| **Significant Weather** | `{wx_desc}` |",
        f"| **Temperature** | `{temp_str}` |",
        f"| **Dewpoint** | `{dewp_str}` |",
        f"| **Altimeter (QNH)** | `{altim_str}` |",
        f"| **Observation Time** | `{obs_time}` |",
        "",
        "**Raw METAR String:**",
        f"```text\n{raw_ob}\n```",
    ]
    return "\n".join(lines)


def format_taf_report(item: Dict[str, Any]) -> str:
    """Format single TAF report into Markdown for display."""
    icao = item.get("icaoId", "UNKNOWN")
    name = item.get("name") or icao
    raw_taf = item.get("rawTAF") or "N/A"
    fcsts = item.get("fcsts", [])

    lines = [
        f"### 📡 TAF Forecast: `{icao}` — {name}",
        f"**Forecast Periods:** {len(fcsts)} scheduled updates",
        "",
    ]

    if fcsts:
        lines.append("| Valid Period | Wind | Visibility | Clouds | Weather |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for f in fcsts[:6]:
            t_from = f.get("timeFrom")
            t_to = f.get("timeTo")
            time_str = "Scheduled"
            if t_from and t_to:
                dt_f = datetime.datetime.fromtimestamp(t_from, tz=datetime.timezone.utc).strftime("%d %H:%MZ")
                dt_t = datetime.datetime.fromtimestamp(t_to, tz=datetime.timezone.utc).strftime("%d %H:%MZ")
                time_str = f"{dt_f} → {dt_t}"

            wdir = f.get("wdir")
            wspd = f.get("wspd")
            wgst = f.get("wgst")
            if wdir is not None and wspd is not None:
                w_str = f"{wdir:03d}°/{wspd}kt"
                if wgst:
                    w_str += f" G{wgst}kt"
            else:
                w_str = "Var"

            v_str = f"{f.get('visib', '6+')}SM"
            c_str = decode_clouds(f.get("clouds", []))
            wx_str = decode_wx_string(f.get("wxString", ""))

            lines.append(f"| `{time_str}` | `{w_str}` | `{v_str}` | {c_str} | {wx_str} |")
        lines.append("")

    lines.append("**Raw TAF String:**")
    lines.append(f"```text\n{raw_taf}\n```")
    return "\n".join(lines)


def spoken_summary(metar_list: List[Dict[str, Any]]) -> str:
    """Create a concise, spoken natural language summary for JARVIS voice output."""
    if not metar_list:
        return "Sir, I was unable to retrieve aviation weather for the requested station."
    summaries = []
    for it in metar_list:
        icao = it.get("icaoId", "the station")
        name = it.get("name") or icao
        # Extract short airport name
        short_name = name.split(",")[0].replace("Intl", "").strip()
        cat = it.get("fltCat") or "VFR"
        temp = it.get("temp")
        wdir = it.get("wdir")
        wspd = it.get("wspd")
        visib = it.get("visib")
        wx = decode_wx_string(it.get("wxString", ""))

        wind_part = f"winds {wdir} degrees at {wspd} knots" if (wdir and wspd) else "calm winds"
        vis_part = f"visibility {visib} miles" if visib is not None else ""
        temp_part = f"temperature {int(round(temp))} degrees Celsius" if temp is not None else ""
        wx_part = f"with {wx.lower()}" if wx and wx != "None reported" else ""

        details = ", ".join(p for p in (wind_part, vis_part, wx_part, temp_part) if p)
        summaries.append(f"At {short_name} ({icao}), flight conditions are {cat}. {details.capitalize()}.")

    return " ".join(summaries)


# ── Plugin Declaration & Entrypoint ──────────────────────────────────────────

PLUGIN = {
    "name": "aviation_weather",
    "description": (
        "Get live aviation weather reports from AviationWeather.gov without any API keys or accounts. "
        "Retrieves current METAR observations (Flight Category: VFR/MVFR/IFR/LIFR, wind, visibility, "
        "ceiling, altimeter/pressure) and TAF terminal aerodrome forecasts. "
        "Accepts ICAO codes (KJFK, EGLL, VIDP, LFPG, EDDF, OMDB), IATA codes (JFK, LHR, DEL, DXB, LAX, SFO), "
        "or airport/city names ('Heathrow', 'Kennedy', 'Dubai', 'Tokyo Haneda'). "
        "Use when the user asks for 'aviation weather', 'METAR', 'TAF', 'flight conditions at Heathrow', "
        "or 'pilot weather for JFK'."
    ),
    "brief": "Live aviation METAR & TAF weather reports from AviationWeather.gov (VFR/IFR, winds, ceiling, pressure).",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "station": {
                "type": "STRING",
                "description": "Airport name, city, ICAO or IATA code (e.g. 'KJFK', 'Heathrow', 'DEL', 'EGLL', 'Dubai'). Can be multiple comma-separated stations.",
            },
            "action": {
                "type": "STRING",
                "description": "One of: metar | taf | briefing | spoken. Defaults to 'metar'.",
            },
        },
        "required": [],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Execute aviation weather retrieval and reporting."""
    raw_station = (parameters.get("station") or "").strip()
    action = (parameters.get("action") or "metar").strip().lower()

    if not raw_station:
        return outcome.ask("aviation_weather", "Which airport or station code (ICAO/IATA) would you like weather for?")

    # Split comma or space-separated stations
    tokens = [t.strip() for t in re.split(r"[,;]+", raw_station) if t.strip()]
    station_ids = [resolve_station_code(t) for t in tokens]

    if player:
        try:
            player.write_log(f"JARVIS: Contacting AviationWeather.gov for {', '.join(station_ids)}...")
        except Exception:
            pass

    try:
        # 1. Action: TAF only
        if action == "taf":
            taf_list = fetch_taf(station_ids)
            if not taf_list:
                return outcome.failed("aviation_weather", f"No active TAF forecast found for {', '.join(station_ids)}.")
            reports = [format_taf_report(t) for t in taf_list]
            return outcome.untrusted("aviation_weather", "\n\n---\n\n".join(reports))

        # 2. Action: BRIEFING (METAR + TAF combined)
        if action == "briefing":
            metar_list = fetch_metar(station_ids)
            taf_list = fetch_taf(station_ids)
            if not metar_list and not taf_list:
                return outcome.failed("aviation_weather", f"No reports found for {', '.join(station_ids)}.")

            sections = [f"# 🛫 Pre-Flight Weather Briefing: {', '.join(station_ids)}"]
            if metar_list:
                sections.append("## Current Observations (METAR)")
                sections.extend(format_metar_report(m) for m in metar_list)
            if taf_list:
                sections.append("## Terminal Forecasts (TAF)")
                sections.extend(format_taf_report(t) for t in taf_list)

            return outcome.untrusted("aviation_weather", "\n\n".join(sections))

        # 3. Action: METAR (Default) or SPOKEN
        metar_list = fetch_metar(station_ids)
        if not metar_list:
            return outcome.failed("aviation_weather", f"No current METAR observation found for station(s): {', '.join(station_ids)}.")

        if action == "spoken":
            speech = spoken_summary(metar_list)
            return outcome.done("aviation_weather", action="spoken", summary=speech)

        reports = [format_metar_report(m) for m in metar_list]
        return outcome.untrusted("aviation_weather", "\n\n---\n\n".join(reports))

    except requests.exceptions.RequestException as e:
        return outcome.failed("aviation_weather", f"AviationWeather.gov connection error: {e}")
    except Exception as e:
        return outcome.failed("aviation_weather", f"Unexpected aviation weather error: {e}")
