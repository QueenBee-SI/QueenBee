import os
import unittest

from queenbee.mother import approver

PROJECT = r"C:\Users\Jane Doe\work\proj"
OPTIONS = [{"n": 1, "text": "Yes"}, {"n": 2, "text": "Yes, allow all edits this session"}, {"n": 3, "text": "No"}]


def ask(text, *context):
    return approver.classify({"text": text, "context": list(context), "options": OPTIONS}, PROJECT)


class EditApproval(unittest.TestCase):
    def test_relative_path_is_inside(self):
        self.assertEqual(ask("Do you want to create lexer.py?", "Create file", r"minidb\lexer.py")[0], "approve")

    @unittest.skipUnless(os.name == "nt", "a Windows path")
    def test_absolute_path_inside_with_spaces(self):
        verdict, _ = ask("Do you want to create lexer.py?", PROJECT + r"\minidb\lexer.py")
        self.assertEqual(verdict, "approve")

    def test_absolute_path_outside_is_the_users(self):
        verdict, why = ask("Do you want to create x.py?", r"C:\Users\Jane Doe\Desktop\x.py")
        self.assertEqual((verdict, why), ("human", "edit outside the project folder"))

    def test_climbing_out_is_the_users(self):
        self.assertEqual(ask("Do you want to make this edit to x.py?", r"..\x.py")[0], "human")

    def test_system_folder_is_the_users(self):
        self.assertEqual(ask("Do you want to overwrite x.py?", r"C:\Windows\System32\x.py")[0], "human")


if __name__ == "__main__":
    unittest.main()


class CodexEdits(unittest.TestCase):
    OPTIONS = [{"n": 1, "text": "Yes, proceed (y)"}, {"n": 2, "text": "Yes, and don't ask again for these files (a)"},
               {"n": 3, "text": "No, and tell Codex what to do differently (esc)"}]

    def ask(self, *destinations, question="Description: command failed; retry without sandbox?"):
        context = ["Would you like to make the following edits?", *[f"Destination: {d}" for d in destinations]]
        return approver.classify({"text": question, "context": context, "options": self.OPTIONS}, PROJECT)

    @unittest.skipUnless(os.name == "nt", "a Windows path")
    def test_edits_to_files_inside_the_project_are_approved_even_when_codex_asks_to_retry_without_its_sandbox(self):
        verdict, why = self.ask(PROJECT + r"\minidb\lexer.py", PROJECT + r"\minidb\parser_base.py")
        self.assertEqual(verdict, "approve")
        self.assertIn("inside the project", why)
        self.assertEqual(approver.approve_option({"options": self.OPTIONS}), 1)             # the plain yes, not "don't ask again"

    def test_one_file_outside_the_project_is_the_users(self):
        self.assertEqual(self.ask(PROJECT + r"\a.py", r"C:\Users\Jane Doe\Desktop\b.py")[0], "human")
        self.assertEqual(self.ask(PROJECT + r"\..\b.py")[0], "human")

    def test_no_named_file_is_the_users(self):
        self.assertEqual(self.ask()[0], "human")
