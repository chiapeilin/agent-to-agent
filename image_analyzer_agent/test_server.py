import unittest

from a2a.helpers import new_raw_part, new_text_part, new_url_part

from image_analyzer_agent.server import (
    image_urls_from_text,
    message_parts_to_openai_content,
)


class MessagePartsToOpenAIContentTests(unittest.TestCase):
    def test_extracts_http_urls_from_plain_text(self) -> None:
        self.assertEqual(
            image_urls_from_text(
                ["Compare http://127.0.0.1:8080/a.png and https://example.com/b.jpg."]
            ),
            ["http://127.0.0.1:8080/a.png", "https://example.com/b.jpg."],
        )

    def test_url_and_raw_image_parts_are_preserved(self) -> None:
        text_parts, image_parts = message_parts_to_openai_content(
            [
                new_text_part(text="Describe both images."),
                new_url_part(
                    url="https://example.com/cat.png", media_type="image/png"
                ),
                new_raw_part(raw=b"raw-image", media_type="image/jpeg"),
            ]
        )

        self.assertEqual(text_parts, ["Describe both images."])
        self.assertEqual(
            image_parts[0],
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
        )
        self.assertEqual(
            image_parts[1],
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64,cmF3LWltYWdl"},
            },
        )
