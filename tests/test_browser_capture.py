import argparse
from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.robotparser import RobotFileParser

import collect_threads as collector

try:
    from browser_capture import BrowserCapture
except ModuleNotFoundError as error:
    if error.name != "playwright":
        raise
    BrowserCapture = None


@unittest.skipIf(BrowserCapture is None, "Install requirements-browser.txt to test browser mode")
class BrowserCaptureTests(unittest.TestCase):
    def setUp(self):
        self.url = collector.ORIGIN + "/thread/123"
        self.playwright = Mock()
        robots = RobotFileParser()
        robots.parse(["User-agent: *", "Disallow: /blocked"])
        self.browser = BrowserCapture(self.playwright, robots, "test@example.com", 0,
                                      collector.HOST, collector.BOT, collector.MAX_BYTES)
        self.browser.target = self.url

    def test_scope_and_robots_are_checked_before_requesting(self):
        attempts = [
            ("https://example.com/script.js", "GET", "script"),
            (collector.ORIGIN + "/blocked", "GET", "fetch"),
            (collector.ORIGIN + "/thread/124", "GET", "document"),
            (collector.ORIGIN + "/reply", "POST", "xhr"),
        ]
        for url, method, resource in attempts:
            with self.subTest(url=url, method=method, resource=resource):
                route = Mock()
                route.request.url = url
                route.request.method = method
                route.request.resource_type = resource
                self.browser.route(route)
                route.abort.assert_called_once()
                route.fetch.assert_not_called()

    def test_rendered_html_uses_existing_snapshot_and_pagination(self):
        page = self.browser.context.new_page.return_value
        page.content.return_value = """
        <h1 id="content-post-title">Fixture thread</h1>
        <div class="content-post question"><div class="content-post-body-content">Test question</div></div>
        <div class="content-post answer"><div class="content-post-body-content">Rendered reply</div></div>
        <a href="/thread/123?page=2">Next page</a>
        """
        with TemporaryDirectory() as name:
            directory = Path(name)
            database = collector.connect(directory)
            try:
                with database:
                    collector.queue(database, [self.url])
                    collector.capture(database, directory, self.url, self.browser.fetch(self.url))
                record, = database.execute("SELECT record FROM pages WHERE url=?", (self.url,)).fetchone()
                self.assertEqual(json.loads(record)["answers"][0]["text"], "Rendered reply")
                state, = database.execute("SELECT state FROM pages WHERE url=?", (self.url + "?page=2",)).fetchone()
                self.assertEqual(state, "queued")
            finally:
                database.close()
        page.close.assert_called_once()

    def test_rate_limit_from_reply_request_is_checkpointed(self):
        route = Mock()
        route.request.url = collector.ORIGIN + "/replies"
        route.request.method = "GET"
        route.request.resource_type = "xhr"
        response = route.fetch.return_value
        response.status = 429
        response.status_text = "Too Many Requests"
        response.headers = {"retry-after": "120"}
        self.browser.route(route)
        route.fetch.assert_called_once_with(max_redirects=0, timeout=25_000)
        route.abort.assert_called_once()
        self.assertIsInstance(self.browser.failure, HTTPError)

        @contextmanager
        def page_fetcher(*args):
            def rate_limited(url):
                raise self.browser.failure
            yield rate_limited

        with TemporaryDirectory() as name:
            directory = Path(name)
            args = argparse.Namespace(command="crawl", directory=directory, contact="test@example.com",
                                      delay=5, max_pages=10, seed=[self.url], browser=True, refresh=False)
            with patch.object(collector, "fetch", return_value=b"User-agent: *\n"), \
                    patch.object(collector, "page_fetcher", page_fetcher):
                self.assertEqual(collector.run(args), 2)
            database = collector.connect(directory)
            try:
                self.assertEqual(database.execute("SELECT state FROM pages").fetchone(), ("queued",))
                retry_at, = database.execute("SELECT value FROM settings WHERE name='retry_at'").fetchone()
                self.assertGreater(retry_at, collector.time.time() + 100)
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
