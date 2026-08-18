#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IBKR Gateway 多品种 H1 技术分析绘图
====================================

默认连接：127.0.0.1:4002

用法：
    # 不带参数：显示中文品种、查询代码及 IBKR 合约信息
    python xauusd_technical_analysis_chart.py

    # 带品种：只跑指定品种
    python xauusd_technical_analysis_chart.py XAUUSD
    python xauusd_technical_analysis_chart.py EURUSD

    # 也支持一次指定多个
    python xauusd_technical_analysis_chart.py XAUUSD EURUSD VIX

    # 查看内置品种
    python xauusd_technical_analysis_chart.py --list

    # 批量运行默认品种
    python xauusd_technical_analysis_chart.py --batch

依赖：
    pandas, numpy, matplotlib, ibapi

说明：
- OHLC 直接从 IB Gateway/TWS API 的 reqHistoricalData 拉取；不再使用模拟行情。
- 当前价格优先使用 IB 行情快照；快照不可用时回退为最后一根 H1 K 线 Close。
- 外汇没有有效成交量时，不伪造成交量；副图自动改用 MACD Histogram。
- 支撑/阻力、HH/HL/LH/LL、趋势线、背离全部根据当前数据自动计算，
  不再写死 XAUUSD 的价位或日期。
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import Rectangle

try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
except ImportError as exc:
    raise SystemExit(
        "缺少 IBKR Python API (ibapi)。请先安装 TWS API Python client，"
        "并确认 Python 能 import ibapi。\n"
        f"原始错误: {exc}"
    )


# ============================================================================
# 基本配置
# ============================================================================
IB_HOST = "127.0.0.1"
IB_PORT = 4002
IB_CLIENT_ID = 86

TIMEFRAME_LABEL = "1H"
BAR_SIZE = "1 hour"
DURATION = "2 M"            # 为 SHOW_LAST_N 留足 H1 数据
SHOW_LAST_N = 280
OUTPUT_DIR = "ibkr_technical_output"
REQUEST_TIMEOUT = 20.0
SNAPSHOT_TIMEOUT = 2.0
OUTPUT_WIDTH_PX = 1440
OUTPUT_HEIGHT_PX = 2560
OUTPUT_DPI = 180

# 默认批量品种。某品种因行情权限/合约不可用失败时，只跳过该品种。
DEFAULT_SYMBOLS = [
    # Metals: GC is COMEX gold continuous future; XAUUSD remains available as spot gold.
    "GC", "XAGUSD",

    # Major/cross FX (6-letter FX symbols are also auto-supported even if not listed here)
    "EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "USDCHF", "USDCAD",
    "EURJPY", "EURGBP", "EURCHF", "EURAUD", "EURCAD",
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD",
    "AUDJPY", "AUDCHF", "AUDCAD",
    "CADJPY", "CADCHF", "CHFJPY",

    # Macro / indices / continuous futures
    "VIX", "SPX", "NDX", "DAX", "N225",
    "DXY", "WTI", "BRENT", "NATGAS", "GC", "SI", "COPPER", "US10Y", "US02Y",
    "ES", "YM", "NQ", "RTY",
]

FX_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "CNH", "HKD", "SGD"
}

# 非 FX 品种的 IBKR Contract 映射。
# CONTFUT 适合连续历史分析；如你的账户/交易所定义不同，可只修改这里。
SPECIAL_CONTRACTS: Dict[str, Dict[str, str]] = {
    # Continuous futures are appropriate for uninterrupted historical analysis.
    # IBKR does not provide real-time data for CONTFUT, so the script will
    # naturally fall back to the latest H1 Close when a snapshot is unavailable.
    "GC":     {"symbol": "GC",     "secType": "CONTFUT", "exchange": "COMEX", "currency": "USD", "what": "TRADES"},
    "SI":     {"symbol": "SI",     "secType": "CONTFUT", "exchange": "COMEX", "currency": "USD", "what": "TRADES"},
    "XAUUSD": {"symbol": "XAUUSD", "secType": "CMDTY", "exchange": "SMART", "currency": "USD", "what": "MIDPOINT"},
    "XAGUSD": {"symbol": "XAGUSD", "secType": "CMDTY", "exchange": "SMART", "currency": "USD", "what": "MIDPOINT"},

    "VIX":     {"symbol": "VIX",  "secType": "IND",     "exchange": "CBOE",    "currency": "USD", "what": "TRADES"},
    "SPX":     {"symbol": "SPX",  "secType": "IND",     "exchange": "CBOE",    "currency": "USD", "what": "TRADES"},
    "US500":   {"symbol": "SPX",  "secType": "IND",     "exchange": "CBOE",    "currency": "USD", "what": "TRADES"},
    "NDX":     {"symbol": "NDX",  "secType": "IND",     "exchange": "NASDAQ",  "currency": "USD", "what": "TRADES"},
    "USTEC":   {"symbol": "NDX",  "secType": "IND",     "exchange": "NASDAQ",  "currency": "USD", "what": "TRADES"},
    "DAX":     {"symbol": "DAX",  "secType": "IND",     "exchange": "EUREX",   "currency": "EUR", "what": "TRADES"},
    "DE40":    {"symbol": "DAX",  "secType": "IND",     "exchange": "EUREX",   "currency": "EUR", "what": "TRADES"},
    "N225":    {"symbol": "N225", "secType": "IND",     "exchange": "OSE.JPN", "currency": "JPY", "what": "TRADES"},
    "JP225":   {"symbol": "N225", "secType": "IND",     "exchange": "OSE.JPN", "currency": "JPY", "what": "TRADES"},

    "DXY":     {"symbol": "DX", "secType": "CONTFUT", "exchange": "NYBOT", "currency": "USD", "what": "TRADES"},
    "WTI":     {"symbol": "CL", "secType": "CONTFUT", "exchange": "NYMEX", "currency": "USD", "what": "TRADES"},
    "BRENT":   {"symbol": "BZ", "secType": "CONTFUT", "exchange": "NYMEX", "currency": "USD", "what": "TRADES"},
    "NATGAS":  {"symbol": "NG", "secType": "CONTFUT", "exchange": "NYMEX", "currency": "USD", "what": "TRADES"},
    "COPPER":  {"symbol": "HG", "secType": "CONTFUT", "exchange": "COMEX", "currency": "USD", "what": "TRADES"},
    "CORN":    {"symbol": "ZC", "secType": "CONTFUT", "exchange": "CBOT",  "currency": "USD", "what": "TRADES"},
    "WHEAT":   {"symbol": "ZW", "secType": "CONTFUT", "exchange": "CBOT",  "currency": "USD", "what": "TRADES"},
    "SOYBEAN": {"symbol": "ZS", "secType": "CONTFUT", "exchange": "CBOT",  "currency": "USD", "what": "TRADES"},
    "COFFEE":  {"symbol": "KC", "secType": "CONTFUT", "exchange": "NYBOT", "currency": "USD", "what": "TRADES"},
    "SUGAR":   {"symbol": "SB", "secType": "CONTFUT", "exchange": "NYBOT", "currency": "USD", "what": "TRADES"},

    "ES":      {"symbol": "ES", "secType": "CONTFUT", "exchange": "CME",   "currency": "USD", "what": "TRADES"},
    "YM":      {"symbol": "YM", "secType": "CONTFUT", "exchange": "CBOT",  "currency": "USD", "what": "TRADES"},
    "NQ":      {"symbol": "NQ", "secType": "CONTFUT", "exchange": "CME",   "currency": "USD", "what": "TRADES"},
    "RTY":     {"symbol": "RTY","secType": "CONTFUT", "exchange": "CME",   "currency": "USD", "what": "TRADES"},
    "US10Y":   {"symbol": "ZN", "secType": "CONTFUT", "exchange": "CBOT",  "currency": "USD", "what": "TRADES"},
    "US02Y":   {"symbol": "ZT", "secType": "CONTFUT", "exchange": "CBOT",  "currency": "USD", "what": "TRADES"},
}

ALIASES = {
    "GOLD": "GC", "GOLD_FUT": "GC", "GOLDSPOT": "XAUUSD", "XAU/USD": "XAUUSD",
    "SILVER": "SI", "SILVER_FUT": "SI", "XAG/USD": "XAGUSD",
    "S&P500": "ES", "SP500": "ES", "US500": "ES",
    "NASDAQ100": "NDX", "NAS100": "NDX", "USTEC": "USTEC",
    "DE40": "DE40", "GER40": "DE40",
    "JP225": "JP225", "NIKKEI": "JP225",
    "OIL": "WTI", "CL": "WTI", "CRUDE": "WTI", "BZ": "BRENT", "BRENT_OIL": "BRENT", "NG": "NATGAS",
    "HG": "COPPER",
    "DOW": "YM", "DJI": "YM", "YM": "YM", "ES": "ES", "NQ": "NQ", "RUSSELL": "RTY",
    "ZC": "CORN", "ZW": "WHEAT", "ZS": "SOYBEAN", "KC": "COFFEE", "SB": "SUGAR",
    "黄金": "GC", "白银": "SI", "铜": "COPPER", "原油": "WTI", "布伦特": "BRENT", "天然气": "NATGAS",
    "标普": "ES", "标普500": "ES", "道琼斯": "YM", "纳斯达克": "NQ", "罗素2000": "RTY",
    "玉米": "CORN", "小麦": "WHEAT", "大豆": "SOYBEAN", "咖啡": "COFFEE", "糖": "SUGAR",
    "10Y": "US10Y", "2Y": "US02Y",
}

# 无参数/--list 时展示；代码可直接作为命令行参数，也接受常见中文名。
INSTRUMENT_CATALOG = [
    ("贵金属", "黄金期货", "GC"), ("贵金属", "黄金现货", "XAUUSD"),
    ("贵金属", "白银期货", "SI"), ("贵金属", "白银现货", "XAGUSD"),
    ("工业金属", "COMEX 铜", "COPPER"),
    ("能源", "WTI 原油", "WTI"), ("能源", "布伦特原油", "BRENT"), ("能源", "天然气", "NATGAS"),
    ("股指期货", "标普 500 E-mini", "ES"), ("股指期货", "道琼斯 E-mini", "YM"),
    ("股指期货", "纳斯达克 100 E-mini", "NQ"), ("股指期货", "罗素 2000 E-mini", "RTY"),
    ("股指现货", "标普 500 指数", "SPX"), ("股指现货", "纳斯达克 100 指数", "NDX"), ("股指现货", "VIX 波动率指数", "VIX"),
    ("农产品", "玉米", "CORN"), ("农产品", "小麦", "WHEAT"), ("农产品", "大豆", "SOYBEAN"),
    ("农产品", "咖啡", "COFFEE"), ("农产品", "原糖", "SUGAR"),
    ("宏观", "美元指数", "DXY"), ("利率", "美国 10 年期国债", "US10Y"), ("利率", "美国 2 年期国债", "US02Y"),
]

# Theme
BG_COLOR = "#131722"
GRID_COLOR = "#2a2e39"
TEXT_COLOR = "#d1d4dc"
UP_COLOR = "#26a69a"
DOWN_COLOR = "#ef5350"
LINE_COLOR = "#787b86"
ZONE_SUPPORT = (38/255, 166/255, 154/255, 0.10)
ZONE_RESIST = (239/255, 83/255, 80/255, 0.10)


# ============================================================================
# IB Gateway client
# ============================================================================
@dataclass
class ContractSpec:
    display_symbol: str
    contract: Contract
    what_to_show: str


class IBGateway(EWrapper, EClient):
    def __init__(self):
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._req_lock = threading.Lock()
        self._next_req_id = 1000

        self.hist_bars: Dict[int, list] = {}
        self.hist_events: Dict[int, threading.Event] = {}
        self.errors: Dict[int, Tuple[int, str]] = {}

        self.snap_ticks: Dict[int, Dict[int, float]] = {}
        self.snap_events: Dict[int, threading.Event] = {}

    def nextValidId(self, orderId: int):
        self.ready.set()

    def connectionClosed(self):
        self.ready.clear()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # 常见 farm/status 消息不是请求失败
        informational = {2104, 2106, 2107, 2108, 2158}
        if errorCode not in informational:
            if reqId is not None and reqId >= 0:
                self.errors[reqId] = (int(errorCode), str(errorString))
                if reqId in self.hist_events:
                    self.hist_events[reqId].set()
                # 对 snapshot 某些权限错误，不立即终止，允许 delayed/fallback
                if reqId in self.snap_events and errorCode in {200, 354, 10167, 10168}:
                    self.snap_events[reqId].set()
            else:
                print(f"[IB ERROR] code={errorCode}: {errorString}", file=sys.stderr)

    def historicalData(self, reqId, bar):
        self.hist_bars.setdefault(reqId, []).append(bar)

    def historicalDataEnd(self, reqId, start, end):
        ev = self.hist_events.get(reqId)
        if ev:
            ev.set()

    def tickPrice(self, reqId, tickType, price, attrib):
        try:
            p = float(price)
        except Exception:
            return
        if p > 0 and math.isfinite(p):
            self.snap_ticks.setdefault(reqId, {})[int(tickType)] = p

    def tickSnapshotEnd(self, reqId: int):
        ev = self.snap_events.get(reqId)
        if ev:
            ev.set()

    def alloc_req_id(self) -> int:
        with self._req_lock:
            self._next_req_id += 1
            return self._next_req_id

    def connect_and_start(self, host: str, port: int, client_id: int, timeout: float = 10.0):
        self.connect(host, port, clientId=client_id)
        self._thread = threading.Thread(target=self.run, name="ibapi-loop", daemon=True)
        self._thread.start()
        if not self.ready.wait(timeout):
            self.disconnect()
            raise ConnectionError(
                f"无法连接 IB Gateway {host}:{port} (clientId={client_id})。"
                "请确认 Gateway 已登录并启用 API socket。"
            )

    def get_historical(
        self,
        spec: ContractSpec,
        duration: str,
        bar_size: str,
        timeout: float = REQUEST_TIMEOUT,
    ) -> pd.DataFrame:
        req_id = self.alloc_req_id()
        ev = threading.Event()
        self.hist_events[req_id] = ev
        self.hist_bars[req_id] = []
        self.errors.pop(req_id, None)

        self.reqHistoricalData(
            req_id,
            spec.contract,
            "",                 # endDateTime = now
            duration,
            bar_size,
            spec.what_to_show,
            0,                  # useRTH=0: include full session
            2,                  # formatDate=2: intraday epoch seconds
            False,              # keepUpToDate
            [],
        )

        if not ev.wait(timeout):
            try:
                self.cancelHistoricalData(req_id)
            except Exception:
                pass
            raise TimeoutError(f"{spec.display_symbol}: historical data timeout ({timeout:.0f}s)")

        err = self.errors.get(req_id)
        bars = self.hist_bars.get(req_id, [])

        self.hist_events.pop(req_id, None)
        self.hist_bars.pop(req_id, None)

        if not bars:
            if err:
                raise RuntimeError(f"IB {err[0]}: {err[1]}")
            raise RuntimeError("IB 未返回历史 K 线")

        rows = []
        for b in bars:
            dt = parse_ib_bar_time(b.date)
            rows.append({
                "Date": dt,
                "Open": float(b.open),
                "High": float(b.high),
                "Low": float(b.low),
                "Close": float(b.close),
                "Volume": safe_float(getattr(b, "volume", np.nan)),
            })

        df = pd.DataFrame(rows).dropna(subset=["Date", "Open", "High", "Low", "Close"])
        if df.empty:
            raise RuntimeError("IB 返回了数据，但无法解析为 OHLC")
        df = df.drop_duplicates(subset=["Date"], keep="last").set_index("Date").sort_index()
        return df

    def get_snapshot_price(self, spec: ContractSpec, timeout: float = SNAPSHOT_TIMEOUT) -> Optional[float]:
        """优先 live/frozen，失败时 delayed；最终由调用方回退到最后一根 H1 Close。"""
        for market_data_type in (1, 3):  # 1=live, 3=delayed
            req_id = self.alloc_req_id()
            ev = threading.Event()
            self.snap_events[req_id] = ev
            self.snap_ticks[req_id] = {}
            self.errors.pop(req_id, None)
            try:
                self.reqMarketDataType(market_data_type)
                self.reqMktData(req_id, spec.contract, "", True, False, [])
                ev.wait(timeout)
            finally:
                try:
                    self.cancelMktData(req_id)
                except Exception:
                    pass

            ticks = self.snap_ticks.pop(req_id, {})
            self.snap_events.pop(req_id, None)

            # live: bid=1 ask=2 last=4 close=9
            # delayed: bid=66 ask=67 last=68 close=75
            last = first_valid(ticks, [4, 68])
            bid = first_valid(ticks, [1, 66])
            ask = first_valid(ticks, [2, 67])
            close = first_valid(ticks, [9, 75])
            if last is not None:
                return last
            if bid is not None and ask is not None:
                return (bid + ask) / 2.0
            if close is not None:
                return close
        return None


def safe_float(v) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else np.nan
    except Exception:
        return np.nan


def first_valid(d: Dict[int, float], keys: List[int]) -> Optional[float]:
    for k in keys:
        v = d.get(k)
        if v is not None and v > 0 and math.isfinite(v):
            return float(v)
    return None


def parse_ib_bar_time(value) -> pd.Timestamp:
    # formatDate=2 intraday bars normally return Unix epoch seconds.
    s = str(value).strip()
    if re.fullmatch(r"\d{9,13}", s):
        iv = int(s)
        if iv > 10_000_000_000:  # milliseconds, just in case
            ts = pd.to_datetime(iv, unit="ms", utc=True)
        else:
            ts = pd.to_datetime(iv, unit="s", utc=True)
        return ts.tz_convert("Asia/Shanghai").tz_localize(None)

    # Defensive fallback for textual IB dates.
    cleaned = re.sub(r"\s+[A-Za-z_]+/[A-Za-z_]+$", "", s)
    for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return pd.Timestamp(pd.to_datetime(cleaned, format=fmt))
        except Exception:
            pass
    return pd.Timestamp(pd.to_datetime(cleaned, errors="coerce"))


# ============================================================================
# Contract builder
# ============================================================================
def normalize_symbol(symbol: str) -> str:
    s = symbol.strip().upper().replace(" ", "")
    return ALIASES.get(s, s)


def make_contract(symbol: str) -> ContractSpec:
    display = normalize_symbol(symbol)

    # FX auto resolver, e.g. EURUSD / USDJPY / NZDUSD
    compact = display.replace("/", "")
    if len(compact) == 6 and compact[:3] in FX_CURRENCIES and compact[3:] in FX_CURRENCIES:
        c = Contract()
        c.symbol = compact[:3]
        c.secType = "CASH"
        c.exchange = "IDEALPRO"
        c.currency = compact[3:]
        return ContractSpec(display_symbol=compact, contract=c, what_to_show="MIDPOINT")

    cfg = SPECIAL_CONTRACTS.get(display)
    if not cfg:
        raise ValueError(
            f"未知品种 {symbol!r}。6 字母外汇可自动识别；其他品种请在 SPECIAL_CONTRACTS 中添加 IB 合约映射。"
        )

    c = Contract()
    c.symbol = cfg["symbol"]
    c.secType = cfg["secType"]
    c.exchange = cfg["exchange"]
    c.currency = cfg["currency"]
    return ContractSpec(display_symbol=display, contract=c, what_to_show=cfg.get("what", "TRADES"))


# ============================================================================
# Technical analysis
# ============================================================================
def add_indicators(df: pd.DataFrame) -> Tuple[pd.DataFrame, str]:
    df = df.copy()
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["ATR14"] = tr.rolling(14, min_periods=3).mean()
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()

    volume = pd.to_numeric(df.get("Volume", pd.Series(index=df.index, dtype=float)), errors="coerce")
    valid_volume = volume.replace([np.inf, -np.inf], np.nan).dropna()
    has_real_volume = len(valid_volume) >= max(20, len(df) // 3) and (valid_volume > 0).mean() > 0.7

    if has_real_volume:
        v = volume.fillna(0).clip(lower=0).to_numpy(dtype=float)
        c = df["Close"].to_numpy(dtype=float)
        obv = np.zeros(len(df), dtype=float)
        for i in range(1, len(df)):
            if c[i] > c[i - 1]:
                obv[i] = obv[i - 1] + v[i]
            elif c[i] < c[i - 1]:
                obv[i] = obv[i - 1] - v[i]
            else:
                obv[i] = obv[i - 1]
        obv_s = pd.Series(obv, index=df.index)
        osc = obv_s - obv_s.rolling(20, min_periods=5).mean()
        df["OSC"] = osc.rolling(3, min_periods=1).mean()
        osc_name = "OBVOSC(20)"
    else:
        ema12 = df["Close"].ewm(span=12, adjust=False).mean()
        ema26 = df["Close"].ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        df["OSC"] = macd - signal
        osc_name = "MACD Histogram"

    return df, osc_name


def find_pivots(df: pd.DataFrame, window: int = 5) -> Tuple[List[int], List[int]]:
    hi = df["High"].to_numpy()
    lo = df["Low"].to_numpy()
    highs: List[int] = []
    lows: List[int] = []
    for i in range(window, len(df) - window):
        if hi[i] >= np.nanmax(hi[i-window:i+window+1]):
            highs.append(i)
        if lo[i] <= np.nanmin(lo[i-window:i+window+1]):
            lows.append(i)
    return highs, lows


def cluster_levels(df: pd.DataFrame, pivot_highs: List[int], pivot_lows: List[int]) -> List[Tuple[float, int]]:
    if df.empty:
        return []
    atr = float(df["ATR14"].dropna().iloc[-1]) if df["ATR14"].notna().any() else float((df["High"]-df["Low"]).median())
    current = float(df["Close"].iloc[-1])
    tol = max(atr * 0.35, abs(current) * 0.00035, 1e-9)

    points = [(float(df["High"].iloc[i]), i) for i in pivot_highs] + [(float(df["Low"].iloc[i]), i) for i in pivot_lows]
    if not points:
        return []
    points.sort(key=lambda x: x[0])

    clusters: List[List[Tuple[float, int]]] = []
    for price, idx in points:
        if not clusters:
            clusters.append([(price, idx)])
            continue
        center = np.mean([p for p, _ in clusters[-1]])
        if abs(price - center) <= tol:
            clusters[-1].append((price, idx))
        else:
            clusters.append([(price, idx)])

    levels = []
    n = max(len(df), 1)
    for cl in clusters:
        # recent pivots slightly higher weight
        weights = np.array([1.0 + idx / n for _, idx in cl], dtype=float)
        prices = np.array([p for p, _ in cl], dtype=float)
        level = float(np.average(prices, weights=weights))
        touches = len(cl)
        levels.append((level, touches))
    return levels


def choose_levels(levels: List[Tuple[float, int]], current: float, max_each_side: int = 3):
    supports = [(p, t) for p, t in levels if p < current]
    resists = [(p, t) for p, t in levels if p > current]
    # nearer levels first; ties favor more touches
    supports = sorted(supports, key=lambda x: (current - x[0], -x[1]))[:max_each_side]
    resists = sorted(resists, key=lambda x: (x[0] - current, -x[1]))[:max_each_side]
    return supports, resists


def structure_label(df: pd.DataFrame, highs: List[int], lows: List[int]) -> str:
    parts = []
    if len(highs) >= 2:
        h1, h2 = df["High"].iloc[highs[-2]], df["High"].iloc[highs[-1]]
        parts.append("HH" if h2 > h1 else "LH")
    if len(lows) >= 2:
        l1, l2 = df["Low"].iloc[lows[-2]], df["Low"].iloc[lows[-1]]
        parts.append("HL" if l2 > l1 else "LL")
    return "/".join(parts) if parts else "N/A"


def trend_label(df: pd.DataFrame) -> str:
    row = df.iloc[-1]
    c, e20, e50 = float(row["Close"]), float(row["EMA20"]), float(row["EMA50"])
    if c > e20 > e50:
        return "BULLISH"
    if c < e20 < e50:
        return "BEARISH"
    return "MIXED"


def detect_divergence(df: pd.DataFrame, highs: List[int], lows: List[int]) -> Optional[Tuple[str, int, int]]:
    osc = df["OSC"]
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        if pd.notna(osc.iloc[a]) and pd.notna(osc.iloc[b]):
            if df["High"].iloc[b] > df["High"].iloc[a] and osc.iloc[b] < osc.iloc[a]:
                return "BEARISH DIVERGENCE", a, b
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        if pd.notna(osc.iloc[a]) and pd.notna(osc.iloc[b]):
            if df["Low"].iloc[b] < df["Low"].iloc[a] and osc.iloc[b] > osc.iloc[a]:
                return "BULLISH DIVERGENCE", a, b
    return None


def price_decimals(price: float) -> int:
    a = abs(price)
    if a >= 1000:
        return 1
    if a >= 100:
        return 2
    if a >= 10:
        return 3
    if a >= 1:
        return 4
    return 5


def fmt_price(price: Optional[float], decimals: int) -> str:
    if price is None or not math.isfinite(price):
        return "N/A"
    return f"{price:.{decimals}f}"


def draw_right_profile(
    ax: plt.Axes,
    df: pd.DataFrame,
    x_start: float,
    width: float,
    bins: int = 42,
) -> Dict[str, object]:
    """Draw a right-side Volume Profile, falling back to a TPO time profile.

    IBKR spot/CFD and MIDPOINT bars commonly have no meaningful volume.  In
    that case we count bars by price (TPO) instead of presenting it as volume.
    """
    typical_price = ((df["High"] + df["Low"] + df["Close"]) / 3).to_numpy(dtype=float)
    volume = pd.to_numeric(df.get("Volume"), errors="coerce").fillna(0).clip(lower=0).to_numpy(dtype=float)
    real_volume = (volume > 0).mean() >= 0.70
    weights = volume if real_volume else np.ones(len(df), dtype=float)
    mode = "VP" if real_volume else "TPO"

    counts, edges = np.histogram(typical_price, bins=bins, weights=weights)
    centers = (edges[:-1] + edges[1:]) / 2
    heights = np.diff(edges) * 0.84
    if not np.any(counts > 0):
        return {"Mode": "N/A", "POC": np.nan, "VAH": np.nan, "VAL": np.nan}

    poc_idx = int(np.argmax(counts))
    poc = float(centers[poc_idx])
    scale = width / float(counts.max())
    bar_widths = counts * scale
    profile_color = "#4ea1ff" if real_volume else "#9d7cff"
    ax.barh(
        centers, bar_widths, height=heights, left=x_start,
        color=profile_color, alpha=0.48, edgecolor="none", zorder=1,
    )

    # Expand outwards from POC until 70% of volume/time is covered.
    included = {poc_idx}
    left, right = poc_idx - 1, poc_idx + 1
    target = float(counts.sum()) * 0.70
    covered = float(counts[poc_idx])
    while covered < target and (left >= 0 or right < len(counts)):
        left_count = counts[left] if left >= 0 else -1
        right_count = counts[right] if right < len(counts) else -1
        if right_count > left_count:
            included.add(right)
            covered += float(right_count)
            right += 1
        else:
            included.add(left)
            covered += float(left_count)
            left -= 1

    val = float(edges[min(included)])
    vah = float(edges[max(included) + 1])
    x_end = x_start + width
    ax.hlines(poc, x_start, x_end, color="#ffd166", linewidth=1.25, zorder=3)
    ax.hlines([val, vah], x_start, x_end, color=profile_color, linewidth=0.75,
              linestyle="--", alpha=0.9, zorder=3)
    ax.text(x_start, ax.get_ylim()[1], f"{mode}  POC", color="#ffd166", fontsize=7.5,
            va="top", ha="left", zorder=4)

    return {"Mode": mode, "POC": poc, "VAH": vah, "VAL": val}


# ============================================================================
# Chart
# ============================================================================
def draw_chart(
    symbol: str,
    raw_df: pd.DataFrame,
    snapshot_price: Optional[float],
    output_path: Path,
) -> Dict[str, object]:
    df, osc_name = add_indicators(raw_df)
    if len(df) < 30:
        raise RuntimeError(f"{symbol}: K 线数量太少 ({len(df)})")

    current_price = float(snapshot_price) if snapshot_price and snapshot_price > 0 else float(df["Close"].iloc[-1])
    price_source = "IB snapshot" if snapshot_price and snapshot_price > 0 else "latest H1 close"
    dec = price_decimals(current_price)

    all_highs, all_lows = find_pivots(df, window=5)
    levels = cluster_levels(df, all_highs, all_lows)
    supports, resists = choose_levels(levels, current_price, 3)
    trend = trend_label(df)
    structure = structure_label(df, all_highs, all_lows)
    divergence = detect_divergence(df, all_highs, all_lows)

    df_plot = df.iloc[-SHOW_LAST_N:].copy()
    plot_start = len(df) - len(df_plot)
    highs = [i - plot_start for i in all_highs if i >= plot_start]
    lows = [i - plot_start for i in all_lows if i >= plot_start]

    fig = plt.figure(
        figsize=(OUTPUT_WIDTH_PX / OUTPUT_DPI, OUTPUT_HEIGHT_PX / OUTPUT_DPI),
        facecolor=BG_COLOR,
    )
    grid = fig.add_gridspec(
        2, 2, height_ratios=[3, 1], width_ratios=[4.2, 1],
        hspace=0.055, wspace=0.08,
    )
    ax1 = fig.add_subplot(grid[0, 0])
    ax2 = fig.add_subplot(grid[1, 0])
    ax_info = fig.add_subplot(grid[:, 1])
    ax1.set_facecolor(BG_COLOR)
    ax2.set_facecolor(BG_COLOR)
    ax_info.set_facecolor(BG_COLOR)
    ax_info.axis("off")

    # Candles
    candle_min_body = max(float(df_plot["ATR14"].median(skipna=True)) * 0.015, abs(current_price) * 1e-6)
    for i, (_, row) in enumerate(df_plot.iterrows()):
        o, h, l, c = map(float, (row["Open"], row["High"], row["Low"], row["Close"]))
        color = UP_COLOR if c >= o else DOWN_COLOR
        height = max(abs(c - o), candle_min_body)
        bottom = min(o, c)
        ax1.add_patch(Rectangle((i - 0.38, bottom), 0.76, height,
                                facecolor=color, edgecolor=color, linewidth=0.5))
        ax1.plot([i, i], [l, h], color=color, linewidth=0.55)

    # EMA
    ax1.plot(range(len(df_plot)), df_plot["EMA20"], linewidth=1.0, alpha=0.8, label="EMA20")
    ax1.plot(range(len(df_plot)), df_plot["EMA50"], linewidth=1.0, alpha=0.75, label="EMA50")

    # Auto support / resistance zones
    atr_now = float(df["ATR14"].dropna().iloc[-1]) if df["ATR14"].notna().any() else float((df["High"] - df["Low"]).median())
    zone_half = max(atr_now * 0.12, abs(current_price) * 0.0001)
    for p, touches in supports:
        ax1.axhspan(p - zone_half, p + zone_half, color=ZONE_SUPPORT, zorder=0)
        ax1.axhline(p, color=UP_COLOR, linestyle="--", linewidth=0.8, alpha=0.55)
        ax1.text(len(df_plot) + 2, p, f"S {p:.{dec}f} ({touches})", color=UP_COLOR, fontsize=8, va="center")
    for p, touches in resists:
        ax1.axhspan(p - zone_half, p + zone_half, color=ZONE_RESIST, zorder=0)
        ax1.axhline(p, color=DOWN_COLOR, linestyle="--", linewidth=0.8, alpha=0.55)
        ax1.text(len(df_plot) + 2, p, f"R {p:.{dec}f} ({touches})", color=DOWN_COLOR, fontsize=8, va="center")

    # Current price
    ax1.axhline(current_price, color=LINE_COLOR, linestyle="-.", linewidth=1.3, alpha=0.9)
    ax1.text(len(df_plot) + 2, current_price, f"NOW {current_price:.{dec}f}", color=TEXT_COLOR, fontsize=9, va="center")

    # Latest pivot trend lines
    if len(lows) >= 2:
        pts = lows[-3:]
        ax1.plot(pts, [df_plot["Low"].iloc[i] for i in pts], color=UP_COLOR, linestyle="--", linewidth=1.4, alpha=0.65)
    if len(highs) >= 2:
        pts = highs[-3:]
        ax1.plot(pts, [df_plot["High"].iloc[i] for i in pts], color=DOWN_COLOR, linestyle=":", linewidth=1.3, alpha=0.6)

    # Price panel limits
    y_min = float(df_plot["Low"].min())
    y_max = float(df_plot["High"].max())
    pad = max((y_max - y_min) * 0.06, atr_now)
    ax1.set_ylim(y_min - pad, y_max + pad)
    ax1.set_xlim(-3, len(df_plot) + 34)
    profile = draw_right_profile(ax1, df_plot, x_start=len(df_plot) + 5, width=26)
    ax1.grid(True, color=GRID_COLOR, linewidth=0.5, alpha=0.5)
    ax1.tick_params(colors=TEXT_COLOR)
    ax1.set_ylabel("Price", color=TEXT_COLOR)

    latest_time = df.index[-1]
    ax1.set_title(
        f"{symbol} {TIMEFRAME_LABEL} | {latest_time:%Y-%m-%d %H:%M} UTC+8\n"
        f"Price {current_price:.{dec}f} ({price_source}) | {trend} | {structure}",
        color=TEXT_COLOR, fontsize=10, fontweight="bold", pad=12,
    )

    # Oscillator
    osc = df_plot["OSC"].fillna(0).to_numpy(dtype=float)
    x = np.arange(len(df_plot))
    ax2.fill_between(x, osc, 0, where=osc >= 0, color=UP_COLOR, alpha=0.28, interpolate=True)
    ax2.fill_between(x, osc, 0, where=osc < 0, color=DOWN_COLOR, alpha=0.28, interpolate=True)
    ax2.plot(x, osc, color=LINE_COLOR, linewidth=1.0, alpha=0.9)
    ax2.axhline(0, color=LINE_COLOR, linewidth=0.7, alpha=0.7)

    # Divergence on plotted range
    if divergence:
        div_label, a_global, b_global = divergence
        a, b = a_global - plot_start, b_global - plot_start
        if 0 <= a < len(df_plot) and 0 <= b < len(df_plot):
            c = DOWN_COLOR if div_label.startswith("BEARISH") else UP_COLOR
            ax2.plot([a, b], [osc[a], osc[b]], color=c, linestyle="--", linewidth=1.7)
            ax2.annotate(div_label, xy=(b, osc[b]), xytext=(max(0, b - 55), osc[b]),
                         color=c, fontsize=9, fontweight="bold",
                         arrowprops=dict(arrowstyle="->", color=c, lw=1.0))

    ax2.set_xlim(-3, len(df_plot) + 34)
    ax2.grid(True, color=GRID_COLOR, linewidth=0.5, alpha=0.5)
    ax2.tick_params(colors=TEXT_COLOR)
    ax2.set_ylabel(osc_name, color=TEXT_COLOR)

    xticks = np.unique(np.linspace(0, len(df_plot) - 1, min(9, len(df_plot)), dtype=int))
    ax1.set_xticks(xticks)
    ax1.set_xticklabels([])
    ax2.set_xticks(xticks)
    ax2.set_xticklabels([df_plot.index[i].strftime("%m/%d\n%H:%M") for i in xticks],
                        color=TEXT_COLOR, fontsize=8)

    # Right info panel
    s_lines = "\n".join([f"S{i+1}: {fmt_price(p, dec)}  touches={t}" for i, (p, t) in enumerate(supports)]) or "S: N/A"
    r_lines = "\n".join([f"R{i+1}: {fmt_price(p, dec)}  touches={t}" for i, (p, t) in enumerate(resists)]) or "R: N/A"
    div_text = divergence[0] if divergence else "NONE"
    info_text = (
        "AUTO ANALYSIS\n"
        f"Price : {current_price:.{dec}f}\n"
        f"Trend : {trend}\n"
        f"Struct: {structure}\n"
        f"ATR14 : {fmt_price(atr_now, dec)}\n"
        f"Profile: {profile['Mode']}\n"
        f"POC   : {fmt_price(profile['POC'], dec)}\n"
        f"VA 70%: {fmt_price(profile['VAL'], dec)} - {fmt_price(profile['VAH'], dec)}\n"
        f"Osc   : {osc_name}\n"
        f"Div   : {div_text}\n\n"
        "SUPPORT\n" + s_lines + "\n\n"
        "RESISTANCE\n" + r_lines
    )
    ax_info.text(0.02, 0.98, info_text, transform=ax_info.transAxes, fontsize=7.3,
                 color=TEXT_COLOR, family="monospace", verticalalignment="top",
                 bbox=dict(boxstyle="round,pad=0.55", facecolor="#1e222d",
                           edgecolor=GRID_COLOR, linewidth=1.2, alpha=0.97))

    legend_elements = [
        mpatches.Patch(facecolor=(*ZONE_SUPPORT[:3], 0.30), edgecolor="none", label="Auto Support Zone"),
        mpatches.Patch(facecolor=(*ZONE_RESIST[:3], 0.30), edgecolor="none", label="Auto Resistance Zone"),
    ]
    ax1.legend(handles=legend_elements, loc="upper left",
               facecolor=BG_COLOR, edgecolor=GRID_COLOR, labelcolor=TEXT_COLOR, fontsize=8)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.subplots_adjust(left=0.075, right=0.97, bottom=0.055, top=0.93)
    fig.savefig(output_path, dpi=OUTPUT_DPI, facecolor=BG_COLOR, edgecolor="none")
    plt.close(fig)

    return {
        "Symbol": symbol,
        "Price": current_price,
        "PriceSource": price_source,
        "Time": latest_time.strftime("%Y-%m-%d %H:%M"),
        "Trend": trend,
        "Structure": structure,
        "ATR14": atr_now,
        "ProfileMode": profile["Mode"],
        "POC": profile["POC"],
        "VAH": profile["VAH"],
        "VAL": profile["VAL"],
        "Support1": supports[0][0] if supports else np.nan,
        "Resistance1": resists[0][0] if resists else np.nan,
        "Divergence": divergence[0] if divergence else "",
        "Bars": len(df),
        "Chart": str(output_path),
    }


# ============================================================================
# CLI
# ============================================================================
def print_instrument_catalog() -> None:
    print("可查询品种（代码或中文名均可直接作为参数）：")
    print(f"{'分类':<10} {'中文名称':<20} {'查询代码':<10} {'IB 合约':<20} {'行情'}")
    print("-" * 90)
    for category, name, code in INSTRUMENT_CATALOG:
        spec = make_contract(code)
        c = spec.contract
        contract = f"{c.symbol} {c.secType} {c.exchange} {c.currency}"
        print(f"{category:<10} {name:<20} {code:<10} {contract:<20} {spec.what_to_show}")
    print("\n示例：")
    print("  py xauusd_technical_analysis_chart.py GC SI COPPER ES YM WTI")
    print("  py xauusd_technical_analysis_chart.py 黄金 白银 铜 标普 道琼斯 原油")
    print("  py xauusd_technical_analysis_chart.py --batch")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="IB Gateway multi-symbol H1 technical chart")
    p.add_argument("symbols", nargs="*", help="查询代码或中文品种名；不填时显示品种列表")
    p.add_argument("--host", default=IB_HOST)
    p.add_argument("--port", type=int, default=IB_PORT)
    p.add_argument("--client-id", type=int, default=IB_CLIENT_ID)
    p.add_argument("--output-dir", default=OUTPUT_DIR)
    p.add_argument("--duration", default=DURATION, help='IB duration，例如 "2 M"')
    p.add_argument("--bar-size", default=BAR_SIZE, help='IB barSize，例如 "1 hour"')
    p.add_argument("--no-snapshot", action="store_true", help="不请求行情快照，直接使用最后 H1 Close")
    p.add_argument("--list", action="store_true", help="列出中文品种、查询代码和 IB 合约后退出")
    p.add_argument("--batch", action="store_true", help="批量运行 DEFAULT_SYMBOLS")
    return p


def main() -> int:
    args = build_parser().parse_args()

    if args.list or (not args.symbols and not args.batch):
        print_instrument_catalog()
        return 0

    raw_symbols = args.symbols if args.symbols else DEFAULT_SYMBOLS
    # 保序去重
    symbols = list(dict.fromkeys(normalize_symbol(s) for s in raw_symbols))
    single_mode = bool(args.symbols) and len(symbols) == 1

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[CONNECT] IB Gateway {args.host}:{args.port}, clientId={args.client_id}")
    ib = IBGateway()
    try:
        ib.connect_and_start(args.host, args.port, args.client_id)
    except Exception as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 2

    print(f"[MODE] {'single' if single_mode else 'batch'} | symbols={len(symbols)}")

    summaries: List[Dict[str, object]] = []
    failures: List[Tuple[str, str]] = []

    try:
        for idx, symbol in enumerate(symbols, 1):
            print(f"\n[{idx}/{len(symbols)}] {symbol}")
            try:
                spec = make_contract(symbol)
                print(
                    f"  contract: {spec.contract.symbol} {spec.contract.secType} "
                    f"{spec.contract.exchange} {spec.contract.currency} | {spec.what_to_show}"
                )

                df = ib.get_historical(spec, args.duration, args.bar_size)
                print(f"  bars: {len(df)} | {df.index[0]} -> {df.index[-1]}")

                snap = None
                if not args.no_snapshot:
                    try:
                        snap = ib.get_snapshot_price(spec)
                    except Exception as snap_exc:
                        print(f"  snapshot warning: {snap_exc}")

                file_name = f"{re.sub(r'[^A-Z0-9_-]+', '_', symbol)}_{TIMEFRAME_LABEL}.png"
                output_path = out_dir / file_name
                summary = draw_chart(symbol, df, snap, output_path)
                summaries.append(summary)
                print(
                    f"  DONE price={summary['Price']} trend={summary['Trend']} "
                    f"structure={summary['Structure']} -> {output_path}"
                )

                # 稍微错开历史请求，减少批量 pacing 压力
                time.sleep(0.15)

            except Exception as exc:
                msg = str(exc)
                failures.append((symbol, msg))
                print(f"  FAILED: {msg}", file=sys.stderr)
                if single_mode:
                    break
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    # Summary CSV
    if summaries:
        summary_path = out_dir / "summary.csv"
        pd.DataFrame(summaries).to_csv(summary_path, index=False, encoding="utf-8-sig")
        print(f"\n[SUMMARY] {summary_path}")

    if failures:
        fail_path = out_dir / "failures.csv"
        pd.DataFrame(failures, columns=["Symbol", "Error"]).to_csv(fail_path, index=False, encoding="utf-8-sig")
        print(f"[FAILED] {len(failures)} symbol(s) -> {fail_path}", file=sys.stderr)

    print(f"[DONE] success={len(summaries)}, failed={len(failures)}")
    return 0 if summaries and (not single_mode or not failures) else 1


if __name__ == "__main__":
    raise SystemExit(main())
