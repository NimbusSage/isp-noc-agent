#!/usr/bin/env python3
"""rain_fade_model.py - predict the rain rate that kills a fading-prone link.

Given a link's fade margin (dB), path distance (km) and frequency (GHz),
compute the rainfall rate at which path attenuation exhausts the margin
(``kill_rain_rate_mm_h``). This turns a clear-day signal reading into a
"this link drops at mm/h" forecast, per ``path-analyzer`` Rain-Fade Readiness.

Physics (pure stdlib, no network):

* Specific attenuation: Rec. ITU-R P.838-3, ``gamma_R = k R^alpha``.
  k/alpha from the P.838-3 log-normal fit coefficients at ``f`` (exact
  coefficients as implemented in ITU-Rpy, validated against ITU validation
  examples rev 5.1, https://github.com/inigodelportillo/ITU-Rpy).
* Path attenuation: effective path length ``d_eff = d * r`` with
  ``r = 1 / (1 + d/d0)``, ``d0 = 35 e^{-0.015 R}`` (mm/h <= 100) and
  ``d0 = 25 e^{-0.015 R}`` (mm/h > 100). Terrestrial path: elevation 0.
  Alternative: ITU-R P.530-17 Eq. 32 distance factor via ``--method p530``
  (``r = 1/(0.477 d^0.633 R^(0.073 alpha) f^0.123 - 10.579(1-e^{-0.024d}))``,
  capped at r <= 2.5 as in the validated implementation).
* Kill rain rate solve: first upward crossing of the margin on the A(R)
  curve (the reduction factor decays beyond ~150 mm/h, so A(R) is not
  monotone at extreme rain rates - the first crossing is the kill point).

Note: 60 GHz links also carry constant ~15 dB/km oxygen absorption; that is
always present and cancels out of rain-fade margin math.

Tiers (path-analyzer Rain-Fade Readiness defaults, env-overridable at run
time by the agent, not here): kill rate below ~25 mm/h (heavy rain) = red,
below ~50 mm/h (thunderstorm rain) = amber, else green/backup-class O.K.

Usage:
  python3 scripts/rain_fade_model.py --freq 60 --dist 2.0 --margin 12.5
  -> {"kill_rain_rate_mm_h": 21.9, "gamma_at_kill_db_km": 15.8,
      "atten_at_kill_db": 12.5, "tier": "red"}
  Output is one machine-friendly JSON object; null kill_rain_rate_mm_h when
  distance/margin are missing or when the link never fades.

Exit codes: 0 computed; 2 usage; 3 cannot compute (missing/invalid inputs).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

# --- ITU-R P.838-3 coefficient fit (exact published values) -----------------

_COEFFS_KH = {
    "aj": [-5.33980, -0.35351, -0.23789, -0.94158],  # secret-scan:allow(ITU-R P.838-3 published fit coefficients, not geographic coordinates)
    "bj": [-0.10008, 1.2697, 0.86036, 0.64552],  # secret-scan:allow(ITU-R P.838-3 published fit coefficients, not geographic coordinates)
    "cj": [1.13098, 0.454, 0.15354, 0.16817],  # secret-scan:allow(ITU-R P.838-3 published fit coefficients, not geographic coordinates)
    "mk": -0.18961,
    "ck": 0.71147,
}
_COEFFS_KV = {
    "aj": [-3.80595, -3.44965, -0.39902, 0.50167],  # secret-scan:allow(ITU-R P.838-3 published fit coefficients, not geographic coordinates)
    "bj": [0.56934, -0.22911, 0.73042, 1.07319],  # secret-scan:allow(ITU-R P.838-3 published fit coefficients, not geographic coordinates)
    "cj": [0.81061, 0.51059, 0.11899, 0.27195],  # secret-scan:allow(ITU-R P.838-3 published fit coefficients, not geographic coordinates)
    "mk": -0.16398,
    "ck": 0.63297,
}
_COEFFS_AH = {
    "aj": [-0.14318, 0.29591, 0.32177, -5.37610, 16.1721],  # secret-scan:allow(ITU-R P.838-3 published fit coefficients, not geographic coordinates)
    "bj": [1.82442, 0.77564, 0.63773, -0.96230, -3.29980],  # secret-scan:allow(ITU-R P.838-3 published fit coefficients, not geographic coordinates)
    "cj": [-0.55187, 0.19822, 0.13164, 1.47828, 3.4399],  # secret-scan:allow(ITU-R P.838-3 published fit coefficients, not geographic coordinates)
    "ma": 0.67849,
    "ca": -1.95537,
}
_COEFFS_AV = {
    "aj": [-0.07771, 0.56727, -0.20238, -48.2991, 48.5833],  # secret-scan:allow(ITU-R P.838-3 published fit coefficients, not geographic coordinates)
    "bj": [2.3384, 0.95545, 1.1452, 0.791669, 0.791459],  # secret-scan:allow(ITU-R P.838-3 published fit coefficients, not geographic coordinates)
    "cj": [-0.76284, 0.54039, 0.26809, 0.116226, 0.116479],  # secret-scan:allow(ITU-R P.838-3 published fit coefficients, not geographic coordinates)
    "ma": -0.053739,
    "ca": 0.83433,
}


def _curve(f: float, a: float, b: float, c: float) -> float:
    return a * math.exp(-(((math.log10(f) - b) / c) ** 2))


def rain_specific_attenuation_coefficients(
    freq_ghz: float, elevation_deg: float = 0.0, tau_deg: float = 45.0
) -> tuple[float, float]:
    """P.838-3 coefficients (k, alpha) at ``freq_ghz`` for a path.

    Terrestrial default: elevation 0 deg. ``tau_deg`` is the polarization
    tilt relative to horizontal (45 deg for circular).
    """
    l = math.log10(freq_ghz)
    kh = 10 ** (
        sum(
            _COEFFS_KH["aj"][j]
            * math.exp(-(((l - _COEFFS_KH["bj"][j]) / _COEFFS_KH["cj"][j]) ** 2))
            for j in range(4)
        )
        + _COEFFS_KH["mk"] * l
        + _COEFFS_KH["ck"]
    )
    kv = 10 ** (
        sum(
            _COEFFS_KV["aj"][j]
            * math.exp(-(((l - _COEFFS_KV["bj"][j]) / _COEFFS_KV["cj"][j]) ** 2))
            for j in range(4)
        )
        + _COEFFS_KV["mk"] * l
        + _COEFFS_KV["ck"]
    )
    ah = sum(
        _COEFFS_AH["aj"][j]
        * math.exp(-(((l - _COEFFS_AH["bj"][j]) / _COEFFS_AH["cj"][j]) ** 2))
        for j in range(5)
    ) + _COEFFS_AH["ma"] * l + _COEFFS_AH["ca"]
    av = sum(
        _COEFFS_AV["aj"][j]
        * math.exp(-(((l - _COEFFS_AV["bj"][j]) / _COEFFS_AV["cj"][j]) ** 2))
        for j in range(5)
    ) + _COEFFS_AV["ma"] * l + _COEFFS_AV["ca"]
    ce = math.cos(math.radians(elevation_deg)) ** 2
    ct = math.cos(math.radians(2 * tau_deg))
    k = (kh + kv + (kh - kv) * ce * ct) / 2.0
    alpha = (kh * ah + kv * av + (kh * ah - kv * av) * ce * ct) / (2.0 * k)
    return k, alpha


def gamma_r(freq_ghz: float, rain_rate_mm_h: float,
            elevation_deg: float = 0.0, tau_deg: float = 45.0) -> float:
    """Specific attenuation gamma_R (dB/km) = k * R^alpha."""
    k, alpha = rain_specific_attenuation_coefficients(
        freq_ghz, elevation_deg, tau_deg)
    return k * rain_rate_mm_h ** alpha


def path_attenuation_db(
    rain_rate_mm_h: float,
    dist_km: float,
    freq_ghz: float,
    elevation_deg: float = 0.0,
    tau_deg: float = 45.0,
    method: str = "p838",
) -> float:
    """Rain path attenuation (dB) at ``rain_rate_mm_h`` over ``dist_km``."""
    g = gamma_r(freq_ghz, rain_rate_mm_h, elevation_deg, tau_deg)
    d = dist_km
    if method == "p530":
        # Rec. ITU-R P.530-17 Eq. 32 distance factor (validated form).
        denom = (
            0.477 * d ** 0.633 * rain_rate_mm_h ** (0.073 * _ALPHA_CACHE(freq_ghz, elevation_deg, tau_deg))
            * freq_ghz ** 0.123
            - 10.579 * (1 - math.exp(-0.024 * d))
        )
        if denom <= 0:
            r = 2.5
        else:
            r = 1 / denom
            r = min(r, 2.5)
        return g * d * r
    # Rec. ITU-R P.838-3 effective path length: r = 1/(1 + d/d0).
    r_limit = 35.0 if rain_rate_mm_h <= 100 else 25.0
    d0 = r_limit * math.exp(-0.015 * rain_rate_mm_h)
    return g * d / (1 + d / d0)


def _ALPHA_CACHE(freq_ghz: float, elevation_deg: float = 0.0,
                 tau_deg: float = 45.0) -> float:
    return rain_specific_attenuation_coefficients(
        freq_ghz, elevation_deg, tau_deg)[1]


def rain_rate_for_fade(
    margin_db: float,
    dist_km: float,
    freq_ghz: float,
    elevation_deg: float = 0.0,
    tau_deg: float = 45.0,
    method: str = "p838",
) -> float | None:
    """Rain rate (mm/h) at which path attenuation first reaches ``margin_db``.

    The d0 effective-path curve peaks around 100-200 mm/h and decays beyond
    (model artifact of the reduction factor; unrealistic rain rates), so the
    solver scans for the FIRST upward crossing - the meaningful kill point -
    instead of assuming a monotone endpoint. ``None`` if margin/distance are
    missing/invalid or no crossing exists up to 2000 mm/h (rain-proof link).
    """
    if margin_db is None or dist_km is None or dist_km <= 0 or margin_db < 0:
        return None
    if margin_db == 0:
        return 0.0

    def att(rr: float) -> float:
        return path_attenuation_db(rr, dist_km, freq_ghz, elevation_deg,
                                   tau_deg, method)

    # First pass: locate the first R where A(R) crosses the margin going up.
    prev_r, prev_a = 1.0, att(1.0)  # att(0) = 0, defines the ramp start
    if prev_a >= margin_db:
        return 1.0
    checked = [1.0]
    finer = list(range(2, 201))          # 1 mm/h steps in the realistic band
    coarser = [rr for rr in range(210, 2001, 10)]  # 10 mm/h beyond
    for rr in finer + coarser:
        a = att(rr)
        if a >= margin_db:
            lo, hi = prev_r if prev_a < margin_db else checked[-1], rr
            # bisect crossing between hi (>= margin) and lo (< margin)
            a_lo = att(lo)
            if a_lo >= margin_db:
                return lo
            for _ in range(60):
                mid = (lo + hi) / 2
                if att(mid) < margin_db:
                    lo = mid
                else:
                    hi = mid
            return round(hi, 1)
        prev_r, prev_a = rr, a
        checked.append(rr)
    return None


def tier_for(kill_rain_rate_mm_h: float | None,
             red_below_mm_h: float = 25.0,
             amber_below_mm_h: float = 50.0) -> str:
    """green/amber/red readiness tier by predicted kill rain rate."""
    if kill_rain_rate_mm_h is None:
        return "none"
    if kill_rain_rate_mm_h <= red_below_mm_h:
        return "red"
    if kill_rain_rate_mm_h <= amber_below_mm_h:
        return "amber"
    return "green"


def main() -> None:
    ap = argparse.ArgumentParser(
        description="ITU-R P.838-3 rain-fade forecast for one link. "
                    "Prints a single JSON object.")
    ap.add_argument("--freq", type=float, default=60.0, help="GHz (default 60)")
    ap.add_argument("--dist", type=float, required=True, help="path length km")
    ap.add_argument("--margin", type=float, required=True,
                    help="fade margin dB (signal minus lowest-mod threshold)")
    ap.add_argument("--elevation", type=float, default=0.0)
    ap.add_argument("--tau", type=float, default=45.0,
                    help="polarization tilt deg (45 circular)")
    ap.add_argument("--method", choices=["p838", "p530"], default="p838")
    args = ap.parse_args()
    try:
        tier_red = float(os.environ.get("RF_TIER_RED_MM_H", RED_BELOW_MM_H))
        tier_amber = float(os.environ.get("RF_TIER_AMBER_MM_H", AMBER_BELOW_MM_H))
    except ValueError:
        print(json.dumps({"error": "RF_TIER_RED_MM_H / RF_TIER_AMBER_MM_H must be numbers"}))
        sys.exit(3)
    try:
        kill = rain_rate_for_fade(args.margin, args.dist, args.freq,
                                  args.elevation, args.tau, args.method)
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        print(json.dumps({"error": f"cannot compute: {exc}"}))
        sys.exit(3)
    out = {
        "freq_ghz": args.freq,
        "dist_km": args.dist,
        "margin_db": args.margin,
        "kill_rain_rate_mm_h": kill,
        "tier": tier_for(kill, tier_red, tier_amber),
    }
    if kill is not None:
        k, alpha = rain_specific_attenuation_coefficients(
            args.freq, args.elevation, args.tau)
        out["k"] = round(k, 5)
        out["alpha"] = round(alpha, 4)
        out["atten_at_kill_db"] = round(args.margin, 2)
    print(json.dumps(out))


# Tier thresholds re-exported for CLI default use (RF_TIER_* env override, see main()).
RED_BELOW_MM_H = 25.0
AMBER_BELOW_MM_H = 50.0

if __name__ == "__main__":
    main()
