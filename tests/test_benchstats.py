import unittest

from queenbee import benchstats as b


class Stats(unittest.TestCase):
    def test_two_arms_are_abba(self):
        s = b.schedule(["A", "B"], 4)
        self.assertEqual([a for a, _ in s], list("ABBAABBA"))
        self.assertEqual(sorted(r for a, r in s if a == "A"), [1, 2, 3, 4])

    def test_many_arms_shuffle_reproducibly_each_round_complete(self):
        s = b.schedule(["a", "b", "c", "d"], 3, seed=7)
        self.assertEqual(s, b.schedule(["a", "b", "c", "d"], 3, seed=7))
        for i in range(0, 12, 4):
            self.assertEqual(sorted(x for x, _ in s[i:i + 4]), list("abcd"))

    def test_ci(self):
        d = b.mean_ci([1, 2, 3])
        self.assertAlmostEqual(d["mean"], 2)
        self.assertAlmostEqual(d["ci95"], 4.303 / 3 ** 0.5, places=3)
        self.assertIsNone(b.mean_ci([5])["ci95"])

    def test_failure_is_not_free(self):
        rows = [{"arm": "x", "minutes": 5, "cost_usd": 1.0, "score": {"score": 1.0}},
                {"arm": "x", "minutes": 9, "cost_usd": 2.0, "score": {"score": 0.5}},
                {"arm": "x", "minutes": 9, "cost_usd": 3.0, "score": {"score": 1.0}, "timed_out": True}]
        a = b.aggregate(rows)["x"]
        self.assertEqual(a["succeeded"], 1)
        self.assertAlmostEqual(a["cost_per_success"], 6.0)
        self.assertAlmostEqual(a["cost_usd"]["mean"], 2.0)

    def test_trace_ids_are_unique(self):
        self.assertNotEqual(b.trace_id("q", 1), b.trace_id("q", 1))

    def test_single_agent_stops_are_all_human_and_kinds_are_named(self):
        run = {"interventions": ["Do you want to create tests/test_x.py?", "Allow reading ISSUES.md",
                                 "Run python -m pytest?", "Which database should it use?"],
               "steering_stops": [{"at_s": 10}], "setup_approvals": 1}
        s = b.human_stops(run)
        self.assertEqual((s["total"], s["to_human"]), (6, 6))
        self.assertEqual(s["kinds"], {"write": 1, "read": 1, "command": 1, "question": 1,
                                      "handed back a choice": 1, "folder trust": 1})

    def test_a_queen_answers_most_of_them_herself(self):
        s = b.human_stops({"stops": 9, "left_to_user": 1, "answered_by_queen": 8, "queen_steers": 2})
        self.assertEqual((s["total"], s["to_human"], s["answered_by_queen"]), (9, 1, 8))

    def test_reaction_time_is_added_per_human_stop(self):
        self.assertAlmostEqual(b.with_human_wait(5.0, 6, 33), 5.0 + 6 * 30 / 60)
        self.assertEqual(b.with_human_wait(5.0, 0, 120), 5.0)
