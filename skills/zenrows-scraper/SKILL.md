SKILL NAME: ZenRows Website Scraper
VERSION: 3.0
AUTHOR: OlympoLabs

DESCRIPTION:
Scrapes homepage content for every company in a CSV using the
ZenRows API. Auto-detects website and company name columns.
Two-phase flow only: Phase 1 (main scrape at user concurrency,
default 30, hard cap 40) and Phase 3 (JS render retry at
concurrency 25 on Phase 1 failures). Phase 2 was removed in
v3.0 because identical-settings retries never recovered rows.
The scraper is already implemented as scraper.py in this repo.
Claude Code DOWNLOADS and RUNS that file. It does NOT rewrite,
regenerate, or "improve" it.

================================================================
IMPORTANT FOR CLAUDE CODE — READ FIRST
================================================================
1. Do NOT build a Python script from scratch. The script
   already exists at scraper.py in this skill folder. Just
   download and run it.
2. Always run scripts using bash syntax:
       ZENROWS_API_KEY="key" python "path/script.py" "csv_path"
   NEVER use PowerShell syntax like:
       $env:ZENROWS_API_KEY = "key"
3. NEVER use backslashes in paths passed on the bash command line.
   Convert D:\folder\file.csv to D:/folder/file.csv before running.
4. Download scraper.py into the SAME folder as the input CSV.
   NEVER write it to C:/Users/DELL/ or D:/ root.
5. Cache and output files MUST live in the CSV folder.
   Never under C:/Users/ or a drive root.
6. The cache DB (scrape_cache.db) is the source of truth.
   The output _scraped.csv is rebuilt from cache; delete it
   before restarting (see "Restart Rules" below).

================================================================
USER-FACING SETUP (one time only)
================================================================
The user runs this once in a terminal:

    setx ZENROWS_API_KEY "your_zenrows_key_here"

Then closes and reopens the terminal so the env var is visible
to new processes.

================================================================
USER-FACING USAGE (every run)
================================================================
The user opens Claude Code and types:

    Read this skill and run it:
    https://raw.githubusercontent.com/ranamuneebsb/Olympo-labs-skills/main/skills/zenrows-scraper/SKILL.md

    CSV: D:\My Folder\companies.csv

Claude Code then performs the steps below WITHOUT asking
follow-up questions and WITHOUT rewriting the script.

================================================================
STEPS CLAUDE CODE MUST FOLLOW
================================================================

STEP 1 — Download scraper.py into the CSV folder
-------------------------------------------------
Compute {csv_folder} from the user-provided CSV path
(everything before the final filename), using forward slashes.

Run exactly:

    curl -fsSL -o "{csv_folder}/scraper.py" \
      "https://raw.githubusercontent.com/ranamuneebsb/Olympo-labs-skills/main/skills/zenrows-scraper/scraper.py"

If curl is unavailable, fall back to:

    wget -O "{csv_folder}/scraper.py" \
      "https://raw.githubusercontent.com/ranamuneebsb/Olympo-labs-skills/main/skills/zenrows-scraper/scraper.py"

STEP 2 — Read API key from environment
---------------------------------------
The script reads ZENROWS_API_KEY from the environment itself.
Do NOT prompt the user for it. Do NOT hardcode it.

If ZENROWS_API_KEY is not set, the script will print setup
instructions and exit. In that case, tell the user to run:

    setx ZENROWS_API_KEY "your_zenrows_key_here"

then close and reopen the terminal, and try again.

STEP 3 — Run the script immediately
------------------------------------
Run, with bash syntax, exactly:

    ZENROWS_API_KEY="$ZENROWS_API_KEY" python "{csv_folder}/scraper.py" "{csv_path}" --concurrency 30

Notes:
- {csv_path} uses forward slashes.
- The CSV path is positional. Do NOT pass --csv-path.
- The API key is taken from the environment. Do NOT pass it
  as an argument.
- Default concurrency is 30. Hard cap is 40 (see "Concurrency
  Limits" below).

================================================================
RESTART RULES — READ BEFORE STOPPING OR RESTARTING
================================================================

1) 15-MINUTE COOLDOWN AFTER ANY KILL/RESTART
   ZenRows' edge WAF blocks the source IP for 10-15 minutes
   after a hard stop or rapid restart. Symptom: 100% Failed
   with 0 OK and 0 DD (no HTTP responses arrive at all — pure
   TCP-close / RemoteDisconnected).

   Rule: wait at least 15 minutes between any stop and the
   next start. scraper.py records the last start time in
   `{csv_folder}/.zenrows_last_run` and will refuse to launch
   inside the 15-minute window unless you pass `--force`.
   Use `--force` only after you've actually waited (e.g. after
   running probe_resume.py and confirming responses arrive).

2) PROBE BEFORE RELAUNCHING IF UNSURE
   If you don't remember when you last stopped, or you killed
   the process abruptly, run probe_resume.py first:
       python "{csv_folder}/probe_resume.py" "{csv_path}"
   It hits ZenRows with 3 URLs. ≥1 HTTP response = WAF clear,
   safe to launch. 0/3 = still blocked, wait longer.

3) DELETE THE OUTPUT CSV BEFORE RESTART
   The output `<basename>_scraped.csv` is in append mode. If
   you restart while it still exists, the pre-Phase-1 cached
   rows get appended on top of the previous run's rows →
   duplicate rows for every cached URL.

   Cache (scrape_cache.db) is the source of truth — NOT the
   output CSV. The scraper will rebuild the output CSV from
   cache on the next run.

   If you need to recover the output CSV without re-running
   the scraper, use export_from_cache.py:
       python "{csv_folder}/export_from_cache.py" "{csv_path}"

4) GHOST CONNECTIONS AFTER HARD KILL
   Killing the process with taskkill / SIGKILL leaves ZenRows
   counting your concurrency slots as in-use for ~10 minutes
   (Concurrency-Remaining: 0 even though nothing is running).
   Prefer Ctrl-C or `kill -INT` so the SIGINT handler closes
   sessions cleanly. If you had to hard-kill, wait the full
   15-minute cooldown before relaunching.

================================================================
CONCURRENCY LIMITS
================================================================
- Default: 30. Sustainable for hours at a time.
- Hard cap: 40. scraper.py clamps higher values down to 40 and
  prints a warning. Above 40 triggers ZenRows per-IP rate
  limiting within ~40 minutes.
- Above 80: Windows socket exhaustion errors.
- If you see sustained low success rate (<30% over 5k+ rows)
  while a fresh probe shows healthy responses, drop to 15-20
  rather than killing the run — the issue is per-IP
  throttling, not a code bug.

================================================================
WHAT THE SCRIPT DOES (reference only — do not reimplement)
================================================================
- Auto-detects website and company columns from CSV headers.
- Normalizes URLs (adds https://, strips www., strips trailing /).
- Phase 1: scrape all rows concurrently at user concurrency
  (default 30) with autoparse=true, antibot=true,
  js_render=false, collect failures.
- Phase 3: retry Phase 1 failures with js_render=true and
  premium_proxy=true at concurrency 25. Anything still failing
  is marked scrape_failed. (Phase 2 — same-settings retry —
  was removed; it never recovered rows.)
- Cache: scrape_cache.db (SQLite, WAL mode) saved next to the
  input CSV. Loads 70k entries in <0.5s and uses ~50 MB RAM
  (previous CSV cache loaded in 7 min and used 6-7 GB).
- Streaming writes: each successful row is appended to the
  output CSV the moment its future completes, and the HTML
  content is dropped from in-memory dicts immediately after.
  Steady-state RAM is ~1-3 GB even on 300k-row runs.
- Output: <csv_basename>_scraped.csv saved next to the input
  CSV. Append mode — delete before restart (see Restart Rules).
- Adds columns: homepage_content, homepage_status,
  homepage_content_length, scrape_phase.
- Permanent verdicts that won't be retried on next run:
    * `dead_domain` — Content-Type application/problem+json
      with HTTP status in {400, 404, 410, 413, 422}. Covers
      ZenRows RESP001 (parked), RESP002/007 (404),
      RESP005 (response too large).
    * `cloudflare_blocked` — detected from response body
      (ZenRows strips CF headers), or 403 status, or
      200-with-empty-body.
- Transient errors that ARE retried on next run: timeouts,
  402 (quota), 429 (rate limit), 5xx, TCP-close.
- Graceful shutdown: SIGINT / SIGTERM sets a stop flag, waits
  up to 10s for in-flight requests, closes thread-local
  Sessions, and exits 0. Use Ctrl-C, not taskkill /F.

================================================================
PROGRESS DISPLAY
================================================================
The script prints these lines as it runs:

    Phase 1: Scraping X/Y | OK: A | CF: C | DD: D | Failed: B | ETA: Xmin
    Phase 3: JS retry X rows | New OK: A | Final failed: B
    Done: Total OK: X | Total Failed: Y | Saved to: [path]

CF = cloudflare_blocked, DD = dead_domain. Both are cached
permanently and written to the output CSV (just with empty
homepage_content) — they are NOT counted as failures.

================================================================
TROUBLESHOOTING
================================================================
- "ZENROWS_API_KEY not set" → user must run setx and reopen
  terminal.
- "Last run was N minutes ago — wait 15 min or pass --force"
  → genuine cooldown protection. Wait, then retry. Use
  --force only after probe_resume.py confirms the WAF is clear.
- 100% Failed with 0 OK and 0 DD → edge WAF block. Stop,
  wait 15 min, run probe_resume.py, then relaunch.
- 429 rate limit → reduce --concurrency (try 15 or 10).
- Sustained <30% success while probe is healthy → reduce
  concurrency to 15-20 instead of killing.
- Many failures in Phase 3 → likely dead domains, not bugs.
- Concurrency-Remaining: 0 with no scraper running → ghost
  connections from a hard kill. Wait 10-15 min.
- Duplicate rows in output CSV → restart-without-delete. Stop,
  delete `<basename>_scraped.csv`, restart (cache repopulates
  it on the cached_rows pre-Phase-1 pass). Or use
  export_from_cache.py.
- Output CSV is 0 bytes for 1-3 min after launch with a
  populated cache → normal. The pre-Phase-1 cached_rows loop
  is running; stdout is block-buffered until the first
  Phase-1 progress line forces a flush. Check the file's
  mtime to confirm activity.
