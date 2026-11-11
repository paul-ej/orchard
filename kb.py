"""Import local evidence, build a static site, or search reviewed articles."""
import argparse
from collections import defaultdict
from datetime import date
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
THREAD = re.compile(r"/thread/(\d+)/?")
CONTRIBUTION = re.compile(r"https://github\.com/paul-ej/orchard/issues/[1-9][0-9]*")
SOURCE_LABELS = {
    "community": "Community",
    "official": "Official",
    "contribution": "Orchard contribution",
}


def thread_url(value):
    parsed = urlsplit(value)
    match = THREAD.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "discussions.apple.com" or not match:
        raise ValueError("Expected an HTTPS Apple Community thread URL")
    return f"https://discussions.apple.com/thread/{match[1]}"


class StructuredData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.objects = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.active = dict(attrs).get("type") == "application/ld+json"

    def handle_data(self, data):
        if self.active:
            try:
                self.objects.append(json.loads(data))
            except json.JSONDecodeError:
                pass

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False


class PlainText(HTMLParser):
    def __init__(self, value):
        super().__init__()
        self.parts = []
        self.feed(value)

    def handle_data(self, data):
        self.parts.append(data)


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def plain(value):
    return " ".join(" ".join(PlainText(value).parts).split())


def extract(raw):
    parser = StructuredData()
    parser.feed(raw)
    question = next((item for item in walk(parser.objects)
                     if item.get("@type") == "Question"), None)
    if question is None:
        raise ValueError("No JSON-LD Question found. Use the JSONL adapter for this export.")
    answers = []
    for field in ("acceptedAnswer", "suggestedAnswer"):
        items = question.get(field, [])
        for item in items if isinstance(items, list) else [items]:
            if isinstance(item, dict) and item.get("text"):
                answers.append({"text": plain(item["text"]), "accepted": field == "acceptedAnswer"})
    return {"title": plain(question.get("name", "")), "question": plain(question.get("text", "")),
            "answers": answers}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def ingest(manifest):
    groups = defaultdict(dict)
    errors = []
    for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            url = thread_url(row["url"])
            captured = date.fromisoformat(row["captured_on"]).isoformat()
            topic = row["topic"].strip().casefold()
            if not topic:
                raise ValueError("Missing topic")
            if "html" in row:
                source = (manifest.parent / row["html"]).read_text(encoding="utf-8")
                row = extract(source)
            title = row["title"].strip()
            if not title:
                raise ValueError("Missing title")
            question = row.get("question", "").strip()
            answers = []
            for answer in row.get("answers", []):
                body = answer["text"].strip()
                if body:
                    answers.append({"text": body, "accepted": answer.get("accepted") is True})
            snapshot = {"title": title, "question": question, "answers": answers}
            digest = sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
            thread = groups[topic].setdefault(url, {"url": url, "snapshots": {}})
            thread["snapshots"].setdefault(digest, {"captured_on": [], **snapshot})
            dates = thread["snapshots"][digest]["captured_on"]
            if captured not in dates:
                dates.append(captured)
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
            errors.append({"line": number, "error": type(error).__name__,
                           "detail": str(error)})
    topics = {topic: list(threads.values()) for topic, threads in sorted(groups.items())}
    write_json(ROOT / ".private" / "review.json", {"topics": topics, "errors": errors})
    print(f"{len(topics)} topics, {len(errors)} rejected rows. See .private/review.json.")
    return int(bool(errors))


def public_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"Invalid HTTPS source URL: {value}")
    return value


def reviewed():
    articles = []
    for path in sorted((ROOT / "articles").glob("*.json")):
        article = json.loads(path.read_text(encoding="utf-8"))
        if article["status"] != "reviewed":
            continue
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", article["id"]) or article["id"] != path.stem:
            raise ValueError(f"Invalid article ID in {path.name}")
        date.fromisoformat(article["reviewed_on"])
        for key in ("title", "product", "applies_to", "summary", "evidence_notes"):
            if not isinstance(article[key], str) or not article[key].strip():
                raise ValueError(f"Missing {key} in {path.name}")
        for key in ("keywords", "steps"):
            if not isinstance(article[key], list) or not article[key] or any(
                not isinstance(value, str) or not value.strip() for value in article[key]
            ):
                raise ValueError(f"Invalid {key} in {path.name}")
        kinds = {source["kind"] for source in article["sources"]}
        if "community" not in kinds or not kinds.intersection({"official", "contribution"}):
            raise ValueError(f"An original thread and official or contribution evidence are required in {path.name}")
        for source in article["sources"]:
            public_url(source["url"])
            if source["kind"] == "community":
                thread_url(source["url"])
            if source["kind"] == "contribution" and not CONTRIBUTION.fullmatch(source["url"]):
                raise ValueError(f"Expected an Orchard contribution issue URL in {path.name}")
            if source.get("archive_url"):
                public_url(source["archive_url"])
        public_fields = ("id", "status", "title", "product", "applies_to", "summary",
                         "keywords", "steps", "evidence_notes", "reviewed_on")
        public = {key: article[key] for key in public_fields}
        public["sources"] = [
            {key: source[key] for key in ("kind", "label", "url", "archive_url") if key in source}
            for source in article["sources"]
        ]
        articles.append(public)
    return articles


def search_text(article):
    return " ".join([article["title"], article["product"], article["summary"],
                     article["applies_to"], *article["keywords"], *article["steps"]]).casefold()


def page(body):
    return """<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Orchard · Apple troubleshooting</title><link rel="stylesheet" href="style.css">
<script src="search.js" defer></script></head>
<body>
<nav class="site-nav" aria-label="Site"><div class="site-nav-inner">
<a class="brand" href="./">Orchard</a>
<div class="nav-links"><a href="#browse">Browse</a><a href="contribute.html">Answer a thread</a></div>
</div></nav>
<main><header class="hero">
<h1>Apple troubleshooting</h1>
<p>Find reviewed answers from Apple Discussions.</p>
<div class="search-panel">
<label class="visually-hidden" for="query">Search by symptom, product or error</label>
<input id="query" type="search" placeholder="Search by symptom, product or error" autocomplete="off">
</div></header>
<section id="browse" aria-labelledby="browse-title">
<div class="section-heading"><h2 id="browse-title">Browse guides</h2><p id="count" role="status"></p></div>
""" + body + """</section></main>
<footer><div class="footer-inner">Orchard is an independent project. <a href="https://discussions.apple.com/">Original Apple Discussions</a></div></footer>
</body></html>
"""


def card(article):
    links = []
    for source in article["sources"]:
        link = f'<a href="{escape(source["url"])}">{escape(source["label"])}</a>'
        if source.get("archive_url"):
            link += f' · <a href="{escape(source["archive_url"])}">Archived copy</a>'
        label = SOURCE_LABELS.get(source["kind"], source["kind"].capitalize())
        links.append(f'<li>{escape(label)} · {link}</li>')
    steps = "".join(f"<li>{escape(step)}</li>" for step in article["steps"])
    return f"""<article id="{article['id']}" data-search="{escape(search_text(article))}">
<p class="meta">{escape(article['product'])} · Reviewed {escape(article['reviewed_on'])}</p>
<h3><a href="#{article['id']}">{escape(article['title'])}</a></h3>
<p>{escape(article['summary'])}</p>
<details><summary>Read troubleshooting steps and sources</summary>
<p class="scope"><strong>Applies to</strong> {escape(article['applies_to'])}</p>
<ol>{steps}</ol>
<p class="scope">{escape(article['evidence_notes'])}</p>
<ul class="sources">{''.join(links)}</ul></details></article>"""


def build(articles):
    site = ROOT / "docs"
    (site / "index.html").write_text(page("\n".join(map(card, articles))), encoding="utf-8")
    write_json(site / "articles.json", {"schema_version": 1, "articles": articles})
    print(f"Built {len(articles)} articles in docs/")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build")
    search = commands.add_parser("search")
    search.add_argument("query")
    importer = commands.add_parser("ingest")
    importer.add_argument("manifest", type=Path)
    args = parser.parse_args()
    if args.command == "ingest":
        return ingest(args.manifest)
    articles = reviewed()
    if args.command == "build":
        build(articles)
    else:
        terms = args.query.casefold().split()
        matches = [article for article in articles if all(term in search_text(article) for term in terms)]
        print(json.dumps(matches, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
