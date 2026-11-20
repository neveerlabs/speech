from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from text_to_speech import SoundOfTextWorker


class _JsonResponse:
    def __init__(self, value: dict[str, object]) -> None:
        self._data = json.dumps(value).encode("utf-8")

    def __enter__(self) -> _JsonResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read(self) -> bytes:
        return self._data


class SoundOfTextWorkerTests(unittest.TestCase):
    def test_requests_audio_and_polls_until_location_is_available(self) -> None:
        worker = SoundOfTextWorker("Halo dunia")
        responses = [
            _JsonResponse({"id": "sound-123", "status": "pending"}),
            _JsonResponse(
                {
                    "id": "sound-123",
                    "status": "done",
                    "location": "https://cdn.example.test/audio.mp3",
                }
            ),
        ]

        with (
            patch("text_to_speech.urlopen", side_effect=responses) as urlopen,
            patch.object(worker, "_wait_before_poll") as wait_before_poll,
        ):
            audio_url = worker._request_audio_url()

        self.assertEqual(audio_url, "https://cdn.example.test/audio.mp3")
        self.assertEqual(urlopen.call_count, 2)
        wait_before_poll.assert_called_once_with()
        post_request = urlopen.call_args_list[0].args[0]
        self.assertEqual(post_request.get_method(), "POST")
        self.assertEqual(
            json.loads(post_request.data.decode("utf-8")),
            {
                "engine": "Google",
                "data": {"text": "Halo dunia", "voice": "id-ID"},
            },
        )
        self.assertEqual(
            urlopen.call_args_list[1].args[0].get_method(),
            "GET",
        )

    def test_rejects_non_https_audio_url(self) -> None:
        worker = SoundOfTextWorker("Halo")
        with patch(
            "text_to_speech.urlopen",
            return_value=_JsonResponse(
                {"location": "http://cdn.example.test/audio.mp3"}
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "tidak aman"):
                worker._request_audio_url()


if __name__ == "__main__":
    unittest.main()
