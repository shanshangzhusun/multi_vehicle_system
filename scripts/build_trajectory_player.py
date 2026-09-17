#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REGIONS = [
    {
        "id": "scheduler_001",
        "label": "一区",
        "task_id": "task_10_3600",
        "graph": "result/theaters/henan/data/road_graph_shp_demo.json",
    },
    {
        "id": "scheduler_002",
        "label": "二区",
        "task_id": "task_10_3700",
        "graph": "result/theaters/guangdong/data/road_graph_shp_demo.json",
    },
]


def read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return value


def latest_capture(result_dir: Path, scheduler_id: str, msg_type: str) -> Optional[Path]:
    paths = sorted(
        (result_dir / "message_capture" / "scheduler" / scheduler_id / "recv").glob(f"*_{msg_type}.json")
    )
    return paths[-1] if paths else None


def point_rows(result_dir: Path, scheduler_id: str, msg_type: str) -> List[Dict[str, Any]]:
    path = latest_capture(result_dir, scheduler_id, msg_type)
    if path is None:
        return []
    payload = read_json(path).get("payload") or {}
    data = payload.get("data")
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    output = []
    for row in rows:
        if not isinstance(row, dict) or row.get("lon") is None or row.get("lat") is None:
            continue
        output.append(
            {
                "id": str(row.get("vehicle_id") or row.get("index") or row.get("name") or ""),
                "name": str(row.get("name") or ""),
                "lon": round(float(row["lon"]), 7),
                "lat": round(float(row["lat"]), 7),
            }
        )
    return output


def road_lines(path: Path) -> List[List[List[float]]]:
    graph = read_json(path)
    nodes = {
        str(node.get("id")): (node.get("lon"), node.get("lat"))
        for node in graph.get("nodes", [])
        if isinstance(node, dict)
    }
    lines: List[List[List[float]]] = []
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
        geometry = edge.get("geometry") or []
        line = [
            [round(float(point["lon"]), 6), round(float(point["lat"]), 6)]
            for point in geometry
            if isinstance(point, dict) and point.get("lon") is not None and point.get("lat") is not None
        ]
        if len(line) < 2:
            a = nodes.get(src)
            b = nodes.get(dst)
            if a and b and None not in a and None not in b:
                line = [
                    [round(float(a[0]), 6), round(float(a[1]), 6)],
                    [round(float(b[0]), 6), round(float(b[1]), 6)],
                ]
        if len(line) >= 2:
            lines.append(line)
    return lines


def trajectory_rows(bundle_path: Path) -> List[Dict[str, Any]]:
    bundle = read_json(bundle_path)
    output = []
    for row in bundle.get("trajectories", []):
        if not isinstance(row, dict):
            continue
        points = []
        for point in row.get("path_points", []):
            if not isinstance(point, dict):
                continue
            if point.get("lon") is None or point.get("lat") is None or point.get("time") is None:
                continue
            points.append(
                [
                    round(float(point["time"]), 3),
                    round(float(point["lon"]), 7),
                    round(float(point["lat"]), 7),
                ]
            )
        if not points:
            continue
        output.append(
            {
                "vehicle_id": str(row.get("vehicle_id") or row.get("port") or ""),
                "fire_point": str(row.get("fire_point_id") or ""),
                "hide_point": str(row.get("hide_point") or ""),
                "depot": str(row.get("depot_id") or ""),
                "points": points,
            }
        )
    return output


def all_coordinates(region: Dict[str, Any]) -> Iterable[Tuple[float, float]]:
    for line in region["roads"]:
        for lon, lat in line:
            yield lon, lat
    for rows in region["special"].values():
        for row in rows:
            yield row["lon"], row["lat"]
    for trajectory in region["trajectories"]:
        for _time, lon, lat in trajectory["points"]:
            yield lon, lat


def build_region(root: Path, result_dir: Path, spec: Dict[str, str], replay_dir: Path) -> Dict[str, Any]:
    scheduler_id = spec["id"]
    task_id = spec["task_id"]
    bundle_path = replay_dir / scheduler_id / f"{task_id}_dispatch_trajectory_bundle.json"
    region = {
        "id": scheduler_id,
        "label": spec["label"],
        "task_id": task_id,
        "roads": road_lines(root / spec["graph"]),
        "special": {
            "launch": point_rows(result_dir, scheduler_id, "FA_SHE_DIAN"),
            "hide": point_rows(result_dir, scheduler_id, "YIN_BI_DIAN"),
            "depot": point_rows(result_dir, scheduler_id, "ZHU_BEI_DIAN"),
            "start": point_rows(result_dir, scheduler_id, "VEHICLE_DIAN"),
        },
        "trajectories": trajectory_rows(bundle_path),
    }
    times = [point[0] for trajectory in region["trajectories"] for point in trajectory["points"]]
    coords = list(all_coordinates(region))
    region["time_min"] = min(times)
    region["time_max"] = max(times)
    region["bounds"] = {
        "min_lon": min(point[0] for point in coords),
        "max_lon": max(point[0] for point in coords),
        "min_lat": min(point[1] for point in coords),
        "max_lat": max(point[1] for point in coords),
    }
    return region


HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>最终轨迹动态播放器</title>
<style>
:root{--bg:#f4f6f8;--panel:#fff;--line:#d8dee6;--text:#17202a;--muted:#657180;--accent:#1769aa}
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:Arial,"Microsoft YaHei",sans-serif}
body{display:grid;grid-template-rows:48px minmax(0,1fr) 72px}.topbar{display:flex;align-items:center;gap:10px;padding:6px 12px;background:var(--panel);border-bottom:1px solid var(--line)}
.title{font-size:16px;font-weight:700;margin-right:8px}.tabs{display:flex;gap:4px}.button,select{height:34px;border:1px solid #b8c2ce;background:#fff;border-radius:6px;padding:0 12px;color:var(--text);font-size:13px;cursor:pointer}.button:hover{background:#edf3f8}.button.active,.button.primary{background:var(--accent);border-color:var(--accent);color:#fff}.spacer{flex:1}.status{font-size:12px;color:var(--muted)}
.stage{position:relative;min-height:0}canvas{display:block;width:100%;height:100%;background:#fff}.legend{position:absolute;top:12px;left:12px;display:flex;flex-wrap:wrap;gap:9px;padding:8px 10px;background:rgba(255,255,255,.92);border:1px solid var(--line);border-radius:6px;font-size:12px;pointer-events:none}.legend span{display:flex;align-items:center;gap:5px}.mark{width:11px;height:11px;display:inline-block}.launch{background:#f2b705;clip-path:polygon(50% 0,61% 35%,98% 35%,68% 57%,79% 94%,50% 72%,21% 94%,32% 57%,2% 35%,39% 35%)}.hide{background:#7b4bb7;transform:rotate(45deg)}.depot{background:#e47b1d;clip-path:polygon(50% 0,100% 100%,0 100%)}.start{background:#2176ae}.moving{background:#d7263d;border-radius:50%}.road{width:18px;height:2px;background:#aab2bb}.trail{width:18px;height:3px;background:#238b45}
.tooltip{position:absolute;display:none;pointer-events:none;max-width:260px;padding:7px 9px;background:rgba(20,25,30,.9);color:#fff;border-radius:5px;font-size:12px;line-height:1.45;white-space:pre-line}
.controls{display:grid;grid-template-columns:auto minmax(180px,1fr) auto;align-items:center;gap:12px;padding:10px 14px;background:var(--panel);border-top:1px solid var(--line)}.playgroup{display:flex;gap:7px;align-items:center}.timeline{display:grid;grid-template-rows:22px 22px;min-width:0}.time-row{display:flex;justify-content:space-between;font-variant-numeric:tabular-nums;font-size:12px;color:var(--muted)}input[type=range]{width:100%;accent-color:var(--accent)}.help{text-align:right;font-size:11px;line-height:1.5;color:var(--muted)}
@media(max-width:760px){body{grid-template-rows:88px minmax(0,1fr) 108px}.topbar{flex-wrap:wrap}.title{width:100%}.status{display:none}.controls{grid-template-columns:1fr}.help{display:none}.legend{right:8px;left:8px}.button{padding:0 9px}}
</style>
</head>
<body>
<header class="topbar"><div class="title">最终轨迹动态播放器</div><div id="tabs" class="tabs"></div><button id="reset" class="button">重置视图</button><div class="spacer"></div><div id="status" class="status"></div></header>
<main id="stage" class="stage"><canvas id="map"></canvas><div class="legend"><span><i class="mark road"></i>路网</span><span><i class="mark launch"></i>发射点</span><span><i class="mark hide"></i>隐蔽点</span><span><i class="mark depot"></i>贮备库</span><span><i class="mark start"></i>车辆起点</span><span><i class="mark trail"></i>已行驶路线</span><span><i class="mark moving"></i>执行车辆</span></div><div id="tooltip" class="tooltip"></div></main>
<footer class="controls"><div class="playgroup"><button id="play" class="button primary">播放</button><button id="restart" class="button">回到开始</button><select id="speed"><option value="10">10倍</option><option value="25">25倍</option><option value="50" selected>50倍</option><option value="100">100倍</option><option value="200">200倍</option></select></div><div class="timeline"><div class="time-row"><span id="currentTime"></span><span id="endTime"></span></div><input id="slider" type="range" min="0" max="1" step="0.1" value="0"></div><div class="help">拖动进度条调整时间<br>滚轮缩放，按住拖动画布</div></footer>
<script id="dataset" type="application/json">__DATA__</script>
<script>
const DATA=JSON.parse(document.getElementById('dataset').textContent);const canvas=document.getElementById('map'),ctx=canvas.getContext('2d'),stage=document.getElementById('stage'),slider=document.getElementById('slider'),playBtn=document.getElementById('play'),speedSel=document.getElementById('speed'),tooltip=document.getElementById('tooltip');
let regionIndex=0,region=DATA.regions[0],current=region.time_min,playing=false,lastFrame=0,dpr=1,view=null,drag=null,hoverItems=[];
const tabs=document.getElementById('tabs');DATA.regions.forEach((r,i)=>{const b=document.createElement('button');b.className='button'+(i===0?' active':'');b.textContent=r.label+' '+r.trajectories.length+'辆';b.onclick=()=>switchRegion(i);tabs.appendChild(b)});
function fullView(){const b=region.bounds,dx=b.max_lon-b.min_lon,dy=b.max_lat-b.min_lat;view={minLon:b.min_lon-dx*.025,maxLon:b.max_lon+dx*.025,minLat:b.min_lat-dy*.04,maxLat:b.max_lat+dy*.04}}
function switchRegion(i){regionIndex=i;region=DATA.regions[i];current=region.time_min;playing=false;lastFrame=0;fullView();slider.min=region.time_min;slider.max=region.time_max;slider.value=current;[...tabs.children].forEach((b,j)=>b.classList.toggle('active',j===i));playBtn.textContent='播放';resize();updateLabels()}
function resize(){const r=stage.getBoundingClientRect();dpr=Math.max(1,window.devicePixelRatio||1);canvas.width=Math.round(r.width*dpr);canvas.height=Math.round(r.height*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);draw()}
function metrics(){const w=canvas.width/dpr,h=canvas.height/dpr,pad=18,lat=(view.minLat+view.maxLat)/2*Math.PI/180,cos=Math.cos(lat),worldW=(view.maxLon-view.minLon)*cos,worldH=view.maxLat-view.minLat,scale=Math.min((w-2*pad)/worldW,(h-2*pad)/worldH),drawW=worldW*scale,drawH=worldH*scale;return{w,h,pad,cos,scale,ox:(w-drawW)/2,oy:(h-drawH)/2}}
function project(lon,lat,m){return[m.ox+(lon-view.minLon)*m.cos*m.scale,m.h-m.oy-(lat-view.minLat)*m.scale]}
function unproject(x,y,m){return[view.minLon+(x-m.ox)/(m.cos*m.scale),view.minLat+(m.h-m.oy-y)/m.scale]}
function visible(lon,lat){return lon>=view.minLon&&lon<=view.maxLon&&lat>=view.minLat&&lat<=view.maxLat}
function drawRoads(m){ctx.strokeStyle='#aeb6bf';ctx.lineWidth=.55;ctx.globalAlpha=.72;ctx.beginPath();for(const line of region.roads){let started=false;for(const p of line){if(!started){const q=project(p[0],p[1],m);ctx.moveTo(q[0],q[1]);started=true}else{const q=project(p[0],p[1],m);ctx.lineTo(q[0],q[1])}}}ctx.stroke();ctx.globalAlpha=1}
function star(x,y,r){ctx.beginPath();for(let i=0;i<10;i++){const a=-Math.PI/2+i*Math.PI/5,rr=i%2? r*.42:r;const px=x+Math.cos(a)*rr,py=y+Math.sin(a)*rr;i?ctx.lineTo(px,py):ctx.moveTo(px,py)}ctx.closePath()}
function specialIcon(kind,x,y){ctx.save();ctx.lineWidth=1.1;ctx.strokeStyle='#fff';if(kind==='launch'){ctx.fillStyle='#f2b705';star(x,y,6)}else if(kind==='hide'){ctx.fillStyle='#7b4bb7';ctx.beginPath();ctx.moveTo(x,y-5);ctx.lineTo(x+5,y);ctx.lineTo(x,y+5);ctx.lineTo(x-5,y);ctx.closePath()}else if(kind==='depot'){ctx.fillStyle='#e47b1d';ctx.beginPath();ctx.moveTo(x,y-6);ctx.lineTo(x+6,y+5);ctx.lineTo(x-6,y+5);ctx.closePath()}else{ctx.fillStyle='#2176ae';ctx.beginPath();ctx.rect(x-4.5,y-4.5,9,9)}ctx.fill();ctx.stroke();ctx.restore()}
function drawSpecial(m){for(const [kind,rows] of Object.entries(region.special)){for(const p of rows){if(!visible(p.lon,p.lat))continue;const q=project(p.lon,p.lat,m);specialIcon(kind,q[0],q[1]);hoverItems.push({x:q[0],y:q[1],text:(kind==='launch'?'发射点':kind==='hide'?'隐蔽点':kind==='depot'?'贮备库':'车辆起点')+'\n'+p.name+(p.id?'\nID: '+p.id:'')})}}}
function stateAt(tr,t){const pts=tr.points;if(t<=pts[0][0])return{lon:pts[0][1],lat:pts[0][2],idx:0,f:0};const n=pts.length;if(t>=pts[n-1][0])return{lon:pts[n-1][1],lat:pts[n-1][2],idx:n-1,f:0};let lo=0,hi=n-1;while(lo+1<hi){const mid=(lo+hi)>>1;if(pts[mid][0]<=t)lo=mid;else hi=mid}const a=pts[lo],b=pts[hi],dt=b[0]-a[0],f=dt>0?(t-a[0])/dt:1;return{lon:a[1]+(b[1]-a[1])*f,lat:a[2]+(b[2]-a[2])*f,idx:lo,f}}
function vehicleColor(i){return `hsl(${(i*137.508)%360} 72% 42%)`}
function drawTrajectories(m){region.trajectories.forEach((tr,i)=>{const s=stateAt(tr,current),color=vehicleColor(i),pts=tr.points;ctx.strokeStyle=color;ctx.globalAlpha=.58;ctx.lineWidth=1.45;ctx.beginPath();let started=false;for(let j=0;j<=s.idx;j++){const q=project(pts[j][1],pts[j][2],m);if(!started){ctx.moveTo(q[0],q[1]);started=true}else ctx.lineTo(q[0],q[1])}if(s.idx<pts.length-1){const q=project(s.lon,s.lat,m);started?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1])}ctx.stroke();ctx.globalAlpha=1;const q=project(s.lon,s.lat,m),next=pts[Math.min(s.idx+1,pts.length-1)],prev=pts[Math.max(0,s.idx)],a=Math.atan2(-(next[2]-prev[2]),(next[1]-prev[1])*m.cos);ctx.save();ctx.translate(q[0],q[1]);ctx.rotate(a);ctx.fillStyle=color;ctx.strokeStyle='#fff';ctx.lineWidth=1.2;ctx.beginPath();ctx.moveTo(8,0);ctx.lineTo(-5,-5);ctx.lineTo(-3,0);ctx.lineTo(-5,5);ctx.closePath();ctx.fill();ctx.stroke();ctx.restore();hoverItems.push({x:q[0],y:q[1],text:'车辆 '+tr.vehicle_id+'\n发射点: '+(tr.fire_point||'-')+'\n隐蔽点: '+(tr.hide_point||'-')+'\n贮备库: '+(tr.depot||'-')})})}
function draw(){if(!view)return;const m=metrics();ctx.clearRect(0,0,m.w,m.h);ctx.fillStyle='#fff';ctx.fillRect(0,0,m.w,m.h);hoverItems=[];drawRoads(m);drawTrajectories(m);drawSpecial(m)}
function fmt(t){t=Math.max(0,t);const h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=Math.floor(t%60);return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0')}
function updateLabels(){slider.value=current;document.getElementById('currentTime').textContent='当前 '+fmt(current)+'  ('+current.toFixed(1)+'s)';document.getElementById('endTime').textContent='结束 '+fmt(region.time_max);document.getElementById('status').textContent=region.label+' | '+region.task_id+' | '+region.trajectories.length+'辆 | '+region.roads.length+'条路网边';draw()}
function frame(ts){if(playing){if(!lastFrame)lastFrame=ts;current+=(ts-lastFrame)/1000*Number(speedSel.value);lastFrame=ts;if(current>=region.time_max){current=region.time_max;playing=false;playBtn.textContent='播放'}updateLabels()}requestAnimationFrame(frame)}
playBtn.onclick=()=>{playing=!playing;if(playing&&current>=region.time_max)current=region.time_min;lastFrame=0;playBtn.textContent=playing?'暂停':'播放';updateLabels()};document.getElementById('restart').onclick=()=>{playing=false;current=region.time_min;playBtn.textContent='播放';updateLabels()};document.getElementById('reset').onclick=()=>{fullView();draw()};slider.oninput=()=>{current=Number(slider.value);lastFrame=0;updateLabels()};
canvas.addEventListener('wheel',e=>{e.preventDefault();const m=metrics(),r=canvas.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top,[lon,lat]=unproject(x,y,m),factor=e.deltaY>0?1.18:.84;view.minLon=lon+(view.minLon-lon)*factor;view.maxLon=lon+(view.maxLon-lon)*factor;view.minLat=lat+(view.minLat-lat)*factor;view.maxLat=lat+(view.maxLat-lat)*factor;draw()},{passive:false});
canvas.onmousedown=e=>{drag={x:e.clientX,y:e.clientY,view:{...view}}};window.onmouseup=()=>drag=null;window.onmousemove=e=>{if(drag){const m=metrics(),dx=(e.clientX-drag.x)/(m.cos*m.scale),dy=(e.clientY-drag.y)/m.scale;view={minLon:drag.view.minLon-dx,maxLon:drag.view.maxLon-dx,minLat:drag.view.minLat+dy,maxLat:drag.view.maxLat+dy};draw();return}const r=canvas.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top;let best=null,dist=11;for(const item of hoverItems){const d=Math.hypot(x-item.x,y-item.y);if(d<dist){dist=d;best=item}}if(best){tooltip.style.display='block';tooltip.textContent=best.text;tooltip.style.left=Math.min(r.width-270,x+14)+'px';tooltip.style.top=Math.min(r.height-100,y+14)+'px'}else tooltip.style.display='none'};
window.addEventListener('resize',resize);switchRegion(0);requestAnimationFrame(frame);
</script>
</body></html>'''


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a standalone interactive player for final dispatch trajectories.")
    parser.add_argument("--result-dir", type=Path, default=Path("result"))
    parser.add_argument("--replay-dir", type=Path, default=Path("result/conflict_replay_speed80"))
    parser.add_argument("--out", type=Path, default=Path("result/visuals/final_trajectory_player.html"))
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    regions = [build_region(root, args.result_dir, spec, args.replay_dir) for spec in REGIONS]
    data = json.dumps({"regions": regions}, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(HTML.replace("__DATA__", data), encoding="utf-8")
    for region in regions:
        print(
            f"{region['id']}: roads={len(region['roads'])} trajectories={len(region['trajectories'])} "
            f"time={region['time_min']:.3f}..{region['time_max']:.3f}"
        )
    print(f"player={args.out} size_bytes={args.out.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
