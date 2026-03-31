# Pyxis Tactical Intel: Multi-Tier Cache & Web Tile Manager

This document outlines the architecture, deployment, and operational parameters of the **Pyxis Multi-Tier Map Caching Engine** and its integrated **React Web Tile Manager**. This custom offline-first infrastructure ensures high-performance tactical map rendering on the Garmin watch while maintaining long-term offline vault storage in Google Drive. 

## 1. Multi-Tier Map Caching Architecture

The mapping engine built into `proxy_v4.1.0_RADAR.py` serves CartoDB Dark Matter, Topographic Base Maps, Nautical Esri Charts, and OpenSeaMap overlays to the Garmin watch. It relies on a three-tier cascading cache to deliver these maps without exhausting the compute node's disk overhead or incurring excessive API quotas.

### Tier 1: Local SSD Hot Cache
- **Path:** `/home/icanjumpuddles/manta-comms/tile_cache`
- **Mechanism:** Ultra-fast, NVMe-backed local file system read/writes.
- **Latency:** ~<5ms per tile retrieval.
- **Role:** Primary serving layer. All requests from the Garmin watch check this layer first.

### Tier 2: The Google Drive Vault (Cold Cache)
- **Mount Point:** `/mnt/gdrive/`
- **Underlying Path:** `gdrive:Pyxis/backup/tile_cache`
- **Daemon:** Custom `rclone-gdrive.service` systemd daemon running FUSE `rclone mount`.
- **Role:** Infinite offline persistent storage. If a tile expires or is cleaned out of Tier 1, the Python proxy dynamically checks Tier 2. If it exists, the daemon silently pulls it down, clones it back to Tier 1, and serves it—preventing repetitive remote internet API calls. 

### Tier 3: Remote Map APIs (Source)
- **Role:** If neither cache layer contains a tile, the proxy downloads it from Esri, CartoDB, or OpenSeaMap natively.
- **Write-back:** Once downloaded from the internet, the tile is written to **Tier 1 (Hot)** for immediate availability, and synchronously mirrored across the FUSE mount to **Tier 2 (Cold)** for permanent storage.

---

> [!TIP]
> **Data Longevity**
> The `os.getmtime` threshold in the Python proxy extends the Tile Expiry horizon from the standard 1 hour out to **180 days**. Navigational tiles essentially become semi-permanent fixtures in your database.

---

## 2. Resource Management: The Tile Janitor Worker

To prevent the local VM's SSD from overflowing over time, a protective background thread continuously monitors the Tier 1 cache. 

**`tile_janitor_worker` Parameters:**
- **Trigger Cap:** 2.0 Gigabytes (GB)
- **Reduction Target:** 1.6 Gigabytes (GB)
- **Algorithm:** **LRU** (Least Recently Used). The proxy touches `os.utime()` whenever a tile is requested by the watcher. The janitor actively sorts the cache folder chronologically and deletes the oldest unused regions until disk quota aligns.
- **Google Drive Protection:** The janitor explicitly **does not cross the FUSE boundary** into `/mnt/gdrive`. Therefore, the 2.0GB cap is strictly a "fast cache" restriction. The full map library lives forever in the Cloud Vault.

## 3. The React Web Tile Manager

To pre-emptively cache zones for offline expeditions, Pyxis utilizes a custom Web Interface seamlessly attached to the primary Vue/React dashboard. 

### Web Components
- **Dashboard Component:** Embedded safely inside the Pyxis React Dashboard (`ControlPanel.tsx`) under the **SCENARIO** sub-menu.
- **Dynamic Auto-Centring:** Directly tracks the current injected live telemetry (`last_known_lat` / `last_known_lon`) and forces the Leaflet HTML container to centre precisely around the vessel.
- **Bounding Box Geometry Calculator:** Uses `Leaflet.Draw` bounding boxes along with `vLat / vLon` + radius macros (e.g. `VESSEL +50nm`) to generate an exact rectangle matrix. 
- **Tile Count Algorithms:** Instantly calculates the mathematical permutations of Matrix `tx / ty` coverage against zoom dimensions, estimating total file count and download time in Javascript *before* initializing the job. 

### The ThreadPool Fetcher (`/fetch_tile_region`)
1. The Tile Manager sends the requested Bounding Box and active Layers via `POST`.
2. A fast algorithm maps Lat/Lon bounds to Web-Mercator `tx -> ty` XYZ bounds for each active zoom level. 
3. Python spins up a `ThreadPoolExecutor(max_workers=5)` for asynchronous concurrent fetching.
4. Jobs are tracked in memory (`_tile_jobs`), allowing the frontend UI to poll `/tile_job_status/:id` every 1200ms and render accurate progress bars.

> [!WARNING]
> Do NOT set `max_workers` significantly higher than 5 without risking severe HTTP 429 Rate Limiting from Esri and OpenSeaMap public-facing APIs. The `concurrent.futures` queue is tuned precisely for optimal network throughput without risking a blacklisted IP address. 

## 4. Boot Sequencing and Server Operation

When the `benfishmanta` Linux generic cloud runner restarts:
1. `systemd` spins up `rclone-gdrive.service`, restoring the infinite file system under the `icanjumpuddles` user context seamlessly due to FUSE `user_allow_other` overrides. 
2. `systemd` then triggers `manta-proxy.service`.
3. The Proxy initializes its 15+ sub-workers (CMEMS, GMDSS, Janitors... etc). 
4. The React fast-front-end mounts to NGINX natively at `/dashboard`, securely locking routing via HTTP basic auth and rendering the Command User Interface.
