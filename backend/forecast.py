"""Google TimesFM 2.5 による株価予測のオーケストレーター。

TimesFM本体(torch)はmatrix-quant本体のvenvには入れず、隔離venv
(~/.timesfm-forecast/.venv)でサブプロセスとして実行する(scripts/timesfm_runner.py)。

設計原則: 予測が失敗してもREFRESH(市場スナップショット生成)は絶対に失敗させない。
サブプロセスが落ちても・TimesFM未導入でも、前回の forecast.json を維持して返す。
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from backend.util import atomic_write_json, load_json_safe

ROOT = Path(__file__).resolve().parent.parent
FORECAST_PATH = ROOT / "data" / "forecast.json"
TIMESFM_PY = Path.home() / ".timesfm-forecast" / ".venv" / "bin" / "python"
RUNNER = ROOT / "scripts" / "timesfm_runner.py"
HORIZON = 30
MAX_AGE_HOURS = 20
TIMEOUT_S = 300

MIN_POINTS = 200  # モデルの最低限の文脈として必要な点数


def build_forecast(closes_by_symbol: dict, path=FORECAST_PATH, python=TIMESFM_PY,
                   max_age_hours=MAX_AGE_HOURS):
    """closes_by_symbol(値はpandas Series、index=日付)からTimesFMで予測しforecast.jsonを更新する。

    戻り値: (doc, note) のタプル。noteは呼び出し側がforecast_metaに使う一言(文書には保存しない)。
    どんな失敗経路でも例外は投げない(前回のdocを返す)。
    """
    existing = load_json_safe(path, {})

    # 200点未満はモデルの最低限の文脈にも満たないため対象から外す
    inputs = {}
    for sym, s in closes_by_symbol.items():
        s = s.dropna()
        if len(s) < MIN_POINTS:
            continue
        inputs[sym] = s.tail(1024)

    if not inputs:
        return existing, "予測対象銘柄がありません(データ不足)"

    # スキップ判定: 既存データが十分新しく、対象銘柄が「前回試行した銘柄集合」に
    # 含まれるなら再計算しない。dataのキーではなくattemptedと比べるのは、
    # NaN等で除外された銘柄があっても毎回再計算にならないようにするため
    generated_at = existing.get("generated_at")
    existing_syms = set(existing.get("attempted") or existing.get("data", {}).keys())
    if generated_at and set(inputs.keys()) <= existing_syms:
        age_hours = (time.time() - generated_at) / 3600
        if age_hours <= max_age_hours:
            return existing, f"スキップ({int(max_age_hours)}時間以内に生成済み)"

    if not Path(python).exists():
        return existing, "TimesFM未導入のため予測なし(investmentスキルのsetup.shで導入可)"

    payload = {sym: [round(float(v), 6) for v in s.tolist()] for sym, s in inputs.items()}

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = Path(tmpdir) / "in.json"
        out_path = Path(tmpdir) / "out.json"
        in_path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            result = subprocess.run(
                [str(python), str(RUNNER), str(in_path), str(out_path), str(HORIZON)],
                timeout=TIMEOUT_S, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            print("[forecast] timesfm_runnerがタイムアウトしました", file=sys.stderr)
            return existing, "予測の更新に失敗(前回分を維持)"
        if result.returncode != 0:
            print(f"[forecast] timesfm_runner失敗(exit={result.returncode}): "
                 f"{result.stderr[-2000:]}", file=sys.stderr)
            return existing, "予測の更新に失敗(前回分を維持)"
        try:
            raw = json.loads(out_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[forecast] 出力JSONの読込に失敗: {e}", file=sys.stderr)
            return existing, "予測の更新に失敗(前回分を維持)"

    data = {}
    for sym, vals in raw.items():
        if sym not in inputs:
            continue
        s = inputs[sym]
        anchor_date = s.index[-1]
        anchor_date_str = (anchor_date.strftime("%Y-%m-%d")
                           if hasattr(anchor_date, "strftime") else str(anchor_date))
        data[sym] = {**vals, "anchor_date": anchor_date_str,
                     "anchor_close": float(s.iloc[-1])}

    doc = {"generated_at": time.time(), "horizon": HORIZON,
           "model": "google/timesfm-2.5-200m-pytorch",
           "attempted": sorted(inputs.keys()), "data": data}
    atomic_write_json(path, doc, indent=1)
    return doc, f"予測を更新({len(data)}銘柄)"


def load_forecast(path=FORECAST_PATH) -> dict:
    return load_json_safe(path, {})
