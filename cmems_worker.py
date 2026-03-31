#!/usr/bin/env python3
"""
cmems_worker.py — Pyxis Copernicus Marine Worker  v2.0
=======================================================
Improvements in v2.0:
  #1  Parallel fetch: wave + currents downloaded concurrently via ThreadPoolExecutor
  #2  nearest_ocean_val at module level: shared by both fetch functions
  #3  Partial cache writes: if one fetch fails, old data is preserved
  #4  Exponential backoff: 3 retries (5 / 30 / 120 s) before giving up per cycle
  #5  Freshness check: skips re-fetch if cache is < 50 min old on startup/loop

Fetches two CMEMS datasets every hour:

  1. PHYSICS (currents + SST at 0.5–10m depth)
     cmems_mod_glo_phy_anfc_0.083deg_PT1H-m
     Variables: uo, vo, thetao → spatial grid + vessel-centre vectors

  2. WAVE MODEL
     cmems_mod_glo_wav_anfc_0.083deg_PT3H-i
     Variables: VHM0, VMDR, VTM10, VHM0_SW1, VMDR_SW1, VTM01_SW1, VHM0_WW, VMDR_WW

OUTPUT: /home/icanjumpuddles/manta-comms/currents_grid_cache.json
"""

import os, sys, json, time, math
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

B              = os.environ.get("B", "/home/icanjumpuddles/manta-comms")
CACHE          = os.path.join(B, "currents_grid_cache.json")
TELEM          = os.path.join(B, "sim_telemetry.json")
ENV_FILE       = os.path.join(B, ".env")
INTERVAL       = 3600   # full refresh every 60 minutes
FRESH_SEC      = 3000   # skip re-fetch if cache is < 50 min old
RADIUS_NM      = 5      # current/SST bbox radius
WAVE_RADIUS_NM = 60     # wave model bbox radius

DATASET_CUR    = "cmems_mod_glo_phy_anfc_0.083deg_PT1H-m"
DATASET_WAVE   = "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i"
RETRY_DELAYS   = [5, 30, 120]   # exponential backoff per fetch attempt

# ── Load credentials from .env ───────────────────────────────────────
def load_env():
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"')
    except Exception:
        pass
    return env

# ── Get vessel position ──────────────────────────────────────────────
def get_pos():
    try:
        with open(TELEM) as f:
            d = json.load(f)
        for lk, lok in [("BOAT_LAT", "BOAT_LON"), ("lat", "lon")]:
            la, lo = d.get(lk), d.get(lok)
            if la and lo:
                return float(la), float(lo)
    except Exception:
        pass
    try:
        r = requests.get("https://benfishmanta.duckdns.org/scenario", timeout=5, verify=False)
        if r.status_code == 200:
            d = r.json()
            for lk, lok in [("BOAT_LAT", "BOAT_LON"), ("lat", "lon"), ("pyxis_lat", "pyxis_lon")]:
                la, lo = d.get(lk), d.get(lok)
                if la and lo:
                    return float(str(la).replace("°", "").strip()), float(str(lo).replace("°", "").strip())
    except Exception:
        pass
    return None, None

# ── Convert u/v components → speed (kn) + direction (°) ─────────────
def uv_to_speed_dir(u, v):
    speed     = math.sqrt(u * u + v * v) * 1.944  # m/s → knots
    direction = (math.degrees(math.atan2(u, v)) + 360) % 360
    return speed, direction

# ── Module-level nearest-ocean helper (#2) ───────────────────────────
def nearest_ocean_val(arr2d, lat_vals, lon_vals, vlat, vlon, search=40):
    """Return value at nearest non-NaN ocean grid point to (vlat, vlon).
    Performs a spiral search up to `search` grid squares from the target point.
    Expanded to 40 squares (was 15) to handle near-coast/port NaN cells.
    """
    import numpy as np
    la_i = int(abs(lat_vals - vlat).argmin())
    lo_i = int(abs(lon_vals - vlon).argmin())
    val  = float(arr2d[la_i, lo_i])
    if not np.isnan(val):
        return val
    for r in range(1, search):
        for di in range(-r, r + 1):
            for dj in range(-r, r + 1):
                ii, jj = la_i + di, lo_i + dj
                if 0 <= ii < arr2d.shape[0] and 0 <= jj < arr2d.shape[1]:
                    v2 = float(arr2d[ii, jj])
                    if not np.isnan(v2):
                        return v2
    return float("nan")

# ── Fetch CMEMS currents + SST grid (#4 backoff) ─────────────────────
def fetch_cmems_grid(lat, lon, username, password):
    try:
        import copernicusmarine
        import xarray as xr
        import numpy as np
    except ImportError as e:
        print(f"  [cmems] Missing dependency: {e}")
        return None

    nm_lat  = RADIUS_NM / 60.0
    nm_lon  = RADIUS_NM / (60.0 * math.cos(math.radians(lat)))
    lat_min = round(lat - nm_lat, 3)
    lat_max = round(lat + nm_lat, 3)
    lon_min = round(lon - nm_lon, 3)
    lon_max = round(lon + nm_lon, 3)

    print(f"  [cmems] Fetching {DATASET_CUR} — BBox: {lat_min},{lon_min}→{lat_max},{lon_max}")
    tmp_file = "/tmp/cmems_currents.nc"

    # Exponential backoff retries (#4)
    for attempt, delay in enumerate(RETRY_DELAYS + [None]):
        try:
            copernicusmarine.subset(
                dataset_id=DATASET_CUR,
                variables=["uo", "vo", "thetao"],
                minimum_latitude=lat_min,
                maximum_latitude=lat_max,
                minimum_longitude=lon_min,
                maximum_longitude=lon_max,
                minimum_depth=0.0,
                maximum_depth=10.0,  # Widened from 1.0 — ensures 0.49m+1.54m depth levels included
                username=username,
                password=password,
                output_filename="cmems_currents.nc",
                output_directory="/tmp",
            )
            print(f"  [cmems] Download complete → {tmp_file}")
            break
        except Exception as e:
            if delay is not None:
                print(f"  [cmems] Error (attempt {attempt+1}/{len(RETRY_DELAYS)}): {e} — retry in {delay}s")
                time.sleep(delay)
            else:
                print(f"  [cmems] All retries exhausted: {e}")
                return None

    try:
        ds   = xr.open_dataset(tmp_file)
        # Shallowest depth slice (depth=0 ≈ 0.5m in the 0.5–10m range)
        uo   = ds["uo"].isel(time=-1).isel(depth=0)
        vo   = ds["vo"].isel(time=-1).isel(depth=0)
        sst  = ds["thetao"].isel(time=-1).isel(depth=0) if "thetao" in ds else None
        lats = uo["latitude"].values
        lons = uo["longitude"].values

        points = []
        for i, la in enumerate(lats):
            for j, lo in enumerate(lons):
                u = float(uo.values[i, j])
                v = float(vo.values[i, j])
                if np.isnan(u) or np.isnan(v):
                    continue
                spd, dr = uv_to_speed_dir(u, v)
                sst_val = float(sst.values[i, j]) if sst is not None else None
                if sst_val is not None and np.isnan(sst_val):
                    sst_val = None
                points.append({
                    "lat":      round(float(la), 4),
                    "lon":      round(float(lo), 4),
                    "speed_kn": round(spd, 3),
                    "dir_deg":  round(dr, 1),
                    "sst_c":    round(sst_val, 2) if sst_val is not None else None,
                })
        ds.close()

        # Vessel SST — nearest ocean point (#2 shared helper)
        vessel_sst = None
        if sst is not None:
            v_sst = nearest_ocean_val(sst.values, sst["latitude"].values,
                                      sst["longitude"].values, lat, lon)
            vessel_sst = None if np.isnan(v_sst) else round(v_sst, 2)

        # Vessel current — nearest ocean point (#2 shared helper)
        vessel_curr = None
        u_v = nearest_ocean_val(uo.values, lats, lons, lat, lon)
        v_v = nearest_ocean_val(vo.values, lats, lons, lat, lon)
        if not np.isnan(u_v) and not np.isnan(v_v):
            spd_v, dir_v = uv_to_speed_dir(u_v, v_v)
            vessel_curr  = {"speed_kn": round(spd_v, 3), "dir_deg": round(dir_v, 1)}

        print(f"  [cmems] {len(points)} vectors. SST:{vessel_sst}°C  Curr:{vessel_curr}")
        return points, vessel_sst, vessel_curr
    except Exception as e:
        print(f"  [cmems] Parse error: {e}")
        return None

# ── Fetch CMEMS wave model (#4 backoff) ──────────────────────────────
def fetch_cmems_wave(lat, lon, username, password):
    try:
        import copernicusmarine
        import xarray as xr
        import numpy as np
    except ImportError as e:
        print(f"  [wave] Missing dependency: {e}")
        return None

    nm_lat  = WAVE_RADIUS_NM / 60.0
    nm_lon  = WAVE_RADIUS_NM / (60.0 * math.cos(math.radians(lat)))
    lat_min = round(lat - nm_lat, 3)
    lat_max = round(lat + nm_lat, 3)
    lon_min = round(lon - nm_lon, 3)
    lon_max = round(lon + nm_lon, 3)

    print(f"  [wave] Fetching {DATASET_WAVE}")
    tmp_file = "/tmp/cmems_wave.nc"

    # Exponential backoff retries (#4)
    for attempt, delay in enumerate(RETRY_DELAYS + [None]):
        try:
            copernicusmarine.subset(
                dataset_id=DATASET_WAVE,
                variables=["VHM0", "VMDR", "VTM10", "VHM0_SW1", "VMDR_SW1",
                           "VTM01_SW1", "VHM0_WW", "VMDR_WW"],
                minimum_latitude=lat_min,
                maximum_latitude=lat_max,
                minimum_longitude=lon_min,
                maximum_longitude=lon_max,
                username=username,
                password=password,
                output_filename="cmems_wave.nc",
                output_directory="/tmp",
            )
            print(f"  [wave] Download complete → {tmp_file}")
            break
        except Exception as e:
            if delay is not None:
                print(f"  [wave] Error (attempt {attempt+1}/{len(RETRY_DELAYS)}): {e} — retry in {delay}s")
                time.sleep(delay)
            else:
                print(f"  [wave] All retries exhausted: {e}")
                return None

    try:
        ds  = xr.open_dataset(tmp_file)
        wave = {}
        VAR_MAP = [
            ("VHM0",     "wave_h"),       # Sig wave height (m)
            ("VMDR",     "wave_dir"),      # Mean wave direction (°)
            ("VTM10",    "wave_period"),   # Mean wave period (s)
            ("VHM0_SW1", "swell_h"),       # Primary swell height (m)
            ("VMDR_SW1", "swell_dir"),     # Primary swell direction (°)
            ("VHM0_WW",  "wind_wave_h"),   # Wind wave height (m)
            ("VMDR_WW",  "wind_wave_dir"), # Wind wave direction (°)
        ]
        for var, key in VAR_MAP:
            if var not in ds:
                wave[key] = None
                continue
            arr = ds[var].isel(time=-1)
            # Use module-level nearest_ocean_val (#2) for all wave variables
            val = nearest_ocean_val(arr.values, arr["latitude"].values,
                                    arr["longitude"].values, lat, lon)
            wave[key] = None if np.isnan(val) else round(float(val), 2)
        ds.close()
        print(f"  [wave] Wave data: {wave}")
        return wave
    except Exception as e:
        print(f"  [wave] Parse error: {e}")
        return None

# ── Cache freshness check (#5) ───────────────────────────────────────
def cache_age_seconds():
    """Returns age of the on-disk cache in seconds, or infinity if unreadable."""
    try:
        with open(CACHE) as f:
            d = json.load(f)
        return time.time() - float(d.get("updated", 0))
    except Exception:
        return float("inf")

# ── Load existing cache for partial-write fallback (#3) ──────────────
def _load_cache_field(field, default):
    try:
        with open(CACHE) as f:
            return json.load(f).get(field, default)
    except Exception:
        return default

# ── Write cache ──────────────────────────────────────────────────────
def write_cache(lat, lon, points, wave=None):
    data = {
        "updated":     time.time(),
        "updated_str": time.strftime("%Y-%m-%dT%H:%M UTC"),
        "vessel_lat":  lat,
        "vessel_lon":  lon,
        "count":       len(points),
        "vessel_wave": wave or {},
        "points":      points,
    }
    with open(CACHE, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"  [cmems] Cache written: {len(points)} current pts, wave={wave is not None} → {CACHE}")

    # Write notification to GMDSS cache
    try:
        gmdss_path = os.path.join(B, "gmdss_cache.json")
        gmdss_msgs = []
        if os.path.exists(gmdss_path):
            with open(gmdss_path, "r") as gf:
                gmdss_msgs = json.load(gf)
        
        # Remove old CMEMS notifications to avoid clutter
        gmdss_msgs = [m for m in gmdss_msgs if m.get("id") != "SYS_CMEMS"]
        
        sys_msg = {
            "id": "SYS_CMEMS",
            "type": "SYSTEM_NOTIFY",
            "name": "CMEMS Update",
            "navArea": "SYS",
            "text": f"CMEMS Oceanographic Data Sync Complete. High-resolution wave and current grids updated for current bounding box.\nVector Count: {len(points)}\nTime: {data['updated_str']}.",
            "lat": lat,
            "lon": lon
        }
        gmdss_msgs.insert(0, sys_msg)
        with open(gmdss_path, "w") as gf:
            json.dump(gmdss_msgs[:20], gf)  # Keep last 20 messages
    except Exception as e:
        print(f"  [cmems] Failed to write notification: {e}")

# ── Main loop ────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("Pyxis CMEMS Worker v2.0  (parallel · backoff · partial)")
    print(f"  Cache:     {CACHE}")
    print(f"  Interval:  {INTERVAL}s   Fresh threshold: {FRESH_SEC}s")
    print("=" * 55)

    env      = load_env()
    username = env.get("CMEMS_USER") or os.environ.get("CMEMS_USER", "")
    password = env.get("CMEMS_PASS") or os.environ.get("CMEMS_PASS", "")

    if not username or not password:
        print("ERROR: Set CMEMS_USER and CMEMS_PASS in manta-comms/.env")
        sys.exit(1)

    while True:
        # ── Freshness check (#5) — skip fetch if cache is recent ─────
        age = cache_age_seconds()
        if age < FRESH_SEC:
            wait = FRESH_SEC - age
            print(f"  [cmems] Cache is {age/60:.1f} min old (< {FRESH_SEC/60:.0f} min) "
                  f"— skipping fetch, sleeping {wait/60:.1f} min")
            time.sleep(wait)
            continue

        lat, lon = get_pos()
        if lat is None:
            print("  [cmems] No vessel position — retrying in 60s")
            time.sleep(60)
            continue

        print(f"[{time.strftime('%H:%M:%S')}] Vessel @ {lat:.3f},{lon:.3f}")

        # ── Parallel fetch: currents + wave simultaneously (#1) ───────
        pts, sst, vessel_curr = [], None, None
        wave = None
        grid_ok = False
        wave_ok = False

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="cmems") as ex:
            fut_grid = ex.submit(fetch_cmems_grid, lat, lon, username, password)
            fut_wave = ex.submit(fetch_cmems_wave, lat, lon, username, password)

            for fut in as_completed([fut_grid, fut_wave]):
                try:
                    result = fut.result()
                    if fut is fut_grid:
                        if result:
                            pts, sst, vessel_curr = result
                            grid_ok = True
                            print(f"  [✓] Grid: {len(pts)} pts, SST={sst}, Curr={vessel_curr}")
                        else:
                            print("  [✗] Grid fetch failed this cycle")
                    else:
                        wave = result
                        wave_ok = wave is not None
                        print(f"  [{'✓' if wave_ok else '✗'}] Wave fetch {'OK' if wave_ok else 'failed this cycle'}")
                except Exception as e:
                    print(f"  [!!] Fetch thread exception: {e}")

        # ── Merge vessel current + SST into wave dict ─────────────────
        if wave is not None:
            if sst is not None:
                wave["sst_c"] = sst
            if vessel_curr:
                wave["curr_v"]   = vessel_curr.get("speed_kn")
                wave["curr_dir"] = vessel_curr.get("dir_deg")

        # ── Partial write: preserve old data if one fetch failed (#3) ─
        if not wave_ok:
            wave = _load_cache_field("vessel_wave", {})
            print(f"  [cmems] Wave failed — preserving cached wave data: wave_h={wave.get('wave_h')}")
        if not grid_ok:
            pts = _load_cache_field("points", [])
            print(f"  [cmems] Grid failed — preserving {len(pts)} cached current pts")

        if grid_ok or wave_ok:
            write_cache(lat, lon, pts, wave)
        else:
            print("  [cmems] Both fetches failed — keeping existing cache unchanged")

        print(f"  [cmems] Next full update in {INTERVAL//60} minutes")
        time.sleep(INTERVAL)
