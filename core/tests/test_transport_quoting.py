import unittest

from valtrion.transport import CommandResult, SshTransport, quote_cmd

from .shortdir import short_temp_dir


class QuoteCmdTests(unittest.TestCase):
    def test_plain_arguments_stay_as_they_are(self):
        for argument in ("python", "C:\\Users\\me\\tools\\tool.py", "--timeout-sec", "a/b.c-d_e:1", "user@host"):
            self.assertEqual(argument, quote_cmd(argument))

    def test_metacharacters_are_wrapped_in_quotes_where_cmd_treats_them_literally(self):
        for argument in ("a&b", "a|b", "a<b", "a>b", "a^b", "(x)", "a b", "x;y", "a=b", "!x!", "it's", "", "a,b"):
            with self.subTest(argument):
                self.assertEqual(f'"{argument}"', quote_cmd(argument))

    def test_trailing_backslashes_cannot_escape_the_closing_quote(self):
        self.assertEqual('"C:\\my dir\\\\"', quote_cmd("C:\\my dir\\"))

    def test_what_cannot_be_made_literal_is_refused(self):
        for argument in ("%PATH%", "50%", 'say "hi"', "a\nb", "a\rb", "a\x00b"):
            with self.subTest(repr(argument)), self.assertRaises(ValueError):
                quote_cmd(argument)


class ConnectionOptionTests(unittest.TestCase):
    def option(self, argv, name):
        return [argv[i + 1] for i, item in enumerate(argv) if item == "-o" and argv[i + 1].startswith(name + "=")]

    def test_liveness_options_are_always_set(self):
        argv = SshTransport("me@host", multiplex=False).ssh_argv(["x"])
        self.assertEqual(["ServerAliveInterval=15"], self.option(argv, "ServerAliveInterval"))
        self.assertEqual(["ServerAliveCountMax=4"], self.option(argv, "ServerAliveCountMax"))
        self.assertEqual(["BatchMode=yes"], self.option(argv, "BatchMode"))
        self.assertEqual([], self.option(argv, "ControlMaster"))

    def test_a_posix_client_shares_one_connection_through_a_socket_in_the_state_folder(self):
        import tempfile
        from pathlib import Path
        with short_temp_dir() as folder:
            transport = SshTransport("me@host", control_dir=Path(folder) / "ssh", multiplex=True)
            argv = transport.ssh_argv(["x"])
            self.assertEqual(["ControlMaster=auto"], self.option(argv, "ControlMaster"))
            self.assertEqual(["ControlPersist=10m"], self.option(argv, "ControlPersist"))
            path = self.option(argv, "ControlPath")[0]
            self.assertTrue(path.startswith("ControlPath=" + str(Path(folder) / "ssh")))
            self.assertTrue(path.endswith("cm-%C"))
            self.assertTrue((Path(folder) / "ssh").is_dir())
            self.assertEqual(self.option(argv, "ControlPath"), self.option(transport._options(), "ControlPath"), "scp shares the connection too")

    def test_the_windows_client_has_no_control_master(self):
        from pathlib import Path
        argv = SshTransport("me@host", control_dir=Path("C:/gdv/ssh"), multiplex=False).ssh_argv(["x"])
        self.assertEqual([], self.option(argv, "ControlPath"))

    def test_a_state_folder_with_whitespace_disables_sharing_instead_of_breaking_every_call(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            argv = SshTransport("me@host", control_dir=Path(folder) / "my state", multiplex=True).ssh_argv(["x"])
            self.assertEqual([], self.option(argv, "ControlPath"))

    def test_a_state_folder_too_long_for_a_unix_socket_disables_sharing_instead_of_failing(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            long_dir = Path(folder) / ("d" * 90)
            argv = SshTransport("me@host", control_dir=long_dir, multiplex=True).ssh_argv(["x"])
            self.assertEqual([], self.option(argv, "ControlPath"))


class WindowsSshTests(unittest.TestCase):
    def test_arguments_are_quoted_for_the_remote_cmd(self):
        transport = SshTransport("me@win", posix=False)
        argv = transport.ssh_argv(["python", "C:\\p q\\tools\\tool.py", "job", "start", "--", "tool", "a&calc"])
        self.assertEqual('python "C:\\p q\\tools\\tool.py" job start -- tool "a&calc"', argv[-1])
        self.assertEqual("--", argv[-2])

    def test_an_unsafe_argument_is_refused_before_anything_is_sent(self):
        sent = []
        transport = SshTransport("me@win", posix=False, runner=lambda *call: (sent.append(call), CommandResult(0))[1])
        result = transport.run(["python", "valtrion.py", "echo", "%USERPROFILE%"], 10)
        self.assertEqual(2, result.returncode)
        self.assertIn("refused", result.stderr)
        self.assertEqual([], sent)

    def test_posix_hosts_keep_shell_quoting(self):
        argv = SshTransport("me@mac").ssh_argv(["python3", "tools/tool.py", "a b", "$HOME", "x;y"], "~/Documents/Projects/x y")
        self.assertEqual("cd ~/'Documents/Projects/x y' && python3 tools/tool.py 'a b' '$HOME' 'x;y'", argv[-1])


if __name__ == "__main__":
    unittest.main()
