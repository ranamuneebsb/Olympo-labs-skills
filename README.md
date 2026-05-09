# Olympo Clay Skills

Reusable Claude Code skills for OlympoLabs
lead generation workflows.

## How to Use Next Time

Open Claude Code and paste:

### For Scraping:
"Read this skill and execute it:
https://raw.githubusercontent.com/ranamuneebsb/Olympo-labs-skills/main/skills/zenrows-scraper/SKILL.md

Inputs:
- csv_path: D:\My Campaign\companies.csv
- zenrows_api_key: YOUR_ZENROWS_KEY
- concurrent_requests: 30"

### For ICP Classification:
"Read this skill and execute it:
https://raw.githubusercontent.com/ranamuneebsb/Olympo-labs-skills/main/skills/icp-classifier/SKILL.md

Inputs:
- csv_path: D:\My Campaign\companies_scraped.csv
- openrouter_api_key: YOUR_OPENROUTER_KEY
- icp_prompt: [paste your full ICP prompt here]"

## Available Skills

| Skill | What It Does |
|-------|-------------|
| zenrows-scraper | 3-phase scraping with auto-retry |
| icp-classifier | ICP scoring with any prompt |

## Key Features
- Auto-detects CSV columns — no mapping needed
- 3-phase scraping with automatic JS render retry
- Resume safe — restart anytime, no data lost
- Incremental saves — no blocking on large files
- Missing content fallback: description →
  web search → name only
- Works with any ICP prompt you provide

## Supported ICP Types
Any — just paste your prompt.
Pre-built prompts available for:
- Private Equity firms
- Robotics manufacturers
- Branded merch companies
- Nonprofits (good/bad classifier)
- PR firms
