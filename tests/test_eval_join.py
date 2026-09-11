"""Tests for the corpus join in code/run_eval.py.

`question_id` is a PER-GAME index. The 25,919 instances in fbsim's
lowprob_questions.json carry only 336 distinct ids, and 246 of those name a
different (template, target, resolution turn) in different games. Keying a
lookup on the id alone therefore silently matches each eval row against an
unrelated game's question. That single mistake produced both bugs this repo
has hit: Graph 4 selected the wrong subset, and run_eval stamped 826 of 1,739
rows with the wrong climatology.

These tests pin the join itself — that the key is the PAIR, that a repeated
id across games cannot bleed, and that a row with no corpus match is left
None rather than silently given someone else's rate.

Run with:  python3 -m unittest tests.test_eval_join -v   (from the repo root)
"""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load_run_eval():
    """Import code/run_eval.py by path; it is a script, not a package module.

    Its model/eval harness imports are deferred (load_harness), so this works
    without litellm or the FreeCiv gym environment installed.
    """
    spec = importlib.util.spec_from_file_location("run_eval", REPO / "code" / "run_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    run_eval = _load_run_eval()
except Exception as exc:                                    # pragma: no cover
    run_eval = None
    _why = exc


def q(game, qid, rate, **extra):
    return dict(game_id=game, question_id=qid, class_base_rate=rate, **extra)


def r(game, qid, rate=None):
    row = {"game_id": game, "question_id": qid}
    if rate is not None:
        row["class_base_rate"] = rate
    return row


@unittest.skipIf(run_eval is None, lambda: f"could not import code/run_eval.py: {_why}")
class TestAttachBaseRates(unittest.TestCase):
    def test_same_question_id_in_two_games_does_not_bleed(self):
        # The exact shape of the bug: one id, two games, two different rates.
        corpus = [q("seed1", "q0249", 0.051), q("seed2", "q0249", 0.082)]
        rows = [r("seed1", "q0249"), r("seed2", "q0249")]
        run_eval.attach_base_rates(rows, corpus)
        self.assertEqual([x["class_base_rate"] for x in rows], [0.051, 0.082])

    def test_returns_the_number_of_rows_it_changed(self):
        corpus = [q("seed1", "q1", 0.06), q("seed2", "q1", 0.07)]
        rows = [r("seed1", "q1", 0.06),      # already right
                r("seed2", "q1", 0.06)]      # wrong: another game's rate
        self.assertEqual(run_eval.attach_base_rates(rows, corpus), 1)
        self.assertEqual(rows[1]["class_base_rate"], 0.07)

    def test_unmatched_row_is_set_to_none_not_left_stale(self):
        # A stale wrong number is worse than a missing one — score() handles
        # None explicitly (see the companion test below).
        rows = [r("seed9", "q1", 0.06)]
        run_eval.attach_base_rates(rows, [q("seed1", "q1", 0.06)])
        self.assertIsNone(rows[0]["class_base_rate"])

    def test_is_idempotent(self):
        corpus = [q("seed1", "q1", 0.06), q("seed2", "q1", 0.07)]
        rows = [r("seed1", "q1"), r("seed2", "q1")]
        run_eval.attach_base_rates(rows, corpus)
        self.assertEqual(run_eval.attach_base_rates(rows, corpus), 0)


@unittest.skipIf(run_eval is None, lambda: f"could not import code/run_eval.py: {_why}")
class TestScoreUsesTheRowsClimatology(unittest.TestCase):
    def test_bss_is_measured_against_the_attached_base_rate(self):
        # Two rows, same forecast, different climatology: the skill score must
        # move with the climatology, which is what the bad join corrupted.
        def bss_for(rate):
            rows = [{"question_id": "q1", "game_id": "g1", "ground_truth": False,
                     "class_base_rate": rate,
                     "predictions": {"m": {"probability": 0.5}}}]
            return run_eval.score(rows, ["m"])["m"]["bss"]

        self.assertNotAlmostEqual(bss_for(0.01), bss_for(0.08))

    def test_a_row_with_no_climatology_falls_back_instead_of_crashing(self):
        # attach_base_rates SETS the key to None on an unmatched row, so a
        # dict.get(key, default) would hand None straight into the arithmetic.
        rows = [{"question_id": "q1", "game_id": "g1", "ground_truth": False,
                 "class_base_rate": None,
                 "predictions": {"m": {"probability": 0.5}}}]
        m = run_eval.score(rows, ["m"])["m"]
        self.assertEqual(m["n"], 1)
        self.assertAlmostEqual(m["brier_climatology"], run_eval.CLIM_FALLBACK ** 2)


if __name__ == "__main__":
    unittest.main()
