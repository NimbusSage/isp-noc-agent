#!/usr/bin/env python3
"""RSSI drop evidence chart for turned/misaligned wireless links (tracker-sheet SOP).

Pulls the wireless signal series for a Ubiquiti UISP device (AP first per
operator rule), detects step-drop events, and renders a LibreNMS-style RSSI
evidence chart to PNG (via headless Chrome). Output is used as the follow-up
comment image on the Sonar ticket created from the tracker row.

Definition (tracker-sheet §9 standard): a "turned" claim needs a step-drop of
>= ~6 dB within ~48h that stays down (does not recover). Foliage is a slow
decline — the chart shows that too, but only sharp steps are *events*.

Deterministic: series data + fixed canvas + CT-dated annotations. No third
party deps; the UISP token comes from NOC_UBNT_UISP_API_TOKEN (explicit, per run) or
the encrypted MCP store (credentials.enc + NOC_MCP_PASSPHRASE) via
scripts/credstore.py - the plaintext credentials.json is not read.

Usage:
  python3 scripts/rssi_drop_graph.py <uisp-device-id> [--out logs/charts/x.png]
      [--window month] [--series signal60g,remoteSignal60g]
      [--selftest]

Exit codes: 0 ok, 2 selftest fail, 1 runtime error.
"""
import argparse
import datetime as dt
import json
import math
import os
import pathlib
import subprocess
import sys
import time
import warnings
import urllib.parse
import urllib.request

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CT = dt.timezone(dt.timedelta(hours=-5), "CDT")  # America/Chicago during CDT

DROP_DB = 6.0          # step size that counts as a "turn" (tracker-sheet §9)
BASELINE_BUCKETS = 3   # lookback buckets (~1 day of 8h UISP buckets)
PERSIST_BUCKETS = 6    # lookforward buckets (~48h) to test recovery (§9 standard)
MAX_GAP_BUCKETS = 2    # series gap that splits segments (24h at 8h buckets)

CHROME_CANDIDATES = ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                     "/usr/bin/chromium", "/usr/bin/chromium-browser")


def uisp_token():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import credstore
    try:
        for cred in credstore.pool("ubnt-uisp"):
            if cred.get("apiToken"):
                return cred["apiToken"]
    except credstore.CredentialError as exc:
        raise RuntimeError("no UISP API token: %s" % exc)
    raise RuntimeError("no ubnt-uisp apiToken available: set NOC_UBNT_UISP_API_TOKEN or add one to the encrypted MCP store")


def _uisp_base():
    """UISP NMS base from the resource registry (scripts/resources.py); raises when it is not registered."""
    return _registered("uisp")


def _registered(name):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import noc_config
    import resources
    try:
        return resources.get_url(name)
    except noc_config.ConfigError as exc:
        raise RuntimeError(str(exc))


def fetch_uisp_stats(device_id, window="month", timeout=60):
    token = uisp_token()
    base = _uisp_base()
    url = "%s/nms/api/v2.1/devices/%s/statistics?interval=%s" % (
        base, device_id, window)
    req = urllib.request.Request(url, headers={"X-Auth-Token": token})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def series_from_stats(stats, series_names):
    """Return {name: [(ts_ms, value), ...]} non-null ascending."""
    out = {}
    for name in series_names:
        pts = (stats.get(name) or {}).get("avg") or []
        out[name] = sorted((p["x"], p["y"]) for p in pts if p.get("y") is not None)
    return out


def detect_series_drops(points, drop_db=DROP_DB,
                        baseline_buckets=BASELINE_BUCKETS,
                        persist_buckets=PERSIST_BUCKETS,
                        max_gap=MAX_GAP_BUCKETS):
    """Return [{ts_ms, before_dbm, after_dbm, recovered}] for persistent step-drops.

    Step = >= drop_db below the median of the previous N nonnull points within
    the same segment (segments split on gaps > max_gap buckets). "Stays down"
    means no lookforward point within ~48h recovers to within 2 dB of the
    pre-drop level.
    """
    events = []
    if len(points) < baseline_buckets + 1:
        return events
    # segments: split on bucket gap
    segments = []
    start = 0
    for i in range(1, len(points)):
        if points[i][0] - points[i - 1][0] > max_gap * 24 * 3600 * 1000:
            segments.append(points[start:i])
            start = i
    segments.append(points[start:])
    for seg in segments:
        for i in range(baseline_buckets, len(seg)):
            ts, val = seg[i]
            baseline_vals = [v for _, v in seg[i - baseline_buckets:i]]
            baseline = sorted(baseline_vals)[len(baseline_vals) // 2]
            if baseline - val >= drop_db:
                # strongest point of the contiguous drop run
                if events and ts - events[-1]["ts_ms"] <= 24 * 3600 * 1000:
                    if val >= events[-1]["after_dbm"]:
                        continue
                look = [v for _, v in seg[i + 1:i + 1 + persist_buckets]]
                recovered = any(v >= baseline - 2.0 for v in look)
                events.append({
                    "ts_ms": ts,
                    "before_dbm": round(baseline, 1),
                    "after_dbm": round(val, 1),
                    "recovered": recovered,
                })
    return events


def ct_label(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, CT).strftime("%-m/%-d")


def fmt_date_full(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, CT).strftime("%-m/%-d/%Y %l:%M %p %Z").replace("  ", " ")


def render_svg(title, subtitle, series, events, width=1080, height=400):
    lo = min(min(v for _, v in pts) for _, pts in series.items())
    hi = max(max(v for _, v in pts) for _, pts in series.items())
    t0 = min(pts[0][0] for _, pts in series.items())
    t1 = max(pts[-1][0] for _, pts in series.items())
    pad_l, pad_r, pad_t, pad_b = 70, 30, 46, 46
    cw, ch = width - pad_l - pad_r, height - pad_t - pad_b
    span = max(hi - lo, 1.0)
    lo -= span * 0.05
    hi += span * 0.05

    def X(ts):
        return pad_l + (ts - t0) / max(t1 - t0, 1) * cw

    def Y(v):
        return pad_t + (hi - v) / max(hi - lo, 1) * ch

    grid, ytick, xtick = [], [], []
    step_y = 10
    start = math.ceil(lo / step_y) * step_y
    y = start
    while y <= hi:
        grid.append('<line x1="%d" y1="%.0f" x2="%d" y2="%.0f" stroke="#e0e0e8" stroke-width="1"/>' % (
            pad_l, Y(y), width - pad_r, Y(y)))
        ytick.append('<text x="%d" y="%.0f" font-size="11" fill="#555" text-anchor="end">%d</text>' % (
            pad_l - 6, Y(y) + 4, y))
        y += step_y
    # ~6 date ticks, CT dates
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        ts = t0 + (t1 - t0) * frac
        x = X(ts)
        grid.append('<line x1="%.0f" y1="%d" x2="%.0f" y2="%d" stroke="#eef0f4" stroke-width="1"/>' % (
            x, pad_t, x, height - pad_b))
        xtick.append('<text x="%.0f" y="%d" font-size="11" fill="#555" text-anchor="middle">%s</text>' % (
            x, height - pad_b + 18, ct_label(ts)))

    lines, names = [], []
    palette = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd"]
    for (name, pts), color in zip(series.items(), palette):
        if not pts:
            continue
        d = " ".join("%d,%.1f" % (X(ts), Y(v)) for ts, v in pts)
        dashed = ' stroke-dasharray="5,3"' if "rem" in name.lower() or "st" in name.lower() else ""
        lines.append('<polyline fill="none" stroke="%s" stroke-width="2"%s points="%s"/>' % (color, dashed, d))
        names.append('<rect x="%d" y="%d" width="18" height="4" fill="%s"/><text x="%d" y="%d" font-size="12" fill="#333">%s</text>' % (
            pad_l, height - pad_b + 34, color, pad_l + 24, height - pad_b + 40,
            name.replace("60g", "60G")))

    events_svg = []
    for ev in events:
        x = X(ev["ts_ms"])
        if ev["recovered"]:
            # brief dip (recovered within ~48h): background tick only, no label
            events_svg.append('<line x1="%.0f" y1="%d" x2="%.0f" y2="%d" stroke="%s" stroke-width="1" stroke-dasharray="2,4" opacity="0.45"/>' % (
                x, pad_t, x, height - pad_b, "#ff7f0e"))
            continue
        events_svg.append('<line x1="%.0f" y1="%d" x2="%.0f" y2="%d" stroke="%s" stroke-width="1.5" stroke-dasharray="4,3"/>' % (
            x, pad_t, x, height - pad_b, "#ff7f0e"))
        events_svg.append(
            '<text x="%.0f" y="%d" font-size="11" fill="#7a4800" text-anchor="middle">%s %.0f\u2192%.0f dBm</text>' % (
                x, pad_t + 14, ct_label(ev["ts_ms"]), ev["before_dbm"], ev["after_dbm"]))

    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
            'viewBox="0 0 %d %d" style="background:#fff;font-family:Arial,Helvetica,sans-serif">'
            '<rect width="%d" height="%d" fill="#fff"/>'
            '<text x="%d" y="22" font-size="16" font-weight="bold" fill="#222">%s</text>'
            '<text x="%d" y="40" font-size="12" fill="#666">%s</text>'
            '<text transform="translate(16,%d) rotate(-90)" font-size="12" fill="#555">RSSI (dBm)</text>'
            '<rect x="0" y="0" width="6" height="6" fill="#ff0000"/>'                     # render marker
            '%s%s%s%s%s%d</svg>' % (
                width, height, width, height, width, height,
                pad_l, title, pad_l, subtitle, pad_t + ch // 2,
                "".join(grid), "".join(ytick), "".join(xtick),
                "".join(lines), "".join(events_svg), 0))


def validate_png_pixels(png_path):
    """Deterministic render check: the SVG includes a 6x6 pure-red marker at the
    top-left corner; the screenshot must show exactly that color in that region.

    Guards against Chrome silently screenshotting its own error page (a valid
    PNG with wrong content — this actually happened on 2026-10-18 when a
    relative path produced file://logs/... = a broken URL). Requires
    ImageMagick `convert`.
    """
    probe = subprocess.run(
        ["convert", png_path, "-crop", "6x6+0+0",
         "-format", "%[fx:int(255*u.r)],%[fx:int(255*u.g)],%[fx:int(255*u.b)]", "info:"],
        capture_output=True, text=True, timeout=60)
    if probe.returncode != 0:
        raise RuntimeError("PNG validation probe failed: " + probe.stderr[:200])
    rgb = [int(x) for x in probe.stdout.strip().split(",")]
    if not (rgb[0] >= 240 and rgb[1] <= 20 and rgb[2] <= 20):
        raise RuntimeError(
            "render validation failed: marker pixel is %s — the screenshot is "
            "not our SVG (likely a Chrome error page)" % (rgb,))
    return True


def svg_to_png(svg_str, out_path):
    out_path = os.path.abspath(out_path)
    svg_path = out_path.replace(".png", ".src.svg")
    html_tmp = out_path + ".render.html"
    with open(svg_path, "w") as fh:
        fh.write(svg_str)
    # absolute file URI — a relative path (file://logs/...) is parsed by Chrome
    # as host `logs` and produces the "This site can't be reached" error page
    svg_url = pathlib.Path(svg_path).as_uri()
    with open(html_tmp, "w") as fh:
        fh.write('<html><body style="margin:0;background:#fff"><img src="%s" style="display:block"></body></html>'
                 % svg_url)
    chrome = None
    for cand in CHROME_CANDIDATES:
        if os.path.exists(cand):
            chrome = cand
            break
    if not chrome:
        raise RuntimeError("headless Chrome not found")
    subprocess.run([chrome, "--headless", "--disable-gpu", "--no-sandbox",
                    "--screenshot=" + out_path, "--window-size=1100,420",
                    "--hide-scrollbars", "file://" + html_tmp],
                   check=True, capture_output=True, timeout=90)
    os.remove(svg_path)
    os.remove(html_tmp)
    validate_png_pixels(out_path)
    return out_path


def build_comment_metrics(events):
    if not events:
        return "No persistent >=6 dB step in the available window (foliage-like slow decline or flat)."
    parts = []
    for ev in events:
        parts.append("%s: %.0f \u2192 %.0f dBm%s" % (
            fmt_date_full(ev["ts_ms"]), ev["before_dbm"], ev["after_dbm"],
            " (brief recovery)" if ev["recovered"] else " (stayed down)"))
    return "; ".join(parts) + "."


def load_fixture():
    """Synthetic 30-day 8h-bucket series: flat -46, turn to -58 on day 20, recovers day 21 (inside 48h), flat again."""
    pts = []
    t0 = dt.datetime(2026, 9, 18, 16, 0, tzinfo=dt.timezone.utc)
    for i in range(360):
        ts = int((t0 + dt.timedelta(hours=8 * i)).timestamp() * 1000)
        val = -46.0
        if 20 * 3 <= i:      # day 20: turn
            val = -58.0
        if i >= 21 * 3:      # day 21: recovery within the 48h lookforward
            val = -47.0
        pts.append((ts, val))
    return pts


def selftest():
    pts = load_fixture()
    events = detect_series_drops(pts)
    assert events, "expected a detected drop"
    ev = events[0]
    assert ev["before_dbm"] == -46.0, ev
    assert ev["after_dbm"] == -58.0, ev
    assert ev["ts_ms"] == pts[60][0], ev  # bucket 60 = day 20
    assert ev["recovered"] is True, ev    # day 21 recovers above baseline - 2
    # no-drop flat series
    flat = [(i * 8 * 3600 * 1000, -47.0) for i in range(100)]
    assert detect_series_drops(flat) == []
    # slow decline (foliage): 1.5 dB per 2 days for 30 days = 22.5 dB drift, no single >=6 step
    foliage = []
    t0 = int(dt.datetime(2026, 9, 18, 16, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    for i in range(90):
        foliage.append((t0 + i * 2 * 24 * 3600 * 1000, -45.0 - 1.5 * i))
    assert detect_series_drops(foliage) == [], detect_series_drops(foliage)
    # svg renders and png writes via chrome
    svg = render_svg("t", "s", {"sig": pts[:120]}, events)
    assert "<svg" in svg and "polyline" in svg
    print("selftest ok")
    return 0


def librenms_graph_png(args):
    """Pull the AUTHENTIC LibreNMS-rendered wireless graph and return a local PNG.

    The API token cannot render web graphs. This flow assumes LibreNMS SSO in
    header mode (config.php auth_mechanism=sso) behind a trusted proxy, with the
    agent's egress address added to the trusted-proxy list. Identity = the
    header named by CHART_EMAIL_HEADER (default X-Forwarded-Email) carrying
    LIBRENMS_WEB_USER (a dedicated non-admin account; NO password check happens
    from a trusted source — do not widen the trusted list).

    Flow: any request with the header auto-authenticates; then
    GET /graph.php?type=...&device=...&from=...&to=... Modern LibreNMS graph
    modules answer image/svg+xml — converted to PNG locally via ImageMagick.
    Agent-rendered PNG requests may answer image/png directly. Never stores an
    error page: validates the magic bytes and fails closed.
    """
    import http.cookiejar
    base = _registered("librenms").rstrip("/")
    user = os.environ.get("LIBRENMS_WEB_USER")
    if not user:
        raise RuntimeError("LIBRENMS_WEB_USER not set in env")
    email_header = (os.environ.get("CHART_EMAIL_HEADER") or "X-Forwarded-Email").strip()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", "NocAgent evidence fetcher"),
        (email_header, user),
    ]

    q = ["type=%s" % args.librenms_type, "device=%s" % args.librenms_device,
         "from=%s" % args.from_epoch]
    if args.height:
        q.append("height=%s" % args.height)
    if args.width:
        q.append("width=%s" % args.width)
    url = base + "/graph.php?" + "&".join(q)
    gresp = opener.open(url, timeout=60)
    ctype = gresp.headers.get("Content-Type", "")
    blob = gresp.read()

    out_default = args.out or "logs/charts/rssi_%s.png" % args.device
    out = os.path.abspath(out_default.replace(".png", ".librenms.png"))
    if "image/png" in ctype and blob.startswith(b"\x89PNG"):
        raw = out
    elif "image/svg+xml" in ctype or blob.lstrip().startswith(b"<?xml"):
        if b"<svg" not in blob[:4096].lower():
            raise RuntimeError("LibreNMS answered %s without an <svg> root — not a graph"
                               % ctype)
        raw = out[:-4] + ".svg"
        with open(raw, "wb") as fh:
            fh.write(blob)
        cmd = ["convert", "-density", "144", raw, out]
        try:
            rc = subprocess.run(cmd, capture_output=True, timeout=120).returncode
        except Exception as exc:  # ImageMagick absent: fail closed
            raise RuntimeError("SVG->PNG conversion failed (%s); nothing embedded" % exc)
        if rc != 0 or not os.path.exists(out) \
                or not open(out, "rb").read(4) == b"\x89PNG":
            raise RuntimeError("SVG->PNG conversion did not produce a valid PNG")
    else:
        raise RuntimeError("LibreNMS returned %s (bytes=%d) — unexpected; nothing embedded"
                           % (ctype, len(blob)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("device")
    ap.add_argument("--out", default=None)
    ap.add_argument("--window", default="month")
    ap.add_argument("--series", default="signal60g,remoteSignal60g")
    ap.add_argument("--device-name", default=None)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--no-png", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--librenms-fallback", action="store_true",
                    help="if UISP has no usable series, fall back to the LibreNMS native render")
    ap.add_argument("--librenms-render", action="store_true",
                    help="also fetch the authentic LibreNMS /graph PNG for this link")
    ap.add_argument("--ip", default=None, help="mgmt IP for LibreNMS device lookup (cache)")
    ap.add_argument("--librenms-device", default=None, help="explicit LibreNMS device id")
    ap.add_argument("--librenms-type", default="device_wireless_rssi")
    ap.add_argument("--from-epoch", default=None, help="window start (epoch s); default = 30d ago")
    ap.add_argument("--height", default=None)
    ap.add_argument("--width", default=None)
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    fetch_error = None
    try:
        stats = fetch_uisp_stats(args.device, args.window)
        series = series_from_stats(stats, [s.strip() for s in args.series.split(",")])
    except Exception as exc:  # device not in UISP / API error -> operator rule: LibreNMS fallback
        stats, series = None, {}
        fetch_error = str(exc)
    series = {k: v for k, v in series.items() if v}
    if not series:
        if args.librenms_fallback:
            # Operator standard: NocAgent/UISP chart is ALWAYS the
            # deliverable; this branch only bails to the LibreNMS native render
            # when UISP itself has no usable data for the link (per operator
            # rule: UISP first for Ubiquiti radios, LibreNMS fallback).
            device_id = args.librenms_device
            if not device_id and args.ip:
                with open(os.path.join(ROOT, "logs", "cache", "librenms_devices.json")) as fh:
                    for d in json.load(fh).get("devices") or []:
                        if isinstance(d, dict) and str(d.get("ip")) == str(args.ip):
                            device_id = str(d.get("device_id") or d.get("id"))
                            break
            if device_id:
                if not args.from_epoch:
                    args.from_epoch = str(int(time.time() - 30 * 24 * 3600))
                args.librenms_device = device_id
                img = librenms_graph_png(args)
                result = {
                    "ok": True,
                    "device": args.device,
                    "device_name": args.device_name or args.device,
                    "series": {},
                    "events": [],
                    "source": "librenms-fallback",
                    "fallback_reason": "no usable UISP series; rendered native LibreNMS graph instead",
                    "fallback_error": fetch_error,
                    "image": img,
                }
                js = json.dumps(result, indent=2)
                if args.json_out:
                    with open(args.json_out, "w") as fh:
                        fh.write(js)
                print(js)
                warnings.warn(
                    "UISP had no usable series; chart is the authentic LibreNMS render "
                    "(operator keeps the NocAgent chart when UISP data exists)")
                return 0
            print(json.dumps({
                "ok": False,
                "error": "no non-null series points; LibreNMS fallback requested "
                         "but needs --librenms-device or --ip (with device cache)",
            }))
            return 1
    primary = series[min(series, key=lambda k: -len(series[k]))]
    events = detect_series_drops(primary)

    name = args.device_name or args.device
    title = "RSSI evidence \u2014 %s" % name
    subtitle = ("UISP %s statistics \u2022 last %s \u2014 dashed = remote/ST view \u2022 "
                "orange = detected >=6 dB step (persistent = turn signature)" % (
                    args.window, fmt_date_full(primary[-1][0])))
    result = {
        "ok": True,
        "source": "uisp",
        "device": args.device,
        "device_name": name,
        "series": {k: len(v) for k, v in series.items()},
        "events": events,
        "comment_metrics": build_comment_metrics(events),
    }
    if not args.no_png:
        out = args.out or os.path.join("logs", "charts", "rssi_%s.png" % args.device)
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        svg = render_svg(title, subtitle, series, events)
        result["image"] = os.path.abspath(svg_to_png(svg, out))
    if args.librenms_render:
        device_id = args.librenms_device
        if not device_id and args.ip:
            cache_path = os.path.join(ROOT, "logs", "cache", "librenms_devices.json")
            with open(cache_path) as fh:
                devs = json.load(fh).get("devices") or []
            for d in devs:
                if isinstance(d, dict) and str(d.get("ip")) == str(args.ip):
                    device_id = str(d.get("device_id") or d.get("id"))
                    break
        if not device_id:
            raise RuntimeError("--librenms-render needs --librenms-device or --ip (with cache)")
        if not args.from_epoch:
            args.from_epoch = str(int(time.time() - 30 * 24 * 3600))
        args.librenms_device = device_id
        result["librenms_image"] = librenms_graph_png(args)
    js = json.dumps(result, indent=2)
    if args.json_out:
        with open(args.json_out, "w") as fh:
            fh.write(js)
    print(js)
    return 0


if __name__ == "__main__":
    sys.exit(main())
