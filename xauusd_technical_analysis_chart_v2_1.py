#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IBKR Gateway 多品种 H1 技术位置雷达 v2.1 + 企业微信预警
===================================================

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
    python xauusd_technical_analysis_chart_v2_1.py --batch

    # 不带品种时读取同目录配置文件 xauusd_technical_analysis_chart_v2_1.json
    python xauusd_technical_analysis_chart_v2_1.py

    # 计划任务：只要设置 WEWORK_WEBHOOK_URL，位置状态变化时自动推送
    python xauusd_technical_analysis_chart_v2_1.py GC ES NQ YM

依赖：
    pandas, numpy, matplotlib, ibapi

说明：
- OHLC 直接从 IB Gateway/TWS API 的 reqHistoricalData 拉取；不再使用模拟行情。
- 当前价格优先使用 IB 行情快照；快照不可用时回退为最后一根 H1 K 线 Close。
- 外汇没有有效成交量时，不伪造成交量；副图自动改用 MACD Histogram。
- 支撑/阻力、HH/HL/LH/LL、趋势线、背离全部根据当前数据自动计算。
- 结构计算只使用已完成 H1；实时 snapshot 仅用于“是否进入位置区域”的判断。
- 新增 Price Z-score、EMA/ATR 乖离、RVOL、HVN/LVN、位置评分与 NO_CHASE 过滤。
- 输出 technical_triggers.json，SETUP_READY 及以上可交给现有 Gold × Index × FX + 信息面系统继续评估。
- 企业微信 webhook 从环境变量 WEWORK_WEBHOOK_URL 读取；使用 alert_state.json 做跨计划任务去重。
- 本脚本保持 one-shot 执行，不包含循环监控，适合 cron/GitHub Actions/Windows Task Scheduler。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
SR_WINDOW = 280              # SR / structure use the longer H1 context
VP_WINDOW = 120              # VP/TPO uses an independent recent-cost window
# 固定到脚本所在目录，避免 Windows 计划任务因“起始于”目录不同而把文件写到别处。
OUTPUT_DIR = str(Path(__file__).resolve().parent / "ibkr_technical_output")
REQUEST_TIMEOUT = 20.0
SNAPSHOT_TIMEOUT = 2.0
OUTPUT_WIDTH_PX = 1440
OUTPUT_HEIGHT_PX = 2560
OUTPUT_DPI = 180
WEWORK_WEBHOOK_ENV = "WEWORK_WEBHOOK_URL"
TRIGGER_FILE_NAME = "technical_triggers.json"
ALERT_STATE_FILE_NAME = "alert_state.json"
PROFILE_BINS = 42
DEFAULT_SYMBOL_CONFIG = Path(__file__).with_suffix(".json")

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
    "DXY", "WTI", "BRENT", "NATGAS", "SI", "COPPER", "US10Y", "US02Y",
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
def has_meaningful_volume(df: pd.DataFrame) -> bool:
    volume = pd.to_numeric(df.get("Volume", pd.Series(index=df.index, dtype=float)), errors="coerce")
    valid_volume = volume.replace([np.inf, -np.inf], np.nan).dropna()
    return len(valid_volume) >= max(20, len(df) // 3) and (valid_volume > 0).mean() > 0.70


def filter_completed_h1(df: pd.DataFrame, now_utc: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """只保留已经完整结束的 H1。

    IB intraday bar 时间按脚本既有逻辑转换为北京时间 naive timestamp。
    当前小时正在形成的 bar 不参与结构、Z-score、SR 和触发确认；实时 snapshot
    只用于判断价格是否进入预警区域。
    """
    if df.empty:
        return df.copy()
    now = pd.Timestamp.now(tz="UTC") if now_utc is None else pd.Timestamp(now_utc)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    now_bj = now.tz_convert("Asia/Shanghai").tz_localize(None)
    cutoff = now_bj.floor("h")
    completed = df[df.index < cutoff].copy()
    return completed if not completed.empty else df.iloc[:-1].copy()


def beijing_naive_to_utc_iso(ts: pd.Timestamp) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("Asia/Shanghai")
    else:
        t = t.tz_convert("Asia/Shanghai")
    return t.tz_convert("UTC").isoformat().replace("+00:00", "Z")


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

    # 位置/追价过滤：用波动率标准化，而不是只看绝对均线乖离。
    df["SMA20"] = df["Close"].rolling(20, min_periods=10).mean()
    df["STD20"] = df["Close"].rolling(20, min_periods=10).std(ddof=0)
    safe_std = df["STD20"].replace(0, np.nan)
    safe_atr = df["ATR14"].replace(0, np.nan)
    df["PRICE_Z20"] = (df["Close"] - df["SMA20"]) / safe_std
    df["BB_UPPER"] = df["SMA20"] + 2.0 * df["STD20"]
    df["BB_LOWER"] = df["SMA20"] - 2.0 * df["STD20"]
    df["EMA20_ATR_DIST"] = (df["Close"] - df["EMA20"]) / safe_atr
    df["EMA50_ATR_DIST"] = (df["Close"] - df["EMA50"]) / safe_atr

    volume = pd.to_numeric(df.get("Volume", pd.Series(index=df.index, dtype=float)), errors="coerce")
    real_volume = has_meaningful_volume(df)
    df.attrs["has_real_volume"] = real_volume

    if real_volume:
        volume_clean = volume.fillna(0).clip(lower=0)
        df["VOL_MA20"] = volume_clean.rolling(20, min_periods=5).mean()
        df["RVOL20"] = volume_clean / df["VOL_MA20"].replace(0, np.nan)
        v = volume_clean.to_numpy(dtype=float)
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
        df["VOL_MA20"] = np.nan
        df["RVOL20"] = np.nan
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


def compute_profile_stats(df: pd.DataFrame, bins: int = PROFILE_BINS) -> Dict[str, Any]:
    """计算 H1 Volume Profile；无可靠成交量时退化为 TPO。

    注意：H1 每根 bar 的量仍近似分配到 typical price，因此这是位置筛选用途，
    不是逐笔/分钟级精确 Volume-at-Price。GC/ES/NQ/YM 有真实 TRADES volume 时
    可信度高于 MIDPOINT/无量品种。
    """
    typical_price = ((df["High"] + df["Low"] + df["Close"]) / 3).to_numpy(dtype=float)
    volume = pd.to_numeric(df.get("Volume"), errors="coerce").fillna(0).clip(lower=0).to_numpy(dtype=float)
    real_volume = has_meaningful_volume(df)
    weights = volume if real_volume else np.ones(len(df), dtype=float)
    mode = "VP" if real_volume else "TPO"

    counts, edges = np.histogram(typical_price, bins=bins, weights=weights)
    centers = (edges[:-1] + edges[1:]) / 2
    if not np.any(counts > 0):
        return {
            "Mode": "N/A", "POC": np.nan, "VAH": np.nan, "VAL": np.nan,
            "HVN": [], "LVN": [], "WindowBars": len(df),
            "counts": counts, "edges": edges, "centers": centers,
        }

    poc_idx = int(np.argmax(counts))
    poc = float(centers[poc_idx])

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

    # 3-bin 平滑后找局部峰谷。只保留最有代表性的 3 个 HVN/LVN。
    sm = pd.Series(counts, dtype=float).rolling(3, center=True, min_periods=1).mean().to_numpy()
    positive = sm[sm > 0]
    hvn_levels: List[Tuple[float, float]] = []
    lvn_levels: List[Tuple[float, float]] = []
    if len(positive) >= 5:
        high_thr = float(np.quantile(positive, 0.70))
        low_thr = float(np.quantile(positive, 0.30))
        for i in range(1, len(sm) - 1):
            if sm[i] >= sm[i - 1] and sm[i] >= sm[i + 1] and sm[i] >= high_thr:
                hvn_levels.append((float(centers[i]), float(sm[i])))
            if sm[i] <= sm[i - 1] and sm[i] <= sm[i + 1] and 0 < sm[i] <= low_thr:
                lvn_levels.append((float(centers[i]), float(sm[i])))
    hvn_levels = sorted(hvn_levels, key=lambda x: x[1], reverse=True)[:3]
    lvn_levels = sorted(lvn_levels, key=lambda x: x[1])[:3]

    return {
        "Mode": mode,
        "POC": poc,
        "VAH": vah,
        "VAL": val,
        "HVN": [x[0] for x in hvn_levels],
        "LVN": [x[0] for x in lvn_levels],
        "WindowBars": len(df),
        "counts": counts,
        "edges": edges,
        "centers": centers,
    }


def _atr_distance(a: float, b: float, atr: float) -> float:
    if not all(math.isfinite(x) for x in (a, b, atr)) or atr <= 0:
        return float("inf")
    return abs(a - b) / atr


def _nearest_profile_feature(level: float, profile: Dict[str, Any], atr: float) -> Dict[str, Any]:
    candidates: List[Tuple[str, float]] = []
    for name in ("POC", "VAH", "VAL"):
        v = safe_float(profile.get(name))
        if math.isfinite(v):
            candidates.append((name, v))
    for v in profile.get("HVN", []) or []:
        candidates.append(("HVN", float(v)))
    for v in profile.get("LVN", []) or []:
        candidates.append(("LVN", float(v)))
    if not candidates:
        return {"feature": "NONE", "price": np.nan, "distance_atr": np.inf}
    name, price = min(candidates, key=lambda x: abs(level - x[1]))
    return {"feature": name, "price": price, "distance_atr": _atr_distance(level, price, atr)}


def _rejection_signal(df: pd.DataFrame, level: Optional[float], atr: float, direction: str) -> bool:
    if level is None or len(df) < 1 or not math.isfinite(atr) or atr <= 0:
        return False
    row = df.iloc[-1]
    o, h, l, c = map(float, (row["Open"], row["High"], row["Low"], row["Close"]))
    rng = max(h - l, 1e-12)
    body = max(abs(c - o), rng * 0.05)
    if direction == "LONG":
        lower_wick = min(o, c) - l
        near = abs(l - level) <= atr * 0.30 or l <= level <= h
        return near and lower_wick >= body * 1.2 and c >= l + rng * 0.60
    upper_wick = h - max(o, c)
    near = abs(h - level) <= atr * 0.30 or l <= level <= h
    return near and upper_wick >= body * 1.2 and c <= l + rng * 0.40


def evaluate_location(
    df: pd.DataFrame,
    current_price: float,
    supports: List[Tuple[float, int]],
    resists: List[Tuple[float, int]],
    profile: Dict[str, Any],
    trend: str,
    structure: str,
) -> Dict[str, Any]:
    """位置引擎：只回答“值不值得进一步分析”，不产生交易指令。"""
    row = df.iloc[-1]
    atr = safe_float(row.get("ATR14"))
    ema20 = safe_float(row.get("EMA20"))
    ema50 = safe_float(row.get("EMA50"))
    sma20 = safe_float(row.get("SMA20"))
    std20 = safe_float(row.get("STD20"))
    rvol = safe_float(row.get("RVOL20"))

    z_now = (current_price - sma20) / std20 if math.isfinite(std20) and std20 > 0 else np.nan
    ema20_atr_now = (current_price - ema20) / atr if math.isfinite(atr) and atr > 0 else np.nan
    ema50_atr_now = (current_price - ema50) / atr if math.isfinite(atr) and atr > 0 else np.nan
    no_chase_long = bool((math.isfinite(z_now) and z_now > 2.0) or (math.isfinite(ema20_atr_now) and ema20_atr_now > 1.5))
    no_chase_short = bool((math.isfinite(z_now) and z_now < -2.0) or (math.isfinite(ema20_atr_now) and ema20_atr_now < -1.5))

    def side_score(direction: str, levels: List[Tuple[float, int]]) -> Dict[str, Any]:
        level = levels[0][0] if levels else None
        touches = levels[0][1] if levels else 0
        if level is None or not math.isfinite(atr) or atr <= 0:
            return {
                "direction": direction, "score": 0, "level": None, "touches": 0,
                "distance_atr": np.inf, "profile_feature": "NONE", "profile_distance_atr": np.inf,
                "ema_confluence": [], "rejection": False,
            }

        dist = _atr_distance(current_price, level, atr)
        score = 0.0
        if dist <= 0.15:
            score += 25
        elif dist <= 0.30:
            score += 20
        elif dist <= 0.50:
            score += 12
        elif dist <= 0.75:
            score += 5

        score += min(float(touches) * 5.0, 15.0)

        pf = _nearest_profile_feature(level, profile, atr)
        pf_dist = float(pf["distance_atr"])
        pf_name = str(pf["feature"])
        if pf_name in {"POC", "HVN"}:
            if pf_dist <= 0.15:
                score += 20
            elif pf_dist <= 0.30:
                score += 12
        elif pf_name in {"VAH", "VAL"}:
            if pf_dist <= 0.20:
                score += 8
        elif pf_name == "LVN" and pf_dist <= 0.20:
            # LVN 更偏突破/加速提示，不把它当成强承接。
            score += 3

        ema_hits: List[str] = []
        if math.isfinite(ema20) and _atr_distance(level, ema20, atr) <= 0.20:
            score += 10
            ema_hits.append("EMA20")
        if math.isfinite(ema50) and _atr_distance(level, ema50, atr) <= 0.20:
            score += 6
            ema_hits.append("EMA50")

        if direction == "LONG":
            if trend == "BULLISH": score += 10
            elif trend == "BEARISH": score -= 10
            if "HL" in structure: score += 10
            if no_chase_long: score -= 20
        else:
            if trend == "BEARISH": score += 10
            elif trend == "BULLISH": score -= 10
            if "LH" in structure: score += 10
            if no_chase_short: score -= 20

        rejection = _rejection_signal(df, level, atr, direction)
        if rejection:
            score += 8
        if math.isfinite(rvol) and rvol >= 1.20:
            score += 5

        return {
            "direction": direction,
            "score": int(round(max(0.0, min(100.0, score)))),
            "level": float(level),
            "touches": int(touches),
            "distance_atr": float(dist),
            "profile_feature": pf_name,
            "profile_price": safe_float(pf.get("price")),
            "profile_distance_atr": pf_dist,
            "ema_confluence": ema_hits,
            "rejection": rejection,
        }

    long_side = side_score("LONG", supports)
    short_side = side_score("SHORT", resists)

    # 同时靠近两边时，只有明显分数差才给方向；否则保持中性，避免窄区间误报。
    best = long_side if long_side["score"] >= short_side["score"] else short_side
    other = short_side if best is long_side else long_side
    direction = best["direction"]
    dist = best["distance_atr"]
    score = best["score"]
    no_chase = no_chase_long if direction == "LONG" else no_chase_short

    status = "NO_SETUP"
    level_no = 0
    if score >= 45 and dist <= 0.50 and not no_chase:
        status = f"WATCH_{direction}"
        level_no = 1
    if score >= 65 and dist <= 0.30 and not no_chase:
        status = f"SETUP_READY_{direction}"
        level_no = 2
    if score >= 75 and dist <= 0.30 and best["rejection"] and not no_chase:
        status = f"CONFIRMED_{direction}"
        level_no = 3

    if other["score"] >= 55 and abs(best["score"] - other["score"]) < 10:
        status = "CONFLICT_WATCH"
        level_no = 1
        direction = "NEUTRAL"

    return {
        "setup_status": status,
        "alert_level": level_no,
        "candidate_direction": direction,
        "location_score": int(score),
        "analysis_required": bool(level_no >= 2),
        "price_z20_now": safe_float(z_now),
        "ema20_atr_distance_now": safe_float(ema20_atr_now),
        "ema50_atr_distance_now": safe_float(ema50_atr_now),
        "no_chase_long": no_chase_long,
        "no_chase_short": no_chase_short,
        "rvol20": safe_float(rvol),
        "long": long_side,
        "short": short_side,
    }


def draw_right_profile(
    ax: plt.Axes,
    df: pd.DataFrame,
    x_start: float,
    width: float,
    bins: int = PROFILE_BINS,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, object]:
    """绘制右侧 VP/TPO，并标示 POC、VAH/VAL、HVN/LVN。"""
    profile = profile or compute_profile_stats(df, bins=bins)
    counts = np.asarray(profile.get("counts", []), dtype=float)
    edges = np.asarray(profile.get("edges", []), dtype=float)
    centers = np.asarray(profile.get("centers", []), dtype=float)
    if len(counts) == 0 or not np.any(counts > 0):
        return profile

    heights = np.diff(edges) * 0.84
    scale = width / float(counts.max())
    bar_widths = counts * scale
    real_volume = profile.get("Mode") == "VP"
    profile_color = "#4ea1ff" if real_volume else "#9d7cff"
    ax.barh(
        centers, bar_widths, height=heights, left=x_start,
        color=profile_color, alpha=0.48, edgecolor="none", zorder=1,
    )

    poc = float(profile["POC"])
    val = float(profile["VAL"])
    vah = float(profile["VAH"])
    x_end = x_start + width
    ax.hlines(poc, x_start, x_end, color="#ffd166", linewidth=2.8, zorder=3)
    ax.hlines([val, vah], x_start, x_end, color=profile_color, linewidth=1.15,
              linestyle="--", alpha=0.9, zorder=3)

    for y in profile.get("HVN", []) or []:
        half_bin = float(np.nanmedian(np.diff(edges))) * 0.42
        ax.axhspan(float(y) - half_bin, float(y) + half_bin,
                   xmin=x_start / max(ax.get_xlim()[1], 1.0), xmax=x_end / max(ax.get_xlim()[1], 1.0),
                   color="#4ea1ff", alpha=0.24, zorder=2)
        ax.hlines(float(y), x_start, x_end, color="#8bd3ff", linewidth=1.15, alpha=0.95, zorder=3)
    for y in profile.get("LVN", []) or []:
        ax.hlines(float(y), x_start, x_end, color="#c9a7ff", linewidth=0.8,
                  linestyle=":", alpha=0.9, zorder=3)

    ax.text(x_start, ax.get_ylim()[1], f"{profile['Mode']} {profile.get('WindowBars', 0)}H | POC", color="#ffd166", fontsize=7.5,
            va="top", ha="left", zorder=4)
    return profile


# ============================================================================
# Chart
# ============================================================================
def draw_chart(
    symbol: str,
    raw_df: pd.DataFrame,
    snapshot_price: Optional[float],
    output_path: Path,
) -> Dict[str, object]:
    completed_raw = filter_completed_h1(raw_df)
    if len(completed_raw) < 30:
        raise RuntimeError(f"{symbol}: 完整 H1 K 线数量太少 ({len(completed_raw)})")

    df, osc_name = add_indicators(completed_raw)
    # Keep SR/structure context stable and independent from the shorter profile window.
    df = df.iloc[-SR_WINDOW:].copy()
    current_price = float(snapshot_price) if snapshot_price and snapshot_price > 0 else float(df["Close"].iloc[-1])
    price_source = "IB snapshot" if snapshot_price and snapshot_price > 0 else "completed H1 close"
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

    atr_now = float(df["ATR14"].dropna().iloc[-1]) if df["ATR14"].notna().any() else float((df["High"] - df["Low"]).median())
    profile_df = df.iloc[-VP_WINDOW:].copy()
    profile = compute_profile_stats(profile_df)
    location = evaluate_location(df, current_price, supports, resists, profile, trend, structure)

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

    # Candles (只画已完成 H1)
    candle_min_body = max(float(df_plot["ATR14"].median(skipna=True)) * 0.015, abs(current_price) * 1e-6)
    for i, (_, row) in enumerate(df_plot.iterrows()):
        o, h, l, c = map(float, (row["Open"], row["High"], row["Low"], row["Close"]))
        color = UP_COLOR if c >= o else DOWN_COLOR
        height = max(abs(c - o), candle_min_body)
        bottom = min(o, c)
        ax1.add_patch(Rectangle((i - 0.38, bottom), 0.76, height,
                                facecolor=color, edgecolor=color, linewidth=0.5))
        ax1.plot([i, i], [l, h], color=color, linewidth=0.55)

    # EMA + Bollinger（淡化，主要用于位置上下文）
    ax1.plot(range(len(df_plot)), df_plot["EMA20"], linewidth=1.0, alpha=0.85, label="EMA20")
    ax1.plot(range(len(df_plot)), df_plot["EMA50"], linewidth=1.0, alpha=0.75, label="EMA50")
    ax1.plot(range(len(df_plot)), df_plot["BB_UPPER"], linewidth=0.65, alpha=0.30, linestyle=":")
    ax1.plot(range(len(df_plot)), df_plot["BB_LOWER"], linewidth=0.65, alpha=0.30, linestyle=":")

    # Auto support / resistance zones
    zone_half = max(atr_now * 0.12, abs(current_price) * 0.0001)
    for p, touches in supports:
        ax1.axhspan(p - zone_half, p + zone_half, color=ZONE_SUPPORT, zorder=0)
        ax1.axhline(p, color=UP_COLOR, linestyle="--", linewidth=0.8, alpha=0.55)
        ax1.text(len(df_plot) + 2, p, f"S {p:.{dec}f} ({touches})", color=UP_COLOR, fontsize=8, va="center")
    for p, touches in resists:
        ax1.axhspan(p - zone_half, p + zone_half, color=ZONE_RESIST, zorder=0)
        ax1.axhline(p, color=DOWN_COLOR, linestyle="--", linewidth=0.8, alpha=0.55)
        ax1.text(len(df_plot) + 2, p, f"R {p:.{dec}f} ({touches})", color=DOWN_COLOR, fontsize=8, va="center")

    # Profile features on the price chart itself: recent acceptance/cost structure.
    profile_centers = np.asarray(profile.get("centers", []), dtype=float)
    profile_half_bin = float(np.nanmedian(np.diff(profile_centers))) * 0.46 if len(profile_centers) > 1 else atr_now * 0.04
    for y in profile.get("HVN", []) or []:
        ax1.axhspan(float(y) - profile_half_bin, float(y) + profile_half_bin,
                    color="#4ea1ff", alpha=0.10, zorder=0)
    ax1.axhline(float(profile["POC"]), color="#ffd166", linewidth=2.2, alpha=0.90, zorder=2)
    ax1.axhline(float(profile["VAH"]), color="#8bd3ff", linewidth=0.9, linestyle="--", alpha=0.75)
    ax1.axhline(float(profile["VAL"]), color="#8bd3ff", linewidth=0.9, linestyle="--", alpha=0.75)
    for y in profile.get("LVN", []) or []:
        ax1.axhline(float(y), color="#c9a7ff", linewidth=0.8, linestyle=":", alpha=0.75)

    # Emphasize the scored Long/Short decision zones instead of leaving them as generic SR bands.
    decision_x = max(0, len(df_plot) - 72)
    decision_width = max(20, len(df_plot) - decision_x)
    for side_name, side_data, side_color in (
        ("LONG", location["long"], UP_COLOR),
        ("SHORT", location["short"], DOWN_COLOR),
    ):
        level = side_data.get("level")
        if level is None or not math.isfinite(float(level)):
            continue
        rect = Rectangle((decision_x, float(level) - zone_half), decision_width, zone_half * 2,
                         fill=False, edgecolor=side_color, linewidth=1.5, linestyle="-", alpha=0.90, zorder=4)
        ax1.add_patch(rect)
        ax1.text(decision_x + 1, float(level) + zone_half,
                 f"{side_name} ZONE | {side_data['score']}/100 | {side_data.get('profile_feature', 'NONE')}",
                 color=side_color, fontsize=7.2, fontweight="bold", va="bottom", zorder=5)

    # Snapshot/当前价格只作为实时位置线，不参与 H1 结构确认。
    ax1.axhline(current_price, color=LINE_COLOR, linestyle="-.", linewidth=1.3, alpha=0.9)
    ax1.text(len(df_plot) + 2, current_price, f"NOW {current_price:.{dec}f}", color=TEXT_COLOR, fontsize=9, va="center")

    if len(lows) >= 2:
        pts = lows[-3:]
        ax1.plot(pts, [df_plot["Low"].iloc[i] for i in pts], color=UP_COLOR, linestyle="--", linewidth=1.4, alpha=0.65)
    if len(highs) >= 2:
        pts = highs[-3:]
        ax1.plot(pts, [df_plot["High"].iloc[i] for i in pts], color=DOWN_COLOR, linestyle=":", linewidth=1.3, alpha=0.6)

    y_min = float(df_plot["Low"].min())
    y_max = float(df_plot["High"].max())
    pad = max((y_max - y_min) * 0.06, atr_now)
    ax1.set_ylim(y_min - pad, y_max + pad)
    ax1.set_xlim(-3, len(df_plot) + 34)
    draw_right_profile(ax1, df_plot, x_start=len(df_plot) + 5, width=26, profile=profile)
    ax1.grid(True, color=GRID_COLOR, linewidth=0.5, alpha=0.5)
    ax1.tick_params(colors=TEXT_COLOR)
    ax1.set_ylabel("Price", color=TEXT_COLOR)
    status_color = UP_COLOR if location["candidate_direction"] == "LONG" else DOWN_COLOR if location["candidate_direction"] == "SHORT" else "#ffd166"
    ax1.text(0.985, 0.985,
             f"{location['setup_status']}\nLOCATION {location['location_score']}/100\n"
             f"AI TRIGGER: {'YES' if location['analysis_required'] else 'NO'}",
             transform=ax1.transAxes, ha="right", va="top", color=status_color,
             fontsize=8.5, fontweight="bold",
             bbox=dict(boxstyle="round,pad=0.45", facecolor="#171b24", edgecolor=status_color, alpha=0.94))

    latest_time = df.index[-1]
    ax1.set_title(
        f"{symbol} {TIMEFRAME_LABEL} | completed {latest_time:%Y-%m-%d %H:%M} UTC+8\n"
        f"Price {current_price:.{dec}f} ({price_source}) | {trend} | {structure} | {location['setup_status']}",
        color=TEXT_COLOR, fontsize=10, fontweight="bold", pad=12,
    )

    # Price extension panel: Z-score and EMA20/ATR distance are the no-chase contract.
    x = np.arange(len(df_plot))
    z_series = df_plot["PRICE_Z20"].to_numpy(dtype=float)
    atr_extension = df_plot["EMA20_ATR_DIST"].to_numpy(dtype=float)
    ax2.plot(x, z_series, color="#4ea1ff", linewidth=1.25, label="Price Z20")
    ax2.plot(x, atr_extension, color="#ffd166", linewidth=0.95, alpha=0.90, label="EMA20 / ATR")
    ax2.axhline(0, color=LINE_COLOR, linewidth=0.7, alpha=0.7)
    ax2.axhline(2.0, color=DOWN_COLOR, linewidth=1.0, linestyle="--", alpha=0.90)
    ax2.axhline(-2.0, color=UP_COLOR, linewidth=1.0, linestyle="--", alpha=0.90)
    ax2.axhline(1.5, color="#ff9f43", linewidth=0.75, linestyle=":", alpha=0.80)
    ax2.axhline(-1.5, color="#ff9f43", linewidth=0.75, linestyle=":", alpha=0.80)
    finite_extension = np.concatenate([z_series[np.isfinite(z_series)], atr_extension[np.isfinite(atr_extension)]])
    extension_limit = max(3.0, float(np.max(np.abs(finite_extension))) + 0.35) if len(finite_extension) else 3.0
    ax2.set_ylim(-extension_limit, extension_limit)
    ax2.axhspan(2.0, extension_limit, color=DOWN_COLOR, alpha=0.08)
    ax2.axhspan(-extension_limit, -2.0, color=UP_COLOR, alpha=0.08)
    ax2.text(1, 2.03, "NO CHASE LONG (Z > +2)", color=DOWN_COLOR, fontsize=7, va="bottom")
    ax2.text(1, -2.03, "NO CHASE SHORT (Z < -2)", color=UP_COLOR, fontsize=7, va="top")

    ax2.set_xlim(-3, len(df_plot) + 34)
    ax2.grid(True, color=GRID_COLOR, linewidth=0.5, alpha=0.5)
    ax2.tick_params(colors=TEXT_COLOR)
    ax2.set_ylabel("PRICE EXTENSION", color=TEXT_COLOR)
    ax2.legend(loc="upper right", facecolor=BG_COLOR, edgecolor=GRID_COLOR,
               labelcolor=TEXT_COLOR, fontsize=7)

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
    hvn_text = "\n        ".join(fmt_price(float(v), dec) for v in profile.get("HVN", []) or []) or "N/A"
    lvn_text = "\n        ".join(fmt_price(float(v), dec) for v in profile.get("LVN", []) or []) or "N/A"
    z_text = "N/A" if not math.isfinite(location["price_z20_now"]) else f"{location['price_z20_now']:+.2f}"
    e20_text = "N/A" if not math.isfinite(location["ema20_atr_distance_now"]) else f"{location['ema20_atr_distance_now']:+.2f} ATR"
    rvol_text = "N/A" if not math.isfinite(location["rvol20"]) else f"{location['rvol20']:.2f}x"
    decision_side = location["long"] if location["candidate_direction"] == "LONG" else location["short"]
    if location["candidate_direction"] == "NEUTRAL":
        decision_side = location["long"] if location["long"]["score"] >= location["short"]["score"] else location["short"]
    why_lines = [
        f"SR distance {decision_side.get('distance_atr', np.inf):.2f} ATR",
        f"Touches {decision_side.get('touches', 0)}",
        f"{decision_side.get('profile_feature', 'NONE')} overlap {decision_side.get('profile_distance_atr', np.inf):.2f} ATR",
    ]
    if decision_side.get("ema_confluence"):
        why_lines.append("EMA " + ", ".join(decision_side["ema_confluence"]))
    if decision_side.get("rejection"):
        why_lines.append("Completed-H1 rejection")
    why_lines.append(f"{trend} / {structure}")
    risk_lines = []
    if location["no_chase_long"]: risk_lines.append("NO_CHASE_LONG")
    if location["no_chase_short"]: risk_lines.append("NO_CHASE_SHORT")
    if trend == "MIXED": risk_lines.append("Trend mixed")
    if location["setup_status"] == "CONFLICT_WATCH": risk_lines.append("Long/Short conflict")
    if not risk_lines: risk_lines.append("Await H1 confirmation")
    if location["analysis_required"]:
        next_text = "RUN FULL ANALYSIS\nPRICE>RATIO>MACRO\n>FX>EVENT>FIT"
    elif location["alert_level"] >= 1:
        next_text = "MONITOR ZONE\nWait completed H1"
    else:
        next_text = "WAIT FOR LOCATION"
    info_text = (
        "STATUS\n"
        f"{location['setup_status']}\n"
        f"Direction: {location['candidate_direction']}\n\n"
        "QUALITY\n"
        f"{location['location_score']}/100 | Level {location['alert_level']}\n"
        f"AI Trigger: {'YES' if location['analysis_required'] else 'NO'}\n\n"
        "WHY\n- " + "\n- ".join(why_lines) + "\n\n"
        "RISK\n- " + "\n- ".join(risk_lines) + "\n\n"
        "NEXT\n" + next_text + "\n\n"
        "MARKET\n"
        f"Price : {current_price:.{dec}f}\n"
        f"ATR14 : {fmt_price(atr_now, dec)}\n"
        f"Z20   : {z_text}\n"
        f"E20Dst: {e20_text}\n"
        f"RVOL20: {rvol_text}\n\n"
        f"PROFILE {profile['Mode']} {profile.get('WindowBars', 0)}H\n"
        f"POC   : {fmt_price(profile['POC'], dec)}\n"
        f"VA 70%: {fmt_price(profile['VAL'], dec)} - {fmt_price(profile['VAH'], dec)}\n"
        f"HVN   : {hvn_text}\n"
        f"LVN   : {lvn_text}\n"
        f"Div   : {div_text}\n\n"
        "SUPPORT\n" + s_lines + "\n\n"
        "RESISTANCE\n" + r_lines
    )
    ax_info.text(0.02, 0.98, info_text, transform=ax_info.transAxes, fontsize=7.0,
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

    generated_utc = pd.Timestamp.now(tz="UTC").isoformat().replace("+00:00", "Z")
    completed_h1_utc = beijing_naive_to_utc_iso(latest_time)
    zone_side = location["long"] if location["candidate_direction"] == "LONG" else location["short"]
    if location["candidate_direction"] == "NEUTRAL":
        zone_side = location["long"] if location["long"]["score"] >= location["short"]["score"] else location["short"]

    trigger = {
        "symbol": symbol,
        "timeframe": TIMEFRAME_LABEL,
        "timestamp_utc": generated_utc,
        "completed_h1_utc": completed_h1_utc,
        "beijing_time": latest_time.strftime("%Y-%m-%d %H:%M"),
        "price": current_price,
        "price_source": price_source,
        "trend": trend,
        "structure": structure,
        "atr14": atr_now,
        "profile": {
            "mode": profile["Mode"],
            "window_h1": int(profile.get("WindowBars", 0)),
            "poc": safe_float(profile["POC"]),
            "vah": safe_float(profile["VAH"]),
            "val": safe_float(profile["VAL"]),
            "hvn": [float(v) for v in profile.get("HVN", []) or []],
            "lvn": [float(v) for v in profile.get("LVN", []) or []],
        },
        "extension": {
            "price_z20": location["price_z20_now"],
            "ema20_atr_distance": location["ema20_atr_distance_now"],
            "ema50_atr_distance": location["ema50_atr_distance_now"],
            "no_chase_long": location["no_chase_long"],
            "no_chase_short": location["no_chase_short"],
            "rvol20": location["rvol20"],
        },
        "location": {
            "setup_status": location["setup_status"],
            "alert_level": location["alert_level"],
            "candidate_direction": location["candidate_direction"],
            "score": location["location_score"],
            "analysis_required": location["analysis_required"],
            "nearest_level": zone_side.get("level"),
            "distance_atr": zone_side.get("distance_atr"),
            "touches": zone_side.get("touches"),
            "profile_feature": zone_side.get("profile_feature"),
            "profile_distance_atr": zone_side.get("profile_distance_atr"),
            "ema_confluence": zone_side.get("ema_confluence", []),
            "rejection": zone_side.get("rejection", False),
            "long": location["long"],
            "short": location["short"],
        },
        "divergence": divergence[0] if divergence else None,
        "chart": str(output_path),
    }

    return {
        "Symbol": symbol,
        "Price": current_price,
        "PriceSource": price_source,
        "Time": latest_time.strftime("%Y-%m-%d %H:%M"),
        "CompletedH1UTC": completed_h1_utc,
        "Trend": trend,
        "Structure": structure,
        "ATR14": atr_now,
        "Z20": location["price_z20_now"],
        "EMA20ATRDist": location["ema20_atr_distance_now"],
        "RVOL20": location["rvol20"],
        "ProfileMode": profile["Mode"],
        "POC": profile["POC"],
        "VAH": profile["VAH"],
        "VAL": profile["VAL"],
        "HVN": ";".join(str(round(float(v), dec)) for v in profile.get("HVN", []) or []),
        "LVN": ";".join(str(round(float(v), dec)) for v in profile.get("LVN", []) or []),
        "Support1": supports[0][0] if supports else np.nan,
        "Resistance1": resists[0][0] if resists else np.nan,
        "SetupStatus": location["setup_status"],
        "AlertLevel": location["alert_level"],
        "CandidateDirection": location["candidate_direction"],
        "LocationScore": location["location_score"],
        "AnalysisRequired": location["analysis_required"],
        "Divergence": divergence[0] if divergence else "",
        "Bars": len(df),
        "Chart": str(output_path),
        "Trigger": trigger,
    }


# ============================================================================
# Trigger / WeCom alert
# ============================================================================
def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        x = float(value)
        return x if math.isfinite(x) else None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def load_json_file(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] 无法读取 {path}: {exc}", file=sys.stderr)
    return default


def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _alert_zone_key(trigger: Dict[str, Any]) -> str:
    loc = trigger.get("location", {})
    direction = str(loc.get("candidate_direction", "NEUTRAL"))
    level = safe_float(loc.get("nearest_level"))
    atr = safe_float(trigger.get("atr14"))
    if math.isfinite(level) and math.isfinite(atr) and atr > 0:
        # 用 0.10 ATR 量化，避免 SR 聚类每次轻微漂移导致重复通知。
        bucket = round(level / (atr * 0.10))
        return f"{direction}:{bucket}"
    return direction


def should_notify_trigger(
    trigger: Dict[str, Any],
    previous: Optional[Dict[str, Any]],
    min_level: int,
    force: bool = False,
    notify_clear: bool = False,
) -> bool:
    loc = trigger.get("location", {})
    level = int(loc.get("alert_level") or 0)
    status = str(loc.get("setup_status", "NO_SETUP"))
    if force and level >= min_level:
        return True

    prev = previous or {}
    prev_level = int(prev.get("alert_level") or 0)
    prev_status = str(prev.get("setup_status", "NO_SETUP"))

    if level == 0:
        return bool(notify_clear and prev_level >= min_level)
    if level < min_level:
        return False
    if not previous:
        return True
    if not prev.get("last_sent_utc"):
        return True
    if level > prev_level:
        return True
    if status != prev_status:
        return True
    if _alert_zone_key(trigger) != str(prev.get("zone_key", "")):
        return True
    return False


def build_alert_state(trigger: Dict[str, Any], sent: bool) -> Dict[str, Any]:
    loc = trigger.get("location", {})
    state = {
        "setup_status": loc.get("setup_status", "NO_SETUP"),
        "alert_level": int(loc.get("alert_level") or 0),
        "candidate_direction": loc.get("candidate_direction", "NEUTRAL"),
        "nearest_level": loc.get("nearest_level"),
        "zone_key": _alert_zone_key(trigger),
        "completed_h1_utc": trigger.get("completed_h1_utc"),
        "updated_at_utc": pd.Timestamp.now(tz="UTC").isoformat().replace("+00:00", "Z"),
    }
    if sent:
        state["last_sent_utc"] = state["updated_at_utc"]
    return state


def _fmt_num(v: Any, digits: int = 2, suffix: str = "") -> str:
    x = safe_float(v)
    return f"{x:.{digits}f}{suffix}" if math.isfinite(x) else "N/A"


def format_wework_markdown(triggers: List[Dict[str, Any]]) -> str:
    now_bj = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
    lines = [f"### Gold × Index 位置预警  {now_bj}"]
    for t in triggers:
        loc = t.get("location", {})
        ext = t.get("extension", {})
        profile = t.get("profile", {})
        status = str(loc.get("setup_status", "NO_SETUP"))
        direction = str(loc.get("candidate_direction", "NEUTRAL"))
        score = int(loc.get("score") or 0)
        level = int(loc.get("alert_level") or 0)
        level_name = {0: "CLEAR", 1: "WATCH", 2: "READY", 3: "CONFIRMED"}.get(level, str(level))
        direction_cn = {"LONG": "偏多候选", "SHORT": "偏空候选", "NEUTRAL": "冲突/中性"}.get(direction, direction)
        lines.extend([
            "",
            f"> **{t.get('symbol')} | L{level} {level_name} | {direction_cn}**",
            f"> Price `{_fmt_num(t.get('price'), 2)}` | Score `{score}/100` | `{status}`",
            f"> H1 `{t.get('trend')}` / `{t.get('structure')}` | ATR `{_fmt_num(t.get('atr14'), 2)}`",
            f"> Key `{_fmt_num(loc.get('nearest_level'), 2)}` | distance `{_fmt_num(loc.get('distance_atr'), 2, ' ATR')}` | touches `{loc.get('touches', 0)}`",
            f"> Profile `{profile.get('mode')}` / `{loc.get('profile_feature')}` | overlap `{_fmt_num(loc.get('profile_distance_atr'), 2, ' ATR')}`",
            f"> Z20 `{_fmt_num(ext.get('price_z20'), 2)}` | EMA20 `{_fmt_num(ext.get('ema20_atr_distance'), 2, ' ATR')}` | RVOL `{_fmt_num(ext.get('rvol20'), 2, 'x')}`",
        ])
        if loc.get("analysis_required"):
            lines.append("> **状态：位置已达到完整数据/信息面分析触发条件。**")
        else:
            lines.append("> 状态：仅位置预警，暂不需要调用完整模型分析。")
    lines.append("\n> 注：候选方向是技术位置筛选，不是交易指令；完整方向仍交给 Gold × Index × FX + 信息面系统复核。")
    return "\n".join(lines)


def send_wework_markdown(webhook_url: str, content: str, timeout: float = 8.0) -> Tuple[bool, str]:
    payload = json.dumps({"msgtype": "markdown", "markdown": {"content": content}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
            if int(data.get("errcode", 0)) != 0:
                return False, f"errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
        except Exception:
            pass
        return True, body[:300]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        return False, f"HTTP {exc.code}: {body[:300]}"
    except Exception as exc:
        return False, str(exc)


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
    print("  py xauusd_technical_analysis_chart_v2.py GC ES NQ YM")
    print("  py xauusd_technical_analysis_chart_v2.py 黄金 标普 纳斯达克 道琼斯")
    print("  py xauusd_technical_analysis_chart_v2.py --batch")
    print("\n企业微信：设置环境变量 WEWORK_WEBHOOK_URL 后，满足预警状态变化时自动推送。")


def load_symbol_config(path: Path) -> List[str]:
    """Load enabled instrument codes from a small, user-editable JSON file."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"品种配置文件不存在: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取品种配置文件 {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise ValueError(f"品种配置文件必须包含 symbols 数组: {path}")

    symbols: List[str] = []
    for item in payload["symbols"]:
        if isinstance(item, str):
            code = item
            enabled = True
        elif isinstance(item, dict):
            code = str(item.get("code") or "")
            enabled = bool(item.get("enabled", True))
        else:
            continue
        if enabled and code.strip():
            symbols.append(normalize_symbol(code))
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        raise ValueError(f"品种配置文件没有启用品种: {path}")
    return symbols


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="IB Gateway H1 technical location radar + WeCom alert")
    p.add_argument("symbols", nargs="*", help="查询代码或中文品种名；不填时显示品种列表")
    p.add_argument("--host", default=IB_HOST)
    p.add_argument("--port", type=int, default=IB_PORT)
    p.add_argument("--client-id", type=int, default=IB_CLIENT_ID)
    p.add_argument("--output-dir", default=OUTPUT_DIR)
    p.add_argument("--duration", default=DURATION, help='IB duration，例如 "2 M"')
    p.add_argument("--bar-size", default=BAR_SIZE, help='IB barSize，例如 "1 hour"')
    p.add_argument("--no-snapshot", action="store_true", help="不请求实时/延迟快照，使用最后完整 H1 Close")
    p.add_argument("--list", action="store_true", help="列出中文品种、查询代码和 IB 合约后退出")
    p.add_argument("--batch", action="store_true", help="批量运行 DEFAULT_SYMBOLS")
    p.add_argument(
        "--config",
        default=os.environ.get("TECHNICAL_SYMBOL_CONFIG", str(DEFAULT_SYMBOL_CONFIG)),
        help="品种配置 JSON；不指定命令行品种且未使用 --batch 时读取",
    )
    p.add_argument("--no-wework", action="store_true", help=f"即使存在 {WEWORK_WEBHOOK_ENV} 也不推送企业微信")
    p.add_argument("--wework-min-level", type=int, choices=[1, 2, 3], default=1,
                   help="企业微信最低预警级别：1=WATCH, 2=READY, 3=CONFIRMED；默认1")
    p.add_argument("--force-notify", action="store_true", help="本次对所有达到最低级别的状态强制推送，忽略去重")
    p.add_argument("--notify-clear", action="store_true", help="位置预警解除时也发送一条 CLEAR 通知")
    p.add_argument("--state-file", default="", help="预警去重状态文件；默认 <output-dir>/alert_state.json")
    p.add_argument("--trigger-file", default="", help="技术触发 JSON；默认 <output-dir>/technical_triggers.json")
    return p


def main() -> int:
    args = build_parser().parse_args()

    if args.list:
        print_instrument_catalog()
        return 0

    if args.symbols:
        raw_symbols = args.symbols
        symbol_source = "command_line"
    elif args.batch:
        raw_symbols = DEFAULT_SYMBOLS
        symbol_source = "built_in_batch"
    else:
        try:
            raw_symbols = load_symbol_config(Path(args.config))
        except ValueError as exc:
            print(f"[FATAL] {exc}", file=sys.stderr)
            return 2
        symbol_source = f"config:{Path(args.config).resolve()}"
    symbols = list(dict.fromkeys(normalize_symbol(s) for s in raw_symbols))
    single_mode = bool(args.symbols) and len(symbols) == 1

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = Path(args.state_file) if args.state_file else out_dir / ALERT_STATE_FILE_NAME
    trigger_path = Path(args.trigger_file) if args.trigger_file else out_dir / TRIGGER_FILE_NAME
    alert_state = load_json_file(state_path, {})
    if not isinstance(alert_state, dict):
        alert_state = {}

    print(f"[CONNECT] IB Gateway {args.host}:{args.port}, clientId={args.client_id}")
    ib = IBGateway()
    try:
        ib.connect_and_start(args.host, args.port, args.client_id)
    except Exception as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 2

    print(
        f"[MODE] {'single' if single_mode else 'batch'} | symbols={len(symbols)} "
        f"| source={symbol_source} | one-shot scheduled mode"
    )

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
                completed_count = len(filter_completed_h1(df))
                print(f"  bars: raw={len(df)}, completed_h1={completed_count} | {df.index[0]} -> {df.index[-1]}")

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
                    f"  DONE price={summary['Price']} trend={summary['Trend']} structure={summary['Structure']} "
                    f"setup={summary['SetupStatus']} score={summary['LocationScore']} "
                    f"analysis_required={summary['AnalysisRequired']} -> {output_path}"
                )
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

    generated_utc = pd.Timestamp.now(tz="UTC").isoformat().replace("+00:00", "Z")
    triggers = [s["Trigger"] for s in summaries]
    trigger_payload = {
        "schema_version": "2.0",
        "generated_at_utc": generated_utc,
        "source": "IBKR",
        "timeframe": TIMEFRAME_LABEL,
        "policy": {
            "completed_h1_only_for_structure": True,
            "snapshot_for_proximity_only": True,
            "watch_level": 1,
            "analysis_trigger_level": 2,
            "confirmed_level": 3,
            "wework_env": WEWORK_WEBHOOK_ENV,
        },
        "symbols": triggers,
    }
    write_json_file(trigger_path, trigger_payload)
    print(f"\n[TRIGGER] {trigger_path}")

    if summaries:
        summary_path = out_dir / "summary.csv"
        csv_rows = [{k: v for k, v in row.items() if k != "Trigger"} for row in summaries]
        pd.DataFrame(csv_rows).to_csv(summary_path, index=False, encoding="utf-8-sig")
        print(f"[SUMMARY] {summary_path}")

    if failures:
        fail_path = out_dir / "failures.csv"
        pd.DataFrame(failures, columns=["Symbol", "Error"]).to_csv(fail_path, index=False, encoding="utf-8-sig")
        print(f"[FAILED] {len(failures)} symbol(s) -> {fail_path}", file=sys.stderr)

    # 企业微信：仅在状态升级/切换/关键区域变化时发送，适合 cron/GitHub Actions/Windows Task Scheduler。
    to_notify: List[Dict[str, Any]] = []
    next_state = dict(alert_state)
    for trigger in triggers:
        symbol = str(trigger.get("symbol"))
        prev = alert_state.get(symbol) if isinstance(alert_state.get(symbol), dict) else None
        notify = should_notify_trigger(
            trigger, prev, min_level=args.wework_min_level,
            force=args.force_notify, notify_clear=args.notify_clear,
        )
        if notify:
            to_notify.append(trigger)
        next_state[symbol] = build_alert_state(trigger, sent=False)
        if prev and not notify and prev.get("last_sent_utc"):
            next_state[symbol]["last_sent_utc"] = prev.get("last_sent_utc")

    webhook_url = os.environ.get(WEWORK_WEBHOOK_ENV, "").strip()
    if args.no_wework:
        print("[WEWORK] disabled by --no-wework")
    elif not webhook_url:
        print(f"[WEWORK] {WEWORK_WEBHOOK_ENV} not set; skip notification")
    elif not to_notify:
        print("[WEWORK] no new qualifying alert (deduplicated)")
    else:
        content = format_wework_markdown(to_notify)
        ok, detail = send_wework_markdown(webhook_url, content)
        if ok:
            print(f"[WEWORK] sent {len(to_notify)} alert(s)")
            for trigger in to_notify:
                symbol = str(trigger.get("symbol"))
                next_state[symbol] = build_alert_state(trigger, sent=True)
        else:
            print(f"[WEWORK] FAILED: {detail}", file=sys.stderr)
            # 推送失败时不要把 sent 状态落盘，否则下次计划任务无法重试。
            for trigger in to_notify:
                symbol = str(trigger.get("symbol"))
                prev = alert_state.get(symbol) if isinstance(alert_state.get(symbol), dict) else None
                next_state[symbol] = build_alert_state(trigger, sent=False)
                if prev and prev.get("last_sent_utc"):
                    next_state[symbol]["last_sent_utc"] = prev.get("last_sent_utc")

    write_json_file(state_path, next_state)
    print(f"[STATE] {state_path}")

    print(f"[DONE] success={len(summaries)}, failed={len(failures)}")
    return 0 if summaries and (not single_mode or not failures) else 1


if __name__ == "__main__":
    raise SystemExit(main())
