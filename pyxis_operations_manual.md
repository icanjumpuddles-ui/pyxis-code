# ⚓ PYXIS TACTICAL INTELLIGENCE
## Operations & Engineering Handbook

*A comprehensive technical manual detailing the Pyxis Multi-Domain Architecture: Python Edge Computing, ConnectIQ Wearable UI, and React Command Dashboards.*

---

### **Table of Contents**
1. System Architecture Overview
2. Chapter 1: The Manta Proxy (Backend Server Core)
3. Chapter 2: External Data Integration (OSINT & Environment)
4. Chapter 3: The Threat Intel & AI Assessment Pipeline
5. Chapter 4: The React Web Dashboard
6. Chapter 5: The Garmin Tactical Wearable
7. Chapter 6: The Two-Tier Map Caching System
8. Chapter 7: Deployment & Maintenance Procedures

---

## System Architecture Overview

Pyxis acts as an intermediary intelligence pipeline for maritime operations. At its core, it translates enormous, heavy data feeds (global shipping vectors, satellite sea-state grids, dynamic map tiles, and live OSINT threat feeds) into micro-optimized JSON packets and hyper-compressed Map Tiles specifically designed for the absolute lowest-bandwidth transmission to a Garmin watch.

```mermaid
graph TD
    subgraph Data Sources
        API1[CartoDB & OpenSeaMap]
        API2[Copernicus Marine (CMEMS)]
        API3[AISStream.io]
        API4[Google Gemini AI]
    end

    subgraph Backend: Manta Proxy (GCP VM)
        PW[OSINT / Weather Workers]
        CM[Two-Tier Map Cache]
        SQL[(SQLite Radar Cache)]
        TTS[Google TTS Service]
        PW --> SQL
        CM --> APIs
    end

    subgraph Frontends
        R[React Command Dashboard]
        G[Garmin Enduro Watch App]
    end

    API1 --> CM
    API2 --> PW
    API3 -.-> SQL
    API4 --> PW
    
    Backend <-->|WebSockets & HTTPS| R
    Backend <-->|MakeWebRequest (JSON/JPEG)| G
```

---

## Chapter 1: The Manta Proxy (Backend Server Core)

The central server component is `proxy_v4.1.0_RADAR.py`. Running permanently on a Google Compute Engine Node, this Python Flask application manages the entire heavy-lifting pipeline. 

### Core Responsibilities:
1. **Background Polling:** Utilizing Python's `threading` and global dictionaries, the proxy spins up dedicated workers to pull from APIs on timed delays (every 5 to 60 minutes) to prevent rate-limits and keep data *instantly* ready in RAM for the watch's request.
2. **Endpoint Exposure:** Exposing simple endpoints like `/status_api`, `/sea_state_json`, and `/weather_radar` that respond in milliseconds.
3. **Image Processing:** Using `Pillow` (PIL) to natively downsample, crop, stitch, layer, and palette-reduce massive Web-Mercator map tiles into 320x320 16-bit physical Garmin-compatible matrix screens. 

---

## Chapter 2: External Data Integration

The true power of Pyxis lies in merging separate commercial and intelligence feeds into a singular tactical picture.

*   **Copernicus Marine Service (CMEMS):** The `cmems_worker` dynamically downloads live 0.083-degree global NetCDF oceanic grids, interpreting Ocean Currents (u/v velocities), Sea Surface Temperatures (SST), and Wave Heights. It slices this global matrix into coordinates relative to the vessel.
*   **AISStream.io:** A live WebSocket listener opens a permanent pipe to global AIS traffic (`aisstream_worker`). It parses raw NMEA sentences and caches nearby structural vessel data (MMSI, Class, Size, Call Sign, Speed/Heading) into an SQLite Database (`ais_cache.db`).
*   **OpenSeaMap / Esri:** Fetches bathymetric topological contours and physical channel markers, stitching them natively via Python logic.

---

## Chapter 3: The Threat Intel & AI Assessment Pipeline

Pyxis integrates **Google Gemini 2.0 Flash** directly into the vessel's daily processing loop. The `brain_worker.py` system dynamically compiles the last 3 hours of local weather, engine telemetry, and physical surroundings.

### Google Search Grounding & OSINT

> [!IMPORTANT]
> The AI is configured with **Google Search Grounding**, enabling it to break out of its knowledge cutoff. When Pyxis enters a conflict zone (e.g., the Red Sea / Bab el-Mandeb), the Gemini Prompt enforces live OSINT web searches, instructing the AI to hunt for active piracy alerts or maritime missile intercepts matching the exact GPS coordinates, issuing critical audio alarms globally.

---

## Chapter 4: The React Web Dashboard

To control the headless architecture (since the proxy has no inherently visible GUI on the server), users log into the `pyxis-dashboard` React application. 

- **Vite & React Flow:** An ultra-fast single page application that visualizes the AI decision trees and engine telemetry.
- **Glassmorphism Design:** A deep space, dark tactical aesthetic utilizing TailwindCSS.
- **Headless Simulator Overrides:** Direct `/api/sim_override` REST controls that allow operators to instantly warp the environment variable arrays (forcing engine fires, pirate encounters, or severe weather conditions) for hardware testing.

---

## Chapter 5: The Garmin Tactical Wearable

The terminal endpoint of Pyxis is written in **Monkey C** for Garmin watches.

### Critical Constraints
1. Memory footprint is strictly capped (~124kb dynamically allocated).
2. Processing power must remain minimal to protect 45-day battery reserves.
3. Bluetooth communication (`MakeWebRequest`) to a phone must timeout cleanly.

The architecture completely bypasses Garmin's heavy rendering logic. The server `proxy.py` performs 100% of the rotational math, mapping vector overlays, and sea-state grids into a single `320x320 JPG`. The watch merely paints a static picture onto the screen and draws a static reticle over the top.

---

## Chapter 6: The Two-Tier Map Caching System

To resolve API starvation and prevent infinite server disk exhaustion when sailing across massive maritime domains, Pyxis uses a Google Drive integrated architecture. 

### Mechanism of Action
1. **Tier 1 (Hot SSD):** The server retains the 2.0 Gigabytes of most recently viewed map tiles locally on high-speed compute NVMe storage. A `tile_janitor_worker` strictly enforces this limit, deleting stale caches sequentially. 
2. **Tier 2 (Cold Vault):** A background systemic FUSE driver (`rclone-gdrive.service`) hard-mounts a Google Drive directory natively to the Linux path. If Tier 1 experiences a cache-miss, the script checks `/mnt/gdrive`. **This preserves maps infinitely.**

> [!TIP]
> Navigational expeditions can be explicitly "Pre-Warmed" using the Web Tile Manager via the React Dashboard. Select a 100 nautical-mile rectangle, and the server parallelizes 20 threads to build out the tactical grids into both local and cloud storage simultaneously before the vessel departs Wi-Fi connectivity.

---

## Chapter 7: Deployment & Maintenance Procedures

### Restarting the Primary Pipeline
If the `manta-proxy.service` hangs or dependencies fail, deployment is tracked via `systemctl`.
```bash
sudo systemctl daemon-reload
sudo systemctl restart manta-proxy
sudo systemctl status manta-proxy
```

### Restarting the FUSE Vault 
If Google Drive caching becomes unstable due to token expiration:
1. Re-authorize on a local Windows machine: `rclone authorize "drive"`.
2. Push the new encrypted JSON token to `/home/icanjumpuddles/.config/rclone/rclone.conf`.
3. Bounce the systemd daemon: `sudo systemctl restart rclone-gdrive`.

### React Dashboard Deployment
Any modifications to the `pyxis-dashboard` require a TypeScript validation layer and Vite roll-up:
```bash
cd pyxis-dashboard
npm run build
scp -r dist/* [SERVER]:/home/icanjumpuddles/manta-comms/dashboard/
```
