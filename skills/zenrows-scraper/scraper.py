"""
ZenRows Website Scraper - 3-phase scraping with auto-retry.
See SKILL.md for the full spec.
"""
import argparse
import csv
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests


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


def scrape_one(url, api_key, phase_settings, session_local, timeout=30):
    """Returns (status, content, error_or_None).

    phase_settings: dict with keys js_render (bool) and premium (bool).
    session_local: threading.local() shared by the worker pool. Each
    thread lazily creates its own requests.Session() the first time it
    runs, so concurrent workers do not share connection state.
    """
    if not hasattr(session_local, "session"):
        session_local.session = requests.Session()
    session = session_local.session

    js_render = phase_settings.get("js_render", False)
    premium = phase_settings.get("premium", False)

    params = {
        "url": url,
        "apikey": api_key,
        "autoparse": "true",
        "antibot": "true",
        "js_render": "true" if js_render else "false",
    }
    if premium:
        params["premium_proxy"] = "true"

    backoffs = [5, 10, 20]
    last_err = None
    tried_no_verify = False

    for attempt in range(3):
        try:
            r = session.get(ZENROWS_ENDPOINT, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < len(backoffs):
                    time.sleep(backoffs[attempt])
                    continue
                return "failed", "", f"429 after {attempt + 1} attempts"
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


def load_cache(cache_path):
    cache = {}
    if not os.path.exists(cache_path):
        return cache
    with open(cache_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = row.get("url")
            if url:
                cache[url] = row
    return cache


def append_cache(cache_path, row):
    new = not os.path.exists(cache_path)
    with open(cache_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["url", "status", "content_length", "phase", "content"]
        )
        if new:
            writer.writeheader()
        writer.writerow(row)
        f.flush()


def append_output(output_path, headers, row):
    new = not os.path.exists(output_path)
    with open(output_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)
        f.flush()


def progress_line(phase, done, total, ok, fail, start):
    elapsed = time.time() - start
    rate = done / elapsed if elapsed > 0 else 0
    remaining = (total - done) / rate if rate > 0 else 0
    eta = f"{remaining / 60:.1f}min"
    if phase == 1:
        return (
            f"\rPhase 1: Scraping {done}/{total} | "
            f"OK: {ok} | Failed: {fail} | ETA: {eta}"
        )
    if phase == 2:
        return (
            f"\rPhase 2: Retrying {done}/{total} failed rows | "
            f"New OK: {ok} | Still failed: {fail}"
        )
    return (
        f"\rPhase 3: JS retry {done}/{total} rows | "
        f"New OK: {ok} | Final failed: {fail}"
    )


def run_phase(api_key, rows, concurrency, js_render, premium, phase, cache_path, cache):
    """Returns (successes, failures). successes = [(row, cache_row), ...]."""
    successes, failures = [], []
    total = len(rows)
    if total == 0:
        return successes, failures

    done = ok = fail = 0
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
                append_cache(cache_path, cache_row)
                cache[url] = cache_row
                successes.append((row, cache_row))
            else:
                fail += 1
                failures.append(row)

            if done % 5 == 0 or done == total:
                print(progress_line(phase, done, total, ok, fail, start),
                      end="", flush=True)
    print()
    return successes, failures


def write_success_row(row, cache_row, output_path, out_headers):
    row.update({
        "homepage_content": cache_row["content"],
        "homepage_status": cache_row["status"],
        "homepage_content_length": cache_row["content_length"],
        "scrape_phase": cache_row["phase"],
    })
    row.pop("_normalized_url", None)
    append_output(output_path, out_headers, row)


def main():
    parser = argparse.ArgumentParser(description="ZenRows Website Scraper")
    parser.add_argument("csv_path", help="Path to input CSV")
    parser.add_argument("--concurrency", type=int, default=30,
                        help="Phase 1 concurrency (1-50, default 30)")
    args = parser.parse_args()

    api_key = os.environ.get("ZENROWS_API_KEY", "").strip()
    if not api_key:
        print(SETUP_INSTRUCTIONS)
        sys.exit(1)

    csv_path = args.csv_path
    concurrency = min(max(1, args.concurrency), 50)

    if not os.path.exists(csv_path):
        print(f"ERROR: CSV not found: {csv_path}")
        sys.exit(1)

    folder = os.path.dirname(os.path.abspath(csv_path))
    base = os.path.splitext(os.path.basename(csv_path))[0]
    output_path = os.path.join(folder, f"{base}_scraped.csv")
    cache_path = os.path.join(folder, "scrape_cache.csv")

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

    # If output already exists from a prior run, leave it; rows we skip below
    # will already be in it via the cache logic. Otherwise start fresh.
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

    cache = load_cache(cache_path)
    print(f"Loaded {len(cache)} cached domains from {cache_path}")

    cached_rows = [r for r in todo if r["_normalized_url"] in cache]
    fresh_rows = [r for r in todo if r["_normalized_url"] not in cache]
    print(f"Cached: {len(cached_rows)} | Fresh to scrape: {len(fresh_rows)}")

    for row in cached_rows:
        write_success_row(row, cache[row["_normalized_url"]], output_path, out_headers)

    # PHASE 1
    successes_1, failures_1 = run_phase(
        api_key, fresh_rows,
        concurrency=concurrency,
        js_render=False, premium=False, phase=1,
        cache_path=cache_path, cache=cache,
    )
    for row, cache_row in successes_1:
        write_success_row(row, cache_row, output_path, out_headers)

    # PHASE 2
    failures_2 = []
    successes_2 = []
    if failures_1:
        time.sleep(5)
        successes_2, failures_2 = run_phase(
            api_key, failures_1,
            concurrency=10,
            js_render=False, premium=False, phase=2,
            cache_path=cache_path, cache=cache,
        )
        for row, cache_row in successes_2:
            write_success_row(row, cache_row, output_path, out_headers)

    # PHASE 3
    failures_3 = []
    successes_3 = []
    if failures_2:
        time.sleep(5)
        successes_3, failures_3 = run_phase(
            api_key, failures_2,
            concurrency=5,
            js_render=True, premium=True, phase=3,
            cache_path=cache_path, cache=cache,
        )
        for row, cache_row in successes_3:
            write_success_row(row, cache_row, output_path, out_headers)
        for row in failures_3:
            row.update({
                "homepage_content": "",
                "homepage_status": "scrape_failed",
                "homepage_content_length": 0,
                "scrape_phase": "",
            })
            row.pop("_normalized_url", None)
            append_output(output_path, out_headers, row)

    total_ok = (
        len(cached_rows)
        + len(successes_1)
        + len(successes_2)
        + len(successes_3)
    )
    total_failed = len(failures_3)
    print(
        f"\nDone: Total OK: {total_ok} | Total Failed: {total_failed} | "
        f"Saved to: {output_path}"
    )


if __name__ == "__main__":
    main()
