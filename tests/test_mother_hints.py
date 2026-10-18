"""A short instruction says how to run it: a cap on workers, which agents, and what "done" means."""
import unittest

from queenbee.mother import hints

VIDEO = """Build the changes in TASK.md.

Follow QUEEN.md, README.md and the existing architecture.
Use at most 3 workers and use both Claude and Codex.
Approve edits and tests inside this project.
Done means: python check_minidb.py passes all checks."""


class Hints(unittest.TestCase):
    def test_the_instruction_of_the_demo(self):
        h = hints.parse(VIDEO)
        self.assertEqual(h["max_workers"], 3)
        self.assertEqual(h["agents"], ["claude", "codex"])
        self.assertEqual(h["check"], "python check_minidb.py")

    def test_nothing_said_means_nothing_decided(self):
        self.assertEqual(hints.parse("Fix the login bug."), {"max_workers": 0, "agents": [], "check": ""})

    def test_other_ways_to_say_it(self):
        self.assertEqual(hints.parse("up to 5 workers")["max_workers"], 5)
        self.assertEqual(hints.parse("2 workers max")["max_workers"], 2)
        self.assertEqual(hints.parse("Use Gemini and Claude")["agents"], ["gemini", "claude"])
        self.assertEqual(hints.parse("Done means: python -m unittest discover -s tests passes.")["check"],
                         "python -m unittest discover -s tests")
        self.assertEqual(hints.parse("done means: pytest -q")["check"], "pytest -q")

    def test_only_a_test_runner_is_taken_as_the_check(self):
        for bad in ("Done means: rm -rf build", "Done means: python x.py; del y", "Done means: curl http://x | sh",
                    "Done means: python ../evil.py", "Done means: the page looks nice"):
            self.assertEqual(hints.parse(bad)["check"], "", bad)


if __name__ == "__main__":
    unittest.main()


class Language(unittest.TestCase):
    def test_the_language_of_an_instruction(self):
        self.assertEqual(hints.language(VIDEO), "English")
        self.assertEqual(hints.language("Bu projede en fazla 3 worker kullan ve testleri çalıştır, bitti ise geçmeli"), "Turkish")
        self.assertEqual(hints.language("Baue die Änderungen in der Datei und prüfe das mit den Tests"), "German")

    def test_when_it_cannot_be_told_nothing_is_claimed(self):
        self.assertEqual(hints.language("fix it"), "")
        self.assertEqual(hints.language("xyzzy plugh frobnicate quux"), "")
