# Discord market digest

Posts two daily reports to a Discord channel through a webhook, built
from TradingView screener data (the same data the TradingView MCP tools
`run_screener` and `get_earnings_calendar` return):

| Report | When (New York) | What's in it |
|--------|-----------------|--------------|
| `earnings` | ~08:07, weekdays | Today's US earnings (market cap ≥ $2B), split into before-open / after-close, with consensus EPS and revenue |
| `sectors` | ~16:17, weekdays | SPDR sector ETFs ranked by day % change, cap-weighted TradingView sectors with % advancing, and the top 5 leaders in each of the 3 strongest sectors |

A **leader** is a stock in a top sector that is up on the day, closes
above its 50-day SMA, has market cap ≥ $2B and averages ≥ $20M a day in
dollar volume. A ★ marks one within 3% of its 52-week high on ≥ 1.5×
relative volume.

## Setup

1. In Discord: channel → **Edit Channel → Integrations → Webhooks → New
   Webhook** → **Copy Webhook URL**.
2. In GitHub: repo → **Settings → Secrets and variables → Actions → New
   repository secret**, name `DISCORD_WEBHOOK_URL`, paste the URL.
3. Repo → **Actions → Discord market digest → Run workflow** to send a
   test post right away. After that it runs on its own every weekday
   (`.github/workflows/discord-market-digest.yml`). GitHub only runs
   scheduled workflows from the default branch, so the workflow has to
   be merged to `main` first.

## Running locally

```bash
uv run discord-alerts/market_digest.py all --dry-run      # print, don't post
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... \
    uv run discord-alerts/market_digest.py sectors
uv run discord-alerts/market_digest.py earnings --date 2026-10-27 --dry-run
```

Useful flags: `--top-sectors 3`, `--leaders 5`, `--leaders-min-cap 2e9`,
`--earnings-min-cap 2e9`, `--per-group 20`.

The script uses only the Python standard library. It calls TradingView's
public screener endpoint without logging in; if TradingView changes it,
the job fails with an HTTP error in the Actions log.
