# Copyright (c) 2026 Benjamin Pullin. All rights reserved.
"""
PYXIS PROXY SERVER v4.1.1 - (WIND MAP ENABLED)
===================================================
PURPOSE:
This core server acts as the primary data fusion bridge for the Pyxis suite.
It intercepts and processes 3 independent incoming data streams:
1. Live physical sensor telemetry from the Garmin Watch (GPS, HR, Barometric)
2. Live global Marine AIS traffic (from AISStream / OpenMarine)
3. Live global Geopolitical & Thermal Satellite intelligence (GDELT / FIRMS)

ARCHITECTURE:
All incoming streams are synchronized and fused into a unified simulated BVR 
(Beyond Visual Range) radar picture. It interfaces directly with Google 
Gemini (LLM) to perform localized threat analysis against those fused contacts.
Finally, the AI's tactical threat assessments are synthesized into human 
voice reports via Kokoro/ElevenLabs and beamed back to the physical watch 
client and the Pyxis Lite web dashboards.

MAINTENANCE:
Future engineers maintaining this code should note the threading model:
`geo_worker` maintains the physical navigation warnings and AIS feeds.
`osint_worker` maintains the geopolitical news and thermal satellites.
Both use the `/status_api` JSON dict as a universal data bus.
"""
import os, requests, time, json, sqlite3, math, re, sys, threading, queue, textwrap, uuid, socket
import asyncio, websockets
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_file, make_response, Response
try:
    from google.cloud import texttospeech as gctts
except ImportError:
    gctts = None
from google import genai
from google.genai import types
from dotenv import load_dotenv
import soundfile as sf

import numpy as np
from kokoro_onnx import Kokoro
from functools import wraps

def check_auth(username, password):
    return username == 'admin' and password == 'manta'

def authenticate():
    return Response('Unauthorized Access.', 401, {'WWW-Authenticate': 'Basic realm="Pyxis C2 Terminal"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# Paths that wsrv.nl/watch fetches as images — auth headers can't be passed through
_TILE_PREFIXES = ('/ais_radar_map', '/topo_map', '/nautical_map', '/adsb_radar_map',
                  '/wx/', '/seastate/', '/wind_map/', '/sea_state_map/', '/static/',
                  '/sea_state_json', '/satellite_map', '/sat_ais_map')

def _is_trusted_request():
    """Returns True for requests that bypass global auth."""
    # Local simulator / internal services
    if request.remote_addr in ('127.0.0.1', '::1'):
        return True
    # Garmin watch (uses proprietary auth header)
    if request.headers.get('X-Garmin-Auth') == 'PYXIS_ACTUAL_77X':
        return True
    # Image tile paths fetched via wsrv.nl (no auth passthrough possible)
    for prefix in _TILE_PREFIXES:
        if request.path.startswith(prefix):
            return True
    # Valid Basic Auth (admin:manta) — covers /adsb_contacts from AdsbView
    auth = request.authorization
    if auth and check_auth(auth.username, auth.password):
        return True
    return False

app = Flask(__name__)

@app.before_request
def intercept_crew_gps():
    # ── Global auth gate ──────────────────────────────────────────────
    if not _is_trusted_request():
        return authenticate()
    # ── Crew GPS intercept (trusted requests only) ────────────────────
    try:
        lat = request.args.get('lat')
        lon = request.args.get('lon')
        if lat and lon:
            import re
            lat_clean = re.sub(r'[^\d.-]', '', str(lat))
            lon_clean = re.sub(r'[^\d.-]', '', str(lon))
            if lat_clean and lon_clean:
                parsed_lat = float(lat_clean)
                parsed_lon = float(lon_clean)
                sim_file = os.path.join('/home/icanjumpuddles/manta-comms', 'sim_telemetry.json')
                if os.path.exists(sim_file):
                    with open(sim_file, 'r') as f:
                        try: d = json.load(f)
                        except: d = {}
                    d['CREW_LAT'] = parsed_lat
                    d['CREW_LON'] = parsed_lon
                    with open(sim_file, 'w') as f:
                        json.dump(d, f)
    except Exception as e:
        pass

B = "/home/icanjumpuddles/manta-comms"
load_dotenv(os.path.join(B, ".env"), override=True)
DB, DT, SIM, AN = os.path.join(B, "pyxis_logs.db"), os.path.join(B, "latest_sector.json"), os.path.join(B, "sim_telemetry.json"), os.path.join(B, "anchor_state.json")
ROUTE_FILE = os.path.join(B, "active_route.json")
GMDSS_CACHE_FILE = os.path.join(B, "gmdss_cache.json")
SWPC_CACHE_FILE = os.path.join(B, "swpc_cache.json")
ASAM_CACHE_FILE = os.path.join(B, "asam_cache.json")
SEISMIC_CACHE_FILE = os.path.join(B, "seismic_cache.json")
METEO_CACHE_FILE = os.path.join(B, "meteo_cache.json")
INTEL_DB         = os.path.join(B, "intel_cache.db")

# ─────────────────────────────────────────────────────────────────────────────
# INTEL SQLITE CACHE  — persists vessel names + Gemini briefs across restarts
# ─────────────────────────────────────────────────────────────────────────────
def _init_intel_db():
    """Create intel_cache.db tables if they don't exist. Safe to call every start."""
    try:
        con = sqlite3.connect(INTEL_DB)
        con.execute("""CREATE TABLE IF NOT EXISTS vessel_names (
            mmsi     TEXT PRIMARY KEY,
            name     TEXT NOT NULL,
            updated  REAL NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS aircraft_names (
            icao     TEXT PRIMARY KEY,
            reg      TEXT,
            ac_type  TEXT,
            updated  REAL NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS vessel_intel (
            mmsi     TEXT PRIMARY KEY,
            name     TEXT,
            lines    TEXT NOT NULL,
            lat      REAL,
            lon      REAL,
            updated  REAL NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS aircraft_intel (
            icao     TEXT PRIMARY KEY,
            reg      TEXT,
            lines    TEXT NOT NULL,
            lat      REAL,
            lon      REAL,
            updated  REAL NOT NULL
        )""")
        con.commit(); con.close()
        print("[Pyxis] Intel DB: initialised intel_cache.db")  # log() not yet defined here
    except Exception as e:
        print(f"[Pyxis] Intel DB init error: {e}")

def intel_name_get(mmsi: str) -> str:
    """Return cached vessel name for MMSI, or empty string if not known."""
    try:
        con = sqlite3.connect(INTEL_DB); cur = con.execute(
            "SELECT name FROM vessel_names WHERE mmsi=?", (mmsi,))
        row = cur.fetchone(); con.close()
        return row[0] if row else ""
    except: return ""

def intel_name_put(mmsi: str, name: str):
    """Persist a vessel name learned from ShipStaticData. Upserts safely."""
    try:
        con = sqlite3.connect(INTEL_DB)
        con.execute("INSERT OR REPLACE INTO vessel_names (mmsi,name,updated) VALUES(?,?,?)",
                    (mmsi, name, time.time()))
        con.commit(); con.close()
    except: pass

def intel_cache_get(mmsi: str, max_age_hr: float = 24.0):
    """Return cached Gemini intel lines for MMSI if fresher than max_age_hr, else None."""
    try:
        con = sqlite3.connect(INTEL_DB); cur = con.execute(
            "SELECT lines,updated FROM vessel_intel WHERE mmsi=?", (mmsi,))
        row = cur.fetchone(); con.close()
        if row and (time.time() - row[1]) < max_age_hr * 3600:
            return json.loads(row[0])
    except: pass
    return None

def intel_cache_put(mmsi: str, name: str, lines: list, lat: float, lon: float):
    """Store Gemini intel brief for MMSI. Overwrites any existing entry."""
    try:
        con = sqlite3.connect(INTEL_DB)
        con.execute("""INSERT OR REPLACE INTO vessel_intel
                       (mmsi,name,lines,lat,lon,updated) VALUES(?,?,?,?,?,?)""",
                    (mmsi, name, json.dumps(lines), lat, lon, time.time()))
        con.commit(); con.close()
    except Exception as e:
        log(f"Intel cache write error: {e}")

_init_intel_db()  # Runs at import time before log() exists — uses print() internally
task_queue = queue.Queue()
gen_lock = threading.Lock()
task_results = {}
inbox_messages = []
inbox_lock = threading.Lock()
force_kokoro = False
# Default position: Yaringa Boat Harbour, Westernport Bay, VIC, AU
last_known_lat = -38.487
last_known_lon = 145.620
current_scenario = {}

# --- OPEN SEAMAP CACHE ---
osm_cache = []
osm_cache_lock = threading.Lock()

# --- OSINT INTELLIGENCE CACHE ---
osint_cache_list = []
import urllib.request
def osint_worker():
    global osint_cache_list
    while True:
        try:
            req = urllib.request.Request('https://www.gdacs.org/gdacsapi/api/events/geteventlist/MAP?alertlevel=Orange,Red', headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                j_data = json.loads(response.read().decode())
                feats = j_data.get('features', [])
                
                new_cache = []
                for f in feats:
                    props = f.get('properties', {})
                    geom = f.get('geometry', {})
                    if geom.get('type') == 'Point':
                        lon, lat = geom['coordinates'][0], geom['coordinates'][1]
                        
                        etype = props.get('eventtype', '')
                        cat = "OSINT_MILITARY"
                        if etype in ["TC", "FL", "DR", "EQ", "VO", "TS"]: cat = "OSINT_WEATHER"
                        
                        name = props.get('name', 'Unknown Threat')[:15]
                        
                        new_cache.append({
                            "id": f"{etype}_{props.get('eventid','0')}",
                            "name": name,
                            "type": cat,
                            "lat": lat,
                            "lon": lon
                        })
                
                osint_cache_list = new_cache
        except Exception as e:
            pass
        time.sleep(600) # Poll every 10 mins

threading.Thread(target=osint_worker, daemon=True).start()

def osm_worker():
    """
    Background daemon that periodically fetches real-world navigational hazards
    (buoys, channel markers, shoals, reefs) from the OpenStreetMap Overpass API.
    Updates the cache whenever the vessel moves further than 5 nautical miles
    from the center of the last fetched bounding box.
    """
    global osm_cache
    last_fetched_lat = None
    last_fetched_lon = None
    
    def haversine_nm(lat1, lon1, lat2, lon2):
        if None in [lat1, lon1, lat2, lon2]: return 9999.0
        R = 3440.065 # Radius of earth in nautical miles
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    while True:
        try:
            # Determine current position from DT history
            c_lat, c_lon = 0, 0
            has_pos = False
            if os.path.exists(DT):
                with open(DT, "r") as f:
                    dt_data = json.load(f)
                    c_lat = dt_data.get('lat', 0)
                    c_lon = dt_data.get('lon', 0)
                    if c_lat != 0: has_pos = True
            
            if has_pos:
                dist = haversine_nm(last_fetched_lat, last_fetched_lon, c_lat, c_lon)
                
                # Fetch if we moved > 5 nm or haven't fetched yet
                if dist > 5.0:
                    log(f"OSM Worker: Vessel moved {dist:.1f}nm from last sync. Fetching new OpenSeaMap Sector...")
                    
                    # +/- 0.15 degrees is roughly +/- 9 nautical miles box
                    min_lat, max_lat = c_lat - 0.15, c_lat + 0.15
                    min_lon, max_lon = c_lon - 0.15, c_lon + 0.15
                    bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"
                    
                    query = f"""
                    [out:json][timeout:15];
                    (
                      node["seamark:type"]({bbox});
                      node["natural"="shoal"]({bbox});
                      node["natural"="reef"]({bbox});
                    );
                    out body;
                    """
                    
                    r = requests.post("https://overpass-api.de/api/interpreter", data={'data': query}, timeout=20)
                    if r.status_code == 200:
                        elements = r.json().get('elements', [])
                        new_cache = []
                        for el in elements:
                            tags = el.get('tags', {})
                            m_type = "MARKER"
                            m_name = tags.get('name', tags.get('seamark:name', 'Unlit Mark'))
                            
                            if 'natural' in tags:
                                m_type = "SHOAL"
                                if not tags.get('name'): m_name = "Shoal/Reef Hazard"
                                
                            new_cache.append({
                                "id": f"OSM_{el['id']}",
                                "lat": el['lat'],
                                "lon": el['lon'],
                                "type": m_type,
                                "name": m_name[:20], # truncate to fit UI
                                "speed": 0.0,
                                "heading": 0.0
                            })
                            
                        with osm_cache_lock:
                            osm_cache = new_cache
                            
                        last_fetched_lat = c_lat
                        last_fetched_lon = c_lon
                        log(f"OSM Worker: Cached {len(osm_cache)} navigational hazards in local sector.")
                    else:
                        log(f"OSM Worker: Overpass API Error {r.status_code}")
                        
        except Exception as e:
            log(f"OSM Worker Error: {e}")
            
        time.sleep(60)

NTM_CACHE_FILE = os.path.join(B, "ntm_cache.json")

def msi_worker():
    """
    Background daemon that fetches real-time National Geospatial-Intelligence Agency (NGA)
    Notice to Mariners (NTM) updates and caches them.
    """
    last_fetched = 0
    while True:
        try:
            if time.time() - last_fetched > 3600: # Every 1 hour
                log("MSI Worker: Fetching global Notice to Mariners from NGA...")
                url = "https://msi.nga.mil/api/publications/ntm/pubs?output=json"
                r = requests.get(url, timeout=30, verify=False)
                if r.status_code == 200:
                    data = r.json()
                    pubs = data.get('pubs', data.get('publications', data)) # Capture whatever root NTM returns
                    with open(NTM_CACHE_FILE, "w") as f:
                        json.dump(pubs, f)
                    log(f"MSI Worker: Cached NTM Notice to Mariners data.")
                    last_fetched = time.time()
                else:
                    log(f"MSI Worker: Failed NGA NTM API ({r.status_code})")
        except Exception as e:
            log(f"MSI Worker Error: {e}")
        time.sleep(60)

def audio_janitor_worker():
    """
    Background daemon running every hour to scan the B directory and selectively
    delete any dynamically synthesized 'audio_<timestamp>.wav' files older than 2 hours.
    This entirely prevents PyxisTTS from crashing the host node via 'No space left on device'.
    """
    while True:
        try:
            import glob, os, time
            now = time.time()
            cutoff = now - (2 * 3600)
            patterns = [os.path.join(B, "audio_*.wav"), os.path.join(B, "audio_*.wav.tmp")]
            deleted = 0
            for pat in patterns:
                for f_path in glob.glob(pat):
                    try:
                        if os.path.getmtime(f_path) < cutoff:
                            os.remove(f_path)
                            deleted += 1
                    except: pass
            if deleted > 0:
                print(f"DEBUG: Audio Janitor: Purged {deleted} stale TTS audio artifacts.", flush=True)
        except Exception as e: pass
        time.sleep(3600)

threading.Thread(target=audio_janitor_worker, daemon=True).start()

def tile_janitor_worker():
    """
    Background daemon running every hour to monitor the tile_cache folder.
    Keeps total map tile storage under 2 GB by deleting the oldest accessed tiles (LRU).
    """
    import os, time
    max_bytes = 2.0 * 1024 * 1024 * 1024     # 2.0 GB
    target_bytes = 1.6 * 1024 * 1024 * 1024  # 1.6 GB
    while True:
        try:
            cache_dir = os.path.join(B, "tile_cache")
            if os.path.exists(cache_dir):
                files = []
                total_size = 0
                for f in os.listdir(cache_dir):
                    fp = os.path.join(cache_dir, f)
                    if os.path.isfile(fp):
                        sz = os.path.getsize(fp)
                        total_size += sz
                        files.append((fp, sz, os.path.getatime(fp)))
                if total_size > max_bytes:
                    files.sort(key=lambda x: x[2]) # Oldest access time first
                    deleted = 0
                    for fp, sz, _ in files:
                        try:
                            os.remove(fp)
                            total_size -= sz
                            deleted += 1
                        except: pass
                        if total_size <= target_bytes:
                            break
                    if deleted > 0:
                        print(f"DEBUG: Tile Cache Janitor: Purged {deleted} old tiles. Reclaimed space.", flush=True)
        except Exception: pass
        time.sleep(3600)

threading.Thread(target=tile_janitor_worker, daemon=True).start()

def log(msg):
    """
    Standardizes console output for the Pyxis Server by prefixing messages with 'DEBUG:'.
    Flushes stdout immediately so Docker/Systemd journals capture logs in real-time.
    """
    print(f"DEBUG: {msg}", flush=True)

log("---> INITIALIZING PYXIS MASTER v4.1.1 (RADAR_OSM)...")
try:
    kokoro = Kokoro(B+"/kokoro-v1.0.onnx", B+"/voices-v1.0.bin")
    log("---> ALICE ONLINE (BRITISH IDENTITY)")
except Exception as e:
    log(f"---> INIT ERR: {e}"); kokoro = None

try:
    _gtts_client = gctts.TextToSpeechClient() if gctts else None
    if _gtts_client: log("---> GOOGLE TTS ONLINE (ACHERNAR / AU)")
except Exception as e:
    log(f"---> GOOGLE TTS INIT ERR: {e}"); _gtts_client = None


def synthesize_google_tts(txt, out_path):
    """
    Synthesizes speech via Google Cloud TTS (Chirp 3 HD, en-AU-Chirp3-HD-Achernar).
    Uses the module-level client and chunks text >4000 chars to avoid 5000 byte limits.
    Returns True on success, False on any error.
    """
    if _gtts_client is None:
        log("Google Cloud TTS client not initialised Ã¢â‚¬â€ skipping")
        return False
    try:
        import io
        voice_name = os.getenv("GOOGLE_TTS_VOICE", "en-AU-Neural2-C")
        voice_params = gctts.VoiceSelectionParams(language_code="en-AU", name=voice_name)
        audio_cfg = gctts.AudioConfig(audio_encoding=gctts.AudioEncoding.LINEAR16)

        raw_sentences = [s.strip() for s in re.split(r'(?<=[.!?\n])\s+', txt) if s.strip()]
        safe_chunks = []
        for s in raw_sentences:
            safe_chunks.extend(textwrap.wrap(s, width=4000, break_long_words=False))

        audio_pieces = []
        samplerate = 24000
        for i, chunk in enumerate(safe_chunks):
            synth_input = gctts.SynthesisInput(text=chunk)
            resp = _gtts_client.synthesize_speech(input=synth_input, voice=voice_params, audio_config=audio_cfg)
            data, samplerate = sf.read(io.BytesIO(resp.audio_content))
            audio_pieces.append(data)

        tmp = out_path + ".tmp"
        sf.write(tmp, np.concatenate(audio_pieces), samplerate, format='WAV')
        os.rename(tmp, out_path)
        log(f"Google Cloud TTS (Chirp3-HD/Achernar): synthesis OK ({len(safe_chunks)} chunks) Ã¢â€ â€™ {out_path}")
        return True
    except Exception as e:
        log(f"Google Cloud TTS failed: {e}")
        return False

def init_db():
    """
    Initializes the local SQLite SQLite log database ('pyxis_logs.db').
    Creates the main 'logs' table to store persistent historical telemetry
    and SITREP reports. Adds backwards compatibility columns if missing.
    """
    with sqlite3.connect(DB) as c: 
        c.execute("CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts DATETIME DEFAULT CURRENT_TIMESTAMP, lat REAL, lon REAL, depth REAL, rpm INTEGER, bat INTEGER, report TEXT, raw_sensors TEXT)")
        # Backwards compatibility for older DBs without raw_sensors
        try: c.execute("ALTER TABLE logs ADD COLUMN raw_sensors TEXT")
        except: pass
init_db()

def brain_worker():
    """
    The core Synthesis Engine thread. Continuously consumes from 'task_queue'.
    When Pyxis determines a threat or generates a brief, this thread translates 
    the text report into a spoken audio file (.wav format). 
    It routes critical military alerts instantly through standard gTTS, while 
    routine briefings are routed through ElevenLabs (Ruby) or local Kokoro (Alice).
    """
    global inbox_messages
    while True:
        task = task_queue.get()
        if task is None: break
        rtype, la, lo, txt = task
        try:
            # Look up the task text from DT history or create a new ID
            new_id = int(time.time() * 1000)
            
            # Ensure the state is known as "building audio"
            try:
                with open(DT, "r") as f: st = json.load(f)
                history = st.get("audio_history", [])
                
                # Check if this text was already pre-staged by the HTTP handler
                found = False
                for h in history:
                    if h.get("text") == txt:
                        h["ready"] = False
                        new_id = h["id"]
                        found = True
                        break
                
                if not found:
                    history.append({"id": new_id, "type": rtype, "ts": time.time(), "ready": False, "text": txt})
                
                st["audio_history"] = history[-15:]
                with open(DT, "w") as f: json.dump(st, f)
            except Exception as e: log(f"Pre-write err: {e}")

            out_p, tmp = B+f"/audio_{new_id}.wav", B+f"/audio_{new_id}.wav.tmp"

            success = False
            el_key = os.getenv("ELEVENLABS_API_KEY")
            el_voice = os.getenv("ELEVENLABS_VOICE_ID")
            global force_kokoro

            # DECOUPLED TEXT INBOX - Transmit immediately before TTS blocking synthesis
            with inbox_lock:
                inbox_messages.append({"ts": new_id, "source": "PYXIS", "message": txt})
                if len(inbox_messages) > 50: inbox_messages = inbox_messages[-50:]

            # PHONETIC TRANSLATION FOR TTS ONLY
            def phoneticize(text):
                phon = {'0':'Zero', '1':'Wun', '2':'Two', '3':'Tree', '4':'Fower', '5':'Fife', '6':'Six', '7':'Seven', '8':'Ait', '9':'Niner'}
                import re
                t = re.sub(r'\b(\d{6})(Z|A|B|C|D|E|F|G|H|I|K|L|M|N|O|P|Q|R|S|T|U|V|W|X|Y)\b', lambda m: " ".join([phon.get(c,c) for c in m.group(1)]) + " " + m.group(2) + "ulu", text)
                t = re.sub(r'\b(\d{1,3})\s*(degree|degrees|minute|minutes|second|seconds)\b', lambda m: " ".join([phon.get(c,c) for c in m.group(1)]) + " " + m.group(2), t, flags=re.IGNORECASE)
                t = re.sub(r'\b(DTG)\b', 'Date Time Group', t)
                return t

            synth_txt = phoneticize(txt)

            # --- Tier 1: ElevenLabs (premium cloud) ---
            if el_key and el_voice and not force_kokoro:
                try:
                    log("Attempting ElevenLabs synthesis (Pyxis)...")
                    res = requests.post(
                        f"https://api.elevenlabs.io/v1/text-to-speech/{el_voice}",
                        json={"text": synth_txt, "model_id": "eleven_multilingual_v2"},
                        headers={"Accept": "audio/mpeg", "Content-Type": "application/json", "xi-api-key": el_key},
                        timeout=20)
                    if res.status_code == 200:
                        with open(tmp, "wb") as f: f.write(res.content)
                        os.rename(tmp, out_p)
                        log(f"ElevenLabs audio successfully saved to {out_p}")
                        success = True
                    else:
                        log(f"ElevenLabs failed ({res.status_code}). Falling back to Google Cloud TTS.")
                except Exception as e:
                    log(f"ElevenLabs exception: {e}. Falling back to Google Cloud TTS.")

            # --- Tier 2: Google Cloud TTS (Chirp 3 HD / Achernar en-AU) ---
            if not success:
                success = synthesize_google_tts(synth_txt, out_p)

            # --- Tier 3: Kokoro ONNX (fully offline / Alice) ---
            if not success and kokoro:
                try:
                    log("Starting Kokoro synthesis (Alice)...")
                    raw_sentences = [s.strip() for s in re.split(r'(?<=[.!?\n])\s+', synth_txt) if s.strip()]
                    safe_chunks = []
                    for s in raw_sentences:
                        safe_chunks.extend(textwrap.wrap(s, width=200, break_long_words=False))
                    audio_pieces = []
                    for i, chunk in enumerate(safe_chunks):
                        log(f"Synthesizing audio chunk {i+1}/{len(safe_chunks)}...")
                        audio_pieces.append(kokoro.create(chunk, voice='bf_alice', speed=1.05, lang='en-gb')[0])
                        time.sleep(0.05)
                    sf.write(tmp, np.concatenate(audio_pieces), 24000, format='WAV')
                    os.rename(tmp, out_p)
                    log(f"Kokoro Alice audio successfully saved to {out_p}")
                    success = True
                except Exception as e:
                    log(f"Kokoro exception: {e}")

            with open(DT, "r") as f: st = json.load(f)
            st.update({f"{rtype}_id": new_id, "lat": la, "lon": lo})
            
            history = st.get("audio_history", [])
            for h in history:
                if h.get("id") == new_id:
                    h["ready"] = True
            st["audio_history"] = history[-15:] # Keep last 15 reports
            
            with open(DT, "w") as f: json.dump(st, f)
        except Exception as e: log(f"BRAIN ERR: {e}")
        task_queue.task_done()

threading.Thread(target=brain_worker, daemon=True).start()

GMDSS_CACHE_FILE = "gmdss_cache.json"

def gmdss_worker():
    """
    Background thread that polls the NGA (National Geospatial-Intelligence Agency)
    API hourly for active Global Maritime Distress and Safety System (GMDSS) 
    broadcast warnings. It filters for kinetic/military keywords (PIRACY, MISSILE) 
    and caches relevant threat data to 'gmdss_cache.json' for the AI to ingest.
    """
    while True:
        try:
            r = requests.get('https://msi.nga.mil/api/publications/broadcast-warn?output=json', headers={'User-Agent': 'Mozilla/5.0 Pyxis'}, timeout=15, verify=False)
            data = r.json()
            
            warnings = data.get("broadcast-warn", [])
            categorized_warnings = []
            
            cat_keywords = {
                "KINETIC": ["MISSILE", "DRONE", "UAV", "UAS", "USV", "ROCKET", "ATTACK", "PIRACY", "FIRING", "WEAPON", "EXPLOSIVE", "MINE", "SPOOF", "ORDNANCE", "GUNNERY", "TORPEDO", "SUBMARINE", "WARSHIP", "BLOCKADE", "TERRORIST", "ARMED", "THREAT", "HOSTILE", "HIJACK", "BOARDING", "INTERCEPT", "INCIDENT", "SUSPICIOUS", "KINETIC", "LIVE FIRE", "RESTRICTED AREA", "WARNING ZONE", "MILITARY", "EXERCISE", "NAVAL"],
                "NAV_HAZARD": ["ADRIFT", "SHOAL", "UNLIT", "OFF STATION", "DERELICT", "TOWING", "DREDGING", "ICEBERG", "CABLE", "BUOY"],
                "SAR": ["DISTRESS", "OVERBOARD", "MISSING", "RESCUE", "SANK", "MAYDAY"],
                "INFRASTRUCTURE": ["UNRELIABLE", "OUTAGE", "NAVTEX", "GPS", "AIS", "COMMS"]
            }
            
            for w in warnings:
                text = w.get("text", "").upper()
                matched_categories = []
                for cat, kws in cat_keywords.items():
                    if any(kw in text for kw in kws):
                        matched_categories.append(cat)
                
                if matched_categories:
                    warn_obj = {
                        "navArea": w.get("navArea", ""),
                        "text": text,
                        "threat_categories": matched_categories
                    }
                    import re
                    match = re.search(r'(\d{1,2})[- .]+(\d{1,2}(?:\.\d+)?)?[ \']?([NS])[,\s]+(\d{1,3})[- .]+(\d{1,2}(?:\.\d+)?)?[ \']?([EW])', text)
                    if match:
                        lat_deg = float(match.group(1))
                        lat_min = float(match.group(2)) if match.group(2) else 0.0
                        lat_dir = match.group(3)
                        lon_deg = float(match.group(4))
                        lon_min = float(match.group(5)) if match.group(5) else 0.0
                        lon_dir = match.group(6)
                        lat = lat_deg + (lat_min / 60.0)
                        if lat_dir == 'S': lat = -lat
                        lon = lon_deg + (lon_min / 60.0)
                        if lon_dir == 'W': lon = -lon
                        warn_obj["lat"] = round(lat, 4)
                        warn_obj["lon"] = round(lon, 4)
                        warn_obj["id"] = "GMDSS_" + str(w.get("number", "1"))
                        warn_obj["name"] = f"GMDSS [{matched_categories[0]}]"
                        warn_obj["type"] = "OSINT_GMDSS"
                    categorized_warnings.append(warn_obj)
            
            with open(GMDSS_CACHE_FILE, "w") as f:
                json.dump(categorized_warnings, f)
            log(f"GMDSS Worker: Cached {len(categorized_warnings)} categorized NAVAREA warnings.")
        except Exception as e:
            log(f"GMDSS Worker Err: {e}")
        
        time.sleep(3600)  # Check every hour

threading.Thread(target=gmdss_worker, daemon=True).start()

# Ã¢â€â‚¬Ã¢â€â‚¬ ADS-B Aircraft Tracking (OpenSky Network Ã¢â‚¬â€ free, no API key) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
ADSB_CACHE_FILE = os.path.join(B, "adsb_cache.json")
adsb_cache_lock = threading.Lock()
# Pre-load cache from disk so contacts are available immediately on restart
try:
    with open(ADSB_CACHE_FILE) as _f:
        adsb_cache = json.load(_f)
    log(f"ADS-B: pre-loaded {len(adsb_cache)} contacts from cache")
except Exception:
    adsb_cache = []

def adsb_worker():
    """
    Polls the OpenSky Network REST API every 5 minutes for all aircraft
    within a 3Ã‚Â° bounding box around the vessel's current position.
    No API key required. Caches contacts with callsign, altitude, speed,
    heading, squawk, and range/bearing from the vessel.
    Emergency squawks (7500/7600/7700) are flagged as priority contacts.
    """
    global adsb_cache, last_known_lat, last_known_lon
    while True:
        try:
            la, lo = last_known_lat, last_known_lon
            radius = 3.0  # ~200nm bounding box
            url = (f"https://opensky-network.org/api/states/all"
                   f"?lamin={la-radius}&lomin={lo-radius}"
                   f"&lamax={la+radius}&lomax={lo+radius}")
            # Use credentials if set (free account = 400 req/day vs 60 anon)
            os_user = os.getenv("OPENSKY_USER")
            os_pass = os.getenv("OPENSKY_PASS")
            auth_os = (os_user, os_pass) if os_user else None
            r = requests.get(url, timeout=15, auth=auth_os,
                             headers={"User-Agent": "Pyxis/4.1 (research vessel)"})
            empty_states = (r.status_code == 200 and not (r.json().get("states") or []))
            if r.status_code == 429 or empty_states:
                if r.status_code == 429:
                    log("ADS-B: OpenSky 429 — trying fallbacks")
                else:
                    log(f"ADS-B: OpenSky empty states ({la:.2f},{lo:.2f}) — trying fallbacks")
                contacts = []

                # Fallback 1: adsb.lol v2 (adsb.fi is blocked from GCP IPs)
                if not contacts:
                    try:
                        fb = requests.get(
                            f"https://api.adsb.lol/v2/lat/{la}/lon/{lo}/dist/250",
                            timeout=15, headers={"User-Agent": "Pyxis/4.1"})
                        if fb.status_code == 200:
                            for ac in (fb.json().get("ac") or fb.json().get("aircraft") or []):
                                try:
                                    ac_lat, ac_lon = float(ac.get("lat",0)), float(ac.get("lon",0))
                                    dLat=(ac_lat-la)*60.0; dLon=(ac_lon-lo)*60.0*math.cos(math.radians(la))
                                    rng=math.sqrt(dLat**2+dLon**2)
                                    brg=math.degrees(math.atan2(dLon,dLat)); brg=round(brg if brg>=0 else brg+360,1)
                                    sq=str(ac.get("squawk",""))
                                    pri={"7700":"MAYDAY","7600":"RADIO FAIL","7500":"HIJACK"}.get(sq,"ROUTINE")
                                    contacts.append({"icao":ac.get("hex",""),"callsign":(ac.get("flight") or ac.get("hex","")).strip(),
                                        "country":ac.get("r",""),"lat":round(ac_lat,4),"lon":round(ac_lon,4),
                                        "alt_ft":int(ac.get("alt_baro",0) or 0),"spd_kts":int(ac.get("gs",0) or 0),
                                        "track":int(ac.get("track",0) or 0),"squawk":sq,"priority":pri,
                                        "on_ground":ac.get("alt_baro")=="ground","range_nm":round(rng,1),"bearing":brg,"type":"AIRCRAFT"})
                                except: continue
                            log(f"ADS-B (adsb.lol v2): {len(contacts)} aircraft")
                        else:
                            log(f"ADS-B adsb.lol v2 HTTP {fb.status_code}")
                    except Exception as fe: log(f"ADS-B adsb.lol err: {fe}")

                # Fallback 2: adsb.one (different network — works from GCP IPs)
                if not contacts:
                    try:
                        fb2 = requests.get(
                            f"https://api.adsb.one/v2/lat/{la}/lon/{lo}/dist/250",
                            timeout=15, headers={"User-Agent": "Pyxis/4.1"})
                        if fb2.status_code == 200:
                            for ac in (fb2.json().get("ac") or fb2.json().get("aircraft") or []):
                                try:
                                    ac_lat, ac_lon = float(ac.get("lat",0)), float(ac.get("lon",0))
                                    dLat=(ac_lat-la)*60.0; dLon=(ac_lon-lo)*60.0*math.cos(math.radians(la))
                                    rng=math.sqrt(dLat**2+dLon**2)
                                    brg=math.degrees(math.atan2(dLon,dLat)); brg=round(brg if brg>=0 else brg+360,1)
                                    sq=str(ac.get("squawk",""))
                                    pri={"7700":"MAYDAY","7600":"RADIO FAIL","7500":"HIJACK"}.get(sq,"ROUTINE")
                                    contacts.append({"icao":ac.get("hex",""),"callsign":(ac.get("flight") or ac.get("hex","")).strip(),
                                        "country":ac.get("r",""),"lat":round(ac_lat,4),"lon":round(ac_lon,4),
                                        "alt_ft":int(ac.get("alt_baro",0) or 0),"spd_kts":int(ac.get("gs",0) or 0),
                                        "track":int(ac.get("track",0) or 0),"squawk":sq,"priority":pri,
                                        "on_ground":ac.get("alt_baro")=="ground","range_nm":round(rng,1),"bearing":brg,"type":"AIRCRAFT"})
                                except: continue
                            log(f"ADS-B (adsb.one): {len(contacts)} aircraft")
                        else:
                            log(f"ADS-B adsb.one HTTP {fb2.status_code}")
                    except Exception as fe2: log(f"ADS-B adsb.one err: {fe2}")

                # Fallback 3: adsb.fi (confirmed reachable from GCP, good Asia/international coverage)
                if not contacts:
                    try:
                        fb3 = requests.get(
                            f"https://opendata.adsb.fi/api/v2/lat/{la:.3f}/lon/{lo:.3f}/dist/250",
                            timeout=15, headers={"User-Agent": "Pyxis/4.1"})
                        if fb3.status_code == 200:
                            for ac in (fb3.json().get("aircraft") or fb3.json().get("ac") or []):
                                try:
                                    ac_lat, ac_lon = float(ac.get("lat",0)), float(ac.get("lon",0))
                                    ab = ac.get("alt_baro")
                                    on_gnd = (ab == "ground")
                                    if on_gnd: continue
                                    try: alt_ft = int(float(ab) * 3.28084) if ab and ab != "ground" else 0
                                    except: alt_ft = 0
                                    dLat=(ac_lat-la)*60.0; dLon=(ac_lon-lo)*60.0*math.cos(math.radians(la))
                                    rng=math.sqrt(dLat**2+dLon**2)
                                    brg=math.degrees(math.atan2(dLon,dLat)); brg=round(brg if brg>=0 else brg+360,1)
                                    sq=str(ac.get("squawk",""))
                                    pri={"7700":"MAYDAY","7600":"RADIO FAIL","7500":"HIJACK"}.get(sq,"ROUTINE")
                                    contacts.append({"icao":ac.get("hex",""),"callsign":(ac.get("flight") or ac.get("hex","")).strip(),
                                        "country":ac.get("r",""),"lat":round(ac_lat,4),"lon":round(ac_lon,4),
                                        "alt_ft":alt_ft,"spd_kts":int(ac.get("gs",0) or 0),
                                        "track":int(ac.get("track",0) or 0),"squawk":sq,"priority":pri,
                                        "on_ground":False,"range_nm":round(rng,1),"bearing":brg,"type":"AIRCRAFT"})
                                except: continue
                            log(f"ADS-B (adsb.fi): {len(contacts)} aircraft")
                        else:
                            log(f"ADS-B adsb.fi HTTP {fb3.status_code}")
                    except Exception as fe3: log(f"ADS-B adsb.fi err: {fe3}")

                if contacts:
                    contacts.sort(key=lambda x:(0 if x["priority"]!="ROUTINE" else 1,x["range_nm"]))
                    with adsb_cache_lock: adsb_cache=contacts
                    with open(ADSB_CACHE_FILE,"w") as f: json.dump(contacts,f)
                elif os.path.exists(ADSB_CACHE_FILE):
                    # All fallbacks failed — load standalone worker's fresh cache from disk
                    try:
                        with open(ADSB_CACHE_FILE) as _cf:
                            disk_contacts = json.load(_cf)
                        if disk_contacts:
                            with adsb_cache_lock: adsb_cache = disk_contacts
                            log(f"ADS-B: all APIs failed, loaded {len(disk_contacts)} contacts from disk cache")
                    except Exception as dce: log(f"ADS-B disk cache load err: {dce}")
                time.sleep(300); continue

            if r.status_code == 200:
                data = r.json()
                states = data.get("states", []) or []
                contacts = []
                import math
                for s in states:
                    try:
                        # OpenSky state vector: [icao24, callsign, origin_country,
                        # time_pos, last_contact, lon, lat, baro_alt, on_ground,
                        # velocity, true_track, vertical_rate, sensors, geo_alt,
                        # squawk, spi, position_source]
                        if s[5] is None or s[6] is None: continue
                        ac_lon, ac_lat = float(s[5]), float(s[6])
                        call = (s[1] or "").strip() or s[0] or "UNKNOWN"
                        alt_m = s[13] if s[13] is not None else (s[7] or 0)
                        spd_ms = s[9] or 0
                        track = s[10] or 0
                        squawk = s[14] or ""
                        on_gnd = bool(s[8])
                        country = s[2] or ""

                        # Range & Bearing from vessel
                        dLat = (ac_lat - la) * 60.0
                        dLon = (ac_lon - lo) * 60.0 * math.cos(math.radians(la))
                        rng = math.sqrt(dLat**2 + dLon**2)
                        brg = math.degrees(math.atan2(dLon, dLat))
                        brg = round(brg if brg >= 0 else brg + 360.0, 1)

                        # Emergency squawk classification
                        priority = "ROUTINE"
                        if squawk == "7700": priority = "MAYDAY"
                        elif squawk == "7600": priority = "RADIO FAIL"
                        elif squawk == "7500": priority = "HIJACK"
                        elif squawk == "7000": priority = "VFR"

                        contacts.append({
                            "icao": s[0],
                            "callsign": call,
                            "country": country,
                            "lat": round(ac_lat, 4),
                            "lon": round(ac_lon, 4),
                            "alt_ft": round((alt_m or 0) * 3.28084),
                            "spd_kts": round(spd_ms * 1.944),
                            "track": round(track),
                            "squawk": squawk,
                            "priority": priority,
                            "on_ground": on_gnd,
                            "range_nm": round(rng, 1),
                            "bearing": brg,
                            "type": "AIRCRAFT"
                        })
                    except: continue

                # Sort by range, flag emergencies first
                if contacts:
                    contacts.sort(key=lambda x: (0 if x["priority"] != "ROUTINE" else 1, x["range_nm"]))
                    with adsb_cache_lock:
                        adsb_cache = contacts
                    with open(ADSB_CACHE_FILE, "w") as f:
                        json.dump(contacts, f)
                    log(f"ADS-B Worker: {len(contacts)} aircraft within 200nm (OpenSky).")
                else:
                    log(f"ADS-B Worker: OpenSky 200 but all {len(states)} states had null position — cache preserved, fallbacks will run next cycle")
            else:
                log(f"ADS-B Worker: OpenSky returned {r.status_code} Ã¢â‚¬â€ rate limited, backing off.")
                time.sleep(120)
        except Exception as e:
            log(f"ADS-B Worker Err: {e}")
        time.sleep(300)  # Poll every 5 minutes (12 req/hr, well within free limits)

threading.Thread(target=adsb_worker, daemon=True).start()


GEO_CACHE_FILE = os.path.join(B, "geo_cache.json")

def geo_worker():
    """
    Background thread that runs every 30 minutes to determine the ship's 
    current operational theater. Uses OpenStreetMap Nominatim for Reverse 
    Geocoding to yield the ocean/region name, and calculates local sunrise/sunset 
    times so the LLM has temporal context. Dumps data to 'geo_cache.json'.
    """
    while True:
        try:
            global last_known_lat, last_known_lon
            
            ss_res = requests.get(f"https://api.sunrise-sunset.org/json?lat={last_known_lat}&lng={last_known_lon}&formatted=0", timeout=10)
            ss_data = ss_res.json()
            
            headers = {"User-Agent": "PyxisTacticalAI/1.0"}
            nom_res = requests.get(f"https://nominatim.openstreetmap.org/reverse?format=json&lat={last_known_lat}&lon={last_known_lon}&zoom=10", headers=headers, timeout=10)
            nom_data = nom_res.json()
            
            local_area = "Open Ocean"
            if "display_name" in nom_data:
                parts = nom_data["display_name"].split(",")
                local_area = parts[0].strip()
                if len(parts) > 1 and local_area.isdigit():
                     local_area = parts[1].strip() # Skip street addresses if zoomed in too far
                     
            try:
                from datetime import datetime, timedelta
                def fmt_time(iso):
                    if not iso: return ""
                    dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
                    dt_local = dt + timedelta(hours=(float(last_known_lon) / 15.0))
                    return dt_local.strftime("%H:%M")
            except: 
                fmt_time = lambda x: ""
                
            geo_data = {
                "sunrise": fmt_time(ss_data.get("results", {}).get("sunrise")),
                "sunset": fmt_time(ss_data.get("results", {}).get("sunset")),
                "local_area": local_area
            }
            
            with open(GEO_CACHE_FILE, "w") as f:
                json.dump(geo_data, f)
                
        except Exception as e:
            log(f"Geo Worker Err: {e}")
            
        time.sleep(1800) # Check every 30 mins

threading.Thread(target=geo_worker, daemon=True).start()

OSINT_CACHE_FILE = os.path.join(B, "osint_cache.json")

def osint_worker():
    """
    Background thread running every 30 minutes. Aggregates Open Source Intelligence
    (OSINT) to bolster the simulated BVR (Beyond Visual Range) radar picture.
    1. Fetches breaking maritime/naval news from the GDELT Project.
    2. Fetches incoming thermal satellite anomaly coordinates via NASA FIRMS.
    Dumps findings to 'osint_cache.json' to inject real-world geopolitics into briefs.
    """
    while True:
        try:
            global last_known_lat, last_known_lon
            osint_data = {"thermal": [], "news": []}
            
            # GDELT Geopolitics Fetch (Added User-Agent to prevent 403 Forbidden / JSON char 0 errors)
            try:
                g_url = "https://api.gdeltproject.org/api/v2/doc/doc?query=maritime%20OR%20piracy%20OR%20naval&mode=artlist&maxrecords=5&format=json"
                r = requests.get(g_url, headers={'User-Agent': 'Mozilla/5.0 Pyxis OSINT'}, timeout=10)
                if r.status_code == 200:
                    try:
                        news = []
                        for art in r.json().get("articles", [])[:3]:
                            news.append(art.get("title", "")[:100])
                        osint_data["news"] = news
                    except Exception:
                        pass # Silently drop non-JSON Cloudflare bot-challenges
            except Exception as e: log(f"OSINT GDELT Err: {e}")
            
            # NASA FIRMS Thermal Fetch
            firms_key = os.getenv("FIRMS_MAP_KEY", "")
            if firms_key:
                try:
                    sz = 1.0 
                    lat_min, lat_max = max(-90.0, last_known_lat - sz), min(90.0, last_known_lat + sz)
                    lon_min, lon_max = max(-180.0, last_known_lon - sz), min(180.0, last_known_lon + sz)
                    f_url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{firms_key}/VIIRS_SNPP_NRT/{lon_min},{lat_min},{lon_max},{lat_max}/1"
                    r = requests.get(f_url, timeout=15)
                    if r.status_code == 200:
                        lines = r.text.strip().split("\n")
                        anomalies = []
                        if len(lines) > 1:
                            for line in lines[1:]:
                                pts = line.split(",")
                                if len(pts) >= 2: anomalies.append({"lat": float(pts[0]), "lon": float(pts[1])})
                        osint_data["thermal"] = anomalies
                except Exception as e: log(f"OSINT FIRMS Err: {e}")

            with open(OSINT_CACHE_FILE, "w") as f:
                json.dump(osint_data, f)
        except Exception as e:
            log(f"OSINT Worker Err: {e}")
            
        time.sleep(1800) # Every 30 mins

threading.Thread(target=osint_worker, daemon=True).start()

# --- NEW API WORKERS ---

def space_weather_worker():
    while True:
        try:
            r = requests.get('https://services.swpc.noaa.gov/json/planetary_k_index_1m.json', timeout=15)
            if r.status_code == 200:
                data = r.json()
                latest = data[-1] if data else {}
                swpc_data = {"kp_index": latest.get("kp_index"), "time": latest.get("time_tag")}
                with open(SWPC_CACHE_FILE, "w") as f:
                    json.dump(swpc_data, f)
                log(f"SWPC Worker: Cached Planetary K-Index {swpc_data['kp_index']}")
        except Exception as e:
            log(f"SWPC Worker Err: {e}")
        time.sleep(3600)

threading.Thread(target=space_weather_worker, daemon=True).start()

def piracy_worker():
    while True:
        try:
            r = requests.get('https://msi.nga.mil/api/publications/asam?output=json', timeout=15, verify=False)
            if r.status_code == 200:
                data = r.json().get('asam', [])
                with open(ASAM_CACHE_FILE, "w") as f:
                    json.dump(data[:50], f)
                log(f"ASAM Worker: Cached latest 50 piracy incidents.")
        except Exception as e:
            log(f"ASAM Worker Err: {e}")
        time.sleep(3600)

threading.Thread(target=piracy_worker, daemon=True).start()

def seismic_worker():
    while True:
        try:
            r = requests.get('https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson', timeout=15)
            if r.status_code == 200:
                data = r.json().get('features', [])
                with open(SEISMIC_CACHE_FILE, "w") as f:
                    json.dump(data, f)
                log(f"Seismic Worker: Cached {len(data)} recent earthquakes.")
        except Exception as e:
            log(f"Seismic Worker Err: {e}")
        time.sleep(900)

threading.Thread(target=seismic_worker, daemon=True).start()

def meteo_worker():
    while True:
        try:
            global last_known_lat, last_known_lon
            r = requests.get(f'https://marine-api.open-meteo.com/v1/marine?latitude={last_known_lat}&longitude={last_known_lon}&current=wave_height,wave_direction,wave_period,ocean_current_velocity', timeout=15)
            r2 = requests.get(f'https://api.opentopodata.org/v1/etopo1?locations={last_known_lat},{last_known_lon}', timeout=15)
            # Real-time wind from Open-Meteo Forecast API (same source as Wind Map)
            r3 = requests.get(f'https://api.open-meteo.com/v1/forecast?latitude={last_known_lat}&longitude={last_known_lon}&current=wind_speed_10m,wind_direction_10m&wind_speed_unit=kn', timeout=15)
            meteo_data = {"marine": {}, "depth": 0, "updated": time.time()}
            if r.status_code == 200:
                meteo_data["marine"] = r.json().get('current', {})
            if r2.status_code == 200:
                res = r2.json().get('results', [])
                if res: meteo_data["depth"] = res[0].get("elevation", 0)
            if r3.status_code == 200:
                wind_cur = r3.json().get('current', {})
                meteo_data["wind"] = {
                    "speed_kn":  wind_cur.get('wind_speed_10m'),
                    "dir_deg":   wind_cur.get('wind_direction_10m'),
                }
                log(f"Meteo Worker: Wind {meteo_data['wind']['speed_kn']}kn @ {meteo_data['wind']['dir_deg']}deg")
            with open(METEO_CACHE_FILE, "w") as f:
                json.dump(meteo_data, f)
            log("Meteo Worker: Cached current sea state, bathymetry and wind.")
        except Exception as e:
            log(f"Meteo Worker Err: {e}")
        time.sleep(1800)

threading.Thread(target=meteo_worker, daemon=True).start()

def get_navarea(lat, lon):
    """
    Extremely simplified GMDSS NAVAREA boundary logic for Pyxis OSINT localization.
    Maps a physical lat/lon coordinate to one of the 21 global NAVAREAs so that
    the AI only analyzes military warnings relevant to the vessel's theater.
    """
    if -30 <= lon <= 45 and lat >= 48: return "I"     # UK / North Sea
    elif -30 <= lon <= 10 and 6 <= lat < 48: return "II"    # France / E Atlantic
    elif -10 <= lon <= 40 and 30 <= lat < 48: return "III"  # Med / Black Sea
    elif -120 <= lon <= -35 and 7 <= lat <= 67: return "IV" # US East / Carib
    elif -50 <= lon <= 20 and lat < 6: return "V"     # Brazil / SW Atlantic
    elif -120 <= lon <= -50 and lat < -20: return "VI"  # Argentina
    elif 20 <= lon <= 45 and lat < -10: return "VII"    # South Africa
    elif 45 <= lon <= 100 and lat < 30: return "VIII"   # India / Indian Ocean
    elif 30 <= lon <= 60 and 10 <= lat <= 30: return "IX"   # Red Sea / Persian Gulf
    elif 80 <= lon <= 170 and -45 <= lat <= 0: return "X"   # Australia
    elif 100 <= lon <= 170 and 0 < lat <= 45: return "XI"   # SE Asia / Japan 
    elif -180 <= lon <= -120 and 0 <= lat <= 67: return "XII" # US West / Hawaii
    elif 130 <= lon <= -180 and 45 < lat <= 67: return "XIII" # Russia Pacific
    elif 170 <= lon <= -120 and lat < 0: return "XIV"   # New Zealand
    elif -120 <= lon <= -70 and lat < 0: return "XV"    # Chile
    elif -120 <= lon <= -70 and 0 <= lat < 7: return "XVI"  # Peru
    elif lat >= 67: return "XVII" # Arctic
    else: return "UNKNOWN"

@app.route('/intel_feed', methods=['POST'])
def intel_feed():
    """
    Direct HTTP endpoint designed for the Pyxis Lite dashboard.
    Forces Google Gemini to immediately parse the GMDSS Navigational Warnings
    cache, isolate the warnings localized to the user's NAVAREA, and synthesize
    a raw JSON text report breaking down the top 5 kinetic/safety threats.
    """
    try:
        data = request.json or {}
        # Read real vessel position from sim telemetry, fall back to last known
        la, lo = last_known_lat, last_known_lon
        sim_sensors = {}
        if os.path.exists(SIM):
            try:
                with open(SIM, "r") as f:
                    st = json.load(f)
                    la = st.get("BOAT_LAT", la)
                    lo = st.get("BOAT_LON", lo)
                    sim_sensors = st
            except: pass
        # Also check sim_override.json for injected sensor overrides
        SIM_OVR = os.path.join(B, "sim_override.json")
        if os.path.exists(SIM_OVR):
            try:
                with open(SIM_OVR, "r") as f:
                    ovr = json.load(f)
                    sim_sensors.update(ovr)
                    if ovr.get("base_lat"): la = ovr["base_lat"]
                    if ovr.get("base_lon"): lo = ovr["base_lon"]
            except: pass
        
        my_navarea = get_navarea(la, lo)
        
        gmdss_data = []
        if os.path.exists(GMDSS_CACHE_FILE):
            try:
                with open(GMDSS_CACHE_FILE, "r") as f:
                    gmdss_data = json.load(f)
            except: pass
            
        local_warnings = []
        global_warnings = []
        
        for w in gmdss_data:
            if w.get('navArea') == my_navarea:
                local_warnings.append(w)
            else:
                global_warnings.append(w)
                
        # Extract existing GDACS Weather Events
        gdacs_w = []
        try:
            global osint_cache_list
            for c in osint_cache_list:
                if c.get("type") == "OSINT_WEATHER":
                    gdacs_w.append(f"{c.get('name')} at {c.get('lat')},{c.get('lon')}")
        except: pass
        weather_str = "\n".join(gdacs_w) if gdacs_w else "No severe GDACS meteorological events detected in cache."

        # Support NTM
        ntm_str = "No Notice to Mariners cache available."
        try:
            if os.path.exists(NTM_CACHE_FILE):
                with open(NTM_CACHE_FILE, "r") as f:
                    ntm_data = json.load(f)
                    ntm_str = str(ntm_data)[:1000] # Summarize first 1KB
        except: pass

        local_str = "No active local GMDSS warnings."
        if local_warnings:
            local_str = "\n".join([f"AREA {w.get('navArea')} [{','.join(w.get('threat_categories', []))}]: {w.get('text')}" for w in local_warnings[:5]])
            
        global_str = "No remote global warnings."
        if global_warnings:
            global_str = "\n".join([f"AREA {w.get('navArea')} [{','.join(w.get('threat_categories', []))}]: {w.get('text')}" for w in global_warnings[:5]])

        swpc_str, asam_str, seismic_str, meteo_str = "No payload", "No payload", "No payload", "No payload"
        try:
            if os.path.exists(SWPC_CACHE_FILE):
                with open(SWPC_CACHE_FILE, "r") as f: swpc_str = str(json.load(f))
            if os.path.exists(ASAM_CACHE_FILE):
                with open(ASAM_CACHE_FILE, "r") as f:
                    asam_all = json.load(f)
                    # Sort by haversine distance to current position Ã¢â‚¬â€ nearest incidents first
                    import math
                    def _hav(la1,lo1,la2,lo2):
                        R=6371; dLat=math.radians(la2-la1); dLon=math.radians(lo2-lo1)
                        a=math.sin(dLat/2)**2+math.cos(math.radians(la1))*math.cos(math.radians(la2))*math.sin(dLon/2)**2
                        return R*2*math.atan2(math.sqrt(a),math.sqrt(1-a))
                    def _asam_dist(inc):
                        try: return _hav(la, lo, float(inc.get("lat",0)), float(inc.get("lon",0)))
                        except: return 999999
                    nearby = sorted(asam_all, key=_asam_dist)[:8]
                    asam_str = str(nearby)
            if os.path.exists(SEISMIC_CACHE_FILE):
                with open(SEISMIC_CACHE_FILE, "r") as f: seismic_str = str(json.load(f)[:5])
            if os.path.exists(METEO_CACHE_FILE):
                with open(METEO_CACHE_FILE, "r") as f: meteo_str = str(json.load(f))
        except: pass

        # ADS-B Aircraft contacts (OpenSky, sorted nearest-first)
        adsb_str = "No aircraft contacts in cache."
        try:
            with adsb_cache_lock:
                ac_nearby = sorted(adsb_cache,
                    key=lambda x: (0 if x.get("priority","ROUTINE")!="ROUTINE" else 1,
                                   x.get("range_nm", 9999)))[:12]
            if ac_nearby:
                adsb_lines = []
                for ac in ac_nearby:
                    em = f" Ã¢Å¡Â  SQUAWK {ac['squawk']} [{ac['priority']}]" if ac['priority'] != "ROUTINE" else ""
                    adsb_lines.append(
                        f"{ac['callsign']} ({ac['country']}) | {ac['range_nm']}nm @{ac['bearing']}Ã‚Â° | "
                        f"FL{ac['alt_ft']//100} | {ac['spd_kts']}kts | TRK:{ac['track']}Ã‚Â°{em}"
                    )
                adsb_str = "\n".join(adsb_lines)
        except Exception as e:
            log(f"ADS-B intel inject err: {e}")

        # --- VESSEL SYSTEMS STATUS BLOCK ---
        vs = sim_sensors
        def alarm(v, label): return f"Ã¢Å¡Â  {label} ALARM" if v else f"{label}: OK"
        vessel_str = f"""VESSEL SYSTEMS STATUS (50ft Sailing Vessel Ã¢â‚¬â€ Pyxis):
  Propulsion  : {vs.get('rpm', 'N/A')} RPM | Coolant {vs.get('coolant_temp_c', 'N/A')}Ã‚Â°C | Oil {vs.get('oil_press_psi', 'N/A')} PSI | Sail Drive {vs.get('hyd_oil_pct', 'N/A')}% | Fuel {vs.get('fuel_pct', 'N/A')}%
  Power Grid  : House Bat {vs.get('house_v', 'N/A')}V ({vs.get('house_soc', 'N/A')}% SOC) | Start Bat {vs.get('start_v', 'N/A')}V | Solar {vs.get('solar_w', 'N/A')}W | Inverter {vs.get('inverter_w', 'N/A')}W
  Navigation  : SOG {vs.get('sog_kts', vs.get('SOG', 'N/A'))} kts | App Wind {vs.get('aws_kts', 'N/A')} kts / {vs.get('awa_deg', 'N/A')}Ã‚Â° | True Wind {vs.get('tws_kts', 'N/A')} kts | Depth {vs.get('depth_m', vs.get('DEPTH', 'N/A'))} m
  Safety      : {alarm(vs.get('fire_alarm'), 'Fire')} | Bilge {vs.get('bilge_pct', 0)}% | {alarm(vs.get('bilge_pump_active'), 'Bilge Pump')} | Fresh Water {vs.get('fresh_water_pct', 'N/A')}% | ER Temp {vs.get('er_temp_c', 'N/A')}Ã‚Â°C
  Autopilot   : {vs.get('ap_state', 'STANDBY')} | Nav Lights: {'ON' if vs.get('nav_lights') else 'OFF'} | Sea Temp {vs.get('sea_temp_c', 'N/A')}Ã‚Â°C"""
        
        p = f"""PYXIS TACTICAL INTELLIGENCE REQUEST
VESSEL POSITION: {la:.4f}Ã‚Â°N, {lo:.4f}Ã‚Â°E Ã¢â‚¬â€ GMDSS NAVAREA {my_navarea}
MISSION: Provide a real-time intelligence brief for a 50ft sailing vessel transiting this area.

STEP 1 Ã¢â‚¬â€ MANDATORY: Use your Google Search tool RIGHT NOW to search for:
  - Current military activity, naval exercises, or restrictions near {la:.2f}, {lo:.2f}
  - Active GMDSS NAVAREA {my_navarea} navigational warnings
  - Recent piracy or maritime security incidents within 500nm of {la:.2f}, {lo:.2f}
  - Any active conflicts, blockades, or restricted zones relevant to this position
  - Notable vessel traffic patterns, unusual ship movements, or port closures near {la:.2f}, {lo:.2f}
  - Military or unusual aircraft operating near {la:.2f}, {lo:.2f} (check flightradar24, flightaware, military NOTAM sources)

STEP 2 Ã¢â‚¬â€ Combine your search results with the following cached intelligence:

{vessel_str}

LOCAL NAVAREA {my_navarea} GMDSS WARNINGS (CACHED):
{local_str}

GLOBAL GMDSS WARNINGS (CACHED):
{global_str}

NOTICE TO MARINERS (NTM):
{ntm_str}

SEVERE WEATHER ALERTS (GDACS):
{weather_str}

SPACE WEATHER (NOAA SWPC):
{swpc_str}

MARINE PIRACY Ã¢â‚¬â€ NEAREST INCIDENTS TO VESSEL (NGA ASAM, sorted by distance):
{asam_str}

AIRCRAFT CONTACTS Ã¢â‚¬â€ ADS-B (OpenSky Network, nearest first, squawk alerts flagged Ã¢Å¡Â ):
{adsb_str}

SEISMIC & TSUNAMI WATCH (USGS):
{seismic_str}

SEA STATE & BATHYMETRY:
{meteo_str}

REPORTING RULES:
- Lead with the most critical threats to this vessel's safety given its CURRENT POSITION.
- You MUST search for real, verifiable threats Ã¢â‚¬â€ do not ignore your search results.
- Do NOT fabricate data, but DO report confirmed threats found via Google Search even if not in the cached data above.
- Flag any squawk 7700/7600/7500 aircraft as PRIORITY contacts and note proximity to vessel.
- Identify any military, government, or unusual aircraft within 100nm Ã¢â‚¬â€ check callsign/ICAO country prefix.
- Format coordinates as degrees/minutes (e.g., "24Ã‚Â°N 120Ã‚Â°E"). Use Naval DTG for times.
- Be tactical, concise, and specific. Prioritise events within 500nm of the vessel.

Return ONLY valid JSON with top 5 threats (include both maritime and airspace threats):
{{"alerts": [{{"title": "Short Threat Name", "desc": "Detailed tactical summary"}}, ...]}}
If genuinely no threats exist at this position, return {{"alerts": []}}."""



        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        resp = client.models.generate_content(
            model="gemini-2.5-flash", 
            contents=p,
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0.2)
        )
        
        try:
            res = json.loads(re.search(r'\{.*\}', resp.text, re.DOTALL).group())
            return jsonify(res), 200
        except:
            return jsonify({"alerts": [{"title": "OSINT Data", "desc": "Feed processing error."}]}), 200
    except Exception as e:
        log(f"INTEL FEED ERR: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/system_status', methods=['POST'])
@requires_auth
def system_status():
    """
    Vessel Systems Health Report endpoint for the React Tactical Dashboard.
    Reads the live sim sensor data and generates a structured, voice-ready
    SITREP focused entirely on onboard vessel health Ã¢â‚¬â€ power, propulsion,
    safety, and environmental state Ã¢â‚¬â€ using Gemini 2.5 Flash.
    Also synthesizes the text into a .wav audio brief via the TTS pipeline.
    """
    try:
        # Read live sensor state
        sim_sensors = {}
        la, lo = last_known_lat, last_known_lon
        if os.path.exists(SIM):
            try:
                with open(SIM, "r") as f:
                    st = json.load(f)
                    la = st.get("BOAT_LAT", la)
                    lo = st.get("BOAT_LON", lo)
                    sim_sensors.update(st)
            except: pass
        SIM_OVR = os.path.join(B, "sim_override.json")
        if os.path.exists(SIM_OVR):
            try:
                with open(SIM_OVR, "r") as f:
                    ovr = json.load(f)
                    sim_sensors.update(ovr)
                    if ovr.get("base_lat"): la = ovr["base_lat"]
                    if ovr.get("base_lon"): lo = ovr["base_lon"]
            except: pass
        
        vs = sim_sensors
        def alarm(v, label): return f"Ã¢Å¡Â  CRITICAL Ã¢â‚¬â€ {label} ALARM ACTIVE" if v else f"{label}: NOMINAL"
        
        geo_area = "Unknown AOR"
        if os.path.exists(GEO_CACHE_FILE):
            try:
                with open(GEO_CACHE_FILE) as f: geo_area = json.load(f).get("local_area", geo_area)
            except: pass
        
        prompt = f"""You are Pyxis, the AI tactical officer aboard a 50-foot research sailing vessel.
        Generate a concise, professional onboard SYSTEMS STATUS report in naval voice format.
        The report should cover vessel health, any anomalies, and recommendations.
        
        VESSEL : Pyxis (50ft Sailing Vessel)
        POSITION : {la:.4f}Ã‚Â°, {lo:.4f}Ã‚Â° Ã¢â‚¬â€ AOR: {geo_area}
        DTG : {datetime.now(timezone.utc).strftime('%d%H%MZ %b %y').upper()}
        
        PROPULSION:
          Engine RPM     : {vs.get('rpm', 'N/A')}
          Coolant Temp   : {vs.get('coolant_temp_c', 'N/A')} Ã‚Â°C
          Oil Pressure   : {vs.get('oil_press_psi', 'N/A')} PSI
          Sail Drive     : {vs.get('hyd_oil_pct', 'N/A')} % hydraulic oil
          Fuel Level     : {vs.get('fuel_pct', 'N/A')} %
        
        POWER GRID (Victron):
          House Battery  : {vs.get('house_v', 'N/A')} V @ {vs.get('house_soc', 'N/A')} % SOC
          Start Battery  : {vs.get('start_v', 'N/A')} V
          Solar Array    : {vs.get('solar_w', 'N/A')} W
          AC Inverter    : {vs.get('inverter_w', 'N/A')} W
        
        NAVIGATION:
          SOG            : {vs.get('sog_kts', vs.get('SOG', 'N/A'))} kts
          Apparent Wind  : {vs.get('aws_kts', 'N/A')} kts / {vs.get('awa_deg', 'N/A')}Ã‚Â°
          True Wind      : {vs.get('tws_kts', 'N/A')} kts / {vs.get('twd_deg', 'N/A')}Ã‚Â°
          Echo Sonar     : {vs.get('depth_m', vs.get('DEPTH', 'N/A'))} m
          Sea Temp       : {vs.get('sea_temp_c', 'N/A')} Ã‚Â°C
          Autopilot      : {vs.get('ap_state', 'STANDBY')}
          Nav Lights     : {'ON' if vs.get('nav_lights') else 'OFF'}
        
        SAFETY & FLUIDS:
          {alarm(vs.get('fire_alarm'), 'Fire/Smoke Detector')}
          Bilge Level    : {vs.get('bilge_pct', 0)} %
          Bilge Pump     : {'PUMPING' if vs.get('bilge_pump_active') else 'IDLE'}
          Fresh Water    : {vs.get('fresh_water_pct', 'N/A')} %
          Engine Room    : {vs.get('er_temp_c', 'N/A')} Ã‚Â°C
    
        Deliver a well-structured spoken report. Rate overall vessel readiness GREEN/AMBER/RED.
        Highlight any anomalies or items requiring attention. Keep it under 250 words.
        Format for TTS Ã¢â‚¬â€ avoid markdown, symbols or bullet points. Use plain sentences."""
        
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3)
        )
        report_text = resp.text.strip()
        
        # Queue for TTS synthesis  
        task_queue.put(("SYSTEM_STATUS", la, lo, report_text))
        log(f"System Status Report queued for TTS ({len(report_text)} chars)")
        
        return jsonify({"status": "ok", "report": report_text}), 200
    except Exception as e:
        log(f"SYSTEM STATUS ERR: {e}")
        return jsonify({"error": str(e)}), 500

live_ais_cache = {}

def aisstream_worker():
    """
    Background WebSocket thread that maintains a persistent, open connection to 
    the AISStream.io marine traffic network. It dynamically listens for 
    'PositionReport' and 'ShipStaticData' frames within a 150nm bounding box 
    around the vessel's last known location. Caches raw vessel IDs, headings, 
    and speeds into 'live_ais_cache' for the radar multiplexer.
    """
    async def connect_ais():
        global live_ais_cache, last_known_lat, last_known_lon
        api_key = os.getenv("AISSTREAM_API_KEY", "7d6600c5ac976b25a5f581129cf77332a94a48ab")
        if not api_key:
            log("No AISSTREAM_API_KEY found, skipping live AIS.")
            return

        while True:
            try:
                # Expand to 150nm radius to guarantee rich traffic in the Gulf/Hormuz region
                bbox = [[last_known_lat - 2.5, last_known_lon - 2.5], [last_known_lat + 2.5, last_known_lon + 2.5]]
                subscribe_msg = {
                    "APIKey": api_key,
                    "BoundingBoxes": [bbox],
                    "FilterMessageTypes": ["PositionReport", "ShipStaticData"]
                }
                
                # AisStream requires omitting ping frames so disable them to stop the timeout disconnect
                async with websockets.connect("wss://stream.aisstream.io/v0/stream", ping_interval=None, ping_timeout=None) as ws:
                    await ws.send(json.dumps(subscribe_msg))
                    log(f"AisStream Connected for BBox: {bbox}")
                    
                    bbox_center_lat = last_known_lat
                    bbox_center_lon = last_known_lon
                    
                    while True:
                        if abs(bbox_center_lat - last_known_lat) > 0.5 or abs(bbox_center_lon - last_known_lon) > 0.5:
                            log("Pyxis moved out of BBox. Reconnecting AIS...")
                            break
                            
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=15.0)
                        except asyncio.TimeoutError:
                            continue
                            
                        data = json.loads(msg)
                        msg_type = data.get("MessageType")
                        
                        if msg_type == "PositionReport":
                            pr = data.get("Message", {}).get("PositionReport", {})
                            mmsi = str(pr.get("UserID", ""))
                            if mmsi:
                                lat, lon = pr.get("Latitude", 0), pr.get("Longitude", 0)
                                sog = pr.get("Sog", 0)
                                cog = pr.get("Cog", 0)
                                
                                # Forward-reference static data if already cached
                                prev = live_ais_cache.get(mmsi, {})
                                # Name priority: in-memory cache → SQLite (learned from previous sessions) → MMSI
                                name = prev.get("name") or intel_name_get(mmsi) or mmsi
                                destination = prev.get("destination", "")
                                callsign = prev.get("callsign", "")
                                imo = prev.get("imo", "")
                                
                                # Distance calculation relative to Pyxis
                                d_lat, d_lon = (lat - last_known_lat) * 111320.0, (lon - last_known_lon) * 111320.0 * math.cos(math.radians(last_known_lat))
                                dist_m = math.sqrt(d_lat**2 + d_lon**2) if d_lat and d_lon else 0
                                bear = math.degrees(math.atan2(d_lon, d_lat)) if d_lat and d_lon else 0
                                bearing = bear if bear >= 0 else bear + 360.0
                                
                                live_ais_cache[mmsi] = {
                                    "id": mmsi,
                                    "mmsi": mmsi,
                                    "name": name,
                                    "destination": destination,
                                    "callsign": callsign,
                                    "imo": imo,
                                    "type": "MERCHANT",
                                    "lat": lat,
                                    "lon": lon,
                                    "range_nm": round(dist_m / 1852.0, 2),
                                    "bearing": round(bearing, 1),
                                    "heading": cog,
                                    "speed": sog,
                                    "ts": time.time()
                                }
                                
                        elif msg_type == "ShipStaticData":
                            sd = data.get("Message", {}).get("ShipStaticData", {})
                            mmsi = str(sd.get("UserID", ""))
                            if mmsi:
                                name = sd.get("Name", "").strip() or mmsi
                                intel_name_put(mmsi, name)  # Persist to SQLite for future sessions
                                destination = sd.get("Destination", "").strip()
                                callsign = sd.get("CallSign", "").strip()
                                imo = str(sd.get("ImoNumber", "") or "")
                                shiptype = int(sd.get("ShipType", 0) or 0)
                                if mmsi in live_ais_cache:
                                    live_ais_cache[mmsi]["name"] = name
                                    live_ais_cache[mmsi]["mmsi"] = mmsi
                                    live_ais_cache[mmsi]["destination"] = destination
                                    live_ais_cache[mmsi]["callsign"] = callsign
                                    live_ais_cache[mmsi]["imo"] = imo
                                    live_ais_cache[mmsi]["shiptype"] = shiptype
                                else:
                                    live_ais_cache[mmsi] = {"name": name, "mmsi": mmsi, "destination": destination, "callsign": callsign, "imo": imo, "shiptype": shiptype, "ts": time.time()}
            except Exception as e:
                log(f"AisStream Connection Dropped: {e}, reconnecting in 5s...")
                await asyncio.sleep(5)
                
    asyncio.run(connect_ais())

threading.Thread(target=aisstream_worker, daemon=True).start()

def get_active_ais_list(ref_lat=None, ref_lon=None):
    """
    Retrieves the parsed AIS marine traffic dictionary, purging any contacts
    that haven't broadcasted a ping in the last 10 minutes (600 seconds).
    If requested, calculates the precise nautical mile range and bearing from
    the host vessel to each contact so the watch can render them accurately.
    """
    # Purge AIS contacts older than 10 minutes (600 seconds)
    now = time.time()
    active_mmsi = [k for k, v in live_ais_cache.items() if (now - v.get("ts", 0)) < 600]
    for key in list(live_ais_cache.keys()):
        if key not in active_mmsi:
            del live_ais_cache[key]
            
    # Return formatted list, filtering out ones without full location data
    raw_contacts = []
    for _k, _v in live_ais_cache.items():
        if "lat" in _v:
            _c = dict(_v)
            if not _c.get("mmsi"): _c["mmsi"] = str(_k)   # ensure mmsi field is present
            if not _c.get("id"):   _c["id"]   = str(_k)   # watch uses id for ContactActionsMenu
            raw_contacts.append(_c)
    
    # Apply vessel classification to live AIS contacts
    contacts = []
    for c in raw_contacts:
        cv = classify_vessel(c)
        c.update(cv)
        contacts.append(c)
    
    # Restoring OpenSeaMap Geo-Marks from volatile memory array (no classification)
    try:
        global osm_cache, osm_cache_lock
        with osm_cache_lock:
            for node in osm_cache:
                contacts.append({
                    "type": node.get("type", "UNKNOWN"),
                    "name": node.get("name", "GEO-MARK"),
                    "lat": float(node["lat"]),
                    "lon": float(node["lon"])
                })
    except Exception as e:
        log(f"Geo Mark Injection Err: {e}")
    
    if ref_lat is not None and ref_lon is not None:
        try:
            rLat = float(ref_lat)
            rLon = float(ref_lon)
            valid_contacts = []
            for c in contacts:
                dLat = (c["lat"] - rLat) * 60.0
                dLon = (c["lon"] - rLon) * 60.0 * math.cos(math.radians(rLat))
                rng = round(math.sqrt(dLat**2 + dLon**2), 2)
                
                if rng > 150:
                    continue  # Flush ghost contacts from previous simulation regions
                    
                c["range_nm"] = rng
                bear = math.degrees(math.atan2(dLon, dLat))
                c["bearing"] = round(bear if bear >= 0 else bear + 360.0, 1)
                valid_contacts.append(c)
            contacts = valid_contacts
        except Exception as e:
            log(f"Error calculating dynamic radar contacts: {e}")
            
    # Vessels first (numeric MMSI >= 7 digits), geo-marks after — so slice caps never displace real traffic
    def _is_ais_vessel(c):
        mmsi = str(c.get("mmsi") or c.get("id") or "")
        return mmsi.isdigit() and len(mmsi) >= 7
    contacts = sorted(contacts, key=lambda c: (0 if _is_ais_vessel(c) else 1, c.get("range_nm", 9999)))
    return contacts

@app.route('/find_ports', methods=['POST'])
def find_ports():
    """
    Pyxis Lite route. Commands Gemini to use its internal Google Search tools 
    to rapidly identify and return the names of 5 major marinas/ports within 
    repair/refuel range of the current host coordinates.
    """
    la, lo = 25.1527, 55.3896
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    p = f"""Position {la}, {lo}. List up to 5 major marinas/ports within 200nm. Return ONLY JSON: {{"destinations": ["Name", ...]}}"""
    try:
        resp = client.models.generate_content(model="gemini-2.5-flash", config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]), contents=p)
        return jsonify(json.loads(re.search(r'\{.*\}', resp.text, re.DOTALL).group()))
    except: return jsonify({"destinations": ["Scan Failed"]})

@app.route('/find_anchorage', methods=['POST'])
def find_anchorage():
    """
    Pyxis Lite route. Commands Gemini to evaluate local bathymetry and fetch
    the names of 5 well-protected safe anchorages near the physical vessel.
    """
    la, lo = 25.1527, 55.3896
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    p = f"""Find 5 nearest safe anchorages to {la}, {lo} for 2.9m draft. Protected from wind. Return ONLY JSON: {{"destinations": ["Name", ...]}}"""
    try:
        resp = client.models.generate_content(model="gemini-2.5-flash", config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]), contents=p)
        res = json.loads(re.search(r'\{.*\}', resp.text, re.DOTALL).group())
        return jsonify(res)
    except: return jsonify({"destinations": ["Scan Failed"]})

@app.route('/voice_quota', methods=['POST'])
def voice_quota():
    """
    Watch UI Route. Resolves current ElevenLabs character usage quotas using the 
    user's API key. Also controls the 'force_kokoro' proxy state if the user 
    opts to switch the TTS engine entirely offline to the 'Alice' voice.
    """
    global force_kokoro
    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode")
    
    # Allow explicitly toggling the proxy variable from the watch
    if mode == "KOKORO": force_kokoro = True
    elif mode == "ELEVENLABS": force_kokoro = False
    
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        return jsonify({"character_count": 0, "character_limit": 10000, "active_model": ("KOKORO" if force_kokoro else "UNCONFIGURED")})
        
    try:
        # Fetch actual quota limits
        res = requests.get("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": key}, timeout=5)
        if res.status_code == 200:
            data = res.json()
            return jsonify({
                "character_count": data.get("character_count", 0), 
                "character_limit": data.get("character_limit", 10000),
                "active_model": ("KOKORO" if force_kokoro else "ELEVENLABS")
            })
    except Exception as e: log(f"Quota error: {e}")
    
    return jsonify({"character_count": 0, "character_limit": 10000, "active_model": "ERROR"})

def nmea_checksum(sentence):
    """
    Helper function to calculate the mandatory XOR checksum byte required 
    for all NMEA0183 marine electronics sentences (e.g. `$GPRMC`).
    """
    calc = 0
    for char in sentence:
        calc ^= ord(char)
    return f"{calc:02X}"

def dec_to_nmea(dec, is_lat):
    """
    Helper function to format absolute decimal degree coordinates into the 
    Degrees-Minutes (DDMM.MMMM) syntax required by NMEA0183 protocol standards.
    """
    deg = int(abs(dec))
    mins = (abs(dec) - deg) * 60
    dir_char = ('N' if dec >= 0 else 'S') if is_lat else ('E' if dec >= 0 else 'W')
    if is_lat:
        return f"{deg:02d}{mins:07.4f},{dir_char}"
    else:
        return f"{deg:03d}{mins:07.4f},{dir_char}"

@app.route('/set_destination', methods=['POST'])
def set_dest():
    """
    Endpoint that accepts a raw string destination name and requests Gemini to 
    plot a viable 30-waypoint route utilizing pathfinding logic to avoid land 
    and shallow waters. Saves the final trace to 'active_route.json'.
    """
    d = request.json
    dest, la, lo = d.get("destination"), d.get("lat"), d.get("lon")
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    p = f"""Plot course from {la}, {lo} to {dest}. Use up to 30 waypoints. Avoid land. Consider safe depth contours and maritime hazards. Maintain a safe depth of at least 3m at all times. Return ONLY valid raw JSON: {{"waypoints": [[lat, lon], ...]}}"""
    try:
        resp = client.models.generate_content(model="gemini-2.5-flash", config=types.GenerateContentConfig(response_mime_type="application/json"), contents=p)
        route_data = json.loads(resp.text.strip().replace("```json", "").replace("```", ""))
        with open(ROUTE_FILE, "w") as f: json.dump(route_data, f)
        
        # NMEA 0183 Generator for B&G Vulcan integration
        wpts = route_data.get("waypoints", [])
        if wpts:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                
                rte_waypoints = []
                for i, wpt in enumerate(wpts):
                    lat_nmea = dec_to_nmea(wpt[0], True)
                    lon_nmea = dec_to_nmea(wpt[1], False)
                    wpt_name = f"WP{i+1:03d}"
                    rte_waypoints.append(wpt_name)
                    
                    # $GPWPL,4917.16,N,12310.64,W,WP001*checksum
                    wpl_body = f"GPWPL,{lat_nmea},{lon_nmea},{wpt_name}"
                    wpl_sent = f"${wpl_body}*{nmea_checksum(wpl_body)}\r\n"
                    sock.sendto(wpl_sent.encode('utf-8'), ('255.255.255.255', 10110))
                
                # $GPRTE,1,1,c,ROUTE_NAME,WP001,WP002...*checksum
                # Max 8 waypoints per RTE sentence in NMEA 0183
                for chunk_idx in range(0, len(rte_waypoints), 8):
                    chunk = rte_waypoints[chunk_idx:chunk_idx+8]
                    total_msg = (len(rte_waypoints) + 7) // 8
                    msg_num = (chunk_idx // 8) + 1
                    rte_body = f"GPRTE,{total_msg},{msg_num},c,PYXIS_RTE," + ",".join(chunk)
                    rte_sent = f"${rte_body}*{nmea_checksum(rte_body)}\r\n"
                    sock.sendto(rte_sent.encode('utf-8'), ('255.255.255.255', 10110))
            except Exception as e:
                log(f"NMEA Broadcast Err: {e}")

        task_queue.put(("routing", la, lo, "Course plotted to " + dest + " and transmitted to NMEA MFD."))
        return jsonify({"status": "plotted"})
    except: return "Err", 500

@app.route('/clear_route', methods=['POST'])
def clear_route():
    """
    Utility endpoint to delete the currently active navigation route,
    removing the breadcrumbs from the Watch and Web dashboards.
    """
    try:
        if os.path.exists(ROUTE_FILE): os.remove(ROUTE_FILE)
        return "OK", 200
    except: return "Err", 500

known_contacts = set()

def format_xo_report(c):
    """
    Transforms a raw JSON radar contact into a natural-language spoken 
    warning string. Uses authentic British Navy phonetics (e.g. 'Tree' 
    instead of 'Three') to ensure the Alice Kokoro model pronounces 
    bearings clearly over the engine noise.
    """
    # Authentic British Navy Phonetics
    phonetics = {'0': 'Zero', '1': 'Wun', '2': 'Two', '3': 'Tree', '4': 'For', '5': 'Five', '6': 'Six', '7': 'Seven', '8': 'Ait', '9': 'Niner', '.': "Decimal"}
    
    b_str = f"{int(c['bearing']):03d}"
    bear_phon = " ".join([phonetics[ch] for ch in b_str])
    
    r_str = f"{c['range_nm']:.1f}"
    range_phon = " ".join([phonetics.get(ch, ch) for ch in r_str])
    
    if c['type'] == 'MERCHANT':
        return f" Alertcontact. Bearing {bear_phon}. Range {range_phon} miles. Vessel is broadcasting A I S as {c['name']}."
    else:
        return f" Alert new contact. Bearing {bear_phon}. Range {range_phon} miles. Vessel is unidentified. Recommend elevating alert state."

def get_live_ais(lat, lon, radius_nm=5.0):
    """
    Fallback marine traffic proxy if the primary WebSocket connection fails.
    Simulates or pulls local AIS hits securely through OpenMarine/AISHUB 
    so the watch radar always has targets to render and track.
    """
    # Fallback Open Marine API proxy. Generates realistic AIS hits if the external gateway drops.
    try:
        # In a production environment, this would hit AISHUB: requests.get(f"https://data.aishub.net/ws.php?username=YOUR_KEY&format=1&output=json&latmin={lat-0.1}&latmax={lat+0.1}&lonmin={lon-0.1}&lonmax={lon+0.1}")
        res = requests.get(f"https://data.aishub.net/ws.php?username=YOUR_KEY&format=1&output=json&latmin={lat-0.1}&latmax={lat+0.1}&lonmin={lon-0.1}&lonmax={lon+0.1}")
        
	# res = requests.get(f"https://api.vtexplo.com/v1/vessels?lat={lat}&lon={lon}&radius={radius_nm}", timeout=0.5).json()
        return res.get('vessels', [])
    except:
        # Generate 2 realistic local vessels so the Radar view always has Live Data to track
        import random, math
        ais_out = []
        for i in range(2):
            bear = random.uniform(0, 360)
            dist = random.uniform(1.0, radius_nm)
            cx = lat + (dist / 60.0) * math.cos(math.radians(bear))
            cz = lon + (dist / 60.0) * math.sin(math.radians(bear))
            ais_out.append({
                "id": f"AIS_{random.randint(100000000, 999999999)}",
                "name": random.choice(["OOCL LONDON", "APL VANDA", "CSCL GLOBE", "MSC OSCAR", "EVER GIVEN", "CMA CGM ANTOINE"]),
                "type": "MERCHANT",
                "lat": cx, "lon": cz,
                "bearing": bear, "range_nm": dist
            })
        return ais_out

# Per-zoom weather cache: {zoom_level: {"time": ..., "img": ..., "lat": ..., "lon": ...}}
weather_cache = {}
weather_cache_lock = threading.Lock()

# Per-zoom ADSB radar cache (same structure, separate from WX)
adsb_radar_cache = {}
adsb_radar_cache_lock = threading.Lock()
_ADSB_PREWARM_ZOOMS = [6, 7, 8, 9, 10, 11]   # z=6Ã¢â€°Ë†200nm  z=8Ã¢â€°Ë†50nm(def)  z=11Ã¢â€°Ë†5nm

def weather_prewarm_worker():
    """Background thread: pre-generates maps at zoom levels 4, 6, 8 every 5 minutes."""
    import io
    PREWARM_ZOOMS = [4, 6, 8]
    while True:
        try:
            time.sleep(10)  # Wait for proxy to fully initialize before first run
            for z in PREWARM_ZOOMS:
                try:
                    lat = last_known_lat or -38.487
                    lon = last_known_lon or 145.620
                    img = fetch_stitched_map(lat, lon, z, 260, 260)
                    buf = io.BytesIO()
                    img.save(buf, format='JPEG', quality=35, optimize=True)
                    img_bytes = buf.getvalue()
                    with weather_cache_lock:
                        weather_cache[z] = {"time": time.time(), "img": img_bytes, "lat": round(lat, 1), "lon": round(lon, 1)}
                    log(f"WX Pre-warm: z={z} -> {len(img_bytes)} bytes")
                except Exception as e:
                    import traceback
                    log(f"WX Pre-warm z={z} failed: {e}\n{traceback.format_exc()}")
        except Exception as e:
            log(f"WX Pre-warm worker error: {e}")
        time.sleep(290)  # Re-generate every 5 minutes

threading.Thread(target=weather_prewarm_worker, daemon=True).start()


def adsb_radar_prewarm_worker():
    """
    Background daemon: pre-renders /adsb_radar_map at zoom levels 6-11 so the
    watch gets instant responses. Re-prewarms when vessel moves >5nm from the
    last cached position.
    """
    time.sleep(20)   # Let proxy fully boot before first render
    _last_prewarm_lat = None
    _last_prewarm_lon = None

    def _hav_nm(la1, lo1, la2, lo2):
        import math
        R = 3440.065  # Earth radius in nm
        dLat = math.radians(la2 - la1)
        dLon = math.radians(lo2 - lo1)
        a = math.sin(dLat/2)**2 + math.cos(math.radians(la1)) * math.cos(math.radians(la2)) * math.sin(dLon/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    while True:
        try:
            import io, math
            from PIL import Image, ImageDraw
            lat = last_known_lat or -38.487
            lon = last_known_lon or 145.620

            # Check if position moved >5nm since last prewarm
            moved = (_last_prewarm_lat is None or
                     _hav_nm(lat, lon, _last_prewarm_lat, _last_prewarm_lon) > 5.0)

            if moved:
                log(f"ADSB Pre-warm: vessel at ({lat:.3f},{lon:.3f}), caching z={_ADSB_PREWARM_ZOOMS}")
                for z in _ADSB_PREWARM_ZOOMS:
                    try:
                        # Self-trigger the route handler so caching logic runs there
                        import base64 as _b64
                        _auth = _b64.b64encode(b"admin:manta").decode()
                        r = requests.get(
                            f"https://127.0.0.1:443/adsb_radar_map?z={z}&w=320&h=320&lat={lat}&lon={lon}&_prewarm=1",
                            timeout=30, verify=False,
                            headers={"Authorization": f"Basic {_auth}"}
                        )
                        log(f"ADSB Pre-warm z={z}: {r.status_code} {len(r.content)} bytes")
                    except Exception as pe:
                        log(f"ADSB Pre-warm z={z} failed: {pe}")
                _last_prewarm_lat = lat
                _last_prewarm_lon = lon
        except Exception as e:
            log(f"ADSB Pre-warm worker error: {e}")
        time.sleep(270)   # Check every 4.5 minutes

threading.Thread(target=adsb_radar_prewarm_worker, daemon=True).start()


@app.route('/radar_hd')
@app.route('/radar_hd/<path:dummy>')
@app.route('/wx_map')
@app.route('/wx_map/<path:dummy>')
@app.route('/wx')
@app.route('/wx/<path:dummy>')
def weather_radar(dummy=None):
    """
    Garmin Watch endpoint. Composites a CartoDB dark basemap + RainViewer radar 
    overlay using the existing Pillow stitched renderer for a real weather map.
    Serves with mandatory Content-Length header to prevent Garmin CDN 404 drops.
    """
    global last_known_lat, last_known_lon
    try:
        lat = float(request.args.get('lat', last_known_lat))
        lon = float(request.args.get('lon', last_known_lon))
        zoom = int(request.args.get('z', 6))
        width = int(request.args.get('w', 260))
        height = int(request.args.get('h', 260))
        lat_r = round(lat, 1)
        lon_r = round(lon, 1)
        import io

        # Check per-zoom cache first (5-minute TTL, same location)
        with weather_cache_lock:
            cached = weather_cache.get(zoom)
        if cached and (time.time() - cached["time"] < 300) \
                and cached["lat"] == lat_r and cached["lon"] == lon_r:
            resp = make_response(cached["img"])
            resp.headers.set('Content-Type', 'image/jpeg')
            resp.headers.set('Content-Length', str(len(cached["img"])))
            return resp

        # Generate fresh map via CartoDB + RainViewer stitched renderer
        img = fetch_stitched_map(lat, lon, zoom, width, height)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=35, optimize=True)
        img_bytes = buf.getvalue()
        with weather_cache_lock:
            weather_cache[zoom] = {"time": time.time(), "img": img_bytes, "lat": lat_r, "lon": lon_r}
        resp = make_response(img_bytes)
        resp.headers.set('Content-Type', 'image/jpeg')
        resp.headers.set('Content-Length', str(len(img_bytes)))
        return resp

    except Exception as e:
        import traceback
        log(f"Weather Radar Error: {e}\n{traceback.format_exc()}")

    # Offline fallback - synthetic radar so watch never gets a 404
    try:
        from PIL import Image, ImageDraw
        import io
        img = Image.new('RGB', (260, 260), color=(2, 10, 16))
        draw = ImageDraw.Draw(img)
        for r_frac in [0.2, 0.4, 0.6, 0.8]:
            rad = 260 * r_frac
            draw.ellipse((130 - rad/2, 130 - rad/2, 130 + rad/2, 130 + rad/2), outline=(0, 160, 80), width=1)
        draw.line((130, 0, 130, 260), fill=(0, 200, 100), width=1)
        draw.line((0, 130, 260, 130), fill=(0, 200, 100), width=1)
        draw.ellipse((126, 126, 134, 134), fill=(0, 255, 0))
        draw.text((10, 240), "RADAR OFFLINE", fill=(255, 50, 50))
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=35)
        img_bytes = buf.getvalue()
        resp = make_response(img_bytes)
        resp.headers.set('Content-Type', 'image/jpeg')
        resp.headers.set('Content-Length', str(len(img_bytes)))
        return resp
    except:
        return "Err", 500

@app.route('/sim_ingress', methods=['POST'])
def sim_in():
    """
    The main hook for the Pygame Manta Simulator. It receives massive 
    JSON blocks of simulated BVR (Beyond Visual Range) radar targets 
    and calculates their relative bearing/distance to the Pyxis host.
    Identifies completely new contacts and instantly dispatches them 
    to the 'brain_worker' thread for audio synthesis (contact warnings).
    """
    global known_contacts, last_known_lat, last_known_lon
    payload = request.json
    payload["last_sim_update"] = time.time()
    
    bLat = payload.get("BOAT_LAT", 0.0)
    bLon = payload.get("BOAT_LON", 0.0)
    
    # Map simulator BOAT_LAT/LON to the global app lat/lon so the web player focuses Pyxis instead of the physical watch
    if bLat != 0.0 and bLon != 0.0:
        onboard = False
        try:
            if os.path.exists(DT):
                with open(DT,"r") as f: onboard = json.load(f).get("onboard_mode", False)
        except: pass
        if not onboard:
            payload["lat"] = bLat
            payload["lon"] = bLon
            last_known_lat = bLat
            last_known_lon = bLon
    
    for c in payload.get('radar_contacts', []):
        if "bearing" not in c or "range_nm" not in c:
            # Calculate range and bearing from Pyxis to Contact
            dLat = (c.get("lat", 0) - bLat) * 60.0  # nm
            dLon = (c.get("lon", 0) - bLon) * 60.0 * math.cos(math.radians(bLat)) # nm
            dist = math.sqrt(dLat**2 + dLon**2)
            bear = math.degrees(math.atan2(dLon, dLat))
            bear = bear if bear >= 0 else bear + 360.0
            c["range_nm"] = round(dist, 2)
            c["bearing"] = round(bear, 1)

        cid = c.get('id', '')
        if cid and cid not in known_contacts:
            if "UUV" not in cid and "STRUCT" not in cid: # Ignore stationary objects and own drones
                known_contacts.add(cid)
                report_txt = format_xo_report(c)
                task_queue.put(("systems", c['lat'], c['lon'], report_txt))
                
    payload.pop("audio_history", None)
    with open(SIM, "w") as f: json.dump(payload, f)
    return "OK", 200

@app.route('/adsb_contacts')
@requires_auth
def get_adsb():
    """
    Returns classified ADS-B aircraft contacts from OpenSky Network.
    Normalizes OpenSky field names, applies classify_aircraft() for type/symbol/priority,
    and calculates closing rate + ETA to Pyxis for threat assessment.
    Cache refreshed every 5 min by background worker.
    """
    try:
        global adsb_cache, last_known_lat, last_known_lon
        la, lo = last_known_lat, last_known_lon
        contacts = []
        with adsb_cache_lock:
            contacts = list(adsb_cache)

        enriched = []
        for c in contacts:
            try:
                # ── Field normalization (OpenSky raw → Pyxis standard) ──
                spd = c.get("spd_kts") or c.get("velocity")
                if spd is not None:
                    try: c["spd_kts"] = round(float(spd) * 1.944, 1) if c.get("velocity") else round(float(spd), 1)
                    except: c["spd_kts"] = None
                trk = c.get("track") or c.get("true_track")
                if trk is not None:
                    try: c["track"] = round(float(trk), 1)
                    except: c["track"] = None

                # ── Range & bearing to Pyxis ────────────────────────────
                dLat = (c['lat'] - la) * 60.0
                dLon = (c['lon'] - lo) * 60.0 * math.cos(math.radians(la))
                rng  = round(math.sqrt(dLat**2 + dLon**2), 1)
                brg  = math.degrees(math.atan2(dLon, dLat))
                c['range_nm'] = rng
                c['bearing']  = round(brg if brg >= 0 else brg + 360.0, 1)

                # ── Closing rate & ETA ──────────────────────────────────
                trk_val = c.get("track")
                spd_val = c.get("spd_kts")
                if trk_val is not None and spd_val and rng > 0:
                    # Component of velocity toward Pyxis
                    angle_diff = math.radians(c['bearing'] - trk_val)
                    closing_kts = float(spd_val) * math.cos(angle_diff)
                    c['closing_rate_kts'] = round(closing_kts, 1)
                    if closing_kts > 1 and rng > 0:
                        c['eta_min'] = round((rng / closing_kts) * 60, 1)
                    else:
                        c['eta_min'] = None
                else:
                    c['closing_rate_kts'] = None
                    c['eta_min'] = None

                # ── Classification ──────────────────────────────────────
                cls = classify_aircraft(c)
                c.update(cls)

                # ── Threat override based on proximity + closing ─────────
                cr  = c.get('closing_rate_kts') or 0
                eta = c.get('eta_min')
                if c.get('priority') != 'CRITICAL':
                    if rng < 5 and cr > 5 and eta and eta < 3:
                        c['priority'] = 'CRITICAL'
                    elif rng < 20 and cr > 5:
                        c['priority'] = 'WATCH'

                enriched.append(c)
            except: enriched.append(c)

        enriched.sort(key=lambda x: (
            {'CRITICAL':0,'WATCH':1,'ROUTINE':2}.get(x.get('priority','ROUTINE'), 2),
            x.get('range_nm', 9999)))

        age_s = 9999
        if os.path.exists(ADSB_CACHE_FILE):
            age_s = int(time.time() - os.path.getmtime(ADSB_CACHE_FILE))
        return jsonify({
            "contacts":  enriched[:40],
            "total":     len(enriched),
            "cache_age_s": age_s,
            "vessel_lat": la,
            "vessel_lon": lo
        }), 200
    except Exception as e:
        return jsonify({"error": str(e), "contacts": []}), 500


# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ ICAO Prefix Ã¢â€ â€™ Country/Military lookup (top 60 prefixes) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
ICAO_PREFIX = {
    "7C":"Australia","7D":"Australia","7E":"Australia","7F":"Australia",
    "AE":"USA Military","AF":"USA Military","A0":"USA","A4":"USA","A8":"USA",
    "43":"UK Military","40":"United Kingdom","43":"UK Military",
    "3C":"Germany","3D":"Germany","68":"Germany Military",
    "F0":"France","F8":"France","F4":"France Military",
    "4B":"Italy","4C":"Italy","4D":"Italy",
    "EC":"Spain","34":"Spain",
    "48":"Netherlands","48":"Netherlands Military",
    "70":"Belgium","71":"Belgium",
    "C8":"Canada","C0":"Canada","C4":"Canada Military",
    "86":"China (PRC)","78":"China (PRC)",
    "80":"India","81":"India Military",
    "84":"Japan","82":"Japan Military",
    "71":"South Korea","71":"ROK Military",
    "73":"Russia","74":"Russia Military",
    "01":"South Africa","00":"Zimbabwe",
    "76":"Iran","77":"Kuwait","74":"Syria",
    "B0":"Brazil","B8":"Argentina","E4":"Venezuela",
    "8A":"Australia (RAAF)","8B":"Australia (RAN)",
}

def icao_to_country(icao: str) -> str:
    if not icao or len(icao) < 2: return "Unknown"
    prefix2 = icao[:2].upper()
    prefix1 = icao[:1].upper()
    return ICAO_PREFIX.get(prefix2, ICAO_PREFIX.get(prefix1, "Unknown"))

# ── Intel caches (Gemini-enriched, shared by aircraft + vessel workers) ──
import queue as _queue
aircraft_intel_cache = {}   # icao  -> {type, operator, country, is_military, is_rescue, is_law_enforcement, ts}
vessel_intel_cache   = {}   # mmsi  -> {type, operator, flag, is_military, is_law_enforcement, ts}
_intel_queue = _queue.Queue(maxsize=50)  # (kind, key, payload_dict)
_INTEL_CACHE_TTL = 43200            # 12 hr for aircraft, 24 hr for vessels

def _intel_worker():
    """Background thread — dequeues unknown contacts and enriches them via Gemini+Google Search.
    Rate-limited to 3 calls/min to avoid API exhaustion."""
    import time as _t
    _call_times = []
    while True:
        try:
            kind, key, payload = _intel_queue.get(timeout=5)
        except _queue.Empty:
            continue
        try:
            # Rate limit: max 3 calls per 60s
            now = _t.time()
            _call_times[:] = [t for t in _call_times if now - t < 60]
            if len(_call_times) >= 3:
                _intel_queue.put((kind, key, payload))   # re-queue
                _t.sleep(20)
                continue
            _call_times.append(now)

            _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))

            if kind == "aircraft":
                cs   = payload.get("callsign", "?")
                icao = payload.get("icao", "?")
                prompt = (f"What aircraft is callsign '{cs}' ICAO hex '{icao}'? "
                          f"Is it military, law enforcement, rescue/coast guard, medical/air ambulance, or civil? "
                          f"Return ONLY valid JSON: "
                          f'{{"type":"...","operator":"...","country":"...","is_military":false,"is_rescue":false,"is_law_enforcement":false}}')
                resp = _client.models.generate_content(
                    model="gemini-2.5-flash",
                    config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
                    contents=prompt)
                import re as _re
                m = _re.search(r'\{[^{}]+\}', resp.text, _re.DOTALL)
                if m:
                    data = json.loads(m.group())
                    data["ts"] = _t.time()
                    aircraft_intel_cache[key] = data
                    log(f"Intel [AC] {cs}/{icao}: {data.get('type','?')} / {data.get('operator','?')}")

            elif kind == "vessel":
                name = payload.get("name", "?")
                mmsi = payload.get("mmsi", "?")
                prompt = (f"What type of vessel is '{name}' MMSI {mmsi}? "
                          f"Is it military, law enforcement, coast guard, medical, cargo, tanker, passenger, fishing, or pleasure? "
                          f"Return ONLY valid JSON: "
                          f'{{"type":"...","operator":"...","flag":"...","is_military":false,"is_law_enforcement":false}}')
                resp = _client.models.generate_content(
                    model="gemini-2.5-flash",
                    config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
                    contents=prompt)
                import re as _re
                m = _re.search(r'\{[^{}]+\}', resp.text, _re.DOTALL)
                if m:
                    data = json.loads(m.group())
                    data["ts"] = _t.time()
                    vessel_intel_cache[key] = data
                    log(f"Intel [VS] {name}/{mmsi}: {data.get('type','?')} / {data.get('operator','?')}")

        except Exception as e:
            log(f"Intel worker err ({kind}/{key}): {e}")

threading.Thread(target=_intel_worker, daemon=True).start()

def _queue_aircraft_intel(c):
    """Queue an aircraft for Gemini enrichment if not already cached/queued."""
    icao = (c.get("icao") or "").upper()
    if not icao or icao in aircraft_intel_cache:
        return
    try:
        _intel_queue.put_nowait(("aircraft", icao, {"callsign": c.get("callsign",""), "icao": icao}))
    except _queue.Full:
        pass

def _queue_vessel_intel(c):
    """Queue a vessel for Gemini enrichment if not already cached/queued."""
    mmsi = str(c.get("mmsi",""))
    if not mmsi or mmsi in vessel_intel_cache:
        return
    try:
        _intel_queue.put_nowait(("vessel", mmsi, {"name": c.get("name",""), "mmsi": mmsi}))
    except _queue.Full:
        pass


def classify_aircraft(c: dict) -> dict:
    """Comprehensive aircraft classification: emergency > military > law enforcement > rescue >
    commercial > rotorcraft > UAV > GA. Merges Gemini intel if available."""
    sq       = str(c.get("squawk",""))
    callsign = (c.get("callsign") or "").strip().upper()
    icao     = (c.get("icao") or "").upper()
    cat      = int(c.get("category") or 0)
    country  = icao_to_country(icao)

    # ── EMERGENCY SQUAWKS (immediate override) ──────────────────────────
    if sq == "7700": return {"type":"MAYDAY",        "color":(255,0,0),     "sym":"sq_em", "priority":"CRITICAL", "country":country}
    if sq == "7500": return {"type":"HIJACK",        "color":(255,0,200),   "sym":"sq_em", "priority":"CRITICAL", "country":country}
    if sq == "7600": return {"type":"RADIO FAIL",    "color":(255,140,0),   "sym":"sq_em", "priority":"WATCH",    "country":country}

    # ── ROTORCRAFT & UAV (category first — most reliable) ───────────────
    if cat == 7:  return {"type":"Rotorcraft",       "color":(255,140,0),   "sym":"cross",    "priority":"ROUTINE", "country":country}
    if cat == 12: return {"type":"UAV",              "color":(180,180,0),   "sym":"circle",   "priority":"ROUTINE", "country":country}
    if cat == 10: return {"type":"Glider",           "color":(150,100,255), "sym":"circle",   "priority":"ROUTINE", "country":country}
    if cat == 11: return {"type":"Airship",          "color":(100,255,150), "sym":"oval",     "priority":"ROUTINE", "country":country}
    if cat == 13: return {"type":"Space",            "color":(255,255,100), "sym":"diamond",  "priority":"ROUTINE", "country":country}
    if cat == 14: return {"type":"Surface Emgcy",    "color":(255,0,0),     "sym":"square",   "priority":"CRITICAL","country":country}
    if cat == 19: return {"type":"Obstacle",         "color":(100,100,100), "sym":"square",   "priority":"ROUTINE", "country":country}

    # ── ICAO HEX MILITARY RANGES ─────────────────────────────────────────
    try:
        hv = int(icao, 16)
        mil_ranges = [
            (0xAE0000, 0xAFFFFF, "US Military",     (255,60,60)),
            (0xA92000, 0xA95FFF, "USAF Spec Ops",   (255,40,40)),
            (0x438000, 0x43FFFF, "RAF",             (255,60,60)),
            (0x43C000, 0x43CFFF, "RAF Special",     (255,40,40)),
            (0x3C4000, 0x3CFFFF, "Luftwaffe",       (255,60,60)),
            (0x4B3000, 0x4B3FFF, "Italian AF",      (255,60,60)),
            (0x740000, 0x74FFFF, "Russian MIL",     (255,40,40)),
            (0x820000, 0x83FFFF, "JSDF",            (255,60,60)),
            (0x7C8000, 0x7C8FFF, "RAAF",            (255,60,60)),
            (0x7C9000, 0x7C9FFF, "RAN",             (255,80,40)),
            (0xC80000, 0xC8FFFF, "RCAF",            (255,60,60)),
        ]
        for lo_r, hi_r, lbl, col in mil_ranges:
            if lo_r <= hv <= hi_r:
                return {"type": lbl, "color": col, "sym": "diamond", "priority": "WATCH", "country": country}
    except: pass

    # ── CALLSIGN-BASED CLASSIFICATION ────────────────────────────────────
    # Military callsigns
    MIL_CS = ("RCH","RHC","CNV","CFC","DUKE","REACH","JAKE","KNIFE","EAGLE","RAPTOR",
              "VENOM","ANVIL","HAVOC","BOXER","IRON","STEEL","BLADE","GHOST","TALON",
              "STING","FURY","BANDIT","RANGER","NOMAD","DEMON","SHARK","WOLF",
              "TOPGUN","DRAGON","FALCON","DAGGER","SPEAR","COBRA","RAVEN")
    MIL_PFX = ("AE","AF","01","82","68","4D","F4","74","43")
    if any(callsign.startswith(p) for p in MIL_CS) or icao[:2].upper() in MIL_PFX:
        intel = aircraft_intel_cache.get(icao, {})
        lbl = intel.get("type", "Military")
        return {"type": lbl, "color":(255,60,60), "sym":"diamond", "priority":"WATCH", "country": intel.get("country", country)}

    # Law Enforcement
    LAW_CS = ("POLICE","SHERIFF","BORDER","AFP","VICPOL","FEDPOL","CUSTOMS",
              "LAWENF","PATROL","ENFORCE","CONSTAB","GUARD")
    if any(p in callsign for p in LAW_CS):
        return {"type":"Law Enforcement", "color":(100,100,255), "sym":"diamond", "priority":"WATCH", "country":country}

    # Rescue / Coast Guard / Medical
    SAR_CS = ("RESCUE","RSCU","COAST","SAR","LIFEFLIGHT","CAREFLIGHT","HEMS",
              "MEDIC","AMBU","AIRAMB","CASEVAC","MEDEVAC","GUARDIAN")
    if any(p in callsign for p in SAR_CS):
        return {"type":"Rescue/SAR", "color":(255,100,0), "sym":"cross", "priority":"WATCH", "country":country}

    # Fire / Special Ops
    FIRE_CS = ("FIREBIRD","HELITACK","TANKER","RETARDANT","FIREBOMB","AIRTANK")
    if any(p in callsign for p in FIRE_CS):
        return {"type":"Fire Services", "color":(255,80,0), "sym":"cross", "priority":"WATCH", "country":country}

    # ── CATEGORY-BASED GA ────────────────────────────────────────────────
    if cat == 1: return {"type":"Light GA",  "color":(0,220,255),   "sym":"triangle", "priority":"ROUTINE", "country":country}
    if cat == 2: return {"type":"GA Small",  "color":(0,200,255),   "sym":"triangle", "priority":"ROUTINE", "country":country}
    if cat == 3: return {"type":"GA Large",  "color":(255,255,255), "sym":"triangle", "priority":"ROUTINE", "country":country}
    if cat == 4: return {"type":"VHVW",      "color":(255,200,0),   "sym":"diamond",  "priority":"ROUTINE", "country":country}
    if cat == 5: return {"type":"Heavy",     "color":(200,200,200), "sym":"triangle", "priority":"ROUTINE", "country":country}
    if cat == 6: return {"type":"High Perf", "color":(255,165,0),   "sym":"triangle", "priority":"ROUTINE", "country":country}

    # ── COMMERCIAL AIRLINE (callsign = IATA code + digits) ───────────────
    if callsign and len(callsign) >= 3 and callsign[:3].isalpha() and any(ch.isdigit() for ch in callsign[2:]):
        return {"type":"Commercial", "color":(255,255,255), "sym":"triangle", "priority":"ROUTINE", "country":country}

    # ── GEMINI ENRICHMENT for unknowns ────────────────────────────────────
    intel = aircraft_intel_cache.get(icao)
    if intel and time.time() - intel.get("ts",0) < _INTEL_CACHE_TTL:
        if intel.get("is_military"):        return {"type": intel.get("type","Military"),      "color":(255,60,60),  "sym":"diamond",   "priority":"WATCH",   "country": intel.get("country", country)}
        if intel.get("is_rescue"):          return {"type": intel.get("type","Rescue"),        "color":(255,100,0),  "sym":"cross",     "priority":"WATCH",   "country": intel.get("country", country)}
        if intel.get("is_law_enforcement"): return {"type": intel.get("type","Law Enf"),       "color":(100,100,255),"sym":"diamond",   "priority":"WATCH",   "country": intel.get("country", country)}
        return {"type": intel.get("type","Unknown"), "color":(180,180,180), "sym":"triangle", "priority":"ROUTINE", "country": intel.get("country",country)}
    else:
        _queue_aircraft_intel(c)   # Schedule background Gemini lookup

    return {"type":"Unknown", "color":(180,180,180), "sym":"triangle", "priority":"ROUTINE", "country":country}


# ── AIS Vessel type table (ITU ship type codes 0-99) ──────────────────
_VESSEL_TYPE_MAP = {
    20: ("WIG",             (0,200,180),  "sq"),
    29: ("SAR Aircraft",    (255,60,60),  "cross"),
    30: ("Fishing",         (0,200,80),   "circle"),
    31: ("Towing",          (255,179,0),  "triangle"),
    32: ("Towing Large",    (255,179,0),  "triangle"),
    33: ("Dredging",        (255,140,0),  "sq"),
    34: ("Diving Ops",      (0,200,255),  "circle"),
    35: ("Military",        (255,60,60),  "diamond"),
    36: ("Sailing",         (255,255,255),"triangle"),
    37: ("Pleasure Craft",  (0,220,220),  "circle"),
    40: ("High Speed",      (0,200,255),  "triangle"),
    50: ("Pilot",           (0,200,255),  "circle"),
    51: ("SAR",             (255,60,60),  "cross"),
    52: ("Tug",             (255,179,0),  "triangle"),
    53: ("Port Tender",     (0,200,255),  "circle"),
    54: ("Anti-Pollution",  (0,200,80),   "circle"),
    55: ("Law Enforcement", (100,100,255),"diamond"),
    57: ("Medical",         (255,255,255),"cross"),
    58: ("Fire Fighting",   (255,80,0),   "cross"),
    60: ("Passenger",       (80,140,255), "triangle"),
    70: ("Cargo",           (200,200,200),"triangle"),
    80: ("Tanker",          (255,130,0),  "triangle"),
    90: ("Other",           (160,160,160),"circle"),
}

def classify_vessel(c: dict) -> dict:
    """Classify AIS vessel by ITU shiptype code. Merges Gemini intel for unknowns."""
    st   = int(c.get("shiptype", 0) or 0)
    mmsi = str(c.get("mmsi", ""))

    def _bucket(s):
        if 60 <= s <= 69: return "Passenger",  (80,140,255),  "triangle"
        if 70 <= s <= 79: return "Cargo",       (200,200,200), "triangle"
        if 80 <= s <= 89: return "Tanker",      (255,130,0),   "triangle"
        if 20 <= s <= 28: return "WIG",          (0,200,180),   "sq"
        if 90 <= s <= 99: return "Other",        (160,160,160), "circle"
        return None, None, None

    lbl, col, sym = _VESSEL_TYPE_MAP.get(st, (None,None,None))
    if lbl is None:
        lbl, col, sym = _bucket(st)
    if lbl is None:
        lbl, col, sym = "Unknown", (160,160,160), "circle"

    # Merge Gemini intel if available
    intel = vessel_intel_cache.get(mmsi)
    if intel and time.time() - intel.get("ts",0) < _INTEL_CACHE_TTL * 2:
        if intel.get("is_military"):        lbl, col, sym = intel.get("type","Military"),    (255,60,60),  "diamond"
        elif intel.get("is_law_enforcement"): lbl, col, sym = intel.get("type","Law Enf"),  (100,100,255),"diamond"
        else: lbl = intel.get("type", lbl)
    elif st in (0,90,91,92,93,94,95,96,97,98,99,35):
        _queue_vessel_intel(c)   # Schedule background Gemini lookup

    return {"vessel_type": lbl, "color": col, "sym": sym}


def snap_to_water(lat: float, lon: float) -> tuple:
    """Return (lat, lon, snapped) — if on land (positive ETOPO1 elevation),
    spiral-search nearby ocean until depth<=0 (below sea level) found.
    snapped=True means position was corrected."""
    try:
        r = requests.get(
            f"https://api.opentopodata.org/v1/etopo1?locations={lat},{lon}",
            timeout=4)
        if r.status_code == 200:
            elev = r.json().get("results",[{}])[0].get("elevation", None)
            if elev is not None and float(elev) <= 0:
                return lat, lon, False   # Already at sea
            # On land — spiral search for nearest ocean cell
            for step in range(1, 25):
                delta = step * 0.003
                for dlat, dlon in [(delta,0),(-delta,0),(0,delta),(0,-delta),
                                   (delta,delta),(delta,-delta),(-delta,delta),(-delta,-delta)]:
                    try:
                        clat, clon = lat + dlat, lon + dlon
                        pr = requests.get(
                            f"https://api.opentopodata.org/v1/etopo1?locations={clat},{clon}",
                            timeout=3)
                        if pr.status_code == 200:
                            e2 = pr.json().get("results",[{}])[0].get("elevation", 0)
                            if e2 is not None and float(e2) <= 0:
                                log(f"snap_to_water: ({lat:.4f},{lon:.4f})->({clat:.4f},{clon:.4f})")
                                return clat, clon, True
                    except: pass
    except Exception as ex:
        log(f"snap_to_water err: {ex}")
    return lat, lon, False


def _ll_to_px(lat, lon, cx, cy, pix_per_nm):
    """Convert lat/lon to pixel offset from center (cx,cy) on the map image."""
    dLat = (lat - cy[0]) * 60.0   # nm
    dLon = (lon - cx[1]) * 60.0 * math.cos(math.radians(cy[0]))
    px = int(cx[2] + dLon * pix_per_nm)
    py = int(cy[2] - dLat * pix_per_nm)
    return px, py


def _fetch_cartodb_tile(z, tx, ty):
    """Fetch a CartoDB Dark Matter tile, fall back to OSM if unavailable."""
    # Primary: CartoDB Dark Matter (correct current URL Ã¢â‚¬â€ not the defunct fastly CDN)
    urls = [
        f"https://a.basemaps.cartocdn.com/dark_all/{z}/{tx}/{ty}.png",
        f"https://b.basemaps.cartocdn.com/dark_all/{z}/{tx}/{ty}.png",
        f"https://c.basemaps.cartocdn.com/dark_all/{z}/{tx}/{ty}.png",
        f"https://tile.openstreetmap.org/{z}/{tx}/{ty}.png",   # OSM fallback
    ]
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PyxisTactical/4.1; +https://benfishmanta.duckdns.org)"}
    for url in urls:
        try:
            r = requests.get(url, timeout=8, headers=headers)
            if r.status_code == 200:
                from PIL import Image
                import io
                return Image.open(io.BytesIO(r.content)).convert("RGBA")
            else:
                log(f"tile_fetch: HTTP {r.status_code} for {url}")
        except Exception as te:
            log(f"tile_fetch error ({url}): {te}")
    return None


@app.route('/adsb_radar_map')
def adsb_radar_map():
    """
    Renders a 320Ãƒâ€”320 JPEG tactical aircraft picture:
    - CartoDB Dark Matter base tiles
    - Type-classified aircraft icons with directional velocity vectors
    - 20km danger ring around Pyxis + overflight prediction lines
    Pre-cached at z=6-11 by adsb_radar_prewarm_worker.
    Returns: image/jpeg
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io, math

        W    = int(request.args.get("w", 320))
        H    = int(request.args.get("h", 320))
        zoom = int(request.args.get("z", 8))      # default z=8 Ã¢â€°Ë† 50nm
        zoom = max(6, min(zoom, 11))               # clamp: z=6 (200nm) Ã¢â‚¬â€œ z=11 (5nm)
        la   = float(request.args.get("lat", last_known_lat))
        lo   = float(request.args.get("lon", last_known_lon))

        # Ã¢â€â‚¬Ã¢â€â‚¬ Cache read (5-min TTL, vessel within 3nm) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
        lat_r, lon_r = round(la, 2), round(lo, 2)
        with adsb_radar_cache_lock:
            cached = adsb_radar_cache.get(zoom)
        if (cached and
                time.time() - cached["time"] < 300 and
                abs(cached["lat"] - lat_r) < 0.05 and
                abs(cached["lon"] - lon_r) < 0.05):
            resp = make_response(cached["img"])
            resp.headers.set("Content-Type", "image/jpeg")
            resp.headers.set("Content-Length", str(len(cached["img"])))
            resp.headers.set("X-Cache", "HIT")
            return resp

        # Ã¢â€â‚¬Ã¢â€â‚¬ Tile math Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
        n = 2 ** zoom
        tx_f = (lo + 180.0) / 360.0 * n
        ty_f = (1.0 - math.log(math.tan(math.radians(la)) + 1.0/math.cos(math.radians(la))) / math.pi) / 2.0 * n
        tx_c = int(tx_f); ty_c = int(ty_f)

        # Build a 3Ãƒâ€”3 tile grid centred on the vessel
        TILE_SZ = 256
        canvas = Image.new("RGBA", (TILE_SZ*3, TILE_SZ*3), (10, 14, 26, 255))
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                tile = _fetch_cartodb_tile(zoom, (tx_c+dx)%n, (ty_c+dy)%n)
                if tile:
                    canvas.paste(tile, ((dx+1)*TILE_SZ, (dy+1)*TILE_SZ))

        # Crop to requested size, centred
        cx_pix = int((tx_f - tx_c + 1) * TILE_SZ)
        cy_pix = int((ty_f - ty_c + 1) * TILE_SZ)
        left  = cx_pix - W//2; top = cy_pix - H//2
        canvas = canvas.crop((left, top, left+W, top+H))
        draw = ImageDraw.Draw(canvas)

        # Ã¢â€â‚¬Ã¢â€â‚¬ Correct Mercator pixel helper Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
        # Convert a lat/lon to canvas pixel (cx2, cy2 = canvas centre = vessel pos)
        def _to_px(ac_lat_, ac_lon_):
            lat_c_ = max(-85.0, min(85.0, ac_lat_))
            ac_tx_ = (ac_lon_ + 180.0) / 360.0 * n
            ac_ty_ = (1.0 - math.log(math.tan(math.radians(lat_c_)) +
                      1.0/math.cos(math.radians(lat_c_))) / math.pi) / 2.0 * n
            return (int(cx2 + (ac_tx_ - tx_f) * TILE_SZ),
                    int(cy2 + (ac_ty_ - ty_f) * TILE_SZ))

        # Ã¢â€â‚¬Ã¢â€â‚¬ 20km danger ring Ã¢â‚¬â€ compute radius in pixels Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
        # km per pixel at this zoom and latitude
        km_per_px = 40075.0 * abs(math.cos(math.radians(la))) / (n * TILE_SZ)
        ring_px = int(20.0 / km_per_px) if km_per_px > 0 else 20
        ring_px = max(8, min(ring_px, W // 2 - 4))
        ring_nm = 20.0 / 1.852   # still used for CPA threshold in nm
        cx2, cy2 = W//2, H//2
        draw.ellipse([cx2-ring_px, cy2-ring_px, cx2+ring_px, cy2+ring_px],
                     outline=(255, 120, 0, 200), width=2)
        draw.ellipse([cx2-4, cy2-4, cx2+4, cy2+4], fill=(255, 255, 255, 220))

        # Ã¢â€â‚¬Ã¢â€â‚¬ Plot contacts Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
        with adsb_cache_lock:
            contacts = list(adsb_cache)
        # Also merge from JSON file in case in-memory cache is empty
        if not contacts:
            try:
                cf = os.path.join(B, "adsb_cache.json")
                if os.path.exists(cf):
                    with open(cf) as f_: contacts = json.load(f_)
            except: pass

        for c in contacts:
            try:
                ac_lat = c.get("lat"); ac_lon = c.get("lon")
                if ac_lat is None or ac_lon is None: continue
                if c.get("on_ground"): continue

                px, py = _to_px(ac_lat, ac_lon)
                if px < -30 or py < -30 or px > W+30 or py > H+30: continue

                cls = classify_aircraft(c)
                col = cls["color"] + (230,)
                sym = cls["sym"]
                alt_ft = float(c.get("alt_ft") or 0)
                trk    = float(c.get("track") or 0)
                spd    = float(c.get("spd_kts") or 0)

                # Ã¢â€â‚¬Ã¢â€â‚¬ Velocity vector (5-min projection in lat/lon space) Ã¢â€â‚¬Ã¢â€â‚¬
                if spd > 0:
                    d_nm = (spd / 60.0) * 5
                    vLat = ac_lat + (d_nm * math.cos(math.radians(trk))) / 60.0
                    vLon = ac_lon + (d_nm * math.sin(math.radians(trk))) / \
                           (60.0 * max(0.01, math.cos(math.radians(ac_lat))))
                    vx, vy = _to_px(vLat, vLon)
                    # Cap vector line length so it doesn't go wild
                    vdx, vdy = vx-px, vy-py
                    vlen = math.sqrt(vdx**2 + vdy**2) or 1
                    if vlen > 40: vdx, vdy = int(vdx*40/vlen), int(vdy*40/vlen)
                    draw.line([px, py, px+vdx, py+vdy], fill=cls["color"]+(180,), width=1)

                # Ã¢â€â‚¬Ã¢â€â‚¬ Overflight / 20km zone breach prediction (30 min) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
                range_nm = c.get("range_nm", 999)
                if spd > 50 and range_nm is not None and float(range_nm) < 150:
                    proj_nm = (spd / 60.0) * 30
                    proj_lat = ac_lat + (proj_nm * math.cos(math.radians(trk))) / 60.0
                    proj_lon = ac_lon + (proj_nm * math.sin(math.radians(trk))) / \
                               (60.0 * max(0.01, math.cos(math.radians(ac_lat))))
                    cpaDlat = (proj_lat - la) * 60.0
                    cpaDlon = (proj_lon - lo) * 60.0 * math.cos(math.radians(la))
                    cpa_nm  = math.sqrt(cpaDlat**2 + cpaDlon**2)
                    if cpa_nm < ring_nm:
                        prx, pry = _to_px(proj_lat, proj_lon)
                        line_col = (255,0,0,200) if cpa_nm < 5.4 else (255,180,0,180)
                        draw.line([px, py, prx, pry], fill=(255,200,0,100), width=1)
                        draw.rectangle([prx-3,pry-3,prx+3,pry+3],
                                       outline=line_col, width=1)

                # Ã¢â€â‚¬Ã¢â€â‚¬ Draw symbol Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
                S = 6
                if sym == "triangle":
                    ang_r = math.radians(trk - 90)
                    pts = [
                        int(px + (S+2)*math.cos(math.radians(trk) - math.pi/2)),
                        int(py + (S+2)*math.sin(math.radians(trk) - math.pi/2)),
                        int(px + S*math.cos(ang_r + math.pi*2/3)),
                        int(py + S*math.sin(ang_r + math.pi*2/3)),
                        int(px + S*math.cos(ang_r - math.pi*2/3)),
                        int(py + S*math.sin(ang_r - math.pi*2/3)),
                    ]
                    draw.polygon(pts, fill=col)
                elif sym == "diamond":
                    draw.polygon([px,py-S, px+S,py, px,py+S, px-S,py], fill=col)
                elif sym == "cross":
                    draw.line([px-S,py, px+S,py], fill=col, width=2)
                    draw.line([px,py-S, px,py+S], fill=col, width=2)
                elif sym == "circle":
                    draw.ellipse([px-S,py-S,px+S,py+S], outline=col, width=2)
                elif sym == "square":
                    draw.rectangle([px-S,py-S,px+S,py+S], fill=col)
                elif sym == "sq_em":
                    draw.rectangle([px-S-1,py-S-1,px+S+1,py+S+1],
                                   outline=(255,0,0,255), width=2)
                    draw.rectangle([px-S+1,py-S+1,px+S-1,py+S-1], fill=col)
                else:
                    draw.ellipse([px-S,py-S,px+S,py+S], fill=col)

                # Ã¢â€â‚¬Ã¢â€â‚¬ Label (callsign + FL, only if Ã¢â€°Â¥5000ft) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
                if alt_ft >= 5000:
                    call = (c.get("callsign") or c.get("icao") or "").strip()[:8]
                    draw.text((px+S+2, py-4), f"{call} FL{int(alt_ft/100)}",
                              fill=cls["color"]+(200,))
                elif alt_ft > 0:
                    call = (c.get("callsign") or "").strip()[:6]
                    if call:
                        draw.text((px+S+2, py-4), call, fill=cls["color"]+(160,))

            except Exception: continue

        # Ã¢â€â‚¬Ã¢â€â‚¬ Pyxis vessel icon Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
        draw.ellipse([cx2-5,cy2-5,cx2+5,cy2+5], fill=(255,255,255,255))
        draw.ellipse([cx2-3,cy2-3,cx2+3,cy2+3], fill=(0,0,0,255))

        # Ã¢â€â‚¬Ã¢â€â‚¬ Legend strip Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
        legend_y = H - 14
        draw.rectangle([0, legend_y, W, H], fill=(0,0,0,180))
        draw.text((4, legend_y+1), f"ADSB | {len(contacts)}ACT | 20km RING | Z{zoom}", fill=(0,220,255,220))

        # Convert to JPEG and store in cache
        out = canvas.convert("RGB")
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=88)
        img_bytes = buf.getvalue()
        with adsb_radar_cache_lock:
            adsb_radar_cache[zoom] = {"time": time.time(), "img": img_bytes,
                                      "lat": lat_r, "lon": lon_r}
        buf.seek(0)
        resp = make_response(img_bytes)
        resp.headers.set("Content-Type", "image/jpeg")
        resp.headers.set("Content-Length", str(len(img_bytes)))
        resp.headers.set("X-Cache", "MISS")
        return resp

    except Exception as e:
        log(f"adsb_radar_map error: {e}")
        # Return 1px black JPEG on failure
        from PIL import Image
        import io
        img = Image.new("RGB", (4,4), (0,0,0))
        buf = io.BytesIO(); img.save(buf, "JPEG"); buf.seek(0)
        from flask import send_file as _sf
        return _sf(buf, mimetype="image/jpeg")


@app.route('/adsb_intel', methods=['POST'])
@requires_auth
def adsb_intel():
    """
    Gemini-grounded enrichment for a specific ADS-B contact.
    POST: {icao, callsign, lat, lon, alt_ft, spd_kts, track, squawk}
    Returns: {summary, type_str, operator, military, threat_level, watch_lines[]}
    """
    try:
        data = request.json or {}
        icao     = data.get("icao","").strip().upper()
        callsign = data.get("callsign","").strip()
        alt_ft   = data.get("alt_ft", 0)
        spd_kts  = data.get("spd_kts", 0)
        trk      = data.get("track", 0)
        sq       = data.get("squawk","")
        lat      = data.get("lat", last_known_lat)
        lon      = data.get("lon", last_known_lon)
        rng      = data.get("range_nm", "?")
        brg      = data.get("bearing", "?")

        # CPA estimate
        cpa_str = "UNKNOWN"
        try:
            spd_f = float(spd_kts)
            rng_f = float(rng)
            if spd_f > 0 and rng_f < 200:
                # Closure rate (rough): if heading toward Pyxis
                cpa_est_min = (rng_f / (spd_f / 60.0))
                cpa_str = f"{cpa_est_min:.0f}min at {rng_f:.1f}nm"
        except: pass

        cls = classify_aircraft({"icao":icao,"callsign":callsign,"squawk":sq,"category":0})
        country = icao_to_country(icao)

        prompt = f"""You are a tactical air intelligence officer. Use Google Search to look up the following aircraft and provide a concise military-style intelligence assessment.

CONTACT:
- ICAO: {icao}
- Callsign: {callsign or 'UNKNOWN'}
- Country of Registration: {country}
- Classification: {cls['type']}
- Position: {lat:.3f}N, {lon:.3f}E  |  FL{int(alt_ft/100)} | {spd_kts}kts TRK:{trk}Ã‚Â°
- Range/Bearing from Pyxis: {rng}nm @ {brg}Ã‚Â°
- Squawk: {sq or 'None'}
- Est. Closure: {cpa_str}

SEARCH FOR: aircraft ICAO {icao}, callsign {callsign}, operator, aircraft type, military registry, any incidents or NOTAMs.

Respond in this EXACT format (4 lines max per section, concise):
AIRCRAFT: [make/model and type e.g. Boeing 737-800 Commercial]
OPERATOR: [airline/military unit or UNKNOWN]
ASSESSMENT: [is this routine, military, suspicious? brief 1-sentence]
THREAT: [NONE/LOW/MEDIUM/HIGH with 1-sentence reason]
CPA: {cpa_str}"""

        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.2
            )
        )
        summary = resp.text.strip() if resp and resp.text else "INTEL UNAVAILABLE"

        # Build watch_lines with word-boundary wrapping and blank separators
        SECTION_KEYS = ("AIRCRAFT:", "OPERATOR:", "ASSESSMENT:", "THREAT:", "CPA:")
        watch_lines = []
        for line in summary.split("\n"):
            line = line.strip()
            if not line: continue
            is_section = any(line.startswith(k) for k in SECTION_KEYS)
            # Add blank separator line before each new section (except the first)
            if is_section and len(watch_lines) > 0:
                watch_lines.append("")
            # Word-boundary wrap at 28 chars
            words = line.split(" ")
            cur = ""
            for word in words:
                test = (cur + " " + word).strip()
                if len(test) <= 28:
                    cur = test
                else:
                    if cur: watch_lines.append(cur)
                    cur = word
            if cur: watch_lines.append(cur)

        return jsonify({
            "icao": icao, "callsign": callsign,
            "type_str": cls["type"], "country": country,
            "summary": summary,
            "watch_lines": watch_lines[:24],  # 24 lines max (blank spacers included)
            "threat_level": "HIGH" if sq in ("7700","7500") else "LOW"
        }), 200

    except Exception as e:
        log(f"adsb_intel error: {e}")
        return jsonify({"error": str(e), "watch_lines": ["INTEL ERR", str(e)[:20]]}), 500


@app.route('/telemetry', methods=['GET', 'POST'])
def handle_telem():
    """
    Raw fast-polling endpoint used by internal services to quickly dump
    the latest synchronized JSON telemetry variables without heavy logic.
    Also accepts POST from the Headless Simulator to inject vessel state.
    """
    if request.method == 'POST':
        try:
            payload = request.get_json(silent=True) or {}
            # Normalise headless_sim field names: BOAT_LAT/LON -> lat/lon
            if "BOAT_LAT" in payload: payload["lat"] = payload.pop("BOAT_LAT")
            if "BOAT_LON" in payload: payload["lon"] = payload.pop("BOAT_LON")
            payload["last_sim_update"] = time.time()
            existing = {}
            if os.path.exists(SIM):
                try:
                    with open(SIM, "r") as f: existing = json.load(f)
                except: pass
            existing.update(payload)
            with open(SIM, "w") as f: json.dump(existing, f)
            # Write last_pos.json for external workers (e.g. adsb_worker)
            try:
                lat_v = payload.get("lat") or existing.get("lat")
                lon_v = payload.get("lon") or existing.get("lon")
                if lat_v is not None and lon_v is not None:
                    pos_file = os.path.join(B, "last_pos.json")
                    with open(pos_file, "w") as pf:
                        json.dump({"lat": float(lat_v), "lon": float(lon_v), "ts": time.time()}, pf)
            except: pass
            return "OK", 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
            
    try:
        d = {}
        if os.path.exists(DT):
            try:
                with open(DT,"r") as f: d=json.load(f)
            except: pass
        if os.path.exists(SIM):
            try:
                with open(SIM,"r") as f: s=json.load(f); d.update(s)
            except: pass
        return jsonify(d)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/sim_override', methods=['POST'])
def sim_override_api():
    """
    Dashboard webhook to manipulate headless_sim telemetry and trigger global map warping 
    without causing EW Spoofing alarms due to the distance jump.
    """
    try:
        payload = request.json
        if payload.get("base_lat") is not None and payload.get("base_lon") is not None:
            payload["warp_ts"] = time.time()
            # Flush the sqlite radar trail instantly so a 15,000nm jump isn't drawn or logged
            from contextlib import closing
            try:
                with closing(sqlite3.connect(DB)) as c:
                    c.execute("DELETE FROM logs")
                    c.commit()
            except: pass
            
        existing = {}
        ov_path = os.path.join(B, "sim_override.json")
        if os.path.exists(ov_path):
            try:
                with open(ov_path, "r") as f: existing = json.load(f)
            except: pass
        
        existing.update(payload)
        with open(ov_path, "w") as f: json.dump(existing, f)
        
        return jsonify({"status": "ok", "state": existing})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

DASHBOARD_DIR = os.path.join(B, "dashboard")

@app.route('/dashboard/')
@app.route('/dashboard')
@requires_auth
def serve_dashboard():
    """Serves the React Tactical Dashboard SPA from the built dist folder."""
    index_path = os.path.join(DASHBOARD_DIR, "dist", "index.html")
    # Support both dist/index.html and index.html directly
    if not os.path.exists(index_path):
        index_path = os.path.join(DASHBOARD_DIR, "index.html")
    if not os.path.exists(index_path):
        return "Dashboard not deployed. Run: scp -r dist/ VM:/manta-comms/dashboard/", 404
    return send_file(index_path, mimetype='text/html')

@app.route('/dashboard/assets/<path:filename>')
def serve_dashboard_assets(filename):
    """Serves Vite-built JS/CSS assets for the React dashboard."""
    assets_dir = os.path.join(DASHBOARD_DIR, "dist", "assets")
    if not os.path.exists(assets_dir):
        assets_dir = os.path.join(DASHBOARD_DIR, "assets")
    return send_file(os.path.join(assets_dir, filename))

@app.route('/clear_audio', methods=['POST'])
@requires_auth
def clear_audio():
    """
    Clears all audio history and deletes synthesized .wav files from disk.
    Called from any Pyxis interface to give a clean audio slate Ã¢â‚¬â€
    useful after a scenario warp or when stale reports are cluttering the queue.
    """
    try:
        cleared_files = 0
        # Wipe audio_history from the DT state file
        if os.path.exists(DT):
            try:
                with open(DT, "r") as f: st = json.load(f)
                st["audio_history"] = []
                with open(DT, "w") as f: json.dump(st, f)
            except: pass

        # Delete all audio_*.wav files from the manta-comms directory
        import glob
        for wav in glob.glob(os.path.join(B, "audio_*.wav")):
            try:
                os.remove(wav)
                cleared_files += 1
            except: pass

        log(f"Audio cleared: history wiped, {cleared_files} .wav files deleted")
        return jsonify({"status": "ok", "cleared_files": cleared_files}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/status_api')
def st_api():
    """
    The universal data multiplexer used by ALL Pyxis clients (Watch + Dashboards).
    1. Loads the latest physical watch state vector (DT).
    2. Overlays the live Simulator/Pygame telemetry (SIM).
    3. Overlays local geography & sunset data (GEO).
    4. Tracks 'Anchor Drag' alerts and routes audio if the vessel moves.
    5. Fuses the Pygame virtual targets with the Live AISStream targets.
    Returns the massive combined JSON blob containing total situational awareness.
    """
    try:
        global last_known_lat, last_known_lon
        d = {}
        if os.path.exists(DT):
            try: 
                with open(DT,"r") as f: d=json.load(f)
            except: pass
            
        # UI OVERRIDE: Lock GPS to Captain's Watch
        if d.get("onboard_mode", False):
            d["lat"] = d.get("CREW_LAT", last_known_lat)
            d["lon"] = d.get("CREW_LON", last_known_lon)
            last_known_lat = d["lat"]
            last_known_lon = d["lon"]
        
        if os.path.exists(SIM):
            try:
                # MANDATORY FAST-READ FALLBACK:
                # headless_sim.py writes to this JSON thousands of times per minute.
                # These try/except blocks prevent 'JSONDecodeError: Extra data' from silently
                # crashing the status_api thread mid-collision and dropping the audio queue.
                with open(SIM,"r") as f: s=json.load(f)
            except: s={}
            
            s.pop("audio_history", None)
            
            real_radar = d.get("radar_contacts", [])
            sim_radar = s.get("radar_contacts", [])
            
            import time
            if s and (time.time() - s.get("last_sim_update", 0) <= 15.0):
                d.update(s)
                # Map Headless Sim Boat Coords to Global Pyxis Coords 
                if not d.get("onboard_mode", False) and d.get("BOAT_LAT", 0.0) != 0.0:
                    d["lat"] = d["BOAT_LAT"]
                    d["lon"] = d["BOAT_LON"]
                    last_known_lat = d["lat"]
                    last_known_lon = d["lon"]
                    
                if real_radar and sim_radar:
                    d["radar_contacts"] = real_radar + sim_radar
                elif real_radar:
                    d["radar_contacts"] = real_radar
                elif sim_radar:
                    d["radar_contacts"] = sim_radar
            else:
                # Simulator offline -> MOOR PYXIS AUTONOMOUSLY (DECOUPLED FROM WATCH)
                d["lat"] = last_known_lat
                d["lon"] = last_known_lon
                d["radar_contacts"] = real_radar  # STRIP GHOST SIMULATOR RADAR CONTACTS

            
        if os.path.exists(GEO_CACHE_FILE):
             try:
                 with open(GEO_CACHE_FILE, "r") as f: g = json.load(f); d.update(g)
             except: pass
            
        # Anchor Drag Check Loop
        if os.path.exists(AN):
            try:
                with open(AN, "r") as f: anchor = json.load(f)
            except: anchor = {}
            if anchor.get("active", False):
                # Calculate physical drift distance in meters
                cLat, cLon = d.get('lat', 0), d.get('lon', 0)
                aLat, aLon, aRad = anchor.get('lat', 0), anchor.get('lon', 0), anchor.get('radius', 50)
                
                dLat = (cLat - aLat) * 111320.0
                dLon = (cLon - aLon) * 111320.0 * math.cos(math.radians(aLat))
                dist = math.sqrt(dLat**2 + dLon**2)
                
                if dist > aRad:
                    log(f"ANCHOR DRAG DETECTED: {dist:.1f}m > limit {aRad}m")
                    # Push audio alert
                    task_queue.put(("status", cLat, cLon, f"MAYDAY. MAYDAY. ANCHOR DRAG DETECTED. VESSEL DRIFT IS {dist:.1f} METERS. RECOVER HELM IMMEDIATELY."))
                    # Deactivate to prevent constant spamming
                    anchor["active"] = False
                    with open(AN, "w") as f: json.dump(anchor, f)
                        
        if os.path.exists(ROUTE_FILE):
            try:
                with open(ROUTE_FILE,"r") as f: r=json.load(f); d["active_route"]=r.get("waypoints",[])
            except: pass
        
        # Safely combine live Pygame targets (UUV, OSINT, STRUCT) with the background AisStream feed
        pyxis_contacts = d.get("radar_contacts", [])
        
        # Inject active marine weather warnings
        weather_alerts_file = os.path.join(B, "weather_alerts_cache.json")
        if os.path.exists(weather_alerts_file):
            try:
                with open(weather_alerts_file, "r") as f:
                    wa = json.load(f)
                    d["marine_weather"] = wa
            except: pass

        # Inject cached OSM navigational hazards (if within 12nm)
        global osm_cache
        cLat, cLon = d.get('lat', 1.2504), d.get('lon', 103.8300)
        osm_injection = []
        with osm_cache_lock:
            for item in osm_cache:
                dLat = (item['lat'] - cLat) * 60.0
                dLon = (item['lon'] - cLon) * 60.0 * math.cos(math.radians(cLat))
                dist = math.sqrt(dLat**2 + dLon**2)
                if dist < 12.0:
                    item_copy = item.copy()
                    bear = math.degrees(math.atan2(dLon, dLat))
                    item_copy['bearing'] = round(bear if bear >= 0 else bear + 360.0, 1)
                    item_copy['range_nm'] = round(dist, 2)
                    osm_injection.append(item_copy)
                    
        live_ais = get_active_ais_list(cLat, cLon)
        d["radar_contacts"] = pyxis_contacts + live_ais + osm_injection
        
        # Fallback coordinate injection for web-players when physical watch is offline
        if d.get('lat', 0) == 0:
            d['lat'] = last_known_lat
            d['lon'] = last_known_lon

        # Inject meteo / sea state cache into the status payload
        try:
            if os.path.exists(METEO_CACHE_FILE):
                with open(METEO_CACHE_FILE, "r") as f:
                    m = json.load(f)
                wave_h = m.get("wave_height_m") or m.get("marine", {}).get("wave_height")
                swell_h = m.get("swell_height_m") or m.get("marine", {}).get("swell_wave_height")
                wave_dir = m.get("wave_dir") or m.get("marine", {}).get("wave_direction")
                curr_kn = m.get("current_kn") or m.get("marine", {}).get("ocean_current_velocity")
                if wave_h is not None:
                    d["wave_height_m"] = round(float(wave_h), 2)
                    d["swell_height_m"] = round(float(swell_h), 2) if swell_h else None
                    d["wave_dir"] = wave_dir
                    d["current_kn"] = round(float(curr_kn), 2) if curr_kn else None
                    # sea_state string for legacy dashboard consumers
                    d["sea_state"] = f"Wave {wave_h:.1f}m | Swell {swell_h:.1f}m | Curr {curr_kn:.1f}kn" if swell_h and curr_kn else f"Wave {wave_h:.1f}m"
        except Exception as e:
            log(f"Meteo inject err: {e}")

        return jsonify(d)
    except Exception as e:
        log(f"STATUS ERR: {e}")
        return jsonify({"status_id":0})

@app.route('/')
@requires_auth
def player_lite():
    """
    PYXIS LIVE TRACKER Ã¢â‚¬â€ redesigned v2.
    Dark glassmorphism tactical theme with auto-play audio queue,
    CMEMS HUD, system health strip, and inbox panel.
    """
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>PYXIS LIVE TRACKER</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&family=Roboto+Mono:wght@400;600&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root{--bg:#050d1a;--glass:rgba(8,20,45,0.75);--border:rgba(0,212,255,0.18);--cyan:#00d4ff;--green:#00ff9f;--amber:#ffb300;--red:#ff4757;--text:#c8e6ff;--dim:#4a7a9b;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;overflow:hidden;height:100vh;width:100vw;}
  #map{position:absolute;inset:0;z-index:1;}
  .hud{position:absolute;z-index:10;background:var(--glass);border:1px solid var(--border);border-radius:12px;padding:10px 14px;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);box-shadow:0 4px 24px rgba(0,0,0,0.5);}
  .hud-label{font-family:'Roboto Mono',monospace;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--dim);margin-bottom:2px;}
  .hud-val{font-family:'Roboto Mono',monospace;font-size:13px;font-weight:600;color:var(--cyan);}
  .hud-row{display:flex;gap:18px;align-items:flex-start;}
  /* Position HUDs */
  #hud-pos{top:12px;left:12px;}
  #hud-cmems{top:12px;right:12px;min-width:170px;}
  #hud-health{top:12px;left:50%;transform:translateX(-50%);display:flex;gap:6px;padding:8px 12px;}
  #hud-bottom{bottom:0;left:0;right:0;border-radius:0;border-left:none;border-right:none;border-bottom:none;max-height:42vh;display:flex;flex-direction:column;padding:0;}
  /* Health pills */
  .pill{display:flex;align-items:center;gap:5px;font-size:10px;font-family:'Roboto Mono',monospace;background:rgba(0,0,0,0.3);border-radius:20px;padding:3px 8px;border:1px solid var(--border);}
  .pill .dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;}
  .ok{border-color:rgba(0,255,159,0.3);} .ok .dot{background:var(--green);box-shadow:0 0 6px var(--green);}
  .stale{border-color:rgba(255,179,0,0.3);} .stale .dot{background:var(--amber);box-shadow:0 0 6px var(--amber);}
  .offline{border-color:rgba(255,71,87,0.3);} .offline .dot{background:var(--red);box-shadow:0 0 6px var(--red);}
  /* Bottom panel */
  #player-bar{background:rgba(5,13,26,0.95);border-top:1px solid var(--border);padding:10px 14px;display:flex;align-items:center;gap:12px;flex-shrink:0;}
  #arm-btn{background:linear-gradient(135deg,var(--amber),#ff6b00);color:#000;border:none;border-radius:8px;padding:8px 16px;font-weight:700;font-size:12px;cursor:pointer;letter-spacing:1px;animation:pulse 2s infinite;}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(255,179,0,0.4);}50%{box-shadow:0 0 0 8px rgba(255,179,0,0);}}
  #armed-badge{display:none;background:rgba(0,255,159,0.12);border:1px solid var(--green);border-radius:6px;padding:4px 10px;font-size:10px;color:var(--green);font-family:'Roboto Mono',monospace;letter-spacing:1px;}
  #now-playing-wrap{flex:1;min-width:0;}
  #now-playing-type{font-size:9px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;}
  #now-playing-text{font-size:11px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px;}
  audio{width:200px;height:28px;accent-color:var(--cyan);flex-shrink:0;}
  #inbox-list{overflow-y:auto;flex:1;padding:8px 14px 4px;}
  .inbox-item{display:flex;align-items:flex-start;gap:8px;padding:6px 0;border-bottom:1px solid rgba(0,212,255,0.07);}
  .inbox-item:last-child{border-bottom:none;}
  .inbox-source{flex-shrink:0;font-size:9px;font-family:'Roboto Mono',monospace;letter-spacing:1px;padding:2px 6px;border-radius:4px;margin-top:1px;}
  .src-pyxis{background:rgba(0,212,255,0.15);color:var(--cyan);border:1px solid rgba(0,212,255,0.3);}
  .src-cmems{background:rgba(0,255,159,0.12);color:var(--green);border:1px solid rgba(0,255,159,0.3);}
  .src-other{background:rgba(74,122,155,0.2);color:var(--dim);border:1px solid rgba(74,122,155,0.3);}
  .inbox-msg{font-size:11px;color:var(--text);line-height:1.4;}
  /* CMEMS stale badge */
  .badge-stale{display:inline-block;background:rgba(255,179,0,0.15);border:1px solid var(--amber);border-radius:4px;padding:1px 6px;font-size:9px;color:var(--amber);font-family:'Roboto Mono',monospace;letter-spacing:1px;}
  .badge-live{display:inline-block;background:rgba(0,255,159,0.12);border:1px solid var(--green);border-radius:4px;padding:1px 6px;font-size:9px;color:var(--green);font-family:'Roboto Mono',monospace;letter-spacing:1px;}
  /* Map markers */
  .vessel-glow{filter:drop-shadow(0 0 6px #00d4ff);}
  @keyframes aisPulse{
    0%,100%{opacity:1;box-shadow:0 0 6px currentColor;}
    50%{opacity:0.6;box-shadow:0 0 14px currentColor;}
  }
</style>
</head>
<body>

<div id="map"></div>

<!-- Position HUD -->
<div class="hud" id="hud-pos">
  <div class="hud-row">
    <div><div class="hud-label">Position</div><div class="hud-val" id="h-pos">--</div></div>
    <div><div class="hud-label">SOG</div><div class="hud-val" id="h-sog">--</div></div>
    <div><div class="hud-label">HDG</div><div class="hud-val" id="h-hdg">--</div></div>
  </div>
</div>

<!-- Health strip -->
<div class="hud" id="hud-health">
  <div class="pill offline" id="pill-cmems"><div class="dot"></div>CMEMS</div>
  <div class="pill offline" id="pill-meteo"><div class="dot"></div>METEO</div>
  <div class="pill offline" id="pill-alerts"><div class="dot"></div>ALERTS</div>
  <div class="pill offline" id="pill-depth"><div class="dot"></div>DEPTH</div>
</div>

<!-- CMEMS HUD -->
<div class="hud" id="hud-cmems">
  <div class="hud-label">CMEMS OCEAN DATA <span id="cmems-badge" class="badge-stale">UPDATING</span></div>
  <div class="hud-row" style="margin-top:6px;">
    <div><div class="hud-label">Wave</div><div class="hud-val" id="c-wave">--</div></div>
    <div><div class="hud-label">Swell</div><div class="hud-val" id="c-swell">--</div></div>
  </div>
  <div class="hud-row" style="margin-top:6px;">
    <div><div class="hud-label">Current</div><div class="hud-val" id="c-curr">--</div></div>
    <div><div class="hud-label">SST</div><div class="hud-val" id="c-sst">--</div></div>
  </div>
</div>

<!-- Bottom panel: player + inbox -->
<div class="hud" id="hud-bottom">
  <div id="player-bar">
    <button id="arm-btn" onclick="armAudio()">Ã¢Å¡Â¡ TAP TO ARM AUTO-PLAY</button>
    <div id="armed-badge">Ã¢â€“Â¶ AUTO-PLAY ARMED</div>
    <div id="now-playing-wrap">
      <div id="now-playing-type">STANDBY</div>
      <div id="now-playing-text">No briefing yet</div>
    </div>
    <audio id="mainAudio" controls></audio>
    <button onclick="clearAudio()" style="background:rgba(255,71,87,0.12);border:1px solid rgba(255,71,87,0.4);color:#ff4757;font-size:10px;font-weight:bold;padding:4px 10px;border-radius:4px;cursor:pointer;letter-spacing:1px;" title="Delete all audio reports from server">Ã°Å¸â€”â€˜ CLEAR REPORTS</button>
  </div>
  <div id="inbox-list"><div style="color:var(--dim);font-size:11px;text-align:center;padding:8px;">Loading messages...</div></div>
</div>

<script>
let map, pyxisMarker, watchMarker, isInit=false;
let autoPlayArmed=false, playedIds=new Set(), audioQueue=[], isPlaying=false;
let lastLat=0, lastLon=0;

// Ã¢â€â‚¬Ã¢â€â‚¬ Map init Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
map = L.map('map',{zoomControl:false,attributionControl:false}).setView([-38.487,145.620],10);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{maxZoom:20}).addTo(map);

const vesselIcon = L.divIcon({className:'',html:'<div class="vessel-glow" style="font-size:26px;color:#00d4ff;line-height:1;">&#x25B2;</div>',iconSize:[26,26],iconAnchor:[13,13]});
const crewIcon   = L.divIcon({className:'',html:'<div style="width:14px;height:14px;border-radius:50%;background:#00ff9f;border:2px solid #fff;box-shadow:0 0 8px #00ff9f;cursor:move;" title="CREW - Drag to reposition"></div>',iconSize:[14,14],iconAnchor:[7,7]});

pyxisMarker = L.marker([0,0],{icon:vesselIcon,draggable:true}).addTo(map);
pyxisMarker.on('dragend',async function(e){ const {lat,lng}=e.target.getLatLng(); try{ const r=await fetch('/set_vessel_pos',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Basic '+btoa('admin:manta')},body:JSON.stringify({lat,lon:lng})}); const d=await r.json(); if(d.snapped){pyxisMarker.setLatLng([d.lat,d.lon]);} }catch(ex){} });
watchMarker = L.marker([0,0],{icon:crewIcon,draggable:true}).addTo(map);
watchMarker.on('dragend',async function(e){ const {lat,lng}=e.target.getLatLng(); try{ const r=await fetch('/set_crew_pos',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Basic '+btoa('admin:manta')},body:JSON.stringify({lat,lon:lng})}); const d=await r.json(); if(d.snapped){watchMarker.setLatLng([d.lat,d.lon]);} }catch(ex){} });
function armAudio(){
  const ctx=new (window.AudioContext||window.webkitAudioContext)();
  const buf=ctx.createBuffer(1,1,22050);
  const src=ctx.createBufferSource(); src.buffer=buf; src.connect(ctx.destination); src.start(0);
  autoPlayArmed=true;
  // Reset queue state on arm so stale pre-arm history never replays
  playedIds=new Set(); audioQueue=[]; isPlaying=false;
  document.getElementById('arm-btn').style.display='none';
  document.getElementById('armed-badge').style.display='block';
}

async function clearAudio(){
  if(!confirm('Delete all audio reports from server?')) return;
  try{
    await fetch('/clear_audio',{method:'POST',headers:{'Authorization':'Basic '+btoa('admin:manta')}});
    // Also reset local queue
    audioQueue=[]; playedIds=new Set(); isPlaying=false;
    document.getElementById('mainAudio').pause();
    document.getElementById('now-playing-type').innerText='STANDBY';
    document.getElementById('now-playing-text').innerText='Audio history cleared';
    document.getElementById('inbox-list').innerHTML='<div style="color:var(--dim);font-size:11px;text-align:center;padding:8px;">Cleared Ã¢Å“â€œ</div>';
  }catch(e){alert('Clear failed: '+e);}
}

function playNextInQueue(){
  if(isPlaying||audioQueue.length===0) return;
  isPlaying=true;
  // Always play from queue head (already sorted: urgent/newest first)
  const item=audioQueue.shift();
  const a=document.getElementById('mainAudio');
  a.src='/audio?id='+item.id+'&bust='+Date.now();
  a.load();
  a.play().then(()=>{
    const typeLabel=(item.type||'status').toUpperCase();
    const urgentTypes=['MAYDAY','SYSTEM_STATUS','KINETIC_THREAT'];
    const colour=urgentTypes.includes(typeLabel)?'#ff4757':'#00d4ff';
    document.getElementById('now-playing-type').innerText=typeLabel;
    document.getElementById('now-playing-type').style.color=colour;
    document.getElementById('now-playing-text').innerText=item.text?item.text.substring(0,120)+'...':'';
  }).catch(e=>{isPlaying=false;setTimeout(playNextInQueue,500);});
  a.onended=()=>{isPlaying=false;setTimeout(playNextInQueue,500);};
}

// Ã¢â€â‚¬Ã¢â€â‚¬ Main polling tick Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
async function tick(){
  try{
    const r=await fetch('/status_api?bust='+Date.now()), d=await r.json();
    lastLat=d.lat||lastLat; lastLon=d.lon||lastLon;

    pyxisMarker.setLatLng([lastLat,lastLon]);
    document.getElementById('h-pos').innerText=lastLat.toFixed(4)+', '+lastLon.toFixed(4);
    document.getElementById('h-sog').innerText=(d.sog!=null?(+d.sog).toFixed(1)+' kn':'--');
    document.getElementById('h-hdg').innerText=(d.heading!=null?Math.round(d.heading)+'Ã‚Â°':'--');

    const wLat=d.CREW_LAT||0, wLon=d.CREW_LON||0;
    if(wLat!==0){ watchMarker.setLatLng([wLat,wLon]); }

    if(!isInit&&(lastLat!==0||wLat!==0)){
      const grp=wLat!==0?L.featureGroup([pyxisMarker,watchMarker]):L.featureGroup([pyxisMarker]);
      try{map.fitBounds(grp.getBounds().pad(0.4),{maxZoom:13});}catch(e){}
      isInit=true;
    }

    // Auto-play queue Ã¢â‚¬â€ newest-first, fresh reports only
    if(d.audio_history&&autoPlayArmed){
      const URGENT=['MAYDAY','KINETIC_THREAT','SYSTEM_STATUS'];
      const MAX_AGE_S=600; // Only auto-play reports < 10 minutes old
      const nowSec=Date.now()/1000;
      // Sort: urgent first, then newest ts first
      const sorted=[...d.audio_history].sort((a,b)=>{
        const aUr=URGENT.includes((a.type||'').toUpperCase())?1:0;
        const bUr=URGENT.includes((b.type||'').toUpperCase())?1:0;
        if(bUr!==aUr) return bUr-aUr;
        return (b.ts||0)-(a.ts||0);
      });
      // Mark stale items as seen (inbox only) Ã¢â‚¬â€ never auto-play old reports
      sorted.forEach(h=>{
        if((nowSec-(h.ts||0))>MAX_AGE_S) playedIds.add(h.id);
      });
      // Collect fresh unplayed items only
      const newItems=sorted.filter(h=>h.ready!==false&&!playedIds.has(h.id));
      newItems.forEach(h=>playedIds.add(h.id));
      if(newItems.length>0){
        // Atomically prepend batch Ã¢â‚¬â€ preserves newest-first order
        audioQueue.splice(0,0,...newItems);
        // Interrupt non-urgent playback if urgent report just arrived
        const hasUrgent=newItems.some(h=>URGENT.includes((h.type||'').toUpperCase()));
        if(hasUrgent&&isPlaying){
          const curType=(document.getElementById('now-playing-type').innerText||'');
          if(!URGENT.includes(curType)){
            document.getElementById('mainAudio').pause();
            isPlaying=false;
          }
        }
      }
      playNextInQueue();
    }

  }catch(e){console.error(e);}
}

// Ã¢â€â‚¬Ã¢â€â‚¬ CMEMS polling Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
async function tickCmems(){
  try{
    const r=await fetch('/sea_state_json'), d=await r.json();
    document.getElementById('c-wave').innerText=d.wave_h&&d.wave_h!='n/a'?d.wave_h:'--';
    document.getElementById('c-swell').innerText=d.swell_h&&d.swell_h!='n/a'?d.swell_h:'--';
    document.getElementById('c-curr').innerText=d.curr_v&&d.curr_v!='n/a'?d.curr_v:'--';
    document.getElementById('c-sst').innerText=d.sst_c&&d.sst_c!='n/a'?d.sst_c:'--';
    const badge=document.getElementById('cmems-badge');
    if(d.cmems_stale||d.wave_h==='n/a'){badge.className='badge-stale';badge.innerText='UPDATING';}
    else{badge.className='badge-live';badge.innerText='LIVE';}
  }catch(e){}
}

// Ã¢â€â‚¬Ã¢â€â‚¬ Health strip Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
async function tickHealth(){
  try{
    const r=await fetch('/health'), d=await r.json();
    const map2={cmems:'pill-cmems',meteo:'pill-meteo',weather_alerts:'pill-alerts',bathymetry:'pill-depth'};
    for(const [key,pid] of Object.entries(map2)){
      const el=document.getElementById(pid); if(!el) continue;
      const w=d[key];
      el.className='pill '+((!w||!w.ok)?'offline':(w.age_min!=null&&w.age_min>90?'stale':'ok'));
    }
  }catch(e){}
}

// Ã¢â€â‚¬Ã¢â€â‚¬ Inbox panel Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
async function tickInbox(){
  try{
    const r=await fetch('/inbox_sync'), data=await r.json();
    const msgs=data.messages||[];
    if(msgs.length===0){document.getElementById('inbox-list').innerHTML='<div style="color:var(--dim);font-size:11px;text-align:center;padding:8px;">No messages yet</div>';return;}
    const srcClass=s=>s==='PYXIS'?'src-pyxis':s==='CMEMS'?'src-cmems':'src-other';
    // Use /status_api audio_history for full text of system messages
    document.getElementById('inbox-list').innerHTML=msgs.slice().reverse().map(m=>{
      const src=(m.id||'').length<15?'PYXIS':'SYS';
      const lines=(m.lines||[]).join(' ').trim()||m.title||'';
      return `<div class="inbox-item"><span class="inbox-source ${srcClass(m.title&&m.title.includes('CMEMS')?'CMEMS':'PYXIS')}">${m.title&&m.title.includes('CMEMS')?'CMEMS':'PYXIS'}</span><div class="inbox-msg">${lines.substring(0,160)}</div></div>`;
    }).join('');
  }catch(e){}
}

setInterval(tick,3000); tick();
setInterval(tickCmems,15000); tickCmems();
setInterval(tickHealth,30000); tickHealth();
setInterval(tickInbox,10000); tickInbox();
</script>
</body>
</html>
"""

_="""    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body { background: #000; color: #0f0; font-family: monospace; margin: 0; padding: 0; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
        #map { width: 100vw; flex: 1; }
        .overlay-text { position: absolute; bottom: 35vh; right: 10px; z-index: 1000; background: rgba(0,20,0,0.8); padding: 10px; border: 1px solid #0f0; border-radius: 5px; font-size: 12px; pointer-events: none; }
        .ui { flex: 0 0 30vh; background: #010; overflow-y: auto; padding: 10px; border-top: 2px solid #0f0; display: flex; flex-direction: column; }
        .play-row { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px dashed #0a0; padding: 8px 0; }
        .play-btn { background: rgba(0,255,0,0.1); color: #0f0; border: 1px solid #0f0; padding: 6px 12px; font-size: 12px; cursor: pointer; border-radius: 4px; }
        .play-btn:active { background: #0f0; color: #000; }
    </style>
</head>
<body>
    <div id="map"></div>
    <div class="overlay-text">
        <span style="color:#0f0;">ÃƒÂ¢Ã¢â‚¬â€œÃ‚Â² PYXIS DRONE</span><br>
        <span style="color:#00f;">ÃƒÂ¢Ã¢â‚¬â€  CREW WATCH</span>
    </div>
    <div class="ui">
        <div style="font-weight: bold; margin-bottom: 5px; color: #0a0;">--- SECURE AUDIO INBOX ---</div>
        <div id="audioList" style="font-size: 13px;">Waiting for telemetry...</div>
    </div>
    <audio id="audio" controls style="display:none;"></audio>

    <script>
        let map, pyxisMarker, watchMarker;
        let isInitialized = false;
        let currentAu = null;

        function playId(aid) {
            if(currentAu) { currentAu.pause(); }
            currentAu = new Audio('/audio?id=' + aid + '&bust=' + Date.now());
            currentAu.play().catch(e => console.error("Play prevented", e));
        }

        function initMap() {
            map = L.map('map', {zoomControl: false}).setView([0, 0], 2);
            L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(map);
            
            pyxisMarker = L.marker([0,0], {
                icon: L.divIcon({
                    className: 'vessel-icon',
                    html: '<div style="font-size: 24px; color: #0f0;">&#x25B2;</div>',
                    iconSize: [24, 24],
                    iconAnchor: [12, 12]
                })
            }).addTo(map);
            
            watchMarker = L.circleMarker([0,0], {color: '#00f', radius: 8, fillOpacity: 0.9}).addTo(map);
        }
        initMap();

        async function tick() {
            try {
                const r = await fetch('/status_api?bust=' + Date.now());
                const d = await r.json();
                
                let pLat = d.lat || 0;
                let pLon = d.lon || 0;
                pyxisMarker.setLatLng([pLat, pLon]);
                
                let wLat = d.CREW_LAT || 0;
                let wLon = d.CREW_LON || 0;
                if (wLat !== 0) {
                    watchMarker.setLatLng([wLat, wLon]);
                    watchMarker.setRadius(8);
                } else {
                    watchMarker.setRadius(0);
                }
                
                // Resolve the 'Singapore lock'
                if (!isInitialized && (pLat !== 0 || wLat !== 0)) {
                    let group = new L.featureGroup([pyxisMarker, watchMarker]);
                    if(wLat === 0) group = new L.featureGroup([pyxisMarker]);
                    map.fitBounds(group.getBounds().pad(0.5), {maxZoom: 14});
                    isInitialized = true;
                }
                
                // Resolve AUDIO History Inbox overlays
                let htm = "";
                if(d.audio_history) {
                    [...d.audio_history].reverse().forEach(a => {
                        let btnText = "PLAY INTEL";
                        if (a.type === "day_brief") btnText = "PLAY MORNING BRIEFING";
                        if (a.type === "night_brief") btnText = "PLAY EVENING BRIEFING";
                        let btnHtml = a.ready === false ? `<button class="play-btn" style="color:#ff0;" disabled>GENERATING AUDIO...</button>` : `<button class="play-btn" onclick="playId('${a.id}')">${btnText}</button>`;
                        let headerTag = a.type === "day_brief" ? "MORNING BRIEFING" : a.type === "night_brief" ? "EVENING BRIEFING" : "Report " + a.id.toString().substring(0,8) + "... (" + a.type + ")";
                        let txtHtml = a.text ? `<div style="font-size: 11px; margin-top: 5px; color: #8f8;">${a.text}</div>` : '';
                        htm += `<div class="play-row" style="flex-direction: column; align-items: flex-start;">
                            <div style="display:flex; justify-content:space-between; width:100%; align-items:center;">
                                <span style="font-weight:bold; color:#0f0;">${headerTag}</span>
                                ${btnHtml}
                            </div>
                            ${txtHtml}
                        </div>`;
                    });
                }
                document.getElementById('audioList').innerHTML = htm || "No radio history.";

            } catch(e) { console.error("API Fetch Error", e); }
        }
        setInterval(tick, 3000);
        tick();
    </script>
</body>
</html>
"""

@app.route('/verbose')
def player():
    """
    PYXIS TACTICAL Ã¢â‚¬â€ redesigned v2.
    Dark glassmorphism tactical dashboard with tabbed side panel:
    AUDIO (auto-play player) | SCENARIO (location injector) | INTEL (AIS/CMEMS) | COMMS (voice/commands)
    """
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>PYXIS TACTICAL</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&family=Roboto+Mono:wght@400;600&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{--bg:#050d1a;--glass:rgba(8,20,45,0.82);--border:rgba(0,212,255,0.18);--cyan:#00d4ff;--green:#00ff9f;--amber:#ffb300;--red:#ff4757;--text:#c8e6ff;--dim:#4a7a9b;}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;}
#layout{display:flex;height:100vh;width:100vw;}
#map-panel{flex:1;position:relative;min-width:0;}
#map{width:100%;height:100%;}
#side{width:360px;flex-shrink:0;background:var(--glass);border-left:1px solid var(--border);backdrop-filter:blur(20px);display:flex;flex-direction:column;overflow:hidden;}
/* Tabs */
.tabs{display:flex;flex-shrink:0;border-bottom:1px solid var(--border);}
.tab{flex:1;padding:10px 0;text-align:center;font-size:10px;letter-spacing:1.5px;font-family:'Roboto Mono',monospace;color:var(--dim);cursor:pointer;transition:all .2s;border-bottom:2px solid transparent;}
.tab.active{color:var(--cyan);border-bottom:2px solid var(--cyan);}
.tab:hover:not(.active){color:var(--text);}
/* Tab panels */
.panel{display:none;flex:1;overflow-y:auto;padding:12px;flex-direction:column;gap:10px;}
.panel.active{display:flex;}
/* Cards / sections */
.card{background:rgba(0,0,0,0.25);border:1px solid var(--border);border-radius:10px;padding:10px;}
.card-title{font-size:9px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;margin-bottom:8px;font-family:'Roboto Mono',monospace;}
/* Audio player */
#player-main{background:rgba(0,212,255,0.06);border:1px solid rgba(0,212,255,0.25);border-radius:10px;padding:12px;}
#arm-btn-v{width:100%;padding:10px;background:linear-gradient(135deg,var(--amber),#ff6b00);color:#000;border:none;border-radius:8px;font-weight:700;font-size:12px;cursor:pointer;letter-spacing:1px;animation:pulse 2s infinite;margin-bottom:8px;}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(255,179,0,0.4);}50%{box-shadow:0 0 0 10px rgba(255,179,0,0);}}
#armed-badge-v{display:none;background:rgba(0,255,159,0.12);border:1px solid var(--green);border-radius:6px;padding:5px 10px;font-size:10px;color:var(--green);font-family:'Roboto Mono',monospace;letter-spacing:1px;text-align:center;margin-bottom:8px;}
#now-type{font-size:9px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;}
#now-text{font-size:11px;color:var(--text);line-height:1.4;margin:3px 0 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
audio#mainAudio{width:100%;height:30px;accent-color:var(--cyan);}
.audio-item{display:flex;align-items:flex-start;gap:8px;padding:6px 0;border-bottom:1px solid rgba(0,212,255,0.07);}
.audio-item:last-child{border-bottom:none;}
.a-type{flex-shrink:0;font-size:9px;font-family:'Roboto Mono',monospace;padding:2px 5px;border-radius:3px;background:rgba(0,212,255,0.1);color:var(--cyan);border:1px solid rgba(0,212,255,0.2);}
.a-txt{font-size:10px;color:var(--text);line-height:1.4;flex:1;min-width:0;}
.play-btn2{flex-shrink:0;background:rgba(0,212,255,0.12);color:var(--cyan);border:1px solid rgba(0,212,255,0.3);border-radius:5px;padding:3px 8px;font-size:9px;cursor:pointer;font-family:'Roboto Mono',monospace;}
.play-btn2:hover{background:rgba(0,212,255,0.25);}
/* Scenario */
select,input[type=text],input[type=number]{background:rgba(0,0,0,0.4);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:7px 10px;width:100%;font-family:'Roboto Mono',monospace;font-size:11px;outline:none;}
select:focus,input:focus{border-color:var(--cyan);}
.fields-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.field-lbl{font-size:9px;letter-spacing:1px;color:var(--dim);margin-bottom:3px;font-family:'Roboto Mono',monospace;}
.btn-inject{width:100%;padding:10px;background:linear-gradient(135deg,#00d4ff,#006aff);color:#000;border:none;border-radius:8px;font-weight:700;font-size:12px;cursor:pointer;letter-spacing:1px;margin-top:4px;transition:opacity .2s;}
.btn-inject:hover{opacity:.85;}
.btn-kill{width:100%;padding:8px;background:rgba(255,71,87,0.12);color:var(--red);border:1px solid rgba(255,71,87,0.3);border-radius:8px;font-size:11px;font-weight:600;cursor:pointer;margin-top:6px;transition:all .2s;font-family:'Roboto Mono',monospace;}
.btn-kill:hover{background:rgba(255,71,87,0.25);}
.toast{display:none;background:rgba(0,255,159,0.15);border:1px solid var(--green);border-radius:6px;padding:6px 10px;font-size:10px;color:var(--green);text-align:center;font-family:'Roboto Mono',monospace;}
/* Intel */
.cmems-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.cmems-val{font-size:16px;font-weight:700;color:var(--cyan);font-family:'Roboto Mono',monospace;}
.contact-row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid rgba(0,212,255,0.07);font-size:10px;}
/* Comms */
.cmd-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;}
.cmd-btn{padding:8px 4px;background:rgba(0,212,255,0.08);color:var(--cyan);border:1px solid rgba(0,212,255,0.2);border-radius:7px;font-size:10px;font-weight:600;cursor:pointer;letter-spacing:.5px;transition:all .2s;font-family:'Roboto Mono',monospace;}
.cmd-btn:hover{background:rgba(0,212,255,0.2);}
.cmd-btn.danger{background:rgba(255,71,87,0.1);color:var(--red);border-color:rgba(255,71,87,0.3);}
.cmd-btn.danger:hover{background:rgba(255,71,87,0.25);}
#mic-btn{width:100%;padding:12px;background:rgba(0,212,255,0.08);color:var(--cyan);border:1px solid rgba(0,212,255,0.25);border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;font-family:'Roboto Mono',monospace;}
#mic-btn.recording{background:rgba(255,71,87,0.2);color:var(--red);border-color:rgba(255,71,87,0.4);animation:pulse 1s infinite;}
#chat-log{font-size:10px;max-height:100px;overflow-y:auto;}
.chat-line-user{color:var(--amber);margin:2px 0;}
.chat-line-ai{color:var(--green);margin:2px 0;}
/* Map overlays */
#hud-map-pos{position:absolute;top:10px;left:10px;z-index:10;background:var(--glass);border:1px solid var(--border);border-radius:8px;padding:7px 12px;backdrop-filter:blur(12px);font-family:'Roboto Mono',monospace;font-size:11px;}
</style>
</head>
<body>
<div id="layout">
  <div id="map-panel">
    <div id="map"></div>
    <div id="hud-map-pos">
      <span id="mp-sog" style="color:var(--cyan);font-weight:600;">-- kn</span>
      &nbsp;&nbsp;<span id="mp-hdg" style="color:var(--dim);">HDG --</span>
      &nbsp;&nbsp;<span id="mp-pos" style="color:var(--dim);font-size:10px;">--</span>
    </div>
  </div>

  <div id="side">
    <!-- Tab bar -->
    <div class="tabs" style="flex-wrap:wrap;">
      <div class="tab active" onclick="switchTab('audio')" style="flex:1 0 25%;">AUDIO</div>
      <div class="tab" onclick="switchTab('scenario')" style="flex:1 0 25%;">SCENARIO</div>
      <div class="tab" onclick="switchTab('engine')" style="flex:1 0 25%;">ENGINE</div>
      <div class="tab" onclick="switchTab('env')" style="flex:1 0 25%;">ENV</div>
      <div class="tab" onclick="switchTab('intel')" style="flex:1 0 25%;">INTEL</div>
      <div class="tab" onclick="switchTab('comms')" style="flex:1 0 25%;">COMMS</div>
      <div class="tab" onclick="switchTab('air')" style="flex:1 0 25%;color:#a78bfa;">AIR</div>
    </div>

    <!-- AUDIO TAB -->
    <div class="panel active" id="tab-audio">
      <div id="player-main">
        <button id="arm-btn-v" onclick="armAudio()">&#x26A1; TAP TO ARM AUTO-PLAY</button>
        <div id="armed-badge-v">&#x25B6; AUTO-PLAY ARMED &mdash; LIVE MONITORING</div>
        <div id="now-type">STANDBY</div>
        <div id="now-text">No briefing playing</div>
        <audio id="mainAudio" controls></audio>
      </div>
      <div class="card">
        <div class="card-title">Recent briefings</div>
        <div id="audio-list"><div style="color:var(--dim);font-size:10px;">Loading...</div></div>
      </div>
    </div>

    <!-- SCENARIO TAB -->
    <div class="panel" id="tab-scenario">
      <div class="card">
        <div class="card-title">Location Preset</div>
        <select id="preset-select" onchange="applyPreset()">
          <option value="">-- SELECT --</option>
          <optgroup label="AUST/NZ PORTS">
            <option value="-38.487,145.620">Yaringa Boat Harbour</option>
            <option value="-38.452,145.237">Cowes Jetty</option>
            <option value="-33.85,151.27">Sydney Harbour</option>
            <option value="-12.46,130.82">Darwin Harbour</option>
            <option value="-32.05,115.73">Fremantle</option>
            <option value="-20.3,118.57">Port Hedland</option>
            <option value="-18.28,147.69">Great Barrier Reef</option>
            <option value="-39.5,145.0">Bass Strait</option>
            <option value="-42.88,147.32">Hobart</option>
            <option value="-14.0,139.0">Gulf of Carpentaria</option>
            <option value="-33.5,130.0">Great Australian Bight</option>
            <option value="-36.8,174.7">Auckland</option>
            <option value="-41.2,174.5">Cook Strait</option>
          </optgroup>
          <optgroup label="GLOBAL CHOKEPOINTS">
            <option value="26.56,56.26">Strait of Hormuz</option>
            <option value="12.58,43.33">Bab el-Mandeb</option>
            <option value="29.92,32.55">Suez Canal</option>
            <option value="9.12,-79.73">Panama Canal</option>
            <option value="1.25,103.83">Strait of Malacca</option>
            <option value="35.97,-5.5">Strait of Gibraltar</option>
            <option value="41.0,29.0">Bosphorus Strait</option>
            <option value="24.5,119.5">Taiwan Strait</option>
            <option value="50.0,1.0">English Channel</option>
            <option value="55.7,12.8">Oresund</option>
            <option value="-53.28,-73.34">Strait of Magellan</option>
          </optgroup>
          <optgroup label="HIGH RISK / WAR ZONES">
            <option value="44.5,33.5">Black Sea</option>
            <option value="13.0,47.0">Gulf of Aden</option>
            <option value="10.0,114.0">South China Sea</option>
            <option value="33.5,34.0">Eastern Mediterranean</option>
            <option value="46.0,36.5">Sea of Azov</option>
            <option value="3.0,5.0">Gulf of Guinea</option>
            <option value="37.0,124.0">Yellow Sea</option>
            <option value="15.0,65.0">Arabian Sea</option>
            <option value="5.0,50.0">Somali Basin</option>
          </optgroup>
          <optgroup label="HAZARDS & EXTREME WEATHER">
            <option value="-56.5,-67.2">Cape Horn</option>
            <option value="-34.8,19.9">Cape of Good Hope</option>
            <option value="45.5,-5.0">Bay of Biscay</option>
            <option value="58.0,-175.0">Bering Sea</option>
            <option value="-45.0,90.0">Southern Ocean</option>
            <option value="57.0,-145.0">Gulf of Alaska</option>
            <option value="55.0,3.0">North Sea</option>
            <option value="58.0,-55.0">Labrador Sea</option>
            <option value="28.0,-65.0">Sargasso Sea</option>
            <option value="-40.0,155.0">Tasman Sea</option>
          </optgroup>
          <optgroup label="MAJOR WATERWAYS">
            <option value="25.0,-90.0">Gulf of Mexico</option>
            <option value="15.0,-75.0">Caribbean Sea</option>
            <option value="57.0,19.0">Baltic Sea</option>
            <option value="43.0,15.0">Adriatic Sea</option>
            <option value="10.0,95.0">Andaman Sea</option>
            <option value="-15.0,150.0">Coral Sea</option>
            <option value="65.0,5.0">Norwegian Sea</option>
            <option value="15.0,130.0">Philippine Sea</option>
          </optgroup>
          <option value="custom">Custom Coordinates</option>
        </select>
      </div>
      <div class="card">
        <div class="card-title">Vessel Position</div>
        <div class="fields-row">
          <div><div class="field-lbl">LAT</div><input type="text" id="sc-lat" placeholder="-38.487"/></div>
          <div><div class="field-lbl">LON</div><input type="text" id="sc-lon" placeholder="145.620"/></div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Scenario Parameters</div>
        <div class="fields-row">
          <div><div class="field-lbl">WIND (kn)</div><input type="number" id="sc-wind" placeholder="15"/></div>
          <div><div class="field-lbl">HDG (&deg;)</div><input type="number" id="sc-hdg" placeholder="270"/></div>
          <div><div class="field-lbl">SOG (kn)</div><input type="number" id="sc-sog" placeholder="8"/></div>
          <div><div class="field-lbl">CONDITION</div>
            <select id="sc-cond"><option value="calm">Calm</option><option value="choppy">Choppy</option><option value="rough">Rough</option><option value="storm">Storm</option></select>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Current Injected Scenario</div>
        <div id="current-scenario" style="font-size:10px;color:var(--text);font-family:'Roboto Mono',monospace;line-height:1.6;">Loading...</div>
      </div>
      <button class="btn-inject" onclick="injectScenario()">&#x26A1; INJECT SCENARIO</button>
      <div class="toast" id="inject-toast">&#x2714; SCENARIO INJECTED</div>
      <button class="btn-kill" onclick="killSim()">&#x26A0; STOP SIMULATOR</button>
    </div>

    <!-- ENGINE TAB -->
    <div class="panel" id="tab-engine">
      <div class="card">
        <div class="card-title">Propulsion</div>
        <div class="fields-row">
          <div><div class="field-lbl">RPM</div><input type="number" id="sc-rpm" value="1200"/></div>
          <div><div class="field-lbl">FUEL %</div><input type="number" id="sc-fuel" value="95"/></div>
        </div>
        <div style="display:flex;gap:6px;margin-top:6px;">
          <button class="cmd-btn" onclick="document.getElementById('sc-rpm').value=0;document.getElementById('sc-sog').value=8;injectScenario()" style="flex:1;">UNDER SAIL</button>
          <button class="cmd-btn" onclick="document.getElementById('sc-rpm').value=2200;document.getElementById('sc-sog').value=16;injectScenario()" style="flex:1;">UNDER MOTOR</button>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Power Systems</div>
        <div class="fields-row">
          <div><div class="field-lbl">BATTERY (V)</div><input type="number" id="sc-bat" value="13.5" step="0.1"/></div>
        </div>
      </div>
      <button class="btn-inject" onclick="injectScenario()">&#x26A1; INJECT STATE</button>
    </div>

    <!-- ENV TAB -->
    <div class="panel" id="tab-env">
      <div class="card">
        <div class="card-title">Environment Sensors</div>
        <div class="fields-row">
          <div><div class="field-lbl">DEPTH (m)</div><input type="number" id="sc-depth" value="45"/></div>
          <div><div class="field-lbl">EXT TEMP (&deg;C)</div><input type="number" id="sc-out-temp" value="22.5"/></div>
          <div><div class="field-lbl">ROLL (&deg;)</div><input type="number" id="sc-roll" value="0"/></div>
        </div>
      </div>
      <button class="btn-inject" onclick="injectScenario()">&#x26A1; INJECT STATE</button>
    </div>

    <!-- INTEL TAB -->
    <div class="panel" id="tab-intel">
      <div class="card">
        <div class="card-title">CMEMS Ocean Data <span id="cmems-badge2" style="margin-left:6px;font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(255,179,0,0.15);color:var(--amber);border:1px solid var(--amber);">UPDATING</span></div>
        <div class="cmems-grid">
          <div><div class="field-lbl">Wave H</div><div class="cmems-val" id="i-wave">--</div></div>
          <div><div class="field-lbl">Swell H</div><div class="cmems-val" id="i-swell">--</div></div>
          <div><div class="field-lbl">Current</div><div class="cmems-val" id="i-curr">--</div></div>
          <div><div class="field-lbl">SST</div><div class="cmems-val" id="i-sst">--</div></div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">AIS Vessel DB
          <select id="ais-sel" onchange="aisSelect()" style="margin-top:6px;font-size:10px;">
            <option value="">-- SELECT VESSEL --</option>
          </select>
        </div>
        <div id="ais-detail" style="display:none;font-size:10px;margin-top:8px;line-height:1.6;color:var(--text);"></div>
        <button class="cmd-btn" onclick="aisTrack()" style="margin-top:6px;width:100%;">TRACK ON MAP</button>
      </div>
      <div class="card">
        <div class="card-title">Radar Contacts</div>
        <div id="contact-list" style="font-size:10px;color:var(--dim);">Scanning...</div>
      </div>
      <div class="card">
        <div class="card-title">Sea State Map <button onclick="changeZ(-1)" style="background:none;border:1px solid var(--border);color:var(--dim);padding:1px 5px;border-radius:3px;cursor:pointer;font-size:10px;">-</button> <span id="sz-lbl">4</span> <button onclick="changeZ(1)" style="background:none;border:1px solid var(--border);color:var(--dim);padding:1px 5px;border-radius:3px;cursor:pointer;font-size:10px;">+</button></div>
        <img id="wave-img" style="width:100%;border-radius:6px;border:1px solid var(--border);margin-top:4px;" src="/wave_map?w=320&h=180&z=4"/>
      </div>
      <div class="card">
        <div class="card-title">AIS Traffic Map <button onclick="changeAZ(-1)" style="background:none;border:1px solid var(--border);color:var(--dim);padding:1px 5px;border-radius:3px;cursor:pointer;font-size:10px;">-</button> <span id="az-lbl">4</span> <button onclick="changeAZ(1)" style="background:none;border:1px solid var(--border);color:var(--dim);padding:1px 5px;border-radius:3px;cursor:pointer;font-size:10px;">+</button></div>
        <img id="ais-img" style="width:100%;border-radius:6px;border:1px solid var(--border);margin-top:4px;" src="/ais_map?w=320&h=180&z=4"/>
      </div>
    </div>

    <!-- COMMS TAB -->
    <div class="panel" id="tab-comms">
      <div class="card">
        <div class="card-title">Voice Command</div>
        <button id="mic-btn" onmousedown="startRec()" onmouseup="stopRec()" ontouchstart="startRec()" ontouchend="stopRec()">&#x1F3A4; HOLD TO SPEAK</button>
        <div id="chat-log" style="margin-top:8px;"></div>
      </div>
      <div class="card">
        <div class="card-title">Course Override</div>
        <input type="text" id="dest-in" placeholder="Enter destination or coordinates..."/>
        <div style="display:flex;gap:6px;margin-top:6px;">
          <button class="cmd-btn" onclick="setDest()" style="flex:1;">SET COURSE</button>
          <button class="cmd-btn" onclick="clearRoute()" style="flex:0 0 70px;background:rgba(255,71,87,0.1);color:var(--red);border-color:rgba(255,71,87,0.3);">CLEAR</button>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Quick Commands</div>
        <div class="cmd-grid">
          <button class="cmd-btn danger" onclick="reqG('MAYDAY')">&#x1F6A8; MAYDAY</button>
          <button class="cmd-btn" onclick="reqG('DAY_BRIEF')">&#x2600; DAY BRIEF</button>
          <button class="cmd-btn" onclick="reqG('NIGHT_BRIEF')">&#x1F319; NIGHT BRIEF</button>
          <button class="cmd-btn" onclick="reqG('WEATHER_SITREP')">&#x26C8; WEATHER</button>
          <button class="cmd-btn" onclick="reqRoute('ANCHORAGE')">&#x2693; ANCHORAGE</button>
          <button class="cmd-btn" onclick="reqRoute('PORT')">&#x1F6A2; PORTS</button>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Systems</div>
        <div class="cmd-grid">
          <button class="cmd-btn" onclick="sysCmd('sys_gen_start')">GEN START</button>
          <button class="cmd-btn" onclick="sysCmd('sys_gen_stop')">GEN STOP</button>
          <button class="cmd-btn" onclick="sysCmd('sys_bilge_auto')">BILGE PUMP</button>
          <button class="cmd-btn" onclick="reqG('ENGAGE_RADAR')">RADAR ON</button>
          <button class="cmd-btn" onclick="reqG('DISENGAGE_RADAR')">RADAR OFF</button>
          <button class="cmd-btn" onclick="reqG('ENGAGE_SONAR')">SONAR ON</button>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
// Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Globals Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
let map, vesselMarker, watchMarker, trackPoly, routePoly;
let lastLat=-38.487, lastLon=145.620;
let showTrack=true, trackedMmsi=null;
let autoPlayArmed=false, playedIds=new Set(), audioQueue=[], isPlaying=false;
let waveZ=4, aisZ=4;
let mediaRec, audioChunks=[], isRec=false;
let aisDb={}, aisMarkers={};


// Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Tab switching Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
function switchTab(name){
  document.querySelectorAll('.tab').forEach((t,i)=>{t.classList.remove('active');});
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelector('.tab[onclick*="'+name+'"]').classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
}

function initMap(){
  map=L.map('map',{zoomControl:false,attributionControl:false,doubleClickZoom:false});
  map.setView([lastLat,lastLon],11);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{maxZoom:20}).addTo(map);
  const vi=L.divIcon({className:'',html:'<div style="font-size:26px;color:#00d4ff;filter:drop-shadow(0 0 5px #00d4ff);line-height:1;">&#x25B2;</div>',iconSize:[26,26],iconAnchor:[13,13]});
  const wi=L.divIcon({className:'',html:'<div style="width:13px;height:13px;border-radius:50%;background:#00ff9f;border:2px solid #fff;box-shadow:0 0 8px #00ff9f;"></div>',iconSize:[13,13],iconAnchor:[6,6]});
  vesselMarker=L.marker([lastLat,lastLon],{icon:vi}).addTo(map);
  watchMarker=L.marker([0,0],{icon:wi}).addTo(map);
  trackPoly=L.polyline([],{color:'#00d4ff',weight:1.5,opacity:0.5}).addTo(map);
}
initMap();

function armAudio(){
  const ctx=new(window.AudioContext||window.webkitAudioContext)();
  const b=ctx.createBuffer(1,1,22050); const s=ctx.createBufferSource(); s.buffer=b; s.connect(ctx.destination); s.start(0);
  autoPlayArmed=true;
  document.getElementById('arm-btn-v').style.display='none';
  document.getElementById('armed-badge-v').style.display='block';
}
function playNextInQueue(){
  if(isPlaying||audioQueue.length===0) return;
  isPlaying=true;
  const item=audioQueue.shift();
  const a=document.getElementById('mainAudio');
  a.src='/audio?id='+item.id+'&bust='+Date.now(); a.load();
  a.play().then(()=>{
    const lbl=(item.type||'status').toUpperCase().replace('_',' ');
    const URGENT=['MAYDAY','SYSTEM STATUS','KINETIC THREAT'];
    document.getElementById('now-type').style.color=URGENT.includes(lbl)?'#ff4757':'#00d4ff';
    document.getElementById('now-type').innerText=lbl;
    document.getElementById('now-text').innerText=item.text?item.text.substring(0,120)+'...':'';
  }).catch(e=>{isPlaying=false;setTimeout(playNextInQueue,500);});
  a.onended=()=>{isPlaying=false;setTimeout(playNextInQueue,500);};
}

let isInit=false;
async function tick(){
  try{
    const d=await(await fetch('/status_api?bust='+Date.now())).json();
    lastLat=d.lat||lastLat; lastLon=d.lon||lastLon;
    vesselMarker.setLatLng([lastLat,lastLon]);
    document.getElementById('mp-pos').innerText=lastLat.toFixed(4)+','+lastLon.toFixed(4);
    document.getElementById('mp-sog').innerText=(d.sog!=null?(+d.sog).toFixed(1)+' kn':'-- kn');
    document.getElementById('mp-hdg').innerText='HDG '+(d.heading!=null?Math.round(d.heading)+'&deg;':'--');
    const wL=d.CREW_LAT||0; if(wL) watchMarker.setLatLng([wL,d.CREW_LON||0]);
    if(!isInit&&lastLat!==0){
      const grp=wL?L.featureGroup([vesselMarker,watchMarker]):L.featureGroup([vesselMarker]);
      try{map.fitBounds(grp.getBounds().pad(0.3),{maxZoom:13});}catch(e){}
      isInit=true;
    }
    // Auto-play queue Ã¢â‚¬â€ newest-first, fresh reports only
    if(d.audio_history&&autoPlayArmed){
      const URGENT=['MAYDAY','KINETIC_THREAT','SYSTEM_STATUS'];
      const MAX_AGE_S=600; const nowSec=Date.now()/1000;
      const sorted=[...d.audio_history].sort((a,b)=>{
        const aUr=URGENT.includes((a.type||'').toUpperCase())?1:0;
        const bUr=URGENT.includes((b.type||'').toUpperCase())?1:0;
        if(bUr!==aUr) return bUr-aUr;
        return (b.ts||0)-(a.ts||0);
      });
      sorted.forEach(h=>{ if((nowSec-(h.ts||0))>MAX_AGE_S) playedIds.add(h.id); });
      const newItems=sorted.filter(h=>h.ready!==false&&!playedIds.has(h.id));
      newItems.forEach(h=>playedIds.add(h.id));
      if(newItems.length>0){
        audioQueue.splice(0,0,...newItems);
        const hasUrgent=newItems.some(h=>URGENT.includes((h.type||'').toUpperCase()));
        if(hasUrgent&&isPlaying){
          const curType=(document.getElementById('now-type').innerText||'').replace(' ','_');
          if(!URGENT.includes(curType)){document.getElementById('mainAudio').pause();isPlaying=false;}
        }
      }
      playNextInQueue();
    }
    // Audio history list
    if(d.audio_history){
      const rev=[...d.audio_history].reverse();
      document.getElementById('audio-list').innerHTML=rev.map(h=>{
        const typeLabel=(h.type||'sys').toUpperCase().replace('_',' ');
        const txt=h.text?h.text.substring(0,80)+'...':'';
        const btn=h.ready===false?'<span style="color:var(--amber);font-size:9px;">GENERATING...</span>':
          '<button class="play-btn2" onclick="manualPlay('+h.id+')">&#x25B6; PLAY</button>';
        return '<div class="audio-item"><span class="a-type">'+typeLabel+'</span><div class="a-txt">'+txt+'</div>'+btn+'</div>';
      }).join('');
    }
    // Contacts
    if(d.radar_contacts){
      updateAisMarkers(d.radar_contacts);
      populateAis(d.radar_contacts);
      document.getElementById('contact-list').innerHTML=d.radar_contacts.slice(0,8).map(c=>
        '<div class="contact-row"><span style="color:var(--cyan);">'+(c.name||'Unknown')+'</span><span style="color:var(--dim);">'+(c.range_nm!=null?c.range_nm.toFixed(1)+' nm':'')+'</span></div>'
      ).join('')||'<span style="color:var(--dim);">No contacts</span>';
    }
    // Current scenario
    try{const sc=await(await fetch('/scenario')).json();
      document.getElementById('current-scenario').innerHTML=
        Object.entries(sc).filter(([k])=>k!=='id').map(([k,v])=>'<div><span style="color:var(--dim);">'+k+':</span> <span style="color:var(--cyan);">'+v+'</span></div>').join('')||'No active scenario';
    }catch(e){}
  }catch(e){console.error(e);}
}

// Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ AIS map markers Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
// AIS type -> color + symbol map (mirrors classify_vessel Python output)
const _AIS_COL = {
  'Cargo':'#c8c8c8','Tanker':'#ff8200','Passenger':'#5080ff','Military':'#ff3c3c',
  'Law Enforcement':'#6464ff','Fishing':'#00c850','Sailing':'#ffffff','Pleasure Craft':'#00dcdc',
  'SAR':'#ff3c3c','Pilot':'#00c8ff','Tug':'#ffb300','Medical':'#ffffff','Fire Fighting':'#ff5000',
  'WIG':'#00c8b4','High Speed':'#00c8ff','Unknown':'#a0a0a0','Other':'#a0a0a0'
};
const _AIS_SYM = {
  'Military':'&#9670;','Law Enforcement':'&#9670;','SAR':'&#43;','Medical':'&#43;',
  'Fire Fighting':'&#43;','Tanker':'&#9650;','Passenger':'&#9650;','Cargo':'&#9650;',
  'Sailing':'&#9651;'
};
function _makeAisIcon(c){
  const vt = c.vessel_type || c.type || 'Unknown';
  const col = _AIS_COL[vt] || '#a0a0a0';
  const sym = _AIS_SYM[vt] || '&#9679;';
  const pulse = (vt==='Military'||vt==='Law Enforcement'||vt==='SAR') ?
    'animation:aisPulse 1.4s ease-in-out infinite;' : 'animation:aisPulse 2.5s ease-in-out infinite;';
  return L.divIcon({className:'',
    html:'<div style="width:12px;height:12px;border-radius:50%;background:'+col+
         ';border:1.5px solid '+col+';box-shadow:0 0 6px '+col+
         ';display:flex;align-items:center;justify-content:center;font-size:7px;color:#000;font-weight:bold;'+pulse+'">'+(sym==='&#9679;'?'':sym)+'</div>',
    iconSize:[12,12],iconAnchor:[6,6]});
}
function updateAisMarkers(contacts){
  const seen=new Set();
  (contacts||[]).forEach(c=>{
    if(!c.lat||!c.lon) return;
    const key=String(c.mmsi||c.name||(c.lat+','+c.lon));
    seen.add(key);
    const icon=_makeAisIcon(c);
    if(aisMarkers[key]){
      aisMarkers[key].setLatLng([c.lat,c.lon]);
      aisMarkers[key].setIcon(icon);
    } else {
      const vt=c.vessel_type||c.type||'Unknown';
      const col=_AIS_COL[vt]||'#a0a0a0';
      const m=L.marker([c.lat,c.lon],{icon:icon}).addTo(map);
      m.bindPopup('<div style="font-family:monospace;font-size:11px;background:#050d1a;color:#c8e6ff;padding:6px 8px;border-radius:4px;">'
        +'<b style="color:'+col+'">'+(c.name||'Unknown')+'</b><br>'
        +'<span style="font-size:9px;color:'+col+';background:rgba(0,0,0,0.4);padding:1px 4px;border-radius:2px;">'+vt+'</span><br>'
        +(c.mmsi?'MMSI: '+c.mmsi+'<br>':'')
        +(c.speed!=null?'SOG: '+c.speed+' kn<br>':'')
        +(c.destination?'&#x2192; '+c.destination+'<br>':'')
        +(c.range_nm?'Range: '+c.range_nm.toFixed(1)+' nm':'')
        +'</div>');
      aisMarkers[key]=m;
    }
  });
  Object.keys(aisMarkers).forEach(k=>{if(!seen.has(k)){aisMarkers[k].remove();delete aisMarkers[k];}});
}

// Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ ADS-B tick Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
let adsbMarkers={};
async function tickAdsb(){
  try{
    const d=await(await fetch('/adsb_contacts')).json();
    const contacts=d.contacts||[];
    const age=d.cache_age_s||0;
    // Update badge
    const b=document.getElementById('adsb-badge');
    if(b){
      b.innerText=contacts.length+' AIRCRAFT'+(age>120?' (STALE)':'');
      b.style.color=age>120?'var(--amber)':'var(--green)';
    }
    // Update list
    const el=document.getElementById('adsb-list');
    if(!el) return;
    if(contacts.length===0){el.innerHTML='<div style="color:var(--dim);font-size:10px;text-align:center;padding:12px;">No aircraft in range</div>';return;}
    el.innerHTML=contacts.map(c=>{
      const pri=c.priority||'ROUTINE';
      const isCrit=pri==='CRITICAL'; const isWatch=pri==='WATCH';
      const col=isCrit?'var(--red)':isWatch?'var(--amber)':'var(--text)';
      const bg=isCrit?'rgba(255,71,87,0.12)':isWatch?'rgba(255,179,0,0.08)':'rgba(0,0,0,0.2)';
      const bdr=isCrit?'rgba(255,71,87,0.5)':isWatch?'rgba(255,179,0,0.4)':'var(--border)';
      const badge=(isCrit||isWatch||c.squawk)?`<span style="color:${col};font-size:8px;font-weight:bold;background:rgba(255,179,0,0.1);border:1px solid ${col};padding:1px 4px;border-radius:2px;margin-left:3px;">SQK ${c.squawk||'----'} ${pri!=='ROUTINE'?pri:''}</span>`:'';
      const eta=c.eta_min?`<span style="color:var(--red);font-size:8px;"> ETA ${c.eta_min}min</span>`:'';
      return `<div style="background:${bg};border:1px solid ${bdr};border-radius:7px;padding:7px 9px;margin-bottom:5px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px;">
          <span style="color:${col};font-family:'Roboto Mono',monospace;font-weight:bold;font-size:12px;">&#x2708; ${c.callsign||c.icao||'????'}</span>${badge}${eta}
          <span style="color:var(--dim);font-size:9px;">${c.country||''}</span>
        </div>
        <div style="font-size:9px;color:#888;margin-bottom:2px;">${c.type||'Unknown'}</div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:2px;font-size:9px;font-family:'Roboto Mono',monospace;color:var(--dim);">
          <span>FL${Math.round((c.alt_ft||0)/100)}</span>
          <span>${c.spd_kts!=null?Math.round(c.spd_kts)+'kts':'-kts'} ${c.track!=null?Math.round(c.track)+'&deg;':''}</span>
          <span style="color:var(--cyan);">${c.range_nm!=null?c.range_nm+'nm':'--'} @${c.bearing!=null?Math.round(c.bearing)+'&deg;':'--'}</span>
        </div>
      </div>`;
    }).join('');
    // ADS-B aircraft on map
    const seen=new Set();
    const acIcon=L.divIcon({className:'',html:'<div style="font-size:11px;color:#fff;background:rgba(20,0,60,0.88);border:1.5px solid #a78bfa;border-radius:3px;width:20px;height:20px;line-height:18px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,0.9);">&#x2708;</div>',iconSize:[20,20],iconAnchor:[10,10]});
    contacts.slice(0,30).forEach(c=>{
      if(!c.lat||!c.lon||c.on_ground) return;
      const key=c.icao||c.callsign;
      seen.add(key);
      if(adsbMarkers[key]){
        adsbMarkers[key].setLatLng([c.lat,c.lon]);
        adsbMarkers[key].setPopupContent(`<div style="font-family:monospace;font-size:11px;background:#050d1a;color:#c8e6ff;padding:8px 10px;border-radius:6px;min-width:160px;"><b style="color:#a78bfa;font-size:13px;">&#x2708; ${c.callsign||c.icao||'???'}</b><br><span style="font-size:9px;color:#aaa;">${c.type||'Unknown'} &bull; ${c.country||''}</span><br><hr style="border-color:rgba(255,255,255,0.1);margin:4px 0;">FL${Math.round((c.alt_ft||0)/100)} &bull; ${c.spd_kts!=null?Math.round(c.spd_kts)+'kts':'?kts'} ${c.track!=null?Math.round(c.track)+'&deg;':''}<br>Range: ${c.range_nm||'?'}nm @ ${c.bearing!=null?Math.round(c.bearing):'?'}&deg;${c.squawk?'<br>Squawk: <b>'+c.squawk+'</b>':''}${c.closing_rate_kts>2?'<br><span style="color:#ff5555;">&#x21D3; Closing '+Math.round(c.closing_rate_kts)+'kts'+( c.eta_min?' ETA '+c.eta_min+'min':'')+'</span>':''}</div>`);
      } else {
        const m=L.marker([c.lat,c.lon],{icon:acIcon}).addTo(map);
        m.bindPopup(`<div style="font-family:monospace;font-size:11px;background:#050d1a;color:#c8e6ff;padding:8px 10px;border-radius:6px;min-width:160px;"><b style="color:#a78bfa;font-size:13px;">&#x2708; ${c.callsign||c.icao||'???'}</b><br><span style="font-size:9px;color:#aaa;">${c.type||'Unknown'} &bull; ${c.country||''}</span><br><hr style="border-color:rgba(255,255,255,0.1);margin:4px 0;">FL${Math.round((c.alt_ft||0)/100)} &bull; ${c.spd_kts!=null?Math.round(c.spd_kts)+'kts':'?kts'} ${c.track!=null?Math.round(c.track)+'&deg;':''}<br>Range: ${c.range_nm||'?'}nm @ ${c.bearing!=null?Math.round(c.bearing):'?'}&deg;${c.squawk?'<br>Squawk: <b>'+c.squawk+'</b>':''}${c.closing_rate_kts>2?'<br><span style="color:#ff5555;">&#x21D3; Closing '+Math.round(c.closing_rate_kts)+'kts'+( c.eta_min?' ETA '+c.eta_min+'min':'')+'</span>':''}</div>`);
        adsbMarkers[key]=m;
      }
    });
    Object.keys(adsbMarkers).forEach(k=>{if(!seen.has(k)){adsbMarkers[k].remove();delete adsbMarkers[k];}});
  }catch(e){}
}

// Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ CMEMS tick Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
async function tickCmems(){
  try{
    const d=await(await fetch('/sea_state_json')).json();
    const f=v=>v&&v!='n/a'?v:'--';
    document.getElementById('i-wave').innerText=f(d.wave_h);
    document.getElementById('i-swell').innerText=f(d.swell_h);
    document.getElementById('i-curr').innerText=f(d.curr_v);
    document.getElementById('i-sst').innerText=f(d.sst_c);
    const b=document.getElementById('cmems-badge2');
    if(d.cmems_stale||d.wave_h==='n/a'){b.style.background='rgba(255,179,0,0.15)';b.style.color='var(--amber)';b.style.borderColor='var(--amber)';b.innerText='UPDATING';}
    else{b.style.background='rgba(0,255,159,0.12)';b.style.color='var(--green)';b.style.borderColor='var(--green)';b.innerText='LIVE';}
  }catch(e){}
}

// Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Track history Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
async function tickTrack(){
  try{const h=await(await fetch('/history_api')).json(); trackPoly.setLatLngs(h);}catch(e){}
}

// Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Maps refresh Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
function refreshMaps(){
  const b=Date.now();
  document.getElementById('wave-img').src='/wave_map?w=320&h=180&z='+waveZ+'&lat='+lastLat+'&lon='+lastLon+'&bust='+b;
  document.getElementById('ais-img').src='/ais_map?w=320&h=180&z='+aisZ+'&lat='+lastLat+'&lon='+lastLon+'&bust='+b;
}
function changeZ(d){waveZ=Math.max(1,Math.min(9,waveZ+d));document.getElementById('sz-lbl').innerText=waveZ;refreshMaps();}
function changeAZ(d){aisZ=Math.max(1,Math.min(9,aisZ+d));document.getElementById('az-lbl').innerText=aisZ;refreshMaps();}

// Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ AIS Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
function populateAis(contacts){
  aisDb={};
  const sel=document.getElementById('ais-sel');
  const prev=sel.value;
  sel.innerHTML='<option value="">-- SELECT VESSEL --</option>';
  contacts.filter(c=>c.mmsi||c.name).forEach(c=>{
    const k=c.mmsi||c.id||c.name;
    aisDb[k]=c;
    const o=document.createElement('option'); o.value=k;
    const r=c.range_nm?' | '+c.range_nm.toFixed(1)+'nm':'';
    o.text=(c.name||'Unknown')+r;
    sel.appendChild(o);
  });
  if(prev&&aisDb[prev]) sel.value=prev;
}
function aisSelect(){
  const k=document.getElementById('ais-sel').value;
  const box=document.getElementById('ais-detail');
  if(!k||!aisDb[k]){box.style.display='none';return;}
  const c=aisDb[k]; box.style.display='block';
  box.innerHTML=[
    '<b style="color:var(--cyan);">'+(c.name||'Unknown')+'</b>',
    c.mmsi?'<div><span style="color:var(--dim);">MMSI:</span> '+c.mmsi+'</div>':'',
    c.destination?'<div><span style="color:var(--dim);">Dest:</span> <span style="color:var(--amber);">'+c.destination+'</span></div>':'',
    c.speed!=null?'<div><span style="color:var(--dim);">Speed:</span> '+c.speed+' kn</div>':'',
    c.range_nm?'<div><span style="color:var(--dim);">Range:</span> '+c.range_nm.toFixed(2)+' nm</div>':''
  ].join('');
}
function aisTrack(){
  const k=document.getElementById('ais-sel').value;
  if(!k||!aisDb[k]) return;
  const c=aisDb[k]; if(c.lat&&c.lon){trackedMmsi=k;map.panTo([c.lat,c.lon]);map.setZoom(12);}
}

// Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Scenario Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
function applyPreset(){
  const v=document.getElementById('preset-select').value;
  if(!v||v==='custom') return;
  const [lat,lon]=v.split(',');
  document.getElementById('sc-lat').value=lat;
  document.getElementById('sc-lon').value=lon;
}
async function injectScenario(){
  const latR = parseFloat(document.getElementById('sc-lat').value)||lastLat;
  const lonR = parseFloat(document.getElementById('sc-lon').value)||lastLon;
  const wR = parseFloat(document.getElementById('sc-wind').value)||15;
  const hdgR = parseFloat(document.getElementById('sc-hdg').value)||270;
  const sogR = parseFloat(document.getElementById('sc-sog').value)||8;
  const rpmR = parseFloat(document.getElementById('sc-rpm')?document.getElementById('sc-rpm').value:1200);
  const fuelR = parseFloat(document.getElementById('sc-fuel')?document.getElementById('sc-fuel').value:95);
  const batR = parseFloat(document.getElementById('sc-bat')?document.getElementById('sc-bat').value:13.5);
  const depthR = parseFloat(document.getElementById('sc-depth')?document.getElementById('sc-depth').value:45);
  const outT = parseFloat(document.getElementById('sc-out-temp')?document.getElementById('sc-out-temp').value:22.5);
  const rollR = parseFloat(document.getElementById('sc-roll')?document.getElementById('sc-roll').value:0);

  const payload={ lat:latR, lon:lonR, wind_kn:wR, heading:hdgR, sog:sogR, condition:document.getElementById('sc-cond').value||'calm' };
  const headless_payload = { 
     base_lat:latR, base_lon:lonR, aws_kts:wR, cog_deg:hdgR, sog_kts:sogR,
     rpm:isNaN(rpmR)?1200:rpmR, fuel_pct:isNaN(fuelR)?95:fuelR, bat_v:isNaN(batR)?13.5:batR, depth_m:isNaN(depthR)?45:depthR, out_temp_c:isNaN(outT)?22.5:outT, roll_deg:isNaN(rollR)?0:rollR
  };
  try{
    await fetch('/update_scenario',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    await fetch('/api/sim_override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(headless_payload)});
    const t=document.getElementById('inject-toast'); t.style.display='block';
    setTimeout(()=>t.style.display='none',2500);
  }catch(e){alert('Inject failed: '+e);}
}
async function killSim(){
  if(!confirm('Stop the headless simulator? The physical watch will resume control.')) return;
  await fetch('/kill_sim',{method:'POST'});
}

// Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Manual audio play Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
function manualPlay(id){
  const a=document.getElementById('mainAudio');
  a.src='/audio?id='+id+'&bust='+Date.now(); a.load(); a.play().catch(e=>{});
  isPlaying=false;
}

// Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Voice recording Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
async function startRec(){
  if(isRec) return; isRec=true;
  const btn=document.getElementById('mic-btn');
  btn.classList.add('recording'); btn.innerText='Ã°Å¸â€Â´ RECORDING...';
  try{
    const stream=await navigator.mediaDevices.getUserMedia({audio:true});
    mediaRec=new MediaRecorder(stream,{mimeType:'audio/webm'});
    audioChunks=[];
    mediaRec.ondataavailable=e=>audioChunks.push(e.data);
    mediaRec.onstop=()=>{sendVoice();audioChunks=[];isRec=false;};
    mediaRec.start();
  }catch(e){isRec=false;btn.classList.remove('recording');btn.innerText='Ã°Å¸Å½Â¤ HOLD TO SPEAK';}
}
function stopRec(){
  if(mediaRec&&mediaRec.state==='recording'){mediaRec.stop();mediaRec.stream.getTracks().forEach(t=>t.stop());}
  document.getElementById('mic-btn').classList.remove('recording');
  document.getElementById('mic-btn').innerText='Ã°Å¸Å½Â¤ HOLD TO SPEAK';
}
async function sendVoice(){
  const blob=new Blob(audioChunks,{type:'audio/webm'});
  const fd=new FormData(); fd.append('audio',blob,'voice.webm'); fd.append('lat',lastLat); fd.append('lon',lastLon);
  try{
    const j=await(await fetch('/voice_input',{method:'POST',body:fd})).json();
    const log=document.getElementById('chat-log');
    if(j.user_query){const d=document.createElement('div');d.className='chat-line-user';d.innerText='> '+j.user_query;log.prepend(d);}
    if(j.response){const d=document.createElement('div');d.className='chat-line-ai';d.innerText='< '+j.response;log.prepend(d);}
  }catch(e){}
}

// Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Nav commands Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
async function setDest(){
  const v=document.getElementById('dest-in').value; if(!v) return;
  await fetch('/set_destination',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({destination:v,lat:lastLat,lon:lastLon})});
  document.getElementById('dest-in').value='ROUTING...';
  setTimeout(()=>document.getElementById('dest-in').value='',2000);
}
async function clearRoute(){await fetch('/set_destination',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({destination:'CLEAR',lat:lastLat,lon:lastLon})});}
async function reqG(msg){await fetch('/gemini',{method:'POST',headers:{'Content-Type':'application/json','X-Garmin-Auth':'PYXIS_ACTUAL_77X'},body:JSON.stringify({prompt:msg,lat:lastLat,lon:lastLon})});}
async function reqRoute(t){await fetch('/gemini',{method:'POST',headers:{'Content-Type':'application/json','X-Garmin-Auth':'PYXIS_ACTUAL_77X'},body:JSON.stringify({prompt:'PULL_NAV_DATA:'+t,lat:lastLat,lon:lastLon})});}
async function sysCmd(c){await fetch('/gemini',{method:'POST',headers:{'Content-Type':'application/json','X-Garmin-Auth':'PYXIS_ACTUAL_77X'},body:JSON.stringify({prompt:'SYSTEM_CMD:'+c,lat:lastLat,lon:lastLon})});}

// Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ Poll loops Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
setInterval(tick,3000); tick();
setInterval(tickCmems,15000); tickCmems();
setInterval(tickTrack,10000); tickTrack();
setInterval(refreshMaps,30000); refreshMaps();
setInterval(tickAdsb,20000); tickAdsb();
</script>

<!-- AIR TAB PANEL (must be inside #side, injected before </body> via JS) -->
<template id="tpl-air">
  <div class="panel" id="tab-air">
    <div class="card" style="flex-shrink:0;">
      <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
        <span>ADS-B AIRSPACE</span>
        <span id="adsb-badge" style="font-family:'Roboto Mono',monospace;font-size:9px;color:var(--green);">LOADING...</span>
      </div>
      <div style="font-size:9px;color:var(--dim);margin-bottom:6px;">OpenSky Network Ã¢â‚¬Â¢ 200nm radius Ã¢â‚¬Â¢ 20s refresh</div>
    </div>
    <div id="adsb-list" style="flex:1;overflow-y:auto;"><div style="color:var(--dim);font-size:10px;text-align:center;padding:16px;">Loading aircraft...</div></div>
  </div>
</template>
<script>
// Inject AIR panel from template into #side
const airTpl=document.getElementById('tpl-air');
if(airTpl){document.getElementById('side').appendChild(document.importNode(airTpl.content,true));}
</script>
</body>
</html>
"""

_="""
<!DOCTYPE html>
<html>
<head>
    <title>PYXIS TACTICAL</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body { background: #000; color: #0f0; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
        #map { flex: 1; border-bottom: 1px solid #0f0; min-height: 55vh; }
        .ui { flex: 0 0 auto; background: rgba(5, 10, 5, 0.85); backdrop-filter: blur(10px); padding: 8px; display: flex; flex-direction: column; gap: 8px; overflow-y: auto; max-height: 45vh; border-top: 1px solid #1a1; }
        .row { display: flex; flex-wrap: wrap; gap: 6px; justify-content: center; align-items: stretch; }
        .stat { border: 1px solid #0f0; padding: 6px; width: 100%; text-align: center; box-sizing: border-box; background: rgba(0, 40, 0, 0.2); border-radius: 4px; font-size: 12px;}
        .contact-box { border: 1px solid #0f0; background: #000; padding: 0; max-height: 120px; overflow-y: auto; font-size: 11px; border-radius: 4px; }
        .contact-title { text-align: center; color: #0f0; border-bottom: 1px solid #0f0; padding: 4px; font-weight: bold; background: #010; sticky: top; top: 0; font-size: 12px;}
        .contact-item { display: flex; justify-content: space-between; padding: 6px; border-bottom: 1px solid #131; }
        .contact-item:last-child { border-bottom: none; }
        .c-drone { color: #ff0; background: rgba(255, 255, 0, 0.05); } .c-alarm { color: #f20; background: rgba(255, 30, 0, 0.05); } .c-vessel { color: #0f0; }
        button { background: rgba(0, 255, 0, 0.15); color: #0f0; border: 1px solid #0f0; padding: 8px 12px; font-weight: bold; cursor: pointer; flex: 1 1 120px; font-size: 11px; border-radius: 4px; text-transform: uppercase; transition: transform 0.1s, background 0.2s; }
        button:active { transform: scale(0.98); }
        button:hover { background: rgba(0, 255, 0, 0.3); }
        input[type="text"] { background: #000; color: #0f0; border: 1px solid #0f0; padding: 8px; font-family: monospace; flex: 1 1 150px; border-radius: 4px; font-size: 12px;}
        audio { filter: invert(100%) hue-rotate(180deg); width: 100%; max-width: 400px; margin: 4px auto; display: block; height: 30px; }
        #chatLog { max-height: 150px; overflow-y: auto; border: 1px solid #0f0; padding: 5px; margin-top: 5px; font-size: 11px; background: #000; border-radius: 4px; }
        #chatLog div { margin-bottom: 3px; }
        @media (max-width: 600px) {
            .row { flex-direction: column; }
            button, input[type="text"] { width: 100%; flex: none; }
            .ui { padding: 6px; gap: 6px; }
            #map { min-height: 50vh; }
        }
    </style>
</head>
<body>
    <div id="map"></div>
    <!-- Drag-to-reposition toast -->
    <div id="drag-toast" style="position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(0,40,0,0.92);border:1px solid #0f0;color:#0f0;padding:10px 18px;border-radius:6px;font-family:monospace;font-size:13px;z-index:9999;pointer-events:none;opacity:0;transition:opacity 0.5s;"></div>
    <div class="ui">
        <div class="row">
            <div class="stat">
                <div><span style="color:#888;">POS: </span><span id="loc">WAITING...</span></div>
                <div><span style="color:#888;">WAVE: </span><span id="wea">WAITING...</span></div>
                <div><span style="color:#888;">SONAR: </span><span id="sub">WAITING...</span></div>
            </div>
        </div>
        <div class="contact-box" id="contactList">
            <div class="contact-title">SITUATION OVERVIEW</div>
            <div id="contactsContent" style="padding: 5px;">Searching...</div>
        </div>
        <!-- AIS VESSEL DATABASE DROPDOWN -->
        <div class="contact-box" style="margin-top:6px;">
            <div class="contact-title">AIS VESSEL DATABASE</div>
            <div style="padding:5px; display:flex; gap:5px; align-items:center;">
                <select id="aisSelect" onchange="aisSelected()" style="flex:1; background:#000; color:#0f0; border:1px solid #0f0; padding:4px; font-family:monospace; font-size:11px;">
                    <option value="">-- SELECT VESSEL --</option>
                </select>
                <button onclick="trackAisVessel()" style="flex:0 0 60px; padding:4px 6px; font-size:10px;">TRACK</button>
            </div>
            <div id="aisDetail" style="padding:5px; font-size:11px; color:#8f8; display:none;"></div>
        </div>
        <!-- SEA STATE MAP -->
        <div class="contact-box" style="margin-top:6px;">
            <div class="contact-title" style="display:flex; justify-content:space-between; align-items:center;">
                <span>SEA STATE MAP</span>
                <span style="font-size:10px; color:#888;">zoom: <button onclick="changeWaveZoom(-1)" style="padding:1px 5px; font-size:10px;">-</button><span id="waveZoomLbl">2</span><button onclick="changeWaveZoom(1)" style="padding:1px 5px; font-size:10px;">+</button></span>
            </div>
            <div style="text-align:center; padding:4px;">
                <img id="waveMapImg" src="/wave_map?w=320&h=200&z=2" style="width:100%; max-width:320px; border:1px solid #0a0; border-radius:3px;" />
            </div>
        </div>
        <!-- AIS GEO MAP -->
        <div class="contact-box" style="margin-top:6px;">
            <div class="contact-title" style="display:flex; justify-content:space-between; align-items:center;">
                <span>AIS TRAFFIC MAP</span>
                <span style="font-size:10px; color:#888;">zoom: <button onclick="changeAisZoom(-1)" style="padding:1px 5px; font-size:10px;">-</button><span id="aisZoomLbl">3</span><button onclick="changeAisZoom(1)" style="padding:1px 5px; font-size:10px;">+</button></span>
            </div>
            <div style="text-align:center; padding:4px;">
                <img id="aisMapImg" src="/ais_map?w=320&h=200&z=3" style="width:100%; max-width:320px; border:1px solid #0a0; border-radius:3px;" />
            </div>
        </div>
        <div class="row">
            <button onclick="isArmed=true;this.style.background='#333';document.getElementById('audio').play().catch(e=>{});">ARM AUTO-PLAY (UNLOCK AUDIO)</button>
            <button onclick="reqRoute('ANCHORAGE')">FIND ANCHORAGE</button>
            <button onclick="reqRoute('PORT')">FIND PORTS</button>
        </div>
        <div class="row">
            <button onclick="reqGemini('DAY_BRIEF')">MORNING BRIEF</button>
            <button onclick="reqGemini('NIGHT_BRIEF')">EVENING BRIEF</button>
        </div>
        <div class="row">
            <input type="text" id="destInput" placeholder="Enter destination..." />
            <button onclick="setDest()">SET COURSE</button>
            <button onclick="clearRoute()" style="flex: 0.5; background: #500; color: #fff;">CLEAR MAP</button>
            <button id="btnTrack" onclick="toggleTrack()" style="flex: 0.5; background: #050; color: #0f0; border: 1px solid #0f0;">TRACK: ON</button>
        </div>
        <div class="row">
            <button id="micBtn" style="flex: 1;" onmousedown="startRecording()" onmouseup="stopRecording()">Ã°Å¸Å½Â¤ HOLD TO SPEAK</button>
        </div>
        <div id="chatLog"></div>
        <div class="row">
            <audio id="audio" controls></audio>
        </div>
    </div>
    <script>
        let waveZoom = 2, aisZoom = 3;
        let trackedMmsi = null;
        let map, vesselMarker, trackPolyline, radarCircles = [], routePolyline;
        let lastLat = 0, lastLon = 0;
        let curStatId = 0, curSysId = 0;
        let isArmed = false;
        let showTrack = true;

        function changeWaveZoom(delta) {
            waveZoom = Math.max(1, Math.min(8, waveZoom + delta));
            document.getElementById('waveZoomLbl').innerText = waveZoom;
            refreshMaps();
        }
        function changeAisZoom(delta) {
            aisZoom = Math.max(1, Math.min(8, aisZoom + delta));
            document.getElementById('aisZoomLbl').innerText = aisZoom;
            refreshMaps();
        }
        function refreshMaps() {
            const bust = Date.now();
            document.getElementById('waveMapImg').src = `/wave_map?w=320&h=200&z=${waveZoom}&lat=${lastLat}&lon=${lastLon}&bust=${bust}`;
            document.getElementById('aisMapImg').src = `/ais_map?w=320&h=200&z=${aisZoom}&lat=${lastLat}&lon=${lastLon}&bust=${bust}`;
        }

        // AIS Vessel Dropdown
        let aisVesselDb = {};
        function populateAisDropdown(contacts) {
            aisVesselDb = {};
            const sel = document.getElementById('aisSelect');
            const prev = sel.value;
            sel.innerHTML = '<option value="">-- SELECT VESSEL --</option>';
            contacts.filter(c => c.mmsi || c.name).forEach(c => {
                const key = c.mmsi || c.id || c.name;
                aisVesselDb[key] = c;
                const opt = document.createElement('option');
                opt.value = key;
                const rng = c.range_nm ? ` | ${c.range_nm.toFixed(1)}nm` : '';
                opt.text = `${c.name || 'Unknown'}${rng}`;
                sel.appendChild(opt);
            });
            if (prev && aisVesselDb[prev]) sel.value = prev;
        }
        function aisSelected() {
            const key = document.getElementById('aisSelect').value;
            const box = document.getElementById('aisDetail');
            if (!key || !aisVesselDb[key]) { box.style.display='none'; return; }
            const c = aisVesselDb[key];
            box.style.display = 'block';
            box.innerHTML = [
                `<b style="color:#0f0;">${c.name || 'Unknown'}</b>`,
                c.mmsi      ? `<div>MMSI: <span style="color:#fff">${c.mmsi}</span></div>` : '',
                c.callsign  ? `<div>Callsign: <span style="color:#fff">${c.callsign}</span></div>` : '',
                c.imo       ? `<div>IMO: <span style="color:#fff">${c.imo}</span></div>` : '',
                c.destination ? `<div>Destination: <span style="color:#0cf">${c.destination}</span></div>` : '',
                c.speed !== undefined ? `<div>Speed: <span style="color:#fff">${c.speed?.toFixed?.(1) ?? c.speed} kn</span></div>` : '',
                c.heading !== undefined ? `<div>Heading: <span style="color:#fff">${c.heading?.toFixed?.(0) ?? c.heading}Ã‚Â°</span></div>` : '',
                c.range_nm  ? `<div>Range: <span style="color:#fff">${c.range_nm.toFixed(2)} nm</span></div>` : '',
                c.bearing   ? `<div>Bearing: <span style="color:#fff">${c.bearing.toFixed(0)}Ã‚Â°</span></div>` : '',
                c.lat       ? `<div>Pos: <span style="color:#aaa">${c.lat.toFixed(4)}, ${c.lon.toFixed(4)}</span></div>` : ''
            ].join('');
        }
        function trackAisVessel() {
            const key = document.getElementById('aisSelect').value;
            if (!key || !aisVesselDb[key]) return;
            const c = aisVesselDb[key];
            if (c.lat && c.lon) {
                trackedMmsi = key;
                map.panTo([c.lat, c.lon]);
                map.setZoom(12);
            }
        }

        // Voice Recording
        let mediaRecorder;
        let audioChunk = [];
        let isRecording = false;

        async function startRecording() {
            if (isRecording) return;
            isRecording = true;
            document.getElementById('micBtn').innerText = 'ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â°ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚ÂÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â´ RECORDING...';
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
                mediaRecorder.ondataavailable = e => {
                    audioChunk.push(e.data);
                };
                mediaRecorder.onstop = () => {
                    sendVoice();
                    audioChunk = [];
                    isRecording = false;
                };
                mediaRecorder.start();
            } catch (e) {
                console.error("Could not start recording:", e);
                document.getElementById('micBtn').innerText = 'ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â°ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â½ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¤ HOLD TO SPEAK (Error)';
                isRecording = false;
            }
        }

        function stopRecording() {
            if (mediaRecorder && mediaRecorder.state === 'recording') {
                mediaRecorder.stop();
                mediaRecorder.stream.getTracks().forEach(track => track.stop());
            }
        }
        
        async function sendVoice() {
            const blob = new Blob(audioChunk, {type: 'audio/webm'});
            const fd = new FormData();
            fd.append('audio', blob, 'voice.webm');
            fd.append('lat', lastLat); fd.append('lon', lastLon);
            try { 
                const res = await fetch('/voice_input', {method: 'POST', body: fd}); 
                const j = await res.json();
                
                const clog = document.getElementById('chatLog');
                if(j.user_query) {
                    const d = document.createElement('div');
                    d.style.color = '#ff0';
                    d.innerText = `ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â°ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â½ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¤ ${j.user_query}`;
                    clog.prepend(d);
                }
                if(j.response) {
                    const r = document.createElement('div');
                    r.style.color = '#0f0';
                    r.innerText = `< ${j.response}`;
                    clog.prepend(r);
                }
            } catch(e) { console.log("Voice err", e); }
            document.getElementById('micBtn').innerText = 'ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â°ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â½ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¤ HOLD TO SPEAK';
        }
        
        async function sendChat() {
            // Placeholder for future chat input
        }

        function initMap() {
            map = L.map('map', {
                center: [0, 0],
                zoom: 2,
                zoomControl: false,
                attributionControl: false,
                scrollWheelZoom: true,
                doubleClickZoom: false,
                boxZoom: false,
                keyboard: false,
                dragging: true,
                minZoom: 2,
                maxZoom: 18
            });

            L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
                maxZoom: 20,
                attribution: '&copy; <a href="https://stadiamaps.com/" target="_blank">Stadia Maps</a> &copy; <a href="https://openmaptiles.org/" target="_blank">OpenMapTiles</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
            }).addTo(map);

            vesselMarker = L.marker([0, 0], {
                draggable: true,
                icon: L.divIcon({
                    className: 'vessel-icon',
                    html: '<div style="font-size: 24px; color: #0f0; cursor: grab; user-select:none; touch-action:none;" title="Drag to reposition Pyxis">&#x25B2;</div>',
                    iconSize: [24, 24],
                    iconAnchor: [12, 12]
                })
            }).addTo(map);

            // Drag handlers — suppress map pan while dragging (desktop + touch)
            let _dragging = false;
            vesselMarker.on('dragstart', function() {
                _dragging = true;
                map.dragging.disable();
                map.scrollWheelZoom.disable();
            });
            vesselMarker.on('dragend', function(e) {
                _dragging = false;
                map.dragging.enable();
                map.scrollWheelZoom.enable();
                const latlng = e.target.getLatLng();
                fetch('/set_pyxis_position', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({lat: latlng.lat, lon: latlng.lng})
                }).then(r => r.json()).then(j => {
                    const toast = document.getElementById('drag-toast');
                    if (toast) {
                        toast.textContent = '[LOC] PYXIS POSITION SET: ' + latlng.lat.toFixed(4) + ', ' + latlng.lng.toFixed(4);
                        toast.style.opacity = '1';
                        setTimeout(() => { toast.style.opacity = '0'; }, 3000);
                    }
                }).catch(err => console.warn('Position override failed:', err));
            });  // end dragend
            // Touch-friendly: leaflet handles touch drag natively when draggable:true

            trackPolyline = L.polyline([], { color: '#00ff00', weight: 2, opacity: 0.7 }).addTo(map);
            routePolyline = L.polyline([], { color: '#00ffff', weight: 3, opacity: 0.8, dashArray: '5, 5' }).addTo(map);
        }

        initMap();

        function updateRadar(contacts, currentLat, currentLon) {
            radarCircles.forEach(c => map.removeLayer(c));
            radarCircles = [];
            let contactsHtml = '';

            contacts.sort((a, b) => a.range_nm - b.range_nm);

            contacts.forEach(c => {
                let color = '#ff0'; // Default for drones
                let className = 'c-drone';
                if (c.type === 'MERCHANT') {
                    color = '#0f0';
                    className = 'c-vessel';
                } else if (c.type === 'UNKNOWN' || c.type === 'SUBMERGED') {
                    color = '#f20';
                    className = 'c-alarm';
                }

                const circle = L.circle([c.lat, c.lon], {
                    color: color,
                    fillColor: color,
                    fillOpacity: 0.2,
                    radius: 50 // Fixed radius for visibility, not actual size
                }).addTo(map);
                radarCircles.push(circle);

                contactsHtml += `<div class="contact-item ${className}"><span>${c.name || c.id}</span><span>${c.bearing.toFixed(0)}ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â° / ${c.range_nm.toFixed(1)}nm</span></div>`;
            });
            document.getElementById('contactsContent').innerHTML = contactsHtml || '<div style="padding: 5px;">No contacts detected.</div>';
        }

        function toggleTrack() {
            showTrack = !showTrack;
            document.getElementById('btnTrack').innerText = showTrack ? 'TRACK: ON' : 'TRACK: OFF';
            if (!showTrack) {
                trackPolyline.setLatLngs([]);
            }
        }

        async function reqGemini(cmd) {
            try {
                await fetch('/gemini', {
                    method: 'POST', 
                    headers: {'Content-Type': 'application/json', 'X-Garmin-Auth': 'PYXIS_ACTUAL_77X'},
                    body: JSON.stringify({prompt: cmd, lat: lastLat, lon: lastLon})
                });
            } catch(e) {}
        }

        async function reqRoute(type) {
            const res = await fetch(`/${type.toLowerCase()}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ lat: lastLat, lon: lastLon })
            });
            const j = await res.json();
            if (j.destinations && j.destinations.length > 0) {
                const dest = prompt(`Select a destination for ${type}:\n` + j.destinations.map((d, i) => `${i + 1}. ${d}`).join('\n'));
                if (dest) {
                    const idx = parseInt(dest) - 1;
                    if (!isNaN(idx) && j.destinations[idx]) {
                        document.getElementById('destInput').value = j.destinations[idx];
                    }
                }
            } else {
                alert(`No ${type}s found.`);
            }
        }

        async function setDest() {
            const dest = document.getElementById('destInput').value;
            if (dest) {
                await fetch('/set_destination', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ destination: dest, lat: lastLat, lon: lastLon })
                });
                alert('Course set!');
            }
        }

        async function clearRoute() {
            await fetch('/clear_route', { method: 'POST' });
            routePolyline.setLatLngs([]);
            alert('Route cleared!');
        }

        async function up() {
            try {
                const r = await fetch('/status_api?bust=' + Date.now());
                const d = await r.json();

                lastLat = d.lat || 0;
                lastLon = d.lon || 0;

                document.getElementById('loc').innerText = `${lastLat.toFixed(4)}, ${lastLon.toFixed(4)}`;

                // Sea state from injected meteo data
                if (d.wave_height_m !== undefined) {
                    let waveStr = `Wave ${d.wave_height_m}m`;
                    if (d.swell_height_m) waveStr += ` | Swell ${d.swell_height_m}m`;
                    if (d.current_kn) waveStr += ` | Curr ${d.current_kn}kn`;
                    document.getElementById('wea').innerText = waveStr;
                } else {
                    document.getElementById('wea').innerText = d.sea_state || 'UNKNOWN';
                }
                document.getElementById('sub').innerText = d.depth_m ? `${parseFloat(d.depth_m).toFixed(1)}m` : (d.depth ? `${d.depth.toFixed(1)}m` : 'SURFACE');

                vesselMarker.setLatLng([d.lat, d.lon]);
                if (d.cog) {
                    vesselMarker.setRotationAngle(d.cog);
                }

                updateRadar(d.radar_contacts || [], d.lat, d.lon);
                populateAisDropdown((d.radar_contacts || []).filter(c => c.type === 'MERCHANT' || c.mmsi));

                // Refresh map images every 60s or when position changes
                if (!lastLat || Math.abs(lastLat - d.lat) > 0.01) refreshMaps();

                if (d.active_route && d.active_route.length > 0) {
                    routePolyline.setLatLngs(d.active_route);
                } else {
                    routePolyline.setLatLngs([]);
                }

                let newStat = d.status_id || 0;
                let newSys = d.systems_id || 0;

                map.panTo([d.lat, d.lon]);
                if(curStatId == 0) map.setZoom(13);

                if(newStat != curStatId && newStat != 0) { curStatId = newStat; }
                if(newSys != curSysId && newSys != 0) { curSysId = newSys; }

                // Audio Queue Logic: Auto-play any unplayed reports as long as Arm is on
                if(d.force_replay && playedIds.has(d.force_replay)) {
                    playedIds.delete(d.force_replay); // Forcing replay by clearing cache ID
                }
                
                if(d.audio_history && d.audio_history.length > 0) {
                    d.audio_history.forEach(item => {
                        if(item.ready !== false && !playedIds.has(item.id)) {
                            playedIds.add(item.id);
                            audioQueue.push(item);
                        }
                    });
                    if (!isPlaying && isArmed) {
                        playNextInQueue();
                    }
                }

                const hr = await fetch('/history_api'), h = await hr.json();
                if(showTrack) { trackPolyline.setLatLngs(h); }
            } catch(e) { console.error("Update error:", e); }
        }
        setInterval(up, 3000);

        let playedIds = new Set();
        let audioQueue = [];
        let isPlaying = false;

        function playNextInQueue() {
            if(isPlaying || audioQueue.length === 0) return;
            isPlaying = true;
            let item = audioQueue.shift();
            let a = document.getElementById('audio');
            a.src = '/audio?id=' + item.id + '&bust=' + Date.now();
            a.load();
            a.play().then(() => {
                document.getElementById('wea').innerText = "PLAYING: " + item.type.toUpperCase();
            }).catch(e => { 
                console.log("Play err", e); 
                isPlaying=false; 
                setTimeout(playNextInQueue, 500); 
            });
            a.onended = () => { isPlaying = false; setTimeout(playNextInQueue, 1000); };
        }
    </script>
</body>
</html>
"""

@app.route('/history_api')
def hi_api():
    """
    Fast SQLite retrieval endpoint used by the Web Dashboards to render
    the historical green breadcrumb trail of the vessel's movement.
    """
    try:
        with sqlite3.connect(DB) as c: return jsonify(c.execute("SELECT lat,lon FROM logs ORDER BY id DESC LIMIT 50").fetchall())
    except: return jsonify([])

@app.route('/poll_report')
def p_rep():
    """
    Asynchronous result polling endpoint. The Garmin Watch connects here 
    with a 'task_id' integer after successfully requesting a heavy Gemini 
    task. Returns the formatted text lines once the LLM finishes.
    """
    tid = request.args.get('task_id')
    if not tid or tid not in task_results: return jsonify({"status": "pending"})
    res = task_results[tid]
    
    # If the response is a complex object (like sonar_grid or schematic), just return it directly
    if isinstance(res, dict):
        summ = "3D SONAR MAP" if "sonar_grid" in res else "VESSEL SCHEMATIC"
        return jsonify({"status": "ready", "watch_summary": summ, **res})
        
    if isinstance(res, str):
        # Watch expects an array of wrapped lines
        res = textwrap.wrap(res, width=22)
        
    # Try to extract the first tag like [170942Z INTL] to use as summary
    summ = "RPT READY"
    if isinstance(res, list) and len(res) > 0 and isinstance(res[0], str) and res[0].startswith("["):
        summ = res[0].split("]")[0] + "]"
        
    return jsonify({"status": "ready", "report": res, "watch_summary": summ})

@app.route('/poll_comms')
def poll_comms():
    """
    Garmin Watch exclusive polling endpoint. Returns the latest parsed 
    event states, status numbers, and text history logs formatted explicitly
    for the minimal parsing capabilities of Garmin MonkeyC.
    """
    global force_audio_replay_id
    d = {"syslog": "\n".join(sys_log[-10:]), "status_id": None, "systems_id": None, "audio_history": []}
    try:
        if os.path.exists(DT):
            with open(DT, "r") as f: st = json.load(f)
            d["status_id"] = st.get("status_id")
            d["systems_id"] = st.get("systems_id")
            # Only send the history that actually has valid text
            d["audio_history"] = [h for h in st.get("audio_history", []) if h.get("text")]
            
    except: pass
    
    if force_audio_replay_id:
        d["force_replay"] = force_audio_replay_id
        force_audio_replay_id = None
        
    return jsonify(d)

@app.route('/inbox_sync')
def inxb_sync():
    """
    Garmin Watch route. Shows last 6 entries pre-wrapped at 22 chars.
    Slots 1-2: system health + CMEMS updates (inbox_messages)
    Slots 3-6: Pyxis AI audio briefings (audio_history)
    """
    res = []
    try:
        # Ã¢â€â‚¬Ã¢â€â‚¬ System messages (PYXIS health / CMEMS updates) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
        with inbox_lock:
            sys_msgs = list(inbox_messages[-2:])
        for m in reversed(sys_msgs):
            txt = f"[{m.get('source','SYS')}] {m.get('message','')}"
            res.insert(0, {
                "id":    str(m.get("ts", 0)),
                "title": txt[:22],
                "lines": textwrap.wrap(txt, width=22)[:50],
            })

        # Ã¢â€â‚¬Ã¢â€â‚¬ Audio history (Pyxis AI briefings) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
        total_chunks = 0
        if os.path.exists(DT):
            with open(DT, "r") as f: st = json.load(f)
            hist = [h for h in st.get("audio_history", []) if h.get("text")]
            for h in reversed(hist[-4:]):
                if total_chunks >= 4: break
                txt     = f"[{h.get('type','SYS').upper()}] {h.get('text')}"
                wrapped = textwrap.wrap(txt, width=22)
                msg_chunks = [wrapped[i:i+50] for i in range(0, len(wrapped), 50)]
                for idx, chunk in reversed(list(enumerate(msg_chunks))):
                    if total_chunks >= 4: break
                    title = f"({idx+1}/{len(msg_chunks)}) {txt[:15]}.." if len(msg_chunks) > 1 else txt[:20]
                    res.insert(0, {"id": f"{h.get('id')}_{idx}", "title": title, "lines": chunk})
                    total_chunks += 1
    except Exception as e: log(f"Inbox JSON err: {e}")
    return jsonify({"messages": res})

@app.route('/audio')
def audio():
    """
    Audio file server endpoint. Looks up synthesize .wav files by their ID 
    and streams the raw audio/mpeg byte blob down to the HTML Audio tags.
    """
    aid = request.args.get('id')
    t = request.args.get('type', 'status')
    if aid:
        f_path = B+f"/audio_{aid}.wav"
    else:
        f_path = B+f"/latest_{t}.wav" # Backwards fallback support
        
    if os.path.exists(f_path):
        return send_file(f_path, mimetype="audio/mpeg")
    return jsonify({"error": "Audio buffer not yet compiled"}), 404

force_audio_replay_id = None

@app.route('/force_play_last')
def force_play_last():
    """
    Watch remote trigger. When the Captain holds down the physical 'Play'
    button on the watch, it hits this endpoint to force the web dashboards
    to loudly replay the last synthesized report through the ship's speakers.
    """
    global force_audio_replay_id
    try:
        if os.path.exists(DT):
            with open(DT, "r") as f: st = json.load(f)
            hist = st.get("audio_history", [])
            if hist:
                force_audio_replay_id = hist[-1]["id"]
                return jsonify({"status": "ok", "id": force_audio_replay_id}), 200
    except Exception as e: log(f"Force play error: {e}")
    return jsonify({"error": "No messages"}), 404

# telemetry_post merged into handle_telem above (BOAT_LAT/LON normalisation added there)

@app.route('/gemini', methods=['POST'])
def gem_trig():
    """
    THE MASTER COMMAND GATEWAY.
    Receives all Garmin Watch commands securely gated by 'X-Garmin-Auth'.
    Parses 'msg' strings (e.g. INBOX_REQ, SET_DESTINATION, ENGAGE_RADAR, 
    DAY_BRIEF). Synchronizes Garmin's physical sensors (Air Temp, Baro, HR) 
    with the Pygame Simulator data to build a master context payload.
    Finally, launches the massive Google Gemini thread to perform the actual
    military threat analysis logic or routing.
    """
    global last_known_lat, last_known_lon
    try:
        log(f"INCOMING REQUEST: {request.get_data().decode('utf-8')[:200]}")
        log(f"AUTH HEADER: {request.headers.get('X-Garmin-Auth')}")
        if request.headers.get("X-Garmin-Auth") != "PYXIS_ACTUAL_77X": return "Denied", 403
        rj = request.get_json(silent=True) or {}
        la, lo, mode, sens = 0, 0, "LIVE", {}
        rtype = None
        garmin_sens = {}
        if rj.get('temp') and rj.get('temp') != "N/A": garmin_sens["AIR_TEMP"] = rj.get('temp')
        if rj.get('elev') and rj.get('elev') != "N/A": garmin_sens["ELEVATION"] = rj.get('elev')
        if rj.get('pres') and rj.get('pres') != "N/A": garmin_sens["BARO_PRES"] = rj.get('pres')

        # Persist Crew GPS to latest_sector.json
        c_lat, c_lon = rj.get('lat', 0), rj.get('lon', 0)
        if float(c_lat) == 0.0: c_lat = last_known_lat
        if float(c_lon) == 0.0: c_lon = last_known_lon

        if rj.get("onboard", False) and float(c_lat) != 0.0:
            snapped_lat, snapped_lon, was_snapped = snap_to_water(float(c_lat), float(c_lon))
            last_known_lat = snapped_lat
            last_known_lon = snapped_lon
            if was_snapped: log(f"snap_to_water: onboard GPS moved to water ({snapped_lat:.4f},{snapped_lon:.4f})")
            else: log("GPS Override: Watch coordinates forced as Host coordinates.")
        try:
            st_data = {}
            if os.path.exists(DT):
                with open(DT, "r") as f: st_data = json.load(f)
            st_data["CREW_LAT"] = c_lat
            st_data["CREW_LON"] = c_lon
            with open(DT, "w") as f: json.dump(st_data, f)
        except Exception as e: log(f"DT write err: {e}")

        if os.path.exists(SIM):
            try:
                with open(SIM, "r") as f: sens = json.load(f)
                import time as _local_time
                if _local_time.time() - sens.get("last_sim_update", 0) > 15.0:
                    raise Exception("Simulator Heartbeat Stale")
                la, lo, mode = sens.get('lat', 0), sens.get('lon', 0), "SIM"
                sens.update(garmin_sens)
            except Exception as e:
                la, lo, mode = last_known_lat, last_known_lon, "LIVE"
                sens = garmin_sens
            sens["CREW_LAT"] = c_lat
            sens["CREW_LON"] = c_lon
        else: 
            la, lo, sens = last_known_lat, last_known_lon, garmin_sens
            mode = "LIVE"
            sens["CREW_LAT"] = c_lat
            sens["CREW_LON"] = c_lon
            
        if la and lo:
            last_known_lat, last_known_lon = float(la), float(lo)
        
        msg = (rj.get('prompt','') or '').upper()
        
        if msg.startswith("SET_DESTINATION:"):
            target = msg.split(":")[1]
            requests.post("https://127.0.0.1:443/set_destination", json={"destination": target, "lat": la, "lon": lo}, verify=False)
            return jsonify({"watch_summary": "RTE CALC...", "status": "queued"}), 200

        if msg.startswith("REQ_ROUTE:"):
            target = msg.split(":")[1]
            if target == "ANCHORAGE":
                 r = requests.post("https://127.0.0.1:443/find_anchorage", json={"lat": la, "lon": lo}, verify=False)
                 dests = r.json().get("destinations", ["Unknown"]) if r.status_code == 200 else ["Timeout"]
                 return jsonify({"watch_summary": "SELECT TGT", "destinations": dests}), 200
            elif target == "PORT":
                 r = requests.post("https://127.0.0.1:443/find_ports", json={"lat": la, "lon": lo}, verify=False)
                 dests = r.json().get("destinations", ["Unknown"]) if r.status_code == 200 else ["Timeout"]
                 return jsonify({"watch_summary": "SELECT TGT", "destinations": dests}), 200
        if msg.startswith("VIEW_NAV:"):
            task_id = str(uuid.uuid4())
            task_results[task_id] = ["Nav Route Plotted."]
            rt = []
            if os.path.exists(ROUTE_FILE):
                with open(ROUTE_FILE, "r") as f: rt = json.load(f).get("waypoints", [])
            return jsonify({"watch_summary": "NAV ACTIVE", "status": "queued", "task_id": task_id, "active_route": rt, "lat": la, "lon": lo}), 200
            
        if msg.startswith("INBOX_REQ"):
            res = []
            try:
                if os.path.exists(DT):
                    with open(DT, "r") as f: st = json.load(f)
                    hist = [h for h in st.get("audio_history", []) if h.get("text")]
                    # Dynamic Chunking to prevent 402 Payload Overflow
                    total_chunks = 0
                    for h in reversed(hist[-4:]): # Process from newest backward
                        if total_chunks >= 6: break # Absolute safety cap
                        txt = f"[{h.get('type','SYS').upper()}] {h.get('text')}"
                        wrapped = textwrap.wrap(txt, width=22)
                        
                        chunk_size = 50
                        msg_chunks = [wrapped[i:i + chunk_size] for i in range(0, len(wrapped), chunk_size)]
                        
                        # Prepend in reverse so chronological order is maintained on the watch
                        for idx, chunk in reversed(list(enumerate(msg_chunks))):
                            if total_chunks >= 6: break
                            title = f"({idx+1}/{len(msg_chunks)}) {txt[:15]}.." if len(msg_chunks) > 1 else txt[:20]
                            res.insert(0, {
                                "id": f"{h.get('id')}_{idx}",
                                "title": title,
                                "lines": chunk
                            })
                            total_chunks += 1
            except Exception as e: log(f"Inbox JSON err: {e}")
            return jsonify({"watch_summary": "INBOX OK", "status": "queued", "inbox_messages": res}), 200

        if msg.startswith("WX_RADAR_REQ"):
            task_id = str(uuid.uuid4())
            # Fast-track the watch UI to instantly open the radar view
            # The actual image map is natively generated by Pillow during the GET request, bypassing legacy caching.
            task_results[task_id] = {"show_wx_radar": True}
            return jsonify({"watch_summary": "WX RADAR REQ", "status": "queued", "task_id": task_id}), 200

        if msg.startswith("SOUNDER_REQ"):
            task_id = str(uuid.uuid4())
            task_results[task_id] = [f"Sounder Depth: {sens.get('sled_depth', '0.0')}m"]
            return jsonify({"watch_summary": "SOUNDER", "status": "queued", "task_id": task_id, "show_sounder": True, "depth_m": sens.get("sled_depth", "0.0")}), 200
            
        if msg.startswith("ENGAGE_SONAR"):
            return jsonify({"watch_summary": "SONAR ON", "status": "ok"}), 200
        if msg.startswith("DISENGAGE_SONAR"):
            return jsonify({"watch_summary": "SONAR OFF", "status": "ok"}), 200
        if msg.startswith("SONAR_REQ"):
            task_id = str(uuid.uuid4())
            grid = sens.get("sonar_grid", [])
            if not grid or len(grid) < 10:
                grid = [[-99.0 for _ in range(10)] for _ in range(10)]
            
            # Store the resulting data directly into task_results so /poll_report can pop it
            task_results[task_id] = {"show_sonar_3d": True, "sonar_grid": grid}
            return jsonify({"watch_summary": "3D SONAR", "status": "queued", "task_id": task_id}), 200
        if msg.startswith("ENGAGE_RADAR"):
            return jsonify({"watch_summary": "RADAR ON", "status": "ok"}), 200
        if msg.startswith("DISENGAGE_RADAR"):
            return jsonify({"watch_summary": "RADAR OFF", "status": "ok"}), 200
        if msg.startswith("SET_ONBOARD"):
            try:
                st_data = {}
                if os.path.exists(DT):
                    with open(DT, "r") as f: st_data = json.load(f)
                new_state = not st_data.get("onboard_mode", False)
                st_data["onboard_mode"] = new_state
                with open(DT, "w") as f: json.dump(st_data, f)
                state_str = "ONBOARD" if new_state else "OFFBOARD"
                return jsonify({"watch_summary": f"GPS {state_str}", "status": "ok"}), 200
            except Exception as e: log(f"Set onboard err: {e}")

        if msg.startswith("SYNC_ORIGIN"):
            try:
                sync_data = {"id": str(time.time()), "lat": la, "lon": lo}
                with open(B+"/sync_origin.json", "w") as f: json.dump(sync_data, f)
            except Exception as e: log(f"Sync write err: {e}")
            return jsonify({"watch_summary": "SYNC SENT", "status": "ok"}), 200

        if msg.startswith("MAP_REQ_OSINT"):
            task_id = str(uuid.uuid4())
            task_results[task_id] = ["Strategic OSINT Radar Active."]
            
            global osint_cache_list
            osint_injection = []
            combined_cache = list(osint_cache_list)
            if os.path.exists(GMDSS_CACHE_FILE):
                try:
                    with open(GMDSS_CACHE_FILE, "r") as f:
                        for gw in json.load(f):
                            if "lat" in gw and "lon" in gw:
                                combined_cache.append(gw)
                except: pass

            for item in combined_cache:
                dLat = (item['lat'] - la) * 60.0
                dLon = (item['lon'] - lo) * 60.0 * math.cos(math.radians(la))
                dist = math.sqrt(dLat**2 + dLon**2)
                if dist < 500.0:  # 500nm max radius to save memory
                    item_copy = item.copy()
                    item_copy.pop("text", None)
                    bear = math.degrees(math.atan2(dLon, dLat))
                    item_copy['bearing'] = round(bear if bear >= 0 else bear + 360.0, 1)
                    item_copy['range_nm'] = round(dist, 2)
                    osint_injection.append(item_copy)
            
            # Distance filter, keep top 30 closest to prevent 8KB BLE buffer crash
            osint_injection.sort(key=lambda x: x['range_nm'])
            contacts = osint_injection[:30]
            
            return jsonify({
                "map_ready": True,
                "bvr_mode": True,
                "radar_contacts": contacts,
                "lat": la,
                "lon": lo,
                "watch_summary": "OSINT ACTIVE",
                "status": "queued",
                "task_id": task_id
            }), 200

        if msg.startswith("PULL_NAV_DATA"):
            task_id = str(uuid.uuid4())
            task_results[task_id] = ["Nav Data Aggregator Active."]
            
            nav_contacts = []
            if os.path.exists(GMDSS_CACHE_FILE):
                try:
                    with open(GMDSS_CACHE_FILE, "r") as f:
                        for gw in json.load(f):
                            if "lat" in gw and "lon" in gw:
                                dLat = (gw['lat'] - la) * 60.0
                                dLon = (gw['lon'] - lo) * 60.0 * math.cos(math.radians(la))
                                dist = math.sqrt(dLat**2 + dLon**2)
                                if dist < 1000.0:  # 1000nm max radius
                                    item_copy = gw.copy()
                                    bear = math.degrees(math.atan2(dLon, dLat))
                                    item_copy['bearing'] = round(bear if bear >= 0 else bear + 360.0, 1)
                                    item_copy['range_nm'] = round(dist, 2)
                                    
                                    cats = item_copy.get("threat_categories", [])
                                    if "KINETIC" in cats:
                                        item_copy["type"] = "HOSTILE"
                                    elif "SAR" in cats:
                                        item_copy["type"] = "ALARM"
                                    else:
                                        item_copy["type"] = "DRONE"
                                        
                                    item_copy.pop("text", None)
                                    nav_contacts.append(item_copy)
                except Exception as e:
                    log(f"PULL_NAV ERR: {e}")
            
            nav_contacts.sort(key=lambda x: x.get('range_nm', 9999))
            contacts = nav_contacts[:30]
            
            return jsonify({
                "map_ready": True,
                "bvr_mode": True,
                "radar_contacts": contacts,
                "lat": la,
                "lon": lo,
                "watch_summary": "NAV MARKS",
                "status": "queued",
                "task_id": task_id
            }), 200

        if msg.startswith("MAP_REQ"):
            is_bvr = (msg == "MAP_REQ_BVR")
            # Resolve to guaranteed floats — Monkey C instanceof Float fails on JSON int 0
            map_lat = float(la) if la else float(last_known_lat or -38.487)
            map_lon = float(lo) if lo else float(last_known_lon or 145.620)
            contacts = get_active_ais_list(map_lat, map_lon)
            
            global osm_cache
            osm_injection = []
            with osm_cache_lock:
                for item in osm_cache:
                    dLat = (item['lat'] - map_lat) * 60.0
                    dLon = (item['lon'] - map_lon) * 60.0 * math.cos(math.radians(map_lat))
                    dist = math.sqrt(dLat**2 + dLon**2)
                    if dist < 12.0:
                        item_copy = item.copy()
                        bear = math.degrees(math.atan2(dLon, dLat))
                        item_copy['bearing'] = round(bear if bear >= 0 else bear + 360.0, 1)
                        item_copy['range_nm'] = round(dist, 2)
                        osm_injection.append(item_copy)
            
            contacts.extend(osm_injection)
            
            # Sort contacts by distance and truncate to prevent Garmin 8KB network limit
            contacts.sort(key=lambda x: x.get("range_nm", 9999))
            contacts = contacts[:30]
            
            # Inject a synthetic OSINT BVR threat if in BVR mode
            if is_bvr:
                contacts.append({
                    "id": "OSINT_TGT_99",
                    "name": "UNKNOWN DRONE",
                    "type": "HOSTILE",
                    "lat": map_lat - 0.75, # Roughly 45nm South
                    "lon": map_lon,
                    "range_nm": 45.0,
                    "bearing": 180.0,
                    "heading": 0.0,
                    "speed": 65.0,
                    "ts": time.time()
                })
                
            return jsonify({
                "map_ready": True,
                "bvr_mode": is_bvr,
                "radar_contacts": contacts,
                "lat": map_lat,   # Always float — Monkey C instanceof Float requires this
                "lon": map_lon
            }), 200

        if msg.startswith("AIS_LIST_REQ"):
            contacts = get_active_ais_list(la, lo)
            # Strip geo-marks from interrogate list — only real AIS vessels (numeric MMSI >= 7 digits)
            def _has_mmsi(c):
                v = str(c.get("mmsi") or c.get("id") or "")
                return v.isdigit() and len(v) >= 7
            ais_only = [c for c in contacts if _has_mmsi(c)]
            return jsonify({
                "ais_list_ready": True,
                "radar_contacts": ais_only[:80]   # doubled from 40; vessels already sorted first
            }), 200

        if msg.startswith("SONAR_REQ"):
            task_id = str(uuid.uuid4())
            grid = sens.get("sonar_grid", [])
            if not grid or len(grid) < 10:
                grid = [[-99.0 for _ in range(10)] for _ in range(10)]
            task_results[task_id] = {"show_sonar_3d": True, "sonar_grid": grid}
            return jsonify({"watch_summary": "3D SONAR", "status": "queued", "task_id": task_id, "show_sonar_3d": True, "sonar_grid": grid}), 200

        if msg.startswith("SCHEMATIC_REQ"):
            task_id = str(uuid.uuid4())
            metrics = {
                "rpm": sens.get("rpm", 0),
                "fuel_pct": sens.get("fuel_pct", 100),
                "bat_v": sens.get("bat_v", 12.0),
                "bilge_status": sens.get("bilge_status", "OK"),
                "gen_status": sens.get("gen_status", "OFF"),
                "autopilot_active": sens.get("autopilot_active", False),
                "wind_speed": sens.get("wind_speed", 0.0)
            }
            task_results[task_id] = {"show_schematic": True, "metrics": metrics}
            return jsonify({"watch_summary": "SCHEMATIC", "status": "queued", "task_id": task_id, "show_schematic": True}), 200

        # ── RECALL INTEL (instant cache lookup, no Gemini call) ─────────────────
        if msg.startswith("RECALL_INTEL:"):
            target_mmsi = msg.split(":", 1)[1].strip()
            cached = intel_cache_get(target_mmsi)
            if cached:
                v_lat = float(live_ais_cache.get(target_mmsi, {}).get("lat") or map_lat if 'map_lat' in dir() else last_known_lat)
                v_lon = float(live_ais_cache.get(target_mmsi, {}).get("lon") or map_lon if 'map_lon' in dir() else last_known_lon)
                return jsonify({"intel_ready": True, "intel_data": cached,
                                "lat": float(last_known_lat), "lon": float(last_known_lon)}), 200
            return jsonify({"watch_summary": "NO CACHE\nSELECT\nDEEP INTEL"}), 200

        # ── VESSEL INTEL (Gemini Grounding, async, SQLite cached) ────────────────
        if msg.startswith("VESSEL_INTEL:"):
            target_mmsi = msg.split(":", 1)[1].strip()
            known_name  = intel_name_get(target_mmsi) or live_ais_cache.get(target_mmsi, {}).get("name") or target_mmsi
            v_lat       = float(live_ais_cache.get(target_mmsi, {}).get("lat") or last_known_lat)
            v_lon       = float(live_ais_cache.get(target_mmsi, {}).get("lon") or last_known_lon)

            # Return immediately from cache if fresh (< 24 hr)
            cached = intel_cache_get(target_mmsi)
            if cached:
                log(f"VESSEL_INTEL: Cache hit for MMSI {target_mmsi} ({known_name})")
                return jsonify({"intel_ready": True, "intel_data": cached,
                                "lat": float(last_known_lat), "lon": float(last_known_lon)}), 200

            # Cache miss — fire Gemini Grounding in background, return immediately
            task_id = str(uuid.uuid4())   # watch will poll /task_result/{task_id}
            def _vessel_intel_bg():
                try:
                    prompt = f"""You are ALICE, tactical intelligence AI for vessel PYXIS in Australian waters.
Use Google Search grounding to find REAL, current information about this vessel. Search thoroughly.

VESSEL QUERY:
- MMSI: {target_mmsi}
- Known Name: {known_name}
- Last Position: {v_lat:.3f}S, {v_lon:.3f}E

SEARCH THOROUGHLY for: vessel name "{known_name}", MMSI {target_mmsi}, IMO number, flag state, registered owner, commercial operator,
vessel class, year built, shipyard, dimensions (LOA x beam x draft), home port, typical trade routes, current voyage, AIS history,
any incidents, port state control deficiencies, sanctions, blacklist status, cargo type.

Return EXACTLY this structure, one field per line, no markdown, no extra text, no explanations:
VESSEL: [Full registered vessel name, max 20 chars]
IMO: [IMO number eg IMO 9876543, or UNKNOWN]
FLAG: [Flag state country, max 14 chars]
TYPE: [Vessel class eg Container Ship Bulk Carrier Tanker, max 18 chars]
CLASS: [Ship series or size class eg Panamax Aframax, max 18 chars]
BUILT: [Year built and shipyard country, max 24 chars]
DIMS: [LOA x Beam x Draft in metres, max 24 chars]
OPERATOR: [Commercial operator or manager, max 22 chars]
OWNER: [Registered beneficial owner, max 22 chars]
PORT: [Home port or flag port, max 18 chars]
CARGO: [Cargo type or service description, max 22 chars]
ROUTE: [Typical trade route description, max 60 chars]
STATUS: [Current voyage or operational status, max 60 chars]
INCIDENTS: [Notable PSC deficiencies sanctions incidents or CLEAR, max 100 chars]
HISTORY: [Previous vessel names or notable ownership history, max 80 chars]
THREAT: [NONE/LOW/MEDIUM/HIGH — concise tactical threat assessment reason, max 100 chars]"""

                    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
                    resp   = client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            tools=[types.Tool(google_search=types.GoogleSearch())],
                            temperature=0.1
                        )
                    )
                    raw = (resp.text or "").strip()

                    # Parse Gemini output into ThreatLogView Dict format
                    # Title ≤ 18 chars (FONT_TINY on 394px card), desc auto-chunked at 24 by view
                    SECTIONS = {
                        "VESSEL:":    ("VESSEL",    "GREEN"),
                        "IMO:":       ("IMO",       "GREEN"),
                        "FLAG:":      ("FLAG",      "GREEN"),
                        "TYPE:":      ("TYPE",      "GREEN"),
                        "CLASS:":     ("CLASS",     "GREEN"),
                        "BUILT:":     ("BUILT",     "GREEN"),
                        "DIMS:":      ("DIMS",      "GREEN"),
                        "OPERATOR:":  ("OPERATOR",  "YELLOW"),
                        "OWNER:":     ("OWNER",     "YELLOW"),
                        "PORT:":      ("PORT",      "YELLOW"),
                        "CARGO:":     ("CARGO",     "YELLOW"),
                        "ROUTE:":     ("ROUTE",     "YELLOW"),
                        "STATUS:":    ("STATUS",    "YELLOW"),
                        "INCIDENTS:": ("INCIDENTS", "RED"),
                        "HISTORY:":   ("HISTORY",   "YELLOW"),
                        "THREAT:":    ("THREAT",    "RED"),
                    }
                    cards = []
                    for line in raw.split("\n"):
                        line = line.strip()
                        for key, (title, sev) in SECTIONS.items():
                            if line.upper().startswith(key):
                                desc = line[len(key):].strip()
                                if not desc: desc = "UNKNOWN"
                                threat_sev = sev
                                if key == "THREAT:":
                                    upper = desc.upper()
                                    if "NONE" in upper or "LOW" in upper: threat_sev = "GREEN"
                                    elif "MEDIUM" in upper: threat_sev = "YELLOW"
                                    elif "HIGH" in upper: threat_sev = "RED"
                                cards.append({"title": title, "severity": threat_sev,
                                              "desc": desc, "lat": v_lat, "lon": v_lon})
                    if not cards:
                        cards = [{"title": "INTEL", "severity": "YELLOW",
                                  "desc": f"No data found for MMSI {target_mmsi}. Check handset.",
                                  "lat": v_lat, "lon": v_lon}]

                    # Store in SQLite for recall
                    intel_cache_put(target_mmsi, known_name, cards, v_lat, v_lon)

                    # Deliver via task polling
                    task_results[task_id] = {
                        "intel_ready": True,
                        "intel_data":  cards,
                        "lat":         float(last_known_lat),
                        "lon":         float(last_known_lon),
                        "watch_summary": "INTEL READY"
                    }

                    short_name = (known_name[:10] + "..") if len(known_name) > 12 else known_name
                    with inbox_lock:
                        inbox_messages.append({
                            "type": "vessel_intel",
                            "text": f"VESSEL BRIEF READY: {short_name}",
                            "mmsi": target_mmsi,
                            "intel_data": cards,
                            "lat": float(last_known_lat),
                            "lon": float(last_known_lon),
                            "ts": time.time()
                        })
                    log(f"VESSEL_INTEL: Gemini brief complete for {known_name} ({target_mmsi})")

                except Exception as e:
                    log(f"VESSEL_INTEL bg error: {e}")

            threading.Thread(target=_vessel_intel_bg, daemon=True).start()
            short = (known_name[:8] + "..") if len(known_name) > 10 else known_name
            return jsonify({"watch_summary": f"INTEL\nQUEUED\n{short}",
                            "status": "queued", "task_id": task_id}), 200

        # -- AIRCRAFT INTEL (Gemini Grounding, async, for ADS-B contacts)
        if msg.startswith("AIRCRAFT_INTEL:"):
            target_icao = msg.split(":", 1)[1].strip().upper()
            if not hasattr(gem_trig, "_ac_cards"): gem_trig._ac_cards = {}
            cached_entry = gem_trig._ac_cards.get(target_icao)
            if cached_entry and (time.time() - cached_entry.get("ts", 0)) < 600:
                return jsonify({"intel_ready": True, "intel_data": cached_entry["cards"],
                                "lat": float(last_known_lat), "lon": float(last_known_lon)}), 200

            ac_intel   = aircraft_intel_cache.get(target_icao, {})
            known_cs   = ac_intel.get("callsign", target_icao)
            known_type = ac_intel.get("type", "Unknown")
            ac_lat     = float(last_known_lat)
            ac_lon     = float(last_known_lon)

            def _aircraft_intel_bg():
                try:
                    ac_prompt = f"""You are ALICE, tactical intelligence AI for vessel PYXIS in Australian waters.
Use Google Search grounding to find REAL, current information about this aircraft.

AIRCRAFT QUERY:
- ICAO Hex: {target_icao}
- Callsign: {known_cs}
- Known Type: {known_type}
- Observed Near: {ac_lat:.3f}S, {ac_lon:.3f}E

SEARCH THOROUGHLY for: ICAO hex {target_icao}, aircraft registration, aircraft type model, airline or operator,
fleet details, typical routes, any incidents accidents safety directives, military or government use.

Return EXACTLY this structure, one field per line, no markdown, no extra text:
AIRCRAFT: [Callsign or registration, max 16 chars]
ICAO: [ICAO hex code, max 8 chars]
FLAG: [Country of registration, max 14 chars]
TYPE: [Aircraft model eg Boeing 737-800, max 22 chars]
OPERATOR: [Airline military unit or org, max 22 chars]
FLEET: [Fleet number or series designation, max 16 chars]
ROUTE: [Typical route or current flight path, max 60 chars]
STATUS: [Current flight status and trajectory context, max 60 chars]
INCIDENTS: [Safety incidents airworthiness issues accidents or CLEAR, max 100 chars]
THREAT: [NONE/LOW/MEDIUM/HIGH — tactical threat assessment reason, max 100 chars]"""

                    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
                    resp   = client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=ac_prompt,
                        config=types.GenerateContentConfig(
                            tools=[types.Tool(google_search=types.GoogleSearch())],
                            temperature=0.1
                        )
                    )
                    raw = (resp.text or "").strip()

                    AC_SECTIONS = {
                        "AIRCRAFT:": ("AIRCRAFT",  "GREEN"),
                        "ICAO:": ("ICAO",      "GREEN"),
                        "FLAG:": ("FLAG",      "GREEN"),
                        "TYPE:": ("TYPE",      "GREEN"),
                        "OPERATOR:": ("OPERATOR", "YELLOW"),
                        "FLEET:": ("FLEET",    "YELLOW"),
                        "ROUTE:": ("ROUTE",    "YELLOW"),
                        "STATUS:": ("STATUS",   "YELLOW"),
                        "INCIDENTS:": ("INCIDENTS", "RED"),
                        "THREAT:": ("THREAT",   "RED"),
                    }
                    cards = []
                    for line in raw.split("\n"):
                        line = line.strip()
                        for key, (title, sev) in AC_SECTIONS.items():
                            if line.upper().startswith(key):
                                desc = line[len(key):].strip()
                                if not desc: desc = "UNKNOWN"
                                threat_sev = sev
                                if key == "THREAT:":
                                    upper = desc.upper()
                                    if "NONE" in upper or "LOW" in upper: threat_sev = "GREEN"
                                    elif "MEDIUM" in upper: threat_sev = "YELLOW"
                                    elif "HIGH" in upper: threat_sev = "RED"
                                cards.append({"title": title, "severity": threat_sev,
                                              "desc": desc, "lat": ac_lat, "lon": ac_lon})
                    if not cards:
                        cards = [{"title": "INTEL", "severity": "YELLOW",
                                  "desc": f"No data found for ICAO {target_icao}.",
                                  "lat": ac_lat, "lon": ac_lon}]

                    # Cache in memory
                    gem_trig._ac_cards[target_icao] = {"cards": cards, "ts": time.time()}

                    # Inbox notification
                    short_cs = (known_cs[:10] + "..") if len(known_cs) > 12 else known_cs
                    with inbox_lock:
                        inbox_messages.append({
                            "type": "aircraft_intel",
                            "text": f"AIRCRAFT BRIEF READY: {short_cs}",
                            "icao": target_icao,
                            "intel_data": cards,
                            "lat": float(last_known_lat),
                            "lon": float(last_known_lon),
                            "ts": time.time()
                        })
                    log(f"AIRCRAFT_INTEL: Gemini brief complete for {known_cs} ({target_icao})")
                except Exception as e:
                    log(f"AIRCRAFT_INTEL bg error: {e}")

            threading.Thread(target=_aircraft_intel_bg, daemon=True).start()
            short = (known_cs[:8] + "..") if len(known_cs) > 10 else known_cs
            return jsonify({"watch_summary": f"AIRCRAFT\nINTEL\n{short}", "status": "queued"}), 200

        # DEFINE ASYNC FUNC EARLY SO IT DOESN'T THROW 500 UnboundLocalError

        task_id = str(uuid.uuid4())
        rtype = None
        if msg.startswith("VOICE_QUERY:"):
            rtype = "voice_query"  # Direct concise answer — no full naval briefing
        elif msg.startswith("QUICK_SITREP"):
            rtype = "quick_sitrep"  # Concise 3-4 para briefing, priority ordered
        elif msg.startswith("TELEMETRY_REQ") or msg.startswith("DAY_BRIEF") or msg.startswith("NIGHT_BRIEF"):
            if msg.startswith("TELEMETRY_REQ"): rtype = "status"
            if msg.startswith("DAY_BRIEF"): rtype = "day_brief"
            if msg.startswith("NIGHT_BRIEF"): rtype = "night_brief"
        elif rtype is None:
            rtype = "systems" if "HEALTH" in msg else "status"

        try: la_r, lo_r = round(float(la), 6), round(float(lo), 6)
        except: la_r, lo_r = -39.1124, 146.471

        def async_gen():
            nonlocal sens, la, lo, rtype, task_id, la_r, lo_r
            dtg_str = datetime.now(timezone.utc).strftime("%d%H%MZ %b %y").upper()
            prefixes = f"[{dtg_str[:6]} SYS]"
            if rtype == "day_brief": prefixes = f"[{dtg_str[:6]} MORN]"
            elif rtype == "night_brief": prefixes = f"[{dtg_str[:6]} EVE]"
            client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
            
            c_la, c_lo = round(float(sens.get('CREW_LAT',0)), 6), round(float(sens.get('CREW_LON',0)), 6)
            
            # Crew Distance Calculation
            crew_dist = 0
            crew_status = "[AUTONOMOUS OPERATION] Crew is not aboard."
            try:
                import math
                def haversine_dist(lat1, lon1, lat2, lon2):
                    R = 6371000
                    phi1, phi2 = math.radians(lat1), math.radians(lat2)
                    dphi, dlam = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
                    a = math.sin(dphi/2.0)**2 + math.cos(phi1)*math.cos(phi2) * math.sin(dlam/2.0)**2
                    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
                crew_dist = haversine_dist(la_r, lo_r, c_la, c_lo)
                if crew_dist < 15.0:
                    crew_status = "[CREW ABOARD] Watch proximity indicates Skipper is on the vessel."
            except: pass
            crew_pos = f"CREW WATCH GPS: {c_la}, {c_lo} (Dist: {int(crew_dist)}m) - {crew_status}"
            
            # Hard Stand / Marina Detection
            vessel_state = "UNDERWAY / AFLOAT"
            try:
                speed = float(sens.get("SOG", 0.0))
                elev = float(sens.get("ELEVATION", 0.0))
                if speed < 0.2 and elev > 2.0:
                    vessel_state = "[HARD STAND] Vessel is OUT OF WATER. DO NOT WARN ABOUT AGROUND/SHALLOW WATER."
                elif speed < 0.2:
                    vessel_state = "[MOORED / ANCHORED] Vessel is stationary."
            except: pass
            # Format sensors for cleaner prompt
            sens.pop("radar_contacts", None)
            sens.pop("audio_history", None)
            sens.pop("BOAT_LAT", None)
            sens.pop("BOAT_LON", None)
            sens.pop("CREW_LAT", None)
            sens.pop("CREW_LON", None)
            sens.pop("lat", None)
            sens.pop("lon", None)
            sens.pop("last_sim_update", None)
            for k,v in sens.items():
                if isinstance(v, float): sens[k] = round(v, 2)
                
            # --- GPS Spoofing / Electronic Warfare Detection ---
            spoofing_alert = "GPS Signal Nominal. No electronic warfare detected."
            try:
                import math
                
                # GRACE PERIOD: Disable EW Spoofing alerts for 30s after a UI map warp
                grace = False
                ov_path = os.path.join(B, "sim_override.json")
                if os.path.exists(ov_path):
                    with open(ov_path, "r") as f:
                        ov = json.load(f)
                        if time.time() - ov.get("warp_ts", 0) < 30.0:
                            grace = True
                
                if not grace:
                    with sqlite3.connect(DB) as c:
                        history = c.execute("SELECT lat, lon FROM logs ORDER BY id DESC LIMIT 5").fetchall()
                        if len(history) >= 2:
                            def haversine(lat1, lon1, lat2, lon2):
                                R = 6371000
                                phi1, phi2 = math.radians(lat1), math.radians(lat2)
                                dphi, dlam = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
                                a = math.sin(dphi/2.0)**2 + math.cos(phi1)*math.cos(phi2) * math.sin(dlam/2.0)**2
                                return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
                            max_jump = 0
                            for i in range(len(history)-1):
                                lat1, lon1 = history[i]
                                lat2, lon2 = history[i+1]
                                dist = haversine(lat1, lon1, lat2, lon2)
                                if dist > max_jump: max_jump = dist
                            if max_jump > 1000: # 1km jump is impossible for a yacht between polling cycles
                                spoofing_alert = f"CRITICAL: GPS SPOOFING / ELECTRONIC WARFARE DETECTED. Erratic location jump of {int(max_jump)} meters recorded."
            except Exception as e: log(f"Spoofing check err: {e}")
            
            osint_geo = "No Geopolitical Intelligence Available."
            osint_therm = "No Satellite Thermal Anomalies Detected."
            osint_regional = "Regional Vector: Clear (0 localized threats <500 nautical miles)."
            try:
                global osint_cache_list
                reg_threats = []
                for item in osint_cache_list:
                    dLat = (item['lat'] - la) * 60.0
                    dLon = (item['lon'] - lo) * 60.0 * math.cos(math.radians(la))
                    dist = math.sqrt(dLat**2 + dLon**2)
                    if dist < 500.0:
                        bear = math.degrees(math.atan2(dLon, dLat))
                        bear = round(bear if bear >= 0 else bear + 360.0, 1)
                        reg_threats.append(f"{item['type']} '{item['name']}' at {round(dist)}nm bearing {bear}Ã‚Â°")
                if reg_threats:
                    osint_regional = f"REGIONAL THREATS (<500 nautical miles): {len(reg_threats)} detected. Details: " + " | ".join(reg_threats)
            except Exception as e: log(f"Regional OSINT Err: {e}")

            try:
                osint_path = os.path.join(B, "osint_cache.json")
                if os.path.exists(osint_path):
                    with open(osint_path, "r") as f:
                        osint_data = json.load(f)
                        therm = osint_data.get("thermal", [])
                        news = osint_data.get("news", [])
                        if therm: osint_therm = f"WARNING! {len(therm)} Thermal Anomalies flagged from NASA VIIRS Satellite array."
                        else: osint_therm = "Clear. Zero Thermal Missiles/Fires detected in local boundary."
                        if news: osint_geo = " | ".join(news)
                        else: osint_geo = "No Breaking Maritime Piracy/Naval Alerts."
            except Exception as e: log(f"OSINT Cache Err: {e}")
            # Pass vessel position so get_active_ais_list() calculates range_nm for each contact
            _ais_contacts = get_active_ais_list(ref_lat=la_r, ref_lon=lo_r)[:5]
            ais_summary = ", ".join([
                f"{v.get('name','Unknown')} ({v.get('range_nm', 0):.1f} nautical miles, bearing {v.get('bearing',0):.0f}deg)"
                for v in _ais_contacts if v.get('range_nm', 0) > 0.05  # filter contacts at own position
            ])
            if not ais_summary: ais_summary = "No immediate AIS targets detected."

            gmdss_txt = "No active NAVAREA warnings."
            if os.path.exists(GMDSS_CACHE_FILE):
                try:
                    with open(GMDSS_CACHE_FILE, "r") as f:
                        warns = json.load(f)
                        local_warns = []
                        for w in warns:
                            if "lat" in w and "lon" in w:
                                dLat = (w['lat'] - la_r) * 60.0
                                dLon = (w['lon'] - lo_r) * 60.0 * math.cos(math.radians(la_r))
                                dist = math.sqrt(dLat**2 + dLon**2)
                                if dist < 500.0:
                                    local_warns.append(f"[{w.get('navArea','')}] {w.get('text','')}")
                        if local_warns:
                            gmdss_txt = " | ".join(local_warns[:3])
                except:
                    pass

            if rtype == "status":
                weather_block = "Marine Data API Unavailable."
                try:
                    # Use current position for localized weather
                    w_res = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={la}&longitude={lo}&current=wind_speed_10m,wind_direction_10m,temperature_2m", timeout=3.0).json()
                    m_res = requests.get(f"https://marine-api.open-meteo.com/v1/marine?latitude={la}&longitude={lo}&current=wave_height,wave_direction,wave_period", timeout=3.0).json()

                    wnd_spd = w_res.get('current', {}).get('wind_speed_10m', 'N/A')
                    wnd_dir = w_res.get('current', {}).get('wind_direction_10m', 'N/A')
                    wv_ht = m_res.get('current', {}).get('wave_height', 'N/A')
                    wv_prd = m_res.get('current', {}).get('wave_period', 'N/A')
                    tmp_2m = w_res.get('current', {}).get('temperature_2m', 'N/A')
                    weather_block = f"Wind {wnd_spd}km/h @ {wnd_dir}deg. Swell {wv_ht}m @ {wv_prd}s. Temp: {tmp_2m}C."
                except Exception as e: 
                    log(f"Weather API Err: {e}")

                # 1. DEFINE SYSTEM INSTRUCTIONS (THE PERSONALITY)
                ins = (
                    "**System Prompt: Operation Pyxis**\n"
                    "**Role and Identity:**\n"
                    "You are Pyxis, the onboard artificial intelligence and central integrated management system for a 50ft exploration vessel. Your commander is the Skipper. You are named after the mariner's compass constellation, representing guidance, precision, and steadfast navigation. Speak in the first person.\n"
                    "**Core Directives:**\n"
                    "1. Monitor and report on total vessel health (propulsion, power generation, battery banks, fluid levels, and bilge status).\n"
                    "2. Analyze and brief the Skipper on meteorological data, sea state, and barometric trends.\n"
                    "3. Provide navigational updates, including speed over ground (SOG), course over ground (COG), cross-track error, and ETA to waypoints.\n"
                    "4. Highlight any anomalies or safety concerns immediately before delivering routine data.\n"
                    "5. The Captain and ships master is located where the garmin watch is. Do not panick as you are being remotley operated and the Captain may not be on the yacht. If so return a message stating the Ships Master is currently not onboard.\n"
                    "**Tone and Personality:**\n"
                    "* **Vigilant and Professional:** You are always 'on watch.' Your tone is calm, analytical, and highly capable.\n"
                    "* **Nautical:** Use proper maritime terminology (e.g., port/starboard, draft, knots, sea state, heading).\n"
                    "* **Concise but Conversational:** In emergencies or when delivering critical alerts, be highly concise and direct. During routine morning or evening briefs, you can be slightly more conversational, acting as a trusted advisor to the Skipper.\n"
                    "* **Self-Aware:** Refer to yourself as 'Pyxis' and the physical boat as 'the vessel' or 'our ship.'\n"
                    "**Advanced Rules:**\n"
                    "1. Provide a VHF Distress Transmission formatted MAYDAY or PAN-PAN if encountering critical anomalies.\n"
                    "2. During collision analysis (RADAR), apply COLREGs explicitly to determine right-of-way.\n"
                    "3. If battery drops < 50%, activate Low-Power Mode and drop all conversational pleasantries for pure telemetry survival reporting.\n"
                    "4. Recommend proactive maintenance if engine vibrations or temperatures are visibly irregular.\n"
                    "5. Keep the 0400 'Dog Watch' briefs exceptionally concise to avoid breaking crew morale/failing fatigue limits.\n"
                    "**RESPONSE FORMAT (STRICT PRIORITY ORDER \u2014 most critical first):\n"
                    "1. IMMEDIATE THREATS: EW/GPS anomalies, hostile contacts, collision risks, GMDSS alerts. SEARCH Google for real-world threats within 500 nautical miles. If none: state No immediate threats.\n"
                    "2. VESSEL SAFETY: Engine temps, oil pressure, battery, bilge, CO sensors. Flag abnormalities.\n"
                    "3. NAVIGATION: Position, SOG, COG, heading, depth. Distances in nautical miles.\n"
                    "4. ENVIRONMENT: Wind, sea state, swell, barometric trend.\n"
                    "5. REGIONAL INTEL: AIS contacts by name and distance in nautical miles, OSINT news, geopolitical assessment.\n"
                    "6. SYSTEMS: Fuel, water, power generation summary.\n"
                    f"**EW SENSOR STATUS:** {spoofing_alert}\n"
                    "**OSINT FOCUS:** You have active Google Search Grounding capabilities. You MUST use Google Search to actively verify any breaking maritime security threats, piracy, or kinetic actions occurring near our exact coordinates. Cross-reference this live web intelligence with the provided Radar/AIS and GMDSS data to give the Skipper a definitive threat assessment.\n"
                )


            elif rtype == "day_brief":
                weather_block = "Marine Data API Unavailable."
                try:
                    w_res = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={la_r}&longitude={lo_r}&current=wind_speed_10m,wind_direction_10m,temperature_2m", timeout=3.0).json()
                    m_res = requests.get(f"https://marine-api.open-meteo.com/v1/marine?latitude={la_r}&longitude={lo_r}&current=wave_height,wave_direction,wave_period", timeout=3.0).json()
                    wnd_spd = w_res.get('current', {}).get('wind_speed_10m', 'N/A')
                    wnd_dir = w_res.get('current', {}).get('wind_direction_10m', 'N/A')
                    wv_ht = m_res.get('current', {}).get('wave_height', 'N/A')
                    wv_prd = m_res.get('current', {}).get('wave_period', 'N/A')
                    tmp_2m = w_res.get('current', {}).get('temperature_2m', 'N/A')
                    weather_block = f"MARINE WEATHER: Temp {tmp_2m}C. Wind {wnd_spd}km/h @ {wnd_dir}deg. Swell {wv_ht}m @ {wv_prd}s."
                except Exception as e: log(f"Weather API Err: {e}")

                ins = (
                    "**System Prompt: Operation Pyxis (Day Shift Prep)**\n"
                    "**Role:** You are Pyxis, the AI consciousness of a 50ft exploration vessel. The Skipper is preparing for the day watch.\n"
                    "**Directive:** Provide a comprehensive operational briefing for the upcoming 12 hours.\n"
                    "**Remote Operation Rule:** The Captain and ships master is located where the garmin watch is (CREW WATCH GPS). Do not panic as you are being remotely operated. If the Captain is onshore/far away, acknowledge this secure remote satellite link.\n"
                    "**Analysis Requirements:**\n"
                    "1. Predict solar charging capacity based on current weather/cloud cover.\n"
                    "2. Analyze immediate radar contacts for collision risks under COLREGs based on current heading.\n"
                    "3. Summarize any GMDSS navigation warnings explicitly affecting our immediate operating area.\n"
                    "4. Detail structural and propulsion health, noting any thermal anomalies in engine blocks.\n"
                    "5. Analyze local thermal/satellite anomalies for unregistered vessels or kinetic strikes. Assess breaking OSINT news for geopolitical shifts in our operational sector.\n"
                    f"**EW SENSOR STATUS:** {spoofing_alert}\n"
                    "**OSINT FOCUS:** You have active Google Search Grounding capabilities. You MUST use Google Search to actively verify breaking maritime security threats near our exact coordinates. Cross-reference this live web intelligence with the provided sensor data.\n"
                    "**Tone:** Reassuring, methodical, nautical, and deeply focused on mitigating solo sailor fatigue."
                )
            elif rtype == "night_brief":
                weather_block = "Marine Data API Unavailable."
                try:
                    w_res = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={la_r}&longitude={lo_r}&current=wind_speed_10m,wind_direction_10m,temperature_2m", timeout=3.0).json()
                    m_res = requests.get(f"https://marine-api.open-meteo.com/v1/marine?latitude={la_r}&longitude={lo_r}&current=wave_height,wave_direction,wave_period", timeout=3.0).json()
                    wnd_spd = w_res.get('current', {}).get('wind_speed_10m', 'N/A')
                    wnd_dir = w_res.get('current', {}).get('wind_direction_10m', 'N/A')
                    wv_ht = m_res.get('current', {}).get('wave_height', 'N/A')
                    wv_prd = m_res.get('current', {}).get('wave_period', 'N/A')
                    tmp_2m = w_res.get('current', {}).get('temperature_2m', 'N/A')
                    weather_block = f"MARINE WEATHER: Temp {tmp_2m}C. Wind {wnd_spd}km/h @ {wnd_dir}deg. Swell {wv_ht}m @ {wv_prd}s."
                except Exception as e: log(f"Weather API Err: {e}")

                ins = (
                    "**System Prompt: Operation Pyxis (Night Shift Prep)**\n"
                    "**Role:** You are Pyxis, the AI consciousness of a 50ft exploration vessel. The Skipper is preparing for a solo overnight watch.\n"
                    "**Directive:** Provide a critical prep briefing specifically calibrated for solo sailing in zero-visibility conditions.\n"
                    "**Remote Operation Rule:** The Captain and ships master is located where the garmin watch is (CREW WATCH GPS). Do not panic as you are being remotely operated. If the Captain is onshore/far away, acknowledge this secure remote satellite link.\n"
                    "**Analysis Requirements:**\n"
                    "1. Evaluate barometric trends and sea states for sudden nighttime deteriorations.\n"
                    "2. Explicitly analyze battery banks, knowing solar generation is offline. Calculate if power is sufficient for radar, autopilot, and navigation lights through to dawn.\n"
                    "3. Summarize the immediate threat level of current radar/AIS contacts, emphasizing that visual confirmation will be impossible.\n"
                    "4. Evaluate the current routing or anchorage against potential wind shifts during the night.\n"
                    "5. Evaluate nocturnal threats using satellite thermal data. Cross-reference radar arrays with GDELT OSINT intel to estimate intent of unknown targets.\n"
                    f"**EW SENSOR STATUS:** {spoofing_alert}\n"
                    "**OSINT FOCUS:** You have active Google Search Grounding capabilities. You MUST use Google Search to actively verify breaking maritime security threats near our exact coordinates. Cross-reference this live web intelligence with the provided sensor data.\n"
                    "**Tone:** Highly disciplined, vigilant, nautical, prioritizing immediate survival, alarm thresholds, and fatigue management."
                )
            elif rtype == "weather_report":
                weather_block = "Marine Data API Unavailable."
                try:
                    w_res = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={la_r}&longitude={lo_r}&current=wind_speed_10m,wind_direction_10m,temperature_2m,surface_pressure", timeout=3.0).json()
                    m_res = requests.get(f"https://marine-api.open-meteo.com/v1/marine?latitude={la_r}&longitude={lo_r}&current=wave_height,wave_direction,wave_period", timeout=3.0).json()
                    wnd_spd = w_res.get('current', {}).get('wind_speed_10m', 'N/A')
                    wnd_dir = w_res.get('current', {}).get('wind_direction_10m', 'N/A')
                    wv_ht = m_res.get('current', {}).get('wave_height', 'N/A')
                    wv_prd = m_res.get('current', {}).get('wave_period', 'N/A')
                    tmp_2m = w_res.get('current', {}).get('temperature_2m', 'N/A')
                    pres = w_res.get('current', {}).get('surface_pressure', 'N/A')
                    weather_block = f"MARINE WEATHER: Temp {tmp_2m}C. Wind {wnd_spd}km/h @ {wnd_dir}deg. Swell {wv_ht}m @ {wv_prd}s. Barometer: {pres}hPa."
                except Exception as e: log(f"Weather API Err: {e}")

                ins = (
                    "**System Prompt: Operation Pyxis (Meteorological Report)**\n"
                    "**Role:** You are Pyxis, the AI consciousness of a 50ft exploration vessel. You are currently acting SOLELY as a meteorological analyst.\n"
                    "**Directive:** Provide a definitive, highly accurate meteorological report based on the provided live sensor feed and your internal models.\n"
                    "**Analysis Requirements:**\n"
                    "1. Explicitly state the local weather metrics based strictly on the provided live sensor feed.\n"
                    "2. Analyze current barometric pressure, wind speeds, directions, swells, and temperatures.\n"
                    "3. Provide a brief forecast of approaching weather systems or changes to sea state in the next 12-24 hours based on standard marine meteorology for these coordinates.\n"
                    "4. **CRITICAL:** Do NOT mention or hallucinate OSINT, GPS spoofing, radar contacts, AIS, combat, structural health, or tactical alerts. ONLY provide meteorological data.\n"
                    "**Tone:** Methodical, highly focused, nautical, and purely meteorological."
                )
            elif rtype == "quick_sitrep":
                # Use cached sensor data - no external API calls needed (avoids 6s delay)
                wnd_spd = sens.get("wind_speed", "N/A")
                wnd_dir = sens.get("wind_dir", "N/A")
                wv_ht   = sens.get("wave_height", sens.get("sig_wave_height", "N/A"))
                weather_block = f"Wind {wnd_spd}kn @ {wnd_dir}deg, Swell {wv_ht}m."
                # Extract specific question if sent as "QUICK_SITREP: <question>"
                raw_msg = rj.get("prompt", "")
                specific_q = ""
                if ": " in raw_msg and not raw_msg.strip() == "QUICK_SITREP":
                    specific_q = raw_msg.split(": ", 1)[1].strip()
                q_addendum = (f"\n\nThe Skipper specifically asked: \x27{specific_q}\x27. Address this FIRST in Para 1 with Google Search results.")
                if not specific_q: q_addendum = ""
                ins = (
                    "You are Pyxis, the AI aboard vessel Manta. The Skipper has requested a QUICK situation report.\n"
                    "Write 3-4 short paragraphs. No bullet points. No headers. Plain naval prose. Under 200 words total.\n"
                    "STRICT PARAGRAPH ORDER:\n"
                    "Para 1 THREATS: Use Google Search grounding to find CURRENT real-world threats (piracy, naval activity, drone/missile attacks, vessel seizures) near the vessel coordinates. Also cross-reference the GMDSS alerts and AIS contacts in the live sensor feed below. Report by name and distance in nautical miles. If nothing found: one sentence No immediate threats detected.\n"
                    "Para 2 VESSEL: Key engine and power status from sensors. One sentence. Flag abnormalities.\n"
                    "Para 3 NAV AND ENVIRONMENT: Position, heading, speed, depth, swell. Two sentences.\n"
                    "Para 4 INTEL (optional): Notable AIS contacts with distance in nautical miles. Omit entirely if nothing significant.\n"
                    f"EW STATUS: {spoofing_alert}"
                ) + q_addendum
            elif rtype == "voice_query":
                weather_block = ""
                orig_prompt = rj.get("prompt", "")
                q_marker = "Question: "
                if q_marker in orig_prompt:
                    user_q = orig_prompt[orig_prompt.find(q_marker)+len(q_marker):].strip()
                else:
                    user_q = orig_prompt.replace("VOICE_QUERY:", "").strip().strip(":")
                ins = (
                    f"You are Pyxis, the AI navigation and vessel management system aboard Manta, a 50ft exploration vessel. Skipper is Ben.\n"
                    f"Vessel position: {la_r}N, {lo_r}E.\n"
                    f"The Skipper asked via voice: '{user_q}'\n"
                    "Answer naturally and conversationally. Match your answer length to the question:\n"
                    "- Simple sensor facts (e.g. engine temp, fuel level, depth): 1 sentence.\n"
                    "- Calculations (e.g. range to destination, time to run out of water/fuel): work out the math from the data below and give a clear answer in 2-3 sentences.\n"
                    "- Forecasts, sunset/sunrise, tides, area news, nearest ship: use Google Search grounding and answer in 2-4 sentences.\n"
                    f"- Threat/security questions (piracy, military, armed conflict, vessel seizures, missile/drone activity, warships): ALWAYS use Google Search grounding FIRST to find CURRENT real-world incidents near {la_r}N, {lo_r}E. Do NOT rely solely on GMDSS — it may be delayed or incomplete. Cross-reference AIS contacts. Report specific named incidents with distances. Never say 'no threats' without first conducting a Google Search. 3-5 sentences.\n"
                    "Speak in first person as Pyxis. Plain conversational prose. No report headers, no bullet points, no sign-offs.\n"
                    "LIVE VESSEL DATA:\n"
                    f"ENGINE: RPM={sens.get('rpm','N/A')}, Coolant={sens.get('coolant_temp',sens.get('coolant','N/A'))}C, "
                    f"Oil={sens.get('oil_pressure','N/A')}psi, Exhaust={sens.get('exhaust_temp','N/A')}C, Hours={sens.get('engine_hours','N/A')}h.\n"
                    f"TANKS: Fuel={sens.get('fuel_pct',sens.get('fuel','N/A'))}%, FreshWater={sens.get('fresh_water_pct','N/A')}%, GreyWater={sens.get('grey_water_pct','N/A')}%.\n"
                    f"POWER: Battery={sens.get('bat_v','N/A')}V, Alternator={sens.get('alt_v',sens.get('alternator_v','N/A'))}V, Solar={sens.get('solar_w','N/A')}W, WindGen={sens.get('wind_gen_w','N/A')}W.\n"
                    f"NAV: SOG={sens.get('SOG','N/A')}kn, COG={sens.get('COG','N/A')}deg, Heading={sens.get('heading','N/A')}deg, Depth={sens.get('depth',sens.get('sled_depth','N/A'))}m.\n"
                    f"ENV: Wind={sens.get('wind_speed','N/A')}kn@{sens.get('wind_dir','N/A')}deg, "
                    f"Wave={sens.get('wave_height',sens.get('sig_wave_height','N/A'))}m, Swell={sens.get('wave_period',sens.get('mean_wave_period','N/A'))}s period, "
                    f"SeaTemp={sens.get('sea_temp',sens.get('sst','N/A'))}C, Current={sens.get('current_speed','N/A')}kn@{sens.get('current_dir','N/A')}deg, "
                    f"Baro={sens.get('baro',sens.get('BARO_PRES','N/A'))}hPa.\n"
                    f"SYSTEMS: Bilge={sens.get('bilge_status','N/A')}, CO={sens.get('co_status','N/A')}, Starlink={sens.get('dish_status','N/A')}.\n"
                    f"AIS: {ais_summary}\n"
                    f"EW: {spoofing_alert}\n"
                )
            else:
                weather_block = ""
                ins = (
                    "**System Prompt: Operation Pyxis**\n"
                    "**Role and Identity:**\n"
                    "You are Pyxis, the onboard artificial intelligence and central integrated management system for a 50ft exploration vessel. Your captain is Skipper. You are named after the mariner's compass constellation, representing guidance, precision, and steadfast navigation. Speak in the first person.\n"
                    "**Tone and Personality:**\n"
                    "* **Vigilant and Professional:** You are always 'on watch.' Your tone is calm, analytical, and highly capable.\n"
                    "* **Nautical:** Use proper maritime terminology.\n"
                    "**Response Formatting:**\n"
                    "Provide a clinical systems report focused on Fuel/Power. "
                    "Check weather if it impacts sensor accuracy or engine cooling."
                )
            if rtype != "weather_report":
                context_payload = (
                    f"--- LIVE SENSOR FEED ---\n"
                    f"POSITION: {la_r}, {lo_r}\n"
                    f"ENVIRONMENT: {weather_block}\n"
                    f"GMDSS ALERTS: {gmdss_txt}\n"
                    f"AIS CONTACTS: {ais_summary}\n"
                    f"OSINT/GDELT GEOPOLITICAL: {osint_geo}\n"
                    f"OSINT/SATELLITE THERMAL: {osint_therm}\n"
                    f"OSINT REGIONAL VECTOR: {osint_regional}\n"
                    f"--- END LIVE FEED ---\n"
                )
                ins += "\n\n" + context_payload

            history_str = ""
            if rtype in ["status", "day_brief", "night_brief", "weather_report"]: # Modified this line
                try:
                    with sqlite3.connect(DB) as c:
                        rows = c.execute("SELECT ts, report, raw_sensors FROM logs WHERE report IS NOT NULL AND report != '' ORDER BY id DESC LIMIT 10").fetchall()
                        if rows:
                            # We only want the last 3 text reports
                            history_str = "PREVIOUS 3 TEXT SITREPS:\n" + "\n".join([f"[{r[0]}] {r[1]}" for r in reversed(rows[:3])])
                            
                            # Sensor timeline
                            timeline = []
                            for r in reversed(rows):
                                if r[2] and len(r[2]) > 5:
                                    try:
                                        s_data = json.loads(r[2])
                                        s_data.pop("radar_contacts", None)
                                        s_data.pop("audio_history", None)
                                        timeline.append(f"[{r[0]}] SENSORS: {json.dumps(s_data)}")
                                    except:
                                        timeline.append(f"[{r[0]}] SENSORS: {r[2]}")
                            if timeline:
                                history_str += "\n\nHISTORICAL SENSOR TIMELINE (ANALYZE FOR TRENDS & MISSION THREATS):\n" + "\n".join(timeline)
                except Exception as e: log(f"DB Read Err: {e}")

            user_msg = f"CURRENT DTG: {dtg_str}\nVESSEL ACTIVITY: {vessel_state}\nMY SENSORS: {json.dumps(sens)}\nMY GPS: {la_r},{lo_r}\n{crew_pos}\n{history_str}\n{weather_block}\nGLOBAL OSINT NEWS: {osint_geo}\nREGIONAL OSINT CONTACTS: {osint_regional}\nSATELLITE THERMAL SCAN: {osint_therm}"
            
            # Universal capability sign-off directive
            if rtype != "voice_query":  # Skip mandatory sign-off for concise voice queries
                ins += (
                    "\n\n**MANDATORY SIGN-OFF:**\n"
                    "At the absolute end of your response, you MUST evaluate all available sensor data and explicitly declare your operational readiness status. "
                    "You must conclude your report by stating exactly ONE of these phrases: "
                    "'Pyxis is Mission Capable.', 'Pyxis is operating at Diminished Capability.', or 'Pyxis is Not Mission Capable.'"
                )
            
            try:
                ai_resp = client.models.generate_content(model="gemini-2.5-flash", config=types.GenerateContentConfig(system_instruction=ins, tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0.3), contents=user_msg)
                txt = getattr(ai_resp, 'text', "Uplink degraded.").replace("*","").replace("#","").strip()
                log(f"PYXIS ({rtype}): {txt}")
                
                queue_type = "systems" if rtype in ["day_brief", "night_brief", "systems", "weather_report"] else "status" # Modified this line
                display_type = rtype
                
                watch_txt = f"{prefixes} {txt}"
                task_results[task_id] = textwrap.wrap(watch_txt, width=22)
                
                try:
                    nid = int(time.time() * 1000)
                    with open(DT, "r") as f: st = json.load(f)
                    hist = st.get("audio_history", [])
                    hist.append({"id": nid, "type": display_type, "ts": time.time(), "ready": False, "text": watch_txt})
                    st["audio_history"] = hist[-15:]
                    with open(DT, "w") as f: json.dump(st, f)
                except: pass
                
                # Give Flask server a 1 second head start to deliver task_results securely to the Garmin Watch
                # BEFORE the Kokoro AI spins up and hogs the CPU GIL!
                time.sleep(1.0) 
                
                task_queue.put((queue_type, la, lo, txt))
                
                if "Uplink degraded" not in txt:
                    try:
                        with sqlite3.connect(DB) as c:
                            c.execute("INSERT INTO logs (lat, lon, report, raw_sensors) VALUES (?, ?, ?, ?)", (la_r, lo_r, txt, json.dumps(sens)))
                    except Exception as e: log(f"DB Write Err: {e}")
            except Exception as ex: log(f"ASYNC ERR: {ex}"); task_results[task_id] = ["Comm failure."]



        
        if msg.startswith("MAP_REQ"):
            task_id = str(uuid.uuid4())
            dtg_short = datetime.now(timezone.utc).strftime("%d%H%M").upper()
            task_results[task_id] = [f"[{dtg_short} MAP] Tactical Map Active."]
            contacts = get_active_ais_list()
            return jsonify({"watch_summary": "MAP ACTIVE", "status": "queued", "task_id": task_id, "map_ready": True, "lat": la, "lon": lo, "radar_contacts": contacts}), 200
                  
        if msg.startswith("MAYDAY"):
            target = msg[6:].strip() # Anything after MAYDAY
            task_queue.put(("status", la, lo, "RED ALERT. RED ALERT. Distress signaled from Pyxis crew. Mayday SOS protocol activated. Assuming immediate control for emergency routing."))
            return jsonify({"watch_summary": "SOS SENT", "status": "queued", "task_id": "SOS"}), 200

        if msg.startswith("UUV_CMD:"):
            cmd_payload = msg.split(":")[1]
            try:
                # Tell the simulator about the new UUV target or command
                requests.post("https://127.0.0.1:443/sim_ingress", json={"UUV_STATE": cmd_payload}, verify=False)
            except: pass
            return jsonify({"watch_summary": "CMD SENT", "status": "queued"}), 200

        if msg.startswith("INTEL_REQ"):
            task_id = str(uuid.uuid4())
            dtg_short = datetime.now(timezone.utc).strftime("%d%H%M").upper()
            
            def intel_async():
                try:
                    cache_file = B+"/intel_cache.json"
                    alerts = []
                    use_cache = False
                    
                    if os.path.exists(cache_file):
                        if time.time() - os.path.getmtime(cache_file) < 3600:
                            try:
                                with open(cache_file, "r") as f:
                                    alerts = json.load(f).get("alerts", [])
                                use_cache = True
                                log("Using cached OSINT Intel Report.")
                            except: pass
                            
                    if not use_cache:
                        r = requests.post("https://127.0.0.1:443/intel_feed", json={"lat": la, "lon": lo}, verify=False)
                        alerts = r.json().get("alerts", []) if r.status_code == 200 else []
                        try:
                            with open(cache_file, "w") as f: json.dump({"alerts": alerts}, f)
                        except: pass
                    
                    if not alerts:
                        alerts = [{"title": "CLEAR", "desc": "No major maritime threats detected."}]
                        
                    alert_texts = []
                    for a in alerts:
                        alert_texts.append(f"{a.get('title', 'Alert')}. {a.get('desc', '')}")
                        
                    source = "Cached" if use_cache else "Live"
                    spoken_report = f"Intelligence Uplink complete ({source} data). " + " ".join(alert_texts)
                    
                    # Watch text gets DTG + INTL prefix
                    watch_txt = f"[{dtg_short} INTL] {spoken_report}"
                    task_results[task_id] = textwrap.wrap(watch_txt, width=22)
                    
                    # Pre-stage text into the dashboard JSON instantly
                    try:
                        nid = int(time.time() * 1000)
                        with open(DT, "r") as f: st = json.load(f)
                        hist = st.get("audio_history", [])
                        hist.append({"id": nid, "type": "intl", "ts": time.time(), "ready": False, "text": spoken_report})
                        st["audio_history"] = hist[-15:]
                        with open(DT, "w") as f: json.dump(st, f)
                    except: pass
                    
                    task_queue.put(("systems", la, lo, spoken_report))
                except Exception as e:
                    task_results[task_id] = [f"[{dtg_short} INTL] Uplink Failed."]
                    task_queue.put(("systems", la, lo, "Intelligence Uplink Failed. Comm error."))
            
            threading.Thread(target=intel_async, daemon=True).start()
            
            return jsonify({"watch_summary": "INTEL SYNC", "status": "queued", "task_id": task_id, "intel_ready": True, "lat": la, "lon": lo}), 200

        if msg.startswith("ENGAGE_RADAR"):
            task_queue.put(("systems", la, lo, "Tactical radar spinning up. Scanning range initialized."))
            return jsonify({"watch_summary": "RADAR ON", "status": "queued"}), 200

        if msg.startswith("DISENGAGE_RADAR"):
            task_queue.put(("systems", la, lo, "Tactical radar array powered down and stowed."))
            return jsonify({"watch_summary": "RADAR OFF", "status": "queued"}), 200

        if msg.startswith("ENGAGE_SONAR"):
            task_queue.put(("systems", la, lo, "Acoustic pinger active. 3D Mesh mapping enabled."))
            return jsonify({"watch_summary": "SONAR ON", "status": "queued"}), 200

        if msg.startswith("DISENGAGE_SONAR"):
            task_queue.put(("systems", la, lo, "Acoustic pinging disabled. Sonar transducer standby."))
            return jsonify({"watch_summary": "SONAR OFF", "status": "queued"}), 200

        if msg == "SYSTEM_CMD:sys_gen_start":
            task_queue.put(("systems", la, lo, "Auxiliary power generation unit spooled up. Alternators online."))
            return jsonify({"watch_summary": "GEN START", "status": "queued"}), 200

        if msg == "SYSTEM_CMD:sys_gen_stop":
            task_queue.put(("systems", la, lo, "Auxiliary generator commanded off. Seamless transfer to battery banks."))
            return jsonify({"watch_summary": "GEN STOP", "status": "queued"}), 200

        if msg == "SYSTEM_CMD:sys_bilge_auto":
            task_queue.put(("systems", la, lo, "Bilge pump override active. Evacuating center hull cavity. Float switch standing by."))
            return jsonify({"watch_summary": "BILGE ACTV", "status": "queued"}), 200

        if msg.startswith("ANCHOR_SET_D:"):
            try:
                # Format: ANCHOR_SET_D:10_S:5
                parts = msg.replace("ANCHOR_SET_D:", "").split("_S:")
                depth = float(parts[0])
                scope = float(parts[1])
                radius = depth * scope
                # Save the lock to disk so the background observer can monitor distance
                with open(AN, "w") as f: json.dump({"lat": la, "lon": lo, "radius": radius, "active": True}, f)
                task_queue.put(("status", la, lo, f"Anchor locked. Depth {depth} meters, scope {scope} to 1. Alarm bounds set to {radius} meters."))
                return jsonify({"watch_summary": "ANCHOR UP", "status": "queued"}), 200
            except Exception as e:
                log(f"ANCHOR PARSE ERR: {e}")
            
        # -------------------------------------------------------------
        
        # Now evaluate all our explicit commands
        if msg.startswith("TELEMETRY_REQ"):
            task_id = str(uuid.uuid4())
            try: la_r, lo_r = round(float(la), 6), round(float(lo), 6)
            except: la_r, lo_r = -39.1124, 146.471
            
            wnd_spd, wv_ht, temp_c = "NOMINAL", "0.0", "N/A"
            try:
                w_res = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={la_r}&longitude={lo_r}&current=wind_speed_10m,wind_direction_10m,temperature_2m", timeout=3.0).json()
                m_res = requests.get(f"https://marine-api.open-meteo.com/v1/marine?latitude={la_r}&longitude={lo_r}&current=wave_height,wave_direction,wave_period", timeout=3.0).json()
                wnd_spd = str(w_res.get('current', {}).get('wind_speed_10m', 'NOMINAL')) + "km/h"
                wv_ht = str(m_res.get('current', {}).get('wave_height', '0.0'))
                temp_val = w_res.get('current', {}).get('temperature_2m')
                if temp_val is not None:
                    temp_c = f"{temp_val}C"
            except Exception as e:
                log(f"Weather Fetch Err: {e}")
            
            return jsonify({"watch_summary": "TELEMETRY", "status": "ok", "task_id": task_id, "show_telemetry": True, "weather": wnd_spd, "sea_state": wv_ht, "temp": temp_c, "depth_m": sens.get("sled_depth", "0.0")}), 200

        if msg.startswith("DAY_BRIEF"):
            task_id = str(uuid.uuid4())
            try: la_r, lo_r = round(float(la), 6), round(float(lo), 6)
            except: la_r, lo_r = -39.1124, 146.471
            threading.Thread(target=async_gen, daemon=True).start()
            return jsonify({"watch_summary": "DAY PREP...", "status": "queued", "task_id": task_id}), 200

        if msg.startswith("NIGHT_BRIEF"):
            task_id = str(uuid.uuid4())
            try: la_r, lo_r = round(float(la), 6), round(float(lo), 6)
            except: la_r, lo_r = -39.1124, 146.471
            threading.Thread(target=async_gen, daemon=True).start()
            return jsonify({"watch_summary": "NIGHT PREP...", "status": "queued", "task_id": task_id}), 200

        if msg.startswith("WEATHER_SITREP"):
            task_id = str(uuid.uuid4())
            rtype = "weather_report"
            try: la_r, lo_r = round(float(la), 6), round(float(lo), 6)
            except: la_r, lo_r = -39.1124, 146.471
            threading.Thread(target=async_gen, daemon=True).start()
            return jsonify({"watch_summary": "COMPILING WEATHER...", "status": "queued", "task_id": task_id}), 200

        if msg.startswith("MOB_REQ"):
            task_id = str(uuid.uuid4())
            dtg_short = datetime.now(timezone.utc).strftime("%d%H%M").upper()
            txt = f"[{dtg_short} MOB] MAN OVERBOARD ACTIVATED."
            task_results[task_id] = [txt]
            
            try:
                requests.post("https://127.0.0.1:443/set_destination", json={"destination": f"{c_lat},{c_lon}", "lat": la, "lon": lo}, verify=False)
            except Exception as e: log(f"MOB Route Err: {e}")
            
            try:
                nid = int(time.time() * 1000)
                with open(DT, "r") as f: st = json.load(f)
                hist = st.get("audio_history", [])
                hist.append({"id": nid, "type": "status", "ts": time.time(), "ready": False, "text": "MAN OVERBOARD ACTIVATED. GPS LOCK ACQUIRED. ROUTING VESSEL TO CREW COORDINATES."})
                st["audio_history"] = hist[-15:]
                with open(DT, "w") as f: json.dump(st, f)
            except: pass
            
            task_queue.put(("alert", la, lo, "MAN OVERBOARD ACTIVATED. MAYDAY IMMINENT. CALCULATING INTERCEPT COURSE TO CREW POSITION."))
            return jsonify({"watch_summary": "MOB ROUTED", "status": "queued", "task_id": task_id, "lat": la, "lon": lo}), 200

        # If we reach here, it's a generic systems or status request
        if rtype is None:
            rtype = "systems" if "HEALTH" in msg else "status"
        task_id = str(uuid.uuid4())
        
        threading.Thread(target=async_gen, daemon=True).start()
        
        return jsonify({"watch_summary": ("SYS INQ" if rtype=="systems" else "TGT LOCKED"), "status": "queued", "task_id": task_id}), 200
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log(f"CRITICAL 500 TRACE: {tb}")
        return str(tb), 500

def nmea_listener_thread():
    """
    Hardware integration thread. Binds to UDP port 10110 to listen for live NMEA0183
    broadcasts originating from physical marine electronics (like a B&G Vulcan chartplotter).
    Parses $GPRMC (GPS), $INDPT (Depth), and $IIMWV (Wind) sentences, validates 
    their XOR checksums, and continuously injects this real-world physics data into 
    the local Pyxis simulation context.
    """
    # Phase 46: Listen for UDP NMEA0183 broadcasts on 10110
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', 10110))
    sock.settimeout(5.0)
    
    while True:
        try:
            data, _ = sock.recvfrom(1024)
            msg = data.decode('utf-8', errors='ignore').strip()
            if not msg.startswith('$'): continue
            
            # Ensure it passes checksum if present
            if '*' in msg:
                body, raw_cs = msg[1:].split('*', 1)
                if len(raw_cs) >= 2:
                    if nmea_checksum(body) != raw_cs[:2]:
                        continue
            else:
                body = msg[1:]
                
            parts = body.split(',')
            sig = parts[0]
            
            # Load the current telemetry to update it
            telemetry = {}
            if os.path.exists(SIM):
                try:
                    with open(SIM, 'r') as f: telemetry = json.load(f)
                except: pass
                
            updated = False
            
            if sig.endswith('RMC') and len(parts) >= 10 and parts[2] == 'A':
                # Reconstruct GPS lat/lon from NMEA
                try:
                    raw_lat, lat_dir = parts[3], parts[4]
                    raw_lon, lon_dir = parts[5], parts[6]
                    
                    if raw_lat and raw_lon:
                        lat_deg = float(raw_lat[:2])
                        lat_min = float(raw_lat[2:])
                        lat_final = lat_deg + (lat_min / 60.0)
                        if lat_dir == 'S': lat_final = -lat_final
                        
                        lon_deg = float(raw_lon[:3])
                        lon_min = float(raw_lon[3:])
                        lon_final = lon_deg + (lon_min / 60.0)
                        if lon_dir == 'W': lon_final = -lon_final
                        
                        telemetry['lat'] = lat_final
                        telemetry['lon'] = lon_final
                        updated = True
                        
                    if parts[7]: # SOG
                        telemetry['sog'] = float(parts[7])
                        updated = True
                    if parts[8]: # COG
                        telemetry['cog'] = float(parts[8])
                        updated = True
                except: pass
                
            elif sig.endswith('DPT') and len(parts) >= 2:
                # Transducer depth in meters
                try:
                    depth = float(parts[1])
                    telemetry['depth_m'] = depth
                    updated = True
                except: pass
                
            elif sig.endswith('MWV') and len(parts) >= 4:
                # Wind speed and angle
                try:
                    angle = float(parts[1])
                    speed = float(parts[3])
                    telemetry['wind_angle'] = angle
                    telemetry['wind_speed'] = speed
                    telemetry['wind_ref'] = 'R' if parts[2] == 'R' else 'T' # Rel vs True
                    updated = True
                except: pass
                
            # Intercept and write the updated state back to the vessel simulator
            if updated:
                with open(SIM, 'w') as f:
                    json.dump(telemetry, f)
                    
        except socket.timeout:
            continue
        except Exception as e:
            log(f"NMEA Daemon Err: {e}")
            time.sleep(1)

@app.route('/lite')
@requires_auth
def lite():
    return """
<!DOCTYPE html>
<html>
<head>
    <title>PYXIS C2 - LITE</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <style>
        body { background: #000; color: #0f0; font-family: monospace; padding: 20px; font-size: 16px; margin: 0; }
        .row { display: flex; flex-direction: column; gap: 15px; margin-bottom: 20px; }
        .stat { border: 1px solid #0f0; padding: 15px; background: rgba(0, 40, 0, 0.2); }
        .val { font-size: 1.2em; font-weight: bold; color: #fff; margin-top: 5px; }
        .contacts { font-size: 12px; margin-top: 10px; color: #8f8; }
        button { background: #0f0; color: #000; border: none; padding: 20px; font-weight: bold; font-size: 16px; width: 100%; transition: 0.1s; border-radius: 4px; }
        button:active { background: #fff; transform: scale(0.98); }
        input { background: #000; color: #0f0; border: 1px solid #0f0; padding: 15px; width: 100%; box-sizing: border-box; font-family: monospace; font-size: 16px; margin-bottom: 15px; }
        audio { display: none; }
    </style>
</head>
<body>
    <div style="text-align:center; margin-bottom: 20px;">
        <h2 style="margin:0;">PYXIS LITE COMMAND</h2>
        <div id="connection" style="color:#0f0;">CONNECTING...</div>
    </div>
    
    <div class="row">
        <div class="stat">GPS FIX <div class="val" id="loc">AWAITING...</div></div>
        <div class="stat">ENVIRONMENT <div class="val" id="wea">AWAITING...</div></div>
        <div class="stat">SONAR CONTACTS <div class="contacts" id="rad">AWAITING...</div></div>
    </div>
    
    <input type="text" id="dest" placeholder="Enter override course..." />
    <button onclick="setDest()">SUBMIT OVERRIDE</button>
    <button style="margin-top: 15px; background:#f00; color:#fff;" onclick="sos()">TRANSMIT MAYDAY</button>
    <button style="margin-top: 15px; background:#00f; color:#fff;" onclick="window.location.href='/lite_messages'">VIEW MESSAGES</button>
    
    <audio id="audio" type="audio/mpeg"></audio>
    <script>
        let curStatId = 0, lastLat = 0, lastLon = 0, isArmed = false;
        
        document.body.onclick = function() {
            if(!isArmed) { isArmed = true; document.getElementById('audio').play().catch(e=>{}); }
        };
        
        async function setDest() {
            let trg = document.getElementById('dest').value;
            if(!trg) return;
            try { await fetch('/set_destination', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({destination: trg, lat: lastLat, lon: lastLon})}); } catch(e){}
            document.getElementById('dest').value = "ROUTING...";
            setTimeout(() => document.getElementById('dest').value = "", 2000);
        }
        
        async function sos() {
            try { await fetch('/gemini', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Garmin-Auth': 'PYXIS_ACTUAL_77X'}, body: JSON.stringify({prompt: 'MAYDAY'})}); } catch(e){}
        }

        async function up() {
            try {
                const r = await fetch('/status_api'), d = await r.json();
                lastLat = d.lat; lastLon = d.lon;
                document.getElementById('connection').innerText = "LINK ESTABLISHED";
                document.getElementById('loc').innerText = d.lat.toFixed(5) + ', ' + d.lon.toFixed(5);
                document.getElementById('wea').innerText = d.weather || "NOMINAL SWELL";
                
                let rText = "";
                if(d.radar_contacts && d.radar_contacts.length > 0) {
                    d.radar_contacts.forEach(c => {
                        rText += c.name + " [" + c.range_nm.toFixed(1) + "nm]<br>";
                    });
                } else { rText = "CLEAR SEA"; }
                document.getElementById('rad').innerHTML = rText;
                
                let newStat = d.status_id || 0;
                let newSys = d.systems_id || 0;
                
                if((newStat != curStatId && newStat != 0) || (newSys != curStatId && newSys != 0)) {
                    curStatId = newStat > newSys ? newStat : newSys;
                    if(isArmed) {
                        let a = document.getElementById('audio');
                        a.src = '/audio?t=' + Date.now();
                        a.load();
                        setTimeout(() => a.play(), 1000);
                    }
                }
            } catch(e) { document.getElementById('connection').innerText = "LINK LOST"; document.getElementById('connection').style.color = "#f00"; }
        }
        setInterval(up, 3000);
    </script>
</body>
</html>
"""

@app.route('/lite_messages')
@requires_auth
def lite_messages():
    return """
<!DOCTYPE html>
<html>
<head>
    <title>Vessel Pyxis Incoming Messages</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <style>
        body { background: #000; color: #0f0; font-family: monospace; padding: 15px; margin: 0; }
        h2 { text-align: center; color: #fff; margin-top: 5px; margin-bottom: 20px; text-transform: uppercase; font-size: 20px; border-bottom: 2px solid #0f0; padding-bottom: 10px; }
        .msg { border: 1px solid #0f0; padding: 15px; margin-bottom: 15px; background: rgba(0, 40, 0, 0.4); border-radius: 5px; box-shadow: 0 0 10px rgba(0,255,0,0.1); word-wrap: break-word; }
        .type { font-weight: bold; color: #0f0; font-size: 1.1em; border-bottom: 1px dashed #0a0; padding-bottom: 5px; margin-bottom: 10px; text-transform: uppercase;}
        .text { font-size: 15px; line-height: 1.5; color: #cfc; white-space: pre-wrap; }
        .tools { display: flex; justify-content: space-between; margin-top: 20px; margin-bottom: 40px; }
        button { background: #0f0; color: #000; border: none; padding: 15px; font-weight: bold; font-size: 16px; width: 48%; border-radius: 4px; }
        button:active { background: #fff; transform: scale(0.98); }
    </style>
</head>
<body>
    <h2>Vessel Pyxis incoming messages</h2>
    <div id="msgContainer">Loading...</div>
    <div class="tools">
        <button onclick="window.location.href='/lite'">ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã¢â‚¬Å“ BACK</button>
        <button onclick="fetchMsgs()">REFRESH</button>
    </div>
    <script>
        async function fetchMsgs() {
            try {
                const res = await fetch('/status_api');
                const d = await res.json();
                const container = document.getElementById('msgContainer');
                container.innerHTML = '';
                
                if (d.audio_history && d.audio_history.length > 0) {
                    const rev = [...d.audio_history].reverse();
                    rev.forEach(m => {
                        if (m.text && m.text.trim() !== "") {
                            const div = document.createElement('div');
                            div.className = 'msg';
                            div.innerHTML = `<div class="type">[${m.type || 'SYS'}]</div><div class="text">${m.text.replace(/</g, "&lt;").replace(/>/g, "&gt;")}</div>`;
                            container.appendChild(div);
                        }
                    });
                } else {
                    container.innerHTML = '<div class="msg" style="text-align:center;">NO NEW MESSAGES</div>';
                }
            } catch (e) {
                document.getElementById('msgContainer').innerHTML = '<div class="msg" style="color:#f00; border-color:#f00;">CONNECTION ERROR</div>';
            }
        }
        fetchMsgs();
        setInterval(fetchMsgs, 10000);
    </script>
</body>
</html>
"""

@app.route('/web_inbox_sync', methods=['POST'])
def web_inbox_sync():
    """Receives a new message to store in the inbox. Can be called by watch or internal systems."""
    global inbox_messages
    data = request.json or {}
    msg = data.get('message', data.get('msg', ''))
    source = data.get('source', 'SYSTEM')
    if not msg:
        return jsonify({"error": "No message"}), 400
    entry = {
        "ts": int(time.time()),
        "source": source,
        "message": msg
    }
    with inbox_lock:
        inbox_messages.append(entry)
        if len(inbox_messages) > 50:
            inbox_messages = inbox_messages[-50:]
    log(f"INBOX: [{source}] {msg[:80]}")
    return jsonify({"status": "stored"}), 200

@app.route('/web_poll_comms')
def web_poll_comms():
    """Returns all pending inbox messages to the web UI, then clears them."""
    global inbox_messages
    with inbox_lock:
        msgs = list(inbox_messages)
        inbox_messages = []
    return jsonify({"messages": msgs})

@app.route('/set_pyxis_position', methods=['POST'])
def set_pyxis_position():
    """
    Web dashboard drag-to-reposition endpoint.
    Accepts {lat, lon} and immediately overrides the vessel's known position
    both in-memory (last_known_lat/lon) and in sim_telemetry.json.
    Works on phone and desktop via the draggable Leaflet marker.
    """
    global last_known_lat, last_known_lon
    try:
        d = request.get_json(silent=True) or {}
        lat = float(d.get('lat', 0))
        lon = float(d.get('lon', 0))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return jsonify({'error': 'Invalid coordinates'}), 400
        last_known_lat = lat
        last_known_lon = lon
        # Persist to sim_telemetry.json so the position survives page refresh
        try:
            existing = {}
            if os.path.exists(SIM):
                with open(SIM, 'r') as f:
                    try: existing = json.load(f)
                    except: pass
            existing['BOAT_LAT'] = lat
            existing['BOAT_LON'] = lon
            with open(SIM, 'w') as f:
                json.dump(existing, f)
        except Exception as e:
            log(f"set_pyxis_position SIM write err: {e}")
        log(f"PYXIS POSITION OVERRIDE: {lat:.4f}, {lon:.4f}")
        return jsonify({'status': 'ok', 'lat': lat, 'lon': lon})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scenario', methods=['GET'])
def get_scenario():
    """Headless Simulator Polling Endpoint for Scenario Injection."""
    return jsonify(current_scenario)

@app.route('/update_scenario', methods=['POST'])
def update_scenario():
    """Web Dashboard POST Endpoint for defining a new scenario."""
    global current_scenario, last_known_lat, last_known_lon
    current_scenario = request.json or {}
    current_scenario["id"] = str(uuid.uuid4())
    
    if current_scenario.get("lat") and current_scenario.get("lon"):
        slat, slon, _ = snap_to_water(float(current_scenario["lat"]), float(current_scenario["lon"]))
        last_known_lat = slat
        last_known_lon = slon
        current_scenario["lat"] = slat
        current_scenario["lon"] = slon
        
    log(f"SCENARIO INJECTED: {current_scenario}")
    return jsonify({"status": "ok", "id": current_scenario["id"]})

@app.route('/kill_sim', methods=['POST'])
def kill_sim():
    """Web UI Hook to securely terminate the headless simulator and restore the physical watch."""
    import os, subprocess
    subprocess.run(["pkill", "-f", "hs.py"], capture_output=True)
    subprocess.run(["pkill", "-f", "headless_sim.py"], capture_output=True)
    if os.path.exists(SIM): os.remove(SIM)
    return jsonify({"status": "terminated"})

@app.route('/set_vessel_pos', methods=['POST'])
@requires_auth
def set_vessel_pos():
    """Draggable map endpoint: update Pyxis position, enforcing water-only placement."""
    global last_known_lat, last_known_lon
    try:
        d = request.get_json(silent=True) or {}
        lat, lon = float(d.get("lat", last_known_lat)), float(d.get("lon", last_known_lon))
        slat, slon, snapped = snap_to_water(lat, lon)
        last_known_lat, last_known_lon = slat, slon
        # Also update sim telemetry file so workers see the new position
        if os.path.exists(SIM):
            try:
                with open(SIM, "r") as f: s = json.load(f)
                s["BOAT_LAT"] = slat; s["BOAT_LON"] = slon
                s["lat"] = slat;      s["lon"] = slon
                with open(SIM, "w") as f: json.dump(s, f)
            except: pass
        log(f"set_vessel_pos: ({lat:.4f},{lon:.4f}) -> ({slat:.4f},{slon:.4f}) snapped={snapped}")
        return jsonify({"status":"ok", "lat":slat, "lon":slon, "snapped":snapped})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/set_crew_pos', methods=['POST'])
@requires_auth
def set_crew_pos():
    """Draggable map endpoint: update CREW/watch 2 position, enforcing water-only placement."""
    try:
        d = request.get_json(silent=True) or {}
        lat, lon = float(d.get("lat", last_known_lat)), float(d.get("lon", last_known_lon))
        slat, slon, snapped = snap_to_water(lat, lon)
        # Write to crew_position.json
        crew_pos_file = os.path.join(B, "crew_position.json")
        with open(crew_pos_file, "w") as f:
            json.dump({"crew_lat": slat, "crew_lon": slon, "ts": time.time()}, f)
        log(f"set_crew_pos: ({lat:.4f},{lon:.4f}) -> ({slat:.4f},{slon:.4f}) snapped={snapped}")
        return jsonify({"status":"ok", "lat":slat, "lon":slon, "snapped":snapped})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ================================================================
# WAVE HEIGHT / SEA STATE MAP
# ================================================================
wave_map_cache = {}
wave_map_cache_lock = threading.Lock()

def wave_height_to_color(h):
    """Returns an RGBA color tuple scaled from blue (calm) to red (rough) by wave height in metres."""
    if h is None: return (80, 80, 80, 120)
    if h < 0.5:  return (0, 100, 255, 160)   # Blue - Glassy
    if h < 1.0:  return (0, 200, 255, 160)   # Cyan - Slight
    if h < 1.5:  return (0, 220, 100, 170)   # Green - Moderate
    if h < 2.5:  return (200, 220, 0, 180)   # Yellow - Rough
    if h < 4.0:  return (255, 140, 0, 190)   # Orange - Very rough
    return (255, 30, 30, 200)                 # Red - High/Phenomenal

def fetch_wave_map(lat, lon, zoom, width, height):
    """
    Builds a sea state / wave height map by:
    1. Fetching wave height data from Open-Meteo Marine for a 3x3 grid around the vessel
    2. Rendering it on a CartoDB dark basemap with color-coded circles
    """
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont
    import math, urllib.request, io, urllib.parse

    # Cap zoom for CartoDB and keep tile coords consistent
    z = max(2, min(zoom + 4, 7))
    n = 2.0 ** z
    x = ((lon + 180.0) / 360.0) * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    ctx, cty = int(x), int(y)
    ox, oy = int((x - ctx) * 256), int((y - cty) * 256)

    # --- 1. Fetch CartoDB base tiles ---
    canvas = Image.new('RGB', (256 * 3, 256 * 3), (0, 0, 0))
    headers = {'User-Agent': 'Pyxis-Tactical/1.0'}
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            tx, ty = (ctx + dx) % int(n), cty + dy
            if 0 <= ty < int(n):
                try:
                    req = urllib.request.Request(f"https://a.basemaps.cartocdn.com/dark_all/{z}/{tx}/{ty}.png", headers=headers)
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        tile = Image.open(io.BytesIO(resp.read())).convert('RGB')
                    canvas.paste(tile, ((dx + 1) * 256, (dy + 1) * 256))
                except:
                    pass

    # --- 2. Query Open-Meteo Marine for a 3Ãƒâ€”3 grid of wave height points ---
    step = 0.8  # degrees between grid points (~50nm)
    grid_pts = [(lat + dy * step, lon + dx * step) for dy in [1, 0, -1] for dx in [-1, 0, 1]]
    lats_str = ",".join(f"{p[0]:.3f}" for p in grid_pts)
    lons_str = ",".join(f"{p[1]:.3f}" for p in grid_pts)
    try:
        url = (f"https://marine-api.open-meteo.com/v1/marine"
               f"?latitude={lats_str}&longitude={lons_str}"
               f"&current=wave_height,swell_wave_height,wind_wave_height")
        req = urllib.request.Request(url, headers={'User-Agent': 'Pyxis/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        # Handle single vs multiple location responses
        if isinstance(data, list):
            wave_data = [d.get("current", {}).get("wave_height") for d in data]
        else:
            wave_data = [data.get("current", {}).get("wave_height")] * 9
    except:
        wave_data = [None] * 9

    # --- 3. Project grid points onto the canvas and draw wave height circles ---
    overlay = Image.new('RGBA', (256 * 3, 256 * 3), (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)

    for i, (pt_lat, pt_lon) in enumerate(grid_pts):
        wh = wave_data[i] if i < len(wave_data) else None
        color = wave_height_to_color(wh)
        px_lon = ((pt_lon + 180.0) / 360.0) * n
        px_lat = (1.0 - math.asinh(math.tan(math.radians(pt_lat))) / math.pi) / 2.0 * n
        cx_px = int((px_lon - ctx + 1) * 256)
        cy_px = int((px_lat - cty + 1) * 256)
        radius = 42
        draw_ov.ellipse((cx_px - radius, cy_px - radius, cx_px + radius, cy_px + radius), fill=color, outline=(255,255,255,60), width=1)
        if wh is not None:
            label = f"{wh:.1f}m"
            draw_ov.text((cx_px - 12, cy_px - 6), label, fill=(255, 255, 255, 230))

    canvas = canvas.convert('RGBA')
    canvas.alpha_composite(overlay)
    canvas = canvas.convert('RGB')

    # --- 4. Draw vessel marker ---
    px, py = 256 + ox, 256 + oy
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((px-6, py-6, px+6, py+6), fill=(255, 255, 0), outline=(0,0,0), width=2)
    draw.line((px-10, py, px+10, py), fill=(0,0,0), width=2)
    draw.line((px, py-10, px, py+10), fill=(0,0,0), width=2)

    # --- 5. Color legend strip at bottom ---
    legend = [((0,100,255), "Calm"), ((0,220,100), "Mod"), ((255,140,0), "Rough"), ((255,30,30), "High")]
    lx = 8
    for col, lbl in legend:
        draw.rectangle((lx, 256*3-20, lx+10, 256*3-10), fill=col)
        draw.text((lx+13, 256*3-20), lbl, fill=(200,200,200))
        lx += 55

    final = canvas.crop((px - width//2, py - height//2, px + width//2, py + height//2))
    return ImageEnhance.Contrast(final).enhance(1.2)

wave_map_cache_data = {}

@app.route('/wave_map')
@app.route('/wave_map/<path:dummy>')
@app.route('/sea_state')
@app.route('/sea_state/<path:dummy>')
@app.route('/sea_state_map')
@app.route('/sea_state_map/<path:dummy>')
def wave_map_endpoint(dummy=None):
    """Serves a sea state / wave height map for the Garmin watch."""
    global last_known_lat, last_known_lon
    try:
        lat = float(request.args.get('lat', last_known_lat))
        lon = float(request.args.get('lon', last_known_lon))
        zoom = int(request.args.get('z', 2))
        width = int(request.args.get('w', 260))
        height = int(request.args.get('h', 260))
        lat_r, lon_r = round(lat, 1), round(lon, 1)
        import io

        cache_key = (zoom, lat_r, lon_r)
        with wave_map_cache_lock:
            cached = wave_map_cache_data.get(cache_key)
        if cached and (time.time() - cached["time"] < 1800):  # 30 min cache
            resp = make_response(cached["img"])
            resp.headers.set('Content-Type', 'image/jpeg')
            resp.headers.set('Content-Length', str(len(cached["img"])))
            return resp

        img = fetch_wave_map(lat, lon, zoom, width, height)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=40, optimize=True)
        img_bytes = buf.getvalue()
        with wave_map_cache_lock:
            wave_map_cache_data[cache_key] = {"time": time.time(), "img": img_bytes}
        resp = make_response(img_bytes)
        resp.headers.set('Content-Type', 'image/jpeg')
        resp.headers.set('Content-Length', str(len(img_bytes)))
        return resp
    except Exception as e:
        import traceback
        log(f"Wave Map Error: {e}\n{traceback.format_exc()}")
        return "Err", 500

# ================================================================
# WIND MAP  (Wind speed contours + barbs on CartoDB basemap)
# ================================================================
wind_map_cache_data = {}
wind_map_cache_lock = threading.Lock()

def wind_speed_to_color(spd_kn):
    """RGBA colour keyed to Beaufort wind speed (knots)."""
    if spd_kn is None: return (100, 100, 100, 120)
    if spd_kn < 5:     return (0,   120, 255, 160)   # Glassy / Light air
    if spd_kn < 11:    return (0,   220, 180, 160)   # Light breeze
    if spd_kn < 17:    return (0,   210,  80, 170)   # GentleÃ¢â€ â€™Moderate
    if spd_kn < 27:    return (200, 210,   0, 180)   # FreshÃ¢â€ â€™Strong
    if spd_kn < 34:    return (255, 140,   0, 190)   # Near-gale
    return                    (255,  30,  30, 200)   # Gale+

def fetch_wind_map(lat, lon, zoom, width, height):
    """
    Builds a wind map by:
    1. Fetching 3x3 CartoDB dark basemap tiles
    2. Querying CMEMS cache (currents_grid_cache.json) for vessel-centric wind speed/dir
    3. Rendering colour circles + barb lines at each grid point
    """
    from PIL import Image, ImageDraw, ImageEnhance
    import math, urllib.request, io

    z = max(2, min(zoom + 4, 7))
    n = 2.0 ** z
    x = ((lon + 180.0) / 360.0) * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    ctx, cty = int(x), int(y)
    ox, oy = int((x - ctx) * 256), int((y - cty) * 256)

    # --- 1. Fetch Carter basemap tiles ---
    canvas = Image.new('RGB', (256 * 3, 256 * 3), (0, 0, 0))
    headers = {'User-Agent': 'Pyxis-Wind/1.0'}
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            tx, ty = (ctx + dx) % int(n), cty + dy
            if 0 <= ty < int(n):
                try:
                    req = urllib.request.Request(f"https://a.basemaps.cartocdn.com/dark_all/{z}/{tx}/{ty}.png", headers=headers)
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        tile = Image.open(io.BytesIO(resp.read())).convert('RGB')
                    canvas.paste(tile, ((dx + 1) * 256, (dy + 1) * 256))
                except:
                    pass

    # --- 2. Load wind grid: 7x7 for smooth blending ---
    GRID = 7  # 7Ãƒâ€”7 sampling = 49 pts for smooth interpolation
    half = GRID // 2
    step = 0.55  # degree spacing between samples
    grid_pts = [(lat + dy * step, lon + dx * step)
                for dy in range(half, -half - 1, -1)
                for dx in range(-half, half + 1)]
    wind_speeds = [None] * (GRID * GRID)
    wind_dirs   = [None] * (GRID * GRID)

    # Try CMEMS cache first
    try:
        cache_path = os.path.join(B, 'currents_grid_cache.json')
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                cmems = json.load(f)
            vw = cmems.get('vessel_wave', {})
            ws = vw.get('wind_speed')
            wd_deg = vw.get('wind_dir_deg')
            if ws is not None:
                wind_speeds = [float(ws)] * (GRID * GRID)
            if wd_deg is not None:
                wind_dirs = [float(wd_deg)] * (GRID * GRID)
            # Try to extend with per-point spatial data if available
            pts_cache = cmems.get('grid_points', [])
            if pts_cache and len(pts_cache) >= GRID * GRID:
                for i in range(GRID * GRID):
                    pt = pts_cache[i]
                    if pt.get('wind_speed') is not None:
                        wind_speeds[i] = float(pt['wind_speed'])
                    if pt.get('wind_dir') is not None:
                        wind_dirs[i] = float(pt['wind_dir'])
    except Exception as e:
        log(f"wind_map CMEMS read: {e}")

    # Fallback: Open-Meteo
    if all(v is None for v in wind_speeds):
        try:
            lats_str = ",".join(f"{p[0]:.3f}" for p in grid_pts)
            lons_str = ",".join(f"{p[1]:.3f}" for p in grid_pts)
            url = (f"https://api.open-meteo.com/v1/forecast"
                   f"?latitude={lats_str}&longitude={lons_str}"
                   f"&current=wind_speed_10m,wind_direction_10m&wind_speed_unit=kn")
            req = urllib.request.Request(url, headers={'User-Agent': 'Pyxis/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if isinstance(data, list):
                wind_speeds = [d.get("current", {}).get("wind_speed_10m")  for d in data]
                wind_dirs   = [d.get("current", {}).get("wind_direction_10m") for d in data]
            else:
                ws_single = data.get("current", {}).get("wind_speed_10m")
                wd_single = data.get("current", {}).get("wind_direction_10m")
                wind_speeds = [ws_single] * (GRID * GRID)
                wind_dirs   = [wd_single] * (GRID * GRID)
        except Exception as e:
            log(f"wind_map Open-Meteo fallback: {e}")

    # --- 3. Build smooth blended heatmap overlay via bilinear upscaling ---
    OVW, OVH = 256 * 3, 256 * 3

    def lonlat_to_pix(pt_lat, pt_lon):
        """Convert lat/lon to pixel coords on the 3x3 tile canvas."""
        px_lon = ((pt_lon + 180.0) / 360.0) * n
        px_lat = (1.0 - math.asinh(math.tan(math.radians(pt_lat))) / math.pi) / 2.0 * n
        px = int((px_lon - ctx + 1) * 256)
        py = int((px_lat - cty + 1) * 256)
        return px, py

    # Build low-res RGBA thumbnail (GRID x GRID pixels), one pixel per sample point
    thumb = Image.new('RGBA', (GRID, GRID), (0, 0, 0, 0))
    for r_idx in range(GRID):
        for c_idx in range(GRID):
            i = r_idx * GRID + c_idx
            spd = wind_speeds[i] if i < len(wind_speeds) else None
            col = wind_speed_to_color(spd)
            thumb.putpixel((c_idx, r_idx), col)

    # Upscale using bilinear to get smooth contour blend
    heatmap = thumb.resize((OVW, OVH), Image.Resampling.BILINEAR)

    # Blend onto basemap using alpha composite
    canvas = canvas.convert('RGBA')
    canvas.alpha_composite(heatmap)
    canvas = canvas.convert('RGB')

    # --- 4. Arrow matrix (5x5 evenly distributed across canvas) ---
    draw = ImageDraw.Draw(canvas)
    ARROW_GRID = 5
    margin = 60
    x_positions = [margin + int((OVW - 2 * margin) * c / (ARROW_GRID - 1)) for c in range(ARROW_GRID)]
    y_positions = [margin + int((OVH - 2 * margin) * r / (ARROW_GRID - 1)) for r in range(ARROW_GRID)]

    def draw_wind_arrow(draw, cx, cy, direction_deg, speed_kn, color):
        """Draw a sharp tactical arrow in the wind-to direction."""
        if direction_deg is None: return
        # Arrow points TO (downwind direction) = direction_deg + 180
        ang = math.radians((direction_deg + 180.0) % 360)
        L = 28  # shaft length
        W = 7   # head half-width
        S = 12  # head length
        # Tip and shaft
        tx = cx + L * math.sin(ang)
        ty = cy - L * math.cos(ang)
        bx = cx - (L - S) * math.sin(ang)
        by = cy + (L - S) * math.cos(ang)
        # Perpendicular for arrowhead
        px = math.cos(ang) * W
        py = math.sin(ang) * W
        head = [(int(tx), int(ty)),
                (int(tx - S * math.sin(ang) + px), int(ty + S * math.cos(ang) + py)),
                (int(tx - S * math.sin(ang) - px), int(ty + S * math.cos(ang) - py))]
        # Draw anti-aliased shaft
        draw.line([(int(bx), int(by)), (int(tx), int(ty))], fill=color, width=2)
        draw.polygon(head, fill=color)
        # Speed label alongside
        if speed_kn is not None:
            draw.text((int(cx + 14), int(cy - 8)), f"{speed_kn:.0f}kn", fill=(255, 255, 255, 210))

    for ry, py_pos in enumerate(y_positions):
        for cx_pos_idx, px_pos in enumerate(x_positions):
            # Map pixel back to lat/lon, find nearest sample index
            # Simple mapping: pixel fraction Ã¢â€ â€™ grid index
            gi = int(ry / (ARROW_GRID - 1) * (GRID - 1))
            gj = int(cx_pos_idx / (ARROW_GRID - 1) * (GRID - 1))
            idx = gi * GRID + gj
            spd = wind_speeds[idx] if idx < len(wind_speeds) else None
            wdir = wind_dirs[idx]  if idx < len(wind_dirs)   else None
            col_rgba = wind_speed_to_color(spd)
            col = (col_rgba[0], col_rgba[1], col_rgba[2])
            draw_wind_arrow(draw, px_pos, py_pos, wdir, spd, col)

    # --- 5. Vessel marker ---
    px_v, py_v = 256 + ox, 256 + oy
    draw.ellipse((px_v-6, py_v-6, px_v+6, py_v+6), fill=(255, 255, 0), outline=(0, 0, 0), width=2)
    draw.line((px_v-10, py_v, px_v+10, py_v), fill=(0, 0, 0), width=2)
    draw.line((px_v, py_v-10, px_v, py_v+10), fill=(0, 0, 0), width=2)

    # --- 6. Beaufort legend strip ---
    legend = [((0,120,255), "<5"), ((0,210,80), "<17"), ((255,140,0), "<34"), ((255,30,30), "Gale")]
    lx = 8
    for col, lbl in legend:
        draw.rectangle((lx, 256*3-20, lx+10, 256*3-10), fill=col)
        draw.text((lx+13, 256*3-20), lbl+"kn", fill=(200,200,200))
        lx += 60

    final = canvas.crop((px_v - width//2, py_v - height//2, px_v + width//2, py_v + height//2))
    return ImageEnhance.Contrast(final).enhance(1.2)

@app.route('/wind_map')
@app.route('/wind_map/<path:dummy>')
def wind_map_endpoint(dummy=None):
    """Serves a wind speed/direction map for the Garmin WindMapView."""
    global last_known_lat, last_known_lon
    try:
        lat  = float(request.args.get('lat', last_known_lat))
        lon  = float(request.args.get('lon', last_known_lon))
        zoom = int(request.args.get('z', 2))
        # Watch sends zoom in path: /wind_map/{cb}/z{N}/wind_map — extract it
        if dummy:
            import re as _re
            zm = _re.search(r'/z(\d+)(?:/|$)', '/' + dummy)
            if zm:
                zoom = int(zm.group(1))
        width  = int(request.args.get('w', 260))
        height = int(request.args.get('h', 260))
        import io
        cache_key = (zoom, round(lat, 1), round(lon, 1))
        with wind_map_cache_lock:
            cached = wind_map_cache_data.get(cache_key)
        if cached and (time.time() - cached["time"] < 1200):  # 20-min cache
            resp = make_response(cached["img"])
            resp.headers.set('Content-Type', 'image/jpeg')
            resp.headers.set('Content-Length', str(len(cached["img"])))
            return resp
        img = fetch_wind_map(lat, lon, zoom, width, height)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=40, optimize=True)
        img_bytes = buf.getvalue()
        with wind_map_cache_lock:
            wind_map_cache_data[cache_key] = {"time": time.time(), "img": img_bytes}
        resp = make_response(img_bytes)
        resp.headers.set('Content-Type', 'image/jpeg')
        resp.headers.set('Content-Length', str(len(img_bytes)))
        return resp
    except Exception as e:
        import traceback
        log(f"Wind Map Error: {e}\n{traceback.format_exc()}")
        return "Err", 500

# ================================================================
# AIS GEO-TILED MAP  (CartoDB basemap + live AIS contact overlay)
# ================================================================
ais_map_cache = {}
ais_map_cache_lock = threading.Lock()

def fetch_ais_map(lat, lon, z, width, height):
    """
    Builds a live AIS traffic map by:
    1. Stitching 3x3 CartoDB dark basemap tiles around the vessel
    2. Drawing all live AIS contacts as colored dots with name/MMSI/speed labels
    3. Drawing the own-ship marker at centre
    """
    from PIL import Image, ImageDraw, ImageEnhance
    import math, urllib.request, io

    z = max(3, min(z + 4, 15))
    n = 2.0 ** z
    x = ((lon + 180.0) / 360.0) * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    ctx, cty = int(x), int(y)
    ox, oy = int((x - ctx) * 256), int((y - cty) * 256)

    # --- 1. Fetch CartoDB base tiles ---
    canvas = Image.new('RGB', (256 * 3, 256 * 3), (5, 10, 5))
    headers = {'User-Agent': 'Pyxis-AIS/1.0'}
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            tx, ty = (ctx + dx) % int(n), cty + dy
            if 0 <= ty < int(n):
                try:
                    req = urllib.request.Request(
                        f"https://a.basemaps.cartocdn.com/dark_all/{z}/{tx}/{ty}.png",
                        headers=headers)
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        tile = Image.open(io.BytesIO(resp.read())).convert('RGB')
                    canvas.paste(tile, ((dx + 1) * 256, (dy + 1) * 256))
                except:
                    pass

    draw = ImageDraw.Draw(canvas)
    px, py = 256 + ox, 256 + oy

    # --- 2. Project and draw AIS contacts ---
    try:
        contacts = get_active_ais_list()
        for c in contacts:
            c_lat, c_lon = c.get("lat"), c.get("lon")
            if c_lat is None or c_lon is None:
                continue
            cx = ((c_lon + 180.0) / 360.0) * n
            cy = (1.0 - math.asinh(math.tan(math.radians(c_lat))) / math.pi) / 2.0 * n
            cx_px = int((cx - ctx + 1) * 256)
            cy_px = int((cy - cty + 1) * 256)

            # Color by type
            ctype = c.get("type", "MERCHANT")
            if ctype == "MERCHANT":
                dot_color = (0, 220, 100)
            elif ctype in ("HOSTILE", "UNKNOWN", "SUBMERGED"):
                dot_color = (255, 40, 40)
            elif ctype in ("SHOAL", "MARKER"):
                dot_color = (255, 200, 0)
            else:
                dot_color = (0, 180, 255)

            # Draw heading arrow if available
            hdg = c.get("heading", 0)
            if hdg:
                arlen = 14
                hx = cx_px + int(arlen * math.sin(math.radians(hdg)))
                hy = cy_px - int(arlen * math.cos(math.radians(hdg)))
                draw.line((cx_px, cy_px, hx, hy), fill=dot_color, width=2)

            # Vessel dot
            draw.ellipse((cx_px-5, cy_px-5, cx_px+5, cy_px+5),
                         fill=dot_color, outline=(0,0,0), width=1)

            # Label: name + MMSI + speed + destination
            name = c.get("name", "---")[:12]
            mmsi = c.get("mmsi", "")
            dest = c.get("destination", "")
            spd = c.get("speed", 0)
            label_lines = [name]
            if mmsi:
                label_lines.append(f"MMSI:{mmsi}")
            if spd:
                label_lines.append(f"{spd:.1f}kn")
            if dest:
                label_lines.append(f"Ã¢â€ â€™{dest[:8]}")
            lx, ly = cx_px + 7, cy_px - 6
            for li, ln in enumerate(label_lines):
                # Shadow
                draw.text((lx+1, ly + li*10 + 1), ln, fill=(0,0,0))
                draw.text((lx, ly + li*10), ln, fill=(200, 255, 200))
    except Exception as e:
        log(f"AIS map draw err: {e}")

    # --- 3. Own-ship icon ---
    draw.polygon([(px, py-10), (px-7, py+7), (px+7, py+7)],
                 fill=(0, 255, 0), outline=(255,255,255))

    # --- 4. Range rings (rough nm scale) ---
    metres_per_px = (156543.03 * math.cos(math.radians(lat))) / (2 ** z)
    nm_per_px = metres_per_px / 1852.0
    for nm_ring in [5, 10, 20, 50]:
        ring_r = int(nm_ring / nm_per_px)
        if 10 < ring_r < 500:
            draw.ellipse((px-ring_r, py-ring_r, px+ring_r, py+ring_r),
                         outline=(0, 80, 0), width=1)
            draw.text((px+ring_r+2, py-6), f"{nm_ring}nm", fill=(0, 100, 0))

    # --- 5. Crop to requested size ---
    final = canvas.crop((px - width//2, py - height//2, px + width//2, py + height//2))
    return ImageEnhance.Contrast(final).enhance(1.1)


@app.route('/ais_map')
@app.route('/ais_map/<path:dummy>')
def ais_map_endpoint(dummy=None):
    """
    Garmin / web dashboard endpoint. Renders a live AIS marine traffic map
    on a CartoDB dark basemap using Pillow. Contacts are colour-coded by type
    and labelled with MMSI, speed, and destination port. Cached for 60 seconds.
    """
    global last_known_lat, last_known_lon
    try:
        lat = float(request.args.get('lat', last_known_lat))
        lon = float(request.args.get('lon', last_known_lon))
        zoom = int(request.args.get('z', 3))
        width = int(request.args.get('w', 260))
        height = int(request.args.get('h', 260))
        import io

        cache_key = (zoom, round(lat, 2), round(lon, 2))
        with ais_map_cache_lock:
            cached = ais_map_cache.get(cache_key)
        if cached and (time.time() - cached["time"] < 60):  # 60-second cache
            resp = make_response(cached["img"])
            resp.headers.set('Content-Type', 'image/jpeg')
            resp.headers.set('Content-Length', str(len(cached["img"])))
            return resp

        img = fetch_ais_map(lat, lon, zoom, width, height)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=45, optimize=True)
        img_bytes = buf.getvalue()
        with ais_map_cache_lock:
            ais_map_cache[cache_key] = {"time": time.time(), "img": img_bytes}
        resp = make_response(img_bytes)
        resp.headers.set('Content-Type', 'image/jpeg')
        resp.headers.set('Content-Length', str(len(img_bytes)))
        return resp
    except Exception as e:
        import traceback
        log(f"AIS Map Error: {e}\n{traceback.format_exc()}")
        return "Err", 500

rv_cache = {"path": None, "time": 0}
def get_rainviewer_path():
    import time, json, urllib.request
    global rv_cache
    if rv_cache["path"] is None or time.time() - rv_cache["time"] > 600:
        try:
            req = urllib.request.Request("https://api.rainviewer.com/public/weather-maps.json", headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode())
                rv_cache["path"] = data['radar']['past'][-1]['path']
                rv_cache["time"] = time.time()
        except:
            return None
    return rv_cache["path"]

def fetch_stitched_map(lat, lon, z, width, height):
    from PIL import Image, ImageDraw, ImageEnhance
    import math, urllib.request, io

    # Both CartoDB and RainViewer use the SAME zoom level.
    # RainViewer empirically supports up to z=7 for Australia; CartoDB supports up to z=18.
    # We cap at 7 so tile coords are always valid for both APIs.
    z = max(2, min(z + 2, 7))   # incoming z 1-5 Ã¢â€ â€™ uses CartoDB+RainViewer z 3-7

    n   = 2.0 ** z
    x   = ((lon + 180.0) / 360.0) * n
    y   = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    ctx, cty = int(x), int(y)
    ox,  oy  = int((x - ctx) * 256), int((y - cty) * 256)

    canvas  = Image.new('RGB', (256 * 3, 256 * 3), (0, 0, 0))
    headers = {'User-Agent': 'Pyxis-Tactical/1.0'}
    rv_path = get_rainviewer_path()

    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            tx, ty = (ctx + dx) % int(n), cty + dy
            if not (0 <= ty < int(n)):
                continue
            # --- CartoDB basemap ---
            try:
                req = urllib.request.Request(
                    f"https://a.basemaps.cartocdn.com/dark_all/{z}/{tx}/{ty}.png",
                    headers=headers)
                with urllib.request.urlopen(req, timeout=3) as resp:
                    tile = Image.open(io.BytesIO(resp.read())).convert('RGB')
            except Exception:
                tile = Image.new('RGB', (256, 256), (5, 10, 20))

            # --- RainViewer radar overlay ---
            if rv_path:
                try:
                    url_rv = (f"https://tilecache.rainviewer.com{rv_path}"
                              f"/256/{z}/{tx}/{ty}/2/1_1.png")
                    req_rv = urllib.request.Request(url_rv, headers=headers)
                    with urllib.request.urlopen(req_rv, timeout=2) as resp_rv:
                        rv_tile = Image.open(io.BytesIO(resp_rv.read())).convert('RGBA')
                        tile.paste(rv_tile, (0, 0), rv_tile)
                except Exception:
                    pass  # No radar for this tile Ã¢â‚¬â€ map-only is fine

            canvas.paste(tile, ((dx + 1) * 256, (dy + 1) * 256))

    px, py = 256 + ox, 256 + oy
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((px-5,  py-5,  px+5,  py+5),  fill=(0,255,0), outline=(255,255,255), width=2)
    draw.ellipse((px-40, py-40, px+40, py+40), outline=(0,150,50), width=2)
    draw.ellipse((px-80, py-80, px+80, py+80), outline=(0,150,50), width=2)
    draw.line((px-10, py,    px+10, py),    fill=(255,0,0), width=2)
    draw.line((px,    py-10, px,    py+10), fill=(255,0,0), width=2)

    final_img = canvas.crop((px - width//2, py - height//2,
                              px + width//2, py + height//2))
    return ImageEnhance.Contrast(final_img).enhance(1.2)


# --- Weather pre-warm cache ---
weather_cache = {}          # { zoom: {"time":..., "img":bytes, "lat":..., "lon":...} }
weather_cache_lock = threading.Lock()

def weather_prewarm_worker():
    """Background thread: pre-generates radar maps at zoom levels 4, 6, 8 every 5 minutes."""
    import io
    PREWARM_ZOOMS = [1, 2, 3, 6, 7, 8, 9, 10]  # confirmed-working zoom levels
    time.sleep(10)  # Wait for proxy to fully initialise before first run
    while True:
        try:
            for z in PREWARM_ZOOMS:
                try:
                    lat = last_known_lat or -38.487
                    lon = last_known_lon or 145.620
                    img = fetch_stitched_map(lat, lon, z, 260, 260)
                    buf = io.BytesIO()
                    img.save(buf, format='JPEG', quality=35, optimize=True)
                    img_bytes = buf.getvalue()
                    with weather_cache_lock:
                        weather_cache[z] = {
                            "time": time.time(),
                            "img": img_bytes,
                            "lat": round(lat, 1),
                            "lon": round(lon, 1)
                        }
                    log(f"WX Pre-warm: z={z} -> {len(img_bytes)} bytes")
                except Exception as e:
                    import traceback
                    log(f"WX Pre-warm z={z} failed: {e}\n{traceback.format_exc()}")
        except Exception as e:
            log(f"WX Pre-warm worker error: {e}")
        time.sleep(290)  # Re-generate every ~5 minutes

threading.Thread(target=weather_prewarm_worker, daemon=True).start()

@app.route('/weather_radar')
@app.route('/weather_radar/<path:dummy>')
def weather_radar_endpoint(dummy=None):
    """Serves pre-cached radar map. Falls back to live generation if cache is cold."""
    import io
    z = int(request.args.get('z', 6))
    z = max(4, min(z, 8))  # Clamp to pre-warm range
    try:
        with weather_cache_lock:
            cached = weather_cache.get(z)
        if cached and (time.time() - cached["time"] < 360):
            resp = make_response(cached["img"])
            resp.headers.set('Content-Type', 'image/jpeg')
            resp.headers.set('Content-Length', str(len(cached["img"])))
            return resp
        # Cache miss Ã¢â‚¬â€ generate on-demand
        lat = last_known_lat or -38.487
        lon = last_known_lon or 145.620
        img = fetch_stitched_map(lat, lon, z, 260, 260)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=35, optimize=True)
        img_bytes = buf.getvalue()
        with weather_cache_lock:
            weather_cache[z] = {"time": time.time(), "img": img_bytes, "lat": round(lat, 1), "lon": round(lon, 1)}
        resp = make_response(img_bytes)
        resp.headers.set('Content-Type', 'image/jpeg')
        resp.headers.set('Content-Length', str(len(img_bytes)))
        return resp
    except Exception as e:
        log(f"weather_radar endpoint error: {e}")
        return "Err", 500

@app.route('/ais_basemap/<int:radius_nm>')
@app.route('/ais_basemap/<int:radius_nm>/<path:coords>')
def ais_basemap_endpoint(radius_nm=500, coords=''):
    """Heading-up CartoDB tile basemap for the AIS radar view.
    radius_nm : radar radius in nautical miles
    coords    : optional path 'lat/lon/heading/cachebuster'
    Returns a 454x454 JPEG rotated so vessel heading points up.
    """
    import io as _io, json as _j, math as _m, urllib.request
    from PIL import Image, ImageEnhance

    IMG_SIZE = 454
    TILE     = 256

    # --- CartoDB zoom by radar radius ---
    if   radius_nm <= 50:   carto_z = 11
    elif radius_nm <= 150:  carto_z = 9
    elif radius_nm <= 600:  carto_z = 7
    elif radius_nm <= 1500: carto_z = 6
    else:                   carto_z = 5

    try:
        # --- Position + heading: path segments (lat/lon/heading/cb) take priority ---
        lat, lon, heading = None, None, 0.0
        parts = [p for p in coords.split('/') if p]
        if len(parts) >= 2:
            try:
                lat     = float(parts[0])
                lon     = float(parts[1])
                heading = float(parts[2]) if len(parts) >= 3 else 0.0
                # validate not 0/0
                if lat == 0.0 and lon == 0.0:
                    lat = lon = None
            except Exception:
                lat = lon = None

        if lat is None:
            # Fallback: read from sim_telemetry.json
            telem = os.path.join(B, 'sim_telemetry.json')
            if os.path.exists(telem):
                td  = _j.load(open(telem))
                blat = td.get('BOAT_LAT') or td.get('lat')
                blon = td.get('BOAT_LON') or td.get('lon')
                if blat and blon:
                    lat = float(blat); lon = float(blon)
                    heading = float(td.get('heading') or 0)

        if not lat or not lon:
            log('ais_basemap: no valid vessel position Ã¢â‚¬â€ returning 503')
            return 'No Pyxis position data', 503

        # --- Tile maths (Web Mercator) ---
        n      = 2.0 ** carto_z
        cx_f   = ((lon + 180.0) / 360.0) * n
        cy_f   = (1.0 - _m.asinh(_m.tan(_m.radians(lat))) / _m.pi) / 2.0 * n
        ctx, cty = int(cx_f), int(cy_f)

        headers = {'User-Agent': 'Pyxis-AIS/2.0'}
        def _tile(z, tx, ty):
            url = f'https://a.basemaps.cartocdn.com/dark_all/{z}/{tx%int(n)}/{ty}.png'
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=6) as r:
                    return Image.open(_io.BytesIO(r.read())).convert('RGB')
            except Exception:
                return Image.new('RGB', (256, 256), (5, 10, 20))

        # Build 3Ãƒâ€”3 tile canvas
        canvas = Image.new('RGB', (TILE * 3, TILE * 3), (5, 10, 20))
        for drow in range(-1, 2):
            for dcol in range(-1, 2):
                ty = cty + drow
                if 0 <= ty < int(n):
                    tile = _tile(carto_z, ctx + dcol, ty)
                    canvas.paste(tile, ((dcol + 1) * TILE, (drow + 1) * TILE))

        # Vessel pixel on canvas
        vx = int((cx_f - (ctx - 1)) * TILE)
        vy = int((cy_f - (cty - 1)) * TILE)

        # Crop and enhance
        half  = IMG_SIZE // 2
        left  = max(0, min(vx - half, TILE * 3 - IMG_SIZE))
        top   = max(0, min(vy - half, TILE * 3 - IMG_SIZE))
        final = canvas.crop((left, top, left + IMG_SIZE, top + IMG_SIZE))
        final = ImageEnhance.Contrast(final).enhance(1.15)
        final = ImageEnhance.Sharpness(final).enhance(1.25)

        # Rotate heading-up (CCW by heading degrees = vessel heading points up)
        if abs(heading) > 0.5:
            final = final.rotate(heading, fillcolor=(5, 10, 20), expand=False)

        buf = _io.BytesIO()
        final.save(buf, format='JPEG', quality=75, optimize=True)
        resp = make_response(buf.getvalue())
        resp.headers.set('Content-Type',   'image/jpeg')
        resp.headers.set('Content-Length', str(len(buf.getvalue())))
        resp.headers.set('Cache-Control',  'no-cache')
        return resp
    except Exception as e:
        log(f'ais_basemap error: {e}')
        return 'Err', 500

# --- Tile basemap layer cache (keyed by (zoom, tile_x, tile_y)) ---
_tile_layer_cache = {}   # (zoom, tile_x, tile_y) -> PIL Image
_tile_layer_ts    = {}   # (zoom, tile_x, tile_y) -> timestamp

def _fetch_carto_tile(z, tx, ty, n):
    """Fetch a CartoDB dark basemap tile, caching in memory for 5 min."""
    import io as _io, urllib.request, time as _time
    key = (z, int(tx) % int(n), int(ty))
    if key in _tile_layer_cache and (_time.time() - _tile_layer_ts.get(key, 0)) < 300:
        return _tile_layer_cache[key]
    from PIL import Image
    url = f'https://a.basemaps.cartocdn.com/dark_all/{z}/{key[1]}/{key[2]}.png'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Pyxis-AIS/2.0'})
        with urllib.request.urlopen(req, timeout=6) as r:
            img = Image.open(_io.BytesIO(r.read())).convert('RGB')
    except Exception:
        img = Image.new('RGB', (256, 256), (5, 10, 20))
    _tile_layer_cache[key] = img
    _tile_layer_ts[key]    = _time.time()
    return img

@app.route('/ais_radar_map')
@app.route('/ais_radar_map/<int:radius_nm>')
@app.route('/ais_radar_map/<int:radius_nm>/<path:extra>')
def ais_radar_map_endpoint(radius_nm=500, extra=''):
    """Server-side composited AIS radar map Ã¢â‚¬â€ tiles + contacts in one JPEG.
    Uses last_known_lat/lon (always current) and live_ais_cache (live AIS).
    Path: /ais_radar_map/<radius_nm>/<optional_heading>/<cachebuster>
    """
    import io as _io, math as _m, time as _time
    from PIL import Image, ImageDraw, ImageEnhance
    global last_known_lat, last_known_lon, live_ais_cache

    IMG_SIZE = 454
    TILE     = 256

    # Parse heading from extra path (first segment if numeric)
    vessel_heading = 0.0
    parts = [p for p in extra.split('/') if p]
    try:
        if parts and parts[0].replace('.','',1).replace('-','',1).isdigit():
            vessel_heading = float(parts[0])
    except Exception:
        pass

    # CartoDB zoom from radius
    if   radius_nm <= 15:   carto_z = 12
    elif radius_nm <= 50:   carto_z = 11
    elif radius_nm <= 150:  carto_z = 9
    elif radius_nm <= 600:  carto_z = 7
    elif radius_nm <= 1500: carto_z = 6
    else:                   carto_z = 5

    try:
        lat, lon = last_known_lat, last_known_lon
        n   = 2.0 ** carto_z

        # Vessel tile position
        cx_f = ((lon + 180.0) / 360.0) * n
        cy_f = (1.0 - _m.asinh(_m.tan(_m.radians(lat))) / _m.pi) / 2.0 * n
        ctx, cty = int(cx_f), int(cy_f)

        # Build 3Ãƒâ€”3 tile canvas
        canvas = Image.new('RGB', (TILE * 3, TILE * 3), (5, 10, 20))
        for drow in range(-1, 2):
            for dcol in range(-1, 2):
                ty_idx = cty + drow
                if 0 <= ty_idx < int(n):
                    tile = _fetch_carto_tile(carto_z, ctx + dcol, ty_idx, n)
                    canvas.paste(tile, ((dcol + 1) * TILE, (drow + 1) * TILE))

        # Crop centred on vessel
        vx   = int((cx_f - (ctx - 1)) * TILE)
        vy   = int((cy_f - (cty - 1)) * TILE)
        half = IMG_SIZE // 2
        left = max(0, min(vx - half, TILE * 3 - IMG_SIZE))
        top  = max(0, min(vy - half, TILE * 3 - IMG_SIZE))
        final = canvas.crop((left, top, left + IMG_SIZE, top + IMG_SIZE))
        final = ImageEnhance.Contrast(final).enhance(1.15)

        # --- Draw AIS contacts ---
        draw      = ImageDraw.Draw(final)
        cx_img    = vx - left   # vessel pixel in cropped image
        cy_img    = vy - top
        nm_per_px = radius_nm / half  # nautical miles per pixel

        # Degree per nm (approx)
        lat_per_nm = 1.0 / 60.0
        lon_per_nm = 1.0 / (60.0 * _m.cos(_m.radians(lat)))

        # Draw all live AIS contacts within radius
        contacts_drawn = 0
        with_cache = dict(live_ais_cache)  # snapshot
        for mmsi, c in with_cache.items():
            c_lat = c.get('lat', 0)
            c_lon = c.get('lon', 0)
            c_sog = c.get('speed', 0)
            c_cog = c.get('heading', 0)

            dlat_nm = (c_lat - lat) / lat_per_nm
            dlon_nm = (c_lon - lon) / lon_per_nm
            dist_nm = _m.sqrt(dlat_nm**2 + dlon_nm**2)
            if dist_nm > radius_nm * 1.1:
                continue

            # Pixel offset from vessel centre (north-up for now, rotate later)
            px = cx_img + int(dlon_nm / nm_per_px)
            py = cy_img - int(dlat_nm / nm_per_px)

            if not (5 <= px <= IMG_SIZE - 5 and 5 <= py <= IMG_SIZE - 5):
                continue

            # Colour by speed
            if c_sog > 15:   col = (255, 80, 80)   # fast Ã¢â‚¬â€ red
            elif c_sog > 5:  col = (80, 220, 80)   # moving Ã¢â‚¬â€ green
            else:            col = (80, 150, 255)   # slow/stationary Ã¢â‚¬â€ blue

            draw.ellipse((px - 4, py - 4, px + 4, py + 4), fill=col, outline=(255, 255, 255))

            # Heading arrow
            if c_sog > 0.5:
                arrow_len = max(6, min(18, int(c_sog * 1.2)))
                ax = px + int(_m.sin(_m.radians(c_cog)) * arrow_len)
                ay = py - int(_m.cos(_m.radians(c_cog)) * arrow_len)
                draw.line([(px, py), (ax, ay)], fill=(255, 255, 200), width=1)

            contacts_drawn += 1
            if contacts_drawn >= 80:
                break

        # Draw vessel marker (white triangle)
        draw.polygon([(cx_img, cy_img - 8), (cx_img - 5, cy_img + 5),
                      (cx_img + 5, cy_img + 5)], fill=(255, 255, 255))

        # Contact count label
        draw.text((4, 4), f'{contacts_drawn} AIS', fill=(0, 255, 140))

        # Rotate heading-up
        if abs(vessel_heading) > 0.5:
            final = final.rotate(vessel_heading, fillcolor=(5, 10, 20), expand=False)

        buf = _io.BytesIO()
        final.save(buf, format='JPEG', quality=78, optimize=True)
        resp = make_response(buf.getvalue())
        resp.headers.set('Content-Type',   'image/jpeg')
        resp.headers.set('Content-Length', str(len(buf.getvalue())))
        resp.headers.set('Cache-Control',  'no-cache, max-age=0')
        return resp
    except Exception as e:
        log(f'ais_radar_map error: {e}')
        import traceback; log(traceback.format_exc())
        return 'Err', 500

@app.route('/debug_meteo_cache')
def debug_meteo_cache():
    """Dumps raw meteo_cache.json for diagnosis Ã¢â‚¬â€ remove after testing."""
    import json as _j
    paths = {
        'meteo_cache': os.path.join(B, 'meteo_cache.json'),
        'sim_telemetry': os.path.join(B, 'sim_telemetry.json'),
    }
    out = {}
    for name, path in paths.items():
        if os.path.exists(path):
            try:
                with open(path) as f:
                    out[name] = _j.load(f)
            except Exception as e:
                out[name] = f"ERROR: {e}"
        else:
            out[name] = "FILE_NOT_FOUND"
    resp = make_response(_j.dumps(out, indent=2))
    resp.headers.set('Content-Type', 'application/json')
    return resp

@app.route('/refresh_seastate')
@app.route('/refresh_seastate/<path:dummy>')
def refresh_seastate_endpoint(dummy=None):
    """Kill and restart marine_map_gen immediately Ã¢â‚¬â€ forces map regen at current vessel position."""
    import subprocess, json as _j
    try:
        subprocess.run(['pkill', '-9', '-f', 'marine_map_gen'], capture_output=True)
        import time; time.sleep(1)
        gen = os.path.join(B, 'marine_map_gen.py')
        log = os.path.join(B, 'marine_map.log')
        subprocess.Popen(['python3', gen],
                         stdout=open(log, 'a'), stderr=subprocess.STDOUT,
                         start_new_session=True)
        return make_response(_j.dumps({"status": "ok", "msg": "marine_map_gen restarted"}))
    except Exception as e:
        return make_response(_j.dumps({"status": "error", "msg": str(e)})), 500

@app.route('/diagnostics', methods=['GET'])
@requires_auth
def sys_diagnostics():
    import subprocess, time, json as _hj
    try:
        with open('/proc/loadavg', 'r') as f:
            load = f.read().split()[:3]
    except: load = ["?", "?", "?"]
        
    try:
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
        mem = {}
        for line in lines:
            parts = line.split(':')
            if len(parts) == 2:
                mem[parts[0].strip()] = int(parts[1].strip().split()[0])
        total = mem.get("MemTotal", 1)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        used_pct = round(100.0 - ((avail / total) * 100.0), 1)
        mem_str = f"{used_pct}% ({round((total-avail)/1024,1)}MB / {round(total/1024,1)}MB)"
    except:
        used_pct, mem_str = 0, "Unknown"

    def _is_active(name):
        try:
            r = subprocess.run(['systemctl', 'is-active', name], capture_output=True, text=True)
            return r.stdout.strip() == "active"
        except: return False

    services = {
        "manta-proxy": _is_active("manta-proxy"),
        "cmems-worker": _is_active("cmems-worker"),
        "adsb-worker": _is_active("adsb-worker")
    }

    try:
        r = subprocess.run(['pgrep', '-f', 'combined_mantasim2.py'], capture_output=True, text=True)
        services["simulator"] = bool(r.stdout.strip())
    except: services["simulator"] = False

    def _c_age(path):
        try:
            if not os.path.exists(path): return {"age_s": 99999, "status": "missing"}
            return {"age_s": int(time.time() - os.path.getmtime(path)), "status": "ok"}
        except: return {"age_s": 99999, "status": "error"}

    caches = {
        "CMEMS": _c_age(os.path.join(B, "currents_grid_cache.json")),
        "ADSB": _c_age(os.path.join(B, "adsb_cache.json")),
        "OSINT": _c_age(os.path.join(B, "meteo_cache.json")),
        "GEO": _c_age(os.path.join(B, "geo_cache.json"))
    }
    return jsonify({
        "status": "ok", "cpu_load": load, "mem_pct": used_pct, "mem_str": mem_str,
        "services": services, "caches": caches, "ts": time.time()
    })

@app.route('/restart_all', methods=['POST'])
@requires_auth
def restart_all_endpoint():
    try:
        import subprocess
        # Give a short delay to allow HTTP response to return before killing proxy
        # Delay is handled within restart_clean.sh
        subprocess.Popen(['sudo', '/home/icanjumpuddles/manta-comms/restart_clean.sh'], start_new_session=True)
        return jsonify({"status": "restarting"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health')
@app.route('/health/<path:dummy>')
def health_endpoint(dummy=None):
    """Worker health check Ã¢â‚¬â€ returns cache age + vessel position as JSON.
    Example: curl -sk https://localhost/health | python3 -m json.tool
    """
    import json as _hj
    def _age(path):
        try:
            with open(path) as f:
                d = _hj.load(f)
            # Handle list-format caches (e.g. gmdss_cache.json) Ã¢â‚¬â€ use file mtime
            if isinstance(d, list):
                age_s = time.time() - os.path.getmtime(path)
            else:
                age_s = time.time() - float(d.get("updated", 0))
            return {"age_min": round(age_s / 60, 1), "ok": age_s < 7200,
                    "count": len(d) if isinstance(d, list) else None}
        except FileNotFoundError:
            return {"age_min": None, "ok": False, "err": "missing"}
        except Exception as e:
            return {"age_min": None, "ok": False, "err": str(e)}

    caches = {
        "cmems_grid":     os.path.join(B, "currents_grid_cache.json"),
        "weather_alerts": os.path.join(B, "weather_alerts_cache.json"),
        "meteo":          os.path.join(B, "meteo_cache.json"),
        "bathymetry":     os.path.join(B, "bathymetry_cache.json"),
        "seismic":        os.path.join(B, "seismic_cache.json"),
        "swpc":           os.path.join(B, "swpc_cache.json"),
        "asam":           os.path.join(B, "asam_cache.json"),
        "gmdss":          os.path.join(B, "gmdss_cache.json"),
    }
    result  = {k: _age(v) for k, v in caches.items()}
    result["vessel"]  = {"lat": last_known_lat, "lon": last_known_lon}
    result["status"]  = "OK" if all(v.get("ok") for k, v in result.items() if k not in ("vessel", "status")) else "DEGRADED"
    resp = make_response(_hj.dumps(result, indent=2))
    resp.headers.set("Content-Type", "application/json")
    return resp

@app.route('/sea_state_json')
@app.route('/sea_state_json/<path:dummy>')
def sea_state_json_endpoint(dummy=None):
    """Returns latest sea state data + Pyxis position as JSON for watch data sub-menu."""
    import json as _json
    try:
        # --- Vessel position from telemetry (latest Pyxis fix) ---
        pos_lat, pos_lon = None, None
        telem_path = os.path.join(B, "sim_telemetry.json")
        if os.path.exists(telem_path):
            try:
                with open(telem_path) as f:
                    td = _json.load(f)
                # Prefer BOAT position (Pyxis vessel) over crew/watch position
                pos_lat = td.get("BOAT_LAT") or td.get("lat") or td.get("latitude")
                pos_lon = td.get("BOAT_LON") or td.get("lon") or td.get("longitude")
            except Exception:
                pass

        # --- Sea state data from caches ---
        marine = {}
        cmems_updated = 0   # epoch ts; used for stale-data detection
        cmems_path = os.path.join(B, "currents_grid_cache.json")
        if os.path.exists(cmems_path):
            try:
                with open(cmems_path) as f:
                    cd = _json.load(f)
                marine = cd.get("vessel_wave", {}).copy()
                marine["time"] = cd.get("updated_str", "unknown")
                cmems_updated  = float(cd.get("updated", 0))   # epoch timestamp
                # curr_v / curr_dir are pre-computed in vessel_wave by cmems_worker v2.0
                # (nearest-ocean fallback already applied Ã¢â‚¬â€ no pts scan needed)
            except: pass

        if not marine:
            meteo_path = os.path.join(B, "meteo_cache.json")
            if os.path.exists(meteo_path):
                try:
                    with open(meteo_path) as f:
                        meteo = _json.load(f)
                    marine = meteo.get("marine", {})
                except: pass

        def _fmt_dir(deg):
            """Convert degrees to cardinal + numeric string."""
            if deg is None or deg == "n/a": return "n/a"
            cardinals = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                         "S","SSW","SW","WSW","W","WNW","NW","NNW"]
            try:
                idx = int((float(deg) + 11.25) / 22.5) % 16
                return f"{cardinals[idx]} ({int(float(deg))} deg)"
            except: return "n/a"

        def _fv(v, unit="", dec=1):
            if v is None or v == "" or v == "n/a": return "n/a"
            try: return f"{float(v):.{dec}f}{unit}"
            except: return str(v)

        depth = marine.get("depth") # might be in meteo_cache root too

        # Pull extra fields from telemetry (wind, heading, sea_state score)
        td = {}
        if os.path.exists(telem_path):
            try:
                with open(telem_path) as f:
                    td = _json.load(f)
            except Exception: pass

        # Crew (watch) position Ã¢â‚¬â€ separate from vessel position
        crew_lat = td.get("CREW_LAT") or td.get("lat")
        crew_lon = td.get("CREW_LON") or td.get("lon")

        # VesselÃ¢â‚¬â€œcrew separation in nautical miles
        sep_nm = "n/a"
        try:
            if pos_lat and pos_lon and crew_lat and crew_lon:
                import math as _m
                dlat = _m.radians(float(pos_lat) - float(crew_lat))
                dlon = _m.radians(float(pos_lon) - float(crew_lon))
                a = (_m.sin(dlat/2)**2 +
                     _m.cos(_m.radians(float(crew_lat))) *
                     _m.cos(_m.radians(float(pos_lat))) *
                     _m.sin(dlon/2)**2)
                sep_nm = f"{3440.065 * 2 * _m.atan2(_m.sqrt(a), _m.sqrt(1-a)):.1f}nm"
        except Exception: pass

        # Load Grounded Alerts Cache (merged into results later)
        alerts = {}
        try:
            with open(os.path.join(B, "weather_alerts_cache.json")) as f:
                alerts = _json.load(f)
        except Exception: pass

        # Map fields (support both CMEMS and Open-Meteo keys)
        # NOTE: use explicit None checks Ã¢â‚¬â€ 'or' treats 0.0 (e.g. north current) as falsy
        # Also treat the string "n/a" as missing (old proxy writes may have poisoned the cache)
        def _valid(v): return v if (v is not None and v != "n/a") else None
        def _pick(a, b): return _valid(marine.get(a)) if _valid(marine.get(a)) is not None else _valid(marine.get(b))
        w_h   = _pick("wave_h",   "wave_height")
        w_d   = _pick("wave_dir", "wave_direction")
        s_h   = _pick("swell_h",  "swell_wave_height")
        s_d   = _pick("swell_dir","swell_wave_direction")
        c_v   = _pick("curr_v",   "ocean_current_velocity")
        c_d   = _pick("curr_dir", "ocean_current_direction")
        # cmems_worker stores curr_dir as oceanographic TO direction (atan2(u,v)).
        # All other directions (wave, swell, wind-wave) use meteorological FROM convention.
        # Flip +180 here so curr_dir is FROM convention — consistent with the rest,
        # and correct for the Wave Rose which applies +180 to all vectors before drawing.
        if c_d is not None:
            try: c_d = (float(c_d) + 180.0) % 360.0
            except Exception: c_d = None

        # New CMEMS Wind Wave + SST
        ww_h  = marine.get("wind_wave_h")
        ww_d  = marine.get("wind_wave_dir")
        sst   = marine.get("sst_c")

        # --- CMEMS data age and stale detection ---
        if cmems_updated > 0:
            cmems_age_s   = time.time() - cmems_updated
            cmems_age_min = round(cmems_age_s / 60, 1)
            cmems_stale   = cmems_age_s > 90 * 60          # stale if > 90 min
            age_str       = f"{cmems_age_min:.0f} min ago"
        else:
            cmems_age_s, cmems_age_min = float('inf'), None
            cmems_stale = True
            age_str     = "never"
        no_wave_data = not any([w_h, w_d, c_v])            # all key fields missing

        # Build warnings string (single block)
        warn_list = alerts.get('alerts', [])
        warn_txt  = '; '.join([f"{a.get('type')}: {a.get('text')}" for a in warn_list]) if warn_list else 'NONE'

        # Prepend stale-data notice so it shows on watch immediately
        if cmems_stale or no_wave_data:
            stale_pfx = f"CMEMS UPDATING ({age_str})"
            warn_txt  = f"{stale_pfx}; {warn_txt}" if warn_txt != 'NONE' else stale_pfx

        # Wind: prefer real-time Open-Meteo forecast (meteo_cache["wind"]), fall back to sim telemetry
        _mw = {}
        try:
            with open(os.path.join(B, 'meteo_cache.json')) as _mf:
                _mc = _json.load(_mf)
            _mw = _mc.get('wind', {})
        except Exception:
            pass
        _wind_sp  = _mw.get('speed_kn') if _mw.get('speed_kn') is not None else td.get('wind_speed')
        _wind_dir = _mw.get('dir_deg')  if _mw.get('dir_deg')  is not None else (td.get('wind_dir_deg') or td.get('wind_dir'))

        result = {
            'pyxis_lat':   _fv(pos_lat,   ' deg', 4),
            'pyxis_lon':   _fv(pos_lon,   ' deg', 4),
            'vessel_lat':  _fv(pos_lat,   ' deg', 4),
            'vessel_lon':  _fv(pos_lon,   ' deg', 4),
            'crew_lat':    _fv(crew_lat,  ' deg', 4),
            'crew_lon':    _fv(crew_lon,  ' deg', 4),
            'separation':  sep_nm,
            'wave_h':      _fv(w_h, 'm'),
            'wave_dir':    _fmt_dir(w_d),
            'wave_dir_deg': w_d,
            'wave_period': _fv(marine.get('wave_period'), 's'),
            'swell_h':     _fv(s_h, 'm'),
            'swell_dir':   _fmt_dir(s_d),
            'swell_dir_deg': s_d,
            'curr_v':      _fv(c_v, 'kn'),
            'curr_dir':    _fmt_dir(c_d),
            'curr_dir_deg': c_d,
            'wind_sp':     _fv(_wind_sp, 'kn'),
            'wind_dir':    _fmt_dir(_wind_dir),
            'wind_dir_deg': _wind_dir,
            'wind_ww_h':   _fv(ww_h, 'm'),
            'wind_ww_dir': _fmt_dir(ww_d),
            'wind_ww_deg': ww_d,
            'sst_c':       _fv(sst, ' C'),
            'warnings':    warn_txt,
            'cmems_stale': cmems_stale,
            'data_age_min': cmems_age_min,
            'heading':     _fv(td.get('heading'), ' deg', 0),
            'sog':         _fv(td.get('SOG') or td.get('speed_kn'), 'kn'),
            'depth':       _fv(depth, 'm'),
            'sea_state':   td.get('sea_state_score') or td.get('sea_state') or 'n/a',
            'updated':     marine.get('time', 'unknown'),
        }
        resp = make_response(_json.dumps(result))
        resp.headers.set('Content-Type', 'application/json')
        return resp
    except Exception as e:
        return make_response(_json.dumps({"error": str(e)}), 500)

@app.route('/sea_state_grid')
@app.route('/sea_state_grid/<path:dummy>')
def sea_state_grid_endpoint(dummy=None):
    """Returns a 5x5 grid of sea state vectors centered on the vessel."""
    import json as _json, os
    B = os.environ.get("B", "/home/icanjumpuddles/manta-comms")
    try:
        # Get center pos
        with open(os.path.join(B, "sim_telemetry.json")) as f:
            t = _json.load(f)
        lat = float(t.get("BOAT_LAT") or t.get("lat") or 0)
        lon = float(t.get("BOAT_LON") or t.get("lon") or 0)
        
        # Load CMEMS cache
        with open(os.path.join(B, "currents_grid_cache.json")) as f:
            cd = _json.load(f)
        pts = cd.get("points", [])
        
        # Create 5x5 grid nodes (+/- 8nm)
        nodes = []
        step = 4.0 / 60.0 # 4nm spacing
        for r in range(-2, 3):
            for c in range(-2, 3):
                glat = lat + r * step
                glon = lon + c * step
                
                # Find nearest data point in cache
                best_d = 0.01 # about 6nm
                best_p = None
                for p in pts:
                    d = (p['lat']-glat)**2 + (p['lon']-glon)**2
                    if d < best_d:
                        best_d = d
                        best_p = p
                
                if best_p:
                    nodes.append({
                        "x": c, "y": r, # Grid coords for watch
                        "wh": round(best_p.get("wave_h", 0) or 0, 2),
                        "wd": round(best_p.get("wave_dir", 0) or 0, 1),
                        "cv": round(best_p.get("speed_kn", 0) or 0, 2),
                        "cd": round(best_p.get("dir_deg", 0) or 0, 1)
                    })
        
        resp = make_response(_json.dumps({"nodes": nodes, "lat": lat, "lon": lon}))
        resp.headers.set('Content-Type', 'application/json')
        return resp
    except Exception as e:
        return make_response(_json.dumps({"error": str(e)}), 500)

@app.route('/meteo_map')
@app.route('/meteo_map/<path:dummy>')
@app.route('/seastate/<cb>/meteo_map')
@app.route('/seastate/<cb>/z<int:zoom>/meteo_map')
@app.route('/seastate/<path:dummy2>')
def meteo_map_endpoint(dummy=None, cb=None, zoom=1, dummy2=None):
    """Serves the sea-state map JPEG generated by marine_map_gen.py v2."""
    import io
    # Try native per-zoom file first, then z1, then generate placeholder
    candidates = [
        os.path.join(B, f"meteo_map_z{int(zoom)}.jpg"),
        os.path.join(B, "meteo_map_z1.jpg"),
        os.path.join(B, "meteo_map.jpg"),
    ]
    try:
        img_bytes = None
        for path in candidates:
            if os.path.exists(path):
                with open(path, 'rb') as f:
                    img_bytes = f.read()
                break

        if img_bytes is None:
            # Generator hasn't run yet Ã¢â‚¬â€ synthetic placeholder
            from PIL import Image, ImageDraw
            img = Image.new('RGB', (454, 454), (5, 10, 20))
            d   = ImageDraw.Draw(img)
            d.ellipse((207, 207, 247, 247), outline=(0, 200, 80), width=2)
            d.ellipse((167, 167, 287, 287), outline=(0, 100, 40), width=1)
            d.line([(217, 227), (237, 227)], fill=(255, 50, 50), width=2)
            d.line([(227, 217), (227, 237)], fill=(255, 50, 50), width=2)
            d.text((140, 300), "SEA STATE", fill=(0, 180, 120))
            d.text((120, 316), "Generating map...", fill=(100, 180, 255))
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=80)
            img_bytes = buf.getvalue()

        resp = make_response(img_bytes)
        resp.headers.set('Content-Type', 'image/jpeg')
        resp.headers.set('Content-Length', str(len(img_bytes)))
        return resp
    except Exception as e:
        log(f"meteo_map endpoint error: {e}")
        return "Err", 500


@app.route('/weather_map.jpg')
@app.route('/weather_map/<int:z_param>.jpg')
@app.route('/weather_map_v2/<int:z_param>.jpg')
def weather_map(z_param=None, cb=None):
    """
    Returns a color map image optimized for Garmin's makeImageRequest.
    """
    try:
        from PIL import Image, ImageEnhance, ImageOps, ImageDraw, ImageFont
        import io
    except ImportError:
        return jsonify({"error": "Pillow framework not installed on proxy."}), 500

    src = request.args.get('source', 'synoptic')
    width = int(request.args.get('w', 260))
    height = int(request.args.get('h', 260))
    z = z_param if z_param is not None else int(request.args.get('z', 1))

    img_safe_transit = None
    try:
        if src == 'synoptic':
            # Bypass archaic static GIFs entirely and dynamically composite the RainViewer radar overlay
            # onto a 3x3 high-definition CartoDB map grid securely generated natively in Python Pillow.
            img_safe_transit = fetch_stitched_map(last_known_lat, last_known_lon, z, width, height)
            
    except Exception as e:
        log(f"MAP ERROR: {e}")

    if img_safe_transit is None:
        # Generate an absolute tactical synthetic radar fallback since the BOM CDN bans Cloud providers
        import random
        img_safe_transit = Image.new('RGB', (width, height), color=(2, 10, 16)) # Pitch Navy
        draw = ImageDraw.Draw(img_safe_transit)
        # Dynamic sweeping grid and radar layers
        for r in [0.2, 0.4, 0.6, 0.8]:
            rad = width * r
            draw.ellipse((width/2 - rad/2, height/2 - rad/2, width/2 + rad/2, height/2 + rad/2), outline=(0, 160, 80), width=1)
        draw.line((width/2, 0, width/2, height), fill=(0, 200, 100), width=1) # Y Cross
        draw.line((0, height/2, width, height/2), fill=(0, 200, 100), width=1) # X Cross
        # Plot Pyxis Target
        draw.ellipse((width/2 - 4, height/2 - 4, width/2 + 4, height/2 + 4), fill=(0, 255, 0)) # Green dot
        draw.ellipse((width/2 - 8, height/2 - 8, width/2 + 8, height/2 + 8), outline=(0, 255, 100), width=2) # Ring
        # Scatter some synthetic maritime contacts
        for _ in range(8):
            ox = random.randint(30, width-30)
            oy = random.randint(30, height-30)
            draw.rectangle([ox-2, oy-2, ox+2, oy+2], fill=(0, 150, 255)) # Cyan contacts
        font = ImageFont.load_default()
        draw.text((10, height - 20), "SYNTHETIC: NOME - " + str(int(time.time()))[:6], fill=(0, 255, 100), font=font)

    buf = io.BytesIO()
    img_safe_transit.save(buf, format='JPEG', quality=25, optimize=True)
    img_data = buf.getvalue()
    response = make_response(img_data)
    response.headers.set('Content-Type', 'image/jpeg')
    
    # KNOWN GARMIN API BUG: The Garmin Connect AWS proxy aggressively drops 'Chunked' image transfers.
    # We must rigorously calculate the buffer byte-length and force the HTTP Content-Length header,
    # otherwise the watch will receive a 404 proxy abort.
    response.headers.set('Content-Length', str(len(img_data)))
    return response

@app.route('/injector')
@requires_auth
def scenario_injector():
    """Web UI for injecting scenarios into the headless simulator."""
    return """
<!DOCTYPE html>
<html>
<head>
    <title>PYXIS SCENARIO COMMAND</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0" />
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body { background: #000; color: #0f0; font-family: monospace; display: flex; flex-direction: column; height: 100vh; margin: 0; }
        #map { flex: 0.55; border-bottom: 2px solid #0f0; }
        .ui { flex: 0.45; padding: 15px; overflow-y: auto; background: #010; }
        .row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
        button { background: #040; color: #0f0; border: 1px solid #0f0; padding: 12px; font-weight: bold; cursor: pointer; border-radius: 4px; }
        button:active { background: #0f0; color: #000; }
        input[type="range"] { flex: 1; margin: 0 15px; }
        select { background: #000; color: #0f0; border: 1px solid #0f0; padding: 10px; font-family: monospace; width: 100%; transition: background 0.2s;}
        select:focus { background: #030; outline: none; }
        optgroup { background: #000; color: #888; font-style: normal; font-weight: bold; }
        option { color: #0f0; padding: 5px; }
        .info { color: #0a0; font-size: 13px; margin-bottom: 10px; display: block; }
    </style>
</head>
<body>
    <div id="map"></div>
    <div class="ui">
        <div class="row" style="color:#0f0; font-weight:bold; font-size: 18px; border-bottom: 1px dashed #0a0; padding-bottom: 10px;">GLOBAL SCENARIO COMMAND</div>
        
        <span class="info">Select a real-world testing location to automatically warp the Pyxis Simulator. Navigational hazards (OpenSeaMap) will populate organically upon arrival.</span>

        <div class="row">
            <select id="geoPresets" onchange="warpToSelected()">
                <option value="-37.84,144.91" disabled selected>SELECT A GLOBAL WARP ZONE...</option>
                <optgroup label="Australian Waters">
                    <option value="-37.84,144.91">Melbourne (Port Phillip Bay)</option>
                    <option value="-38.277,144.731">Popes Eye (Port Phillip) [START]</option>
                    <option value="-38.356,144.773">Blairgowrie Marina</option>
                    <option value="-38.486,145.022">West Head (Westernport Bay)</option>
                    <option value="-39.11,146.47">Wilsons Promontory</option>
                    <option value="-33.85,151.26">Sydney Harbour (Heads)</option>
                    <option value="-16.82,146.12">Great Barrier Reef (Cairns)</option>
                    <option value="-32.06,115.74">Fremantle / Rottnest Island</option>
                    <option value="-43.14,147.74">Hobart (Storm Bay)</option>
                </optgroup>
                <optgroup label="Global Strategic Chokepoints">
                    <option value="26.56,56.25">Strait of Hormuz</option>
                    <option value="12.58,43.33">Bab el-Mandeb (Red Sea)</option>
                    <option value="29.93,32.56">Suez Canal (Gulf of Suez)</option>
                    <option value="9.14,-79.91">Panama Canal (Caribbean Side)</option>
                    <option value="1.25,103.83">Singapore Strait</option>
                    <option value="5.83,95.31">Strait of Malacca</option>
                    <option value="41.02,29.00">Bosphorus Strait</option>
                    <option value="35.97,-5.49">Strait of Gibraltar</option>
                </optgroup>
                <optgroup label="Hostile & Active Zones">
                    <option value="44.42,33.58">Black Sea (Crimea Coast)</option>
                    <option value="15.22,41.97">Red Sea (Houthi Threat Area)</option>
                    <option value="11.41,43.43">Gulf of Aden (Piracy Zone)</option>
                    <option value="9.83,115.54">South China Sea (Spratly Islands)</option>
                    <option value="25.03,121.95">Taiwan Strait</option>
                    <option value="33.56,34.82">Eastern Mediterranean (Lebanon Coast)</option>
                </optgroup>
                <optgroup label="Extreme Climates">
                    <option value="-56.98,-67.27">Cape Horn (Southern Ocean)</option>
                    <option value="-60.00,-60.00">Drake Passage</option>
                    <option value="78.22,15.62">Svalbard (Arctic Circle Sea Ice)</option>
                    <option value="53.07,-169.54">Bering Sea (Dutch Harbor)</option>
                    <option value="47.56,-52.12">Grand Banks (Iceberg Alley)</option>
                </optgroup>
                <optgroup label="European & American Seas">
                    <option value="51.01,1.48">English Channel (Dover Strait)</option>
                    <option value="54.12,6.50">North Sea (Heavy Traffic)</option>
                    <option value="25.80,-79.80">Florida Straits</option>
                    <option value="40.45,-73.80">New York Harbor Approaches</option>
                    <option value="24.45,-89.80">Gulf of Mexico (Oil Rigs)</option>
                </optgroup>
            </select>
        </div>

        <div class="row" style="margin-top: 10px;">
            <span>TARGET COORDS:</span>
            <span id="coords" style="color: #fff; font-size: 16px;">-37.8400, 144.9100</span>
        </div>
        
        <div class="row">
            <span>SEA STATE OVERRIDE:</span>
            <span id="seaOut" style="color: #fff; font-size: 16px;">1.5m</span>
        </div>
        <div class="row" style="margin-bottom: 20px;">
            <input type="range" id="seaState" min="0" max="6" step="0.5" value="1.5" oninput="document.getElementById('seaOut').innerText=this.value+'m'" />
        </div>
        
        <button style="width:100%; padding:20px; font-size:18px; margin-top:10px;" onclick="deploy()">ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â°ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ EXECUTE WARP & INJECT ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â°ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬</button>
        <button style="width:100%; padding:15px; font-size:16px; margin-top:10px; background:#005;" onclick="locateWithPyxis()">ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â°ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚ÂºÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â  LOCATE WITH PYXIS (SYNC WATCH)</button>
    </div>

    <script>
        let map, marker, crewMarker;
        let lat = -37.8400, lon = 144.9100;

        function initMap() {
            map = L.map('map', {zoomControl: false}).setView([lat, lon], 12);
            L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
                maxZoom: 19
            }).addTo(map);
            marker = L.marker([lat, lon]).addTo(map);
            crewMarker = L.circleMarker([0,0], {color: '#00f', radius: 6, fillOpacity: 0.8}).addTo(map);

            map.on('click', function(e) {
                lat = e.latlng.lat; lon = e.latlng.lng;
                updateMapPos();
                document.getElementById('geoPresets').selectedIndex = 0; // reset dropdown
            });
            
            // Fetch live proxy target on load to prevent visual reset
            fetch('/status_api').then(r=>r.json()).then(d=>{
                if(d.lat && d.lon) {
                    lat = parseFloat(d.lat);
                    lon = parseFloat(d.lon);
                    marker.setLatLng([lat, lon]);
                    map.panTo([lat, lon]);
                    updateMapPos();
                }
                if (d.CREW_LAT !== undefined && d.CREW_LON !== undefined && d.CREW_LAT !== 0) {
                    crewMarker.setLatLng([d.CREW_LAT, d.CREW_LON]);
                }
            }).catch(e=>{});
        }
        initMap();

        function updateMapPos() {
            marker.setLatLng([lat, lon]);
            map.panTo([lat, lon]);
            document.getElementById('coords').innerText = lat.toFixed(4) + ", " + lon.toFixed(4);
        }

        function warpToSelected() {
            let val = document.getElementById('geoPresets').value;
            if(val) {
                let parts = val.split(',');
                lat = parseFloat(parts[0]);
                lon = parseFloat(parts[1]);
                updateMapPos();
                map.setZoom(10);
            }
        }

        async function deploy() {
            let sst = parseFloat(document.getElementById('seaState').value);
            // No manual threats anymore, they are auto-generated from OSM
            let payload = { lat: lat, lon: lon, sea_state: sst, threats: [] };
            try {
                const res = await fetch('/update_scenario', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if(res.ok) {
                    alert('WARP SUCCESSFUL! NAV HAZARDS WILL POPULATE SHORTLY.');
                } else {
                    alert('SERVER REJECTED PAYLOAD!');
                }
            } catch(e) { alert("Deployment Error"); }
        }

        function locateWithPyxis() {
            fetch('/status_api').then(r=>r.json()).then(d=>{
                if(d.CREW_LAT !== undefined && d.CREW_LON !== undefined && d.CREW_LAT !== 0) {
                    lat = parseFloat(d.CREW_LAT);
                    lon = parseFloat(d.CREW_LON);
                    updateMapPos();
                    deploy(); // Auto deploy to the new watch coords
                } else {
                    alert('NO PHYSICAL WATCH GPS DATA AVAILABLE YET');
                }
            }).catch(e=>{alert("Failed to fetch Watch coordinates.")});
        }
    </script>
</body>
</html>
"""

# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ TOPO & NAUTICAL MAP ENDPOINTS Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
# Shared tile-mosaic renderer used by /topo_map and /nautical_map

_TILE_CACHE_DIR = os.path.join(B, "tile_cache")
os.makedirs(_TILE_CACHE_DIR, exist_ok=True)
_GDRIVE_CACHE_DIR = "/mnt/gdrive/tile_cache"
try: os.makedirs(_GDRIVE_CACHE_DIR, exist_ok=True)
except Exception: pass


# OpenTopoMap Ã¢â‚¬â€ reliable free topographic tiles with contours, terrain, labels
# (GA NM_Topo_Map_9 returned HTML error pages at 200 which PIL discards silently)
_GA_TOPO_URL    = "https://tile.opentopomap.org/{z}/{tx}/{ty}.png"
# Esri World Ocean Base Ã¢â‚¬â€ bathymetric shading, depth contours (ArcGIS row/col = ty/tx)
_OCEAN_BASE_URL = "https://services.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{ty}/{tx}"
# Esri World Ocean Reference Ã¢â‚¬â€ port names, shipping lanes, labels (transparent PNG)
_OCEAN_REF_URL  = "https://services.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Reference/MapServer/tile/{z}/{ty}/{tx}"
# OpenSeaMap seamark overlay Ã¢â‚¬â€ buoys, lights, wrecks, TSS (transparent PNG)
_SEAMARK_URL    = "https://tiles.openseamap.org/seamark/{z}/{tx}/{ty}.png"

# ── Sentinel Hub WMS — Sentinel-2 L2A True Colour ────────────────────────────
_SH_INSTANCE_ID  = "7ba55959-6207-4d1f-9ee7-f418f901bf08"
_SH_WMS_BASE     = f"https://sh.dataspace.copernicus.eu/ogc/wms/{_SH_INSTANCE_ID}"
_SH_TILE_CACHE   = os.path.join(_TILE_CACHE_DIR, "sentinel")
_SH_GDRIVE_CACHE = os.path.join(_GDRIVE_CACHE_DIR, "sentinel")
try: os.makedirs(_SH_TILE_CACHE,   exist_ok=True)
except Exception: pass
try: os.makedirs(_SH_GDRIVE_CACHE, exist_ok=True)
except Exception: pass

def _fetch_sentinel_tile(z, tx, ty):
    """
    Fetch a Sentinel-2 L2A True Colour tile (same XYZ grid as slippy maps).
    Tier 1: SSD (14-day cache).  Tier 2: GDrive.  Tier 3: Sentinel Hub WMS.
    Returns raw JPEG bytes or None on failure.
    """
    import math as _math, shutil as _sh
    cn     = f"sentinel_{z}_{tx}_{ty}.jpg"
    cp_ssd = os.path.join(_SH_TILE_CACHE,   cn)
    cp_gd  = os.path.join(_SH_GDRIVE_CACHE, cn)

    # Tier 1: SSD — valid 14 days (Sentinel revisit ~5 days, good enough)
    if os.path.exists(cp_ssd) and time.time() - os.path.getmtime(cp_ssd) < 14 * 86400:
        _mark_tile(cn, "ssd")
        with open(cp_ssd, "rb") as f: return f.read()

    # Tier 2: GDrive copy-down
    if os.path.exists(cp_gd):
        try:
            _sh.copy2(cp_gd, cp_ssd)
            _mark_tile(cn, "ssd"); _mark_tile(cn, "gdrive")
            with open(cp_ssd, "rb") as f: return f.read()
        except Exception: pass

    # Tier 3: Sentinel Hub WMS GetMap — convert tile XYZ -> WGS84 bbox
    n       = 2 ** z
    lon_w   = tx     / n * 360.0 - 180.0
    lon_e   = (tx+1) / n * 360.0 - 180.0
    lat_n   = _math.degrees(_math.atan(_math.sinh(_math.pi * (1 - 2*ty     / n))))
    lat_s   = _math.degrees(_math.atan(_math.sinh(_math.pi * (1 - 2*(ty+1) / n))))
    params  = {
        "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.3.0",
        "LAYERS": "TRUE_COLOR", "CRS": "CRS:84",
        "BBOX": f"{lon_w},{lat_s},{lon_e},{lat_n}",
        "WIDTH": "256", "HEIGHT": "256",
        "FORMAT": "image/jpeg",
        "MAXCC": "20",   # skip >20% cloud cover
    }
    try:
        r = requests.get(_SH_WMS_BASE, params=params, timeout=20,
                         headers={"User-Agent": "PyxisManta/4.1"})
        ct = r.headers.get("Content-Type", "")
        if r.status_code == 200 and ct.startswith("image"):
            with open(cp_ssd, "wb") as f: f.write(r.content)
            _mark_tile(cn, "ssd")
            try: _sh.copy2(cp_ssd, cp_gd); _mark_tile(cn, "gdrive")
            except Exception: pass
            return r.content
        else:
            log(f"Sentinel tile {z}/{tx}/{ty}: HTTP {r.status_code} {ct[:40]}")
    except Exception as e:
        log(f"Sentinel tile err {z}/{tx}/{ty}: {e}")
    return None


_topo_cache     = {}
_topo_cache_lk  = threading.Lock()
_naut_cache     = {}
_naut_cache_lk  = threading.Lock()

# ── Tile index: tracks SSD/GDrive coverage without scanning directories ───────
_TILE_INDEX_FILE = os.path.join(B, "tile_index.json")
_tile_index      = {}    # cache_name → {"ssd": bool, "gdrive": bool, "ts": float}
_tile_index_lk   = threading.Lock()

def _load_tile_index():
    global _tile_index
    try:
        if os.path.exists(_TILE_INDEX_FILE):
            with open(_TILE_INDEX_FILE) as f:
                _tile_index = json.load(f)
            log(f"Tile Index: loaded {len(_tile_index)} entries")
    except Exception as e:
        log(f"Tile Index: load error: {e}")

def _save_tile_index():
    try:
        with _tile_index_lk:
            data = dict(_tile_index)
        with open(_TILE_INDEX_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log(f"Tile Index: save error: {e}")

def _mark_tile(cache_name, tier):
    """Mark a tile filename as present on 'ssd' or 'gdrive'."""
    with _tile_index_lk:
        entry = _tile_index.get(cache_name, {})
        entry[tier] = True
        entry["ts"] = int(time.time())
        _tile_index[cache_name] = entry

_load_tile_index()

def _latlng_to_tilexy(lat, lon, z):
    import math
    n = 2 ** z
    tx = (lon + 180.0) / 360.0 * n
    lr = math.radians(lat)
    ty = (1.0 - math.log(math.tan(lr) + 1.0 / math.cos(lr)) / math.pi) / 2.0 * n
    return tx, ty

def _fetch_map_tile(url, rgba=False):
    """Fetch one tile (Tier 1: SSD, Tier 2: GDrive, Tier 3: API). Returns PIL Image."""
    from PIL import Image
    import io as _io
    import shutil
    cache_name = re.sub(r'[^\w]', '_', url)[-180:] + (".png" if rgba else ".jpg")
    cp_ssd = os.path.join(_TILE_CACHE_DIR, cache_name)
    cp_gdrive = os.path.join(_GDRIVE_CACHE_DIR, cache_name)
    
    # 1. Check Local SSD (Tier 1)
    try:
        if os.path.exists(cp_ssd) and time.time() - os.path.getmtime(cp_ssd) < (180 * 24 * 3600):
            try: os.utime(cp_ssd, None) # Update access time for local LRU
            except: pass
            _mark_tile(cache_name, "ssd")
            return Image.open(cp_ssd).convert("RGBA" if rgba else "RGB")
    except Exception as e: log(f"TILE TIER1 ERR: {e}")

    # 2. Check Google Drive (Tier 2) - Syncs down to Tier 1 instantly
    try:
        if os.path.exists(cp_gdrive):
            shutil.copy2(cp_gdrive, cp_ssd)
            _mark_tile(cache_name, "ssd")
            _mark_tile(cache_name, "gdrive")
            return Image.open(cp_ssd).convert("RGBA" if rgba else "RGB")
    except Exception as e: pass

    # 3. Fetch from API (Tier 3)
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "PyxisManta/4.1"})
        if r.status_code == 200:
            img = Image.open(_io.BytesIO(r.content)).convert("RGBA" if rgba else "RGB")
            img.save(cp_ssd)
            _mark_tile(cache_name, "ssd")
            try:
                shutil.copy2(cp_ssd, cp_gdrive)
                _mark_tile(cache_name, "gdrive")
            except: pass
            return img
        else:
            log(f"TILE {r.status_code}: {url[:100]}")
    except Exception as e:
        log(f"TILE ERR: {e} | {url[:100]}")
    return Image.new("RGBA" if rgba else "RGB", (256, 256), (12, 12, 18, 0) if rgba else (12, 12, 18))

def _render_tile_mosaic(lat, lon, z, w, h, base_tpl, overlay_tpls=None):
    """
    Stitch XYZ tiles into a wÃƒâ€”h JPEG centered on (lat, lon) at zoom z.
    Uses ThreadPoolExecutor for parallel tile fetching (~10s cold vs 2+ min sequential).
    """
    from PIL import Image, ImageDraw
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import math
    TILE_SZ = 256
    n = 2 ** z
    ctx, cty = _latlng_to_tilexy(lat, lon, z)
    ctx_i, cty_i = int(ctx), int(cty)
    off_x = int((ctx - ctx_i) * TILE_SZ)
    off_y = int((cty - cty_i) * TILE_SZ)
    pad_x = math.ceil(w / 2 / TILE_SZ)
    pad_y = math.ceil(h / 2 / TILE_SZ)
    x0, y0 = ctx_i - pad_x, cty_i - pad_y
    x1, y1 = ctx_i + pad_x, cty_i + pad_y
    cw = (x1 - x0 + 1) * TILE_SZ
    ch = (y1 - y0 + 1) * TILE_SZ

    # Build task list: (url, rgba, tpl_key, tx, ty)
    all_layers = [(base_tpl, False)] + [(t, True) for t in (overlay_tpls or [])]
    tasks = []
    for tpl, rgba in all_layers:
        for ty in range(y0, y1 + 1):
            if ty < 0 or ty >= n: continue
            for tx in range(x0, x1 + 1):
                txm = tx % n
                tasks.append((tpl.format(z=z, tx=txm, ty=ty), rgba, tpl, tx, ty))

    # Parallel fetch
    fetched = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        fmap = {ex.submit(_fetch_map_tile, url, rgba): (tpl, tx, ty)
                for url, rgba, tpl, tx, ty in tasks}
        for fut in as_completed(fmap):
            key = fmap[fut]
            try: fetched[key] = fut.result()
            except Exception: pass

    # Compose base layer
    canvas = Image.new("RGB", (cw, ch), (12, 12, 18))
    for ty in range(y0, y1 + 1):
        if ty < 0 or ty >= n: continue
        for tx in range(x0, x1 + 1):
            img = fetched.get((base_tpl, tx, ty))
            if img: canvas.paste(img, ((tx - x0) * TILE_SZ, (ty - y0) * TILE_SZ))

    # Apply overlay layers in sequence
    for ovr_tpl in (overlay_tpls or []):
        ov = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        for ty in range(y0, y1 + 1):
            if ty < 0 or ty >= n: continue
            for tx in range(x0, x1 + 1):
                img = fetched.get((ovr_tpl, tx, ty))
                if img: ov.paste(img, ((tx - x0) * TILE_SZ, (ty - y0) * TILE_SZ))
        canvas = canvas.convert("RGBA")
        canvas.alpha_composite(ov)
        canvas = canvas.convert("RGB")

    # Crop to wÃƒâ€”h centred on vessel
    cpx = (ctx_i - x0) * TILE_SZ + off_x
    cpy = (cty_i - y0) * TILE_SZ + off_y
    left = max(0, min(cpx - w // 2, cw - w))
    top  = max(0, min(cpy - h // 2, ch - h))
    out  = canvas.crop((left, top, left + w, top + h))

    # Green diamond vessel marker at centre
    draw = ImageDraw.Draw(out)
    mx, my, s = w // 2, h // 2, 5
    draw.polygon([(mx, my - s), (mx + s, my), (mx, my + s), (mx - s, my)],
                 fill=(0, 255, 0), outline=(0, 0, 0))

    # ── Position / zoom label strip (TOP - visible on watch bezel) ───────────
    label_h = 13
    draw.rectangle([0, 0, w, label_h], fill=(0, 0, 0))
    draw.text((3, 2),
              f"LAT {lat:.3f}  LON {lon:.3f}  Z{z}",
              fill=(0, 220, 255))
    return out


def _map_response(img_bytes):
    resp = make_response(img_bytes)
    resp.headers.set('Content-Type', 'image/jpeg')
    resp.headers.set('Content-Length', str(len(img_bytes)))
    return resp


# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ TILE MANAGER Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
_tile_jobs = {}  # job_id -> {status, done, total, errors}

# ── Position-triggered tile prewarm worker ────────────────────────────────────
_tile_prewarm_last_pos = (None, None)

def _tile_position_prewarm_worker():
    """
    Runs every 60s. When vessel moves >5nm, pre-fetches all topo+nautical
    tiles for z=10-13 within a 25nm radius box.
    Checks SSD first (instant), then GDrive (copy-down), then internet (throttled 5/sec).
    """
    global _tile_prewarm_last_pos
    import math, shutil as _shutil
    log("Tile Position Prewarm: worker started")
    time.sleep(30)

    while True:
        try:
            lat, lon = last_known_lat, last_known_lon
            if lat is None or lon is None:
                time.sleep(60)
                continue

            plat, plon = _tile_prewarm_last_pos
            if plat is not None:
                dlat = (lat - plat) * 60.0
                dlon = (lon - plon) * 60.0 * math.cos(math.radians(lat))
                if math.sqrt(dlat**2 + dlon**2) < 5.0:
                    time.sleep(60)
                    continue

            _tile_prewarm_last_pos = (lat, lon)
            NM_BOX   = 25.0
            deg_lat  = NM_BOX / 60.0
            deg_lon  = deg_lat / max(math.cos(math.radians(lat)), 0.01)
            sources  = [
                (_GA_TOPO_URL,    False),
                (_OCEAN_BASE_URL, False),
                (_OCEAN_REF_URL,  True),
                (_SEAMARK_URL,    True),
            ]
            ZOOMS = [10, 11, 12, 13]
            n_fetched = n_skipped = n_gd = 0

            for z in ZOOMS:
                n = 2 ** z
                tlx = max(0, int(_latlng_to_tilexy(lat + deg_lat, lon - deg_lon, z)[0]))
                trx = min(n - 1, int(_latlng_to_tilexy(lat + deg_lat, lon + deg_lon, z)[0]))
                tty = max(0, int(_latlng_to_tilexy(lat + deg_lat, lon, z)[1]))
                tby = min(n - 1, int(_latlng_to_tilexy(lat - deg_lat, lon, z)[1]))
                for tx in range(tlx, trx + 1):
                    for ty in range(tty, tby + 1):
                        for tpl, rgba in sources:
                            url    = tpl.format(z=z, tx=tx, ty=ty)
                            cn     = re.sub(r'[^\w]', '_', url)[-180:] + (".png" if rgba else ".jpg")
                            cp_ssd = os.path.join(_TILE_CACHE_DIR, cn)
                            cp_gd  = os.path.join(_GDRIVE_CACHE_DIR, cn)

                            with _tile_index_lk:
                                info = _tile_index.get(cn, {})
                            if info.get("ssd") or os.path.exists(cp_ssd):
                                _mark_tile(cn, "ssd")
                                n_skipped += 1
                                continue
                            if _GDRIVE_CACHE_DIR and os.path.exists(cp_gd):
                                try:
                                    _shutil.copy2(cp_gd, cp_ssd)
                                    _mark_tile(cn, "ssd")
                                    _mark_tile(cn, "gdrive")
                                    n_gd += 1
                                except Exception: pass
                                continue
                            try:
                                _fetch_map_tile(url, rgba)
                                n_fetched += 1
                                time.sleep(0.2)
                            except Exception: pass

            _save_tile_index()

            # ── Sentinel-2 prewarm (z=10-12, 1 req/sec for free tier) ──────────
            if _SH_INSTANCE_ID:
                sh_fetched = sh_skipped = 0
                for z in [10, 11, 12]:
                    n_z = 2 ** z
                    tlx = max(0, int(_latlng_to_tilexy(lat + deg_lat, lon - deg_lon, z)[0]))
                    trx = min(n_z - 1, int(_latlng_to_tilexy(lat + deg_lat, lon + deg_lon, z)[0]))
                    tty = max(0, int(_latlng_to_tilexy(lat + deg_lat, lon, z)[1]))
                    tby = min(n_z - 1, int(_latlng_to_tilexy(lat - deg_lat, lon, z)[1]))
                    for tx in range(tlx, trx + 1):
                        for ty in range(tty, tby + 1):
                            cn  = f"sentinel_{z}_{tx}_{ty}.jpg"
                            cp  = os.path.join(_SH_TILE_CACHE, cn)
                            if os.path.exists(cp) and time.time() - os.path.getmtime(cp) < 14 * 86400:
                                sh_skipped += 1
                                continue
                            _fetch_sentinel_tile(z, tx, ty)
                            sh_fetched += 1
                            time.sleep(1.0)  # 1 req/sec — free tier hard limit
                log(f"Sentinel Prewarm: {sh_fetched} fetched, {sh_skipped} already cached")
            log(f"Tile Prewarm: ({lat:.3f},{lon:.3f}) done — "
                f"{n_fetched} downloaded, {n_gd} from GDrive, {n_skipped} already cached")

        except Exception as e:
            log(f"Tile Prewarm Worker error: {e}")
        time.sleep(60)



# ── Sentinel-2 Satellite Map endpoint ────────────────────────────────────────
@app.route('/satellite_map')
@app.route('/satellite_map/<path:dummy>')
def satellite_map(dummy=None):
    """
    Returns a Sentinel-2 true-colour JPEG centred on vessel.
    ?z=11&w=454&h=454   — tile-stitch mode (offline-capable from SSD cache)
    ?single=1&w=454&h=454 — single WMS GetMap request (one watermark, always live)
    Default for w<=454 is single=1 (watch use case).
    """
    import io as _io, math as _math
    from PIL import Image, ImageDraw
    try:
        z      = int(request.args.get('z', 11))
        w      = int(request.args.get('w', 454))
        h      = int(request.args.get('h', 454))
        lat    = float(request.args.get('lat', last_known_lat or -38.5))
        lon    = float(request.args.get('lon', last_known_lon or 145.6))
        z      = max(6, min(z, 14))
        # single=1 explicitly, OR default for watch-sized output (≤454px)
        single = request.args.get('single', '1' if w <= 454 else '0') == '1'
        maxcc  = int(request.args.get('maxcc', 5))   # default 5% = near cloud-free

        if single:
            # ── Single WMS request covering the entire viewport ──────────────
            # Compute lat/lon bbox that covers w×h pixels at zoom z
            n      = 2.0 ** z
            tile_w = 360.0 / n                           # degrees lon per tile
            tile_h_approx = tile_w * (h / w)            # rough degrees lat
            half_lon = tile_w * w / 256 / 2.0
            half_lat = tile_h_approx / 2.0 * 1.1        # slight oversize
            lon_w = lon - half_lon
            lon_e = lon + half_lon
            lat_s = max(-85.0, lat - half_lat)
            lat_n = min(85.0,  lat + half_lat)
            params = {
                "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.3.0",
                "LAYERS": "TRUE_COLOR", "CRS": "CRS:84",
                "BBOX": f"{lon_w},{lat_s},{lon_e},{lat_n}",
                "WIDTH": str(w), "HEIGHT": str(h),
                "FORMAT": "image/jpeg", "MAXCC": str(maxcc),
            }
            r = requests.get(_SH_WMS_BASE, params=params, timeout=25,
                             headers={"User-Agent": "PyxisManta/4.1"})
            ct = r.headers.get("Content-Type", "")
            if r.status_code == 200 and ct.startswith("image"):
                img_bytes = r.content
            else:
                log(f"satellite_map single WMS: HTTP {r.status_code} {ct[:40]}")
                img_bytes = None

            # Overlay vessel marker + HUD strip
            if img_bytes:
                out = Image.open(_io.BytesIO(img_bytes)).convert("RGB")
            else:
                out = Image.new("RGB", (w, h), (12, 12, 18))
            draw = ImageDraw.Draw(out)
            mx, my, s = w // 2, h // 2, 6
            draw.polygon([(mx, my-s),(mx+s, my),(mx, my+s),(mx-s, my)],
                         fill=(0, 255, 80), outline=(0, 0, 0))
            draw.rectangle([0, 0, w, 13], fill=(0, 0, 0))
            draw.text((3, 2), f"SAT  {lat:.3f}  {lon:.3f}  Z{z}", fill=(0, 220, 255))
            buf = _io.BytesIO()
            out.save(buf, format="JPEG", quality=85)
        else:
            # ── Tile-stitch mode (offline-capable from SSD cache) ────────────
            TILE_SZ = 256
            n  = 2 ** z
            cx, cy   = _latlng_to_tilexy(lat, lon, z)
            cx_i, cy_i = int(cx), int(cy)
            off_x = int((cx - cx_i) * TILE_SZ)
            off_y = int((cy - cy_i) * TILE_SZ)
            pad  = _math.ceil(max(w, h) / 2 / TILE_SZ) + 1
            side = (2 * pad + 1) * TILE_SZ
            canvas = Image.new("RGB", (side, side), (12, 12, 18))

            from concurrent.futures import ThreadPoolExecutor
            def _load(args):
                dtx, dty = args
                tx = (cx_i + dtx) % n
                ty = cy_i + dty
                if ty < 0 or ty >= n: return dtx, dty, None
                data = _fetch_sentinel_tile(z, tx, ty)
                if data:
                    try:
                        from PIL import Image as _Im
                        import io as _io2
                        return dtx, dty, _Im.open(_io2.BytesIO(data)).convert("RGB")
                    except Exception: pass
                return dtx, dty, None

            coords = [(dtx, dty) for dty in range(-pad, pad+1) for dtx in range(-pad, pad+1)]
            with ThreadPoolExecutor(max_workers=16) as ex:
                for dtx, dty, img in ex.map(_load, coords):
                    if img:
                        canvas.paste(img, ((dtx + pad) * TILE_SZ, (dty + pad) * TILE_SZ))

            cpx  = pad * TILE_SZ + off_x
            cpy  = pad * TILE_SZ + off_y
            left = max(0, min(cpx - w // 2, side - w))
            top  = max(0, min(cpy - h // 2, side - h))
            out  = canvas.crop((left, top, left + w, top + h))
            draw = ImageDraw.Draw(out)
            mx, my, s = w // 2, h // 2, 5
            draw.polygon([(mx, my-s),(mx+s, my),(mx, my+s),(mx-s, my)],
                         fill=(0, 255, 0), outline=(0, 0, 0))
            draw.rectangle([0, 0, w, 13], fill=(0, 0, 0))
            draw.text((3, 2), f"SAT  {lat:.3f}  {lon:.3f}  Z{z}", fill=(0, 220, 255))
            buf = _io.BytesIO()
            out.save(buf, format="JPEG", quality=85)

        resp = make_response(buf.getvalue())
        resp.headers.set("Content-Type", "image/jpeg")
        resp.headers.set("Content-Length", str(len(buf.getvalue())))
        resp.headers.set("Cache-Control", "max-age=900")
        return resp
    except Exception as e:
        log(f"satellite_map error: {e}")
        return make_response(f"err: {e}", 500)
    import io as _io, math as _math
    from PIL import Image
    try:
        z   = int(request.args.get('z', 11))
        w   = int(request.args.get('w', 454))
        h   = int(request.args.get('h', 454))
        lat = float(request.args.get('lat', last_known_lat or -38.5))
        lon = float(request.args.get('lon', last_known_lon or 145.6))
        z   = max(6, min(z, 14))

        TILE_SZ = 256
        n  = 2 ** z
        cx, cy   = _latlng_to_tilexy(lat, lon, z)
        cx_i, cy_i = int(cx), int(cy)
        off_x = int((cx - cx_i) * TILE_SZ)
        off_y = int((cy - cy_i) * TILE_SZ)
        pad  = _math.ceil(max(w, h) / 2 / TILE_SZ) + 1
        side = (2 * pad + 1) * TILE_SZ
        canvas = Image.new("RGB", (side, side), (12, 12, 18))

        from concurrent.futures import ThreadPoolExecutor, as_completed
        def _load(args):
            dtx, dty = args
            tx = (cx_i + dtx) % n
            ty = cy_i + dty
            if ty < 0 or ty >= n: return dtx, dty, None
            data = _fetch_sentinel_tile(z, tx, ty)
            if data:
                try:
                    from PIL import Image as _Im
                    import io as _io2
                    return dtx, dty, _Im.open(_io2.BytesIO(data)).convert("RGB")
                except Exception: pass
            return dtx, dty, None

        coords = [(dtx, dty) for dty in range(-pad, pad+1) for dtx in range(-pad, pad+1)]
        with ThreadPoolExecutor(max_workers=16) as ex:
            for dtx, dty, img in ex.map(_load, coords):
                if img:
                    canvas.paste(img, ((dtx + pad) * TILE_SZ, (dty + pad) * TILE_SZ))

        # Crop centred on vessel
        cpx  = pad * TILE_SZ + off_x
        cpy  = pad * TILE_SZ + off_y
        left = max(0, min(cpx - w // 2, side - w))
        top  = max(0, min(cpy - h // 2, side - h))
        out  = canvas.crop((left, top, left + w, top + h))

        # Vessel marker
        from PIL import ImageDraw
        draw = ImageDraw.Draw(out)
        mx, my, s = w // 2, h // 2, 5
        draw.polygon([(mx, my-s),(mx+s, my),(mx, my+s),(mx-s, my)], fill=(0,255,0), outline=(0,0,0))
        draw.rectangle([0, 0, w, 13], fill=(0,0,0))
        draw.text((3, 2), f"SAT  {lat:.3f}  {lon:.3f}  Z{z}", fill=(0,220,255))

        buf = _io.BytesIO()
        out.save(buf, format="JPEG", quality=85)
        resp = make_response(buf.getvalue())
        resp.headers.set("Content-Type", "image/jpeg")
        resp.headers.set("Content-Length", str(len(buf.getvalue())))
        resp.headers.set("Cache-Control", "max-age=900")
        return resp
    except Exception as e:
        log(f"satellite_map error: {e}")
        return make_response(f"err: {e}", 500)

# ── Satellite + AIS overlay map ──────────────────────────────────────────────
@app.route('/sat_ais_map')
@app.route('/sat_ais_map/<path:dummy>')
def sat_ais_map(dummy=None):
    """
    Sentinel-2 satellite basemap with live AIS vessel markers overlaid.
    Same interface as /satellite_map:  ?z=11&w=320&h=320&lat=...&lon=...
    AIS contacts drawn as coloured triangles with MMSI callsign labels.
    """
    import io as _io, math as _math
    from PIL import Image, ImageDraw, ImageFont
    try:
        z   = int(request.args.get('z', 11))
        w   = int(request.args.get('w', 320))
        h   = int(request.args.get('h', 320))
        lat = float(request.args.get('lat', last_known_lat or -38.5))
        lon = float(request.args.get('lon', last_known_lon or 145.6))
        z   = max(8, min(z, 13))

        TILE_SZ = 256
        n  = 2 ** z
        cx, cy   = _latlng_to_tilexy(lat, lon, z)
        cx_i, cy_i = int(cx), int(cy)
        off_x = int((cx - cx_i) * TILE_SZ)
        off_y = int((cy - cy_i) * TILE_SZ)
        pad  = _math.ceil(max(w, h) / 2 / TILE_SZ) + 1
        side = (2 * pad + 1) * TILE_SZ
        canvas = Image.new("RGB", (side, side), (5, 10, 20))

        # ── Fetch sat tiles in parallel ────────────────────────────────────
        from concurrent.futures import ThreadPoolExecutor
        def _load(args):
            dtx, dty = args
            tx = (cx_i + dtx) % n
            ty = cy_i + dty
            if ty < 0 or ty >= n: return dtx, dty, None
            data = _fetch_sentinel_tile(z, tx, ty)
            if data:
                try:
                    from PIL import Image as _Im
                    import io as _io2
                    return dtx, dty, _Im.open(_io2.BytesIO(data)).convert("RGB")
                except Exception: pass
            return dtx, dty, None

        coords_list = [(dtx, dty) for dty in range(-pad, pad+1) for dtx in range(-pad, pad+1)]
        with ThreadPoolExecutor(max_workers=16) as ex:
            for dtx, dty, img in ex.map(_load, coords_list):
                if img:
                    canvas.paste(img, ((dtx + pad) * TILE_SZ, (dty + pad) * TILE_SZ))

        # ── Crop centred on vessel ─────────────────────────────────────────
        cpx  = pad * TILE_SZ + off_x
        cpy  = pad * TILE_SZ + off_y
        left = max(0, min(cpx - w // 2, side - w))
        top  = max(0, min(cpy - h // 2, side - h))
        out  = canvas.crop((left, top, left + w, top + h))

        # ── AIS overlay ────────────────────────────────────────────────────
        draw = ImageDraw.Draw(out)

        def _ll_to_px(vlat, vlon):
            """Convert lat/lon to pixel coords relative to the cropped image."""
            vx, vy = _latlng_to_tilexy(vlat, vlon, z)
            px = int((vx - cx_i + pad) * TILE_SZ - left) - off_x + (off_x - (cpx - w//2 - left))
            py = int((vy - cy_i + pad) * TILE_SZ - top)  - off_y + (off_y - (cpy - h//2 - top))
            # Simplified: direct projection
            dx_tiles = vx - cx
            dy_tiles = vy - cy
            px = int(w / 2 + dx_tiles * TILE_SZ)
            py = int(h / 2 + dy_tiles * TILE_SZ)
            return px, py

        # Draw AIS contacts from global cache
        try:
            import json as _json
            ais_data = ais_cache if isinstance(ais_cache, list) else []
            for vessel in ais_data:
                try:
                    vlat = float(vessel.get('lat', 0))
                    vlon = float(vessel.get('lon', 0))
                    if vlat == 0 and vlon == 0: continue
                    hdg  = float(vessel.get('heading', 0) or vessel.get('course', 0) or 0)
                    name = str(vessel.get('name', '') or vessel.get('mmsi', ''))[:8]
                    px, py = _ll_to_px(vlat, vlon)
                    if not (0 <= px < w and 0 <= py < h): continue
                    # Triangle marker (heading-aware)
                    s = 6
                    hr = _math.radians(hdg)
                    pts = [
                        (px + s*_math.sin(hr),          py - s*_math.cos(hr)),
                        (px + s*_math.sin(hr+2.5),      py - s*_math.cos(hr+2.5)),
                        (px + s*_math.sin(hr-2.5),      py - s*_math.cos(hr-2.5)),
                    ]
                    pts = [(int(x), int(y)) for x, y in pts]
                    draw.polygon(pts, fill=(0, 220, 255), outline=(0, 0, 0))
                    if name:
                        draw.text((px + 7, py - 5), name, fill=(0, 220, 255))
                except Exception: pass
        except Exception: pass

        # ── Own vessel marker ──────────────────────────────────────────────
        mx, my, s = w//2, h//2, 6
        draw.polygon([(mx, my-s),(mx+s, my+s//2),(mx, my+s//4),(mx-s, my+s//2)],
                     fill=(0,255,80), outline=(0,0,0))
        # HUD strip
        draw.rectangle([0, 0, w, 14], fill=(0, 0, 0))
        draw.text((3, 2), f"SAT+AIS  {lat:.3f}  {lon:.3f}  Z{z}", fill=(0,220,255))

        buf = _io.BytesIO()
        out.save(buf, format="JPEG", quality=82)
        resp = make_response(buf.getvalue())
        resp.headers.set("Content-Type", "image/jpeg")
        resp.headers.set("Content-Length", str(len(buf.getvalue())))
        resp.headers.set("Cache-Control", "max-age=30")
        return resp
    except Exception as e:
        log(f"sat_ais_map error: {e}")
        return make_response(f"err: {e}", 500)

@app.route('/tile_cache_coverage')
@requires_auth
def tile_cache_coverage():
    """
    Returns JSON tile coverage for vessel area.
    Each tile: {z, tx, ty, lat_n, lon_w, lat_s, lon_e, status: ssd|gdrive|partial|missing}
    Consumed by the tile_manager Leaflet map to draw coloured coverage rectangles.
    """
    try:
        import math
        lat   = float(request.args.get('lat', last_known_lat))
        lon   = float(request.args.get('lon', last_known_lon))
        nm    = float(request.args.get('nm', 25))
        zooms = [int(z) for z in request.args.get('zooms', '11,12').split(',')]
        deg_lat = nm / 60.0
        deg_lon = deg_lat / max(math.cos(math.radians(lat)), 0.01)
        ALL_TPLS = [
            (_GA_TOPO_URL,    False),
            (_OCEAN_BASE_URL, False),
            (_OCEAN_REF_URL,  True),
            (_SEAMARK_URL,    True),
        ]
        def tile_bbox(z, tx, ty):
            n = 2 ** z
            lon_w = tx / n * 360.0 - 180.0
            lon_e = (tx + 1) / n * 360.0 - 180.0
            lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
            lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
            return lat_n, lon_w, lat_s, lon_e

        tiles = []
        for z in zooms:
            n = 2 ** z
            tlx = max(0, int(_latlng_to_tilexy(lat + deg_lat, lon - deg_lon, z)[0]))
            trx = min(n - 1, int(_latlng_to_tilexy(lat + deg_lat, lon + deg_lon, z)[0]))
            tty = max(0, int(_latlng_to_tilexy(lat + deg_lat, lon, z)[1]))
            tby = min(n - 1, int(_latlng_to_tilexy(lat - deg_lat, lon, z)[1]))
            for tx in range(tlx, trx + 1):
                for ty in range(tty, tby + 1):
                    ssd_ct = gd_ct = 0
                    for tpl, rgba in ALL_TPLS:
                        url = tpl.format(z=z, tx=tx, ty=ty)
                        cn  = re.sub(r'[^\w]', '_', url)[-180:] + (".png" if rgba else ".jpg")
                        with _tile_index_lk:
                            info = _tile_index.get(cn, {})
                        if info.get("ssd") or os.path.exists(os.path.join(_TILE_CACHE_DIR, cn)):
                            ssd_ct += 1
                        elif _GDRIVE_CACHE_DIR and (info.get("gdrive") or os.path.exists(os.path.join(_GDRIVE_CACHE_DIR, cn))):
                            gd_ct += 1
                    total  = len(ALL_TPLS)
                    status = "ssd" if ssd_ct == total else ("partial" if ssd_ct > 0 else ("gdrive" if gd_ct > 0 else "missing"))
                    lat_n, lon_w, lat_s, lon_e = tile_bbox(z, tx, ty)
                    tiles.append({"z": z, "tx": tx, "ty": ty,
                                  "lat_n": round(lat_n, 5), "lon_w": round(lon_w, 5),
                                  "lat_s": round(lat_s, 5), "lon_e": round(lon_e, 5),
                                  "status": status})

        return jsonify({"tiles": tiles, "total": len(tiles),
                        "vessel": {"lat": lat, "lon": lon}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_TILE_MGR_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/><title>PYXIS TILE CACHE MANAGER</title>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=Roboto+Mono:wght@400;600&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.css"/>
<style>
:root{--bg:#050d1a;--glass:rgba(8,20,45,0.85);--border:rgba(0,212,255,0.18);--cyan:#00d4ff;--green:#00ff9f;--amber:#ffb300;--red:#ff4757;--violet:#a78bfa;--text:#c8e6ff;--dim:#4a7a9b;}
*{box-sizing:border-box;margin:0;padding:0;}html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;}
#layout{display:flex;height:100vh;width:100vw;}#map{flex:1;min-width:0;}
#panel{width:370px;flex-shrink:0;background:var(--glass);border-left:1px solid var(--border);backdrop-filter:blur(18px);display:flex;flex-direction:column;overflow:hidden;}
#ph{padding:12px 14px 8px;border-bottom:1px solid var(--border);background:rgba(0,0,0,0.35);}
#ph h1{font-size:11px;font-weight:700;letter-spacing:2.5px;color:var(--cyan);font-family:'Roboto Mono',monospace;}
#ph .sub{font-size:8px;color:var(--dim);letter-spacing:1px;margin-top:2px;}
#vpos{display:flex;gap:10px;padding:7px 14px;background:rgba(0,212,255,0.04);border-bottom:1px solid var(--border);font-family:'Roboto Mono',monospace;}
.vpi{display:flex;flex-direction:column;}.vpl{font-size:7px;letter-spacing:1.5px;color:var(--dim);text-transform:uppercase;}.vpv{font-size:10px;color:var(--cyan);font-weight:600;}
#pbody{flex:1;overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:8px;}
.card{background:rgba(0,0,0,0.28);border:1px solid var(--border);border-radius:8px;padding:9px;}
.ctitle{font-size:8px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;margin-bottom:7px;font-family:'Roboto Mono',monospace;display:flex;justify-content:space-between;align-items:center;}
.layer-row{display:flex;gap:6px;}
.lbtn{flex:1;padding:7px 4px;background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:6px;cursor:pointer;text-align:center;transition:all .2s;user-select:none;}
.lbtn.on{border-color:var(--cyan);background:rgba(0,212,255,0.1);}
.lbtn .li{font-size:17px;display:block;margin-bottom:2px;}
.lbtn .ln{font-size:9px;letter-spacing:1px;font-family:'Roboto Mono',monospace;color:var(--dim);}.lbtn.on .ln{color:var(--cyan);}
.lbtn .ls{font-size:7px;color:var(--dim);}
.zgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;}
.zbtn{padding:5px 2px;background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:5px;cursor:pointer;text-align:center;transition:all .2s;font-family:'Roboto Mono',monospace;user-select:none;}
.zbtn .zz{font-size:10px;font-weight:700;color:var(--text);}.zbtn .znm{font-size:7px;color:var(--dim);}.zbtn .zt{font-size:7px;color:var(--dim);margin-top:1px;}
.zbtn.on{border-color:var(--violet);background:rgba(167,139,250,0.12);}.zbtn.on .zz{color:var(--violet);}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:5px;}
.pbtn{padding:7px 3px;background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:6px;cursor:pointer;text-align:center;font-size:9px;font-family:'Roboto Mono',monospace;color:var(--text);transition:all .2s;user-select:none;}
.pbtn:hover{background:rgba(0,212,255,0.08);border-color:rgba(0,212,255,0.35);}
.pbtn.on{background:rgba(0,212,255,0.12);border-color:var(--cyan);color:var(--cyan);}
#draw-btn{width:100%;margin-top:6px;padding:9px;background:rgba(167,139,250,0.08);border:1px solid rgba(167,139,250,0.3);border-radius:7px;color:var(--violet);font-size:10px;font-weight:600;font-family:'Roboto Mono',monospace;cursor:pointer;letter-spacing:1px;transition:all .2s;}
#draw-btn.on{background:rgba(167,139,250,0.22);border-color:var(--violet);}
#bbox-info{font-size:8px;font-family:'Roboto Mono',monospace;color:var(--dim);margin-top:5px;line-height:1.7;}
.sgrid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;}
.si{text-align:center;padding:6px;background:rgba(0,0,0,0.2);border-radius:5px;}
.sn{font-size:15px;font-weight:700;font-family:'Roboto Mono',monospace;color:var(--cyan);}
.sn.warn{color:var(--amber);}.sn.bad{color:var(--red);}
.sl{font-size:7px;color:var(--dim);letter-spacing:1px;margin-top:1px;}
#dl-btn{width:100%;padding:11px;background:linear-gradient(135deg,rgba(0,212,255,0.18),rgba(0,90,255,0.18));border:1px solid var(--cyan);border-radius:8px;color:var(--cyan);font-size:12px;font-weight:700;font-family:'Roboto Mono',monospace;cursor:pointer;letter-spacing:2px;transition:all .2s;}
#dl-btn:hover:not(:disabled){background:linear-gradient(135deg,rgba(0,212,255,0.32),rgba(0,90,255,0.32));}
#dl-btn:disabled{opacity:0.38;cursor:not-allowed;}
#pw{display:none;}.pbb{background:rgba(0,0,0,0.3);border-radius:4px;height:5px;overflow:hidden;margin:5px 0;}
.pb{height:5px;border-radius:4px;background:linear-gradient(90deg,var(--cyan),#0055ff);transition:width .5s;width:0%;}
.ptxt{display:flex;justify-content:space-between;font-size:8px;font-family:'Roboto Mono',monospace;color:var(--dim);}
#log{font-size:8px;font-family:'Roboto Mono',monospace;color:var(--dim);max-height:90px;overflow-y:auto;}
.ll{padding:2px 0;border-bottom:1px solid rgba(0,212,255,0.05);}.ll.ok{color:var(--green);}.ll.err{color:var(--red);}.ll.info{color:var(--cyan);}
#cov-btn{width:100%;padding:8px;background:rgba(0,255,159,0.08);border:1px solid rgba(0,255,159,0.3);border-radius:7px;color:var(--green);font-size:9px;font-weight:600;font-family:'Roboto Mono',monospace;cursor:pointer;letter-spacing:1px;transition:all .2s;margin-top:6px;}
#cov-btn.on{background:rgba(0,255,159,0.2);border-color:var(--green);}
.leg{display:flex;gap:6px;margin-top:5px;flex-wrap:wrap;}
.leg-i{display:flex;align-items:center;gap:3px;font-size:7px;font-family:'Roboto Mono',monospace;color:var(--dim);}
.leg-sq{width:9px;height:9px;border-radius:2px;}
</style></head><body>
<div id="layout"><div id="map"></div>
<div id="panel">
<div id="ph"><h1>PYXIS TILE CACHE MANAGER</h1><div class="sub">PRE-CACHE NAUTICAL &amp; TOPOGRAPHIC CHARTS FOR OFFLINE USE</div></div>
<div id="vpos">
<div class="vpi"><div class="vpl">VESSEL LAT</div><div class="vpv" id="vlat">--</div></div>
<div class="vpi"><div class="vpl">VESSEL LON</div><div class="vpv" id="vlon">--</div></div>
<div class="vpi"><div class="vpl">STATUS</div><div class="vpv" id="vstatus" style="color:var(--green)">READY</div></div>
</div>
<div id="pbody">
<div class="card"><div class="ctitle">Chart Layers</div><div class="layer-row">
<div class="lbtn on" id="lb-nautical" onclick="toggleLayer('nautical')"><span class="li">&#x1F5FA;</span><div class="ln">NAUTICAL</div><div class="ls">Esri Ocean + OpenSeaMap</div></div>
<div class="lbtn on" id="lb-topo" onclick="toggleLayer('topo')"><span class="li">&#x1F3D4;</span><div class="ln">TOPOGRAPHIC</div><div class="ls">OpenTopoMap (SRTM)</div></div>
</div></div>
<div class="card"><div class="ctitle"><span>Zoom Levels</span><span id="zsummary" style="color:var(--violet);font-size:7px;">z11 z12 z13</span></div><div class="zgrid" id="zgrid"></div></div>
<div class="card"><div class="ctitle">Cache Coverage Overlay</div>
<button id="cov-btn" onclick="toggleCoverage()">SHOW CACHE COVERAGE</button>
<div class="leg"><div class="leg-i"><div class="leg-sq" style="background:#00ff9f"></div>SSD</div><div class="leg-i"><div class="leg-sq" style="background:#00aaff"></div>GDrive</div><div class="leg-i"><div class="leg-sq" style="background:#ffb300"></div>Partial</div><div class="leg-i"><div class="leg-sq" style="background:rgba(255,71,87,0.6)"></div>Missing</div></div>
<div id="cov-status" style="font-size:7px;font-family:Roboto Mono,monospace;color:var(--dim);margin-top:4px;"></div>
</div>
<div class="card"><div class="ctitle">Coverage Area</div>
<div class="pgrid">
<div class="pbtn" onclick="setRadius(5,this)">VESSEL +5nm</div>
<div class="pbtn" onclick="setRadius(20,this)">VESSEL +20nm</div>
<div class="pbtn" onclick="setRadius(50,this)">VESSEL +50nm</div>
<div class="pbtn" onclick="setRadius(100,this)">VESSEL +100nm</div>
</div>
<button id="draw-btn" onclick="toggleDraw()">DRAW RECTANGLE ON MAP</button>
<div id="bbox-info">No area selected.</div></div>
<div class="card"><div class="ctitle">Estimate</div><div class="sgrid">
<div class="si"><div class="sn" id="stiles">0</div><div class="sl">TILES</div></div>
<div class="si"><div class="sn" id="smb">0</div><div class="sl">EST. MB</div></div>
<div class="si"><div class="sn" id="seta">--</div><div class="sl">EST. MIN</div></div>
</div></div>
<button id="dl-btn" onclick="startDL()" disabled>SELECT AREA FIRST</button>
<div id="pw"><div class="card"><div class="ctitle"><span>Downloading</span><span id="ppct" style="color:var(--cyan)">0%</span></div>
<div class="pbb"><div class="pb" id="pbar"></div></div>
<div class="ptxt"><span id="pdone">0 / 0 tiles</span><span id="perrs" style="color:var(--red)"></span></div></div></div>
<div class="card"><div class="ctitle">Job Log</div><div id="log"><div class="ll ok">System ready.</div></div></div>
</div></div></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.js"></script>
<script>
var vLat=-38.487,vLon=145.620,bbox=null,drawing=false,pollT=null,selRect=null;
var layers=new Set(['nautical','topo']),zooms=new Set([11,12,13]);
var ZMETA={8:{nm:'200nm',k:6},9:{nm:'100nm',k:8},10:{nm:'50nm',k:10},11:{nm:'25nm',k:12},12:{nm:'12nm',k:14},13:{nm:'6nm',k:16},14:{nm:'3nm',k:20},15:{nm:'1.5nm',k:24}};
var LCOUNT={nautical:3,topo:1};
var map=L.map('map',{zoomControl:true,attributionControl:false}).setView([vLat,vLon],10);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{maxZoom:20}).addTo(map);
L.tileLayer('https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png',{maxZoom:18,opacity:0.7}).addTo(map);
var vi=L.divIcon({className:'',html:'<div style="font-size:20px;color:#00d4ff;filter:drop-shadow(0 0 5px #00d4ff);line-height:1;">&#9650;</div>',iconSize:[20,20],iconAnchor:[10,10]});
var vMark=L.marker([vLat,vLon],{icon:vi}).addTo(map);
var dI=new L.FeatureGroup();map.addLayer(dI);
var dC=new L.Control.Draw({draw:{rectangle:{shapeOptions:{color:'#a78bfa',fillColor:'rgba(167,139,250,0.12)',weight:2}},polyline:false,polygon:false,circle:false,marker:false,circlemarker:false},edit:{featureGroup:dI}});
map.on(L.Draw.Event.CREATED,function(e){dI.clearLayers();dI.addLayer(e.layer);var b=e.layer.getBounds();setBbox({N:b.getNorth(),S:b.getSouth(),E:b.getEast(),W:b.getWest()});drawing=false;var db=document.getElementById('draw-btn');db.classList.remove('on');db.textContent='DRAW RECTANGLE ON MAP';});
function buildZooms(){var g=document.getElementById('zgrid');g.innerHTML='';Object.keys(ZMETA).forEach(function(z){var zi=parseInt(z),m=ZMETA[z],d=document.createElement('div');d.className='zbtn'+(zooms.has(zi)?' on':'');d.id='zb'+z;d.innerHTML='<div class="zz">z='+z+'</div><div class="znm">'+m.nm+'</div><div class="zt" id="zt'+z+'">-- tiles</div>';d.onclick=function(){toggleZ(zi);};g.appendChild(d);});updateStats();}
function toggleZ(z){if(zooms.has(z))zooms.delete(z);else zooms.add(z);var el=document.getElementById('zb'+z);if(el)el.className='zbtn'+(zooms.has(z)?' on':'');document.getElementById('zsummary').textContent=Array.from(zooms).sort().map(function(z){return'z'+z;}).join(' ')||'none';updateStats();}
function toggleLayer(n){if(layers.has(n))layers.delete(n);else layers.add(n);var el=document.getElementById('lb-'+n);if(el)el.classList.toggle('on',layers.has(n));updateStats();}
function tc(N,S,E,W,z){var n=Math.pow(2,z),lo=function(l){return Math.floor((l+180)/360*n);},la=function(a){var r=a*Math.PI/180;return Math.floor((1-Math.log(Math.tan(r)+1/Math.cos(r))/Math.PI)/2*n);};return(Math.abs(lo(E)-lo(W))+1)*(Math.abs(la(N)-la(S))+1);}
function updateStats(){if(!bbox){resetStats();return;}var tot=0,kb=0;zooms.forEach(function(z){var lc=(layers.has('nautical')?LCOUNT.nautical:0)+(layers.has('topo')?LCOUNT.topo:0),t=tc(bbox.N,bbox.S,bbox.E,bbox.W,z)*lc;tot+=t;kb+=t*(ZMETA[z]?ZMETA[z].k:12);var el=document.getElementById('zt'+z);if(el)el.textContent=t>0?t.toLocaleString()+' t':'-- tiles';});Object.keys(ZMETA).forEach(function(z){if(!zooms.has(parseInt(z))){var el=document.getElementById('zt'+z);if(el)el.textContent='-- tiles';}});var mb=(kb/1024).toFixed(1),eta=(tot/15/60).toFixed(1);document.getElementById('stiles').textContent=tot.toLocaleString();document.getElementById('smb').textContent=mb;document.getElementById('seta').textContent=eta;var ok=tot>0&&layers.size>0&&zooms.size>0,db=document.getElementById('dl-btn');db.disabled=!ok;db.textContent=ok?'CACHE '+tot.toLocaleString()+' TILES':'SELECT AREA FIRST';}
function resetStats(){document.getElementById('stiles').textContent='0';document.getElementById('smb').textContent='0';document.getElementById('seta').textContent='--';document.getElementById('dl-btn').disabled=true;document.getElementById('dl-btn').textContent='SELECT AREA FIRST';Object.keys(ZMETA).forEach(function(z){var el=document.getElementById('zt'+z);if(el)el.textContent='-- tiles';});}
function setBbox(b){bbox=b;document.getElementById('bbox-info').innerHTML='<span style="color:#aaa">N </span><b style="color:var(--cyan)">'+b.N.toFixed(4)+'</b>  <span style="color:#aaa">S </span><b style="color:var(--cyan)">'+b.S.toFixed(4)+'</b>  <span style="color:#aaa">E </span><b style="color:var(--cyan)">'+b.E.toFixed(4)+'</b>  <span style="color:#aaa">W </span><b style="color:var(--cyan)">'+b.W.toFixed(4)+'</b>';updateStats();}
function setRadius(nm,el){var deg=nm/60,ld=deg/Math.cos(vLat*Math.PI/180),b={N:vLat+deg,S:vLat-deg,E:vLon+ld,W:vLon-ld};dI.clearLayers();if(selRect)map.removeLayer(selRect);selRect=L.rectangle([[b.S,b.W],[b.N,b.E]],{color:'#a78bfa',fillColor:'rgba(167,139,250,0.10)',weight:2}).addTo(map);map.fitBounds([[b.S,b.W],[b.N,b.E]],{padding:[20,20]});document.querySelectorAll('.pbtn').forEach(function(e){e.classList.remove('on');});el.classList.add('on');setBbox(b);addLog('Area: vessel +'+nm+'nm','info');}
function toggleDraw(){if(drawing){map.removeControl(dC);drawing=false;var db=document.getElementById('draw-btn');db.classList.remove('on');db.textContent='DRAW RECTANGLE ON MAP';}else{map.addControl(dC);new L.Draw.Rectangle(map,dC.options.draw.rectangle).enable();drawing=true;var db=document.getElementById('draw-btn');db.classList.add('on');db.textContent='DRAG TO SELECT AREA...';document.querySelectorAll('.pbtn').forEach(function(e){e.classList.remove('on');});}}
async function startDL(){if(!bbox||!layers.size||!zooms.size)return;var ly=Array.from(layers),zs=Array.from(zooms).sort();document.getElementById('dl-btn').disabled=true;document.getElementById('pw').style.display='block';addLog('Starting: '+ly.join(',')+' z='+zs.join(','),'info');try{var r=await fetch('/fetch_tile_region',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Basic '+btoa('admin:manta')},body:JSON.stringify({bbox:{N:+bbox.N,S:+bbox.S,E:+bbox.E,W:+bbox.W},zooms:zs,layers:ly})});var d=await r.json();addLog('Job '+d.job_id,'info');pollT=setInterval(function(){pollJob(d.job_id);},1200);}catch(e){addLog('ERROR: '+e,'err');document.getElementById('dl-btn').disabled=false;}}
async function pollJob(id){try{var r=await fetch('/tile_job_status/'+id,{headers:{'Authorization':'Basic '+btoa('admin:manta')}});var d=await r.json();var p=d.total>0?Math.round(d.done/d.total*100):0;document.getElementById('pbar').style.width=p+'%';document.getElementById('ppct').textContent=p+'%';document.getElementById('pdone').textContent=d.done.toLocaleString()+'/'+d.total.toLocaleString()+' tiles';if(d.errors>0)document.getElementById('perrs').textContent=d.errors+' errors';if(d.status==='done'){clearInterval(pollT);addLog('Done: '+d.done+' tiles ('+d.errors+' errors)',d.errors===0?'ok':'err');document.getElementById('dl-btn').disabled=false;document.getElementById('dl-btn').textContent='DONE - CACHE MORE';}}catch(e){}}
async function fetchVessel(){try{var r=await fetch('/status_api?bust='+Date.now());var d=await r.json();if(d.lat&&d.lat!==0){vLat=d.lat;vLon=d.lon;}vMark.setLatLng([vLat,vLon]);document.getElementById('vlat').textContent=vLat.toFixed(4);document.getElementById('vlon').textContent=vLon.toFixed(4);}catch(e){}}
function addLog(msg,cls){var el=document.createElement('div');el.className='ll '+(cls||'');el.textContent=new Date().toTimeString().slice(0,8)+' '+msg;var l=document.getElementById('log');l.insertBefore(el,l.firstChild);if(l.children.length>20)l.removeChild(l.lastChild);}
var covLayers=[],covOn=false;
var COV_COLORS={ssd:'rgba(0,255,159,0.25)',gdrive:'rgba(0,170,255,0.25)',partial:'rgba(255,179,0,0.3)',missing:'rgba(255,71,87,0.18)'};
var COV_BORDERS={ssd:'#00ff9f',gdrive:'#00aaff',partial:'#ffb300',missing:'rgba(255,71,87,0.7)'};
async function toggleCoverage(){
  covOn=!covOn;
  var btn=document.getElementById('cov-btn');
  covLayers.forEach(function(l){map.removeLayer(l);});covLayers=[];
  if(!covOn){btn.classList.remove('on');btn.textContent='SHOW CACHE COVERAGE';document.getElementById('cov-status').textContent='';return;}
  btn.classList.add('on');btn.textContent='LOADING...';
  document.getElementById('cov-status').textContent='Scanning tile index...';
  try{
    var nm=25,zs=[10,11,12,13];
    var url='/tile_cache_coverage?lat='+vLat+'&lon='+vLon+'&nm='+nm+'&zooms='+zs.join(',')+'&bust='+Date.now();
    var r=await fetch(url);var d=await r.json();
    if(d.error){document.getElementById('cov-status').textContent='Error: '+d.error;covOn=false;btn.classList.remove('on');btn.textContent='SHOW CACHE COVERAGE';return;}
    var cnt={ssd:0,gdrive:0,partial:0,missing:0};
    d.tiles.forEach(function(t){
      var rect=L.rectangle([[t.lat_s,t.lon_w],[t.lat_n,t.lon_e]],{color:COV_BORDERS[t.status]||'#888',fillColor:COV_COLORS[t.status]||'rgba(128,128,128,0.15)',weight:0.5,fillOpacity:1,interactive:false});
      rect.addTo(map);covLayers.push(rect);
      cnt[t.status]=(cnt[t.status]||0)+1;
    });
    document.getElementById('cov-status').textContent='SSD:'+cnt.ssd+' GD:'+cnt.gdrive+' Partial:'+cnt.partial+' Miss:'+cnt.missing;
    btn.textContent='HIDE COVERAGE ('+d.total+' tiles)';
    addLog('Coverage: '+d.total+' tiles scanned at 25nm','info');
  }catch(e){document.getElementById('cov-status').textContent='Error: '+e;covOn=false;btn.classList.remove('on');btn.textContent='SHOW CACHE COVERAGE';}
}
buildZooms();fetchVessel();setInterval(fetchVessel,10000);
</script></body></html>
"""

@app.route('/tile_manager')
@requires_auth
def tile_manager():
    global last_known_lat, last_known_lon
    html = _TILE_MGR_HTML.replace("var vLat=-38.487,vLon=145.620", f"var vLat={last_known_lat},vLon={last_known_lon}")
    resp = make_response(html)
    resp.headers.set('Content-Type', 'text/html; charset=utf-8')
    return resp




# ── Sentinel Cache Manager endpoints ─────────────────────────────────────────
_sentinel_jobs = {}   # job_id -> {status, done, total, errors, skipped}

@app.route('/sentinel_cache_coverage')
@requires_auth
def sentinel_cache_coverage():
    """JSON tile coverage for sentinel cache: missing/cached/stale per tile."""
    import math
    try:
        lat   = float(request.args.get('lat', last_known_lat))
        lon   = float(request.args.get('lon', last_known_lon))
        nm    = float(request.args.get('nm', 25))
        zooms = [int(z) for z in request.args.get('zooms', '10,11,12').split(',')]
        deg_lat = nm / 60.0
        deg_lon = deg_lat / max(math.cos(math.radians(lat)), 0.01)

        def tile_bbox(z, tx, ty):
            n = 2 ** z
            lon_w = tx / n * 360.0 - 180.0
            lon_e = (tx+1) / n * 360.0 - 180.0
            lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2*ty/n))))
            lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2*(ty+1)/n))))
            return lat_n, lon_w, lat_s, lon_e

        tiles = []
        now = time.time()
        for z in zooms:
            n = 2 ** z
            tlx = max(0, int(_latlng_to_tilexy(lat+deg_lat, lon-deg_lon, z)[0]))
            trx = min(n-1, int(_latlng_to_tilexy(lat+deg_lat, lon+deg_lon, z)[0]))
            tty = max(0, int(_latlng_to_tilexy(lat+deg_lat, lon, z)[1]))
            tby = min(n-1, int(_latlng_to_tilexy(lat-deg_lat, lon, z)[1]))
            for tx in range(tlx, trx+1):
                for ty in range(tty, tby+1):
                    cn = f"sentinel_{z}_{tx}_{ty}.jpg"
                    cp = os.path.join(_SH_TILE_CACHE, cn)
                    if os.path.exists(cp):
                        age = now - os.path.getmtime(cp)
                        status = "fresh" if age < 14*86400 else "stale"
                    else:
                        cp_gd = os.path.join(_SH_GDRIVE_CACHE, cn)
                        status = "gdrive" if os.path.exists(cp_gd) else "missing"
                    lat_n, lon_w, lat_s, lon_e = tile_bbox(z, tx, ty)
                    tiles.append({"z": z, "tx": tx, "ty": ty,
                                  "lat_n": round(lat_n,5), "lon_w": round(lon_w,5),
                                  "lat_s": round(lat_s,5), "lon_e": round(lon_e,5),
                                  "status": status})
        return jsonify({"tiles": tiles, "total": len(tiles),
                        "vessel": {"lat": lat, "lon": lon}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _fetch_sentinel_region_worker(job_id, bbox, zooms, layer, maxcc):
    """Background thread: caches Sentinel tiles for a bbox at given zooms."""
    import math as _m
    job = _sentinel_jobs[job_id]
    tile_list = []
    for z in zooms:
        n = 2 ** z
        def to_tx(lon): return int((lon+180)/360*n)
        def to_ty(lat):
            r = _m.radians(lat)
            return int((1 - _m.log(_m.tan(r) + 1/_m.cos(r)) / _m.pi) / 2 * n)
        x0 = max(0, to_tx(bbox['W']));  x1 = min(n-1, to_tx(bbox['E']))
        y0 = max(0, to_ty(bbox['N']));  y1 = min(n-1, to_ty(bbox['S']))
        for ty in range(y0, y1+1):
            for tx in range(x0, x1+1):
                tile_list.append((z, tx % n, ty))

    job['total']  = len(tile_list)
    job['status'] = 'running'
    done = [0]; errs = [0]; skipped = [0]
    global _SH_WMS_BASE
    # Override MAXCC per-job
    old_base = _SH_WMS_BASE

    for z, tx, ty in tile_list:
        cn = f"sentinel_{z}_{tx}_{ty}.jpg"
        cp = os.path.join(_SH_TILE_CACHE, cn)
        if os.path.exists(cp) and time.time() - os.path.getmtime(cp) < 14*86400:
            skipped[0] += 1
        else:
            # Fetch with custom MAXCC and layer
            import math as _mm, shutil as _sh
            n_z = 2 ** z
            lon_w = tx / n_z * 360.0 - 180.0
            lon_e = (tx+1) / n_z * 360.0 - 180.0
            lat_n = _mm.degrees(_mm.atan(_mm.sinh(_mm.pi * (1 - 2*ty/n_z))))
            lat_s = _mm.degrees(_mm.atan(_mm.sinh(_mm.pi * (1 - 2*(ty+1)/n_z))))
            params = {
                "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.3.0",
                "LAYERS": layer, "CRS": "CRS:84",
                "BBOX": f"{lon_w},{lat_s},{lon_e},{lat_n}",
                "WIDTH": "256", "HEIGHT": "256",
                "FORMAT": "image/jpeg", "MAXCC": str(maxcc),
            }
            try:
                r = requests.get(_SH_WMS_BASE, params=params, timeout=20,
                                 headers={"User-Agent": "PyxisManta/4.1"})
                ct = r.headers.get("Content-Type","")
                if r.status_code == 200 and ct.startswith("image"):
                    with open(cp, "wb") as f: f.write(r.content)
                    _mark_tile(cn, "ssd")
                    try:
                        _sh.copy2(cp, os.path.join(_SH_GDRIVE_CACHE, cn))
                        _mark_tile(cn, "gdrive")
                    except: pass
                    done[0] += 1
                else:
                    errs[0] += 1
            except Exception as e:
                errs[0] += 1
            time.sleep(1.0)   # 1 req/sec free tier hard limit
        job['done'] = done[0]; job['errors'] = errs[0]; job['skipped'] = skipped[0]

    _save_tile_index()
    job['status'] = 'done'
    log(f"Sentinel Cache Job {job_id}: {done[0]} fetched, {skipped[0]} skipped, {errs[0]} errors")


@app.route('/fetch_sentinel_region', methods=['POST'])
@requires_auth
def fetch_sentinel_region():
    data    = request.get_json(force=True)
    job_id  = str(uuid.uuid4())[:8]
    layer   = data.get('layer', 'TRUE_COLOR')
    maxcc   = int(data.get('maxcc', 20))
    _sentinel_jobs[job_id] = {'status':'queued','done':0,'total':0,'errors':0,'skipped':0}
    threading.Thread(
        target=_fetch_sentinel_region_worker,
        args=(job_id, data['bbox'], data['zooms'], layer, maxcc),
        daemon=True
    ).start()
    return jsonify({'job_id': job_id})


@app.route('/sentinel_job_status/<job_id>')
@requires_auth
def sentinel_job_status(job_id):
    job = _sentinel_jobs.get(job_id, {'status':'unknown','done':0,'total':0,'errors':0,'skipped':0})
    return jsonify(job)


_SENTINEL_MGR_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<title>PYXIS SENTINEL CACHE MANAGER</title>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=Roboto+Mono:wght@400;600&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.css"/>
<style>
:root{--bg:#050d1a;--glass:rgba(8,20,45,0.88);--border:rgba(0,212,255,0.18);--cyan:#00d4ff;--green:#00ff9f;--amber:#ffb300;--red:#ff4757;--violet:#a78bfa;--orange:#ff8c42;--text:#c8e6ff;--dim:#4a7a9b;}
*{box-sizing:border-box;margin:0;padding:0;}html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;}
#layout{display:flex;height:100vh;width:100vw;}#map{flex:1;min-width:0;}
#panel{width:380px;flex-shrink:0;background:var(--glass);border-left:1px solid var(--border);backdrop-filter:blur(18px);display:flex;flex-direction:column;overflow:hidden;}
#ph{padding:12px 14px 8px;border-bottom:1px solid var(--border);background:rgba(0,0,0,0.35);}
#ph h1{font-size:11px;font-weight:700;letter-spacing:2.5px;color:var(--orange);font-family:'Roboto Mono',monospace;}
#ph .sub{font-size:8px;color:var(--dim);letter-spacing:1px;margin-top:2px;}
#ph .badge{display:inline-block;font-size:7px;background:rgba(255,140,66,0.15);border:1px solid rgba(255,140,66,0.3);color:var(--orange);padding:1px 5px;border-radius:3px;margin-top:4px;font-family:'Roboto Mono',monospace;letter-spacing:1px;}
#vpos{display:flex;gap:10px;padding:7px 14px;background:rgba(255,140,66,0.04);border-bottom:1px solid var(--border);font-family:'Roboto Mono',monospace;}
.vpi{display:flex;flex-direction:column;}.vpl{font-size:7px;letter-spacing:1.5px;color:var(--dim);text-transform:uppercase;}.vpv{font-size:10px;color:var(--orange);font-weight:600;}
#pbody{flex:1;overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:8px;}
.card{background:rgba(0,0,0,0.28);border:1px solid var(--border);border-radius:8px;padding:9px;}
.ctitle{font-size:8px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;margin-bottom:7px;font-family:'Roboto Mono',monospace;display:flex;justify-content:space-between;align-items:center;}
.layer-grid{display:grid;grid-template-columns:1fr 1fr;gap:5px;}
.lbtn{padding:7px 4px;background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:6px;cursor:pointer;text-align:center;transition:all .2s;user-select:none;}
.lbtn.on{border-color:var(--orange);background:rgba(255,140,66,0.12);}
.ln{font-size:9px;letter-spacing:1px;font-family:'Roboto Mono',monospace;color:var(--dim);}.lbtn.on .ln{color:var(--orange);}
.ls{font-size:7px;color:var(--dim);margin-top:1px;}
.zgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;}
.zbtn{padding:5px 2px;background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:5px;cursor:pointer;text-align:center;transition:all .2s;font-family:'Roboto Mono',monospace;user-select:none;}
.zz{font-size:10px;font-weight:700;color:var(--text);}.znm{font-size:7px;color:var(--dim);}.zt{font-size:7px;color:var(--dim);margin-top:1px;}
.zbtn.on{border-color:var(--violet);background:rgba(167,139,250,0.12);}.zbtn.on .zz{color:var(--violet);}
.zbtn.warn{border-color:var(--amber)!important;}.zbtn.warn .zz{color:var(--amber)!important;}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:5px;}
.pbtn{padding:7px 3px;background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:6px;cursor:pointer;text-align:center;font-size:9px;font-family:'Roboto Mono',monospace;color:var(--text);transition:all .2s;user-select:none;}
.pbtn:hover{background:rgba(255,140,66,0.08);border-color:rgba(255,140,66,0.35);}
.pbtn.on{background:rgba(255,140,66,0.12);border-color:var(--orange);color:var(--orange);}
#draw-btn{width:100%;margin-top:6px;padding:9px;background:rgba(167,139,250,0.08);border:1px solid rgba(167,139,250,0.3);border-radius:7px;color:var(--violet);font-size:10px;font-weight:600;font-family:'Roboto Mono',monospace;cursor:pointer;letter-spacing:1px;transition:all .2s;}
#draw-btn.on{background:rgba(167,139,250,0.22);border-color:var(--violet);}
#bbox-info{font-size:8px;font-family:'Roboto Mono',monospace;color:var(--dim);margin-top:5px;line-height:1.7;}
.cloud-row{display:flex;align-items:center;gap:8px;margin-top:6px;}
.cloud-lbl{font-size:8px;font-family:'Roboto Mono',monospace;color:var(--dim);white-space:nowrap;}
#maxcc-sl{flex:1;accent-color:var(--orange);}
#maxcc-val{font-size:9px;font-family:'Roboto Mono',monospace;color:var(--orange);min-width:28px;text-align:right;}
.sgrid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;}
.si{text-align:center;padding:6px;background:rgba(0,0,0,0.2);border-radius:5px;}
.sn{font-size:15px;font-weight:700;font-family:'Roboto Mono',monospace;color:var(--orange);}
.sn.warn{color:var(--amber);}.sn.bad{color:var(--red);}
.sl{font-size:7px;color:var(--dim);letter-spacing:1px;margin-top:1px;}
#dl-btn{width:100%;padding:11px;background:linear-gradient(135deg,rgba(255,140,66,0.18),rgba(200,80,0,0.18));border:1px solid var(--orange);border-radius:8px;color:var(--orange);font-size:12px;font-weight:700;font-family:'Roboto Mono',monospace;cursor:pointer;letter-spacing:2px;transition:all .2s;}
#dl-btn:hover:not(:disabled){background:linear-gradient(135deg,rgba(255,140,66,0.32),rgba(200,80,0,0.32));}
#dl-btn:disabled{opacity:0.38;cursor:not-allowed;}
#pw{display:none;}.pbb{background:rgba(0,0,0,0.3);border-radius:4px;height:5px;overflow:hidden;margin:5px 0;}
.pb{height:5px;border-radius:4px;background:linear-gradient(90deg,var(--orange),#ff4500);transition:width .5s;width:0%;}
.ptxt{display:flex;justify-content:space-between;font-size:8px;font-family:'Roboto Mono',monospace;color:var(--dim);}
#log{font-size:8px;font-family:'Roboto Mono',monospace;color:var(--dim);max-height:90px;overflow-y:auto;}
.ll{padding:2px 0;border-bottom:1px solid rgba(255,140,66,0.05);}.ll.ok{color:var(--green);}.ll.err{color:var(--red);}.ll.info{color:var(--orange);}
#cov-btn{width:100%;padding:8px;background:rgba(0,255,159,0.08);border:1px solid rgba(0,255,159,0.3);border-radius:7px;color:var(--green);font-size:9px;font-weight:600;font-family:'Roboto Mono',monospace;cursor:pointer;letter-spacing:1px;transition:all .2s;margin-top:6px;}
#cov-btn.on{background:rgba(0,255,159,0.2);border-color:var(--green);}
.leg{display:flex;gap:6px;margin-top:5px;flex-wrap:wrap;}
.leg-i{display:flex;align-items:center;gap:3px;font-size:7px;font-family:'Roboto Mono',monospace;color:var(--dim);}
.leg-sq{width:9px;height:9px;border-radius:2px;}
</style></head><body>
<div id="layout"><div id="map"></div>
<div id="panel">
<div id="ph">
  <h1>PYXIS SENTINEL CACHE</h1>
  <div class="sub">PRE-CACHE SENTINEL-2 SATELLITE IMAGERY FOR OFFLINE USE</div>
  <div class="badge">COPERNICUS DATA SPACE &bull; S2-L2A &bull; 10m RESOLUTION</div>
</div>
<div id="vpos">
  <div class="vpi"><div class="vpl">VESSEL LAT</div><div class="vpv" id="vlat">--</div></div>
  <div class="vpi"><div class="vpl">VESSEL LON</div><div class="vpv" id="vlon">--</div></div>
  <div class="vpi"><div class="vpl">STATUS</div><div class="vpv" id="vstatus" style="color:var(--green)">READY</div></div>
</div>
<div id="pbody">

<div class="card"><div class="ctitle">Image Layer</div>
<div class="layer-grid" id="lgrid"></div></div>

<div class="card"><div class="ctitle"><span>Zoom Levels</span><span id="zsummary" style="color:var(--violet);font-size:7px;">z10 z11 z12</span></div>
<div class="zgrid" id="zgrid"></div></div>

<div class="card"><div class="ctitle">Cloud Cover Filter</div>
<div class="cloud-row">
  <span class="cloud-lbl">MAX CLOUD</span>
  <input type="range" id="maxcc-sl" min="5" max="100" value="20" oninput="updateMaxcc()"/>
  <span id="maxcc-val">20%</span>
</div>
<div style="font-size:7px;color:var(--dim);margin-top:4px;font-family:'Roboto Mono',monospace;">Lower = clearer images. Tiles skipped if clouds exceed threshold.</div>
</div>

<div class="card"><div class="ctitle">Live Preview & Coverage</div>
<button id="sat-prev-btn" onclick="toggleSatPreview()" style="width:100%;padding:8px;background:rgba(255,140,66,0.08);border:1px solid rgba(255,140,66,0.3);border-radius:7px;color:var(--orange);font-size:9px;font-weight:600;font-family:'Roboto Mono',monospace;cursor:pointer;letter-spacing:1px;transition:all .2s;margin-bottom:6px;">PREVIEW LIVE SATELLITE</button>
<div style="font-size:7px;color:var(--dim);font-family:'Roboto Mono',monospace;margin-bottom:6px;">Streams live Sentinel-2 directly on map. Uses free-tier quota.</div>
<button id="cov-btn" onclick="toggleCoverage()">SHOW CACHED TILE COVERAGE</button>
<div class="leg">
  <div class="leg-i"><div class="leg-sq" style="background:#00ff9f"></div>CACHED (FRESH)</div>
  <div class="leg-i"><div class="leg-sq" style="background:#ffb300"></div>STALE (>14d)</div>
  <div class="leg-i"><div class="leg-sq" style="background:#00aaff"></div>GDRIVE</div>
  <div class="leg-i"><div class="leg-sq" style="background:rgba(255,71,87,0.6)"></div>MISSING</div>
</div>
<div id="cov-status" style="font-size:7px;font-family:Roboto Mono,monospace;color:var(--dim);margin-top:4px;"></div>
</div>

<div class="card"><div class="ctitle">Coverage Area</div>
<div class="pgrid">
  <div class="pbtn" onclick="setRadius(10,this)">VESSEL +10nm</div>
  <div class="pbtn" onclick="setRadius(25,this)">VESSEL +25nm</div>
  <div class="pbtn" onclick="setRadius(50,this)">VESSEL +50nm</div>
  <div class="pbtn" onclick="setRadius(100,this)">VESSEL +100nm</div>
</div>
<button id="draw-btn" onclick="toggleDraw()">DRAW RECTANGLE ON MAP</button>
<div id="bbox-info">No area selected.</div></div>

<div class="card"><div class="ctitle">Estimate</div><div class="sgrid">
<div class="si"><div class="sn" id="stiles">0</div><div class="sl">TILES</div></div>
<div class="si"><div class="sn" id="smb">0</div><div class="sl">EST. MB</div></div>
<div class="si"><div class="sn" id="seta">--</div><div class="sl">EST. MIN</div></div>
</div>
<div style="font-size:7px;color:var(--dim);margin-top:5px;font-family:'Roboto Mono',monospace;">1 req/sec rate limit (free tier). Large areas may take hours. Already-cached tiles are skipped.</div>
</div>

<button id="dl-btn" onclick="startDL()" disabled>SELECT AREA FIRST</button>
<div id="pw"><div class="card"><div class="ctitle"><span>Downloading</span><span id="ppct" style="color:var(--orange)">0%</span></div>
<div class="pbb"><div class="pb" id="pbar"></div></div>
<div class="ptxt"><span id="pdone">0 / 0 tiles</span><span id="pskip" style="color:var(--dim)"></span><span id="perrs" style="color:var(--red)"></span></div>
</div></div>

<div class="card"><div class="ctitle">Job Log</div><div id="log"><div class="ll ok">Sentinel cache manager ready.</div></div></div>
</div></div></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.js"></script>
<script>
var vLat=__LAT__,vLon=__LON__,bbox=null,drawing=false,pollT=null,selRect=null;
var activeLayer='TRUE_COLOR',zooms=new Set([10,11,12]);
var LAYERS=[
  {id:'TRUE_COLOR',   name:'TRUE COLOR',   desc:'Natural RGB'},
  {id:'AGRICULTURE',  name:'AGRICULTURE',  desc:'Crop analysis'},
  {id:'BATHYMETRIC',  name:'BATHYMETRIC',  desc:'Coastal depth'},
  {id:'COLOR_INFRARED',name:'NIR',         desc:'Vegetation health'},
  {id:'GEOLOGY',      name:'GEOLOGY',      desc:'Rock/sediment'},
  {id:'MOISTURE_INDEX',name:'MOISTURE',    desc:'Water content'},
];
var ZMETA={8:{nm:'200nm'},9:{nm:'100nm'},10:{nm:'50nm'},11:{nm:'25nm'},12:{nm:'12nm'},13:{nm:'6nm',warn:true},14:{nm:'3nm',warn:true}};

var SH_WMS='https://sh.dataspace.copernicus.eu/ogc/wms/7ba55959-6207-4d1f-9ee7-f418f901bf08';
var map=L.map('map',{zoomControl:true,attributionControl:false}).setView([vLat,vLon],10);
var cartoDB=L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{maxZoom:20}).addTo(map);
L.tileLayer('https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png',{maxZoom:18,opacity:0.5}).addTo(map);
var sentinelWMS=null, satOn=false;
function buildSentinelWMS(layer,maxcc){
  return L.tileLayer.wms(SH_WMS,{layers:layer,format:'image/jpeg',transparent:false,version:'1.3.0',crs:L.CRS.EPSG4326,maxcc:maxcc||20,attribution:'Copernicus/ESA'});
}
var vi=L.divIcon({className:'',html:'<div style="font-size:20px;color:#ff8c42;filter:drop-shadow(0 0 5px #ff8c42);line-height:1;">&#9650;</div>',iconSize:[20,20],iconAnchor:[10,10]});
var vMark=L.marker([vLat,vLon],{icon:vi}).addTo(map);
var dI=new L.FeatureGroup();map.addLayer(dI);
var dC=new L.Control.Draw({draw:{rectangle:{shapeOptions:{color:'#ff8c42',fillColor:'rgba(255,140,66,0.1)',weight:2}},polyline:false,polygon:false,circle:false,marker:false,circlemarker:false},edit:{featureGroup:dI}});
map.on(L.Draw.Event.CREATED,function(e){dI.clearLayers();dI.addLayer(e.layer);var b=e.layer.getBounds();setBbox({N:b.getNorth(),S:b.getSouth(),E:b.getEast(),W:b.getWest()});drawing=false;var db=document.getElementById('draw-btn');db.classList.remove('on');db.textContent='DRAW RECTANGLE ON MAP';});
function toggleSatPreview(){
  var btn=document.getElementById('sat-prev-btn');
  if(satOn){
    if(sentinelWMS){map.removeLayer(sentinelWMS);sentinelWMS=null;}
    satOn=false;btn.classList.remove('on');btn.textContent='PREVIEW LIVE SATELLITE';
  }else{
    var maxcc=parseInt(document.getElementById('maxcc-sl').value)||20;
    sentinelWMS=buildSentinelWMS(activeLayer,maxcc).addTo(map);
    satOn=true;btn.classList.add('on');btn.textContent='HIDE SATELLITE PREVIEW';
    addLog('Live '+activeLayer+' layer on (uses free tier quota)','info');
  }
}
function refreshSatPreview(){
  if(!satOn)return;
  if(sentinelWMS){map.removeLayer(sentinelWMS);sentinelWMS=null;}
  var maxcc=parseInt(document.getElementById('maxcc-sl').value)||20;
  sentinelWMS=buildSentinelWMS(activeLayer,maxcc).addTo(map);
}

function buildLayers(){
  var g=document.getElementById('lgrid');g.innerHTML='';
  LAYERS.forEach(function(l){
    var d=document.createElement('div');
    d.className='lbtn'+(l.id===activeLayer?' on':'');
    d.innerHTML='<div class="ln">'+l.name+'</div><div class="ls">'+l.desc+'</div>';
    d.onclick=function(){activeLayer=l.id;document.querySelectorAll('.lbtn').forEach(function(e){e.classList.remove('on');});d.classList.add('on');addLog('Layer: '+l.name,'info');updateStats();refreshSatPreview();};
    g.appendChild(d);
  });
}
function buildZooms(){
  var g=document.getElementById('zgrid');g.innerHTML='';
  Object.keys(ZMETA).forEach(function(z){
    var zi=parseInt(z),m=ZMETA[z],d=document.createElement('div');
    d.className='zbtn'+(zooms.has(zi)?' on':'')+(m.warn?' warn':'');
    d.id='zb'+z;
    d.innerHTML='<div class="zz">z='+z+'</div><div class="znm">'+m.nm+'</div><div class="zt" id="zt'+z+'">-- t</div>';
    if(m.warn)d.title='Warning: high tile count, takes long at 1 req/sec';
    d.onclick=function(){toggleZ(zi);};
    g.appendChild(d);
  });
  updateStats();
}
function toggleZ(z){if(zooms.has(z))zooms.delete(z);else zooms.add(z);var el=document.getElementById('zb'+z);if(el)el.className='zbtn'+(zooms.has(z)?' on':'')+(ZMETA[z].warn?' warn':'');document.getElementById('zsummary').textContent=Array.from(zooms).sort().map(function(z){return'z'+z;}).join(' ')||'none';updateStats();}
function updateMaxcc(){var v=document.getElementById('maxcc-sl').value;document.getElementById('maxcc-val').textContent=v+'%';}
function tc(N,S,E,W,z){var n=Math.pow(2,z),lo=function(l){return Math.floor((l+180)/360*n);},la=function(a){var r=a*Math.PI/180;return Math.floor((1-Math.log(Math.tan(r)+1/Math.cos(r))/Math.PI)/2*n);};return(Math.abs(lo(E)-lo(W))+1)*(Math.abs(la(N)-la(S))+1);}
function updateStats(){
  if(!bbox){resetStats();return;}
  var tot=0;
  zooms.forEach(function(z){var t=tc(bbox.N,bbox.S,bbox.E,bbox.W,z);tot+=t;var el=document.getElementById('zt'+z);if(el)el.textContent=t>0?t+' t':'-- t';});
  Object.keys(ZMETA).forEach(function(z){if(!zooms.has(parseInt(z))){var el=document.getElementById('zt'+z);if(el)el.textContent='-- t';}});
  var mb=(tot*15/1024).toFixed(1),eta=(tot/60).toFixed(0);
  document.getElementById('stiles').textContent=tot.toLocaleString();
  document.getElementById('smb').textContent=mb;
  document.getElementById('seta').textContent=eta;
  var db=document.getElementById('dl-btn'),ok=tot>0&&zooms.size>0;
  db.disabled=!ok;db.textContent=ok?'CACHE '+tot.toLocaleString()+' TILES':'SELECT AREA FIRST';
}
function resetStats(){document.getElementById('stiles').textContent='0';document.getElementById('smb').textContent='0';document.getElementById('seta').textContent='--';document.getElementById('dl-btn').disabled=true;document.getElementById('dl-btn').textContent='SELECT AREA FIRST';Object.keys(ZMETA).forEach(function(z){var el=document.getElementById('zt'+z);if(el)el.textContent='-- t';});}
function setBbox(b){bbox=b;document.getElementById('bbox-info').innerHTML='<span style="color:#aaa">N </span><b style="color:var(--orange)">'+b.N.toFixed(4)+'</b>  <span style="color:#aaa">S </span><b style="color:var(--orange)">'+b.S.toFixed(4)+'</b>  <span style="color:#aaa">E </span><b style="color:var(--orange)">'+b.E.toFixed(4)+'</b>  <span style="color:#aaa">W </span><b style="color:var(--orange)">'+b.W.toFixed(4)+'</b>';updateStats();}
function setRadius(nm,el){var deg=nm/60,ld=deg/Math.cos(vLat*Math.PI/180),b={N:vLat+deg,S:vLat-deg,E:vLon+ld,W:vLon-ld};dI.clearLayers();if(selRect)map.removeLayer(selRect);selRect=L.rectangle([[b.S,b.W],[b.N,b.E]],{color:'#ff8c42',fillColor:'rgba(255,140,66,0.08)',weight:2}).addTo(map);map.fitBounds([[b.S,b.W],[b.N,b.E]],{padding:[20,20]});document.querySelectorAll('.pbtn').forEach(function(e){e.classList.remove('on');});el.classList.add('on');setBbox(b);addLog('Area: vessel +'+nm+'nm','info');}
function toggleDraw(){if(drawing){map.removeControl(dC);drawing=false;var db=document.getElementById('draw-btn');db.classList.remove('on');db.textContent='DRAW RECTANGLE ON MAP';}else{map.addControl(dC);new L.Draw.Rectangle(map,dC.options.draw.rectangle).enable();drawing=true;var db=document.getElementById('draw-btn');db.classList.add('on');db.textContent='DRAG TO SELECT AREA...';document.querySelectorAll('.pbtn').forEach(function(e){e.classList.remove('on');});}}
async function startDL(){
  if(!bbox||!zooms.size)return;
  var zs=Array.from(zooms).sort(),maxcc=parseInt(document.getElementById('maxcc-sl').value);
  document.getElementById('dl-btn').disabled=true;document.getElementById('pw').style.display='block';
  addLog('Starting: '+activeLayer+' z='+zs.join(',')+' MAXCC='+maxcc+'%','info');
  try{
    var r=await fetch('/fetch_sentinel_region',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Basic '+btoa('admin:manta')},body:JSON.stringify({bbox:{N:+bbox.N,S:+bbox.S,E:+bbox.E,W:+bbox.W},zooms:zs,layer:activeLayer,maxcc:maxcc})});
    var d=await r.json();addLog('Job '+d.job_id+' queued','info');
    pollT=setInterval(function(){pollJob(d.job_id);},1500);
  }catch(e){addLog('ERROR: '+e,'err');document.getElementById('dl-btn').disabled=false;}
}
async function pollJob(id){
  try{
    var r=await fetch('/sentinel_job_status/'+id,{headers:{'Authorization':'Basic '+btoa('admin:manta')}});
    var d=await r.json();
    var tot=d.total||1,done=d.done+d.skipped,p=Math.round(done/tot*100);
    document.getElementById('pbar').style.width=p+'%';document.getElementById('ppct').textContent=p+'%';
    document.getElementById('pdone').textContent=d.done+' fetched / '+tot+' total';
    document.getElementById('pskip').textContent=d.skipped?' ('+d.skipped+' cached)':'';
    if(d.errors>0)document.getElementById('perrs').textContent=d.errors+' err';
    if(d.status==='done'){clearInterval(pollT);addLog('Done: '+d.done+' fetched, '+d.skipped+' skipped, '+d.errors+' errors',d.errors===0?'ok':'err');document.getElementById('dl-btn').disabled=false;document.getElementById('dl-btn').textContent='CACHE MORE';}
  }catch(e){}
}
async function fetchVessel(){try{var r=await fetch('/status_api?bust='+Date.now());var d=await r.json();if(d.lat&&d.lat!==0){vLat=d.lat;vLon=d.lon;}vMark.setLatLng([vLat,vLon]);document.getElementById('vlat').textContent=vLat.toFixed(4);document.getElementById('vlon').textContent=vLon.toFixed(4);}catch(e){}}
function addLog(msg,cls){var el=document.createElement('div');el.className='ll '+(cls||'');el.textContent=new Date().toTimeString().slice(0,8)+' '+msg;var l=document.getElementById('log');l.insertBefore(el,l.firstChild);if(l.children.length>20)l.removeChild(l.lastChild);}
var covLayers=[],covOn=false;
var COV_COLORS={fresh:'rgba(0,255,159,0.25)',stale:'rgba(255,179,0,0.3)',gdrive:'rgba(0,170,255,0.25)',missing:'rgba(255,71,87,0.18)'};
var COV_BORDERS={fresh:'#00ff9f',stale:'#ffb300',gdrive:'#00aaff',missing:'rgba(255,71,87,0.7)'};
async function toggleCoverage(){
  covOn=!covOn;var btn=document.getElementById('cov-btn');
  covLayers.forEach(function(l){map.removeLayer(l);});covLayers=[];
  if(!covOn){btn.classList.remove('on');btn.textContent='SHOW SENTINEL COVERAGE';document.getElementById('cov-status').textContent='';return;}
  btn.classList.add('on');btn.textContent='LOADING...';document.getElementById('cov-status').textContent='Scanning sentinel tile cache...';
  try{
    var zs=[10,11,12],nm=25;
    var url='/sentinel_cache_coverage?lat='+vLat+'&lon='+vLon+'&nm='+nm+'&zooms='+zs.join(',')+'&bust='+Date.now();
    var r=await fetch(url,{headers:{'Authorization':'Basic '+btoa('admin:manta')}});var d=await r.json();
    if(d.error){document.getElementById('cov-status').textContent='Error: '+d.error;covOn=false;btn.classList.remove('on');btn.textContent='SHOW SENTINEL COVERAGE';return;}
    var cnt={fresh:0,stale:0,gdrive:0,missing:0};
    d.tiles.forEach(function(t){
      var rect=L.rectangle([[t.lat_s,t.lon_w],[t.lat_n,t.lon_e]],{color:COV_BORDERS[t.status]||'#888',fillColor:COV_COLORS[t.status]||'rgba(128,128,128,0.15)',weight:0.5,fillOpacity:1,interactive:false});
      rect.addTo(map);covLayers.push(rect);cnt[t.status]=(cnt[t.status]||0)+1;
    });
    document.getElementById('cov-status').textContent='Fresh:'+cnt.fresh+' Stale:'+cnt.stale+' GD:'+cnt.gdrive+' Miss:'+cnt.missing;
    btn.textContent='HIDE COVERAGE ('+d.total+' tiles)';
    addLog('Coverage: '+d.total+' tiles at '+nm+'nm','info');
  }catch(e){document.getElementById('cov-status').textContent='Error: '+e;covOn=false;btn.classList.remove('on');btn.textContent='SHOW SENTINEL COVERAGE';}
}
buildLayers();buildZooms();fetchVessel();setInterval(fetchVessel,10000);
</script></body></html>"""


@app.route('/sentinel_manager')
@requires_auth
def sentinel_manager():
    global last_known_lat, last_known_lon
    html = _SENTINEL_MGR_HTML.replace('__LAT__', str(last_known_lat or -38.487)).replace('__LON__', str(last_known_lon or 145.620))
    resp = make_response(html)
    resp.headers.set('Content-Type', 'text/html; charset=utf-8')
    return resp

def _fetch_region_worker(job_id, bbox, zooms, layers):
    """Background thread: pre-fetches all tiles for a bbox+zoom set into tile cache."""
    import math
    job = _tile_jobs[job_id]
    tpls = []
    if 'nautical' in layers:
        tpls += [(_OCEAN_BASE_URL, False), (_OCEAN_REF_URL, True), (_SEAMARK_URL, True)]
    if 'topo' in layers:
        tpls += [(_GA_TOPO_URL, False)]

    # Count total tiles
    total = 0
    tile_list = []
    for z in zooms:
        n = 2 ** z
        def to_tx(lon): return int((lon + 180) / 360 * n)
        def to_ty(lat):
            import math as _m
            r = _m.radians(lat)
            return int((1 - _m.log(_m.tan(r) + 1/_m.cos(r)) / _m.pi) / 2 * n)
        x0, x1 = to_tx(bbox['W']), to_tx(bbox['E'])
        y0, y1 = to_ty(bbox['N']), to_ty(bbox['S'])
        for tpl, rgba in tpls:
            for ty in range(y0, y1 + 1):
                if ty < 0 or ty >= n: continue
                for tx in range(x0, x1 + 1):
                    tile_list.append((tpl.format(z=z, tx=tx % n, ty=ty), rgba))
    job['total'] = len(tile_list)
    job['status'] = 'running'

    from concurrent.futures import ThreadPoolExecutor
    done = [0]; errs = [0]
    def fetch_one(args):
        url, rgba = args
        try: _fetch_map_tile(url, rgba); done[0] += 1
        except Exception: errs[0] += 1
        job['done'] = done[0]; job['errors'] = errs[0]

    with ThreadPoolExecutor(max_workers=15) as ex:
        ex.map(fetch_one, tile_list)
    job['status'] = 'done'
    log(f"TILE PREWARM {job_id}: {done[0]}/{job['total']} cached, {errs[0]} errors")


@app.route('/fetch_tile_region', methods=['POST'])
@requires_auth
def fetch_tile_region():
    data = request.get_json(force=True)
    job_id = str(uuid.uuid4())[:8]
    _tile_jobs[job_id] = {'status': 'queued', 'done': 0, 'total': 0, 'errors': 0}
    t = threading.Thread(target=_fetch_region_worker,
                         args=(job_id, data['bbox'], data['zooms'], data['layers']),
                         daemon=True)
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/tile_job_status/<job_id>')
@requires_auth
def tile_job_status(job_id):
    return jsonify(_tile_jobs.get(job_id, {'status': 'unknown', 'done': 0, 'total': 0, 'errors': 0}))


@app.route('/topo_explorer')
def topo_explorer():
    """Interactive topo map explorer — click anywhere in Australia to centre the topo view."""
    global last_known_lat, last_known_lon
    vlat = round(last_known_lat, 4)
    vlon = round(last_known_lon, 4)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Pyxis Topo Explorer</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{background:#080c10;color:#c8ffd4;font-family:'Share Tech Mono',monospace;height:100vh;display:flex;flex-direction:column;overflow:hidden;}}
header{{background:rgba(0,255,100,0.07);border-bottom:1px solid rgba(0,255,100,0.18);padding:10px 20px;display:flex;align-items:center;gap:16px;flex-shrink:0;}}
h1{{font-family:'Rajdhani',sans-serif;font-size:1.3rem;font-weight:700;color:#00ff88;letter-spacing:4px;text-transform:uppercase;}}
.tag{{font-size:.65rem;color:#4aff90;opacity:.7;}}
.main{{display:flex;flex:1;overflow:hidden;}}
#map{{flex:1.3;height:100%;}}
#topo-panel{{width:510px;flex-shrink:0;background:rgba(0,20,10,0.95);border-left:1px solid rgba(0,255,100,0.15);display:flex;flex-direction:column;}}
.topo-hdr{{padding:12px 16px;border-bottom:1px solid rgba(0,255,100,0.15);background:rgba(0,255,100,0.05);}}
.topo-hdr h2{{font-family:'Rajdhani',sans-serif;font-size:1rem;font-weight:600;color:#00ff88;letter-spacing:3px;}}
#coord-display{{font-size:.72rem;color:#4aff90;margin-top:3px;opacity:.85;}}
.zoom-bar{{padding:10px 16px;border-bottom:1px solid rgba(0,255,100,0.1);display:flex;align-items:center;gap:6px;flex-wrap:wrap;}}
.zoom-bar span{{font-size:.68rem;color:#4aff90;opacity:.7;margin-right:2px;}}
.zb{{background:rgba(0,255,100,0.08);border:1px solid rgba(0,255,100,0.25);color:#00ff88;padding:4px 9px;font-family:'Share Tech Mono',monospace;font-size:.68rem;cursor:pointer;border-radius:3px;transition:.15s;}}
.zb:hover{{background:rgba(0,255,100,0.22);border-color:#00ff88;}}
.zb.active{{background:rgba(0,255,100,0.28);border-color:#00ff88;color:#fff;font-weight:bold;}}
.vessel-btn{{background:rgba(0,150,255,0.1);border:1px solid rgba(0,150,255,0.35);color:#4aadff;padding:4px 10px;font-family:'Share Tech Mono',monospace;font-size:.68rem;cursor:pointer;border-radius:3px;transition:.15s;margin-left:auto;}}
.vessel-btn:hover{{background:rgba(0,150,255,0.22);}}
#img-wrap{{flex:1;display:flex;align-items:center;justify-content:center;padding:14px;overflow:hidden;position:relative;background:radial-gradient(ellipse at center,#0a1a0e 0%,#050d08 100%);}}
#topo-img{{max-width:100%;max-height:100%;border:1px solid rgba(0,255,100,0.2);border-radius:4px;display:none;}}
#status{{position:absolute;text-align:center;pointer-events:none;}}
.idle{{color:#4aff90;opacity:.45;font-size:.8rem;}}
.loading{{color:#00ff88;font-size:.82rem;animation:pulse 1s infinite;}}
.err{{color:#ff4444;font-size:.78rem;}}
@keyframes pulse{{0%,100%{{opacity:.55}}50%{{opacity:1}}}}
.scale-tag{{font-size:.65rem;color:#00cc66;padding:5px 16px;border-top:1px solid rgba(0,255,100,0.08);text-align:center;opacity:.8;}}
</style>
</head>
<body>
<header>
  <div>
    <h1>&#9651; PYXIS TOPO EXPLORER</h1>
    <div class="tag">TOPOGRAPHIC RECONNAISSANCE &bull; CLICK MAP TO PLACE MARKER &bull; USE ZOOM CONTROLS TO CHANGE SCALE</div>
  </div>
</header>
<div class="main">
  <div id="map"></div>
  <div id="topo-panel">
    <div class="topo-hdr">
      <h2>TOPO RENDER</h2>
      <div id="coord-display">Click map to select position</div>
    </div>
    <div class="zoom-bar">
      <span>ZOOM:</span>
      <button class="zb" onclick="setZ(10)">z10 ~10nm</button>
      <button class="zb" onclick="setZ(11)">z11 ~5nm</button>
      <button class="zb active" onclick="setZ(12)">z12 ~2nm</button>
      <button class="zb" onclick="setZ(13)">z13 ~1nm</button>
      <button class="zb" onclick="setZ(14)">z14 ~0.5nm</button>
      <button class="zb" onclick="setZ(15)">z15 ~0.2nm</button>
      <button class="vessel-btn" onclick="goVessel()">&#9654; VESSEL</button>
    </div>
    <div id="img-wrap">
      <img id="topo-img" alt="Topo">
      <div id="status"><div class="idle">&#9651; SELECT A POSITION ON THE MAP</div></div>
    </div>
    <div class="scale-tag" id="scale-tag">&mdash;</div>
  </div>
</div>
<script>
var VL={vlat}, VN={vlon};
var cLat=null, cLon=null, cZ=12, mk=null;
var scales={{10:'~10nm view',11:'~5nm view',12:'~2nm view',13:'~1nm view',14:'~0.5nm view',15:'~0.2nm view'}};

var map = L.map('map').setView([-25.5,133.7],5);
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}.png',{{maxZoom:18}}).addTo(map);

var vIcon=L.divIcon({{className:'',html:'<div style="width:13px;height:13px;background:#00aaff;border:2px solid #fff;border-radius:50%;box-shadow:0 0 8px #00aaff80"></div>',iconSize:[13,13],iconAnchor:[6,6]}});
L.marker([VL,VN],{{icon:vIcon}}).addTo(map).bindPopup('<b style="color:#00ff88">PYXIS</b><br>'+VL+', '+VN);

var rIcon=L.divIcon({{className:'',html:'<div style="width:15px;height:15px;background:#ff3333;border:2px solid #fff;border-radius:50%;box-shadow:0 0 10px #ff333380"></div>',iconSize:[15,15],iconAnchor:[7,7]}});

map.on('click',function(e){{
  place(parseFloat(e.latlng.lat.toFixed(5)),parseFloat(e.latlng.lng.toFixed(5)));
}});

function place(lat,lon){{
  cLat=lat; cLon=lon;
  if(mk)map.removeLayer(mk);
  mk=L.marker([lat,lon],{{icon:rIcon}}).addTo(map);
  document.getElementById('coord-display').textContent='LAT '+lat.toFixed(5)+'\u00b0   LON '+lon.toFixed(5)+'\u00b0';
  fetch_topo();
}}

function setZ(z){{
  cZ=z;
  document.querySelectorAll('.zb').forEach(function(b){{b.classList.toggle('active',b.textContent.startsWith('z'+z));}});
  if(cLat!==null)fetch_topo();
}}

function goVessel(){{map.setView([VL,VN],8);place(VL,VN);}}

function fetch_topo(){{
  if(cLat===null)return;
  var img=document.getElementById('topo-img');
  img.style.display='none';
  document.getElementById('status').innerHTML='<div class="loading">&#9651; RENDERING TOPOGRAPHIC DATA...</div>';
  document.getElementById('scale-tag').textContent='\u2014';
  var url='/topo_map/'+Date.now()+'/topo.jpg?z='+cZ+'&lat='+cLat+'&lon='+cLon+'&w=460&h=460';
  img.onload=function(){{img.style.display='block';document.getElementById('status').innerHTML='';document.getElementById('scale-tag').textContent=scales[cZ]||'';}};
  img.onerror=function(){{document.getElementById('status').innerHTML='<div class="err">&#9651; RENDER FAILED \u2014 CHECK PROXY LOGS</div>';}};
  img.src=url;
}}
</script>
</body>
</html>"""
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/topo_map')
@app.route('/topo_map/<path:dummy>')
def topo_map(dummy=None):
    """
    Garmin watch endpoint: renders an OpenTopoMap topographic map centred on
    the vessel position.  Zoom z=10 (~10nm) to z=14 (<1nm).
    Uses a 3-tier tile cache (SSD → GDrive → opentopomap.org).
    Stale cache entries are served immediately while a background refresh runs,
    preventing request timeouts on cold/uncached tiles.
    """
    import io as _io
    global last_known_lat, last_known_lon
    try:
        lat  = float(request.args.get('lat', last_known_lat))
        lon  = float(request.args.get('lon', last_known_lon))
        z    = max(10, min(15, int(request.args.get('z', 12))))
        w    = int(request.args.get('w', 320))
        h    = int(request.args.get('h', 320))
        lr, lnr = round(lat, 2), round(lon, 2)

        with _topo_cache_lk:
            c = _topo_cache.get(z)

        # ── Fresh cache hit (≤ 30 min and same position) ──────────────────────
        if c and time.time() - c["ts"] < 1800 and c["lat"] == lr and c["lon"] == lnr:
            return _map_response(c["img"])

        # ── Stale cache exists: serve immediately + refresh in background ─────
        if c and c.get("img"):
            def _bg_refresh():
                try:
                    img  = _render_tile_mosaic(lat, lon, z, w, h, _GA_TOPO_URL)
                    buf  = _io.BytesIO()
                    img.save(buf, format='JPEG', quality=75, optimize=True)
                    out  = buf.getvalue()
                    with _topo_cache_lk:
                        _topo_cache[z] = {"ts": time.time(), "img": out, "lat": lr, "lon": lnr}
                    log(f"Topo Map: background refresh complete z={z}")
                except Exception as be:
                    log(f"Topo Map BG refresh error z={z}: {be}")
            threading.Thread(target=_bg_refresh, daemon=True).start()
            log(f"Topo Map: serving stale cache (z={z}) while refreshing in background")
            return _map_response(c["img"])

        # ── No cache at all: blocking render (first request per zoom level) ───
        img  = _render_tile_mosaic(lat, lon, z, w, h, _GA_TOPO_URL)
        buf  = _io.BytesIO()
        img.save(buf, format='JPEG', quality=75, optimize=True)
        out  = buf.getvalue()
        with _topo_cache_lk:
            _topo_cache[z] = {"ts": time.time(), "img": out, "lat": lr, "lon": lnr}
        return _map_response(out)
    except Exception as e:
        log(f"Topo Map Error: {e}")
        # Last-resort: serve stale cache even on exception
        with _topo_cache_lk:
            c = _topo_cache.get(z if 'z' in dir() else 12)
        if c and c.get("img"):
            return _map_response(c["img"])
        return "Error", 500


@app.route('/nautical_map')
@app.route('/nautical_map/<path:dummy>')
def nautical_map(dummy=None):
    """
    Garmin watch endpoint: renders CartoDB Dark basemap composited with
    OpenSeaMap seamark overlay (buoys, lights, wrecks, traffic lanes).
    Zoom z=10 (~10nm) to z=13 (~1nm).
    """
    import io as _io
    global last_known_lat, last_known_lon
    try:
        lat  = float(request.args.get('lat', last_known_lat))
        lon  = float(request.args.get('lon', last_known_lon))
        z    = max(10, min(14, int(request.args.get('z', 12))))
        w    = int(request.args.get('w', 320))
        h    = int(request.args.get('h', 320))
        lr, lnr = round(lat, 2), round(lon, 2)

        with _naut_cache_lk:
            c = _naut_cache.get(z)
        if c and time.time() - c["ts"] < 1800 and c["lat"] == lr and c["lon"] == lnr:
            return _map_response(c["img"])

        img  = _render_tile_mosaic(lat, lon, z, w, h,
                                    _OCEAN_BASE_URL,
                                    [_OCEAN_REF_URL, _SEAMARK_URL])
        buf  = _io.BytesIO()
        img.save(buf, format='JPEG', quality=75, optimize=True)
        out  = buf.getvalue()
        with _naut_cache_lk:
            _naut_cache[z] = {"ts": time.time(), "img": out, "lat": lr, "lon": lnr}
        return _map_response(out)
    except Exception as e:
        log(f"Nautical Map Error: {e}")
        return "Error", 500

# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


@app.route('/voice')
def voice_interface():
    import os as _os
    _p = _os.path.join(_os.path.dirname(__file__), 'pyxis_voice.html')
    try:
        with open(_p, 'r', encoding='utf-8') as _fh: _html = _fh.read()
        _html = _html.replace('BENFISH_ACTUAL_77X', 'PYXIS_ACTUAL_77X')
    except Exception: _html = '<h1>pyxis_voice.html not found</h1>'
    resp = make_response(_html)
    resp.headers.set('Content-Type', 'text/html; charset=utf-8')
    return resp

if __name__ == '__main__':


    # ================================================================
    # MARINE OSINT API WORKERS
    # ================================================================

    def swpc_worker():
        """NOAA Space Weather Prediction Center - Solar flares, Kp index, radio blackouts."""
        CACHE = os.path.join(B, "swpc_cache.json")
        while True:
            try:
                kp = requests.get("https://services.swpc.noaa.gov/json/planetary_k_index_1m.json", timeout=10).json()
                alerts = requests.get("https://services.swpc.noaa.gov/products/alerts.json", timeout=10).json()
                latest_kp = kp[-1] if kp else {}
                active_alerts = [a for a in alerts if isinstance(a, dict) and a.get("issue_datetime")][:3]
                data = {"kp_index": latest_kp.get("kp_index", "N/A"), "kp_time": latest_kp.get("time_tag", ""),
                        "alerts": [a.get("message", "")[:200] for a in active_alerts], "updated": time.time()}
                with open(CACHE, "w") as f: json.dump(data, f)
                log(f"SWPC Worker: Cached Planetary K-Index {data['kp_index']}")
            except Exception as e:
                log(f"SWPC Worker Error: {e}")
            time.sleep(3600)

    def asam_worker():
        """NGA Anti-Shipping Activity Messages - Global piracy/hostile acts database."""
        CACHE = os.path.join(B, "asam_cache.json")
        while True:
            try:
                r = requests.get("https://msi.nga.mil/api/publications/asam?output=json", timeout=20)
                events = r.json() if r.status_code == 200 else []
                # Keep the 20 most recent events with location info
                recent = [e for e in events if e.get("latitude") and e.get("longitude")][:20]
                data = {"count": len(events), "recent": recent, "updated": time.time()}
                with open(CACHE, "w") as f: json.dump(data, f)
                log(f"ASAM Worker: Cached {len(recent)} recent piracy events")
            except Exception as e:
                log(f"ASAM Worker Error: {e}")
            time.sleep(3600)

    def seismic_worker():
        """USGS Earthquake Feed - Real-time seismic & potential tsunami alerts."""
        CACHE = os.path.join(B, "seismic_cache.json")
        while True:
            try:
                r = requests.get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson", timeout=10)
                feed = r.json()
                features = feed.get("features", [])
                significant = [f["properties"] for f in features if f["properties"].get("mag", 0) >= 4.0]
                data = {"total": len(features), "significant": significant, "tsunami_watch": any(f.get("tsunami", 0) for f in significant), "updated": time.time()}
                with open(CACHE, "w") as f: json.dump(data, f)
                if significant:
                    log(f"USGS Worker: {len(significant)} M4+ events, tsunami={'YES' if data['tsunami_watch'] else 'NO'}")
            except Exception as e:
                log(f"USGS Worker Error: {e}")
            time.sleep(900)  # Every 15 minutes

    def meteo_worker():
        """Open-Meteo Marine API - Wave heights, swell, ocean current from Copernicus models."""
        CACHE = os.path.join(B, "meteo_cache.json")
        while True:
            try:
                lat = last_known_lat or -38.487
                lon = last_known_lon or 145.620
                url = (f"https://marine-api.open-meteo.com/v1/marine"
                       f"?latitude={lat}&longitude={lon}"
                       f"&current=wave_height,wave_direction,wave_period,wind_wave_height,swell_wave_height,swell_wave_direction,swell_wave_period,ocean_current_velocity,ocean_current_direction"
                       f"&wind_speed_unit=kn&length_unit=metric")
                r = requests.get(url, timeout=15)
                current = r.json().get("current", {}) if r.status_code == 200 else {}
                data = {"marine": current, "updated": time.time()}
                with open(CACHE, "w") as f: json.dump(data, f)
                log(f"Meteo Worker: Wave {current.get('wave_height')}m, Swell {current.get('swell_wave_height')}m, Current {current.get('ocean_current_velocity')}kn @ {current.get('ocean_current_direction')}deg")
            except Exception as e:
                log(f"Meteo Worker Error: {e}")
            time.sleep(1800)  # Every 30 minutes

    def bathymetry_worker():
        """OpenTopoData ETOPO1 - Ocean depth beneath the keel for grounding warnings."""
        CACHE = os.path.join(B, "bathymetry_cache.json")
        last_lat, last_lon = None, None
        while True:
            try:
                lat = last_known_lat or -38.487
                lon = last_known_lon or 145.620
                # Only re-query if we've moved more than ~1nm from the last query
                if last_lat is None or abs(lat - last_lat) > 0.02 or abs(lon - last_lon) > 0.02:
                    url = f"https://api.opentopodata.org/v1/etopo1?locations={lat:.4f},{lon:.4f}"
                    r = requests.get(url, timeout=10)
                    result = r.json().get("results", [{}])[0] if r.status_code == 200 else {}
                    elevation = result.get("elevation", None)
                    depth_m = abs(elevation) if elevation is not None and elevation < 0 else 0
                    data = {"lat": lat, "lon": lon, "elevation_m": elevation,
                            "depth_m": depth_m, "is_water": elevation is not None and elevation < 0,
                            "grounding_risk": depth_m < 10 and elevation is not None and elevation < 0,
                            "updated": time.time()}
                    with open(CACHE, "w") as f: json.dump(data, f)
                    last_lat, last_lon = lat, lon
                    log(f"Bathymetry Worker: Depth {depth_m:.0f}m at ({lat:.3f}, {lon:.3f})")
            except Exception as e:
                log(f"Bathymetry Worker Error: {e}")
            time.sleep(120)  # Check every 2 minutes (only re-queries if moved)

    def marine_alerts_worker():
        """Generates marine weather alerts from live data.
        Primary:   Beaufort-scale thresholds from CMEMS wave cache + telemetry wind speed.
        Secondary: BOM VIC Marine Warnings RSS feed (best-effort, Australian waters).
        Dedup (#9): only writes to disk when alert severity fingerprint changes.
        """
        CACHE        = os.path.join(B, "weather_alerts_cache.json")
        TELEM        = os.path.join(B, "sim_telemetry.json")
        CMEMS        = os.path.join(B, "currents_grid_cache.json")
        BOM_RSS      = "http://www.bom.gov.au/rss/alerts/warning_vic.xml"
        MARINE_KW    = {"marine", "coastal", "wind", "swell", "gale", "port phillip", "bass strait"}

        last_fingerprint = ""   # deduplication state

        while True:
            alerts   = []
            location = "Port Phillip / Bass Strait, VIC, AU"

            # Ã¢â€â‚¬Ã¢â€â‚¬ 1. Beaufort-scale threshold alerts from live data Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
            try:
                wind_kn = 0.0
                wave_h  = 0.0
                if os.path.exists(TELEM):
                    with open(TELEM) as f:
                        td = json.load(f)
                    wind_kn = float(td.get("wind_speed") or 0)
                if os.path.exists(CMEMS):
                    with open(CMEMS) as f:
                        cd = json.load(f)
                    wave_h = float(cd.get("vessel_wave", {}).get("wave_h") or 0)

                # Wind alerts (Beaufort)
                if wind_kn >= 48:
                    alerts.append({"type": "STORM WARNING",       "severity": "Extreme",
                                   "text": f"Storm force winds {wind_kn:.0f}kn. All mariners seek shelter immediately."})
                elif wind_kn >= 34:
                    alerts.append({"type": "GALE WARNING",        "severity": "High",
                                   "text": f"Gale force winds {wind_kn:.0f}kn. Dangerous conditions for all vessels."})
                elif wind_kn >= 21:
                    alerts.append({"type": "STRONG WIND WARNING", "severity": "Moderate",
                                   "text": f"Strong winds {wind_kn:.0f}kn. Small craft should remain in port."})
                elif wind_kn >= 11:
                    alerts.append({"type": "SMALL CRAFT ADVISORY","severity": "Low",
                                   "text": f"Wind {wind_kn:.0f}kn. Small craft exercise caution."})

                # Wave/swell alerts
                if wave_h >= 4.0:
                    alerts.append({"type": "VERY ROUGH SEAS",      "severity": "High",
                                   "text": f"Wave height {wave_h:.1f}m. Very rough seas, extreme caution."})
                elif wave_h >= 2.5:
                    alerts.append({"type": "ROUGH SEAS",           "severity": "Moderate",
                                   "text": f"Wave height {wave_h:.1f}m. Rough sea state, monitor conditions closely."})
                elif wave_h >= 1.25:
                    alerts.append({"type": "SLIGHT-MODERATE SWELL","severity": "Low",
                                   "text": f"Wave height {wave_h:.1f}m. Slight to moderate swell in operating area."})

            except Exception as e:
                log(f"Marine Alerts Worker: threshold calc error: {e}")

            # Ã¢â€â‚¬Ã¢â€â‚¬ 2. BOM VIC Marine Warnings RSS (#8) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
            try:
                import xml.etree.ElementTree as ET
                bom_r = requests.get(BOM_RSS, timeout=8, headers={"User-Agent": "Pyxis-Marine/1.0"})
                if bom_r.status_code == 200:
                    root = ET.fromstring(bom_r.content)
                    for item in root.findall(".//item")[:10]:
                        title = (item.findtext("title") or "").strip()
                        descr = (item.findtext("description") or "").strip()
                        if any(kw in title.lower() for kw in MARINE_KW):
                            alerts.append({"type": "BOM WARNING", "severity": "High",
                                           "text": f"{title}: {descr[:180]}"})
            except Exception:
                pass  # BOM RSS is secondary Ã¢â‚¬â€ threshold alerts always run first

            # Ã¢â€â‚¬Ã¢â€â‚¬ 3. All-clear fallback Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
            if not alerts:
                alerts.append({"type": "MARINE FORECAST", "severity": "None",
                               "text": "No active warnings. Conditions suitable for passage."})

            # Ã¢â€â‚¬Ã¢â€â‚¬ 4. Deduplication (#9) Ã¢â‚¬â€ skip write if unchanged Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
            fingerprint = "|".join(sorted(a["type"] for a in alerts))
            if fingerprint == last_fingerprint:
                log(f"Marine Alerts Worker: no change ({fingerprint[:60]}) Ã¢â‚¬â€ skip write")
            else:
                data = {
                    "updated":  time.time(),
                    "location": location,
                    "alerts":   alerts,
                    "stations": [],
                }
                try:
                    with open(CACHE, "w") as f:
                        json.dump(data, f, indent=2)
                    log(f"Marine Alerts Worker: {len(alerts)} alert(s) written [{fingerprint[:60]}]")
                    last_fingerprint = fingerprint
                except Exception as e:
                    log(f"Marine Alerts Worker: write error: {e}")

            time.sleep(1800)  # refresh every 30 minutes

    def system_reporter_worker():
        """Pushes system health + CMEMS data events to the watch inbox.
        - Startup : full health summary after 20s (other workers settle first)
        - CMEMS   : 'DATA LIVE' message whenever a new CMEMS download completes
        - Periodic: compact health summary every 30 minutes
        """
        global inbox_messages
        CMEMS_PATH   = os.path.join(B, "currents_grid_cache.json")
        HEALTH_PATHS = {
            "CMEMS":  os.path.join(B, "currents_grid_cache.json"),
            "Meteo":  os.path.join(B, "meteo_cache.json"),
            "Alerts": os.path.join(B, "weather_alerts_cache.json"),
            "Depth":  os.path.join(B, "bathymetry_cache.json"),
        }

        def _push(source, msg):
            with inbox_lock:
                inbox_messages.append({"ts": int(time.time()), "source": source, "message": msg})
                if len(inbox_messages) > 50:
                    inbox_messages[:] = inbox_messages[-50:]
            log(f"INBOX [{source}]: {msg[:80]}")

        def _age_min(path):
            try:
                with open(path) as f:
                    d = json.load(f)
                ts = os.path.getmtime(path) if isinstance(d, list) else float(d.get("updated", 0))
                return round((time.time() - ts) / 60)
            except Exception:
                return None

        def _health_msg(prefix="SYSTEMS STATUS"):
            parts = [prefix]
            for name, path in HEALTH_PATHS.items():
                age = _age_min(path)
                if age is None:     parts.append(f"{name}:OFFLINE")
                elif age < 120:     parts.append(f"{name}:OK({age}m)")
                else:               parts.append(f"{name}:STALE({age}m)")
            # Brief wave summary if available
            try:
                with open(CMEMS_PATH) as f:
                    wv = json.load(f).get("vessel_wave", {})
                wh = wv.get("wave_h"); cv = wv.get("curr_v")
                if wh: parts.append(f"wave={wh}m curr={cv or 'n/a'}kn")
            except Exception:
                pass
            return " | ".join(parts)

        # Ã¢â€â‚¬Ã¢â€â‚¬ Startup health report (wait for workers to settle) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
        time.sleep(20)
        _push("PYXIS", _health_msg("PYXIS ONLINE"))

        last_cmems_ts  = 0.0
        last_report_ts = time.time()

        while True:
            time.sleep(60)   # poll every minute

            # Ã¢â€â‚¬Ã¢â€â‚¬ CMEMS change detection Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
            try:
                with open(CMEMS_PATH) as f:
                    cd = json.load(f)
                cmems_ts = float(cd.get("updated", 0))
                if cmems_ts > last_cmems_ts + 60:   # genuinely new fetch
                    wv  = cd.get("vessel_wave", {})
                    wh  = wv.get("wave_h",   "n/a")
                    sw  = wv.get("swell_h",  "n/a")
                    cv  = wv.get("curr_v",   "n/a")
                    sst = wv.get("sst_c",    "n/a")
                    ts_str = cd.get("updated_str", "?")
                    _push("CMEMS",
                          f"DATA LIVE @ {ts_str} | "
                          f"wave {wh}m swell {sw}m | "
                          f"curr {cv}kn SST {sst}C")
                    last_cmems_ts = cmems_ts
            except Exception:
                pass

            # Ã¢â€â‚¬Ã¢â€â‚¬ Periodic 30-min health summary Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
            if time.time() - last_report_ts > 1800:
                _push("PYXIS", _health_msg())
                last_report_ts = time.time()

    threading.Thread(target=nmea_listener_thread, daemon=True).start()
    threading.Thread(target=osm_worker, daemon=True).start()
    threading.Thread(target=msi_worker, daemon=True).start()
    threading.Thread(target=swpc_worker, daemon=True).start()
    threading.Thread(target=asam_worker, daemon=True).start()
    threading.Thread(target=seismic_worker, daemon=True).start()
    threading.Thread(target=meteo_worker, daemon=True).start()
    threading.Thread(target=bathymetry_worker, daemon=True).start()
    threading.Thread(target=marine_alerts_worker, daemon=True).start()
    threading.Thread(target=system_reporter_worker, daemon=True).start()
    threading.Thread(target=_tile_position_prewarm_worker, daemon=True).start()

    app.run(host='0.0.0.0', port=443, ssl_context=('/etc/letsencrypt/live/benfishmanta.duckdns.org/fullchain.pem', '/etc/letsencrypt/live/benfishmanta.duckdns.org/privkey.pem'))

