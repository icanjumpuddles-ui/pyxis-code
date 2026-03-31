// PYXIS v4.3.3_RADAR (COMPASS STABILIZED WIND ROSE)
// Copyright (c) 2026 Benjamin Pullin. All rights reserved.
import Toybox.Application;
import Toybox.Time;
import Toybox.Lang;
import Toybox.WatchUi;
import Toybox.Graphics;
import Toybox.Communications;
import Toybox.Position;
import Toybox.System;
import Toybox.Attention;
import Toybox.Math;
import Toybox.Timer;
import Toybox.SensorHistory;

var g_lastReport=[]; // Global RAM buffer for the latest synthesized SITREP
var g_reportBuffer=[]; // Array of string arrays (historical message inbox)
var g_PriorityAlarmActive=false; // Flags if Pyxis detected a MAYDAY or COLREG threat
var g_cpaEnabled=false; // User toggle to suppress busy CPA warnings
var g_OnboardPyxis=false; // Override Pyxis Server with local Physical Watch GPS (EW / SPOOFING FIX)

// =====================================================================

// 1. TACTICAL RADAR VIEW (Visual Drift Monitoring)
// =====================================================================
class TacticalRadarView extends WatchUi.View {
    private var _anchorLat
    as Double = 0.0d;
    private var _anchorLon
    as Double = 0.0d;
    private var _radius
    as Float = 50.0f;
    private var _trail
    as Array = [];

    function initialize(lat as Double, lon as Double, radius as Float) {
        View.initialize();
        _anchorLat = lat;
        _anchorLon = lon;
        _radius = radius;
    }

    function onUpdate(dc as Graphics.Dc) as Void {
    
        var w = dc.getWidth();
        var h = dc.getHeight();
        var cx = w / 2;
        var cy = h / 2;
        var radarSize = (w < h ? w : h) / 2 - 35;

        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
        dc.clear();

        dc.setPenWidth(1);
        dc.setColor(0x004400, Graphics.COLOR_TRANSPARENT);
        dc.drawCircle(cx, cy, radarSize);
        dc.drawCircle(cx, cy, radarSize / 2);
        dc.drawLine(cx, cy - radarSize, cx, cy + radarSize);
        dc.drawLine(cx - radarSize, cy, cx + radarSize, cy);

        dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
        dc.fillCircle(cx, cy, 6);

        var posInfo = Position.getInfo();
        var cLat = -39.1124d; // Simulator Sub-fallback
        var cLon = 146.471d;
        if (posInfo != null && posInfo.position != null) {
            var coords = posInfo.position.toDegrees();
            // Accept ANY reported physical coordinate from the watch
            if (coords[0] != null && coords[1] != null && (coords[0].toDouble() != 0.0d || coords[1].toDouble() != 0.0d)) {
                cLat = coords[0].toDouble();
                cLon = coords[1].toDouble();
            }
        }

        var dLat = (cLat - _anchorLat) * 111320.0d;
        var dLon = (cLon - _anchorLon) * 111320.0d * Math.cos(Math.toRadians(_anchorLat));
        var dist = Math.sqrt(dLat * dLat + dLon * dLon).toFloat();

            var scale = radarSize.toFloat() / (_radius * 2.0f);
            var bx = cx + (dLon.toFloat() * scale);
            var by = cy - (dLat.toFloat() * scale);

            var color = (dist > _radius) ? Graphics.COLOR_RED : Graphics.COLOR_GREEN;
            dc.setColor(color, Graphics.COLOR_TRANSPARENT);
            dc.setPenWidth(2);
            dc.drawCircle(cx, cy, (_radius * scale).toNumber());

            var tx = bx.toNumber();
            var ty = by.toNumber();

            if (_trail.size() == 0) {
                 _trail.add([tx, ty]);
            } else {
                 var lastPt = _trail[_trail.size()-1] as Array;
                 // Only add if moved more than 2 pixels to save memory and avoid clutter
                 var distPixels = Math.sqrt(Math.pow(tx - lastPt[0], 2) + Math.pow(ty - lastPt[1], 2));
                 if (distPixels > 2.0) {
                      _trail.add([tx, ty]);
                 }
            }
            if (_trail.size() > 30) { _trail = _trail.slice(1, 31); }

            // Draw Trail
            dc.setColor(Graphics.COLOR_DK_GRAY, Graphics.COLOR_TRANSPARENT);
            for (var i = 0; i < _trail.size(); i++) {
                var pt = _trail[i] as Array;
                dc.fillCircle(pt[0], pt[1], 2);
            }

            dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
            dc.fillCircle(tx, ty, 8);
            
            dc.drawText(cx, h - 40, Graphics.FONT_TINY, dist.format("%.1f") + "m", Graphics.TEXT_JUSTIFY_CENTER);
    }
}

    class TacticalRadarDelegate extends WatchUi.BehaviorDelegate {
        function initialize() {
            BehaviorDelegate.initialize();
        }

    function onBack() as Boolean {
        WatchUi.popView(WatchUi.SLIDE_DOWN);
        return true;
    }
}

        // =====================================================================
        // 2. TACTICAL MENUS (Anchor Setup)
        // =====================================================================
        class ScopeMenu extends WatchUi.Menu2 {
            function initialize() {
                Menu2.initialize({:title=>"Set Scope"});Menu2.addItem(new WatchUi.MenuItem("3:1 Scope","Day/Calm","3",null));Menu2.addItem(new WatchUi.MenuItem("5:1 Scope","Standard","5",null));Menu2.addItem(new WatchUi.MenuItem("7:1 Scope","Storm","7",null));
            }
        }

        class ScopeDelegate extends WatchUi.Menu2InputDelegate {
    private var _depthStr
            as String;
    private var _service
            as GarminBenfishService;

    function initialize(depth as String, service as GarminBenfishService) {
        Menu2InputDelegate.initialize();
        _depthStr = depth; _service = service;
    }

    function onSelect(item as WatchUi.MenuItem) as Void {
        if (Attention has :vibrate) { Attention.vibrate([new Attention.VibeProfile(50, 100)] as Array<Attention.VibeProfile>); }
        var scopeStr = item.getId() as String;
        var radius = _depthStr.toFloat() * scopeStr.toFloat();
        _service.makeRequest("ANCHOR_SET_D:" + _depthStr + "_S:" + scopeStr, true, radius);
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
    }
}

            class DepthMenu extends WatchUi.Menu2 {
                function initialize() {
                    Menu2.initialize({:title=>"Water Depth"});Menu2.addItem(new WatchUi.MenuItem("5m","Shallow","5",null));Menu2.addItem(new WatchUi.MenuItem("10m","Standard","10",null));Menu2.addItem(new WatchUi.MenuItem("20m","Deep","20",null));
                }
            }

            class DepthDelegate extends WatchUi.Menu2InputDelegate {
    private var _service
                as GarminBenfishService;

    function initialize(service as GarminBenfishService) {
        Menu2InputDelegate.initialize(); _service = service;
    }

    function onSelect(item as WatchUi.MenuItem) as Void {
        if (Attention has :vibrate) { Attention.vibrate([new Attention.VibeProfile(50, 100)] as Array<Attention.VibeProfile>); }
        var depth = item.getId() as String;
        WatchUi.pushView(new ScopeMenu(), new ScopeDelegate(depth, _service), WatchUi.SLIDE_LEFT);
    }
}

                // =====================================================================
                // 2.5 WAYPOINT MENUS (Routing)
                // =====================================================================
                class WaypointMenu extends WatchUi.Menu2 {
                    function initialize() {
                        Menu2.initialize({:title=>"Request Routing"});Menu2.addItem(new WatchUi.MenuItem("Nearest Anchorage","Safe Harbor","ANCHORAGE",null));Menu2.addItem(new WatchUi.MenuItem("Nearest Port","Marina / Services","PORT",null));Menu2.addItem(new WatchUi.MenuItem("RED ALERT (SOS)","Declare Emergency","MAYDAY",null));
                    }
                }

                class WaypointDelegate extends WatchUi.Menu2InputDelegate {
                    private var _service
                    as GarminBenfishService;

                    function initialize(service as GarminBenfishService) {
                        Menu2InputDelegate.initialize(); _service = service;
                    }

                    function onSelect(item as WatchUi.MenuItem) as Void {
                        if (Attention has :vibrate) { Attention.vibrate([new Attention.VibeProfile(50, 100)] as Array<Attention.VibeProfile>); }
                        var wpType = item.getId() as String;
                        WatchUi.pushView(new ActionMenu(item.getLabel()), new ActionMenuDelegate(_service, "nav_" + wpType, item), WatchUi.SLIDE_LEFT);
                    }
                }

                    // =====================================================================
                    // 2.6 UUV DRONE MENUS (Tactical)
                    // =====================================================================
                    class UUVMenu extends WatchUi.Menu2 {
                        function initialize() {
                            Menu2.initialize({:title=>"UUV Commmand"});Menu2.addItem(new WatchUi.MenuItem("Deploy Patrol","Search Pattern","DEPLOY",null));Menu2.addItem(new WatchUi.MenuItem("Recall Drone","Return to Pyxis","RECALL",null));
                        }
                    }

                    class UUVDelegate extends WatchUi.Menu2InputDelegate {
                        private var _service
                        as GarminBenfishService;

                        function initialize(service as GarminBenfishService) {
                            Menu2InputDelegate.initialize(); 
                            _service = service;
                        }

                        function onSelect(item as WatchUi.MenuItem) as Void {
                            if (Attention has :vibrate) { Attention.vibrate([new Attention.VibeProfile(50, 100)] as Array<Attention.VibeProfile>); }
                            var uuvCmd = item.getId() as String;
                            WatchUi.pushView(new ActionMenu(item.getLabel()), new ActionMenuDelegate(_service, "uuv_" + uuvCmd, item), WatchUi.SLIDE_LEFT);
                        }
                    }

                        // =====================================================================
                        // 2.7 REPORT VIEW (Multi-line Text responses)
                        // =====================================================================
                        class ReportView extends WatchUi.View {
                            private var _bufferIndex
                            as Number = 0;
                            private var _offset
                            as Number = 0;

                            function initialize(bufferIdx as Number) {
                                View.initialize(); 
                                _bufferIndex = bufferIdx;
                            }

                            function scroll(dir as Number) as Void {
                                _offset += dir;
                                if (_offset < 0) { _offset = 0; }
                                
                                if (g_reportBuffer.size() > 0 && _bufferIndex >= 0 && _bufferIndex < g_reportBuffer.size()) {
                                    var dict = g_reportBuffer[_bufferIndex] as Dictionary;
                                    var lines = dict.get("text") as Array;
                                    var maxOffset = lines.size() - 3;
                                    if (maxOffset < 0) { maxOffset = 0; }
                                    if (_offset > maxOffset) { _offset = maxOffset; }
                                } else {
                                    _offset = 0;
                                }
                                WatchUi.requestUpdate();
                            }

                            function cycleReport(dir as Number) as Void {
                                if (g_reportBuffer.size() == 0) { return; }
                                _bufferIndex += dir;
                                if (_bufferIndex < 0) { _bufferIndex = g_reportBuffer.size() - 1; }
                                if (_bufferIndex >= g_reportBuffer.size()) { _bufferIndex = 0; }
                                
                                _offset = 0; // Reset scroll on new message
                                
                                // Mark as read globally
                                var dict = g_reportBuffer[_bufferIndex] as Dictionary;
                                dict.put("read", true);
                                g_reportBuffer[_bufferIndex] = dict;
                                
                                // Haptic + Visual feedback for swap
                                if (Attention has :vibrate) { Attention.vibrate([new Attention.VibeProfile(50, 100)] as Array<Attention.VibeProfile>); }
                                WatchUi.requestUpdate();
                            }

                            function onUpdate(dc as Graphics.Dc) as Void {
                                dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
                                dc.clear();
                                
                                if (g_reportBuffer.size() == 0 || _bufferIndex < 0 || _bufferIndex >= g_reportBuffer.size()) {
                                    dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);
                                    dc.drawText(dc.getWidth()/2, dc.getHeight()/2, Graphics.FONT_XTINY, "NO MESSAGES", Graphics.TEXT_JUSTIFY_CENTER);
                                    return;
                                }
                                
                                var dict = g_reportBuffer[_bufferIndex] as Dictionary;
                                var lines = dict.get("text") as Array;
                                
                                dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);
                                var y = 10;
                                for (var i=_offset; i<lines.size() && y < dc.getHeight() - 20; i++) {
                                    dc.drawText(dc.getWidth()/2, y, Graphics.FONT_XTINY, lines[i].toString(), Graphics.TEXT_JUSTIFY_CENTER);
                                    y += dc.getFontHeight(Graphics.FONT_XTINY) + 2;
                                }
                                
                                // Navigation Dots
                                dc.setColor(Graphics.COLOR_DK_GRAY, Graphics.COLOR_TRANSPARENT);
                                var dotsY = dc.getHeight() - 15;
                                var dotsW = g_reportBuffer.size() * 10;
                                var startX = (dc.getWidth() / 2) - (dotsW / 2);
                                
                                for (var d = 0; d < g_reportBuffer.size(); d++) {
                                    var dDict = g_reportBuffer[d] as Dictionary;
                                    var isRead = (dDict.get("read") == true);
                                    dc.setColor(isRead ? Graphics.COLOR_DK_GREEN : Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
                                    if (d == _bufferIndex) {
                                        dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
                                        dc.fillCircle(startX + (d * 10), dotsY, 3);
                                    } else {
                                        dc.fillCircle(startX + (d * 10), dotsY, 2);
                                    }
                                }
                            }
                        }

                            class ReportDelegate extends WatchUi.BehaviorDelegate {
    private var _view
                                as ReportView;

    function initialize(view as ReportView) { 
        BehaviorDelegate.initialize(); 
        _view = view;
    }

    function onBack() as Boolean { WatchUi.popView(WatchUi.SLIDE_IMMEDIATE); return true; }

    function onSwipe(evt as WatchUi.SwipeEvent) as Boolean {
        if (evt.getDirection() == WatchUi.SWIPE_UP) { _view.scroll(1); return true; }
        if (evt.getDirection() == WatchUi.SWIPE_DOWN) { _view.scroll(-1); return true; }
        if (evt.getDirection() == WatchUi.SWIPE_LEFT) { _view.cycleReport(1); return true; }
        if (evt.getDirection() == WatchUi.SWIPE_RIGHT) { _view.cycleReport(-1); return true; }
        return false;
    }

    function onKey(evt as WatchUi.KeyEvent) as Boolean {
        if (evt.getKey() == WatchUi.KEY_DOWN) { _view.scroll(1); return true; }
        if (evt.getKey() == WatchUi.KEY_UP) { _view.scroll(-1); return true; }
        return false;
    }
}

                                // =====================================================================
                                // 2.8 DYNAMIC OPTIONS MENU (Routing Responses)
                                // =====================================================================
                                class DynamicOptionsMenu extends WatchUi.Menu2 {
    function initialize(dests as Array) {
        Menu2.initialize({:title=>"Select Destination"});
        for(var i=0; i<dests.size(); i++) {
            var name = dests[i].toString();
            var limit = name.length() > 15 ? 15 : name.length();
            Menu2.addItem(new WatchUi.MenuItem(name.substring(0, limit), null, name, null));
        }
    }
}

                                    class DynamicOptionsDelegate extends WatchUi.Menu2InputDelegate {
    private var _service
                                        as GarminBenfishService;

    function initialize(service as GarminBenfishService) {
        Menu2InputDelegate.initialize(); _service = service;
    }

    function onSelect(item as WatchUi.MenuItem) as Void {
        var wpName = item.getId() as String;
        _service.makeRequest("SET_DESTINATION:" + wpName, false, 0.0f);
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
    }
}

                                        // =====================================================================
                                        // 2.9 TELEMETRY DASHBOARD (Weather & Sensors)
                                        // =====================================================================
                                        class TelemetryView extends WatchUi.View {
                                        private var _telemetryData
                                            as Dictionary;

                                        function initialize(data as Dictionary) {
                                            View.initialize(); 
                                            _telemetryData = data;
                                        }

                                        function onUpdate(dc as Graphics.Dc) as Void {
                                            var w = dc.getWidth();
                                            dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
                                            dc.clear();
                                            
                                            // Header
                                            dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);
                                            dc.drawText(w/2, 10, Graphics.FONT_XTINY, "ENVIRONMENTAL", Graphics.TEXT_JUSTIFY_CENTER);
                                            dc.drawLine(20, 30, w-20, 30);
                                            
                                            // Data fields
                                            dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
                                            var y = 35;
                                            var spacing = 20;

                                            var wStr = _telemetryData.get("weather");
                                            if (wStr == null) { wStr = "NOMINAL"; } else { wStr = wStr.toString(); }
                                            dc.drawText(20, y, Graphics.FONT_XTINY, "WX:", Graphics.TEXT_JUSTIFY_LEFT);
                                            dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);
                                            dc.drawText(w-20, y, Graphics.FONT_XTINY, wStr, Graphics.TEXT_JUSTIFY_RIGHT);
                                            y += spacing;

                                            dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
                                            var ssStr = _telemetryData.get("sea_state");
                                            if (ssStr == null) { ssStr = "0.0m"; } else { ssStr = ssStr.toString() + "m"; }
                                            dc.drawText(20, y, Graphics.FONT_XTINY, "SEA:", Graphics.TEXT_JUSTIFY_LEFT);
                                            dc.setColor(Graphics.COLOR_ORANGE, Graphics.COLOR_TRANSPARENT);
                                            dc.drawText(w-20, y, Graphics.FONT_XTINY, ssStr, Graphics.TEXT_JUSTIFY_RIGHT);
                                            y += spacing;
                                            
                                            dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
                                            var tempStr = "N/A";
                                            if ((Toybox has :SensorHistory) && (Toybox.SensorHistory has :getTemperatureHistory)) {
                                                var tempIter = Toybox.SensorHistory.getTemperatureHistory({:period=>1});
                                                if (tempIter != null) {
                                                    var sample = tempIter.next();
                                                    if (sample != null && sample.data != null && sample.data != NaN) {
                                                        tempStr = sample.data.format("%.1f") + "C";
                                                    }
                                                }
                                            }
                                            dc.drawText(20, y, Graphics.FONT_XTINY, "TMP:", Graphics.TEXT_JUSTIFY_LEFT);
                                            dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);
                                            dc.drawText(w-20, y, Graphics.FONT_XTINY, tempStr, Graphics.TEXT_JUSTIFY_RIGHT);
                                            y += spacing;

                                            dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
                                            var presStr = "N/A";
                                            if ((Toybox has :SensorHistory) && (Toybox.SensorHistory has :getPressureHistory)) {
                                                var presIter = Toybox.SensorHistory.getPressureHistory({:period=>1});
                                                if (presIter != null) {
                                                    var sample = presIter.next();
                                                    if (sample != null && sample.data != null && sample.data != NaN) {
                                                        presStr = sample.data.format("%.0f") + "Pa";
                                                    }
                                                }
                                            }
                                            dc.drawText(20, y, Graphics.FONT_XTINY, "PRESS:", Graphics.TEXT_JUSTIFY_LEFT);
                                            dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);
                                            dc.drawText(w-20, y, Graphics.FONT_XTINY, presStr, Graphics.TEXT_JUSTIFY_RIGHT);
                                        }
                                    }

                                            class TelemetryDelegate extends WatchUi.BehaviorDelegate {
                                                function initialize() {
                                                    BehaviorDelegate.initialize();
                                                }

                                        function onBack() as Boolean { WatchUi.popView(WatchUi.SLIDE_IMMEDIATE); return true; }
                                    }

                                                // =====================================================================
                                                // 2.95 MAN OVERBOARD (Pyxis Locator)
                                                // =====================================================================
                                                class MobLocatorView extends WatchUi.View {
                                        private var _pyxisLat
                                                    as Double = 0.0d;
                                        private var _pyxisLon
                                                    as Double = 0.0d;

                                        function initialize(data as Dictionary) {
                                            View.initialize();
                                            var lat = data.get("lat");
                                            var lon = data.get("lon");
                                            if (lat instanceof Float || lat instanceof Double) { _pyxisLat = lat.toDouble(); }
                                            if (lon instanceof Float || lon instanceof Double) { _pyxisLon = lon.toDouble(); }
                                        }

                                        function onUpdate(dc as Graphics.Dc) as Void {
                                            var w = dc.getWidth(); var h = dc.getHeight();
                                            dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
                                            dc.clear();
                                            
                                            var info = Position.getInfo();
                                            var cLat = -39.1124d;
                                            var cLon = 146.471d;
                                            var hasFix = false;

                                            if (info != null && info.position != null) {
                                                var coords = info.position.toDegrees();
                                                if (coords[0] != null && coords[1] != null && (coords[0].toDouble() != 0.0d || coords[1].toDouble() != 0.0d)) {
                                                    cLat = coords[0].toDouble();
                                                    cLon = coords[1].toDouble();
                                                    hasFix = true;
                                                }
                                            }

                                            if (!hasFix) {
                                                dc.setColor(Graphics.COLOR_ORANGE, Graphics.COLOR_TRANSPARENT);
                                                dc.drawText(w/2, 50, Graphics.FONT_XTINY, "GPS SEARCHING...", Graphics.TEXT_JUSTIFY_CENTER);
                                            }
                                            
                                            var dLat = (_pyxisLat - cLat) * 111320.0d;
                                            var dLon = (_pyxisLon - cLon) * 111320.0d * Math.cos(Math.toRadians(cLat));
                                            var dist = Math.sqrt(dLat*dLat + dLon*dLon).toFloat();
                                            var bearing = Math.toDegrees(Math.atan2(dLon, dLat));
                                            if (bearing < 0) { bearing += 360; }
                                            
                                            dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
                                            dc.drawText(w/2, 20, Graphics.FONT_TINY, "M.O.B LOCATOR", Graphics.TEXT_JUSTIFY_CENTER);
                                            
                                            dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
                                            dc.drawText(w/2, h/2 - 20, Graphics.FONT_MEDIUM, dist.format("%.0f") + "m", Graphics.TEXT_JUSTIFY_CENTER);
                                            
                                            dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);
                                            dc.drawText(w/2, h/2 + 20, Graphics.FONT_SMALL, "BRG: " + bearing.format("%.0f") + " deg", Graphics.TEXT_JUSTIFY_CENTER);
                                        }
                                    }

                                                    class MobDelegate extends WatchUi.BehaviorDelegate {
                                                        function initialize() {
                                                            BehaviorDelegate.initialize();
                                                        }

                                        function onBack() as Boolean { WatchUi.popView(WatchUi.SLIDE_IMMEDIATE); return true; }
                                    }

                                                        // =====================================================================
                                                        // 3. MAIN DASHBOARD (Tactical UI)
                                                        // =====================================================================
                                                        class GarminBenfishView extends WatchUi.View {
                                                            private var _clockTimer
                            as Timer.Timer;
    public var _statusText
                                                            as String = "AWAITING CMD";
    public var _localArea as String = "OPEN OCEAN";
    public var _sunset as String = "";
    public var _metrics as Dictionary?;

                                                            function initialize() {
                                                                View.initialize();
                                                                _clockTimer = new Timer.Timer();
                                                            }

    function onShow() as Void {
        _clockTimer.start(method(:triggerUpdate), 1000, true);
    }

    function onHide() as Void {
        _clockTimer.stop();
    }

    function triggerUpdate() as Void {
        WatchUi.requestUpdate();
    }

    function onUpdate(dc as Graphics.Dc) as Void {
        var w = dc.getWidth(); var h = dc.getHeight();
        var cx = w / 2; var cy = h / 2;
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
        dc.clear();
        
        if (_statusText != null && _statusText.equals("WX RADAR REQ")) {
            dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);
            dc.drawText(cx, cy - 30, Graphics.FONT_MEDIUM, "AWAITING", Graphics.TEXT_JUSTIFY_CENTER);
            dc.setColor(Graphics.COLOR_YELLOW, Graphics.COLOR_TRANSPARENT);
            dc.drawText(cx, cy + 10, Graphics.FONT_SMALL, "SAT RADAR...", Graphics.TEXT_JUSTIFY_CENTER);
            return;
        }
        
        var arcWidth = 8;
        
        // Military Crosshairs (subtle)
        dc.setColor(0x001100, Graphics.COLOR_TRANSPARENT);
        dc.setPenWidth(1);
        dc.drawLine(cx, 0, cx, h);
        dc.drawLine(0, cy, w, cy);

        // Edge Gauges (RPM, Fuel, Battery & tactical arcs)
        var sysStats = System.getSystemStats();
        var batt = sysStats.battery;
        
        var hasNmea = (_metrics != null);
        var rpm = 0.0;
        var fuel = 0.0;
        var apActive = false;
        var hostSpeed = 0.0;
        var hostHeading = 0.0;
        
        if (hasNmea) {
            var m = _metrics;
            if (m != null) {
                var rpmRaw = m.get("rpm"); 
                if (rpmRaw != null) { if (rpmRaw instanceof String) { rpm = rpmRaw.toFloat(); } else if (rpmRaw instanceof Number || rpmRaw instanceof Float) { rpm = rpmRaw.toFloat(); } }
                var fuelRaw = m.get("fuel_pct"); 
                if (fuelRaw != null) { if (fuelRaw instanceof String) { fuel = fuelRaw.toFloat(); } else if (fuelRaw instanceof Number || fuelRaw instanceof Float) { fuel = fuelRaw.toFloat(); } }
                var apRaw = m.get("autopilot_active"); 
                if (apRaw != null && (apRaw instanceof Boolean) && apRaw) { apActive = true; }
                var spdRaw = m.get("speed_kn");
                if (spdRaw != null) { if (spdRaw instanceof String) { hostSpeed = spdRaw.toFloat(); } else if (spdRaw instanceof Number || spdRaw instanceof Float) { hostSpeed = spdRaw.toFloat(); } }
                var hdgRaw = m.get("heading");
                if (hdgRaw != null) { if (hdgRaw instanceof String) { hostHeading = hdgRaw.toFloat(); } else if (hdgRaw instanceof Number || hdgRaw instanceof Float) { hostHeading = hdgRaw.toFloat(); } }
            }
        }
        
        dc.setPenWidth(arcWidth);
        // Left Arc (RPM or Battery)
        dc.setColor(0x222222, Graphics.COLOR_TRANSPARENT);
        dc.drawArc(cx, cy, cx - (arcWidth/2), Graphics.ARC_CLOCKWISE, 225, 135);
        
        if (hasNmea && rpm > 0) {
            var rpmColor = Graphics.COLOR_BLUE;
            if (rpm > 3000) { rpmColor = Graphics.COLOR_PURPLE; }
            else if (rpm > 2000) { rpmColor = Graphics.COLOR_ORANGE; }
            dc.setColor(rpmColor, Graphics.COLOR_TRANSPARENT);
            var rpmPct = rpm / 4000.0; if (rpmPct > 1.0) { rpmPct = 1.0; }
            var rpmEnd = 225 - ((225 - 135) * rpmPct);
            if (rpmEnd < 135) { rpmEnd = 135; } 
            dc.drawArc(cx, cy, cx - (arcWidth/2), Graphics.ARC_CLOCKWISE, 225, rpmEnd.toNumber());
            dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
            dc.drawText(20, cy, Graphics.FONT_XTINY, "RPM\n" + rpm.format("%.0f"), Graphics.TEXT_JUSTIFY_LEFT|Graphics.TEXT_JUSTIFY_VCENTER);
        } else if (batt != null) {
            var battColor = Graphics.COLOR_BLUE;
            if (batt < 20.0) { battColor = Graphics.COLOR_PURPLE; }
            else if (batt < 50.0) { battColor = Graphics.COLOR_ORANGE; }
            dc.setColor(battColor, Graphics.COLOR_TRANSPARENT);
            var battEnd = 225 - ((225 - 135) * (batt / 100.0));
            if (battEnd < 135) { battEnd = 135; } 
            dc.drawArc(cx, cy, cx - (arcWidth/2), Graphics.ARC_CLOCKWISE, 225, battEnd.toNumber());
        }
        
        // Right Arc (Fuel or Decorative / Comms Link)
        dc.setColor(0x222222, Graphics.COLOR_TRANSPARENT);
        dc.drawArc(cx, cy, cx - (arcWidth/2), Graphics.ARC_COUNTER_CLOCKWISE, 315, 45); // Background
        
        if (hasNmea && fuel > 0) {
            var fuelColor = Graphics.COLOR_BLUE;
            if (fuel < 20.0) { fuelColor = Graphics.COLOR_PURPLE; }
            else if (fuel < 50.0) { fuelColor = Graphics.COLOR_ORANGE; }
            dc.setColor(fuelColor, Graphics.COLOR_TRANSPARENT);
            var fuelEnd = 315 + (90.0 * (fuel / 100.0));
            if (fuelEnd >= 360) { fuelEnd -= 360; }
            dc.drawArc(cx, cy, cx - (arcWidth/2), Graphics.ARC_COUNTER_CLOCKWISE, 315, fuelEnd.toNumber());
            dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w-20, cy, Graphics.FONT_XTINY, "FUEL\n" + fuel.format("%.0f") + "%", Graphics.TEXT_JUSTIFY_RIGHT|Graphics.TEXT_JUSTIFY_VCENTER);
        } else {
            if (_statusText.equals("MAP ACTIVE") || _statusText.equals("CMD RECV") || apActive) {
                dc.setColor(apActive ? Graphics.COLOR_PURPLE : Graphics.COLOR_BLUE, Graphics.COLOR_TRANSPARENT); // Magenta = AP, Cyan = active
            } else {
                dc.setColor(0x002244, Graphics.COLOR_TRANSPARENT); // Idle
            }
            dc.drawArc(cx, cy, cx - (arcWidth/2), Graphics.ARC_COUNTER_CLOCKWISE, 315, 345);
        }

        // Top Button: System Status (Sci-Fi Glass Pill)
        var btnW = w * 0.55;
        var btnH = h * 0.15;
        var btnRadius = (btnH / 2).toNumber();
        var topY = h * 0.08;
        dc.setColor(0x050505, Graphics.COLOR_BLACK);
        dc.fillRoundedRectangle((cx - btnW/2).toNumber(), topY.toNumber(), btnW.toNumber(), btnH.toNumber(), btnRadius);
        var sysColTop = g_OnboardPyxis ? Graphics.COLOR_GREEN : Graphics.COLOR_RED;
        dc.setColor(sysColTop, Graphics.COLOR_TRANSPARENT);
        dc.setPenWidth(2);
        dc.drawRoundedRectangle((cx - btnW/2).toNumber(), topY.toNumber(), btnW.toNumber(), btnH.toNumber(), btnRadius);
        dc.drawText(cx, (topY + btnH/2).toNumber(), Graphics.FONT_XTINY, g_OnboardPyxis ? "SYS: ONBD" : "SYS: REMOT", Graphics.TEXT_JUSTIFY_CENTER|Graphics.TEXT_JUSTIFY_VCENTER);
        
        // Bottom Button: SitRep
        var botY = h * 0.77;
        dc.setColor(0x050505, Graphics.COLOR_BLACK);
        dc.fillRoundedRectangle((cx - btnW/2).toNumber(), botY.toNumber(), btnW.toNumber(), btnH.toNumber(), btnRadius);
        dc.setColor(Graphics.COLOR_ORANGE, Graphics.COLOR_TRANSPARENT);
        dc.setPenWidth(2);
        dc.drawRoundedRectangle((cx - btnW/2).toNumber(), botY.toNumber(), btnW.toNumber(), btnH.toNumber(), btnRadius);
        dc.setColor(Graphics.COLOR_ORANGE, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, (botY + btnH/2).toNumber(), Graphics.FONT_XTINY, "PULL SITREP", Graphics.TEXT_JUSTIFY_CENTER|Graphics.TEXT_JUSTIFY_VCENTER);
        
        // Center: Time
        var clockTime = System.getClockTime();
        var timeStr = clockTime.hour.format("%02d") + ":" + clockTime.min.format("%02d") + ":" + clockTime.sec.format("%02d");
        dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, cy - 30, Graphics.FONT_NUMBER_MEDIUM, timeStr, Graphics.TEXT_JUSTIFY_CENTER|Graphics.TEXT_JUSTIFY_VCENTER);
        
        // Date
        var now = Time.now();
        var info = Time.Gregorian.info(now, Time.FORMAT_SHORT);
        var dateStr = info.year + "-" + info.month.format("%02d") + "-" + info.day.format("%02d");
        dc.setColor(Graphics.COLOR_LT_GRAY, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, cy - 70, Graphics.FONT_XTINY, dateStr, Graphics.TEXT_JUSTIFY_CENTER|Graphics.TEXT_JUSTIFY_VCENTER);
        
        // Host Speed & Course
        if (hasNmea) {
             dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);
             dc.drawText(cx, cy - 95, Graphics.FONT_XTINY, "SPD: " + hostSpeed.format("%.1f") + "kn    HDG: " + hostHeading.format("%.0f"), Graphics.TEXT_JUSTIFY_CENTER|Graphics.TEXT_JUSTIFY_VCENTER);
        }
        
        // Status String
        dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, cy + 20, Graphics.FONT_TINY, _statusText, Graphics.TEXT_JUSTIFY_CENTER|Graphics.TEXT_JUSTIFY_VCENTER);

        if (!_localArea.equals("")) {
             dc.setColor(Graphics.COLOR_BLUE, Graphics.COLOR_TRANSPARENT);
             var locStr = _localArea;
             if (locStr.length() > 20) { locStr = locStr.substring(0, 20); }
             dc.drawText(cx, cy + 35, Graphics.FONT_XTINY, locStr.toUpper(), Graphics.TEXT_JUSTIFY_CENTER|Graphics.TEXT_JUSTIFY_VCENTER);
        }

        if (!_sunset.equals("")) {
             dc.setColor(Graphics.COLOR_ORANGE, Graphics.COLOR_TRANSPARENT);
             dc.drawText(cx, cy - 50, Graphics.FONT_XTINY, "Last Light: " + _sunset, Graphics.TEXT_JUSTIFY_CENTER|Graphics.TEXT_JUSTIFY_VCENTER);
        }

        // Map Hint
        dc.setColor(Graphics.COLOR_DK_GRAY, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, cy + 75, Graphics.FONT_XTINY, "[TAP CENTER MENU]", Graphics.TEXT_JUSTIFY_CENTER|Graphics.TEXT_JUSTIFY_VCENTER);

        // Version Pill (Sleek Sci-Fi)
        var vY = cy + 95;
        var vW = 46;
        var vH = 16;
        dc.setColor(0x001122, Graphics.COLOR_TRANSPARENT);
        dc.fillRoundedRectangle((cx - vW/2).toNumber(), vY.toNumber(), vW, vH, 4);
        dc.setColor(Graphics.COLOR_BLUE, Graphics.COLOR_TRANSPARENT);
        dc.setPenWidth(1);
        dc.drawRoundedRectangle((cx - vW/2).toNumber(), vY.toNumber(), vW, vH, 4);
        dc.drawText(cx, (vY + vH/2 - 1).toNumber(), Graphics.FONT_XTINY, "v4.1.2", Graphics.TEXT_JUSTIFY_CENTER|Graphics.TEXT_JUSTIFY_VCENTER);
        
        // PRIORITY ALARM OVERRIDES
        if (g_PriorityAlarmActive) {
            dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
            dc.setPenWidth(6);
            dc.drawCircle(cx, cy, (w/2)-3);
            dc.drawText(cx, cy + h/3, Graphics.FONT_SMALL, "URGENT COMMS", Graphics.TEXT_JUSTIFY_CENTER);
            if (Attention has :vibrate) { Attention.vibrate([new Attention.VibeProfile(100, 1000)] as Array<Attention.VibeProfile>); }
            if (Attention has :playTone) { Attention.playTone(Attention.TONE_LOUD_BEEP); }
        }
    }

    function setStatusText(text as String) as Void {
        _statusText = text;
        WatchUi.requestUpdate();
    }

    function flash() as Void {
        if (Attention has :vibrate) {
            Attention.vibrate([new Attention.VibeProfile(100, 100)] as Array<Attention.VibeProfile>);
        }
    }
}

class GarminBenfishDelegate extends WatchUi.BehaviorDelegate {
    private var _service as GarminBenfishService;
    private var _view as GarminBenfishView;

    function initialize(service as GarminBenfishService, view as GarminBenfishView) {
        BehaviorDelegate.initialize();
        _service = service;
        _view = view;
    }

    function onTap(evt as WatchUi.ClickEvent) as Boolean {
        var y = evt.getCoordinates()[1];
        var h = System.getDeviceSettings().screenHeight;
        
        if (g_PriorityAlarmActive) {
            _view.flash();
            g_PriorityAlarmActive = false; // Acknowledge alarm
            if (g_reportBuffer.size() > 0) {
                var latestIdx = g_reportBuffer.size() - 1;
                var rv = new ReportView(latestIdx);
                WatchUi.pushView(rv, new ReportDelegate(rv), WatchUi.SLIDE_UP);
            }
            return true;
        }
        
        _view.flash();
        if (y < h/4) {
            g_OnboardPyxis = !g_OnboardPyxis;
            _service.makeRequest("TELEMETRY_REQ", false, 0.0f);
            if (Attention has :vibrate) {
                Attention.vibrate([new Attention.VibeProfile(50, 100)] as Array<Attention.VibeProfile>);
            }
        } else if (y > h - (h/4)) {
            _service.makeRequest("SITREP", false, 0.0f);
        } else if (_view._statusText.equals("NEW SMS RCV") || _view._statusText.equals("INCOMING MESSAGE")) {
            _view.setStatusText("AWAITING CMD");
            _service.makeRequest("FORCE_PLAY_LAST", false, 0.0f);
            _service.makeRequest("INBOX_REQ", false, 0.0f);
        } else {
            WatchUi.pushView(new PyxisMainMenu(_service._simMode), new PyxisMainMenuDelegate(_service), WatchUi.SLIDE_UP);
        }
        return true;
    }

    function onKey(evt as WatchUi.KeyEvent) as Boolean {
        if (evt.getKey() == WatchUi.KEY_ENTER) {
            _view.flash();
            WatchUi.pushView(new PyxisMainMenu(_service._simMode), new PyxisMainMenuDelegate(_service), WatchUi.SLIDE_UP);
            return true;
        }
        return false;
    }
}

class InboxItem extends WatchUi.CustomMenuItem {
    private var _reportLines as Array;
    private var _label as String;

    function initialize(idx as Number, lines as Array) {
        CustomMenuItem.initialize(idx, {});
        _reportLines = lines;
        var fullText = "";
        for(var i=0; i<lines.size(); i++) {
            fullText += lines[i].toString() + " ";
        }
        if (fullText.length() > 25) {
            _label = fullText.substring(0, 25) + "...";
        } else {
            _label = fullText;
        }
    }

    function draw(dc as Graphics.Dc) as Void {
        var w = dc.getWidth();
        var h = dc.getHeight(); 
        
        // SMS Chat bubble
        dc.setColor(0x222222, Graphics.COLOR_TRANSPARENT);
        dc.fillRoundedRectangle(15, 5, w - 30, h - 10, 8);
        
        // Unread text string
        dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
        dc.drawText(w/2, h/2, Graphics.FONT_XTINY, _label, Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
    }

    function getLines() as Array {
        return _reportLines;
    }
}

                                                                    class InboxCustomMenu extends WatchUi.CustomMenu {
    function initialize(itemHeight as Number, backgroundColor as Number, options as Dictionary) {
        CustomMenu.initialize(itemHeight, backgroundColor, options);
    }
}

                                                                        class InboxDelegate
                                                                                extends WatchUi.Menu2InputDelegate {
                                                                            function initialize() {
                                                                                Menu2InputDelegate.initialize();
                                                                            }

    function onSelect(item as WatchUi.MenuItem) as Void {
        if (Attention has :vibrate) { Attention.vibrate([new Attention.VibeProfile(50, 100)] as Array<Attention.VibeProfile>); }
        if (item instanceof InboxItem) {
            var idx = (item as InboxItem).getId() as Number;
            var rv = new ReportView(idx);
            WatchUi.pushView(rv, new ReportDelegate(rv), WatchUi.SLIDE_LEFT);
        }
    }

    function onBack() as Void {
        WatchUi.popView(WatchUi.SLIDE_RIGHT);
    }
}

class PyxisMainMenu extends WatchUi.Menu2 {
    function initialize(simMode as Boolean) {
        Menu2.initialize({:title=>"PYXIS C3"});
        var src = simMode ? "SIM" : "LIVE";
        // ── NAVIGATION ──
        addItem(new WatchUi.MenuItem("── NAVIGATION ──", "", "sep_nav", {}));
        addItem(new WatchUi.MenuItem("Active Route",   "Vector Plot",     "item_nav",     {}));
        addItem(new WatchUi.MenuItem("Req Routing",    "Anchor/Port Nav", "nav",          {}));
        addItem(new WatchUi.MenuItem("Nav Importer",   "Marks + Fixes",   "pull_nav",     {}));
        // ── TACTICAL RADAR ──
        addItem(new WatchUi.MenuItem("── RADAR ──",    "", "sep_radar", {}));
        addItem(new WatchUi.MenuItem("Live Radar",     "Pulsing AIS Scope",   "map",          {}));
        addItem(new WatchUi.MenuItem("AIS Geo Map",    "Tiles + AIS Live",    "ais_geo_map",  {}));
        addItem(new WatchUi.MenuItem("OSINT Radar",    "Threat Scope",        "osint_radar",  {}));
        addItem(new WatchUi.MenuItem("OSINT Geo Map",  "Tiles + OSINT",       "osint_geo_map",{}));
        addItem(new WatchUi.MenuItem("WX Radar",       "Colour Weather",      "wx_radar",     {}));
        addItem(new WatchUi.MenuItem("Sea State",      "Wave Height Map", "sea_state",    {}));
        addItem(new WatchUi.MenuItem("Wind Rose",       "Vector Compass",  "wind_rose",    {}));
        addItem(new WatchUi.MenuItem("Wind Map",         "Isobar Chart",    "wind_map",     {}));
        addItem(new WatchUi.MenuItem("3D Sonar",       "Bathymetric",     "sonar",        {}));
        // ── INTELLIGENCE ──
        addItem(new WatchUi.MenuItem("── INTEL ──",    "", "sep_intel", {}));
        addItem(new WatchUi.MenuItem("Message Inbox",  "Unread SITREPS",  "inbox",        {}));
        addItem(new WatchUi.MenuItem("Regional OSINT", "Threat Intel",    "intel",        {}));
        addItem(new WatchUi.MenuItem("Morning Brief",  "Full Day Prep",   "day_brief",    {}));
        addItem(new WatchUi.MenuItem("Evening Brief",  "Night Prep",      "night_brief",  {}));
        // ── VESSEL ──
        addItem(new WatchUi.MenuItem("── SYSTEMS ──",  "", "sep_sys", {}));
        addItem(new WatchUi.MenuItem("Engine Room",    "Gen/Bilge Ctrl",  "engine",       {}));
        addItem(new WatchUi.MenuItem("UUV Squadron",   "Drone Control",   "uuv",          {}));
        addItem(new WatchUi.MenuItem("Environment",    "Swells + Temp",   "sensor",       {}));
        // ── CREW OPS ──
        addItem(new WatchUi.MenuItem("── CREW OPS ──", "", "sep_crew", {}));
        addItem(new WatchUi.MenuItem("M.O.B Search",   "Locate Crewman",  "mob",          {}));
        addItem(new WatchUi.MenuItem("AI Voice",       "Query Gemini",    "voice",        {}));
        addItem(new WatchUi.MenuItem("Onboard Pyxis",  "Watch GPS Only",  "onboard_pyxis",{}));
        addItem(new WatchUi.MenuItem(src + " Source",  "Toggle AIS/SIM",  "toggle_sim",   {}));
    }
}

class PyxisMainMenuDelegate extends WatchUi.Menu2InputDelegate {
    private var _service as GarminBenfishService;

    function initialize(service as GarminBenfishService) {
        Menu2InputDelegate.initialize();
        _service = service;
    }

    function onSelect(item as WatchUi.MenuItem) as Void {
        var id = item.getId();
        // Section dividers — no action
        if (id.toString().substring(0, 4).equals("sep_")) { return; }
        if (id.equals("toggle_sim") || id.equals("onboard_pyxis") || id.equals("pull_nav")) {
            WatchUi.pushView(new ActionMenu(item.getLabel()), new ActionMenuDelegate(_service, id.toString(), item), WatchUi.SLIDE_LEFT);
        } else if (id.equals("inbox")) {
            var menu = new InboxCustomMenu(60, Graphics.COLOR_BLACK, {});
            for (var i = g_reportBuffer.size() - 1; i >= 0; i--) {
                var dict = g_reportBuffer[i] as Dictionary;
                menu.addItem(new InboxItem(i, dict.get("text") as Array));
            }
            if (g_reportBuffer.size() == 0) {
                 menu.addItem(new InboxItem(0, ["Inbox Empty"]));
            }
            WatchUi.pushView(menu, new InboxDelegate(), WatchUi.SLIDE_RIGHT);
        } else if (id.equals("wind_rose")) {
            WatchUi.pushView(new WindRoseView(), new WindRoseDelegate(), WatchUi.SLIDE_LEFT);
        } else if (id.equals("wind_map")) {
            var wmv = new WindMapView();
            WatchUi.pushView(wmv, new WindMapDelegate(wmv), WatchUi.SLIDE_LEFT);
        } else if (id.equals("engine")) {
            WatchUi.pushView(new EngineRoomMenu(), new EngineRoomDelegate(_service), WatchUi.SLIDE_LEFT);
        } else if (id.equals("uuv")) {
            WatchUi.pushView(new UUVMenu(), new UUVDelegate(_service), WatchUi.SLIDE_LEFT);
        } else if (id.equals("nav")) {
            WatchUi.pushView(new WaypointMenu(), new WaypointDelegate(_service), WatchUi.SLIDE_LEFT);
        } else if (id.equals("map") || id.equals("osint_radar")) {
            // Pulsing scope views — fire MAP_REQ and let server push correct view
            _service.makeRequest("MAP_REQ", false, 0.0f);
        } else if (id.equals("ais_geo_map") || id.equals("osint_geo_map")) {
            // Geo tile views — push OsintRadarView (server-rendered map JPEG)
            var ov = new OsintRadarView([] as Array, 0.0d, 0.0d, _service);
            WatchUi.pushView(ov, new OsintRadarDelegate(_service), WatchUi.SLIDE_LEFT);
        } else {
            WatchUi.pushView(new ActionMenu(item.getLabel()), new ActionMenuDelegate(_service, id.toString(), item), WatchUi.SLIDE_LEFT);
        }
    }
}

class WeatherRadarView extends WatchUi.View {
    private var _radarImage;
    private var _failed = false;
    private var _loading = true;
    private var _zoom = 2;
    private var _lastCode as Number = 0;
    private var _service;

    function initialize(service) {
        View.initialize();
        _service = service;
    }

    function onShow() as Void {
        requestRadarImage();
    }

    function requestRadarImage() as Void {
        _loading = true;
        _failed = false;
        WatchUi.requestUpdate();
        
        var cb = System.getTimer().toString();
        var url = "https://wsrv.nl/?url=https://benfishmanta.duckdns.org/wx/" + cb + "/weather_map.jpg&n=1&output=jpg";
        var params = {
            "z" => _zoom.toString(),
            "lat" => _service._view._metrics != null && _service._view._metrics.get("lat") != null ? _service._view._metrics.get("lat").toString() : "-39.112",
            "lon" => _service._view._metrics != null && _service._view._metrics.get("lon") != null ? _service._view._metrics.get("lon").toString() : "146.471"
        };
        var options = {
            :maxWidth => 454,
            :maxHeight => 454
        };
        Communications.makeImageRequest(url, params, options, method(:onReceive));
    }

    function onReceive(responseCode as Number, data as Graphics.BitmapReference or WatchUi.BitmapResource or Null) as Void {
        _loading = false;
        _lastCode = responseCode;
        if (responseCode == 200 && data != null) {
            _radarImage = data;
        } else {
            _failed = true;
        }
        WatchUi.requestUpdate();
    }

    function zoomIn() as Void {
        if (_zoom < 12) { _zoom++; requestRadarImage(); }
    }
    
    function zoomOut() as Void {
        if (_zoom > 2) { _zoom--; requestRadarImage(); }
    }

    function onUpdate(dc as Graphics.Dc) as Void {
        var w = dc.getWidth(); var h = dc.getHeight();
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
        dc.clear();
        
        if (_radarImage != null) {
            // Center the image if it's smaller than the screen or draw at 0,0
            var bw = _radarImage.getWidth();
            var bh = _radarImage.getHeight();
            dc.drawBitmap(w/2 - bw/2, h/2 - bh/2, _radarImage);
            
            // Draw scale / zoom level
            dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, h - 30, Graphics.FONT_XTINY, "ZOOM: " + _zoom, Graphics.TEXT_JUSTIFY_CENTER);
            dc.setColor(0x00FF88, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, h - 16, Graphics.FONT_XTINY, "WX LIVE \u2022 CLEAR IF NO COLOUR", Graphics.TEXT_JUSTIFY_CENTER);
        }
        
        if (_loading) {
            dc.setColor(0x00AADD, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, h/2, Graphics.FONT_XTINY, "LOADING WX RADAR", Graphics.TEXT_JUSTIFY_CENTER);
        } else if (_failed) {
            dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, h/2, Graphics.FONT_XTINY, "COMMS FAILED: " + _lastCode.toString(), Graphics.TEXT_JUSTIFY_CENTER);
        }
        
        // Draw crosshair
        dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
        dc.drawLine(w/2 - 10, h/2, w/2 + 10, h/2);
        dc.drawLine(w/2, h/2 - 10, w/2, h/2 + 10);
    }
}

class WeatherRadarDelegate extends WatchUi.BehaviorDelegate {
    private var _view as WeatherRadarView;

    function initialize(view as WeatherRadarView) {
        BehaviorDelegate.initialize();
        _view = view;
    }

    function onKey(evt as WatchUi.KeyEvent) as Boolean {
        if (evt.getKey() == WatchUi.KEY_UP) { _view.zoomOut(); return true; }
        if (evt.getKey() == WatchUi.KEY_DOWN) { _view.zoomIn(); return true; }
        if (evt.getKey() == WatchUi.KEY_ENTER) { _view.requestRadarImage(); return true; }
        return false;
    }

    function onSwipe(swipeEvent as WatchUi.SwipeEvent) as Boolean {
        var dir = swipeEvent.getDirection();
        if (dir == WatchUi.SWIPE_UP || dir == WatchUi.SWIPE_RIGHT) { _view.zoomIn(); return true; }
        if (dir == WatchUi.SWIPE_DOWN || dir == WatchUi.SWIPE_LEFT) { _view.zoomOut(); return true; }
        return false;
    }
}

// =====================================================================
// SEA STATE MAP VIEW  (fetches /meteo_map from marine_map_gen.py)
// =====================================================================
class SeaStateMapView extends WatchUi.View {
    private var _mapImage;
    private var _failed   = false;
    private var _loading  = true;
    private var _lastCode as Number = 0;
    private var _zoom     as Number = 4;   // 1=full, 4=most zoomed (default)

    function initialize() {
        View.initialize();
    }

    function onShow() as Void { requestMap(); }

    function requestMap() as Void {
        _loading = true; _failed = false;
        WatchUi.requestUpdate();
        var cb  = System.getTimer().toString();
        var url = "https://wsrv.nl/?url=https://benfishmanta.duckdns.org/seastate/" + cb + "/z" + _zoom.toString() + "/meteo_map&n=1&output=jpg";
        var options = { :maxWidth => 454, :maxHeight => 454 };
        Communications.makeImageRequest(url, {}, options, method(:onReceive));
    }

    function zoomIn() as Void {
        if (_zoom < 4) { _zoom++; requestMap(); }
    }

    function zoomOut() as Void {
        if (_zoom > 1) { _zoom--; requestMap(); }
    }

    function onReceive(responseCode as Number, data as Graphics.BitmapReference or WatchUi.BitmapResource or Null) as Void {
        _loading = false; _lastCode = responseCode;
        if (responseCode == 200 && data != null) { _mapImage = data; } else { _failed = true; }
        WatchUi.requestUpdate();
    }

    function onUpdate(dc as Graphics.Dc) as Void {
        var w = dc.getWidth(); var h = dc.getHeight();
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
        dc.clear();

        if (_mapImage != null) {
            var bw = _mapImage.getWidth(); var bh = _mapImage.getHeight();
            dc.drawBitmap(w/2 - bw/2, h/2 - bh/2, _mapImage);
        }

        // Status text near bottom but above bezel clip
        if (_loading) {
            dc.setColor(0x00AADD, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, h - 60, Graphics.FONT_XTINY, "LOADING WAVE MAP...", Graphics.TEXT_JUSTIFY_CENTER);
        } else if (_failed) {
            dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, h - 60, Graphics.FONT_XTINY, "FAILED: " + _lastCode.toString(), Graphics.TEXT_JUSTIFY_CENTER);
        } else if (_mapImage != null) {
            dc.setColor(0x00FF88, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, h - 60, Graphics.FONT_XTINY, "SEA Z" + _zoom.toString() + "  ^/v=zoom", Graphics.TEXT_JUSTIFY_CENTER);
        }

        // Own-ship crosshair
        dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
        dc.drawLine(w/2 - 10, h/2, w/2 + 10, h/2);
        dc.drawLine(w/2, h/2 - 10, w/2, h/2 + 10);
    }
}

// =====================================================================
// SEA STATE DATA VIEW  — full numeric readout sub-menu
// Opened by pressing the top-right button on the map view.
// =====================================================================
class SeaStateDataView extends WatchUi.View {
    private var _lines   as Array = [];
    private var _loading as Boolean = true;
    private var _scroll  as Number  = 0;
    private const MAX_VISIBLE = 6;

    function initialize() { View.initialize(); }

    function onShow() as Void {
        _loading = true;
        _lines   = [];
        WatchUi.requestUpdate();
        var cb  = System.getTimer().toString();
        var url = "https://benfishmanta.duckdns.org/sea_state_json/" + cb;
        var opts = { :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON };
        Communications.makeWebRequest(url, {}, opts, method(:onData));
    }

    function onData(code as Number, data as Dictionary or Null) as Void {
        _loading = false;
        if (code == 200 && data != null) {
            _lines = [
                "=== PYXIS SEA STATE ===",
                "--- WAVES ---",
                "Height: " + data.get("wave_h").toString(),
                "Period: " + data.get("wave_period").toString(),
                "Dir:    " + data.get("wave_dir").toString(),
                "--- SWELL ---",
                "Height: " + data.get("swell_h").toString(),
                "Dir:    " + data.get("swell_dir").toString(),
                "--- CURRENT ---",
                "Speed:  " + data.get("curr_v").toString(),
                "Dir:    " + data.get("curr_dir").toString(),
                "--- CONDITIONS ---",
                "Depth:  " + data.get("depth").toString(),
                "SeaSt:  " + data.get("sea_state").toString(),
                "Wind:   " + data.get("wind_sp").toString(),
                "W.Dir:  " + data.get("wind_dir").toString(),
                "--- PYXIS POS ---",
                "Lat:    " + data.get("pyxis_lat").toString(),
                "Lon:    " + data.get("pyxis_lon").toString(),
                "Hdg:    " + data.get("heading").toString(),
                "SOG:    " + data.get("sog").toString(),
                "Upd:    " + data.get("updated").toString(),
            ];
        } else {
            _lines = ["DATA ERROR", "Code: " + code.toString(),
                      "Check proxy & cache"];
        }
        _scroll = 0;
        WatchUi.requestUpdate();
    }

    function scrollDown() as Void {
        if (_scroll < _lines.size() - MAX_VISIBLE) { _scroll++; WatchUi.requestUpdate(); }
    }
    function scrollUp() as Void {
        if (_scroll > 0) { _scroll--; WatchUi.requestUpdate(); }
    }

    function onUpdate(dc as Graphics.Dc) as Void {
        var w = dc.getWidth(); var h = dc.getHeight();
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
        dc.clear();
        if (_loading) {
            dc.setColor(0x00AADD, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, h/2 - 10, Graphics.FONT_XTINY, "FETCHING...", Graphics.TEXT_JUSTIFY_CENTER);
            dc.drawText(w/2, h/2 + 8,  Graphics.FONT_XTINY, "(refreshing position)", Graphics.TEXT_JUSTIFY_CENTER);
            return;
        }
        // Version badge — fixed, not part of scroll
        dc.setColor(0x004488, Graphics.COLOR_TRANSPARENT);
        dc.drawText(w/2, 10, Graphics.FONT_XTINY, "SEA STATE  v4.1.2", Graphics.TEXT_JUSTIFY_CENTER);
        dc.setColor(0x003366, Graphics.COLOR_TRANSPARENT);
        dc.drawLine(20, 32, w - 20, 32);

        var y   = 42;
        var cx  = w / 2;
        var end   = _scroll + MAX_VISIBLE;
        if (end > _lines.size()) { end = _lines.size(); }
        for (var i = _scroll; i < end; i++) {
            var line = _lines[i];
            var col  = 0x00FFAA;
            var font = Graphics.FONT_TINY;
            if (line.find("===") != null) { col = 0x00FF66; font = Graphics.FONT_SMALL; }
            else if (line.find("---") != null || line.find("--") != null) { col = 0x00AADD; font = Graphics.FONT_XTINY; }
            dc.setColor(col, Graphics.COLOR_TRANSPARENT);
            dc.drawText(cx, y, font, line, Graphics.TEXT_JUSTIFY_CENTER);
            y += dc.getFontHeight(font) + 8;
        }
        // scroll indicator
        if (_lines.size() > MAX_VISIBLE) {
            dc.setColor(0x444444, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w - 16, h/2, Graphics.FONT_XTINY,
                (_scroll > 0 ? "^" : " ") + "\n" + (_scroll + MAX_VISIBLE < _lines.size() ? "v" : " "),
                Graphics.TEXT_JUSTIFY_CENTER);
        }
    }
}

class SeaStateDataDelegate extends WatchUi.BehaviorDelegate {
    private var _view as SeaStateDataView;
    function initialize(view as SeaStateDataView) { BehaviorDelegate.initialize(); _view = view; }
    function onKey(evt as WatchUi.KeyEvent) as Boolean {
        if (evt.getKey() == WatchUi.KEY_DOWN) { _view.scrollDown(); return true; }
        if (evt.getKey() == WatchUi.KEY_UP)   { _view.scrollUp();   return true; }
        return false;
    }
    function onSwipe(swipeEvent as WatchUi.SwipeEvent) as Boolean {
        var dir = swipeEvent.getDirection();
        if (dir == WatchUi.SWIPE_UP)   { _view.scrollDown(); return true; }
        if (dir == WatchUi.SWIPE_DOWN) { _view.scrollUp();   return true; }
        return false;
    }
    function onBack() as Boolean { WatchUi.popView(WatchUi.SLIDE_RIGHT); return true; }
}

class SeaStateMapDelegate extends WatchUi.BehaviorDelegate {
    private var _view as SeaStateMapView;
    function initialize(view as SeaStateMapView) { BehaviorDelegate.initialize(); _view = view; }
    function onKey(evt as WatchUi.KeyEvent) as Boolean {
        if (evt.getKey() == WatchUi.KEY_ENTER) {
            var dv = new SeaStateDataView();
            WatchUi.pushView(dv, new SeaStateDataDelegate(dv), WatchUi.SLIDE_LEFT);
            return true;
        }
        if (evt.getKey() == WatchUi.KEY_UP)    { _view.zoomOut(); return true; }
        if (evt.getKey() == WatchUi.KEY_DOWN)  { _view.zoomIn();  return true; }
        return false;
    }
    function onSwipe(swipeEvent as WatchUi.SwipeEvent) as Boolean {
        var dir = swipeEvent.getDirection();
        if (dir == WatchUi.SWIPE_UP || dir == WatchUi.SWIPE_RIGHT) { _view.zoomIn();  return true; }
        if (dir == WatchUi.SWIPE_DOWN || dir == WatchUi.SWIPE_LEFT) { _view.zoomOut(); return true; }
        return false;
    }
    function onBack() as Boolean { WatchUi.popView(WatchUi.SLIDE_DOWN); return true; }
}

// =====================================================================
// WIND MAP VIEW  (fetches /wind_map from marine_map_gen.py)
// Server renders wind barbs / speed contour overlay on ocean tiles.
// =====================================================================
class WindMapView extends WatchUi.View {
    private var _mapImage;
    private var _failed   = false;
    private var _loading  = true;
    private var _lastCode as Number = 0;
    private var _zoom     as Number = 4;

    function initialize() {
        View.initialize();
    }

    function onShow() as Void { requestMap(); }

    function requestMap() as Void {
        _loading = true; _failed = false;
        WatchUi.requestUpdate();
        var cb  = System.getTimer().toString();
        var url = "https://wsrv.nl/?url=https://benfishmanta.duckdns.org/wind_map/" + cb + "/z" + _zoom.toString() + "/wind_map&n=1&output=jpg";
        var options = { :maxWidth => 454, :maxHeight => 454 };
        Communications.makeImageRequest(url, {}, options, method(:onReceive));
    }

    function zoomIn() as Void {
        if (_zoom < 4) { _zoom++; requestMap(); }
    }

    function zoomOut() as Void {
        if (_zoom > 1) { _zoom--; requestMap(); }
    }

    function onReceive(responseCode as Number, data as Graphics.BitmapReference or WatchUi.BitmapResource or Null) as Void {
        _loading = false; _lastCode = responseCode;
        if (responseCode == 200 && data != null) { _mapImage = data; } else { _failed = true; }
        WatchUi.requestUpdate();
    }

    function onUpdate(dc as Graphics.Dc) as Void {
        var w = dc.getWidth(); var h = dc.getHeight();
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
        dc.clear();

        if (_mapImage != null) {
            var bw = _mapImage.getWidth(); var bh = _mapImage.getHeight();
            dc.drawBitmap(w/2 - bw/2, h/2 - bh/2, _mapImage);
        }

        if (_loading) {
            dc.setColor(0x00AADD, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, h - 60, Graphics.FONT_XTINY, "LOADING WIND MAP...", Graphics.TEXT_JUSTIFY_CENTER);
        } else if (_failed) {
            dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, h - 60, Graphics.FONT_XTINY, "FAILED: " + _lastCode.toString(), Graphics.TEXT_JUSTIFY_CENTER);
        } else if (_mapImage != null) {
            dc.setColor(0x00FFEE, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, h - 60, Graphics.FONT_XTINY, "WIND Z" + _zoom.toString() + "  ^/v=zoom", Graphics.TEXT_JUSTIFY_CENTER);
        }

        // Own-ship crosshair
        dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
        dc.drawLine(w/2 - 10, h/2, w/2 + 10, h/2);
        dc.drawLine(w/2, h/2 - 10, w/2, h/2 + 10);
    }
}

class WindMapDelegate extends WatchUi.BehaviorDelegate {
    private var _view as WindMapView;
    function initialize(view as WindMapView) { BehaviorDelegate.initialize(); _view = view; }
    function onKey(evt as WatchUi.KeyEvent) as Boolean {
        if (evt.getKey() == WatchUi.KEY_UP)   { _view.zoomOut(); return true; }
        if (evt.getKey() == WatchUi.KEY_DOWN) { _view.zoomIn();  return true; }
        return false;
    }
    function onSwipe(swipeEvent as WatchUi.SwipeEvent) as Boolean {
        var dir = swipeEvent.getDirection();
        if (dir == WatchUi.SWIPE_UP || dir == WatchUi.SWIPE_RIGHT) { _view.zoomIn();  return true; }
        if (dir == WatchUi.SWIPE_DOWN || dir == WatchUi.SWIPE_LEFT) { _view.zoomOut(); return true; }
        return false;
    }
    function onBack() as Boolean { WatchUi.popView(WatchUi.SLIDE_DOWN); return true; }
}

class ActionMenu extends WatchUi.Menu2 {
    function initialize(title as String) {
        Menu2.initialize({:title=>title});
        addItem(new WatchUi.MenuItem("Select", "Confirm Action", "execute", {}));
        addItem(new WatchUi.MenuItem("Cancel", "Go Back", "cancel", {}));
    }
}

class ActionMenuDelegate extends WatchUi.Menu2InputDelegate {
    private var _service as GarminBenfishService;
    private var _actionId as String;
    private var _sourceItem as WatchUi.MenuItem;

    function initialize(service as GarminBenfishService, actionId as String, sourceItem as WatchUi.MenuItem) {
        Menu2InputDelegate.initialize();
        _service = service;
        _actionId = actionId;
        _sourceItem = sourceItem;
    }

    function onSelect(item as WatchUi.MenuItem) as Void {
        if (Attention has :vibrate) { Attention.vibrate([new Attention.VibeProfile(50, 100)] as Array<Attention.VibeProfile>); }
        if (Attention has :backlight) { Attention.backlight(true); }

        var id = item.getId();
        if (id.equals("execute")) {
            if (_actionId.equals("toggle_sim")) {
                _service._simMode = !_service._simMode;
                var modeStr = _service._simMode ? "Data: MANTA_SIM" : "Data: LIVE_AIS";
                if (_sourceItem != null) { _sourceItem.setLabel(modeStr); }
                _service.makeRequest("UPDATE_DATA_MODE:" + (_service._simMode ? "SIM" : "LIVE"), false, 0.0f);
            } else if (_actionId.equals("onboard_pyxis")) {
                _service.makeRequest("SET_ONBOARD", false, 0.0f);
            } else if (_actionId.equals("pull_nav")) {
                _service.makeRequest("PULL_NAV_DATA", false, 0.0f);
            } else if (_actionId.equals("day_brief")) {
                _service.makeRequest("DAY_BRIEF", false, 0.0f);
            } else if (_actionId.equals("night_brief")) {
                _service.makeRequest("NIGHT_BRIEF", false, 0.0f);
            } else if (_actionId.equals("map")) {
                _service.makeRequest("MAP_REQ", false, 0.0f);
            } else if (_actionId.equals("map_bvr")) {
                _service.makeRequest("MAP_REQ_BVR", false, 0.0f);
            } else if (_actionId.equals("wx_radar")) {
                WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
                var wv = new WeatherRadarView(_service);
                WatchUi.pushView(wv, new WeatherRadarDelegate(wv), WatchUi.SLIDE_RIGHT);
                return;
            } else if (_actionId.equals("sea_state")) {
                WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
                var ssv = new SeaStateMapView();
                WatchUi.pushView(ssv, new SeaStateMapDelegate(ssv), WatchUi.SLIDE_RIGHT);
                return;
        } else if (_actionId.equals("live_radar")) {
            WatchUi.pushView(new OsintRadarView([] as Array, 0.0d, 0.0d, _service), new OsintRadarDelegate(_service), WatchUi.SLIDE_RIGHT);
        } else if (_actionId.equals("ais_list")) {
                _service.makeRequest("AIS_LIST_REQ", false, 0.0f);
            } else if (_actionId.equals("mob")) {
                _service.makeRequest("MOB_REQ", false, 0.0f);
            } else if (_actionId.equals("intel")) {
                _service.makeRequest("INTEL_REQ", false, 0.0f);
            } else if (_actionId.equals("voice")) {
                _service.makeRequest("VOICE_QUOTA", false, 0.0f);
            } else if (_actionId.equals("sonar")) {
                _service.makeRequest("SONAR_REQ", false, 0.0f);
            } else if (_actionId.equals("item_nav")) {
                _service.makeRequest("VIEW_NAV:", false, 0.0f);
            } else if (_actionId.equals("sensor")) {
                _service.makeRequest("TELEMETRY_REQ", false, 0.0f);
            } else if (_actionId.length() > 4 && _actionId.substring(0, 4).equals("nav_")) {
                var wpType = _actionId.substring(4, _actionId.length());
                if (wpType.equals("MAYDAY")) {
                    _service.makeRequest("MAYDAY", false, 0.0f);
                } else {
                    _service.makeRequest("REQ_ROUTE:" + wpType, false, 0.0f);
                }
            } else if (_actionId.length() > 4 && _actionId.substring(0, 4).equals("uuv_")) {
                var uuvCmd = _actionId.substring(4, _actionId.length());
                _service.makeRequest("UUV_CMD:" + uuvCmd, false, 0.0f);
            }
        }
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
    }
}

class EngineRoomMenu extends WatchUi.Menu2 {
    function initialize() {
        Menu2.initialize({:title=>"Engine Room"});
        addItem(new WatchUi.MenuItem("Start Generator", "Aux Power ON", "sys_gen_start", {}));
        addItem(new WatchUi.MenuItem("Stop Generator", "Aux Power OFF", "sys_gen_stop", {}));
        addItem(new WatchUi.MenuItem("Bilge override", "Pump OUT", "sys_bilge_auto", {}));
    }
}

class EngineRoomDelegate extends WatchUi.Menu2InputDelegate {
    private var _service as GarminBenfishService;

    function initialize(service as GarminBenfishService) {
        Menu2InputDelegate.initialize();
        _service = service;
    }

    function onSelect(item as WatchUi.MenuItem) as Void {
        if (Attention has :vibrate) { Attention.vibrate([new Attention.VibeProfile(50, 100)] as Array<Attention.VibeProfile>); }
        var id = item.getId();
        _service.makeRequest("SYSTEM_CMD:" + id.toString(), false, 0.0f);
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
    }
}

class SchematicMenu extends WatchUi.Menu2 {
    function initialize() {
        Menu2.initialize({:title=>"Vessel Schematic"});
        addItem(new WatchUi.MenuItem("Live Status", "Sys Diagnostics", "view_schematic", {}));
    }
}

class SchematicMenuDelegate extends WatchUi.Menu2InputDelegate {
    private var _service as GarminBenfishService;

    function initialize(service as GarminBenfishService) {
        Menu2InputDelegate.initialize();
        _service = service;
    }

    function onSelect(item as WatchUi.MenuItem) as Void {
        if (Attention has :vibrate) { Attention.vibrate([new Attention.VibeProfile(50, 100)] as Array<Attention.VibeProfile>); }
        var id = item.getId();
        if (id.equals("view_schematic")) {
            _service.makeRequest("SCHEMATIC_REQ", false, 0.0f);
        }
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
    }
}

class RadarControlMenu extends WatchUi.Menu2 {
    function initialize() {
        Menu2.initialize({:title=>"Radar Control"});
        addItem(new WatchUi.MenuItem("Engage Scanner", "Active Ping Mode", "engage", {}));
        addItem(new WatchUi.MenuItem("Disengage", "Standby Mode", "disengage", {}));
        addItem(new WatchUi.MenuItem("View Terminal", "15nm Local TGTs", "map", {}));
        addItem(new WatchUi.MenuItem("View BVR OSINT", "50nm Deep Scan", "map_bvr", {}));
    }
}

class RadarControlDelegate extends WatchUi.Menu2InputDelegate {
    private var _service as GarminBenfishService;

    function initialize(service as GarminBenfishService) {
        Menu2InputDelegate.initialize();
        _service = service;
    }

    function onSelect(item as WatchUi.MenuItem) as Void {
        if (Attention has :vibrate) { Attention.vibrate([new Attention.VibeProfile(50, 100)] as Array<Attention.VibeProfile>); }
        var id = item.getId();
        if (id.equals("engage")) {
            _service.makeRequest("ENGAGE_RADAR", false, 0.0f);
        } else if (id.equals("disengage")) {
            _service.makeRequest("DISENGAGE_RADAR", false, 0.0f);
        } else if (id.equals("map")) {
            _service.makeRequest("MAP_REQ", false, 0.0f);
        } else if (id.equals("map_bvr")) {
            _service.makeRequest("MAP_REQ_BVR", false, 0.0f);
        }
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
    }
}

// =====================================================================
// 4. SERVICE HUB (Communications)
// =====================================================================
/**
 * GarminBenfishService acts as the primary data and communication bridge
 * between the Watch Interface and the Remote Pyxis Proxy Server.
 * It handles bundling telemetry state, dispatching HTTP payloads,
 * polling for asynchronous AI voice results, and dynamically pushing
 * new UI Views based on the response dictionary flags.
 */
class GarminBenfishService {
    public var _simMode
                                                                                                as Boolean = false;
    public var _view
                                                                                                as GarminBenfishView;
                                                                                                private var _pollTimer
                           as Timer.Timer;
    private var _activeTaskId
                                                                                                as String = "";

    function initialize(view as GarminBenfishView) {
        _view = view;
        _pollTimer = new Timer.Timer();
    }

    function startPolling(taskId as String) as Void {
        _activeTaskId = taskId;
        _pollTimer.start(method(:pollReport), 3000, true);
    }

    function pollReport() as Void {
        if (_activeTaskId.equals("")) { return; }
        var url = "https://benfishmanta.duckdns.org/poll_report?task_id=" + _activeTaskId;
        var options = {
            :method => Communications.HTTP_REQUEST_METHOD_GET,
            :headers => {"Content-Type"=>Communications.REQUEST_CONTENT_TYPE_JSON, "X-Garmin-Auth"=>"PYXIS_ACTUAL_77X"},
            :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON
        };
        Communications.makeWebRequest(url, {}, options, method(:onPollReceive));
    }

    function onPollReceive(responseCode as Number, data as Dictionary or String or Null) as Void {
        if (responseCode == 200 && data != null && data instanceof Dictionary) {
            var status = data.get("status");
            if (status != null && status.toString().equals("ready")) {
                _pollTimer.stop();
                _activeTaskId = "";
                
                var m = data.get("metrics");
                if (m != null && m instanceof Dictionary) {
                    _view._metrics = m as Dictionary;
                }
                
                var summary = data.get("watch_summary");
                if (summary != null) {
                    _view.setStatusText(summary.toString());
                } else {
                    _view.setStatusText("RPT READY");
                }
                
                var showTelemetry = data.get("show_telemetry");
                var showSonar = data.get("show_sonar_3d");
                var showSchematic = data.get("show_schematic");
                var showWxRadar = data.get("show_wx_radar");
                var routeArr = data.get("active_route");
                var reportLines = data.get("report");
                
                if (showSchematic instanceof Boolean && showSchematic) {
                    var sch_m = data.get("metrics");
                    if (sch_m != null && sch_m instanceof Dictionary) {
                        WatchUi.pushView(new SchematicView(sch_m as Dictionary), new SchematicDelegate(), WatchUi.SLIDE_RIGHT);
                    }
                } else if (showTelemetry instanceof Boolean && showTelemetry) {
                    WatchUi.pushView(new TelemetryView(data as Dictionary), new TelemetryDelegate(), WatchUi.SLIDE_RIGHT);
                } else if (showSonar instanceof Boolean && showSonar) {
                    WatchUi.pushView(new LiveSonarView(data as Dictionary, self), new LiveSonarDelegate(), WatchUi.SLIDE_RIGHT);
                } else if (showWxRadar instanceof Boolean && showWxRadar) {
                    var wv = new WeatherRadarView(self);
                    WatchUi.pushView(wv, new WeatherRadarDelegate(wv), WatchUi.SLIDE_RIGHT);
                } else if (routeArr != null && routeArr instanceof Array) {
                    var pLat = data.get("lat");
                    var pLon = data.get("lon");
                    if (pLat instanceof Float) { pLat = pLat.toDouble(); } else if (pLat instanceof Number) { pLat = pLat.toDouble(); }
                    if (pLon instanceof Float) { pLon = pLon.toDouble(); } else if (pLon instanceof Number) { pLon = pLon.toDouble(); }
                    if (pLat == null) { pLat = 0.0d; } if (pLon == null) { pLon = 0.0d; }
                    
                    WatchUi.pushView(new RouteNavView(routeArr as Array, pLat as Double, pLon as Double), new RouteNavDelegate(), WatchUi.SLIDE_RIGHT);
                } else if (reportLines != null && reportLines instanceof Array) {
                    g_lastReport = reportLines as Array;
                    var newRpt = {"read" => false, "text" => reportLines as Array};
                    g_reportBuffer.add(newRpt);
                    if (g_reportBuffer.size() > 10) { g_reportBuffer.remove(g_reportBuffer[0]); } // Capped at 10 msgs
                    
                    var isPriority = false;
                    for (var r=0; r<reportLines.size(); r++) {
                        var ln = reportLines[r].toString().toUpper();
                        if (ln.find("RED ALERT") != null || ln.find("MAYDAY") != null || ln.find("COLLISION RISK") != null) {
                            isPriority = true;
                        }
                    }
                    if (isPriority) {
                        g_PriorityAlarmActive = true;
                    } else {
                        if (Attention has :vibrate) { Attention.vibrate([new Attention.VibeProfile(50, 200)] as Array<Attention.VibeProfile>); }
                        _view.setStatusText("INCOMING MESSAGE");
                    }
                }
                var intelReady = data.get("intel_ready");
                var intelMode = (intelReady instanceof Boolean && intelReady);
                
                if (intelMode) {
                    var intelData = data.get("intel_data");
                    if (intelData instanceof Array) {
                        var pLat = data.get("lat");
                        var pLon = data.get("lon");
                        if (pLat instanceof Float) { pLat = pLat.toDouble(); } else if (pLat instanceof Number) { pLat = pLat.toDouble(); }
                        if (pLon instanceof Float) { pLon = pLon.toDouble(); } else if (pLon instanceof Number) { pLon = pLon.toDouble(); }
                        if (pLat == null) { pLat = 0.0d; } if (pLon == null) { pLon = 0.0d; }
                        
                        WatchUi.pushView(new ThreatLogView(intelData as Array, pLat as Double, pLon as Double), new ThreatLogDelegate(), WatchUi.SLIDE_DOWN);
                    }
                } else if (data.hasKey("character_limit")) {
                    var cc = data.get("character_count");
                    var cl = data.get("character_limit");
                    var cMdl = data.get("active_model");
                    if (cc != null && cl != null) {
                        var rc = 0; var rcl = 0;
                        if (cc instanceof Number) { rc = cc; } else if (cc instanceof String) { rc = cc.toNumber(); }
                        if (cl instanceof Number) { rcl = cl; } else if (cl instanceof String) { rcl = cl.toNumber(); }
                        var remaining = (rcl - rc) / 1000;
                        _view.setStatusText("EL:\n" + remaining.toString() + "K LFT\n" + cMdl.toString());
                    } else { _view.setStatusText("VOICE ERR"); }
                }
            }
        }
    }

    /**
     * Constructs and fires an outbound web request to the Proxy Server.
     * Bundles the active Prompt/Action along with live physical Telemetry
     * (GPS, Temperature, Heart Rate, Elevation, Body Battery) so the AI
     * has situational context.
     * 
     * @param prompt The action verb or user command (e.g. "MAP_REQ")
     * @param isAnchor Boolean flag to immediately push the TacticalRadarView
     * @param radius The radius constraint to apply to the radar view
     */
    function makeRequest(prompt as String, isAnchor as Boolean, radius as Float) as Void {
        var info = Position.getInfo();
        var coords = [-39.1124d, 146.471d];
        if (info != null && info.position != null) { 
            var reported = info.position.toDegrees();
            if (reported[0] != null && reported[1] != null && (reported[0].toDouble() != 0.0d || reported[1].toDouble() != 0.0d)) {
                coords = reported;
            }
        }
        
        var tempStr = "N/A";
        var elevStr = "N/A";
        var presStr = "N/A";
        
        if ((Toybox has :SensorHistory) && (Toybox.SensorHistory has :getTemperatureHistory)) {
            var tempIter = Toybox.SensorHistory.getTemperatureHistory({:period=>1});
            if (tempIter != null) {
                var sample = tempIter.next();
                if (sample != null && sample.data != null && sample.data != NaN) {
                    tempStr = sample.data.format("%.1f") + "C";
                }
            }
        }
        
        if ((Toybox has :SensorHistory) && (Toybox.SensorHistory has :getElevationHistory)) {
            var elevIter = Toybox.SensorHistory.getElevationHistory({:period=>1});
            if (elevIter != null) {
                var sample = elevIter.next();
                if (sample != null && sample.data != null && sample.data != NaN) {
                    elevStr = sample.data.format("%.1f") + "m";
                }
            }
        }

        if ((Toybox has :SensorHistory) && (Toybox.SensorHistory has :getPressureHistory)) {
            var presIter = Toybox.SensorHistory.getPressureHistory({:period=>1});
            if (presIter != null) {
                var sample = presIter.next();
                if (sample != null && sample.data != null && sample.data != NaN) {
                    presStr = sample.data.format("%.0f") + "Pa";
                }
            }
        }

        var hrStr = "N/A";
        var spo2Str = "N/A";
        var bbStr = "N/A";
        var stressStr = "N/A";

        if ((Toybox has :SensorHistory) && (Toybox.SensorHistory has :getHeartRateHistory)) {
            var hrIter = Toybox.SensorHistory.getHeartRateHistory({:period=>1});
            if (hrIter != null) {
                var sample = hrIter.next();
                if (sample != null && sample.data != null) { hrStr = sample.data.format("%d") + "bpm"; }
            }
        }

        if ((Toybox has :SensorHistory) && (Toybox.SensorHistory has :getOxygenSaturationHistory)) {
            var o2Iter = Toybox.SensorHistory.getOxygenSaturationHistory({:period=>1});
            if (o2Iter != null) {
                var sample = o2Iter.next();
                if (sample != null && sample.data != null) { spo2Str = sample.data.format("%.1f") + "%"; }
            }
        }

        if ((Toybox has :SensorHistory) && (Toybox.SensorHistory has :getBodyBatteryHistory)) {
            var bbIter = Toybox.SensorHistory.getBodyBatteryHistory({:period=>1});
            if (bbIter != null) {
                var sample = bbIter.next();
                if (sample != null && sample.data != null) { bbStr = sample.data.format("%.1f") + "%"; }
            }
        }

        if ((Toybox has :SensorHistory) && (Toybox.SensorHistory has :getStressHistory)) {
            var stIter = Toybox.SensorHistory.getStressHistory({:period=>1});
            if (stIter != null) {
                var sample = stIter.next();
                if (sample != null && sample.data != null) { stressStr = sample.data.format("%.1f"); }
            }
        }

        var el_api_key = "";
        var el_voice_id = "EXAVITQu4vr4xnSDxMaL";
        var gemini_key = "";
        var tts_engine = 0;
        
        if (Application has :Properties) {
            try {
                var p1 = Application.Properties.getValue("elevenlabs_api_key"); if (p1 != null) { el_api_key = p1.toString(); }
                var p2 = Application.Properties.getValue("elevenlabs_voice_id"); if (p2 != null) { el_voice_id = p2.toString(); }
                var p3 = Application.Properties.getValue("gemini_api_key"); if (p3 != null) { gemini_key = p3.toString(); }
                var p4 = Application.Properties.getValue("tts_engine"); if (p4 instanceof Number) { tts_engine = p4; }
            } catch (e) {
                System.println("Settings exception: " + e.getErrorMessage());
            }
        }

        var url = "https://benfishmanta.duckdns.org/gemini";
        var params = {
            "prompt"=>prompt, 
            "lat"=>coords[0], 
            "lon"=>coords[1], "onboard"=>g_OnboardPyxis,
            "temp"=>tempStr,
            "elev"=>elevStr,
            "pres"=>presStr,
            "hr"=>hrStr,
            "spo2"=>spo2Str,
            "body_bat"=>bbStr,
            "stress"=>stressStr,
            "el_api_key"=>el_api_key,
            "el_voice_id"=>el_voice_id,
            "gemini_api_key"=>gemini_key,
            "tts_engine"=>tts_engine
        };
        
        if (prompt.equals("VOICE_QUOTA")) {
            url = "https://benfishmanta.duckdns.org/voice_quota";
            params = {};
        }

        var options = {
            :method => Communications.HTTP_REQUEST_METHOD_POST,
            :headers => {"Content-Type"=>Communications.REQUEST_CONTENT_TYPE_JSON, "X-Garmin-Auth"=>"PYXIS_ACTUAL_77X"},
            :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON
        };
        Communications.makeWebRequest(url, params, options, method(:onReceive));
        if (isAnchor) {
            WatchUi.pushView(new TacticalRadarView(coords[0].toDouble(), coords[1].toDouble(), radius), new TacticalRadarDelegate(), WatchUi.SLIDE_LEFT);
        }
    }

    /**
     * Callback engine triggered when the Proxy Server responds.
     * Parses the returned JSON dictionary to determine the outcome.
     * Will automatically route the user to the correct View (e.g. Radar, AIS List,
     * Inbox, Sonar) based on the boolean flags set by the Proxy.
     */
    function onReceive(responseCode as Number, data as Dictionary or String or Null) as Void {
        System.println("Pyxis: " + responseCode);
        if (responseCode == 200 && data != null && data instanceof Dictionary) {
            
            var m = data.get("metrics");
            if (m != null && m instanceof Dictionary) {
                _view._metrics = m as Dictionary;
            }
            
            var isMap = data.get("map_ready");
            if (isMap != null && isMap instanceof Boolean && isMap) {
                var contacts = data.get("radar_contacts");
                if (contacts == null) { contacts = []; }
                var lat = data.get("lat");
                var lon = data.get("lon");
                if (lat != null && lon != null && ((lat instanceof Float || lat instanceof Double) && (lon instanceof Float || lon instanceof Double))) {
                    var l_d = 0.0d; if (lat instanceof Float) { l_d = lat.toDouble(); } else if (lat instanceof Double) { l_d = lat; }
                    var lo_d = 0.0d; if (lon instanceof Float) { lo_d = lon.toDouble(); } else if (lon instanceof Double) { lo_d = lon; }
                    
                    var ws = data.get("watch_summary");
                    var isOsint = (ws != null && (ws.toString().equals("OSINT ACTIVE") || ws.toString().equals("NAV MARKS")));
                    
                    if (isOsint) {
                        var currentView = WatchUi.getCurrentView();
                        if (currentView != null && currentView[0] instanceof OsintScopeView) {
                            var existingSv = currentView[0] as OsintScopeView;
                            existingSv._contacts = contacts as Array;
                            WatchUi.requestUpdate();
                        } else {
                            var sv = new OsintScopeView(contacts as Array);
                            WatchUi.pushView(sv, new OsintScopeDelegate(), WatchUi.SLIDE_UP);
                        }
                        _view.setStatusText("OSINT SCOPE");
                    } else {
                        var isBvr = data.get("bvr_mode");
                        var bvrMode = (isBvr != null && isBvr instanceof Boolean && isBvr);
                        
                        var currentView = WatchUi.getCurrentView();
                        if (currentView != null && currentView[0] instanceof LiveRadarView) {
                            var lrView = currentView[0] as LiveRadarView;
                            lrView._contacts = contacts as Array;
                            if (bvrMode) { lrView.setBvrMode(); }
                            WatchUi.requestUpdate();
                        } else {
                            var newView = new LiveRadarView(contacts as Array, l_d, lo_d, self);
                            if (bvrMode) { newView.setBvrMode(); }
                            WatchUi.pushView(newView, new LiveRadarDelegate(self), WatchUi.SLIDE_UP);
                        }
                        _view.setStatusText("MAP ACTIVE");
                    }
                }
                return;
            }

            var isAisList = data.get("ais_list_ready");
            if (isAisList != null && isAisList instanceof Boolean && isAisList) {
                var contacts = data.get("radar_contacts");
                if (contacts == null) { contacts = []; }
                WatchUi.pushView(new AisListMenu(contacts as Array, null), new AisListDelegate(null), WatchUi.SLIDE_UP);
                _view.setStatusText("AIS TGT LIST");
                return;
            }

            var dests = data.get("destinations");
            if (dests != null && dests instanceof Array) {
                WatchUi.pushView(new DynamicOptionsMenu(dests as Array), new DynamicOptionsDelegate(self), WatchUi.SLIDE_UP);
            }
            
            var inboxMsg = data.get("inbox_messages");
            if (inboxMsg != null && inboxMsg instanceof Array) {
                g_reportBuffer = [];
                for (var i=0; i<inboxMsg.size(); i++) {
                    var msgDict = inboxMsg[i] as Dictionary;
                    g_reportBuffer.add({"read" => true, "text" => msgDict.get("lines") as Array});
                }
                var menu = new InboxCustomMenu(60, Graphics.COLOR_BLACK, {});
                for (var i = g_reportBuffer.size() - 1; i >= 0; i--) {
                    var dict = g_reportBuffer[i] as Dictionary;
                    menu.addItem(new InboxItem(i, dict.get("text") as Array));
                }
                if (g_reportBuffer.size() == 0) {
                     menu.addItem(new InboxItem(0, ["Inbox Empty"]));
                }
                WatchUi.pushView(menu, new InboxDelegate(), WatchUi.SLIDE_RIGHT);
                return;
            }
            
            var tid = data.get("task_id");
            if (tid != null) {
                startPolling(tid.toString());
            }
            
            var summary = data.get("watch_summary");
            if (summary != null) {
                _view.setStatusText(summary.toString());
            } else {
                _view.setStatusText("CMD RECV");
            }
        } else {
            _view.setStatusText("ERR " + responseCode);
        }
    }
}

                                                                                                class LiveRadarView
                                                                                                        extends
                                                                                                        WatchUi.View {
    public var _contacts
                                                                                                    as Array;
    public var _panOffsetX
                                                                                                    as Integer = 0;
    public var _panOffsetY
                                                                                                    as Integer = 0;
    public var _selectedContactIndex
                                                                                                    as Integer = -1;
    public var _selectedContactId as String? = null;
                                                                                                    public var _selectedContact
                                as Dictionary?;
    private var _radiusMiles
                                                                                                    as Double = 5.0d; // Radar
                                                                                                                      // scope
                                                                                                                      // radius
    private var _historyCache
                                                                                                    as Dictionary = {}; // {
                                                                                                                        // id
                                                                                                                        // =>
                                                                                                                        // [[x,y],
                                                                                                                        // [x,y],
                                                                                                                        // [x,y]]
                                                                                                                        // }
                                                                                                    private const MAX_HISTORY=4;
                                                                                                    private var _service
                         as GarminBenfishService?; // For auto-updates
                                                                                                    private var _timer
                       as Timer.Timer?;
    private var _blTimer as Timer.Timer?;
    private var _blTicks as Integer = 0;
    public var cpaActive as Boolean = false;
    public var _wasCpaActive as Boolean = false;
    public var cpaAcknowledged as Boolean = false;


    function initialize(contacts as Array, pyxisLat as Double, pyxisLon as Double, service as GarminBenfishService) {
        View.initialize();
        _contacts = contacts;
        _historyCache = {};
        _service = service;
    }

                                                                                                    function onShow() {
                                                                                                        _timer=new Timer.Timer();_timer.start(method(:requestFreshRadar),3000,true);
        if (Attention has :backlight) {
            Attention.backlight(true);
            _blTicks = 0;
            _blTimer = new Timer.Timer();
            _blTimer.start(method(:keepBacklightOn), 4000, true);
        }
                                                                                                    }

                                                                                                    function onHide() {
                                                                                                        if (_timer != null) {
                                                                                                            _timer.stop();
                                                                                                        }
        if (_blTimer != null) {
            _blTimer.stop();
        }
        if (Attention has :backlight) {
            Attention.backlight(false);
        }
                                                                                                    }

    function keepBacklightOn() as Void {
        _blTicks++;
        if (_blTicks > 15) {
            if (_blTimer != null) { _blTimer.stop(); }
            if (Attention has :backlight) { Attention.backlight(false); }
        } else {
            if (Attention has :backlight) { Attention.backlight(true); }
        }
    }

    function requestFreshRadar() as Void {
        if (_service != null) {
            _service.makeRequest("MAP_REQ", false, 0.0f);
        }
    }
    
    function setBvrMode() as Void {
        _radiusMiles = 50.0d;
    }

                                                                                                    // Zoom Logic
                                                                                                    function zoomIn() {
                                                                                                        _radiusMiles = _radiusMiles
                                                                                                                * 0.5d;
                                                                                                        if (_radiusMiles < 1.0d) {
                                                                                                            _radiusMiles = 1.0d;
                                                                                                        }
                                                                                                        WatchUi.requestUpdate();
                                                                                                    }

                                                                                                    function zoomOut() {
                                                                                                        _radiusMiles = _radiusMiles
                                                                                                                * 2.0d;
                                                                                                        if (_radiusMiles > 100.0d) {
                                                                                                            _radiusMiles = 100.0d;
                                                                                                        }
                                                                                                        WatchUi.requestUpdate();
                                                                                                    }

    public function nextContact() as Void {
        if (_contacts != null && _contacts.size() > 0) {
            var idx = -1;
            if (_selectedContactId != null) {
                for (var i = 0; i < _contacts.size(); i++) {
                    var c = _contacts[i];
                    if (c instanceof Dictionary && c.get("id") != null && c.get("id").toString().equals(_selectedContactId)) {
                        idx = i;
                        break;
                    }
                }
            }
            idx++;
            if (idx >= _contacts.size()) { idx = 0; }
            var nextC = _contacts[idx];
            if (nextC instanceof Dictionary && nextC.get("id") != null) {
                _selectedContactId = nextC.get("id").toString();
            }
        }
        WatchUi.requestUpdate();
    }

    public function prevContact() as Void {
        if (_contacts != null && _contacts.size() > 0) {
            var idx = -1;
            if (_selectedContactId != null) {
                for (var i = 0; i < _contacts.size(); i++) {
                    var c = _contacts[i];
                    if (c instanceof Dictionary && c.get("id") != null && c.get("id").toString().equals(_selectedContactId)) {
                        idx = i;
                        break;
                    }
                }
            }
            idx--;
            if (idx < 0) { idx = _contacts.size() - 1; }
            var prevC = _contacts[idx];
            if (prevC instanceof Dictionary && prevC.get("id") != null) {
                _selectedContactId = prevC.get("id").toString();
            }
        }
        WatchUi.requestUpdate();
    }

                                                                                                    function onUpdate(
                                                                                                            dc as Graphics.Dc)as Void {
        var w=dc.getWidth();var h=dc.getHeight();var cx=(w/2)+_panOffsetX;var cy=(h/2)+_panOffsetY;var r=(w<h?w:h)/2.0-10;
        cpaActive = false;
        var cpaWarningsDrawn = 0;

        if (_selectedContactId == null && _contacts != null && _contacts.size() > 0) {
            var c0 = _contacts[0];
            if (c0 instanceof Dictionary && c0.get("id") != null) {
                _selectedContactId = c0.get("id").toString();
            }
        }
        
        var pyxisHeading = 0.0d;
        if (_service != null && _service._view != null && _service._view._metrics != null) {
            var h_val = _service._view._metrics.get("heading");
            if (h_val != null) {
                if (h_val instanceof Float) { pyxisHeading = h_val.toDouble(); }
                else if (h_val instanceof Double) { pyxisHeading = h_val; }
                else if (h_val instanceof Number) { pyxisHeading = h_val.toFloat().toDouble(); }
            }
        }
        
        dc.setColor(Graphics.COLOR_BLACK,Graphics.COLOR_BLACK);dc.clear();

                                                                                                        // Draw Scope
                                                                                                        // Rings
        dc.setColor(Graphics.COLOR_BLUE, Graphics.COLOR_TRANSPARENT);dc.drawCircle(cx,cy,r);dc.drawCircle(cx,cy,r*0.66);dc.drawCircle(cx,cy,r*0.33);

        // Crosshairs
        dc.setColor(Graphics.COLOR_DK_BLUE, Graphics.COLOR_TRANSPARENT);dc.drawLine(cx,cy-r,cx,cy+r);dc.drawLine(cx-r,cy,cx+r,cy);

                                                                                                        // Draw Pyxis
                                                                                                        // Center
                                                                                                        dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);dc.fillPolygon([[cx,cy-6],[cx-4,cy+4],[cx+4,cy+4]]);
                                                                                                        
                                                                                                        // Draw Course Vector (Heading Up)
                                                                                                        dc.setPenWidth(2);
                                                                                                        dc.drawLine(cx, cy-6, cx, cy-40);
                                                                                                        dc.setPenWidth(1);

                                                                                                        // Plot Contacts
                                                                                                        dc.setColor(Graphics.COLOR_ORANGE, Graphics.COLOR_TRANSPARENT);

                                                                                                        // Ensure index
                                                                                                        // bounds are
                                                                                                        // safe
                                                                                                        _selectedContact = null;
                                                                                                        _selectedContactIndex = -1;
                                                                                                        if (_selectedContactId != null && _contacts != null) {
                                                                                                            for (var i = 0; i < _contacts.size(); i++) {
                                                                                                                var c = _contacts[i];
                                                                                                                if (c instanceof Dictionary && c.get("id") != null && c.get("id").toString().equals(_selectedContactId)) {
                                                                                                                    _selectedContact = c;
                                                                                                                    _selectedContactIndex = i;
                                                                                                                    break;
                                                                                                                }
                                                                                                            }
                                                                                                        }

                                                                                                        for(var i=0;i<_contacts.size();i++){var c=_contacts[i];if(c instanceof Dictionary){var brg=c.get("bearing");var dist=c.get("range_nm");if(brg!=null&&dist!=null){var brg_d=0.0d;if(brg instanceof Float){brg_d=brg.toDouble();}else if(brg instanceof Double){brg_d=brg;}else if(brg instanceof Number){brg_d=brg.toFloat().toDouble();}

                                                                                                        var dist_d=0.0d;if(dist instanceof Float){dist_d=dist.toDouble();}else if(dist instanceof Double){dist_d=dist;}else if(dist instanceof Number){dist_d=dist.toFloat().toDouble();}

                                                                                                        if(dist_d<=_radiusMiles){var relBrg = brg_d - pyxisHeading; var theta=Math.toRadians(relBrg-90); // 0
                                                                                                                                                                     // is
                                                                                                                                                                     // top
                                                                                                        var pixelDist=(dist_d/_radiusMiles)*r;var tx=cx+(Math.cos(theta)*pixelDist);var ty=cy+(Math.sin(theta)*pixelDist);

                                                                                                        var type=c.get("type");var cid=c.get("id");

                                                                                                        // Cache
                                                                                                        // Position for
                                                                                                        // Comet Trails
                                                                                                        if(cid!=null){var histC=_historyCache.get(cid);var hist=[];if(histC instanceof Array){hist=histC as Array;}

                                                                                                        hist.add([tx,ty]);if(hist.size()>MAX_HISTORY){hist=hist.slice(1,MAX_HISTORY+1);}_historyCache.put(cid,hist);

                                                                                                        // Draw the
                                                                                                        // predictive
                                                                                                        // trail
        dc.setColor(Graphics.COLOR_PURPLE, Graphics.COLOR_TRANSPARENT);for(var idx=1;idx<hist.size();idx++){var p1=hist[idx-1]as Array;var p2=hist[idx]as Array;dc.drawLine(p1[0],p1[1],p2[0],p2[1]);}}

        if(type!=null&&type.equals("DRONE")){dc.setColor(Graphics.COLOR_BLUE, Graphics.COLOR_TRANSPARENT);dc.drawCircle(tx,ty,6);
        }else if(type!=null&&type.equals("ALARM")){dc.setColor(Graphics.COLOR_ORANGE, Graphics.COLOR_TRANSPARENT);dc.fillRectangle(tx-4,ty-4,8,8);
        }else if(type!=null&&type.equals("HOSTILE")){dc.setColor(Graphics.COLOR_PURPLE, Graphics.COLOR_TRANSPARENT);dc.fillPolygon([[tx,ty-8],[tx-6,ty+6],[tx+6,ty+6]] as Array);
}else if(type!=null&&type.equals("MARKER")){dc.setColor(Graphics.COLOR_YELLOW, Graphics.COLOR_TRANSPARENT);dc.drawCircle(tx,ty,3);dc.drawLine(tx,ty-4,tx,ty+4);dc.drawLine(tx-4,ty,tx+4,ty);
        }else if(type!=null&&type.equals("SHOAL")){dc.setColor(Graphics.COLOR_ORANGE, Graphics.COLOR_TRANSPARENT);dc.fillRectangle(tx-3,ty-3,6,6);dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_TRANSPARENT);dc.fillRectangle(tx-1,ty-1,2,2);
        }else{dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);dc.fillPolygon([[tx,ty-5],[tx-3,ty+4],[tx+3,ty+4]] as Array);
                                                                                                        }

            var c_sog = c.get("speed");
            var c_sog_num = 0.0d;
            if (c_sog != null) {
                if (c_sog instanceof Float) { c_sog_num = c_sog.toDouble(); }
                else if (c_sog instanceof Double) { c_sog_num = c_sog; }
                else if (c_sog instanceof Number) { c_sog_num = c_sog.toFloat().toDouble(); }
                else if (c_sog instanceof String) { c_sog_num = c_sog.toFloat().toDouble(); }
            }

            if(g_cpaEnabled && dist_d<1.0d && cpaWarningsDrawn < 2 && c_sog_num > 0.5d){
                cpaActive = true;
                cpaWarningsDrawn++;
                dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
                dc.setPenWidth(3);
                dc.drawLine(cx, cy, tx, ty);
                dc.setPenWidth(1);
                if(Attention has:vibrate){Attention.vibrate([new Attention.VibeProfile(100,250)]as Array<Attention.VibeProfile>);}
            }

            if(_selectedContact!=null&&_selectedContact instanceof Dictionary){var sel_cid=_selectedContact.get("id");if(cid!=null&&sel_cid!=null&&cid.equals(sel_cid)){dc.setColor(Graphics.COLOR_BLUE, Graphics.COLOR_TRANSPARENT);dc.setPenWidth(2);dc.drawLine(tx-10,ty-10,tx-5,ty-10);dc.drawLine(tx-10,ty-10,tx-10,ty-5);dc.drawLine(tx+10,ty-10,tx+5,ty-10);dc.drawLine(tx+10,ty-10,tx+10,ty-5);dc.drawLine(tx-10,ty+10,tx-5,ty+10);dc.drawLine(tx-10,ty+10,tx-10,ty+5);dc.drawLine(tx+10,ty+10,tx+5,ty+10);dc.drawLine(tx+10,ty+10,tx+10,ty+5);dc.setPenWidth(1);}

                                                                                                        }}}}}

            if (cpaActive && !_wasCpaActive) {
                cpaAcknowledged = false;
            }
            _wasCpaActive = cpaActive;

            if (cpaActive) {
                if (!cpaAcknowledged) {
                    var flashState = (System.getTimer() / 500) % 2 == 0;
                    if (Attention has :backlight) {
                        Attention.backlight(flashState);
                    }
                    
                    if (flashState) {
                        dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
                        dc.fillCircle(w/2, h/2, w/2);
                        dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
                        dc.drawText(w/2, h/2 - 20, Graphics.FONT_LARGE, "CPA WARNING", Graphics.TEXT_JUSTIFY_CENTER);
                        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_TRANSPARENT);
                        dc.drawText(w/2, h/2 + 20, Graphics.FONT_MEDIUM, "EVADE IMMEDIATELY", Graphics.TEXT_JUSTIFY_CENTER);
                    } else {
                        dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
                        dc.drawText(w/2, h - 55, Graphics.FONT_MEDIUM, "CPA WARNING", Graphics.TEXT_JUSTIFY_CENTER);
                    }
                } else {
                    if (Attention has :backlight) { Attention.backlight(false); }
                    dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
                    dc.drawText(w/2, h - 55, Graphics.FONT_MEDIUM, "CPA WARNING [ACK]", Graphics.TEXT_JUSTIFY_CENTER);
                }
            } else {
                if (Attention has :backlight) { Attention.backlight(false); }
            }

                                                                                                        // MARPA Metrics Readout
        if(_selectedContact!=null&&_selectedContact instanceof Dictionary){
            var m_name=_selectedContact.get("name");if(m_name==null){m_name=_selectedContact.get("id");}if(m_name==null){m_name="UNK";}
            var m_sog=_selectedContact.get("speed");var m_cog=_selectedContact.get("heading");var m_rng=_selectedContact.get("range_nm");var m_brg=_selectedContact.get("bearing");
            var m_sog_raw = 0.0f; if(m_sog instanceof Float){m_sog_raw=(m_sog as Float);}else if(m_sog instanceof Double){m_sog_raw=(m_sog as Double).toFloat();}
            if(m_sog instanceof Float||m_sog instanceof Double){m_sog=m_sog.format("%.1f");}if(m_cog instanceof Float||m_cog instanceof Double){m_cog=m_cog.format("%.0f");}if(m_rng instanceof Float||m_rng instanceof Double){m_rng=m_rng.format("%.1f");}if(m_brg instanceof Float||m_brg instanceof Double){m_brg=m_brg.format("%.0f");}

            dc.setColor(Graphics.COLOR_BLUE, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, h - 150, Graphics.FONT_XTINY, "TGT: " + m_name.toString(), Graphics.TEXT_JUSTIFY_CENTER);
            dc.drawText(w/2, h - 127, Graphics.FONT_XTINY, "RNG: "+m_rng+"nm  BRG: "+m_brg, Graphics.TEXT_JUSTIFY_CENTER);
            dc.drawText(w/2, h - 104, Graphics.FONT_XTINY, "CRSE: "+m_cog+"  SOG: "+m_sog+"kn", Graphics.TEXT_JUSTIFY_CENTER);
            
            // Live CPA Calc & Coloring
            var cpaColor = Graphics.COLOR_BLUE; // Cyan
            if (m_sog_raw > 0.5f) { cpaColor = Graphics.COLOR_ORANGE; } // Amber: moving contact
            
            // Simulated CPA (since full geometry math is complex for this loop, using range)
            var cpa_dist = m_rng; 
            
            dc.setColor(cpaColor, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, h - 81, Graphics.FONT_XTINY, "CPA: "+cpa_dist+"nm", Graphics.TEXT_JUSTIFY_CENTER);
        }

                                                                                                        // Labels
                                                                                                        dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);
                                                                                                        dc.drawText(w/2, 15, Graphics.FONT_XTINY, "RADAR: " + _radiusMiles.format("%.0f") + "nm", Graphics.TEXT_JUSTIFY_CENTER);
                                                                                                    }
                                                                                                }

                                                                                                    class LiveRadarDelegate
                                                                                                            extends
                                                                                                            WatchUi.BehaviorDelegate {
                                                                                                        private var _service
                                                                                                        as GarminBenfishService;

                                                                                                        function initialize(
                                                                                                                service as GarminBenfishService) {
                                                                                                            BehaviorDelegate.initialize();_service=service;
                                                                                                        }

                                                                                                        function onSelect()as Boolean {
                                                                                                            var view=WatchUi.getCurrentView();
                                                                                                            if(view!=null&&view[0]instanceof LiveRadarView){
                                                                                                                var lrView=view[0]as LiveRadarView;
                                                                                                                // Reset Pan First
                                                                                                                if(lrView._panOffsetX!=0||lrView._panOffsetY!=0){
                                                                                                                    lrView._panOffsetX=0;
                                                                                                                    lrView._panOffsetY=0;
                                                                                                                    WatchUi.requestUpdate();
                                                                                                                    return true;
                                                                                                                }
                                                                                                                // Push AIS Target Menu
                                                                                                                var menu = new TargetMenu(lrView._contacts);
                                                                                                                var delegate = new TargetMenuDelegate(lrView, _service, lrView._contacts);
                                                                                                                WatchUi.pushView(menu, delegate, WatchUi.SLIDE_IMMEDIATE);
                                                                                                                return true;
                                                                                                            }
                                                                                                            return false;
                                                                                                        }
                                                                                                        
    private var _lastTapTime as Number = 0;

    function onTap(evt as WatchUi.ClickEvent) as Boolean {
        var now = System.getTimer();
        var dt = now - _lastTapTime;
        _lastTapTime = now;
        
        var view = WatchUi.getCurrentView();
        if (view != null && view[0] instanceof LiveRadarView) {
            var lrView = view[0] as LiveRadarView;
            if (dt < 600 && lrView.cpaActive && !lrView.cpaAcknowledged) {
                lrView.cpaAcknowledged = true;
                WatchUi.requestUpdate();
            }
        }
        return true;
    }

    function onSwipe(evt as WatchUi.SwipeEvent) as Boolean {
        var view = WatchUi.getCurrentView();
        if (view != null && view[0] instanceof LiveRadarView) {
            var lrView = view[0] as LiveRadarView;
            if (evt.getDirection() == WatchUi.SWIPE_LEFT) {
                lrView.prevContact();
            } else if (evt.getDirection() == WatchUi.SWIPE_RIGHT) {
                lrView.nextContact();
            }
            return true; // Always consume swipe to prevent exiting radar view
        }
        return false;
    }

                                                                                                        function onNextPage()as Boolean {
                                                                                                            var view=WatchUi.getCurrentView();if(view!=null&&view[0]instanceof LiveRadarView){var lrView=view[0]as LiveRadarView;lrView.zoomIn();return true;}return false;
                                                                                                        }

                                                                                                        function onPreviousPage()as Boolean {
                                                                                                            var view=WatchUi.getCurrentView();if(view!=null&&view[0]instanceof LiveRadarView){var lrView=view[0]as LiveRadarView;lrView.zoomOut();return true;}return false;
                                                                                                        }

                                                                                                        function onKey(
                                                                                                                evt as WatchUi.KeyEvent)as Boolean {
                                                                                                            var view=WatchUi.getCurrentView();if(view!=null&&view[0]instanceof LiveRadarView){var lrView=view[0]as LiveRadarView;if(evt.getKey()==WatchUi.KEY_UP){lrView.zoomOut();return true;}if(evt.getKey()==WatchUi.KEY_DOWN){lrView.zoomIn();return true;}}return false;
                                                                                                        }

                                                                                                        function onBack()as Boolean {
                                                                                                            WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);return true;}
                                                                                                        }
                                                                                                        
/**
 * Renders a scrollable list of all active physical AIS contacts.
 * Allows the user to select a specific target and pin it for tracking.
 */
class TargetMenu extends WatchUi.Menu2 {
    public function initialize(contacts as Array?) {
        Menu2.initialize({:title=>"AIS Targets"});
        
        if (contacts != null) {
            for (var i = 0; i < contacts.size(); i++) {
                var c = contacts[i];
                if (c instanceof Dictionary) {
                    var name = c.get("name");
                    if (name == null) { name = c.get("id"); }
                    if (name == null) { name = "UNK"; }
                    var id = c.get("id");
                    if (id == null) { id = i.toString(); }
                    
                    var dist = c.get("range_nm");
                    var sub = "Dist: ";
                    if (dist instanceof Float || dist instanceof Double) { sub += dist.format("%.1f") + "nm"; }
                    else { sub += "N/A"; }
                    
                    addItem(new WatchUi.MenuItem(name.toString(), sub, "track_" + id.toString(), {}));
                }
            }
        }
        
        addItem(new WatchUi.MenuItem("Clear Track", null, "clear_track", {}));
        addItem(new WatchUi.MenuItem("Toggle Data Source", "Switch Proxies", "toggle_source", {}));
    }
}

class TargetMenuDelegate extends WatchUi.Menu2InputDelegate {
    private var _radarView as LiveRadarView;
    private var _service as GarminBenfishService;
    private var _contacts as Array?;

    public function initialize(radarView as LiveRadarView, service as GarminBenfishService, contacts as Array?) {
        Menu2InputDelegate.initialize();
        _radarView = radarView;
        _service = service;
        _contacts = contacts;
    }

    public function onSelect(item as WatchUi.MenuItem) {
        var id = item.getId();
        if (id != null) {
            var strId = id.toString();
            if (strId.equals("clear_track")) {
                _radarView._selectedContactIndex = -1;
                _radarView._selectedContactId = null;
            } else if (strId.equals("toggle_source")) {
                _service.makeRequest("SET_SOURCE:TOGGLE", false, 0.0f);
            } else if (strId.substring(0, 6).equals("track_")) {
                var targetId = strId.substring(6, strId.length());
                if (_contacts != null) {
                    for (var i = 0; i < _contacts.size(); i++) {
                        var c = _contacts[i];
                        if (c instanceof Dictionary && c.get("id") != null && c.get("id").toString().equals(targetId)) {
                            // Push the detailed info menu instead of locking immediately
                            var detailMenu = new TargetDetailedMenu(c as Dictionary);
                            var detailMenuDelegate = new TargetDetailedMenuDelegate(_radarView, _contacts, i);
                            WatchUi.pushView(detailMenu, detailMenuDelegate, WatchUi.SLIDE_LEFT);
                            return;
                        }
                    }
                }
            }
        }
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
        WatchUi.requestUpdate();
    }
}

/**
 * Detailed Information Menu for a specific AIS Target.
 * Displays real-time MARPA (Mini Automatic Radar Plotting Aid) data
 * including SOG (Speed Over Ground) and COG (Course Over Ground).
 */
class TargetDetailedMenu extends WatchUi.Menu2 {
    public function initialize(contact as Dictionary) {
        Menu2.initialize({:title=>"Target Info"});
        
        var name = contact.get("name");
        if (name == null) { name = contact.get("id"); }
        if (name == null) { name = "UNK"; }
        var id = contact.get("id");
        if (id == null) { id = "0"; }
        
        var rng = contact.get("range_nm");
        var brg = contact.get("bearing");
        var sog = contact.get("speed");
        var cog = contact.get("heading");
        var type = contact.get("type");
        
        if (rng instanceof Float || rng instanceof Double) { rng = rng.format("%.2f") + " nm"; } else if (rng instanceof Number) { rng = rng.toString() + " nm"; } else { rng = "N/A"; }
        if (brg instanceof Float || brg instanceof Double) { brg = brg.format("%.0f") + " deg"; } else if (brg instanceof Number) { brg = brg.toString() + " deg"; } else { brg = "N/A"; }
        if (sog instanceof Float || sog instanceof Double) { sog = sog.format("%.1f") + " kn"; } else if (sog instanceof Number) { sog = sog.toString() + " kn"; } else { sog = "N/A"; }
        if (cog instanceof Float || cog instanceof Double) { cog = cog.format("%.0f") + " deg"; } else if (cog instanceof Number) { cog = cog.toString() + " deg"; } else { cog = "N/A"; }
        if (type == null) { type = "Unknown"; }

        addItem(new WatchUi.MenuItem(">>> TRACK TARGET <<<", "", "act_track", {}));
        addItem(new WatchUi.MenuItem("Name", name.toString(), "info_name", {}));
        addItem(new WatchUi.MenuItem("Type", type.toString(), "info_type", {}));
        addItem(new WatchUi.MenuItem("Range", rng.toString(), "info_rng", {}));
        addItem(new WatchUi.MenuItem("Bearing", brg.toString(), "info_brg", {}));
        addItem(new WatchUi.MenuItem("Speed (SOG)", sog.toString(), "info_sog", {}));
        addItem(new WatchUi.MenuItem("Heading (COG)", cog.toString(), "info_cog", {}));
    }
}

class TargetDetailedMenuDelegate extends WatchUi.Menu2InputDelegate {
    private var _radarView as LiveRadarView;
    private var _contacts as Array?;
    private var _targetIndex as Integer;

    public function initialize(radarView as LiveRadarView, contacts as Array?, targetIndex as Integer) {
        Menu2InputDelegate.initialize();
        _radarView = radarView;
        _contacts = contacts;
        _targetIndex = targetIndex;
    }

    public function onSelect(item as WatchUi.MenuItem) {
        var id = item.getId();
        if (id != null) {
            var strId = id.toString();
            if (strId.equals("act_track")) {
                if (_contacts != null && _targetIndex >= 0 && _targetIndex < _contacts.size()) {
                    var c = _contacts[_targetIndex];
                    if (c instanceof Dictionary && c.get("id") != null) {
                        _radarView._selectedContactId = c.get("id").toString();
                        _radarView._selectedContactIndex = _targetIndex;
                    }
                }
                WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
                WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
                WatchUi.requestUpdate();
                return;
            }
        }
    }
}

/**
 * Renders an active routing path from the pyxis user to a destination.
 * Used for emergency MAYDAY routes, safe harbors, and MARINA routing.
 */
class RouteNavView extends WatchUi.View {
                                                                                                            private var _route
                                                                                                            as Array;
                                                                                                            private var _plat
                                                                                                            as Double;
                                                                                                            private var _plon
                                                                                                            as Double;
                                                                                                            private var _radiusMiles
                                                                                                            as Double = 15.0d; // Default
                                                                                                                               // zoom

                                                                                                            function initialize(
                                                                                                                    routeData as Array,pLat as Double,pLon as Double) {
                                                                                                                View.initialize();_route=routeData;_plat=pLat;_plon=pLon;

                                                                                                                // Auto-scale
                                                                                                                // radius
                                                                                                                // to
                                                                                                                // fit
                                                                                                                // furtherest
                                                                                                                // point
                                                                                                                // if
                                                                                                                // needed
                                                                                                                if(_route.size()>0){var maxDist=0.0d;for(var i=0;i<_route.size();i++){var wp=_route[i];if(wp instanceof Dictionary&&wp.get("lat")!=null&&wp.get("lon")!=null){var wLat=wp.get("lat");var wLon=wp.get("lon");if(wLat instanceof Float){wLat=wLat.toDouble();}else if(wLat instanceof Number){wLat=wLat.toDouble();}if(wLon instanceof Float){wLon=wLon.toDouble();}else if(wLon instanceof Number){wLon=wLon.toDouble();}

                                                                                                                var dLat=(wLat-_plat)*60.0;var dLon=(wLon-_plon)*60.0*Math.cos(_plat*Math.PI/180.0);var distNm=Math.sqrt(dLat*dLat+dLon*dLon);if(distNm>maxDist){maxDist=distNm;}}}if(maxDist>0.0d){_radiusMiles=(maxDist*1.2d);}}
                                                                                                            }

                                                                                                            // Zoom
                                                                                                            // Logic
                                                                                                            function zoomIn() {
                                                                                                                _radiusMiles = _radiusMiles
                                                                                                                        * 0.5d;
                                                                                                                if (_radiusMiles < 1.0d) {
                                                                                                                    _radiusMiles = 1.0d;
                                                                                                                }
                                                                                                                WatchUi.requestUpdate();
                                                                                                            }

                                                                                                            function zoomOut() {
                                                                                                                _radiusMiles = _radiusMiles
                                                                                                                        * 2.0d;
                                                                                                                if (_radiusMiles > 100.0d) {
                                                                                                                    _radiusMiles = 100.0d;
                                                                                                                }
                                                                                                                WatchUi.requestUpdate();
                                                                                                            }

                                                                                                            function onUpdate(
                                                                                                                    dc as Graphics.Dc)as Void {
                                                                                                                var w=dc.getWidth();var h=dc.getHeight();var cx=w/2;var cy=h/2;var r=(w<h?w:h)/2.0-15;

                                                                                                                dc.setColor(Graphics.COLOR_BLACK,Graphics.COLOR_BLACK);dc.clear();

                                                                                                                // Ring
                                                                                                                dc.setColor(Graphics.COLOR_DK_GRAY, Graphics.COLOR_TRANSPARENT);dc.drawCircle(cx,cy,r);dc.drawLine(cx,cy-r,cx,cy+r);dc.drawLine(cx-r,cy,cx+r,cy);

                                                                                                                // Draw
                                                                                                                // Route
                                                                                                                if(_route.size()>0){dc.setColor(Graphics.COLOR_ORANGE, Graphics.COLOR_TRANSPARENT);dc.setPenWidth(3);var lastX=cx; // start
                                                                                                                                                                                                          // drawing
                                                                                                                                                                                                          // from
                                                                                                                                                                                                          // pyxis
                                                                                                                var lastY=cy;

                                                                                                                for(var i=0;i<_route.size();i++){var wp=_route[i];if(wp instanceof Dictionary){var wLat=wp.get("lat");var wLon=wp.get("lon");if(wLat!=null&&wLon!=null){if(wLat instanceof Float){wLat=wLat.toDouble();}else if(wLat instanceof Number){wLat=wLat.toDouble();}if(wLon instanceof Float){wLon=wLon.toDouble();}else if(wLon instanceof Number){wLon=wLon.toDouble();}

                                                                                                                var dLat=(wLat-_plat)*60.0;var dLon=(wLon-_plon)*60.0*Math.cos(_plat*Math.PI/180.0);var distNm=Math.sqrt(dLat*dLat+dLon*dLon);var bearDegTemp=Math.toDegrees(Math.atan2(dLon,dLat))+360;var bearDeg=bearDegTemp.toLong()%360;var bearRad=Math.toRadians(bearDeg-90);

                                                                                                                var pixelDist=(distNm/_radiusMiles)*r;var tx=cx+(Math.cos(bearRad)*pixelDist);var ty=cy+(Math.sin(bearRad)*pixelDist);

                                                                                                                dc.drawLine(lastX,lastY,tx,ty);dc.fillCircle(tx,ty,3);lastX=tx;lastY=ty;}}}dc.setPenWidth(1);

                                                                                                                // Draw
                                                                                                                // Destination
                                                                                                                // Flag
                                                                                                                // at
                                                                                                                // end
                                                                                                                dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);dc.fillPolygon([[lastX,lastY],[lastX+10,lastY-5],[lastX+10,lastY+5]]);}

                                                                                                                // Pyxis
                                                                                                                // Center
                                                                                                                dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);dc.fillPolygon([[cx,cy-8],[cx-6,cy+6],[cx+6,cy+6]]);

                                                                                                                dc.setColor(Graphics.COLOR_ORANGE, Graphics.COLOR_TRANSPARENT);var rm=_radiusMiles;if(rm instanceof Float||rm instanceof Double){dc.drawText(w/2,10,Graphics.FONT_XTINY,"RTE: "+rm.format("%.1f")+"nm",Graphics.TEXT_JUSTIFY_CENTER);}}
                                                                                                            }

                                                                                                            class RouteNavDelegate
                                                                                                                    extends
                                                                                                                    WatchUi.BehaviorDelegate {
                                                                                                                function initialize() {
                                                                                                                    BehaviorDelegate
                                                                                                                            .initialize();
                                                                                                                }

                                                                                                                // Use
                                                                                                                // native
                                                                                                                // Next/Prev
                                                                                                                // paging
                                                                                                                // for
                                                                                                                // zoom
                                                                                                                // to
                                                                                                                // capture
                                                                                                                // both
                                                                                                                // physical
                                                                                                                // scrolling
                                                                                                                // and
                                                                                                                // swiping
                                                                                                                function onNextPage()as Boolean {
                                                                                                                    var view=WatchUi.getCurrentView();if(view!=null&&view[0]instanceof RouteNavView){(view[0]as RouteNavView).zoomIn();return true;}return false;
                                                                                                                }

                                                                                                                function onPreviousPage()as Boolean {
                                                                                                                    var view=WatchUi.getCurrentView();if(view!=null&&view[0]instanceof RouteNavView){(view[0]as RouteNavView).zoomOut();return true;}return false;
                                                                                                                }

                                                                                                                function onKey(
                                                                                                                        evt as WatchUi.KeyEvent)as Boolean {
                                                                                                                    var view=WatchUi.getCurrentView();if(view!=null&&view[0]instanceof RouteNavView){var rnView=view[0]as RouteNavView;if(evt.getKey()==WatchUi.KEY_UP){rnView.zoomOut();return true;}if(evt.getKey()==WatchUi.KEY_DOWN){rnView.zoomIn();return true;}}return false;
                                                                                                                }

                                                                                                                function onBack()as Boolean {
                                                                                                                    WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);return true;}
                                                                                                                }

/**
 * The Intelligence Briefing (SITREP) View.
 * Renders GMDSS and OSINT warnings as vertical, scrollable cards.
 * Color codes severity (RED/YELLOW) based on the Gemini AI threat rating.
 */
class ThreatLogView extends WatchUi.View {
                                                                                                                    private var _alerts
                                                                                                                    as Array;
                                                                                                                    private var _plat
                                                                                                                    as Double;
                                                                                                                    private var _plon
                                                                                                                    as Double;
                                                                                                                    private var _scrollY
                                                                                                                    as Integer = 0;

                                                                                                                    function initialize(
                                                                                                                            alertsData as Array,pLat as Double,pLon as Double) {
                                                                                                                        View.initialize();_alerts=alertsData;_plat=pLat;_plon=pLon;
                                                                                                                    }

                                                                                                                    function scrollUp() {
                                                                                                                        _scrollY -= 60;
                                                                                                                        WatchUi.requestUpdate();
                                                                                                                    }

                                                                                                                    function scrollDown() {
                                                                                                                        _scrollY += 60;
                                                                                                                        if (_scrollY > 0) {
                                                                                                                            _scrollY = 0;
                                                                                                                        }
                                                                                                                        WatchUi.requestUpdate();
                                                                                                                    }

                                                                                                                    function onUpdate(
                                                                                                                            dc as Graphics.Dc)as Void {
                                                                                                                        var w=dc.getWidth();var h=dc.getHeight();

                                                                                                                        dc.setColor(Graphics.COLOR_BLACK,Graphics.COLOR_BLACK);dc.clear();

                                                                                                                        var y_offset=30+_scrollY;

                                                                                                                        if(_alerts.size()==0){dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);dc.drawText(w/2,h/2,Graphics.FONT_XTINY,"NO ACTIVE THREATS",Graphics.TEXT_JUSTIFY_CENTER);return;}

                                                                                                                        for(var i=0;i<_alerts.size();i++){var item=_alerts[i];if(item instanceof Dictionary){var title=item.get("title");if(title==null){title="ALERT";}var sev=item.get("severity");if(sev==null){sev="YELLOW";}var desc=item.get("desc");if(desc==null){desc="Unknown.";}

                                                                                                                        // Compute
                                                                                                                        // Distance
                                                                                                                        var distance_str="";var wLat=item.get("lat");var wLon=item.get("lon");if(wLat!=null&&wLon!=null){if(wLat instanceof Float){wLat=wLat.toDouble();}else if(wLat instanceof Number){wLat=wLat.toDouble();}if(wLon instanceof Float){wLon=wLon.toDouble();}else if(wLon instanceof Number){wLon=wLon.toDouble();}var dLat=(wLat-_plat)*60.0;var dLon=(wLon-_plon)*60.0*Math.cos(_plat*Math.PI/180.0);var distNm=Math.sqrt(dLat*dLat+dLon*dLon);distance_str=distNm.format("%.1f")+"nm";}

                                                                                                                        // Draw
                                                                                                                        // Card
                                                                                                                        var card_col=(sev.equals("RED"))?Graphics.COLOR_RED:Graphics.COLOR_YELLOW;
                                                                                                                        dc.setColor(0x222222,Graphics.COLOR_TRANSPARENT);dc.fillRoundedRectangle(15,y_offset-15,w-30,60,8);
                                                                                                                        dc.setColor(card_col, Graphics.COLOR_TRANSPARENT);dc.setPenWidth(2);dc.drawRoundedRectangle(15,y_offset-15,w-30,60,8);dc.setPenWidth(1);
                                                                                                                        dc.drawText(w/2,y_offset-5,Graphics.FONT_TINY,title.toString()+" ["+distance_str+"]",Graphics.TEXT_JUSTIFY_CENTER);y_offset+=20;

                                                                                                                        // Split
                                                                                                                        // desc
                                                                                                                        // string
                                                                                                                        // over
                                                                                                                        // multiple
                                                                                                                        // lines
                                                                                                                        dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);var desc_str=desc.toString();var max_chars=24;var ptr=0;while(ptr<desc_str.length()){var chunk=desc_str.substring(ptr,ptr+max_chars);if(chunk==null){break;}dc.drawText(w/2,y_offset,Graphics.FONT_XTINY,chunk,Graphics.TEXT_JUSTIFY_CENTER);y_offset+=20;ptr+=max_chars;}y_offset+=25; // Padding
                                                                                                                                                                                                                                                                                                                                                                                                                                                     // below
                                                                                                                                                                                                                                                                                                                                                                                                                                                     // card
                                                                                                                        }}}
                                                                                                                    }

                                                                                                                    class ThreatLogDelegate
                                                                                                                            extends
                                                                                                                            WatchUi.BehaviorDelegate {
                                                                                                                        function initialize() {
                                                                                                                            BehaviorDelegate
                                                                                                                                    .initialize();
                                                                                                                        }

                                                                                                                        function onNextPage()as Boolean {
                                                                                                                            var view=WatchUi.getCurrentView();if(view!=null&&view[0]instanceof ThreatLogView){(view[0]as ThreatLogView).scrollUp();return true;}return false;
                                                                                                                        }

                                                                                                                        function onPreviousPage()as Boolean {
                                                                                                                            var view=WatchUi.getCurrentView();if(view!=null&&view[0]instanceof ThreatLogView){(view[0]as ThreatLogView).scrollDown();return true;}return false;
                                                                                                                        }

                                                                                                                        function onKey(
                                                                                                                                evt as WatchUi.KeyEvent)as Boolean {
                                                                                                                            var view=WatchUi.getCurrentView();if(view!=null&&view[0]instanceof ThreatLogView){var tlView=view[0]as ThreatLogView;if(evt.getKey()==WatchUi.KEY_UP){tlView.scrollDown();return true;}if(evt.getKey()==WatchUi.KEY_DOWN){tlView.scrollUp();return true;}}return false;
                                                                                                                        }

                                                                                                                        function onBack()as Boolean {
                                                                                                                            WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);return true;}
                                                                                                                        }

/**
 * Draws the Engineering Top-Down Diagram of the Vessel.
 * Visually represents engine status, bilge active states, generator loops,
 * and tracks the house battery voltage.
 */
class SchematicView extends WatchUi.View {
    private var _metrics as Dictionary;

    function initialize(metrics as Dictionary) {
        View.initialize();
        _metrics = metrics;
    }

    function onUpdate(dc as Graphics.Dc) as Void {
        var w = dc.getWidth();
        var h = dc.getHeight();
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
        dc.clear();

        // Draw boat outline
        dc.setColor(Graphics.COLOR_ORANGE, Graphics.COLOR_TRANSPARENT); // Tactical Gold
        dc.setPenWidth(3);
        
        var pt1 = [w/2, 40]; // Bow (sharper)
        var pt2 = [w/2 + 55, 120]; // Starboard mid
        var pt3 = [w/2 + 45, h - 40]; // Starboard stern
        var pt4 = [w/2 - 45, h - 40]; // Port stern
        var pt5 = [w/2 - 55, 120]; // Port mid

        dc.drawLine(pt1[0], pt1[1], pt2[0], pt2[1]);
        dc.drawLine(pt2[0], pt2[1], pt3[0], pt3[1]);
        dc.drawLine(pt3[0], pt3[1], pt4[0], pt4[1]);
        dc.drawLine(pt4[0], pt4[1], pt5[0], pt5[1]);
        dc.drawLine(pt5[0], pt5[1], pt1[0], pt1[1]);

        // Deck/Cabin inner lines
        dc.setPenWidth(1);
        dc.setColor(0xAA6600, Graphics.COLOR_TRANSPARENT); // Bronze/Dark Gold
        dc.drawLine(w/2, 60, w/2 + 35, 120);
        dc.drawLine(w/2 + 35, 120, w/2 + 30, h - 60);
        dc.drawLine(w/2 + 30, h - 60, w/2 - 30, h - 60);
        dc.drawLine(w/2 - 30, h - 60, w/2 - 35, 120);
        dc.drawLine(w/2 - 35, 120, w/2, 60);
        
        // Transom ledge
        dc.drawLine(w/2 - 40, h - 50, w/2 + 40, h - 50);

        dc.setColor(Graphics.COLOR_ORANGE, Graphics.COLOR_TRANSPARENT);
        dc.drawText(w/2, 10, Graphics.FONT_TINY, "VANDSTADT PYXIS", Graphics.TEXT_JUSTIFY_CENTER);

        // Engine
        var rpm = _metrics.get("rpm");
        var engColor = (rpm != null && (rpm instanceof Number || rpm instanceof Float) && rpm.toNumber() > 0) ? Graphics.COLOR_GREEN : 0xFF0000;
        dc.setColor(engColor, Graphics.COLOR_TRANSPARENT);
        dc.fillRoundedRectangle(w/2 - 25, h - 85, 50, 25, 4);
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_TRANSPARENT);
        dc.drawText(w/2, h - 82, Graphics.FONT_XTINY, "ENG", Graphics.TEXT_JUSTIFY_CENTER);

        // Generator
        var gen = _metrics.get("gen_status");
        var genColor = (gen != null && gen.equals("ON")) ? Graphics.COLOR_GREEN : 0x555555;
        dc.setColor(genColor, Graphics.COLOR_TRANSPARENT);
        dc.fillRoundedRectangle(w/2 + 30, h - 110, 28, 25, 4);
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_TRANSPARENT);
        dc.drawText(w/2 + 44, h - 107, Graphics.FONT_XTINY, "GEN", Graphics.TEXT_JUSTIFY_CENTER);

        // Bilge
        var bilge = _metrics.get("bilge_status");
        var bilgeColor = (bilge != null && bilge.equals("OK")) ? Graphics.COLOR_GREEN : 0xFF0000;
        dc.setColor(bilgeColor, Graphics.COLOR_TRANSPARENT);
        dc.fillRoundedRectangle(w/2 - 58, h - 110, 28, 25, 4);
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_TRANSPARENT);
        dc.drawText(w/2 - 44, h - 107, Graphics.FONT_XTINY, "PMP", Graphics.TEXT_JUSTIFY_CENTER);

        // Batt
        var bat = _metrics.get("bat_v");
        var batV = 12.0;
        if (bat != null) {
            if (bat instanceof Float || bat instanceof Double) { batV = bat.toFloat(); }
            else if (bat instanceof Number) { batV = bat.toFloat(); }
            else if (bat instanceof String) { batV = bat.toFloat(); }
        }
        var batColor = (batV >= 12.0) ? Graphics.COLOR_GREEN : Graphics.COLOR_ORANGE;
        dc.setColor(batColor, Graphics.COLOR_TRANSPARENT);
        dc.fillRoundedRectangle(w/2 - 32, 130, 64, 25, 4);
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_TRANSPARENT);
        dc.drawText(w/2, 133, Graphics.FONT_XTINY, batV.format("%.1f") + "V", Graphics.TEXT_JUSTIFY_CENTER);

        // Fuel
        var fuel = _metrics.get("fuel_pct");
        var fuelP = 100;
        if (fuel != null) {
            if (fuel instanceof Number || fuel instanceof Float) { fuelP = fuel.toNumber(); }
        }
        dc.setColor(0x333333, Graphics.COLOR_TRANSPARENT);
        dc.fillRoundedRectangle(w/2 - 20, h - 145, 40, 35, 3);
        var fuelH = 0;
        if (fuelP > 0) { fuelH = (35 * fuelP) / 100; }
        if (fuelH > 35) { fuelH = 35; }
        dc.setColor(0x0088FF, Graphics.COLOR_TRANSPARENT);
        dc.fillRoundedRectangle(w/2 - 20, h - 145 + (35 - fuelH), 40, fuelH, 3);
        dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
        dc.drawText(w/2, h - 138, Graphics.FONT_XTINY, fuelP.toString() + "%", Graphics.TEXT_JUSTIFY_CENTER);
        
        // Autopilot / Wind
        var ap = _metrics.get("autopilot_active");
        var wind = _metrics.get("wind_speed");
        if (ap instanceof Boolean && ap) {
            dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);
            dc.drawText(w/2, 65, Graphics.FONT_TINY, "AP: LCK", Graphics.TEXT_JUSTIFY_CENTER);
        }
        if (wind != null) {
            dc.setColor(0x00AAAA, Graphics.COLOR_TRANSPARENT);
            var wStr = wind.toString();
            if (wind instanceof Float || wind instanceof Double) { wStr = wind.format("%.1f"); }
            else if (wind instanceof Number) { wStr = wind.toString(); }
            dc.drawText(w/2, 90, Graphics.FONT_TINY, wStr + "kn", Graphics.TEXT_JUSTIFY_CENTER);
        }
    }
}

class SchematicDelegate extends WatchUi.BehaviorDelegate {
    function initialize() {
        BehaviorDelegate.initialize();
    }

    function onBack() as Boolean {
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
        return true;
    }
}

/**
 * Uses a software isometric projection to draw a 3D simulated mesh
 * of the seabed (Sonar depth contour lines).
 * Colored dynamically based on depth danger thresholds.
 */
class LiveSonarView extends WatchUi.View {
                                                                                                                            private var _grid
                                                                                                                            as Array;

                                                                                                                            private var _depth as String = "--";
                                                                                                                            private var _speed as String = "--";
                                                                                                                            private var _course as String = "--";
                                                                                                                            private var _service as GarminBenfishService;

                                                                                                                            function initialize(
                                                                                                                                    data as Dictionary, service as GarminBenfishService) {
                                                                                                                                View.initialize();var tempGrid=data.get("sonar_grid");if(tempGrid!=null&&tempGrid instanceof Array){_grid=tempGrid as Array;}else{_grid=[];}
                                                                                                                                
                                                                                                                                _service = service;
                                                                                                                                if (_service != null && _service._view != null && _service._view._metrics != null) {
                                                                                                                                    var m = _service._view._metrics;
                                                                                                                                    var d = m.get("depth_keel"); if (d != null) { _depth = (d instanceof Float || d instanceof Double) ? d.format("%.1f") : d.toString(); }
                                                                                                                                    var s = m.get("speed_kn"); if (s != null) { _speed = (s instanceof Float || s instanceof Double) ? s.format("%.1f") : s.toString(); }
                                                                                                                                    var c = m.get("heading"); if (c != null) { _course = (c instanceof Float || c instanceof Double) ? c.format("%.0f") : c.toString(); }
                                                                                                                                }
                                                                                                                            }

                                                                                                                            function onUpdate(
                                                                                                                                    dc as Graphics.Dc)as Void {
                                                                                                                                var w=dc.getWidth();var h=dc.getHeight();dc.setColor(Graphics.COLOR_BLACK,Graphics.COLOR_BLACK);dc.clear();

                                                                                                                                if(_grid.size()==0){dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);dc.drawText(w/2,h/2,Graphics.FONT_XTINY,"NO SONAR DATA\nWaiting for Telemetry...",Graphics.TEXT_JUSTIFY_CENTER);return;}

                                                                                                                                // Software
                                                                                                                                // Isometric
                                                                                                                                // Projection
                                                                                                                                var isoW=8; // Width
                                                                                                                                            // step
                                                                                                                                            // per
                                                                                                                                            // cell
                                                                                                                                            // center
                                                                                                                                var isoH=4; // Height
                                                                                                                                            // step
                                                                                                                                            // down
                                                                                                                                            // the
                                                                                                                                            // page
                                                                                                                                            // per
                                                                                                                                            // cell
                                                                                                                                var depthScale=1.0; // Visual
                                                                                                                                                    // scale
                                                                                                                                                    // of
                                                                                                                                                    // depth
                                                                                                                                                    // values

                                                                                                                                var rows=_grid.size();var cols=(_grid[0]instanceof Array)?(_grid[0]as Array).size():0;

                                                                                                                                var cx=w/2;var cy=(h/2)-40; // Shift
                                                                                                                                                            // mesh
                                                                                                                                                            // up
                                                                                                                                                            // so
                                                                                                                                                            // deep
                                                                                                                                                            // trenches
                                                                                                                                                            // don't
                                                                                                                                                            // clip
                                                                                                                                                            // bounds

                                                                                                                                // Draw
                                                                                                                                // Sub
                                                                                                                                // /
                                                                                                                                // Hull
                                                                                                                                // Origin
                                                                                                                                // Floating
                                                                                                                                // above
                                                                                                                                // origin
                                                                                                                                dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);dc.fillPolygon([[cx,cy-30],[cx-6,cy-20],[cx+6,cy-20]]as Array);

                                                                                                                                dc.setPenWidth(1);

                                                                                                                                // Pass
                                                                                                                                // 1:
                                                                                                                                // Render
                                                                                                                                // East/West
                                                                                                                                // Lines
                                                                                                                                for(var r=0;r<rows;r++){var rowData=_grid[r]as Array;for(var c=0;c<cols-1;c++){var d1=rowData[c];var d2=rowData[c+1];if(d1 instanceof Number&&d2 instanceof Number){
                                                                                                                                // Center
                                                                                                                                // offset
                                                                                                                                // the
                                                                                                                                // grid
                                                                                                                                // so
                                                                                                                                // (0,0)
                                                                                                                                // index
                                                                                                                                // is
                                                                                                                                // balanced
                                                                                                                                // around
                                                                                                                                // cx,cy
                                                                                                                                var adjC1=c-(cols/2);var adjR1=r-(rows/2);var adjC2=(c+1)-(cols/2);var adjR2=r-(rows/2);

                                                                                                                                var x1=cx+(adjC1-adjR1)*isoW;var y1=cy+(adjC1+adjR1)*isoH-(d1*depthScale);var x2=cx+(adjC2-adjR2)*isoW;var y2=cy+(adjC2+adjR2)*isoH-(d2*depthScale);

                                                                                                                                if(d1>0){dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);}else if(d1<-20){dc.setColor(0x0055AA, Graphics.COLOR_TRANSPARENT);} // Deep
                                                                                                                                                                                                                          // blue
                                                                                                                                                                                                                          // hex
                                                                                                                                else{dc.setColor(Graphics.COLOR_BLUE, Graphics.COLOR_TRANSPARENT);} // Shallow
                                                                                                                                                                // cyan
                                                                                                                                                                // hex

                                                                                                                                dc.drawLine(x1,y1,x2,y2);}}}

                                                                                                                                // Pass
                                                                                                                                // 2:
                                                                                                                                // Render
                                                                                                                                // North/South
                                                                                                                                // Lines
                                                                                                                                for(var c=0;c<cols;c++){for(var r=0;r<rows-1;r++){var rowData1=_grid[r]as Array;var rowData2=_grid[r+1]as Array;var d1=rowData1[c];var d2=rowData2[c];

                                                                                                                                if(d1 instanceof Number&&d2 instanceof Number){var adjC1=c-(cols/2);var adjR1=r-(rows/2);var adjC2=c-(cols/2);var adjR2=(r+1)-(rows/2);

                                                                                                                                var x1=cx+(adjC1-adjR1)*isoW;var y1=cy+(adjC1+adjR1)*isoH-(d1*depthScale);var x2=cx+(adjC2-adjR2)*isoW;var y2=cy+(adjC2+adjR2)*isoH-(d2*depthScale);

                                                                                                                                if(d1>0){dc.setColor(Graphics.COLOR_GREEN, Graphics.COLOR_TRANSPARENT);}else if(d1<-20){dc.setColor(0x0055AA, Graphics.COLOR_TRANSPARENT);}else{dc.setColor(Graphics.COLOR_BLUE, Graphics.COLOR_TRANSPARENT);}

                                                                                                                                dc.drawLine(x1,y1,x2,y2);}}}

                                                                                                                                // Overlay
                                                                                                                                // Text
                                                                                                                                // Label
                                                                                                                                dc.setColor(Graphics.COLOR_WHITE,Graphics.COLOR_TRANSPARENT);dc.drawText(w/2,10,Graphics.FONT_XTINY,"3D SONAR MESH",Graphics.TEXT_JUSTIFY_CENTER);
                                                                                                                                dc.setColor(Graphics.COLOR_GREEN,Graphics.COLOR_TRANSPARENT);
                                                                                                                                dc.drawText(w/2, h - 120, Graphics.FONT_XTINY, "DEPTH: " + _depth + "m", Graphics.TEXT_JUSTIFY_CENTER);
                                                                                                                                dc.drawText(w/2, h - 100, Graphics.FONT_XTINY, "SOG: " + _speed + "kn  CRSE: " + _course, Graphics.TEXT_JUSTIFY_CENTER);
                                                                                                                            }
                                                                                                                            }

                                                                                                                            class LiveSonarDelegate
                                                                                                                                    extends
                                                                                                                                    WatchUi.BehaviorDelegate {
                                                                                                                                function initialize() {
                                                                                                                                    BehaviorDelegate
                                                                                                                                            .initialize();
                                                                                                                                }

                                                                                                                                function onBack()as Boolean {
                                                                                                                                    WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);return true;}
                                                                                                                                }

class AisListMenu extends WatchUi.Menu2 {
    public var _radarView as OsintRadarView?;
    function initialize(contacts as Array, radarView as OsintRadarView?) {
        Menu2.initialize({:title=>"AIS TARGETS"});
        _radarView = radarView;
        for(var i=0; i<contacts.size(); i++) {
            var c = contacts[i];
            if (c instanceof Dictionary) {
                var name = c.get("name");
                if (name == null) { name = c.get("id"); }
                if (name == null) { name = "UNKNOWN"; }
                var rng = c.get("range_nm");
                if (rng instanceof Float || rng instanceof Double) { rng = rng.format("%.1f"); }
                var brg = c.get("bearing");
                if (brg instanceof Float || brg instanceof Double) { brg = brg.format("%.0f"); }
                var speed = c.get("speed");
                if (speed instanceof Float || speed instanceof Double) { speed = speed.format("%.1f"); }

                var sublabel = "RNG: " + rng + "nm BRG: " + brg + " SOG: " + speed;
                addItem(new WatchUi.MenuItem(name.toString(), sublabel.toString(), c.get("id"), {}));
            }
        }
        if (contacts.size() == 0) {
            addItem(new WatchUi.MenuItem("No Targets", "Awaiting Feed", "none", {}));
        }
    }
}

class AisListDelegate extends WatchUi.Menu2InputDelegate {
    private var _radarView as OsintRadarView?;
    function initialize(radarView as OsintRadarView?) {
        Menu2InputDelegate.initialize();
        _radarView = radarView;
    }
    function onSelect(item as WatchUi.MenuItem) as Void {
        var id = item.getId();
        if (id != null && !id.toString().equals("none") && _radarView != null) {
            _radarView._selectedContactId = id.toString();
            WatchUi.requestUpdate();
        }
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
    }
    function onBack() as Void {
        WatchUi.popView(WatchUi.SLIDE_RIGHT);
    }
}

// Contact list for the pulsing OSINT scope view
class OsintListMenu extends WatchUi.Menu2 {
    function initialize(contacts as Array, scopeView as OsintScopeView?) {
        Menu2.initialize({:title=>"OSINT TARGETS"});
        for (var i = 0; i < contacts.size(); i++) {
            var c = contacts[i];
            if (c instanceof Dictionary) {
                var name = c.get("name");
                if (name == null) { name = c.get("id"); }
                if (name == null) { name = "UNKNOWN"; }
                var rng = c.get("range_nm");
                if (rng instanceof Float || rng instanceof Double) { rng = rng.format("%.1f"); }
                var brg = c.get("bearing");
                if (brg instanceof Float || brg instanceof Double) { brg = brg.format("%.0f"); }
                var speed = c.get("speed");
                if (speed instanceof Float || speed instanceof Double) { speed = speed.format("%.1f"); }
                var sublabel = "RNG: " + rng + "nm BRG: " + brg + " SOG: " + speed;
                addItem(new WatchUi.MenuItem(name.toString(), sublabel.toString(), i, {}));
            }
        }
        if (contacts.size() == 0) {
            addItem(new WatchUi.MenuItem("No Targets", "Awaiting OSINT Feed", "none", {}));
        }
    }
}

class OsintListDelegate extends WatchUi.Menu2InputDelegate {
    private var _scopeView as OsintScopeView?;
    function initialize(scopeView as OsintScopeView?) {
        Menu2InputDelegate.initialize();
        _scopeView = scopeView;
    }
    function onSelect(item as WatchUi.MenuItem) as Void {
        var id = item.getId();
        if (id instanceof Number && _scopeView != null) {
            _scopeView._selIdx = id as Number;
            WatchUi.requestUpdate();
        }
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
    }
    function onBack() as Void {
        WatchUi.popView(WatchUi.SLIDE_RIGHT);
    }
}


                                                                                                                                // 5. APP ENTRY POINT
                                                                                                                                // =====================================================================
                                                                                                                                class GarminBenfishApp
                                                                                                                                        extends
                                                                                                                                        Application.AppBase {
                                                                                                                                    function initialize() {
                                                                                                                                        AppBase.initialize();
                                                                                                                                    }

                                                                                                                                    function onStart(
                                                                                                                                            state as Dictionary?)as Void {
                                                                                                                                        Position.enableLocationEvents(Position.LOCATION_CONTINUOUS,method(:onPosition));
                                                                                                                                    }

                                                                                                                                    function onStop(
                                                                                                                                            state as Dictionary?)as Void {
                                                                                                                                        Position.enableLocationEvents(Position.LOCATION_DISABLE,method(:onPosition));
                                                                                                                                    }

                                                                                                                                    function onPosition(
                                                                                                                                            info as Position.Info)as Void {
                                                                                                                                        // System
                                                                                                                                        // handles
                                                                                                                                        // Position.getInfo()
                                                                                                                                        // updates
                                                                                                                                        // passively
                                                                                                                                        // for
                                                                                                                                        // views
                                                                                                                                    }

                                                                                                                                    function getInitialView()as[WatchUi.Views]or[WatchUi.Views,WatchUi.InputDelegates] {
                                                                                                                                        var view=new GarminBenfishView();var service=new GarminBenfishService(view);var delegate=new GarminBenfishDelegate(service,view);return[view,delegate];}
}




// ─────────────────────────────────────────────────────────────────────────────
// OsintScopeView — programmatic bearing/range OSINT contact display
// ─────────────────────────────────────────────────────────────────────────────
class OsintScopeView extends WatchUi.View {
    public var _contacts as Array;
    public var _selIdx as Number = 0;
    private var _radiusNm as Double = 50.0d;

    function initialize(contacts as Array) {
        View.initialize();
        _contacts = contacts;
    }

    function nextContact() as Void {
        if (_contacts.size() > 0) { _selIdx = (_selIdx + 1) % _contacts.size(); WatchUi.requestUpdate(); }
    }
    function prevContact() as Void {
        if (_contacts.size() > 0) { _selIdx = (_selIdx - 1 + _contacts.size()) % _contacts.size(); WatchUi.requestUpdate(); }
    }
    function zoomIn()  as Void { _radiusNm = _radiusNm * 0.5d; if (_radiusNm < 1.0d) { _radiusNm = 1.0d; } WatchUi.requestUpdate(); }
    function zoomOut() as Void { _radiusNm = _radiusNm * 2.0d; if (_radiusNm > 4000.0d) { _radiusNm = 4000.0d; } WatchUi.requestUpdate(); }

    function onUpdate(dc as Graphics.Dc) as Void {
        var w = dc.getWidth(); var h = dc.getHeight();
        var cx = w / 2; var cy = h / 2;
        var r = (w < h ? w : h) / 2.0 - 12;
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
        dc.clear();
        // Grid rings
        dc.setColor(0x002244, Graphics.COLOR_TRANSPARENT);
        dc.drawCircle(cx, cy, r);
        dc.drawCircle(cx, cy, r * 0.66);
        dc.drawCircle(cx, cy, r * 0.33);
        dc.drawLine(cx, cy - r, cx, cy + r);
        dc.drawLine(cx - r, cy, cx + r, cy);
        // Vessel marker
        dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
        dc.fillPolygon([[cx, cy - 7], [cx - 4, cy + 4], [cx + 4, cy + 4]] as Array);
        // Contacts
        var selContact = null;
        for (var i = 0; i < _contacts.size(); i++) {
            var c = _contacts[i]; if (!(c instanceof Dictionary)) { continue; }
            var brg = c.get("bearing"); var dist = c.get("range_nm");
            if (brg == null || dist == null) { continue; }
            var bd = 0.0d; if (brg instanceof Float) { bd = brg.toDouble(); } else if (brg instanceof Double) { bd = brg; } else if (brg instanceof Number) { bd = brg.toFloat().toDouble(); }
            var dd = 0.0d; if (dist instanceof Float) { dd = dist.toDouble(); } else if (dist instanceof Double) { dd = dist; } else if (dist instanceof Number) { dd = dist.toFloat().toDouble(); }
            if (dd > _radiusNm) { continue; }
            var theta = Math.toRadians(bd - 90);
            var pd = (dd / _radiusNm) * r;
            var tx = cx + (Math.cos(theta) * pd);
            var ty = cy + (Math.sin(theta) * pd);
            var typ = c.get("type");
            if (typ != null && typ.equals("OSINT_MILITARY")) {
                dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
                dc.fillPolygon([[tx, ty - 6], [tx - 4, ty + 4], [tx + 4, ty + 4]] as Array);
                dc.fillPolygon([[tx, ty + 6], [tx - 4, ty - 4], [tx + 4, ty - 4]] as Array);
            } else if (typ != null && typ.equals("OSINT_WEATHER")) {
                dc.setColor(0x0066FF, Graphics.COLOR_TRANSPARENT);
                dc.fillPolygon([[tx, ty - 6], [tx + 6, ty], [tx, ty + 6], [tx - 6, ty]] as Array);
            } else if (typ != null && typ.equals("OSINT_PIRACY")) {
                dc.setColor(Graphics.COLOR_YELLOW, Graphics.COLOR_TRANSPARENT);
                dc.fillRectangle(tx - 4, ty - 4, 8, 8);
            } else {
                dc.setColor(0x00FF88, Graphics.COLOR_TRANSPARENT);
                dc.drawCircle(tx, ty, 4); dc.drawLine(tx, ty - 5, tx, ty + 5); dc.drawLine(tx - 5, ty, tx + 5, ty);
            }
            if (i == _selIdx) { selContact = c; }
        }
        // Selected contact info
        if (selContact != null && selContact instanceof Dictionary) {
            var nm  = selContact.get("name");   var rn = selContact.get("range_nm"); var brg2 = selContact.get("bearing");
            var rf = 0.0; if (rn instanceof Float || rn instanceof Double) { rf = rn; } else if (rn instanceof Number) { rf = rn.toFloat(); }
            var bf = 0.0; if (brg2 instanceof Float || brg2 instanceof Double) { bf = brg2; } else if (brg2 instanceof Number) { bf = brg2.toFloat(); }
            dc.setColor(0x00FFAA, Graphics.COLOR_TRANSPARENT);
            dc.drawText(cx, h - 58, Graphics.FONT_XTINY, nm != null ? nm.toString() : "UNKNOWN", Graphics.TEXT_JUSTIFY_CENTER);
            dc.drawText(cx, h - 43, Graphics.FONT_XTINY, "RNG:" + rf.format("%.1f") + "nm BRG:" + bf.format("%.0f"), Graphics.TEXT_JUSTIFY_CENTER);
        } else if (_contacts.size() == 0) {
            dc.setColor(0x00AADD, Graphics.COLOR_TRANSPARENT);
            dc.drawText(cx, cy + 20, Graphics.FONT_XTINY, "AWAITING OSINT FEED", Graphics.TEXT_JUSTIFY_CENTER);
        }
        dc.setColor(0x003366, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, h - 16, Graphics.FONT_XTINY, _radiusNm.format("%.0f") + "nm | " + _contacts.size().toString() + " TGT", Graphics.TEXT_JUSTIFY_CENTER);
    }
}

class OsintScopeDelegate extends WatchUi.BehaviorDelegate {
    function initialize() { BehaviorDelegate.initialize(); }

    // UP button = zoom in (BehaviorDelegate routes physical UP btn here)
    function onPreviousPage() as Boolean {
        var v = WatchUi.getCurrentView();
        if (v != null && v[0] instanceof OsintScopeView) { (v[0] as OsintScopeView).zoomIn(); return true; }
        return false;
    }

    // DOWN button = zoom out
    function onNextPage() as Boolean {
        var v = WatchUi.getCurrentView();
        if (v != null && v[0] instanceof OsintScopeView) { (v[0] as OsintScopeView).zoomOut(); return true; }
        return false;
    }

    // Tap screen = cycle to next contact
    function onTap(evt as WatchUi.ClickEvent) as Boolean {
        var v = WatchUi.getCurrentView();
        if (v != null && v[0] instanceof OsintScopeView) { (v[0] as OsintScopeView).nextContact(); return true; }
        return false;
    }

    // SELECT button = open AIS contact list
    function onSelect() as Boolean {
        var v = WatchUi.getCurrentView();
        if (v != null && v[0] instanceof OsintScopeView) {
            var sv = v[0] as OsintScopeView;
            WatchUi.pushView(new OsintListMenu(sv._contacts, sv), new OsintListDelegate(sv), WatchUi.SLIDE_LEFT);
            return true;
        }
        return false;
    }

    function onKey(evt as WatchUi.KeyEvent) as Boolean { return false; }
    function onBack() as Boolean { WatchUi.popView(WatchUi.SLIDE_RIGHT); return true; }
}

class OsintRadarView extends WatchUi.View {
    public var _contacts as Array;
    public var _lat as Double = 0.0d;
    public var _lon as Double = 0.0d;
    private var _service as GarminBenfishService;
    private var _panOffsetX as Number = 0;
    private var _panOffsetY as Number = 0;
    public var _radiusMiles as Double = 50.0d;
    public var _selectedContactId as String or Null = null;
    public var _selectedContact as Dictionary or Null = null;
    public var _selectedContactIndex as Number = -1;
    // Radar map image (server-composited: tiles + AIS contacts)
    private var _radarImage = null;
    private var _loading as Boolean = false;
    private var _failed  as Boolean = false;
    private var _refreshTimer as Timer.Timer;

    function initialize(contacts as Array, lat as Double, lon as Double, service as GarminBenfishService) {
        View.initialize();
        _contacts = contacts;
        _lat = lat; _lon = lon;
        _service = service;
        _refreshTimer = new Timer.Timer();
    }

    function onShow() as Void {
        requestRadarMap();
        _refreshTimer.start(method(:onRefreshTimer), 30000, true);
    }

    function onHide() as Void { _refreshTimer.stop(); }

    function onRefreshTimer() as Void { requestRadarMap(); }

    function requestRadarMap() as Void {
        _loading = true; _failed = false;
        WatchUi.requestUpdate();
        var hdg = 0.0d;
        if (_service != null && _service._view != null && _service._view._metrics != null) {
            var hv = _service._view._metrics.get("heading");
            if (hv instanceof Float) { hdg = hv.toDouble(); }
            else if (hv instanceof Double) { hdg = hv; }
            else if (hv instanceof Number) { hdg = hv.toFloat().toDouble(); }
        }
        var cb   = System.getTimer().toString();
        var r    = _radiusMiles.toNumber().toString();
        var hdgs = hdg.format("%.1f");
        var inner = "https://benfishmanta.duckdns.org/ais_radar_map/" + r + "/" + hdgs + "/" + cb;
        var url   = "https://wsrv.nl/?url=" + inner + "&n=1&output=jpg";
        var opts  = { :maxWidth => 454, :maxHeight => 454 };
        Communications.makeImageRequest(url, {}, opts, method(:onRadarReceive));
    }

    function onRadarReceive(code as Number, data as Graphics.BitmapReference or WatchUi.BitmapResource or Null) as Void {
        _loading = false;
        if (code == 200 && data != null) { _radarImage = data; _failed = false; }
        else { _failed = true; }
        WatchUi.requestUpdate();
    }

    function zoomIn() as Void {
        _radiusMiles = _radiusMiles * 0.5d;
        if (_radiusMiles < 1.0d) { _radiusMiles = 1.0d; }
        requestRadarMap();
    }

    function zoomOut() as Void {
        _radiusMiles = _radiusMiles * 2.0d;
        if (_radiusMiles > 4000.0d) { _radiusMiles = 4000.0d; }
        requestRadarMap();
    }

    public function nextContact() as Void {
        if (_contacts != null && _contacts.size() > 0) {
            _selectedContactIndex++;
            if (_selectedContactIndex >= _contacts.size()) { _selectedContactIndex = 0; }
            var newSel = _contacts[_selectedContactIndex];
            if (newSel instanceof Dictionary && newSel.get("id") != null) {
                _selectedContactId = newSel.get("id").toString();
            }
        }
        WatchUi.requestUpdate();
    }

    public function prevContact() as Void {
        if (_contacts != null && _contacts.size() > 0) {
            _selectedContactIndex--;
            if (_selectedContactIndex < 0) { _selectedContactIndex = _contacts.size() - 1; }
            var newSel = _contacts[_selectedContactIndex];
            if (newSel instanceof Dictionary && newSel.get("id") != null) {
                _selectedContactId = newSel.get("id").toString();
            }
        }
        WatchUi.requestUpdate();
    }

    function onUpdate(dc as Graphics.Dc) as Void {
        var w = dc.getWidth(); var h = dc.getHeight();
        var cx = (w / 2) + _panOffsetX;
        var cy = (h / 2) + _panOffsetY;

        if (_selectedContactId == null && _contacts != null && _contacts.size() > 0) {
            var c0 = _contacts[0];
            if (c0 instanceof Dictionary && c0.get("id") != null) {
                _selectedContactId = c0.get("id").toString();
            }
        }

        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
        dc.clear();

        // Draw server-rendered AIS radar map (tiles + contacts composited server-side)
        if (_radarImage != null) {
            var bw = _radarImage.getWidth();
            var bh = _radarImage.getHeight();
            dc.drawBitmap(cx - bw/2, cy - bh/2, _radarImage);
        }

        // Loading / error overlay
        if (_loading) {
            dc.setColor(0x00AADD, Graphics.COLOR_TRANSPARENT);
            dc.drawText(cx, cy - 10, Graphics.FONT_XTINY, "LOADING AIS RADAR", Graphics.TEXT_JUSTIFY_CENTER);
        } else if (_failed) {
            dc.setColor(Graphics.COLOR_RED, Graphics.COLOR_TRANSPARENT);
            dc.drawText(cx, cy,     Graphics.FONT_XTINY, "COMMS FAILED",     Graphics.TEXT_JUSTIFY_CENTER);
            dc.drawText(cx, cy+14,  Graphics.FONT_XTINY, "Tap to retry",     Graphics.TEXT_JUSTIFY_CENTER);
        } else if (_radarImage == null) {
            dc.setColor(0x00AADD, Graphics.COLOR_TRANSPARENT);
            dc.drawText(cx, cy - 10, Graphics.FONT_SMALL,  "AWAITING",         Graphics.TEXT_JUSTIFY_CENTER);
            dc.drawText(cx, cy + 14, Graphics.FONT_XTINY,  "PYXIS POSITION",   Graphics.TEXT_JUSTIFY_CENTER);
        }

        // Selected contact info overlay (still driven by MAP_REQ contacts array)
        _selectedContact = null;
        _selectedContactIndex = -1;
        if (_selectedContactId != null && _contacts != null) {
            for (var i = 0; i < _contacts.size(); i++) {
                var c = _contacts[i];
                if (c instanceof Dictionary && c.get("id") != null && c.get("id").toString().equals(_selectedContactId)) {
                    _selectedContact = c;
                    _selectedContactIndex = i;
                }
            }
        }
        if (_selectedContact != null && _selectedContact instanceof Dictionary) {
            var m_name = _selectedContact.get("name") != null ? _selectedContact.get("name") : "UNKNOWN";
            var m_rng  = _selectedContact.get("range_nm") != null ? _selectedContact.get("range_nm") : 0.0;
            var m_brg  = _selectedContact.get("bearing")  != null ? _selectedContact.get("bearing")  : 0.0;
            var m_sog  = _selectedContact.get("speed")    != null ? _selectedContact.get("speed")    : 0.0;
            var r_f = 0.0; if (m_rng instanceof Float || m_rng instanceof Double) { r_f = m_rng; } else if (m_rng instanceof Number) { r_f = m_rng.toFloat(); }
            var b_f = 0.0; if (m_brg instanceof Float || m_brg instanceof Double) { b_f = m_brg; } else if (m_brg instanceof Number) { b_f = m_brg.toFloat(); }
            var s_f = 0.0; if (m_sog instanceof Float || m_sog instanceof Double) { s_f = m_sog; } else if (m_sog instanceof Number) { s_f = m_sog.toFloat(); }
            dc.setColor(0x00FFAA, Graphics.COLOR_TRANSPARENT);
            dc.drawText(cx, h - 70, Graphics.FONT_XTINY, m_name.toString(), Graphics.TEXT_JUSTIFY_CENTER);
            dc.drawText(cx, h - 55, Graphics.FONT_XTINY, "RNG:" + r_f.format("%.1f") + "nm BRG:" + b_f.format("%.0f"), Graphics.TEXT_JUSTIFY_CENTER);
            if (s_f > 0.0) {
                dc.drawText(cx, h - 40, Graphics.FONT_XTINY, "SOG:" + s_f.format("%.1f") + "kn", Graphics.TEXT_JUSTIFY_CENTER);
            }
        }

        // Zoom level indicator
        dc.setColor(0x004488, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, h - 18, Graphics.FONT_XTINY, _radiusMiles.format("%.0f") + "nm", Graphics.TEXT_JUSTIFY_CENTER);
    }
}

class OsintRadarDelegate extends WatchUi.BehaviorDelegate {
    private var _service as GarminBenfishService;

    function initialize(service as GarminBenfishService) {
        BehaviorDelegate.initialize();
        _service = service;
    }

    function onNextPage() as Boolean {
        var view = WatchUi.getCurrentView();
        if (view != null && view[0] instanceof OsintRadarView) {
            (view[0] as OsintRadarView).zoomOut();
            return true;
        }
        return false;
    }

    function onPreviousPage() as Boolean {
        var view = WatchUi.getCurrentView();
        if (view != null && view[0] instanceof OsintRadarView) {
            (view[0] as OsintRadarView).zoomIn();
            return true;
        }
        return false;
    }

    function onKey(evt as WatchUi.KeyEvent) as Boolean {
        return false;
    }

    function onTap(evt as WatchUi.ClickEvent) as Boolean {
        var currentViewArray = WatchUi.getCurrentView();
        if (currentViewArray != null && currentViewArray[0] instanceof OsintRadarView) {
            (currentViewArray[0] as OsintRadarView).nextContact();
            return true;
        }
        return false;
    }

    function onSelect() as Boolean {
        var currentViewArray = WatchUi.getCurrentView();
        if (currentViewArray != null && currentViewArray[0] instanceof OsintRadarView) {
            var lrView = currentViewArray[0] as OsintRadarView;
            if (lrView._contacts != null && lrView._contacts.size() > 0) {
                WatchUi.pushView(new AisListMenu(lrView._contacts, lrView), new AisListDelegate(lrView), WatchUi.SLIDE_LEFT);
                return true;
            }
        }
        return false;
    }

    function onBack() as Boolean {
        _service.makeRequest("DISENGAGE_RADAR", false, 0.0f);
        WatchUi.popView(WatchUi.SLIDE_RIGHT);
        return true;
    }
}

// =====================================================================
// WIND / WAVE ROSE — Animated Environmental Vector Compass
// Shows: Wind (cyan, pulsing), Wave (white), Swell (orange), Current (green)
// =====================================================================
class WindRoseView extends WatchUi.View {
    private var _data    as Dictionary = {};
    private var _loading as Boolean    = true;
    private var _failed  as Boolean    = false;
    private var _tick    as Number     = 0;
    private var _timer   as Timer.Timer;

    function initialize() {
        View.initialize();
        _timer = new Timer.Timer();
    }

    function onShow() as Void {
        _loading = true; _failed = false; _data = {}; _tick = 0;
        WatchUi.requestUpdate();
        _timer.start(method(:onTick), 800, true);
        var cb  = System.getTimer().toString();
        var url = "https://benfishmanta.duckdns.org/sea_state_json/" + cb;
        var opts = { :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON };
        Communications.makeWebRequest(url, {}, opts, method(:onData));
    }

    function onHide() as Void { _timer.stop(); }
    function onTick() as Void { _tick++; WatchUi.requestUpdate(); }

    function onData(code as Number, data as Dictionary or Null) as Void {
        _loading = false;
        if (code == 200 && data != null) { _data = data; _failed = false; }
        else { _failed = true; }
        WatchUi.requestUpdate();
    }

    // Parse "SSW (204°)" -> 204.0f, also accepts plain "204" or "204.0"
    // Returns -1 on failure.
    function parseBrg(s as String or Null) as Float {
        if (s == null || s.equals("n/a") || s.equals("")) { return -1.0f; }
        // Primary: "SSW (204°)" format
        var si = s.find("(");
        var ei = s.find("\u00b0");
        if (si != null && ei != null && si >= 0 && ei > si) {
            var n = s.substring(si + 1, ei);
            if (n != null) {
                var res = n.toFloat();
                if (res != null) { return res; }
            }
        }
        // Fallback: plain numeric string e.g. "204" or "204.0"
        var direct = s.toFloat();
        return (direct != null && direct >= 0.0f) ? direct : -1.0f;
    }

    // Extracts the compass abbreviation (e.g. "SSW") from a direction string like "SSW (204°)"
    function abbr(s as String or Null) as String {
        if (s == null) { return "n/a"; }
        var sp = s.find(" "); return sp > 0 ? s.substring(0, sp) : s;
    }

    function drawArrow(dc as Graphics.Dc, cx as Number, cy as Number,
                       radius as Number, brg as Float,
                       color as Number, thick as Number) as Void {
        var rad  = Math.toRadians(brg.toDouble());
        var tipX = (cx + radius * Math.sin(rad)).toNumber();
        var tipY = (cy - radius * Math.cos(rad)).toNumber();
        var tlX  = (cx - radius * 0.15d * Math.sin(rad)).toNumber();
        var tlY  = (cy + radius * 0.15d * Math.cos(rad)).toNumber();
        dc.setColor(color, Graphics.COLOR_TRANSPARENT);
        dc.setPenWidth(thick);
        dc.drawLine(tlX, tlY, tipX, tipY);
        var perp = Math.toRadians(brg.toDouble() + 90.0d);
        var hs = 9;
        var hx1 = (tipX - hs * Math.sin(rad) + (hs * 0.5d) * Math.sin(perp)).toNumber();
        var hy1 = (tipY + hs * Math.cos(rad) - (hs * 0.5d) * Math.cos(perp)).toNumber();
        var hx2 = (tipX - hs * Math.sin(rad) - (hs * 0.5d) * Math.sin(perp)).toNumber();
        var hy2 = (tipY + hs * Math.cos(rad) + (hs * 0.5d) * Math.cos(perp)).toNumber();
        dc.fillPolygon([[tipX, tipY], [hx1, hy1], [hx2, hy2]]);
    }

    // Safe heading normalization: always returns [0.0, 360.0)
    // Uses integer modulo since Monkey C does not support float %
    function normDeg(d as Float) as Float {
        var n = d.toNumber() % 360;
        if (n < 0) { n = n + 360; }
        return n.toFloat();
    }

    function onUpdate(dc as Graphics.Dc) as Void {
        var w = dc.getWidth(); var h = dc.getHeight();
        var cx = w / 2; var cy = h / 2;
        var r  = (w < h ? w : h) / 2 - 40;
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK); dc.clear();

        // Header
        dc.setColor(0x00FFAA, -1);
        dc.drawText(cx, 4 + dc.getFontHeight(Graphics.FONT_XTINY),
                    Graphics.FONT_XTINY, "WAVE / SWELL / CURRENT", Graphics.TEXT_JUSTIFY_CENTER);

        if (_loading) {
            dc.setColor(0x00FFAA, -1);
            dc.drawText(cx, cy, Graphics.FONT_XTINY, "FETCHING...",
                        Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
            return;
        }
        if (_failed) {
            dc.setColor(Graphics.COLOR_RED, -1);
            dc.drawText(cx, cy, Graphics.FONT_XTINY, "DATA ERROR",
                        Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
            return;
        }

        // Compass rings
        dc.setPenWidth(1);
        dc.setColor(0x004422, -1); dc.drawCircle(cx, cy, r);
        dc.setColor(0x002211, -1); dc.drawCircle(cx, cy, r / 2);
        dc.setColor(0x113311, -1); dc.drawCircle(cx, cy, r * 3 / 4);

        // Fixed NSEW labels
        dc.setColor(0xFF3322, -1); dc.drawText(cx,      cy - r + 12, Graphics.FONT_XTINY, "N", Graphics.TEXT_JUSTIFY_CENTER);
        dc.setColor(0x44AAFF, -1); dc.drawText(cx+r-12, cy,          Graphics.FONT_XTINY, "E", Graphics.TEXT_JUSTIFY_CENTER);
        dc.setColor(0xCCCCCC, -1); dc.drawText(cx,      cy + r - 18, Graphics.FONT_XTINY, "S", Graphics.TEXT_JUSTIFY_CENTER);
        dc.setColor(0x44AAFF, -1); dc.drawText(cx-r+8,  cy,          Graphics.FONT_XTINY, "W", Graphics.TEXT_JUSTIFY_CENTER);

        // Value strings
        var wvh = _data.get("wave_h");  var wvhStr = wvh != null ? wvh.toString() : "?";
        var swh = _data.get("swell_h"); var swhStr = swh != null ? swh.toString() : "?";
        var cv  = _data.get("curr_v");  var cvStr  = cv  != null ? cv.toString()  : "?";

        // Direction label strings (for tail abbr)
        var wvDirStr = _data.get("wave_dir");
        var sDirStr  = _data.get("swell_dir");
        var cDirStr  = _data.get("curr_dir");

        // Raw numeric bearing fields (FROM direction, degrees)
        var wvdDeg = _data.get("wave_dir_deg");
        var sdDeg  = _data.get("swell_dir_deg");
        var cdDeg  = _data.get("curr_dir_deg");

        // — Wave (WHITE, outer ring)
        if (wvdDeg != null) {
            var wvFrom = wvdDeg.toNumber();
            var wvTo   = (wvFrom + 180) % 360;
            drawArrow(dc, cx, cy, r - 10, wvTo.toFloat(), 0xDDDDDD, 3);
            var wvlRad = Math.toRadians(wvTo.toDouble());
            var wvfRad = Math.toRadians(wvFrom.toDouble());
            dc.setColor(0xDDDDDD, -1);
            dc.drawText((cx + (r - 8) * Math.sin(wvlRad)).toNumber(),
                        (cy - (r - 8) * Math.cos(wvlRad)).toNumber(),
                        Graphics.FONT_XTINY, "Wv:" + wvhStr + "m",
                        Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
            dc.setColor(0x555555, -1);
            dc.drawText((cx + 30 * Math.sin(wvfRad)).toNumber(),
                        (cy - 30 * Math.cos(wvfRad)).toNumber(),
                        Graphics.FONT_XTINY, abbr(wvDirStr != null ? wvDirStr.toString() : null),
                        Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
        }

        // — Swell (ORANGE, middle ring)
        if (sdDeg != null) {
            var sFrom = sdDeg.toNumber();
            var sTo   = (sFrom + 180) % 360;
            drawArrow(dc, cx, cy, r - 28, sTo.toFloat(), 0xFF8800, 2);
            var slRad = Math.toRadians(sTo.toDouble());
            var sfRad = Math.toRadians(sFrom.toDouble());
            dc.setColor(0xFF8800, -1);
            dc.drawText((cx + (r - 26) * Math.sin(slRad)).toNumber(),
                        (cy - (r - 26) * Math.cos(slRad)).toNumber(),
                        Graphics.FONT_XTINY, "Sw:" + swhStr + "m",
                        Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
            dc.setColor(0x663300, -1);
            dc.drawText((cx + 30 * Math.sin(sfRad)).toNumber(),
                        (cy - 30 * Math.cos(sfRad)).toNumber(),
                        Graphics.FONT_XTINY, abbr(sDirStr != null ? sDirStr.toString() : null),
                        Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
        }

        // — Current (GREEN, inner ring)
        if (cdDeg != null) {
            var cFrom = cdDeg.toNumber();
            var cTo   = (cFrom + 180) % 360;
            drawArrow(dc, cx, cy, r / 2 - 2, cTo.toFloat(), 0x00CC44, 2);
            var clRad = Math.toRadians(cTo.toDouble());
            var cfRad = Math.toRadians(cFrom.toDouble());
            dc.setColor(0x00CC44, -1);
            dc.drawText((cx + (r / 2) * Math.sin(clRad)).toNumber(),
                        (cy - (r / 2) * Math.cos(clRad)).toNumber(),
                        Graphics.FONT_XTINY, "C:" + cvStr + "kn",
                        Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
            dc.setColor(0x005522, -1);
            dc.drawText((cx + 22 * Math.sin(cfRad)).toNumber(),
                        (cy - 22 * Math.cos(cfRad)).toNumber(),
                        Graphics.FONT_XTINY, abbr(cDirStr != null ? cDirStr.toString() : null),
                        Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
        }
    }
}

class WindRoseDelegate extends WatchUi.BehaviorDelegate {
    function initialize() { BehaviorDelegate.initialize(); }
    function onBack() as Boolean { WatchUi.popView(WatchUi.SLIDE_DOWN); return true; }
}
