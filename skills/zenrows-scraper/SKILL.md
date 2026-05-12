SKILL NAME: ZenRows Website Scraper
VERSION: 2.1
AUTHOR: OlympoLabs

DESCRIPTION:
Scrapes homepage content for every company in a CSV using the
ZenRows API. Auto-detects website and company name columns.
Runs a two-phase flow: Phase 1 (main scrape at user concurrency,
default 30) and Phase 3 (JS render retry at concurrency 25 on
Phase 1 failures). The scraper is already implemented as
scraper.py in this repo. Claude Code DOWNLOADS and RUNS that
file. It does NOT rewrite, regenerate, or "improve" it.

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

Claude Code then performs the three steps below WITHOUT asking
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
- Default concurrency is 30. Cap is 50.

================================================================
WHAT THE SCRIPT DOES (reference only — do not reimplement)
================================================================
- Auto-detects website and company columns from CSV headers.
- Normalizes URLs (adds https://, strips www., strips trailing /).
- Phase 1: scrape all rows concurrently at user concurrency
  (default 30) with autoparse=true, anti_bot=true, js_render=false,
  collect failures.
- Phase 3: retry Phase 1 failures with js_render=true and
  premium_proxy=true at concurrency 25. Anything still failing
  is marked scrape_failed.
- Cache: scrape_cache.csv saved next to the input CSV.
  On restart, cached domains are skipped.
- Output: <csv_basename>_scraped.csv saved next to the input CSV.
- Adds columns: homepage_content, homepage_status,
  homepage_content_length, scrape_phase.
- Per-thread requests.Session() via threading.local for safe
  concurrent scraping.

================================================================
PROGRESS DISPLAY
================================================================
The script prints these lines as it runs:

    Phase 1: Scraping X/Y | OK: A | Failed: B | ETA: Xmin
    Phase 3: JS retry X rows | New OK: A | Final failed: B
    Done: Total OK: X | Total Failed: Y | Saved to: [path]

================================================================
TROUBLESHOOTING
================================================================
- "ZENROWS_API_KEY not set" → user must run setx and reopen
  terminal.
- 429 rate limit → reduce --concurrency (try 15 or 10).
- Many failures in Phase 3 → likely dead domains, not bugs.
- Windows socket exhaustion at 80+ → keep concurrency <= 50.
