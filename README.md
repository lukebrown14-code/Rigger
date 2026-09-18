# Rigger

A personal investment research assistant for people who enjoy investing as a hobby.

You tell Rigger what you're interested in — a company, a sector, an industry, a market, a theme. It gathers evidence about them from public sources, an AI synthesises that evidence into reports you can check line by line, and an optional thesis layer tracks a long-horizon idea as evidence accumulates for and against it.

**It does not trade, and it does not tell you what to buy.** The product is clarity: organised facts, cited summaries, and somewhere to reason about an idea. Nothing here is financial advice.

## What it does

```
Data plugins ──ingest──▶ SQLite ──extract──▶ Events
(yfinance, rss, sec_edgar,          (LLM turns news into
 asx_announcements, calendar)        structured facts)
                                              │
                                    evidence pool (prices, news, events, fundamentals)
                                              │
                    ┌─────────────────────────┼─────────────────────────┐
                    ▼                         ▼                         ▼
             cited reports              grounded chat            optional theses
        (reports/<target>/<date>.md)  (local data first,    (evidence for and against,
                                       web search opt-in)      health computed, not claimed)
```

Four rules the code actually enforces:

- **Facts come from the harness, not the model.** Prompts forbid reasoning from memory. Every fact the model uses was collected by a data plugin.
- **Everything is cited.** A report claim naming an evidence id that wasn't gathered is dropped before you see it, and a draft left with no substantive claims is rejected outright. Chat citations are machine-checked; an answer without verifiable support is labelled AI inference rather than passed off as fact.
- **The AI synthesises and challenges — it doesn't tip.** It summarises what was found and argues both sides. It does not recommend buying or selling.
- **Discovery never auto-accepts.** A thesis proposes *candidate* evidence; only you accept it, and thesis health is computed by a pure function over what you accepted. The model can write prose about that state, but it never decides it.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- An LLM provider key. OpenRouter is the simplest single-key option.

## Install

```bash
git clone https://github.com/lukebrown14-code/Rigger.git
cd Rigger
uv sync
cp .env.example .env      # then fill in your key(s)
```

## Configure

`config.toml` holds your targets, model routing and plugin settings. `.env` holds secrets and is never committed.

Pick a provider under `[llm]`:

| `provider`      | What it does                                | Key needed |
|-----------------|---------------------------------------------|------------|
| `openrouter`    | One API for many models                     | `OPENROUTER_API_KEY` |
| `openai`        | ChatGPT models direct from OpenAI           | `OPENAI_API_KEY` |
| `anthropic`     | Claude models direct from Anthropic         | `ANTHROPIC_API_KEY` |
| `custom`        | Any OpenAI-compatible server (Ollama, Groq, Together…) | `[llm] base_url` + key |

The easiest way to connect one is in the app: press `p` to browse providers, paste your key (masked input), and it's verified live, saved to `.env`, and activated immediately — no file editing, no restart. Manual setup works too: put the key in `.env`, set `provider` in `config.toml`. For `custom`, also set `base_url` (and optionally `api_key_env`, default `CUSTOM_API_KEY`):

```toml
[llm]
provider = "custom"
base_url = "http://localhost:11434/v1"   # e.g. Ollama
api_key_env = "OLLAMA_KEY"               # optional, for non-local servers
```

Four jobs route to models independently in `[llm.routing]` — `extract`, `report`, `chat` and `thesis` — so you can put a cheap fast model on bulk news extraction and a stronger one on reports. Model ids are plain strings; change them freely, or press `m` in the app to browse what your provider offers, with prices. Route ids are provider-native (e.g. `claude-sonnet-4-...` on `anthropic`, `gpt-4o` on `openai`, `vendor/model` on `openrouter`) — switching providers warns when existing routes don't look right.

Set `[plugins.sec_edgar].contact` to a real email before ingesting US filings — the SEC requires a contact address in the User-Agent.

## Use it

Open the app:

```bash
uv run rig
```

| Key | Screen |
|-----|--------|
| `1` | Watchlist |
| `2` / `3` | Research: Evidence / Report |
| `4` / `5` | Theses / Ask |
| `c` | Settings |
| `h` | Home |
| `m` | Model picker |
| `?` / `q` | Help / quit |

Everything happens inside the app. The watchlist is built on the Watchlist panel (**1**), which takes a name, kind, market, tickers and tags — and the command palette (ctrl+p) has a **Gather evidence** action that ingests prices, news, filings and fundamentals, then turns the news into structured events.

Watchlist entries come in five kinds — `company`, `sector`, `industry`, `market` and `theme`. All but `market` name tickers in one market; a market entry names only the market. Set `asset_class` to `equity`, `etf`, `bond`, `commodity`, `fx`, `crypto`, `cash` or `other` when a ticker is not an equity. The Watchlist inspector uses that class to show relevant metrics: commodities such as gold get price and volatility measures, while bonds such as the US 10-year yield get yield and basis-point measures. Older `[watchlists]` tables keep working, with their kind inferred from shape.

Theses are optional. Skip them entirely and you still get a watchlist, evidence and reports. On the Theses screen (**4**) write down something you believe, let the model propose candidate evidence, and accept or reject each piece yourself.

Press **2** for Research: choose a watch target and company, then search and filter its sources. **Gather all** collects evidence across configured targets. **Generate report** writes a report for the selected company; browsing filters do not change its inputs. Press **3** to read the latest report and follow its citations back to Evidence.

Reports keep their Markdown export alongside a structured JSON sidecar for citation navigation. Older Markdown-only reports remain readable; regenerate them to enable interactive citations. Database counts, price timestamps and cumulative model spend are under **Settings → Diagnostics** (**c**).

## Project layout

```
rigger/
├── core/            models, SQLite (SQLModel), config, plugin registry, event bus
├── llm/             provider-agnostic client, model catalog, routing, structured calls
├── plugins/
│   ├── markets/     us, asx
│   ├── data/        yfinance, yfinance_calendar, rss, asx_announcements, sec_edgar
│   └── targets/     company, sector, industry, theme, market
├── targets.py       what you follow, and how it resolves to instruments
├── evidence.py      one read model over bars, news, events and fundamentals
├── extract.py       news → structured Event rows
├── reports.py       cited research reports, with the citation contract enforced
├── chat.py          grounded Q&A, local evidence first, web search opt-in
├── theses.py        long-horizon claims with evidence for and against
├── thesis_health.py pure function over accepted evidence
├── tui/             Textual app and screens
tests/               pytest, fully offline (respx + a fake LLM)
```

Plugins are discovered through the `rigger.plugins` and `rigger.targets` entry-point groups in `pyproject.toml`. A new data source is one file implementing one class.

### Configuring data sources

Press `c` for Settings, select a source in **data sources**, then press `d` to configure it. Source adapters declare the settings they accept; ordinary settings are saved in `config.toml`, while declared API-key fields are saved only in `.env` and are masked in the UI. Rigger deliberately does not offer a generic authenticated-HTTP connector: a commercial source such as Financial Times needs a dedicated adapter built against its licensed API contract, pagination rules and content-use rights.

### Adding an exchange

Press `c`, then `a` in Settings to add an exchange-level market. Enter its ID, currency and Yahoo suffix (for example, `lse`, `GBP`, `.L`). Yahoo Finance prices/calendar data and RSS work for every configured market; country-specific disclosure plugins are enabled separately and can be assigned to selected markets in their source setup form.

## Develop

```bash
uv run pytest
uv run ruff check . && uv run ruff format .
uv run mypy --strict rigger/core rigger/llm
```

The same three run in CI on every push. Tests never touch the network.

[PROJECT_SPEC.md](PROJECT_SPEC.md) is the source of truth for scope, architecture, plugin contracts and conventions — including why this stopped being a trading harness.

## Caveats

- Free data sources are rate-limited and sometimes wrong. Treat a single citation as a lead, not a fact.
- A cited report is only as good as what was gathered. If ingest missed something the model cannot know about it — that is the deliberate trade for never inventing facts.
- Nothing here is financial advice, and none of it substitutes for reading the primary source.
