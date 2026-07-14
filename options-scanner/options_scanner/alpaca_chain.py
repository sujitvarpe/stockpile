"""Fetch option chain from the Alpaca Market Data API and return the
normalized DataFrame shape of chain.py:fetch_chain(), so all downstream
code (iv_surface, earnings, display, report) is unchanged.

Why this source exists: Yahoo soft-rate-limits bulk chain requests
(the "degraded option chain" error), while Alpaca serves full OPRA
snapshots — real quotes, real IV, real Greeks — with generous limits.

Data endpoints used (all read-only):
  - GET data.alpaca.markets/v1beta1/options/snapshots/{underlying}
      latest quote/trade + implied_volatility + greeks, paginated
  - GET data.alpaca.markets/v2/stocks/{underlying}/trades/latest
      spot price
  - GET paper-api.alpaca.markets/v2/options/contracts
      open_interest per contract (not present in snapshots)

Credentials: APCA_API_KEY_ID / APCA_API_SECRET_KEY from the environment,
falling back to the shared paper-account .env used by the live bots
(~/pinscript/options_trade/.env). No trading scope is exercised.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from options_scanner.chain_common import build_option_row, safe_float, safe_int

log = logging.getLogger(__name__)

_DATA_BASE = "https://data.alpaca.markets"
_TRADING_BASE = "https://paper-api.alpaca.markets"
_ENV_FALLBACK = Path.home() / "pinscript" / "options_trade" / ".env"


def _credentials() -> dict:
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not (key and secret) and _ENV_FALLBACK.exists():
        for line in _ENV_FALLBACK.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "APCA_API_KEY_ID":
                    key = key or v
                elif k == "APCA_API_SECRET_KEY":
                    secret = secret or v
    if not (key and secret):
        raise ValueError(
            "Alpaca credentials not found - set APCA_API_KEY_ID / "
            f"APCA_API_SECRET_KEY or provide {_ENV_FALLBACK}")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _get(url: str, headers: dict, params: dict) -> dict:
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code == 429:
        raise RuntimeError("Alpaca rate limit hit (HTTP 429)")
    resp.raise_for_status()
    return resp.json()


def _parse_occ(sym: str, underlying: str) -> tuple[date, str, float]:
    """OCC symbol -> (expiry, side, strike). e.g. AMD260918C00170000"""
    tail = sym[len(underlying):]
    expiry = datetime.strptime(tail[:6], "%y%m%d").date()
    side = "call" if tail[6] == "C" else "put"
    strike = float(tail[7:]) / 1000.0
    return expiry, side, strike


def _fetch_spot(underlying: str, headers: dict) -> float:
    d = _get(f"{_DATA_BASE}/v2/stocks/{underlying}/trades/latest", headers, {})
    return float(d["trade"]["p"])


def _fetch_open_interest(underlying: str, headers: dict,
                         exp_gte: str, exp_lte: str | None) -> dict[str, int]:
    """OCC symbol -> open_interest from the contracts (reference) endpoint."""
    oi: dict[str, int] = {}
    params = {"underlying_symbols": underlying, "limit": 10000,
              "expiration_date_gte": exp_gte}
    if exp_lte:
        params["expiration_date_lte"] = exp_lte
    while True:
        d = _get(f"{_TRADING_BASE}/v2/options/contracts", headers, params)
        for c in d.get("option_contracts") or []:
            if c.get("open_interest") is not None:
                oi[c["symbol"]] = safe_int(c["open_interest"])
        token = d.get("next_page_token")
        if not token:
            return oi
        params["page_token"] = token


def fetch_chain_alpaca(ticker: str, opt_type: str = "both",
                       min_dte: int = 30, max_dte: int | None = 90,
                       alpaca_config: dict | None = None) -> pd.DataFrame:
    """Same contract as chain.fetch_chain: rows via build_option_row."""
    headers = _credentials()
    underlying = ticker.upper()
    today = date.today()
    exp_gte = today.fromordinal(today.toordinal() + min_dte).isoformat()
    exp_lte = (today.fromordinal(today.toordinal() + max_dte).isoformat()
               if max_dte is not None else None)

    spot = _fetch_spot(underlying, headers)
    if not spot or spot <= 0:
        raise ValueError(f"Could not fetch Alpaca spot price for {ticker}")

    oi_map = _fetch_open_interest(underlying, headers, exp_gte, exp_lte)

    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    feed = (alpaca_config or {}).get("feed", "opra")
    params: dict = {"feed": feed, "limit": 1000,
                    "expiration_date_gte": exp_gte}
    if exp_lte:
        params["expiration_date_lte"] = exp_lte
    if opt_type in ("calls", "puts"):
        params["type"] = opt_type.rstrip("s")

    url = f"{_DATA_BASE}/v1beta1/options/snapshots/{underlying}"
    while True:
        try:
            d = _get(url, headers, params)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 403 \
                    and feed == "opra":
                # no OPRA subscription on this key - fall back to indicative
                feed = params["feed"] = "indicative"
                continue
            raise
        for sym, snap in (d.get("snapshots") or {}).items():
            expiry, side, strike = _parse_occ(sym, underlying)
            dte = (expiry - today).days
            if dte < min_dte or (max_dte is not None and dte > max_dte):
                continue
            if opt_type == "calls" and side != "call":
                continue
            if opt_type == "puts" and side != "put":
                continue
            quote = snap.get("latestQuote") or {}
            trade = snap.get("latestTrade") or {}
            greeks = snap.get("greeks") or {}
            last_trade_days = float("nan")
            if trade.get("t"):
                try:
                    ts = pd.Timestamp(trade["t"])
                    last_trade_days = float((now - ts.to_pydatetime())
                                            .total_seconds()) / 86400.0
                except (TypeError, ValueError):
                    pass
            row = build_option_row(
                side=side, strike=strike, expiration=expiry.isoformat(),
                dte=dte, spot=spot,
                bid=safe_float(quote.get("bp")),
                ask=safe_float(quote.get("ap")),
                mid=0.0,
                last=safe_float(trade.get("p")),
                iv=safe_float(snap.get("impliedVolatility")),
                delta=safe_float(greeks.get("delta")),
                gamma=safe_float(greeks.get("gamma")),
                theta=safe_float(greeks.get("theta")),
                vega=safe_float(greeks.get("vega")),
                open_interest=oi_map.get(sym, 0),
                volume=0,  # snapshots carry no daily volume; OI is the liquidity gate
                last_trade_days=last_trade_days,
            )
            if row is not None:
                rows.append(row)
        token = d.get("next_page_token")
        if not token:
            break
        params["page_token"] = token

    log.info("  Alpaca chain %s: %d rows (feed=%s)", underlying, len(rows), feed)
    return pd.DataFrame(rows) if rows else pd.DataFrame()
