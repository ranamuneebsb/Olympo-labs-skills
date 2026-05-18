"""Single-shot ZenRows connection probe.

Hits ZenRows with antibot+autoparse against 3 URLs from the input CSV.
- Exit 0 if at least one URL returns ANY HTTP status (edge WAF is clear,
  safe to launch the scraper).
- Exit 1 if all three trigger TCP-close / RemoteDisconnected — the edge
  WAF is still blocking the source IP. Wait another 5-10 minutes and try
  again.

Reads ZENROWS_API_KEY from the environment. Reads the website column from
the same column-name heuristics as scraper.py.

Usage:
    python probe_resume.py "path/to/input.csv"
"""
import csv
import os
import sys

import requests
from requests.exceptions import ConnectionError as ReqConnErr


csv.field_size_limit(sys.maxsize)


WEBSITE_KEYS = ["website", "domain", "url", "site", "web", "homepage"]
ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"


def find_website_column(headers):
    lower = {h.lower(): h for h in headers}
    for h_low, h_orig in lower.items():
        for k in WEBSITE_KEYS:
            if k in h_low:
                return h_orig
    if len(headers) == 1:
        return headers[0]
    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python probe_resume.py <csv_path>")
        sys.exit(2)
    csv_path = sys.argv[1]

    api_key = os.environ.get("ZENROWS_API_KEY", "").strip()
    if not api_key:
        print("ERROR: ZENROWS_API_KEY not set in environment.")
        sys.exit(2)

    if not os.path.exists(csv_path):
        print(f"ERROR: CSV not found: {csv_path}")
        sys.exit(2)

    urls = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        col = find_website_column(headers)
        if not col:
            print(f"ERROR: no website column in {headers}")
            sys.exit(2)
        for row in reader:
            u = (row.get(col) or "").strip()
            if not u:
                continue
            if not u.startswith(("http://", "https://")):
                u = "https://" + u
            urls.append(u)
            if len(urls) >= 3:
                break

    if not urls:
        print("ERROR: no usable URLs found in CSV")
        sys.exit(2)

    http_responses = 0
    for u in urls:
        print(f"=== {u} ===", flush=True)
        try:
            r = requests.get(
                ZENROWS_ENDPOINT,
                params={
                    "url": u,
                    "apikey": api_key,
                    "antibot": "true",
                    "autoparse": "true",
                },
                timeout=60,
            )
            print(f"  status: {r.status_code}, body_len: {len(r.text or '')}",
                  flush=True)
            print(f"  content-type: {r.headers.get('Content-Type', '')}",
                  flush=True)
            http_responses += 1
        except ReqConnErr as e:
            print(f"  CONNECTION-ERROR (likely TCP-close / WAF): {e}",
                  flush=True)
        except Exception as e:
            print(f"  OTHER: {type(e).__name__}: {e}", flush=True)

    print(f"\nPROBE_RESULT: {http_responses}/3 HTTP responses received",
          flush=True)
    if http_responses > 0:
        print("VERDICT: edge WAF clear — safe to launch scraper.")
        sys.exit(0)
    else:
        print("VERDICT: still blocked — wait 5-10 more minutes and retry.")
        sys.exit(1)


if __name__ == "__main__":
    main()
