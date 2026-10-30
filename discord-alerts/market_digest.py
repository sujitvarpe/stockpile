# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Post a daily market digest to a Discord webhook.

Two reports, both built from TradingView screener data (the same backend
the TradingView MCP's run_screener / get_earnings_calendar tools query):

  sectors   Which sectors led today (SPDR sector ETFs + cap-weighted
            TradingView sectors with breadth), and the strongest leaders
            inside the top sectors.
  earnings  Today's earnings calendar: before-open and after-close
            reporters with EPS / revenue consensus.

Usage (from the repo root):
    uv run discord-alerts/market_digest.py sectors
    uv run discord-alerts/market_digest.py earnings --date 2026-10-27
    uv run discord-alerts/market_digest.py all --dry-run

Webhook URLs are read from DISCORD_WEBHOOK_URL and DISCORD_WEBHOOK_URL_2
(every report goes to each of them). --dry-run prints the
Discord payload instead of posting it. Standard library only, so the
GitHub Actions job needs nothing but uv.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from zoneinfo import ZoneInfo

SCAN_URL = "https://scanner.tradingview.com/america/scan"
ET = ZoneInfo("America/New_York")
USER_AGENT = "stockpile-discord-digest/1.0"

SECTOR_ETFS = {
    "XLK": "Technology",
    "XLC": "Communication",
    "XLY": "Cons. Discretionary",
    "XLP": "Cons. Staples",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLV": "Health Care",
    "XLI": "Industrials",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
}

# Primary listings of US common stock (and ADRs) on the major exchanges;
# keeps OTC lines, preferreds and secondary share classes out.
US_STOCK_FILTER = [
    {"left": "exchange", "operation": "in_range", "right": ["AMEX", "NASDAQ", "NYSE"]},
    {"left": "type", "operation": "equal", "right": "stock"},
    {"left": "subtype", "operation": "in_range", "right": ["common", "foreign-issuer"]},
    {"left": "is_primary", "operation": "equal", "right": True},
]

GREEN, RED, GREY = 0x22C55E, 0xEF4444, 0x64748B


# ── TradingView screener ────────────────────────────────────────────────


def scan(columns: list[str], filters: list[dict] | None = None, *,
         tickers: list[str] | None = None, sort_by: str | None = None,
         limit: int = 1000) -> list[dict]:
    """Run one screener query and return rows as {column: value} dicts."""
    body: dict = {
        "columns": columns,
        "options": {"lang": "en"},
        "range": [0, limit],
    }
    if filters:
        body["filter"] = filters
    if tickers:
        body["symbols"] = {"tickers": tickers}
    if sort_by:
        body["sort"] = {"sortBy": sort_by, "sortOrder": "desc"}
    req = urllib.request.Request(
        SCAN_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    rows = []
    for item in data.get("data") or []:
        row = dict(zip(columns, item["d"]))
        row["symbol"] = item["s"]
        rows.append(row)
    return rows


# ── formatting helpers ──────────────────────────────────────────────────


def pct(v: float | None, width: int = 6) -> str:
    return f"{v:+{width}.2f}%" if v is not None else " " * width + " "


def money(v: float | None) -> str:
    if v is None:
        return "—"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:,.0f}"


def code(lines: list[str]) -> str:
    """Wrap lines in a code block, trimmed to Discord's 1024-char field cap."""
    out, size = [], 8
    for line in lines:
        if size + len(line) + 1 > 1024:
            break
        out.append(line)
        size += len(line) + 1
    return "```\n" + "\n".join(out) + "\n```"


def tv_link(symbol: str) -> str:
    return f"https://www.tradingview.com/symbols/{symbol.replace(':', '-')}/"


# ── sectors report ──────────────────────────────────────────────────────


def sector_etf_field() -> dict:
    rows = scan(
        ["name", "change", "Perf.W"],
        tickers=[f"AMEX:{t}" for t in [*SECTOR_ETFS, "SPY"]],
    )
    spy = next((r for r in rows if r["name"] == "SPY"), None)
    etfs = sorted((r for r in rows if r["name"] != "SPY"),
                  key=lambda r: r["change"] or 0, reverse=True)
    lines = [f"{'ETF':<5} {'Sector':<19} {'Day':>7} {'Week':>7}"]
    for r in etfs:
        lines.append(f"{r['name']:<5} {SECTOR_ETFS[r['name']]:<19} "
                     f"{pct(r['change'])} {pct(r['Perf.W'])}")
    if spy:
        lines.append(f"{'SPY':<5} {'S&P 500':<19} {pct(spy['change'])} {pct(spy['Perf.W'])}")
    return {"name": "SPDR sector ETFs", "value": code(lines), "inline": False}


def sectors_report(min_cap: float, leaders_per_sector: int, top_sectors: int) -> dict:
    cols = ["name", "description", "sector", "close", "change", "Perf.W",
            "market_cap_basic", "average_volume_30d_calc",
            "relative_volume_10d_calc", "SMA50", "price_52_week_high"]
    universe = scan(
        cols,
        US_STOCK_FILTER + [{"left": "market_cap_basic", "operation": "greater", "right": 1e9}],
        sort_by="market_cap_basic",
        limit=5000,
    )
    universe = [r for r in universe if r["sector"] and r["change"] is not None
                and r["market_cap_basic"]]

    # Cap-weighted daily change and breadth per TradingView sector.
    agg: dict[str, dict] = defaultdict(lambda: {"w": 0.0, "wc": 0.0, "n": 0, "up": 0})
    for r in universe:
        a = agg[r["sector"]]
        a["w"] += r["market_cap_basic"]
        a["wc"] += r["market_cap_basic"] * r["change"]
        a["n"] += 1
        a["up"] += r["change"] > 0
    sectors = sorted(
        ((s, a["wc"] / a["w"], 100 * a["up"] / a["n"], a["n"]) for s, a in agg.items()),
        key=lambda t: t[1], reverse=True,
    )
    lines = [f"{'Sector':<24} {'Day':>7} {'Adv':>5}"]
    for s, chg, adv, _n in sectors:
        lines.append(f"{s[:24]:<24} {pct(chg)} {adv:4.0f}%")
    breadth_field = {
        "name": f"TradingView sectors (cap-weighted, {len(universe)} stocks > $1B)",
        "value": code(lines),
        "inline": False,
    }

    # Leaders: liquid names in the top sectors that are up on the day and
    # trading above a rising-trend proxy (the 50-day SMA).
    fields = [sector_etf_field(), breadth_field]
    for s, chg, adv, _n in sectors[:top_sectors]:
        picks = [
            r for r in universe
            if r["sector"] == s
            and r["market_cap_basic"] >= min_cap
            and (r["average_volume_30d_calc"] or 0) * (r["close"] or 0) >= 20e6
            and r["change"] > 0
            and r["SMA50"] and r["close"] > r["SMA50"]
        ]
        picks.sort(key=lambda r: r["change"], reverse=True)
        picks = picks[:leaders_per_sector]
        if not picks:
            continue
        rows = [f"{'Ticker':<6} {'Day':>7} {'RVOL':>5} {'52wH':>7} {'Cap':>6}"]
        for r in picks:
            off_high = (100 * (r["close"] / r["price_52_week_high"] - 1)
                        if r["price_52_week_high"] else None)
            rvol = r["relative_volume_10d_calc"] or 0
            star = "★" if (off_high is not None and off_high > -3 and rvol >= 1.5) else " "
            rows.append(f"{r['name'][:6]:<6} {pct(r['change'])} {rvol:5.1f} "
                        f"{pct(off_high)} {money(r['market_cap_basic']):>6}{star}")
        fields.append({
            "name": f"Leaders · {s} ({pct(chg, 0).strip()}, {adv:.0f}% adv)",
            "value": code(rows),
            "inline": False,
        })

    top_chg = sectors[0][1] if sectors else 0
    today = dt.datetime.now(ET).strftime("%a %b %d, %Y")
    return {
        "title": f"Sector performance · {today}",
        "description": (
            f"Leading: **{sectors[0][0]}** · Lagging: **{sectors[-1][0]}**\n"
            f"Leaders = up today, above 50-day SMA, cap ≥ {money(min_cap)}, "
            f"≥ $20M avg daily $ volume. ★ = within 3% of 52-week high on ≥ 1.5× RVOL."
            if sectors else "No sector data returned."
        ),
        "color": GREEN if top_chg > 0 else RED,
        "fields": fields,
        "footer": {"text": "Data: TradingView screener · not financial advice"},
    }


# ── earnings report ─────────────────────────────────────────────────────


def earnings_report(day: dt.date, min_cap: float, per_group: int) -> dict:
    start = int(dt.datetime.combine(day, dt.time.min, ET).timestamp())
    end = int(dt.datetime.combine(day, dt.time.max, ET).timestamp())
    cols = ["name", "description", "market_cap_basic",
            "earnings_release_next_date", "earnings_release_next_time",
            "earnings_release_date", "earnings_release_time",
            "earnings_per_share_forecast_next_fq", "revenue_forecast_next_fq",
            "earnings_per_share_fq", "eps_surprise_percent_fq"]
    base = US_STOCK_FILTER + [{"left": "market_cap_basic", "operation": "greater", "right": min_cap}]

    # A report that is still upcoming sits in *_next_*; once it is out
    # TradingView moves it to the "last report" columns. Query both.
    found: dict[str, dict] = {}
    for prefix in ("earnings_release_next", "earnings_release"):
        rows = scan(cols, base + [{"left": f"{prefix}_date", "operation": "in_range",
                                   "right": [start, end]}],
                    sort_by="market_cap_basic", limit=500)
        for r in rows:
            r["_when"] = r[f"{prefix}_time"]
            r["_reported"] = prefix == "earnings_release"
            found.setdefault(r["symbol"], r)

    groups = {"Before open": [], "After close": [], "Time not confirmed": []}
    for r in sorted(found.values(), key=lambda r: r["market_cap_basic"] or 0, reverse=True):
        key = {-1: "Before open", 1: "After close"}.get(r["_when"], "Time not confirmed")
        groups[key].append(r)

    fields = []
    for title, rows in groups.items():
        if not rows:
            continue
        lines = [f"{'Ticker':<6} {'Cap':>6} {'EPS est':>8} {'Rev est':>7}  Name"]
        for r in rows[:per_group]:
            if r["_reported"]:
                eps = f"{r['earnings_per_share_fq']:.2f}" if r["earnings_per_share_fq"] is not None else "—"
                surprise = r["eps_surprise_percent_fq"]
                eps += "✓" if surprise is not None and surprise >= 0 else "✗" if surprise is not None else ""
                rev = "out"
            else:
                eps = (f"{r['earnings_per_share_forecast_next_fq']:.2f}"
                       if r["earnings_per_share_forecast_next_fq"] is not None else "—")
                rev = money(r["revenue_forecast_next_fq"])
            lines.append(f"{r['name'][:6]:<6} {money(r['market_cap_basic']):>6} "
                         f"{eps:>8} {rev:>7}  {(r['description'] or '')[:22]}")
        more = len(rows) - per_group
        name = f"{title} ({len(rows)})" + (f" — top {per_group} by cap" if more > 0 else "")
        fields.append({"name": name, "value": code(lines), "inline": False})

    total = len(found)
    when = "today" if day == dt.datetime.now(ET).date() else f"on {day.strftime('%A')}"
    return {
        "title": f"Earnings calendar · {day.strftime('%a %b %d, %Y')}",
        "description": (
            f"**{total}** US companies with market cap ≥ {money(min_cap)} report {when}."
            " Reported rows show actual EPS (✓ beat / ✗ miss)."
            if total else f"No US companies with market cap ≥ {money(min_cap)} report {when}."
        ),
        "color": GREY,
        "fields": fields,
        "footer": {"text": "Data: TradingView earnings calendar · times are as reported by TradingView"},
    }


# ── Discord ─────────────────────────────────────────────────────────────


def post(embeds: list[dict], webhooks: list[str], dry_run: bool) -> None:
    payload = {"username": "Market Digest", "embeds": embeds}
    if dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        for e in embeds:
            print(f"\n## {e['title']}\n{e['description']}")
            for f in e["fields"]:
                print(f"\n{f['name']}\n{f['value']}")
        return
    if not webhooks:
        sys.exit("DISCORD_WEBHOOK_URL is not set (use --dry-run to preview).")
    # Post to every channel before failing, so one bad webhook doesn't
    # silence the others.
    failed = []
    for i, webhook in enumerate(webhooks, 1):
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            urllib.request.urlopen(req, timeout=30).close()
        except urllib.error.HTTPError as e:
            failed.append(f"webhook #{i}: {e.code} {e.read().decode(errors='replace')}")
    if failed:
        sys.exit("Discord webhook failed: " + "; ".join(failed))


def webhooks_from_env() -> list[str]:
    """DISCORD_WEBHOOK_URL and DISCORD_WEBHOOK_URL_2, each of which may also
    hold several URLs separated by commas or whitespace."""
    raw = " ".join(os.environ.get(k, "") for k in ("DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_URL_2"))
    return list(dict.fromkeys(raw.replace(",", " ").split()))


def next_trading_day(day: dt.date) -> dt.date:
    """The day itself on a weekday, else the following Monday."""
    return day + dt.timedelta(days=7 - day.weekday()) if day.weekday() >= 5 else day


def cron_is_et(cron_utc: str, et_time: str) -> bool:
    """True if a HH:MM UTC cron lands on HH:MM New York time today.

    Decided from today's UTC offset, not the clock, because GitHub often
    starts scheduled runs an hour or more late.
    """
    utc = dt.datetime.combine(dt.datetime.now(dt.timezone.utc).date(),
                              dt.time.fromisoformat(cron_utc), dt.timezone.utc)
    return utc.astimezone(ET).time() == dt.time.fromisoformat(et_time)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("report", choices=["sectors", "earnings", "all"])
    p.add_argument("--date", type=dt.date.fromisoformat,
                   help="earnings date (default: today in New York, or Monday on a weekend)")
    p.add_argument("--earnings-min-cap", type=float, default=2e9)
    p.add_argument("--leaders-min-cap", type=float, default=2e9)
    p.add_argument("--top-sectors", type=int, default=3)
    p.add_argument("--leaders", type=int, default=5, help="leaders per sector")
    p.add_argument("--per-group", type=int, default=20, help="earnings rows per session")
    p.add_argument("--cron-utc", help="HH:MM UTC this run was scheduled for; with "
                   "--et-time, lets a DST-agnostic pair of UTC crons fire once")
    p.add_argument("--et-time", help="HH:MM New York time the report is meant for")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.cron_utc and args.et_time and not cron_is_et(args.cron_utc, args.et_time):
        print(f"{args.cron_utc} UTC is not {args.et_time} New York time today; "
              "the other cron of the pair posts.")
        return

    embeds = []
    if args.report in ("sectors", "all"):
        embeds.append(sectors_report(args.leaders_min_cap, args.leaders, args.top_sectors))
    if args.report in ("earnings", "all"):
        day = args.date or next_trading_day(dt.datetime.now(ET).date())
        embeds.append(earnings_report(day, args.earnings_min_cap, args.per_group))
    webhooks = webhooks_from_env()
    for e in embeds:
        post([e], webhooks, args.dry_run)


if __name__ == "__main__":
    main()
