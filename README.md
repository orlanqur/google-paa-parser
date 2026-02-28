# Google PAA Parser

Extracts questions and answers from Google's **"People Also Ask"** (PAA) blocks.

For each search query, the parser:
1. Opens Google Search
2. Finds the PAA block
3. Clicks on questions to expand them (new questions appear dynamically)
4. Extracts each question-answer pair **immediately after click** (before Google collapses it)
5. Exports results to XLSX and/or JSON

## Features

- **High answer rate** (~100%) — reads answers immediately after each click
- **Interactive mode** — prompts for queries, language, and region when run without arguments
- **Language & region support** — `--hl` and `--gl` flags for any Google locale
- **Auto captcha solving** — optional API integration (2Captcha, rucaptcha, CapGuru)
- **Headless mode** — run without browser window
- **Checkpoint & resume** — auto-saves progress every 5 queries; resume after crash/captcha with `--resume`
- **Deduplication** — skips duplicate questions across queries
- **Captcha detection** — auto-solve via API, or pause for manual resolution
- **Cross-platform** — works on macOS, Windows, and Linux

## Requirements

- Python 3.10+
- Google Chrome installed
- ChromeDriver is downloaded automatically via `webdriver-manager`

## Installation

```bash
git clone https://github.com/orlanqur/google-paa-parser.git
cd google-paa-parser
pip install -r requirements.txt
```

## Usage

### Interactive mode (no arguments needed)

```bash
python google_paa_parser.py
```

The script will prompt you for:
1. **Queries** — paste them one per line, or enter a path to a file
2. **Language & region** — choose from presets (ru, en, de, fr, ...) or enter custom `hl=xx gl=yy`

### CLI mode

```bash
# Custom input/output files
python google_paa_parser.py -i my_queries.txt -o my_results.xlsx

# Russian Google
python google_paa_parser.py -i queries.txt --hl ru --gl ru

# English (US)
python google_paa_parser.py -i queries.txt --hl en --gl us

# More questions per query (default: 15)
python google_paa_parser.py -i queries.txt --clicks 20

# Headless mode (no browser window, faster)
python google_paa_parser.py -i queries.txt --headless

# Auto-solve captchas via 2Captcha API
python google_paa_parser.py -i queries.txt --captcha-key YOUR_API_KEY

# Auto-solve via rucaptcha or CapGuru
python google_paa_parser.py -i queries.txt --captcha-key KEY --captcha-service rucaptcha
python google_paa_parser.py -i queries.txt --captcha-key KEY --captcha-service capguru

# Resume after crash or captcha
python google_paa_parser.py --resume

# Combine options
python google_paa_parser.py -i queries.txt --hl ru --gl ru --clicks 20 --headless --captcha-key KEY
```

### All flags

| Flag | Default | Description |
|------|---------|-------------|
| `-i`, `--input` | *(interactive)* | Input file (one query per line) |
| `-o`, `--output` | `results.xlsx` | Output file (.xlsx or .json) |
| `--hl` | *(interactive / en)* | Google interface language |
| `--gl` | *(interactive / us)* | Google country/region |
| `--clicks` | `15` | Max questions to expand per query |
| `--headless` | off | Run without browser window |
| `--resume` | off | Continue from last checkpoint |
| `--pause-min` | `10` | Min pause between queries (seconds) |
| `--pause-max` | `20` | Max pause between queries (seconds) |
| `--captcha-key` | *(none)* | API key for captcha solving (or env `CAPTCHA_API_KEY`) |
| `--captcha-service` | `2captcha` | Captcha service: `2captcha`, `rucaptcha`, or `capguru` |

## Output format

### XLSX

| Column | Description |
|--------|-------------|
| Query | Original search query |
| Question | PAA question text |
| Answer | Full answer with sources |

### JSON

```json
[
  {
    "query": "what is bitcoin",
    "question": "How does Bitcoin work?",
    "answer": "Bitcoin is a decentralized digital currency..."
  }
]
```

A JSON file is always saved alongside XLSX for reliability.

## How it works

Google's PAA block shows 4 initial questions. When you click one, the answer expands and 2-3 new questions appear. The parser exploits this to collect 15-30+ unique Q&A pairs per query.

Key implementation detail: answers are read **immediately after each click**. Google collapses previously expanded answers when a new one opens, so batch extraction at the end would miss ~70% of answers.

## Captcha handling

Google may show a captcha after many queries from the same IP. The parser:

1. Detects captcha automatically
2. **With `--captcha-key`**: sends reCAPTCHA to solving API, injects token, continues automatically
3. **Without API key**: pauses and waits up to 5 minutes for manual resolution (non-headless mode)
4. After 3 consecutive captchas: saves checkpoint and stops
5. Use `--resume` to continue after solving captcha or changing IP

Supported captcha services (all use the same 2captcha-compatible protocol):
- [2captcha.com](https://2captcha.com) — international
- [rucaptcha.com](https://rucaptcha.com) — Russian interface
- [cap.guru](https://cap.guru) — budget option

Tips to avoid captchas:
- Keep default pauses (10-20s between queries)
- Don't run more than 50-100 queries per session
- Use `--headless` (slightly lower detection rate)

## Limitations

- PAA blocks are not available for every query (especially brand queries in some regions)
- Google may change DOM selectors — fallback selectors are included
- No proxy support (yet) — use VPN for IP rotation if needed
- Answers may include "AI Overview" content from Google's AI features

## Changelog (v1 → v2)

| # | What | v1 (original) | v2 (rewrite) |
|---|------|---------------|--------------|
| 1 | **Answer extraction** | Batch at the end — **~31% answers** | After each click — **~100% answers** |
| 2 | **Browser sessions** | New Chrome per query (+10s overhead) | Single session, reused across all queries |
| 3 | **Language/region** | None (depends on IP) | `--hl` / `--gl` flags for any Google locale |
| 4 | **Interactive mode** | None | Prompts for queries + language + region at startup |
| 5 | **Captcha handling** | Silent crash | Auto-solve via API (2captcha/rucaptcha/capguru) + manual fallback + auto-stop after 3 |
| 6 | **Crash recovery** | None (all data lost) | Checkpoint every 5 queries + `--resume` |
| 7 | **Headless mode** | None | `--headless` flag |
| 8 | **Deduplication** | Same question repeated across queries | Skips duplicates by question text |
| 9 | **Output format** | XLSX only | XLSX + JSON (always both) |
| 10 | **CLI arguments** | None (edit source code) | Full `argparse` with all options |
| 11 | **Logging** | `print()` | `logging` with timestamps |
| 12 | **Pause control** | Hardcoded 15-25s | `--pause-min` / `--pause-max` |
| 13 | **File paths** | Hardcoded `C:\py-projects\...` (Windows-only) | `pathlib` — cross-platform (macOS/Win/Linux) |
| 14 | **Cookie consent** | Single hardcoded selector | Multiple fallback selectors |
| 15 | **PAA detection** | Single CSS selector | Primary + 2 fallback selectors |

### Summary

The original script collected answers for only **31% of questions** (batch extraction at the end, after Google collapsed them). The rewrite reads each answer **immediately after click**, achieving **~100% answer rate**. Added interactive startup, auto-captcha solving, crash recovery, deduplication, headless mode, and cross-platform support — turning a fragile Windows-only script into a production-ready CLI tool.

## License

MIT
