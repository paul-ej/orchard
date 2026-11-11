"""Collect permitted Apple Discussions pages into Orchard's private import format."""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from lxml import html

from kb import ROOT, THREAD, thread_url

HOST = "discussions.apple.com"
ORIGIN = f"https://{HOST}"
SITEMAP = f"{ORIGIN}/productsitemap.jspa"
ROBOTS = f"{ORIGIN}/robots.txt"
BOT = "OrchardPreservation"
MAX_BYTES = 8 * 1024 * 1024
CLASS = 'contains(concat(" ", normalize-space(@class), " "), " {} ")'


def crawl_url(base, href):
    try:
        parsed = urlsplit(urljoin(base, href))
        if parsed.scheme != "https" or parsed.netloc != HOST:
            return None
        query = parse_qs(parsed.query, max_num_fields=30)
    except ValueError:
        return None
    path = parsed.path.rstrip("/")
    if not THREAD.fullmatch(path) and not re.fullmatch(
        r"/(?:community(?:/[A-Za-z0-9_-]+)+|browse|productsitemap\.jspa|welcome)", path
    ):
        return None
    page = query.get("page", ["1"])[0]
    if not re.fullmatch(r"[1-9][0-9]{0,8}", page):
        return None
    return ORIGIN + path + ("?" + urlencode({"page": page}) if page != "1" else "")


def document(raw):
    return html.fromstring(raw, parser=html.HTMLParser(encoding="utf-8", no_network=True))


def post_text(element):
    parts = element.xpath('.//text()[not(ancestor::script or ancestor::style)]')
    return " ".join(" ".join(parts).split())


def thread_record(page, url, captured_on):
    titles = page.xpath('//*[@id="content-post-title"]')
    posts = page.xpath(f'//*[{CLASS.format("content-post")}]')
    questions = [post for post in posts if "question" in post.classes]
    if not titles or not questions:
        raise ValueError("Thread question markup missing")
    bodies = questions[0].xpath(f'.//*[{CLASS.format("content-post-body-content")}]')
    if not bodies or not post_text(titles[0]):
        raise ValueError("Thread body or title missing")
    replies = {}
    for post in posts:
        if "question" in post.classes:
            continue
        for reply in post.xpath(f'.//*[{CLASS.format("content-post-body-content")}]'):
            text = post_text(reply)
            if not text:
                continue
            key = post.get("data-analytics-id") or sha256(text.encode()).hexdigest()
            accepted = bool(post.xpath(f'.//*[{CLASS.format("post-solved")}]'))
            previous = replies.get(key)
            replies[key] = {"text": text, "accepted": accepted or bool(previous and previous["accepted"])}
    return {
        "url": thread_url(url), "page_url": url, "captured_on": captured_on,
        "topic": "apple-discussions", "title": post_text(titles[0]), "question": post_text(bodies[0]),
        "answers": list(replies.values()),
    }


def fetch(opener, url, contact, robots=None):
    if robots is not None and not robots.can_fetch(BOT, url):
        raise PermissionError("robots.txt disallows this URL")
    request = Request(url, headers={"User-Agent": f"{BOT}/1.0 ({contact})"})
    # A redirect could target another path with different robots rules.
    # Live collection records redirects for review rather than following them.
    with opener.open(request, timeout=25) as response:
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("Response exceeds capture size limit")
        return body


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def connect(directory):
    directory.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(directory / "index.sqlite3")
    database.execute("""CREATE TABLE IF NOT EXISTS pages (
        url TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'queued',
        snapshot TEXT, record TEXT, error TEXT
    )""")
    database.execute("CREATE INDEX IF NOT EXISTS pages_by_state ON pages(state)")
    database.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value REAL)")
    return database


def queue(database, urls):
    database.executemany("INSERT OR IGNORE INTO pages(url) VALUES (?)", ((url,) for url in urls if url))


def retry_at(value):
    if value:
        try:
            seconds = int(value)
        except ValueError:
            try:
                deadline = parsedate_to_datetime(value)
                return max(time.time() + 60, deadline.timestamp())
            except (TypeError, ValueError, OverflowError):
                pass
        else:
            return time.time() + max(60, seconds)
    return time.time() + 15 * 60


def status(database):
    for state, count in database.execute("SELECT state, count(*) FROM pages GROUP BY state ORDER BY state"):
        print(f"{state}: {count}")
    threads = database.execute(
        "SELECT count(DISTINCT CASE WHEN instr(url, '?') > 0 THEN substr(url, 1, instr(url, '?')-1) "
        "ELSE url END) FROM pages WHERE record IS NOT NULL"
    ).fetchone()[0]
    print(f"Distinct parsed threads: {threads}")


def export(database, directory):
    destination = directory / "threads.jsonl"
    temporary = destination.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        for record, in database.execute("SELECT record FROM pages WHERE record IS NOT NULL ORDER BY url"):
            output.write(record + "\n")
    temporary.replace(destination)
    status(database)
    print(f"Manifest: {destination}")


def capture(database, directory, url, raw, captured_on=None):
    snapshot = Path("snapshots", sha256(raw).hexdigest() + ".html")
    destination = directory / snapshot
    destination.parent.mkdir(exist_ok=True)
    if not destination.exists():
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(raw)
        temporary.replace(destination)
    try:
        page = document(raw)
        queue(database, (crawl_url(url, href) for href in page.xpath("//a/@href | //link[@rel='next']/@href")))
        record = None
        if THREAD.fullmatch(urlsplit(url).path):
            captured_on = captured_on or datetime.now(timezone.utc).date().isoformat()
            record = thread_record(page, url, captured_on)
            record["snapshot"] = str(snapshot)
        database.execute(
            "UPDATE pages SET state='captured', snapshot=?, record=?, error=NULL WHERE url=?",
            (str(snapshot), json.dumps(record, ensure_ascii=False) if record else None, url),
        )
    except (ValueError, html.etree.ParserError):
        database.execute(
            "UPDATE pages SET state='parse_failed', snapshot=?, error='Unexpected markup' WHERE url=?",
            (str(snapshot), url),
        )


@contextmanager
def page_fetcher(args, opener, robots, delay):
    if not args.browser:
        def read_http(url):
            time.sleep(delay)
            return fetch(opener, url, args.contact, robots)
        yield read_http
        return

    try:
        from browser_capture import BrowserCapture, Error, sync_playwright
    except ImportError as error:
        raise URLError("Install requirements-browser.txt to enable browser capture") from error
    with sync_playwright() as playwright:
        try:
            browser = BrowserCapture(playwright, robots, args.contact, delay, HOST, BOT, MAX_BYTES)
        except Error as error:
            raise URLError("Chromium could not start. Install it with python3 -m playwright install chromium") from error
        try:
            yield browser.fetch
        finally:
            browser.close()


def crawl(args, database, directory):
    cooldown = database.execute("SELECT value FROM settings WHERE name='retry_at'").fetchone()
    if cooldown and time.time() < cooldown[0]:
        print(f"Paused until {datetime.fromtimestamp(cooldown[0], timezone.utc).isoformat()}.")
        return 2

    opener = build_opener(NoRedirect())
    try:
        robots = RobotFileParser()
        robots.parse(fetch(opener, ROBOTS, args.contact).decode("utf-8").splitlines())
    except HTTPError as error:
        if error.code != 404:
            raise
        robots.parse([])
    delay = max(args.delay, robots.crawl_delay(BOT) or 0)
    rate = robots.request_rate(BOT)
    if rate and rate.requests:
        delay = max(delay, rate.seconds / rate.requests)

    with database:
        seeds = args.seed or [SITEMAP]
        for seed in seeds:
            normalized = crawl_url(ORIGIN, seed)
            if not normalized:
                raise ValueError("Expected an in-scope Apple Discussions seed")
            queue(database, [normalized])
            if args.refresh:
                database.execute("UPDATE pages SET state='queued', error=NULL WHERE url=?", (normalized,))

    with page_fetcher(args, opener, robots, delay) as read_page:
        return crawl_queue(database, directory, args.max_pages, read_page)


def crawl_queue(database, directory, max_pages, read_page):
    for _ in range(max_pages):
        row = database.execute(
            "SELECT url FROM pages WHERE state='queued' ORDER BY rowid LIMIT 1"
        ).fetchone()
        if row is None:
            break
        url = row[0]
        try:
            raw = read_page(url)
        except PermissionError:
            with database:
                database.execute("UPDATE pages SET state='blocked', error='robots.txt' WHERE url=?", (url,))
        except HTTPError as error:
            if error.code in (429, 503):
                raise
            if error.code not in (301, 302, 303, 307, 308, 404, 410):
                raise
            with database:
                database.execute("UPDATE pages SET state='unavailable', error=? WHERE url=?", (str(error.code), url))
        except ValueError:
            with database:
                database.execute("UPDATE pages SET state='unavailable', error='Response too large' WHERE url=?", (url,))
        else:
            with database:
                capture(database, directory, url, raw)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / ".private" / "capture")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("export", help="Write the JSONL manifest and show coverage counts")
    live = commands.add_parser("crawl", help="Follow public links with permission from Apple")
    live.add_argument("--apple-permission", action="store_true", required=True)
    live.add_argument("--contact", required=True, help="Contact email for the crawler User-Agent")
    live.add_argument("--seed", action="append")
    live.add_argument("--delay", type=float, default=5, help="Seconds between requests, minimum 2")
    live.add_argument("--max-pages", type=int, default=100)
    live.add_argument("--browser", action="store_true", help="Capture rendered HTML in a Chromium window")
    live.add_argument("--refresh", action="store_true", help="Queue explicit seeds again, including captured pages")
    for command, help_text in (
        ("parse", "Print one saved Apple thread page as JSONL"),
        ("add-saved", "Add a saved Apple thread page to the local capture index"),
    ):
        saved = commands.add_parser(command, help=help_text)
        saved.add_argument("html", type=Path)
        saved.add_argument("--url", required=True)
        saved.add_argument("--captured-on", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    if args.command in {"parse", "add-saved"}:
        args.url = crawl_url(ORIGIN, args.url)
        if not args.url or not THREAD.fullmatch(urlsplit(args.url).path):
            parser.error("Expected an Apple Discussions thread URL")
        if args.command == "parse":
            record = thread_record(document(args.html.read_bytes()), args.url, args.captured_on.isoformat())
            print(json.dumps(record, ensure_ascii=False))
            return 0
    if args.command == "crawl":
        if args.refresh and not args.seed:
            parser.error("--refresh requires at least one explicit --seed URL")
        if not (2 <= args.delay <= 3600 and args.max_pages > 0):
            parser.error("Use a delay between 2 and 3600 seconds and a positive page limit")
        if not re.fullmatch(r"[^\s()@]+@[^\s()@]+", args.contact) or not args.contact.isascii():
            parser.error("Provide an ASCII contact email address")

    args.directory.mkdir(parents=True, exist_ok=True)
    with (args.directory / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("A collector is already using this directory")
        return run(args)


def run(args):
    database = connect(args.directory)
    try:
        if args.command == "export":
            export(database, args.directory)
            return 0
        if args.command == "add-saved":
            with database:
                queue(database, [args.url])
                capture(database, args.directory, args.url, args.html.read_bytes(), args.captured_on.isoformat())
            export(database, args.directory)
            state, = database.execute("SELECT state FROM pages WHERE url=?", (args.url,)).fetchone()
            return 0 if state == "captured" else 1
        try:
            return crawl(args, database, args.directory)
        except HTTPError as error:
            if error.code in (429, 503):
                deadline = retry_at(next((value for key, value in error.headers.items()
                                          if key.lower() == "retry-after"), None))
                with database:
                    database.execute("INSERT OR REPLACE INTO settings VALUES ('retry_at', ?)", (deadline,))
                print(f"HTTP {error.code}. Paused until {datetime.fromtimestamp(deadline, timezone.utc).isoformat()}.",
                      file=sys.stderr)
            else:
                print(f"HTTP {error.code}. Queue retained.", file=sys.stderr)
            return 2
        except URLError as error:
            print(f"{error.reason}. Queue retained.", file=sys.stderr)
            return 2
        except (TimeoutError, KeyboardInterrupt):
            print("Collection interrupted. Queue retained.", file=sys.stderr)
            return 2
        finally:
            export(database, args.directory)
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
