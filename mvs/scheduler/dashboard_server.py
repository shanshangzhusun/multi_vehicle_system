from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Optional


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>MVS Simulation</title>
  <style>
    :root {
      --bg0: #f6f7fb;
      --bg1: #eef2f7;
      --panel: rgba(255,255,255,0.74);
      --panel-strong: rgba(255,255,255,0.86);
      --line: rgba(35, 53, 74, 0.08);
      --text: #102033;
      --muted: #617284;
      --accent: #0c7f5f;
      --accent-soft: rgba(12,127,95,0.12);
      --warning: #d88226;
      --danger: #c24d32;
      --blue: #2b6cb0;
      --road: #d6dbe3;
      --road-core: #f9fbfd;
      --shadow: 0 22px 60px rgba(74, 93, 124, 0.12);
      --radius-xl: 28px;
      --radius-lg: 22px;
      --radius-md: 18px;
    }

    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "PingFang SC", "Hiragino Sans GB", "Noto Sans SC", sans-serif;
      background:
        radial-gradient(circle at 16% 18%, rgba(84,158,235,0.18), transparent 26%),
        radial-gradient(circle at 88% 14%, rgba(12,127,95,0.12), transparent 24%),
        radial-gradient(circle at 76% 88%, rgba(216,130,38,0.10), transparent 28%),
        linear-gradient(180deg, var(--bg0), var(--bg1));
      color: var(--text);
      overflow: hidden;
    }

    .shell {
      position: relative;
      min-height: 100vh;
      padding: 18px;
    }

    .ambient {
      position: absolute;
      border-radius: 999px;
      filter: blur(24px);
      opacity: 0.5;
      pointer-events: none;
    }

    .ambient.a { width: 260px; height: 260px; background: rgba(75, 140, 255, 0.12); top: 18px; left: 22px; }
    .ambient.b { width: 220px; height: 220px; background: rgba(12, 127, 95, 0.12); bottom: 28px; right: 46px; }
    .ambient.c { width: 180px; height: 180px; background: rgba(208, 120, 64, 0.10); top: 180px; right: 240px; }

    .glass {
      background: var(--panel);
      border: 1px solid rgba(255,255,255,0.62);
      box-shadow: var(--shadow);
      backdrop-filter: blur(22px);
      -webkit-backdrop-filter: blur(22px);
    }

    .topbar {
      position: relative;
      z-index: 2;
      display: grid;
      grid-template-columns: 280px 1fr;
      gap: 14px;
      padding: 16px 18px;
      border-radius: var(--radius-xl);
      margin-bottom: 16px;
    }

    .brandTitle {
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.03em;
    }

    .brandSub {
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(6, minmax(110px, 1fr));
      gap: 10px;
    }

    .metric {
      padding: 12px 14px;
      border-radius: 18px;
      background: rgba(255,255,255,0.58);
      border: 1px solid rgba(255,255,255,0.72);
    }

    .metric .label {
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
    }

    .metric .value {
      font-size: 21px;
      font-weight: 700;
      letter-spacing: -0.03em;
    }

    .layout {
      position: relative;
      z-index: 2;
      display: grid;
      grid-template-columns: minmax(640px, 1.35fr) minmax(360px, 0.78fr);
      gap: 16px;
      height: calc(100vh - 128px);
    }

    .mapCard {
      display: grid;
      grid-template-rows: auto 1fr auto;
      border-radius: var(--radius-xl);
      overflow: hidden;
    }

    .mapHead {
      padding: 18px 20px 12px 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 18px;
    }

    .mapTitle {
      font-size: 18px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }

    .mapSub {
      color: var(--muted);
      font-size: 13px;
      margin-top: 4px;
    }

    .clockBlock {
      text-align: right;
    }

    .clockValue {
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.03em;
    }

    .clockMeta {
      margin-top: 3px;
      font-size: 12px;
      color: var(--muted);
    }

    .canvasWrap {
      position: relative;
      padding: 6px 14px 0 14px;
    }

    #map {
      width: 100%;
      height: 100%;
      border-radius: 22px;
      background:
        radial-gradient(circle at 20% 18%, rgba(255,255,255,0.88), rgba(247,250,252,0.92) 40%, rgba(237,241,247,0.96) 100%);
      border: 1px solid rgba(255,255,255,0.72);
    }

    .mapFooter {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 12px 18px 18px 18px;
      color: var(--muted);
      font-size: 13px;
    }

    .legend {
      display: flex;
      gap: 14px;
      flex-wrap: wrap;
    }

    .legendItem {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.58);
      border: 1px solid rgba(255,255,255,0.72);
    }

    .swatch {
      width: 10px;
      height: 10px;
      border-radius: 999px;
    }

    .side {
      display: grid;
      grid-template-rows: 220px 1fr 1fr 1fr;
      gap: 16px;
      min-height: 0;
    }

    .panel {
      border-radius: var(--radius-xl);
      padding: 16px;
      overflow: hidden;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }

    .panelHead {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      margin-bottom: 12px;
    }

    .panelTitle {
      font-size: 16px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }

    .panelMeta {
      font-size: 12px;
      color: var(--muted);
    }

    .scroll {
      overflow: auto;
      min-height: 0;
      padding-right: 2px;
    }

    .vehicleSummary {
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 12px;
      min-height: 0;
    }

    .statGrid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }

    .miniStat {
      padding: 12px;
      border-radius: 18px;
      background: rgba(255,255,255,0.56);
      border: 1px solid rgba(255,255,255,0.72);
    }

    .miniStat .name {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }

    .miniStat .num {
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.03em;
    }

    .vehicleCard, .taskCard, .eventCard {
      border-radius: 18px;
      padding: 12px 14px;
      background: rgba(255,255,255,0.58);
      border: 1px solid rgba(255,255,255,0.72);
      margin-bottom: 10px;
    }

    .vehicleCard {
      cursor: pointer;
      transition: transform 180ms ease, box-shadow 180ms ease, border-color 180ms ease;
    }

    .vehicleCard:hover {
      transform: translateY(-2px);
      box-shadow: 0 16px 28px rgba(52, 74, 107, 0.10);
    }

    .vehicleCard.active {
      border-color: rgba(43,108,176,0.34);
      box-shadow: 0 16px 28px rgba(52, 74, 107, 0.14);
      background: rgba(255,255,255,0.78);
    }

    .cardTitle {
      font-size: 14px;
      font-weight: 700;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
    }

    .statusTag {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 11px;
      font-weight: 600;
      background: rgba(16,32,51,0.06);
      color: var(--text);
    }

    .statusDot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
    }

    .muted {
      color: var(--muted);
    }

    .kv {
      margin-top: 10px;
      display: grid;
      grid-template-columns: 110px 1fr;
      gap: 6px 10px;
      font-size: 13px;
      line-height: 1.4;
    }

    .kv .k { color: var(--muted); }
    .kv .v { color: var(--text); word-break: break-word; }

    .taskBar {
      height: 8px;
      border-radius: 999px;
      background: rgba(16,32,51,0.08);
      overflow: hidden;
      margin-top: 12px;
    }

    .taskBarFill {
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, rgba(12,127,95,0.88), rgba(42,156,122,0.92));
    }

    .eventCard {
      display: grid;
      grid-template-columns: 64px 1fr;
      gap: 10px;
      align-items: start;
    }

    .eventTime {
      font-size: 11px;
      color: var(--muted);
      padding-top: 2px;
    }

    .eventBody {
      font-size: 13px;
      line-height: 1.45;
    }

    @media (max-width: 1180px) {
      .topbar { grid-template-columns: 1fr; }
      .metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .layout { grid-template-columns: 1fr; height: auto; }
      .side { grid-template-rows: none; grid-auto-rows: minmax(220px, auto); }
      body { overflow: auto; }
      .shell { min-height: auto; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="ambient a"></div>
    <div class="ambient b"></div>
    <div class="ambient c"></div>

    <header class="topbar glass">
      <div>
        <div class="brandTitle">MVS Live Simulation</div>
        <div class="brandSub">iOS 风格仿真面板，展示地图、时间流动、车辆动画、配置和任务状态</div>
      </div>
      <div class="metrics">
        <div class="metric"><div class="label">在线车辆</div><div class="value" id="mOnline">0</div></div>
        <div class="metric"><div class="label">执行中任务</div><div class="value" id="mRunning">0</div></div>
        <div class="metric"><div class="label">待分配任务</div><div class="value" id="mPending">0</div></div>
        <div class="metric"><div class="label">死锁退避</div><div class="value" id="mDeadlock">0</div></div>
        <div class="metric"><div class="label">缓存命中率</div><div class="value" id="mCache">0%</div></div>
        <div class="metric"><div class="label">预约拒绝</div><div class="value" id="mReject">0</div></div>
      </div>
    </header>

    <main class="layout">
      <section class="mapCard glass">
        <div class="mapHead">
          <div>
            <div class="mapTitle">Operation Theater</div>
            <div class="mapSub">地图按真实边几何渲染，车辆会沿当前执行轨迹持续移动</div>
          </div>
          <div class="clockBlock">
            <div class="clockValue" id="clock">--:--:--</div>
            <div class="clockMeta" id="clockMeta">等待调度状态</div>
          </div>
        </div>
        <div class="canvasWrap">
          <canvas id="map"></canvas>
        </div>
        <div class="mapFooter">
          <div class="legend">
            <span class="legendItem"><span class="swatch" style="background:#118a63"></span>隐蔽点</span>
            <span class="legendItem"><span class="swatch" style="background:#c24d32"></span>发射点</span>
            <span class="legendItem"><span class="swatch" style="background:#2b6cb0"></span>贮备库</span>
            <span class="legendItem"><span class="swatch" style="background:#121821"></span>车辆</span>
          </div>
          <div id="mapMeta">地图未加载</div>
        </div>
      </section>

      <aside class="side">
        <section class="panel glass">
          <div class="panelHead">
            <div class="panelTitle">Selected Vehicle</div>
            <div class="panelMeta" id="selectedMeta">未选择</div>
          </div>
          <div class="vehicleSummary">
            <div class="scroll" id="selectedVehicle"></div>
            <div class="statGrid" id="selectedStats"></div>
          </div>
        </section>

        <section class="panel glass">
          <div class="panelHead">
            <div class="panelTitle">Recent Events</div>
            <div class="panelMeta">最新 20 条</div>
          </div>
          <div class="scroll" id="events"></div>
        </section>

        <section class="panel glass">
          <div class="panelHead">
            <div class="panelTitle">Fleet</div>
            <div class="panelMeta" id="fleetMeta">0 辆</div>
          </div>
          <div class="scroll" id="fleet"></div>
        </section>

        <section class="panel glass">
          <div class="panelHead">
            <div class="panelTitle">Tasks</div>
            <div class="panelMeta" id="taskMeta">0 项</div>
          </div>
          <div class="scroll" id="tasks"></div>
        </section>
      </aside>
    </main>
  </div>

  <script>
    const App = {
      state: null,
      selectedVehicleId: null,
      nowMs: Date.now(),
      lastFetchMs: 0,
      mapVersion: "",
      dpr: Math.max(1, window.devicePixelRatio || 1),
    };

    const els = {
      map: document.getElementById("map"),
      clock: document.getElementById("clock"),
      clockMeta: document.getElementById("clockMeta"),
      mapMeta: document.getElementById("mapMeta"),
      fleet: document.getElementById("fleet"),
      fleetMeta: document.getElementById("fleetMeta"),
      tasks: document.getElementById("tasks"),
      taskMeta: document.getElementById("taskMeta"),
      events: document.getElementById("events"),
      selectedVehicle: document.getElementById("selectedVehicle"),
      selectedStats: document.getElementById("selectedStats"),
      selectedMeta: document.getElementById("selectedMeta"),
      mOnline: document.getElementById("mOnline"),
      mRunning: document.getElementById("mRunning"),
      mPending: document.getElementById("mPending"),
      mDeadlock: document.getElementById("mDeadlock"),
      mCache: document.getElementById("mCache"),
      mReject: document.getElementById("mReject"),
    };
    const ctx = els.map.getContext("2d");

    function resizeCanvas() {
      const rect = els.map.getBoundingClientRect();
      els.map.width = Math.max(10, Math.floor(rect.width * App.dpr));
      els.map.height = Math.max(10, Math.floor(rect.height * App.dpr));
      ctx.setTransform(App.dpr, 0, 0, App.dpr, 0, 0);
    }

    function parseTs(ts) {
      const ms = Date.parse(ts || "");
      return Number.isFinite(ms) ? ms : null;
    }

    function clamp(v, a, b) {
      return Math.max(a, Math.min(b, v));
    }

    function fmtTime(ts) {
      const d = parseTs(ts);
      if (d === null) return "-";
      return new Date(d).toLocaleTimeString("zh-CN", { hour12: false });
    }

    function getStatusColor(status) {
      if (!status) return "#7f8fa3";
      if (status.includes("EXEC") || status.includes("ENROUTE")) return "#2b6cb0";
      if (status === "PLANNING") return "#d88226";
      if (status === "IDLE") return "#118a63";
      if (status === "OFFLINE") return "#c24d32";
      if (status === "FIRING" || status === "RELOADING" || status === "TO_DEPOT" || status === "RETURNING") return "#8a4fff";
      return "#5f6b76";
    }

    function getNodeMap() {
      const out = {};
      for (const n of (App.state?.map?.nodes || [])) out[n.id] = n;
      return out;
    }

    function getTransform() {
      const nodes = App.state?.map?.nodes || [];
      const W = els.map.clientWidth;
      const H = els.map.clientHeight;
      if (!nodes.length) return { minX: 0, minY: 0, sx: 1, sy: 1, pad: 42, H };
      const xs = nodes.map(n => n.x);
      const ys = nodes.map(n => n.y);
      const minX = Math.min(...xs);
      const maxX = Math.max(...xs);
      const minY = Math.min(...ys);
      const maxY = Math.max(...ys);
      const pad = 42;
      const sx = (W - pad * 2) / Math.max(1, maxX - minX);
      const sy = (H - pad * 2) / Math.max(1, maxY - minY);
      return { minX, minY, sx, sy, pad, H };
    }

    function toCanvas(pt, t) {
      const x = (pt.x - t.minX) * t.sx + t.pad;
      const y = t.H - ((pt.y - t.minY) * t.sy + t.pad);
      return { x, y };
    }

    function getEdgePolyline(edge, nodeMap) {
      if (edge.geometry && edge.geometry.length >= 2) return edge.geometry;
      const a = nodeMap[edge.from];
      const b = nodeMap[edge.to];
      return a && b ? [{ x: a.x, y: a.y }, { x: b.x, y: b.y }] : [];
    }

    function polylineLength(poly) {
      let total = 0;
      for (let i = 0; i + 1 < poly.length; i++) {
        total += Math.hypot(poly[i + 1].x - poly[i].x, poly[i + 1].y - poly[i].y);
      }
      return total;
    }

    function samplePolyline(poly, dist) {
      if (!poly.length) return { x: 0, y: 0, yawDeg: 0 };
      if (poly.length === 1) return { x: poly[0].x, y: poly[0].y, yawDeg: 0 };
      let remain = clamp(dist, 0, polylineLength(poly));
      for (let i = 0; i + 1 < poly.length; i++) {
        const a = poly[i];
        const b = poly[i + 1];
        const seg = Math.hypot(b.x - a.x, b.y - a.y);
        if (seg < 1e-6) continue;
        if (remain <= seg) {
          const r = remain / seg;
          return {
            x: a.x + (b.x - a.x) * r,
            y: a.y + (b.y - a.y) * r,
            yawDeg: Math.atan2(b.y - a.y, b.x - a.x) * 180 / Math.PI,
          };
        }
        remain -= seg;
      }
      const a = poly[poly.length - 2];
      const b = poly[poly.length - 1];
      return {
        x: b.x,
        y: b.y,
        yawDeg: Math.atan2(b.y - a.y, b.x - a.x) * 180 / Math.PI,
      };
    }

    function getVehiclePose(vehicle, nowMs, nodeMap) {
      const plan = vehicle.active_plan;
      if (!plan) {
        const node = nodeMap[vehicle.current_node];
        if (!node) return null;
        return { x: node.x, y: node.y, yawDeg: 0, progress: 0, moving: false };
      }

      const issuedAt = parseTs(plan.issued_at);
      const scale = Number(plan.realtime_scale || vehicle.realtime_scale || 0.05);
      const delayMs = Number(plan.delay_sec || 0) * 1000 * scale;
      const travelMs = (plan.edge_seconds || []).reduce((a, b) => a + Number(b || 0), 0) * 1000 * scale;
      const waitMs = Number(plan.wait_seconds || 0) * 1000 * scale;
      const realStart = (issuedAt || nowMs) + delayMs;
      const realEndTravel = realStart + travelMs;
      const realEnd = realEndTravel + waitMs;
      const trajectory = (plan.trajectory || []).map(p => ({ x: p.x, y: p.y, yawDeg: p.yaw_deg || 0 }));

      let poly = trajectory;
      if (!poly.length) {
        poly = [];
        for (const nid of (plan.node_path || [])) {
          const n = nodeMap[nid];
          if (n) poly.push({ x: n.x, y: n.y, yawDeg: 0 });
        }
      }
      if (!poly.length) {
        const node = nodeMap[vehicle.current_node];
        if (!node) return null;
        return { x: node.x, y: node.y, yawDeg: 0, progress: 0, moving: false };
      }

      if (nowMs <= realStart) {
        return { x: poly[0].x, y: poly[0].y, yawDeg: poly[0].yawDeg || 0, progress: 0, moving: false };
      }
      if (travelMs <= 1) {
        const last = poly[poly.length - 1];
        return { x: last.x, y: last.y, yawDeg: last.yawDeg || 0, progress: 1, moving: false };
      }
      if (nowMs >= realEndTravel) {
        const last = poly[poly.length - 1];
        return { x: last.x, y: last.y, yawDeg: last.yawDeg || 0, progress: 1, moving: nowMs < realEnd };
      }

      const length = polylineLength(poly);
      const elapsed = nowMs - realStart;
      const ratio = clamp(elapsed / travelMs, 0, 1);
      const sampled = samplePolyline(poly, length * ratio);
      return {
        x: sampled.x,
        y: sampled.y,
        yawDeg: sampled.yawDeg,
        progress: ratio,
        moving: true,
      };
    }

    function drawRoundedRect(x, y, w, h, r) {
      const rr = Math.min(r, w / 2, h / 2);
      ctx.beginPath();
      ctx.moveTo(x + rr, y);
      ctx.arcTo(x + w, y, x + w, y + h, rr);
      ctx.arcTo(x + w, y + h, x, y + h, rr);
      ctx.arcTo(x, y + h, x, y, rr);
      ctx.arcTo(x, y, x + w, y, rr);
      ctx.closePath();
    }

    function renderMap(nowMs) {
      const W = els.map.clientWidth;
      const H = els.map.clientHeight;
      ctx.clearRect(0, 0, W, H);
      if (!App.state) return;

      const nodeMap = getNodeMap();
      const t = getTransform();
      const pulse = (Math.sin(nowMs / 700) + 1) / 2;
      const scale = (t.sx + t.sy) * 0.5;

      ctx.save();
      ctx.fillStyle = "rgba(255,255,255,0.2)";
      for (let i = 0; i < 10; i++) {
        ctx.beginPath();
        ctx.arc((W * 0.12) + i * 160, (H * 0.22) + (i % 2) * 90, 1.4 + pulse * 0.9, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();

      for (const edge of (App.state.map.edges || [])) {
        const poly = getEdgePolyline(edge, nodeMap);
        if (poly.length < 2) continue;
        const lineWidth = Math.max(5, edge.width * scale * 0.09);
        ctx.strokeStyle = "#d7dde5";
        ctx.lineWidth = lineWidth;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.beginPath();
        let p = toCanvas(poly[0], t);
        ctx.moveTo(p.x, p.y);
        for (let i = 1; i < poly.length; i++) {
          p = toCanvas(poly[i], t);
          ctx.lineTo(p.x, p.y);
        }
        ctx.stroke();

        ctx.strokeStyle = "rgba(255,255,255,0.86)";
        ctx.lineWidth = Math.max(2, lineWidth * 0.38);
        ctx.beginPath();
        p = toCanvas(poly[0], t);
        ctx.moveTo(p.x, p.y);
        for (let i = 1; i < poly.length; i++) {
          p = toCanvas(poly[i], t);
          ctx.lineTo(p.x, p.y);
        }
        ctx.stroke();
      }

      function drawPointMarker(ids, color, radius) {
        for (const id of (ids || [])) {
          const n = nodeMap[id];
          if (!n) continue;
          const p = toCanvas(n, t);
          ctx.save();
          ctx.fillStyle = color;
          ctx.globalAlpha = 0.14 + pulse * 0.10;
          ctx.beginPath();
          ctx.arc(p.x, p.y, radius + 6 + pulse * 3.5, 0, Math.PI * 2);
          ctx.fill();
          ctx.globalAlpha = 1.0;
          ctx.beginPath();
          ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = "#ffffff";
          ctx.beginPath();
          ctx.arc(p.x, p.y, Math.max(1.5, radius * 0.34), 0, Math.PI * 2);
          ctx.fill();
          ctx.restore();
        }
      }

      drawPointMarker(App.state.points.hide_points, "#118a63", 5);
      drawPointMarker(App.state.points.launch_points, "#c24d32", 6);
      drawPointMarker(App.state.points.depots, "#2b6cb0", 7);

      for (const vehicle of (App.state.vehicles || [])) {
        if (!vehicle.active_plan) continue;
        const poly = (vehicle.active_plan.trajectory || []).map(p => ({ x: p.x, y: p.y }));
        if (poly.length < 2) continue;
        ctx.save();
        ctx.strokeStyle = vehicle.vehicle_id === App.selectedVehicleId ? "rgba(43,108,176,0.52)" : "rgba(16,32,51,0.16)";
        ctx.lineWidth = vehicle.vehicle_id === App.selectedVehicleId ? 3 : 2;
        ctx.setLineDash([8, 7]);
        ctx.lineCap = "round";
        ctx.beginPath();
        let p = toCanvas(poly[0], t);
        ctx.moveTo(p.x, p.y);
        for (let i = 1; i < poly.length; i++) {
          p = toCanvas(poly[i], t);
          ctx.lineTo(p.x, p.y);
        }
        ctx.stroke();
        ctx.restore();
      }

      for (const vehicle of (App.state.vehicles || [])) {
        const pose = getVehiclePose(vehicle, nowMs, nodeMap);
        if (!pose) continue;
        const p = toCanvas(pose, t);
        const selected = vehicle.vehicle_id === App.selectedVehicleId;
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate((pose.yawDeg || 0) * Math.PI / 180);

        ctx.fillStyle = selected ? "rgba(43,108,176,0.16)" : "rgba(18,24,33,0.12)";
        ctx.beginPath();
        ctx.arc(0, 0, selected ? 17 + pulse * 2.8 : 13 + pulse * 1.5, 0, Math.PI * 2);
        ctx.fill();

        ctx.fillStyle = selected ? "#17365f" : "#121821";
        drawRoundedRect(-10, -6, 20, 12, 5);
        ctx.fill();

        ctx.fillStyle = "#eff5fb";
        drawRoundedRect(-7, -4.5, 8, 9, 3);
        ctx.fill();

        ctx.fillStyle = "#ffb55b";
        ctx.beginPath();
        ctx.moveTo(10, 0);
        ctx.lineTo(15, -3.2);
        ctx.lineTo(15, 3.2);
        ctx.closePath();
        ctx.fill();
        ctx.restore();

        ctx.save();
        ctx.font = selected ? "700 12px -apple-system, BlinkMacSystemFont, sans-serif" : "600 11px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.fillStyle = selected ? "#17365f" : "rgba(16,32,51,0.82)";
        ctx.fillText(vehicle.vehicle_id, p.x + 12, p.y - 10);
        if (vehicle.active_plan) {
          ctx.fillStyle = "rgba(16,32,51,0.5)";
          ctx.fillText(vehicle.active_plan.phase || "", p.x + 12, p.y + 6);
        }
        ctx.restore();
      }
    }

    function renderMetrics() {
      const metrics = App.state?.metrics || {};
      const lane = App.state?.lane_graph || {};
      const conflicts = App.state?.conflicts || {};
      els.mOnline.textContent = metrics.vehicles_online ?? 0;
      els.mRunning.textContent = metrics.subtasks_running ?? 0;
      els.mPending.textContent = metrics.subtasks_pending ?? 0;
      els.mDeadlock.textContent = metrics.deadlock_risk ?? 0;
      els.mCache.textContent = `${Math.round((lane.cache_hit_rate || 0) * 100)}%`;
      els.mReject.textContent = conflicts.reserve_reject ?? 0;
    }

    function ensureSelection() {
      const vehicles = App.state?.vehicles || [];
      if (!vehicles.length) {
        App.selectedVehicleId = null;
        return;
      }
      if (App.selectedVehicleId && vehicles.some(v => v.vehicle_id === App.selectedVehicleId)) return;
      const active = vehicles.find(v => v.active_plan) || vehicles.find(v => v.online) || vehicles[0];
      App.selectedVehicleId = active.vehicle_id;
    }

    function renderSelectedVehicle() {
      ensureSelection();
      const vehicles = App.state?.vehicles || [];
      const v = vehicles.find(x => x.vehicle_id === App.selectedVehicleId);
      if (!v) {
        els.selectedVehicle.innerHTML = '<div class="muted">暂无车辆数据</div>';
        els.selectedStats.innerHTML = "";
        els.selectedMeta.textContent = "未选择";
        return;
      }
      els.selectedMeta.textContent = `${v.vehicle_id} · ${v.status}`;
      const plan = v.active_plan;
      els.selectedVehicle.innerHTML = `
        <div class="vehicleCard active" style="margin:0;">
          <div class="cardTitle">
            <span>${v.vehicle_id}</span>
            <span class="statusTag"><span class="statusDot" style="background:${getStatusColor(v.status)}"></span>${v.online ? "在线" : "离线"}</span>
          </div>
          <div class="kv">
            <div class="k">当前位置</div><div class="v">${v.current_node}</div>
            <div class="k">归属节点</div><div class="v">${v.home_node}</div>
            <div class="k">弹种</div><div class="v">${(v.ammo_types || []).join(", ") || "-"}</div>
            <div class="k">速度</div><div class="v">${v.speed_mps ?? "-"} m/s</div>
            <div class="k">监听地址</div><div class="v">${v.endpoint || "-"}</div>
            <div class="k">实时缩放</div><div class="v">${v.realtime_scale ?? "-"}</div>
            <div class="k">活动子任务</div><div class="v">${v.active_subtask_id || "-"}</div>
            <div class="k">当前计划</div><div class="v">${plan ? `${plan.subtask_id} / ${plan.phase}` : "-"}</div>
            <div class="k">计划开始</div><div class="v">${plan ? fmtTime(plan.start_at) : "-"}</div>
            <div class="k">计划结束</div><div class="v">${plan ? fmtTime(plan.end_at) : "-"}</div>
          </div>
        </div>
      `;
      const kin = v.kinematics || {};
      const stats = [
        ["车宽", `${kin.width_m ?? "-"} m`],
        ["车长", `${kin.length_m ?? "-"} m`],
        ["轴距", `${kin.wheelbase_m ?? "-"} m`],
        ["最小转弯半径", `${kin.min_turn_radius_m ?? "-"} m`],
        ["最大速度", `${kin.max_speed_mps ?? "-"} m/s`],
        ["最小安全余量", `${kin.min_clearance_m ?? "-"} m`],
      ];
      els.selectedStats.innerHTML = stats.map(([k, v]) => `
        <div class="miniStat">
          <div class="name">${k}</div>
          <div class="num" style="font-size:18px;">${v}</div>
        </div>
      `).join("");
    }

    function renderFleet() {
      const vehicles = [...(App.state?.vehicles || [])];
      vehicles.sort((a, b) => {
        const aScore = a.active_plan ? 0 : a.online ? 1 : 2;
        const bScore = b.active_plan ? 0 : b.online ? 1 : 2;
        return aScore - bScore || a.vehicle_id.localeCompare(b.vehicle_id);
      });
      els.fleetMeta.textContent = `${vehicles.length} 辆`;
      els.fleet.innerHTML = vehicles.map(v => `
        <div class="vehicleCard ${v.vehicle_id === App.selectedVehicleId ? "active" : ""}" data-vehicle-id="${v.vehicle_id}">
          <div class="cardTitle">
            <span>${v.vehicle_id}</span>
            <span class="statusTag"><span class="statusDot" style="background:${getStatusColor(v.status)}"></span>${v.status}</span>
          </div>
          <div class="kv">
            <div class="k">位置</div><div class="v">${v.current_node}</div>
            <div class="k">子任务</div><div class="v">${v.active_subtask_id || "-"}</div>
            <div class="k">计划阶段</div><div class="v">${v.active_plan ? v.active_plan.phase : "-"}</div>
          </div>
        </div>
      `).join("");
      els.fleet.querySelectorAll("[data-vehicle-id]").forEach(node => {
        node.onclick = () => {
          App.selectedVehicleId = node.getAttribute("data-vehicle-id");
          renderFleet();
          renderSelectedVehicle();
        };
      });
    }

    function renderTasks() {
      const subtasks = [...(App.state?.subtasks || [])];
      subtasks.sort((a, b) => {
        const rank = x => (x.status === "DONE" ? 2 : x.status.startsWith("ENROUTE") ? 0 : 1);
        return rank(a) - rank(b) || (a.fire_time || "").localeCompare(b.fire_time || "");
      });
      els.taskMeta.textContent = `${subtasks.length} 项`;
      els.tasks.innerHTML = subtasks.map(t => {
        const progress = t.status === "DONE" ? 100 : t.status.startsWith("ENROUTE") ? 72 : t.status === "WAIT_PROPOSAL" ? 38 : 18;
        return `
          <div class="taskCard">
            <div class="cardTitle">
              <span>${t.subtask_id}</span>
              <span class="statusTag"><span class="statusDot" style="background:${getStatusColor(t.status)}"></span>${t.status}</span>
            </div>
            <div class="kv">
              <div class="k">弹种</div><div class="v">${t.ammo_type}</div>
              <div class="k">阶段</div><div class="v">${t.phase}</div>
              <div class="k">车辆</div><div class="v">${t.assigned_vehicle || "-"}</div>
              <div class="k">发射点</div><div class="v">${t.assigned_launch_point || "-"}</div>
              <div class="k">要求发射</div><div class="v">${fmtTime(t.fire_time)}</div>
            </div>
            <div class="taskBar"><div class="taskBarFill" style="width:${progress}%"></div></div>
          </div>
        `;
      }).join("");
    }

    function renderEvents() {
      const events = [...(App.state?.recent_events || [])].reverse();
      els.events.innerHTML = events.map(e => {
        let text = `${e.kind}`;
        if (e.kind === "subtask_assigned") text = `${e.subtask_id} 分配给 ${e.vehicle_id}，发射点 ${e.launch_point}`;
        else if (e.kind === "plan_execute") text = `${e.vehicle_id} 开始执行 ${e.subtask_id} / ${e.phase}，延迟 ${e.delay_sec}s`;
        else if (e.kind === "vehicle_event") text = `${e.vehicle_id} 上报 ${e.event}，子任务 ${e.subtask_id}`;
        else if (e.kind === "request_depot") text = `${e.vehicle_id} 发射后转入补给，目标 ${e.depot}`;
        else if (e.kind === "request_return") text = `${e.vehicle_id} 补给后返航，目标 ${e.home}`;
        else if (e.kind === "reservation_deadlock_risk") text = `${e.subtask_id} 在 ${e.phase} 阶段出现预约冲突退避`;
        else if (e.kind === "subtask_done") text = `${e.subtask_id} 完成，车辆 ${e.vehicle_id}`;
        else if (e.kind === "task_received") text = `收到任务 ${e.task_id}，共 ${e.launches} 个子项`;
        else if (e.kind === "subtask_reset") text = `${e.subtask_id} 被回收，原因 ${e.reason}`;
        else if (e.kind === "path_rejected") text = `${e.subtask_id} 路径被拒，原因 ${e.reason}`;
        return `
          <div class="eventCard">
            <div class="eventTime">${fmtTime(e.ts)}</div>
            <div class="eventBody">${text}</div>
          </div>
        `;
      }).join("");
    }

    function renderChrome(nowMs) {
      const state = App.state;
      if (!state) return;
      renderMetrics();
      renderSelectedVehicle();
      renderFleet();
      renderTasks();
      renderEvents();
      els.clock.textContent = fmtTime(state.server_time);
      els.clockMeta.textContent = `刷新于 ${new Date(App.lastFetchMs).toLocaleTimeString("zh-CN", { hour12: false })}`;
      const points = state.points || {};
      els.mapMeta.textContent =
        `节点 ${state.map.nodes.length} · 路段 ${state.map.edges.length} · 隐蔽点 ${(points.hide_points || []).length} · 发射点 ${(points.launch_points || []).length} · 贮备库 ${(points.depots || []).length}`;
    }

    async function refreshState() {
      try {
        const resp = await fetch("/api/state", { cache: "no-store" });
        const data = await resp.json();
        App.state = data;
        App.lastFetchMs = Date.now();
        ensureSelection();
        renderChrome(App.lastFetchMs);
      } catch (err) {
      }
    }

    function frame(ts) {
      App.nowMs = ts;
      renderMap(ts);
      requestAnimationFrame(frame);
    }

    window.addEventListener("resize", resizeCanvas);
    resizeCanvas();
    refreshState();
    setInterval(refreshState, 1000);
    requestAnimationFrame(frame);
  </script>
</body>
</html>
"""


class DashboardServer:
    def __init__(self, host: str, port: int, state_provider: Callable[[], Dict]) -> None:
        self.host = host
        self.port = port
        self.state_provider = state_provider
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path in ("/", "/index.html"):
                    body = DASHBOARD_HTML.encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if self.path == "/api/state":
                    payload = json.dumps(server.state_provider(), ensure_ascii=False).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                self.send_response(HTTPStatus.NOT_FOUND)
                self.end_headers()

            def log_message(self, fmt: str, *args) -> None:
                return

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
