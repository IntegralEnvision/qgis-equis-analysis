# GeoParquet Formatting Requirements for ArcGIS Pro (3.7)

**Purpose:** A reference for writing Parquet files that ArcGIS Pro reads correctly: added to a map or scene, used as a geoprocessing input, or published as a hosted layer. It is written for tooling (DuckDB, GeoPandas, pyarrow, Rust) that writes GeoParquet for Pro users.

**Basis:** Esri's ArcGIS Pro help, version 3.7 (pages last modified 2026-06/08), plus DuckDB 1.5 and GeoPandas docs, all retrieved 2026-10-14. The brief marks each statement as one of:

- **[ESRI]**: stated in Esri documentation.
- **[INFERRED]**: not documented by Esri. It is a conservative recommendation based on the GeoParquet spec or general behavior, and should be validated against a real Pro install.

---

## 1. How Pro actually consumes a Parquet file

This explains most of the constraints below.

ArcGIS Pro does **not** query Parquet in place. Whenever you touch a Parquet file, Pro reads it **in full** and builds a local cache: a feature class if a supported geometry column is found, otherwise a stand-alone table. Every map, query, and geoprocessing operation reads that cache, not the file. **[ESRI]**

Actions that trigger caching **[ESRI]**:

- adding the file to a map or scene;
- opening the Fields view or the Properties dialog in the Catalog pane;
- passing it to a geoprocessing tool or accessing it from ArcPy.

There are two cache types **[ESRI]**:

| Records in file | Cache type | Lifetime |
|---|---|---|
| < 500,000 | In-memory | Deleted when Pro closes |
| ≥ 500,000 | Persistent, on disk | Survives restarts. Default location `C:\Users\<user>\AppData\Local\ESRI\Local Caches\ParquetCacheV1` |

Cache behavior **[ESRI]**:

- **Created per user, per machine.**
- **Invalidated by the source file's last-modified date.** If the file changes, Pro rebuilds the cache.
- **Provides the ObjectID.** The cache supplies the unique identifier field ArcGIS requires, so you do **not** need to write an OBJECTID column.
- **Much larger than the source.** Esri's example is a 20 MB file of 1M points producing a roughly 250 MB cache. The ratio is nonlinear and depends on column count, data types, and geometry type.
- **Can be pre-built** with the `Create Parquet Cache` geoprocessing tool or `arcpy` `CreateParquetCache`, which avoids first-open latency on large files.
- **Retention settings:** Options → Map and Scene → Parquet Cache offers three strategies:
  - *Balanced* (default): deletes caches over 1 GB that haven't been accessed in 30 days.
  - *Preserve caches.*
  - *Preserve disk space:* deletes anything not accessed in 7 days.

  Note that one Esri page describes Balanced the opposite way, saying caches of 1 GB or less are auto-deleted after 30 days. Treat the exact rule as uncertain.
- **Not packageable.** Cached Parquet data cannot be included in map or project packages.

**Implication:** Pro reads the whole file regardless, so Parquet-level optimizations (row-group pruning, bbox covering columns, Hilbert sort order) give Pro **no read-time benefit**. Include them for your *other* consumers (DuckDB, GDAL, web clients), not for Pro.

---

## 2. File-level requirements

| Requirement | Detail | Source |
|---|---|---|
| Extension | Must be `.parquet` | [ESRI] |
| Encryption | Unencrypted only | [ESRI] |
| Compression | Uncompressed, **Snappy**, **GZIP**, or **Zstd** only. Brotli, LZ4, LZ4_RAW, and custom codecs are **not supported** | [ESRI] |
| File name characters | Alphanumeric plus `-` and `_` | [ESRI] |
| File name sanitization | These characters are replaced with `_` in the cached layer name: `( ) [ ] { } & @ ^ . ! # % + ' ~ $`. Any **other** special character is **unsupported** | [ESRI] |
| Single file vs. dataset | The map/scene path takes **one file = one layer**. Hive-partitioned directories and multi-file datasets go through the separate *multifile feature connection* path (GeoAnalytics Desktop), which has its own requirements | [ESRI] |
| Storage location | Local folder, or a cloud storage connection to Amazon S3, Google Cloud Storage, Azure Blob Storage, or Azure Data Lake Storage Gen2 | [ESRI] |

**Practical rule:** name files `[A-Za-z0-9_-]+\.parquet`, and write with `snappy` or `zstd`. DuckDB's default (`snappy`) and GeoPandas' default (`snappy`) are both fine. Do **not** use DuckDB's `brotli`, `lz4`, or `lz4_raw` options.

---

## 3. Column (attribute) requirements

| Requirement | Detail | Source |
|---|---|---|
| Name length | 128 characters or fewer | [ESRI] |
| Name characters | Alphanumeric plus `_` | [ESRI] |
| Name sanitization | Spaces, tabs, line breaks, and `( ) [ ] < > { } & * @ ^ : . = ! # - % \| + ; ' \ / ~ $` are replaced with `_` in the cached field name. Any other special character is **unsupported** | [ESRI] |
| Nesting | **Columns cannot be nested.** No STRUCT, LIST, or MAP columns | [ESRI] |
| Coordinate columns | Separate X/Y/Z, lat/lon, or elevation columns are **not** recognized as geometry; they are read as plain numeric or string fields | [ESRI] |

Notes:

- **Hyphens in column names become underscores.** `sample-id` arrives as `sample_id`. If a downstream join depends on the exact name, use snake_case at the source.
- **Watch for name collisions after sanitization.** For example, `a.b` and `a_b` both become `a_b`. Esri does not document what happens in that case. **[INFERRED]** Avoid it.
- **Flatten all nested data before export.** Common sources of nested columns are DuckDB `LIST` aggregates, Overture-style `names`/`sources` structs, and GeoPandas columns holding dicts or lists. Flatten them into scalar columns, or serialize them to a JSON string column.
- **Prefer conservative scalar types. [INFERRED]** Esri does not publish a Parquet-to-geodatabase type mapping. These are safe:
  - `INT32`/`INT64`, `FLOAT`/`DOUBLE`, `BOOLEAN`, `UTF8` string, `DATE`, and `TIMESTAMP` (milliseconds or microseconds).

  These are riskier, so cast them before export:
  - unsigned integers (especially `UINT64`): cast to `INT64`;
  - `DECIMAL` with high precision: cast to `DOUBLE` or a string;
  - `TIME`, `INTERVAL`, `UUID`, `ENUM`/dictionary-typed categoricals: cast to a string;
  - nanosecond timestamps: cast to microseconds;
  - `VARIANT`/JSON logical types: cast to a string;
  - `BLOB` columns other than the geometry: drop or encode them.
- **Do not write an `OBJECTID` column expecting Pro to use it.** The cache generates its own ObjectID. **[ESRI]** Esri does not say how an existing column named `OBJECTID` is handled; **[INFERRED]** rename it (e.g., `src_oid`) to avoid a conflict.

---

## 4. Geometry requirements (the important part)

### 4.1 Accepted geometry definitions **[ESRI]**

The spatial column must be defined by **one** of:

1. **GeoParquet metadata, version `1.0.0` or `1.1.0`.** This is the `geo` key in the Parquet file's key-value metadata.
2. **A native Parquet geospatial logical type**, `GEOMETRY` or `GEOGRAPHY`. This is the Parquet format-level type, which GeoParquet 2.0 builds on.

Option 2 is new in Pro 3.7. **Pro 3.5 and 3.6 accept only GeoParquet 1.0.0/1.1.0 metadata.** If your audience runs mixed Pro versions, write GeoParquet 1.x metadata. Writing **both** the 1.x metadata and the native type is the most compatible choice (see §6).

If neither is present, the file loads as a **stand-alone table** with no geometry. **[ESRI]**

### 4.2 Exactly one geometry column **[ESRI]**

The file must contain a **single** spatial column. If you have more than one (e.g., `geom` plus `centroid`, or a `bbox_geom`), drop the extras or convert them to WKT strings. **[INFERRED]** Multiple geometry columns are legal in GeoParquet but outside what Pro documents.

### 4.3 One geometry type per file **[ESRI]**

This is the most common source of silent data loss.

- **Pro displays one entity type per layer:** points, lines, or polygons.
- If the file's metadata declares a specific geometry type (GeoParquet `geometry_types`, e.g., `["Point"]`), Pro caches **only** features of that type. **All other features are dropped and inaccessible.**
- If no type is declared (`geometry_types: []`), Pro uses the **type of the first feature** in the file. Again, only matching features are cached, and the rest are silently dropped.
- **GeometryCollection is not supported.**

**Rules to follow:**

1. **Split mixed-geometry data into separate files by type** before export (e.g., `sites_pt.parquet`, `sites_poly.parquet`).
2. **Promote singles to multis for consistency [INFERRED]:** cast every polygon to MultiPolygon (`ST_Multi` in DuckDB, `shapely.multipolygons`/`force_multi` patterns in Python) when a dataset mixes Polygon and MultiPolygon. Esri does not say whether Polygon and MultiPolygon count as one "entity type". Pro's feature model treats both as the *polygon* shape type, so mixing them is *probably* fine. Making them uniform removes the question.
3. **Declare `geometry_types` accurately** in GeoParquet metadata (e.g., `["MultiPolygon"]`). Do not leave it empty if you can avoid it; otherwise the first row determines everything.
4. **Remove or explode GeometryCollections** (e.g., `ST_CollectionExtract` in DuckDB) before export.
5. **No NULL or empty geometries, ever.** **[TESTED 2026-10-14]** In ArcGIS Pro, a file with any NULL geometry, or any empty point (WKB `POINT` with NaN coordinates), **fails to load once it is large enough for the persistent cache** (>= 500,000 rows): 557,691 rows failed both ways - nulls first, nulls last, and as empty points with no nulls at all - while the same rows with every geometry present (539,831) loaded. Small files (4,000-5,000 rows, in-memory cache) with 1,000 nulls or empty points, first or last, loaded. So the in-memory path tolerates them and the persistent-cache builder does not. Do not rely on the small-file behaviour: a file that grows past the threshold stops loading. Put rows without a location in a separate non-spatial table.

### 4.4 Geometry encoding **[INFERRED]**

Esri does not say which GeoParquet geometry *encodings* it reads. GeoParquet 1.1 allows WKB plus GeoArrow-native encodings (`point`, `linestring`, `polygon`, `multipoint`, `multilinestring`, `multipolygon`).

- **Use WKB.** It is the GeoParquet 1.0 baseline and the only encoding guaranteed by the 1.0.0 spec Pro claims to support.
- **Avoid GeoArrow-native encodings** unless you have tested them in Pro. GeoPandas itself labels `geometry_encoding="geoarrow"` experimental and possibly unsupported by readers.
- With the native Parquet `GEOMETRY`/`GEOGRAPHY` logical type, the physical encoding is WKB by definition.

### 4.5 Z and M values **[INFERRED]**

Not documented. GeoParquet 1.x supports Z (e.g., `"Point Z"` in `geometry_types`). Pro scenes can display Z. If you need 3D, declare the Z variants explicitly (`"Point Z"`, `"MultiPolygon Z"`) and test. M values are a lower-confidence case.

### 4.6 Coordinate reference system **[INFERRED — high-priority to test]**

Esri's Parquet pages say **nothing** about CRS handling. Per the GeoParquet spec:

- The `crs` field in the column's `geo` metadata is **PROJJSON**.
- If `crs` is **absent**, the CRS defaults to **OGC:CRS84** (WGS84 longitude/latitude).
- `crs: null` means the CRS is explicitly unknown.

Recommendations:

1. **Always write an explicit PROJJSON `crs`**, including for WGS84 data. Do not rely on the default.
2. **Prefer an EPSG-identifiable CRS.** PROJJSON with an `"id": {"authority": "EPSG", "code": N}` member gives Pro the best chance of mapping it to a known spatial reference.
3. **Projected data is where things go wrong.** If your coordinates are in UTM or a State Plane system and the file's metadata is missing or wrong, Pro will most likely treat meters as degrees, and the features land in the wrong place or don't draw. Verify in Pro by checking the cached layer's spatial reference in Layer Properties → Source.
4. **The safest interchange is EPSG:4326/CRS84 lon/lat [INFERRED].** Pro projects on the fly. If you can tolerate reprojecting at export, it removes the CRS-metadata failure mode entirely. For analysis-grade projected data (kriging grids, etc.), keep the native CRS and verify carefully.
5. **Axis order:** GeoParquet requires **longitude, latitude** (x, y) order even for EPSG:4326. Never write lat/lon order into WKB.

> **DuckDB warning (see §6.1):** DuckDB 1.5.x geometries carry **no CRS**. After `ST_Transform` to a projected CRS, `COPY ... TO` will still declare the default OGC:CRS84, which **mislabels** the data. A third-party validator confirmed this with DuckDB 1.5.5 and EPSG:3857 output, and its author notes that the user cannot fix it from `COPY` alone. Use GeoPandas or SedonaDB for projected output, or patch the footer metadata with pyarrow (§7.3).

---

## 5. Capabilities and limitations once loaded **[ESRI]**

| Capability | Status |
|---|---|
| Display in maps/scenes, symbology | Yes, as a **read-only** feature layer or table |
| Query / selection | Yes |
| Editing | **No.** Read-only layer |
| Geoprocessing input | Yes, from a map layer or directly from a folder or cloud connection (3.6+) |
| Export to geodatabase | Yes, with `Export Features` / `Export Table` |
| Publish hosted layers | Yes (3.6+): web feature layers, vector tile layers and scene layers (each with an associated feature layer), and tables. **Copies all data** |
| Binning | Automatic geosquare binning when there are more than 10,000 rows. Geosquare is the only bin type. You can change the threshold or disable binning |
| XY columns → points | Run `XY Table To Point` on the Parquet table layer (writes a new feature class) |
| Packages (mpkx/ppkx) | **Not supported** |
| License level | Any (Basic/Standard/Advanced) |

### 5.1 Writing Parquet *from* Pro (3.7) **[ESRI]**

The `Export To Parquet` tool (Conversion toolbox) works as follows:

- **Inputs:** shapefile; feature class or table from a file, mobile, or enterprise geodatabase; or a relational database.
- **Output:** a `.parquet` file in a local or shared network directory. **Cloud storage output is not supported.**
- **Parameters:** only input and output. There are no documented options for compression, geometry encoding, GeoParquet version, row-group size, or bbox covering.
- **Licensing:** all license levels.

Treat its output as "Pro-compatible by construction", but not as optimized for cloud-native consumers.

---

## 6. Writer recipes

### 6.1 DuckDB (1.5.x)

The relevant `COPY ... TO` Parquet options (verified against DuckDB 1.5 docs):

- `COMPRESSION`: one of `uncompressed | snappy | gzip | zstd | brotli | lz4 | lz4_raw`. Default `snappy`. **Use `snappy` or `zstd` only.**
- `GEOPARQUET_VERSION`: one of `NONE | V1 | V2 | BOTH`. Default `V1`. `NONE` disables GeoParquet metadata.
  - `V1` writes GeoParquet 1.x `geo` metadata. This is readable by Pro 3.5, 3.6, and 3.7.
  - `V2` targets GeoParquet 2.0 and the native Parquet geometry type. It is readable via Pro 3.7's native-type path. **[INFERRED]** Untested.
  - `BOTH` is the most compatible across Pro versions. **[INFERRED]**
- `ROW_GROUP_SIZE`: irrelevant to Pro (it reads everything), but useful for other readers.

```sql
INSTALL spatial; LOAD spatial;

-- WGS84 point data: the safest case
COPY (
    SELECT
        sample_id,                                    -- snake_case, no hyphens
        CAST(result_value AS DOUBLE)  AS result_value,
        CAST(sample_date  AS DATE)    AS sample_date,
        CAST(analyte      AS VARCHAR) AS analyte,     -- no ENUMs
        geom                                          -- single GEOMETRY column, POINT only
    FROM results
    WHERE geom IS NOT NULL
      AND ST_GeometryType(geom) = 'POINT'
)
TO 'results_pt.parquet'
(FORMAT parquet, COMPRESSION zstd, GEOPARQUET_VERSION 'BOTH');

-- Polygons: normalize to MultiPolygon, strip collections
COPY (
    SELECT site_id, site_name,
           ST_Multi(ST_CollectionExtract(geom, 3)) AS geom   -- 3 = polygonal
    FROM sites
    WHERE NOT ST_IsEmpty(geom)
)
TO 'sites_poly.parquet'
(FORMAT parquet, COMPRESSION zstd, GEOPARQUET_VERSION 'BOTH');
```

**CRS caveat:** As noted in §4.6, DuckDB 1.5.x writes CRS84 by default and cannot attach a projected CRS through `COPY`. For projected data, either:

- reproject to EPSG:4326 before export (`ST_Transform(geom, 'EPSG:xxxx', 'EPSG:4326', always_xy := true)`), **or**
- export, then patch the `geo` metadata with pyarrow (§7.3), **or**
- write through GeoPandas instead (§6.2).

Before relying on the `ST_Transform` signature above, check it against the current DuckDB spatial function reference; the argument names have changed across releases.

### 6.2 GeoPandas (≥ 1.x)

The `GeoDataFrame.to_parquet` signature (verified):

```text
to_parquet(path, index=None, compression='snappy', geometry_encoding='WKB',
           write_covering_bbox=False, schema_version=None, **kwargs)
```

- `geometry_encoding`: `'WKB'` (default) or `'geoarrow'`. **Keep `'WKB'`.**
- `schema_version`: one of `'0.1.0'`, `'0.4.0'`, `'1.0.0'`, `'1.1.0'`, `'2.0.0'`. The default is 1.1.0.
  - **Use `'1.1.0'` or `'1.0.0'` for compatibility with Pro 3.5 and 3.6.**
  - `'2.0.0'` should work only via Pro 3.7's native-type path. **[INFERRED]**
- `compression`: `'snappy'` or `'zstd'`. **Not** `'brotli'` or `'lz4'`.
- GeoPandas writes the GeoDataFrame's CRS as PROJJSON automatically. **This is the easiest way to get correct projected-CRS metadata.**

```python
import geopandas as gpd
from shapely.geometry import MultiPolygon

gdf = gpd.read_file("sites.gpkg")

# 1. One geometry type per file; normalize to multi
gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
polys = gdf[gdf.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
polys["geometry"] = polys.geometry.apply(
    lambda g: g if g.geom_type == "MultiPolygon" else MultiPolygon([g])
)

# 2. Flatten / cast attributes
polys.columns = [c.replace("-", "_").replace(" ", "_") for c in polys.columns]
for c in polys.select_dtypes(include=["category", "uint64"]).columns:
    polys[c] = polys[c].astype("string" if polys[c].dtype.name == "category" else "int64")

# 3. Ensure the CRS is set (written as PROJJSON)
assert polys.crs is not None, "Set a CRS before export"

polys.to_parquet(
    "sites_poly.parquet",
    index=False,
    compression="zstd",
    geometry_encoding="WKB",
    schema_version="1.1.0",
)
```

### 6.3 GDAL/OGR **[INFERRED]**

The GDAL Parquet driver writes GeoParquet 1.x with WKB by default. Useful layer creation options include `COMPRESSION=ZSTD` and `GEOMETRY_ENCODING=WKB`. Check `ogrinfo --format Parquet` on your GDAL build for the exact option names; they vary by version.

```bash
ogr2ogr -f Parquet sites_poly.parquet sites.gpkg \
  -nlt PROMOTE_TO_MULTI -lco COMPRESSION=ZSTD -lco GEOMETRY_ENCODING=WKB
```

---

## 7. Pre-flight validation

### 7.1 Checklist

- [ ] The file name matches `^[A-Za-z0-9_-]+\.parquet$`.
- [ ] Compression is none, snappy, gzip, or zstd in every column chunk.
- [ ] The file is not encrypted.
- [ ] There is exactly one geometry column.
- [ ] The geometry is defined by GeoParquet `geo` metadata at version 1.0.0 or 1.1.0, **or** by a native Parquet GEOMETRY/GEOGRAPHY type (Pro 3.7 only). For mixed Pro fleets, the 1.x metadata is present.
- [ ] Geometry encoding is WKB.
- [ ] Every row has the same geometry type, `geometry_types` is declared and accurate, and there are no GeometryCollections.
- [ ] There are no NULL or empty geometries (NaN-coordinate points count as empty). Pro's persistent cache refuses the file otherwise; see §4.3.
- [ ] `crs` is present as PROJJSON with an EPSG id, coordinates are in x/y (lon/lat) order, and the declared CRS matches the actual coordinates.
- [ ] There are no nested (STRUCT/LIST/MAP) columns.
- [ ] Column names are 128 characters or fewer, are `[A-Za-z0-9_]` only, and have no collisions after sanitization.
- [ ] There are no unsigned 64-bit integers, nanosecond timestamps, ENUMs, or other exotic types.
- [ ] There is no column named `OBJECTID`.

### 7.2 Validation script (pyarrow only)

```python
"""Pre-flight check for ArcGIS Pro Parquet compatibility. Usage: python check_pro.py file.parquet"""
import json, re, sys
import pyarrow.parquet as pq
import pyarrow as pa

OK_CODECS = {"UNCOMPRESSED", "SNAPPY", "GZIP", "ZSTD"}
problems, warnings = [], []

path = sys.argv[1]
fname = path.replace("\\", "/").split("/")[-1]
if not re.fullmatch(r"[A-Za-z0-9_-]+\.parquet", fname):
    warnings.append(f"File name '{fname}' has characters Pro will sanitize or reject")

pf = pq.ParquetFile(path)
md, schema = pf.metadata, pf.schema_arrow

# Compression
codecs = {md.row_group(i).column(j).compression
          for i in range(md.num_row_groups) for j in range(md.num_columns)}
bad = codecs - OK_CODECS
if bad:
    problems.append(f"Unsupported compression: {bad}")

# Nested columns and odd types
for f in schema:
    if pa.types.is_nested(f.type):
        problems.append(f"Nested column not allowed: {f.name} ({f.type})")
    if pa.types.is_uint64(f.type):
        warnings.append(f"UINT64 column {f.name}: cast to INT64")
    if pa.types.is_timestamp(f.type) and f.type.unit == "ns":
        warnings.append(f"Nanosecond timestamp {f.name}: cast to us")
    if pa.types.is_dictionary(f.type):
        warnings.append(f"Dictionary/categorical column {f.name}: cast to string")
    if len(f.name) > 128:
        problems.append(f"Column name > 128 chars: {f.name[:40]}...")
    if re.search(r"[^A-Za-z0-9_]", f.name):
        warnings.append(f"Column '{f.name}' will be renamed (special characters -> _)")
    if f.name.upper() == "OBJECTID":
        warnings.append("Column named OBJECTID: rename to avoid a conflict with Pro's generated OID")

# Sanitization collisions
sanitized = [re.sub(r"[^A-Za-z0-9_]", "_", f.name).lower() for f in schema]
dupes = {n for n in sanitized if sanitized.count(n) > 1}
if dupes:
    problems.append(f"Column names collide after sanitization: {dupes}")

# GeoParquet metadata
kv = schema.metadata or {}
geo = json.loads(kv[b"geo"]) if b"geo" in kv else None
if geo is None:
    warnings.append("No GeoParquet 'geo' metadata: OK only for Pro 3.7 with a native Parquet "
                    "GEOMETRY type; otherwise this loads as a table")
else:
    ver = geo.get("version")
    if ver not in ("1.0.0", "1.1.0"):
        warnings.append(f"GeoParquet version {ver}: Pro documents 1.0.0/1.1.0")
    cols = geo.get("columns", {})
    if len(cols) != 1:
        problems.append(f"{len(cols)} geometry columns: Pro requires exactly one")
    for name, c in cols.items():
        if c.get("encoding", "WKB").upper() != "WKB":
            warnings.append(f"{name}: encoding {c.get('encoding')} (WKB is the safe choice)")
        gt = c.get("geometry_types", [])
        if not gt:
            warnings.append(f"{name}: geometry_types empty, so Pro uses the first feature's type "
                            "and drops all others")
        base = {t.replace(" Z", "").replace(" M", "").replace(" ZM", "") for t in gt}
        if "GeometryCollection" in base:
            problems.append(f"{name}: GeometryCollection is not supported")
        families = {"Point" if "Point" in t else "Line" if "LineString" in t else "Polygon"
                    for t in base if t != "GeometryCollection"}
        if len(families) > 1:
            problems.append(f"{name}: mixed entity types {gt}; Pro keeps only one, the rest are dropped")
        if "crs" not in c:
            warnings.append(f"{name}: no crs, so it defaults to OGC:CRS84. Verify the coordinates "
                            "really are lon/lat")
        elif c["crs"] is None:
            warnings.append(f"{name}: crs is explicitly null (unknown)")

print(f"{path}: {md.num_rows:,} rows, {md.num_row_groups} row groups"
      + ("  (>=500k rows: persistent cache; consider Create Parquet Cache)"
         if md.num_rows >= 500_000 else ""))
for p in problems:
    print("  ERROR  ", p)
for w in warnings:
    print("  WARN   ", w)
sys.exit(1 if problems else 0)
```

### 7.3 Patching CRS metadata after a DuckDB export

This fixes DuckDB's mislabeled projected CRS. It rewrites the file with corrected `geo` metadata and leaves the data untouched. It requires `pyproj` to generate PROJJSON.

```python
import json, pyarrow.parquet as pq
from pyproj import CRS

src, dst, epsg = "out_duckdb.parquet", "out_fixed.parquet", 26917  # e.g., NAD83 / UTM 17N

table = pq.read_table(src)
meta = dict(table.schema.metadata)
geo = json.loads(meta[b"geo"])
for col in geo["columns"].values():
    col["crs"] = CRS.from_epsg(epsg).to_json_dict()   # PROJJSON
meta[b"geo"] = json.dumps(geo).encode()
pq.write_table(table.replace_schema_metadata(meta), dst, compression="zstd")
```

If the file was written with `GEOPARQUET_VERSION 'V2'` or `'BOTH'`, the native Parquet geometry logical type may also carry its own CRS, which this script does not touch. **[INFERRED]** For projected data destined for Pro, write with `'V1'` and patch, or use GeoPandas.

---

## 8. Version matrix

| Capability | Pro 3.3–3.4 | Pro 3.5 | Pro 3.6 | Pro 3.7 |
|---|---|---|---|---|
| Multifile feature connections (GeoAnalytics Desktop) | ✓ | ✓ | ✓ | ✓ |
| Single file → map/scene (cached) | — | ✓ | ✓ | ✓ |
| GeoParquet 1.0.0 / 1.1.0 metadata | — | ✓ | ✓ | ✓ |
| Native Parquet GEOMETRY/GEOGRAPHY type | — | — | — | ✓ |
| Single file as geoprocessing input | — | ? | ✓ | ✓ |
| Publish hosted web layers from Parquet | — | ✗ | ✓ | ✓ |
| `Export To Parquet` tool | — | — | — | ✓ |

`?` = not stated in the 3.5 docs reviewed. `✗` = explicitly unsupported at that release.

Related, outside Pro: ArcGIS Online added a **Parquet feature layer (beta)** in June 2026, produced by Data Pipelines. As of that announcement, it **cannot be used in ArcGIS Pro**, edited, or used for analysis.

---

## 9. Recommended default profile

For files a Pro user will open, absent other constraints:

- **Format:** GeoParquet **1.1.0** metadata (add the native geometry type too if your writer supports writing both).
- **Encoding:** **WKB**.
- **Compression:** **zstd** (or snappy).
- **Geometry:**
  - one column;
  - one entity type per file;
  - polygons and lines promoted to Multi;
  - no collections, NULLs, or empties;
  - `geometry_types` declared.
- **CRS:** explicit PROJJSON with an EPSG id. Use EPSG:4326 lon/lat unless projected coordinates are required, and if they are, verify in Pro.
- **Attributes:**
  - flat scalars only;
  - snake_case ASCII names of 128 characters or fewer;
  - signed integers, double, string, date, and microsecond or millisecond timestamps;
  - no `OBJECTID` column.
- **Files:**
  - one layer per file;
  - names matching `[A-Za-z0-9_-]+\.parquet`;
  - for files of 500k rows or more, tell users to run `Create Parquet Cache` first.

---

## 10. Open questions to test empirically

Esri documentation is silent on all of these:

1. Does Pro honor a projected PROJJSON `crs`, or does it assume CRS84?
2. How does Pro map Parquet types to geodatabase field types, especially for DECIMAL, TIME, and UINT?
3. Are Polygon and MultiPolygon treated as one entity type in the same file?
4. Is Z/M preserved into the cache and into scenes?
5. Does Pro read GeoArrow-native encodings at all?
6. ~~How are NULL and empty geometries handled, particularly in row 1?~~ Answered 2026-10-14: refused by the persistent cache whatever the row; see §4.3.
7. What does `Export To Parquet` actually write: GeoParquet version, encoding, compression, and `geometry_types`? Inspect a sample with `parquet_kv_metadata()` in DuckDB.
8. What happens with an existing `OBJECTID` column or sanitized-name collisions?

---

## Sources

- Esri, *Parquet in ArcGIS Pro* (3.7): https://doc.esri.com/en/arcgis-pro/latest/help/data/parquet/parquet-in-arcgis.html
- Esri, *Parquet in ArcGIS Pro* (3.6): https://pro.arcgis.com/en/pro-app/3.6/help/data/parquet/parquet-in-arcgis.htm
- Esri, *FAQs about using a Parquet file in ArcGIS Pro* (3.7): https://doc.esri.com/en/arcgis-pro/latest/help/data/parquet/faqs-parquet-in-maps.html
- Esri, *Add a Parquet file to a map or scene* (3.7): https://doc.esri.com/en/arcgis-pro/latest/help/data/parquet/add-parquet-to-map.html
- Esri, *Manage Parquet data caches* (3.7): https://doc.esri.com/en/arcgis-pro/latest/help/data/parquet/cached-parquet-data.html
- Esri, *Export To Parquet* tool (3.7): https://doc.esri.com/en/arcgis-pro/latest/tool-reference/conversion/export-to-parquet.html
- Esri blog, *Enhancing support for Parquet files* (Pro 3.5): https://www.esri.com/arcgis-blog/products/arcgis-pro/announcements/geoparquet-arcgis
- Esri blog, *What's New in ArcGIS Data Pipelines (June 2026)*: https://www.esri.com/arcgis-blog/products/arcgis-online/announcements/whats-new-in-arcgis-data-pipelines-june-2026
- DuckDB 1.5, *COPY statement* (Parquet options, `GEOPARQUET_VERSION`): https://duckdb.org/docs/current/sql/statements/copy
- GeoPandas, *GeoDataFrame.to_parquet*: https://geopandas.org/en/stable/docs/reference/api/geopandas.GeoDataFrame.to_parquet.html
- geoparquet-validator (DuckDB CRS mislabel finding): https://pypi.org/project/geoparquet-validator/
- GeoParquet specification releases: https://github.com/opengeospatial/geoparquet/releases
