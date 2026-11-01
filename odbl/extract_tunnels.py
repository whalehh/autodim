#!/usr/bin/env python3
"""
SMALS 터널 DB 추출기 (OpenStreetMap → 앱 자산 파일).

차가 다니는 길의 터널·지하차도(highway=* + tunnel=yes|building_passage)와
지하주차장 입구(amenity=parking_entrance + parking=underground)를 뽑아
앱이 읽는 작은 이진 파일(brain/src/main/assets/tunnels/<region>.smt)로 만든다.

원본 (둘 중 하나):
  - pbf (기본, 권장): Geofabrik 지역 추출본(.osm.pbf)을 내려받아 pyosmium 으로 읽는다. 재현 가능, 서버 부담 없음.
        pip install osmium   (4.x 에서 확인)
  - overpass: 공용 Overpass API 를 1° 타일로 조회. 서버가 바쁘면 504 가 잦다(2026-11 확인).

사용:
    python tools/tunnels/extract_tunnels.py --region kr                 # pbf 내려받기(없을 때만) + 생성
    python tools/tunnels/extract_tunnels.py --region kr --source overpass
    python tools/tunnels/extract_tunnels.py --region kr --from-cache    # 받아 둔 원본 재사용
    python tools/tunnels/extract_tunnels.py --region kr --refresh       # 출시 전: 최신 추출본 강제로 새로 받기

결과: brain/src/main/assets/tunnels/<region>.smt (앱), docs/odbl/<region>.geojson·<region>_manifest.json·
extract_tunnels.py (ODbL 공개 묶음).

해외 지역은 REGIONS 에 한 줄 추가하면 된다(앱은 assets/tunnels 의 .smt 를 모두 읽는다).

데이터 © OpenStreetMap contributors, ODbL 1.0. 결과 파일은 ODbL 의 "파생 데이터베이스"이므로
앱에 출처를 표시하고, 요청 시 이 파일(또는 out/<region>.geojson)과 이 스크립트를 공개해야 한다.

출력 형식 (big-endian, 전체를 gzip):
    "SMT1"  u8 version(1)  2바이트 지역코드  i32 생성일(yyyymmdd)  str OSM 기준시각
    i32 문자열 수, [str]                         (str = u16 길이 + UTF-8)
    i32 터널 수, [u8 kind, u8 flags, i32 이름번호(-1 없음), i32 길이 m, i64 OSM way id,
                 u16 점 수, [i32 위도×1e6, i32 경도×1e6]]
    i32 주차장 입구 수, [i32 위도×1e6, i32 경도×1e6, i32 이름번호, i64 OSM node id]
    kind: 0 터널, 1 지하차도, 2 지하주차장 / flags: bit0 일방통행(점 순서가 진행 방향)
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import math
import pathlib
import struct
import sys
import time
import urllib.parse
import urllib.request

_REPO = pathlib.Path(__file__).resolve().parents[2]
# 저장소 안(tools/tunnels/)이면 저장소 기준, 공개 묶음처럼 따로 떨어져 있으면 현재 폴더 기준으로 쓴다.
ROOT = _REPO if (_REPO / "core").is_dir() else pathlib.Path.cwd()
HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE / "cache"
OUT = HERE / "out"
ASSETS = ROOT / "brain" / "src" / "main" / "assets" / "tunnels"

ODBL = ROOT / "docs" / "odbl"


def _support_email() -> str:
    """연락처는 앱과 같은 곳(core AppInfo.SUPPORT_EMAIL)에서 읽는다. 개인 메일 금지."""
    import re
    f = ROOT / "core" / "src" / "main" / "java" / "com" / "smals" / "core" / "AppInfo.kt"
    if not f.exists():
        return ""  # 저장소 밖에서 실행(공개 묶음): 연락처 없이 이름만
    m = re.search(r'SUPPORT_EMAIL\s*=\s*"([^"]+)"', f.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("AppInfo.SUPPORT_EMAIL 을 찾지 못함")
    return m.group(1)


_CONTACT = _support_email()
USER_AGENT = f"AutoDim-tunnel-extractor/1.1 ({_CONTACT})" if _CONTACT else "AutoDim-tunnel-extractor/1.1"


def _urlopen(req, timeout):
    """certifi 가 있으면 그 CA 묶음으로 검증(Windows 파이썬의 오래된 인증서 저장소 문제 회피). 검증은 끄지 않는다."""
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)
ENDPOINTS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

REGIONS = {
    # 코드: (Overpass area 선택자, 대략 범위 (남, 서, 북, 동), 설명). 큰 나라는 1° 타일로 나눠 받는다(서버 시간 초과 방지).
    "kr": ('area["ISO3166-1"="KR"][admin_level=2]', (33.0, 124.0, 39.0, 132.0), "South Korea"),
}
# Geofabrik 추출본 경로 (지역 경계로 이미 잘려 있음)
GEOFABRIK = {
    "kr": "https://download.geofabrik.de/asia/south-korea-latest.osm.pbf",
}
TILE_DEG = 1.0

DRIVABLE = (
    "motorway|trunk|primary|secondary|tertiary|unclassified|residential|service|living_street|road"
    "|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link"
)
MIN_LENGTH_M = 50.0  # 이보다 짧으면 통과 시간이 너무 짧아 어둡게 할 이유가 없다(80km/h 에서 2.2초)
SIMPLIFY_M = 3.0     # 더글러스-퓨커 허용 오차

KIND_TUNNEL, KIND_UNDERPASS, KIND_PARKING = 0, 1, 2
NAME_HINTS = ("터널", "지하차도", "굴", "tunnel", "underpass", "주차")


def overpass(query: str) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for url in ENDPOINTS:
        for attempt in range(2):
            try:
                print(f"  → {url} (시도 {attempt + 1})", file=sys.stderr)
                req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
                with _urlopen(req, 400) as r:
                    return json.load(r)
            except Exception as e:  # noqa: BLE001 — 다음 서버로
                last = e
                print(f"    실패: {e}", file=sys.stderr)
                time.sleep(5)
    raise RuntimeError(f"Overpass 실패: {last}")


def fetch(region: str, from_cache: bool) -> dict:
    """타일별로 받아 cache/<region>/ 에 저장(중단돼도 이어 받기). 경계에 걸친 way 는 id 로 중복 제거."""
    area, (s0, w0, n0, e0), _ = REGIONS[region]
    tdir = CACHE / region
    tdir.mkdir(parents=True, exist_ok=True)
    elements: dict[tuple[str, int], dict] = {}
    base = ""
    lat = s0
    while lat < n0:
        lon = w0
        while lon < e0:
            bbox = f"{lat},{lon},{lat + TILE_DEG},{lon + TILE_DEG}"
            path = tdir / f"{lat:.1f}_{lon:.1f}.json"
            if path.exists() and (from_cache or path.stat().st_size > 0):
                raw = json.loads(path.read_text(encoding="utf-8"))
            else:
                q = f"""
[out:json][timeout:300][bbox:{bbox}];
{area}->.a;
(
  way["tunnel"~"^(yes|building_passage)$"]["highway"~"^({DRIVABLE})$"](area.a);
  node["amenity"="parking_entrance"]["parking"="underground"](area.a);
);
out body geom qt;
"""
                print(f"[{region}] 타일 {bbox} 조회", file=sys.stderr)
                raw = overpass(q)
                path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
                time.sleep(2)  # 공용 서버 예의
            base = max(base, raw.get("osm3s", {}).get("timestamp_osm_base", ""))
            for e in raw.get("elements", []):
                elements[(e["type"], e["id"])] = e
            lon += TILE_DEG
        lat += TILE_DEG
    print(f"[{region}] 요소 {len(elements)}개", file=sys.stderr)
    return {"osm3s": {"timestamp_osm_base": base}, "elements": list(elements.values())}


def fetch_pbf(region: str, from_cache: bool, refresh: bool = False) -> dict:
    """Geofabrik .osm.pbf 를 두 번 훑는다: 1) 대상 way 와 필요한 노드 id, 2) 그 노드 좌표 + 주차장 입구."""
    import re
    import osmium  # pip install osmium

    CACHE.mkdir(parents=True, exist_ok=True)
    pbf = CACHE / f"{GEOFABRIK[region].rsplit('/', 1)[1]}"
    if refresh or not pbf.exists() or (not from_cache and time.time() - pbf.stat().st_mtime > 7 * 86400):
        print(f"[{region}] 내려받는 중: {GEOFABRIK[region]}", file=sys.stderr)
        req = urllib.request.Request(GEOFABRIK[region], headers={"User-Agent": USER_AGENT})
        tmp = pbf.with_suffix(".part")
        with _urlopen(req, 3600) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        # Geofabrik 이 함께 주는 md5 로 확인
        req = urllib.request.Request(GEOFABRIK[region] + ".md5", headers={"User-Agent": USER_AGENT})
        with _urlopen(req, 60) as r:
            want = r.read().decode().split()[0]
        got = _md5(tmp)
        if got != want:
            tmp.unlink()
            raise SystemExit(f"md5 불일치: {got} != {want}")
        tmp.replace(pbf)

    tunnel_re = re.compile(r"^(yes|building_passage)$")
    hw_re = re.compile(f"^({DRIVABLE})$")
    ways: list[dict] = []
    need: set[int] = set()

    class Ways(osmium.SimpleHandler):
        def way(self, w):
            t = w.tags
            if tunnel_re.match(t.get("tunnel", "")) and hw_re.match(t.get("highway", "")):
                refs = [n.ref for n in w.nodes]
                ways.append({"type": "way", "id": w.id, "nodes": refs, "tags": {k.k: k.v for k in t}})
                need.update(refs)

    coords: dict[int, tuple[float, float]] = {}
    parks: list[dict] = []

    class Nodes(osmium.SimpleHandler):
        def node(self, n):
            if n.id in need:
                coords[n.id] = (n.location.lat, n.location.lon)
            t = n.tags
            if t.get("amenity") == "parking_entrance" and t.get("parking") == "underground":
                parks.append({"type": "node", "id": n.id, "lat": n.location.lat, "lon": n.location.lon,
                              "tags": {k.k: k.v for k in t}})

    print(f"[{region}] 1차: way 훑기", file=sys.stderr)
    Ways().apply_file(str(pbf))
    print(f"[{region}] 2차: 노드 {len(need)}개 좌표", file=sys.stderr)
    Nodes().apply_file(str(pbf))
    for w in ways:
        w["geometry"] = [{"lat": coords[r][0], "lon": coords[r][1]} for r in w["nodes"] if r in coords]
    ways = [w for w in ways if len(w["geometry"]) == len(w["nodes"])]
    header = osmium.io.Reader(str(pbf), osmium.osm.osm_entity_bits.NOTHING)
    base = header.header().get("osmosis_replication_timestamp", "")
    header.close()
    return {"osm3s": {"timestamp_osm_base": base}, "elements": ways + parks}


def _md5(path: pathlib.Path) -> str:
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _sha256(path: pathlib.Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_odbl_bundle(region: str, stats: dict, source: str) -> None:
    """ODbL 공개 묶음(docs/odbl): 파생 DB(GeoJSON), 통계·출처, 만든 스크립트. README 는 손으로 쓴 것을 둔다."""
    import shutil
    ODBL.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(OUT / f"{region}.geojson", ODBL / f"{region}.geojson")
    shutil.copyfile(pathlib.Path(__file__), ODBL / "extract_tunnels.py")
    info = {
        "region": region,
        "database": f"{region}.geojson (same content as the app asset {region}.smt)",
        "license": "Open Database License (ODbL) 1.0 — https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution": "© OpenStreetMap contributors",
        "source": source,
        "osm_data_timestamp": stats.get("osm_base"),
        "built": dt.date.today().isoformat(),
        "app_asset": f"brain/src/main/assets/tunnels/{region}.smt",
        "app_asset_sha256": _sha256(ASSETS / f"{region}.smt"),
        "geojson_sha256": _sha256(ODBL / f"{region}.geojson"),
        "stats": stats,
    }
    with open(ODBL / f"{region}_manifest.json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
        f.write("\n")


def hav(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def length(pts) -> float:
    return sum(hav(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def simplify(pts, tol):
    if len(pts) < 3:
        return pts
    lat0 = pts[0][0]
    kx = 111_320 * math.cos(math.radians(lat0))
    xy = [((p[1] - pts[0][1]) * kx, (p[0] - lat0) * 110_540) for p in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = xy[i], xy[j]
        dx, dy = x2 - x1, y2 - y1
        seg = math.hypot(dx, dy) or 1e-9
        best, idx = -1.0, -1
        for k in range(i + 1, j):
            d = abs(dy * (xy[k][0] - x1) - dx * (xy[k][1] - y1)) / seg
            if d > best:
                best, idx = d, k
        if best > tol:
            keep[idx] = True
            stack += [(i, idx), (idx, j)]
    return [p for p, k in zip(pts, keep) if k]


def tunnel_name(tags: dict) -> str | None:
    for k in ("tunnel:name:ko", "tunnel:name"):
        if tags.get(k):
            return tags[k]
    for k in ("name:ko", "name"):
        n = tags.get(k)
        if n and any(h in n.lower() for h in NAME_HINTS):
            return n
    return None


def kind_of(tags: dict, name: str | None) -> int:
    n = (name or "").lower()
    if tags.get("service") == "parking_aisle" or "주차" in n or "parking" in n:
        return KIND_PARKING
    if "지하차도" in n or "underpass" in n:
        return KIND_UNDERPASS
    return KIND_TUNNEL


class Piece:
    __slots__ = ("ids", "nodes", "pts", "oneway", "name", "kind", "tags")

    def __init__(self, el):
        tags = el.get("tags", {})
        pts = [(g["lat"], g["lon"]) for g in el["geometry"]]
        nodes = list(el["nodes"])
        ow = tags.get("oneway", "")
        if tags.get("highway") in ("motorway", "motorway_link") and ow == "":
            ow = "yes"  # 고속도로는 기본이 일방통행
        if ow == "-1":
            pts.reverse()
            nodes.reverse()
        self.ids = [el["id"]]
        self.nodes = nodes
        self.pts = pts
        self.oneway = ow in ("yes", "1", "true", "-1")
        self.name = tunnel_name(tags)
        self.kind = kind_of(tags, self.name)
        self.tags = tags


def merge(pieces: list[Piece]) -> list[Piece]:
    """끝점을 공유하는 조각을 하나의 터널로 잇는다. 갈림길(세 갈래 이상)은 잇지 않는다."""
    alive = set(range(len(pieces)))
    changed = True
    while changed:
        changed = False
        ends: dict[int, list[tuple[int, int]]] = {}
        for i in alive:
            p = pieces[i]
            ends.setdefault(p.nodes[0], []).append((i, 0))
            ends.setdefault(p.nodes[-1], []).append((i, 1))
        for node, inc in ends.items():
            if len(inc) != 2:
                continue
            (i, ei), (j, ej) = inc
            if i == j or i not in alive or j not in alive:
                continue
            a, b = pieces[i], pieces[j]
            if a.oneway != b.oneway:
                continue
            if a.name and b.name and a.name != b.name:
                continue
            if a.oneway:
                # 진행 방향이 이어져야 함: a 끝 → b 시작
                if ei == 1 and ej == 0:
                    first, second = a, b
                elif ei == 0 and ej == 1:
                    first, second = b, a
                else:
                    continue
            else:
                first, second = a, b
                if ei == 0:
                    first.nodes.reverse(); first.pts.reverse()
                if ej == 1:
                    second.nodes.reverse(); second.pts.reverse()
            first.nodes += second.nodes[1:]
            first.pts += second.pts[1:]
            first.ids += second.ids
            first.name = first.name or second.name
            first.kind = KIND_PARKING if KIND_PARKING in (first.kind, second.kind) else kind_of(first.tags, first.name)
            keep, drop = (i, j) if first is a else (j, i)
            pieces[keep] = first
            alive.discard(drop)
            changed = True
            break  # ends 가 바뀌었으니 다시 계산
    return [pieces[i] for i in sorted(alive)]


class Writer:
    def __init__(self):
        self.buf = io.BytesIO()

    def u8(self, v): self.buf.write(struct.pack(">B", v))
    def u16(self, v): self.buf.write(struct.pack(">H", v))
    def i32(self, v): self.buf.write(struct.pack(">i", v))
    def i64(self, v): self.buf.write(struct.pack(">q", v))

    def s(self, v: str):
        b = v.encode("utf-8")
        self.u16(len(b)); self.buf.write(b)


def build(region: str, raw: dict) -> dict:
    ways = [e for e in raw["elements"] if e["type"] == "way" and "geometry" in e]
    nodes = [e for e in raw["elements"] if e["type"] == "node"]
    pieces = [Piece(w) for w in ways if len(w["geometry"]) >= 2]
    print(f"[{region}] 조각 {len(pieces)}개 → 병합 중", file=sys.stderr)
    merged = merge(pieces)

    tunnels = []
    for p in merged:
        L = length(p.pts)
        if L < MIN_LENGTH_M:
            continue
        pts = simplify(p.pts, SIMPLIFY_M)
        if len(pts) > 65535:
            continue
        tunnels.append((p, L, pts))
    tunnels.sort(key=lambda t: (t[2][0][0], t[2][0][1]))

    strings: dict[str, int] = {}

    def sid(n):
        if not n:
            return -1
        return strings.setdefault(n, len(strings))

    recs = [(p.kind, 1 if p.oneway else 0, sid(p.name), int(round(L)), p.ids[0], pts) for p, L, pts in tunnels]
    parks = [(n["lat"], n["lon"], sid(n.get("tags", {}).get("name")), n["id"]) for n in nodes]

    w = Writer()
    w.buf.write(b"SMT1"); w.u8(1); w.buf.write(region.upper().encode("ascii")[:2])
    w.i32(int(dt.date.today().strftime("%Y%m%d")))
    w.s(raw.get("osm3s", {}).get("timestamp_osm_base", ""))
    w.i32(len(strings))
    for n in sorted(strings, key=strings.get):
        w.s(n)
    w.i32(len(recs))
    for kind, flags, name, L, wid, pts in recs:
        w.u8(kind); w.u8(flags); w.i32(name); w.i32(L); w.i64(wid); w.u16(len(pts))
        for lat, lon in pts:
            w.i32(round(lat * 1e6)); w.i32(round(lon * 1e6))
    w.i32(len(parks))
    for lat, lon, name, nid in parks:
        w.i32(round(lat * 1e6)); w.i32(round(lon * 1e6)); w.i32(name); w.i64(nid)

    ASSETS.mkdir(parents=True, exist_ok=True)
    out_bin = ASSETS / f"{region}.smt"
    out_bin.write_bytes(gzip.compress(w.buf.getvalue(), compresslevel=9, mtime=0))

    # 검증·ODbL 공개용 GeoJSON (앱에는 넣지 않음)
    OUT.mkdir(parents=True, exist_ok=True)
    feats = [{
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[lon, lat] for lat, lon in pts]},
        "properties": {"kind": ["tunnel", "underpass", "parking"][kind], "oneway": bool(flags),
                       "name": p.name, "length_m": L, "osm_way_ids": p.ids},
    } for (kind, flags, _, L, _, pts), (p, _, _) in zip(recs, tunnels)]
    feats += [{
        "type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"kind": "parking_entrance", "osm_node_id": nid},
    } for lat, lon, _, nid in parks]
    (OUT / f"{region}.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "attribution": "© OpenStreetMap contributors (ODbL 1.0)",
                    "features": feats}, ensure_ascii=False), encoding="utf-8")

    by_kind = [sum(1 for r in recs if r[0] == k) for k in range(3)]
    stats = {
        "region": region,
        "osm_base": raw.get("osm3s", {}).get("timestamp_osm_base"),
        "raw_ways": len(ways), "merged": len(merged), "tunnels": len(recs),
        "tunnel": by_kind[0], "underpass": by_kind[1], "parking_ramp": by_kind[2],
        "named": sum(1 for r in recs if r[2] >= 0),
        "oneway": sum(1 for r in recs if r[1]),
        "parking_entrances": len(parks),
        "points": sum(len(r[5]) for r in recs),
        "total_km": round(sum(r[3] for r in recs) / 1000, 1),
        "longest": max(((r[3], p.name) for r, (p, _, _) in zip(recs, tunnels)), default=None),
        "raw_bytes": len(w.buf.getvalue()),
        "asset_bytes": out_bin.stat().st_size,
    }
    (OUT / f"{region}_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="kr", choices=sorted(REGIONS))
    ap.add_argument("--source", default="pbf", choices=["pbf", "overpass"])
    ap.add_argument("--from-cache", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="pbf 를 나이와 상관없이 새로 받기")
    ap.add_argument("--no-odbl", action="store_true", help="docs/odbl 공개 묶음을 쓰지 않음")
    a = ap.parse_args()
    if a.source == "pbf":
        raw = fetch_pbf(a.region, a.from_cache, a.refresh)
        source = f"Geofabrik extract {GEOFABRIK[a.region]}"
    else:
        raw = fetch(a.region, a.from_cache)
        source = "Overpass API"
    stats = build(a.region, raw)
    if not a.no_odbl:
        write_odbl_bundle(a.region, stats, source)
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
