"""Build an EQuIS Live AdvancedTimeSeries (widgetTypeId 366) config.

The widget renders from userSettings.legendItemIds + legendObjs (control.js);
the editor grid shows legendItems and rebuilds legendItemIds from it on reorder.
All three must agree, keyed by "<logger_series_id>.<freq>.<calc>.<type>".

The time window comes from the WIDGET-level userSettings["days"] (0 = each
series from its own startDate; N = rolling, last N days). The per-series
"days" field does not move the window. See dashboards.md, "EQuIS Live
historical chart". Verified 2026-10-07 on EQuIS Online 7.26.1 by rendering in headless Chromium.

The logger, series and unit come from an existing 366 widget's
userSettings (GET /api/dashboards/{id}/widgets), whose legendObjs carry
them, or from a widget first built in the dashboard editor:

    old = json.loads(existing_widget["userSettings"])["legendObjs"]
    entries = []
    for i, o in enumerate(old.values()):
        entries.append(series_entry(o["logger"], o["series"],
                                    {"series_unit": o["series_unit"]},
                                    "My Facility", PALETTE[i]))
    us = user_settings(entries, days=0)
    check(us)
    widget["userSettings"] = json.dumps(us)    # then PUT/POST the widget
"""
import json

PALETTE = ["#7cb5ec", "#434348", "#90ed7d", "#f7a35c", "#62449a", "#f15c80",
           "#e4d354", "#8d4653", "#ca6828", "#971b1e"]

_OPT = {
    "frequency": {"label": "None", "value": "none"},
    "calculation": {"value": "none", "label": "None"},
    "type": {"label": "Line", "value": "line"},
    "lineType": {"label": "Solid", "value": "Solid",
                 "example": "________________________"},
    "axis": {"label": "Left(Shared)", "value": "left"},
}

GLOBAL_DEFAULTS = {
    "showGridLegend": False, "showChartLegend": True, "showNav": True,
    "showScrollbar": True, "chartHeight": 400, "axisXLabelShow": False,
    "axisYLabelL": "", "axisYLabelR": "", "showDataPoints": False,
    "showDataGaps": False, "highlightDataGaps": False,
    "showGridLineMinMax": False, "showGridLineMinor": True,
    "showGridLineTick": False, "overrideHeartbeat": False,
    "heartbeatSeconds": None, "digits": 2, "stacking": False,
    "timezoneOffsetEnabled": 0, "timezoneOffsetLocal": False, "axis": "left",
    "days": 0, "dateFormat": "YYYY-MM-DD", "lockYAxis": True,
    "qualifierColors": {}, "appendUnit": True, "axisYLabelShowP": True,
    "seriesConfigParams": {k: {"label": "Default", "value": "default"} for k in (
        "frequencyParam", "calculationParam", "typeParam", "lineTypeParam",
        "axisParam", "showPlotBandParam", "showRemarksParam", "colorParam",
        "hideSmallerParam", "hideLargerParam")},
    "qualifierOptions": [],
}


def series_entry(logger, series, details, facility_name, color,
                 start_iso="2025-01-01T00:00:00.000Z", days=0):
    sid = "%d.none.none.line" % series["logger_series_id"]
    unit = details["series_unit"]
    desc = series["series_desc"]
    tooltip = json.dumps({"valueDecimals": 2, "valueSuffix": " " + unit})
    obj = {
        "facility": {"FacilityId": logger["facility_id"], "FacilityName": facility_name},
        "logger": logger,
        "series": {"logger_series_id": series["logger_series_id"],
                   "series_desc": desc, "series_function": None,
                   "series_function_info": None},
        "frequency": dict(_OPT["frequency"]), "calculation": dict(_OPT["calculation"]),
        "type": dict(_OPT["type"]), "lineType": dict(_OPT["lineType"]),
        "showPlotband": False, "seriesColor": color, "hideSmaller": False,
        "hideLarger": False, "axis": dict(_OPT["axis"]), "min": None,
        "minHide": False, "max": None, "maxHide": False, "showRemarks": False,
        "days": days, "startDate": start_iso, "id": sid, "invertYAxis": False,
        "selectedQualifierObjs": [], "stack": unit, "series_unit": unit,
        "tooltip": tooltip, "selectedQualifiers": [],
        "qualifierObjs": {"qualifiers": [], "indices": []},
    }
    y, m, d = start_iso[:10].split("-")
    item = {
        "Series": desc, "Stack": unit, "Tooltip": tooltip,
        "LoggerSeriesId": series["logger_series_id"], "Frequency": "None",
        "Calculation": "None",
        "Logger": (logger.get("logger_desc") or "").strip() or logger["logger_code"],
        "Axis": "Left(Shared)", "Max": None, "Min": None, "SeriesColor": color,
        "ShowPlotband": False, "Type": "Line", "LineType": "Solid",
        "HideLarger": False, "HideSmaller": False, "SelectedQualifiers": [],
        "ShowRemarks": False, "InvertYAxis": False, "Days": days,
        "StartDate": "%s/%s/%s" % (m, d, y), "Id": sid, "customOffset": "Local",
    }
    return sid, item, obj


def user_settings(entries, **overrides):
    us = json.loads(json.dumps(GLOBAL_DEFAULTS))
    us.update(overrides)
    us["legendItemIds"] = [sid for sid, _, _ in entries]
    us["legendItems"] = [item for _, item, _ in entries]
    us["legendObjs"] = {sid: obj for sid, _, obj in entries}
    return us


def check(us):
    """The invariants the widget's control.js and editor.js rely on."""
    ids = us["legendItemIds"]
    assert ids, "no series"
    assert len(set(ids)) == len(ids), "duplicate series key"
    assert set(ids) == set(us["legendObjs"]), "legendItemIds != legendObjs keys"
    assert ids == [li["Id"] for li in us["legendItems"]], "legendItems out of order"
    for sid in ids:
        o = us["legendObjs"][sid]
        assert o["id"] == sid
        assert sid.split(".")[0] == str(o["series"]["logger_series_id"])
        assert o["logger"]["logger_id"], "logger missing"
    return True
