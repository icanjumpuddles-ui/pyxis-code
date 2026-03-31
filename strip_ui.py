import sys
with open('proxy_v3.9.3_FINAL.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

out = []
skip = False
for line in lines:
    if line.startswith("@app.route('/')"):
        skip = True
        out.append(line)
        # Inject the new pristine UI
        out.append("@requires_auth\n")
        out.append("def player_lite():\n")
        out.append("    return \"\"\"\n")
        out.append("<!DOCTYPE html>\n<html>\n<head>\n    <title>PYXIS TRACKER</title>\n    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0, maximum-scale=1.0\" />\n    <link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\" />\n    <script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\"></script>\n    <style>\n        body { background: #000; margin: 0; padding: 0; height: 100vh; width: 100vw; overflow: hidden; }\n        #map { height: 100%; width: 100%; }\n        .hud { position: absolute; top: 10px; left: 10px; z-index: 9999; background: rgba(0,20,0,0.8); border: 1px solid #0f0; padding: 10px; font-family: monospace; color: #0f0; pointer-events: none; }\n    </style>\n</head>\n<body>\n    <div id=\"map\"></div>\n    <div class=\"hud\" id=\"hud\">LINKING...</div>\n    <script>\n        let map = L.map('map', {zoomControl: false}).setView([-37.84, 144.91], 10);\n        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(map);\n        let pyxisMarker = L.circleMarker([0,0], {color: '#0f0', radius: 8, fillOpacity: 1}).addTo(map);\n        let watchMarker = L.circleMarker([0,0], {color: '#00f', radius: 6, fillOpacity: 0.8}).addTo(map);\n        let tail = L.polyline([], {color: '#0f0', weight: 2}).addTo(map);\n\n        async function tick() {\n            try {\n                const r = await fetch('/status_api?bust=' + Date.now());\n                const d = await r.json();\n                if (d.lat && d.lon) {\n                    pyxisMarker.setLatLng([d.lat, d.lon]);\n                    map.panTo([d.lat, d.lon]);\n                }\n                if (d.CREW_LAT && d.CREW_LON) {\n                    watchMarker.setLatLng([d.CREW_LAT, d.CREW_LON]);\n                }\n                let hudText = \"PYXIS: \" + (d.lat||0).toFixed(5) + \", \" + (d.lon||0).toFixed(5) + \"<br>\";\n                hudText += \"WATCH: \" + ((d.CREW_LAT||0).toFixed(5) + \", \" + (d.CREW_LON||0).toFixed(5));\n                document.getElementById('hud').innerHTML = hudText;\n\n                const hr = await fetch('/history_api');\n                const h = await hr.json();\n                if(h && h.length > 0) { tail.setLatLngs(h); }\n            } catch(e) {}\n        }\n        setInterval(tick, 3000);\n        tick();\n    </script>\n</body>\n</html>\n")
        out.append("\"\"\"\n")
    elif line.startswith("@app.route('/web_inbox_sync')"):
        skip = False
        
    if not skip:
        out.append(line)

with open('proxy_v3.9.3_FINAL.py', 'w', encoding='utf-8') as f:
    f.writelines(out)
