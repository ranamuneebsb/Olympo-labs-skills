"""Rebuild the output CSV from the SQLite cache.

Use this when:
  - You deleted <basename>_scraped.csv to fix duplicate-row corruption.
  - The scraper is paused and you want a snapshot of current progress.
  - You want to inspect the cache without touching the live cache DB.

This is safe to run while the scraper is alive (SQLite WAL mode allows
concurrent readers).

Usage:
    python export_from_cache.py <csv_path>

Output:
    <basename>_scraped.csv  (overwritten if it exists)
"""
import csv
import os
import sqlite3
import sys
import time
from urllib.parse import urlparse


csv.field_size_limit(sys.maxsize)


WEBSITE_KEYS = ["website", "domain", "url", "site", "web", "homepage"]


def find_website_column(headers):
    lower = {h.lower(): h for h in headers}
    for h_low, h_orig in lower.items():
        for k in WEBSITE_KEYS:
            if k in h_low:
                return h_orig
    if len(headers) == 1:
        return headers[0]
    return None


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


def main():
    if len(sys.argv) < 2:
        print("Usage: python export_from_cache.py <csv_path>")
        sys.exit(2)
    csv_path = sys.argv[1]
    if not os.path.exists(csv_path):
        print(f"ERROR: CSV not found: {csv_path}")
        sys.exit(2)

    folder = os.path.dirname(os.path.abspath(csv_path))
    base = os.path.splitext(os.path.basename(csv_path))[0]
    db_path = os.path.join(folder, "scrape_cache.db")
    out_path = os.path.join(folder, f"{base}_scraped.csv")

    if not os.path.exists(db_path):
        print(f"ERROR: cache DB not found: {db_path}")
        sys.exit(2)

    print(f"Loading TODO from {csv_path} ...", flush=True)
    t0 = time.time()
    todo_by_url = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        in_headers = reader.fieldnames or []
        col = find_website_column(in_headers)
        if not col:
            print(f"ERROR: no website column in headers {in_headers}")
            sys.exit(2)
        for row in reader:
            raw = (row.get(col) or "").strip()
            nu = normalize_url(raw)
            if nu and nu not in todo_by_url:
                todo_by_url[nu] = row
    print(
        f"TODO loaded: {len(todo_by_url):,} URL-keyed rows in "
        f"{time.time()-t0:.1f}s",
        flush=True,
    )

    out_headers = list(in_headers)
    for col in ("homepage_content", "homepage_status",
                "homepage_content_length", "scrape_phase"):
        if col not in out_headers:
            out_headers.append(col)

    print(f"Streaming cache from {db_path} ...", flush=True)
    t0 = time.time()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    cur = conn.execute(
        "SELECT url, status, content_length, phase, content FROM cache"
    )

    n_cache = n_match = n_miss = 0
    status_counts = {}
    with open(out_path, "w", encoding="utf-8", newline="") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=out_headers,
                                extrasaction="ignore")
        writer.writeheader()
        for url, status, cl, phase, content in cur:
            n_cache += 1
            status_counts[status] = status_counts.get(status, 0) + 1
            row = todo_by_url.get(url)
            if row is None:
                n_miss += 1
                continue
            row = dict(row)
            row["homepage_content"] = content
            row["homepage_status"] = status
            row["homepage_content_length"] = cl
            row["scrape_phase"] = phase
            writer.writerow(row)
            n_match += 1
            if n_cache % 5000 == 0:
                print(
                    f"  cache rows processed: {n_cache:,}, written: "
                    f"{n_match:,}, miss: {n_miss:,} "
                    f"({time.time()-t0:.1f}s)",
                    flush=True,
                )

    print(f"\nDone in {time.time()-t0:.1f}s", flush=True)
    print(f"Cache rows scanned: {n_cache:,}", flush=True)
    print(f"Status breakdown: {status_counts}", flush=True)
    print(f"Rows written: {n_match:,}", flush=True)
    print(f"Cache rows with no matching TODO row: {n_miss:,}", flush=True)
    print(f"Output: {out_path}", flush=True)


if __name__ == "__main__":
    main()
