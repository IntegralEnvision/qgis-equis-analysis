"""Small SVG sparklines used as per-location map markers.

Hand-written SVG rather than matplotlib: one marker per location is needed, and
matplotlib's SVG backend is far too slow (and too heavy in markup) for a few
hundred of them.

Markers are embedded in the feature itself as a base64 data URI rather than
written to files. A path into the temp directory dangles as soon as the machine
sweeps it, which would leave a persistent GeoPackage layer with missing symbols
the next time the project opened.
"""

import base64

# Marker canvas, in SVG user units. The label band across the top carries the
# location code and, when markers roll between analytes, the analyte name; the
# trend keeps the rest, so labelling never squashes the line.
#
# 76 wide rather than 60 because chemical names are long: at this width
# "Trichloroethylene" and "Vinyl chloride" fit whole, where narrower markers
# truncated most real analyte names into ambiguity.
WIDTH = 76.0
LABEL_HEIGHT = 10.0
ANALYTE_HEIGHT = 9.0
PLOT_HEIGHT = 28.0
HEIGHT = LABEL_HEIGHT + PLOT_HEIGHT
PAD_X = 3.0
PAD_Y = 4.0

# Roughly the width of one character at FONT_SIZE in a condensed sans face;
# used to decide when a label has to be truncated.
FONT_SIZE = 7.5
ANALYTE_FONT_SIZE = 6.5
CHAR_WIDTH = 4.3
ANALYTE_CHAR_WIDTH = CHAR_WIDTH * ANALYTE_FONT_SIZE / FONT_SIZE
MAX_LABEL_CHARS = int((WIDTH - 2 * PAD_X) / CHAR_WIDTH)
MAX_ANALYTE_CHARS = int((WIDTH - 2 * PAD_X) / ANALYTE_CHAR_WIDTH)

# Corner radius of the marker frame. QGIS draws these markers around 40 px
# wide and scales the SVG to fit, so a radius chosen to look right in the
# 76-unit source has to survive that reduction: the previous 2.5 rounded off
# less than a whole pixel once scaled, which is why the marker read as a
# plain rectangle. Compared side by side, 5 is where the corner is visibly
# softened at map size without the frame starting to look like a lozenge.
CORNER_RADIUS = 5.0
# The analyte band is nested inside the frame, so its corners are tighter -
# matching the frame's radius would push its curve outside the border.
BAND_RADIUS = 3.5

LINE_COLOUR = "#1b6ec2"
FILL_COLOUR = "#1b6ec2"
BACKGROUND = "#ffffff"
BORDER = "#8fa9c4"
LABEL_COLOUR = "#1f2d3a"
ANALYTE_COLOUR = "#8a4b08"
ANALYTE_BAND = "#fdf3e3"
# The peak-value caption. Muted and small: it is a scale reference, not part
# of the trend, and must not compete with the location name above it.
SCALE_COLOUR = "#54636f"
SCALE_FONT_SIZE = 5.4
# The caption gets its own strip above the plot rather than floating over it.
# Overlaid, a tall spike ran straight through the number and both became
# unreadable - and a spike is exactly what the number is there to explain.
SCALE_HEIGHT = 6.0


def _escape(text):
    """Minimal XML escaping; location codes can contain & and angle brackets."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _shorten(text, limit=MAX_LABEL_CHARS):
    """Trim a location code to something that fits the marker.

    Codes are often prefixed alike (MW-MR26, MW-MR27), so the tail is the part
    that distinguishes them and the front is what gets dropped.
    """
    text = str(text).strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[-limit:] if limit else ""
    return "…" + text[-(limit - 1):]


def _shorten_head(text, limit=MAX_ANALYTE_CHARS):
    """Trim a chemical name, keeping the front.

    The opposite of `_shorten`: location codes share a prefix so their tail
    distinguishes them, whereas chemical names are distinguished by their
    start - "Trichloroeth…" is readable, "…roethylene" is not.
    """
    text = str(text).strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def _scale(points, top_offset=0.0):
    """Map (date, value) pairs onto the plot band below the captions.

    `top_offset` is the extra height taken by the captions above the plot -
    the analyte name and the scale reading - so the trend is pushed down
    rather than drawn over them.
    """
    times = [p["date"].timestamp() for p in points]
    values = [p["value"] for p in points]
    t0, t1 = min(times), max(times)
    v0, v1 = min(values), max(values)
    span_t = (t1 - t0) or 1.0
    span_v = (v1 - v0) or 1.0

    top = LABEL_HEIGHT + top_offset
    bottom = HEIGHT + top_offset
    coords = []
    for point, t, v in zip(points, times, values):
        x = PAD_X + (t - t0) / span_t * (WIDTH - 2 * PAD_X)
        # SVG y grows downward, so invert within the plot band.
        y = bottom - PAD_Y - (v - v0) / span_v * (bottom - top - 2 * PAD_Y)
        coords.append((x, y, point))
    return coords


def render_svg(points, detected_key="detect", label=None, analyte=None,
               show_scale=True):
    """SVG text for one location's series, or None if there is nothing to draw.

    `label` is drawn across the top of the marker - normally the location's
    SYS_LOC_CODE, so a map of sparklines is readable without clicking.

    `analyte` adds a second caption naming the chemical. That is essential
    when markers roll between analytes: without it the map shows a trend with
    no indication of what it measures, which is worse than showing nothing.
    The marker grows to make room rather than squeezing the plot, so a
    single-analyte marker keeps its usual proportions.

    `show_scale` prints the peak value at the top of the plot. Every marker
    is scaled to its OWN minimum and maximum, so without it two markers with
    identically shaped peaks can differ by orders of magnitude and the map
    gives no hint of it - a spike at one location reads exactly like a spike
    at another a thousand times larger. One number is enough to anchor the
    scale, and matches the caption the depth profile already carries.
    """
    usable = [p for p in points if p.get("value") is not None and p.get("date")]
    if len(usable) < 1:
        return None

    analyte_band = ANALYTE_HEIGHT if analyte else 0.0
    # Reserved above the plot, and the marker grows to match: taking the room
    # out of the plot instead would flatten the very trend being labelled.
    scale_band = SCALE_HEIGHT if show_scale else 0.0
    height = HEIGHT + analyte_band + scale_band
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%g" height="%g" '
        'viewBox="0 0 %g %g">' % (WIDTH, height, WIDTH, height),
        '<rect x="0.5" y="0.5" width="%g" height="%g" rx="%g" fill="%s" '
        'stroke="%s" stroke-width="1" opacity="0.92"/>'
        % (WIDTH - 1, height - 1, CORNER_RADIUS, BACKGROUND, BORDER),
    ]

    if analyte:
        # Tinted so the chemical reads as a separate fact from the location,
        # and stays legible when many markers overlap. Drawn as a path rather
        # than a rect because only its top corners are rounded: a uniformly
        # rounded rect would either bulge past the frame it sits inside or
        # leave a visible notch where its square bottom meets the curve.
        radius = BAND_RADIUS
        parts.append(
            '<path d="M1,%g V%g A%g,%g 0 0 1 %g,1 H%g A%g,%g 0 0 1 %g,%g V%g Z" '
            'fill="%s"/>'
            % (1 + analyte_band, 1 + radius, radius, radius, 1 + radius,
               WIDTH - 1 - radius, radius, radius, WIDTH - 1, 1 + radius,
               1 + analyte_band, ANALYTE_BAND)
        )
        parts.append(
            '<text x="%g" y="%g" font-family="sans-serif" font-size="%g" '
            'font-weight="700" fill="%s" text-anchor="middle">%s</text>'
            % (WIDTH / 2.0, analyte_band - 2.2, ANALYTE_FONT_SIZE,
               ANALYTE_COLOUR, _escape(_shorten_head(analyte)))
        )

    if label:
        text = _shorten(label)
        parts.append(
            '<text x="%g" y="%g" font-family="sans-serif" font-size="%g" '
            'font-weight="600" fill="%s" text-anchor="middle">%s</text>'
            % (WIDTH / 2.0, analyte_band + LABEL_HEIGHT - 2.4, FONT_SIZE,
               LABEL_COLOUR, _escape(text))
        )
        # A hairline under the label keeps it from reading as part of the plot.
        parts.append(
            '<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="%s" '
            'stroke-width="0.6" opacity="0.55"/>'
            % (PAD_X, analyte_band + LABEL_HEIGHT - 0.5, WIDTH - PAD_X,
               analyte_band + LABEL_HEIGHT - 0.5, BORDER)
        )

    # The peak, named once. Drawn before the trend so the line crosses over
    # it rather than being interrupted by it, and right-aligned because the
    # series is read left to right - the eye lands on the number last, after
    # the shape it describes.
    if show_scale:
        peak = max(p["value"] for p in usable)
        unit = ""
        for p in usable:
            unit = (p.get("unit") or "").strip()
            if unit:
                break
        caption = _trim_number(peak)
        if unit:
            caption += " " + unit
        parts.append(
            '<text x="%g" y="%g" font-family="sans-serif" font-size="%g" '
            'fill="%s" text-anchor="end">%s</text>'
            % (WIDTH - PAD_X,
               analyte_band + LABEL_HEIGHT + SCALE_FONT_SIZE - 0.6,
               SCALE_FONT_SIZE, SCALE_COLOUR, _escape(caption))
        )

    coords = _scale(usable, top_offset=analyte_band + scale_band)
    if len(coords) > 1:
        path = " ".join("%s%.2f,%.2f" % ("M" if i == 0 else "L", x, y)
                        for i, (x, y, _) in enumerate(coords))
        parts.append(
            '<path d="%s" fill="none" stroke="%s" stroke-width="1.4" '
            'stroke-linejoin="round" stroke-linecap="round"/>' % (path, LINE_COLOUR)
        )

    # Only the endpoints are marked: at this size a dot per sample is mush.
    for index in (0, len(coords) - 1):
        x, y, point = coords[index]
        detected = not point.get("at_limit") and str(
            point.get(detected_key, "")).upper() in ("Y", "YES", "TRUE", "1")
        if detected:
            parts.append('<circle cx="%.2f" cy="%.2f" r="2.1" fill="%s"/>'
                         % (x, y, FILL_COLOUR))
        else:
            parts.append(
                '<circle cx="%.2f" cy="%.2f" r="2.0" fill="%s" stroke="%s" '
                'stroke-width="1.1"/>' % (x, y, BACKGROUND, LINE_COLOUR)
            )
        if index == 0 and len(coords) == 1:
            break

    parts.append("</svg>")
    return "".join(parts)


def svg_datauri(svg):
    """Encode SVG markup as the base64 form QGIS accepts for SVG markers.

    QGIS renders raw markup too, but base64 keeps the value on one line and
    free of quotes and angle brackets, which matters because it is stored in a
    feature attribute and written into GeoPackage and project XML.
    """
    if not svg:
        return None
    return "base64:" + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def embed_sparklines(series_by_location, label_locations=True, analyte=None):
    """Render each location's marker into the feature itself.

    Returns {location: "base64:..."} with no files involved, so the symbology
    survives both a temp sweep and the project being reopened on another
    machine. Roughly 450-600 bytes per location, and only locations that
    qualify get an entry, so the cost tracks the markers actually drawn.

    `analyte` captions each marker with the chemical it plots, which markers
    that roll between analytes need in order to be readable at all.
    """
    embedded = {}
    for location, points in series_by_location.items():
        svg = render_svg(points, label=location if label_locations else None,
                         analyte=analyte)
        uri = svg_datauri(svg)
        if uri:
            embedded[location] = uri
    return embedded

# --------------------------------------------------------- depth profiles

# A profile is taller than it is wide: it plots depth down the page, and a
# borehole is deep relative to the spread of its concentrations.
PROFILE_WIDTH = 62.0
PROFILE_PLOT_HEIGHT = 54.0
PROFILE_AXIS = 9.0          # room under the plot for the value axis caption
PROFILE_STEM = "#7a8b9c"
PROFILE_INTERVAL = "#1b6ec2"
PROFILE_GRID = "#d6dfe8"
PROFILE_DEPTH_COLOUR = "#54636f"
PROFILE_FONT_SIZE = 5.6

# One colour per analytic method. A sample is routinely analysed twice, and
# the two can disagree about whether the analyte was detected at all - so
# they are drawn as separate series rather than as scatter on a single line.
# Ordered for contrast at marker size, not for prettiness.
PROFILE_SERIES = ("#1b6ec2", "#c2551b", "#2e8b57", "#8b2e8b", "#b8860b")


def _profile_depth(entry):
    """The depth to plot a result at: the middle of its sampled interval.

    A result belongs to an interval, not a point - "10 to 15 m" - so the
    midpoint is what places it honestly on a depth axis. The interval itself
    is drawn as a bar, so nothing about its extent is hidden by this choice.
    """
    top = entry.get("start_depth")
    base = entry.get("end_depth")
    if top is None and base is None:
        return None
    if top is None:
        return base
    if base is None:
        return top
    return (top + base) / 2.0


def _distinguish_labels(labels):
    """Reduce series labels to the part that tells them apart.

    A label is "date method fraction", but the legend is about ten
    characters wide on a 62-unit marker, so spending it on words every line
    shares leaves nothing to identify them by. Two passes:

      - drop any whole space-separated word common to every label, which
        removes the method and fraction when only the date differs (and the
        date when only the method does);
      - then drop a shared date prefix, since a hole logged four times in
        one year gives "2020-03-11", "2020-06-02"... all of which truncate
        to "2020-03-1…".

    Never returns an empty label: if trimming would leave nothing, the
    untrimmed labels are kept.
    """
    kept = [str(label or "") for label in labels]
    present = [text for text in kept if text]
    if len(present) < 2:
        return kept

    # Words shared by every label carry no information about which line is
    # which - the method when several dates share one method, say.
    common = set(present[0].split())
    for text in present[1:]:
        common &= set(text.split())
    if common:
        trimmed = [" ".join(w for w in text.split() if w not in common)
                   if text else text for text in kept]
        # Identical labels trim to nothing at all, which would leave the
        # legend blank; keep them whole and let the caller truncate.
        if all(t for t in trimmed if t != "") and any(trimmed):
            kept = trimmed
            present = [t for t in kept if t]

    if len(present) < 2:
        return kept

    # Then the shared head of the dates themselves.
    for width in (7, 4):
        prefix = present[0][:width] + "-"
        if len(prefix) > 1 and all(t.startswith(prefix) for t in present):
            trimmed = [t[len(prefix):] if t else t for t in kept]
            if all(t for t in trimmed if t != ""):
                return trimmed
    return kept


def profile_series(entries):
    """Group results into one series per sampling event, in a stable order.

    A profile is a hole logged on a DATE. Re-sampling the same hole a year
    later is a second profile of the same hole, not more points on the first
    - concentrations move between events, so joining them down the hole
    would draw a line through readings that were never in the ground at the
    same time. Method and fraction split a series for the same reason: two
    methods are two analyses, and dissolved and total measure different
    things.

    Ordered by how many results each contributed so the densest series is
    drawn first and takes the first colour, which keeps a map of profiles
    consistent between locations.

    Returns [(label_or_None, [(depth, entry), ...]), ...].
    """
    def series_key(entry):
        """What makes two results different series on one profile.

        Date, method and fraction. Any of them alone names the series;
        together they are combined, so a profile never merges lines that
        mean different things.
        """
        bits = [str(entry.get(name) or "").strip()
                for name in ("date", "method", "fraction")]
        return " ".join(b for b in bits if b)

    grouped = {}
    for entry in entries or []:
        grouped.setdefault(series_key(entry), []).append(entry)
    if len(grouped) <= 1:
        only = list(grouped) or [""]
        return [(only[0] or None, profile_points(entries))]
    # Chronological when the series are sampling events, densest-first
    # otherwise. A reader scanning several events down one hole is following
    # time, so ordering those by how many results each happened to return
    # would shuffle the legend into a meaningless order. The key leads with
    # the date, so sorting on it IS chronological.
    if any(str(e.get("date") or "").strip() for e in entries or []):
        ordered = sorted(grouped.items(), key=lambda kv: kv[0])
    else:
        ordered = sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    out = []
    for label, group in ordered:
        points = profile_points(group)
        if points:
            out.append((label or None, points))
    return out


def profile_points(entries):
    """Keep the results a depth profile can actually draw, shallowest first.

    A result with no depth cannot be placed on a depth axis, and a profile
    built from one depth is a single mark that says nothing a plain circle
    does not already say - so the caller is told to draw something else by
    getting fewer points back than it passed in.
    """
    usable = []
    for entry in entries or []:
        if entry.get("value") is None:
            continue
        depth = _profile_depth(entry)
        if depth is None:
            continue
        usable.append((depth, entry))
    usable.sort(key=lambda pair: pair[0])
    return usable


def render_profile_svg(entries, label=None, analyte=None, unit=None):
    """A depth profile: depth down the Y axis from 0 at the top, value across.

    This is the right picture for a soil boring, where every result shares
    one coordinate and the map alone cannot tell 2 m from 40 m. A sparkline
    would be wrong here: these results are not a series through time, they
    are one sampling event down a hole.

    Returns None when there is nothing worth drawing - fewer than two
    distinct depths - so the caller can fall back to a plain marker rather
    than showing a profile with a single mark on it.
    """
    series = profile_series(entries)
    points = [pair for _, group in series for pair in group]
    if len(points) < 2:
        return None
    depths = [d for d, _ in points]
    if len(set(depths)) < 2:
        return None

    values = [e["value"] for _, e in points]
    v_max = max(values)
    # The value axis always starts at zero: a profile whose axis floated with
    # the data would make a uniformly clean hole look contaminated.
    v_span = v_max or 1.0
    d_min, d_max = min(depths), max(depths)
    # Depth likewise anchors at the surface unless the samples start deep,
    # so shallow contamination reads as shallow.
    d_top = 0.0 if d_min < (d_max or 1.0) * 0.5 else d_min
    d_span = (d_max - d_top) or 1.0

    analyte_band = ANALYTE_HEIGHT if analyte else 0.0
    label_band = LABEL_HEIGHT if label else 0.0
    top = analyte_band + label_band
    height = top + PROFILE_PLOT_HEIGHT + PROFILE_AXIS
    plot_top = top + PAD_Y
    plot_bottom = top + PROFILE_PLOT_HEIGHT - PAD_Y
    # Wide enough for a two-digit depth caption to sit clear of the
    # stems rather than on top of the deepest ones.
    plot_left = PAD_X + 6.5
    plot_right = PROFILE_WIDTH - PAD_X

    def y_for(depth):
        return plot_top + (depth - d_top) / d_span * (plot_bottom - plot_top)

    def x_for(value):
        return plot_left + (value / v_span) * (plot_right - plot_left)

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%g" height="%g" '
        'viewBox="0 0 %g %g">' % (PROFILE_WIDTH, height, PROFILE_WIDTH, height),
        '<rect x="0.5" y="0.5" width="%g" height="%g" rx="%g" fill="%s" '
        'stroke="%s" stroke-width="1" opacity="0.92"/>'
        % (PROFILE_WIDTH - 1, height - 1, CORNER_RADIUS, BACKGROUND, BORDER),
    ]

    if analyte:
        radius = BAND_RADIUS
        parts.append(
            '<path d="M1,%g V%g A%g,%g 0 0 1 %g,1 H%g A%g,%g 0 0 1 %g,%g V%g Z" '
            'fill="%s"/>'
            % (1 + analyte_band, 1 + radius, radius, radius, 1 + radius,
               PROFILE_WIDTH - 1 - radius, radius, radius, PROFILE_WIDTH - 1,
               1 + radius, 1 + analyte_band, ANALYTE_BAND))
        parts.append(
            '<text x="%g" y="%g" font-family="sans-serif" font-size="%g" '
            'font-weight="700" fill="%s" text-anchor="middle">%s</text>'
            % (PROFILE_WIDTH / 2.0, analyte_band - 2.2, ANALYTE_FONT_SIZE,
               ANALYTE_COLOUR, _escape(_shorten_head(analyte))))

    if label:
        parts.append(
            '<text x="%g" y="%g" font-family="sans-serif" font-size="%g" '
            'font-weight="600" fill="%s" text-anchor="middle">%s</text>'
            % (PROFILE_WIDTH / 2.0, analyte_band + LABEL_HEIGHT - 2.4,
               FONT_SIZE, LABEL_COLOUR, _escape(_shorten(label))))
        parts.append(
            '<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="%s" '
            'stroke-width="0.6" opacity="0.55"/>'
            % (PAD_X, analyte_band + LABEL_HEIGHT - 0.5, PROFILE_WIDTH - PAD_X,
               analyte_band + LABEL_HEIGHT - 0.5, BORDER))

    # The depth axis: a vertical rule at zero concentration, which is what
    # every stem is measured from.
    parts.append(
        '<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="%s" '
        'stroke-width="0.8"/>'
        % (plot_left, plot_top, plot_left, plot_bottom, PROFILE_GRID))

    # Depth captions at the top and bottom of the axis, so it has a scale
    # without crowding the plot with ticks. The deepest number is the BASE of
    # the deepest interval, not its midpoint: "how deep does this hole go" is
    # the question a reader asks, and answering it with a midpoint understates
    # the hole by half a sample.
    deepest = max((e.get("end_depth") if e.get("end_depth") is not None else d)
                  for d, e in points)
    for depth, shown, baseline in ((d_top, d_top, "hanging"),
                                   (d_max, deepest, "auto")):
        parts.append(
            '<text x="1.5" y="%.2f" font-family="sans-serif" font-size="%g" '
            'fill="%s" dominant-baseline="%s">%s</text>'
            % (y_for(depth) + (0.4 if baseline == "hanging" else -1.4),
               PROFILE_FONT_SIZE, PROFILE_DEPTH_COLOUR, baseline,
               _escape(_trim_number(shown))))

    multi = len(series) > 1
    for order, (method, group) in enumerate(series):
        colour = (PROFILE_SERIES[order % len(PROFILE_SERIES)] if multi
                  else FILL_COLOUR)

        # One line DOWN the hole, joining this event's readings in depth
        # order. Each point used to carry its own horizontal stem back to
        # the zero axis, which drew a sideways bar chart: it said "here is a
        # value at this depth" one point at a time and never showed the
        # shape of the profile, which is the whole reason to plot against
        # depth. Joining them is also what makes several events comparable -
        # two lines down one hole read as two profiles, where two sets of
        # interleaved bars read as noise.
        if len(group) > 1:
            trace = " ".join("%.2f,%.2f" % (x_for(e["value"]), y_for(d))
                             for d, e in group)
            parts.append(
                '<polyline points="%s" fill="none" stroke="%s" '
                'stroke-width="1.0" stroke-linejoin="round" '
                'stroke-linecap="round" opacity="%.2f"/>'
                % (trace, colour, 0.9 if multi else 1.0))

        for depth, entry in group:
            y = y_for(depth)
            x = x_for(entry["value"])
            detected = not entry.get("at_limit") and str(
                entry.get("detect", "")).upper() in ("Y", "YES", "TRUE", "1")

            # The sampled interval as a short vertical bar, so a 5 m core
            # does not masquerade as a reading at a single depth.
            top_d = entry.get("start_depth")
            base_d = entry.get("end_depth")
            if top_d is not None and base_d is not None and base_d > top_d:
                parts.append(
                    '<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" '
                    'stroke="%s" stroke-width="0.7" opacity="0.5"/>'
                    % (x, y_for(top_d), x, y_for(base_d), colour))

            if detected:
                parts.append('<circle cx="%.2f" cy="%.2f" r="1.8" fill="%s"/>'
                             % (x, y, colour))
            else:
                # Hollow, matching the map's own non-detect convention.
                parts.append(
                    '<rect x="%.2f" y="%.2f" width="3.0" height="3.0" '
                    'fill="none" stroke="%s" stroke-width="0.9"/>'
                    % (x - 1.5, y - 1.5, colour))

    if multi:
        # Name the series: two lines with no key is worse than one line,
        # because the reader cannot tell which event or analysis said what.
        # Labels are trimmed against each other first - several events in
        # one year share "2020-", and spending the legend's ten characters
        # on the part every line has in common would leave nothing to tell
        # them apart.
        labels = _distinguish_labels([m for m, _ in series])
        swatch_y = top + 2.0
        for order, (method, _) in enumerate(series):
            if not method:
                continue
            method = labels[order]
            colour = PROFILE_SERIES[order % len(PROFILE_SERIES)]
            parts.append(
                '<rect x="%.2f" y="%.2f" width="2.6" height="2.6" fill="%s"/>'
                % (plot_right - 2.6, swatch_y, colour))
            parts.append(
                '<text x="%.2f" y="%.2f" font-family="sans-serif" '
                'font-size="%g" fill="%s" text-anchor="end">%s</text>'
                % (plot_right - 3.6, swatch_y + 2.4, PROFILE_FONT_SIZE - 0.5,
                   PROFILE_DEPTH_COLOUR, _escape(_shorten_head(method, 10))))
            swatch_y += 4.4

    # What the horizontal axis measures, which is otherwise unguessable.
    caption = _trim_number(v_max)
    if unit:
        caption += " " + unit
    parts.append(
        '<text x="%g" y="%g" font-family="sans-serif" font-size="%g" '
        'fill="%s" text-anchor="end">%s</text>'
        % (plot_right, height - 2.4, PROFILE_FONT_SIZE, LABEL_COLOUR,
           _escape(caption)))
    parts.append(
        '<text x="%g" y="%g" font-family="sans-serif" font-size="%g" '
        'fill="%s">0</text>'
        % (plot_left, height - 2.4, PROFILE_FONT_SIZE, PROFILE_DEPTH_COLOUR))

    parts.append("</svg>")
    return "".join(parts)


def _trim_number(value):
    """A short human rendering of a number for an axis caption."""
    if value is None:
        return ""
    if value == int(value):
        return str(int(value))
    if abs(value) >= 100:
        return "%.0f" % value
    if abs(value) >= 1:
        return "%.1f" % value
    return ("%.3g" % value)


def embed_depth_profiles(entries_by_location, label_locations=True,
                         analyte=None, unit=None):
    """Render one depth profile per location, as embedded data URIs.

    Mirrors embed_sparklines: only locations with something worth drawing get
    an entry, so a location sampled at one depth simply keeps its plain
    marker rather than being given a profile that shows nothing.
    """
    embedded = {}
    for location, entries in (entries_by_location or {}).items():
        svg = render_profile_svg(
            entries, label=location if label_locations else None,
            analyte=analyte, unit=unit)
        uri = svg_datauri(svg)
        if uri:
            embedded[location] = uri
    return embedded
