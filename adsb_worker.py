#!/usr/bin/env python3
"""
adsb_worker.py  –  Hardened standalone ADS-B poller for Pyxis Manta
===================================================================
Run as a separate systemd service so crashes never affect the proxy.
Reads last_pos.json for vessel position, polls OpenSky Network
(falls back to adsb.fi on rate-limit), writes adsb_cache.json.

Systemd: see adsb-worker.service
"""

import json
import math
import os
import sys
import time
import threading
import logging

import requests

# ── Configuration ─────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE  = os.path.join(BASE_DIR, "adsb_cache.json")
POS_FILE    = os.path.join(BASE_DIR, "last_pos.json")
POLL_SEC    = 300          # 5 minutes between OpenSky polls
RADIUS_DEG  = 3.0          # ≈200nm bounding box half-size
DEFAULT_LAT = -39.5
DEFAULT_LON = 145.0
MAX_ALT_M   = 15000        # ignore stratospheric contacts (>49,000ft)

# Optional OpenSky credentials (free account = 400 req/day authenticated)
OPENSKY_USER = os.environ.get("OPENSKY_USER", "")
OPENSKY_PASS = os.environ.get("OPENSKY_PASS", "")

# ── ICAO prefix → country (abridged) ─────────────────────────────────────────
ICAO_PREFIX = {
    "7C":"Australia","7D":"Australia","7E":"Australia","7F":"Australia",
    "AE":"USA Military","AF":"USA Military","A0":"USA","A8":"USA",
    "40":"United Kingdom","43":"UK Military",
    "3C":"Germany","3D":"Germany","68":"Germany Military",
    "F0":"France","F8":"France","F4":"France Military",
    "4B":"Italy","4C":"Italy",
    "EC":"Spain","34":"Spain",
    "C8":"Canada","C0":"Canada","C4":"Canada Military",
    "86":"China","80":"India","84":"Japan","82":"Japan Military",
    "73":"Russia","74":"Russia Military",
    "B0":"Brazil","B8":"Argentina",
    "8A":"Australia (RAAF)","8B":"Australia (RAN)",
}

def icao_to_country(icao: str) -> str:
    if not icao or len(icao) < 2: return "Unknown"
    return ICAO_PREFIX.get(icao[:2].upper(), ICAO_PREFIX.get(icao[:1].upper(), "Unknown"))

def classify_type(icao: str, callsign: str, cat: int, sq: str) -> str:
    sq = str(sq or "")
    if sq == "7700": return "MAYDAY"
    if sq == "7500": return "HIJACK"
    if sq == "7600": return "RADIO FAIL"
    cat_map = {1:"Light GA",2:"GA Small",3:"GA Large",4:"VHVW",5:"Heavy",
               6:"High Perf",7:"Rotorcraft",10:"Glider",11:"Airship",12:"UAV",13:"Space"}
    if cat in cat_map: return cat_map[cat]
    mil_cs = ("RCH","RHC","CNV","CFC","DUKE","REACH","KNIFE","EAGLE","RAPTOR",
              "VENOM","ANVIL","HAVOC","BOXER","IRON","STEEL","BLADE","GHOST","TALON")
    cs = (callsign or "").strip().upper()
    if any(cs.startswith(p) for p in mil_cs): return "Military"
    try:
        h = int(icao, 16)
        if 0xAE0000 <= h <= 0xAFFFFF: return "US Military"
        if 0x438000 <= h <= 0x43FFFF: return "RAF"
        if 0x3C4000 <= h <= 0x3CFFFF: return "Luftwaffe"
    except: pass
    if cs and len(cs) >= 3 and cs[:3].isalpha(): return "Commercial"
    return "Unknown"

def priority_for(sq: str) -> str:
    sq = str(sq or "")
    if sq in ("7700","7500","7600","7400"): return "EMERGENCY"
    return "ROUTINE"

# ── Position helpers ──────────────────────────────────────────────────────────
def read_vessel_pos():
    """Read last known vessel position, returns (lat, lon)."""
    _auth = ("admin", "manta")   # proxy Basic auth
    _hdrs = {"User-Agent": "PyxisManta/4.1"}

    # Try multiple proxy endpoints in order
    for _url, _keys in [
        ("https://benfishmanta.duckdns.org/scenario",
         [("BOAT_LAT", "BOAT_LON"), ("lat", "lon"), ("pyxis_lat", "pyxis_lon")]),
        ("https://benfishmanta.duckdns.org/sea_state_json",
         [("vessel_lat", "vessel_lon"), ("pyxis_lat", "pyxis_lon")]),
    ]:
        try:
            r = requests.get(_url, auth=_auth, headers=_hdrs, timeout=5, verify=False)
            if r.status_code == 200:
                d = r.json()
                for lk, lok in _keys:
                    la, lo = d.get(lk), d.get(lok)
                    if la is not None and lo is not None:
                        la_s = str(la).replace("\u00b0", "").replace(" deg", "").replace("deg", "").strip()
                        lo_s = str(lo).replace("\u00b0", "").replace(" deg", "").replace("deg", "").strip()
                        try:
                            lat_f, lon_f = float(la_s), float(lo_s)
                            if -90 <= lat_f <= 90 and -180 <= lon_f <= 180:
                                return lat_f, lon_f
                        except ValueError:
                            continue
        except Exception as e:
            logging.warning(f"adsb_worker: pos fetch {_url} error: {e}")

    # Local file fallback
    try:
        if os.path.exists(POS_FILE):
            with open(POS_FILE) as f:
                d = json.load(f)
            lat = float(d.get("lat", DEFAULT_LAT))
            lon = float(d.get("lon", DEFAULT_LON))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
    except Exception as e:
        logging.warning(f"adsb_worker: FILE pos read error: {e}")

    logging.warning(f"adsb_worker: using default position ({DEFAULT_LAT}, {DEFAULT_LON})")
    return DEFAULT_LAT, DEFAULT_LON

def geo_range_bearing(la1, lo1, la2, lo2):
    dLat = (la2 - la1) * 60.0
    dLon = (lo2 - lo1) * 60.0 * math.cos(math.radians(la1))
    dist = math.sqrt(dLat**2 + dLon**2)
    brg  = math.degrees(math.atan2(dLon, dLat))
    brg  = brg if brg >= 0 else brg + 360.0
    return round(dist, 1), round(brg, 1)

# ── OpenSky fetch ─────────────────────────────────────────────────────────────
def fetch_opensky(la, lo) -> list:
    lamin = la - RADIUS_DEG; lamax = la + RADIUS_DEG
    lomin = lo - RADIUS_DEG; lomax = lo + RADIUS_DEG
    url   = (f"https://opensky-network.org/api/states/all"
             f"?lamin={lamin:.3f}&lomin={lomin:.3f}&lamax={lamax:.3f}&lomax={lomax:.3f}")
    kwargs = {"timeout": 20, "headers": {"User-Agent": "PyxisManta/4.1"}}
    if OPENSKY_USER and OPENSKY_PASS:
        kwargs["auth"] = (OPENSKY_USER, OPENSKY_PASS)
    r = requests.get(url, **kwargs)
    if r.status_code == 429:
        raise ConnectionError("OpenSky rate-limited")
    r.raise_for_status()
    data = r.json()
    return data.get("states") or []

def fetch_adsbone(la, lo) -> list:
    """Fallback B: adsb.fi open API (confirmed reachable from GCP)."""
    url = f"https://opendata.adsb.fi/api/v2/lat/{la:.3f}/lon/{lo:.3f}/dist/250"
    r = requests.get(url, timeout=20, headers={"User-Agent": "PyxisManta/4.1"})
    r.raise_for_status()
    data = r.json()
    raw = data.get("aircraft") or data.get("ac") or []
    states = []
    for a in raw:
        lat = a.get("lat"); lon = a.get("lon")
        if lat is None or lon is None: continue
        alt_m = None
        ab = a.get("alt_baro")
        if isinstance(ab, (int, float)): alt_m = float(ab) * 0.3048
        states.append([
            a.get("hex","").lower(),
            (a.get("flight") or a.get("r") or "").strip(),
            None, None, None,
            lon, lat,
            alt_m,
            a.get("alt_baro") == "ground",
            (a.get("gs") or 0) * 0.514444,
            a.get("track") or 0,
            None, None, alt_m,
            a.get("squawk",""),
            False,
            a.get("category", 0),
        ])
    return states

def fetch_airplaneslive(la, lo) -> list:
    """Fallback B: airplanes.live — global community feeder, good AUS/Asia coverage."""
    url = f"https://api.airplanes.live/v2/lat/{la}/lon/{lo}/dist/250"
    r = requests.get(url, timeout=20, headers={"User-Agent": "PyxisManta/4.1"})
    r.raise_for_status()
    data = r.json()
    raw = data.get("ac") or data.get("aircraft") or []
    states = []
    for a in raw:
        lat = a.get("lat"); lon = a.get("lon")
        if lat is None or lon is None: continue
        alt_m = None
        ab = a.get("alt_baro")
        if isinstance(ab, (int, float)): alt_m = float(ab) * 0.3048
        states.append([
            a.get("hex","").lower(),
            (a.get("flight") or a.get("r") or "").strip(),
            None, None, None,
            lon, lat,
            alt_m,
            a.get("alt_baro") == "ground",
            (a.get("gs") or 0) * 0.514444,
            a.get("track") or 0,
            None, None, alt_m,
            a.get("squawk",""),
            False,
            a.get("category", 0),
        ])
    return states

def fetch_adsbfi(la, lo) -> list:
    """Fallback C: adsb.lol v2 (second community aggregator)."""
    url = f"https://api.adsb.lol/v2/lat/{la}/lon/{lo}/dist/250"
    r = requests.get(url, timeout=20, headers={"User-Agent": "PyxisManta/4.1"})
    r.raise_for_status()
    data = r.json()
    raw = data.get("ac") or data.get("aircraft") or []
    states = []
    for a in raw:
        lat = a.get("lat"); lon = a.get("lon")
        if lat is None or lon is None: continue
        alt_m = None
        ab = a.get("alt_baro")
        if isinstance(ab, (int, float)): alt_m = float(ab) * 0.3048
        states.append([
            a.get("hex","").lower(),
            (a.get("flight") or a.get("r") or "").strip(),
            None, None, None,
            lon, lat,
            alt_m,
            a.get("alt_baro") == "ground",
            (a.get("gs") or 0) * 0.514444,
            a.get("track") or 0,
            None, None, alt_m,
            a.get("squawk",""),
            False,
            a.get("category", 0),
        ])
    return states

def parse_states(states, la, lo) -> list:
    """Convert OpenSky state vectors to contact dicts."""
    contacts = []
    for s in states:
        try:
            if not s or len(s) < 17: continue
            icao = (s[0] or "").strip()
            callsign = (s[1] or "").strip()
            # OpenSky: s[6]=lat, s[5]=lon  (confusingly)
            # Handle both OpenSky native and adsb.fi-converted
            s_lat = s[6]; s_lon = s[5]
            if s_lat is None or s_lon is None: continue
            alt_m = s[7] if s[7] is not None else s[13]
            alt_ft = int(float(alt_m) * 3.28084) if alt_m else 0
            if alt_m and float(alt_m) > MAX_ALT_M: continue
            on_ground = bool(s[8])
            if on_ground: continue
            vel_ms = float(s[9] or 0)
            spd_kts = round(vel_ms * 1.94384, 0)
            track = float(s[10] or 0)
            sq = str(s[14] or "")
            try:
                cat = int(s[16]) if s[16] is not None else 0
            except (ValueError, TypeError):
                cat = 0  # adsb.fi returns "A3" style strings — map to 0
            spi = bool(s[15])

            rng, brg = geo_range_bearing(la, lo, s_lat, s_lon)
            country = icao_to_country(icao)
            ac_type = classify_type(icao, callsign, cat, sq)
            pri = priority_for(sq)

            contacts.append({
                "icao":      icao,
                "callsign":  callsign,
                "lat":       s_lat,
                "lon":       s_lon,
                "alt_ft":    alt_ft,
                "spd_kts":   spd_kts,
                "track":     track,
                "squawk":    sq,
                "on_ground": on_ground,
                "spi":       spi,
                "category":  cat,
                "country":   country,
                "type":      ac_type,
                "priority":  pri,
                "range_nm":  rng,
                "bearing":   brg,
            })
        except Exception as ex:
            logging.debug(f"adsb_worker: parse error for {s}: {ex}")
    contacts.sort(key=lambda x: (0 if x["priority"] != "ROUTINE" else 1, x["range_nm"]))
    return contacts

# ── Main poll loop ────────────────────────────────────────────────────────────
def poll_once():
    la, lo = read_vessel_pos()
    logging.info(f"adsb_worker: polling at ({la:.3f},{lo:.3f})")
    states = None
    source = "OpenSky"
    try:
        states = fetch_opensky(la, lo)
    except ConnectionError as e:
        logging.warning(f"adsb_worker: {e} — trying airplanes.live fallback")
        source = "airplanes.live"
        try:
            states = fetch_airplaneslive(la, lo)
        except Exception as e2:
            logging.warning(f"adsb_worker: airplanes.live failed ({e2}) — trying adsb.fi")
            try:
                states = fetch_adsbone(la, lo)   # adsb.fi
                source = "adsb.fi"
            except Exception as e3:
                logging.warning(f"adsb_worker: adsb.fi failed ({e3}) — trying adsb.lol")
                try:
                    states = fetch_adsbfi(la, lo)  # adsb.lol
                    source = "adsb.lol"
                except Exception as e4:
                    logging.error(f"adsb_worker: all fallbacks failed: {e4}")
    except Exception as e:
        logging.error(f"adsb_worker: OpenSky error: {e}")
        for _fn, _name in [(fetch_airplaneslive, "airplanes.live"), (fetch_adsbone, "adsb.fi"), (fetch_adsbfi, "adsb.lol")]:
            try:
                states = _fn(la, lo); source = _name; break
            except: pass

    if states is None:
        logging.error("adsb_worker: both sources failed this cycle, keeping stale cache")
        return

    contacts = parse_states(states, la, lo)
    if not contacts:
        logging.warning(f"adsb_worker: 0 contacts parsed from {len(states)} states — stale cache preserved")
        return   # Never overwrite good cache with empty list
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(contacts, f)
    os.replace(tmp, CACHE_FILE)
    logging.info(f"adsb_worker: {len(contacts)} contacts written (source={source})")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s adsb_worker %(levelname)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    logging.info("adsb_worker starting")
    while True:
        try:
            poll_once()
        except Exception as e:
            logging.error(f"adsb_worker: unhandled error in poll_once: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
