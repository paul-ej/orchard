# Orchard

An independent Apple troubleshooting knowledge base for GitHub Pages. The repository ships with five reviewed guides and a local importer. The site and review importer run without a backend or API key. The optional collection tool has a separate dependency.

Apple announced that the Apple Support Community would close on 20 November 2026. Apple’s website terms restrict scraping and mirroring. Use local inputs from an authorised export, a licensed collection, or your own original notes. Before relying on an archive, verify its coverage and reuse terms. Keep original posts, handles, screenshots and private inputs outside the public repository. Publish original troubleshooting guides with source links.

- [Apple's closure announcement](https://discussions.apple.com/welcome)
- [Apple Website Terms of Use](https://www.apple.com/legal/internet-services/terms/site.html)

## Preview and publish

Run from the repository root with Python 3.10 or later.

```sh
python3 kb.py build
python3 -m http.server 8000 --bind 127.0.0.1 --directory docs
```

Open http://127.0.0.1:8000. Push the repository to a public GitHub repository. Under **Settings → Pages → Build and deployment**, select **Deploy from a branch**, then **main → /docs**. The generated files in `docs/` are committed with the article changes. A free public GitHub Pages site has a published size limit of 1 GB. This initial build uses a single HTML page and JSON index. Split the collection by product and paginate the index as it grows.

- [Configure GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)
- [GitHub Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)

## Import, review and publish

1. Put the complete import manifest and any local exports in `.private/` or outside the checkout. The importer does not make HTTP requests. Use only a manifest you trust because HTML paths refer to local files.
2. Run `python3 kb.py ingest .private/threads.jsonl`. The importer replaces `.private/review.json` with a review queue grouped by supplied topic and canonical thread URL. Exact duplicate snapshots are combined, distinct versions are retained, and malformed rows are reported by line. A nonzero exit means at least one row was rejected.
3. Review the queue and write one original guide per issue in `articles/<id>.json`, using the supplied guides as templates. Record device and OS scope, conflicting evidence and limitations. Check the steps against relevant Apple Support documentation. Keep unreviewed entries as drafts. Set status to `reviewed` only after checking the evidence and removing personal data and copied post text.
4. Run `python3 kb.py build` and commit the article and regenerated `docs/`. Search via `python3 kb.py search 'display HDMI'` or the browser.

### Manifest format

Each JSONL line is one snapshot. Use the canonical Apple discussion thread URL, the date of capture and a consistent editorial topic slug. For normalised text, include `title`, optional `question`, and optional `answers`, a list of objects with `text` and optional boolean `accepted`. The synthetic fixture in `examples/threads.jsonl` is runnable.

For a local HTML export, replace those text fields with `html`, a path relative to the manifest. The HTML adapter reads schema.org Question, acceptedAnswer and suggestedAnswer JSON-LD when present. For other formats or missing replies, normalise the authorised input to JSONL. The adapter was exercised with synthetic HTML. Real export formats and archive coverage can vary.

### Coverage

Start by tracking selected threads by product, symptom and OS version. Prioritise issues that recur across threads, then distinguish confirmed fixes, historical workarounds and unresolved reports. Add original guides incrementally and link archive snapshots only when the exact snapshot and its accessibility have been checked. The starter does not infer completeness from the number of imported records and does not generate public advice automatically.

## Public contributions

Readers can [submit an answer to an unresolved thread](https://paul-ej.dev/orchard/contribute.html). The GitHub issue form records the original thread URL, device and OS, proposed fix, and supporting evidence. See `AGENTS.md` for review and publication.

## Collect Apple Discussions threads

The optional collector runs on macOS or Linux. It can parse a saved thread HTML page without network access. Live collection requires permission from Apple and follows only in-scope public links on `discussions.apple.com`.

```sh
python3 -m venv .private/collector-env
source .private/collector-env/bin/activate
python3 -m pip install -r requirements-collector.txt

# Parse one saved page, keeping the full original HTML separately.
python3 collect_threads.py parse /path/to/saved-thread.html \
  --url https://discussions.apple.com/thread/253164152 \
  --captured-on 2023-09-06 > .private/saved-thread.jsonl

# Run only once Apple has authorised collection. Use your real contact address.
python3 collect_threads.py crawl --apple-permission --contact you@example.com
```

Use the same arguments with `add-saved` to store an existing capture and index its outbound links. Live collection starts from Apple’s product sitemap. Repeat `--seed URL` to add known category, thread or listing URLs. The crawler follows rendered links, including linked reply pages. It keeps one worker per index and uses the greater of its configured delay and applicable robots rules. HTTP 429 or 503 checkpoints the queue and stops the run. A rerun honours the saved cooldown. For a longer authorised run, set `--max-pages` to the desired request budget.

```sh
python3 collect_threads.py export
python3 kb.py ingest .private/capture/threads.jsonl
```

`.private/capture/index.sqlite3` records each discovered URL, capture status, HTML snapshot path and parsed record. The export prints counts by status and deduplicates the reported thread count by canonical thread URL. Its JSONL records retain the page URL separately so reply pages can be audited. Each captured HTML file is stored by content hash. Parse failures keep their original HTML for review, and the normal Orchard importer still quarantines unsafe text. Keep all capture output private.

The index measures linked pages discovered by this collector. It cannot establish the site's total thread count. The HTTP mode does not enumerate thread IDs, capture attachments, or render JavaScript replies. Verify coverage against an authoritative manifest before treating it as complete.

### Capture rendered threads in Chromium

Use this mode when replies are loaded by JavaScript. It uses the same queue, limits, and snapshot index as the HTTP collector. The visible browser uses a fresh session, follows only the queued page for navigation, and fetches permitted same-origin scripts and reply data. It does not sign in. A rate-limit response pauses the run even when it comes from a page resource.

```sh
python3 -m pip install -r requirements-browser.txt
python3 -m playwright install chromium

# Run only once Apple has authorised collection.
python3 collect_threads.py crawl --browser --apple-permission \
  --contact you@example.com \
  --seed https://discussions.apple.com/thread/253164152
```

Existing queued pages are processed before newly added seeds. Add `--refresh` with an explicit `--seed` to capture a page already in the index again. Use `--directory .private/browser-pilot` before `crawl` to try a separate index. The collector saves the rendered HTML, including replies present in the page at capture time. Linked reply pages are queued separately. Attachments and replies behind unvisited interface actions are outside this capture.
