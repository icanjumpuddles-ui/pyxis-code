# Multi-Crew Architecture & Fleet Tracking Plan

## 1. Safety Verification: HTML Conflict Audit
I have reviewed all the static HTML files (`mantacomms.html`, `project2.html`, `diagram.html`) and the `pyxis-dashboard` React application. 
**Verification Confirmed:** None of these user interfaces request your web browser's physical GPS coordinate or automatically push it to the server. The only way the dashboard overrides the vessel's location is if a human manually types coordinates into the Sim Controller and clicks "Update". The HTML pages are definitively **not** fighting your Garmin Watch or the Pyxis hardware for GPS supremacy. 

## 2. Multi-Crew Segregation Plan (Monkey C + Python Proxy)

To expand Pyxis from a single-user C2 watch into a multi-tier fleet intelligence system, we need to restructure both the Garmin edge software and the cloud data structures.

### Step 2a: Monkey C App Refactoring (The Edge)
We will fork or configure the Pyxis Garmin application to embed identity parameters into every network sweep.
- **Unique App Settings:** Every crew member will configure their watch via the Garmin connect app to store a `crew_uid` (e.g., "Capt-Axiom", "Deck-01") and a `clearance_level` (e.g., "ALPHA", "BRAVO").
- **Network Footprint:** Whenever their watch hits the Manta Proxy, it will append `&uid=Deck-01&rank=bravo` to the URL.

### Step 2b: Proxy Fleet State Dictionary (The Cloud)
Currently, `sim_telemetry.json` stores a single `CREW_LAT` / `CREW_LON` pair. I will refactor the Python Proxy so that instead of a single captain, it maintains a dynamic `swarm_crew` dictionary.

```json
"active_crew": {
  "Capt-Axiom": { "lat": -38.4, "lon": 145.2, "rank": "alpha", "last_seen": 170094002 },
  "Deck-01":    { "lat": -38.4, "lon": 145.1, "rank": "bravo", "last_seen": 170093881 }
}
```
This allows the Pyxis dashboard to draw independent blue dots for every single crew member scattered across the vessel or ashore, allowing instant fleet-wide Man Overboard detection. 

### Step 2c: Tiered Intelligence Access (API Hardening)
We will modify the core `/gemini`, `/radar_map`, and `/status_api` routes on the proxy:
- **ALPHA (Captain):** Receives full BVR radar, OSINT alerts, Gemini systems analysis, and engine vitals.
- **BRAVO (Crew):** If the proxy detects `rank=bravo`, it will instantly strip out critical systems (like Engine Fuel, OSINT military targets, and Anchor bounds) and only return a lightweight JSON payload focusing on immediate maritime weather and basic wind roses.

---

## User Review Required

> [!WARNING]  
> Upgrading the central proxy to require a `crew_uid` parameter will break older versions of the Pyxis Watch code until we push the update to the watches. 

1. **Hierarchy:** Are you happy with a simple two-tier system (e.g., Captain vs. Deckhand), or do we need complex role-based clearance (Engineer, Navigator, Deckhand)?
2. **Backward Compatibility:** Shall I program the proxy to assume any watch *without* a unique ID is automatically the "Captain", so your current watch continues to function perfectly while we build the new crew apps? 

If you approve this structure, I will execute the changes to the central proxy's telemetry parser now, ensuring the cloud is ready to accept an unlimited number of crew members before we touch the Monkey C side.
