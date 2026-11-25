"""把某个市场的运行结果合并进 logs/last_run.json（只改那个市场的一段，其他市场原样保留）。

GitHub Actions 用：两个市场的 workflow 可能同时 commit 这个文件。每次重试时先拿最新的
main，再用这个脚本把“自己市场那一段”放回去，所以永远不会有 git 合并冲突。

用法：python -m scripts.merge_last_run --market MY --source <bot 写出的 last_run.json> [--target logs/last_run.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bot.config import PROJECT_NAME, ROOT_DIR


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def merge(target: Path, source: Path, market: str) -> bool:
    """把 source 里 market 那一段写进 target。source 没有这个市场 → 不改，返回 False。"""
    section = (_read(source).get("markets") or {}).get(market)
    if not isinstance(section, dict):
        return False
    current = _read(target)
    markets = current.get("markets") if isinstance(current.get("markets"), dict) else {}
    markets[market] = section
    payload = {"project": PROJECT_NAME, "markets": dict(sorted(markets.items()))}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--market", required=True)
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--target", type=Path, default=ROOT_DIR / "logs" / "last_run.json")
    a = p.parse_args(argv)
    changed = merge(a.target, a.source, a.market.strip().upper())
    print("last_run.json 已更新" if changed else f"source 里没有 {a.market} 的结果，不更新")
    return 0


if __name__ == "__main__":
    sys.exit(main())
