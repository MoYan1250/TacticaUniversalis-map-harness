# -*- coding: utf-8 -*-
"""Dust II collision harness.

Downloads one pinned awpy-data release, rasterizes real wall triangles and the
walkable nav mesh into a top-down grid, and writes preview overlays. Does not
touch match code.
"""
from __future__ import annotations

import json
import struct
import sys
import urllib.request
import zipfile
from array import array
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "Tools" / "map_collision_cache"
OUT = ROOT / "Tools" / "map_collision_out"
OUR_OVERVIEW = ROOT / "Assets" / "_Project" / "Resources" / "Maps" / "dust2_overview.png"
ZONES_JSON = ROOT / "Assets" / "_Project" / "Data" / "JSON" / "dust2.json"

RELEASE = "2000942"
RELEASE_BASE = f"https://github.com/pnxenopoulos/awpy-data/releases/download/{RELEASE}"
ASSETS = {
    "geometry.zip": 56674290,
    "images.zip": 2876805,
    "navs.zip": 8432990,
    "map_data.json": 2903,
}

ZONE_IDS = ("t_spawn", "ct_spawn", "a_site", "b_site")
ZONE_LABELS = {"t_spawn": "T", "ct_spawn": "CT", "a_site": "A", "b_site": "B"}
# Sprite rect in dust2_overview.png.meta. Unity y grows from the bottom.
SPRITE_RECT = (0, 0, 958, 1024)

NEAREST = getattr(getattr(Image, "Resampling", Image), "NEAREST")
FLIP_TB = getattr(getattr(Image, "Transpose", Image), "FLIP_TOP_BOTTOM")
NZ_KEEP = 0.35
IOU_TRUST = 0.55


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.n = len(data)

    def remaining(self) -> int:
        return self.n - self.pos

    def take(self, n: int) -> bytes:
        end = self.pos + n
        if n < 0 or end > self.n:
            raise ValueError(f"nav truncated at {self.pos}, need {n}")
        blob = self.data[self.pos:end]
        self.pos = end
        return blob

    def align8(self) -> None:
        self.take((8 - self.pos % 8) % 8)

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.take(4))[0]

    def skip_cstring(self) -> None:
        while self.u8() != 0:
            pass


def download(name: str) -> Path:
    dest = CACHE / name
    expected = ASSETS[name]
    if dest.is_file() and dest.stat().st_size == expected:
        print(f"cache hit {name}")
        return dest
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    url = f"{RELEASE_BASE}/{name}"
    print(f"download {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "tactica-map-collision-harness"})
    with urllib.request.urlopen(req, timeout=180) as resp, tmp.open("wb") as handle:
        got = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
            got += len(chunk)
            if got % (8 << 20) < (1 << 20):
                print(f"  {name} {got / 1_000_000:.1f} MB")
    size = tmp.stat().st_size
    if size != expected:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{name} size {size} != {expected}")
    tmp.replace(dest)
    return dest


def extract_named(zip_path: Path, filename: str) -> Path:
    dest = CACHE / filename
    with zipfile.ZipFile(zip_path) as archive:
        matches = [info for info in archive.infolist() if Path(info.filename).name.lower() == filename.lower()]
        if filename.lower() == "de_dust2.png":
            matches = [info for info in matches if "lower" not in info.filename.lower()]
        if not matches:
            sample = ", ".join(archive.namelist()[:12])
            raise RuntimeError(f"{filename} not in {zip_path.name}. sample: {sample}")
        matches.sort(key=lambda info: len(info.filename))
        chosen = matches[0]
        dest.write_bytes(archive.read(chosen))
        print(f"extracted {chosen.filename}")
    return dest


def load_map_meta(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data.get("de_dust2") or data.get("dust2")
    if not isinstance(meta, dict):
        raise RuntimeError("map_data.json has no de_dust2 entry")
    out = {
        "pos_x": float(meta["pos_x"]),
        "pos_y": float(meta["pos_y"]),
        "scale": float(meta["scale"]),
    }
    if out["scale"] == 0:
        raise RuntimeError("map scale is 0")
    return out


def parse_mesh(path: Path) -> tuple[array, array]:
    data = path.read_bytes()
    if data[:4] != b"AWMH":
        raise RuntimeError("mesh magic is not AWMH")
    version, n_verts, n_tris = struct.unpack_from("<III", data, 4)
    if version != 1:
        raise RuntimeError(f"unsupported mesh version {version}")
    vert_bytes = n_verts * 12
    tri_bytes = n_tris * 12
    start = 16
    end_v = start + vert_bytes
    end_t = end_v + tri_bytes
    if end_t > len(data):
        raise RuntimeError("mesh truncated")
    verts = array("f")
    verts.frombytes(data[start:end_v])
    if sys.byteorder != "little":
        verts.byteswap()
    indices = array("I")
    indices.frombytes(data[end_v:end_t])
    if sys.byteorder != "little":
        indices.byteswap()
    print(f"mesh version {version} verts {n_verts} tris {n_tris}")
    return verts, indices


def skip_kv3(reader: Reader) -> None:
    reader.align8()
    magic = reader.u32()
    if magic == 0x03564B56:
        raise RuntimeError("KV3 v0 is not supported")
    version = magic & 0xFF
    if (magic & 0xFFFFFF00) != 0x4B563300 or not 1 <= version <= 5:
        raise RuntimeError(f"bad KV3 signature {magic:#010x}")
    if version == 1:
        raise RuntimeError("KV3 v1 is not supported")
    reader.take(16)
    method = reader.u32()
    reader.take(2 + 2)
    reader.take(4 * 4)
    reader.take(2 + 2)
    size_unc_total = reader.i32()
    size_cmp_total = reader.i32()
    n_blocks = reader.i32()
    n_blobs = reader.i32()
    size_block_compressed = 0
    if version >= 4:
        reader.take(4)
        size_block_compressed = reader.i32()
    if version >= 5:
        unc1 = reader.i32()
        cmp1 = reader.i32()
        unc2 = reader.i32()
        cmp2 = reader.i32()
        reader.take(4 * 8)
    else:
        unc1 = size_unc_total
        cmp1 = size_cmp_total
        unc2 = 0
        cmp2 = 0
    if method > 2:
        raise RuntimeError(f"unsupported KV3 compression {method}")
    if n_blocks != 0 or n_blobs != 0:
        raise RuntimeError(f"KV3 block data is unsupported blocks {n_blocks} blobs {n_blobs}")

    def on_disk(unc: int, comp: int) -> int:
        return unc if method == 0 else comp

    body = on_disk(unc1, cmp1) + on_disk(unc2, cmp2) + size_block_compressed
    if body < 0:
        raise RuntimeError("negative KV3 body")
    reader.take(body)


def parse_nav(path: Path) -> list[tuple[int, list[tuple[float, float, float]]]]:
    reader = Reader(path.read_bytes())
    magic = reader.u32()
    if magic != 0xFEEDFACE:
        raise RuntimeError(f"bad nav magic {magic:#010x}")
    version = reader.u32()
    if not 30 <= version <= 36:
        raise RuntimeError(f"unsupported nav version {version}")
    sub_version = reader.u32()
    reader.u32()
    if version >= 36:
        skip_kv3(reader)
    polygons = None
    if version >= 31:
        polygons = read_polygons(reader, version)
    if version >= 32:
        reader.take(4)
    if version >= 35:
        count = reader.u32()
        if count > 100000:
            raise RuntimeError(f"nav marker count looks wrong {count}")
        for _ in range(count):
            reader.skip_cstring()
            reader.take(48)
    if version >= 36:
        skip_kv3(reader)
    area_count = reader.u32()
    if area_count > 500000:
        raise RuntimeError(f"nav area count looks wrong {area_count}")
    areas = []
    hulls: dict[int, int] = {}
    for _ in range(area_count):
        area_id = reader.u32()
        reader.i64()
        hull = reader.u8()
        if polygons is not None and version >= 31:
            index = reader.u32()
            if index >= len(polygons):
                raise RuntimeError(f"polygon index {index} out of range")
            corners = polygons[index]
        else:
            corner_count = reader.u32()
            corners = [(reader.f32(), reader.f32(), reader.f32()) for _ in range(corner_count)]
        reader.take(4)
        for _edge in range(len(corners)):
            links = reader.u32()
            if links > 10000:
                raise RuntimeError("nav connection count looks wrong")
            reader.take(8 * links)
        reader.take(5)
        above = reader.u32()
        reader.take(4 * above)
        below = reader.u32()
        reader.take(4 * below)
        hulls[hull] = hulls.get(hull, 0) + 1
        areas.append((hull, corners))
    print(f"nav version {version} sub {sub_version} areas {area_count} hulls {hulls}")
    if areas:
        sample = areas[0][1][0]
        if not (-20000 < sample[0] < 20000 and -20000 < sample[1] < 20000):
            raise RuntimeError(f"nav corners look unscaled {sample}")
    return areas


def read_polygons(reader: Reader, version: int) -> list[list[tuple[float, float, float]]]:
    corner_count = reader.u32()
    if corner_count > 5_000_000:
        raise RuntimeError(f"nav corner count looks wrong {corner_count}")
    raw = reader.take(corner_count * 12)
    floats = struct.unpack(f"<{corner_count * 3}f", raw)
    corners = [(floats[i], floats[i + 1], floats[i + 2]) for i in range(0, len(floats), 3)]
    polygon_count = reader.u32()
    polygons = []
    for _ in range(polygon_count):
        n = reader.u8()
        poly = []
        for _corner in range(n):
            index = reader.u32()
            if index >= len(corners):
                raise RuntimeError(f"corner index {index} out of range")
            poly.append(corners[index])
        if version >= 35:
            reader.take(4)
        polygons.append(poly)
    return polygons


def load_zones() -> dict[str, tuple[float, float]]:
    data = json.loads(ZONES_JSON.read_text(encoding="utf-8"))
    found = {}
    for zone in data["map"]["zones"]:
        if zone["id"] in ZONE_IDS:
            found[zone["id"]] = (float(zone["x"]), float(zone["y"]))
    missing = [zone_id for zone_id in ZONE_IDS if zone_id not in found]
    if missing:
        raise RuntimeError("missing zones " + ", ".join(missing))
    return found


def load_our_overview() -> Image.Image:
    image = Image.open(OUR_OVERVIEW).convert("RGBA")
    width, height = image.size
    x, y, rect_w, rect_h = SPRITE_RECT
    if width == rect_w and height == rect_h:
        return image
    top = height - (y + rect_h)
    if top < 0 or x < 0 or x + rect_w > width or top + rect_h > height:
        print(f"overview {width}x{height} does not contain sprite rect, using full image")
        return image
    return image.crop((x, top, x + rect_w, top + rect_h))


def non_void_mask(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    raw = rgba.tobytes()
    mask = bytearray(width * height)
    for index in range(width * height):
        r, g, b, a = raw[index * 4:index * 4 + 4]
        if a < 30:
            continue
        if r < 22 and g < 22 and b < 22:
            continue
        mask[index] = 255
    return Image.frombytes("L", (width, height), bytes(mask))


def color_solid_mask(image: Image.Image) -> Image.Image:
    """Same rule as MapCollisionGrid.IsSolidPixel."""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    raw = rgba.tobytes()
    mask = bytearray(width * height)
    for index in range(width * height):
        r, g, b, a = raw[index * 4:index * 4 + 4]
        if a < 0.12 * 255:
            mask[index] = 255
            continue
        lum = (r + g + b) / (3 * 255)
        if lum < 0.12:
            mask[index] = 255
            continue
        max_c = max(r, g, b) / 255
        min_c = min(r, g, b) / 255
        sat = (max_c - min_c) / max_c if max_c > 0.001 else 0.0
        if sat < 0.15 and lum > 0.20:
            mask[index] = 255
    return Image.frombytes("L", (width, height), bytes(mask))


def fill_polygon(grid: bytearray, width: int, height: int, points: list[tuple[float, float]], value: int) -> None:
    if len(points) < 3:
        return
    y_min = min(point[1] for point in points)
    y_max = max(point[1] for point in points)
    y0 = max(0, int(y_min))
    y1 = min(height - 1, int(y_max))
    if y0 > y1:
        return
    stamp = bytes([value])
    count = len(points)
    for y in range(y0, y1 + 1):
        scan = y + 0.5
        xs = []
        for index in range(count):
            x1, y1 = points[index]
            x2, y2 = points[(index + 1) % count]
            if (y1 > scan) != (y2 > scan):
                xs.append(x1 + (scan - y1) * (x2 - x1) / (y2 - y1))
        xs.sort()
        row = y * width
        for index in range(0, len(xs) - 1, 2):
            xa = int(xs[index])
            xb = int(xs[index + 1])
            if xb < xa:
                xa, xb = xb, xa
            if xb < 0 or xa >= width:
                continue
            xa = max(0, xa)
            xb = min(width - 1, xb)
            grid[row + xa:row + xb + 1] = stamp * (xb - xa + 1)


def draw_line(grid: bytearray, width: int, height: int, x0: float, y0: float, x1: float, y1: float) -> None:
    ax = int(round(x0))
    ay = int(round(y0))
    bx = int(round(x1))
    by = int(round(y1))
    dx = abs(bx - ax)
    dy = -abs(by - ay)
    sx = 1 if ax < bx else -1
    sy = 1 if ay < by else -1
    err = dx + dy
    while True:
        if 0 <= ax < width and 0 <= ay < height:
            grid[ay * width + ax] = 1
        if ax == bx and ay == by:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            ax += sx
        if e2 <= dx:
            err += dx
            ay += sy


def dilate_value(grid: bytearray, width: int, height: int, value: int) -> bytearray:
    src = bytes(grid)
    out = bytearray(src)
    for y in range(height):
        row = y * width
        for x in range(width):
            index = row + x
            if src[index] != value:
                continue
            if x > 0:
                out[index - 1] = value
            if x + 1 < width:
                out[index + 1] = value
            if y > 0:
                out[index - width] = value
            if y + 1 < height:
                out[index + width] = value
    return out


def project_factory(meta: dict, height: int, flip_y: bool):
    pos_x = meta["pos_x"]
    pos_y = meta["pos_y"]
    scale = meta["scale"]
    limit = height - 1

    def project(x: float, y: float) -> tuple[float, float]:
        px = (x - pos_x) / scale
        py = (pos_y - y) / scale
        if flip_y:
            py = limit - py
        return px, py

    return project


def inside_margin(points: list[tuple[float, float]], width: int, height: int, margin: float = 8) -> bool:
    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_y = max(point[1] for point in points)
    if max_x < -margin or max_y < -margin or min_x > width + margin or min_y > height + margin:
        return False
    if (max_x - min_x) > width * 0.35 and (max_y - min_y) > height * 0.35:
        return False
    return True


def z_overlaps(lo: float, hi: float, band) -> bool:
    """band is (zmin, zmax, closed_max). None keeps every height."""
    if band is None:
        return True
    zmin, zmax, closed_max = band
    if hi < zmin:
        return False
    return lo <= zmax if closed_max else lo < zmax


def raster_walls(verts: array, indices: array, project, width: int, height: int, band=None) -> tuple[bytearray, int]:
    walls = bytearray(width * height)
    tris = len(indices) // 3
    kept = 0
    nz_limit = NZ_KEEP * NZ_KEEP
    for tri in range(tris):
        base = tri * 3
        i0 = indices[base] * 3
        i1 = indices[base + 1] * 3
        i2 = indices[base + 2] * 3
        ax, ay, az = verts[i0], verts[i0 + 1], verts[i0 + 2]
        bx, by, bz = verts[i1], verts[i1 + 1], verts[i1 + 2]
        cx, cy, cz = verts[i2], verts[i2 + 1], verts[i2 + 2]
        if not z_overlaps(min(az, bz, cz), max(az, bz, cz), band):
            continue
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        mag2 = nx * nx + ny * ny + nz * nz
        if mag2 < 1e-12 or nz * nz > nz_limit * mag2:
            continue
        points = [project(ax, ay), project(bx, by), project(cx, cy)]
        if not inside_margin(points, width, height):
            continue
        kept += 1
        area = abs(
            (points[1][0] - points[0][0]) * (points[2][1] - points[0][1])
            - (points[2][0] - points[0][0]) * (points[1][1] - points[0][1])
        )
        if area >= 2.0:
            fill_polygon(walls, width, height, points, 1)
        if area < 12.0:
            draw_line(walls, width, height, points[0][0], points[0][1], points[1][0], points[1][1])
            draw_line(walls, width, height, points[1][0], points[1][1], points[2][0], points[2][1])
            draw_line(walls, width, height, points[2][0], points[2][1], points[0][0], points[0][1])
        if tri and tri % 250000 == 0:
            print(f"  walls scanned {tri} kept {kept}")
    return walls, kept


def raster_nav(areas, project, width: int, height: int, hull: int | None, band=None) -> bytearray:
    grid = bytearray([1]) * (width * height)
    used = 0
    for area_hull, corners in areas:
        if hull is not None and area_hull != hull:
            continue
        if len(corners) < 3:
            continue
        zs = [z for _x, _y, z in corners]
        if not z_overlaps(sum(zs) / len(zs), sum(zs) / len(zs), band):
            continue
        points = [project(x, y) for x, y, _z in corners]
        fill_polygon(grid, width, height, points, 0)
        used += 1
    print(f"nav carved {used} polygons hull {hull}")
    return dilate_value(grid, width, height, 0)


def combine(walkable: bytearray, walls: bytearray) -> bytearray:
    grid = bytearray(walkable)
    for index, bit in enumerate(walls):
        if bit:
            grid[index] = 1
    return grid


def solid_ratio(grid: bytearray) -> float:
    if not grid:
        return 0.0
    return sum(grid) / len(grid)


def walkable_on_radar(grid: bytearray, radar_mask: Image.Image) -> float:
    raw = radar_mask.tobytes()
    hit = 0
    total = 0
    for index, bit in enumerate(grid):
        if bit:
            continue
        total += 1
        if raw[index] >= 128:
            hit += 1
    if total == 0:
        return 0.0
    return hit / total


def grid_image(grid: bytearray, width: int, height: int) -> Image.Image:
    return Image.frombytes("L", (width, height), bytes(255 if bit else 0 for bit in grid))


def warp_image(src: Image.Image, scale: float, tx: float, ty: float, flip: bool, out_size: tuple[int, int], background: int) -> Image.Image:
    width, height = src.size
    scaled_w = max(1, int(round(width * scale)))
    scaled_h = max(1, int(round(height * scale)))
    resized = src.resize((scaled_w, scaled_h), NEAREST)
    if flip:
        resized = resized.transpose(FLIP_TB)
    canvas = Image.new("L", out_size, background)
    canvas.paste(resized, (int(round(tx)), int(round(ty))))
    return canvas


def iou(left: Image.Image, right: Image.Image) -> float:
    inter = ImageChops.multiply(left, right).histogram()[255]
    union = left.histogram()[255] + right.histogram()[255] - inter
    if union <= 0:
        return 0.0
    return inter / union


def search_at(official: Image.Image, ours: Image.Image, flip: bool, scales, xs, ys) -> tuple[float, float, float, float]:
    best = (1.0, 0.0, 0.0, -1.0)
    for scale in scales:
        for tx in xs:
            for ty in ys:
                warped = warp_image(official, scale, tx, ty, flip, ours.size, 0)
                score = iou(ours, warped)
                if score > best[3]:
                    best = (float(scale), float(tx), float(ty), score)
    return best


def search_alignment(official: Image.Image, ours: Image.Image) -> dict[bool, tuple[float, float, float, float]]:
    factor = 4
    small_official = official.resize((max(1, official.width // factor), max(1, official.height // factor)), NEAREST)
    small_ours = ours.resize((max(1, ours.width // factor), max(1, ours.height // factor)), NEAREST)
    found = {}
    for flip in (False, True):
        coarse = search_at(
            small_official,
            small_ours,
            flip,
            [0.70, 0.82, 0.94, 1.06, 1.18, 1.30],
            range(-60, 45, 5),
            range(-60, 45, 5),
        )
        scale, tx, ty, _score = coarse
        full_tx = tx * factor
        full_ty = ty * factor
        polished = search_at(
            official,
            ours,
            flip,
            list(frange(scale - 0.04, scale + 0.04, 0.02)),
            range(int(full_tx) - 16, int(full_tx) + 17, 4),
            range(int(full_ty) - 16, int(full_ty) + 17, 4),
        )
        found[flip] = polished
        print(
            f"align flip {flip} scale {polished[0]:.3f} tx {polished[1]:.1f} "
            f"ty {polished[2]:.1f} iou {polished[3]:.3f}"
        )
    return found


def frange(start: float, stop: float, step: float):
    value = start
    while value <= stop + 1e-6:
        yield round(value, 3)
        value += step


def uv_to_pixel(u: float, v: float, width: int, height: int) -> tuple[int, int]:
    # Zone y is Unity UV, bottom-up, matching LocalZonePosition.
    x = int(round(u * (width - 1)))
    y = int(round((1.0 - v) * (height - 1)))
    return clamp_pixel(x, y, width, height)


def clamp_pixel(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    return max(0, min(width - 1, x)), max(0, min(height - 1, y))


def zone_hits(mask: Image.Image, zones: dict[str, tuple[float, float]]) -> list[tuple[str, int, int, bool]]:
    width, height = mask.size
    rows = []
    for zone_id in ZONE_IDS:
        u, v = zones[zone_id]
        x, y = uv_to_pixel(u, v, width, height)
        walkable = mask.getpixel((x, y)) < 128
        rows.append((zone_id, x, y, walkable))
    return rows


def paint_solid(base: Image.Image, solid: Image.Image, alpha: int = 120) -> Image.Image:
    color = Image.new("RGBA", base.size, (196, 52, 42, 0))
    lut = [0] * 128 + [alpha] * 128
    color.putalpha(solid.point(lut))
    return Image.alpha_composite(base.convert("RGBA"), color)


def draw_zones(image: Image.Image, rows: list[tuple[str, int, int, bool]]) -> Image.Image:
    draw = ImageDraw.Draw(image)
    for zone_id, x, y, _walkable in rows:
        draw.ellipse((x - 7, y - 7, x + 7, y + 7), outline=(20, 16, 12, 255), width=2)
        draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=(232, 186, 48, 255))
        draw.text((x + 8, y - 8), ZONE_LABELS[zone_id], fill=(20, 16, 12, 255))
    return image


def our_to_official(x: int, y: int, scale: float, tx: float, ty: float, flip: bool, src_size: tuple[int, int]) -> tuple[int, int] | None:
    src_w, src_h = src_size
    scaled_h = max(1, int(round(src_h * scale)))
    local_x = x - tx
    local_y = y - ty
    if flip:
        local_y = (scaled_h - 1) - local_y
    if scale <= 0:
        return None
    src_x = int(round(local_x / scale))
    src_y = int(round(local_y / scale))
    if src_x < 0 or src_y < 0 or src_x >= src_w or src_y >= src_h:
        return None
    return src_x, src_y


def official_zone_rows(rows, scale, tx, ty, flip, src_size) -> list[tuple[str, int, int, bool]]:
    mapped = []
    for zone_id, x, y, walkable in rows:
        point = our_to_official(x, y, scale, tx, ty, flip, src_size)
        if point is None:
            mapped.append((zone_id, 0, 0, False))
        else:
            mapped.append((zone_id, point[0], point[1], walkable))
    return mapped


def choose_projection(areas, meta, radar_mask, verts, indices, band=None) -> tuple[bytearray, dict]:
    width, height = radar_mask.size
    scores = {}
    built = {}
    for flip_y in (False, True):
        project = project_factory(meta, height, flip_y)
        hull_counts: dict[int, int] = {}
        for hull, _corners in areas:
            hull_counts[hull] = hull_counts.get(hull, 0) + 1
        hull = 0 if hull_counts.get(0, 0) else None
        walkable = raster_nav(areas, project, width, height, hull, band)
        if solid_ratio(walkable) > 0.92 and hull == 0:
            print("hull 0 left the map almost solid, carving every hull")
            walkable = raster_nav(areas, project, width, height, None, band)
        walls, kept = raster_walls(verts, indices, project, width, height, band)
        walls = dilate_value(walls, width, height, 1)
        grid = combine(walkable, walls)
        score = walkable_on_radar(grid, radar_mask)
        scores[flip_y] = score
        built[flip_y] = (grid, kept, solid_ratio(grid))
        print(f"projection flip {flip_y} walkable-on-radar {score:.3f} solid {built[flip_y][2]:.3f} walls {kept}")
    # Prefer the unflipped formula unless the flip clearly matches the radar better.
    use_flip = scores[True] > scores[False] + 0.08
    grid, kept, ratio = built[use_flip]
    info = {
        "flip_y": use_flip,
        "score": scores[use_flip],
        "other_score": scores[not use_flip],
        "walls": kept,
        "solid_ratio": ratio,
    }
    return grid, info


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    map_data_path = download("map_data.json")
    images_zip = download("images.zip")
    nav_zip = download("navs.zip")
    geometry_zip = download("geometry.zip")

    meta = load_map_meta(map_data_path)
    radar = Image.open(extract_named(images_zip, "de_dust2.png")).convert("RGBA")
    areas = parse_nav(extract_named(nav_zip, "de_dust2.nav"))
    verts, indices = parse_mesh(extract_named(geometry_zip, "de_dust2.mesh"))
    our = load_our_overview()
    zones = load_zones()
    print(f"radar {radar.size[0]}x{radar.size[1]} our {our.size[0]}x{our.size[1]}")
    print(f"pos_x {meta['pos_x']} pos_y {meta['pos_y']} scale {meta['scale']}")

    radar_mask = non_void_mask(radar)
    our_mask = non_void_mask(our)
    grid, projection = choose_projection(areas, meta, radar_mask, verts, indices)
    occupancy = grid_image(grid, radar.size[0], radar.size[1])

    alignments = search_alignment(radar_mask, our_mask)
    candidates = []
    for flip, (scale, tx, ty, score) in alignments.items():
        warped = warp_image(occupancy, scale, tx, ty, flip, our.size, 255)
        hits = zone_hits(warped, zones)
        walkable_count = sum(1 for _zone, _x, _y, walkable in hits if walkable)
        candidates.append({
            "flip": flip,
            "scale": scale,
            "tx": tx,
            "ty": ty,
            "iou": score,
            "hits": hits,
            "walkable_count": walkable_count,
            "warped": warped,
        })
    best_iou = max(candidates, key=lambda item: item["iou"])
    best_zones = max(candidates, key=lambda item: (item["walkable_count"], item["iou"]))
    chosen = best_iou
    tried_flip = False
    if best_zones["walkable_count"] > best_iou["walkable_count"] and best_zones["iou"] >= 0.45:
        chosen = best_zones
        tried_flip = chosen["flip"] != best_iou["flip"]
    elif best_iou["walkable_count"] < 3:
        tried_flip = True

    trusted = chosen["iou"] >= IOU_TRUST and chosen["walkable_count"] >= 3
    official = paint_solid(radar, occupancy)
    official_rows = official_zone_rows(
        chosen["hits"], chosen["scale"], chosen["tx"], chosen["ty"], chosen["flip"], radar.size)
    # Dots on the official radar use the inverse of the chosen fit, so they show
    # where our zone UVs land in radar space.
    official = draw_zones(official, [(zone, x, y, True) for zone, x, y, _walkable in official_rows if not (x == 0 and y == 0 and zone)])
    our_overlay = draw_zones(paint_solid(our, chosen["warped"]), chosen["hits"])
    color_overlay = paint_solid(our, color_solid_mask(our))

    official_path = OUT / "dust2_official_overlay.png"
    our_path = OUT / "dust2_our_overlay.png"
    color_path = OUT / "dust2_color_overlay.png"
    report_path = OUT / "dust2_report.txt"
    official.save(official_path)
    our_overlay.save(our_path)
    color_overlay.save(color_path)

    lines = [
        "Dust II 碰撞擷取",
        f"release {RELEASE}",
        f"mesh 三角 {len(indices) // 3}，垂直牆 {projection['walls']}",
        f"官方格 {radar.size[0]}x{radar.size[1]}，實心比例 {projection['solid_ratio']:.3f}",
        (
            f"投影 {'上下翻轉' if projection['flip_y'] else '標準 Y'}，"
            f"可走格落在雷達內 {projection['score']:.3f}，另一向 {projection['other_score']:.3f}"
        ),
        (
            f"對齊 scale {chosen['scale']:.3f}，平移 {chosen['tx']:.1f} {chosen['ty']:.1f}，"
            f"翻轉 {'是' if chosen['flip'] else '否'}，IoU {chosen['iou']:.3f}，"
            f"{'可信' if trusted else '不可信'}"
        ),
        f"區點可走 {chosen['walkable_count']} / {len(ZONE_IDS)}",
    ]
    for zone_id, x, y, walkable in chosen["hits"]:
        lines.append(f"{zone_id} 像素 {x} {y} {'可走' if walkable else '實心'}")
    if tried_flip:
        other = best_zones if chosen is best_iou else best_iou
        lines.append(
            f"已比較另一個 Y 翻轉，IoU {other['iou']:.3f}，區點可走 {other['walkable_count']}"
        )
    if not trusted:
        lines.append("對齊不可信，未把這張遮罩套進遊戲")
    lines.append(f"輸出 {official_path.name} {our_path.name} {color_path.name}")
    report = "\n".join(lines) + "\n"
    report_path.write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"harness failed: {exc}", file=sys.stderr)
        raise
