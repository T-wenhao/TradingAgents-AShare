"""Board-gold signal scanning service.

This module integrates the useful parts of the standalone board_has_gold tool
into the FastAPI product: local parquet data loading, entry/exit pattern
strategies, lightweight in-memory scan tasks, and JSON result persistence.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from uuid import uuid4

import pandas as pd


DEFAULT_ENTRY_STRATEGY_PARAMS: Dict[str, Dict[str, Any]] = {
    "three_yin": {
        "enabled": True,
        "min_yin_days": 3,
        "max_yin_days": 5,
        "volume_decrease": True,
    },
    "overnight_hold": {
        "enabled": True,
        "t1_volume_ratio": 1.0,
        "t2_shadow_ratio": 0.5,
    },
    "shrink_yang": {
        "enabled": True,
        "shrink_ratio": 0.6,
        "min_rise": 0.05,
    },
    "phoenix": {
        "enabled": True,
        "min_days": 4,
        "max_days": 10,
        "min_listing_days": 60,
        "min_amplitude": 0.05,
        "close_near_high_ratio": 0.995,
        "consolidation_low_ratio": 0.90,
        "min_avg_amount_20": 50_000_000,
        "require_bearish_candle": False,
    },
    "triple_volume": {
        "enabled": True,
        "volume_ratio": 3.0,
        "min_rise": 0.05,
        "consolidation_days": 5,
        "signal_volume_ratio": 0.6,
    },
    "shrink_yin": {
        "enabled": True,
        "shrink_ratio": 0.6,
    },
}


DEFAULT_EXIT_STRATEGY_PARAMS: Dict[str, Dict[str, Any]] = {
    "fixed_exit": {
        "enabled": True,
        "profit_target": 5.0,
        "stop_loss": -3.0,
        "max_hold_days": 5,
    },
    "trailing_exit": {
        "enabled": True,
        "profit_target": 5.0,
        "stop_loss": -3.0,
        "trailing_stop": 3.0,
        "max_hold_days": 10,
    },
    "phoenix_exit": {
        "enabled": True,
        "stop_loss": -7.0,
        "trailing_stop": 10.0,
        "max_hold_days": 5,
    },
}


BOARD_GOLD_CACHE_SCRIPTS: Dict[str, Dict[str, Any]] = {
    "auto_full": {
        "label": "一键更新本地缓存",
        "description": "自动刷新基础信息、日线缓存、技术因子和质量检查",
        "default_args": [],
        "live_data": True,
        "internal": True,
    },
    "daily_update.py": {
        "label": "每日增量更新",
        "description": "回补最近交易日的前复权和不复权日线缓存",
        "default_args": ["--threads", "1", "--queue-limit", "80", "--low-refresh-per-run", "20"],
        "live_data": True,
    },
    "baostock_daily": {
        "label": "BaoStock 日线增量",
        "description": "使用 BaoStock 写入本地前复权和不复权日线缓存",
        "default_args": ["--max-stocks", "80", "--days", "370", "--sleep", "1.0"],
        "live_data": True,
        "internal": True,
    },
    "collect_stock_basic.py": {
        "label": "股票基础信息",
        "description": "更新 A 股代码和名称缓存",
        "default_args": [],
        "live_data": True,
    },
    "collect_stock_daily.py": {
        "label": "日线采集",
        "description": "按旧采集器更新日线缓存",
        "default_args": ["--fast", "--source", "sina"],
        "live_data": True,
    },
    "calc_factors.py": {
        "label": "技术因子计算",
        "description": "从本地日线缓存计算技术因子",
        "default_args": ["--range", "1y"],
        "live_data": False,
    },
    "collect_adj_factor.py": {
        "label": "复权因子计算",
        "description": "从本地前复权和不复权缓存计算复权因子",
        "default_args": [],
        "live_data": False,
    },
    "check_data_quality.py": {
        "label": "数据质量检查",
        "description": "检查本地 parquet 缓存质量",
        "default_args": [],
        "live_data": False,
    },
}


def _date_to_iso(value: Any) -> str:
    if isinstance(value, pd.Timestamp):
        value = value.date()
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _json_safe(value: Any) -> Any:
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return _date_to_iso(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def normalize_bare_symbol(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    if text.startswith(("SH", "SZ", "BJ")) and len(text) >= 8:
        text = text[2:]
    if "." in text:
        text = text.split(".")[0]
    if text.endswith(".0"):
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text


def normalize_display_symbol(symbol: str) -> str:
    bare = normalize_bare_symbol(symbol)
    if not bare.isdigit() or len(bare) != 6:
        return str(symbol or "").strip().upper()
    if bare.startswith(("6", "5")):
        return f"{bare}.SH"
    if bare.startswith(("8", "4")):
        return f"{bare}.BJ"
    return f"{bare}.SZ"


@dataclass
class Signal:
    strategy: str
    symbol: str
    name: str
    signal_date: date
    base_date: date
    price: float
    change_pct: float
    volume: float
    extra_info: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "symbol": normalize_display_symbol(self.symbol),
            "bare_symbol": normalize_bare_symbol(self.symbol),
            "name": self.name,
            "signal_date": _date_to_iso(self.signal_date),
            "base_date": _date_to_iso(self.base_date),
            "price": _json_safe(self.price),
            "change_pct": _json_safe(self.change_pct),
            "volume": _json_safe(self.volume),
            **_json_safe(self.extra_info),
        }


@dataclass
class ExitSignal:
    strategy: str
    symbol: str
    name: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    exit_type: str
    profit_pct: float
    hold_days: int
    extra_info: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "symbol": normalize_display_symbol(self.symbol),
            "bare_symbol": normalize_bare_symbol(self.symbol),
            "name": self.name,
            "entry_date": _date_to_iso(self.entry_date),
            "entry_price": _json_safe(self.entry_price),
            "exit_date": _date_to_iso(self.exit_date),
            "exit_price": _json_safe(self.exit_price),
            "exit_type": self.exit_type,
            "profit_pct": _json_safe(self.profit_pct),
            "hold_days": _json_safe(self.hold_days),
            **_json_safe(self.extra_info),
        }


class BaseStrategy:
    name = "base"
    description = "策略基类"

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        self.params = params or {}

    def scan(self, df: pd.DataFrame, symbol: str, name: str) -> List[Signal]:
        raise NotImplementedError

    def _is_limit_up(self, row: pd.Series) -> bool:
        return _safe_float(row.get("raw_change_pct", row.get("change_pct", 0))) >= 9.8

    def _is_yin_line(self, row: pd.Series) -> bool:
        return _safe_float(row.get("close")) < _safe_float(row.get("open"))

    def _is_yang_line(self, row: pd.Series) -> bool:
        return _safe_float(row.get("close")) > _safe_float(row.get("open"))

    def _volume_decreased(self, df: pd.DataFrame, days: int) -> bool:
        if len(df) < days:
            return False
        volumes = [_safe_float(value) for value in df["volume"].values]
        return all(volumes[i] > volumes[i + 1] for i in range(days - 1))


class ThreeYinStrategy(BaseStrategy):
    name = "three_yin"
    description = "三阴不破阳：涨停后缩量3连阴，不破涨停最低价，放量阳线确认"

    def scan(self, df: pd.DataFrame, symbol: str, name: str) -> List[Signal]:
        signals: List[Signal] = []
        if len(df) < 10:
            return signals

        min_yin = int(self.params.get("min_yin_days", 3))
        max_yin = int(self.params.get("max_yin_days", 5))
        volume_decrease = bool(self.params.get("volume_decrease", True))

        for i in range(max_yin + 2, len(df)):
            today = df.iloc[i]
            if not self._is_yang_line(today):
                continue

            for yin_days in range(min_yin, max_yin + 1):
                start_idx = i - yin_days - 1
                if start_idx < 0:
                    continue

                limit_up_day = df.iloc[start_idx]
                if not self._is_limit_up(limit_up_day):
                    continue

                yin_slice = df.iloc[start_idx + 1:i]
                if len(yin_slice) != yin_days:
                    continue
                if not all(self._is_yin_line(row) for _, row in yin_slice.iterrows()):
                    continue
                if volume_decrease and not self._volume_decreased(yin_slice, yin_days):
                    continue
                if not all(_safe_float(row["low"]) >= _safe_float(limit_up_day["low"]) for _, row in yin_slice.iterrows()):
                    continue
                if _safe_float(today["volume"]) <= _safe_float(yin_slice.iloc[-1]["volume"]) * 1.2:
                    continue

                signals.append(Signal(
                    strategy=self.name,
                    symbol=symbol,
                    name=name,
                    signal_date=today["date"],
                    base_date=limit_up_day["date"],
                    price=_safe_float(today["close"]),
                    change_pct=_safe_float(today.get("change_pct", 0)),
                    volume=_safe_float(today["volume"]),
                    extra_info={
                        "limit_up_price": round(_safe_float(limit_up_day["close"]), 2),
                        "yin_days": yin_days,
                    },
                ))
                break
        return signals


class OvernightHoldStrategy(BaseStrategy):
    name = "overnight_hold"
    description = "一夜持股：T日涨停，T+1放量高开阳线，T+2缩量长下影阴线"

    def scan(self, df: pd.DataFrame, symbol: str, name: str) -> List[Signal]:
        signals: List[Signal] = []
        if len(df) < 5:
            return signals

        t1_volume_ratio = _safe_float(self.params.get("t1_volume_ratio", 1.0), 1.0)
        t2_shadow_ratio = _safe_float(self.params.get("t2_shadow_ratio", 0.5), 0.5)

        for i in range(2, len(df)):
            t2 = df.iloc[i]
            t1 = df.iloc[i - 1]
            t0 = df.iloc[i - 2]

            if not self._is_limit_up(t0):
                continue
            if not self._is_yang_line(t1):
                continue
            if _safe_float(t1["open"]) <= _safe_float(t0["close"]):
                continue
            if _safe_float(t1["volume"]) < _safe_float(t0["volume"]) * t1_volume_ratio:
                continue
            if _safe_float(t1.get("high", t1["close"])) != _safe_float(t1["close"]):
                continue
            if not self._is_yin_line(t2):
                continue
            if _safe_float(t2["volume"]) >= _safe_float(t1["volume"]):
                continue

            t2_range = _safe_float(t2["high"]) - _safe_float(t2["low"])
            if t2_range == 0:
                continue
            lower_shadow = _safe_float(t2["close"]) - _safe_float(t2["low"])
            if lower_shadow / t2_range < t2_shadow_ratio:
                continue

            t1_change = (_safe_float(t1["close"]) - _safe_float(t1["open"])) / max(_safe_float(t1["open"]), 0.01)
            t2_change = (_safe_float(t2["close"]) - _safe_float(t2["open"])) / max(_safe_float(t2["open"]), 0.01)
            if abs(t2_change) <= abs(t1_change):
                continue

            signals.append(Signal(
                strategy=self.name,
                symbol=symbol,
                name=name,
                signal_date=t2["date"],
                base_date=t0["date"],
                price=_safe_float(t2["close"]),
                change_pct=_safe_float(t2.get("change_pct", 0)),
                volume=_safe_float(t2["volume"]),
                extra_info={
                    "limit_up_price": round(_safe_float(t0["close"]), 2),
                    "t1_open": round(_safe_float(t1["open"]), 2),
                    "t1_close": round(_safe_float(t1["close"]), 2),
                    "t2_lower_shadow_ratio": round(lower_shadow / t2_range, 2),
                },
            ))
        return signals


class ShrinkYangStrategy(BaseStrategy):
    name = "shrink_yang"
    description = "涨停缩量阳：涨停后缩量整理，出现小阳线突破"

    def scan(self, df: pd.DataFrame, symbol: str, name: str) -> List[Signal]:
        signals: List[Signal] = []
        if len(df) < 8:
            return signals

        shrink_ratio = _safe_float(self.params.get("shrink_ratio", 0.6), 0.6)
        min_rise = _safe_float(self.params.get("min_rise", 0.05), 0.05)

        for i in range(3, len(df)):
            signal_day = df.iloc[i]
            if not self._is_yang_line(signal_day):
                continue

            for base_offset in range(2, 6):
                base_idx = i - base_offset
                if base_idx < 0:
                    continue

                base_day = df.iloc[base_idx]
                if not self._is_limit_up(base_day):
                    continue
                if _safe_float(base_day.get("change_pct", 0)) / 100 < min_rise:
                    continue
                if _safe_float(signal_day["volume"]) > _safe_float(base_day["volume"]) * shrink_ratio:
                    continue

                middle_slice = df.iloc[base_idx + 1:i]
                if middle_slice.empty:
                    continue
                if any(_safe_float(row["low"]) < _safe_float(base_day["close"]) * 0.95 for _, row in middle_slice.iterrows()):
                    continue

                signals.append(Signal(
                    strategy=self.name,
                    symbol=symbol,
                    name=name,
                    signal_date=signal_day["date"],
                    base_date=base_day["date"],
                    price=_safe_float(signal_day["close"]),
                    change_pct=_safe_float(signal_day.get("change_pct", 0)),
                    volume=_safe_float(signal_day["volume"]),
                    extra_info={
                        "limit_up_price": round(_safe_float(base_day["close"]), 2),
                        "volume_ratio": round(_safe_float(signal_day["volume"]) / max(_safe_float(base_day["volume"]), 1), 2),
                        "consolidation_days": base_offset - 1,
                    },
                ))
                break
        return signals


class PhoenixStrategy(BaseStrategy):
    name = "phoenix"
    description = "涨停金凤凰：首板启动后高位整理，4-10日内二次涨停确认"

    def scan(self, df: pd.DataFrame, symbol: str, name: str) -> List[Signal]:
        signals: List[Signal] = []
        if df.empty:
            return signals

        min_listing_days = int(self.params.get("min_listing_days", 60))
        min_days = int(self.params.get("min_days", 4))
        max_days = int(self.params.get("max_days", 10))
        min_amplitude = _safe_float(self.params.get("min_amplitude", 0.05), 0.05)
        close_near_high_ratio = _safe_float(self.params.get("close_near_high_ratio", 0.995), 0.995)
        consolidation_low_ratio = _safe_float(self.params.get("consolidation_low_ratio", 0.90), 0.90)
        min_avg_amount_20 = _safe_float(self.params.get("min_avg_amount_20", 50_000_000), 50_000_000)
        require_bearish_candle = bool(self.params.get("require_bearish_candle", False))

        if len(df) < max(max_days + 2, min_listing_days):
            return signals
        if self._is_excluded_stock(name):
            return signals

        df = df.sort_values("date").reset_index(drop=True).copy()
        if "amount" in df.columns and min_avg_amount_20 > 0:
            avg_amount_20 = _safe_float(df["amount"].tail(20).mean())
            if avg_amount_20 < min_avg_amount_20:
                return signals
        else:
            avg_amount_20 = 0.0

        for base_idx in range(1, len(df) - min_days):
            first_day = df.iloc[base_idx]
            prev_day = df.iloc[base_idx - 1]

            if not self._is_tradeable(first_day):
                continue
            if not self._is_valid_limit_day(first_day, prev_day, symbol, min_amplitude, close_near_high_ratio):
                continue
            if self._is_limit_up_day(prev_day, df.iloc[base_idx - 2] if base_idx >= 2 else None, symbol):
                continue

            for signal_idx in range(base_idx + min_days, min(base_idx + max_days + 1, len(df))):
                signal_day = df.iloc[signal_idx]
                prev_signal_day = df.iloc[signal_idx - 1]
                middle_slice = df.iloc[base_idx + 1:signal_idx]

                if middle_slice.empty:
                    continue
                if self._has_limit_up_between(df, base_idx + 1, signal_idx - 1, symbol):
                    continue
                if not self._consolidation_ok(first_day, middle_slice, consolidation_low_ratio, require_bearish_candle):
                    continue
                if not self._is_tradeable(signal_day):
                    continue
                if not self._is_valid_limit_day(signal_day, prev_signal_day, symbol, min_amplitude, close_near_high_ratio):
                    continue

                buy_day = df.iloc[signal_idx + 1] if signal_idx + 1 < len(df) else None
                buy_date = buy_day["date"] if buy_day is not None else None
                buy_price = _safe_float(buy_day["open"]) if buy_day is not None else None

                signals.append(Signal(
                    strategy=self.name,
                    symbol=symbol,
                    name=name,
                    signal_date=signal_day["date"],
                    base_date=first_day["date"],
                    price=buy_price if buy_price else _safe_float(signal_day["close"]),
                    change_pct=_safe_float(signal_day.get("raw_change_pct", signal_day.get("change_pct", 0))),
                    volume=_safe_float(signal_day["volume"]),
                    extra_info={
                        "first_limit_up_date": _date_to_iso(first_day["date"]),
                        "second_limit_up_date": _date_to_iso(signal_day["date"]),
                        "buy_date": _date_to_iso(buy_date) if buy_date is not None else "",
                        "buy_price": round(buy_price, 2) if buy_price else None,
                        "buy_rule": "S+1开盘价买入；若 buy_date 为空，表示数据尚未包含下一交易日",
                        "support_price": round(_safe_float(signal_day["low"]), 2),
                        "signal_low": round(_safe_float(signal_day["low"]), 2),
                        "signal_high": round(_safe_float(signal_day["high"]), 2),
                        "signal_close": round(_safe_float(signal_day["close"]), 2),
                        "first_limit_up_price": round(_safe_float(first_day["close"]), 2),
                        "days_between": signal_idx - base_idx,
                        "consolidation_days": len(middle_slice),
                        "consolidation_min_low": round(_safe_float(middle_slice["low"].min()), 2),
                        "consolidation_min_close": round(_safe_float(middle_slice["close"].min()), 2),
                        "consolidation_drawdown_pct": round(
                            (_safe_float(middle_slice["low"].min()) / max(_safe_float(first_day["close"]), 0.01) - 1) * 100,
                            2,
                        ),
                        "has_bearish_candle": int(any(middle_slice["close"] < middle_slice["open"])),
                        "avg_amount_20": round(avg_amount_20, 2),
                        "limit_rate_pct": round(self._limit_rate(symbol, name) * 100, 2),
                    },
                ))
                break
        return signals

    def _is_excluded_stock(self, name: str) -> bool:
        upper_name = (name or "").upper()
        return "ST" in upper_name or "退" in upper_name

    def _limit_rate(self, symbol: str, name: str = "") -> float:
        bare = normalize_bare_symbol(symbol)
        if bare.startswith(("300", "301", "688", "689")):
            return 0.20
        return 0.10

    def _price(self, row: pd.Series, field_name: str) -> float:
        raw_field = f"raw_{field_name}"
        value = row.get(raw_field, None)
        if value is not None and not pd.isna(value):
            return _safe_float(value)
        return _safe_float(row.get(field_name, 0))

    def _is_tradeable(self, row: pd.Series) -> bool:
        return _safe_float(row.get("volume", 0)) > 0 and _safe_float(row.get("open", 0)) > 0 and _safe_float(row.get("close", 0)) > 0

    def _amplitude(self, row: pd.Series, prev_row: Optional[pd.Series]) -> float:
        if prev_row is None:
            return 0.0
        prev_close = self._price(prev_row, "close")
        if prev_close <= 0:
            return 0.0
        return (self._price(row, "high") - self._price(row, "low")) / prev_close

    def _is_limit_up_day(self, row: pd.Series, prev_row: Optional[pd.Series], symbol: str) -> bool:
        if prev_row is None:
            return False

        close_price = self._price(row, "close")
        prev_close = self._price(prev_row, "close")
        if close_price <= 0 or prev_close <= 0:
            return False

        limit_rate = self._limit_rate(symbol)
        limit_up_price = round(prev_close * (1 + limit_rate), 2)
        if close_price >= limit_up_price * 0.995:
            return True

        fallback_change = row.get("raw_change_pct", row.get("change_pct", 0))
        return _safe_float(fallback_change) >= limit_rate * 100 - 0.5

    def _is_valid_limit_day(
        self,
        row: pd.Series,
        prev_row: pd.Series,
        symbol: str,
        min_amplitude: float,
        close_near_high_ratio: float,
    ) -> bool:
        if not self._is_limit_up_day(row, prev_row, symbol):
            return False
        if self._amplitude(row, prev_row) <= min_amplitude:
            return False

        close_price = self._price(row, "close")
        high_price = self._price(row, "high")
        if high_price <= 0 or close_price < high_price * close_near_high_ratio:
            return False

        low_price = self._price(row, "low")
        if high_price > 0 and (high_price - low_price) / high_price < 0.01:
            return False
        return True

    def _has_limit_up_between(self, df: pd.DataFrame, start_idx: int, end_idx: int, symbol: str) -> bool:
        if start_idx > end_idx:
            return False
        for idx in range(start_idx, end_idx + 1):
            prev_row = df.iloc[idx - 1] if idx > 0 else None
            if self._is_limit_up_day(df.iloc[idx], prev_row, symbol):
                return True
        return False

    def _consolidation_ok(
        self,
        first_day: pd.Series,
        middle_slice: pd.DataFrame,
        consolidation_low_ratio: float,
        require_bearish_candle: bool,
    ) -> bool:
        if _safe_float(middle_slice["low"].min()) < _safe_float(first_day["close"]) * consolidation_low_ratio:
            return False
        first_body_low = min(_safe_float(first_day["open"]), _safe_float(first_day["close"]))
        if _safe_float(middle_slice["close"].min()) < first_body_low:
            return False
        if require_bearish_candle and not any(middle_slice["close"] < middle_slice["open"]):
            return False
        return True


class TripleVolumeStrategy(BaseStrategy):
    name = "triple_volume"
    description = "三倍量突破：成交量3倍以上+涨幅>=5%，缩量整理后小阳线"

    def scan(self, df: pd.DataFrame, symbol: str, name: str) -> List[Signal]:
        signals: List[Signal] = []
        if len(df) < 10:
            return signals

        volume_ratio = _safe_float(self.params.get("volume_ratio", 3.0), 3.0)
        min_rise = _safe_float(self.params.get("min_rise", 0.05), 0.05)
        max_consolidation = int(self.params.get("consolidation_days", 5))
        signal_volume_ratio = _safe_float(self.params.get("signal_volume_ratio", 0.6), 0.6)

        for i in range(max_consolidation + 2, len(df)):
            signal_day = df.iloc[i]
            if not self._is_yang_line(signal_day):
                continue

            for base_offset in range(2, max_consolidation + 2):
                base_idx = i - base_offset
                if base_idx <= 0:
                    continue

                base_day = df.iloc[base_idx]
                prev_day = df.iloc[base_idx - 1]
                if _safe_float(base_day["volume"]) < _safe_float(prev_day["volume"]) * volume_ratio:
                    continue
                base_change = _safe_float(base_day.get("change_pct", 0)) / 100
                if base_change < min_rise:
                    continue
                if _safe_float(signal_day["volume"]) > _safe_float(base_day["volume"]) * signal_volume_ratio:
                    continue

                consolidation_slice = df.iloc[base_idx + 1:i]
                if consolidation_slice.empty:
                    continue
                if any(_safe_float(row["close"]) < _safe_float(base_day["close"]) for _, row in consolidation_slice.iterrows()):
                    continue
                if not any(self._is_yin_line(row) for _, row in consolidation_slice.iterrows()):
                    continue

                base_turnover = _safe_float(base_day.get("turnover", 0))
                turnover_filter = base_turnover <= 5.0 if base_turnover > 0 else True

                signals.append(Signal(
                    strategy=self.name,
                    symbol=symbol,
                    name=name,
                    signal_date=signal_day["date"],
                    base_date=base_day["date"],
                    price=_safe_float(signal_day["close"]),
                    change_pct=_safe_float(signal_day.get("change_pct", 0)),
                    volume=_safe_float(signal_day["volume"]),
                    extra_info={
                        "base_volume_ratio": round(_safe_float(base_day["volume"]) / max(_safe_float(prev_day["volume"]), 1), 2),
                        "base_change_pct": round(base_change * 100, 2),
                        "signal_volume_ratio": round(_safe_float(signal_day["volume"]) / max(_safe_float(base_day["volume"]), 1), 2),
                        "consolidation_days": base_offset - 1,
                        "turnover_filter_passed": 1 if turnover_filter else 0,
                    },
                ))
                break
        return signals


class ShrinkYinStrategy(BaseStrategy):
    name = "shrink_yin"
    description = "涨停缩量阴：涨停后缩量阴线整理"

    def scan(self, df: pd.DataFrame, symbol: str, name: str) -> List[Signal]:
        signals: List[Signal] = []
        if len(df) < 8:
            return signals

        shrink_ratio = _safe_float(self.params.get("shrink_ratio", 0.6), 0.6)

        for i in range(3, len(df)):
            signal_day = df.iloc[i]
            if not self._is_yang_line(signal_day):
                continue

            for base_offset in range(2, 6):
                base_idx = i - base_offset
                if base_idx < 0:
                    continue

                base_day = df.iloc[base_idx]
                if not self._is_limit_up(base_day):
                    continue

                middle_slice = df.iloc[base_idx + 1:i]
                if middle_slice.empty:
                    continue
                if not all(self._is_yin_line(row) for _, row in middle_slice.iterrows()):
                    continue
                if not self._volume_decreased(middle_slice, len(middle_slice)):
                    continue
                if any(_safe_float(row["low"]) < _safe_float(base_day["close"]) * 0.95 for _, row in middle_slice.iterrows()):
                    continue
                if _safe_float(signal_day["volume"]) > _safe_float(base_day["volume"]) * shrink_ratio:
                    continue

                signals.append(Signal(
                    strategy=self.name,
                    symbol=symbol,
                    name=name,
                    signal_date=signal_day["date"],
                    base_date=base_day["date"],
                    price=_safe_float(signal_day["close"]),
                    change_pct=_safe_float(signal_day.get("change_pct", 0)),
                    volume=_safe_float(signal_day["volume"]),
                    extra_info={
                        "limit_up_price": round(_safe_float(base_day["close"]), 2),
                        "yin_days": len(middle_slice),
                        "volume_ratio": round(_safe_float(signal_day["volume"]) / max(_safe_float(base_day["volume"]), 1), 2),
                    },
                ))
                break
        return signals


ENTRY_STRATEGY_REGISTRY: Dict[str, type[BaseStrategy]] = {
    "three_yin": ThreeYinStrategy,
    "overnight_hold": OvernightHoldStrategy,
    "shrink_yang": ShrinkYangStrategy,
    "phoenix": PhoenixStrategy,
    "triple_volume": TripleVolumeStrategy,
    "shrink_yin": ShrinkYinStrategy,
}


class ExitStrategy(BaseStrategy):
    name = "exit"
    description = "离场策略"

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        super().__init__(params)
        self.profit_target = _safe_float(self.params.get("profit_target", 5.0), 5.0)
        self.stop_loss = _safe_float(self.params.get("stop_loss", -3.0), -3.0)
        self.max_hold_days = int(self.params.get("max_hold_days", 5))

    def check_exit(
        self,
        df: pd.DataFrame,
        entry_date: date,
        entry_price: float,
        strategy: str,
        symbol: str,
        name: str,
        entry_signal: Optional[Dict[str, Any]] = None,
    ) -> Optional[ExitSignal]:
        raise NotImplementedError

    def scan(self, df: pd.DataFrame, symbol: str, name: str) -> List[Signal]:
        return []


class FixedExitStrategy(ExitStrategy):
    name = "fixed_exit"
    description = "固定止盈止损：达到目标涨幅或跌破止损位离场"

    def check_exit(
        self,
        df: pd.DataFrame,
        entry_date: date,
        entry_price: float,
        strategy: str,
        symbol: str,
        name: str,
        entry_signal: Optional[Dict[str, Any]] = None,
    ) -> Optional[ExitSignal]:
        entry_idx = df[df["date"] >= pd.Timestamp(entry_date)].index
        if len(entry_idx) == 0:
            return None
        start_idx = int(entry_idx[0])

        for i in range(start_idx + 1, min(start_idx + self.max_hold_days + 1, len(df))):
            row = df.iloc[i]
            current_return = (_safe_float(row["close"]) - entry_price) / entry_price * 100
            if current_return >= self.profit_target:
                return self._make_signal(strategy, symbol, name, entry_date, entry_price, row, "profit", current_return, i - start_idx, {"target": self.profit_target})
            if current_return <= self.stop_loss:
                return self._make_signal(strategy, symbol, name, entry_date, entry_price, row, "stop_loss", current_return, i - start_idx, {"stop_loss": self.stop_loss})

        if start_idx + self.max_hold_days < len(df):
            row = df.iloc[start_idx + self.max_hold_days]
            current_return = (_safe_float(row["close"]) - entry_price) / entry_price * 100
            return self._make_signal(strategy, symbol, name, entry_date, entry_price, row, "time", current_return, self.max_hold_days, {"max_hold_days": self.max_hold_days})
        return None

    def _make_signal(
        self,
        strategy: str,
        symbol: str,
        name: str,
        entry_date: date,
        entry_price: float,
        row: pd.Series,
        exit_type: str,
        current_return: float,
        hold_days: int,
        extra_info: Dict[str, Any],
    ) -> ExitSignal:
        return ExitSignal(
            strategy=strategy,
            symbol=symbol,
            name=name,
            entry_date=entry_date,
            entry_price=entry_price,
            exit_date=pd.Timestamp(row["date"]).date(),
            exit_price=_safe_float(row["close"]),
            exit_type=exit_type,
            profit_pct=round(current_return, 2),
            hold_days=hold_days,
            extra_info=extra_info,
        )


class TrailingExitStrategy(ExitStrategy):
    name = "trailing_exit"
    description = "移动止盈：从最高点回撤一定比例离场"

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        super().__init__(params)
        self.trailing_stop = _safe_float(self.params.get("trailing_stop", 3.0), 3.0)

    def check_exit(
        self,
        df: pd.DataFrame,
        entry_date: date,
        entry_price: float,
        strategy: str,
        symbol: str,
        name: str,
        entry_signal: Optional[Dict[str, Any]] = None,
    ) -> Optional[ExitSignal]:
        entry_idx = df[df["date"] >= pd.Timestamp(entry_date)].index
        if len(entry_idx) == 0:
            return None
        start_idx = int(entry_idx[0])
        max_price = entry_price

        for i in range(start_idx + 1, min(start_idx + self.max_hold_days + 1, len(df))):
            row = df.iloc[i]
            max_price = max(max_price, _safe_float(row["high"]))
            drawdown = (max_price - _safe_float(row["close"])) / max(max_price, 0.01) * 100
            current_return = (_safe_float(row["close"]) - entry_price) / entry_price * 100
            if drawdown >= self.trailing_stop and max_price > entry_price:
                return ExitSignal(
                    strategy=strategy,
                    symbol=symbol,
                    name=name,
                    entry_date=entry_date,
                    entry_price=entry_price,
                    exit_date=pd.Timestamp(row["date"]).date(),
                    exit_price=_safe_float(row["close"]),
                    exit_type="trailing",
                    profit_pct=round(current_return, 2),
                    hold_days=i - start_idx,
                    extra_info={"max_price": max_price, "trailing_stop": self.trailing_stop, "drawdown": round(drawdown, 2)},
                )
            if current_return <= self.stop_loss:
                return ExitSignal(
                    strategy=strategy,
                    symbol=symbol,
                    name=name,
                    entry_date=entry_date,
                    entry_price=entry_price,
                    exit_date=pd.Timestamp(row["date"]).date(),
                    exit_price=_safe_float(row["close"]),
                    exit_type="stop_loss",
                    profit_pct=round(current_return, 2),
                    hold_days=i - start_idx,
                    extra_info={"stop_loss": self.stop_loss},
                )
        return None


class PhoenixExitStrategy(TrailingExitStrategy):
    name = "phoenix_exit"
    description = "涨停金凤凰离场：放量阴线或跌破二次涨停日低点，叠加时间/止损/移动止盈"

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        super().__init__(params)
        self.stop_loss = _safe_float(self.params.get("stop_loss", -7.0), -7.0)
        self.trailing_stop = _safe_float(self.params.get("trailing_stop", 10.0), 10.0)
        self.max_hold_days = int(self.params.get("max_hold_days", 5))

    def check_exit(
        self,
        df: pd.DataFrame,
        entry_date: date,
        entry_price: float,
        strategy: str,
        symbol: str,
        name: str,
        entry_signal: Optional[Dict[str, Any]] = None,
    ) -> Optional[ExitSignal]:
        entry_idx = df[df["date"] >= pd.Timestamp(entry_date)].index
        if len(entry_idx) == 0:
            return None
        start_idx = int(entry_idx[0])
        entry_signal = entry_signal or {}
        support_price = _safe_float(entry_signal.get("support_price") or entry_signal.get("signal_low") or 0)
        signal_high = _safe_float(entry_signal.get("signal_high") or 0)
        highest_close = entry_price
        volume_ma5 = df["volume"].rolling(5, min_periods=1).mean()

        end_idx = min(start_idx + self.max_hold_days + 1, len(df))
        for i in range(start_idx + 1, end_idx):
            row = df.iloc[i]
            prev_row = df.iloc[i - 1]
            highest_close = max(highest_close, _safe_float(row["close"]))
            current_return = (_safe_float(row["close"]) - entry_price) / entry_price * 100
            hold_days = i - start_idx

            if (
                _safe_float(row["close"]) < _safe_float(row["open"])
                and _safe_float(row["close"]) < _safe_float(prev_row["close"])
                and _safe_float(row["volume"]) > _safe_float(volume_ma5.iloc[i])
            ):
                return self._exit_signal(strategy, symbol, name, entry_date, entry_price, row, "phoenix_volume_drop", current_return, hold_days, {"volume_ma5": round(_safe_float(volume_ma5.iloc[i]), 2)})
            if support_price > 0 and _safe_float(row["close"]) < support_price:
                return self._exit_signal(strategy, symbol, name, entry_date, entry_price, row, "phoenix_support_break", current_return, hold_days, {"support_price": support_price})
            if _safe_float(row["close"]) <= entry_price * (1 + self.stop_loss / 100):
                return self._exit_signal(strategy, symbol, name, entry_date, entry_price, row, "stop_loss", current_return, hold_days, {"stop_loss": self.stop_loss})
            if highest_close > entry_price and _safe_float(row["close"]) <= highest_close * (1 - self.trailing_stop / 100):
                drawdown = (highest_close - _safe_float(row["close"])) / max(highest_close, 0.01) * 100
                return self._exit_signal(strategy, symbol, name, entry_date, entry_price, row, "trailing", current_return, hold_days, {"highest_close": highest_close, "trailing_stop": self.trailing_stop, "drawdown": round(drawdown, 2)})

        time_exit_idx = min(start_idx + self.max_hold_days, len(df) - 1)
        if time_exit_idx > start_idx:
            high_since_buy = _safe_float(df.iloc[start_idx:time_exit_idx + 1]["high"].max())
            if signal_high <= 0 or high_since_buy <= signal_high:
                row = df.iloc[time_exit_idx]
                current_return = (_safe_float(row["close"]) - entry_price) / entry_price * 100
                return self._exit_signal(strategy, symbol, name, entry_date, entry_price, row, "time", current_return, time_exit_idx - start_idx, {"max_hold_days": self.max_hold_days, "signal_high": signal_high})
        return None

    def _exit_signal(
        self,
        strategy: str,
        symbol: str,
        name: str,
        entry_date: date,
        entry_price: float,
        row: pd.Series,
        exit_type: str,
        current_return: float,
        hold_days: int,
        extra_info: Dict[str, Any],
    ) -> ExitSignal:
        return ExitSignal(
            strategy=strategy,
            symbol=symbol,
            name=name,
            entry_date=entry_date,
            entry_price=entry_price,
            exit_date=pd.Timestamp(row["date"]).date(),
            exit_price=_safe_float(row["close"]),
            exit_type=exit_type,
            profit_pct=round(current_return, 2),
            hold_days=hold_days,
            extra_info=extra_info,
        )


EXIT_STRATEGY_REGISTRY: Dict[str, type[ExitStrategy]] = {
    "fixed_exit": FixedExitStrategy,
    "trailing_exit": TrailingExitStrategy,
    "phoenix_exit": PhoenixExitStrategy,
}


@dataclass
class ScanTask:
    id: str
    status: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    current: int = 0
    total: int = 0
    signals: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "current": self.current,
            "total": self.total,
            "signals_count": len(self.signals),
            "signals": self.signals,
            "error": self.error,
            "params": self.params,
            "logs": self.logs[-120:],
        }


@dataclass
class CacheUpdateTask:
    id: str
    status: str
    script: str
    args: List[str]
    created_at: str
    scripts_dir: str
    cwd: str
    command: List[str]
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    logs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.id,
            "status": self.status,
            "script": self.script,
            "args": self.args,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "error": self.error,
            "scripts_dir": self.scripts_dir,
            "cwd": self.cwd,
            "command": self.command,
            "logs": self.logs[-240:],
        }


class BoardGoldScanner:
    """Local-cache strategy scanner."""

    def __init__(self, data_dir: Optional[Path] = None, results_dir: Optional[Path] = None):
        self.data_dir = data_dir or self._default_data_dir()
        self.results_dir = results_dir or Path(os.getenv("TA_BOARD_GOLD_RESULTS_DIR", "board_gold_results")).expanduser()

    @staticmethod
    def _default_data_dir() -> Path:
        raw = os.getenv("TA_BOARD_GOLD_DATA_DIR") or os.getenv("BOARD_HAS_GOLD_DATA_DIR") or "data/board_gold"
        return Path(raw).expanduser()

    def _stock_daily_dir(self) -> Path:
        return self.data_dir / "stock_daily"

    def _stock_daily_raw_dir(self) -> Path:
        return self.data_dir / "stock_daily_raw"

    def _stock_basic_file(self) -> Path:
        return self.data_dir / "stock_basic" / "stock_basic.parquet"

    def cache_stats(self) -> Dict[str, Any]:
        daily_dir = self._stock_daily_dir()
        raw_dir = self._stock_daily_raw_dir()
        basic_file = self._stock_basic_file()
        daily_files = list(daily_dir.glob("*.parquet")) if daily_dir.exists() else []
        raw_files = list(raw_dir.glob("*.parquet")) if raw_dir.exists() else []
        stock_count = 0
        if basic_file.exists():
            try:
                stock_count = len(pd.read_parquet(basic_file, columns=None))
            except Exception:
                stock_count = 0
        latest_mtime = max((file.stat().st_mtime for file in daily_files), default=None)
        return {
            "data_dir": str(self.data_dir),
            "available": daily_dir.exists() and bool(daily_files),
            "stock_basic_file": str(basic_file),
            "stock_basic_count": stock_count,
            "daily_count": len(daily_files),
            "raw_count": len(raw_files),
            "latest_file_mtime": datetime.fromtimestamp(latest_mtime).isoformat() if latest_mtime else None,
        }

    def strategy_info(self) -> Dict[str, Any]:
        entry = [
            {
                "name": name,
                "description": cls.description,
                "enabled": DEFAULT_ENTRY_STRATEGY_PARAMS.get(name, {}).get("enabled", True),
                "params": DEFAULT_ENTRY_STRATEGY_PARAMS.get(name, {}),
            }
            for name, cls in ENTRY_STRATEGY_REGISTRY.items()
        ]
        exit_items = [
            {
                "name": name,
                "description": cls.description,
                "enabled": DEFAULT_EXIT_STRATEGY_PARAMS.get(name, {}).get("enabled", True),
                "params": DEFAULT_EXIT_STRATEGY_PARAMS.get(name, {}),
            }
            for name, cls in EXIT_STRATEGY_REGISTRY.items()
        ]
        return {"entry_strategies": entry, "exit_strategies": exit_items}

    def load_stock_list(self) -> List[Dict[str, str]]:
        basic_file = self._stock_basic_file()
        items: List[Dict[str, str]] = []
        if basic_file.exists():
            try:
                df = pd.read_parquet(basic_file)
                code_col = self._first_column(df, ["code", "symbol", "代码", "证券代码"])
                name_col = self._first_column(df, ["name", "名称", "股票简称", "证券简称"])
                if code_col:
                    for _, row in df.iterrows():
                        bare = normalize_bare_symbol(row.get(code_col, ""))
                        if bare.isdigit() and len(bare) == 6:
                            items.append({"symbol": bare, "name": str(row.get(name_col, "")) if name_col else ""})
            except Exception:
                items = []
        if items:
            return items

        daily_dir = self._stock_daily_dir()
        if not daily_dir.exists():
            return []
        return [
            {"symbol": file.stem, "name": ""}
            for file in sorted(daily_dir.glob("*.parquet"))
            if normalize_bare_symbol(file.stem).isdigit()
        ]

    def load_stock_daily(self, symbol: str, days: int = 80) -> pd.DataFrame:
        bare = normalize_bare_symbol(symbol)
        file_path = self._stock_daily_dir() / f"{bare}.parquet"
        if not file_path.exists():
            return pd.DataFrame()

        df = pd.read_parquet(file_path)
        df = self._normalize_daily_columns(df)

        raw_file_path = self._stock_daily_raw_dir() / f"{bare}.parquet"
        if raw_file_path.exists():
            try:
                raw_df = self._normalize_daily_columns(pd.read_parquet(raw_file_path))
                raw_columns = [column for column in ["date", "open", "high", "low", "close", "volume"] if column in raw_df.columns]
                raw_df = raw_df[raw_columns].rename(columns={column: f"raw_{column}" for column in raw_columns if column != "date"})
                df = df.merge(raw_df, on="date", how="left")
            except Exception:
                pass

        if len(df) > days:
            df = df.tail(days)
        if "change_pct" not in df.columns and len(df) > 1:
            df["change_pct"] = df["close"].pct_change() * 100
        if "raw_close" in df.columns and "raw_change_pct" not in df.columns and len(df) > 1:
            df["raw_change_pct"] = df["raw_close"].pct_change() * 100
        return df

    def scan_entries(
        self,
        strategy_names: Optional[List[str]] = None,
        symbols: Optional[List[str]] = None,
        days: int = 80,
        target_date: Optional[str] = None,
        max_stocks: Optional[int] = None,
        progress: Optional[Callable[[int, int, List[Dict[str, Any]], str], None]] = None,
    ) -> Dict[str, Any]:
        strategy_names = strategy_names or list(ENTRY_STRATEGY_REGISTRY.keys())
        strategies = self._build_entry_strategies(strategy_names)
        if not strategies:
            raise ValueError("没有可用的入场策略")

        items = self._select_stock_items(symbols)
        if max_stocks:
            items = items[:max_stocks]
        if not items:
            raise ValueError("本地股票池为空，请检查 TA_BOARD_GOLD_DATA_DIR")

        target = date.fromisoformat(target_date) if target_date else None
        all_signals: List[Signal] = []
        start = time.time()
        total = len(items)

        for index, item in enumerate(items, start=1):
            symbol = item["symbol"]
            name = item.get("name") or ""
            try:
                df = self.load_stock_daily(symbol, days=days)
                if df.empty:
                    message = f"{normalize_display_symbol(symbol)} 无本地日线缓存"
                    if progress:
                        progress(index, total, [s.to_dict() for s in all_signals], message)
                    continue
                for strategy in strategies:
                    for signal in strategy.scan(df, symbol, name):
                        if target and pd.Timestamp(signal.signal_date).date() != target:
                            continue
                        all_signals.append(signal)
            except Exception as exc:
                if progress:
                    progress(index, total, [s.to_dict() for s in all_signals], f"{normalize_display_symbol(symbol)} 扫描异常：{exc}")
                continue

            if progress and (index == total or index % 20 == 0):
                progress(index, total, [s.to_dict() for s in all_signals], f"已扫描 {index}/{total}")

        payload = {
            "scan_time": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.time() - start, 2),
            "strategies": [strategy.name for strategy in strategies],
            "target_date": target_date,
            "days": days,
            "total_stocks": total,
            "signals_count": len(all_signals),
            "signals": [signal.to_dict() for signal in all_signals],
            "summary": self._summary(all_signals),
        }
        self.save_latest_result(payload)
        return payload

    def scan_exits(
        self,
        entries: List[Dict[str, Any]],
        exit_strategy_name: str = "fixed_exit",
        days: int = 120,
    ) -> Dict[str, Any]:
        cls = EXIT_STRATEGY_REGISTRY.get(exit_strategy_name)
        if not cls:
            raise ValueError(f"离场策略不存在: {exit_strategy_name}")
        params = dict(DEFAULT_EXIT_STRATEGY_PARAMS.get(exit_strategy_name, {}))
        params.pop("enabled", None)
        exit_strategy = cls(params)
        exit_signals: List[ExitSignal] = []

        for entry in entries:
            symbol = entry.get("symbol") or entry.get("bare_symbol") or ""
            bare = normalize_bare_symbol(symbol)
            entry_date_str = entry.get("buy_date") or entry.get("signal_date") or entry.get("entry_date")
            entry_price = _safe_float(entry.get("buy_price") or entry.get("price") or entry.get("entry_price"))
            if not bare or not entry_date_str or entry_price <= 0:
                continue
            df = self.load_stock_daily(bare, days=days)
            if df.empty:
                continue
            try:
                exit_signal = exit_strategy.check_exit(
                    df=df,
                    entry_date=date.fromisoformat(str(entry_date_str)[:10]),
                    entry_price=entry_price,
                    strategy=str(entry.get("strategy") or exit_strategy_name),
                    symbol=bare,
                    name=str(entry.get("name") or ""),
                    entry_signal=entry,
                )
                if exit_signal:
                    exit_signals.append(exit_signal)
            except Exception:
                continue

        return {
            "scan_time": datetime.now(timezone.utc).isoformat(),
            "exit_strategy": exit_strategy_name,
            "entries_count": len(entries),
            "exit_signals_count": len(exit_signals),
            "exit_signals": [signal.to_dict() for signal in exit_signals],
        }

    def latest_result(self) -> Optional[Dict[str, Any]]:
        path = self.results_dir / "scan_result_latest.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save_latest_result(self, payload: Dict[str, Any]) -> None:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        latest_path = self.results_dir / "scan_result_latest.json"
        dated_path = self.results_dir / f"scan_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        latest_path.write_text(text, encoding="utf-8")
        dated_path.write_text(text, encoding="utf-8")

    def _select_stock_items(self, symbols: Optional[List[str]]) -> List[Dict[str, str]]:
        if not symbols:
            return self.load_stock_list()
        known = {normalize_bare_symbol(item["symbol"]): item for item in self.load_stock_list()}
        selected: List[Dict[str, str]] = []
        for raw in symbols:
            bare = normalize_bare_symbol(raw)
            if not bare:
                continue
            known_item = known.get(bare)
            selected.append({"symbol": bare, "name": known_item.get("name", "") if known_item else ""})
        return selected

    def _build_entry_strategies(self, strategy_names: Iterable[str]) -> List[BaseStrategy]:
        strategies: List[BaseStrategy] = []
        for name in strategy_names:
            cls = ENTRY_STRATEGY_REGISTRY.get(name)
            if not cls:
                continue
            params = dict(DEFAULT_ENTRY_STRATEGY_PARAMS.get(name, {}))
            enabled = params.pop("enabled", True)
            if enabled:
                strategies.append(cls(params))
        return strategies

    def _summary(self, signals: List[Signal]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {}
        for signal in signals:
            bucket = summary.setdefault(signal.strategy, {"count": 0, "stocks": []})
            bucket["count"] += 1
            bucket["stocks"].append({
                "symbol": normalize_display_symbol(signal.symbol),
                "name": signal.name,
                "price": signal.price,
                "change_pct": signal.change_pct,
                "signal_date": _date_to_iso(signal.signal_date),
            })
        return summary

    def _normalize_daily_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        column_mapping = {
            "日期": "date",
            "交易日期": "date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "成交额": "amount",
            "换手率": "turnover",
            "涨跌幅": "change_pct",
        }
        df = df.rename(columns={key: value for key, value in column_mapping.items() if key in df.columns})
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")
        required = ["open", "high", "low", "close", "volume"]
        for column in required:
            if column not in df.columns:
                df[column] = 0
        return df.reset_index(drop=True)

    def _first_column(self, df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
        for name in candidates:
            if name in df.columns:
                return name
        return None


class BoardGoldTaskManager:
    def __init__(self, scanner: Optional[BoardGoldScanner] = None):
        self.scanner = scanner or BoardGoldScanner()
        workers = int(os.getenv("TA_BOARD_GOLD_SCAN_WORKERS", "1") or "1")
        self.executor = ThreadPoolExecutor(max_workers=max(1, min(workers, 4)))
        self.cache_executor = ThreadPoolExecutor(max_workers=1)
        self.tasks: Dict[str, ScanTask] = {}
        self.cache_tasks: Dict[str, CacheUpdateTask] = {}
        self.lock = threading.Lock()

    def cache_stats(self) -> Dict[str, Any]:
        stats = self.scanner.cache_stats()
        scripts_dir = self._cache_scripts_dir()
        stats["cache_scripts_dir"] = str(scripts_dir)
        stats["cache_scripts_available"] = scripts_dir.exists()
        return stats

    def list_cache_scripts(self) -> Dict[str, Any]:
        scripts_dir = self._cache_scripts_dir()
        scripts = []
        for name, meta in BOARD_GOLD_CACHE_SCRIPTS.items():
            scripts.append({
                "name": name,
                "label": meta["label"],
                "description": meta["description"],
                "default_args": list(meta.get("default_args") or []),
                "live_data": bool(meta.get("live_data")),
                "internal": bool(meta.get("internal")),
                "available": bool(meta.get("internal")) or (scripts_dir / name).exists(),
            })
        return {
            "scripts_dir": str(scripts_dir),
            "available": scripts_dir.exists() or any(item.get("available") for item in scripts),
            "scripts": scripts,
        }

    def start_scan(self, params: Dict[str, Any]) -> ScanTask:
        task = ScanTask(
            id=uuid4().hex,
            status="pending",
            created_at=datetime.now(timezone.utc).isoformat(),
            params=dict(params),
        )
        with self.lock:
            self.tasks[task.id] = task
        self.executor.submit(self._run_task, task.id)
        return task

    def get_task(self, task_id: str) -> Optional[ScanTask]:
        with self.lock:
            return self.tasks.get(task_id)

    def start_cache_update(self, script: str = "auto_full", args: Optional[List[str]] = None) -> CacheUpdateTask:
        script_path, script_name, scripts_dir = self._resolve_cache_script(script)
        safe_args = self._sanitize_cache_args(args)
        if args is None:
            safe_args = list(BOARD_GOLD_CACHE_SCRIPTS[script_name].get("default_args") or [])
        is_internal = bool(BOARD_GOLD_CACHE_SCRIPTS[script_name].get("internal"))

        with self.lock:
            running = next(
                (
                    task for task in self.cache_tasks.values()
                    if task.status in {"pending", "running"}
                ),
                None,
            )
            if running:
                raise ValueError(f"已有缓存更新任务正在运行: {running.id}")

        cwd = self._cache_project_dir(scripts_dir)
        command = ["internal", script_name, *safe_args] if is_internal else [sys.executable, str(script_path), *safe_args]
        task = CacheUpdateTask(
            id=uuid4().hex,
            status="pending",
            script=script_name,
            args=safe_args,
            created_at=datetime.now(timezone.utc).isoformat(),
            scripts_dir=str(scripts_dir),
            cwd=str(cwd),
            command=command,
        )
        with self.lock:
            self.cache_tasks[task.id] = task
        self.cache_executor.submit(self._run_cache_update_task, task.id)
        return task

    def get_cache_update_task(self, task_id: str) -> Optional[CacheUpdateTask]:
        with self.lock:
            return self.cache_tasks.get(task_id)

    def _run_task(self, task_id: str) -> None:
        with self.lock:
            task = self.tasks[task_id]
            task.status = "running"
            task.started_at = datetime.now(timezone.utc).isoformat()

        def progress(current: int, total: int, signals: List[Dict[str, Any]], message: str) -> None:
            with self.lock:
                current_task = self.tasks.get(task_id)
                if not current_task:
                    return
                current_task.current = current
                current_task.total = total
                current_task.signals = signals
                current_task.logs.append(f"{datetime.now().strftime('%H:%M:%S')} {message}")
                current_task.logs = current_task.logs[-120:]

        try:
            result = self.scanner.scan_entries(
                strategy_names=task.params.get("strategies"),
                symbols=task.params.get("symbols"),
                days=int(task.params.get("days") or 80),
                target_date=task.params.get("target_date"),
                max_stocks=task.params.get("max_stocks"),
                progress=progress,
            )
            with self.lock:
                task.status = "completed"
                task.finished_at = datetime.now(timezone.utc).isoformat()
                task.current = result["total_stocks"]
                task.total = result["total_stocks"]
                task.signals = result["signals"]
                task.logs.append(f"{datetime.now().strftime('%H:%M:%S')} 扫描完成，信号 {result['signals_count']} 个")
        except Exception as exc:
            with self.lock:
                task.status = "failed"
                task.finished_at = datetime.now(timezone.utc).isoformat()
                task.error = str(exc)
                task.logs.append(f"{datetime.now().strftime('%H:%M:%S')} 扫描失败：{exc}")

    def _run_cache_update_task(self, task_id: str) -> None:
        with self.lock:
            task = self.cache_tasks[task_id]
            task.status = "running"
            task.started_at = datetime.now(timezone.utc).isoformat()
            command = list(task.command)
            cwd = task.cwd

        self._append_cache_log(task_id, f"启动 {task.script} {' '.join(task.args)}".strip())
        if command[:2] == ["internal", "auto_full"]:
            self._run_auto_cache_update_task(task_id, task.args)
            return
        if command[:2] == ["internal", "baostock_daily"]:
            self._run_baostock_daily_cache_update(task_id, task.args)
            return
        try:
            exit_code = self._run_cache_subprocess(task_id, command, cwd, task.scripts_dir)
            with self.lock:
                task = self.cache_tasks[task_id]
                task.exit_code = exit_code
                task.finished_at = datetime.now(timezone.utc).isoformat()
                task.status = "completed" if exit_code == 0 else "failed"
                if exit_code != 0:
                    task.error = f"脚本退出码 {exit_code}"
            self._append_cache_log(task_id, "缓存更新完成" if exit_code == 0 else f"缓存更新失败，退出码 {exit_code}")
        except Exception as exc:
            with self.lock:
                task = self.cache_tasks[task_id]
                task.status = "failed"
                task.finished_at = datetime.now(timezone.utc).isoformat()
                task.error = str(exc)
            self._append_cache_log(task_id, f"缓存更新异常：{exc}")

    def _append_cache_log(self, task_id: str, message: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')} {message}"
        with self.lock:
            task = self.cache_tasks.get(task_id)
            if not task:
                return
            task.logs.append(line)
            task.logs = task.logs[-240:]

    def _run_cache_subprocess(self, task_id: str, command: List[str], cwd: str, scripts_dir: str) -> int:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["BOARD_HAS_GOLD_DATA_DIR"] = str(self.scanner.data_dir)
        env["TA_BOARD_GOLD_DATA_DIR"] = str(self.scanner.data_dir)
        python_path = os.pathsep.join([scripts_dir, cwd, env.get("PYTHONPATH", "")]).strip(os.pathsep)
        if python_path:
            env["PYTHONPATH"] = python_path

        process: Optional[subprocess.Popen[str]] = None
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            if process.stdout:
                for line in process.stdout:
                    text = line.rstrip()
                    if text:
                        self._append_cache_log(task_id, text)
            return process.wait()
        except Exception:
            if process and process.poll() is None:
                process.kill()
            raise

    def _run_auto_cache_update_task(self, task_id: str, args: List[str]) -> None:
        failed_stages: List[str] = []
        try:
            options = self._parse_auto_cache_args(args)
            with self.lock:
                task = self.cache_tasks[task_id]
                scripts_dir = Path(task.scripts_dir)
                cwd = task.cwd

            self._append_cache_log(task_id, "自动更新开始：基础信息 -> 日线缓存 -> 因子 -> 质量检查")
            self._append_cache_log(task_id, f"本地数据目录：{self.scanner.data_dir}")

            basic_script = scripts_dir / "collect_stock_basic.py"
            if basic_script.exists():
                self._append_cache_log(task_id, "阶段 1/5 刷新股票基础信息")
                exit_code = self._run_cache_subprocess(
                    task_id,
                    [sys.executable, str(basic_script), "--extend"],
                    cwd,
                    str(scripts_dir),
                )
                if exit_code != 0:
                    self._append_cache_log(task_id, f"基础信息刷新失败，退出码 {exit_code}，继续尝试日线缓存")
            else:
                self._append_cache_log(task_id, "阶段 1/5 未找到基础信息脚本，沿用现有 stock_basic 缓存")

            daily_script = scripts_dir / "daily_update.py"
            if daily_script.exists() and not options["symbols"]:
                self._append_cache_log(task_id, "阶段 2/5 运行旧采集器增量修复，不限制股票数量")
                exit_code = self._run_cache_subprocess(
                    task_id,
                    [
                        sys.executable,
                        str(daily_script),
                        "--threads",
                        "1",
                        "--queue-limit",
                        "0",
                        "--low-refresh-per-run",
                        "0",
                    ],
                    cwd,
                    str(scripts_dir),
                )
                if exit_code != 0:
                    self._append_cache_log(task_id, f"旧采集器失败，退出码 {exit_code}，切换 BaoStock 继续补齐")
            elif daily_script.exists():
                self._append_cache_log(task_id, "阶段 2/5 指定了股票范围，跳过旧采集器批量队列")
            else:
                self._append_cache_log(task_id, "阶段 2/5 未找到旧采集器，直接使用 BaoStock")

            self._append_cache_log(task_id, "阶段 3/5 BaoStock 增量补齐前复权和未复权日线")
            baostock_args = ["--all", "--sleep", str(options["sleep"]), "--days", str(options["days"])]
            if options["start_date"]:
                baostock_args.extend(["--start-date", options["start_date"]])
            if options["end_date"]:
                baostock_args.extend(["--end-date", options["end_date"]])
            if options["max_consecutive_failures"]:
                baostock_args.extend(["--max-consecutive-failures", str(options["max_consecutive_failures"])])
            for symbol in options["symbols"] or []:
                baostock_args.extend(["--symbol", symbol])
            baostock_result = self._update_baostock_daily(task_id, baostock_args)
            if baostock_result["stopped"]:
                failed_stages.append("BaoStock 连续失败达到阈值")

            check = self._cross_check_daily_cache(task_id)
            if not check["ok"]:
                failed_stages.append(str(check["message"]))

            factor_script = scripts_dir / "calc_factors.py"
            if factor_script.exists():
                self._append_cache_log(task_id, "阶段 4/5 计算技术因子")
                exit_code = self._run_cache_subprocess(
                    task_id,
                    [sys.executable, str(factor_script), "--range", "1y"],
                    cwd,
                    str(scripts_dir),
                )
                if exit_code != 0:
                    failed_stages.append(f"技术因子脚本退出码 {exit_code}")
            else:
                self._append_cache_log(task_id, "阶段 4/5 未找到技术因子脚本，跳过")

            adj_script = scripts_dir / "collect_adj_factor.py"
            if adj_script.exists():
                self._append_cache_log(task_id, "阶段 4.5/5 计算复权因子")
                exit_code = self._run_cache_subprocess(
                    task_id,
                    [sys.executable, str(adj_script)],
                    cwd,
                    str(scripts_dir),
                )
                if exit_code != 0:
                    failed_stages.append(f"复权因子脚本退出码 {exit_code}")

            quality_script = scripts_dir / "check_data_quality.py"
            if quality_script.exists():
                self._append_cache_log(task_id, "阶段 5/5 数据质量检查")
                exit_code = self._run_cache_subprocess(
                    task_id,
                    [sys.executable, str(quality_script)],
                    cwd,
                    str(scripts_dir),
                )
                if exit_code != 0:
                    failed_stages.append(f"数据质量脚本退出码 {exit_code}")
            else:
                self._append_cache_log(task_id, "阶段 5/5 未找到质量检查脚本，跳过")

            with self.lock:
                task = self.cache_tasks[task_id]
                task.finished_at = datetime.now(timezone.utc).isoformat()
                task.exit_code = 1 if failed_stages else 0
                task.status = "failed" if failed_stages else "completed"
                task.error = "；".join(failed_stages) if failed_stages else None
            if failed_stages:
                self._append_cache_log(task_id, f"自动更新结束：存在异常 {'；'.join(failed_stages)}")
            else:
                self._append_cache_log(task_id, "自动更新完成")
        except Exception as exc:
            with self.lock:
                task = self.cache_tasks[task_id]
                task.status = "failed"
                task.finished_at = datetime.now(timezone.utc).isoformat()
                task.exit_code = 1
                task.error = str(exc)
            self._append_cache_log(task_id, f"自动更新异常：{exc}")

    def _run_baostock_daily_cache_update(self, task_id: str, args: List[str]) -> None:
        try:
            result = self._update_baostock_daily(task_id, args)
            with self.lock:
                task = self.cache_tasks[task_id]
                failed = int(result["failed"])
                task.exit_code = 0 if failed == 0 and not result["stopped"] else 1
                task.status = "completed" if task.exit_code == 0 else "failed"
                task.finished_at = datetime.now(timezone.utc).isoformat()
                if task.exit_code != 0:
                    task.error = f"BaoStock 更新失败: {failed}"
        except Exception as exc:
            with self.lock:
                task = self.cache_tasks[task_id]
                task.status = "failed"
                task.finished_at = datetime.now(timezone.utc).isoformat()
                task.exit_code = 1
                task.error = str(exc)
            self._append_cache_log(task_id, f"BaoStock 更新异常：{exc}")

    def _update_baostock_daily(self, task_id: str, args: List[str]) -> Dict[str, Any]:
        options = self._parse_baostock_args(args)
        symbols = options["symbols"]
        items = self.scanner._select_stock_items(symbols) if symbols else self.scanner.load_stock_list()
        items = self._sort_cache_items_by_staleness(items)
        if options["max_stocks"]:
            items = items[:options["max_stocks"]]
        if not items:
            raise ValueError("BaoStock 更新股票池为空，请检查本地 stock_basic 或传入 --symbol")

        qfq_dir = self.scanner._stock_daily_dir()
        raw_dir = self.scanner._stock_daily_raw_dir()
        total = len(items)
        updated = 0
        empty = 0
        failed = 0
        consecutive_failed = 0
        stopped = False
        end_date = options["end_date"] or datetime.now().strftime("%Y-%m-%d")
        failure_limit = options["max_consecutive_failures"]

        with self._baostock_session() as bs:
            for index, item in enumerate(items, start=1):
                symbol = normalize_bare_symbol(item["symbol"])
                if not symbol:
                    continue
                start_date = options["start_date"] or self._baostock_incremental_start(symbol, end_date, options["days"], options["force"])
                if start_date > end_date:
                    empty += 1
                    if empty <= 20 or empty % 500 == 0:
                        self._append_cache_log(task_id, f"{normalize_display_symbol(symbol)} 本地缓存已覆盖到 {end_date}，跳过")
                    continue
                self._append_cache_log(task_id, f"BaoStock {index}/{total} {normalize_display_symbol(symbol)} {start_date}~{end_date}")
                try:
                    qfq_df = self._fetch_baostock_daily(bs, symbol, start_date, end_date, adjustflag="2")
                    raw_df = pd.DataFrame() if options["no_raw"] else self._fetch_baostock_daily(bs, symbol, start_date, end_date, adjustflag="3")
                    if qfq_df.empty and raw_df.empty:
                        empty += 1
                        consecutive_failed = 0
                        self._append_cache_log(task_id, f"{normalize_display_symbol(symbol)} 无新增数据")
                    else:
                        if not qfq_df.empty:
                            self._merge_daily_cache(symbol, qfq_df, qfq_dir)
                        if not raw_df.empty:
                            self._merge_daily_cache(symbol, raw_df, raw_dir)
                        updated += 1
                        consecutive_failed = 0
                        self._append_cache_log(task_id, f"{normalize_display_symbol(symbol)} 写入完成 qfq={len(qfq_df)} raw={len(raw_df)}")
                except Exception as exc:
                    failed += 1
                    consecutive_failed += 1
                    self._append_cache_log(task_id, f"{normalize_display_symbol(symbol)} 更新失败：{exc}")
                    if failure_limit and consecutive_failed >= failure_limit:
                        stopped = True
                        self._append_cache_log(task_id, f"连续失败 {consecutive_failed} 次，停止 BaoStock 循环，请检查网络、代理或接口状态")
                        break

                if options["sleep"] > 0 and index < total:
                    time.sleep(options["sleep"])

        self._append_cache_log(task_id, f"BaoStock 更新结束：股票={total} 写入={updated} 已覆盖/空数据={empty} 失败={failed}")
        return {"total": total, "updated": updated, "empty": empty, "failed": failed, "stopped": stopped}

    def _parse_baostock_args(self, args: List[str]) -> Dict[str, Any]:
        parser = argparse.ArgumentParser(prog="baostock_daily", add_help=False)
        parser.add_argument("--symbol", action="append", default=[])
        parser.add_argument("--symbols", default="")
        parser.add_argument("--max-stocks", type=int, default=80)
        parser.add_argument("--all", action="store_true")
        parser.add_argument("--days", type=int, default=370)
        parser.add_argument("--start-date")
        parser.add_argument("--end-date")
        parser.add_argument("--sleep", type=float, default=1.0)
        parser.add_argument("--max-consecutive-failures", type=int, default=0)
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--no-raw", action="store_true")
        try:
            ns = parser.parse_args(args)
        except SystemExit as exc:
            raise ValueError("BaoStock 缓存更新参数非法") from exc

        symbols = list(ns.symbol or [])
        if ns.symbols:
            symbols.extend(part.strip() for part in ns.symbols.replace("，", ",").split(","))
        symbols = [normalize_bare_symbol(symbol) for symbol in symbols if normalize_bare_symbol(symbol)]
        max_stocks = None if ns.all else max(1, min(int(ns.max_stocks or 80), 6000))
        days = max(1, min(int(ns.days or 370), 5000))
        sleep = max(0.0, min(float(ns.sleep or 0), 60.0))
        max_consecutive_failures = int(ns.max_consecutive_failures or 0)
        if max_consecutive_failures <= 0:
            max_consecutive_failures = int(os.getenv("TA_BOARD_GOLD_CACHE_CONSECUTIVE_FAIL_LIMIT", "12") or "12")
        return {
            "symbols": symbols or None,
            "max_stocks": max_stocks,
            "days": days,
            "start_date": self._normalize_baostock_date(ns.start_date),
            "end_date": self._normalize_baostock_date(ns.end_date),
            "sleep": sleep,
            "max_consecutive_failures": max(1, min(max_consecutive_failures, 200)),
            "force": bool(ns.force),
            "no_raw": bool(ns.no_raw),
        }

    def _parse_auto_cache_args(self, args: List[str]) -> Dict[str, Any]:
        parser = argparse.ArgumentParser(prog="auto_full", add_help=False)
        parser.add_argument("--symbol", action="append", default=[])
        parser.add_argument("--symbols", default="")
        parser.add_argument("--days", type=int, default=370)
        parser.add_argument("--start-date")
        parser.add_argument("--end-date")
        parser.add_argument("--sleep", type=float, default=1.0)
        parser.add_argument("--max-consecutive-failures", type=int, default=0)
        try:
            ns = parser.parse_args(args)
        except SystemExit as exc:
            raise ValueError("自动缓存更新参数非法") from exc

        symbols = list(ns.symbol or [])
        if ns.symbols:
            symbols.extend(part.strip() for part in ns.symbols.replace("，", ",").split(","))
        symbols = [normalize_bare_symbol(symbol) for symbol in symbols if normalize_bare_symbol(symbol)]
        max_consecutive_failures = int(ns.max_consecutive_failures or 0)
        if max_consecutive_failures <= 0:
            max_consecutive_failures = int(os.getenv("TA_BOARD_GOLD_CACHE_CONSECUTIVE_FAIL_LIMIT", "12") or "12")
        return {
            "symbols": symbols or None,
            "days": max(1, min(int(ns.days or 370), 5000)),
            "start_date": self._normalize_baostock_date(ns.start_date),
            "end_date": self._normalize_baostock_date(ns.end_date),
            "sleep": max(0.0, min(float(ns.sleep or 0), 60.0)),
            "max_consecutive_failures": max(1, min(max_consecutive_failures, 200)),
        }

    def _sort_cache_items_by_staleness(self, items: List[Dict[str, str]]) -> List[Dict[str, str]]:
        def key(item: Dict[str, str]) -> tuple[str, str]:
            symbol = normalize_bare_symbol(item.get("symbol", ""))
            latest = self._cached_daily_max_date(self.scanner._stock_daily_dir() / f"{symbol}.parquet")
            return latest or "", symbol

        return sorted(items, key=key)

    def _cross_check_daily_cache(self, task_id: str) -> Dict[str, Any]:
        qfq_dir = self.scanner._stock_daily_dir()
        raw_dir = self.scanner._stock_daily_raw_dir()
        qfq_files = {path.stem: path for path in qfq_dir.glob("*.parquet")} if qfq_dir.exists() else {}
        raw_files = {path.stem: path for path in raw_dir.glob("*.parquet")} if raw_dir.exists() else {}
        if not qfq_files:
            message = "交叉检查失败：stock_daily 为空"
            self._append_cache_log(task_id, message)
            return {"ok": False, "message": message}

        common = sorted(set(qfq_files) & set(raw_files))
        mismatch = 0
        checked = 0
        qfq_latest: Optional[str] = None
        raw_latest: Optional[str] = None
        for symbol in common:
            qfq_date = self._cached_daily_max_date(qfq_files[symbol])
            raw_date = self._cached_daily_max_date(raw_files[symbol])
            if qfq_date:
                qfq_latest = max(qfq_latest or qfq_date, qfq_date)
            if raw_date:
                raw_latest = max(raw_latest or raw_date, raw_date)
            if qfq_date and raw_date:
                checked += 1
                if qfq_date != raw_date:
                    mismatch += 1

        self._append_cache_log(
            task_id,
            f"落盘交叉检查：前复权={len(qfq_files)} 未复权={len(raw_files)} 共同={len(common)} 日期一致={checked - mismatch}/{checked} 最新={qfq_latest or '--'}",
        )
        if raw_files and mismatch > max(20, checked // 10):
            message = f"交叉检查发现日期不一致过多：{mismatch}/{checked}"
            self._append_cache_log(task_id, message)
            return {"ok": False, "message": message}
        return {"ok": True, "message": "ok", "qfq_latest": qfq_latest, "raw_latest": raw_latest}

    def _normalize_baostock_date(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        text = str(value).strip()
        if len(text) == 8 and text.isdigit():
            return f"{text[:4]}-{text[4:6]}-{text[6:]}"
        return text[:10]

    def _baostock_incremental_start(self, symbol: str, end_date: str, days: int, force: bool) -> str:
        fallback = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
        if force:
            return fallback
        latest = self._cached_daily_max_date(self.scanner._stock_daily_dir() / f"{symbol}.parquet")
        if not latest:
            return fallback
        try:
            next_day = datetime.strptime(latest, "%Y-%m-%d") + timedelta(days=1)
            return next_day.strftime("%Y-%m-%d")
        except Exception:
            return fallback

    def _cached_daily_max_date(self, path: Path) -> Optional[str]:
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path, columns=["日期"])
            if df.empty:
                return None
            return pd.to_datetime(df["日期"]).max().strftime("%Y-%m-%d")
        except Exception:
            return None

    @contextmanager
    def _baostock_session(self):
        try:
            import baostock as bs  # type: ignore
        except ImportError as exc:
            raise ValueError("请先安装 baostock 依赖") from exc
        with redirect_stdout(io.StringIO()):
            login = bs.login()
        if getattr(login, "error_code", "1") != "0":
            raise ValueError(f"BaoStock 登录失败: {getattr(login, 'error_msg', '')}")
        try:
            yield bs
        finally:
            with redirect_stdout(io.StringIO()):
                bs.logout()

    def _fetch_baostock_daily(self, bs: Any, symbol: str, start_date: str, end_date: str, adjustflag: str) -> pd.DataFrame:
        code = f"sh.{symbol}" if symbol.startswith(("5", "6", "9")) else f"sz.{symbol}"
        fields = "date,open,high,low,close,volume,amount,turn,pctChg"
        rs = bs.query_history_k_data_plus(
            code,
            fields,
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag=adjustflag,
        )
        if getattr(rs, "error_code", "0") != "0":
            raise ValueError(f"BaoStock 查询失败: {rs.error_code} {rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=rs.fields)
        df = df.rename(columns={
            "date": "日期",
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "volume": "成交量",
            "amount": "成交额",
            "turn": "换手率",
            "pctChg": "涨跌幅",
        })
        df.insert(0, "symbol", symbol)
        for column in ["开盘", "最高", "最低", "收盘", "成交量", "成交额", "换手率", "涨跌幅"]:
            if column in df.columns:
                df[column] = pd.to_numeric(df[column], errors="coerce")
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
        df = df.dropna(subset=["日期", "开盘", "最高", "最低", "收盘"])
        return df.sort_values("日期").reset_index(drop=True)

    def _merge_daily_cache(self, symbol: str, df: pd.DataFrame, target_dir: Path) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{symbol}.parquet"
        if path.exists():
            old_df = pd.read_parquet(path)
            df = pd.concat([old_df, df], ignore_index=True)
        df = df.drop_duplicates(subset=["日期"], keep="last")
        df = df.sort_values("日期").reset_index(drop=True)
        tmp_path = path.with_name(f".{path.stem}.{os.getpid()}.tmp{path.suffix}")
        df.to_parquet(tmp_path, index=False)
        tmp_path.replace(path)

    def _cache_scripts_dir(self) -> Path:
        raw = os.getenv("TA_BOARD_GOLD_CACHE_SCRIPTS_DIR") or os.getenv("BOARD_HAS_GOLD_CACHE_SCRIPTS_DIR")
        if raw:
            return Path(raw).expanduser()

        sibling = self.scanner.data_dir.expanduser().parent / "cache" / "scripts"
        if sibling.exists():
            return sibling
        return Path("cache/board_gold/scripts")

    def _resolve_cache_script(self, script: str) -> tuple[Optional[Path], str, Path]:
        script_name = Path(str(script or "")).name
        if not script_name or script_name != str(script) or script_name not in BOARD_GOLD_CACHE_SCRIPTS:
            raise ValueError("缓存更新脚本不在白名单内")
        scripts_dir = self._cache_scripts_dir()
        if BOARD_GOLD_CACHE_SCRIPTS[script_name].get("internal"):
            return None, script_name, scripts_dir
        script_path = scripts_dir / script_name
        if not script_path.exists():
            raise ValueError(f"缓存更新脚本不存在: {script_path}")
        return script_path, script_name, scripts_dir

    def _cache_project_dir(self, scripts_dir: Path) -> Path:
        if scripts_dir.name == "scripts" and scripts_dir.parent.name == "cache":
            return scripts_dir.parent.parent
        return scripts_dir

    def _sanitize_cache_args(self, args: Optional[List[str]]) -> List[str]:
        if args is None:
            return []
        if len(args) > 24:
            raise ValueError("缓存更新参数过多")
        safe_args: List[str] = []
        for raw in args:
            value = str(raw)
            if "\x00" in value or len(value) > 200:
                raise ValueError("缓存更新参数非法")
            safe_args.append(value)
        return safe_args


board_gold_service = BoardGoldTaskManager()
