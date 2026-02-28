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
- **Language & region support** — `--hl` and `--gl` flags for any Google locale
- **Headless mode** — run without browser window
- **Checkpoint & resume** — auto-saves progress every 5 queries; resume after crash/captcha with `--resume`
- **Deduplication** — skips duplicate questions across queries
- **Captcha detection** — pauses and waits for manual resolution (in non-headless mode)
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

### Basic

```bash
# Create a queries file (one query per line)
cp queries_example.txt queries.txt
# Edit queries.txt with your queries

# Run
python google_paa_parser.py
```

### Options

```bash
# Custom input/output files
python google_paa_parser.py -i my_queries.txt -o my_results.xlsx

# Russian Google
python google_paa_parser.py --hl ru --gl ru

# English (US)
python google_paa_parser.py --hl en --gl us

# More questions per query (default: 15)
python google_paa_parser.py --clicks 20

# Headless mode (no browser window, faster)
python google_paa_parser.py --headless

# Faster (shorter pauses, higher captcha risk)
python google_paa_parser.py --pause-min 5 --pause-max 10

# Resume after crash or captcha
python google_paa_parser.py --resume

# Combine options
python google_paa_parser.py --hl ru --gl ru --clicks 20 --headless -o results_ru.xlsx
```

### All flags

| Flag | Default | Description |
|------|---------|-------------|
| `-i`, `--input` | `queries.txt` | Input file (one query per line) |
| `-o`, `--output` | `results.xlsx` | Output file (.xlsx or .json) |
| `--hl` | `en` | Google interface language |
| `--gl` | `us` | Google country/region |
| `--clicks` | `15` | Max questions to expand per query |
| `--headless` | off | Run without browser window |
| `--resume` | off | Continue from last checkpoint |
| `--pause-min` | `10` | Min pause between queries (seconds) |
| `--pause-max` | `20` | Max pause between queries (seconds) |

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
2. In non-headless mode: pauses and waits up to 5 minutes for you to solve it manually
3. After 3 consecutive captchas: saves checkpoint and stops
4. Use `--resume` to continue after solving captcha or changing IP

Tips to avoid captchas:
- Keep default pauses (10-20s between queries)
- Don't run more than 50-100 queries per session
- Use `--headless` (slightly lower detection rate)

## Limitations

- PAA blocks are not available for every query (especially brand queries in some regions)
- Google may change DOM selectors — fallback selectors are included
- No proxy support (yet) — use VPN for IP rotation if needed
- Answers may include "AI Overview" content from Google's AI features

## License

MIT
