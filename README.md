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
│   │   └── {project}.json         # Config of a single application
│   ├── config.json                # Central configuration (all projects, parameters)
│   └── start.py                   # Main pipeline script (entry point, orchestration)
├── core/
│   ├── api/                       # Integrations with external APIs
│   │   ├── ai.py                  # Integration with AI
│   │   ├── coingecko.py           # Integration with CoinGecko
│   │   └── strapi.py              # Integration with Strapi CMS
│   ├── parser/                    # All content parsers
│   │   ├── browser_fetch.js       # Playwright + Fingerprint Suite (anti-bot bypass)
│   │   ├── link_aggregator.py     # Linktree, Read.cv and other aggregators
│   │   ├── twitter_scraper.js     # X/Twitter scraper (Node.js)
│   │   ├── twitter.py             # Python logic for X/Twitter (Nitter-first, fallback Playwright)
│   │   ├── web.py                 # Universal web parser (requests + BeautifulSoup)
│   │   └── youtube.py             # YouTube parser
│   ├── collector.py               # Data collection and aggregation (central coordinator)
│   ├── install.py                 # Auto-install of dependencies (Python + Node + Playwright)
│   ├── log_utils.py               # Centralized logging
│   ├── normalize.py               # Data normalization (common rules)
│   ├── orchestrator.py            # Orchestration (main async pipeline)
│   ├── paths.py                   # Absolute project paths
│   ├── seo_utils.py               # SEO data enrichment
│   └── status.py                  # Pipeline status management
├── logs/
│   ├── ai.log                     # AI log
│   ├── host.log                   # Main host pipeline log
│   ├── setup.log                  # Dependency installation log
│   └── strapi.log                 # Strapi publishing log
├── storage/
│   └── apps/
│       └── {project}/
│           └── main.json          # Parsed project results
├── templates/
│   └── main_template.json         # Template structure for main.json
├── requirements.txt               # Python dependencies
├── README.md                      # Documentation
└── start.sh                       # Bash script for quick pipeline launch
```

## Pipeline: How It Works

1. **System startup**:  
   * `start.sh` → `config/start.py` → `core/orchestrator.py`  
2. **Automatic dependency installation**:  
   * `config/start.py` calls `core/install.py`:  
      * Checks for `venv`, creates it if missing.  
      * Installs Python dependencies (`requirements.txt`).  
      * Installs Node.js modules (`core/package.json`).  
      * Downloads Playwright browsers (`npx playwright install`).  
3. **Loading configuration and templates**:  
   * Loads the main config (`config/config.json`): targets, parameters, API keys.  
   * Loads the template `templates/main_template.json` for a unified `main.json` structure.  
4. **Asynchronous parsing and data collection**:  
   * **Web parsing:** `core/parser/web.py` (requests + BeautifulSoup).  
   * **Anti-bot bypass:** `core/parser/browser_fetch.js` (Playwright + Fingerprint Suite).  
   * **Twitter/X:**  
     - `core/parser/twitter.py` (Nitter-first, fallback Playwright).  
     - `core/parser/twitter_scraper.js` (Node.js scraper).  
   * **Link aggregators:** `core/parser/link_aggregator.py`.  
   * **YouTube and docs:** `core/parser/youtube.py` and others.  
   * **Collector:** `core/collector.py` aggregates results into a single flow.  
   * **Asynchronous execution:** powered by asyncio + ThreadPool.  
   * **HTML caching and retries:** built-in for speed and fault tolerance.  
5. **AI generation and enrichment**:  
   * AI creates short and full descriptions.  
   * CoinGecko API enriches token data (fallback — manual templates).  
   * AI selects categories, which are automatically mapped to Strapi IDs.  
6. **Result storage**:  
   * All data is saved in `storage/apps/{app}/{project}/main.json`.  
   * Logos/avatars from Twitter are stored in `storage/apps/{app}/{project}/`.  
7. **Strapi integration**:  
   * `main.json` is uploaded via Strapi API.  
   * Images/logos are attached automatically.  
   * SEO fields are updated.  

**You only need to run `start.sh` — the bot will handle everything else!**

## Installation and Launch

```bash
git clone https://github.com/beesyst/strapi-app-bot.git
cd strapi-app-bot
bash start.sh
```

## Configuration

All parameters are set in the `config/config.json` file:

### General

| Parameter         | Default value          | Description                                                         |
|-------------------|------------------------|---------------------------------------------------------------------|
| `apps`            | `[ "babylon" ]`        | List of target apps (projects with settings and `enabled` flag)     |
| `enabled`         | `true`                 | Whether the project is enabled (false = fully ignored)              |
| `link_collections`| `[ "linktr.ee" ]`      | Services for deep link parsing                                      |
| `clear_logs`      | `true`                 | Whether to clear logs on startup                                    |

### AI

| Parameter              | Default value           | Description                                                                 |
|------------------------|-------------------------|-----------------------------------------------------------------------------|
| `ai.providers`         | `openai`, `perplexity`  | Configured AI providers and API keys                                        |
| `ai.groups`            | see config              | Prompt/model groups with optional `web_search_options`                      |
| `short_desc`           | `max_len=130`           | Constraints for project description length                                  |
| `seo_short`            | `max_len=50`            | Constraints for short SEO description length                                |

### Strapi

| Parameter              | Default value | Description                                    |
|------------------------|---------------|------------------------------------------------|
| `strapi_sync`          | `true`        | Synchronize data with Strapi                   |
| `strapi_publish`       | `true`        | Automatically publish entries                   |
| `http_timeout_sec`     | `45`          | HTTP timeout for Strapi requests                |
| `http_retries`         | `3`           | Number of retry attempts on errors              |
| `http_backoff`         | `1.7`         | Backoff multiplier between retries              |

### Nitter (X/Twitter parser)

| Parameter                  | Default value     | Description                                       |
|----------------------------|------------------|---------------------------------------------------|
| `nitter_instances`         | list of URLs     | List of available Nitter instances                |
| `nitter_retry_per_instance`| `1`              | Retry count per instance                          |
| `nitter_timeout_sec`       | `14`             | Request timeout                                   |
| `nitter_bad_ttl_sec`       | `600`            | TTL for caching failed attempts (in seconds)      |
| `nitter_enabled`           | `true`           | Enable/disable Nitter usage                       |

### CoinGecko

| Parameter       | Default value                     | Description                   |
|-----------------|-----------------------------------|-------------------------------|
| `api_base`      | `https://api.coingecko.com/api/v3`| CoinGecko API base URL        |

### Other

| Parameter            | Description                                                  |
|----------------------|--------------------------------------------------------------|
| `bad_name_keywords`  | Stopword list for filtering invalid project names            |
| `categories`         | Full list of supported project categories                    |


## Terminal Status Codes

During execution, the bot will show a final status for each project:

* `[add]` — project added for the first time (new main.json created, sent to Strapi)
* `[update]` — project data updated (main.json rewritten and sent to Strapi)
* `[skip]` — data unchanged (nothing sent)
* `[error]` — error occurred during data collection or upload
