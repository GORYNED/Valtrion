"""`tools/check_privacy.py`: finds private names, paths and addresses in what a branch adds, without holding a private name itself."""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_privacy", REPOSITORY / "tools" / "check_privacy.py")
check_privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_privacy)


def rules(text: str, hashes=frozenset(), terms=()) -> list[str]:
    return [finding.rule for finding in check_privacy.check_text(text, "x:1", set(hashes), list(terms))]


class PatternTests(unittest.TestCase):
    def test_a_real_looking_user_folder_is_found(self):
        self.assertEqual(["user-folder"], rules(r"C:\Users\Ivanov\project\file.txt"))  # privacy: ok
        self.assertEqual(["user-folder"], rules("see /Users/ivanov/work and more"))  # privacy: ok
        self.assertEqual(["user-folder"], rules("cd /home/ivanov; ls"))  # privacy: ok

    def test_stand_ins_are_not_found(self):
        for text in (r"C:\Users\dev\x", "/Users/builder/.local/bin", "/home/me\\nnext", "/home/x/Documents", r"C:\Users\<user>\x", "/home/user/a"):
            self.assertEqual([], rules(text), text)

    def test_an_email_is_found_but_a_git_remote_and_the_stand_in_domains_are_not(self):
        self.assertEqual(["email"], rules("write to someone@mail.example-company.org"))  # privacy: ok
        for text in ("git@github.com:owner/repo.git", "a@example.com", "noreply@anthropic.com", "123+name@users.noreply.github.com"):
            self.assertEqual([], rules(text), text)

    def test_a_private_network_address_is_found_but_made_up_ranges_are_not(self):
        for text in ("ssh dev@192.168.1.20", "http://100.101.102.103:80", "172.20.0.4", "10.20.30.40"):  # privacy: ok
            self.assertEqual(["private-address"], rules(text), text)
        for text in ("10.0.0.5:10080", "203.0.113.9", "8.8.8.8", "127.0.0.1", "version 1.2.3.4.5"):
            self.assertEqual([], rules(text), text)

    def test_a_real_looking_device_id_is_found_but_a_zero_padded_one_is_not(self):
        self.assertEqual(["device-id"], rules("ios:00008101-00AB12CD34EF5601"))  # privacy: ok
        self.assertEqual([], rules("ios:00008000-0000000000000001"))

    def test_a_line_can_say_it_is_fine(self):
        self.assertEqual([], rules(r"C:\Users\Ivanov\x  # privacy: ok"))


class NameTests(unittest.TestCase):
    NAME = "Example Name"

    def hashes(self):
        return {check_privacy.hash_term(self.NAME)}

    def test_a_hashed_name_is_found_however_it_is_written(self):
        for text in ("Example Name", "example-name", "ExampleName", "the example_name project", "EXAMPLE.NAME"):
            self.assertEqual(["private-term"], rules(text, self.hashes()), text)

    def test_other_words_are_not_found(self):
        for text in ("example", "name example", "examples names", "nothing here"):
            self.assertEqual([], rules(text, self.hashes()), text)

    def test_the_finding_never_prints_the_name(self):
        finding = check_privacy.check_text("our Example Name app", "f:1", self.hashes(), [])[0]
        self.assertNotIn("example", str(finding).lower())
        self.assertNotIn("name:", str(finding).lower().replace("hashed list", ""))

    def test_a_name_of_the_private_list_is_found_in_any_case(self):
        self.assertEqual(["private-term"], rules("About ZZ-Secret-Thing today", terms=["zz-secret-thing"]))

    def test_the_private_list_file_ignores_comments_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "terms.txt"
            path.write_text("# my names\n\nSecretOne\n  Second Name  \n", encoding="utf-8")
            self.assertEqual(["secretone", "second name"], check_privacy.load_private_terms(path))
            self.assertEqual([], check_privacy.load_private_terms(Path(folder) / "missing.txt"))

    def test_the_hash_file_lines_are_read_with_or_without_the_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "h.sha256"
            path.write_text("# comment\nsha256:ABC123\ndef456\n\n", encoding="utf-8")
            self.assertEqual({"abc123", "def456"}, check_privacy.load_hashes(path))

    def test_normalising_joins_the_words(self):
        self.assertEqual("examplename2", check_privacy.normalize("Example-Name_2"))


class BranchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.repo = Path(folder.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "a@example.com")
        self.git("config", "user.name", "a")
        (self.repo / "readme.txt").write_text("hello\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "first")
        self.git("checkout", "-q", "-b", "feature")
        self.addCleanup(setattr, check_privacy, "ROOT", check_privacy.ROOT)
        check_privacy.ROOT = self.repo

    def git(self, *arguments):
        subprocess.run(["git", *arguments], cwd=self.repo, check=True, capture_output=True)

    def commit(self, message="change"):
        self.git("add", ".")
        self.git("commit", "-q", "-m", message)

    def check(self, hashes=frozenset(), terms=()):
        return check_privacy.run("main", False, [], set(hashes), list(terms))

    def test_a_clean_branch_has_no_findings(self):
        (self.repo / "a.txt").write_text("nothing private\n", encoding="utf-8")
        self.commit()
        self.assertEqual(([], []), self.check())

    def test_an_added_line_is_found_with_its_file_and_line(self):
        (self.repo / "a.txt").write_text("fine\nC:\\Users\\Ivanov\\x\n", encoding="utf-8")  # privacy: ok
        self.commit()
        findings, _ = self.check()
        self.assertEqual(["a.txt:2"], [finding.where for finding in findings])

    def test_a_line_that_was_already_there_is_not_blamed(self):
        (self.repo / "readme.txt").write_text("C:\\Users\\Ivanov\\old\n", encoding="utf-8")  # privacy: ok
        self.git("add", ".")
        self.git("commit", "-q", "--amend", "-m", "first")
        self.git("branch", "-f", "main")
        (self.repo / "b.txt").write_text("fine\n", encoding="utf-8")
        self.commit()
        self.assertEqual([], self.check()[0])

    def test_a_commit_message_is_checked(self):
        (self.repo / "a.txt").write_text("fine\n", encoding="utf-8")
        self.commit("fix the thing in the Example Name app")
        findings, _ = self.check(hashes={check_privacy.hash_term("Example Name")})
        self.assertEqual(["commit message 1"], [finding.where for finding in findings])

    def test_the_branch_name_is_checked(self):
        self.git("checkout", "-q", "-b", "work-on-example-name")
        (self.repo / "a.txt").write_text("fine\n", encoding="utf-8")
        self.commit()
        findings, _ = self.check(hashes={check_privacy.hash_term("Example Name")})
        self.assertIn("branch name", [finding.where for finding in findings])

    def test_a_binary_file_is_listed_for_a_person_to_look_at(self):
        (self.repo / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0\0binary")
        self.commit()
        self.assertEqual(["shot.png"], self.check()[1])

    def test_given_text_such_as_a_pull_request_body_is_checked(self):
        (self.repo / "a.txt").write_text("fine\n", encoding="utf-8")
        self.commit()
        body = self.repo / "body.md"
        body.write_text("ok\nsee 192.168.1.9\n", encoding="utf-8")  # privacy: ok
        findings, _ = check_privacy.run("main", False, [body], set(), [])
        self.assertEqual(["body.md:2"], [finding.where for finding in findings])


class RepositoryTests(unittest.TestCase):
    def test_this_repository_passes_its_own_check(self):
        findings, _ = check_privacy.run(None, True, [], check_privacy.load_hashes(), [])
        self.assertEqual([], [str(finding) for finding in findings])

    def test_the_hash_list_in_the_repository_holds_hashes_only(self):
        for line in check_privacy.HASH_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                self.assertRegex(line, r"^sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
