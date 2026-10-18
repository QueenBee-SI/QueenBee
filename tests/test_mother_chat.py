"""Talking the job through with the queen: READY: ends the talk, nothing else does."""
import json
import unittest
from unittest import mock

from queenbee.mother import chat


class Talk(unittest.TestCase):
    def test_ready_splits_what_to_show_from_the_instruction(self):
        shown, instruction = chat.split_ready("Good, I have what I need.\nREADY:\nBuild X in folder Y.\nDone when tests pass.")
        self.assertEqual(shown, "Good, I have what I need.")
        self.assertEqual(instruction, "Build X in folder Y.\nDone when tests pass.")

    def test_no_ready_line_means_the_talk_goes_on(self):
        self.assertEqual(chat.split_ready("Which folder?\nAnd what makes it done?"), ("Which folder?\nAnd what makes it done?", ""))

    def test_the_last_turn_must_be_the_users(self):
        with mock.patch("queenbee.mother.chat.binary_for", return_value="claude"):
            self.assertFalse(chat.reply([], "", "sonnet")["ok"])
            self.assertFalse(chat.reply([{"role": "queen", "text": "hi"}], "", "sonnet")["ok"])

    def test_a_turn_goes_to_claude_on_stdin_with_the_whole_talk(self):
        seen = {}

        def fake(argv, **kw):
            seen.update(argv=argv, prompt=kw["input"])
            return mock.Mock(stdout=json.dumps({"result": "What is done?\nREADY:\nDo it.", "usage": {"input_tokens": 5}}), stderr="")
        with mock.patch("queenbee.mother.chat.binary_for", return_value="claude"), mock.patch("subprocess.run", side_effect=fake):
            out = chat.reply([{"role": "user", "text": "fix the bugs"}, {"role": "queen", "text": "which?"},
                              {"role": "user", "text": "all of them"}], "", "sonnet")
        self.assertTrue(out["ok"])
        self.assertEqual((out["reply"], out["ready"]), ("What is done?", "Do it."))
        self.assertIn("fix the bugs", seen["prompt"])
        self.assertIn("all of them", seen["prompt"])
        self.assertIn("--tools", seen["argv"])
        self.assertNotIn("fix the bugs", " ".join(seen["argv"]))

    def test_long_talks_are_cut_to_the_last_messages(self):
        many = [{"role": "user" if i % 2 == 0 else "queen", "text": f"m{i}"} for i in range(60)]
        many.append({"role": "user", "text": "last"})
        self.assertLessEqual(len(chat._clean(many)), chat.MAX_MESSAGES)
        self.assertEqual(chat._clean(many)[-1]["text"], "last")


if __name__ == "__main__":
    unittest.main()


class WithAHive(unittest.TestCase):
    def test_a_running_hive_goes_into_the_prompt_and_ready_never_starts_anything(self):
        seen = {}

        def fake(argv, **kw):
            seen["prompt"] = kw["input"]
            return mock.Mock(stdout=json.dumps({"result": "W2 is stuck.\nREADY:\nsomething", "usage": {}}), stderr="")
        hive = "- W1 Parser: claude sonnet, done\n- W2 Executor: claude opus, agent working"
        with mock.patch("queenbee.mother.chat.binary_for", return_value="claude"), mock.patch("subprocess.run", side_effect=fake):
            out = chat.reply([{"role": "user", "text": "how is it going?"}], "", "sonnet", hive=hive)
        self.assertIn("W2 Executor", seen["prompt"])
        self.assertIn("The work has already started", seen["prompt"])
        self.assertEqual(out["ready"], "")                          # no plan to start while work is running
        self.assertIn("W2 is stuck.", out["reply"])

    def test_without_a_hive_the_prompt_is_the_planning_talk(self):
        seen = {}

        def fake(argv, **kw):
            seen["prompt"] = kw["input"]
            return mock.Mock(stdout=json.dumps({"result": "ok", "usage": {}}), stderr="")
        with mock.patch("queenbee.mother.chat.binary_for", return_value="claude"), mock.patch("subprocess.run", side_effect=fake):
            chat.reply([{"role": "user", "text": "hi"}], "", "sonnet")
        self.assertNotIn("The work has already started", seen["prompt"])
        self.assertNotIn("The hive right now", seen["prompt"])
