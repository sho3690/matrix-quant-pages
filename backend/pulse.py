"""市況パルス(センチメント・グローバル指標・ニュース)の取得。

外部通信先(すべて無料・APIキー不要・GETのみ):
- alternative.me  : Fear & Greed Index
- coingecko.com   : 暗号資産グローバル指標(BTCドミナンス・総時価総額)
- llama.fi        : DeFi TVL(資金フロー)
- coinpost.jp     : 暗号資産ニュースRSS
- nhk.or.jp       : 経済ニュースRSS
- news.yahoo.co.jp: 経済トピックスRSS
- mof.go.jp       : 国債金利CSV(日本のイールドカーブ、政府一次データ)

ソースごとに独立して失敗を許容し、取れたものだけ pulse.json に保存する。
"""
import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from backend.util import atomic_write_json, load_json_safe

ROOT = Path(__file__).resolve().parent.parent
PULSE_PATH = ROOT / "data" / "pulse.json"
JST = timezone(timedelta(hours=9))
TIMEOUT = 8.0
UA = {"User-Agent": "matrix-quant-terminal/1.1 (personal local dashboard)"}

FNG_LABEL_JA = {
    "Extreme Fear": "極度の恐怖",
    "Fear": "恐怖",
    "Neutral": "中立",
    "Greed": "強欲",
    "Extreme Greed": "極度の強欲",
}


# ---- 純粋なパース関数(テスト対象) -------------------------------------------

def parse_fng(payload: dict) -> dict:
    """alternative.me /fng/?limit=N のレスポンスを整形。dataは新しい順。"""
    data = payload["data"]
    latest = data[0]
    label_en = latest["value_classification"]
    return {
        "value": int(latest["value"]),
        "label": FNG_LABEL_JA.get(label_en, label_en),
        "history": [int(d["value"]) for d in reversed(data)],  # 古い順に直す
    }


def parse_cg_global(payload: dict) -> dict:
    d = payload["data"]
    return {
        "btc_dominance": float(d["market_cap_percentage"]["btc"]),
        "eth_dominance": float(d["market_cap_percentage"].get("eth", 0)),
        "total_mcap_usd": float(d["total_market_cap"]["usd"]),
        "mcap_change_24h": float(d.get("market_cap_change_percentage_24h_usd", 0)),
    }


def parse_llama_tvl(series: list) -> dict:
    """api.llama.fi/v2/historicalChainTvl (全チェーン合計の時系列、古い順)。"""
    if len(series) < 2:
        raise ValueError("TVL時系列が短すぎます")
    last, prev = series[-1]["tvl"], series[-2]["tvl"]
    week_ago = series[-8]["tvl"] if len(series) >= 8 else prev
    year_ago = series[-366]["tvl"] if len(series) >= 366 else None
    tail = [p["tvl"] for p in series[-365:]]
    hi, lo = max(tail), min(tail)
    return {
        "tvl_usd": float(last),
        "change_1d": float(last / prev - 1) if prev else None,
        "change_7d": float(last / week_ago - 1) if week_ago else None,
        "change_1y": float(last / year_ago - 1) if year_ago else None,
        "range_pos_1y": float((last - lo) / (hi - lo)) if hi > lo else None,
    }


def parse_rss(xml_text: str, limit: int = 8, source: str = "") -> list:
    """RSS2.0のitemからタイトル・リンク・日時(JST文字列+ソート用epoch)を抜き出す。"""
    root = ET.fromstring(xml_text)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        published, ts = "", 0.0
        pub = item.findtext("pubDate")
        if pub:
            try:
                dt = parsedate_to_datetime(pub)
                published = dt.astimezone(JST).strftime("%m/%d %H:%M")
                ts = dt.timestamp()
            except (ValueError, TypeError):
                pass
        # 記事本文の書き出し(要約文の素材)。HTMLタグを除去して短く整える
        desc = item.findtext("description") or ""
        desc = html.unescape(re.sub(r"<[^>]*>", "", desc))
        desc = re.sub(r"\s+", " ", desc).strip()[:140]
        items.append({"title": title, "link": link, "published": published,
                      "ts": ts, "source": source, "desc": desc})
        if len(items) >= limit:
            break
    return items


# ---- 日本国債(JGB)イールドカーブ ---------------------------------------------

JGB_ALL_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"
JGB_CUR_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
JGB_CACHE = ROOT / "data" / "jgbcm_all.csv"
JGB_MATURITIES = [1, 2, 5, 10, 20, 30, 40]


def parse_jgb_rows(text: str) -> list:
    """財務省CSVから [{date, curve{年限: 利回り}}] を古い順で返す。"""
    lines = [l for l in text.splitlines() if l.strip()]
    header = None
    rows = []
    for line in lines:
        cells = [c.strip() for c in line.split(",")]
        if cells[0].startswith("基準日"):
            header = cells
            continue
        if header is None or len(cells) != len(header) or not cells[0]:
            continue
        curve = {}
        for m in JGB_MATURITIES:
            name = f"{m}年"
            if name in header:
                v = cells[header.index(name)]
                try:
                    curve[m] = float(v)
                except ValueError:
                    curve[m] = None
        if curve.get(10) is not None:
            rows.append({"date": cells[0], "curve": curve})
    return rows


def build_jgb(all_text: str, cur_text: str) -> dict:
    """全期間+当月CSVを結合し、10年金利の水準・週次変化・1年内順位とカーブを返す。"""
    merged = {}
    for r in parse_jgb_rows(all_text) + parse_jgb_rows(cur_text):
        merged[r["date"]] = r  # 同日重複は後勝ち(当月ファイル優先)
    rows = list(merged.values())
    if not rows:
        raise ValueError("JGBデータが空です")
    tail = rows[-250:]
    y10s = [r["curve"][10] for r in tail]
    y10 = y10s[-1]
    week_ago = y10s[-6] if len(y10s) >= 6 else y10s[0]
    latest = tail[-1]
    c = latest["curve"]
    spread = (c[10] - c[2]) if c.get(10) is not None and c.get(2) is not None else None
    return {
        "date": latest["date"],
        "y10": y10,
        "chg5d": y10 - week_ago,
        "rank_1y": sum(1 for v in y10s if v <= y10) / len(y10s),
        "curve": c,
        "spread_10_2": spread,
    }


# ---- 取得(ネットワーク) ------------------------------------------------------

def _get(client: httpx.Client, url: str):
    r = client.get(url, headers=UA, follow_redirects=True)
    r.raise_for_status()
    return r


def build_pulse(out_path=PULSE_PATH) -> dict:
    pulse = {"updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
             "fng": None, "crypto_global": None, "defi": None,
             "news_crypto": [], "news_jp": [], "jgb": None}
    with httpx.Client(timeout=TIMEOUT) as client:
        try:
            pulse["fng"] = parse_fng(_get(client, "https://api.alternative.me/fng/?limit=30").json())
        except Exception:
            pass
        try:
            pulse["crypto_global"] = parse_cg_global(
                _get(client, "https://api.coingecko.com/api/v3/global").json())
        except Exception:
            pass
        try:
            pulse["defi"] = parse_llama_tvl(
                _get(client, "https://api.llama.fi/v2/historicalChainTvl").json())
        except Exception:
            pass
        try:
            pulse["news_crypto"] = parse_rss(
                _get(client, "https://coinpost.jp/?feed=rss2").text, source="CoinPost")
        except Exception:
            pass
        # 株式・経済ニュース: NHKとYahoo!ニュースをマージして新しい順に
        jp_news = []
        try:
            jp_news += parse_rss(_get(client, "https://www3.nhk.or.jp/rss/news/cat5.xml").text,
                                 source="NHK")
        except Exception:
            pass
        try:
            jp_news += parse_rss(_get(client, "https://news.yahoo.co.jp/rss/topics/business.xml").text,
                                 source="Yahoo!")
        except Exception:
            pass
        pulse["news_jp"] = sorted(jp_news, key=lambda n: n["ts"], reverse=True)[:8]
        # 日本国債イールドカーブ(全期間CSVは7日ごとにキャッシュ更新)
        try:
            import time
            if (not JGB_CACHE.exists()
                    or time.time() - JGB_CACHE.stat().st_mtime > 7 * 86400):
                JGB_CACHE.parent.mkdir(parents=True, exist_ok=True)
                JGB_CACHE.write_bytes(_get(client, JGB_ALL_URL).content)
            all_text = JGB_CACHE.read_bytes().decode("cp932", errors="replace")
            cur_text = _get(client, JGB_CUR_URL).content.decode("cp932", errors="replace")
            pulse["jgb"] = build_jgb(all_text, cur_text)
        except Exception:
            pulse["jgb"] = None
    atomic_write_json(out_path, pulse, indent=1)
    return pulse


def load_pulse(path=PULSE_PATH):
    return load_json_safe(path, None)
