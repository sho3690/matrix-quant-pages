"""ファンド組入動向(FUND FLOW)の取得。

yfinance の Ticker.mutualfund_holders / institutional_holders を使う(Yahoo Finance経由)。
元データは米国SECへの届出(投信のN-PORT・機関投資家の13F)がベースなので、次の限界がある:
- 米国籍の投資信託/ETF(Vanguard, Fidelity, Capital Group, MFS, T. Rowe Price 等)と
  米国の機関投資家の保有が中心。日本国内の投信(ひふみ・さわかみ等)や年金・生保は含まれない
- 報告日は四半期末/月末で、実際の売買より1〜2ヶ月遅れて見える
- 1ファンドで30%超の保有など、明らかな誤データが混じることがある(suspectで除外)
表示専用の参考情報であり、シグナル/ケリーの判定には混ぜない。

pct_change(前回比)の読み方: Yahooは「前回報告からの株数の増減率」を返す。
前回比がちょうど+100%(1.0)の行は「前回は保有なし(新規組入)」として届いている可能性が高いので
new_est=True として推定表示する(断定はしない)。
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

from backend.util import atomic_write_json, load_json_safe

ROOT = Path(__file__).resolve().parent.parent
HOLDERS_PATH = ROOT / "data" / "holders.json"
JST = timezone(timedelta(hours=9))
CACHE_HOURS = 24
WINDOW_DAYS = 120      # 最新報告日から遡って何日分を「最近」とみなすか(四半期報告×2回分)
MIN_CHANGE = 0.20      # 前回比±20%以上を「買い増し/減らした」とみなす
SUSPECT_PCT = 0.30     # 1ファンドで発行済株式の30%超保有は誤データとみなす

# 名称からインデックス/ETFと推定する手がかり(小文字比較)
INDEX_HINTS = ("index", "indx", "idx", "etf", "ishares", "spdr", "betabuilders", "wisdomtree")

# 主要運用会社(長期実績や規模で定評のある運用会社の編集リスト。個別ファンドの成績を数値判定しているわけではない)
# (正規表現, 表示名)。ファンド名/機関名に一致したら「主要運用会社」として扱う
NOTABLE_MANAGERS = [
    (r"blackrock", "ブラックロック"),
    (r"europacific|euro pacific|\beupac\b|new perspective|growth fund of america|investment co(mpany)? of america|"
     r"capital world|capital income builder|new world fund|smallcap world|american balanced|"
     r"intl growth (&|and) income|international growth (&|and) income|american funds|\bamcap\b|"
     r"washington mutual investors|fundamental investors|new economy fund|capital group", "キャピタル・グループ"),
    (r"fidelity|strategic advisers", "フィデリティ"),
    (r"\bt\.? ?rowe\b", "T.ロウ・プライス"),
    (r"\bmfs\b", "MFS"),
    (r"vanguard", "バンガード"),
    (r"dodge & cox", "ドッジ&コックス"),
    (r"oakmark|harris associates", "オークマーク(ハリス)"),
    (r"artisan", "アーティザン"),
    (r"first eagle", "ファースト・イーグル"),
    (r"causeway", "コーズウェイ"),
    (r"harding loevner", "ハーディング・ローブナー"),
    (r"brandes", "ブランデス"),
    (r"hartford|wellington", "ウェリントン(ハートフォード)"),
    (r"jpmorgan|j\.?p\.? ?morgan", "JPモルガン"),
    (r"goldman sachs|\bgqg\b", "ゴールドマン・サックス(GQG)"),
    (r"morgan stanley", "モルガン・スタンレー"),
    (r"franklin|templeton|putnam", "フランクリン・テンプルトン"),
    (r"invesco", "インベスコ"),
    (r"baillie gifford", "ベイリー・ギフォード"),
    (r"lazard", "ラザード"),
    (r"schroder", "シュローダー"),
    (r"\babrdn\b|aberdeen", "abrdn"),
    (r"matthews", "マシューズ・アジア"),
    (r"thornburg", "ソーンバーグ"),
    (r"nuveen", "ヌビーン"),
    (r"janus", "ジャナス・ヘンダーソン"),
    (r"tweedy", "ツイーディ・ブラウン"),
    (r"wasatch", "ワサッチ"),
    (r"\bwcm\b", "WCM"),
    (r"polen", "ポーレン"),
    (r"neuberger", "ニューバーガー・バーマン"),
    (r"alliancebernstein|bernstein", "アライアンス・バーンスタイン"),
    (r"federated", "フェデレーテッド・ハーミーズ"),
    (r"aristotle", "アリストテレス"),
    (r"\bpimco\b", "PIMCO"),
    (r"allspring", "オールスプリング"),
    (r"nomura", "野村"),
    (r"nikko", "日興"),
    (r"daiwa", "大和"),
    (r"sumitomo mitsui|\bsmbc\b", "三井住友"),
    (r"mitsubishi ufj|\bmufg\b", "三菱UFJ"),
    (r"nippon life|nissay", "ニッセイ"),
]
_NOTABLE_RE = [(re.compile(pat, re.I), label) for pat, label in NOTABLE_MANAGERS]


def notable_manager(holder: str):
    """主要運用会社リストに該当すれば表示名、しなければNone。"""
    for rx, label in _NOTABLE_RE:
        if rx.search(holder or ""):
            return label
    return None


def fund_type(holder: str, kind: str) -> str:
    """"index"(指数連動/ETF) / "active"(アクティブ運用の投信) / "inst"(機関投資家)。名称からの推定。"""
    if kind == "inst":
        return "inst"
    name = (holder or "").lower()
    return "index" if any(k in name for k in INDEX_HINTS) else "active"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def _date_str(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return pd.Timestamp(v).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _rows_from_df(df, kind: str) -> list:
    if not isinstance(df, pd.DataFrame) or df.empty or "Holder" not in df.columns:
        return []
    out = []
    for _, r in df.iterrows():
        holder = r.get("Holder")
        if not isinstance(holder, str) or not holder.strip():
            continue
        holder = holder.strip()
        pct_held = _num(r.get("pctHeld"))
        pct_change = _num(r.get("pctChange"))
        shares = _num(r.get("Shares"))
        manager = notable_manager(holder)
        out.append({
            "holder": holder,
            "kind": kind,
            "type": fund_type(holder, kind),
            "manager": manager,                         # 主要運用会社の表示名(該当なしはNone)
            "notable": manager is not None,
            "date": _date_str(r.get("Date Reported")),
            "pct_held": pct_held,                       # 割合(0.0048 = 0.48%)
            "shares": int(shares) if shares is not None else None,
            "pct_change": pct_change,                   # 割合(0.5 = +50%)
            "new_est": pct_change is not None and abs(pct_change - 1.0) < 1e-9,
            "suspect": pct_held is not None and pct_held > SUSPECT_PCT,
        })
    return out


def normalize_rows(mf_df, inst_df) -> list:
    """yfinanceの2つのDataFrame(投信・機関投資家)を1つの行リストに正規化する。"""
    return _rows_from_df(mf_df, "mf") + _rows_from_df(inst_df, "inst")


def apply_first_seen(data: dict, prev: dict, today: str) -> dict:
    """(銘柄, ファンド)の初観測日を積み上げる。既知は据え置き、初めて見た組は今日。"""
    fs = {}
    for sym, rows in data.items():
        old = (prev or {}).get(sym, {})
        cur = {}
        for r in rows:
            cur[r["holder"]] = old.get(r["holder"], today)
        fs[sym] = cur
    return fs


def _direction(r: dict, min_change: float) -> str:
    c = r.get("pct_change")
    if r.get("new_est"):
        return "up"
    if c is None:
        return "flat"
    if c >= min_change:
        return "up"
    if c <= -min_change:
        return "down"
    return "flat"


def report_window(data: dict, window_days: int = WINDOW_DAYS):
    """(最新の報告日, 「最近」とみなす下限日) を返す。異常値行は除外、データが無ければ (None, None)。"""
    dates = [r["date"] for rows in data.values() for r in rows
             if r.get("date") and not r.get("suspect")]
    if not dates:
        return None, None
    newest = max(dates)
    cutoff = (date.fromisoformat(newest) - timedelta(days=window_days)).isoformat()
    return newest, cutoff


def flow_events(data: dict, first_seen: dict, baseline_at: str, names: dict = None,
                window_days: int = WINDOW_DAYS, min_change: float = MIN_CHANGE) -> list:
    """「最近の報告で目立つ動き」だけを抜き出して新しい順に並べる。

    載せる条件: 異常値でない かつ 最新報告日からwindow_days以内 かつ
                (前回比±min_change以上 or 新規推定 or この端末で初登場)
    """
    names = names or {}
    _, cutoff = report_window(data, window_days)
    if cutoff is None:
        return []
    events = []
    for sym, rows in data.items():
        for r in rows:
            if r.get("suspect") or not r.get("date") or r["date"] < cutoff:
                continue
            seen = (first_seen.get(sym) or {}).get(r["holder"])
            new_here = bool(baseline_at and seen and seen > baseline_at)
            d = _direction(r, min_change)
            if d == "flat" and not new_here:
                continue
            events.append({"symbol": sym, "name": names.get(sym, sym), **r,
                           "dir": d, "new_here": new_here})
    events.sort(key=lambda e: (e["date"], e["pct_change"] if e["pct_change"] is not None else -9e9),
                reverse=True)
    return events


# ---- 取得 -------------------------------------------------------------------

def _fetch_one(symbol: str):
    """1銘柄分を取得。失敗はNone(呼び出し側で旧値維持)。"""
    try:
        t = yf.Ticker(symbol)
        return symbol, normalize_rows(t.mutualfund_holders, t.institutional_holders)
    except Exception:
        return symbol, None


def build_holders(symbols: list, names: dict, path=HOLDERS_PATH,
                  max_age_hours: int = CACHE_HOURS) -> dict:
    """キャッシュが新しければそれを返し、古ければ再取得して保存する(fundamentalsと同じ流儀)。"""
    cached = load_json_safe(path, {})
    cdata = cached.get("data", {})
    fresh = time.time() - cached.get("fetched_at", 0) < max_age_hours * 3600
    nonempty = sum(1 for s in symbols if cdata.get(s))
    # 保有報告が無い銘柄も一定数あるので、半数以上取れていれば有効なキャッシュとみなす
    if fresh and set(symbols) <= set(cdata) and nonempty >= len(symbols) * 0.5:
        return cached

    data = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for sym, rows in ex.map(_fetch_one, symbols):
            data[sym] = rows if rows is not None else cdata.get(sym, [])

    today = datetime.now(JST).strftime("%Y-%m-%d")
    baseline_at = cached.get("baseline_at") or today
    first_seen = apply_first_seen(data, cached.get("first_seen", {}), today)
    newest, cutoff = report_window(data)
    doc = {
        "fetched_at": time.time(),
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "baseline_at": baseline_at,        # この端末で初めて取得した日(初登場判定の起点)
        "window_days": WINDOW_DAYS,
        "min_change": MIN_CHANGE,
        "newest_date": newest,             # 最新の報告日(この日からwindow_days遡った分を「最近」とする)
        "cutoff_date": cutoff,
        "coverage": {"with_data": sum(1 for s in symbols if data.get(s)), "total": len(symbols)},
        "managers": [label for _, label in NOTABLE_MANAGERS],   # 画面の「対象の運用会社」一覧用
        "first_seen": first_seen,
        "data": data,
        "events": flow_events(data, first_seen, baseline_at, names),
    }
    atomic_write_json(path, doc, indent=1)
    return doc


def load_holders(path=HOLDERS_PATH) -> dict:
    return load_json_safe(path, {})
