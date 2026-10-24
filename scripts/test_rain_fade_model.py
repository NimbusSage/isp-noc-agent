"""Tests for scripts/rain_fade_model.py (pure math, no network)."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rain_fade_model as rfm  # noqa: E402


class CoefficientTests(unittest.TestCase):
    def test_k60_plausibility(self):
        # P.838-3 ships only fit coefficients (no spot table). At 60 GHz the
        # -3 fit gives k~0.856, alpha~0.757; the superseded P.838-1 spot
        # table interpolates to k~0.6745/alpha~0.825. The two agree within
        # ~40% on gamma through the realistic band, which is far inside the
        # green/amber/red tier spacing used by the readiness check - assert
        # a wide plausibility window, not table equality.
        k, alpha = rfm.rain_specific_attenuation_coefficients(60.0)
        self.assertTrue(0.5 <= k <= 1.2, f"k={k}")
        self.assertTrue(0.6 <= alpha <= 0.95, f"alpha={alpha}")
        k_old = (0.707 + 0.642) / 2  # legacy P.838-1 el=0 average at 60 GHz
        a_old = 0.825
        g3 = rfm.gamma_r(60.0, 30.0)
        g1 = k_old * 30.0 ** a_old
        self.assertAlmostEqual(g3, g1, delta=g1 * 0.45)

    def test_5ghz_is_negligible_vs_60ghz(self):
        k5, a5 = rfm.rain_specific_attenuation_coefficients(5.4)
        k60, a60 = rfm.rain_specific_attenuation_coefficients(60.0)
        self.assertLess(k5, 0.01)
        self.assertGreater(k60, 0.3)


class GammaAndPathTests(unittest.TestCase):
    def test_gamma_monotonic_in_rain_rate(self):
        for r in (1.0, 10.0, 50.0):
            self.assertLess(rfm.gamma_r(60.0, r), rfm.gamma_r(60.0, r + 1))
        self.assertAlmostEqual(rfm.gamma_r(60.0, 0), 0.0)

    def test_path_attenuation_monotonic_in_distance_and_rain(self):
        for r in (10.0, 50.0):
            self.assertLess(rfm.path_attenuation_db(r, 1.0, 60.0),
                            rfm.path_attenuation_db(r, 3.0, 60.0))
            self.assertLess(rfm.path_attenuation_db(r, 2.0, 60.0),
                            rfm.path_attenuation_db(r + 5, 2.0, 60.0))

    def test_short_path_full_length(self):
        # On very short paths d0 >> d, attenuation ~= gamma * d.
        self.assertAlmostEqual(
            rfm.path_attenuation_db(25.0, 0.5, 60.0),
            rfm.gamma_r(60.0, 25.0) * 0.5,
            delta=rfm.gamma_r(60.0, 25.0) * 0.5 * 0.05)


class RainRateForFadeTests(unittest.TestCase):
    def test_zero_margin_starts_at_zero(self):
        self.assertEqual(rfm.rain_rate_for_fade(0.0, 2.0, 60.0), 0.0)

    def test_bigger_margin_needs_more_rain(self):
        low = rfm.rain_rate_for_fade(6.0, 2.0, 60.0)
        high = rfm.rain_rate_for_fade(15.0, 2.0, 60.0)
        self.assertIsNotNone(low)
        self.assertIsNotNone(high)
        self.assertGreater(high, low)

    def test_longer_path_needs_less_rain(self):
        short = rfm.rain_rate_for_fade(10.0, 1.0, 60.0)
        long_ = rfm.rain_rate_for_fade(10.0, 5.0, 60.0)
        self.assertIsNotNone(short)
        self.assertIsNotNone(long_)
        self.assertLess(long_, short)

    def test_invalid_inputs_return_none(self):
        self.assertIsNone(rfm.rain_rate_for_fade(None, 2.0, 60.0))
        self.assertIsNone(rfm.rain_rate_for_fade(10.0, None, 60.0))
        self.assertIsNone(rfm.rain_rate_for_fade(10.0, 0.0, 60.0))
        self.assertIsNone(rfm.rain_rate_for_fade(-1.0, 2.0, 60.0))

    def test_huge_margin_never_fades(self):
        self.assertIsNone(rfm.rain_rate_for_fade(500.0, 2.0, 60.0))

    def test_realistic_60ghz_short_link_plausibility(self):
        # 2 km, 12 dB margin: prediction should sit in the 5-40 mm/h band
        # (order-of-magnitude guard against a silently wrong fit).
        kill = rfm.rain_rate_for_fade(12.0, 2.0, 60.0)
        self.assertIsNotNone(kill)
        self.assertTrue(5.0 <= kill <= 40.0, f"kill={kill}")

    def test_5ghz_backup_does_not_fade_in_realistic_rain(self):
        # 5.4 GHz backup over the same path: 10 dB margin should survive
        # far beyond any realistic rain rate.
        kill = rfm.rain_rate_for_fade(10.0, 2.0, 5.4)
        self.assertTrue(kill is None or kill > 200.0, f"kill={kill}")


class TierTests(unittest.TestCase):
    def test_tiers(self):
        t = rfm.tier_for
        self.assertEqual(t(None), "none")
        self.assertEqual(t(5.0), "red")
        self.assertEqual(t(25.0), "red")
        self.assertEqual(t(49.9), "amber")
        self.assertEqual(t(75.0), "green")


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        import rain_fade_model as mod
        old_argv = sys.argv
        sys.argv = ["rain_fade_model.py", *argv]
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    mod.main()
                except SystemExit as exc:
                    code = int(exc.code or 0)
        finally:
            sys.argv = old_argv
        return code, out.getvalue(), err.getvalue()

    def test_cli_json_shape(self):
        code, out, err = self.run_cli("--freq", "60", "--dist", "2.0",
                                      "--margin", "12.5")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn("kill_rain_rate_mm_h", data)
        self.assertIn("tier", data)
        self.assertEqual(data["tier"], rfm.tier_for(data["kill_rain_rate_mm_h"]))

    def test_cli_missing_required(self):
        code, out, err = self.run_cli("--freq", "60")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
