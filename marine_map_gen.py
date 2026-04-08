#!/usr/bin/env python3
"""
marine_map_gen.py — Pyxis Marine Sea State Map Generator  v2.1
================================================================
Runs as a long-lived background process on the Pyxis server (GCP VM).
Generates 4 native-resolution sea-state map JPEGs for the Garmin Epix 2 Pro
51mm (454×454 AMOLED), rendered at exact CartoDB tile zooms to avoid upscale.

ARCHITECTURE OVERVIEW
─────────────────────
This process is one component in the Pyxis stack:

  [Scenario Injector / NMEA GPS]
        │  writes BOAT_LAT/BOAT_LON
        ▼
  sim_telemetry.json  ◄──── single source of truth for vessel position
        │                   (also written by geo_cache area resolution below)
        ▼
  marine_map_gen.py  ──── reads position, fetches Open-Meteo marine API,
        │                   renders CartoDB basemap + wave heatmap + arrows
        ▼
  meteo_map_z{1-4}.jpg   ──── served by proxy.py via /seastate/{cb}/z{n}/meteo_map
        │
        ▼
  Garmin watch (SeaStateMapView)  ──── displays the JPEG, zoom via UP/DOWN

POSITION RESOLUTION CHAIN (in priority order)
──────────────────────────────────────────────
1. proxy /sea_state_json API  — real Pyxis GPS when NMEA is live
2. geo_cache.json 'local_area' — scenario injector writes area name (e.g. "Singapore");
   this file resolves it to lat/lon using AREA_COORDS lookup and writes back to
   sim_telemetry.json to keep the whole system in sync
3. sim_telemetry.json BOAT_LAT/BOAT_LON — headless sim fallback
4. Placeholder image — "AWAITING PYXIS POSITION" shown on watch

OUTPUT FILES  (all in BASE = /home/icanjumpuddles/manta-comms/)
──────────────────────────────────────────────────────────────────
  meteo_map_z1.jpg  CartoDB z=7   ±30 nm  (strategic overview)
  meteo_map_z2.jpg  CartoDB z=8   ±15 nm  (regional)
  meteo_map_z3.jpg  CartoDB z=10  ±6  nm  (coastal)
  meteo_map_z4.jpg  CartoDB z=11  ±3  nm  (close-up)
  meteo_map.jpg     copy of z1   (legacy fallback for older watch builds)

LAUNCH
──────
  nohup python3 -u marine_map_gen.py >> marine_map.log 2>&1 &
  (restart_clean.sh handles this automatically)

TIMING
──────
  Regenerates every 600s (INTERVAL).  If vessel drifts >5nm it regenerates early.
  On startup waits up to 90s for a valid position before generating placeholder.

DEPENDENCIES
────────────
  pip install Pillow requests
  (system python3 has these — no venv required)

ADDING A NEW SCENARIO AREA
───────────────────────────
  Add an entry to AREA_COORDS dict:
    "port douglas": (-16.4869, 145.4655),

MAINTAINER NOTES (2026)
────────────────────────
  - The geo_cache → sim_telemetry write-back is a bridge until NMEA GPS is live.
    Once NMEA feeds BOAT_LAT/LON directly, the geo_cache path becomes redundant.
  - The Melbourne rejection filter (abs(flat+38)<0.5 etc.) guards against the
    headless sim default. Remove/adjust if Pyxis genuinely operates near Victoria.
  - VERSION constant is stamped top-left of every generated image for debugging.
"""

import os, json, time, math, colorsys, shutil
import urllib.request, io
import requests

# --- Config ---
VERSION     = "v2.2"
B           = os.environ.get("B", "/home/icanjumpuddles/manta-comms")
TELEM_FILE  = os.path.join(B, "sim_telemetry.json")
METEO_FILE  = os.path.join(B, "meteo_cache.json")
CMEMS_FILE  = os.path.join(B, "currents_grid_cache.json")   # written by cmems_worker.py
INTERVAL    = 600        # seconds between regenerations
IMG_SIZE    = 454        # Epix 2 Pro 51mm native display size

# (watch_zoom, carto_z, range_nm, grid_pts, out_filename)
MAP_CONFIGS = [
    (1, 7,  30, 13, "meteo_map_z1.jpg"),
    (2, 8,  15, 13, "meteo_map_z2.jpg"),
    (3, 10,  6, 13, "meteo_map_z3.jpg"),
    (4, 12,  1, 13, "meteo_map_z4.jpg"),
]

PROXY_URL = "https://benfishmanta.duckdns.org/scenario"

# ---------------------------------------------------------------------------
def load_cmems_grid():
    """Load CMEMS current vectors (written by cmems_worker.py). Returns list or None."""
    try:
        age_limit = 7200  # accept cache up to 2 hours old
        cache = json.load(open(CMEMS_FILE))
        age   = time.time() - cache.get("updated", 0)
        if age > age_limit:
            print(f"  [cmems] Cache stale ({age/3600:.1f}h) — using meteo fallback")
            return None
        pts = cache.get("points", [])
        print(f"  [cmems] Loaded {len(pts)} current vectors ({age/60:.0f}min old)")
        return pts
    except Exception:
        return None

def nearest_cmems_current(cmems_pts, lat, lon):
    """Return (speed_kn, dir_deg) from nearest CMEMS grid point, or (0,0)."""
    if not cmems_pts:
        return 0.0, 0.0
    best, best_d = None, float('inf')
    for p in cmems_pts:
        d = (p['lat'] - lat) ** 2 + (p['lon'] - lon) ** 2
        if d < best_d:
            best_d, best = d, p
    return (best['speed_kn'], best['dir_deg']) if best else (0.0, 0.0)

def get_vessel_pos():
    """
    Get Pyxis position from the proxy's /scenario endpoint — updated directly
    by the scenario injector with the current lat/lon.
    Falls back to sim_telemetry.json BOAT_LAT if proxy is unreachable.
    """
    try:
        r = requests.get(PROXY_URL, timeout=5)
        if r.status_code == 200:
            d = r.json()
            lat = d.get("lat") or d.get("pyxis_lat") or d.get("BOAT_LAT")
            lon = d.get("lon") or d.get("pyxis_lon") or d.get("BOAT_LON")
            if lat is not None and lon is not None:
                flat = float(str(lat).replace('°', '').replace('\u00b0', '').strip())
                flon = float(str(lon).replace('°', '').replace('\u00b0', '').strip())
                print(f"  [pos] scenario: {flat:.4f},{flon:.4f}")
                return flat, flon
    except Exception as e:
        print(f"  [pos] proxy unavailable ({e}) — reading sim_telemetry.json")

    # Fallback: read direct from sim_telemetry.json
    try:
        with open(TELEM_FILE) as f:
            d = json.load(f)
        lat = d.get("BOAT_LAT")
        lon = d.get("BOAT_LON")
        if lat is not None and lon is not None:
            print(f"  [pos] telemetry fallback: {float(lat):.4f},{float(lon):.4f}")
            return float(lat), float(lon)
    except Exception:
        pass
    return None, None

# ---------------------------------------------------------------------------
def fetch_marine_grid(center_lat, center_lon, range_nm, n_pts):
    """Fetch all available Open-Meteo Marine API fields for an n_pts×n_pts grid."""
    nm_to_deg_lat = 1.0 / 60.0
    nm_to_deg_lon = 1.0 / (60.0 * math.cos(math.radians(center_lat)))
    lats, lons = [], []
    for i in range(n_pts):
        for j in range(n_pts):
            dlat = -range_nm/2 + (range_nm / (n_pts - 1)) * i if n_pts > 1 else 0
            dlon = -range_nm/2 + (range_nm / (n_pts - 1)) * j if n_pts > 1 else 0
            lats.append(round(center_lat + dlat * nm_to_deg_lat, 4))
            lons.append(round(center_lon + dlon * nm_to_deg_lon, 4))

    # All available current fields from Open-Meteo Marine API
    fields = (
        "wave_height,wave_direction,wave_period,"
        "wind_wave_height,wind_wave_direction,wind_wave_period,"
        "swell_wave_height,swell_wave_direction,swell_wave_period,"
        "ocean_current_velocity,ocean_current_direction,"
        "sea_surface_temperature"
    )
    url = (
        "https://marine-api.open-meteo.com/v1/marine"
        f"?latitude={','.join(map(str,lats))}"
        f"&longitude={','.join(map(str,lons))}"
        f"&current={fields}"
        "&wind_speed_unit=kn"
    )
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            print(f"  Marine API error {r.status_code}")
            return None
        data = r.json()
        if not isinstance(data, list):
            data = [data]
        points = []
        for idx, item in enumerate(data):
            cur = item.get("current", {})
            points.append({
                "lat":          lats[idx],
                "lon":          lons[idx],
                # Combined wave
                "wave_h":       cur.get("wave_height", 0) or 0,
                "wave_dir":     cur.get("wave_direction", 0) or 0,
                "wave_period":  cur.get("wave_period", 0) or 0,
                # Wind wave
                "ww_h":         cur.get("wind_wave_height", 0) or 0,
                "ww_dir":       cur.get("wind_wave_direction", 0) or 0,
                "ww_period":    cur.get("wind_wave_period", 0) or 0,
                # Swell
                "swell_h":      cur.get("swell_wave_height", 0) or 0,
                "swell_dir":    cur.get("swell_wave_direction", 0) or 0,
                "swell_period": cur.get("swell_wave_period", 0) or 0,
                # Current
                "curr_v":       cur.get("ocean_current_velocity", 0) or 0,
                "curr_dir":     cur.get("ocean_current_direction", 0) or 0,
                # Temperature
                "sst":          cur.get("sea_surface_temperature") or None,
            })
        sst_vals = [p["sst"] for p in points if p["sst"] is not None]
        if sst_vals:
            print(f"  SST range: {min(sst_vals):.1f}–{max(sst_vals):.1f}°C")
        return points
    except Exception as e:
        print(f"  Marine fetch error: {e}")
        return None

# ---------------------------------------------------------------------------
def wave_height_to_color(h_m, alpha=170):
    h = min(h_m, 6.0) / 6.0
    hue = (1.0 - h) * 0.67
    r, g, b = colorsys.hls_to_rgb(hue, 0.45, 0.9)
    return (int(r*255), int(g*255), int(b*255), alpha)

def sst_to_color(sst_c, alpha=160):
    """Map SST (°C) to colour: cold=deep blue, temperate=teal, warm=red.
    Range calibrated for Southern Ocean + tropics: 5°C–30°C."""
    t = max(0.0, min(1.0, (sst_c - 5.0) / 25.0))  # 5–30°C → 0–1
    # cold: (0,30,120) → mid: (0,180,140) → warm: (220,40,0)
    if t < 0.5:
        s = t * 2
        r = int(0   + s * 0)
        g = int(30  + s * 150)
        b = int(120 + s * 20)
    else:
        s = (t - 0.5) * 2
        r = int(0   + s * 220)
        g = int(180 - s * 140)
        b = int(140 - s * 140)
    return (r, g, b, alpha)

def draw_arrow(draw, cx, cy, direction_deg, length, color, width=1):
    """Draw a directional arrow pointing in direction_deg (travel direction)."""
    rad   = math.radians(direction_deg)
    dx    = math.sin(rad) * length
    dy    = -math.cos(rad) * length
    ex, ey = cx + dx, cy + dy
    draw.line([(cx, cy), (ex, ey)], fill=color, width=width)
    head_len  = length * 0.35
    for angle in (145, -145):
        hr = rad + math.radians(angle)
        draw.line([(ex, ey),
                   (ex + math.sin(hr) * head_len,
                    ey - math.cos(hr) * head_len)], fill=color, width=width)

def fetch_carto_tile(z, tx, ty, headers):
    url = f"https://a.basemaps.cartocdn.com/dark_all/{z}/{tx}/{ty}.png"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as r:
            from PIL import Image
            return Image.open(io.BytesIO(r.read())).convert('RGB')
    except Exception:
        from PIL import Image
        return Image.new('RGB', (256, 256), (5, 10, 20))

def latlon_to_tile(lat, lon, z):
    n  = 2.0 ** z
    tx = ((lon + 180.0) / 360.0) * n
    ty = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return tx, ty

# ---------------------------------------------------------------------------
def generate_one_map(center_lat, center_lon, carto_z, points, out_path, cmems_pts=None):
    """Render one native-resolution map and save to out_path."""
    from PIL import Image, ImageDraw, ImageFilter, ImageEnhance

    # --- Tile setup: 3×3 tiles centred on vessel ---
    cx_f, cy_f = latlon_to_tile(center_lat, center_lon, carto_z)
    ctx, cty   = int(cx_f), int(cy_f)
    n_tiles    = int(2.0 ** carto_z)
    headers    = {'User-Agent': 'Pyxis-Marine/2.0'}

    TILE      = 256
    GRID      = 3          # 3×3 tile canvas
    canvas_px = TILE * GRID   # 768×768

    canvas = Image.new('RGB', (canvas_px, canvas_px), (5, 10, 20))
    for dx in range(-1, 2):
        for dy in range(-1, 2):
            tx = (ctx + dx) % n_tiles   # column (lon) offset
            ty = cty + dy               # row (lat) offset
            if 0 <= ty < n_tiles:
                tile = fetch_carto_tile(carto_z, tx, ty, headers)
                canvas.paste(tile, ((dx + 1) * TILE, (dy + 1) * TILE))


    # Correct Web Mercator projection: lon → x (east), lat → y (north)
    def ll_to_px(lat, lon):
        tx_f, ty_f = latlon_to_tile(lat, lon, carto_z)
        px = int((tx_f - (ctx - 1)) * TILE)   # lon → x (columns)
        py = int((ty_f - (cty - 1)) * TILE)   # lat → y (rows)
        return px, py

    vessel_px, vessel_py = ll_to_px(center_lat, center_lon)

    # --- SST heatmap from CMEMS (background, behind wave overlay) ---
    sst_overlay = Image.new('RGBA', (canvas_px, canvas_px), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(sst_overlay)
    sst_pts = [p for p in (cmems_pts or []) if p.get('sst_c') is not None]
    if sst_pts:
        sst_radius = max(60, int(canvas_px / 5))
        for p in sst_pts:
            px, py = ll_to_px(p['lat'], p['lon'])
            if not (5 < px < canvas_px - 5 and 5 < py < canvas_px - 5):
                continue
            sdraw.ellipse((px-sst_radius, py-sst_radius, px+sst_radius, py+sst_radius),
                          fill=sst_to_color(p['sst_c'], alpha=130))
        sst_blur = sst_overlay.filter(ImageFilter.GaussianBlur(radius=max(30, int(canvas_px/10))))
        canvas = Image.alpha_composite(canvas.convert('RGBA'), sst_blur).convert('RGB')

    # --- Wave heatmap overlay ---
    overlay = Image.new('RGBA', (canvas_px, canvas_px), (0, 0, 0, 0))
    odraw   = ImageDraw.Draw(overlay)
    valid_pts = 0
    for p in (points or []):
        px, py = ll_to_px(p["lat"], p["lon"])
        if not (5 < px < canvas_px - 5 and 5 < py < canvas_px - 5):
            continue
        valid_pts += 1
        color  = wave_height_to_color(p["wave_h"], alpha=150)
        radius = max(80, int(canvas_px / 4))   # large enough to overlap neighbours and fill canvas
        odraw.ellipse((px - radius, py - radius, px + radius, py + radius),
                      fill=color)

    blur_r = max(40, int(canvas_px / 8))   # proportionally larger blur for smooth fill
    overlay_blur = overlay.filter(ImageFilter.GaussianBlur(radius=blur_r))
    canvas = Image.alpha_composite(canvas.convert('RGBA'), overlay_blur).convert('RGB')

    # --- Arrows ---
    adraw = ImageDraw.Draw(canvas)
    arrow_len = max(10, int(canvas_px / 40))   # base length for dense matrix

    for p in (points or []):
        px, py = ll_to_px(p["lat"], p["lon"])
        if not (15 < px < canvas_px - 15 and 15 < py < canvas_px - 15):
            continue
        # Swell: white arrow — scale by period (longer period = longer arrow)
        if p["swell_h"] > 0.2:
            period_scale = min(1.0 + (p.get("swell_period", 8) / 20.0), 2.0)
            draw_arrow(adraw, px, py, (p["swell_dir"] + 180) % 360,
                       int(arrow_len * period_scale), (255, 255, 255, 210), width=1)
        # Wind wave: yellow arrow
        if p.get("ww_h", 0) > 0.1:
            draw_arrow(adraw, px, py, (p["ww_dir"] + 180) % 360,
                       arrow_len, (255, 220, 0, 200), width=1)
        # Current: cyan arrow scaled by speed
        if p["curr_v"] > 0.05:
            scale = min(p["curr_v"] / 2.0, 1.0)
            draw_arrow(adraw, px, py, p["curr_dir"],
                       int(arrow_len * 0.6 + scale * arrow_len * 0.8),
                       (0, 210, 255), width=1)

    # --- Vessel crosshair (crisp, minimal) ---
    r0, r1 = 4, 36
    adraw.ellipse((vessel_px-r0, vessel_py-r0, vessel_px+r0, vessel_py+r0),
                  fill=(0, 255, 90), outline=(255, 255, 255), width=1)
    adraw.ellipse((vessel_px-r1, vessel_py-r1, vessel_px+r1, vessel_py+r1),
                  outline=(0, 200, 80), width=1)
    for sgn in (-1, 1):
        adraw.line([(vessel_px + sgn * (r0 + 3), vessel_py),
                    (vessel_px + sgn * (r0 + 10), vessel_py)],
                   fill=(255, 50, 50), width=1)
        adraw.line([(vessel_px, vessel_py + sgn * (r0 + 3)),
                    (vessel_px, vessel_py + sgn * (r0 + 10))],
                   fill=(255, 50, 50), width=1)

    # --- Legend (small, bottom-left corner) ---
    try:
        from PIL import ImageFont
        font = ImageFont.load_default()
    except Exception:
        font = None

    legend = [
        ((0, 100, 255),   "0-1m"),
        ((0, 200, 100),   "1-2m"),
        ((220, 180, 0),   "2-4m"),
        ((255, 70, 0),    "4m+"),
        ((255, 255, 255), "swell"),
        ((0, 210, 255),   "current"),
    ]
    lx = 6
    ly = canvas_px - 6 - len(legend) * 12
    for col, lbl in legend:
        adraw.rectangle((lx, ly + 1, lx + 8, ly + 9), fill=col)
        adraw.text((lx + 11, ly), lbl, fill=(200, 200, 200), font=font)
        ly += 12

    # --- Timestamp + version + position stamp ---
    adraw.text((6, 6), time.strftime("%H:%M UTC"), fill=(160, 200, 255), font=font)
    max_wave = max((p["wave_h"] for p in (points or [])), default=0)
    adraw.text((6, 18), f"Max: {max_wave:.1f}m", fill=(255, 190, 90), font=font)
    adraw.text((6, 30), f"{VERSION} {center_lat:.3f},{center_lon:.3f}", fill=(100, 255, 100), font=font)

    # --- Crop to IMG_SIZE centred on vessel, then enhance ---
    half    = IMG_SIZE // 2
    left    = max(0, vessel_px - half)
    top     = max(0, vessel_py - half)
    right   = left + IMG_SIZE
    bottom  = top  + IMG_SIZE
    if right > canvas_px:
        left  = canvas_px - IMG_SIZE
        right = canvas_px
    if bottom > canvas_px:
        top    = canvas_px - IMG_SIZE
        bottom = canvas_px

    final = canvas.crop((left, top, right, bottom))
    final = ImageEnhance.Contrast(final).enhance(1.15)
    final = ImageEnhance.Sharpness(final).enhance(1.3)

    buf = io.BytesIO()
    final.save(buf, format='JPEG', quality=88, optimize=True)
    with open(out_path, 'wb') as f:
        f.write(buf.getvalue())
    print(f"  Saved {len(buf.getvalue())//1024}KB → {out_path}")

def _write_pending_placeholder():
    """Write a 'waiting for position' placeholder JPEG for all zoom levels."""
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (IMG_SIZE, IMG_SIZE), (5, 10, 20))
    d = ImageDraw.Draw(img)
    cx, cy = IMG_SIZE // 2, IMG_SIZE // 2
    d.ellipse((cx-40, cy-40, cx+40, cy+40), outline=(0, 180, 80), width=2)
    d.ellipse((cx-6,  cy-6,  cx+6,  cy+6),  fill=(0, 255, 90))
    d.line([(cx-14, cy), (cx+14, cy)], fill=(255, 50, 50), width=2)
    d.line([(cx, cy-14), (cx, cy+14)], fill=(255, 50, 50), width=2)
    d.text((cx-70, cy+55), "AWAITING PYXIS POSITION", fill=(0, 180, 255))
    d.text((cx-55, cy+70), "Sea state pending...",    fill=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=80)
    data = buf.getvalue()
    for _, _, _, _, fname in MAP_CONFIGS:
        with open(os.path.join(B, fname), 'wb') as f:
            f.write(data)
    legacy = os.path.join(B, "meteo_map.jpg")
    with open(legacy, 'wb') as f:
        f.write(data)
    print("  Placeholder written — waiting for valid position")

# ---------------------------------------------------------------------------
def generate_all_maps():
    center_lat, center_lon = get_vessel_pos()
    is_fallback = (center_lat is None or center_lon is None)
    pos_str = f"{center_lat:.3f},{center_lon:.3f}" if not is_fallback else "UNKNOWN"
    print(f"[{time.strftime('%H:%M:%S')}] Sea state maps @ {pos_str}" +
          (" [POSITION PENDING]" if is_fallback else ""))

    if is_fallback:
        _write_pending_placeholder()
        return

    # Load CMEMS spatial current grid (optional — gracefully absent)
    cmems_pts = load_cmems_grid()

    # Cache marine data per range
    data_cache = {}

    for watch_z, carto_z, range_nm, grid_pts, filename in MAP_CONFIGS:
        out_path = os.path.join(B, filename)
        key = (range_nm, grid_pts)
        if key not in data_cache:
            print(f"  Fetching marine grid range={range_nm}nm pts={grid_pts*grid_pts}...")
            pts = fetch_marine_grid(center_lat, center_lon, range_nm, grid_pts)
            # Overlay CMEMS per-point current vectors when available
            if pts and cmems_pts:
                for p in pts:
                    spd, dr = nearest_cmems_current(cmems_pts, p["lat"], p["lon"])
                    if spd > 0.01:
                        p["curr_v"]   = spd
                        p["curr_dir"] = dr
            data_cache[key] = pts
        points = data_cache[key]
        print(f"  Rendering z{watch_z} (CartoDB z={carto_z})...")
        try:
            generate_one_map(center_lat, center_lon, carto_z, points, out_path, cmems_pts=cmems_pts)
        except Exception as e:
            import traceback
            print(f"  Render error z{watch_z}: {e}\n{traceback.format_exc()}")

    # Legacy fallback: meteo_map.jpg = copy of z1
    z1 = os.path.join(B, "meteo_map_z1.jpg")
    legacy = os.path.join(B, "meteo_map.jpg")
    if os.path.exists(z1):
        shutil.copy2(z1, legacy)


# ---------------------------------------------------------------------------
FALLBACK_LAT, FALLBACK_LON = -38.2, 144.9

def _nm_distance(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles."""
    R = 3440.065
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def _is_fallback(lat, lon):
    return abs(lat - FALLBACK_LAT) < 0.01 and abs(lon - FALLBACK_LON) < 0.01

if __name__ == "__main__":
    print(f"{'='*50}")
    print(f"Pyxis Marine Map Generator {VERSION} started")
    print(f"  TELEM_FILE: {TELEM_FILE}")
    print(f"  Base dir:   {B}")
    print(f"{'='*50}")

    # ── Startup: wait up to 90s for valid (non-fallback) vessel position ──
    print("  Waiting for valid Pyxis position from sim_telemetry.json...")
    for attempt in range(18):       # 18 × 5s = 90s max wait
        lat, lon = get_vessel_pos()
        if lat is not None and lon is not None:
            print(f"  Position acquired: {lat:.4f},{lon:.4f}")
            break
        print(f"  [{attempt*5}s] No valid position yet — waiting for sim...")
        time.sleep(5)
    else:
        print(f"  WARNING: No valid position after 90s — will serve placeholder")
        lat, lon = None, None

    last_lat, last_lon = lat, lon
    while True:
        try:
            generate_all_maps()
            last_lat, last_lon = get_vessel_pos()
        except Exception as e:
            import traceback
            print(f"Gen error: {e}\n{traceback.format_exc()}")

        # Poll every 30s for position change; generate early if vessel moved >5nm
        elapsed = 0
        while elapsed < INTERVAL:
            time.sleep(30)
            elapsed += 30
            cur_lat, cur_lon = get_vessel_pos()
            if cur_lat is None or cur_lon is None:
                continue
            if last_lat is None or last_lon is None:
                # Got first valid position — break to regenerate
                last_lat, last_lon = cur_lat, cur_lon
                print(f"  Position acquired mid-cycle: {cur_lat:.4f},{cur_lon:.4f} — regenerating")
                break
            drift = _nm_distance(last_lat, last_lon, cur_lat, cur_lon)
            if drift > 5.0:
                print(f"  Position drift {drift:.1f}nm — regenerating early")
                break
        else:
            print(f"  Sleeping {INTERVAL}s cycle complete")
