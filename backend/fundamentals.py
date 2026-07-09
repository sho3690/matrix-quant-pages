"""ファンダメンタルズ(PER/PBR/配当利回り/ROE/時価総額/次回決算)の取得。

yfinance の Ticker.get_info() を控えめな並列数(8)で取得し、24時間キャッシュする。
1銘柄ずつのAPIなので初回は数十秒かかるが、以後はキャッシュで即時。
暗号資産には財務諸表が無いため対象は株式(jp/other)のみ。
"""
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yfinance as yf

from backend.util import atomic_write_json, load_json_safe

ROOT = Path(__file__).resolve().parent.parent
FUND_PATH = ROOT / "data" / "fundamentals.json"
CACHE_HOURS = 24


def normalize_info(info: dict) -> dict:
    """yfinanceのinfo辞書から必要な指標だけを取り出して正規化する。"""
    def num(key):
        v = info.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    # 配当利回り: yfinanceは項目・バージョンで%と割合が混在する危険地帯。
    # 「年間配当額÷株価」の自前計算を最優先にして単位の推測を避ける
    rate = num("dividendRate") or num("trailingAnnualDividendRate")
    price = num("currentPrice") or num("regularMarketPrice") or num("previousClose")
    if rate and price:
        dy = rate / price
    else:
        dy = num("trailingAnnualDividendYield")  # この項目は割合(0.007=0.7%)
        if dy is None:
            dy = num("dividendYield")
            if dy is not None:
                dy = dy / 100  # 現行yfinance(1.x)のdividendYieldは%表記
    if dy is not None and not (0 <= dy <= 0.25):
        dy = None  # 利回り25%超はデータ異常として表示しない
    roe = num("returnOnEquity")
    if roe is not None and abs(roe) > 3:  # まれに%表記で来る場合の保険
        roe = roe / 100
    def clamp(v, lo, hi):
        return v if v is not None and lo <= v <= hi else None

    # 自己資本比率(概算): debtToEquityは%表記(58.6=58.6%)。負債=有利子負債ベースの近似
    dte = num("debtToEquity")
    equity_ratio = 100 / (100 + dte) if dte is not None and dte >= 0 else None

    mcap = num("marketCap")
    fcf = num("freeCashflow")
    sector = info.get("sector")
    return {
        "sector": sector if isinstance(sector, str) else None,
        "psr": clamp(num("priceToSalesTrailing12Months"), 0, 200),
        "per": num("trailingPE"),
        "per_fwd": num("forwardPE"),
        "pbr": num("priceToBook"),
        "div_yield": dy,
        "roe": roe,
        "mcap": mcap,
        "op_margin": clamp(num("operatingMargins"), -1, 1),
        "rev_growth": clamp(num("revenueGrowth"), -1, 5),
        "earn_growth": clamp(num("earningsGrowth") or num("earningsQuarterlyGrowth"), -5, 10),
        "payout": clamp(num("payoutRatio"), 0, 3),
        "equity_ratio": equity_ratio,
        "fcf_yield": (fcf / mcap) if fcf is not None and mcap else None,
        # 決算日時(epoch秒)。米国株は精度が高いが日本株は取れないことも多い(目安扱い)
        "earnings_ts": num("earningsTimestamp") or num("earningsTimestampStart"),
    }


def _fetch_one(symbol: str):
    try:
        return symbol, normalize_info(yf.Ticker(symbol).get_info())
    except Exception:
        return symbol, {}


def load_or_fetch(symbols: list, path=FUND_PATH, max_age_hours=CACHE_HOURS) -> dict:
    """キャッシュが新しければそれを返し、古ければ再取得して保存する。"""
    cached = load_json_safe(path, {})
    cdata = cached.get("data", {})
    fresh = time.time() - cached.get("fetched_at", 0) < max_age_hours * 3600
    # 取得失敗(空辞書)の銘柄が多いキャッシュは「有効」と見なさない
    # (yfinance側の一時不調が24時間固定されるのを防ぐ)
    nonempty = sum(1 for s in symbols if cdata.get(s))
    if fresh and set(symbols) <= set(cdata) and nonempty >= len(symbols) * 0.5:
        return cdata
    data = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for sym, d in ex.map(_fetch_one, symbols):
            data[sym] = d or cdata.get(sym, {})  # 今回失敗しても旧値があれば残す
    atomic_write_json(path, {"fetched_at": time.time(), "data": data})
    return data
