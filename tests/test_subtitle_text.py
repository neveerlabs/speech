import unittest

from subtitle_text import merge_transcript_text, recent_wrapped_lines


class MergeTranscriptTextTests(unittest.TestCase):
    def test_replaces_cumulative_stream_update(self) -> None:
        self.assertEqual(
            merge_transcript_text(
                "Halo, saya Stephanie.",
                "Halo, saya Stephanie. Selamat datang.",
            ),
            "Halo, saya Stephanie. Selamat datang.",
        )

    def test_replaces_cumulative_update_after_context_was_trimmed(self) -> None:
        current = "kata sebelumnya yang masih ada di potongan"
        update = "transkrip lama " + current + " lalu konteks terbaru"
        self.assertEqual(
            merge_transcript_text(current, update),
            update,
        )

    def test_ignores_duplicate_and_stale_updates(self) -> None:
        current = "Halo, saya Stephanie. Selamat datang."
        self.assertEqual(merge_transcript_text(current, current), current)
        self.assertEqual(
            merge_transcript_text(current, "Halo, saya Stephanie."),
            current,
        )

    def test_ignores_duplicate_fragment_from_middle_of_transcript(self) -> None:
        current = "Halo, saya Stephanie. Selamat datang."
        self.assertEqual(
            merge_transcript_text(current, "saya Stephanie."),
            current,
        )

    def test_merges_overlapping_chunks_without_repeating_words(self) -> None:
        self.assertEqual(
            merge_transcript_text("Halo, saya Stephanie.", "saya Stephanie. Apa kabar?"),
            "Halo, saya Stephanie. Apa kabar?",
        )

    def test_appends_non_overlapping_translation_chunk(self) -> None:
        self.assertEqual(
            merge_transcript_text("Selamat datang.", "Senang bertemu dengan kalian."),
            "Selamat datang. Senang bertemu dengan kalian.",
        )

    def test_preserves_repeated_words_within_an_update(self) -> None:
        self.assertEqual(
            merge_transcript_text("Itu bagus.", "Tidak, tidak apa-apa."),
            "Itu bagus. Tidak, tidak apa-apa.",
        )


class RecentWrappedLinesTests(unittest.TestCase):
    def test_keeps_only_the_recent_lines_that_fit(self) -> None:
        result = recent_wrapped_lines(
            "satu dua tiga empat lima enam",
            lambda value: len(value),
            maximum_width=10,
            maximum_lines=2,
        )
        self.assertEqual(result, "tiga empat\nlima enam")

    def test_empty_text_returns_empty_output(self) -> None:
        self.assertEqual(recent_wrapped_lines("", len, 10), "")


if __name__ == "__main__":
    unittest.main()
