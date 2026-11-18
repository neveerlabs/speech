import unittest

from api_keys import (
    ApiKeyFailoverError,
    parse_api_keys,
    run_with_api_key_fallback,
)
from audio_capture import AudioCaptureError


class ParseApiKeysTests(unittest.TestCase):
    def test_parses_comma_separated_keys_and_removes_duplicates(self) -> None:
        self.assertEqual(
            parse_api_keys(
                {"GEMINI_API_KEYS": " first-key, second-key, first-key , "}
            ),
            ("first-key", "second-key"),
        )

    def test_falls_back_to_single_legacy_key(self) -> None:
        self.assertEqual(
            parse_api_keys({"GEMINI_API_KEY": " legacy-key "}),
            ("legacy-key",),
        )

    def test_plural_setting_takes_precedence(self) -> None:
        self.assertEqual(
            parse_api_keys(
                {
                    "GEMINI_API_KEYS": "first-key,second-key",
                    "GEMINI_API_KEY": "legacy-key",
                }
            ),
            ("first-key", "second-key"),
        )


class ApiKeyFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_tries_next_key_after_api_failure(self) -> None:
        attempted: list[str] = []
        statuses: list[tuple[int, int]] = []

        async def attempt(api_key: str) -> str:
            attempted.append(api_key)
            if api_key == "first-key":
                raise RuntimeError("temporary API failure")
            return "connected"

        result = await run_with_api_key_fallback(
            ("first-key", "second-key"),
            attempt,
            lambda next_key, total: statuses.append((next_key, total)),
            lambda: False,
        )

        self.assertEqual(result, "connected")
        self.assertEqual(attempted, ["first-key", "second-key"])
        self.assertEqual(statuses, [(2, 2)])

    async def test_reports_all_failures_without_leaking_keys(self) -> None:
        async def attempt(api_key: str) -> None:
            raise RuntimeError(f"rejected {api_key}")

        with self.assertRaises(ApiKeyFailoverError) as raised:
            await run_with_api_key_fallback(
                ("secret-one", "secret-two"),
                attempt,
                lambda _next_key, _total: None,
                lambda: False,
            )

        message = str(raised.exception)
        self.assertIn("Semua 2 API key Gemini gagal", message)
        self.assertNotIn("secret-one", message)
        self.assertNotIn("secret-two", message)

    async def test_does_not_rotate_keys_for_audio_capture_errors(self) -> None:
        attempted: list[str] = []

        async def attempt(api_key: str) -> None:
            attempted.append(api_key)
            raise AudioCaptureError("monitor audio hilang")

        with self.assertRaises(AudioCaptureError):
            await run_with_api_key_fallback(
                ("first-key", "second-key"),
                attempt,
                lambda _next_key, _total: None,
                lambda: False,
            )

        self.assertEqual(attempted, ["first-key"])


if __name__ == "__main__":
    unittest.main()
