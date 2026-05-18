SKILL NAME: ICP Classifier
VERSION: 2.0
AUTHOR: OlympoLabs

DESCRIPTION:
Classifies companies in a CSV for ICP fit using OpenRouter AI.
The classifier is already implemented as classifier.py in this
repo. Claude Code DOWNLOADS and RUNS that file. It does NOT
rewrite, regenerate, or "improve" it.

================================================================
IMPORTANT FOR CLAUDE CODE — READ FIRST
================================================================
1. DO NOT write classifier.py from scratch.
   DO NOT generate any classifier code.
   DO NOT "improve" or rewrite the existing script.
   The script already exists at classifier.py in this skill
   folder. Just DOWNLOAD it and RUN it. Same as the zenrows
   scraper skill.
2. Always run scripts using bash syntax:
       OPENROUTER_API_KEY="key" python "path/classifier.py" "csv_path"
   NEVER use PowerShell syntax like:
       $env:OPENROUTER_API_KEY = "key"
3. NEVER use backslashes in paths passed on the bash command line.
   Convert D:\folder\file.csv to D:/folder/file.csv before running.
4. Download classifier.py into the SAME folder as the input CSV.
   NEVER write it to C:/Users/DELL/ or D:/ root.
5. Progress file (progress.json), done-keys DB (_done_keys.db),
   summary CSV, qualified CSV, and failed-response log MUST live
   in the CSV folder.
6. The _done_keys.db SQLite file is the source of truth for
   resume. The output CSV is append-only. Never delete the DB
   unless you want a full re-run from scratch.

================================================================
USER-FACING SETUP (one time only)
================================================================
The user runs this once in a terminal:

    setx OPENROUTER_API_KEY "your_openrouter_key_here"

Then closes and reopens the terminal so the env var is visible
to new processes.

Manual pip install (if setup scripts not used):

    pip install requests

================================================================
USER-FACING USAGE (every run)
================================================================
The user opens Claude Code and types:

    Read this skill and run it:
    https://raw.githubusercontent.com/ranamuneebsb/Olympo-labs-skills/main/skills/icp-classifier/SKILL.md

    CSV: D:\My Folder\companies.csv
    ICP prompt: D:\My Folder\icp_prompt.txt

Claude Code then performs the steps below WITHOUT asking
follow-up questions and WITHOUT rewriting the script.

================================================================
STEPS CLAUDE CODE MUST FOLLOW
================================================================

STEP 1 — Download classifier.py into the CSV folder
----------------------------------------------------
Compute {csv_folder} from the user-provided CSV path
(everything before the final filename), using forward slashes.

Run exactly:

    curl -fsSL -o "{csv_folder}/classifier.py" \
      "https://raw.githubusercontent.com/ranamuneebsb/Olympo-labs-skills/main/skills/icp-classifier/classifier.py"

If curl is unavailable, fall back to:

    wget -O "{csv_folder}/classifier.py" \
      "https://raw.githubusercontent.com/ranamuneebsb/Olympo-labs-skills/main/skills/icp-classifier/classifier.py"

DO NOT write classifier.py from scratch.
DO NOT generate any classifier code.
DOWNLOAD it from GitHub. Then run it.

STEP 2 — Read API key from environment
---------------------------------------
The script reads OPENROUTER_API_KEY from the environment itself.
Do NOT prompt the user for it. Do NOT hardcode it.

If OPENROUTER_API_KEY is not set, the script will print setup
instructions and exit. In that case, tell the user to run:

    setx OPENROUTER_API_KEY "your_openrouter_key_here"

then close and reopen the terminal, and try again.

STEP 3 — Run the script immediately
------------------------------------
Run, with bash syntax, exactly:

    OPENROUTER_API_KEY="$OPENROUTER_API_KEY" python "{csv_folder}/classifier.py" \
      "{csv_path}" \
      --icp-prompt "{icp_prompt_path_or_text}" \
      --concurrency 30

Notes:
- {csv_path} uses forward slashes.
- The CSV path is positional.
- The API key is taken from the environment.
- Default concurrency = 30. Max recommended = 60.
  The script WARNS the user if --concurrency exceeds 60.

STEP 4 — Check progress instantly (do NOT scan the output CSV)
---------------------------------------------------------------
To report progress to the user, read progress.json in the CSV
folder. It is updated every 100 rows. Do NOT cat or scan the
output CSV — it can be 5–7 GB and takes 17 minutes to read.

    cat "{csv_folder}/{base}_progress.json"

Fields: rows_done, rows_ok, rows_failed, score_7_plus,
last_updated, total_rows, pending.

================================================================
USER INPUTS (only 3 required)
================================================================
- csv_path:           full path to CSV including folder
- OPENROUTER_API_KEY: read from env, do not pass via CLI
- icp_prompt:         text or path to .txt file

OPTIONAL FLAGS:
  --model                  default google/gemini-2.5-flash-lite
  --concurrency            default 30, max recommended 60
  --phase2-concurrency     default 30 (was 10 in v1.0)
  --skip-confirm           skip the >1000-row cost confirmation
  --website-col            override website column auto-detect

================================================================
OUTPUT FILES (all in CSV folder, alongside input)
================================================================
1. {base}_classified.csv
   Full output with all original columns + ICP columns.
   Append-only. Can be 5–7 GB on a 65k-row run.

2. {base}_summary.csv  (LESSON 2)
   Tiny file — only 4 columns:
     Website, icp_score, icp_ecommerce_type, icp_disqualified
   Safe to open in Excel even on a 65k-row run.

3. {base}_qualified.csv  (LESSON 9)
   Only rows with icp_score >= 7. All columns EXCEPT
   homepage_content. Written at end of run.

4. {base}_progress.json  (LESSON 1)
   Updated every 100 rows. Tiny JSON. Read this for status.

5. {base}_done_keys.db  (LESSON 4)
   SQLite DB of already-classified website keys.
   Loaded in under 1 second on restart.

6. {base}_failed_responses.log  (LESSON 10)
   Raw AI responses for rows where JSON parsing failed.
   Each entry: row index, website, raw text. Use for debugging.

================================================================
AUTO-DETECTION LOGIC
================================================================
Script scans CSV headers and finds columns:

Website column (DEDUP KEY) — match first column containing:
website, url, domain, homepage, site

Content column — match first column containing:
homepage_content, content, scraped, body, text

Company name column — match first column containing:
company, name, firm, organization, org, business

Description column — match first column containing:
description, desc, about, summary, overview

Location column — match first column containing:
location, city, country, hq, headquarters, state

Size column — match first column containing:
size, employees, headcount, employee_size, staff

Homepage status column (for SKIP logic) — match first column
containing: homepage_status, scrape_status, status

================================================================
SKIP LOGIC (LESSON 8) — DEAD DOMAIN ROWS
================================================================
If a homepage_status column is present and a row's value is
one of:

    dead_domain, cloudflare_blocked, blocked,
    not_found, dns_failed, ssl_failed

→ DO NOT call the AI on that row. DO NOT use name-only fallback.
Write the row to output with:
    icp_score = ""
    icp_summary = ""
    icp_reasoning = "Skipped: homepage_status={value}"
    icp_confidence = ""
    icp_disqualified = ""
    classification_status = "skipped"
    content_source = "skipped"

This saves API credits on rows with no usable content.

================================================================
3-TIER CONTENT FALLBACK (for rows NOT skipped)
================================================================

TIER 1 — Homepage content available (best):
- Use homepage_content as primary AI input
- Truncate to EXACTLY 4000 chars (LESSON 11 — always 4000)
- Full classification, confidence based on content quality

TIER 2 — No content but description available:
- Use description as primary input
- Force confidence = "low"
- Reasoning: "Classified from description only"

TIER 3 — No content AND no description:
- DuckDuckGo instant answer API search
- If still empty → use company name only, confidence = "low"

================================================================
RESUME / DEDUP LOGIC (LESSONS 3 + 4)
================================================================
1. On startup, open _done_keys.db SQLite. If the file does not
   exist, create it with table done_keys(key TEXT PRIMARY KEY).
2. Load the set of all keys from SQLite. This is fast (under 1
   second even at 200k rows). DO NOT scan the output CSV.
3. For each input row, compute the dedup key:
       key = strip+lower of the website column
       fallback to strip+lower of the company column
4. If key is already in the set → skip (already classified).
5. After successful classification, INSERT the key into
   done_keys.db.
6. Pending count = total input rows minus unique keys in DB.
   Never count rows in the output CSV (it may contain dups
   from earlier broken runs).

This guarantees no duplicate classifications even after many
restarts, AND eliminates the 17-minute startup scan.

================================================================
COST + TIME CONFIRMATION (LESSON 12)
================================================================
For runs with more than 1000 pending rows, the script prints:

    Total pending:      {N} rows
    Model:              {model}
    Estimated tokens:   ~{input_tokens} in / ~{output_tokens} out
    Estimated cost:     ~${cost}
    Estimated time:     ~{minutes} min at concurrency {C}

    Type "yes" to continue, anything else to abort:

Bypass with --skip-confirm for unattended runs.

Cost formula (gemini-2.5-flash-lite default pricing):
    input_per_row  ≈ 1500 tokens  (4000-char content + prompt)
    output_per_row ≈ 200 tokens
    cost_per_row   ≈ 0.0002 USD
For 65k rows: ~$13 and ~36 min at concurrency 30.

================================================================
CONCURRENCY GUIDANCE (LESSON 6)
================================================================
Empirically observed fail rates on gemini-2.5-flash-lite via
OpenRouter:
    concurrency 100  →  37% fail rate (rate-limit storms)
    concurrency 60   →   2% fail rate (sweet spot)
    concurrency 30   →   <1% fail rate (default, safest)

The script WARNS but does not block if --concurrency > 60.
Default = 30. Hard cap = 100.

================================================================
PHASE 2 RETRY (LESSON 5)
================================================================
Phase 1 runs at --concurrency (default 30).
Phase 2 retries failures at --phase2-concurrency (default 30,
was 10 in v1.0 — 10 was unusably slow on thousands of failures).
Phase 2 sleeps 5 seconds before starting.

================================================================
FAILED-RESPONSE LOGGING (LESSON 10)
================================================================
When the AI returns text that cannot be parsed as JSON (even
after stripping markdown fences and extracting first { to
last }), the raw response is appended to:

    {base}_failed_responses.log

Format per entry:

    [{timestamp}] row={row_index} website={website}
    --- raw response ---
    {raw_text}
    --- end ---

Open this file to debug why specific rows keep failing
(usually content-policy refusals or model returning prose).

================================================================
OUTPUT COLUMNS ADDED
================================================================
- icp_score             (1-10 or empty if skipped)
- icp_summary           (one sentence)
- icp_reasoning         (2-3 sentences)
- icp_confidence        (high / medium / low)
- icp_disqualified      (yes / no)
- icp_ecommerce_type    (free-form short label, may be empty)
- classification_status (ok / classification_failed / skipped)
- content_source        (scraped_content / description_only /
                         web_search / name_only / skipped)

================================================================
JSON SCHEMA REQUESTED FROM AI
================================================================
The script asks for:

    {
      "score":            <1-10>,
      "summary":          "<one sentence>",
      "reasoning":        "<2-3 sentences>",
      "confidence":       "high" | "medium" | "low",
      "disqualified":     "yes" | "no",
      "ecommerce_type":   "<short label or empty>"
    }

================================================================
KNOWN ISSUES + LESSONS BAKED IN
================================================================
- LESSON 1: progress.json every 100 rows → instant status.
- LESSON 2: _summary.csv with 4 cols → Excel-friendly.
- LESSON 3: dedup by website key → no duplicates after restart.
- LESSON 4: _done_keys.db SQLite → 17 min → <1 sec startup.
- LESSON 5: phase 2 default concurrency = 30 (was 10).
- LESSON 6: warn if concurrency > 60.
- LESSON 7: DO NOT write classifier.py from scratch.
            DOWNLOAD from GitHub.
- LESSON 8: skip dead_domain / cloudflare_blocked rows.
- LESSON 9: _qualified.csv with score 7+ at end of run.
- LESSON 10: log raw AI text on JSON parse failure.
- LESSON 11: always truncate homepage_content to 4000 chars.
- LESSON 12: cost + time confirmation for runs > 1000 rows.

- OpenRouter returns ```json fences → strip them.
- Gemini flash lite hits 429 at high concurrency → keep ≤ 60.
- Large CSV files block pandas to_csv() → incremental append.
- Empty content → Tier 2/3 fallback automatically.
- OpenRouter key format is sk-or-v1-... validate before run.
- DuckDuckGo API may return empty for niche companies → fall
  back to name-only gracefully.
