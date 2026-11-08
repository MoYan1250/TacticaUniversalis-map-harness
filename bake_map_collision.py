# -*- coding: utf-8 -*-
"""Bake overview collision masks for every current map.

Body size is MapBodyClearance: Dust II's mid door fits one person plus a little room.
Pass resource ids to rebake only those overviews and merge collision_scale.txt.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import map_collision_harness as h
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
MAPS = ROOT / "Assets" / "_Project" / "Resources" / "Maps"
META_TEMPLATE = (MAPS / "dust2_overview.png.meta").read_text(encoding="utf-8")

# (cs2 map, overview file, resources id without Maps/)
JOBS = [
    ("de_dust2", "dust2_overview.png", "dust2_overview"),
    ("de_mirage", "mirage_overview.png", "mirage_overview"),
    ("de_inferno", "inferno_overview.png", "inferno_overview"),
    ("de_ancient", "ancient_overview.png", "ancient_overview"),
    ("de_overpass", "overpass_overview.png", "overpass_overview"),
    ("de_train", "train_overview.png", "train_overview"),
    ("de_nuke", "nuke_overview.png", "nuke_overview"),
    ("de_nuke", "nuke_overview_b1.png", "nuke_overview_b1"),
    ("de_nuke", "nuke_overview_f1.png", "nuke_overview_f1"),
    ("de_vertigo", "vertigo_overview.png", "vertigo_overview"),
    ("de_vertigo", "vertigo_overview_b1.png", "vertigo_overview_b1"),
    ("de_vertigo", "vertigo_overview_b2.png", "vertigo_overview_b2"),
]


# The two radar pictures share an outer frame. These overviews need the altitude
# band of the floor that is actually drawn, which is not always the picture with
# the higher silhouette score. Train is one combined drawing, so it keeps every height.
SECTION_PIN = {
    "nuke_overview": "default",
    "nuke_overview_b1": "default",
    "nuke_overview_f1": "lower",
    "vertigo_overview": "default",
    "vertigo_overview_b1": "lower",
    "vertigo_overview_b2": "default",
}


def load_meta(cs_map: str) -> dict:
    data = json.loads((h.CACHE / "map_data.json").read_text(encoding="utf-8"))
    meta = data[cs_map]
    return {"pos_x": float(meta["pos_x"]), "pos_y": float(meta["pos_y"]), "scale": float(meta["scale"])}


def section_band(cs_map: str, section: str | None):
    if not section:
        return None
    data = json.loads((h.CACHE / "map_data.json").read_text(encoding="utf-8"))
    band = (data[cs_map].get("verticalsections") or {}).get(section)
    if not band:
        return None
    return (float(band["AltitudeMin"]), float(band["AltitudeMax"]), section != "lower")


def overview_mask(image: Image.Image) -> Image.Image:
    """Keep near-black floors when they are a real part of the drawing."""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    raw = rgba.tobytes()
    count = width * height
    dark = 0
    for index in range(count):
        red, green, blue, alpha = raw[index * 4:index * 4 + 4]
        if alpha >= 30 and red < 22 and green < 22 and blue < 22:
            dark += 1
    if dark / count <= 0.02:
        return h.non_void_mask(image)
    mask = bytearray(count)
    for index in range(count):
        if raw[index * 4 + 3] >= 30:
            mask[index] = 255
    return Image.frombytes("L", (width, height), bytes(mask))


def sprite_crop(image: Image.Image, meta_path: Path) -> Image.Image:
    if not meta_path.is_file():
        return image
    text = meta_path.read_text(encoding="utf-8")
    marker = "rect:\n"
    index = text.find(marker)
    if index < 0:
        return image
    block = text[index:index + 280]
    values = {}
    for key in ("x", "y", "width", "height"):
        token = f"{key}: "
        found = block.find(token)
        if found < 0:
            return image
        values[key] = int(block[found + len(token):].split()[0])
    x, y, width, height = values["x"], values["y"], values["width"], values["height"]
    if image.size == (width, height) and x == 0 and y == 0:
        return image
    top = image.size[1] - (y + height)
    return image.crop((x, top, x + width, top + height))


def load_overview(filename: str) -> Image.Image:
    path = MAPS / filename
    image = Image.open(path).convert("RGBA")
    return sprite_crop(image, Path(str(path) + ".meta"))


def radar_files(cs_map: str) -> list[str]:
    archive_path = h.download("images.zip")
    names = []
    import zipfile
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            base = Path(info.filename).name.lower()
            if base == f"{cs_map}.png" or base == f"{cs_map}_lower.png":
                names.append(Path(info.filename).name)
    if not names:
        raise RuntimeError(f"no radar for {cs_map}")
    return names


def write_meta(png_path: Path) -> None:
    guid = uuid.uuid4().hex
    text = META_TEMPLATE
    old_guid = None
    for line in text.splitlines():
        if line.startswith("guid: "):
            old_guid = line.split("guid: ", 1)[1].strip()
            break
    if not old_guid:
        raise RuntimeError("meta template has no guid")
    text = text.replace(old_guid, guid, 1)
    text = text.replace("dust2_overview_0", png_path.stem)
    text = text.replace("filterMode: 1", "filterMode: 0")
    text = text.replace("textureCompression: 1", "textureCompression: 0")
    text = text.replace("isReadable: 0", "isReadable: 1")
    png_path.with_suffix(png_path.suffix + ".meta").write_text(text, encoding="utf-8")


def bake_one(cs_map: str, overview_name: str, resource_name: str, cache: dict) -> str:
    overview = load_overview(overview_name)
    our_mask = overview_mask(overview)
    meta = load_meta(cs_map)
    best = None
    radar_masks = {}
    for radar_name in radar_files(cs_map):
        if radar_name not in radar_masks:
            radar = Image.open(h.extract_named(h.download("images.zip"), radar_name)).convert("RGBA")
            radar_masks[radar_name] = (h.non_void_mask(radar), radar.size)
        radar_mask, _size = radar_masks[radar_name]
        alignments = h.search_alignment(radar_mask, our_mask)
        for flip, (scale, tx, ty, score) in alignments.items():
            if best is None or score > best["iou"]:
                best = {
                    "radar": radar_name,
                    "flip": flip,
                    "scale": scale,
                    "tx": tx,
                    "ty": ty,
                    "iou": score,
                }
    section = SECTION_PIN.get(resource_name)
    band = section_band(cs_map, section)
    radar_mask, radar_size = radar_masks[best["radar"]]
    key = (cs_map, section or "all", best["radar"])
    if key not in cache:
        areas = h.parse_nav(h.extract_named(h.download("navs.zip"), cs_map + ".nav"))
        verts, indices = h.parse_mesh(h.extract_named(h.download("geometry.zip"), cs_map + ".mesh"))
        grid, info = h.choose_projection(areas, meta, radar_mask, verts, indices, band)
        cache[key] = (h.grid_image(grid, radar_size[0], radar_size[1]), info)
        print(
            f"built {cs_map} {best['radar']} section {section or 'all'} "
            f"flip {info['flip_y']} solid {info['solid_ratio']:.3f}"
        )
    occupancy, info = cache[key]
    trusted = best["iou"] >= h.IOU_TRUST
    hammer = meta["scale"] / best["scale"] if best["scale"] else 0.0
    line = (
        f"{resource_name} radar {best['radar']} section {section or 'all'} flip {best['flip']} "
        f"iou {best['iou']:.3f} hammer/px {hammer:.4f} {'trusted' if trusted else 'UNTRUSTED'}"
    )
    print(line)
    if not trusted:
        return line
    warped = h.warp_image(occupancy, best["scale"], best["tx"], best["ty"], best["flip"], overview.size, 255)
    dest = MAPS / f"{resource_name}_collision.png"
    warped.save(dest)
    if not dest.with_suffix(dest.suffix + ".meta").is_file():
        write_meta(dest)
    overlay = h.OUT / f"{resource_name}_overlay.png"
    paint = overview.convert("RGBA")
    red = Image.new("RGBA", paint.size, (220, 40, 40, 0))
    red.putalpha(warped.point(lambda value: 120 if value > 127 else 0))
    Image.alpha_composite(paint, red).save(overlay)
    return line + f"\nMaps/{resource_name} {hammer:.6f}"


def merge_scale(updates: dict[str, float]) -> None:
    scale_path = MAPS / "collision_scale.txt"
    if scale_path.is_file():
        lines = scale_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = ["# hammer units per overview pixel. Body diameter is 33, Dust II mid door is 39."]
    kept = []
    seen = set()
    for line in lines:
        if not line.startswith("Maps/"):
            kept.append(line)
            continue
        key = line.split()[0].split("/", 1)[1]
        if key not in updates:
            kept.append(line)
            continue
        kept.append(f"Maps/{key} {updates[key]:.6f}")
        seen.add(key)
    for key, hammer in updates.items():
        if key not in seen:
            kept.append(f"Maps/{key} {hammer:.6f}")
    scale_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    meta_path = scale_path.with_suffix(scale_path.suffix + ".meta")
    if not meta_path.is_file():
        guid = uuid.uuid4().hex
        meta_path.write_text(
            "fileFormatVersion: 2\n"
            f"guid: {guid}\n"
            "TextScriptImporter:\n"
            "  externalObjects: {}\n"
            "  userData: \n"
            "  assetBundleName: \n"
            "  assetBundleVariant: \n",
            encoding="utf-8",
        )


def main() -> int:
    only = set(sys.argv[1:])
    h.download("map_data.json")
    h.download("images.zip")
    h.download("navs.zip")
    h.download("geometry.zip")
    cache = {}
    updates = {}
    report = ["map collision bake"]
    for cs_map, overview_name, resource_name in JOBS:
        if only and resource_name not in only:
            continue
        try:
            result = bake_one(cs_map, overview_name, resource_name, cache)
        except Exception as exc:
            result = f"{resource_name} FAILED {exc}"
            print(result)
        report.append(result)
        for part in result.splitlines():
            if part.startswith("Maps/"):
                key, hammer = part.split()
                updates[key.split("/", 1)[1]] = float(hammer)
    if only:
        merge_scale(updates)
    else:
        lines = ["# hammer units per overview pixel. Body diameter is 33, Dust II mid door is 39."]
        lines.extend(f"Maps/{key} {hammer:.6f}" for key, hammer in updates.items())
        (MAPS / "collision_scale.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = h.OUT / "bake_report.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    previous = out.read_text(encoding="utf-8") if out.is_file() else ""
    out.write_text(previous + "\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
