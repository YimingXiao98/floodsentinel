"""A reading's own quality flag feeds its credibility, without leaking hindsight.

USGS marks every real-time reading provisional, so that code is the normal case
and carries the value used when nothing is recorded. The term earns its place on
estimates: during a severe event a damaged or obstructed sensor is estimated
rather than measured, and an estimate is weaker evidence.

Approval is the trap. It is granted months after an event, so a replay that read
today's approved status for a 2017 reading would score it on information the live
system could not have had.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from watcher.claims.confidence import (
    provisional_at_observation,
    score_credibility,
    worst_qualifier,
)


def _no_ablation():
    env = {k: v for k, v in os.environ.items() if k != "SENTINEL_ABLATION"}
    return mock.patch.dict(os.environ, env, clear=True)


class TestWorstQualifier(unittest.TestCase):
    def test_a_single_code_is_returned(self):
        self.assertEqual(worst_qualifier(["P"]), "P")

    def test_an_estimate_outranks_anything_beside_it(self):
        self.assertEqual(worst_qualifier(["P", "e"]), "e")
        self.assertEqual(worst_qualifier(["A", "e"]), "e")

    def test_a_bare_string_is_accepted(self):
        self.assertEqual(worst_qualifier("e"), "e")

    def test_nothing_recorded_stays_nothing(self):
        for empty in (None, [], ""):
            self.assertIsNone(worst_qualifier(empty))

    def test_an_unknown_code_is_passed_through(self):
        self.assertEqual(worst_qualifier(["Ice"]), "Ice")


class TestNoHindsightFromApproval(unittest.TestCase):
    def test_approval_is_reported_as_provisional(self):
        self.assertEqual(provisional_at_observation("A"), "P")
        self.assertEqual(provisional_at_observation("R"), "P")

    def test_an_estimate_was_known_at_publication_and_is_kept(self):
        self.assertEqual(provisional_at_observation("e"), "e")

    def test_provisional_is_unchanged(self):
        self.assertEqual(provisional_at_observation("P"), "P")

    def test_a_replayed_reading_scores_the_same_as_a_live_one(self):
        # The whole point: 2017 data is approved by now, but replaying it must
        # not earn credibility the live system could not have had.
        with _no_ablation():
            live = score_credibility(classification_method="stage_derived",
                                     threshold_source="csv", data_qualifier="P")
            replayed = score_credibility(
                classification_method="stage_derived", threshold_source="csv",
                data_qualifier=provisional_at_observation(worst_qualifier(["A"])))
            self.assertEqual(live["quality"], replayed["quality"])


class TestQualifierMovesCredibility(unittest.TestCase):
    def _quality(self, qualifier):
        with _no_ablation():
            return score_credibility(
                classification_method="stage_derived", threshold_source="csv",
                data_qualifier=qualifier)["quality"]

    def test_an_estimate_is_less_credible_than_a_measurement(self):
        self.assertLess(self._quality(["P", "e"]), self._quality(["P"]))

    def test_provisional_matches_an_unrecorded_qualifier(self):
        # Real-time data is always provisional, so it is not a mark against a
        # gauge and must not shift scores relative to a corpus lacking the flag.
        self.assertEqual(self._quality(["P"]), self._quality(None))


if __name__ == "__main__":
    unittest.main()
