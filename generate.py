"""GitHub Actions用: 市場データを生成して docs/data/ に出力する。

失敗時(Yahoo側の制限等)は非ゼロ終了してコミットさせず、前回データを維持する。
"""
import sys
from pathlib import Path

from backend import market, pulse

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "data"


def main() -> int:
    snap = market.build_snapshot(watchlist_path=ROOT / "watchlist.json",
                                 out_path=OUT / "market.json")
    n = len(snap.get("assets", []))
    if n < 50:
        print(f"取得銘柄が{n}件と少なすぎる(Yahoo制限の可能性)。コミットを中止", file=sys.stderr)
        return 1
    try:
        pulse.build_pulse(out_path=OUT / "pulse.json")
    except Exception as e:  # パルスが落ちても株価データは配信する
        print("pulse生成失敗(前回データを維持):", e, file=sys.stderr)
    print(f"OK: {n}銘柄 / 指数{len(snap.get('indices', []))}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
