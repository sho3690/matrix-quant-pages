"""yfinanceによる市場データ取得とスナップショット(market.json)生成。

- 価格は yf.download で全銘柄一括1リクエスト(kabu-compassの実績パターン)
- auto_adjust=True のため Close は配当・分割調整済み
- 取得失敗銘柄は前回スナップショットの値を stale=true で再利用
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

from backend import fundamentals as fnd
from backend import indicators as ind
from backend import regime as rg
from backend.signals import (trend_score, composite_signal, kelly_backtest,
                             caution_notes)
from backend.util import atomic_write_json, load_json_safe

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT / "watchlist.json"
CONFIG_PATH = ROOT / "config.json"
MARKET_PATH = ROOT / "data" / "market.json"

# レジーム判定用TOPIXのデータ源(優先順)。^TPXはYahooで取得不可のことが多く
# その場合はTOPIX連動ETFの1306.Tを代理に使う(調整済み価格)
TOPIX_CANDIDATES = ["^TPX", "1306.T"]


def load_config() -> dict:
    """config.json(regime_index: "topix"|"n225")。無ければ既定値。"""
    return {"regime_index": "topix", **load_json_safe(CONFIG_PATH, {})}
JST = timezone(timedelta(hours=9))
FALLBACK_USDJPY = 150.0

# GLOBAL MARKETSパネル用の指数(yfinanceで取得、APIキー不要)
INDICES = [
    {"symbol": "^N225", "name": "日経225"},
    {"symbol": "^GSPC", "name": "S&P500"},
    {"symbol": "^IXIC", "name": "NASDAQ"},
    {"symbol": "^DJI", "name": "NYダウ"},
    {"symbol": "^HSI", "name": "香港ハンセン"},
    {"symbol": "^GDAXI", "name": "独DAX"},
    {"symbol": "^VIX", "name": "VIX恐怖指数"},
    {"symbol": "^TNX", "name": "米10年金利"},
    {"symbol": "^IRX", "name": "米3ヶ月金利"},  # 10年-3ヶ月スプレッド(逆イールド判定)用
]

# 相対強さ(RS)のベンチマーク: 資産クラス → 指数
RS_BENCHMARK = {"jp": "^N225", "crypto": "BTC-USD", "other": "^GSPC"}

# 2026年の政策イベント(結果発表日)。出典: 日銀公表の年間日程・FRB公式カレンダー
POLICY_EVENTS = [
    ("2026-01-23", "日銀会合"), ("2026-01-28", "FOMC(米)"),
    ("2026-03-18", "FOMC(米)"), ("2026-03-19", "日銀会合"),
    ("2026-04-28", "日銀会合"), ("2026-04-29", "FOMC(米)"),
    ("2026-06-16", "日銀会合"), ("2026-06-17", "FOMC(米)"),
    ("2026-07-29", "FOMC(米)"), ("2026-07-31", "日銀会合"),
    ("2026-09-16", "FOMC(米)"), ("2026-09-18", "日銀会合"),
    ("2026-10-28", "FOMC(米)"), ("2026-10-30", "日銀会合"),
    ("2026-12-09", "FOMC(米)"), ("2026-12-18", "日銀会合"),
]


SIGNAL_STATE_PATH = ROOT / "data" / "signal_state.json"


def update_signal_log(assets: list, path=SIGNAL_STATE_PATH, today=None) -> list:
    """シグナルの変化(LONG→NEUTRAL等)を記録し、直近7日分を新しい順で返す。

    state: {symbol: {"label": ...}} を前回として比較。初回は変化なし扱い。
    """
    today = today or datetime.now(JST).date()
    today_str = today.strftime("%Y-%m-%d")
    log = load_json_safe(path, {"state": {}, "changes": []})
    state, changes = log.get("state", {}), log.get("changes", [])

    current_syms = set()
    for a in assets:
        sym, cur = a["symbol"], a["signal"]["label"]
        current_syms.add(sym)
        prev = state.get(sym, {}).get("label")
        if prev and prev != cur:
            changes.append({"symbol": sym, "name": a["name"],
                            "from": prev, "to": cur, "date": today_str})
        state[sym] = {"label": cur}

    # 監視から外れた銘柄のstateを掃除し、7日より古い変化は捨てる
    state = {s: v for s, v in state.items() if s in current_syms}
    def fresh(c):
        try:
            d = datetime.strptime(c["date"], "%Y-%m-%d").date()
            return (today - d).days <= 7
        except (ValueError, KeyError):
            return False
    changes = [c for c in changes if fresh(c)][-50:]

    atomic_write_json(path, {"state": state, "changes": changes})
    return list(reversed(changes))


def ytd_return(s: pd.Series):
    """年初来リターン。標準的な定義どおり「前年末の終値」を起点にする。
    (今年の初日終値を起点にすると大発会分の値動きが欠落してズレる)"""
    if len(s) < 2:
        return None
    this_year = s.index[-1].year
    prev_year = s[s.index.year < this_year]
    if len(prev_year) == 0:
        return None
    base = prev_year.iloc[-1]
    if pd.isna(base) or base == 0:
        return None
    return float(s.iloc[-1] / base - 1)


def upcoming_events(today=None, count: int = 2) -> list:
    """直近の政策イベント(結果発表日)を残り日数つきで返す。"""
    today = today or datetime.now(JST).date()
    out = []
    for date_str, label in POLICY_EVENTS:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        if d >= today:
            out.append({"label": label, "date": d.strftime("%m/%d"),
                        "days": (d - today).days})
        if len(out) >= count:
            break
    return out


def normalize_watchlist(raw: dict) -> dict:
    """旧形式(crypto/jpの2リスト)と新形式(assets1本+通貨情報)の両方を受け付ける。"""
    if "assets" in raw:
        assets = raw["assets"]
    else:
        assets = ([{**a, "asset_class": "crypto", "currency": "USD"} for a in raw.get("crypto", [])]
                  + [{**a, "asset_class": "jp", "currency": "JPY"} for a in raw.get("jp", [])])
    for a in assets:
        a.setdefault("asset_class", "other")
        a.setdefault("currency", "JPY" if a["asset_class"] == "jp" else "USD")
        a["currency"] = a["currency"].upper()
    return {"assets": assets, "pinned": raw.get("pinned", []),
            "fx": raw.get("fx", "USDJPY=X")}


def load_watchlist(path=WATCHLIST_PATH) -> dict:
    return normalize_watchlist(json.loads(Path(path).read_text(encoding="utf-8")))


def save_watchlist(wl: dict, path=WATCHLIST_PATH):
    Path(path).write_text(json.dumps(wl, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def to_jpy(price, currency: str, usdjpy: float):
    """円換算。JPY建てはそのまま、USD建てはUSDJPYで換算、他通貨はNone(換算不能)。"""
    if price is None:
        return None
    if currency == "JPY":
        return price
    if currency == "USD":
        return price * usdjpy
    return None


def probe_symbol(symbol: str) -> dict:
    """銘柄がYahoo Financeに実在するか確認し、通貨・名前・分類を返す。
    見つからなければ ValueError(日本語メッセージ)。"""
    t = yf.Ticker(symbol)
    hist = t.history(period="3mo", auto_adjust=True)
    if hist is None or hist.empty:
        raise ValueError(
            f"「{symbol}」はYahoo Financeで見つかりませんでした。"
            "表記を確認してください(例: 日本株 7203.T / 暗号通貨 BTC-USD / 米国株 AAPL)")
    currency = None
    try:
        currency = t.fast_info["currency"]
    except Exception:
        pass
    name = None
    try:
        info = t.get_info()
        name = info.get("shortName") or info.get("longName")
    except Exception:
        pass
    up = symbol.upper()
    if up.endswith(".T"):
        asset_class, currency = "jp", (currency or "JPY")
    elif "-USD" in up or "-JPY" in up:
        asset_class, currency = "crypto", (currency or "USD")
    else:
        asset_class, currency = "other", (currency or "USD")
    return {"currency": currency.upper(), "name": name, "asset_class": asset_class}


def fetch_prices(symbols: list) -> pd.DataFrame:
    # ケリー計算はMA50/200クロスの往復数が必要なため10年分取得する
    # (直近指標はどのみち末尾N日しか使わない)
    # timeoutを明示しないとネットワーク不調時に更新が永久に固まりうる
    return yf.download(symbols, period="10y", auto_adjust=True, progress=False,
                       group_by="column", threads=True, timeout=20)


def load_snapshot(path=MARKET_PATH):
    # 壊れたファイルでも起動不能にならない(壊れていたら.bakに退避してNone)
    return load_json_safe(path, None)


def _series_tail(s: pd.Series, n: int = 250) -> list:
    """チャート描画用に末尾n点をJSON化(NaNはNone)。"""
    return [None if pd.isna(v) else round(float(v), 6) for v in s.iloc[-n:]]


def _asset_metrics(close: pd.Series, high: pd.Series, low: pd.Series,
                   volume=None, rs63=None) -> dict:
    price = float(close.iloc[-1])
    ma50_series = ind.sma(close, 50)
    ma200_series = ind.sma(close, 200)
    ma50 = ma50_series.iloc[-1]
    ma200 = ma200_series.iloc[-1]
    t_score = trend_score(price, ma50, ma200)
    ret63 = ind.pct_return(close, 63)
    z20 = ind.zscore(close, 20)
    off_high = ind.pct_off_high(close, 252)
    return {
        "price": price,
        "off_high52": off_high,
        "range_pos52": ind.range_position(close, 252),
        "vol_ratio": ind.volume_ratio(volume, 20) if volume is not None else None,
        "ma200_gap": (price / ma200 - 1) if not pd.isna(ma200) and ma200 else None,
        "rs63": rs63,
        "ret1": ind.pct_return(close, 1),
        "ret63": ret63,
        "ret252": ind.pct_return(close, 252),
        "mom121": ind.mom_12_1(close),
        "trend_score": t_score,
        "z20": z20,
        "rsi14": ind.rsi(close, 14),
        "atr_pct": ind.atr_pct(high, low, close, 14),
        "vol20": ind.ann_vol(close, 20),
        "sharpe1y": ind.sharpe(close, 252),
        "mdd1y": ind.max_drawdown(close, 252),
        "signal": composite_signal(t_score, ret63, z20, rs63=rs63, off_high=off_high),
        "kelly": kelly_backtest(close),
        "spark": {"close": _series_tail(close), "ma50": _series_tail(ma50_series),
                  "ma200": _series_tail(ma200_series)},
    }


def build_snapshot(watchlist_path=WATCHLIST_PATH, out_path=MARKET_PATH) -> dict:
    wl = load_watchlist(watchlist_path)
    assets_def = wl["assets"]
    index_symbols = [i["symbol"] for i in INDICES]
    symbols = list(dict.fromkeys(
        [a["symbol"] for a in assets_def] + [wl["fx"]]
        + index_symbols + TOPIX_CANDIDATES))
    prev = load_snapshot(out_path)
    prev_assets = {a["symbol"]: a for a in (prev or {}).get("assets", [])}

    raw = fetch_prices(symbols)
    closes = raw["Close"] if "Close" in raw else pd.DataFrame()
    volumes = raw["Volume"] if "Volume" in raw else pd.DataFrame()

    # ファンダメンタルズ(株式のみ。初回は数十秒、以後24時間キャッシュ)
    try:
        fund = fnd.load_or_fetch([a["symbol"] for a in assets_def
                                  if a["asset_class"] != "crypto"])
    except Exception:
        fund = {}

    # ベンチマークの63日リターン(相対強さRSの基準)
    bench_ret63 = {}
    for cls_name, bench_sym in RS_BENCHMARK.items():
        if bench_sym in closes.columns:
            s = closes[bench_sym].dropna()
            if len(s) > 64:
                bench_ret63[cls_name] = ind.pct_return(s, 63)

    # USDJPY
    usdjpy = None
    if wl["fx"] in closes.columns:
        fx_series = closes[wl["fx"]].dropna()
        if len(fx_series):
            usdjpy = float(fx_series.iloc[-1])
    if usdjpy is None:
        usdjpy = (prev or {}).get("usdjpy") or FALLBACK_USDJPY

    assets = []
    for a in assets_def:
        sym = a["symbol"]
        col = closes[sym].dropna() if sym in closes.columns else pd.Series(dtype=float)
        if len(col) < 30:
            stale_prev = prev_assets.get(sym)
            if stale_prev:
                assets.append({**stale_prev, "stale": True})
            continue
        high = raw["High"][sym].dropna() if "High" in raw else col
        low = raw["Low"][sym].dropna() if "Low" in raw else col
        vol = volumes[sym] if sym in volumes.columns else None
        ret63 = ind.pct_return(col, 63)
        bench = bench_ret63.get(a["asset_class"])
        # 自分自身がベンチマーク(BTC)の場合はRSを出さない
        rs63 = (ret63 - bench) if (ret63 is not None and bench is not None
                                   and sym != RS_BENCHMARK.get(a["asset_class"])) else None
        m = _asset_metrics(col, high, low, volume=vol, rs63=rs63)
        price_jpy = to_jpy(m["price"], a["currency"], usdjpy)
        f = fund.get(sym, {})
        # LONGでも割高/伸びすぎなら根拠に注意書きを添える(安全域の視点)
        m["signal"]["reasons"] += caution_notes(
            m["signal"]["label"], per=f.get("per"), ma200_gap=m["ma200_gap"])
        assets.append({**a, **m, "price_jpy": price_jpy, "stale": False,
                       "sector": f.get("sector"), "psr": f.get("psr"),
                       "per": f.get("per"), "per_fwd": f.get("per_fwd"),
                       "pbr": f.get("pbr"),
                       "div_yield": f.get("div_yield"), "roe": f.get("roe"),
                       "mcap": f.get("mcap"), "op_margin": f.get("op_margin"),
                       "rev_growth": f.get("rev_growth"),
                       "earn_growth": f.get("earn_growth"),
                       "payout": f.get("payout"),
                       "equity_ratio": f.get("equity_ratio"),
                       "fcf_yield": f.get("fcf_yield"),
                       "earnings_ts": f.get("earnings_ts")})

    # 世界の指数(GLOBAL MARKETSパネル用)
    indices = []
    for idef in INDICES:
        sym = idef["symbol"]
        if sym not in closes.columns:
            continue
        s = closes[sym].dropna()
        if len(s) < 10:
            continue
        ma200 = ind.sma(s, 200).iloc[-1]
        price = float(s.iloc[-1])
        indices.append({
            **idef,
            "price": price,
            "chg1d": ind.pct_return(s, 1),
            "chg5d": ind.pct_return(s, 5),
            "ytd": ytd_return(s),
            "range_pos52": ind.range_position(s, 252),
            "ma200_gap": (price / ma200 - 1) if not pd.isna(ma200) and ma200 else None,
            "pct_rank_1y": ind.pct_rank(s, 252),
        })

    # 指数レジーム判定+バックテスト(月末確定値ベース)
    # 判定用の日本指数はconfig.jsonで選択(既定=TOPIX、"n225"で日経225に戻せる)
    opens = raw["Open"] if "Open" in raw else pd.DataFrame()

    topix_sym, topix_note = None, None
    for cand in TOPIX_CANDIDATES:
        if cand in closes.columns and len(closes[cand].dropna()) >= 500:
            topix_sym = cand
            break
    if topix_sym == "1306.T":
        topix_note = ("データ源: 1306.T(TOPIX連動ETF)による代理。指数^TPXがYahooで取得不可のため。"
                      "調整済み価格で分配金はおおむね補正済みだが、指数とは微差がありうる")
    elif topix_sym == "^TPX":
        topix_note = "データ源: ^TPX(TOPIX指数)"
    print(f"[regime] TOPIXデータ源: {topix_sym or '取得不可'}")

    specs = [("n225", "^N225", "日経225", None),
             ("topix", topix_sym, "TOPIX", topix_note),
             ("spx", "^GSPC", "S&P500", None)]
    built = {}
    for key, sym, name, note in specs:
        if not sym or sym not in closes.columns:
            continue
        c = rg.remove_bad_ticks(closes[sym].dropna())  # 誤配信ティックを除去
        if len(c) < 500:
            continue
        o = (opens[sym] if sym in opens.columns
             else pd.Series(dtype=float)).reindex(c.index)
        try:
            r = rg.build_regime(c, o, sym, name)
            if r:
                r["key"] = key
                if note:
                    r["source_note"] = note
                built[key] = (r, c)
        except Exception:
            pass  # レジームが作れなくても他は配信する

    cfg = load_config()
    jp_key = cfg.get("regime_index", "topix")
    if jp_key not in ("topix", "n225"):
        jp_key = "topix"
    regimes = []
    if jp_key in built:
        regimes.append(built[jp_key][0])
    elif "n225" in built:  # TOPIXが取れない場合のフォールバック
        regimes.append(built["n225"][0])
    if "spx" in built:
        regimes.append(built["spx"][0])

    # 比較検証の記録(日経225の結果は削除せず残す・判定不一致の一覧)
    regime_compare = None
    if "n225" in built and "topix" in built:
        dis = rg.stance_disagreements(rg.monthly_stances(built["n225"][1]),
                                      rg.monthly_stances(built["topix"][1]))
        regime_compare = {
            "judge_key": jp_key,
            "topix_note": topix_note,
            "rows": [{"key": k, "name": built[k][0]["name"],
                      "backtest": built[k][0]["backtest"]}
                     for k in ("n225", "topix", "spx") if k in built],
            "disagreements": dis,
        }

    snapshot = {
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "usdjpy": usdjpy,
        "assets": assets,
        "pinned": wl.get("pinned", []),
        "indices": indices,
        "events": upcoming_events(),
        "signal_changes": update_signal_log(assets),
        "regimes": regimes,
        "regime_compare": regime_compare,
    }
    atomic_write_json(out_path, snapshot, allow_nan=False, default=str, indent=1)
    return snapshot
