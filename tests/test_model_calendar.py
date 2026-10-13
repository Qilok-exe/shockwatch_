"""Exact intraday HAR cannot bridge an absent configured exchange session."""
from pathlib import Path
import tempfile
import unittest
import numpy as np
from shockwatch.common import config, utc
from shockwatch.models import fit, har_forecast
from shockwatch.synthetic import generate


class IntradayTrainingCalendarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.panel, cls.calendar, _ = generate(Path(cls.tmp.name)/"inputs", 160)
        cls.cfg = config()
        cls.cfg["synthetic"] = True
        cls.cutoff = str(cls.calendar.session.iloc[99])+"T23:59:00Z"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_missing_training_day_cannot_be_a_next_session_target_or_lag(self):
        complete = fit(self.panel, self.cutoff, self.cfg)
        missing = str(self.calendar.session.iloc[80])
        panel = self.panel[~self.panel.session.eq(missing)]
        fitted = fit(panel, self.cutoff, self.cfg)
        for symbol in fitted["universe"]:
            entry = fitted["symbols"][symbol]
            self.assertLess(entry["intraday_har_training_pairs"],
                            complete["symbols"][symbol]["intraday_har_training_pairs"]-1)
            self.assertEqual(len(entry["initial_intraday_history"]), 19)
            value = har_forecast({"har":entry["intraday_har"]},
                                 np.asarray(entry["initial_intraday_history"], dtype=float))
            self.assertTrue(np.isnan(value))

    def test_explicit_closed_weekdays_remain_consecutive_market_sessions(self):
        model = fit(self.panel, self.cutoff, self.cfg)
        for entry in model["symbols"].values():
            self.assertEqual(entry["intraday_har_training_pairs"], 77)
            self.assertEqual(len(entry["initial_intraday_history"]), 22)


if __name__ == "__main__":
    unittest.main()
