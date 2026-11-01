# AutoDim tunnel database (OpenStreetMap derivative)

AutoDim is an Android app that dims an external monitor (for example Samsung DeX in a car). Its Pro tunnel feature
uses a small offline database of road tunnels, underpasses and underground-parking entrances in South Korea.
That database is derived from OpenStreetMap, so it is published here under the same licence.

**© OpenStreetMap contributors.** Data is available under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
See [openstreetmap.org/copyright](https://www.openstreetmap.org/copyright).

## Files

| File | What it is |
|---|---|
| `kr.geojson` | The derived database, South Korea. Same content as the app's binary asset `kr.smt`. |
| `kr_manifest.json` | Source extract, OSM data timestamp, build date, counts, and SHA-256 of `kr.geojson` and `kr.smt`. |
| `extract_tunnels.py` | The script that builds both files from OpenStreetMap. |

### `kr.geojson` features
- **LineString**: a tunnel bore. Properties:
  - `kind`: `tunnel`, `underpass` or `parking`
  - `oneway`: `true` if the point order is the direction of travel
  - `name`: tunnel name if OSM has one (`tunnel:name:ko`, `tunnel:name`, or a `name` that contains 터널/지하차도/굴)
  - `length_m`: length in metres
  - `osm_way_ids`: the OSM ways merged into this feature
- **Point**: an underground-parking entrance (`kind` = `parking_entrance`, `osm_node_id`).

### How it was made
1. Read the Geofabrik South Korea extract (`south-korea-latest.osm.pbf`; the file's MD5 is checked).
2. Keep drivable roads (`highway=motorway … service`) with `tunnel=yes` or `tunnel=building_passage`.
   Keep nodes with `amenity=parking_entrance` + `parking=underground`.
3. Merge ways that share an end node and are not at a junction. Drop tunnels shorter than 50 m.
   Simplify with Douglas–Peucker at 3 m.

To rebuild (Python 3.10+, `pip install osmium certifi`):

```
python extract_tunnels.py --region kr --refresh
```

Run outside the app's source tree, the script writes to `./brain/src/main/assets/tunnels/` and `./docs/odbl/`
under the current folder.

## Contact
monsterlabskr@gmail.com

---

## 한국어 요약
AutoDim(덱스 외부 화면 자동 밝기 앱)의 Pro 터널 미리 감지에 쓰는 **한국 터널·지하차도·지하주차장 입구 DB**입니다.
OpenStreetMap에서 만든 파생 데이터베이스라서 같은 라이선스(ODbL 1.0)로 공개합니다.
**© OpenStreetMap contributors** — [openstreetmap.org/copyright](https://www.openstreetmap.org/copyright).
파일 설명과 만드는 방법은 위와 같습니다. 문의: monsterlabskr@gmail.com
