"""EDINET 大量保有報告書(5%ルール)から、監視銘柄の大口保有の動きを取る(LARGE HOLDINGS)。

金融庁 EDINET API v2(無料・要APIキー)。発行済株式の5%超を持つ者(運用会社・事業会社・個人など)は
大量保有報告書を、その後1%以上の増減で変更報告書を、原則5営業日以内に提出する。
米国SEC届出ベースの FUND FLOW(holders.py)より鮮度が高く、日本の運用会社の動きも含まれる。

取得の流れ:
1. EDINETコードリスト(公開CSV)で「発行者のEDINETコード → 証券コード」の対応表を作る(30日キャッシュ)
2. 直近 WINDOW_DAYS 日の書類一覧(documents.json、日ごとにキャッシュ)から
   docTypeCode 350(大量保有報告書・変更報告書)で issuerEdinetCode が監視銘柄のものを選ぶ
   (書類一覧の secCode は提出者の証券コードなので使わない。訂正報告書(360)は数値が訂正前後で紛れるため除外)
3. 各書類のCSV(type=5、XBRL由来・UTF-16タブ区切り)から 保有割合・直前の保有割合・保有者名・保有目的 を読む
   (書類IDごとにキャッシュ。合計行はコンテキストID "FilingDateInstant"、共同保有者ごとの行は接尾辞つき)
APIキーは環境変数 EDINET_FSA_KEY か KEY_FILES のいずれか(1行)。キーが無ければ静かに前回データを返す。
表示専用の参考情報で、シグナル/ケリーには使わない。
"""
import csv
import io
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from backend.util import atomic_write_json, load_json_safe

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
EDINET_PATH = DATA / "edinet.json"
CODES_PATH = DATA / "edinet_codes.json"
DOCS_CACHE_PATH = DATA / "edinet_docs.json"
LISTS_DIR = DATA / "edinet_lists"

BASE = "https://api.edinet-fsa.go.jp/api/v2"
CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
VIEWER_URL = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id}"
KEY_ENV = "EDINET_FSA_KEY"
KEY_FILES = [DATA / "edinet_key_fsa.txt", Path.home() / "cre-scout" / "tools" / "edinet_key_fsa.txt"]

DOC_TYPES = ("350",)      # 大量保有報告書・変更報告書(訂正報告書 360 は除外)
WINDOW_DAYS = 45          # 何日分の提出を見るか
CACHE_HOURS = 6           # この時間以内に取得済みなら再取得しない
CODES_MAX_AGE_DAYS = 30
DOC_SLEEP_S = 0.3         # 書類ダウンロードの間隔(APIへの礼儀)
MAX_DOCS_PER_RUN = 120    # 1回の更新で新たに読む書類数の上限(初回の暴走防止)

# 主要運用会社(編集リスト。名称はNFKC正規化+小文字で照合)。個別ファンドの成績を判定しているわけではない
NOTABLE_MANAGERS = [
    (r"ブラックロック|blackrock", "ブラックロック"),
    (r"バンガード|vanguard", "バンガード"),
    (r"キャピタル・?(グループ|リサーチ|インターナショナル)|capital (group|research|international)", "キャピタル・グループ"),
    (r"フィデリティ|fidelity|\bfmr\b", "フィデリティ"),
    (r"jpモルガン・?アセット|j\.?p\.? ?morgan asset", "JPモルガン・アセット"),
    (r"ゴールドマン・?サックス・?アセット|goldman sachs asset", "ゴールドマン・サックス・アセット"),
    (r"モルガン・?スタンレー・?インベストメント|morgan stanley investment", "モルガン・スタンレー・インベストメント"),
    (r"インベスコ|invesco", "インベスコ"),
    (r"シュローダー|schroder", "シュローダー"),
    (r"アライアンス・?バーンスタイン|alliancebernstein", "アライアンス・バーンスタイン"),
    (r"ティー・?ロウ・?プライス|t\.? ?rowe", "T.ロウ・プライス"),
    (r"ウエリントン|ウェリントン|wellington", "ウェリントン"),
    (r"\bmfs\b|エムエフエス", "MFS"),
    (r"ノルウェー銀行|norges bank", "ノルウェー政府年金基金(NBIM)"),
    (r"ベイリー・?ギフォード|baillie gifford", "ベイリー・ギフォード"),
    (r"ドッジ・?アンド・?コックス|dodge & cox", "ドッジ&コックス"),
    (r"シルチェスター|silchester", "シルチェスター"),
    (r"ラザード|lazard", "ラザード"),
    (r"野村アセット", "野村アセットマネジメント"),
    (r"日興アセット", "日興アセットマネジメント"),
    (r"三井住友ds|三井住友ディーエス", "三井住友DSアセット"),
    (r"三井住友トラスト・?アセット", "三井住友トラスト・アセット"),
    (r"大和アセット", "大和アセットマネジメント"),
    (r"アセットマネジメントone", "アセットマネジメントOne"),
    (r"ニッセイアセット", "ニッセイアセット"),
    (r"りそなアセット", "りそなアセット"),
    (r"東京海上アセット", "東京海上アセット"),
    (r"三菱ufjアセット", "三菱UFJアセット"),
    (r"岡三アセット", "岡三アセット"),
    (r"レオス", "レオス(ひふみ)"),
    (r"さわかみ", "さわかみ投信"),
    (r"スパークス|sparx", "スパークス"),
]
ACTIVIST_RE = re.compile(r"オアシス|oasis|ストラテジックキャピタル|エフィッシモ|effissimo|3d ?インベストメント|3d investment|ダルトン|dalton|"
                         r"エリオット|elliott|バリューアクト|valueact|シティインデックス|レノ株式会社|村上|ニッポン・?アクティブ|"
                         r"パリサー|palliser|アセット・?バリュー・?インベスターズ|asset value investors|ファラロン|farallon|カタリスト|catalyst")
BROKER_RE = re.compile(r"証券|證券|securities|セキュリティーズ|goldman sachs international|ゴールドマン・サックス・インターナショナル|"
                       r"\bubs ag\b|バークレイズ|barclays|bnp ?パリバ|bnp paribas|citigroup global|シティグループ・グローバル|merrill|メリルリンチ|hsbc")
TRUST_RE = re.compile(r"信託銀行|trust bank|マスタートラスト|カストディ")
MANAGER_RE = re.compile(r"アセット|asset|投信|投資顧問|インベストメント|investment|キャピタル|capital|ファンド|fund|"
                        r"パートナーズ|partners|アドバイザー|advis|マネジメント|management|\bl\.?p\.?\b|\bllc\b")
CORP_RE = re.compile(r"株式会社|有限会社|合同会社|合資会社|合名会社|会社|法人|\bltd|\bllc|\binc\b|\bcorp|\bco\.|\bl\.?p\b|\bplc\b|limited|company|"
                     r"ファンド|fund|\bg\.?k\b|\bk\.?k\b|銀行|bank|信託|組合|トラスト|trust|パートナーズ|partners")
MANAGER_PURPOSE_RE = re.compile(r"運用|投資一任|信託財産|投資顧問")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").lower().replace("　", " ").strip()


def read_key() -> str | None:
    env = os.environ.get(KEY_ENV, "").strip()
    if env:
        return env
    for p in KEY_FILES:
        try:
            k = Path(p).read_text(encoding="utf-8").strip()
            if k:
                return k
        except (FileNotFoundError, OSError):
            continue
    return None


def _fetch(url: str, timeout: int = 60, retries: int = 2) -> bytes:
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "matrix-quant/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise PermissionError("EDINET APIキーが認証されませんでした(401/403)") from e
            if e.code == 404:
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"EDINET通信失敗: {last}")


# ---- 1. EDINETコード → 証券コード ----
def parse_code_csv(text: str) -> dict:
    """EdinetcodeDlInfo.csv(1行目=ダウンロード情報、2行目=見出し)を {EDINETコード: {"sym", "name"}} にする。上場銘柄のみ。"""
    rows = list(csv.reader(io.StringIO(text)))
    out = {}
    for r in rows[2:]:
        if len(r) > 11 and r[0].strip() and r[11].strip():
            sec = r[11].strip()
            if len(sec) >= 4:
                out[r[0].strip()] = {"sym": sec[:4] + ".T", "name": r[6].strip().replace("　", " ")}
    return out


def load_code_map(path=CODES_PATH, max_age_days: int = CODES_MAX_AGE_DAYS, fetch=_fetch) -> dict:
    cached = load_json_safe(path, {})
    if cached.get("map") and time.time() - cached.get("fetched_at", 0) < max_age_days * 86400:
        return cached["map"]
    try:
        raw = fetch(CODELIST_URL)
        z = zipfile.ZipFile(io.BytesIO(raw))
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        m = parse_code_csv(z.read(name).decode("cp932", errors="replace"))
        if m:
            if len(m) > 1000:   # 上場企業は約3,800社。極端に少ないときは取得不良とみなして保存しない
                atomic_write_json(path, {"fetched_at": time.time(), "map": m})
            return m
    except Exception:
        pass
    return cached.get("map") or {}


# ---- 2. 書類一覧 ----
def list_day(key: str, day: date, lists_dir=LISTS_DIR, fetch=_fetch, today: date | None = None) -> list:
    """その日に提出された書類の一覧(大量保有関連だけを残して日ごとにキャッシュ)。過去日は不変なので再取得しない。"""
    today = today or date.today()
    p = Path(lists_dir) / f"{day.isoformat()}.json"
    cached = load_json_safe(p, None)
    if cached is not None and (day < today or time.time() - cached.get("fetched_at", 0) < 3600):
        return cached.get("docs", [])
    url = f"{BASE}/documents.json?date={day.isoformat()}&type=2&Subscription-Key={urllib.parse.quote(key)}"
    j = json.loads(fetch(url).decode("utf-8"))
    docs = [d for d in (j.get("results") or []) if d.get("docTypeCode") in ("350", "360")]
    Path(lists_dir).mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, {"fetched_at": time.time(), "docs": docs})
    return docs


def select_reports(docs: list, code_map: dict, watch: dict) -> list:
    """監視銘柄(発行者)についての 大量保有報告書・変更報告書 だけを (doc, symbol) で返す。"""
    out = []
    for d in docs:
        if d.get("docTypeCode") not in DOC_TYPES or d.get("withdrawalStatus") == "1":
            continue
        m = code_map.get(d.get("issuerEdinetCode") or "")
        if m and m["sym"] in watch:
            out.append((d, m["sym"]))
    return out


# ---- 3. 書類の中身 ----
def _num(v):
    v = (v or "").strip().replace(",", "")
    if not v or v in ("－", "-", "―"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_report_rows(rows: list) -> dict:
    """XBRL由来CSVの行(要素ID, 項目名, コンテキストID, ..., 値)から要点を抜く。
    共同保有者がいる書類は合計行(コンテキストID "FilingDateInstant")を持ち、保有者ごとの行は接尾辞つき。
    提出者1者だけの書類は合計行が無く保有者行だけなので、その場合は保有者行の合計を使う。"""
    total, per_ctx = {}, {}
    for r in rows:
        if len(r) < 9:
            continue
        el, ctx, val = r[0].strip(), r[2].strip(), r[8].strip()
        key = el.split(":")[-1]
        if ctx == "FilingDateInstant":
            total[key] = val
        else:
            per_ctx.setdefault(ctx, {})[key] = val
    holders, seen = [], set()
    for ctx, d in per_ctx.items():
        name = (d.get("Name") or d.get("FilerNameInJapaneseDEI") or "").replace("　", " ").strip()
        if not name or name in ("－", "-") or name in seen:
            continue
        seen.add(name)
        purpose = (d.get("PurposeOfHolding") or "").replace("　", " ").strip()
        holders.append({"name": name, "purpose": "" if purpose in ("－", "-") else purpose,
                        "ratio": _num(d.get("HoldingRatioOfShareCertificatesEtc")),
                        "prev_ratio": _num(d.get("HoldingRatioOfShareCertificatesEtcPerLastReport"))})
    ratio = _num(total.get("HoldingRatioOfShareCertificatesEtc"))
    prev = _num(total.get("HoldingRatioOfShareCertificatesEtcPerLastReport"))
    ratio_from = "total"
    if ratio is None and any(h["ratio"] is not None for h in holders):
        ratio = round(sum(h["ratio"] or 0 for h in holders), 6)
        prev = round(sum(h["prev_ratio"] or 0 for h in holders), 6) if any(h["prev_ratio"] is not None for h in holders) else None
        ratio_from = "holders"
    filer = total.get("FilerNameInJapaneseDEI") or total.get("NameCoverPage") or (holders[0]["name"] if holders else "")
    return {
        "holder": filer.replace("　", " ").strip(),
        "holders": [h["name"] for h in holders],
        "holders_detail": holders,
        "n_holders": _num(total.get("TotalNumberOfFilersAndJointHoldersCoverPage")),
        "ratio": ratio, "prev_ratio": prev, "ratio_from": ratio_from,
        "issuer_name": (total.get("NameOfIssuer") or "").replace("　", " ").strip(),
        "issuer_code": (total.get("SecurityCodeOfIssuer") or "").strip(),
        "base_date": total.get("BaseDate") or "",
        "arose_date": total.get("DateWhenFilingRequirementAroseCoverPage") or "",
        "purpose": " / ".join(dict.fromkeys(h["purpose"] for h in holders if h["purpose"]))[:160],
    }


def parse_report_zip(raw: bytes) -> dict:
    z = zipfile.ZipFile(io.BytesIO(raw))
    name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
    text = z.read(name).decode("utf-16", errors="replace")
    return parse_report_rows(list(csv.reader(io.StringIO(text), delimiter="\t")))


def fetch_report(key: str, doc_id: str, fetch=_fetch) -> dict:
    url = f"{BASE}/documents/{doc_id}?type=5&Subscription-Key={urllib.parse.quote(key)}"
    return parse_report_zip(fetch(url, timeout=90))


# ---- 分類 ----
def classify_holder(name: str, purpose: str = "") -> dict:
    """保有者の種類: manager(運用会社)/activist/broker(証券会社)/trust(信託)/company(事業会社)/individual。
    主要運用会社に該当すれば notable=True と表示名 label を付ける。"""
    n = _norm(name)
    for pat, label in NOTABLE_MANAGERS:
        if re.search(pat, n):
            return {"kind": "manager", "label": label, "notable": True}
    if ACTIVIST_RE.search(n):
        return {"kind": "activist", "label": None, "notable": False}
    if BROKER_RE.search(n):
        return {"kind": "broker", "label": None, "notable": False}
    if TRUST_RE.search(n):
        return {"kind": "trust", "label": None, "notable": False}
    if MANAGER_RE.search(n) or MANAGER_PURPOSE_RE.search(purpose or ""):
        return {"kind": "manager", "label": None, "notable": False}
    if CORP_RE.search(n):
        return {"kind": "company", "label": None, "notable": False}
    return {"kind": "individual", "label": None, "notable": False}


def event_from(doc: dict, symbol: str, name: str, parsed: dict) -> dict:
    desc = doc.get("docDescription") or ""
    kind = "新規" if desc.startswith("大量保有報告書") else "変更"
    ratio, prev = parsed.get("ratio"), parsed.get("prev_ratio")
    delta = (ratio - prev) if (ratio is not None and prev is not None) else None
    direction = "new" if kind == "新規" else ("up" if delta is not None and delta > 0.0005 else "down" if delta is not None and delta < -0.0005 else "flat")
    holder = parsed.get("holder") or (doc.get("filerName") or "").replace("　", " ")
    cls = classify_holder(holder, parsed.get("purpose", ""))
    purpose = parsed.get("purpose") or ""
    manager_ratio = None
    # 共同保有者の中に主要運用会社がいれば、それを表示名にし、その保有者の保有目的・保有割合を添える
    # (提出者が証券会社でも運用会社の保有なら拾う。野村證券が提出し野村アセットが共同保有者、など)
    details = parsed.get("holders_detail") or [{"name": n, "purpose": "", "ratio": None} for n in (parsed.get("holders") or [])]
    if not cls["notable"]:
        for h in details:
            c2 = classify_holder(h["name"], h.get("purpose", ""))
            if c2["notable"]:
                cls = {**c2, "kind": "manager"}
                purpose = h.get("purpose") or purpose
                manager_ratio = h.get("ratio")
                break
    else:
        for h in details:
            if _norm(h["name"]) == _norm(holder):
                purpose = h.get("purpose") or purpose
                break
    submit = doc.get("submitDateTime") or ""
    return {
        "doc_id": doc.get("docID"), "date": submit[:10], "time": submit[11:16],
        "symbol": symbol, "name": name, "issuer_name": parsed.get("issuer_name") or "",
        "holder": holder, "holders": parsed.get("holders") or [], "n_holders": parsed.get("n_holders"),
        "holder_kind": cls["kind"], "manager": cls["label"], "notable": cls["notable"],
        "kind": kind, "special": "特例" in desc, "short_transfer": "短期大量譲渡" in desc,
        "direction": direction, "ratio": ratio, "prev_ratio": prev, "delta": delta,
        "ratio_from": parsed.get("ratio_from") or "total", "manager_ratio": manager_ratio,
        "purpose": purpose, "base_date": parsed.get("base_date") or "",
        "arose_date": parsed.get("arose_date") or "", "url": VIEWER_URL.format(doc_id=doc.get("docID")),
    }


# ---- 一括処理 ----
def build_edinet(symbols: list, names: dict, path=EDINET_PATH, max_age_hours: int = CACHE_HOURS,
                 days: int = WINDOW_DAYS, key: str | None = None, fetch=_fetch, today: date | None = None,
                 docs_cache_path=DOCS_CACHE_PATH, lists_dir=LISTS_DIR, codes_path=CODES_PATH) -> dict:
    """監視銘柄についての大量保有報告を集めて data/edinet.json を更新する。失敗しても例外は投げず前回分を返す。"""
    existing = load_json_safe(path, {})
    key = key or read_key()
    if not key:
        return {**existing, "key_present": False} if existing else {"key_present": False, "events": []}
    fresh = time.time() - existing.get("fetched_at", 0) < max_age_hours * 3600
    if fresh and existing.get("events") is not None and set(symbols) <= set(existing.get("symbols") or []):
        return existing

    today = today or date.today()
    watch = {s: names.get(s) or s for s in symbols}
    errors = []
    code_map = load_code_map(codes_path, fetch=fetch)
    if not code_map:
        errors.append("EDINETコードリストを取得できませんでした")
        return {**existing, "errors": errors, "key_present": True} if existing else {"key_present": True, "events": [], "errors": errors}

    candidates = []
    for i in range(days):
        day = today - timedelta(days=i)
        if day.weekday() >= 5:
            continue
        try:
            candidates += select_reports(list_day(key, day, lists_dir, fetch, today), code_map, watch)
        except PermissionError as e:
            errors.append(str(e))
            break
        except Exception as e:
            errors.append(f"{day.isoformat()} の書類一覧: {e.__class__.__name__}")

    docs_cache = load_json_safe(docs_cache_path, {})
    parsed_docs = docs_cache.get("docs", {})
    fetched = 0
    for doc, sym in candidates:
        did = doc.get("docID")
        if not did or did in parsed_docs:
            continue
        if fetched >= MAX_DOCS_PER_RUN:
            errors.append(f"書類の読込を{MAX_DOCS_PER_RUN}件で打ち切り(次回の更新で続き)")
            break
        try:
            parsed_docs[did] = fetch_report(key, did, fetch)
            fetched += 1
            time.sleep(DOC_SLEEP_S)
        except Exception as e:
            errors.append(f"{did}: {e.__class__.__name__}")
    if fetched:
        keep_from = (today - timedelta(days=days + 30)).isoformat()
        # 古い書類のキャッシュは落とす(候補に無い=期間外のもの)
        alive = {d.get("docID") for d, _ in candidates}
        parsed_docs = {k: v for k, v in parsed_docs.items() if k in alive or (v.get("base_date") or "9999") >= keep_from}
        atomic_write_json(docs_cache_path, {"updated_at": time.time(), "docs": parsed_docs})

    events = []
    for doc, sym in candidates:
        p = parsed_docs.get(doc.get("docID"))
        if not p:
            continue
        events.append(event_from(doc, sym, watch[sym], p))
    events.sort(key=lambda e: (e["date"], e["time"]), reverse=True)
    doc = {
        "fetched_at": time.time(), "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "days": days, "since": (today - timedelta(days=days)).isoformat(), "key_present": True,
        "symbols": sorted(symbols), "n_candidates": len(candidates), "n_events": len(events),
        "n_notable": sum(1 for e in events if e["notable"]), "events": events, "errors": errors[:10],
    }
    atomic_write_json(path, doc, indent=1)
    return doc


def load_edinet(path=EDINET_PATH) -> dict:
    return load_json_safe(path, {})
