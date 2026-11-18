import unittest
from pathlib import Path

from install_xfce import build_desktop_entry


class BuildDesktopEntryTests(unittest.TestCase):
    def test_builds_tray_launcher_with_quoted_paths(self) -> None:
        entry = build_desktop_entry(
            Path("/home/user/My Speech/.venv/bin/python"),
            Path("/home/user/My Speech/app.py"),
        )

        self.assertIn(
            'Exec="/home/user/My Speech/.venv/bin/python" '
            '"/home/user/My Speech/app.py" --tray',
            entry,
        )
        self.assertIn("Terminal=false", entry)
        self.assertIn("OnlyShowIn=XFCE;", entry)
        self.assertIn("X-GNOME-Autostart-enabled=true", entry)

    def test_escapes_quotes_in_desktop_exec_paths(self) -> None:
        entry = build_desktop_entry(
            Path('/home/user/Speech "test"/python'),
            Path("/home/user/Speech/app.py"),
        )

        self.assertIn(r'Exec="/home/user/Speech \"test\"/python"', entry)


if __name__ == "__main__":
    unittest.main()
