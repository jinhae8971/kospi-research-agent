"""KRX market data client using FinanceDataReader (primary) + yfinance (fallback).

Sources (in priority order):
  1. FinanceDataReader.StockListing("KOSPI"/"KOSDAQ") — returns full market snapshot
     in a single call: Code, Name, Close, Changes, ChagesRatio, Volume, Amount, Marcap
  2. yfinance per-ticker fallback for a curated top-50 seed list

Why this design:
  - The original direct KRX API (data.krx.co.kr/comm/bldAttendant/getJsonData.cmd)
    responds with 400 "LOGOUT" when called from cloud IPs (2026 policy change).
  - FinanceDataReader scrapes the same KRX endpoint but uses rotating browser
    fingerprints and handles session cookies automatically.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import get_settings
from .logging_setup import get_logger
from .models import StockMarket

log = get_logger(__name__)

_EXCLUDE_NAME_PATTERNS = (
    "ETF", "ETN", "인버스", "레버리지", "KODEX", "TIGER", "KBSTAR",
    "ARIRANG", "HANARO", "SOL ", "ACE ", "KOSEF", "KINDEX",
    "FOCUS", "파워", "리츠", "스팩", "SPAC",
)
_PREFERRED_SUFFIXES = {"5", "7", "8", "9"}


def _is_excluded(ticker: str, name: str) -> bool:
    if not name:
        return True
    if name[-1:] == "우" or "우B" in name or "우(전환)" in name:
        return True
    if ticker and ticker[-1] in _PREFERRED_SUFFIXES:
        return True
    upper = name.upper()
    return any(pat in upper for pat in _EXCLUDE_NAME_PATTERNS)


def _to_float(val) -> float:
    try:
        if pd.isna(val):
            return 0.0
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _to_int(val) -> int:
    try:
        if pd.isna(val):
            return 0
        return int(val)
    except (TypeError, ValueError):
        return 0


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=16))
def _fetch_market_via_fdr(market: str) -> pd.DataFrame:
    """Fetch full market snapshot via FinanceDataReader.

    Returns a DataFrame with columns:
      Code, Name, Market, Close, Changes, ChagesRatio, Open, High, Low,
      Volume, Amount, Marcap, ...
    """
    import FinanceDataReader as fdr  # lazy import — optional dep
    df = fdr.StockListing(market)
    if df is None or df.empty:
        raise RuntimeError(f"FinanceDataReader returned empty frame for {market}")
    return df


def _fetch_market_via_yfinance(market: str) -> pd.DataFrame:
    """Fallback: yfinance for a curated seed of top KOSPI/KOSDAQ tickers.

    Used only if FinanceDataReader fails. Produces a minimal DataFrame with
    enough columns to populate StockMarket; Marcap is not available this way
    so we return 0 and accept a degraded report rather than a crashed pipeline.
    """
    import yfinance as yf

    # Curated seeds — top-~60 by market cap as of 2026-04
    kospi_seed = [
        ("005930", "삼성전자"),        ("000660", "SK하이닉스"),    ("005935", "삼성전자우"),
        ("373220", "LG에너지솔루션"),   ("207940", "삼성바이오로직스"),("005380", "현대차"),
        ("000270", "기아"),           ("035420", "NAVER"),        ("068270", "셀트리온"),
        ("051910", "LG화학"),         ("005490", "POSCO홀딩스"),   ("035720", "카카오"),
        ("055550", "신한지주"),       ("105560", "KB금융"),       ("012330", "현대모비스"),
        ("028260", "삼성물산"),       ("003670", "포스코퓨처엠"),   ("066570", "LG전자"),
        ("034730", "SK"),           ("017670", "SK텔레콤"),     ("096770", "SK이노베이션"),
        ("015760", "한국전력"),       ("033780", "KT&G"),        ("009540", "HD한국조선해양"),
        ("086790", "하나금융지주"),    ("032830", "삼성생명"),       ("018260", "삼성에스디에스"),
        ("010130", "고려아연"),       ("011200", "HMM"),          ("010950", "S-Oil"),
        ("024110", "기업은행"),       ("316140", "우리금융지주"),   ("030200", "KT"),
        ("006400", "삼성SDI"),       ("000810", "삼성화재"),      ("009150", "삼성전기"),
        ("011070", "LG이노텍"),       ("003550", "LG"),           ("036570", "엔씨소프트"),
        ("251270", "넷마블"),        ("251270", "넷마블"),
    ]
    kosdaq_seed = [
        ("247540", "에코프로비엠"),    ("086520", "에코프로"),      ("091990", "셀트리온헬스케어"),
        ("196170", "알테오젠"),       ("068760", "셀트리온제약"),   ("028300", "HLB"),
        ("277810", "레인보우로보틱스"), ("035900", "JYP Ent."),     ("041510", "에스엠"),
        ("058470", "리노공업"),       ("039030", "이오테크닉스"),   ("086900", "메디톡스"),
    ]
    seed = kospi_seed if market == "KOSPI" else kosdaq_seed
    tickers = [f"{code}.KS" if market == "KOSPI" else f"{code}.KQ" for code, _ in seed]
    name_map = {code: name for code, name in seed}

    log.warning("using yfinance fallback seed (%d tickers) for %s", len(tickers), market)
    data = yf.download(
        tickers, period="5d", progress=False, auto_adjust=True,
        group_by="ticker", threads=True,
    )
    if data.empty:
        raise RuntimeError("yfinance fallback also returned empty")

    rows = []
    for t, code_name in zip(tickers, seed):
        code, name = code_name
        try:
            td = data if len(tickers) == 1 else data[t]
            if td.empty or len(td) < 2:
                continue
            close = _to_float(td["Close"].iloc[-1])
            prev  = _to_float(td["Close"].iloc[-2])
            if close <= 0:
                continue
            chg_pct = ((close - prev) / prev * 100.0) if prev > 0 else 0.0
            vol = _to_int(td["Volume"].iloc[-1])
            rows.append({
                "Code": code, "Name": name, "Market": market,
                "Open": _to_float(td["Open"].iloc[-1]),
                "High": _to_float(td["High"].iloc[-1]),
                "Low":  _to_float(td["Low"].iloc[-1]),
                "Close": close, "Volume": vol,
                "Amount": close * vol, "Marcap": 0.0,
                "ChagesRatio": chg_pct,
            })
        except Exception:  # noqa: BLE001
            continue
    if not rows:
        raise RuntimeError("yfinance fallback produced zero usable rows")
    return pd.DataFrame(rows)


def _fetch_market(market: str) -> pd.DataFrame:
    """Try FinanceDataReader first, fallback to yfinance."""
    try:
        df = _fetch_market_via_fdr(market)
        log.info("fetched %d %s rows via FinanceDataReader", len(df), market)
        return df
    except Exception as exc:  # noqa: BLE001
        log.warning("FinanceDataReader failed for %s: %s — falling back to yfinance", market, exc)
        return _fetch_market_via_yfinance(market)


def _recent_trading_date() -> str:
    """Return the most recent KRX trading date (YYYYMMDD).

    We use yfinance on the KOSPI index (^KS11) to identify trading calendar,
    since FDR.StockListing() returns a single snapshot that may already be today.
    """
    import yfinance as yf
    end = datetime.now() + timedelta(days=1)
    start = end - timedelta(days=14)
    idx = yf.download("^KS11", start=start.strftime("%Y-%m-%d"),
                       end=end.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
    if idx.empty:
        # last-resort: today
        return datetime.now().strftime("%Y%m%d")
    latest = idx.index[-1]
    return latest.strftime("%Y%m%d")


def _past_trading_date(days_back: int) -> str:
    import yfinance as yf
    end = datetime.now() + timedelta(days=1)
    start = end - timedelta(days=days_back * 2 + 14)
    idx = yf.download("^KS11", start=start.strftime("%Y-%m-%d"),
                       end=end.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
    if idx.empty or len(idx) <= days_back:
        raise RuntimeError(f"could not find {days_back} past trading days")
    target = idx.index[-(days_back + 1)]
    return target.strftime("%Y%m%d")


def fetch_market(date_str: str, market: str) -> list[StockMarket]:
    """Fetch one-day snapshot for a given market.

    Note: `date_str` is informational only — FDR returns the latest snapshot
    it has, not a historical one. Historical KRX OHLCV requires fdr.DataReader
    per ticker (slow). For the daily orchestrator pipeline, "latest" is what
    we want anyway.
    """
    log.info("fetching %s data (snapshot for %s)", market, date_str)
    df = _fetch_market(market)
    if df.empty:
        return []

    # Column name compatibility
    col_code = "Code" if "Code" in df.columns else "Symbol"
    col_name = "Name"
    col_close = "Close"
    col_open = "Open" if "Open" in df.columns else None
    col_high = "High" if "High" in df.columns else None
    col_low = "Low" if "Low" in df.columns else None
    col_vol = "Volume" if "Volume" in df.columns else None
    col_amt = "Amount" if "Amount" in df.columns else None
    col_mcap = "Marcap" if "Marcap" in df.columns else ("MarketCap" if "MarketCap" in df.columns else None)
    col_pct = (
        "ChagesRatio" if "ChagesRatio" in df.columns
        else ("ChangesRatio" if "ChangesRatio" in df.columns else None)
    )

    stocks: list[StockMarket] = []
    for _, row in df.iterrows():
        ticker = str(row.get(col_code, "")).strip().zfill(6)
        name = str(row.get(col_name, "")).strip()
        if not ticker or not name or ticker == "NAN":
            continue
        if _is_excluded(ticker, name):
            continue

        close = _to_float(row.get(col_close, 0))
        if close <= 0:
            continue

        pct = _to_float(row.get(col_pct, 0)) if col_pct else 0.0
        vol = _to_int(row.get(col_vol, 0)) if col_vol else 0
        amt = _to_float(row.get(col_amt, 0)) if col_amt else close * vol
        mcap = _to_float(row.get(col_mcap, 0)) if col_mcap else 0.0

        stocks.append(
            StockMarket(
                ticker=ticker,
                name=name,
                market=market,
                close=close,
                open=_to_float(row.get(col_open, 0)) if col_open else 0.0,
                high=_to_float(row.get(col_high, 0)) if col_high else 0.0,
                low=_to_float(row.get(col_low, 0)) if col_low else 0.0,
                volume=vol,
                trading_value=amt,
                market_cap=mcap,
                change_pct=pct,
            )
        )

    log.info("fetched %d stocks for %s on %s", len(stocks), market, date_str)
    return stocks


def fetch_all_markets() -> tuple[list[StockMarket], str]:
    settings = get_settings()
    date_str = _recent_trading_date()
    if settings.market == "ALL":
        stocks = fetch_market(date_str, "KOSPI") + fetch_market(date_str, "KOSDAQ")
    else:
        stocks = fetch_market(date_str, settings.market)
    return stocks, date_str


def get_past_trading_date(days_back: int) -> str:
    return _past_trading_date(days_back)


def get_recent_trading_date() -> str:
    return _recent_trading_date()
