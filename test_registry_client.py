import unittest

from registry_client import extract_content_url, guess_media_type


class ContentUrlArgumentTests(unittest.TestCase):
    def test_extracts_content_url_without_including_it_in_query(self) -> None:
        argv, content_url = extract_content_url(
            ["describe", "this", "--url", "https://example.com/cat.png"]
        )

        self.assertEqual(argv, ["describe", "this"])
        self.assertEqual(content_url, "https://example.com/cat.png")
        self.assertEqual(guess_media_type(content_url), "image/png")

    def test_rejects_non_http_content_url(self) -> None:
        with self.assertRaises(ValueError):
            extract_content_url(["describe", "--url=file:///tmp/cat.png"])
