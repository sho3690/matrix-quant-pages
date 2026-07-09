"""テクニカル指標の純粋関数。データ不足・計算不能は None を返す(smaを除く)。"""
import numpy as np
import pandas as pd


def sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window).mean()


def pct_return(close: pd.Series, days: int):
    if len(close) < days + 1:
        return None
    prev = close.iloc[-days - 1]
    if pd.isna(prev) or prev == 0:
        return None
    return float(close.iloc[-1] / prev - 1)


def mom_12_1(close: pd.Series):
    # 12-1モメンタム: 直近21営業日を除いた252営業日リターン
    if len(close) < 253:
        return None
    base, end = close.iloc[-253], close.iloc[-22]
    if pd.isna(base) or base == 0 or pd.isna(end):
        return None
    return float(end / base - 1)


def zscore(close: pd.Series, window: int = 20):
    if len(close) < window:
        return None
    tail = close.iloc[-window:]
    sd = tail.std()
    if pd.isna(sd) or sd == 0:
        return None
    return float((close.iloc[-1] - tail.mean()) / sd)


def rsi(close: pd.Series, period: int = 14):
    """Wilder方式(指数平滑 α=1/period)のRSI。TradingView等の標準実装と一致させる。
    (単純移動平均方式だと標準と最大20ポイント近くずれることがある)"""
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period,
                                   adjust=False).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period,
                                      adjust=False).mean().iloc[-1]
    if pd.isna(gain) or pd.isna(loss):
        return None
    if loss == 0:
        return 100.0
    return float(100 - 100 / (1 + gain / loss))


def atr_pct(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
    """Wilder方式(指数平滑)のATRを終値比%で返す。RSIと平滑方式を統一。"""
    if len(close) < period + 1:
        return None
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean().iloc[-1]
    last = close.iloc[-1]
    if pd.isna(atr) or pd.isna(last) or last == 0:
        return None
    return float(atr / last)


def ann_vol(close: pd.Series, window: int = 20):
    rets = close.pct_change().dropna()
    if len(rets) < window:
        return None
    return float(rets.iloc[-window:].std() * np.sqrt(252))


def sharpe(close: pd.Series, window: int = 252):
    rets = close.pct_change().dropna()
    if len(rets) < 60:
        return None
    r = rets.iloc[-window:]
    sd = r.std()
    if pd.isna(sd) or sd == 0:
        return None
    return float(r.mean() / sd * np.sqrt(252))


def max_drawdown(close: pd.Series, window: int = 252):
    c = close.iloc[-window:] if len(close) > window else close
    if len(c) < 2:
        return None
    dd = c / c.cummax() - 1
    return float(dd.min())


def pct_off_high(close: pd.Series, window: int = 252):
    """52週高値からの下落率(0以下)。0に近いほど高値圏=モメンタム強。"""
    c = close.iloc[-window:] if len(close) > window else close
    if len(c) < 2:
        return None
    hi = c.max()
    if pd.isna(hi) or hi == 0:
        return None
    return float(c.iloc[-1] / hi - 1)


def range_position(close: pd.Series, window: int = 252):
    """52週レンジ内の位置(0=安値、1=高値)。"""
    c = close.iloc[-window:] if len(close) > window else close
    if len(c) < 2:
        return None
    hi, lo = c.max(), c.min()
    if pd.isna(hi) or pd.isna(lo) or hi == lo:
        return None
    return float((c.iloc[-1] - lo) / (hi - lo))


def pct_rank(series: pd.Series, window: int = 252):
    """直近値が過去window日の中で下から何%の位置か(0=最低、1=最高)。"""
    c = series.dropna()
    if len(c) < 30:
        return None
    tail = c.iloc[-window:] if len(c) > window else c
    return float((tail <= tail.iloc[-1]).mean())


def volume_ratio(volume: pd.Series, window: int = 20):
    """直近出来高 ÷ 20日平均出来高。1超=普段より商いが多い。"""
    v = volume.dropna()
    if len(v) < window + 1:
        return None
    avg = v.iloc[-window - 1:-1].mean()
    if pd.isna(avg) or avg == 0:
        return None
    return float(v.iloc[-1] / avg)
