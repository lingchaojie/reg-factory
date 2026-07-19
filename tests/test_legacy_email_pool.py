import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from common import emails


class LegacyEmailPoolDisplayTests(unittest.TestCase):
    def test_invalid_display_does_not_reserve_an_address(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "emails.txt"
            used = root / "emails_used_tri.txt"
            source.write_text(
                "person@example.com----mail-pass----refresh----client\n",
                encoding="utf-8",
            )
            with patch.object(emails, "EMAILS_FILE", str(source)), patch.object(
                emails, "_used_file", return_value=str(used)
            ), self.assertRaisesRegex(ValueError, "display"):
                emails.next_email("tri", display="unsafe")

            self.assertFalse(used.exists())

    def test_masked_display_is_opt_in_and_default_display_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "emails.txt"
            used = root / "emails_used_tri.txt"
            source.write_text(
                "person@example.com----mail-pass----refresh----client\n",
                encoding="utf-8",
            )

            masked_output = io.StringIO()
            with patch.object(emails, "EMAILS_FILE", str(source)), patch.object(
                emails, "_used_file", return_value=str(used)
            ), patch.object(
                emails,
                "_error_file",
                return_value=str(root / "emails_error_tri.txt"),
            ), redirect_stdout(masked_output):
                selected = emails.next_email("tri", display="masked")

            self.assertEqual(selected[0], "person@example.com")
            self.assertNotIn("person@example.com", masked_output.getvalue())
            self.assertIn("pe***@example.com", masked_output.getvalue())

            used.unlink()
            default_output = io.StringIO()
            with patch.object(emails, "EMAILS_FILE", str(source)), patch.object(
                emails, "_used_file", return_value=str(used)
            ), patch.object(
                emails,
                "_error_file",
                return_value=str(root / "emails_error_tri.txt"),
            ), redirect_stdout(default_output):
                emails.next_email("tri")

            self.assertIn("person@example.com", default_output.getvalue())


if __name__ == "__main__":
    unittest.main()
