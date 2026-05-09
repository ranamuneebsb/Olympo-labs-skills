SKILL NAME: ZenRows Website Scraper
VERSION: 1.0
AUTHOR: OlympoLabs

DESCRIPTION:
Scrapes homepage content for every company
in a CSV using ZenRows API. Auto-detects
website and company name columns. No column
mapping needed from user.

USER INPUTS (only 3 required):
- csv_path: full path to CSV including folder
- zenrows_api_key: ZenRows API key
- concurrent_requests: default 30

THAT IS ALL THE USER NEEDS TO PROVIDE.
Script auto-handles everything else.

HOW TO RUN:
The implementation is in scraper.py next to this file.
Invoke it with:

    python scraper.py \
      --csv-path "<csv_path>" \
      --zenrows-api-key "<zenrows_api_key>" \
      --concurrent-requests <concurrent_requests>

Required Python packages: requests

AUTO-DETECTION LOGIC:
Script scans CSV headers automatically and
finds the right columns using these rules:

Website column — match first column containing:
website, domain, url, site, web, homepage

Company name column — match first column containing:
company, name, firm, organization, org, business

If auto-detection fails → print which columns
were found and ask user to confirm before running.

OUTPUT FILE:
Saved to same folder as input CSV
Named: [original_filename]_scraped.csv

---

SCRAPER.PY BEHAVIOR:

URL NORMALIZATION:
- If domain is bare like example.com →
  prepend https:// automatically
- If starts with http:// → convert to https://
- Strip trailing slashes
- Strip www. variations and keep consistent

SCRAPING STRATEGY — 3 PHASE APPROACH:

PHASE 1 — Main scrape run:
- Scrape all rows concurrently at set concurrency
- Settings: autoparse=true, anti_bot=true,
  js_render=false, timeout=30
- Save successful scrapes to cache immediately
- Track all failed rows separately
- Do NOT retry during this phase
  just collect failures

PHASE 2 — Retry failed rows (after Phase 1 done):
- Take all rows that failed in Phase 1
- Wait 5 seconds before starting
- Re-scrape with same settings:
  autoparse=true, anti_bot=true, js_render=false
- Reduce concurrency to 10 for retry phase
- Save any new successes to cache
- Track still-failed rows

PHASE 3 — JS render retry (after Phase 2 done):
- Take all rows still failing after Phase 2
- Re-scrape with js_render=true,
  premium_proxy=true, concurrency=5
- This costs more ZenRows credits
  only used as last resort
- Save any new successes
- Rows still failing after Phase 3 →
  mark as scrape_failed and skip

CACHE SYSTEM:
- Save scrape results to:
  [folder]/scrape_cache.csv after each phase
- Cache keyed by domain URL
- On any restart → load cache first,
  skip already-cached domains
- Never re-scrape a domain already in cache

AUTOSAVE:
- Do NOT use pandas df.to_csv() for autosave
  causes blocking on large files
- Use incremental row-by-row append
  to output CSV using csv.writer
- Open output file in append mode
- Write header once at start
- Append each row immediately after processing
- This prevents data loss on crashes

PROGRESS DISPLAY:
Phase 1: Scraping X/Y | OK: A | Failed: B | ETA: Xmin
Phase 2: Retrying X failed rows | New OK: A | Still failed: B
Phase 3: JS retry X rows | New OK: A | Final failed: B
Done: Total OK: X | Total Failed: Y | Saved to: [path]

ERROR HANDLING:
- 429 rate limit → wait 5 sec, retry up to 3 times
  with exponential backoff (5s, 10s, 20s)
- Timeout → add to failed list, move on
- DNS/connection error → add to failed list, move on
- SSL error → retry once with verify=false
- Empty response → add to failed list for Phase 2
- Redirect loop → mark scrape_failed, skip

OUTPUT COLUMNS ADDED:
- homepage_content (full content, no truncation)
- homepage_status (ok/ok_js/scrape_failed/no_website)
- homepage_content_length (character count)
- scrape_phase (1/2/3 — which phase succeeded)

KNOWN ISSUES TO HANDLE:
- autoparse=true returns JSON-LD schema data
  not raw HTML — this is fine, AI can read it
- PE firm sites are often JS heavy —
  Phase 3 handles these
- IRS nonprofit list has many dead domains —
  Phase 3 will confirm dead vs bot-blocked
- 429 errors mean concurrency too high —
  Phase 2 and 3 use lower concurrency automatically
- pandas to_csv() blocks on large files —
  use incremental append instead (critical fix)
- Windows socket limits at 80+ concurrent —
  keep default at 30, never exceed 50
