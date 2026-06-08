from contextlib import contextmanager
from datetime import date

import pandas as pd

from api.services.board_gold_service import (
    BoardGoldScanner,
    BoardGoldTaskManager,
    FixedExitStrategy,
    ThreeYinStrategy,
    normalize_bare_symbol,
    normalize_display_symbol,
)


def _row(day: int, open_: float, high: float, low: float, close: float, volume: float, change_pct: float = 0):
    return {
        "date": pd.Timestamp(date(2026, 1, day)),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": volume * close,
        "turnover": 2.0,
        "change_pct": change_pct,
    }


def test_symbol_normalization():
    assert normalize_bare_symbol("600519.SH") == "600519"
    assert normalize_bare_symbol("SZ300750") == "300750"
    assert normalize_display_symbol("600519") == "600519.SH"
    assert normalize_display_symbol("300750") == "300750.SZ"


def test_three_yin_strategy_detects_signal():
    df = pd.DataFrame([
        _row(1, 9.8, 10.1, 9.7, 10.0, 800, 0),
        _row(2, 10.1, 11.2, 10.8, 11.0, 1000, 10.0),
        _row(3, 11.1, 11.2, 10.9, 11.0, 900, -0.2),
        _row(4, 11.0, 11.1, 10.9, 10.95, 800, -0.5),
        _row(5, 10.95, 11.0, 10.85, 10.9, 700, -0.4),
        _row(6, 10.9, 11.5, 10.9, 11.4, 1000, 4.5),
        _row(7, 11.4, 11.5, 11.0, 11.1, 600, -2.6),
        _row(8, 11.1, 11.3, 10.9, 11.2, 650, 0.9),
        _row(9, 11.2, 11.4, 11.0, 11.3, 660, 0.8),
        _row(10, 11.3, 11.5, 11.1, 11.4, 670, 0.9),
    ])

    strategy = ThreeYinStrategy({"min_yin_days": 3, "max_yin_days": 3, "volume_decrease": True})
    signals = strategy.scan(df, "600001", "测试股票")

    assert len(signals) == 1
    assert signals[0].strategy == "three_yin"
    assert signals[0].to_dict()["symbol"] == "600001.SH"
    assert signals[0].to_dict()["yin_days"] == 3


def test_fixed_exit_strategy_returns_profit_signal():
    df = pd.DataFrame([
        _row(1, 10.0, 10.2, 9.9, 10.0, 800),
        _row(2, 10.1, 10.8, 10.0, 10.6, 900),
        _row(3, 10.6, 10.9, 10.3, 10.7, 950),
    ])
    strategy = FixedExitStrategy({"profit_target": 5.0, "stop_loss": -3.0, "max_hold_days": 5})

    signal = strategy.check_exit(
        df=df,
        entry_date=date(2026, 1, 1),
        entry_price=10.0,
        strategy="three_yin",
        symbol="600001",
        name="测试股票",
    )

    assert signal is not None
    assert signal.exit_type == "profit"
    assert signal.profit_pct == 6.0


def test_cache_update_task_runs_allowlisted_script(tmp_path):
    scripts_dir = tmp_path / "cache" / "scripts"
    scripts_dir.mkdir(parents=True)
    script_path = scripts_dir / "check_data_quality.py"
    script_path.write_text("print('quality ok')\n", encoding="utf-8")

    scanner = BoardGoldScanner(data_dir=tmp_path / "data", results_dir=tmp_path / "results")
    manager = BoardGoldTaskManager(scanner=scanner)
    task = manager.start_cache_update("check_data_quality.py", [])

    manager.cache_executor.shutdown(wait=True)
    final_task = manager.get_cache_update_task(task.id)
    assert final_task is not None
    manager.executor.shutdown(wait=False)

    assert final_task.status == "completed"
    assert final_task.exit_code == 0
    assert any("quality ok" in line for line in final_task.logs)



def test_baostock_cache_update_writes_qfq_and_raw_parquet(tmp_path, monkeypatch):
    scanner = BoardGoldScanner(data_dir=tmp_path / "data", results_dir=tmp_path / "results")
    manager = BoardGoldTaskManager(scanner=scanner)

    @contextmanager
    def fake_session():
        yield object()

    def fake_fetch(_bs, symbol, start_date, end_date, adjustflag):
        assert symbol == "600001"
        assert start_date <= end_date
        close = 11.0 if adjustflag == "2" else 10.0
        return pd.DataFrame([
            {
                "symbol": symbol,
                "日期": "2026-01-02",
                "开盘": close - 0.2,
                "最高": close + 0.2,
                "最低": close - 0.3,
                "收盘": close,
                "成交量": 1000,
                "成交额": 10000,
                "换手率": 1.2,
                "涨跌幅": 2.0,
            }
        ])

    monkeypatch.setattr(manager, "_baostock_session", fake_session)
    monkeypatch.setattr(manager, "_fetch_baostock_daily", fake_fetch)

    task = manager.start_cache_update("baostock_daily", ["--symbol", "600001", "--days", "5", "--sleep", "0"])
    manager.cache_executor.shutdown(wait=True)
    final_task = manager.get_cache_update_task(task.id)
    assert final_task is not None
    manager.executor.shutdown(wait=False)

    assert final_task.status == "completed"
    qfq = pd.read_parquet(tmp_path / "data" / "stock_daily" / "600001.parquet")
    raw = pd.read_parquet(tmp_path / "data" / "stock_daily_raw" / "600001.parquet")
    assert qfq.iloc[0]["收盘"] == 11.0
    assert raw.iloc[0]["收盘"] == 10.0
    assert any("BaoStock 更新结束" in line for line in final_task.logs)


def test_auto_cache_update_covers_full_stock_pool_without_frontend_limit(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    basic_dir = data_dir / "stock_basic"
    basic_dir.mkdir(parents=True)
    pd.DataFrame([
        {"symbol": "600001", "name": "测试一"},
        {"symbol": "600002", "name": "测试二"},
        {"symbol": "600003", "name": "测试三"},
    ]).to_parquet(basic_dir / "stock_basic.parquet", index=False)

    scanner = BoardGoldScanner(data_dir=data_dir, results_dir=tmp_path / "results")
    manager = BoardGoldTaskManager(scanner=scanner)

    @contextmanager
    def fake_session():
        yield object()

    fetched_symbols = []

    def fake_fetch(_bs, symbol, start_date, end_date, adjustflag):
        fetched_symbols.append((symbol, adjustflag))
        close = 12.0 if adjustflag == "2" else 11.0
        return pd.DataFrame([
            {
                "symbol": symbol,
                "日期": "2026-01-02",
                "开盘": close - 0.2,
                "最高": close + 0.2,
                "最低": close - 0.3,
                "收盘": close,
                "成交量": 1000,
                "成交额": 10000,
                "换手率": 1.2,
                "涨跌幅": 2.0,
            }
        ])

    monkeypatch.setattr(manager, "_baostock_session", fake_session)
    monkeypatch.setattr(manager, "_fetch_baostock_daily", fake_fetch)

    task = manager.start_cache_update(args=["--sleep", "0"])
    manager.cache_executor.shutdown(wait=True)
    final_task = manager.get_cache_update_task(task.id)
    assert final_task is not None
    manager.executor.shutdown(wait=False)

    assert final_task.script == "auto_full"
    assert final_task.status == "completed"
    assert {symbol for symbol, _ in fetched_symbols} == {"600001", "600002", "600003"}
    assert (data_dir / "stock_daily" / "600003.parquet").exists()
    assert any("自动更新完成" in line for line in final_task.logs)


def test_auto_cache_update_stops_after_consecutive_baostock_failures(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    basic_dir = data_dir / "stock_basic"
    basic_dir.mkdir(parents=True)
    pd.DataFrame([
        {"symbol": "600001", "name": "测试一"},
        {"symbol": "600002", "name": "测试二"},
        {"symbol": "600003", "name": "测试三"},
    ]).to_parquet(basic_dir / "stock_basic.parquet", index=False)

    scanner = BoardGoldScanner(data_dir=data_dir, results_dir=tmp_path / "results")
    manager = BoardGoldTaskManager(scanner=scanner)

    @contextmanager
    def fake_session():
        yield object()

    calls = []

    def failing_fetch(_bs, symbol, start_date, end_date, adjustflag):
        calls.append(symbol)
        raise RuntimeError("network down")

    monkeypatch.setattr(manager, "_baostock_session", fake_session)
    monkeypatch.setattr(manager, "_fetch_baostock_daily", failing_fetch)

    task = manager.start_cache_update(args=["--sleep", "0", "--max-consecutive-failures", "2"])
    manager.cache_executor.shutdown(wait=True)
    final_task = manager.get_cache_update_task(task.id)
    assert final_task is not None
    manager.executor.shutdown(wait=False)

    assert final_task.status == "failed"
    assert len(calls) == 2
    assert "连续失败" in (final_task.error or "")
    assert any("连续失败 2 次" in line for line in final_task.logs)
