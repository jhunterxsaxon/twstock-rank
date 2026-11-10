# -*- coding: utf-8 -*-
"""JSON 存檔層：網頁版的「資料庫」。

## 為什麼用 JSON 而不是 SQLite

GitHub Actions 每次都是全新的容器，**狀態只能存在 repo 裡**。
SQLite 是二進位檔，git 每次都會整個換掉、diff 看不出來、體積越滾越大。
JSON 是純文字，git 只記差異，而且**網頁可以直接讀**（不需要後端）。

這是 1151022s-1 驗證過的做法。

## 但運算還是用 SQLite

JSON 是**存檔格式**，不是運算格式。每次執行：

    1. 讀 JSON → 灌進一個暫時的 SQLite（記憶體或暫存檔）
    2. 抓今天的官方資料 → 寫進 SQLite
    3. 用 **和本機版完全相同的 ranking.py** 算榜單
    4. 匯出 JSON → Actions commit

這樣榜單邏輯只有一份，網頁版和本機版不會算出不同的結果。
（1151022s → 1151022s-1 的教訓：邏輯寫兩份一定會分岔。）

## 檔案配置

    data/meta.json              時間戳、延遲、統計、資料來源標示
    data/universe.json          白名單（代號 → 名稱、市場、產業）
    data/latest.json            最新交易日的全市場快照
    data/prices/YYYY-MM.json    每月的全市場行情（給本機同步回來用）
    data/boards/YYYY-MM.json    每月的五張榜單（存到前 300 名）
    data/analysis/*.json        本機推上來的分析結果（不是這支產生的）

## 為什麼按月切檔

單一檔案會無限成長，瀏覽器載入會越來越慢 ——
1151022s-1 踩過，653 KB 的 JSON 就讓網頁變鈍。
按月切之後每個檔案大小封頂，網頁只載需要的月份。

## ★ 壓縮格式：陣列不是物件

    ✗ {"stock_id": "2330", "close": 1500, "volume": 12345}
    ✓ ["2330", 1500, 12345]

欄位名寫在檔頭一次就好，不要每一列重複 —— 體積差 3 倍以上。
"""
import json
import os
from datetime import datetime, timezone

DATA = "data"

PRICE_FIELDS = ["stock_id", "open", "high", "low", "close",
                "volume", "amount", "trades"]
MARGIN_FIELDS = ["stock_id", "mp_balance", "mp_prev", "ss_balance", "ss_prev"]
CHIP_FIELDS = ["stock_id", "foreign_net", "trust_net", "dealer_net", "total_net"]
VAL_FIELDS = ["stock_id", "per", "pbr", "yield_pct"]
BOARD_FIELDS = ["stock_id", "rank", "metric"]

SOURCE_CREDIT = [
    "臺灣證券交易所",
    "證券櫃檯買賣中心",
]
LICENSE_NOTE = (
    "本網站的行情、融資融券、三大法人、本益比等資料來自臺灣證券交易所與"
    "證券櫃檯買賣中心，依「政府資料開放授權條款第 1 版」使用。"
    "資料僅供參考，不構成投資建議。"
)


def _p(*parts):
    return os.path.join(DATA, *parts)


def read(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    return path


def month_of(date):
    return str(date)[:7]


# ---------------- 月檔 ----------------

def months(kind):
    """kind: 'prices' | 'boards'。回傳已有的月份清單（升冪）。"""
    d = _p(kind)
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))


def load_month(kind, ym):
    return read(_p(kind, ym + ".json"), default={})


def save_month(kind, ym, obj):
    return write(_p(kind, ym + ".json"), obj)


# ---------------- 匯入 SQLite ----------------

def load_into_db(conn, limit_months=None):
    """把 JSON 讀回 SQLite，讓 ranking.py 可以直接用。

    limit_months: 只載最近 N 個月（算榜單只需要最近的資料，
    但「連續在榜天數」需要往前追，所以預設全載）。
    回傳載入的交易日數。
    """
    from core import db

    uni = read(_p("universe.json"), default={})
    rows = [(sid, v[0], v[1], v[2], v[3], v[4])
            for sid, v in (uni.get("stocks") or {}).items()]
    if rows:
        db.upsert_stock(conn, rows)

    ms = months("prices")
    if limit_months:
        ms = ms[-limit_months:]
    days = set()
    for ym in ms:
        obj = load_month("prices", ym)
        for date, rows in (obj.get("days") or {}).items():
            days.add(date)
            db.upsert_price(conn, [
                (r[0], date, r[1], r[2], r[3], r[4], int(r[5] or 0),
                 r[6] or 0, int(r[7] or 0), "web") for r in rows])
            for r in (obj.get("margin") or {}).get(date, []):
                pass
        for date, rows in (obj.get("margin") or {}).items():
            db.upsert_margin(conn, [
                (r[0], date, r[1], r[2], r[3], r[4], "web") for r in rows])
        for date, rows in (obj.get("chip") or {}).items():
            db.upsert_chip(conn, [
                (r[0], date, r[1], r[2], r[3], r[4], "web") for r in rows])
        for date, rows in (obj.get("valuation") or {}).items():
            db.upsert_valuation(conn, [
                (r[0], date, r[1], r[2], r[3], "web") for r in rows])

    for ym in months("boards"):
        obj = load_month("boards", ym)
        for date, boards in (obj.get("days") or {}).items():
            for board, rows in boards.items():
                conn.executemany(
                    "INSERT OR REPLACE INTO rank_daily "
                    "(date,board,stock_id,rank,metric) VALUES (?,?,?,?,?)",
                    [(date, board, r[0], r[1], r[2]) for r in rows])

    bm = read(_p("benchmark.json"), default={})
    for code, series in (bm.get("series") or {}).items():
        conn.executemany(
            "INSERT OR REPLACE INTO benchmark_daily (code,date,close) "
            "VALUES (?,?,?)", [(code, d, v) for d, v in series.items()])

    conn.commit()
    return len(days)


# ---------------- 匯出 JSON ----------------

def dump_universe(conn):
    rows = {}
    for sid, name, market, industry, listed, updated in conn.execute(
            "SELECT stock_id,name,market,industry,listed_date,updated "
            "FROM stock ORDER BY stock_id"):
        rows[sid] = [name, market, industry, listed, updated]
    return write(_p("universe.json"),
                 {"fields": ["name", "market", "industry", "listed_date",
                             "updated"],
                  "count": len(rows), "stocks": rows})


def dump_month_prices(conn, ym):
    """把某個月的行情、融資券、法人、本益比寫成一個檔。"""
    obj = {"ym": ym,
           "price_fields": PRICE_FIELDS, "margin_fields": MARGIN_FIELDS,
           "chip_fields": CHIP_FIELDS, "valuation_fields": VAL_FIELDS,
           "days": {}, "margin": {}, "chip": {}, "valuation": {}}
    for date, in conn.execute(
            "SELECT DISTINCT date FROM price_daily WHERE date LIKE ? "
            "ORDER BY date", (ym + "%",)):
        obj["days"][date] = [
            [r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]]
            for r in conn.execute(
                "SELECT stock_id,open,high,low,close,volume,amount,trades "
                "FROM price_daily WHERE date=? ORDER BY stock_id", (date,))]
        m = [[r[0], r[1], r[2], r[3], r[4]] for r in conn.execute(
            "SELECT stock_id,mp_balance,mp_prev,ss_balance,ss_prev "
            "FROM margin_daily WHERE date=? ORDER BY stock_id", (date,))]
        if m:
            obj["margin"][date] = m
        c = [[r[0], r[1], r[2], r[3], r[4]] for r in conn.execute(
            "SELECT stock_id,foreign_net,trust_net,dealer_net,total_net "
            "FROM chip_daily WHERE date=? ORDER BY stock_id", (date,))]
        if c:
            obj["chip"][date] = c
        v = [[r[0], r[1], r[2], r[3]] for r in conn.execute(
            "SELECT stock_id,per,pbr,yield_pct FROM valuation_daily "
            "WHERE date=? ORDER BY stock_id", (date,))]
        if v:
            obj["valuation"][date] = v
    return save_month("prices", ym, obj)


def dump_month_boards(conn, ym):
    from core import ranking
    obj = {"ym": ym, "fields": BOARD_FIELDS, "store_n": ranking.STORE_N,
           "days": {}}
    for date, in conn.execute(
            "SELECT DISTINCT date FROM rank_daily WHERE date LIKE ? "
            "ORDER BY date", (ym + "%",)):
        day = {}
        for board in ranking.BOARDS:
            rows = [[r[0], r[1], r[2]] for r in conn.execute(
                "SELECT stock_id,rank,metric FROM rank_daily "
                "WHERE date=? AND board=? ORDER BY rank", (date, board))]
            if rows:
                day[board] = rows
        if day:
            obj["days"][date] = day
    return save_month("boards", ym, obj)


def dump_benchmark(conn):
    out = {}
    for code, date, close in conn.execute(
            "SELECT code,date,close FROM benchmark_daily ORDER BY code,date"):
        out.setdefault(code, {})[date] = close
    return write(_p("benchmark.json"), {"series": out})


def dump_latest(conn, date):
    """最新交易日的完整快照。網頁一進來就載這個檔。"""
    from core import ranking
    boards = {}
    marks = {}
    for board in ranking.BOARDS:
        rows, gone = ranking.marked(conn, board, date,
                                    top_n=ranking.STORE_N)
        boards[board] = [
            [r["stock_id"], r["rank"], r["metric"], r["streak"],
             1 if r["is_new"] else 0, 1 if r["is_back"] else 0,
             r["move"] if r["move"] is not None else None]
            for r in rows]
        marks[board] = [[g["stock_id"], g["prev_rank"]] for g in gone]
    px = [[r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]] for r in conn.execute(
        "SELECT stock_id,open,high,low,close,volume,amount,trades "
        "FROM price_daily WHERE date=? ORDER BY stock_id", (date,))]
    val = [[r[0], r[1], r[2], r[3]] for r in conn.execute(
        "SELECT stock_id,per,pbr,yield_pct FROM valuation_daily WHERE date=?",
        (date,))]
    return write(_p("latest.json"), {
        "date": date,
        "board_fields": ["stock_id", "rank", "metric", "streak",
                         "is_new", "is_back", "move"],
        "boards": boards, "gone": marks,
        "price_fields": PRICE_FIELDS, "prices": px,
        "valuation_fields": VAL_FIELDS, "valuations": val,
    })


def dump_meta(conn, date, scheduled=None, started=None, notes=None):
    """★ 時間戳三件套：排程時間、實際執行時間、資料日期。

    這樣就能測出 GitHub Actions 的實際延遲，以及官方資料幾點才更新 ——
    不用猜，讓它自己記錄。（1151022s-1 就是這樣測出平均延遲 3.1 小時的。）
    """
    from core import ranking
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM price_daily ORDER BY date")]
    lag = None
    if date and days:
        today = datetime.now().strftime("%Y-%m-%d")
        lag = sum(1 for d in days if d > date)  # 幾個交易日之前
        lag = 0 if date == today else None
    now = datetime.now(timezone.utc)
    q = lambda s: conn.execute(s).fetchone()[0]
    meta = {
        "data_date": date,
        "scheduled": scheduled,
        "run_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_at_tpe": now.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "same_day": (date == datetime.now().strftime("%Y-%m-%d")),
        "trading_days": len(days),
        "first_date": days[0] if days else None,
        "stocks": q("SELECT COUNT(*) FROM stock"),
        "price_rows": q("SELECT COUNT(*) FROM price_daily"),
        "board_days": q("SELECT COUNT(DISTINCT date) FROM rank_daily"),
        "store_n": ranking.STORE_N,
        "top_n": ranking.TOP_N,
        "boards": ranking.BOARDS,
        "board_zh": ranking.BOARD_ZH,
        "sources": SOURCE_CREDIT,
        "license": LICENSE_NOTE,
        "notes": notes or [],
    }
    return write(_p("meta.json"), meta)


def dump_glossary():
    """把名詞說明匯出成 JSON 給網頁用。

    ★ 單一來源：`core/glossary.py` 同時餵本機 GUI、匯出報告、和這個網頁。
    說明文字只寫一次，三邊永遠一致 —— 分成兩份遲早會分岔，
    而「網頁上的說明和程式裡的行為不一樣」比沒有說明更糟。
    """
    from core import glossary as gl
    return write(_p("glossary.json"),
                 {k: {"label": v["label"], "short": v["short"],
                      "full": v["full"]} for k, v in gl.G.items()})


def history_index():
    """給網頁用的月份索引，讓它知道有哪些月份可以載。"""
    return write(_p("index.json"), {
        "prices": months("prices"),
        "boards": months("boards"),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
