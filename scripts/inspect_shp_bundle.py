#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import shapefile


def sizeof_fmt(num: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}PB"


def inspect_shp(path: Path) -> str:
    reader = shapefile.Reader(str(path))
    fields = [f[0] for f in reader.fields[1:]]
    size_bytes = sum(
        p.stat().st_size for p in path.parent.glob(f"{path.stem}.*") if p.is_file()
    )
    lines = [
        f"Layer: {path.name}",
        f"  Shape type : {reader.shapeTypeName}",
        f"  Records    : {len(reader)}",
        f"  BBox       : {tuple(reader.bbox)}",
        f"  Fields     : {', '.join(fields) if fields else '(none)'}",
        f"  File set    : {sizeof_fmt(size_bytes)}",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect all SHP layers in a directory and save a text report.")
    parser.add_argument("--dir", required=True, help="Directory containing .shp files")
    parser.add_argument("--out", required=True, help="Output text report path")
    args = parser.parse_args()

    root = Path(args.dir)
    shp_files = sorted(root.glob("*.shp"))
    if not shp_files:
        raise SystemExit(f"No .shp files found in {root}")

    lines = [
        f"SHP bundle directory: {root}",
        f"Layer count: {len(shp_files)}",
        "",
    ]
    for shp_path in shp_files:
        lines.append(inspect_shp(shp_path))
        lines.append("")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
