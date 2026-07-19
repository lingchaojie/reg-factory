import subprocess
from pathlib import Path
import unittest


class ClaudeLedgerGitignoreTests(unittest.TestCase):
    def test_atomic_ledger_and_journal_residues_are_narrowly_ignored(self):
        root = Path(__file__).resolve().parents[1]
        identifier = "0123456789abcdef0123456789abcdef"
        ignored = {
            f".emails_used.txt.{identifier}.tmp",
            f".emails_error.txt.{identifier}.tmp",
            f".emails_used_claude_api.txt.{identifier}.tmp",
            f".emails_error_claude_api.txt.{identifier}.tmp",
            f".mail_used_claude.txt.{identifier}.tmp",
            f".mail_error_claude.txt.{identifier}.tmp",
            f".mail_used_claude_api.txt.{identifier}.tmp",
            f".mail_error_claude_api.txt.{identifier}.tmp",
            f"..claude_email_pool.journal.{identifier}.tmp",
            ".claude_email_pool.journal",
        }
        visible = {
            f".unrelated.{identifier}.tmp",
            f"nested/.emails_used.txt.{identifier}.tmp",
            f".emails_used.txt.{identifier}.bak",
        }
        matched = set()
        for candidate in sorted(ignored | visible):
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", candidate],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertIn(result.returncode, (0, 1), result.stderr)
            if result.returncode == 0:
                matched.add(candidate)

        self.assertEqual(matched, ignored)


if __name__ == "__main__":
    unittest.main()
