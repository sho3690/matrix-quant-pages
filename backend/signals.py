"""総合シグナル(ルールベース)とケリー基準の簡易バックテスト。"""
import pandas as pd

from backend.indicators import sma


def trend_score(price, ma50, ma200) -> int:
    vals = (price, ma50, ma200)
    if any(v is None or pd.isna(v) for v in vals):
        return 0
    if price > ma50 > ma200:
        return 2
    if price < ma50 < ma200:
        return -2
    if price > ma200:
        return 1
    return 0


def composite_signal(t_score: int, ret63, z, rs63=None, off_high=None) -> dict:
    """トレンド・モメンタム・相対強さ・52週高値圏・過熱の5要素の合議制。

    - rs63: ベンチマーク(日経225/BTC/S&P500)に対する63日の超過リターン。
      相対的に強い銘柄が強いままになりやすい「相対力効果」を織り込む
    - off_high: 52週高値からの下落率(0以下)。高値圏(−5%以内)は
      「52週高値モメンタム」の研究で優位性が確認されているため加点
    判定: score >= 4 → LONG / score <= -2 → AVOID / それ以外 NEUTRAL
    """
    score = t_score
    reasons = []
    trend_text = {2: "強い上昇トレンド(価格>MA50>MA200)", 1: "長期線の上(価格>MA200)",
                  0: "トレンド中立", -2: "明確な下降トレンド(価格<MA50<MA200)"}
    reasons.append(trend_text.get(t_score, "トレンド中立"))
    if ret63 is not None:
        if ret63 > 0.05:
            score += 1
            reasons.append(f"3ヶ月モメンタム {ret63:+.1%}")
        elif ret63 < -0.05:
            score -= 1
            reasons.append(f"3ヶ月モメンタム悪化 {ret63:+.1%}")
    if rs63 is not None:
        if rs63 > 0.02:
            score += 1
            reasons.append(f"ベンチマーク比 {rs63:+.1%} と相対的に強い")
        elif rs63 < -0.02:
            score -= 1
            reasons.append(f"ベンチマーク比 {rs63:+.1%} と相対的に弱い")
    if off_high is not None and off_high > -0.05:
        score += 1
        reasons.append(f"52週高値圏(高値から{off_high:+.1%})")
    if z is not None:
        if z > 2:
            score -= 1
            reasons.append(f"買われすぎ (Z={z:+.1f})")
        elif z < -2 and t_score >= 0:
            score += 1
            reasons.append(f"売られすぎの押し目 (Z={z:+.1f})")
    label = "LONG" if score >= 4 else ("AVOID" if score <= -2 else "NEUTRAL")
    return {"label": label, "score": score, "reasons": reasons}


def caution_notes(label: str, per=None, ma200_gap=None) -> list:
    """LONGなのに割高/伸びすぎのとき、根拠に添える注意書き(ラベルは変えない)。

    モメンタム系シグナルは「強いものを買う」ため割高に見える銘柄を選びやすい。
    グレアムの安全域の視点を注記として補い、判断はユーザーに委ねる。
    """
    if label != "LONG":
        return []
    warns = []
    if per is not None and per > 25:
        warns.append(f"PER{per:.0f}倍と割高圏")
    if ma200_gap is not None and ma200_gap > 0.15:
        warns.append(f"MA200乖離{ma200_gap:+.0%}と伸びすぎ")
    if not warns:
        return []
    return ["※" + "・".join(warns) + "。勢い優先のシグナルのため、押し目待ちも選択肢"]


def kelly_backtest(close: pd.Series) -> dict:
    """MA50>MA200の期間だけ保有するトレンドフォローを過去データで再現し、
    勝率と損益比からケリー比率を求める。表示用はハーフケリーを0〜25%にクランプ。

    約定はシグナル確定の「翌営業日の終値」。確定と同じ足の終値で約定させると
    現実には不可能な楽観バックテスト(ルックアヘッド)になるため。"""
    ma50, ma200 = sma(close, 50), sma(close, 200)
    trades = []
    entry = None
    prev = False
    for i in range(len(close) - 1):  # 約定はi+1なので最終足の1本手前まで
        holding = (not pd.isna(ma200.iloc[i])) and ma50.iloc[i] > ma200.iloc[i]
        if holding and not prev:
            entry = close.iloc[i + 1]   # 翌足の終値でエントリー
        elif not holding and prev and entry is not None:
            trades.append(float(close.iloc[i + 1] / entry - 1))  # 翌足の終値でエグジット
            entry = None
        prev = holding
    if prev and entry is not None:
        trades.append(float(close.iloc[-1] / entry - 1))  # 保有中は最新値で評価

    if len(trades) < 5:
        return {"insufficient": True, "trades": len(trades)}
    wins = [t for t in trades if t > 0]
    losses = [-t for t in trades if t <= 0]
    p = len(wins) / len(trades)
    if not losses:
        kelly = p
        payoff = None
    else:
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses)
        if avg_win == 0:
            kelly, payoff = 0.0, 0.0
        else:
            payoff = avg_win / avg_loss
            kelly = p - (1 - p) / payoff
    half = kelly / 2
    return {"insufficient": False, "trades": len(trades), "win_rate": p,
            "payoff": payoff, "kelly": kelly, "half_kelly": half,
            "position_pct": min(max(half, 0.0), 0.25)}
