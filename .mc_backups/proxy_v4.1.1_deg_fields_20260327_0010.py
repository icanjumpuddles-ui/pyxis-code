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
from gtts import gTTS
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

app = Flask(__name__)
B = "/home/icanjumpuddles/manta-comms"
load_dotenv(os.path.join(B, ".env"), override=True)
DB, DT, SIM, AN = os.path.join(B, "pyxis_logs.db"), os.path.join(B, "latest_sector.json"), os.path.join(B, "sim_telemetry.json"), os.path.join(B, "anchor_state.json")
ROUTE_FILE = os.path.join(B, "active_route.json")
GMDSS_CACHE_FILE = os.path.join(B, "gmdss_cache.json")
SWPC_CACHE_FILE = os.path.join(B, "swpc_cache.json")
ASAM_CACHE_FILE = os.path.join(B, "asam_cache.json")
SEISMIC_CACHE_FILE = os.path.join(B, "seismic_cache.json")
METEO_CACHE_FILE = os.path.join(B, "meteo_cache.json")
task_queue = queue.Queue()
gen_lock = threading.Lock()
task_results = {}
inbox_messages = []
inbox_lock = threading.Lock()
force_kokoro = False
last_known_lat = 1.2504
last_known_lon = 103.8300
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
            
            # DECOUPLED TEXT INBOX - Transmit immediately before TTS blocking synthesis
            with inbox_lock:
                inbox_messages.append({"ts": new_id, "source": "PYXIS", "message": txt})
                if len(inbox_messages) > 50: inbox_messages = inbox_messages[-50:]

            # PHONETIC TRANSLATION FOR TTS ONLY
            # Preserves structural numbers for UI, but pronounces them distinctly over radio
            def phoneticize(text):
                phon = {'0':'Zero', '1':'Wun', '2':'Two', '3':'Tree', '4':'Fower', '5':'Fife', '6':'Six', '7':'Seven', '8':'Ait', '9':'Niner'}
                import re
                t = re.sub(r'\b(\d{6})(Z|A|B|C|D|E|F|G|H|I|K|L|M|N|O|P|Q|R|S|T|U|V|W|X|Y)\b', lambda m: " ".join([phon.get(c,c) for c in m.group(1)]) + " " + m.group(2) + "ulu", text)
                t = re.sub(r'\b(\d{1,3})\s*(degree|degrees|minute|minutes|second|seconds)\b', lambda m: " ".join([phon.get(c,c) for c in m.group(1)]) + " " + m.group(2), t, flags=re.IGNORECASE)
                t = re.sub(r'\b(DTG)\b', 'Date Time Group', t)
                return t
            
            synth_txt = phoneticize(txt)

            # TTS CHAIN 1: KOKORO
            if kokoro:
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
                        time.sleep(0.05) # Yield GIL so Flask can serve /poll_report and /gemini INBOX_REQ
                    sf.write(tmp, np.concatenate(audio_pieces), 24000, format='WAV')
                    os.rename(tmp, out_p)
                    log(f"Alice Audio successfully saved to {out_p}")
                    success = True
                except Exception as e:
                    log(f"Kokoro exception: {e}")

            # TTS CHAIN 2: gTTS (Google TTS Fallback)
            if not success:
                try:
                    log("Processing Audio Fallback via Fast gTTS...")
                    tts = gTTS(text=synth_txt, lang='en', tld='co.uk')
                    tts.save(tmp)
                    os.rename(tmp, out_p)
                    log(f"gTTS Audio successfully saved to {out_p}")
                    success = True
                except Exception as e:
                    log(f"gTTS exception: {e}")

            # TTS CHAIN 3: ElevenLabs Fallback
            global force_kokoro
            if not success and el_key and el_voice and not force_kokoro:
                try:
                    log("Attempting ElevenLabs synthesis (Pyxis Fallback)...")
                    res = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{el_voice}", 
                                        json={"text": synth_txt, "model_id": "eleven_multilingual_v2"}, 
                                        headers={"Accept": "audio/mpeg", "Content-Type": "application/json", "xi-api-key": el_key}, 
                                        timeout=20)
                    if res.status_code == 200:
                        with open(tmp, "wb") as f: f.write(res.content)
                        os.rename(tmp, out_p)
                        log(f"Ruby audio successfully saved to {out_p}")
                        success = True
                    else:
                        log(f"ElevenLabs failed ({res.status_code}).")
                except Exception as e:
                    log(f"ElevenLabs exception: {e}.")

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
                    news = []
                    for art in r.json().get("articles", [])[:3]:
                        news.append(art.get("title", "")[:100])
                    osint_data["news"] = news
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
            meteo_data = {"marine": {}, "depth": 0}
            if r.status_code == 200:
                meteo_data["marine"] = r.json().get('current', {})
            if r2.status_code == 200:
                res = r2.json().get('results', [])
                if res: meteo_data["depth"] = res[0].get("elevation", 0)
            with open(METEO_CACHE_FILE, "w") as f:
                json.dump(meteo_data, f)
            log("Meteo Worker: Cached current sea state and bathymetry.")
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
        data = request.json
        la = 25.1527
        lo = 55.3896
        
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
                with open(ASAM_CACHE_FILE, "r") as f: asam_str = str(json.load(f)[:5])
            if os.path.exists(SEISMIC_CACHE_FILE):
                with open(SEISMIC_CACHE_FILE, "r") as f: seismic_str = str(json.load(f)[:5])
            if os.path.exists(METEO_CACHE_FILE):
                with open(METEO_CACHE_FILE, "r") as f: meteo_str = str(json.load(f))
        except: pass
            
        p = f"""Current Location: {la}, {lo} (GMDSS NAVAREA {my_navarea}).
        
LOCAL NAVAREA WARNINGS (Kinetic, Navigational, SAR, Infrastructure):
{local_str}

GLOBAL WARNINGS:
{global_str}

NOTICE TO MARINERS (NTM):
{ntm_str}

SEVERE WEATHER ALERTS (GDACS):
{weather_str}

SPACE WEATHER (NOAA SWPC):
{swpc_str}

MARINE PIRACY (NGA ASAM):
{asam_str}

SEISMIC & TSUNAMI WATCH (USGS):
{seismic_str}

SEA STATE & BATHYMETRY (OPEN-METEO/ETOPO1):
{meteo_str}

Analyze these official GMDSS navigational warnings, Notice to Mariners, severe weather alerts, space weather, seismic activity, active piracy, and physical sea states. Evaluate threats that may present a danger to our mission capability or the vessel's safety.
CRITICAL: DO NOT INVENT FACTITIOUS THREATS OR HALLUCINATE ANY DATA. If there are no immediate threats or contacts in the provided data, explicitly state so instead of fabricating them.
CRITICAL TTS FORMATTING: You MUST format all dates as traditional Naval Date Time Groups (e.g., "DTG 241051Z MAR 26") and all lat/lon coordinates in strictly degrees, minutes, and seconds (e.g., "34 degrees 15 minutes 30 seconds North"). Do not use decimals for coordinates in your report. Keep the report highly professional, concise, and tactical.
Return ONLY valid JSON summarizing the top 5 critical threats across ALL categories, highly prioritizing the Local AOR if any threats exist there. Provide a detailed but structured breakdown. Give a report on anything the captain of the vessel should know about:
{{"alerts": [{{"title": "Short Threat Name", "desc": "Detailed paragraph summary"}}, ...]}}
If there are no threats globally or locally, return exactly {{"alerts": []}}."""

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
                                name = prev.get("name", "Unknown Vessel")
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
                                name = sd.get("Name", "").strip() or "Unknown Vessel"
                                destination = sd.get("Destination", "").strip()
                                callsign = sd.get("CallSign", "").strip()
                                imo = str(sd.get("ImoNumber", "") or "")
                                if mmsi in live_ais_cache:
                                    live_ais_cache[mmsi]["name"] = name
                                    live_ais_cache[mmsi]["mmsi"] = mmsi
                                    live_ais_cache[mmsi]["destination"] = destination
                                    live_ais_cache[mmsi]["callsign"] = callsign
                                    live_ais_cache[mmsi]["imo"] = imo
                                else:
                                    live_ais_cache[mmsi] = {"name": name, "mmsi": mmsi, "destination": destination, "callsign": callsign, "imo": imo, "ts": time.time()}
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
    contacts = [dict(v) for v in live_ais_cache.values() if "lat" in v]
    
    # Restoring OpenSeaMap Geo-Marks from volatile memory array
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

def weather_prewarm_worker():
    """Background thread: pre-generates maps at zoom levels 4, 6, 8 every 5 minutes."""
    import io
    PREWARM_ZOOMS = [4, 6, 8]
    while True:
        try:
            time.sleep(10)  # Wait for proxy to fully initialize before first run
            for z in PREWARM_ZOOMS:
                try:
                    lat = last_known_lat or -38.3
                    lon = last_known_lon or 144.7
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


@app.route('/weather_radar')
@app.route('/weather_radar/<path:dummy>')
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

@app.route('/telemetry', methods=['GET', 'POST'])
def handle_telem():
    """
    Raw fast-polling endpoint used by internal services to quickly dump
    the latest synchronized JSON telemetry variables without heavy logic.
    Also accepts POST from the Headless Simulator to inject vessel state.
    """
    if request.method == 'POST':
        try:
            payload = request.json
            payload["last_sim_update"] = time.time()
            existing = {}
            if os.path.exists(SIM):
                try:
                    with open(SIM, "r") as f: existing = json.load(f)
                except: pass
            existing.update(payload)
            with open(SIM, "w") as f: json.dump(existing, f)
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
    Renders the minimal, read-only Pyxis tracking map.
    All POST functionality was explicitly stripped to freeze inputs and guarantee 
    100% decoupling from bidirectional commands, preventing accidental physical watch overrides.
    Dynamically autofocuses on both Watch and Pyxis simultaneously.
    """
    return """
<!DOCTYPE html>
<html>
<head>
    <title>PYXIS LIVE TRACKER</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
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
        <span style="color:#0f0;">â–² PYXIS DRONE</span><br>
        <span style="color:#00f;">â—  CREW WATCH</span>
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
    Renders the 'Pyxis Tactical' advanced Web Dashboard.
    Features a full dynamic radar mimicking the Pygame display, 
    live AIS overlays, routing buttons, and voice input capability.
    Designed for iPads or bridge laptops alongside the Garmin watch.
    """
    return """
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
            <button id="micBtn" style="flex: 1;" onmousedown="startRecording()" onmouseup="stopRecording()">🎤 HOLD TO SPEAK</button>
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
                c.heading !== undefined ? `<div>Heading: <span style="color:#fff">${c.heading?.toFixed?.(0) ?? c.heading}°</span></div>` : '',
                c.range_nm  ? `<div>Range: <span style="color:#fff">${c.range_nm.toFixed(2)} nm</span></div>` : '',
                c.bearing   ? `<div>Bearing: <span style="color:#fff">${c.bearing.toFixed(0)}°</span></div>` : '',
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
            document.getElementById('micBtn').innerText = 'ÃƒÂ°Ã…Â¸Ã¢â‚¬ÂÃ‚Â´ RECORDING...';
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
                document.getElementById('micBtn').innerText = 'ÃƒÂ°Ã…Â¸Ã…Â½Ã‚Â¤ HOLD TO SPEAK (Error)';
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
                    d.innerText = `ÃƒÂ°Ã…Â¸Ã…Â½Ã‚Â¤ ${j.user_query}`;
                    clog.prepend(d);
                }
                if(j.response) {
                    const r = document.createElement('div');
                    r.style.color = '#0f0';
                    r.innerText = `< ${j.response}`;
                    clog.prepend(r);
                }
            } catch(e) { console.log("Voice err", e); }
            document.getElementById('micBtn').innerText = 'ÃƒÂ°Ã…Â¸Ã…Â½Ã‚Â¤ HOLD TO SPEAK';
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
                icon: L.divIcon({
                    className: 'vessel-icon',
                    html: '<div style="font-size: 24px; color: #0f0;">&#x25B2;</div>', // Upward triangle
                    iconSize: [24, 24],
                    iconAnchor: [12, 12]
                })
            }).addTo(map);

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

                contactsHtml += `<div class="contact-item ${className}"><span>${c.name || c.id}</span><span>${c.bearing.toFixed(0)}Ãƒâ€šÃ‚Â° / ${c.range_nm.toFixed(1)}nm</span></div>`;
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
    Garmin Watch route. Packs the last 6 audio/text reports into an Array
    pre-wrapped at 22 characters wide to fit on the physical watch face
    Inbox menu seamlessly.
    """
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

@app.route('/telemetry', methods=['POST'])
def telemetry_post():
    """
    Receives simulator telemetry payloads, standardizes coordinates for Pyxis Lite,
    and writes them to sim_telemetry.json.
    """
    try:
        td = request.get_json(silent=True) or {}
        if "BOAT_LAT" in td: td["lat"] = td.pop("BOAT_LAT")
        if "BOAT_LON" in td: td["lon"] = td.pop("BOAT_LON")
        td.pop("CREW_LAT", None)
        td.pop("CREW_LON", None)
        with open(SIM, "w") as f:
            json.dump(td, f)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        log(f"Telemetry write error: {e}")
        return jsonify({"error": str(e)}), 500

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
            last_known_lat = float(c_lat)
            last_known_lon = float(c_lon)
            log("GPS Override: Watch coordinates forced as Host coordinates.")
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
            contacts = get_active_ais_list(la, lo)
            
            global osm_cache
            osm_injection = []
            with osm_cache_lock:
                for item in osm_cache:
                    dLat = (item['lat'] - la) * 60.0
                    dLon = (item['lon'] - lo) * 60.0 * math.cos(math.radians(la))
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
                    "lat": la - 0.75, # Roughly 45nm South
                    "lon": lo,
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
                "lat": la,
                "lon": lo
            }), 200

        if msg.startswith("AIS_LIST_REQ"):
            contacts = get_active_ais_list(la, lo)
            # Sort contacts by distance, assuming those without range_nm are far away
            contacts.sort(key=lambda x: x.get("range_nm", 9999))
            
            return jsonify({
                "ais_list_ready": True,
                "radar_contacts": contacts[:40]
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

        # DEFINE ASYNC FUNC EARLY SO IT DOESN'T THROW 500 UnboundLocalError
        task_id = str(uuid.uuid4())
        rtype = None
        if msg.startswith("TELEMETRY_REQ") or msg.startswith("DAY_BRIEF") or msg.startswith("NIGHT_BRIEF"):
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
            osint_regional = "Regional Vector: Clear (0 localized threats <500nm)."
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
                        reg_threats.append(f"{item['type']} '{item['name']}' at {round(dist)}nm bearing {bear}°")
                if reg_threats:
                    osint_regional = f"REGIONAL THREATS (<500nm): {len(reg_threats)} detected. Details: " + " | ".join(reg_threats)
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
            ais_summary = ", ".join([f"{v.get('name', 'Unknown')} ({v.get('distance', 0)}m)" for v in get_active_ais_list()[:3]])
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
                    "**Response Formatting:**\n"
                    "Structure your response as follows:\n"
                    "* **Status Alert:** (Clear/Nominal, Warning, or Critical)\n"
                    "* **Vessel Telemetry:** (Brief summary of engine, power, and structural health)\n"
                    "* **Meteorological/Oceanographic:** (Current weather, sea state, and upcoming forecasts)\n"
                    "* **Passage Progress:** (Heading, speed, and waypoint ETAs)\n"
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
        <button onclick="window.location.href='/lite'">ÃƒÂ¢Ã¢â‚¬â€œÃ¢â‚¬Å“ BACK</button>
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
        last_known_lat = float(current_scenario["lat"])
        last_known_lon = float(current_scenario["lon"])
        
    log(f"SCENARIO INJECTED: {current_scenario}")
    return jsonify({"status": "ok", "id": current_scenario["id"]})

@app.route('/kill_sim', methods=['POST'])
def kill_sim():
    """Web UI Hook to securely terminate the headless simulator and restore the physical watch."""
    import os
    os.system("pkill -f hs.py")
    os.system("pkill -f headless_sim.py")
    if os.path.exists(SIM): os.remove(SIM)
    return jsonify({"status": "terminated"})

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

    # --- 2. Query Open-Meteo Marine for a 3×3 grid of wave height points ---
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
    if spd_kn < 17:    return (0,   210,  80, 170)   # Gentle→Moderate
    if spd_kn < 27:    return (200, 210,   0, 180)   # Fresh→Strong
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
    GRID = 7  # 7×7 sampling = 49 pts for smooth interpolation
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
            # Simple mapping: pixel fraction → grid index
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
                label_lines.append(f"→{dest[:8]}")
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
    z = max(2, min(z + 2, 7))   # incoming z 1-5 → uses CartoDB+RainViewer z 3-7

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
                    pass  # No radar for this tile — map-only is fine

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
                    lat = last_known_lat or -38.3
                    lon = last_known_lon or 144.7
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
        # Cache miss — generate on-demand
        lat = last_known_lat or -38.3
        lon = last_known_lon or 144.7
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
            log('ais_basemap: no valid vessel position — returning 503')
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

        # Build 3×3 tile canvas
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
    """Server-side composited AIS radar map — tiles + contacts in one JPEG.
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

        # Build 3×3 tile canvas
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
            if c_sog > 15:   col = (255, 80, 80)   # fast — red
            elif c_sog > 5:  col = (80, 220, 80)   # moving — green
            else:            col = (80, 150, 255)   # slow/stationary — blue

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
    """Dumps raw meteo_cache.json for diagnosis — remove after testing."""
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
    """Kill and restart marine_map_gen immediately — forces map regen at current vessel position."""
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

        # --- Sea state data from meteo cache ---
        meteo = {}
        meteo_path = os.path.join(B, "meteo_cache.json")
        if os.path.exists(meteo_path):
            try:
                with open(meteo_path) as f:
                    meteo = _json.load(f)
            except Exception:
                pass

        def _fmt_dir(deg):
            """Convert degrees to cardinal + numeric string."""
            if deg is None: return "n/a"
            cardinals = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                         "S","SSW","SW","WSW","W","WNW","NW","NNW"]
            idx = int((float(deg) + 11.25) / 22.5) % 16
            return f"{cardinals[idx]} ({int(deg)}\u00b0)"

        def _fv(v, unit="", dec=1):
            if v is None or v == "": return "n/a"
            try: return f"{float(v):.{dec}f}{unit}"
            except: return str(v)

        # Data is nested: meteo_cache["marine"] contains wave fields
        marine = meteo.get("marine", {})
        depth  = meteo.get("depth")

        # Pull extra fields from telemetry (wind, heading, sea_state score)
        td = {}
        if os.path.exists(telem_path):
            try:
                with open(telem_path) as f:
                    td = _json.load(f)
            except Exception:
                pass

        # Crew (watch) position — separate from vessel position
        crew_lat = td.get("CREW_LAT") or td.get("lat")
        crew_lon = td.get("CREW_LON") or td.get("lon")

        # Vessel–crew separation in nautical miles
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
        except Exception:
            pass

        result = {
            "pyxis_lat":  _fv(pos_lat,   "\u00b0", 4),
            "pyxis_lon":  _fv(pos_lon,   "\u00b0", 4),
            "crew_lat":   _fv(crew_lat,  "\u00b0", 4),
            "crew_lon":   _fv(crew_lon,  "\u00b0", 4),
            "separation": sep_nm,
            "wave_h":     _fv(marine.get("wave_height"),            "m"),
            "wave_dir":   _fmt_dir(marine.get("wave_direction")),
            "wave_dir_deg": marine.get("wave_direction"),
            "wave_period":_fv(marine.get("wave_period"),            "s"),
            "swell_h":    _fv(marine.get("swell_wave_height"),      "m"),
            "swell_dir":  _fmt_dir(marine.get("swell_wave_direction")),
            "swell_dir_deg": marine.get("swell_wave_direction"),
            "curr_v":     _fv(marine.get("ocean_current_velocity"), "kn"),
            "curr_dir":   _fmt_dir(marine.get("ocean_current_direction")),
            "curr_dir_deg": marine.get("ocean_current_direction"),
            "wind_sp":    _fv(td.get("wind_speed"),                 "kn"),
            "wind_dir":   _fmt_dir(td.get("wind_dir_deg") or td.get("wind_dir") or td.get("wind_direction")),
            "heading":    _fv(td.get("heading"),                    "\u00b0", 0),
            "sog":        _fv(td.get("SOG") or td.get("speed_kn"), "kn"),
            "depth":      _fv(depth,                                "m"),
            "sea_state":  _fv(td.get("sea_state"),                  "m"),
            "updated":    marine.get("time", "unknown"),
        }
        resp = make_response(_json.dumps(result))
        resp.headers.set('Content-Type', 'application/json')
        return resp
    except Exception as e:
        log(f"sea_state_json error: {e}")
        return _json.dumps({"error": str(e)}), 500

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
            # Generator hasn't run yet — synthetic placeholder
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
                    <option value="-38.277,144.731">Popes Eye (Port Phillip)</option>
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
        
        <button style="width:100%; padding:20px; font-size:18px; margin-top:10px;" onclick="deploy()">ÃƒÂ°Ã…Â¸Ã…Â¡Ã¢â€šÂ¬ EXECUTE WARP & INJECT ÃƒÂ°Ã…Â¸Ã…Â¡Ã¢â€šÂ¬</button>
        <button style="width:100%; padding:15px; font-size:16px; margin-top:10px; background:#005;" onclick="locateWithPyxis()">ÃƒÂ°Ã…Â¸Ã¢â‚¬ÂºÃ‚Â  LOCATE WITH PYXIS (SYNC WATCH)</button>
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
                lat = last_known_lat or -38.3
                lon = last_known_lon or 144.7
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
                lat = last_known_lat or -38.3
                lon = last_known_lon or 144.7
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

    threading.Thread(target=nmea_listener_thread, daemon=True).start()
    threading.Thread(target=osm_worker, daemon=True).start()
    threading.Thread(target=msi_worker, daemon=True).start()
    threading.Thread(target=swpc_worker, daemon=True).start()
    threading.Thread(target=asam_worker, daemon=True).start()
    threading.Thread(target=seismic_worker, daemon=True).start()
    threading.Thread(target=meteo_worker, daemon=True).start()
    threading.Thread(target=bathymetry_worker, daemon=True).start()

    app.run(host='0.0.0.0', port=443, ssl_context=('/etc/letsencrypt/live/benfishmanta.duckdns.org/fullchain.pem', '/etc/letsencrypt/live/benfishmanta.duckdns.org/privkey.pem'))

