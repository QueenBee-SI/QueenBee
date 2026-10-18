"""The director: the queen's model looks in on the hive only when something changed and workers are active."""
import threading
import unittest
from unittest import mock

from queenbee.mother import director


class FakeDispatch:
    ENDED = ("done", "failed", "stopped", "stopped early")

    def __init__(self, status="agent working"):
        self.jobs = [{"priority": 1, "kind": "task", "title": "Store", "goal": "build store", "status": status,
                      "project_path": ".", "progress": "decided note shape"}]
        self.board_log, self.verify, self.costs = [], {}, {}
        self.lock = threading.Lock()
        self.said, self.events = [], []

    def view(self):
        return [dict(j) for j in self.jobs]

    def say(self, to, text, sender="queen"):
        self.said.append((to, text, sender))

    def _event_all(self, text):
        self.events.append(text)


class DirectorTest(unittest.TestCase):
    def test_digest_names_task_progress_and_board(self):
        d = FakeDispatch()
        text = director.digest(d.view(), [{"from": "W1", "to": ["all"], "text": "notes are dicts"}], None)
        self.assertIn("W1 Store [agent working]", text)
        self.assertIn("decided note shape", text)
        self.assertIn("notes are dicts", text)

    def test_parse_takes_json_out_of_prose(self):
        self.assertEqual(director._parse('Sure: {"messages": [], "summary": "ok"} done')["summary"], "ok")
        self.assertEqual(director._parse("no json"), {})

    def test_looks_when_active_speaks_and_counts_cost(self):
        d = FakeDispatch()
        queen = director.Director(lambda: d, "sonnet", 1)
        answer = {"messages": [{"to": "W1", "text": "use dicts"}], "summary": "mismatch", "cost_usd": 0.02, "tokens": 900}
        with mock.patch.object(director, "look", return_value=answer) as look:
            queen.tick()
            queen.tick()                          # nothing changed: no second look, no tokens
        self.assertEqual(look.call_count, 1)
        self.assertEqual(d.said, [("W1", "use dicts", "queen")])
        self.assertEqual(d.costs["director"]["cost_usd"], 0.02)
        self.assertEqual(queen.status()["looks"], 1)

    def test_no_look_when_nothing_is_running_or_budget_is_spent(self):
        done = FakeDispatch(status="done")
        with mock.patch.object(director, "look") as look:
            director.Director(lambda: done, "sonnet", 1).tick()
            director.Director(lambda: FakeDispatch(), "sonnet", 1, budget=lambda: False).tick()
        look.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class WakeOnEvents(unittest.TestCase):
    """A worker speaking to the queen, or a task changing state, pulls the next look forward; the queen's own words and a
    quiet hive do not; looks stay min_gap_s apart; the switch turns it off."""

    def test_a_board_message_to_the_queen_wakes_her(self):
        d = FakeDispatch()
        queen = director.Director(lambda: d, "sonnet", 120)
        queen._watch()                                            # the first watch only takes note of the state
        self.assertEqual(queen.wakes, 0)
        d.board_log.append({"from": "queen", "to": ["all"], "text": "carry on"})
        queen._watch()
        self.assertEqual(queen.wakes, 0)                          # her own words are not news
        d.board_log.append({"from": "W1", "to": ["queen"], "text": "which shape for notes?"})
        before = queen.next_at
        queen._watch()
        self.assertEqual(queen.wakes, 1)
        self.assertLess(queen.next_at, before)
        self.assertLessEqual(queen.next_at, director.time.time() + 5.5)

    def test_a_status_change_wakes_her_and_the_gap_holds(self):
        d = FakeDispatch()
        queen = director.Director(lambda: d, "sonnet", 120, min_gap_s=40)
        queen._watch()
        queen.last_look_at = director.time.time()                 # she has just looked
        queen.next_at = queen.last_look_at + 120                  # and the timer says: in two minutes
        d.jobs[0]["status"] = "blocked"
        queen._watch()
        self.assertEqual(queen.wakes, 1)
        self.assertGreaterEqual(queen.next_at, queen.last_look_at + 40 - 0.5)
        queen._watch()                                            # nothing new: no second wake
        self.assertEqual(queen.wakes, 1)

    def test_the_switch_turns_it_off(self):
        d = FakeDispatch()
        queen = director.Director(lambda: d, "sonnet", 120, wake_on_events=False)
        queen._watch()
        d.board_log.append({"from": "W1", "to": ["queen"], "text": "help"})
        queen._watch()
        self.assertEqual(queen.wakes, 0)
        self.assertFalse(queen.status()["wake_on_events"])

