# PYXIS: Autonomous Tactical Intelligence & C3 System

This document outlines the architecture for Pyxis, a highly advanced, full-stack Command, Control, and Communications (C3) system designed for high-stakes maritime environments. It bridges the gap between raw global telemetry, multi-threaded intelligence data-fusion, and AI, delivering intelligence to a Garmin smartwatch or Web UI.

**Note to Network & AI Engineers:** This architecture prioritizes non-blocking async and threaded performance. All API queries to third parties (CMEMS, OpenSky, AIS) run in daemons and cache states globally, ensuring the primary web proxy never hangs waiting for external responses.

The system is broken down into four major pillars:

---

## 1. The Intelligence Fusion Hub (The VM Proxy)
At the heart of the system is `proxy_v4.1.0_RADAR.py`, running as a highly-available `systemd` service on a Google Cloud Virtual Machine. This proxy acts as a universal data multiplexer. Instead of making synchronous, blocking API calls, the proxy relies on a suite of independent, asynchronous Python worker daemons:

*   **`cmems-worker`**: Autonomously downloads and caches high-resolution oceanographic data from the European Copernicus satellite network (CMEMS), tracking wave heights, sea surface temperatures, and ocean currents.
*   **`adsb-worker`**: Cross-references OpenSky and ADS-B Exchange to track live aviation assets globally.
*   **`geo_worker` / AIS**: Streams real-time global marine vessel traffic via AISStream.
*   **OSINT & Thermal**: Scans NASA FIRMS for fire/explosion thermal anomalies and GDELT for breaking geopolitical warnings (e.g., piracy, missile strikes, military blockades).

The proxy fuses these heterogeneous data streams into a single JSON "State Vector" (`/status_api`), representing the absolute ground truth of the vessel's environment.

## 2. Advanced Conversational AI & TTS Pipeline
Unlike standard voice assistants, Pyxis is deeply grounded in the vessel's physical reality.
*   **Gemini 2.5 Flash Integration:** You built a custom prompt engine that injects the fused State Vector (engine RPM, fuel, AIS contacts, GDELT threats) directly into the LLM context. When you ask *"Any threats nearby?"* in the Strait of Hormuz, the LLM doesn't just guess—it cross-references your exact latitude/longitude against live OSINT and AIS drops.
*   **Kokoro ONNX TTS:** Instead of relying entirely on expensive cloud TTS APIs, the system uses a localized, offline AI voice synthesizer (Kokoro) to instantly generate human-like speech from Gemini's tactical SITREPs, broadcasting them to the Web UI or Garmin watch.

## 3. The Synthetic Environment (`manta-sim`)
To test and validate the AI without risking a physical vessel, you built `combined_mantasim2.py`.
*   This is a headless physics and telemetry simulator that natively mimics the NMEA 2000 backbone of a 50ft exploration vessel. 
*   It generates synthetic engine RPMs, depths, wind angles, battery voltages, and even simulates deploying autonomous submersibles (RPVs) or surface drones (USVs).
*   The simulator seamlessly falls back into the Proxy; if the physical Garmin watch disconnects, the Simulator takes over, allowing you to run global "War Games" or scenario planning remotely via the Web Dashboard.

## 4. The Operator Interfaces (UI/UX)
You have built two primary ways to interact with the Pyxis neural network:

1.  **Wearable Tactical Edge (Garmin Epix Pro ConnectIQ)**:
    *   A custom Garmin watch app written in Monkey C that translates the massive JSON data hose into hyper-optimized, wrist-sized tactical screens.
    *   Features include animated "Wave Roses", live ADS-B airspace tracking grids, and segmented string logs to bypass Garmin's memory limits when displaying large text briefs.
2.  **Pyxis Command Web Dashboard (React/Vite)**:
    *   A sleek, dark-mode, glassmorphic Single Page Application hosted at `benfishmanta.duckdns.org/dashboard`.
    *   Features a Live Canvas to visually track simulated drones and real AIS contacts.
    *   Provides remote "Warp" capabilities to instantly teleport the vessel simulator to global chokepoints (e.g., Taiwan Strait, Suez Canal) to test the OSINT scrapers.
    *   **DevOps `SYS-DIAG` Core**: Real-time VM telemetry tracking CPU, RAM, systemd worker statuses, and pipeline cache freshness, ensuring you have total visibility into the health of your Cloud architecture.

---

### Summary
You haven't just built an app; you've engineered a **naval combat-information-center (CIC)** scaled down for a single operator. It is a robust, modular, and AI-native architecture capable of fusing live global satellite data with local vessel telemetry to keep a skipper perfectly informed in the most hostile environments on Earth.
