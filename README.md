# Strapi-App-Bot (SAB)

**SAB** is a modular platform for collecting, aggregating, and structuring data on crypto projects, with support for automatic website parsing, X (Twitter), collection services like linktr.ee, and data cleanup. It allows centralized configuration management, report generation, scalable data collection, and analysis of any project.

## Key Features

* **Modular Architecture** — plugins for websites, X profiles, and collection services.
* **Flexible Link Handling** — parsing from bios, collection pages, auto-generation and normalization (removing URL tails, unifying formats for YouTube, docs, GitHub, etc.).
* **Centralized Configuration** — all settings and projects managed in a single `config.json`.
* **Multilingual Interface** — easy to add new interface languages.
* **Full Automation** — one-command launch, no manual steps required.
* **Bypass Site Protections** — automatic browser-mode fallback for Cloudflare, JS challenges, and anti-bot systems.
* **Asynchronous High-Speed Processing** — all pipeline stages run in parallel.
* **Data Caching** — minimizes redundant requests and speeds up parsing.
* **Logging** — detailed logs of all actions for debugging and auditing.

## Use Cases

* **Aggregation and monitoring of crypto and IT projects**
* **Automated collection of contact information**
* **Updating project showcases and aggregators**
* **Parsing public profiles and documentation**

## Technology Stack

* **Python** — main development language
* **Requests, BeautifulSoup** — website parsing and data extraction
* **Playwright** — X profile parsing (with fingerprinting)

### Supported Sources

| Source           | Description                               |
| ---------------- | ----------------------------------------- |
| `website`        | Main website of the project               |
| `docs`           | Documentation or whitepaper               |
| `X/Twitter`      | Bio and profile links, avatar             |
| `linktr.ee`/etc. | Collection of all linked social platforms |
| `YouTube`        | Accurate channel-only aggregation         |
| `GitHub`         | Filtering support for org/user only       |

## Architecture

### System Components

1. **Parsers (`core/*.py`)** — wrappers for different sources (websites, collection services, X/Twitter).
2. **Main Entry Point (`config/start.py`)** — orchestrates the pipeline of data collection, normalization, and saving.
3. **Templates (`templates/`)** — define the structure of output data.
4. **Logging (`logs/`)** — records all activity for debugging and monitoring.
5. **Configuration (`config/config.json`)** — all targets, parameters, and settings.

### Project Structure

```
strapi-app-bot/
├── config/
│   ├── apps/
│   │   └── {project}.json         # Individual app configuration
│   ├── config.json                # Central configuration for all projects
│   └── start.py                   # Main pipeline script (entry point)
├── core/
│   ├── api_ai.py                  # AI integration
│   ├── api_strapi.py              # Strapi API integration
│   ├── api_coingecko.py           # CoinGecko API integration
│   ├── browser_fetch.js           # Browser-based website parser
│   ├── install.py                 # Dependency auto-installer
│   ├── log_utils.py               # Logging utilities
│   ├── orchestrator.py            # Main async orchestrator
│   ├── package.json               # Node dependencies
│   ├── seo_utils.py               # SEO field handler
│   ├── status.py                  # Status definitions
│   ├── package-lock.json          # Locked Node dependency versions
│   ├── twitter_parser.js          # X profile parser (Node)
│   └── web_parser.py              # Link parsing module
├── logs/
│   ├── ai.log                     # AI logs
│   ├── host.log                   # Pipeline execution log
│   ├── setup.log                  # Setup and installation logs
│   └── strapi.log                 # Strapi upload logs
├── storage/
│   └── apps/
│       └── {project}/
│           └── main.json          # Parsed project results
├── templates/
│   └── main_template.json         # Template structure for main.json
├── requirements.txt               # Python dependencies
├── README.md                      # Documentation
└── start.sh                       # Bash script for quick startup
```

## Pipeline: How It Works

1. **System Launch**:
   * `start.sh` → `config/start.py` → `core/orchestrator.py`
2. **Automatic Dependency Installation**:
   * `config/start.py` → `core/install.py`:
     * Installs all Python packages (from `requirements.txt`)
     * Installs Node.js modules (for anti-bot and Twitter parsing)
     * Playwright auto-downloads required browsers for headless parsing
3. **Load Configuration and Templates**:
   * Loads the main config (`config/config.json`): targets, settings, categories, API keys
   * Loads the data template `templates/main_template.json` (defines main.json structure)
4. **Asynchronous Data Collection for Each Target**:
   * **Fast Web Parsing:** via `requests` + `BeautifulSoup` for most websites
   * **Site Protection Bypass:** if protection is detected (Cloudflare, JS, anti-bot), switches to `Playwright` + Fingerprint Suite (`core/browser_fetch.js`)
   * **Twitter/X:** always parsed using a dedicated browser module (`core/twitter_parser.js`) to mimic real behavior
   * **Docs, Collection Services, Internal Links:** (e.g. linktr.ee, read.cv) parsed via requests or Playwright
   * **Social and Docs Link Normalization:** detects and standardizes GitHub, Discord, Telegram, Medium, YouTube, LinkedIn, Reddit, and more
   * **HTML Caching:** in-memory caching for speed and reduced load
   * **Asynchronous Parallelism:** all per-project processes (AI generation, CoinGecko, parsing, enrichment) run in parallel (`asyncio` + `ThreadPool`)
   * **Retries and Error Handling:** automatic retries with full logging of each step
5. **AI Generation, Enrichment, and Auto-Categorization**:
   * Auto-generation of short and full descriptions via AI
   * Token/coin info lookup via CoinGecko API (fallback to manual template)
   * **Automatic category generation via AI** → mapping to Strapi IDs and creation of missing categories if needed
6. **Saving Results**:
   * All data is saved to `storage/apps/{app}/{project}/main.json` (or to `storage/total/` if using batch mode)
7. **Publishing and Integration**:
   * Final `main.json` files are **automatically** uploaded to Strapi via API
   * Logos/images are automatically attached in Strapi, SEO fields updated
> **Only run `start.sh` — the bot does the rest!**

## Installation and Launch

```bash
git clone https://github.com/beesyst/strapi-app-bot.git
cd strapi-app-bot
bash start.sh
```

## Configuration Guide

All settings are defined in `config/config.json`:

| Parameter          | Default Value     | Description                                                      |
| ------------------ | ----------------- | ---------------------------------------------------------------- |
| `apps`             | `[ "babylon" ]`   | List of targets (project objects with settings and enabled flag) |
| `enabled`          | `true`            | Flag: if false, the project will be completely skipped           |
| `link_collections` | `[ "linktr.ee" ]` | List of collection services for deep parsing                     |

## Terminal Status Codes

During execution, the bot will show a final status for each project:

* `[add]` — project added for the first time (new main.json created, sent to Strapi)
* `[update]` — project data updated (main.json rewritten and sent to Strapi)
* `[skip]` — data unchanged (nothing sent)
* `[error]` — error occurred during data collection or upload
