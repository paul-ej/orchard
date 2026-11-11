import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import kb


class ContentSafetyTests(unittest.TestCase):
    def test_hidden_characters_and_role_markers(self):
        samples = (
            "screen\u200bshot",
            "reordered\u202e label",
            "text&#x200B;here",
            r"text\u200bhere",
            r"text\u{202E}here",
            "text%25E2%2580%258Bhere",
            "text\u034fhere",
            "text\ufe0fhere",
            "text\u2800here",
            "<|system|>",
            "&#60;|system|&#62;",
            "ＳＹＳＴＥＭ： instruction",
        )
        for text in samples:
            with self.subTest(text=repr(text)), self.assertRaises(kb.UnsafeContent):
                kb.check_text(text)

    def test_normal_unicode_is_preserved(self):
        text = "Écran externe – 日本語 – مرحبا – HDMI"
        self.assertEqual(kb.check_text(text), text)

    def test_source_allowlist_and_archive_identity(self):
        source = {
            "kind": "community",
            "label": "Display issue",
            "url": "https://discussions.apple.com/thread/123",
            "archive_url": "https://web.archive.org/web/20230906090618/https://discussions.apple.com/thread/123",
        }
        kb.validate_source(source)
        kb.validate_source({
            "kind": "official", "label": "Display support", "url": "https://support.apple.com/en-gb/102501"
        })
        kb.validate_source({
            "kind": "contribution", "label": "Tested fix", "url": "https://github.com/paul-ej/orchard/issues/42"
        })
        candidates = (
            {**source, "archive_url": source["archive_url"].replace("/123", "/124")},
            {**source, "url": "https://discussions.apple.com.example.org/thread/123"},
            {**source, "url": "https://discussions.apple.com/thread/１２３"},
            {**source, "url": "https://discussions.apple.com/thread/12%0A3"},
            {"kind": "official", "label": "Misleading", "url": "https://support.apple.com@example.org/"},
            {"kind": "contribution", "label": "Outside project", "url": "https://github.com/other/repo/issues/42"},
            {"kind": "unknown", "label": "Misleading", "url": "https://example.org/"},
        )
        for candidate in candidates:
            with self.subTest(source=candidate), self.assertRaises(ValueError):
                kb.validate_source(candidate)

    def test_quarantined_rows_keep_raw_content_out_of_queue(self):
        row = {
            "url": "https://discussions.apple.com/thread/123",
            "captured_on": "2026-11-11",
            "topic": "display",
            "title": "Blank display",
        }
        with TemporaryDirectory() as directory, patch.object(kb, "ROOT", Path(directory)):
            manifest = kb.ROOT / "source.jsonl"
            manifest.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in [
                {**row, "question": "unsafe-text\u2028payload"},
                row,
            ]), encoding="utf-8")
            self.assertEqual(kb.ingest(manifest), 1)
            output = (kb.ROOT / ".private" / "review.json").read_text(encoding="utf-8")
            self.assertNotIn("unsafe-text", output)
            self.assertIn("unsafe-text", manifest.read_text(encoding="utf-8"))
            report = json.loads(output)
            self.assertEqual(len(report["topics"]["display"]), 1)
            self.assertEqual(report["errors"][0]["line"], 1)

    def test_markup_is_not_active_in_published_guide(self):
        article = kb.reviewed()[0]
        article["summary"] = '<img src="x" onerror="bad()">'
        output = kb.card(article)
        self.assertNotIn("<img", output)
        self.assertIn("&lt;img", output)


if __name__ == "__main__":
    unittest.main()
