#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


BLOCK_TERMS = [
    "发射",
    "贮备",
    "隐蔽",
    "launch",
    "fire",
    "depot",
    "hide",
    "yin",
    "yincang",
    "yinbi",
    "yin_bi",
    "zhu",
    "zhubei",
    "zhu_bei",
    "fashe",
    "fa_she",
    "DF",
    "26D",
]

BLOCK_REGEXES = [
    re.compile(re.escape(term), re.IGNORECASE)
    for term in ("发射", "贮备", "隐蔽", "26D")
]
BLOCK_REGEXES.extend(
    re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", re.IGNORECASE)
    for term in (
        "launch",
        "fire",
        "depot",
        "hide",
        "yin",
        "yincang",
        "yinbi",
        "yin_bi",
        "zhu",
        "zhubei",
        "zhu_bei",
        "fashe",
        "fa_she",
        "DF",
    )
)


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>路径动态播放</title>
<style>
:root{--bg:#f5f7fa;--panel:#fff;--line:#d6dde6;--text:#18212c;--muted:#667382;--accent:#1769aa}
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:Arial,"Microsoft YaHei",sans-serif}
body{display:grid;grid-template-rows:50px minmax(0,1fr) 78px}.bar{display:flex;align-items:center;gap:10px;padding:7px 12px;background:var(--panel);border-bottom:1px solid var(--line)}
.title{font-size:16px;font-weight:700}.btn,select{height:34px;border:1px solid #b8c2ce;background:#fff;border-radius:6px;padding:0 12px;color:var(--text);font-size:13px;cursor:pointer}.btn:hover{background:#edf3f8}.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.grow{flex:1}.status{font-size:12px;color:var(--muted)}
.stage{position:relative;min-height:0}canvas{display:block;width:100%;height:100%;background:#fff}.legend{position:absolute;top:12px;left:12px;display:flex;flex-wrap:wrap;gap:10px;padding:9px 11px;background:rgba(255,255,255,.94);border:1px solid var(--line);border-radius:6px;font-size:12px;pointer-events:none}.legend span{display:flex;align-items:center;gap:6px}.mk{width:14px;height:14px;display:inline-block}.mk.a{background:#f2b705;clip-path:polygon(50% 0,61% 35%,98% 35%,68% 57%,79% 94%,50% 72%,21% 94%,32% 57%,2% 35%,39% 35%)}.mk.b{background:#7b4bb7;transform:rotate(45deg)}.mk.c{background:#e47b1d;clip-path:polygon(50% 0,100% 100%,0 100%)}.mk.s{background:#2176ae}.mk.u{background:#d7263d;border-radius:50%}.mk.r{width:20px;height:2px;background:#aab2bb}.mk.t{width:20px;height:3px;background:#238b45}
.tip{position:absolute;display:none;pointer-events:none;max-width:250px;padding:7px 9px;background:rgba(20,25,30,.9);color:#fff;border-radius:5px;font-size:12px;line-height:1.45;white-space:pre-line}
.ctrl{display:grid;grid-template-columns:auto minmax(180px,1fr) auto;align-items:center;gap:12px;padding:10px 14px;background:var(--panel);border-top:1px solid var(--line)}.play{display:flex;gap:7px;align-items:center}.line{display:grid;grid-template-rows:24px 24px;min-width:0}.time{display:flex;justify-content:space-between;font-variant-numeric:tabular-nums;font-size:12px;color:var(--muted)}input[type=range]{width:100%;accent-color:var(--accent)}.help{text-align:right;font-size:11px;line-height:1.5;color:var(--muted)}
@media(max-width:760px){body{grid-template-rows:84px minmax(0,1fr) 108px}.bar{flex-wrap:wrap}.status{display:none}.ctrl{grid-template-columns:1fr}.help{display:none}.legend{right:8px;left:8px}}
</style>
</head>
<body>
<header class="bar"><div class="title">路径动态播放</div><button id="reset" class="btn">重置视图</button><div class="grow"></div><div id="status" class="status"></div></header>
<main id="stage" class="stage"><canvas id="map"></canvas><div class="legend"><span><i class="mk r"></i>路网</span><span><i class="mk a"></i>A类点</span><span><i class="mk b"></i>B类点</span><span><i class="mk c"></i>C类点</span><span><i class="mk s"></i>S类点</span><span><i class="mk t"></i>历史轨迹</span><span><i class="mk u"></i>移动单元</span></div><div id="tip" class="tip"></div></main>
<footer class="ctrl"><div class="play"><button id="play" class="btn primary">播放</button><button id="back" class="btn">回到开始</button><select id="speed"><option value="10">10倍</option><option value="25">25倍</option><option value="50" selected>50倍</option><option value="100">100倍</option><option value="200">200倍</option></select></div><div class="line"><div class="time"><span id="now"></span><span id="end"></span></div><input id="slider" type="range" min="0" max="1" step="0.1" value="0"></div><div class="help">拖动进度条调整时间<br>滚轮缩放，按住拖动画布</div></footer>
<script src="data.js"></script>
<script>
const D=window.ROUTE_DATA,cv=document.getElementById('map'),ctx=cv.getContext('2d'),stage=document.getElementById('stage'),slider=document.getElementById('slider'),playBtn=document.getElementById('play'),speedSel=document.getElementById('speed'),tip=document.getElementById('tip');
let current=D.time_min,playing=false,last=0,dpr=1,view=null,drag=null,hover=[];
function fullView(){const b=D.bounds,dx=b.max_lon-b.min_lon,dy=b.max_lat-b.min_lat;view={minLon:b.min_lon-dx*.025,maxLon:b.max_lon+dx*.025,minLat:b.min_lat-dy*.04,maxLat:b.max_lat+dy*.04}}
function resize(){const r=stage.getBoundingClientRect();dpr=Math.max(1,window.devicePixelRatio||1);cv.width=Math.round(r.width*dpr);cv.height=Math.round(r.height*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);draw()}
function M(){const w=cv.width/dpr,h=cv.height/dpr,pad=18,lat=(view.minLat+view.maxLat)/2*Math.PI/180,cos=Math.cos(lat),ww=(view.maxLon-view.minLon)*cos,wh=view.maxLat-view.minLat,scale=Math.min((w-2*pad)/ww,(h-2*pad)/wh),dw=ww*scale,dh=wh*scale;return{w,h,pad,cos,scale,ox:(w-dw)/2,oy:(h-dh)/2}}
function P(lon,lat,m){return[m.ox+(lon-view.minLon)*m.cos*m.scale,m.h-m.oy-(lat-view.minLat)*m.scale]}
function U(x,y,m){return[view.minLon+(x-m.ox)/(m.cos*m.scale),view.minLat+(m.h-m.oy-y)/m.scale]}
function vis(lon,lat){return lon>=view.minLon&&lon<=view.maxLon&&lat>=view.minLat&&lat<=view.maxLat}
function roads(m){ctx.strokeStyle='#aeb6bf';ctx.lineWidth=.55;ctx.globalAlpha=.72;ctx.beginPath();for(const line of D.roads){let s=false;for(const p of line){const q=P(p[0],p[1],m);if(!s){ctx.moveTo(q[0],q[1]);s=true}else ctx.lineTo(q[0],q[1])}}ctx.stroke();ctx.globalAlpha=1}
function star(x,y,r){ctx.beginPath();for(let i=0;i<10;i++){const a=-Math.PI/2+i*Math.PI/5,rr=i%2?r*.42:r,px=x+Math.cos(a)*rr,py=y+Math.sin(a)*rr;i?ctx.lineTo(px,py):ctx.moveTo(px,py)}ctx.closePath()}
function icon(k,x,y){ctx.save();ctx.lineWidth=1.4;ctx.strokeStyle='#fff';if(k==='a'){ctx.fillStyle='#f2b705';star(x,y,8)}else if(k==='b'){ctx.fillStyle='#7b4bb7';ctx.beginPath();ctx.moveTo(x,y-7);ctx.lineTo(x+7,y);ctx.lineTo(x,y+7);ctx.lineTo(x-7,y);ctx.closePath()}else if(k==='c'){ctx.fillStyle='#e47b1d';ctx.beginPath();ctx.moveTo(x,y-8);ctx.lineTo(x+8,y+7);ctx.lineTo(x-8,y+7);ctx.closePath()}else{ctx.fillStyle='#2176ae';ctx.beginPath();ctx.rect(x-6,y-6,12,12)}ctx.fill();ctx.stroke();ctx.restore()}
function points(m){for(const [k,rows] of Object.entries(D.points)){for(const p of rows){if(!vis(p.x,p.y))continue;const q=P(p.x,p.y,m);icon(k,q[0],q[1]);hover.push({x:q[0],y:q[1],text:p.label})}}}
function at(tr,t){const pts=tr.p;if(t<=pts[0][0])return{x:pts[0][1],y:pts[0][2],i:0};const n=pts.length;if(t>=pts[n-1][0])return{x:pts[n-1][1],y:pts[n-1][2],i:n-1};let lo=0,hi=n-1;while(lo+1<hi){const mid=(lo+hi)>>1;if(pts[mid][0]<=t)lo=mid;else hi=mid}const a=pts[lo],b=pts[hi],f=(t-a[0])/Math.max(1e-6,b[0]-a[0]);return{x:a[1]+(b[1]-a[1])*f,y:a[2]+(b[2]-a[2])*f,i:lo}}
function color(i){return`hsl(${(i*137.508)%360} 72% 42%)`}
function units(m){D.units.forEach((tr,i)=>{const s=at(tr,current),pts=tr.p,c=color(i);ctx.strokeStyle=c;ctx.globalAlpha=.62;ctx.lineWidth=1.7;ctx.beginPath();let st=false;for(let j=0;j<=s.i;j++){const q=P(pts[j][1],pts[j][2],m);if(!st){ctx.moveTo(q[0],q[1]);st=true}else ctx.lineTo(q[0],q[1])}if(s.i<pts.length-1){const q=P(s.x,s.y,m);st?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1])}ctx.stroke();ctx.globalAlpha=1;const q=P(s.x,s.y,m),n=pts[Math.min(s.i+1,pts.length-1)],p=pts[Math.max(0,s.i)],a=Math.atan2(-(n[2]-p[2]),(n[1]-p[1])*m.cos);ctx.save();ctx.translate(q[0],q[1]);ctx.rotate(a);ctx.fillStyle=c;ctx.strokeStyle='#fff';ctx.lineWidth=1.4;ctx.beginPath();ctx.moveTo(10,0);ctx.lineTo(-6,-6);ctx.lineTo(-4,0);ctx.lineTo(-6,6);ctx.closePath();ctx.fill();ctx.stroke();ctx.restore();hover.push({x:q[0],y:q[1],text:tr.label})})}
function draw(){if(!view)return;const m=M();ctx.clearRect(0,0,m.w,m.h);ctx.fillStyle='#fff';ctx.fillRect(0,0,m.w,m.h);hover=[];roads(m);units(m);points(m)}
function fmt(t){t=Math.max(0,t);const h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=Math.floor(t%60);return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0')}
function labels(){slider.value=current;document.getElementById('now').textContent='当前 '+fmt(current)+' ('+current.toFixed(1)+'s)';document.getElementById('end').textContent='结束 '+fmt(D.time_max);document.getElementById('status').textContent=`${D.units.length} 个移动单元 | ${D.roads.length} 条路网边`;draw()}
function frame(ts){if(playing){if(!last)last=ts;current+=(ts-last)/1000*Number(speedSel.value);last=ts;if(current>=D.time_max){current=D.time_max;playing=false;playBtn.textContent='播放'}labels()}requestAnimationFrame(frame)}
playBtn.onclick=()=>{playing=!playing;if(playing&&current>=D.time_max)current=D.time_min;last=0;playBtn.textContent=playing?'暂停':'播放';labels()};document.getElementById('back').onclick=()=>{playing=false;current=D.time_min;playBtn.textContent='播放';labels()};document.getElementById('reset').onclick=()=>{fullView();draw()};slider.oninput=()=>{current=Number(slider.value);last=0;labels()};
cv.addEventListener('wheel',e=>{e.preventDefault();const m=M(),r=cv.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top,[lon,lat]=U(x,y,m),f=e.deltaY>0?1.18:.84;view.minLon=lon+(view.minLon-lon)*f;view.maxLon=lon+(view.maxLon-lon)*f;view.minLat=lat+(view.minLat-lat)*f;view.maxLat=lat+(view.maxLat-lat)*f;draw()},{passive:false});
cv.onmousedown=e=>{drag={x:e.clientX,y:e.clientY,view:{...view}}};window.onmouseup=()=>drag=null;window.onmousemove=e=>{if(drag){const m=M(),dx=(e.clientX-drag.x)/(m.cos*m.scale),dy=(e.clientY-drag.y)/m.scale;view={minLon:drag.view.minLon-dx,maxLon:drag.view.maxLon-dx,minLat:drag.view.minLat+dy,maxLat:drag.view.maxLat+dy};draw();return}const r=cv.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top;let best=null,d=13;for(const it of hover){const dd=Math.hypot(x-it.x,y-it.y);if(dd<d){d=dd;best=it}}if(best){tip.style.display='block';tip.textContent=best.text;tip.style.left=Math.min(r.width-260,x+14)+'px';tip.style.top=Math.min(r.height-100,y+14)+'px'}else tip.style.display='none'};
window.addEventListener('resize',resize);fullView();slider.min=D.time_min;slider.max=D.time_max;slider.value=current;resize();labels();requestAnimationFrame(frame);
</script>
</body></html>"""


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_trajectories_from_capture(capture_root: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    files = sorted(capture_root.glob("scheduler/*/send/*DISPATCH_TRAJECTORY_BUNDLE.json"))
    time_rules = load_time_rules_from_capture(capture_root)
    seen = set()
    schedulers: Dict[str, int] = {}
    for path in files:
        message = read_json(path)
        payload = message.get("payload") or {}
        scheduler_id = path.parent.parent.name
        for row in payload.get("trajectories") or []:
            if not isinstance(row, dict):
                continue
            vehicle_id = str(row.get("vehicle_id") or payload.get("vehicle_id") or "")
            key = (vehicle_id, scheduler_id)
            if key in seen:
                continue
            seen.add(key)
            row = dict(row)
            row["_display_fire_time"] = (time_rules.get(scheduler_id) or {}).get("fire_time")
            row["_scheduler_id"] = scheduler_id
            rows.append(row)
            schedulers[scheduler_id] = schedulers.get(scheduler_id, 0) + 1
    rows.sort(key=lambda item: int(item.get("vehicle_id") or item.get("port") or 0))
    meta = {
        "mode": "capture_final_send",
        "file_count": len(files),
        "scheduler_counts": schedulers,
    }
    return rows, meta


def load_time_rules_from_capture(capture_root: Path) -> Dict[str, Dict[str, Any]]:
    rules: Dict[str, Dict[str, Any]] = {}
    for path in sorted(capture_root.glob("scheduler/*/send/*TIME_BACKPLAN_CONTEXT.json")):
        payload = (read_json(path).get("payload") or {})
        rules[path.parent.parent.name] = {
            "fire_time": payload.get("fire_time"),
            "launch_prepare_time": payload.get("launch_prepare_time"),
            "launch_standby_time": payload.get("launch_standby_time"),
        }
    return rules


def load_vehicle_paths_from_capture(capture_root: Path | None, message_name: str) -> Dict[str, Any]:
    if not capture_root or not capture_root.exists():
        return {}
    out: Dict[str, Any] = {}
    for path in sorted(capture_root.glob(f"vehicle/*/send/*{message_name}.json")):
        payload = read_json(path).get("payload") or {}
        vehicle_id = str(payload.get("vehicle_id") or path.parent.parent.name)
        out[vehicle_id] = payload
    return out


def load_trajectories(bundle_path: Path, capture_root: Path | None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if capture_root and capture_root.exists():
        rows, meta = load_trajectories_from_capture(capture_root)
        if rows:
            return rows, meta
    bundle = read_json(bundle_path)
    return list(bundle.get("trajectories", [])), {"mode": "bundle_file", "file": str(bundle_path)}


def clean_text(value: Any) -> str:
    text = str(value or "")
    for pattern in BLOCK_TERMS:
        text = re.sub(re.escape(pattern), "", text, flags=re.IGNORECASE)
    return text


def point_key(lon: float, lat: float) -> Tuple[int, int]:
    return round(lon * 1_000_000), round(lat * 1_000_000)


def add_unique(points: List[Dict[str, Any]], seen: set, lon: float, lat: float, label: str) -> None:
    key = point_key(lon, lat)
    if key in seen:
        return
    seen.add(key)
    points.append({"x": round(lon, 7), "y": round(lat, 7), "label": label})


def alpha_code(index: int, prefix: str) -> str:
    n = max(0, index - 1)
    chars = []
    for _ in range(3):
        chars.append(chr(ord("A") + (n % 26)))
        n //= 26
    return prefix + "".join(reversed(chars))


def empty_points() -> Dict[str, List[Dict[str, Any]]]:
    return {"a": [], "b": [], "c": [], "s": []}


def collect_points_from_capture(capture_root: Path | None) -> Dict[str, List[Dict[str, Any]]] | None:
    if not capture_root or not capture_root.exists():
        return None
    mapping = {
        "FA_SHE_DIAN": ("a", "A"),
        "YIN_BI_DIAN": ("b", "B"),
        "ZHU_BEI_DIAN": ("c", "C"),
        "VEHICLE_DIAN": ("s", "S"),
    }
    points = empty_points()
    seen = {key: set() for key in points}
    matched = False
    for message_name, (kind, prefix) in mapping.items():
        for path in sorted(capture_root.glob(f"scheduler/*/recv/*{message_name}.json")):
            payload = (read_json(path).get("payload") or {})
            rows = payload.get("data") or payload.get("rows") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("lon") is None or row.get("lat") is None:
                    continue
                matched = True
                add_unique(
                    points[kind],
                    seen[kind],
                    float(row["lon"]),
                    float(row["lat"]),
                    alpha_code(len(points[kind]) + 1, prefix),
                )
    return points if matched else None


def shifted_display_series(row: Dict[str, Any], raw_points: List[Dict[str, Any]]) -> List[List[float]]:
    first_post_start = None
    for segment in row.get("post_fire_segments") or []:
        if isinstance(segment, dict) and segment.get("start_at") is not None:
            first_post_start = float(segment["start_at"])
            break
    true_fire_time = row.get("_display_fire_time")
    try:
        true_fire = float(true_fire_time)
    except (TypeError, ValueError):
        true_fire = None
    delta = 0.0
    if first_post_start is not None and true_fire is not None and true_fire > first_post_start:
        delta = true_fire - first_post_start
    series: List[List[float]] = []
    hold_inserted = False
    for point in raw_points:
        if point.get("lon") is None or point.get("lat") is None or point.get("time") is None:
            continue
        time_value = float(point["time"])
        lon = round(float(point["lon"]), 7)
        lat = round(float(point["lat"]), 7)
        if delta > 0 and first_post_start is not None and time_value >= first_post_start:
            if not hold_inserted:
                series.append([round(first_post_start, 3), lon, lat])
                hold_inserted = True
            time_value += delta
        series.append([round(time_value, 3), lon, lat])
    series.sort(key=lambda item: item[0])
    return series


def normalise_points(raw_points: List[Dict[str, Any]]) -> List[List[float]]:
    series: List[List[float]] = []
    for point in raw_points:
        if point.get("lon") is None or point.get("lat") is None or point.get("time") is None:
            continue
        series.append(
            [
                round(float(point["time"]), 3),
                round(float(point["lon"]), 7),
                round(float(point["lat"]), 7),
            ]
        )
    series.sort(key=lambda item: item[0])
    return series


def select_prelaunch_series(row: Dict[str, Any], candidate_payloads: Dict[str, Any]) -> List[List[float]]:
    vehicle_id = str(row.get("vehicle_id") or row.get("port") or "")
    payload = candidate_payloads.get(vehicle_id) or {}
    paths = payload.get("paths") or []
    launch_node = str(row.get("launch_node") or "")
    fire_point_id = str(row.get("fire_point_id") or "")
    selected = None
    for path in paths:
        if not isinstance(path, dict):
            continue
        same_launch = launch_node and str(path.get("launch_node") or "") == launch_node
        same_public = fire_point_id and str(path.get("fire_point_id") or "") == fire_point_id
        if same_launch or same_public:
            selected = path
            break
    if selected is None and paths:
        selected = min(
            (path for path in paths if isinstance(path, dict)),
            key=lambda item: int(item.get("rank", 999999) or 999999),
            default=None,
        )
    if selected is None:
        return []
    return normalise_points([point for point in (selected.get("path_points") or []) if isinstance(point, dict)])


def postfire_series(row: Dict[str, Any], post_payloads: Dict[str, Any]) -> List[List[float]]:
    vehicle_id = str(row.get("vehicle_id") or row.get("port") or "")
    payload = post_payloads.get(vehicle_id) or {}
    points = payload.get("path_points") or row.get("path_points") or []
    return normalise_points([point for point in points if isinstance(point, dict)])


def rebuilt_display_series(
    row: Dict[str, Any],
    candidate_payloads: Dict[str, Any],
    post_payloads: Dict[str, Any],
) -> List[List[float]]:
    pre = select_prelaunch_series(row, candidate_payloads)
    if len(pre) < 2:
        return shifted_display_series(row, [point for point in (row.get("path_points") or []) if isinstance(point, dict)])
    post = postfire_series(row, post_payloads)
    try:
        true_fire = float(row.get("_display_fire_time"))
    except (TypeError, ValueError):
        true_fire = None
    if not post or true_fire is None:
        return pre
    first_post_time = post[0][0]
    delta = true_fire - first_post_time
    merged = list(pre)
    last_pre = pre[-1]
    if true_fire > last_pre[0] + 1e-6:
        merged.append([round(true_fire, 3), last_pre[1], last_pre[2]])
    elif true_fire < last_pre[0] - 1e-6:
        delta += last_pre[0] - true_fire
    for index, point in enumerate(post):
        shifted_time = round(point[0] + delta, 3)
        if index == 0:
            continue
        merged.append([shifted_time, point[1], point[2]])
    merged.sort(key=lambda item: item[0])
    return merged


def collect_units(
    trajectories: List[Dict[str, Any]],
    capture_points: Dict[str, List[Dict[str, Any]]] | None = None,
    candidate_payloads: Dict[str, Any] | None = None,
    post_payloads: Dict[str, Any] | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    units: List[Dict[str, Any]] = []
    points = capture_points if capture_points is not None else empty_points()
    seen = {key: set() for key in points}
    for key, rows in points.items():
        for point in rows:
            seen[key].add(point_key(point["x"], point["y"]))
    for idx, row in enumerate(trajectories, start=1):
        if not isinstance(row, dict):
            continue
        series = rebuilt_display_series(row, candidate_payloads or {}, post_payloads or {})
        if len(series) < 2:
            continue
        label = alpha_code(idx, "U")
        units.append({"id": label, "label": label, "p": series})
        if capture_points is None:
            add_unique(points["s"], seen["s"], series[0][1], series[0][2], alpha_code(len(points["s"]) + 1, "S"))
        end_before_aux = None
        for seg in row.get("post_fire_segments") or []:
            if isinstance(seg, dict) and seg.get("start_at") is not None:
                end_before_aux = float(seg["start_at"])
                break
        if end_before_aux is None:
            end_before_aux = float(row.get("depot_arrival_time") or series[-1][0])
        target = min(series, key=lambda item: abs(item[0] - end_before_aux))
        if capture_points is None:
            add_unique(points["a"], seen["a"], target[1], target[2], alpha_code(len(points["a"]) + 1, "A"))
        if row.get("hide_selected"):
            wait_start = row.get("wait_start_time")
            if wait_start is None:
                wait_start = series[0][0] + float(row.get("wait_seconds", 0.0) or 0.0)
            wait = min(series, key=lambda item: abs(item[0] - float(wait_start)))
            if capture_points is None:
                add_unique(points["b"], seen["b"], wait[1], wait[2], alpha_code(len(points["b"]) + 1, "B"))
        aux_time = row.get("depot_arrival_time")
        if aux_time is not None:
            aux = min(series, key=lambda item: abs(item[0] - float(aux_time)))
            if capture_points is None:
                add_unique(points["c"], seen["c"], aux[1], aux[2], alpha_code(len(points["c"]) + 1, "C"))
    return units, points


def all_coords(units: Iterable[Dict[str, Any]], points: Dict[str, List[Dict[str, Any]]]) -> Iterable[Tuple[float, float]]:
    for unit in units:
        for _t, lon, lat in unit["p"]:
            yield lon, lat
    for rows in points.values():
        for point in rows:
            yield point["x"], point["y"]


def road_lines(path: Path, bounds: Dict[str, float], margin_ratio: float = 0.08) -> List[List[List[float]]]:
    graph = read_json(path)
    dlon = bounds["max_lon"] - bounds["min_lon"]
    dlat = bounds["max_lat"] - bounds["min_lat"]
    min_lon = bounds["min_lon"] - dlon * margin_ratio
    max_lon = bounds["max_lon"] + dlon * margin_ratio
    min_lat = bounds["min_lat"] - dlat * margin_ratio
    max_lat = bounds["max_lat"] + dlat * margin_ratio
    nodes = {
        str(node.get("id")): (node.get("lon"), node.get("lat"))
        for node in graph.get("nodes", [])
        if isinstance(node, dict)
    }
    out: List[List[List[float]]] = []
    seen = set()
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        src = str(edge.get("from") or "")
        dst = str(edge.get("to") or "")
        key = tuple(sorted((src, dst)))
        if key in seen:
            continue
        seen.add(key)
        geom = edge.get("geometry") or []
        line = []
        for point in geom:
            if isinstance(point, dict) and point.get("lon") is not None and point.get("lat") is not None:
                line.append([float(point["lon"]), float(point["lat"])])
        if len(line) < 2:
            a = nodes.get(src)
            b = nodes.get(dst)
            if a and b and None not in a and None not in b:
                line = [[float(a[0]), float(a[1])], [float(b[0]), float(b[1])]]
        if len(line) < 2:
            continue
        if not any(min_lon <= lon <= max_lon and min_lat <= lat <= max_lat for lon, lat in line):
            continue
        out.append([[round(lon, 6), round(lat, 6)] for lon, lat in line])
    return out


def graph_bounds(path: Path) -> Tuple[float, float, float, float]:
    graph = read_json(path)
    lons = []
    lats = []
    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue
        if node.get("lon") is None or node.get("lat") is None:
            continue
        lons.append(float(node["lon"]))
        lats.append(float(node["lat"]))
    if not lons:
        return (math.inf, -math.inf, math.inf, -math.inf)
    return min(lons), max(lons), min(lats), max(lats)


def overlap_score(bounds: Dict[str, float], graph_box: Tuple[float, float, float, float]) -> float:
    g_min_lon, g_max_lon, g_min_lat, g_max_lat = graph_box
    min_lon = max(bounds["min_lon"], g_min_lon)
    max_lon = min(bounds["max_lon"], g_max_lon)
    min_lat = max(bounds["min_lat"], g_min_lat)
    max_lat = min(bounds["max_lat"], g_max_lat)
    if max_lon <= min_lon or max_lat <= min_lat:
        return 0.0
    return (max_lon - min_lon) * (max_lat - min_lat)


def choose_graphs(explicit_graph: Path | None, bounds: Dict[str, float]) -> List[Path]:
    candidates: List[Path] = []
    if explicit_graph is not None:
        candidates.append(explicit_graph)
    candidates.extend(sorted(Path("result/theaters").glob("*/data/road_graph_shp_demo.json")))
    candidates.append(Path("result/data/road_graph_shp_demo.json"))
    chosen: List[Path] = []
    seen = set()
    seen_box = set()
    for path in candidates:
        if not path.exists() or path in seen:
            continue
        seen.add(path)
        box = graph_bounds(path)
        box_key = tuple(round(value, 5) for value in box)
        if box_key in seen_box:
            continue
        seen_box.add(box_key)
        score = overlap_score(bounds, graph_bounds(path))
        if score > 0:
            chosen.append(path)
    if chosen:
        return chosen
    return [explicit_graph] if explicit_graph and explicit_graph.exists() else []


def contains_blocked(path: Path) -> List[str]:
    bad = []
    text = path.read_text(encoding="utf-8", errors="ignore")
    for regex in BLOCK_REGEXES:
        if regex.search(text):
            bad.append(regex.pattern)
    return bad


def main() -> int:
    parser = argparse.ArgumentParser(description="Package a neutral route playback bundle.")
    parser.add_argument("--bundle", type=Path, default=Path("result/latest_dispatch_trajectory_bundle.json"))
    parser.add_argument("--capture-root", type=Path, default=Path("result/message_capture"))
    parser.add_argument("--graph", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=Path("deliverables/client_route_player"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.out_dir.exists() and args.force:
        shutil.rmtree(args.out_dir)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    trajectories, source_meta = load_trajectories(args.bundle, args.capture_root)
    capture_points = collect_points_from_capture(args.capture_root)
    candidate_payloads = load_vehicle_paths_from_capture(args.capture_root, "VEHICLE_CANDIDATE_PATH_RESULT")
    post_payloads = load_vehicle_paths_from_capture(args.capture_root, "VEHICLE_POST_FIRE_PATH_RESULT")
    units, points = collect_units(trajectories, capture_points, candidate_payloads, post_payloads)
    coords = list(all_coords(units, points))
    if not coords:
        raise RuntimeError("no usable route points found")
    bounds = {
        "min_lon": min(lon for lon, _lat in coords),
        "max_lon": max(lon for lon, _lat in coords),
        "min_lat": min(lat for _lon, lat in coords),
        "max_lat": max(lat for _lon, lat in coords),
    }
    graph_paths = choose_graphs(args.graph, bounds)
    roads: List[List[List[float]]] = []
    for graph_path in graph_paths:
        roads.extend(road_lines(graph_path, bounds))
    times = [p[0] for unit in units for p in unit["p"]]
    data = {
        "version": 1,
        "time_min": min(times),
        "time_max": max(times),
        "bounds": bounds,
        "roads": roads,
        "points": points,
        "units": units,
    }
    data_text = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    data_path = args.out_dir / "data.json"
    js_data_path = args.out_dir / "data.js"
    road_path = args.out_dir / "road_network.json"
    html_path = args.out_dir / "player.html"
    readme_path = args.out_dir / "README.txt"
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    js_data_path.write_text("window.ROUTE_DATA=" + data_text + ";\n", encoding="utf-8")
    road_path.write_text(
        json.dumps(
            {
                "bounds": bounds,
                "roads": roads,
                "road_count": len(roads),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    html_path.write_text(HTML, encoding="utf-8")
    readme_path.write_text(
        "打开 player.html 即可播放。\n"
        "默认 50 倍速，可拖动进度条、滚轮缩放、按住地图拖动。\n"
        "A/B/C/S 为脱敏点位类别，U 为脱敏移动单元编号。\n"
        "播放器所需轨迹和路网已经包含在本目录内，可离线打开。\n",
        encoding="utf-8",
    )

    bad_files = {}
    for path in (data_path, js_data_path, road_path, html_path, readme_path):
        bad = contains_blocked(path)
        if bad:
            bad_files[str(path)] = bad
    manifest = {
        "html": str(html_path),
        "data": str(data_path),
        "script_data": str(js_data_path),
        "road_network": str(road_path),
        "unit_count": len(units),
        "road_count": len(roads),
        "graphs": [str(path) for path in graph_paths],
        "source": source_meta,
        "point_counts": {key: len(value) for key, value in points.items()},
        "blocked_scan": bad_files,
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if bad_files:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
