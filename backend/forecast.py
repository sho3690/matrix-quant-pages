"""株価予測のオーケストレーター: Google TimesFM(主、既定 3.0。config.json の timesfm_model で "2.5" に戻せる)
+ Amazon Chronos-2(第2の意見。config.json の forecast_second_model を "none" にすると省略)。

TimesFM本体(mlx/torch)はmatrix-quant本体のvenvには入れず、隔離venv
(~/.timesfm-forecast/.venv)でサブプロセスとして実行する(scripts/timesfm_runner.py)。
※ TimesFM 3.0 の重みは非商用・非本番限定ライセンス(2.5 は Apache-2.0)。個人利用の範囲で使う。

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
RUNNER2 = ROOT / "scripts" / "chronos_runner.py"   # 第2の意見(Chronos-2)。失敗しても主予測は保存する
CONFIG_PATH = ROOT / "config.json"
MODEL_NAMES = {"3.0": "google/timesfm-3.0-pytorch", "2.5": "google/timesfm-2.5-200m-pytorch"}


def second_model_choice() -> str:
    """config.json の forecast_second_model("chronos-2"|"none")。未設定は "chronos-2"。"""
    v = str(load_json_safe(CONFIG_PATH, {}).get("forecast_second_model", "chronos-2"))
    return v if v in ("chronos-2", "none") else "chronos-2"


def _run_runner(python, runner, in_path: Path, out_path: Path, args, label: str):
    """ランナーをサブプロセスで実行し、出力JSON(dict)を返す。失敗はstderrに記録してNone。"""
    try:
        result = subprocess.run(
            [str(python), str(runner), str(in_path), str(out_path), str(HORIZON), *args],
            timeout=TIMEOUT_S, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        print(f"[forecast] {label}がタイムアウトしました", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"[forecast] {label}失敗(exit={result.returncode}): "
              f"{result.stderr[-2000:]}", file=sys.stderr)
        return None
    try:
        return json.loads(out_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[forecast] {label}の出力JSONの読込に失敗: {e}", file=sys.stderr)
        return None


def model_choice() -> str:
    """config.json の timesfm_model("3.0"|"2.5")。無効値や未設定は "3.0"。"""
    v = str(load_json_safe(CONFIG_PATH, {}).get("timesfm_model", "3.0"))
    return v if v in MODEL_NAMES else "3.0"
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
        in_path.write_text(json.dumps(payload), encoding="utf-8")
        raw = _run_runner(python, RUNNER, in_path, Path(tmpdir) / "out.json",
                          [model_choice()], "timesfm_runner")
        if raw is None:
            return existing, "予測の更新に失敗(前回分を維持)"
        # 第2の意見(Chronos-2)。無くても主予測は保存する
        raw2 = None
        if second_model_choice() != "none":
            raw2 = _run_runner(python, RUNNER2, in_path, Path(tmpdir) / "out2.json",
                               [], "chronos_runner")

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
        if raw2 and isinstance(raw2.get(sym), dict):
            data[sym]["chronos"] = raw2[sym]

    # ランナーが実際に使ったモデル名("_model")を記録する(3.0が使えず2.5に落ちた場合もそのまま分かる)
    doc = {"generated_at": time.time(), "horizon": HORIZON,
           "model": raw.get("_model") or MODEL_NAMES[model_choice()],
           "model2": (raw2 or {}).get("_model"),   # 第2の意見が取れなかったときは None
           "attempted": sorted(inputs.keys()), "data": data}
    atomic_write_json(path, doc, indent=1)
    return doc, f"予測を更新({len(data)}銘柄)"


def load_forecast(path=FORECAST_PATH) -> dict:
    return load_json_safe(path, {})
