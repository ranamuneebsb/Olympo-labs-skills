"""
ZenRows Website Scraper - 2-phase scraping with auto-retry.
Phase 1 runs at user concurrency (default 30, hard cap 40);
Phase 3 retries Phase 1 failures with JS render at concurrency 25.
Phase 2 was removed because identical-settings retries never
recovered additional rows.

See SKILL.md for the full spec and operational rules
(15-minute restart cooldown, source-of-truth cache, etc).
"""
import argparse
import csv
import os
import signal
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

csv.field_size_limit(sys.maxsize)


SETUP_INSTRUCTIONS = """\
ZENROWS_API_KEY is not set in the environment.

ONE TIME SETUP:
  1. Open a terminal and run:
       setx ZENROWS_API_KEY "your_zenrows_key_here"
  2. Close and reopen the terminal so the new env var is visible.
  3. Re-run this command.
"""


WEBSITE_KEYS = ["website", "domain", "url", "site", "web", "homepage"]
COMPANY_KEYS = ["company", "name", "firm", "organization", "org", "business"]

ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"

MAX_CONCURRENCY = 40
COOLDOWN_SECONDS = 15 * 60
LAST_RUN_FILENAME = ".zenrows_last_run"


# Graceful-shutdown state. Workers check _stop_requested between requests;
# SIGINT/SIGTERM set the flag so we can drain in-flight calls and close
# Sessions before the process exits (prevents the 10-15 min ghost-connection
# window where ZenRows still counts our slots as active).
_stop_requested = threading.Event()
_open_sessions = []
_sessions_lock = threading.Lock()


def _register_session(session):
    with _sessions_lock:
        _open_sessions.append(session)


def _close_all_sessions():
    with _sessions_lock:
        for s in _open_sessions:
            try:
                s.close()
            except Exception:
                pass
        _open_sessions.clear()


def _install_signal_handlers():
    def handler(signum, _frame):
        if not _stop_requested.is_set():
            print(
                f"\n[signal {signum}] stop requested — draining "
                f"in-flight requests (up to 10s), then closing sessions...",
                flush=True,
            )
            _stop_requested.set()

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, handler)
        except (ValueError, OSError):
            pass


def detect_columns(headers):
    lower = {h.lower(): h for h in headers}

    def find(keys):
        for h_low, h_orig in lower.items():
            for k in keys:
                if k in h_low:
                    return h_orig
        return None

    return find(WEBSITE_KEYS), find(COMPANY_KEYS)


def normalize_url(url):
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if url.startswith("http://"):
        url = "https://" + url[7:]
    url = url.rstrip("/")
    parsed = urlparse(url)
    netloc = parsed.netloc
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{netloc}{path}"


# ZenRows wraps Cloudflare blocks in application/problem+json with these
# substrings in the body. Headers from the underlying response are stripped
# by ZenRows before relaying, so cf-ray / server-cloudflare detection on the
# *response* headers doesn't fire; we have to inspect the body instead.
_CF_BODY_MARKERS = (
    "RESP004",
    "cloudflare",
    "Attention Required",
    "Just a moment",
    "challenge-platform",
)


def is_cloudflare_blocked(response):
    """Detect Cloudflare-blocked responses.

    Permanent verdict — cached as `cloudflare_blocked` so we never re-fetch.
    Detection order (cheapest first):
      1. HTTP 403 from ZenRows.
      2. ZenRows error envelope (application/problem+json) whose body
         carries a known CF marker (RESP004, "cloudflare", "Attention
         Required", "Just a moment", "challenge-platform"). ZenRows
         strips CF response headers before relaying, so the body is the
         only reliable signal.
      3. 200 with empty body (CF served a JS-challenge HTML shell that
         autoparse stripped to whitespace).
    """
    if response.status_code == 403:
        return True
    content_type = response.headers.get("Content-Type", "")
    body = response.text or ""
    if "application/problem+json" in content_type:
        for marker in _CF_BODY_MARKERS:
            if marker in body:
                return True
    if response.status_code == 200 and not body.strip():
        return True
    return False


_DEAD_STATUS_CODES = {400, 404, 410, 413, 422}


def is_dead_domain(response):
    """Detect ZenRows permanent fetch failures.

    ZenRows wraps these in `application/problem+json` with a RESP00x code
    (RESP001 dead/parked, RESP002/RESP007 URL not found, RESP005 response
    too large, etc). All status codes here are class-permanent for this
    URL — caching as `dead_domain` stops every future restart from
    re-attempting them.

    Excludes 402 (account quota), 429 (rate limit), 5xx (server errors),
    which are all transient and should be retried.
    """
    if response.status_code not in _DEAD_STATUS_CODES:
        return False
    if "application/problem+json" not in response.headers.get("Content-Type", ""):
        return False
    return True


def scrape_one(url, api_key, phase_settings, session_local, timeout=30):
    """Returns (status, content, error_or_None).

    status is one of: "ok", "cloudflare_blocked", "dead_domain", "failed",
    "stopped". `cloudflare_blocked` and `dead_domain` are treated as
    permanent verdicts for the URL — persisted to the cache so future runs
    skip them. "stopped" means a shutdown signal arrived mid-request and
    the row should be left un-cached (will be retried next run).

    phase_settings: dict with keys js_render (bool) and premium (bool).
    session_local: threading.local() shared by the worker pool. Each
    thread lazily creates its own requests.Session() the first time it
    runs, so concurrent workers do not share connection state.
    """
    if _stop_requested.is_set():
        return "stopped", "", "shutdown requested"

    if not hasattr(session_local, "session"):
        session_local.session = requests.Session()
        _register_session(session_local.session)
    session = session_local.session

    js_render = phase_settings.get("js_render", False)
    premium = phase_settings.get("premium", False)

    params = {
        "url": url,
        "apikey": api_key,
        "antibot": "true",
        "autoparse": "true",
    }
    if js_render:
        params["js_render"] = "true"
    if premium:
        params["premium_proxy"] = "true"

    backoffs = [5, 10, 20]
    last_err = None
    tried_no_verify = False

    for attempt in range(3):
        if _stop_requested.is_set():
            return "stopped", "", "shutdown requested"
        try:
            r = session.get(ZENROWS_ENDPOINT, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < len(backoffs):
                    time.sleep(backoffs[attempt])
                    continue
                return "failed", "", f"429 after {attempt + 1} attempts"
            if is_cloudflare_blocked(r):
                return "cloudflare_blocked", "", f"cf-blocked (HTTP {r.status_code})"
            if is_dead_domain(r):
                return "dead_domain", "", "RESP00x dead/unfetchable"
            if r.status_code != 200:
                return "failed", "", f"HTTP {r.status_code}"
            content = r.text or ""
            if not content.strip():
                return "failed", "", "empty response"
            return "ok", content, None
        except requests.exceptions.SSLError as e:
            if not tried_no_verify:
                tried_no_verify = True
                try:
                    r = session.get(
                        ZENROWS_ENDPOINT,
                        params=params,
                        timeout=timeout,
                        verify=False,
                    )
                    if r.status_code == 200 and r.text and r.text.strip():
                        return "ok", r.text, None
                except Exception as e2:
                    last_err = f"ssl-retry: {e2}"
                    break
            last_err = str(e)
            break
        except requests.exceptions.Timeout:
            last_err = "timeout"
            break
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            break
    return "failed", "", last_err or "unknown error"


class SQLiteCache:
    """Dict-like cache backed by SQLite. Loads URL set into memory once for
    O(1) `in` checks; fetches full rows on demand via SELECT; writes via
    INSERT OR REPLACE under a lock so worker threads can call cache[url] = row
    concurrently.

    Why SQLite and not a CSV cache: the legacy CSV cache grew to 2.92 GB on
    a 130k-row run, took 7 minutes to read on startup, and held 6-7 GB of
    Python objects in RAM the whole time. SQLite loads the URL set
    (just primary keys) in well under a second, keeps content on disk, and
    uses ~50 MB of RAM regardless of cache size.
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA cache_size = -200000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache ("
            "url TEXT PRIMARY KEY, status TEXT, content_length INTEGER, "
            "phase INTEGER, content TEXT)"
        )
        self._conn.commit()
        self._urls = {u for (u,) in self._conn.execute("SELECT url FROM cache")}
        self._lock = threading.Lock()

    def __contains__(self, url):
        return url in self._urls

    def __getitem__(self, url):
        cur = self._conn.execute(
            "SELECT url, status, content_length, phase, content "
            "FROM cache WHERE url = ?",
            (url,),
        )
        row = cur.fetchone()
        if not row:
            raise KeyError(url)
        return {
            "url": row[0],
            "status": row[1],
            "content_length": row[2],
            "phase": row[3],
            "content": row[4],
        }

    def __setitem__(self, url, cache_row):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache "
                "(url, status, content_length, phase, content) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    cache_row["url"],
                    cache_row["status"],
                    int(cache_row["content_length"]),
                    int(cache_row["phase"]),
                    cache_row["content"],
                ),
            )
            self._conn.commit()
        self._urls.add(url)

    def __len__(self):
        return len(self._urls)

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def load_cache(db_path):
    """Returns a SQLiteCache. Name preserved for diag scripts that import it."""
    return SQLiteCache(db_path)


def append_output(output_path, headers, row):
    new = not os.path.exists(output_path)
    with open(output_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)
        f.flush()


def progress_line(phase, done, total, ok, fail, cf, dd, start):
    elapsed = time.time() - start
    rate = done / elapsed if elapsed > 0 else 0
    remaining = (total - done) / rate if rate > 0 else 0
    eta = f"{remaining / 60:.1f}min"
    if phase == 1:
        return (
            f"\rPhase 1: Scraping {done}/{total} | "
            f"OK: {ok} | CF: {cf} | DD: {dd} | Failed: {fail} | ETA: {eta}"
        )
    return (
        f"\rPhase 3: JS retry {done}/{total} rows | "
        f"New OK: {ok} | Final failed: {fail}"
    )


def run_phase(api_key, rows, concurrency, js_render, premium, phase, cache,
              output_path, out_headers):
    """Streaming-writes variant: calls write_success_row inline as each future
    completes so HTML content doesn't accumulate in memory. Returns
    (n_successes, failures). failures is a list of un-scraped rows.

    The streaming pattern is load-bearing for memory. The original
    implementation accumulated all OK rows into a `successes` list and
    flushed after the phase finished — at 65k OKs × ~30 KB of HTML each,
    that pushed private bytes to 24 GB. Now each future drops its content
    to disk and pops `homepage_content` off the row dict (see
    write_success_row) so steady-state RAM stays at 1-3 GB on 300k-row runs.
    """
    failures = []
    total = len(rows)
    if total == 0:
        return 0, failures

    done = ok = fail = cf = dd = 0
    start = time.time()
    session_local = threading.local()
    phase_settings = {"js_render": js_render, "premium": premium}

    def task(row):
        url = row["_normalized_url"]
        status, content, err = scrape_one(
            url, api_key, phase_settings, session_local
        )
        return row, url, status, content, err

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(task, r) for r in rows]
        for fut in as_completed(futures):
            row, url, status, content, _err = fut.result()
            done += 1
            if status == "ok":
                ok += 1
                cache_row = {
                    "url": url,
                    "status": "ok_js" if js_render else "ok",
                    "content_length": len(content),
                    "phase": phase,
                    "content": content,
                }
                cache[url] = cache_row
                write_success_row(row, cache_row, output_path, out_headers)
            elif status == "cloudflare_blocked":
                cf += 1
                cache_row = {
                    "url": url,
                    "status": "cloudflare_blocked",
                    "content_length": 0,
                    "phase": phase,
                    "content": "",
                }
                cache[url] = cache_row
                write_success_row(row, cache_row, output_path, out_headers)
            elif status == "dead_domain":
                dd += 1
                cache_row = {
                    "url": url,
                    "status": "dead_domain",
                    "content_length": 0,
                    "phase": phase,
                    "content": "",
                }
                cache[url] = cache_row
                write_success_row(row, cache_row, output_path, out_headers)
            elif status == "stopped":
                failures.append(row)
            else:
                fail += 1
                failures.append(row)

            if done % 5 == 0 or done == total:
                print(progress_line(phase, done, total, ok, fail, cf, dd, start),
                      end="", flush=True)
    print()
    return ok + cf + dd, failures


def write_success_row(row, cache_row, output_path, out_headers):
    row.update({
        "homepage_content": cache_row["content"],
        "homepage_status": cache_row["status"],
        "homepage_content_length": cache_row["content_length"],
        "scrape_phase": cache_row["phase"],
    })
    row.pop("_normalized_url", None)
    append_output(output_path, out_headers, row)
    # After the row is on disk, drop the content refs so 30k+ HTML strings
    # don't sit in cached_rows / successes_1 for the lifetime of main().
    row.pop("homepage_content", None)
    cache_row.pop("content", None)


def check_cooldown(folder, force):
    """Enforce a 15-minute cooldown after the last start.

    ZenRows' edge WAF blacklists the source IP for ~15 minutes after a hard
    stop or rapid restart — symptom is 100% Failed with 0 OK and 0 DD
    (pure TCP-close, no HTTP responses arrive). This guard refuses to
    launch inside that window unless --force is passed.

    Returns the marker path so main() can refresh the timestamp on launch.
    """
    marker_path = os.path.join(folder, LAST_RUN_FILENAME)
    if os.path.exists(marker_path):
        try:
            last = float(open(marker_path).read().strip())
        except (ValueError, OSError):
            last = 0
        age = time.time() - last
        if age < COOLDOWN_SECONDS:
            wait_min = (COOLDOWN_SECONDS - age) / 60
            msg = (
                f"\nLast run started {age/60:.1f} min ago — ZenRows' edge "
                f"WAF needs ~15 min to clear after a stop/restart.\n"
                f"Wait {wait_min:.1f} more minutes, then retry.\n\n"
                f"If you've already waited (e.g. you just rebooted) and a "
                f"probe shows ZenRows responding, re-run with --force.\n"
                f"Probe: python probe_resume.py \"<your_csv>\"\n"
            )
            if not force:
                print(msg)
                sys.exit(2)
            else:
                print(msg.replace("Wait", "WARNING: ignoring cooldown — wait"))
    return marker_path


def stamp_last_run(marker_path):
    try:
        with open(marker_path, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description="ZenRows Website Scraper")
    parser.add_argument("csv_path", help="Path to input CSV")
    parser.add_argument(
        "--concurrency", type=int, default=30,
        help=f"Phase 1 concurrency (1-{MAX_CONCURRENCY}, default 30)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Skip the 15-min restart cooldown check (use only after probing).",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ZENROWS_API_KEY", "").strip()
    if not api_key:
        print(SETUP_INSTRUCTIONS)
        sys.exit(1)

    csv_path = args.csv_path
    requested = max(1, args.concurrency)
    if requested > MAX_CONCURRENCY:
        print(
            f"WARNING: requested concurrency {requested} exceeds the safe "
            f"cap of {MAX_CONCURRENCY}. Above {MAX_CONCURRENCY}, ZenRows "
            f"per-IP rate limiting kicks in within ~40 minutes. Above 80, "
            f"Windows runs out of sockets. Clamping to {MAX_CONCURRENCY}."
        )
    concurrency = min(requested, MAX_CONCURRENCY)

    if not os.path.exists(csv_path):
        print(f"ERROR: CSV not found: {csv_path}")
        sys.exit(1)

    folder = os.path.dirname(os.path.abspath(csv_path))
    base = os.path.splitext(os.path.basename(csv_path))[0]
    output_path = os.path.join(folder, f"{base}_scraped.csv")
    db_path = os.path.join(folder, "scrape_cache.db")

    marker_path = check_cooldown(folder, args.force)
    stamp_last_run(marker_path)
    _install_signal_handlers()

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)
        in_headers = reader.fieldnames or []

    if not all_rows:
        print("ERROR: CSV is empty")
        sys.exit(1)

    website_col, company_col = detect_columns(in_headers)
    if not website_col:
        if len(in_headers) == 1:
            website_col = in_headers[0]
            print(
                f"NOTE: Single-column CSV; using '{website_col}' as the "
                f"website column."
            )
        else:
            print(
                f"ERROR: No website column auto-detected. Headers: {in_headers}\n"
                f"Rename one of your columns to include 'website', 'domain', "
                f"'url', 'site', 'web', or 'homepage' and re-run."
            )
            sys.exit(1)
    if not company_col:
        print(
            f"NOTE: No company column auto-detected (optional). "
            f"Headers: {in_headers}. Continuing without it."
        )

    print(f"Website column: {website_col}")
    print(f"Company column: {company_col}")

    out_headers = list(in_headers)
    for col in ("homepage_content", "homepage_status",
                "homepage_content_length", "scrape_phase"):
        if col not in out_headers:
            out_headers.append(col)

    if os.path.exists(output_path):
        print(
            f"\nWARNING: {output_path} exists from a prior run.\n"
            f"The scraper opens the output CSV in APPEND mode, so the "
            f"cached_rows pre-Phase-1 pass will duplicate every cached URL "
            f"on top of the existing file.\n"
            f"The cache DB (scrape_cache.db) is the source of truth — "
            f"delete the output CSV before restart and the scraper will "
            f"rebuild it from cache.\n"
            f"Delete: rm \"{output_path}\"\n"
            f"Or rebuild without re-running the scraper: python "
            f"export_from_cache.py \"{csv_path}\"\n"
        )

    todo = []
    for row in all_rows:
        raw = row.get(website_col, "")
        url = normalize_url(raw)
        if not url:
            row.update({
                "homepage_content": "",
                "homepage_status": "no_website",
                "homepage_content_length": 0,
                "scrape_phase": "",
            })
            append_output(output_path, out_headers, row)
            continue
        row["_normalized_url"] = url
        todo.append(row)

    t0 = time.time()
    cache = load_cache(db_path)
    print(f"Loaded {len(cache)} cached domains from {db_path} "
          f"in {time.time()-t0:.1f}s")

    cached_rows = [r for r in todo if r["_normalized_url"] in cache]
    fresh_rows = [r for r in todo if r["_normalized_url"] not in cache]
    print(f"Cached: {len(cached_rows)} | Fresh to scrape: {len(fresh_rows)}")

    for row in cached_rows:
        write_success_row(row, cache[row["_normalized_url"]], output_path, out_headers)

    # PHASE 1 (streaming writes — content is flushed to disk per-future)
    n_successes_1, failures_1 = run_phase(
        api_key, fresh_rows,
        concurrency=concurrency,
        js_render=False, premium=False, phase=1,
        cache=cache,
        output_path=output_path,
        out_headers=out_headers,
    )

    # PHASE 2 removed in v3.0: same-settings retry never recovered rows.

    # PHASE 3: JS render + premium proxy on remaining failures.
    n_successes_3 = 0
    failures_final = failures_1
    if failures_1 and not _stop_requested.is_set():
        n_successes_3, failures_final = run_phase(
            api_key, failures_1,
            concurrency=min(25, concurrency),
            js_render=True, premium=True, phase=3,
            cache=cache,
            output_path=output_path,
            out_headers=out_headers,
        )

    for row in failures_final:
        row.update({
            "homepage_content": "",
            "homepage_status": "scrape_failed",
            "homepage_content_length": 0,
            "scrape_phase": "",
        })
        row.pop("_normalized_url", None)
        append_output(output_path, out_headers, row)

    # Drain in-flight requests and close Sessions cleanly. If we skip this,
    # ZenRows keeps counting our concurrency slots as active for ~10-15 min
    # ("ghost connections") and the next launch trips the WAF.
    drain_deadline = time.time() + 10
    while _stop_requested.is_set() and time.time() < drain_deadline:
        time.sleep(0.2)
    _close_all_sessions()
    cache.close()

    total_ok = len(cached_rows) + n_successes_1 + n_successes_3
    total_failed = len(failures_final)
    print(
        f"\nDone: Total OK: {total_ok} | Total Failed: {total_failed} | "
        f"Saved to: {output_path}"
    )


if __name__ == "__main__":
    main()
