"""指数レジーム判定と簡易バックテスト(月末ベース)。

設計(ルックアヘッド防止):
- シグナルは「確定した月末」の値のみで判定する(進行中の月は使わない)
  - シグナル1: 月末終値 > 200日SMA(日次) → 強気
  - シグナル2: 12-1モメンタム(直近1ヶ月を除く過去12ヶ月リターン)が正 → 強気
- 執行はシグナル確定の「翌営業日の始値」。当日終値執行のバイアスを排除
- バックテストは「シグナル1(SMA200)が強気の月だけ保有」をフィルターとし、
  バイ&ホールドと同一期間で比較する
"""
import numpy as np
import pandas as pd


def remove_bad_ticks(s: pd.Series, threshold: float = 0.4) -> pd.Series:
    """明白な誤配信(1日だけ価格が数分の1/数倍に飛んで戻る類)を欠測として除去する。

    前後5日の中央値から±threshold(既定40%)以上乖離した点を落とす。
    本物の暴落(数日かけた下落)は中央値も追随するため除去されない。
    実例: 1306.Tの2026-03-30/31に約1/10の誤配信があり、放置すると
    バックテストのMaxDDが-91%になる(実際のTOPIXはそんな下落をしていない)。
    """
    med = s.rolling(11, center=True, min_periods=3).median()
    bad = (s / med - 1).abs() > threshold
    return s[~bad]


def month_end_trading_days(close: pd.Series) -> pd.DatetimeIndex:
    """各月の最終「取引日」を返す(進行中の月も末尾に含まれる点に注意)。"""
    return pd.DatetimeIndex(close.resample("ME").apply(
        lambda x: x.index[-1] if len(x) else pd.NaT).dropna())


def confirmed_month_ends(close: pd.Series) -> pd.DatetimeIndex:
    """確定済みの月末取引日。最終データと同じ暦月は「進行中」として常に除外する
    (月の途中でデータが終わっているのか月末なのかを推測しない、保守的なルール)。"""
    days = month_end_trading_days(close)
    if len(days) == 0:
        return days
    last = close.index[-1]
    return pd.DatetimeIndex(
        [d for d in days if (d.year, d.month) != (last.year, last.month)])


def regime_signals(close: pd.Series) -> dict:
    """直近の確定月末時点のレジーム判定。データ不足はNone。"""
    me_days = confirmed_month_ends(close)
    if len(me_days) < 14 or len(close) < 210:
        return None
    me = close.loc[me_days]
    sma200 = close.rolling(200).mean()
    as_of = me_days[-1]
    px = float(me.iloc[-1])
    sma = sma200.loc[as_of]
    if pd.isna(sma):
        return None
    # 12-1モメンタム: 1ヶ月前の月末 ÷ 12ヶ月前の月末 − 1 (日次版と同じ流儀)
    mom = float(me.iloc[-2] / me.iloc[-13] - 1)
    return {
        "as_of": as_of.strftime("%Y-%m-%d"),
        "price": px,
        "sma200": float(sma),
        "sig_sma": bool(px > sma),
        "mom121": mom,
        "sig_mom": bool(mom > 0),
        "agree": bool((px > sma) == (mom > 0)),
    }


def _positions_daily(close: pd.Series) -> pd.Series:
    """日次の保有ポジション(0/1)。前月末のSMA200シグナルを翌営業日から適用。"""
    me_days = confirmed_month_ends(close)
    sma200 = close.rolling(200).mean()
    sig = (close.loc[me_days] > sma200.loc[me_days]).astype(float)
    sig[sma200.loc[me_days].isna()] = np.nan  # SMA未定義期間はシグナルなし
    daily = sig.reindex(close.index).ffill()
    # shift(1): d日の保有は「d日より前に確定した」シグナルに基づく(執行ラグ)
    return daily.shift(1)


def backtest_filter(close: pd.Series, open_: pd.Series) -> dict:
    """SMA200レジームフィルター vs バイ&ホールド(同一期間)。執行は翌営業日始値。"""
    pos = _positions_daily(close)
    start = pos.first_valid_index()
    if start is None:
        return None
    close = close.loc[start:]
    open_ = open_.reindex(close.index)
    pos = pos.loc[start:].fillna(0.0)

    cc = close.pct_change().fillna(0.0)
    rets = []
    trade_rets, entry_px = [], None
    prev_pos = 0.0
    for i, d in enumerate(close.index):
        p = pos.iloc[i]
        o, c = open_.iloc[i], close.iloc[i]
        if i == 0:
            rets.append(0.0)
            prev_pos = p
            if p == 1.0:
                entry_px = c
            continue
        c_prev = close.iloc[i - 1]
        if p == 1.0 and prev_pos == 0.0:      # 寄り付きで買い
            buy = o if not pd.isna(o) else c_prev
            rets.append(float(c / buy - 1))
            entry_px = buy
        elif p == 0.0 and prev_pos == 1.0:    # 寄り付きで売り
            sell = o if not pd.isna(o) else c_prev
            rets.append(float(sell / c_prev - 1))
            if entry_px:
                trade_rets.append(float(sell / entry_px - 1))
            entry_px = None
        else:
            rets.append(float(p * cc.iloc[i]))
        prev_pos = p
    if entry_px:  # 保有中のまま終了した分も1トレードとして評価
        trade_rets.append(float(close.iloc[-1] / entry_px - 1))

    strat = pd.Series(rets, index=close.index)
    bh = cc.copy()
    return {
        "filtered": _metrics(strat, trade_rets),
        "buyhold": _metrics(bh, [float(close.iloc[-1] / close.iloc[0] - 1)]),
        "start": close.index[0].strftime("%Y-%m-%d"),
        "end": close.index[-1].strftime("%Y-%m-%d"),
    }


def _metrics(daily_rets: pd.Series, trade_rets: list) -> dict:
    eq = (1 + daily_rets).cumprod()
    n = len(daily_rets)
    cagr = float(eq.iloc[-1] ** (252 / n) - 1) if n > 0 and eq.iloc[-1] > 0 else None
    dd = float((eq / eq.cummax() - 1).min())
    sd = daily_rets.std()
    sharpe = float(daily_rets.mean() / sd * np.sqrt(252)) if sd and sd > 0 else None
    wins = [t for t in trade_rets if t > 0]
    losses = [-t for t in trade_rets if t <= 0]
    win_rate = len(wins) / len(trade_rets) if trade_rets else None
    payoff = (float(np.mean(wins) / np.mean(losses))
              if wins and losses and np.mean(losses) > 0 else None)
    return {"cagr": cagr, "max_dd": dd, "sharpe": sharpe,
            "win_rate": win_rate, "payoff": payoff, "trades": len(trade_rets)}


def monthly_stances(close: pd.Series) -> list:
    """確定月末ごとの2シグナル判定の履歴(比較検証用)。判定ルールはregime_signalsと同一。"""
    me_days = confirmed_month_ends(close)
    if len(me_days) < 14 or len(close) < 210:
        return []
    me = close.loc[me_days]
    sma200 = close.rolling(200).mean()
    out = []
    for i in range(12, len(me_days)):
        d = me_days[i]
        sma = sma200.loc[d]
        if pd.isna(sma):
            continue
        s1 = bool(me.iloc[i] > sma)
        mom = float(me.iloc[i - 1] / me.iloc[i - 12] - 1)
        s2 = bool(mom > 0)
        stance = "強気" if (s1 and s2) else ("弱気" if (not s1 and not s2) else "中立")
        out.append({"month": d.strftime("%Y-%m"), "sig_sma": s1,
                    "sig_mom": s2, "stance": stance})
    return out


def stance_disagreements(a: list, b: list) -> dict:
    """2指数の月次判定(強気/中立/弱気)が食い違った月を一覧化する。共通月のみ比較。"""
    b_by_month = {r["month"]: r for r in b}
    diffs, common = [], 0
    for r in a:
        o = b_by_month.get(r["month"])
        if o is None:
            continue
        common += 1
        if r["stance"] != o["stance"]:
            diffs.append({"month": r["month"], "a": r["stance"], "b": o["stance"]})
    return {"common_months": common, "diff_count": len(diffs), "diffs": diffs}


def build_regime(close: pd.Series, open_: pd.Series, symbol: str, name: str):
    """スナップショット用: レジーム判定+バックテストのまとめ。"""
    sig = regime_signals(close)
    if sig is None:
        return None
    bt = backtest_filter(close, open_)
    return {"symbol": symbol, "name": name, **sig, "backtest": bt}
