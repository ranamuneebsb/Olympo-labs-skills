"""
ICP Classifier - 3-tier content fallback + OpenRouter classification.
See SKILL.md for the full spec.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


CONTENT_KEYS = ["homepage_content", "content", "scraped", "body", "text"]
COMPANY_KEYS = ["company", "name", "firm", "organization", "org", "business"]
DESC_KEYS = ["description", "desc", "about", "summary", "overview"]
LOCATION_KEYS = ["location", "city", "country", "hq", "headquarters", "state"]
SIZE_KEYS = ["size", "employees", "headcount", "employee_size", "staff"]

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

OUTPUT_COLS = [
    "icp_score", "icp_summary", "icp_reasoning",
    "icp_confidence", "icp_disqualified",
    "classification_status", "content_source",
]


def find_col(headers, keys):
    lower = {h.lower(): h for h in headers}
    for h_low, h_orig in lower.items():
        for k in keys:
            if k in h_low:
                return h_orig
    return None


def search_company(company_name):
    if not company_name:
        return None
    try:
        query = urllib.parse.quote(f"{company_name} company about")
        url = (
            f"https://api.duckduckgo.com/"
            f"?q={query}&format=json&no_html=1"
        )
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
        # truncate to 4000 chars to save tokens
        return content[:4000], "scraped_content", None
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
    # strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    # find first { to last }
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
        "Return ONLY valid JSON:\n"
        "{\n"
        '  "score": <1-10>,\n'
        '  "summary": "<one sentence>",\n'
        '  "reasoning": "<2-3 sentences>",\n'
        '  "confidence": "high" | "medium" | "low",\n'
        '  "disqualified": "yes" | "no"\n'
        "}\n"
    )


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
                    "raw": r.text[:500]}
        text = r.json()["choices"][0]["message"]["content"]
        parsed = extract_json(text)
        if not parsed:
            return {"status": "failed", "error": "invalid JSON",
                    "source": source, "raw": text[:500]}
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


def append_row(output_path, headers, row):
    new = not os.path.exists(output_path)
    with open(output_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)
        f.flush()


def load_done_keys(output_path, name_col):
    """Return a set of (row_index_or_name) keys already classified."""
    done = set()
    if not os.path.exists(output_path):
        return done
    with open(output_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            score = (row.get("icp_score") or "").strip()
            if not score:
                continue
            key = (row.get(name_col, "") or "").strip().lower() if name_col else ""
            if key:
                done.add(key)
    return done


def run_phase(api_key, model, prompt, rows, indices, cols,
              concurrency, output_path, out_headers, phase, name_col):
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
            if result["status"] == "ok":
                d = result["data"]
                row.update({
                    "icp_score": d.get("score", ""),
                    "icp_summary": d.get("summary", ""),
                    "icp_reasoning": d.get("reasoning", ""),
                    "icp_confidence": d.get("confidence", ""),
                    "icp_disqualified": d.get("disqualified", ""),
                    "classification_status": "ok",
                    "content_source": result["source"],
                })
                try:
                    s = int(d.get("score", 0))
                    if 1 <= s <= 10:
                        score_buckets[s] += 1
                except (ValueError, TypeError):
                    pass
                sources_count[result["source"]] = sources_count.get(result["source"], 0) + 1
                append_row(output_path, out_headers, row)
            else:
                failed += 1
                failures.append(idx)

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
    return failures


def main():
    parser = argparse.ArgumentParser(description="ICP Classifier")
    parser.add_argument("--csv-path", required=True)
    parser.add_argument("--openrouter-api-key", required=True)
    parser.add_argument("--icp-prompt", required=True,
                        help="Prompt text or path to .txt file")
    parser.add_argument("--model", default="google/gemini-2.5-flash-lite")
    parser.add_argument("--concurrent-requests", type=int, default=30)
    args = parser.parse_args()

    if not args.openrouter_api_key.startswith("sk-or-v1-"):
        print("WARNING: OpenRouter key doesn't start with sk-or-v1-")

    if os.path.exists(args.icp_prompt) and args.icp_prompt.lower().endswith(".txt"):
        with open(args.icp_prompt, encoding="utf-8") as f:
            prompt = f.read()
    else:
        prompt = args.icp_prompt

    if not os.path.exists(args.csv_path):
        print(f"ERROR: CSV not found: {args.csv_path}")
        sys.exit(1)

    folder = os.path.dirname(os.path.abspath(args.csv_path))
    base = os.path.splitext(os.path.basename(args.csv_path))[0]
    output_path = os.path.join(folder, f"{base}_classified.csv")

    with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        in_headers = reader.fieldnames or []

    content_col = find_col(in_headers, CONTENT_KEYS)
    name_col = find_col(in_headers, COMPANY_KEYS)
    desc_col = find_col(in_headers, DESC_KEYS)
    location_col = find_col(in_headers, LOCATION_KEYS)
    size_col = find_col(in_headers, SIZE_KEYS)

    print(f"Content column:     {content_col}")
    print(f"Company column:     {name_col}")
    print(f"Description column: {desc_col}")
    print(f"Location column:    {location_col}")
    print(f"Size column:        {size_col}")

    out_headers = list(in_headers)
    for col in OUTPUT_COLS:
        if col not in out_headers:
            out_headers.append(col)

    done_keys = load_done_keys(output_path, name_col)
    print(f"Already classified: {len(done_keys)}")

    pending = []
    for i, row in enumerate(rows):
        key = (row.get(name_col, "") or "").strip().lower() if name_col else ""
        if key and key in done_keys:
            continue
        pending.append(i)

    print(f"Pending: {len(pending)}")
    if not pending:
        print("Nothing to do.")
        return

    cols = (content_col, desc_col, name_col, location_col, size_col)
    concurrency = min(max(1, args.concurrent_requests), 50)

    failures = run_phase(
        args.openrouter_api_key, args.model, prompt, rows, pending, cols,
        concurrency=concurrency,
        output_path=output_path, out_headers=out_headers,
        phase=1, name_col=name_col,
    )

    if failures:
        time.sleep(5)
        still_failed = run_phase(
            args.openrouter_api_key, args.model, prompt, rows, failures, cols,
            concurrency=10,
            output_path=output_path, out_headers=out_headers,
            phase=2, name_col=name_col,
        )
        for idx in still_failed:
            row = rows[idx]
            row.update({
                "icp_score": "",
                "icp_summary": "",
                "icp_reasoning": "",
                "icp_confidence": "",
                "icp_disqualified": "",
                "classification_status": "classification_failed",
                "content_source": "",
            })
            append_row(output_path, out_headers, row)

    print(f"\nDone. Saved to: {output_path}")


if __name__ == "__main__":
    main()
