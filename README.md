# kroger-agent

Taste-aware Kroger ordering. A local SQLite history of what you actually buy, a
cadence engine that works out what is due, and an agent skill that drives it.

## The hard limit

The Kroger **public** API can add items to your cart. It cannot read the cart,
remove items, or check out. Cart retrieval and checkout are Partner-API only, and
Partner APIs are closed to the public.

So this tool builds the cart. **You check out, in the browser.** Every tool in
this space (kroger-cli, kroger-cart, the MCP servers) has the same ceiling.

```
kroger due  ->  agent proposes  ->  you approve  ->  kroger add  ->  you check out
                                                            |
                                                            v
                                              receipts come back via Gmail
                                                            |
                                              kroger reconcile learns from it
```

## Layout

```
~/kroger-agent/
  data/
    kroger.db          <-- the thing you back up
    config.env         <-- Kroger credentials (gitignored)
    tokens.json        <-- OAuth token, chmod 600
    gmail/             <-- symlinks to your Google OAuth client + token
  exports/             <-- portable JSON/CSV dumps
  src/kroger/
```

`~/kroger-agent` is self-contained. To move to another machine: copy the folder,
`uv sync`, re-consent. To back up, copy `exports/` (safe) rather than the live
`.db` (WAL mode — a half-copied database is not a valid database).

## Commands

Built:

```
kroger doctor              # check config, credentials, db, store
kroger auth login          # one-time browser consent
kroger auth status
kroger store set --zip     # resolve locationId
kroger store show
kroger product lookup --upc 0001111041700   # exact lookup, authoritative price
kroger cart test --upc ... --yes           # add one item (validation gate)
kroger scrape orders --limit N --commit    # pull order history
kroger scrape status | reset
kroger db init | stats
```

Not built yet: `ingest` (raw -> db), `resolve-upcs`, `due`, `propose`, `add`,
`reconcile`, `export`. See "Roadmap" below.

Output is a table on a TTY and JSON when piped, so the agent gets structured
output without a flag.

## Setup

### 1. Kroger developer app

1. <https://developer.kroger.com/> -> create an account -> register an app.
2. **Production** environment. The certification/sandbox env will not touch your
   real account.
3. Redirect URI: `http://localhost:3000` (must match `KROGER_REDIRECT_URI`
   exactly, port included).
4. Copy `.env.example` to `data/config.env` and fill in the credentials.

```
cp .env.example data/config.env
$EDITOR data/config.env
uv run kroger auth login          # opens a browser, click Authorize
uv run kroger doctor              # all checks should pass
```

### 2. Order history: scraping kroger.com

Order history comes from `kroger.com/mypurchases` via Playwright.

```
uv run kroger scrape orders --limit 5 --commit
```

A browser window opens. **Log in there yourself** — the tool never sees or asks
for your password. The login persists in `data/scrape/profile`, so this is a
one-time step.

The first run is `--commit`-gated on purpose: it captures raw receipt text to
`data/scrape/raw/` and writes nothing to the database. We read the real format
before writing a parser against a guess. Subsequent runs resume where they
stopped, so `--limit 50` is fine.

Things this deliberately does not do:

- **No headless mode.** Kroger rejects headless sessions, and `headless_shell` is
  trivially fingerprinted. `open_browser()` raises if you ask for it.
- **No password handling.** You log in; we never touch credentials.
- **No tight loops.** Randomised 2-8s delays and human-like scrolling. A session
  that hammers the account gets the account throttled, which would defeat the
  point.

```
uv run kroger scrape status     # progress
uv run kroger scrape reset      # re-scrape already-captured orders
```

## Design notes

**`order_items` is append-only ground truth.** Everything else is derived. This
is what lets the system be re-derived, re-scored, or re-priced without losing
information.

**Ingest is idempotent.** `orders.order_number` and `orders.source_ref` are
UNIQUE, so re-running `ingest gmail` over the same mail is a no-op rather than a
double-count. Covered by a test.

**Identity is UPC-first, fuzzy-second.** A 13-digit `productId` is
authoritative. Without one, identity falls back to normalized name, then a
recorded alias, then token similarity (`db.SIMILARITY_THRESHOLD`, tuned so
"a2 milk vitamin d whole milk" merges with "a2 milk whole" but "whole milk" does
not merge with "skim milk"). `name_norm` is a candidate-generation index, not an
identity proof — receipts describe the same product inconsistently.

**Cadence is computed in Python, not SQL.** Medians and percentiles are painful
to express in SQLite, and the cadence engine needs them.

**`proposals` is the learning signal.** `reconcile()` scores what we proposed
against what you actually bought. Bought -> cadence and confidence update.
Declined twice or more -> an `avoid` preference is written. Swapped -> recorded,
and the replacement becomes the candidate next time. That is the mechanism by
which the system follows your taste instead of accumulating a static list.

## Roadmap

- [x] project scaffold, schema, identity resolution, doctor
- [x] Playwright scraper with raw capture, resume, and human pacing
- [ ] receipt parser (blocked: needs one real captured receipt)
- [ ] `ingest` raw scraped receipts into the database
- [ ] `resolve-upcs` — name -> UPC via the Products API
- [ ] `due` — cadence engine (median gap, modal qty, confidence, price check)
- [ ] `propose` / `add` — approval-gated cart building
- [ ] `reconcile` — propose-vs-actual learning
- [ ] `export` — JSON/CSV for portability
- [ ] opencode skill

## Rate limits

10,000 product calls/day, 5,000 cart calls/day. Not a constraint at human
shopping cadence, but `due` should price-check in one batched pass rather than
per item.
