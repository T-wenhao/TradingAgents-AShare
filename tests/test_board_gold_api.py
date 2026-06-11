from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient


def _get_client():
    from api.main import app

    return TestClient(app, raise_server_exceptions=False)


def _auth_unique(client: TestClient) -> str:
    from api.database import UserDB, get_db_ctx, init_db
    from api.services import auth_service

    init_db()
    email = auth_service.normalize_email(f"board-gold-{uuid4().hex[:8]}@test.com")
    now = datetime.now(timezone.utc)
    with get_db_ctx() as db:
        user = UserDB(
            id=str(uuid4()),
            email=email,
            is_active=True,
            created_at=now,
            updated_at=now,
            last_login_at=now,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return auth_service.create_access_token(user)


class _FakeTask:
    def __init__(self):
        self.task_id = "task-1"

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "status": "running",
            "created_at": "2026-01-01T00:00:00+00:00",
            "started_at": "2026-01-01T00:00:01+00:00",
            "finished_at": None,
            "current": 1,
            "total": 2,
            "signals_count": 1,
            "signals": [
                {
                    "strategy": "three_yin",
                    "symbol": "600001.SH",
                    "name": "测试股票",
                    "signal_date": "2026-01-02",
                    "base_date": "2026-01-01",
                    "price": 10.5,
                    "change_pct": 4.2,
                    "volume": 1000,
                }
            ],
            "error": None,
            "params": {"strategies": ["three_yin"]},
            "logs": ["开始扫描"],
        }


class _FakeCacheTask:
    def __init__(self):
        self.task_id = "cache-task-1"

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "status": "running",
            "script": "auto_full",
            "args": [],
            "created_at": "2026-01-01T00:00:00+00:00",
            "started_at": "2026-01-01T00:00:01+00:00",
            "finished_at": None,
            "exit_code": None,
            "error": None,
            "scripts_dir": "/tmp/board-gold/cache/scripts",
            "cwd": "/tmp/board-gold",
            "command": ["internal", "auto_full"],
            "logs": ["启动 auto_full"],
        }


class _FakeBacktestTask:
    def __init__(self):
        self.task_id = "backtest-task-1"

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
            "started_at": None,
            "finished_at": None,
            "current": 0,
            "total": 0,
            "stats": None,
            "exit_signals_count": 0,
            "exit_signals": [],
            "error": None,
            "params": {"exit_strategy": "fixed_exit"},
            "logs": [],
        }


class _FakeScanner:
    def strategy_info(self):
        return {
            "entry_strategies": [
                {"name": "three_yin", "description": "三阴不破阳", "enabled": True, "params": {}}
            ],
            "exit_strategies": [
                {"name": "fixed_exit", "description": "固定止盈止损", "enabled": True, "params": {}}
            ],
        }

    def cache_stats(self):
        return {
            "data_dir": "/tmp/board-gold",
            "available": True,
            "stock_basic_file": "/tmp/board-gold/stock_basic/stock_basic.parquet",
            "stock_basic_count": 1,
            "daily_count": 1,
            "raw_count": 1,
            "latest_file_mtime": "2026-01-01T00:00:00",
        }

    def latest_result(self):
        return {
            "scan_time": "2026-01-01T00:00:00+00:00",
            "strategies": ["three_yin"],
            "target_date": None,
            "days": 80,
            "total_stocks": 1,
            "signals_count": 0,
            "signals": [],
            "summary": {},
        }

    def scan_exits(self, entries, exit_strategy_name, days, custom_params=None):
        return {
            "scan_time": "2026-01-01T00:00:00+00:00",
            "exit_strategy": exit_strategy_name,
            "entries_count": len(entries),
            "exit_signals_count": 0,
            "exit_signals": [],
        }


class _FakeBoardGoldService:
    def __init__(self):
        self.scanner = _FakeScanner()
        self.task = _FakeTask()
        self.cache_task = _FakeCacheTask()
        self.started_params = None
        self.started_cache_params = None

    def cache_stats(self):
        stats = self.scanner.cache_stats()
        stats["cache_scripts_dir"] = "/tmp/board-gold/cache/scripts"
        stats["cache_scripts_available"] = True
        return stats

    def list_cache_scripts(self):
        return {
            "scripts_dir": "/tmp/board-gold/cache/scripts",
            "available": True,
            "scripts": [
                {
                    "name": "auto_full",
                    "label": "一键更新本地缓存",
                    "description": "自动编排本地缓存更新",
                    "default_args": [],
                    "live_data": True,
                    "internal": True,
                    "available": True,
                },
                {
                    "name": "daily_update.py",
                    "label": "每日增量更新",
                    "description": "回补最近交易日",
                    "default_args": ["--threads", "1"],
                    "live_data": True,
                    "available": True,
                },
                {
                    "name": "baostock_daily",
                    "label": "BaoStock 日线增量",
                    "description": "BaoStock 本地缓存源",
                    "default_args": ["--max-stocks", "80"],
                    "live_data": True,
                    "internal": True,
                    "available": True,
                }
            ],
        }

    def start_scan(self, params):
        self.started_params = params
        return self.task

    def get_task(self, task_id):
        return self.task if task_id == self.task.task_id else None

    def start_cache_update(self, script="auto_full", args=None):
        self.started_cache_params = {"script": script, "args": args}
        return self.cache_task

    def get_cache_update_task(self, task_id):
        return self.cache_task if task_id == self.cache_task.task_id else None

    def get_active_cache_task(self):
        return None

    def start_backtest(self, params):
        return _FakeBacktestTask()

    def get_backtest_task(self, task_id):
        return None

    def list_backtests(self):
        return []


def test_board_gold_endpoints_require_auth():
    client = _get_client()
    response = client.get("/v1/board-gold/strategies")
    assert response.status_code in (401, 403)


def test_board_gold_strategy_cache_and_scan_endpoints(monkeypatch):
    import api.main as main

    fake_service = _FakeBoardGoldService()
    monkeypatch.setattr(main, "board_gold_service", fake_service)

    client = _get_client()
    token = _auth_unique(client)
    headers = {"Authorization": f"Bearer {token}"}

    strategies = client.get("/v1/board-gold/strategies", headers=headers)
    assert strategies.status_code == 200
    assert strategies.json()["entry_strategies"][0]["name"] == "three_yin"

    stats = client.get("/v1/board-gold/cache/stats", headers=headers)
    assert stats.status_code == 200
    assert stats.json()["available"] is True
    assert stats.json()["cache_scripts_available"] is True

    scripts = client.get("/v1/board-gold/cache/scripts", headers=headers)
    assert scripts.status_code == 200
    script_names = [item["name"] for item in scripts.json()["scripts"]]
    assert "auto_full" in script_names
    assert "daily_update.py" in script_names
    assert "baostock_daily" in script_names

    cache_started = client.post(
        "/v1/board-gold/cache/update",
        headers=headers,
        json={},
    )
    assert cache_started.status_code == 200
    assert cache_started.json()["task_id"] == "cache-task-1"
    assert fake_service.started_cache_params["script"] == "auto_full"

    cache_status = client.get("/v1/board-gold/cache/update/cache-task-1", headers=headers)
    assert cache_status.status_code == 200
    assert cache_status.json()["script"] == "auto_full"

    started = client.post(
        "/v1/board-gold/scan",
        headers=headers,
        json={"strategies": ["three_yin"], "symbols": ["600001.SH"], "days": 80},
    )
    assert started.status_code == 200
    assert started.json()["task_id"] == "task-1"
    assert fake_service.started_params["symbols"] == ["600001.SH"]

    status = client.get("/v1/board-gold/scan/task-1", headers=headers)
    assert status.status_code == 200
    assert status.json()["signals_count"] == 1

    latest = client.get("/v1/board-gold/results/latest", headers=headers)
    assert latest.status_code == 200
    assert latest.json()["result"]["strategies"] == ["three_yin"]

    exits = client.post(
        "/v1/board-gold/exit-scan",
        headers=headers,
        json={"exit_strategy": "fixed_exit", "entries": [{"symbol": "600001.SH"}]},
    )
    assert exits.status_code == 200
    assert exits.json()["entries_count"] == 1
