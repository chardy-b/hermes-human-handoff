from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("leak_lint", ROOT / "qa/leak_lint.py")
leak_lint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(leak_lint)


class LeakLintTests(unittest.TestCase):
    def run_lint(self, history, content=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").mkdir()
            if content is not None:
                (root / "sample.txt").write_text(content, encoding="utf-8")
            output = io.StringIO()
            with (
                mock.patch.object(leak_lint.sys, "argv", ["leak_lint.py", str(root)]),
                mock.patch.object(
                    leak_lint.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout=history),
                ),
                contextlib.redirect_stdout(output),
            ):
                result = leak_lint.main()
            return result, output.getvalue()

    def test_published_author_exceptions_are_exact_commits(self):
        history = "".join(
            f"{commit}\tauthor@example.invalid\n"
            for commit in leak_lint.INHERITED_NON_NOREPLY_COMMITS
        )
        self.assertEqual(self.run_lint(history), (0, "LEAK_LINT_OK\n"))
        unknown = "7b43ec768cb5" + "0" * 28
        result, output = self.run_lint(history + f"{unknown}\tauthor@example.invalid\n")
        self.assertEqual(result, 1)
        self.assertIn(f"git commit {unknown[:12]}: non-noreply author email", output)

    def test_noreply_author_is_accepted_without_exception(self):
        history = "0" * 40 + "\tauthor@users.noreply.github.com\n"
        self.assertEqual(self.run_lint(history), (0, "LEAK_LINT_OK\n"))

    def test_author_exception_does_not_skip_file_scanning(self):
        commit = "7b43ec768cb5ed3a33100d00931d8d1e59e0c932"
        result, output = self.run_lint(
            f"{commit}\tauthor@example.invalid\n", "BEGIN " + "PRIVATE KEY"
        )
        self.assertEqual(result, 1)
        self.assertIn("sample.txt: PEM private key", output)
