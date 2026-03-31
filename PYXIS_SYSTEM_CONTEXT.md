# PYXIS TACTICAL SYSTEM — AI SESSION CONTEXT DOCUMENT
*Last updated: 2026-03-29. Read this FULLY before touching any code.*

---

## 1. WHAT PYXIS IS

Pyxis is a **live, moving vessel** (sailing yacht) intelligence system. Ben is the skipper.
- A **Garmin watch** (the primary display) talks to a **cloud proxy** on GCP via HTTPS
- The proxy aggregates real-time AIS, ADSB, ocean, weather, seismic, and chart data
- The watch renders all of this on a 454x454px round display
- **There are NO hardcoded vessel positions.** Position comes from live telemetry.
- Position source of truth: `last_known_lat` / `last_known_lon` updated via `/telemetry` POST and `/scenario` GET

---

## 2. CRITICAL RULES

1. **Never hardcode lat/lon** — always use `last_known_lat` / `last_known_lon`
2. **Never break the ADSB/AIS contact pipeline** — core tactical display
3. **Never modify watch-side .java/.mc code** unless explicitly asked — watch code is correct
4. **Map images are 320x320px** — don't add text at very bottom, round watch bezel clips it. Use TOP of image
5. **The proxy file on VM is `proxy.py`** — local file is `proxy_v4.1.0_RADAR.py`
6. **`proxy - Copy.py`** = Ben's known-good backup. Check it before large changes
7. **Multiple users connect simultaneously** — don't assume single-client

---

## 3. FILE MAP

```
E:\Garmin Dev\GarminBenfish\source\
  proxy_v4.1.0_RADAR.py   PRIMARY server file (deploy to VM as proxy.py)
  proxy - Copy.py          Ben's known-good backup (reference before big changes)
  proxybak.py              Older backup
  headless_sim.py          Simulates vessel sensors 1Hz, POSTs to /telemetry
  watch_simulator.py       Local Python GUI simulating Garmin watch
  pysxiswatchcode.java     Garmin Connect IQ watch source (DO NOT MODIFY)
  pyxis_diag.py            VM diagnostic script
```

---

## 4. VM AND DEPLOYMENT

- **Host**: benfishmanta.duckdns.org
- **SSH user**: icanjumpuddles
- **SSH key**: C:\Users\Ben\.ssh\google_compute_engine
- **Proxy path**: /home/icanjumpuddles/manta-comms/proxy.py
- **Service**: manta-proxy.service
- **Restart alias**: `pr` (run in SSH terminal, NOT systemctl)
- **Proxy port**: HTTPS **443** — NOT 8080, NOT 5000
- **SSL**: Let's Encrypt at /etc/letsencrypt/live/benfishmanta.duckdns.org/

### Deploy (PowerShell):
```powershell
$key = "C:\Users\Ben\.ssh\google_compute_engine"
scp -i $key "E:\Garmin Dev\GarminBenfish\source\proxy_v4.1.0_RADAR.py" "icanjumpuddles@benfishmanta.duckdns.org:/home/icanjumpuddles/manta-comms/proxy.py"
```
Then `pr` in SSH. Do NOT use sudo systemctl restart.

---

## 5. ARCHITECTURE

```
[Garmin Watch] -HTTPS-> [benfishmanta.duckdns.org:443]
                              |
                         [proxy.py / Flask]
                              |
         +--------------------+--------------------+
         |                    |                    |
   [ADSB Worker]        [AIS Worker]         [CMEMS Worker]
   OpenSky->adsb.lol    AISStream WS         Copernicus Marine
   5min poll            live stream          wave/current/SST
         |                    |
   [adsb_cache.json]    [live_ais_cache]
   persisted to disk    in-memory dict
```

### Background workers (all daemon threads):
- `adsb_worker` - polls OpenSky 5min, fallback: adsb.lol
- `aisstream_worker` - WebSocket stream.aisstream.io, 5x5 deg bbox
- `cmems_worker` - Copernicus Marine wave/current/SST
- `weather_prewarm_worker` - pre-caches weather maps z=1-10
- `adsb_radar_prewarm_worker` - pre-caches ADSB radar z=6-11
- `headless_sim.py` - external process, 1Hz POST /telemetry

---

## 6. KEY ENDPOINTS

| Endpoint | Purpose |
|---|---|
| POST /telemetry | Receive sensor data, updates last_known_lat/lon |
| GET /scenario | Get/set vessel position (source of truth) |
| GET /adsb_contacts | JSON ADSB aircraft list |
| GET /adsb_radar_map?z=N | ADSB aircraft map JPEG (pre-warmed z=6-11) |
| GET /ais_radar_map/NM | AIS vessel radar JPEG |
| GET /ais_map?w=&h=&z=&lat=&lon= | AIS background map tiles |
| GET /sea_state_json | Wave/current/SST JSON |
| GET /topo_map | Topographic chart JPEG |
| GET /nautical_map | Nautical chart JPEG (Esri+OpenSeaMap) |
| GET /status_api | Complete vessel status JSON |
| GET /verbose | Human diagnostic page |

---

## 7. TILE CACHE (3-tier)

Used by topo_map and nautical_map:
1. SSD: /home/icanjumpuddles/manta-comms/tile_cache/ (180-day TTL)
2. GDrive: /mnt/gdrive/tile_cache/ — rclone mount of gdrive:Pyxis/backup
   Mount: `gdrive:Pyxis/backup on /mnt/gdrive type fuse.rclone`
   Ben pre-populated with tiles for expected sailing areas
3. Internet: fetched live, saved to Tier 1 + 2

### Tile sources:
| Map | URL | Notes |
|---|---|---|
| ADSB/AIS bg | basemaps.cartocdn.com/dark_all/ | dark_matter_all is RETIRED - 404 |
| Topo | tile.opentopomap.org/ | May be rate-limited from GCP |
| Nautical base | services.arcgisonline.com/Ocean/World_Ocean_Base/ | |
| Seamark overlay | tiles.openseamap.org/seamark/ | Transparent PNG |

---

## 8. BUGS FIXED THIS SESSION (2026-03-29)

| Bug | Fix | Status |
|---|---|---|
| adsb_cache=[] on startup, no contacts for 5min | Pre-load from adsb_cache.json | DONE |
| adsb.fi API 404 (blocked from GCP) | Replace with adsb.lol as fallback | DONE |
| Pre-warm calling http://127.0.0.1:8080 | Changed to https://127.0.0.1:443 + verify=False | DONE |
| CartoDB dark_matter_all 404 (retired URL) | Changed to dark_all throughout | DONE |
| Topo/naut 6h cache stale after position change | Reduced to 30min TTL | DONE |
| Position label at image bottom (bezel clip) | Moved to image top | DONE |
| headless_sim targeting http://localhost:5000 | Fixed to https://localhost:443 | DONE |
| headless_sim.py not found by pr script | Symlink ~/headless_sim.py -> ~/manta-comms/headless_sim.py | DONE |
| socket.io 404 errors | Expected/harmless - socket.io not implemented | Ignore |

---

## 9. API KEYS (check if missing on VM)

| Source | Env Var | Notes |
|---|---|---|
| AISStream.io | AISSTREAM_API_KEY | Needed for AIS WebSocket |
| Copernicus CMEMS | CMEMS_USER / CMEMS_PASS | Wave/current/SST |
| Google Gemini | GEMINI_API_KEY | AI briefings, TTS |
| OpenSky (auth) | OPENSKY (user:pass) | Optional, increases rate limit |

Set in /home/icanjumpuddles/manta-comms/.env

---

## 10. AIS NOTES

Bass Strait / Victoria = LIGHT ship traffic. Sparse AIS contacts are EXPECTED, not a bug.
Worker shows: `AisStream Connected for BBox: [[-40.987, 143.12], [-35.987, 148.12]]`

---

## 11. DO NOT DO

- Do NOT change CartoDB URL back to dark_matter_all (404)
- Do NOT reset adsb_cache to empty list on startup
- Do NOT change pre-warm URL back to port 8080
- Do NOT put text labels at very bottom of watch images
- Do NOT test APIs from local machine - test from GCP (many block GCP IPs)
- Do NOT modify watch .java/.mc code without explicit instruction
- Do NOT use sudo systemctl restart - use `pr`
- Do NOT hardcode vessel coordinates
- Do NOT add socket.io

---

## 12. CURRENT STATE (2026-03-29 ~15:00 AEDT)

- WORKING: 47-86 ADSB contacts load immediately on restart
- WORKING: ADSB pre-warm z=6-11 (25-27KB images confirm contacts present)
- WORKING: AIS stream connected (low traffic area)
- WORKING: Nautical chart at correct vessel location
- WORKING: Topo map at correct vessel location
- WORKING: headless_sim posting telemetry 1Hz to port 443
- SLOW: Topo tiles (OpenTopoMap rate-limits GCP) - rclone cache is the mitigation
- PENDING: Symbology review on ADSB/AIS maps (possible artifacts from prior sessions)
