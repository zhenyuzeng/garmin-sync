"""配置读取的测试（TOKEN_DIR 等环境变量处理）。"""

from pathlib import Path

import garmin_sync


def test_token_dir_default(monkeypatch):
    monkeypatch.delenv("TOKEN_DIR", raising=False)
    assert garmin_sync._token_dir() == Path.home() / ".garminconnect"


def test_token_dir_expands_tilde(monkeypatch):
    # 回归测试：.env.example 示例写的是 ~/.garminconnect/，必须正确展开
    monkeypatch.setenv("TOKEN_DIR", "~/tokentest")
    assert garmin_sync._token_dir() == Path.home() / "tokentest"


def test_token_dir_absolute_path_passthrough(monkeypatch):
    monkeypatch.setenv("TOKEN_DIR", "/tmp/tokens")
    assert garmin_sync._token_dir() == Path("/tmp/tokens")
