"""Build every case file bit-for-bit reproducibly and write manifests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import cases, cdf5

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
EXPECTED = HERE / "expected"


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    EXPECTED.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {"reference": cases.REF, "cases": {}, "pairs": {}}

    for case in cases.all_cases():
        path = DATA / f"{case.name}_{case.layout}.cdf5"
        info = cases.build(case, str(path))
        reader = cdf5.Reader.open(path)
        manifest["cases"][f"{case.name}_{case.layout}"] = {
            "file": path.name,
            "sha256": info["sha256"],
            "header_bytes": reader.header_bytes,
            "dimensions": reader.dimensions,
            "layout": case.layout,
            "calendar": case.calendar,
            "units": case.units,
            "fill_code": case.fill_code,
            "window": list(case.window),
            "block_extrema": reader.block_extrema("time"),
            "visible_attrs": cdf5._jsonable(reader.visible_attrs("time")),
        }

    for family in ("pair1", "pair2"):
        for layout in ("dim", "aux"):
            keys = [f"{family}_{v}_{layout}" for v in (
                ("tail_1ns", "whole_second") if family == "pair1" else ("fill_only", "fill_plus_real_far")
            )]
            left, right = (cdf5.Reader.open(DATA / manifest["cases"][k]["file"]) for k in keys)
            manifest["pairs"][f"{family}_{layout}"] = {
                "members": keys,
                "catalog_sha256": {
                    "left": left.catalog_digest(left.window or cases.WINDOW),
                    "right": right.catalog_digest(right.window or cases.WINDOW),
                },
            }

    (EXPECTED / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(manifest['cases'])} case files and expected/manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
