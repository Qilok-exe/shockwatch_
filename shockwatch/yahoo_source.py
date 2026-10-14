"""Bounded Yahoo Finance source adapter. No detector or publishing side effects."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal


SOURCE_VERSION = "yahoo-adapter-2"
CALENDAR_VERSION = mcal.__version__
CALENDAR_BY_SYMBOL = {"SPY":"NYSE","UAL":"NASDAQ","LMT":"NYSE"}
HISTORY_KWARGS = {"period": "60d", "interval": "5m", "auto_adjust": False,
                  "actions": True, "repair": False, "keepna": True, "prepost": False,
                  "timeout": 20}


class YahooSourceError(RuntimeError):
    pass


@lru_cache(maxsize=128)
def market_schedule(symbol, day):
    if symbol not in CALENDAR_BY_SYMBOL:
        raise ValueError("symbol_not_in_paper_universe")
    schedule=mcal.get_calendar(CALENDAR_BY_SYMBOL[symbol]).schedule(day,day)
    if schedule.empty:
        return None
    return (pd.Timestamp(schedule.iloc[0]["market_open"]).tz_convert("UTC"),
            pd.Timestamp(schedule.iloc[0]["market_close"]).tz_convert("UTC"))


def utc(value):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError("timestamp_missing_timezone")
    return stamp.tz_convert("UTC").isoformat()


def number(value):
    if value is None or pd.isna(value):
        return None
    try:
        result = float(value)
    except (TypeError,ValueError):
        return None
    return result if math.isfinite(result) else None


def integer(value):
    n = number(value)
    return None if n is None else int(n) if n.is_integer() else None


def nonfinite_present(value):
    if value is None or pd.isna(value):
        return False
    try:
        return not math.isfinite(float(value))
    except (TypeError,ValueError):
        return True


def frame_hash(frame):
    """Hash exact normalized frame values, not a huge persisted response body."""
    wire = frame.to_json(orient="split", date_format="iso", date_unit="ns", default_handler=str)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


@dataclass
class SourceResponse:
    symbol: str
    kind: str
    request: dict
    received_at: str
    response_hash: str
    rows: list[dict]
    issues: list[dict]
    metadata: dict


class YahooSource:
    def __init__(self, ticker_factory=None, clock=None):
        if ticker_factory is None:
            import yfinance as yf
            self.ticker_factory = yf.Ticker
            self.library_version = yf.__version__
        else:
            self.ticker_factory = ticker_factory
            self.library_version = "fixture"
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _ticker(self, symbol):
        if symbol not in {"SPY", "UAL", "LMT"}:
            raise ValueError("symbol_not_in_paper_universe")
        return self.ticker_factory(symbol)

    def history(self, symbol, *, backfill=True):
        ticker = self._ticker(symbol)
        requested_at = utc(self.clock())
        kwargs = {**HISTORY_KWARGS, "period": "60d" if backfill else "5d"}
        request = {"method": "Ticker.history", "symbol": symbol, **kwargs}
        try:
            frame = ticker.history(**kwargs)
            metadata = ticker.history_metadata or {}
        except Exception as exc:
            raise YahooSourceError(f"{symbol} stock history: {type(exc).__name__}: {exc}") from exc
        received_at = utc(self.clock())
        if pd.Timestamp(received_at) < pd.Timestamp(requested_at):
            raise YahooSourceError(f"{symbol} stock history: receipt precedes request")
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise YahooSourceError(f"{symbol} stock history: empty response")
        exchange_tz = metadata.get("exchangeTimezoneName")
        reported_symbol = metadata.get("symbol") or getattr(ticker, "ticker", symbol)
        if str(reported_symbol).upper() != symbol:
            raise YahooSourceError(f"{symbol} stock history: symbol identity mismatch")
        if not exchange_tz:
            raise YahooSourceError(f"{symbol} stock history: missing exchange timezone")
        if exchange_tz not in {"America/New_York","US/Eastern"}:
            raise YahooSourceError(f"{symbol} stock history: unexpected exchange timezone {exchange_tz}")
        try:
            zone = ZoneInfo(exchange_tz)
        except Exception as exc:
            raise YahooSourceError(f"{symbol} stock history: invalid exchange timezone {exchange_tz}") from exc
        rows, issues = [], []
        previous = None
        seen = set()
        for index, item in frame.iterrows():
            raw_stamp = str(index)
            try:
                stamp = pd.Timestamp(index)
                if stamp.tzinfo is None:
                    raise ValueError("timestamp_missing_timezone")
                start = stamp.tz_convert("UTC")
                end = start + pd.Timedelta(minutes=5)
                local = stamp.tz_convert(zone)
                schedule = market_schedule(symbol,local.date().isoformat())
                reason = None
                if previous is not None and start <= previous:
                    reason = "non_monotonic_or_duplicate_bar"
                elif start.isoformat() in seen:
                    reason = "duplicate_bar"
                elif end > pd.Timestamp(received_at):
                    reason = "incomplete_current_or_future_bar"
                elif schedule is None or start < schedule[0] or end > schedule[1]:
                    reason = "outside_exchange_calendar_session"
                elif (start-schedule[0]).total_seconds() % 300 != 0:
                    reason = "unaligned_five_minute_bar"
                vals = {key: number(item.get(key)) for key in ("Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits")}
                if reason is None and any(vals[key] is None for key in ("Open", "High", "Low", "Close")):
                    reason = "missing_or_nonfinite_ohlc"
                if reason is None and (vals["Low"] > min(vals["Open"], vals["Close"]) or vals["High"] < max(vals["Open"], vals["Close"]) or vals["Low"] > vals["High"] or vals["Low"] <= 0):
                    reason = "inconsistent_ohlc"
                if reason is None and vals["Volume"] is not None and vals["Volume"] < 0:
                    reason = "negative_volume"
                if reason is None and vals["Volume"] is not None and not vals["Volume"].is_integer():
                    reason = "noninteger_volume"
                if reason is None and any(nonfinite_present(item.get(k)) for k in ("Open","High","Low","Close","Adj Close","Volume","Dividends","Stock Splits")):
                    reason = "nonfinite_stock_field"
                if reason is None and vals["Adj Close"] is not None and vals["Adj Close"] <= 0:
                    reason = "invalid_adjusted_close"
                if reason is None and (vals["Dividends"] is not None and vals["Dividends"] < 0 or vals["Stock Splits"] is not None and vals["Stock Splits"] < 0):
                    reason = "invalid_corporate_action"
                previous = start
                seen.add(start.isoformat())
                if reason:
                    issues.append({"key": raw_stamp, "reason": reason})
                    continue
                rows.append({"symbol": symbol, "bar_start_utc": start.isoformat(), "bar_end_utc": end.isoformat(),
                             "session_date": local.date().isoformat(), "exchange_timezone": exchange_tz,
                             "open": vals["Open"], "high": vals["High"], "low": vals["Low"], "close": vals["Close"],
                             "adjusted_close": vals["Adj Close"], "volume": integer(vals["Volume"]),
                             "dividends": vals["Dividends"], "split_ratio": vals["Stock Splits"],
                             "received_at": received_at, "source": "yahoo", "source_version": SOURCE_VERSION,
                             "library_version": self.library_version, "exchange_calendar":CALENDAR_BY_SYMBOL[symbol],
                             "calendar_version":CALENDAR_VERSION})
            except (ValueError, TypeError) as exc:
                issues.append({"key": raw_stamp, "reason": str(exc)})
        return SourceResponse(symbol, "history", {**request, "requested_at": requested_at}, received_at,
                              frame_hash(frame), rows, issues,
                              {"exchange_timezone": exchange_tz, "exchange_calendar":CALENDAR_BY_SYMBOL[symbol],
                               "calendar_version":CALENDAR_VERSION, "rows_received": len(frame), "source": "yahoo", "library_version": self.library_version})

    def expiries(self, symbol):
        ticker = self._ticker(symbol)
        requested_at = utc(self.clock())
        try:
            values = tuple(ticker.options)
        except Exception as exc:
            raise YahooSourceError(f"{symbol} expiry list: {type(exc).__name__}: {exc}") from exc
        if not values:
            raise YahooSourceError(f"{symbol} expiry list: empty response")
        received_at=utc(self.clock())
        if pd.Timestamp(received_at)<pd.Timestamp(requested_at):
            raise YahooSourceError(f"{symbol} expiry list: receipt precedes request")
        return ticker, values, {"method": "Ticker.options", "symbol": symbol, "requested_at": requested_at,
                                "received_at": received_at, "response_hash": hashlib.sha256(json.dumps(values).encode()).hexdigest()}

    def chain(self, symbol, expiry, ticker=None):
        ticker = ticker or self._ticker(symbol)
        requested_at = utc(self.clock())
        try:
            chain = ticker.option_chain(expiry)
        except Exception as exc:
            raise YahooSourceError(f"{symbol} option expiry {expiry}: {type(exc).__name__}: {exc}") from exc
        received_at = utc(self.clock())
        if pd.Timestamp(received_at) < pd.Timestamp(requested_at):
            raise YahooSourceError(f"{symbol} option expiry {expiry}: receipt precedes request")
        frames = {"call": chain.calls, "put": chain.puts}
        if any(not isinstance(f, pd.DataFrame) for f in frames.values()):
            raise YahooSourceError(f"{symbol} option expiry {expiry}: invalid side response")
        response_hash = hashlib.sha256("".join(frame_hash(f) for f in frames.values()).encode()).hexdigest()
        rows, issues = [], []
        seen = set()
        fetch_date = pd.Timestamp(received_at).tz_convert("America/New_York").date()
        try:
            expiry_date = pd.Timestamp(expiry).date()
            dte = (expiry_date-fetch_date).days
        except Exception as exc:
            raise YahooSourceError(f"{symbol} option expiry {expiry}: invalid expiry") from exc
        if dte < 0:
            raise YahooSourceError(f"{symbol} option expiry {expiry}: expired")
        group = "research_0_6" if dte <= 6 else "main_7_90" if dte <= 90 else "secondary_91_180" if dte <= 180 else "outside_180"
        underlying = getattr(chain, "underlying", None) or {}
        underlying_price = number(underlying.get("regularMarketPrice") or underlying.get("price")) if isinstance(underlying, dict) else None
        for side, frame in frames.items():
            for _, item in frame.iterrows():
                contract = str(item.get("contractSymbol") or "")
                key = f"{symbol}:{expiry}:{side}:{contract}"
                reason = None
                last_trade = item.get("lastTradeDate")
                try:
                    trade_utc = utc(last_trade) if last_trade is not None and not pd.isna(last_trade) else None
                except ValueError:
                    trade_utc = None
                    reason = "invalid_last_trade_timestamp"
                vals = {name: number(item.get(name)) for name in ("strike", "lastPrice", "bid", "ask", "volume", "openInterest", "impliedVolatility")}
                match = re.fullmatch(re.escape(symbol)+r"(\d{6})([CP])(\d{8})", contract)
                if not match or match.group(1) != expiry_date.strftime("%y%m%d") or match.group(2) != ("C" if side=="call" else "P"):
                    reason = "invalid_contract_symbol"
                elif key in seen:
                    reason = "duplicate_contract_key"
                elif vals["strike"] is None or vals["strike"] <= 0:
                    reason = "invalid_strike"
                elif any(vals[k] is not None and vals[k] < 0 for k in ("lastPrice", "bid", "ask", "volume", "openInterest", "impliedVolatility")):
                    reason = "negative_price_volume_oi_or_iv"
                elif any(vals[k] is not None and not vals[k].is_integer() for k in ("volume","openInterest")):
                    reason = "noninteger_volume_or_oi"
                elif any(nonfinite_present(item.get(k)) for k in ("lastPrice","bid","ask","volume","openInterest","impliedVolatility")):
                    reason = "nonfinite_option_field"
                elif vals["bid"] is not None and vals["ask"] is not None and vals["bid"] > vals["ask"]:
                    reason = "bid_exceeds_ask"
                elif vals["lastPrice"] is None and vals["bid"] is None and vals["ask"] is None:
                    reason = "missing_all_prices"
                elif vals["impliedVolatility"] is None:
                    reason = "missing_or_nonfinite_implied_volatility"
                elif trade_utc is not None and pd.Timestamp(trade_utc) > pd.Timestamp(received_at):
                    reason = "future_last_trade_timestamp"
                seen.add(key)
                if reason:
                    issues.append({"key": key, "reason": reason})
                    continue
                stale = trade_utc is None or (pd.Timestamp(received_at)-pd.Timestamp(trade_utc)).total_seconds() > 900
                rows.append({"symbol": symbol, "contract_symbol": contract, "side": side, "strike": vals["strike"],
                             "expiry": expiry_date.isoformat(), "dte": dte, "dte_group": group,
                             "contract_size": item.get("contractSize") if isinstance(item.get("contractSize"), str) else None,
                             "last_trade_utc": trade_utc, "last_price": vals["lastPrice"], "bid": vals["bid"], "ask": vals["ask"],
                             "volume": integer(vals["volume"]), "open_interest": integer(vals["openInterest"]),
                             "implied_volatility": vals["impliedVolatility"], "in_the_money": bool(item.get("inTheMoney")) if not pd.isna(item.get("inTheMoney")) else None,
                             "underlying_price": underlying_price, "fetch_at": received_at, "source": "yahoo",
                             "source_version": SOURCE_VERSION, "library_version": self.library_version,
                             "vendor_delta": None, "quote_age_uncertain": True, "last_trade_stale": stale})
        return SourceResponse(symbol, "chain", {"method": "Ticker.option_chain", "symbol": symbol, "expiry": expiry,
                               "requested_at": requested_at}, received_at, response_hash, rows, issues,
                              {"rows_received": sum(map(len, frames.values())), "expiry": expiry,
                               "side_counts": {k: len(v) for k,v in frames.items()}, "dte_group": group,
                               "source": "yahoo", "library_version": self.library_version})
