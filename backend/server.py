"""MATRIX QUANT TERMINAL サーバー。ローカル/LAN内専用・実売買機能なし。"""
import os
import re
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend import market, pulse
from backend.paper import PaperBroker, validate_stock_qty

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "frontend" / "index.html"
TRADES_PATH = ROOT / "data" / "paper_trades.json"

app = FastAPI(title="MATRIX QUANT TERMINAL")

# 二重実行・同時書き込みからデータファイルを守るロック
# (PCとiPhoneから同時に操作しても記録が消えないように)
REFRESH_LOCK = threading.Lock()
TRADE_LOCK = threading.Lock()
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-^=]{1,15}$")


class TradeRequest(BaseModel):
    symbol: str
    side: str  # "buy" | "sell"
    jpy_amount: float | None = None
    qty: float | None = None


class SymbolRequest(BaseModel):
    symbol: str
    name: str | None = None


def _snapshot(force_refresh: bool = False) -> dict:
    snap = None if force_refresh else market.load_snapshot()
    if snap is None:
        snap = market.build_snapshot()
    return snap


def _prices_jpy(snap: dict) -> dict:
    return {a["symbol"]: a.get("price_jpy") for a in snap.get("assets", [])}


def _atrs(snap: dict) -> dict:
    return {a["symbol"]: a.get("atr_pct") for a in snap.get("assets", [])}


@app.get("/")
def index():
    # ブラウザに古い画面をキャッシュさせない(更新後に旧UIが表示される事故を防ぐ)
    return FileResponse(INDEX, headers={"Cache-Control": "no-store"})


@app.get("/apple-touch-icon.png")
def touch_icon():
    # iPhoneの「ホーム画面に追加」用アイコン
    return FileResponse(ROOT / "frontend" / "apple-touch-icon.png")


@app.get("/api/market")
def get_market():
    try:
        return _snapshot()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"市場データの取得に失敗しました: {e}")


@app.post("/api/refresh")
def refresh():
    if not REFRESH_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409,
                            detail="更新は実行中です。終わるまでお待ちください")
    try:
        snap = _snapshot(force_refresh=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"市場データの更新に失敗しました: {e}")
    finally:
        REFRESH_LOCK.release()
    try:
        pulse.build_pulse()  # 市況パルスも一緒に更新(失敗しても株価更新は返す)
    except Exception:
        pass
    return snap


@app.get("/api/pulse")
def get_pulse():
    cached = pulse.load_pulse()
    if cached is None:
        try:
            cached = pulse.build_pulse()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"市況データの取得に失敗しました: {e}")
    return cached


@app.get("/api/paper")
def get_paper():
    snap = market.load_snapshot() or {"assets": []}
    return PaperBroker(TRADES_PATH).portfolio(_prices_jpy(snap), atr_pct=_atrs(snap))


@app.post("/api/trade")
def trade(req: TradeRequest):
    snap = market.load_snapshot()
    if snap is None:
        raise HTTPException(status_code=409, detail="市場データ未取得です。先に更新してください")
    price = _prices_jpy(snap).get(req.symbol)
    asset = next((a for a in snap.get("assets", []) if a["symbol"] == req.symbol), {})
    # 売買は直列化(同時に2件来ても記録が消えない)
    if not TRADE_LOCK.acquire(timeout=5):
        raise HTTPException(status_code=409, detail="別の売買を処理中です。もう一度お試しください")
    try:
        return _execute_trade(req, snap, price, asset)
    finally:
        TRADE_LOCK.release()


def _execute_trade(req: TradeRequest, snap: dict, price, asset: dict):
    broker = PaperBroker(TRADES_PATH)
    try:
        if req.side == "buy":
            # 買った瞬間の2×ATR損切りラインを記録する(あとで割れ警告に使う)
            atr = asset.get("atr_pct")
            stop = price * (1 - 2 * atr) if (price and atr) else None
            if asset.get("asset_class") == "crypto":
                # 暗号資産は現実でも端数で買えるため金額指定
                if not req.jpy_amount:
                    raise ValueError("暗号資産の買いは金額(円)を指定してください")
                broker.buy(req.symbol, jpy_amount=req.jpy_amount, price_jpy=price,
                           stop_jpy=stop)
            else:
                # 株式は現実の売買単位に合わせて数量指定(日本株=100株単位)
                q = validate_stock_qty(asset.get("asset_class"), req.qty)
                broker.buy(req.symbol, qty=q, price_jpy=price, stop_jpy=stop)
        elif req.side == "sell":
            if not req.qty:
                raise ValueError("売りは数量を指定してください")
            broker.sell(req.symbol, req.qty, price)
        else:
            raise ValueError("side は buy か sell を指定してください")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return broker.portfolio(_prices_jpy(snap), atr_pct=_atrs(snap))


@app.post("/api/watchlist/add")
def add_symbol(req: SymbolRequest):
    sym = req.symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="銘柄コードを入力してください")
    if not SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400,
                            detail="銘柄コードに使えるのは英数字と . - ^ = (15文字まで)です")
    wl = market.load_watchlist()
    if any(a["symbol"] == sym for a in wl["assets"]):
        raise HTTPException(status_code=400, detail=f"「{sym}」は既に監視リストにあります")
    try:
        info = market.probe_symbol(sym)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    wl["assets"].append({"symbol": sym,
                         "name": (req.name or "").strip() or info["name"] or sym,
                         "asset_class": info["asset_class"],
                         "currency": info["currency"]})
    market.save_watchlist(wl)
    return market.build_snapshot()


@app.post("/api/watchlist/remove")
def remove_symbol(req: SymbolRequest):
    sym = req.symbol.strip().upper()
    positions = PaperBroker(TRADES_PATH).portfolio({}).get("positions", [])
    if any(p["symbol"] == sym for p in positions):
        raise HTTPException(status_code=400,
                            detail=f"「{sym}」は仮想ポジションを保有中のため外せません。先に売却してください")
    wl = market.load_watchlist()
    remaining = [a for a in wl["assets"] if a["symbol"] != sym]
    if len(remaining) == len(wl["assets"]):
        raise HTTPException(status_code=404, detail=f"「{sym}」は監視リストにありません")
    if not remaining:
        raise HTTPException(status_code=400, detail="最後の1銘柄は削除できません")
    wl["assets"] = remaining
    market.save_watchlist(wl)
    return market.build_snapshot()


@app.post("/api/shutdown")
def shutdown():
    """UIの[終了]ボタン用。localhost限定サーバーなので外部からは呼べない。"""
    def _exit():
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()
    return {"ok": True}
