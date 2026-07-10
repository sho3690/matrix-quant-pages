"""ペーパートレード(仮想売買)。実際のお金・取引所には一切接続しない。

記録は data/paper_trades.json に追記保存する:
{"initial_capital": 1000000, "trades": [{ts, symbol, side, qty, price_jpy, jpy}]}
ポジションは平均取得単価方式で計算する。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from backend.util import atomic_write_json, load_json_safe

# 損切りライン = 価格 − STOP_K × ATR (スイングトレードの一般的なパラメータ)
STOP_K = 2.5


def validate_stock_qty(asset_class: str, qty) -> int:
    """株式の数量を検証して整数株数を返す(1株単位)。

    日本株の取引所ルールは100株単元だが、利用者は証券会社の
    単元未満株サービス(S株・ミニ株等)での1株単位取引が基本のため、
    シミュレーションも1株単位に合わせる。暗号資産は対象外(端数OK)。
    """
    if qty is None or qty <= 0:
        raise ValueError("株数を指定してください")
    if abs(qty - round(qty)) > 1e-9:
        raise ValueError("株数は整数で指定してください")
    return int(round(qty))


class PaperBroker:
    def __init__(self, path, initial_capital: int = 1_000_000):
        self.path = Path(path)
        self.initial_capital = initial_capital
        self._load()

    def _load(self):
        # 壊れたファイルは.bakに退避して空の状態から再開(起動不能を防ぐ)
        data = load_json_safe(self.path, {})
        self.initial_capital = data.get("initial_capital", self.initial_capital)
        self.trades = data.get("trades", [])

    def _save(self):
        atomic_write_json(self.path, {"initial_capital": self.initial_capital,
                                      "trades": self.trades}, indent=2)

    def _cash_and_positions(self):
        cash = float(self.initial_capital)
        pos = {}  # symbol -> {"qty": float, "cost_jpy": float}
        for t in self.trades:
            if t["side"] == "buy":
                cash -= t["jpy"]
                p = pos.setdefault(t["symbol"], {"qty": 0.0, "cost_jpy": 0.0,
                                                 "stop_jpy": None})
                p["qty"] += t["qty"]
                p["cost_jpy"] += t["jpy"]
                if t.get("stop_jpy"):
                    p["stop_jpy"] = t["stop_jpy"]  # 最後の買いの損切りラインを採用
            else:
                cash += t["jpy"]
                p = pos[t["symbol"]]
                # 平均取得単価で原価を減らす
                avg = p["cost_jpy"] / p["qty"] if p["qty"] else 0.0
                p["cost_jpy"] -= avg * t["qty"]
                p["qty"] -= t["qty"]
                if p["qty"] <= 1e-12:
                    del pos[t["symbol"]]
        return cash, pos

    def buy(self, symbol: str, jpy_amount: float = None, price_jpy: float = None,
            qty: float = None, stop_jpy=None):
        """金額指定(暗号資産向け)か数量指定(株式向け)のどちらかで買う。"""
        if price_jpy is None or price_jpy <= 0:
            raise ValueError("価格データがないため売買できません")
        if qty is not None:
            if qty <= 0:
                raise ValueError("数量は1以上を指定してください")
            jpy_amount = qty * price_jpy
        if jpy_amount is None or jpy_amount <= 0:
            raise ValueError("金額または数量を指定してください")
        cash, _ = self._cash_and_positions()
        if jpy_amount > cash + 1e-9:
            raise ValueError(f"仮想資金が足りません(残高 ¥{cash:,.0f}、必要 ¥{jpy_amount:,.0f})")
        if qty is None:
            qty = jpy_amount / price_jpy
        trade = {"ts": datetime.now(timezone.utc).isoformat(), "symbol": symbol,
                 "side": "buy", "qty": qty, "price_jpy": price_jpy, "jpy": jpy_amount,
                 "stop_jpy": stop_jpy}  # 買った瞬間に決めた損切りライン(規律の記録)
        self.trades.append(trade)
        self._save()
        return trade

    def sell(self, symbol: str, qty: float, price_jpy: float):
        if price_jpy is None or price_jpy <= 0:
            raise ValueError("価格データがないため売買できません")
        if qty <= 0:
            raise ValueError("数量は0より大きい値を指定してください")
        _, pos = self._cash_and_positions()
        held = pos.get(symbol, {}).get("qty", 0.0)
        if qty > held + 1e-9:
            raise ValueError(f"保有数量({held:.6g})を超える売却はできません")
        trade = {"ts": datetime.now(timezone.utc).isoformat(), "symbol": symbol,
                 "side": "sell", "qty": qty, "price_jpy": price_jpy,
                 "jpy": qty * price_jpy}
        self.trades.append(trade)
        self._save()
        return trade

    def portfolio(self, prices_jpy: dict, atr_pct: dict = None) -> dict:
        cash, pos = self._cash_and_positions()
        positions = []
        total_value = 0.0
        for symbol, p in pos.items():
            price = prices_jpy.get(symbol)
            value = p["qty"] * price if price else None
            avg_cost = p["cost_jpy"] / p["qty"] if p["qty"] else 0.0
            pnl = (value - p["cost_jpy"]) if value is not None else None
            stop = p.get("stop_jpy")
            estimated = False
            if stop is None and atr_pct and atr_pct.get(symbol) and avg_cost:
                # 記録なし(旧仕様の買い)は平均取得単価−STOP_K×ATRで推定ラインを補完
                stop = avg_cost * (1 - STOP_K * atr_pct[symbol])
                estimated = True
            positions.append({
                "symbol": symbol,
                "qty": p["qty"],
                "avg_cost_jpy": avg_cost,
                "value_jpy": value,
                "pnl_jpy": pnl,
                "pnl_pct": (pnl / p["cost_jpy"]) if pnl is not None and p["cost_jpy"] else None,
                "stop_jpy": stop,
                "stop_estimated": estimated,
                "stop_breached": (price is not None and stop is not None
                                  and price < stop),
            })
            total_value += value or 0.0
        equity = cash + total_value
        return {"cash": cash, "equity": equity,
                "total_pnl": equity - self.initial_capital,
                "initial_capital": self.initial_capital,
                "positions": positions,
                "trades": list(reversed(self.trades[-50:]))}
