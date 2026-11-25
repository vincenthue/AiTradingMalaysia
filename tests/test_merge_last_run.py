"""last_run.json：两个市场的 workflow 同时更新时，各自只改自己那一段。"""
import json

from scripts.merge_last_run import merge


def _write(p, data):
    p.write_text(json.dumps(data), encoding="utf-8")


def test_merge_keeps_other_market(tmp_path):
    target, source = tmp_path / "last_run.json", tmp_path / "mine.json"
    # main 上已经有 US（另一个 workflow 刚 push 的）
    _write(target, {"project": "AiTradingMalaysia", "markets": {"US": {"status": "success", "run_date": "2026-11-24"}}})
    # 这次的 MY 结果（bot 写出来的，里面的 US 可能是旧的）
    _write(source, {"project": "AiTradingMalaysia", "markets": {
        "MY": {"status": "success", "run_date": "2026-11-25"},
        "US": {"status": "failed", "run_date": "2026-11-20"}}})
    assert merge(target, source, "MY")
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["markets"]["MY"]["run_date"] == "2026-11-25"
    assert data["markets"]["US"] == {"status": "success", "run_date": "2026-11-24"}  # 没被旧的覆盖


def test_merge_upgrades_old_single_market_file(tmp_path):
    target, source = tmp_path / "last_run.json", tmp_path / "mine.json"
    _write(target, {"project": "AiTradingMalaysia", "run_date": "2026-11-24", "status": "success"})
    _write(source, {"markets": {"US": {"status": "skipped"}}})
    assert merge(target, source, "US")
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"project": "AiTradingMalaysia", "markets": {"US": {"status": "skipped"}}}


def test_merge_without_section_changes_nothing(tmp_path):
    target, source = tmp_path / "last_run.json", tmp_path / "mine.json"
    _write(target, {"markets": {"US": {"status": "success"}}})
    before = target.read_text(encoding="utf-8")
    _write(source, {"markets": {}})
    assert not merge(target, source, "MY")
    assert target.read_text(encoding="utf-8") == before
