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

# ★ chg（官方漲跌價差）加在**最後面**。
# app.js 是按索引讀 price_fields 的，加在中間會把既有欄位整排錯位 ——
# 那種錯不會報錯，只會讓畫面上的數字靜悄悄全部變成隔壁欄的值。
PRICE_FIELDS = ["stock_id", "open", "high", "low", "close",
                "volume", "amount", "trades", "chg"]
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
            # ★ 舊的月檔只有 8 欄（沒有 chg）。用 len(r) 判斷，
            #   不要假設長度 —— repo 裡已經有好幾個月的舊格式檔案。
            db.upsert_price(conn, [
                (r[0], date, r[1], r[2], r[3], r[4], int(r[5] or 0),
                 r[6] or 0, int(r[7] or 0), "web",
                 r[8] if len(r) > 8 else None) for r in rows])
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
        for date, day in (obj.get("days") or {}).items():
            # ★ 辨識新舊格式：
            #   舊 {board: rows}            -> 全部當成 'all'
            #   新 {market: {board: rows}}  -> 照實載入
            #   'all'/'twse'/'tpex' 不會和榜單代號撞名，所以判斷是明確的。
            if set(day) & {"all", "twse", "tpex"}:
                per_market = day
            else:
                per_market = {"all": day}
            for mk, boards in per_market.items():
                for board, rows in boards.items():
                    conn.executemany(
                        "INSERT OR REPLACE INTO rank_daily "
                        "(date,board,market,stock_id,rank,metric) "
                        "VALUES (?,?,?,?,?,?)",
                        [(date, board, mk, r[0], r[1], r[2]) for r in rows])

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
            list(r) for r in conn.execute(
                "SELECT stock_id,open,high,low,close,volume,amount,trades,chg "
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
    """每月的榜單，**三個範圍各存一份**（全市場／上市／上櫃）。

    ★ 格式從 {date: {board: rows}} 變成 {date: {market: {board: rows}}}。
    舊檔案沒有 market 那一層，`load_into_db()` 會自動辨識並當成 'all'。

    ★ 為什麼歷史也要存 per-market
    這個 repo 的 SQLite 是 **:memory:**，每次執行都從 JSON 重建 ——
    沒寫進 JSON 的東西下次就不存在。per-market 的「連續在榜天數」
    需要 per-market 的歷史，不存就永遠是 1 天。

    ★ 體積
    月檔會變成約三倍（326 KB -> 約 1 MB）。這不影響網頁載入速度：
    瀏覽器只載 meta / latest / glossary / universe 四個檔，
    月檔是 Actions 重建資料庫時用的。
    """
    from core import ranking
    obj = {"ym": ym, "fields": BOARD_FIELDS, "store_n": ranking.STORE_N,
           "markets": list(ranking.MARKETS), "days": {}}
    for date, in conn.execute(
            "SELECT DISTINCT date FROM rank_daily WHERE date LIKE ? "
            "ORDER BY date", (ym + "%",)):
        day = {}
        for mk in ranking.MARKETS:
            per = {}
            for board in ranking.BOARDS:
                rows = [[r[0], r[1], r[2]] for r in conn.execute(
                    "SELECT stock_id,rank,metric FROM rank_daily "
                    "WHERE date=? AND board=? AND market=? ORDER BY rank",
                    (date, board, mk))]
                if rows:
                    per[board] = rows
            if per:
                day[mk] = per
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
    px = [list(r) for r in conn.execute(
        "SELECT stock_id,open,high,low,close,volume,amount,trades,chg "
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


def dump_markets(conn, date):
    """上市／上櫃各自的榜單，**獨立一個檔、延後載入**。

    ★ 為什麼不塞進 latest.json
    latest.json 已經 217 KB，首次載入（meta+latest+glossary+universe）
    約 359 KB，而 check_web 盯著 500 KB 的上限。
    把三個範圍都塞進去會直接超標 —— 首次載入量是這個網站少數幾個
    一旦變大就回不去的指標。

    所以 latest.json 維持只放全市場（前端契約不變），
    切到市場篩選時才載這個檔。和基本面、技術面同一個模式。
    """
    from core import ranking
    out, gone = {}, {}
    for mk in ("twse", "tpex"):
        b, g = {}, {}
        for board in ranking.BOARDS:
            rows, gn = ranking.marked(conn, board, date,
                                      top_n=ranking.STORE_N, market=mk)
            b[board] = [
                [r["stock_id"], r["rank"], r["metric"], r["streak"],
                 1 if r["is_new"] else 0, 1 if r["is_back"] else 0,
                 r["move"] if r["move"] is not None else None]
                for r in rows]
            g[board] = [[x["stock_id"], x["prev_rank"]] for x in gn]
        out[mk] = b
        gone[mk] = g
    return write(_p("markets.json"), {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": date,
        "board_fields": ["stock_id", "rank", "metric", "streak",
                         "is_new", "is_back", "move"],
        "market_zh": ranking.MARKET_ZH,
        "boards": out, "gone": gone,
        "note": "上市與上櫃各自排名。全市場榜單在 latest.json，"
                "這個檔只有切到市場篩選時才載入。",
    })


def dump_meta(conn, date, scheduled=None, started=None, notes=None,
              missing=None):
    """★ 時間戳三件套：排程時間、實際執行時間、資料日期。

    這樣就能測出 GitHub Actions 的實際延遲，以及官方資料幾點才更新 ——
    不用猜，讓它自己記錄。（1151022s-1 就是這樣測出平均延遲 3.1 小時的。）

    ★ 這份紀錄確實派上用場了：把歷史上每一次 commit 的 meta.json 排成
    時間軸，夾出官方資料是**隔天台北 05:30~06:00** 才更新，推翻了上線時
    「收盤後一兩小時」的假設，排程因此整個重排。

    ★ age_days 取代了原本的 same_day
    same_day 永遠是 False —— 因為官方隔天清晨才更新，當天抓到當天
    在物理上就不可能。一個恆為假的欄位沒有資訊量，還會讓人以為
    是暫時現象。改成「資料距今幾個日曆天」，這個數字變大才是真的有事。
    """
    from core import ranking
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM price_daily ORDER BY date")]
    age = None
    if date:
        try:
            age = (datetime.now().date()
                   - datetime.strptime(date, "%Y-%m-%d").date()).days
        except ValueError:
            age = None
    now = datetime.now(timezone.utc)
    q = lambda s: conn.execute(s).fetchone()[0]
    meta = {
        "data_date": date,
        "scheduled": scheduled,
        "run_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_at_tpe": now.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "same_day": (date == datetime.now().strftime("%Y-%m-%d")),
        "age_days": age,
        "missing": list(missing or []),
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


# ★ _ytd = 年初至今累計，不是單季。這不是命名潔癖：官方欄位叫
#   「基本每股盈餘」，不帶後綴的話 UI 上會被當成單季盈餘。
#   驗證過程見 core/fundamental.py 的 EPS_ITEMS。
EPS_FIELDS = ["stock_id", "eps_ytd", "revenue_ytd", "op_income_ytd",
              "non_op_ytd", "net_income_ytd"]
REV_FIELDS = ["stock_id", "revenue", "last_month", "last_year", "mom_pct",
              "yoy_pct", "acc", "acc_last_year", "acc_yoy_pct"]


def dump_fundamental(conn):
    """EPS、月營收、本益比匯出成獨立的一個檔。

    ★ 為什麼不塞進 latest.json
    榜單頁每次開啟都會載 latest.json，而基本面只有切到基本面頁才需要。
    合併會讓首次載入憑空多幾百 KB —— 首次載入量是這個網站少數幾個
    一旦變大就回不去的指標，check_web.py 有一條檢查盯著它。

    ★ 為什麼保留全部期別而不是只留最新一季
    「近四季 EPS」要 4 個季報。官方只給最新一季，補不回過去，
    所以從現在開始每一季都要留著 —— 丟掉就永遠拿不回來了。
    """
    from core import db
    eps = {}
    for d, sid, item, v in conn.execute(
            "SELECT date, stock_id, item, value FROM fundamental "
            "WHERE kind='ytd' ORDER BY date, stock_id"):
        eps.setdefault(d, {}).setdefault(sid, {})[item] = v
    eps_out = {d: [[sid] + [m.get(k) for k in EPS_FIELDS[1:]]
                   for sid, m in per.items()]
               for d, per in eps.items()}

    rev = {}
    for row in conn.execute(
            "SELECT ym, stock_id, revenue, last_month, last_year, mom_pct,"
            " yoy_pct, acc, acc_last_year, acc_yoy_pct FROM revenue_month "
            "ORDER BY ym, stock_id"):
        rev.setdefault(row[0], []).append(list(row[1:]))

    vd = db.last_trading_day(conn)
    val = [list(r) for r in conn.execute(
        "SELECT stock_id, per, pbr, yield_pct FROM valuation_daily "
        "WHERE date=? ORDER BY stock_id", (vd,))]

    return write(_p("fundamental.json"), {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "eps_fields": EPS_FIELDS, "eps": eps_out,
        "rev_fields": REV_FIELDS, "revenue": rev,
        "val_fields": VAL_FIELDS, "val_date": vd, "valuation": val,
        "units": {"eps": "元", "revenue": "千元", "acc": "千元",
                  "mom_pct": "%", "yoy_pct": "%", "acc_yoy_pct": "%"},
        "note": "金額單位為千元，與官方原始資料一致（未換算）。"
                "虧損公司沒有本益比，顯示為空白而不是 0。",
    })


def load_fundamental(conn):
    """把 fundamental.json 讀回 SQLite，讓歷史期別累積下去。"""
    from core import db
    obj = read(_p("fundamental.json"), default={})
    if not obj:
        return 0, 0
    n1 = 0
    for d, rows in (obj.get("eps") or {}).items():
        out = []
        for r in rows:
            for i, k in enumerate(EPS_FIELDS[1:], 1):
                if i < len(r) and r[i] is not None:
                    out.append((r[0], d, "ytd", k, r[i]))
        n1 += db.upsert_fundamental(conn, out)
    n2 = 0
    for ym, rows in (obj.get("revenue") or {}).items():
        n2 += db.upsert_revenue(
            conn, [(r[0], ym) + tuple(r[1:9]) for r in rows])
    return n1, n2


def dump_technical(conn, progress=None):
    """算出最新交易日的技術指標並匯出。

    ★ 獨立一個檔、延後載入
    和 fundamental.json 同樣的理由：榜單頁每次開啟都會載 latest.json，
    技術指標只有切到技術面頁才需要。

    ★ ready 一起存
    前端必須知道「這個指標現在算不算得出來」。資料不足時要顯示
    「還需要 N 天」而不是空白 —— 空白看起來像壞掉，而且使用者
    無從得知它什麼時候會有。
    """
    from core import db, technical as tc
    date, rows, ready = tc.compute(conn, allow=db.whitelist(conn),
                                   progress=progress)
    return write(_p("technical.json"), {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": date,
        "fields": tc.FIELDS,
        "zh": tc.ZH,
        "ready": ready,
        "rows": rows,
        "note": "價格為還原股價（已調整除權息），與看盤軟體上的成交價不同。"
                "資料不足的指標不顯示數字，改顯示還需要幾個交易日。",
    })


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
