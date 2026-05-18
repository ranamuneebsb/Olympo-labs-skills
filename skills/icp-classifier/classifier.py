"""
ICP Classifier v2.0 — OpenRouter classification with production
lessons baked in. See SKILL.md for the full spec.

Lessons applied in this version:
  1. progress.json every 100 rows (instant status check)
  2. _summary.csv with 4 cols (Excel-friendly)
  3. dedup by website key (no dup rows after restart)
  4. _done_keys.db SQLite (under 1 sec startup, was 17 min)
  5. phase 2 default concurrency = 30 (was 10)
  6. warn if concurrency > 60
  7. THIS FILE IS THE IMPLEMENTATION — do not rewrite it
  8. skip dead_domain / cloudflare_blocked rows
  9. _qualified.csv (score 7+) at end of run
 10. _failed_responses.log on JSON parse failure
 11. always truncate to 4000 chars
 12. cost + time confirmation for runs > 1000 rows
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests


# ----- column auto-detection keys --------------------------------
WEBSITE_KEYS = ["website", "url", "domain", "homepage", "site"]
CONTENT_KEYS = ["homepage_content", "content", "scraped", "body", "text"]
COMPANY_KEYS = ["company", "name", "firm", "organization", "org", "business"]
DESC_KEYS    = ["description", "desc", "about", "summary", "overview"]
LOCATION_KEYS= ["location", "city", "country", "hq", "headquarters", "state"]
SIZE_KEYS    = ["size", "employees", "headcount", "employee_size", "staff"]
STATUS_KEYS  = ["homepage_status", "scrape_status", "status"]

SKIP_STATUSES = {
    "dead_domain", "cloudflare_blocked", "blocked",
    "not_found", "dns_failed", "ssl_failed",
}

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
CONTENT_TRUNCATE_CHARS = 4000  # LESSON 11 — always 4000

OUTPUT_COLS = [
    "icp_score", "icp_summary", "icp_reasoning",
    "icp_confidence", "icp_disqualified", "icp_ecommerce_type",
    "classification_status", "content_source",
]

SUMMARY_COLS = ["Website", "icp_score", "icp_ecommerce_type", "icp_disqualified"]

PROGRESS_WRITE_EVERY = 100  # LESSON 1


# ----- helpers ---------------------------------------------------
def find_col(headers, keys):
    lower = {h.lower(): h for h in headers}
    for h_low, h_orig in lower.items():
        for k in keys:
            if k in h_low:
                return h_orig
    return None


def website_key(value):
    """Normalize a website/url string into a dedup key."""
    if not value:
        return ""
    s = str(value).strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.rstrip("/")
    return s


def search_company(company_name):
    if not company_name:
        return None
    try:
        query = urllib.parse.quote(f"{company_name} company about")
        url = f"https://api.duckduckgo.com/?q={query}&format=json&no_html=1"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
            abstract = data.get("AbstractText", "")
            related = " ".join(
                t.get("Text", "")
                for t in data.get("RelatedTopics", [])[:3]
                if isinstance(t, dict)
            )
            result = f"{abstract} {related}".strip()
            return result if len(result) > 50 else None
    except Exception:
        return None


def get_best_content(row, content_col, desc_col, name_col):
    content = str(row.get(content_col, "")).strip() if content_col else ""
    description = str(row.get(desc_col, "")).strip() if desc_col else ""
    name = str(row.get(name_col, "")).strip() if name_col else ""

    if content and len(content) > 100:
        return content[:CONTENT_TRUNCATE_CHARS], "scraped_content", None
    if description and len(description) > 50:
        return description, "description_only", "low"
    web = search_company(name)
    if web:
        return web, "web_search", "low"
    return f"Company name: {name}", "name_only", "low"


def extract_json(text):
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def build_prompt(user_prompt, name, description, content, location, size, source):
    return (
        f"{user_prompt}\n\n"
        "---\n"
        "COMPANY DATA:\n"
        f"Company Name: {name}\n"
        f"Description: {description}\n"
        f"Website Content: {content}\n"
        f"Location: {location}\n"
        f"Size: {size}\n"
        f"Content Source: {source}\n"
        "---\n\n"
        "Return ONLY valid JSON with these fields:\n"
        "{\n"
        '  "score": <1-10>,\n'
        '  "summary": "<one sentence>",\n'
        '  "reasoning": "<2-3 sentences>",\n'
        '  "confidence": "high" | "medium" | "low",\n'
        '  "disqualified": "yes" | "no",\n'
        '  "ecommerce_type": "<short label or empty string>"\n'
        "}\n"
    )


# ----- failed-response logging (LESSON 10) -----------------------
def log_failed_response(failed_log_path, row_index, website, raw_text):
    try:
        with open(failed_log_path, "a", encoding="utf-8") as f:
            f.write(
                f"[{datetime.utcnow().isoformat()}Z] "
                f"row={row_index} website={website}\n"
                "--- raw response ---\n"
                f"{raw_text}\n"
                "--- end ---\n\n"
            )
    except Exception:
        pass


# ----- classification --------------------------------------------
def classify_one(api_key, model, user_prompt, row, cols, timeout=60):
    content_col, desc_col, name_col, location_col, size_col = cols
    content, source, forced_conf = get_best_content(row, content_col, desc_col, name_col)
    name = str(row.get(name_col, "")).strip() if name_col else ""
    desc = str(row.get(desc_col, "")).strip() if desc_col else ""
    location = str(row.get(location_col, "")).strip() if location_col else ""
    size = str(row.get(size_col, "")).strip() if size_col else ""

    full = build_prompt(user_prompt, name, desc, content, location, size, source)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": full}],
    }

    try:
        r = requests.post(OPENROUTER_ENDPOINT, headers=headers, json=body, timeout=timeout)
        if r.status_code != 200:
            return {"status": "failed",
                    "error": f"HTTP {r.status_code}",
                    "source": source,
                    "raw": r.text[:2000]}
        text = r.json()["choices"][0]["message"]["content"]
        parsed = extract_json(text)
        if not parsed:
            return {"status": "failed", "error": "invalid JSON",
                    "source": source, "raw": text[:2000]}
        if forced_conf:
            parsed["confidence"] = forced_conf
            note = {
                "description_only": "Classified from description only",
                "web_search": "Classified from web search only",
                "name_only": "Classified from name only",
            }.get(source, "")
            if note:
                existing = (parsed.get("reasoning") or "").rstrip(".")
                parsed["reasoning"] = (
                    f"{existing}. ({note})" if existing else note
                )
        return {"status": "ok", "data": parsed, "source": source}
    except Exception as e:
        return {"status": "failed", "error": str(e), "source": source}


# ----- file IO ---------------------------------------------------
def append_row(output_path, headers, row):
    new = not os.path.exists(output_path)
    with open(output_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)
        f.flush()


def append_summary(summary_path, website, row):
    new = not os.path.exists(summary_path)
    with open(summary_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_COLS, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow({
            "Website": website,
            "icp_score": row.get("icp_score", ""),
            "icp_ecommerce_type": row.get("icp_ecommerce_type", ""),
            "icp_disqualified": row.get("icp_disqualified", ""),
        })


# ----- SQLite done-keys (LESSON 4) -------------------------------
def open_done_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS done_keys (key TEXT PRIMARY KEY)"
    )
    conn.commit()
    return conn


def load_done_keys(conn):
    cur = conn.execute("SELECT key FROM done_keys")
    return {row[0] for row in cur.fetchall()}


def mark_done(conn, key):
    if not key:
        return
    try:
        conn.execute("INSERT OR IGNORE INTO done_keys (key) VALUES (?)", (key,))
        conn.commit()
    except sqlite3.Error:
        pass


# ----- progress.json (LESSON 1) ----------------------------------
def write_progress(progress_path, stats):
    stats = dict(stats)
    stats["last_updated"] = datetime.utcnow().isoformat() + "Z"
    tmp = progress_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)
        os.replace(tmp, progress_path)
    except Exception:
        pass


# ----- cost + time estimate (LESSON 12) --------------------------
def estimate_cost_and_time(pending_count, concurrency, model):
    """Rough estimate for gemini-2.5-flash-lite default pricing."""
    input_tokens_per_row = 1500
    output_tokens_per_row = 200
    if "flash-lite" in model:
        in_price = 0.10 / 1_000_000
        out_price = 0.40 / 1_000_000
    elif "flash" in model:
        in_price = 0.30 / 1_000_000
        out_price = 2.50 / 1_000_000
    else:
        in_price = 1.00 / 1_000_000
        out_price = 3.00 / 1_000_000

    total_in = pending_count * input_tokens_per_row
    total_out = pending_count * output_tokens_per_row
    cost = total_in * in_price + total_out * out_price

    rows_per_sec = max(1.0, concurrency * 0.7)
    minutes = pending_count / rows_per_sec / 60.0
    return total_in, total_out, cost, minutes


def confirm_run(pending_count, concurrency, model, skip_confirm):
    if pending_count <= 1000 or skip_confirm:
        return True
    tin, tout, cost, minutes = estimate_cost_and_time(
        pending_count, concurrency, model
    )
    print()
    print("=" * 60)
    print(f"  Total pending:     {pending_count:,} rows")
    print(f"  Model:             {model}")
    print(f"  Estimated tokens:  ~{tin:,.0f} in / ~{tout:,.0f} out")
    print(f"  Estimated cost:    ~${cost:,.2f}")
    print(f"  Estimated time:    ~{minutes:.1f} min at concurrency {concurrency}")
    print("=" * 60)
    try:
        ans = input('  Type "yes" to continue: ').strip().lower()
    except EOFError:
        ans = ""
    return ans == "yes"


# ----- phase runner ----------------------------------------------
def run_phase(api_key, model, prompt, rows, indices, cols, website_col,
              concurrency, output_path, summary_path, out_headers,
              done_conn, progress_path, failed_log_path,
              global_stats, phase):
    failures = []
    total = len(indices)
    if total == 0:
        return failures

    done = failed = 0
    score_buckets = {i: 0 for i in range(1, 11)}
    sources_count = {"scraped_content": 0, "description_only": 0,
                     "web_search": 0, "name_only": 0}
    start = time.time()

    def task(idx):
        return idx, classify_one(api_key, model, prompt, rows[idx], cols)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(task, idx) for idx in indices]
        for fut in as_completed(futures):
            idx, result = fut.result()
            row = rows[idx]
            done += 1
            website_val = row.get(website_col, "") if website_col else ""
            key = website_key(website_val) or (
                (row.get("__name_key__") or "").lower()
            )

            if result["status"] == "ok":
                d = result["data"]
                row.update({
                    "icp_score": d.get("score", ""),
                    "icp_summary": d.get("summary", ""),
                    "icp_reasoning": d.get("reasoning", ""),
                    "icp_confidence": d.get("confidence", ""),
                    "icp_disqualified": d.get("disqualified", ""),
                    "icp_ecommerce_type": d.get("ecommerce_type", ""),
                    "classification_status": "ok",
                    "content_source": result["source"],
                })
                try:
                    s = int(d.get("score", 0))
                    if 1 <= s <= 10:
                        score_buckets[s] += 1
                        if s >= 7:
                            global_stats["score_7_plus"] += 1
                except (ValueError, TypeError):
                    pass
                sources_count[result["source"]] = sources_count.get(result["source"], 0) + 1
                append_row(output_path, out_headers, row)
                append_summary(summary_path, website_val, row)
                mark_done(done_conn, key)
                global_stats["rows_ok"] += 1
            else:
                failed += 1
                failures.append(idx)
                global_stats["rows_failed"] += 1
                if result.get("error") == "invalid JSON" and result.get("raw"):
                    log_failed_response(
                        failed_log_path, idx, website_val, result["raw"]
                    )

            global_stats["rows_done"] += 1

            if global_stats["rows_done"] % PROGRESS_WRITE_EVERY == 0:
                write_progress(progress_path, global_stats)

            if done % 5 == 0 or done == total:
                ok7 = sum(score_buckets[s] for s in range(7, 11))
                below7 = sum(score_buckets[s] for s in range(1, 7))
                elapsed = time.time() - start
                rate = done / elapsed if elapsed > 0 else 0
                remaining = (total - done) / rate if rate > 0 else 0
                eta = f"{remaining / 60:.1f}min"
                print(
                    f"\rPhase {phase}: {done}/{total} | "
                    f"7+: {ok7} | <7: {below7} | Failed: {failed} | "
                    f"S:{sources_count.get('scraped_content', 0)} "
                    f"D:{sources_count.get('description_only', 0)} "
                    f"W:{sources_count.get('web_search', 0)} "
                    f"N:{sources_count.get('name_only', 0)} | "
                    f"ETA: {eta}",
                    end="", flush=True,
                )
                if done % 50 == 0:
                    dist = " ".join(f"{i}:{score_buckets[i]}" for i in range(10, 0, -1))
                    print(f"\n  Score dist: {dist}")
    print()
    write_progress(progress_path, global_stats)
    return failures


# ----- qualified CSV (LESSON 9) ----------------------------------
def write_qualified_csv(output_path, qualified_path, drop_cols=("homepage_content",)):
    if not os.path.exists(output_path):
        return 0
    count = 0
    drop = {c.lower() for c in drop_cols}
    with open(output_path, newline="", encoding="utf-8") as fin:
        reader = csv.DictReader(fin)
        out_headers = [h for h in (reader.fieldnames or []) if h.lower() not in drop]
        with open(qualified_path, "w", newline="", encoding="utf-8") as fout:
            writer = csv.DictWriter(fout, fieldnames=out_headers, extrasaction="ignore")
            writer.writeheader()
            for row in reader:
                try:
                    score = int((row.get("icp_score") or "").strip())
                except (ValueError, TypeError):
                    continue
                if score >= 7:
                    writer.writerow({k: row.get(k, "") for k in out_headers})
                    count += 1
    return count


# ----- main ------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ICP Classifier v2.0")
    parser.add_argument("csv_path", nargs="?",
                        help="Path to input CSV (positional).")
    parser.add_argument("--csv-path", dest="csv_path_flag",
                        help="Alternate flag for CSV path.")
    parser.add_argument("--openrouter-api-key", default=None,
                        help="OpenRouter key; defaults to env OPENROUTER_API_KEY.")
    parser.add_argument("--icp-prompt", required=True,
                        help="Prompt text or path to .txt file.")
    parser.add_argument("--model", default="google/gemini-2.5-flash-lite")
    parser.add_argument("--concurrency", type=int, default=30,
                        help="Phase 1 concurrency. Default 30, max recommended 60.")
    parser.add_argument("--concurrent-requests", dest="concurrency_legacy",
                        type=int, default=None,
                        help="Legacy alias for --concurrency.")
    parser.add_argument("--phase2-concurrency", type=int, default=30,
                        help="Phase 2 retry concurrency. Default 30 (was 10 in v1.0).")
    parser.add_argument("--skip-confirm", action="store_true",
                        help="Skip the >1000-row cost confirmation.")
    parser.add_argument("--website-col", default=None,
                        help="Override website column auto-detect.")
    args = parser.parse_args()

    csv_path = args.csv_path or args.csv_path_flag
    if not csv_path:
        print("ERROR: CSV path required (positional or --csv-path).")
        sys.exit(1)
    if not os.path.exists(csv_path):
        print(f"ERROR: CSV not found: {csv_path}")
        sys.exit(1)

    api_key = args.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not set.")
        print('Run:  setx OPENROUTER_API_KEY "your_key_here"')
        print("then close and reopen the terminal.")
        sys.exit(1)
    if not api_key.startswith("sk-or-v1-"):
        print("WARNING: OpenRouter key doesn't start with sk-or-v1-")

    if os.path.exists(args.icp_prompt) and args.icp_prompt.lower().endswith(".txt"):
        with open(args.icp_prompt, encoding="utf-8") as f:
            prompt = f.read()
    else:
        prompt = args.icp_prompt

    concurrency = args.concurrency_legacy or args.concurrency
    if concurrency > 60:
        print(f"WARNING: concurrency {concurrency} > 60. "
              "Expect rate-limit failures on gemini-flash-lite. "
              "Recommended max = 60.")
    concurrency = min(max(1, concurrency), 100)
    phase2_concurrency = min(max(1, args.phase2_concurrency), 100)

    folder = os.path.dirname(os.path.abspath(csv_path))
    base = os.path.splitext(os.path.basename(csv_path))[0]
    output_path        = os.path.join(folder, f"{base}_classified.csv")
    summary_path       = os.path.join(folder, f"{base}_summary.csv")
    qualified_path     = os.path.join(folder, f"{base}_qualified.csv")
    progress_path      = os.path.join(folder, f"{base}_progress.json")
    failed_log_path    = os.path.join(folder, f"{base}_failed_responses.log")
    done_db_path       = os.path.join(folder, f"{base}_done_keys.db")

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        in_headers = reader.fieldnames or []

    website_col  = args.website_col or find_col(in_headers, WEBSITE_KEYS)
    content_col  = find_col(in_headers, CONTENT_KEYS)
    name_col     = find_col(in_headers, COMPANY_KEYS)
    desc_col     = find_col(in_headers, DESC_KEYS)
    location_col = find_col(in_headers, LOCATION_KEYS)
    size_col     = find_col(in_headers, SIZE_KEYS)
    status_col   = find_col(in_headers, STATUS_KEYS)

    print(f"Website column:     {website_col}")
    print(f"Content column:     {content_col}")
    print(f"Company column:     {name_col}")
    print(f"Description column: {desc_col}")
    print(f"Location column:    {location_col}")
    print(f"Size column:        {size_col}")
    print(f"Status column:      {status_col}")

    out_headers = list(in_headers)
    for col in OUTPUT_COLS:
        if col not in out_headers:
            out_headers.append(col)

    # LESSON 4: load done keys from SQLite (fast)
    print(f"Loading done-keys DB: {done_db_path}")
    t0 = time.time()
    done_conn = open_done_db(done_db_path)
    done_keys = load_done_keys(done_conn)
    print(f"  Loaded {len(done_keys):,} keys in {time.time() - t0:.2f}s")

    # Bucket pending rows (LESSONS 3 + 8)
    pending = []
    skipped_dead = 0
    for i, row in enumerate(rows):
        # Issue 8: skip dead/blocked rows
        if status_col:
            status_val = (row.get(status_col, "") or "").strip().lower()
            if status_val in SKIP_STATUSES:
                key = website_key(row.get(website_col, "") if website_col else "")
                if key and key in done_keys:
                    continue
                row.update({
                    "icp_score": "",
                    "icp_summary": "",
                    "icp_reasoning": f"Skipped: homepage_status={status_val}",
                    "icp_confidence": "",
                    "icp_disqualified": "",
                    "icp_ecommerce_type": "",
                    "classification_status": "skipped",
                    "content_source": "skipped",
                })
                append_row(output_path, out_headers, row)
                append_summary(
                    summary_path,
                    row.get(website_col, "") if website_col else "",
                    row,
                )
                mark_done(done_conn, key)
                skipped_dead += 1
                continue

        # Issue 3: dedup by website key (with company-name fallback)
        web_val = row.get(website_col, "") if website_col else ""
        key = website_key(web_val)
        if not key and name_col:
            key = (row.get(name_col, "") or "").strip().lower()
        row["__name_key__"] = (row.get(name_col, "") or "").strip().lower() if name_col else ""
        if key and key in done_keys:
            continue
        pending.append(i)

    total_rows = len(rows)
    print(f"Total rows:      {total_rows:,}")
    print(f"Already done:    {len(done_keys):,}")
    print(f"Skipped (dead):  {skipped_dead:,}")
    print(f"Pending:         {len(pending):,}")

    if not pending:
        print("Nothing to do.")
        # Still produce qualified CSV
        n_qual = write_qualified_csv(output_path, qualified_path)
        print(f"Qualified rows (score 7+): {n_qual}")
        return

    # LESSON 12: cost + time confirmation
    if not confirm_run(len(pending), concurrency, args.model, args.skip_confirm):
        print("Aborted by user.")
        return

    cols = (content_col, desc_col, name_col, location_col, size_col)
    global_stats = {
        "rows_done": 0,
        "rows_ok": 0,
        "rows_failed": 0,
        "score_7_plus": 0,
        "total_rows": total_rows,
        "pending": len(pending),
    }
    write_progress(progress_path, global_stats)

    failures = run_phase(
        api_key, args.model, prompt, rows, pending, cols, website_col,
        concurrency=concurrency,
        output_path=output_path, summary_path=summary_path,
        out_headers=out_headers,
        done_conn=done_conn, progress_path=progress_path,
        failed_log_path=failed_log_path,
        global_stats=global_stats, phase=1,
    )

    if failures:
        time.sleep(5)
        # LESSON 5: phase 2 default concurrency = 30
        still_failed = run_phase(
            api_key, args.model, prompt, rows, failures, cols, website_col,
            concurrency=phase2_concurrency,
            output_path=output_path, summary_path=summary_path,
            out_headers=out_headers,
            done_conn=done_conn, progress_path=progress_path,
            failed_log_path=failed_log_path,
            global_stats=global_stats, phase=2,
        )
        for idx in still_failed:
            row = rows[idx]
            row.update({
                "icp_score": "",
                "icp_summary": "",
                "icp_reasoning": "",
                "icp_confidence": "",
                "icp_disqualified": "",
                "icp_ecommerce_type": "",
                "classification_status": "classification_failed",
                "content_source": "",
            })
            append_row(output_path, out_headers, row)
            append_summary(
                summary_path,
                row.get(website_col, "") if website_col else "",
                row,
            )

    # LESSON 9: emit qualified CSV
    print("Writing qualified CSV (score 7+)...")
    n_qual = write_qualified_csv(output_path, qualified_path)

    write_progress(progress_path, global_stats)
    print()
    print(f"Done.")
    print(f"  Output:    {output_path}")
    print(f"  Summary:   {summary_path}")
    print(f"  Qualified: {qualified_path}  ({n_qual} rows score 7+)")
    print(f"  Progress:  {progress_path}")
    print(f"  Done DB:   {done_db_path}")
    if os.path.exists(failed_log_path):
        print(f"  Failed log: {failed_log_path}")


if __name__ == "__main__":
    main()
