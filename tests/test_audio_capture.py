import unittest

from audio_capture import AudioCaptureError, resolve_monitor_source


class ResolveMonitorSourceTests(unittest.TestCase):
    def test_selects_monitor_for_default_sink(self) -> None:
        sources = (
            "1 other-input.monitor module-x\n"
            "2 default-output.monitor module-y\n"
        )
        self.assertEqual(
            resolve_monitor_source("default-output", sources),
            "default-output.monitor",
        )

    def test_reports_missing_monitor_source(self) -> None:
        with self.assertRaises(AudioCaptureError):
            resolve_monitor_source("default-output", "1 microphone module-x\n")


if __name__ == "__main__":
    unittest.main()
