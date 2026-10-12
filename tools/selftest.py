"""End-to-end check of the plugin's non-UI code against the live EQuIS API.

Run with the QGIS python so the qgis.* modules are importable:

  "C:\\Program Files\\QGIS 4.0.2\\bin\\python-qgis.bat" tools\\selftest.py

Add --report to also execute a report (slow: minutes).
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402

from equis_loader import settings as settings_store  # noqa: E402
from equis_loader.equis_api import EquisClient  # noqa: E402
from equis_loader.layer_builder import (  # noqa: E402
    build_feature_layer,
    build_report_layer,
    find_coordinate_columns,
)

FAILURES = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print("[%s] %s%s" % (status, label, (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)
    return condition


def main():
    qgs = QgsApplication([], False)
    qgs.initQgis()
    try:
        cfg = settings_store.env_defaults()
        if not check("read .env credentials", bool(cfg["api_key"]), cfg["site"]):
            return 1

        client = EquisClient(cfg["site"], cfg["api_key"])

        facilities = client.list_facilities()
        check("list facilities", len(facilities) > 0, "%d found" % len(facilities))
        if not facilities:
            return 1

        # Prefer a facility we know holds data; otherwise take the first.
        facility = next(
            (f for f in facilities if f["id"] == 1000002), facilities[0]
        )
        print("\n-- facility: %s (id=%s wkid=%s)" % (
            facility["name"], facility["id"], facility["wkid"]))

        layers, service_wkid = client.facility_layers(facility["id"])
        check("facility layers", len(layers) > 0,
              ", ".join("%s:%s" % (l["id"], l["name"]) for l in layers))

        # Find a layer that actually has features.
        target = None
        for layer in layers:
            count = client.layer_count(facility["id"], layer["id"])
            layer["count"] = count
            print("   layer %s '%s' -> %s features" % (layer["id"], layer["name"], count))
            if count and target is None:
                target = layer
        check("a layer has features", target is not None)
        if target is None:
            return 1

        # Page a bounded slice through the real iterator.
        pages = list(
            client.iter_layer_features(facility["id"], target["id"], limit=2500)
        )
        total = sum(len(f) for _, f in pages)
        check("paged fetch", total == 2500,
              "%d features over %d page(s)" % (total, len(pages)))

        layer, skipped = build_feature_layer(
            "selftest", pages, fallback_wkid=facility["wkid"]
        )
        check("build feature layer", layer is not None and layer.isValid())
        if layer is None:
            return 1

        check("feature count matches", layer.featureCount() == total,
              "%d in layer" % layer.featureCount())
        check("layer has CRS", layer.crs().isValid(), layer.crs().authid())
        check("geometry present", skipped < total,
              "%d of %d without geometry" % (skipped, total))
        check("extent is real", not layer.extent().isEmpty(),
              layer.extent().toString(2))
        check("fields mapped", layer.fields().count() > 5,
              "%d fields" % layer.fields().count())

        feat = next(layer.getFeatures(), None)
        if feat is not None:
            print("   sample: code=%r geom=%s" % (
                feat["code"] if "code" in [f.name() for f in layer.fields()] else "?",
                feat.geometry().asWkt()[:60] if feat.hasGeometry() else "none"))

        # Drain the layer completely: paging past the end returns HTTP 500 on
        # this service, so a full read is the case that catches regressions.
        if "--full" in sys.argv:
            drained = list(client.iter_layer_features(facility["id"], target["id"]))
            got = sum(len(f) for _, f in drained)
            check("full drain reaches the layer count", got == target["count"],
                  "%d of %d over %d pages" % (got, target["count"], len(drained)))

        # Location filtering is applied client-side here because the service
        # ignores where/objectIds on feature queries.
        sample_codes = []
        for _, feats in pages[:1]:
            for feat in feats[:2]:
                code = (feat.get("attributes") or {}).get("code")
                if code:
                    sample_codes.append(str(code))
        if sample_codes:
            filtered = list(
                client.iter_layer_features(facility["id"], target["id"],
                                           out_fields="code,x,y",
                                           location_codes=sample_codes)
            )
            found = sorted({str(f["attributes"]["code"])
                            for _, fl in filtered for f in fl})
            check("location filter returns only those codes",
                  found == sorted(sample_codes),
                  "%s -> %s" % (sample_codes, found))

        reports = client.list_reports(facility["id"])
        spatial = [r for r in reports if r["spatial"]]
        check("list reports", len(reports) > 0,
              "%d total, %d map-capable" % (len(reports), len(spatial)))

        # Verify the CSV coordinate-column detection against real headers.
        header_cases = [
            (["SYS_LOC_CODE", "X_COORD", "Y_COORD", "RESULT"], True),
            (["LOC", "LONGITUDE", "LATITUDE"], True),
            (["A", "B", "C"], False),
        ]
        ok = True
        for headers, expected in header_cases:
            xi, yi = find_coordinate_columns(headers)
            if (xi >= 0 and yi >= 0) != expected:
                ok = False
        check("coordinate column detection", ok)

        # Build a report layer from synthetic rows in the facility's CRS.
        headers = ["SYS_LOC_CODE", "X_COORD", "Y_COORD", "RESULT_NUMERIC"]
        rows = [
            ["MW-1", "-434427.72", "5051058.30", "1.5"],
            ["MW-2", "-434400.00", "5051000.00", "2.5"],
            ["MW-3", "", "", "3.0"],  # no coordinates -> attribute-only row
        ]
        rlayer, placed, unplaced = build_report_layer(
            "selftest report", headers, rows, facility["wkid"]
        )
        check("build report layer", rlayer is not None and rlayer.isValid())
        check("report rows mapped", placed == 2 and unplaced == 1,
              "%d placed / %d unplaced" % (placed, unplaced))
        if rlayer is not None:
            names = [f.name() for f in rlayer.fields()]
            check("report numeric typing",
                  rlayer.fields().at(names.index("RESULT_NUMERIC")).typeName().lower()
                  in ("double", "real", "double precision"),
                  rlayer.fields().at(names.index("RESULT_NUMERIC")).typeName())

        # The chemical workflow: analytes -> aggregate -> joined layer.
        if "--report" not in sys.argv:
            from equis_loader.report_data import ReportTable, MAXIMUM, MOST_RECENT
            from equis_loader.layer_builder import build_joined_layer, style_by_result

            fake_headers = ["SYS_LOC_CODE", "SAMPLE_DATE", "CAS_RN",
                            "CHEMICAL_NAME", "RESULT_NUMERIC", "RESULT_UNIT",
                            "DETECT_FLAG"]
            codes = []
            for _, feats in pages[:1]:
                for feat in feats[:3]:
                    code = (feat.get("attributes") or {}).get("code")
                    if code:
                        codes.append(str(code))
            fake_rows = []
            for i, code in enumerate(codes):
                fake_rows.append([code, "01/01/2020", "79-01-6", "TCE",
                                  str(1.0 + i), "ug/L", "Y"])
                fake_rows.append([code, "01/01/2022", "79-01-6", "TCE",
                                  str(0.5 + i), "ug/L", "Y"])
            if fake_rows:
                table = ReportTable(fake_headers, fake_rows)
                summary = table.aggregate("TCE", "79-01-6", MAXIMUM)
                joined, matched, unmatched = build_joined_layer(
                    "selftest join", pages, summary, "TCE",
                    fallback_wkid=facility["wkid"]
                )
                check("joined layer keeps every location",
                      joined is not None
                      and joined.featureCount() == sum(len(f) for _, f in pages),
                      "%d features, %d matched" % (
                          joined.featureCount() if joined else 0, matched))
                check("joined layer matched the report locations",
                      matched == len(codes), "%d of %d" % (matched, len(codes)))
                check("styling applies to a real layer",
                      style_by_result(joined) or matched < 3,
                      "renderer %s" % type(joined.renderer()).__name__)

        if "--report" in sys.argv:
            # 2000001 is a known-good analytical report for this facility.
            runnable = [r for r in reports if r["id"] == 2000001] or spatial or reports
            if runnable:
                rpt = runnable[0]
                print("\n-- running report %s '%s' (slow)" % (rpt["id"], rpt["name"]))
                rheaders, rrows, truncated = client.report_csv(
                    rpt["id"], facility["id"], max_rows=20000
                )
                check("report executed", len(rheaders) > 0,
                      "%d cols x %d rows%s" % (len(rheaders), len(rrows),
                                               " (truncated)" if truncated else ""))
                # A sane report has a modest number of columns and at least one
                # row; a huge column count means the CSV was mis-parsed.
                check("report shape is sane",
                      0 < len(rheaders) < 2000 and len(rrows) > 0,
                      "%d cols x %d rows" % (len(rheaders), len(rrows)))
                xi, yi = find_coordinate_columns(rheaders)
                print("   coord columns -> x=%s y=%s" % (xi, yi))
                built, placed, unplaced = build_report_layer(
                    rpt["name"], rheaders, rrows, facility["wkid"]
                )
                check("report layer built", built is not None,
                      "%d placed / %d unplaced" % (placed, unplaced))
                check("report rows produced features", placed + unplaced > 0,
                      "%d placed / %d unplaced" % (placed, unplaced))

                # Reports honour `where`, unlike feature queries.
                loc_col = next((i for i, h in enumerate(rheaders)
                                if h.strip().upper() == "SYS_LOC_CODE"), -1)
                if loc_col >= 0 and rrows:
                    codes = sorted({r[loc_col] for r in rrows[:500]
                                    if len(r) > loc_col and r[loc_col].strip()})[:2]
                    clause = client.location_where(codes)
                    print("   filtering report by %s" % clause)
                    fh, frows, _ = client.report_csv(
                        rpt["id"], facility["id"], where=clause, max_rows=20000
                    )
                    fcol = next((i for i, h in enumerate(fh)
                                 if h.strip().upper() == "SYS_LOC_CODE"), -1)
                    got = sorted({r[fcol] for r in frows if len(r) > fcol})
                    check("report where filters to those locations",
                          got == codes and len(frows) < len(rrows),
                          "%d rows, locs=%s" % (len(frows), got))

        print("\n%s" % ("ALL CHECKS PASSED" if not FAILURES
                        else "FAILURES: %s" % ", ".join(FAILURES)))
        return 1 if FAILURES else 0
    finally:
        qgs.exitQgis()


if __name__ == "__main__":
    sys.exit(main())
