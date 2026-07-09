"""ファイル入出力の安全化ユーティリティ。

- atomic_write_json: 一時ファイルに書いてから os.replace で差し替える。
  書き込み途中のクラッシュや二重実行でJSONが半端に壊れるのを防ぐ。
- load_json_safe: 壊れたJSONを読んだら .bak に退避して既定値で復旧する。
  「ファイルが壊れて起動不能」を絶対に起こさないための最後の砦。
"""
import json
import os
from pathlib import Path


def atomic_write_json(path, obj, **dumps_kwargs):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, **dumps_kwargs),
                   encoding="utf-8")
    os.replace(tmp, p)


def load_json_safe(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        # 壊れたファイルは退避して既定値で続行(データは.bakに残す)
        try:
            os.replace(p, p.with_suffix(p.suffix + ".bak"))
        except OSError:
            pass
        return default
