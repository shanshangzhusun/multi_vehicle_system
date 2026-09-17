#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Iterable, Optional

from PIL import Image, ImageDraw
import shapefile


PRESETS = {
    "china_major": {
        "bbox": None,
        "classes": [
            "motorway",
            "motorway_link",
            "trunk",
            "trunk_link",
            "primary",
            "primary_link",
            "secondary",
            "secondary_link",
        ],
        "size": (1800, 1120),
        "title": "China Road Network Preview (Major Roads)",
    },
    "beijing_detail": {
        "bbox": (115.7, 39.4, 117.1, 40.4),
        "classes": [
            "motorway",
            "motorway_link",
            "trunk",
            "trunk_link",
            "primary",
            "primary_link",
            "secondary",
            "secondary_link",
            "tertiary",
            "tertiary_link",
            "residential",
            "service",
            "unclassified",
            "living_street",
        ],
        "size": (1600, 1600),
        "title": "Beijing Detail Preview",
    },
}


CLASS_STYLE = {
    "motorway": ("#0f172a", 5),
    "motorway_link": ("#1e293b", 4),
    "trunk": ("#334155", 4),
    "trunk_link": ("#475569", 3),
    "primary": ("#64748b", 3),
    "primary_link": ("#64748b", 2),
    "secondary": ("#94a3b8", 2),
    "secondary_link": ("#94a3b8", 2),
    "tertiary": ("#cbd5e1", 1),
    "tertiary_link": ("#cbd5e1", 1),
    "residential": ("#dbe4ee", 1),
    "service": ("#e2e8f0", 1),
    "unclassified": ("#e2e8f0", 1),
    "living_street": ("#e2e8f0", 1),
}


def parse_bbox(value: Optional[str]) -> Optional[tuple[float, float, float, float]]:
    if not value:
        return None
    parts = [float(x.strip()) for x in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be minx,miny,maxx,maxy")
    return parts[0], parts[1], parts[2], parts[3]


def project_factory(
    bbox: tuple[float, float, float, float], width: int, height: int, padding: int = 32
):
    minx, miny, maxx, maxy = bbox
    mid_lat = (miny + maxy) / 2.0
    cos_lat = max(0.3, math.cos(math.radians(mid_lat)))
    dx = (maxx - minx) * cos_lat
    dy = maxy - miny
    sx = (width - 2 * padding) / max(dx, 1e-9)
    sy = (height - 2 * padding) / max(dy, 1e-9)
    scale = min(sx, sy)
    x_offset = padding + (width - 2 * padding - dx * scale) / 2.0
    y_offset = padding + (height - 2 * padding - dy * scale) / 2.0

    def project(pt: tuple[float, float]) -> tuple[int, int]:
        x, y = pt
        px = x_offset + ((x - minx) * cos_lat) * scale
        py = height - (y_offset + (y - miny) * scale)
        return int(round(px)), int(round(py))

    return project


def bbox_intersects(a: Iterable[float], b: tuple[float, float, float, float]) -> bool:
    a_minx, a_miny, a_maxx, a_maxy = a
    b_minx, b_miny, b_maxx, b_maxy = b
    return not (a_maxx < b_minx or a_minx > b_maxx or a_maxy < b_miny or a_miny > b_maxy)


def simplify_pixels(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if len(points) <= 2:
        return points
    simplified = [points[0]]
    last = points[0]
    for pt in points[1:]:
        if pt != last:
            simplified.append(pt)
            last = pt
    if simplified[-1] != points[-1]:
        simplified.append(points[-1])
    return simplified


def draw_roads(
    shp_path: Path,
    out_path: Path,
    bbox: Optional[tuple[float, float, float, float]],
    include_classes: set[str],
    size: tuple[int, int],
    title: str,
) -> None:
    reader = shapefile.Reader(str(shp_path))
    target_bbox = tuple(reader.bbox) if bbox is None else bbox
    width, height = size
    image = Image.new("RGB", size, "#f8fafc")
    draw = ImageDraw.Draw(image)
    project = project_factory(target_bbox, width, height)

    drawn = 0
    skipped_bbox = 0
    skipped_class = 0
    for idx, sr in enumerate(reader.iterShapeRecords(fields=["fclass"])):
        fclass = sr.record[0] or ""
        if include_classes and fclass not in include_classes:
            skipped_class += 1
            continue

        shape = sr.shape
        sbbox = getattr(shape, "bbox", None)
        if sbbox is not None and not bbox_intersects(sbbox, target_bbox):
            skipped_bbox += 1
            continue

        color, line_width = CLASS_STYLE.get(fclass, ("#cbd5e1", 1))
        parts = list(shape.parts) + [len(shape.points)]
        for start, end in zip(parts[:-1], parts[1:]):
            pts = [project((x, y)) for x, y in shape.points[start:end]]
            pts = simplify_pixels(pts)
            if len(pts) >= 2:
                draw.line(pts, fill=color, width=line_width, joint="curve")
                drawn += 1

        if (idx + 1) % 500000 == 0:
            print(f"render progress: {idx + 1} features scanned, {drawn} polylines drawn")

    # border and title
    draw.rectangle((0, 0, width - 1, height - 1), outline="#cbd5e1", width=1)
    draw.rectangle((12, 12, width - 12, 60), fill="#ffffff")
    draw.text((24, 22), title, fill="#0f172a")
    draw.text(
        (24, 40),
        f"layer={shp_path.name}  bbox={tuple(round(x, 4) for x in target_bbox)}  drawn={drawn}",
        fill="#475569",
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    print(
        f"saved {out_path} | drawn={drawn} skipped_class={skipped_class} skipped_bbox={skipped_bbox}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a static preview image from a road SHP layer.")
    parser.add_argument("--roads-shp", required=True, help="Path to road shapefile")
    parser.add_argument("--out", required=True, help="Output image path (.png)")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="china_major")
    parser.add_argument("--bbox", default="", help="Override bbox as minx,miny,maxx,maxy")
    parser.add_argument(
        "--classes",
        default="",
        help="Comma-separated road classes to draw; empty uses preset classes",
    )
    parser.add_argument("--width", type=int, default=0, help="Override output width")
    parser.add_argument("--height", type=int, default=0, help="Override output height")
    args = parser.parse_args()

    preset = PRESETS[args.preset]
    bbox = parse_bbox(args.bbox) or preset["bbox"]
    classes = (
        {x.strip() for x in args.classes.split(",") if x.strip()}
        if args.classes
        else set(preset["classes"])
    )
    size = (
        args.width or preset["size"][0],
        args.height or preset["size"][1],
    )

    draw_roads(
        shp_path=Path(args.roads_shp),
        out_path=Path(args.out),
        bbox=bbox,
        include_classes=classes,
        size=size,
        title=preset["title"],
    )


if __name__ == "__main__":
    main()
