"""Render authorised Apple Discussions captures in an isolated Chromium window."""
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from playwright.sync_api import Error, sync_playwright

from kb import THREAD

REQUEST_TYPES = {"document", "script", "stylesheet", "xhr", "fetch"}
THREAD_READY = "#content-post-title"


class BrowserCapture:
    def __init__(self, playwright, robots, contact, delay, host, bot, max_bytes):
        self.robots = robots
        self.host = host
        self.bot = bot
        self.max_bytes = max_bytes
        self.delay = delay
        self.last_request = time.monotonic()  # The preceding robots request also counts.
        self.target = None
        self.failure = None
        self.browser = playwright.chromium.launch(headless=False, chromium_sandbox=True)
        try:
            self.context = self.browser.new_context(
                user_agent=f"{bot}/1.0 ({contact})",
                service_workers="block",
                accept_downloads=False,
            )
            self.context.route("**/*", self.route)
            self.context.route_web_socket("**/*", lambda socket: socket.close())
        except Exception:
            self.browser.close()
            raise

    def close(self):
        self.browser.close()

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        if (
            self.failure
            or url.scheme != "https"
            or url.netloc != self.host
            or request.method != "GET"
            or request.resource_type not in REQUEST_TYPES
            or (request.resource_type == "document" and request.url != self.target)
            or not self.robots.can_fetch(self.bot, request.url)
        ):
            route.abort()
            return

        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        response = None
        try:
            response = route.fetch(max_redirects=0, timeout=25_000)
            if response.status in (401, 403, 429, 503) or (
                request.resource_type == "document" and not 200 <= response.status < 300
            ):
                self.failure = HTTPError(request.url, response.status, response.status_text,
                                         response.headers, None)
                route.abort()
            elif 300 <= response.status < 400:
                route.abort()
            else:
                route.fulfill(response=response)
        except Error:
            self.failure = URLError("Browser request failed")
            route.abort()
        finally:
            if response is not None:
                response.dispose()

    def fetch(self, url):
        if not self.robots.can_fetch(self.bot, url):
            raise PermissionError("robots.txt disallows this URL")
        self.target = url
        self.failure = None
        page = self.context.new_page()
        try:
            timeout = max(45_000, int(self.delay * 3_000))
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if THREAD.fullmatch(urlsplit(url).path):
                page.locator(THREAD_READY).wait_for(state="attached", timeout=timeout)
                page.locator(".content-post.question .content-post-body-content").wait_for(
                    state="attached", timeout=timeout
                )
                page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                # Give replies triggered by the scroll time to render.
                page.wait_for_timeout(1500)
            raw = page.content().encode("utf-8")
        except Error as error:
            if self.failure:
                raise self.failure from error
            raise URLError("Browser could not render the requested page") from error
        finally:
            page.close()
        if self.failure:
            raise self.failure
        if len(raw) > self.max_bytes:
            raise ValueError("Response exceeds capture size limit")
        return raw
