SKILL NAME: ICP Classifier
VERSION: 1.0
AUTHOR: OlympoLabs

DESCRIPTION:
Classifies companies in a CSV for ICP fit
using OpenRouter AI. User provides the CSV,
the ICP prompt, and API key. That is all.

SETUP (run once):
Windows: Double-click setup.bat
Mac/Linux: bash setup.sh
Or manually: pip install requests

USER INPUTS (only 3 required):
- csv_path: full path to CSV including folder
- openrouter_api_key: OpenRouter API key
- icp_prompt: the full classification prompt
  user pastes it or provides path to .txt file

OPTIONAL:
- model: default google/gemini-2.5-flash-lite
- concurrent_requests: default 30

THAT IS ALL THE USER NEEDS TO PROVIDE.
Script auto-handles everything else.

HOW TO RUN:
The implementation is in classifier.py next to this file.
Invoke it with:

    python classifier.py \
      --csv-path "<csv_path>" \
      --openrouter-api-key "<openrouter_api_key>" \
      --icp-prompt "<prompt text or path to .txt file>" \
      [--model "<model>"] \
      [--concurrent-requests <n>]

Required Python packages: requests

AUTO-DETECTION LOGIC:
Script scans CSV headers and finds columns:

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

OUTPUT FILE:
Saved to same folder as input CSV
Named: [original_filename]_classified.csv

---

CLASSIFIER.PY BEHAVIOR:

MISSING CONTENT FALLBACK — 3 TIER SYSTEM:

TIER 1 — Homepage content available (best):
- Use homepage_content as primary AI input
- Full classification
- confidence based on content quality

TIER 2 — No content but description available:
- Use description as primary input
- Force confidence = "low"
- Add to reasoning: "Classified from description only"

TIER 3 — No content AND no description:
- Use DuckDuckGo instant answer API (free, no key):
  https://api.duckduckgo.com/?q={query}&format=json
- Search query: "{company_name} company about"
- Extract AbstractText and RelatedTopics
- Use as context for AI classification
- Force confidence = "low"
- Add to reasoning: "Classified from web search only"
- If web search empty → use company name only
- Force confidence = "low"
- Add to reasoning: "Classified from name only"

WEB SEARCH IMPLEMENTATION:

import urllib.request
import urllib.parse
import json

def search_company(company_name):
    query = urllib.parse.quote(
        f"{company_name} company about"
    )
    url = (
        f"https://api.duckduckgo.com/"
        f"?q={query}&format=json&no_html=1"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
            abstract = data.get("AbstractText", "")
            related = " ".join([
                t.get("Text","")
                for t in data.get("RelatedTopics",[])[:3]
                if isinstance(t, dict)
            ])
            result = f"{abstract} {related}".strip()
            return result if len(result) > 50 else None
    except:
        return None

CONTENT SELECTION LOGIC:

def get_best_content(row, content_col, desc_col, name_col):
    content = str(row.get(content_col, "")).strip()
    description = str(row.get(desc_col, "")).strip()
    name = str(row.get(name_col, "")).strip()

    # Tier 1: scraped content
    if content and len(content) > 100:
        return content, "scraped_content", None

    # Tier 2: description
    if description and len(description) > 50:
        return description, "description_only", "low"

    # Tier 3: web search
    web_result = search_company(name)
    if web_result:
        return web_result, "web_search", "low"

    # Final fallback: name only
    return f"Company name: {name}", "name_only", "low"

PROMPT HANDLING:
Script wraps user ICP prompt with company data:

  [USER ICP PROMPT]

  ---
  COMPANY DATA:
  Company Name: {name}
  Description: {description}
  Website Content: {content}
  Location: {location}
  Size: {size}
  Content Source: {content_source}
  ---

  Return ONLY valid JSON:
  {
    "score": <1-10>,
    "summary": "<one sentence>",
    "reasoning": "<2-3 sentences>",
    "confidence": "high" | "medium" | "low",
    "disqualified": "yes" | "no"
  }

CONTENT TRUNCATION:
- Truncate homepage_content to 4000 chars
  before sending to AI
- Saves tokens and reduces cost
- 4000 chars is enough for accurate classification

CLASSIFICATION PIPELINE:

PHASE 1 — Main classification:
- Process all rows at full concurrency
- Rows with no content → Tier 2 or Tier 3 fallback

PHASE 2 — Retry failed rows:
- Rows where AI returned invalid JSON or error
- Wait 5 seconds then retry at concurrency 10
- If still fails → mark classification_failed

JSON PARSING:
- Strip markdown code fences before parsing
  AI sometimes returns ```json ... ```
- Strip leading/trailing whitespace
- Extract JSON using regex: find first { to last }
- If JSON invalid → retry once
- If still invalid → mark classification_failed
  log raw response for debugging

AUTOSAVE:
- Use incremental row-by-row append
  NOT pandas to_csv() — causes blocking
- Append each row immediately after processing
- Write header once at start

RESUME LOGIC:
- If output file exists → load it
- Skip rows that already have a valid score
- Only process rows without a score
- Safe to stop and restart anytime

PROGRESS DISPLAY:
Classified: X/Y | Score 7+: A | Below 7: B | Failed: C
Content sources: Scraped: A | Desc: B | Web: C | Name: D
ETA: Xmin

Score distribution shown every 50 rows:
10:X 9:X 8:X 7:X 6:X 5:X 4:X 3:X 2:X 1:X

OUTPUT COLUMNS ADDED:
- icp_score (1-10)
- icp_summary (one sentence)
- icp_reasoning (2-3 sentences)
- icp_confidence (high/medium/low)
- icp_disqualified (yes/no)
- classification_status (ok/classification_failed)
- content_source (scraped_content/description_only/
                  web_search/name_only)

KNOWN ISSUES TO HANDLE:
- OpenRouter returns ```json fences → strip them
- Gemini flash lite hits 429 at high concurrency →
  Phase 2 retries at lower concurrency automatically
- Large CSV files block pandas to_csv() →
  use incremental append (critical)
- Empty content → Tier 2/3 fallback automatically
- Model returns extra text before JSON →
  extract using regex: find first { to last }
- Windows socket limits → keep concurrency at 30
- OpenRouter key format is sk-or-v1-...
  validate key format before starting run
- DuckDuckGo API may return empty for niche companies
  → fall back to name-only gracefully
