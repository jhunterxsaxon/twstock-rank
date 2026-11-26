# -*- coding: utf-8 -*-
"""排行榜與在榜標記。

**這支刻意只吃資料庫、不碰網路** —— 因為同一段程式要服務兩件事：
  1. 每天算今天的榜單（階段 1）
  2. 回溯算過去 5 年每一個交易日的榜單（階段 2）
如果把抓取混在這裡，第 2 件事就做不到了。

## 榜單定義

  volume     成交股數前 100
  amount     成交金額前 100
  swing      當日振幅前 100 =（最高−最低）÷ **前一交易日收盤**，有流動性門檻
  chip_buy   三大法人買超金額前 50（池內排名）
  chip_sell  三大法人賣超金額前 50（池內排名）

★ `chip_sell` 是我多加的 —— 同一份資料、同一段程式，幾乎零成本，
  而且風險面比買超更該看。不要的話刪掉 BOARDS 裡那一行即可。

## ★ 振幅用「前一交易日收盤」當分母

這是台股標準算法，好處是能直接對照 ±10% 的漲跌幅限制：
「振幅 9.8%」= 今天幾乎走完整個漲跌空間。用開盤或收盤當分母會失去這個參照點。

## ★ 振幅榜一定要有流動性門檻

一檔成交 10 張的雞蛋水餃股，隨便一筆就振幅 9% —— 那是流動性問題不是行情。
不設門檻的話整張榜會被冷門股佔滿，完全沒有參考價值。

## ★ 在榜標記一律即時推算，不存欄位

連續天數／新進榜／回榜／掉榜全部由 `rank_daily` 的快照推出來。
存成欄位的話，只要補抓一次歷史或漏跑一天就會對不起來，而且永遠修不回去。

## ★ 「連續」按交易日算，不是日曆日

交易日清單直接取 `rank_daily` / `price_daily` 的 `DISTINCT date`，
所以週五在榜、下週一也在榜 = 連續 2 天，不會因為隔了週末就斷掉。
不需要維護行事曆，而且永遠不會跟實際資料不一致。
"""

BOARDS = ["volume", "amount", "change_up", "change_down", "swing",
          "chip_buy", "chip_sell"]

# ★★ 儲存深度與顯示深度是兩回事 ★★
#
# STORE_N  資料庫一律存前幾名。要大一點，因為使用者隨時可能想看更深。
# TOP_N    介面的**預設**顯示名次。使用者可以自己改。
#
# 為什麼要分開：「連續在前 100 名 5 天」和「連續在前 50 名 5 天」
# 是兩件不同的事。使用者改顯示名次時，連續天數／新進榜／掉榜全部要跟著重算，
# 不能只是把表格砍短。所以資料庫要存得夠深，讓顯示層可以任意切。
#
# 代價：rank_daily 大約變 3 倍（全市場後約 120 萬筆、60 MB），SQLite 無壓力。
STORE_N = 300

TOP_N = {
    "volume": 100,
    "amount": 100,
    "change_up": 100,
    "change_down": 100,
    "swing": 100,
    "chip_buy": 50,
    "chip_sell": 50,
}

#: 介面上提供的名次選項
TOP_CHOICES = [10, 20, 30, 50, 100, 150, 200, 300]

BOARD_ZH = {
    "volume": "成交量榜",
    "amount": "成交金額榜",
    "change_up": "漲幅榜",
    "change_down": "跌幅榜",
    "swing": "振幅榜",
    "chip_buy": "三大法人買超榜",
    "chip_sell": "三大法人賣超榜",
}

# ★★ 「指標值」到底是什麼 —— 單一來源 ★★
#
# rank_daily.metric 是該榜的排序值，但**每張榜是不同的東西**。
# 以前表頭就寫「指標值」：對七張榜都成立，可是對每一張都沒說清楚。
#
# ★ 為什麼放在這裡而不是 UI
# 同一件事原本列舉在三個地方 —— ui/rank_page.py 的 UNIT、
# core/export.py 的 _fmt_metric、網頁 app.js 的 UNIT。
# 三份各自列舉七張榜，新增榜單時漏掉任何一份都**不會報錯**，
# 只會讓那一欄顯示成未換算的原始值。
# 現在三邊都讀這一份（網頁透過 meta.json 的 metric_zh）。
#
# 格式：board -> (欄名, 單位)。單位要讓 money() 認得（張／億／%／元）。
METRIC_ZH = {
    "volume": ("成交量", "張"),
    "amount": ("成交金額", "億"),
    "change_up": ("漲幅", "%"),
    "change_down": ("跌幅", "%"),
    "swing": ("振幅", "%"),
    "chip_buy": ("買超金額", "億"),
    "chip_sell": ("賣超金額", "億"),
}

#: 每張榜對應的名詞說明 key。
#  ★ 以前 export.py 用 dict.get(board, "board.chip") 取這個值，
#    於是漲幅榜／跌幅榜的 CSV 標頭寫成「三大法人榜」的說明 ——
#    漏接的 default 把兩張榜靜靜接到錯的地方。列全就不會有 default。
BOARD_GLOSSARY = {
    "volume": "board.volume",
    "amount": "board.amount",
    "change_up": "board.change",
    "change_down": "board.change",
    "swing": "board.swing",
    "chip_buy": "board.chip",
    "chip_sell": "board.chip",
}


def metric_label(board):
    """表頭文字，例如「成交量(張)」、「漲幅(%)」。"""
    name, unit = METRIC_ZH.get(board, ("指標值", ""))
    return f"{name}({unit})" if unit else name


def metric_unit(board):
    """該榜指標值的單位，給 money() 用。"""
    return METRIC_ZH.get(board, ("", ""))[1]


# 振幅榜的流動性門檻：成交金額至少 1,000 萬元
SWING_MIN_AMOUNT = 10_000_000

# ★ 漲跌幅榜需要官方漲跌價差（chg），而它是 2026-11-20 才開始存的欄位，
#   歷史資料沒有。覆蓋率不足時**整張榜留空**，不要出半套的榜。
#
#   空榜在畫面上是「沒有資料」，半套的榜是「看起來正常但少了一半」——
#   後者糟得多，因為沒有人會發現。
#
#   門檻訂 80%：正常情況下兩市幾乎每檔都有 chg（少數停牌股沒有），
#   實測一天只有單邊有資料時是 44%，會被正確擋下。
CHG_MIN_COVERAGE = 0.8


def build_date(conn, date, allow=None, swing_min_amount=SWING_MIN_AMOUNT):
    """算出某一個交易日的全部榜單並寫入 `rank_daily`。

    allow: 白名單集合；None 代表從資料庫讀。
    回傳 {board: 筆數}。
    """
    if allow is None:
        allow = _whitelist(conn)
    prev = conn.execute(
        "SELECT MAX(date) FROM price_daily WHERE date < ?", (date,)).fetchone()[0]

    mkt = _market_of(conn)
    px = {}
    for sid, o, h, lo, c, v, a, chg in conn.execute(
            "SELECT stock_id, open, high, low, close, volume, amount, chg "
            "FROM price_daily WHERE date=?", (date,)):
        if sid in allow:
            px[sid] = (o, h, lo, c, v or 0, a or 0, chg)

    prev_close = {}
    if prev:
        for sid, c in conn.execute(
                "SELECT stock_id, close FROM price_daily WHERE date=?", (prev,)):
            if sid in allow:
                prev_close[sid] = c

    out = {}

    # ---- 成交量榜 ----
    rows = [(sid, d[4]) for sid, d in px.items() if d[4] > 0]
    rows.sort(key=lambda x: -x[1])
    out["volume"] = _write(conn, date, "volume", rows, mkt)

    # ---- 成交金額榜 ----
    rows = [(sid, d[5]) for sid, d in px.items() if d[5] > 0]
    rows.sort(key=lambda x: -x[1])
    out["amount"] = _write(conn, date, "amount", rows, mkt)

    # ---- 漲幅榜／跌幅榜 ----
    # ★ 用官方的漲跌價差，不用 (收盤 − 前日收盤) 自己算。
    #   除權息當天價格會跳空，自己算會得到假性大跌，那幾檔會佔滿跌幅榜
    #   前幾名，而且數字看起來完全正常。官方這個值是對除權息參考價算的。
    # ★ 前一日收盤用 (收盤 − 漲跌價差) 反推，不從資料庫拿：
    #   這樣除權息當天也對，而且第一天上線就算得出來。
    # ★ 先看這一天的 chg 夠不夠。只有單邊市場有的話就不要出榜 ——
    #   「日期完整」不等於「這張榜需要的欄位完整」，這是第三種半套。
    have_chg = sum(1 for d in px.values() if d[6] is not None)
    enough_chg = bool(px) and have_chg >= len(px) * CHG_MIN_COVERAGE
    ups, downs = [], []
    for sid, (o, h, lo, c, v, a, chg) in (px.items() if enough_chg else ()):
        if chg is None or c is None or not a or a < swing_min_amount:
            continue                       # 沒有官方漲跌 or 流動性不足
        prev = c - chg
        if prev <= 0:
            continue
        pct = chg / prev * 100.0
        if pct > 0:
            ups.append((sid, pct))
        elif pct < 0:
            downs.append((sid, pct))
    ups.sort(key=lambda x: -x[1])
    # 跌幅榜由跌最多排到跌最少，所以是「由小到大」
    downs.sort(key=lambda x: x[1])
    out["change_up"] = _write(conn, date, "change_up", ups, mkt)
    out["change_down"] = _write(conn, date, "change_down", downs, mkt)

    # ---- 振幅榜 ----
    rows = []
    for sid, (o, h, lo, c, v, a, _chg) in px.items():
        pc = prev_close.get(sid)
        if not pc or pc <= 0:
            continue                       # 新上市首日沒有前收，跳過
        if h is None or lo is None or h <= lo:
            continue                       # 當日無成交（最高＝最低），跳過
        if a < swing_min_amount:
            continue                       # 流動性門檻
        rows.append((sid, (h - lo) / pc * 100.0))
    rows.sort(key=lambda x: -x[1])
    out["swing"] = _write(conn, date, "swing", rows, mkt)

    # ---- 三大法人榜（金額 = 買賣超股數 × 當日收盤） ----
    chips = []
    for sid, net in conn.execute(
            "SELECT stock_id, total_net FROM chip_daily WHERE date=?", (date,)):
        if sid not in allow or sid not in px or not net:
            continue
        c = px[sid][3]
        if not c:
            continue
        chips.append((sid, net * c))
    buy = sorted([x for x in chips if x[1] > 0], key=lambda x: -x[1])
    sell = sorted([x for x in chips if x[1] < 0], key=lambda x: x[1])
    out["chip_buy"] = _write(conn, date, "chip_buy", buy, mkt)
    out["chip_sell"] = _write(conn, date, "chip_sell", sell, mkt)

    return out


MARKETS = ("all", "twse", "tpex")
MARKET_ZH = {"all": "全市場", "twse": "上市", "tpex": "上櫃"}


def _market_of(conn):
    """{stock_id: 'twse'|'tpex'}。"""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT stock_id, market FROM stock")}


def _write(conn, date, board, rows, mkt=None):
    """寫入某張榜的三個範圍：全市場、上市、上櫃。

    ★ `rows` 必須是**已經排好序的全市場清單**。

    ★ 為什麼可以直接篩選，不用重新排序
    「過濾不改變順序」——把全市場的排序結果篩出單一市場再重新編號，
    **就等於該市場自己排名的結果**（排序依據是同一個 metric）。
    所以這裡不需要、也不應該另外寫一套排序邏輯：多一套就會分岔。

    ★ 為什麼不靠前端過濾
    全市場前 300 名裡上市佔 242 檔，但**上櫃只有 58 檔** ——
    前端拿 latest.json 過濾做不出上櫃前 100 名。
    """
    from core import db
    if mkt is None:
        mkt = _market_of(conn)
    n = db.replace_ranks(conn, date, board, rows[:STORE_N], market="all")
    for m in ("twse", "tpex"):
        db.replace_ranks(conn, date, board,
                         [r for r in rows if mkt.get(r[0]) == m][:STORE_N],
                         market=m)
    return n


def _whitelist(conn):
    return {r[0] for r in conn.execute("SELECT stock_id FROM stock")}


# ---------------- 在榜標記 ----------------

def board_days(conn, board, market="all"):
    """這張榜有資料的日期（升冪）。"""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM rank_daily WHERE board=? AND market=? "
        "ORDER BY date", (board, market))]


def marked(conn, board, date, top_n=None, market="all"):
    """某天某榜的完整內容，附標記。

    ★ `top_n` 是**顯示深度**。連續天數／新進榜／回榜／掉榜全部按這個深度
    重新判定 —— 「連續在前 50 名 5 天」和「連續在前 100 名 5 天」
    是兩件不同的事，不能只把表格砍短。
    None 代表用該榜的預設值。

    回傳 dict 清單，每筆含：
      stock_id, name, rank, metric,
      close     該交易日的收盤價（★ 不是現價；停牌或缺漏為 None）
      streak    連續在榜天數（含今天）
      is_new    今天第一次進榜（前一交易日不在）
      is_back   新進榜且歷史上曾經在過
      move      排名變化（正數=上升幾名）；新進榜為 None
    另外回傳「掉榜」清單。

    ★ 連續天數**不設上限**。
    之前的版本用 lookback=500 的視窗，結果真的連續 600 天的股票會被
    回報成 501 天 —— 一個會靜默報錯數字的 bug。
    現在改成把當日成員的完整在榜歷史一次撈進記憶體（一個 query），
    再沿著交易日往前走到底。

    ★ 「連續」按**交易日**算，不是日曆日 —— days 來自 rank_daily 的
    DISTINCT date，所以週末與國定假日自然不算斷。
    """
    n = int(top_n or TOP_N.get(board, 100))
    days = board_days(conn, board, market)
    if date not in days:
        return [], []
    i = days.index(date)

    today = dict(conn.execute(
        "SELECT stock_id, rank FROM rank_daily WHERE board=? AND date=? "
        "AND market=? AND rank <= ?", (board, date, market, n)))
    metrics = dict(conn.execute(
        "SELECT stock_id, metric FROM rank_daily WHERE board=? AND date=? "
        "AND market=? AND rank <= ?", (board, date, market, n)))
    # ★ 收盤價在這裡一起撈，不要讓每個呼叫端自己查 ——
    #   介面、CSV 匯出、網頁三邊各查一次就會有三種「缺漏時怎麼辦」。
    #   一個 query 撈全市場，成本可以忽略。
    closes = dict(conn.execute(
        "SELECT stock_id, close FROM price_daily WHERE date=?", (date,)))
    prev_day = days[i - 1] if i > 0 else None
    prev = dict(conn.execute(
        "SELECT stock_id, rank FROM rank_daily WHERE board=? AND date=? "
        "AND market=? AND rank <= ?",
        (board, prev_day, market, n))) if prev_day else {}

    # 當日成員的完整在榜歷史，一次撈完（避免逐檔 query，也避免視窗截斷）
    # ★ 這裡也要帶 rank <= n —— 否則「連續在榜」會算成「連續在前 300 名」，
    #   和使用者選的顯示深度對不上。
    members = set(today) | set(prev)
    on = {}
    if members:
        qs = ",".join("?" * len(members))
        for sid, d in conn.execute(
                f"SELECT stock_id, date FROM rank_daily WHERE board=? "
                f"AND market=? AND rank <= ? AND stock_id IN ({qs})",
                (board, market, n, *members)):
            on.setdefault(sid, set()).add(d)

    names = {r[0]: r[1] for r in conn.execute("SELECT stock_id, name FROM stock")}

    rows = []
    for sid, rk in sorted(today.items(), key=lambda x: x[1]):
        hist = on.get(sid, set())
        # 連續天數：沿交易日往前走到不在榜為止，沒有上限
        streak, j = 1, i - 1
        while j >= 0 and days[j] in hist:
            streak += 1
            j -= 1
        is_new = sid not in prev
        # 回榜：今天新進榜，而且在這段連續之前曾經在榜過
        is_back = False
        if is_new:
            start_of_run = days[j + 1]
            is_back = any(d < start_of_run for d in hist)
        rows.append({
            "stock_id": sid,
            "name": names.get(sid, ""),
            "rank": rk,
            "metric": metrics.get(sid),
            "close": closes.get(sid),
            "streak": streak,
            "is_new": is_new,
            "is_back": is_back,
            "move": (prev[sid] - rk) if sid in prev else None,
        })

    gone = [{"stock_id": sid, "name": names.get(sid, ""), "prev_rank": rk}
            for sid, rk in sorted(prev.items(), key=lambda x: x[1])
            if sid not in today]
    return rows, gone


def selectivity(conn, board):
    """★ 榜單的「篩選力」：上榜成員數 ÷ 當日有行情的股票數。

    為什麼需要這個檢查：如果資料庫裡只有 60 檔股票的歷史，
    那「成交量前 100 名」每天都等於「全部 60 檔」——
    `連續在榜 ≥ 5 天` 這個條件永遠成立，榜單策略會退化成
    「買進持有」。回測結果會非常漂亮，但那是假象，不是策略有效。

    回傳 dict：ratio（越接近 1 越沒有篩選力）、members、universe、ok。
    `ok=False` 時，所有 rank_* 規則的結論都不可信。
    """
    n = TOP_N.get(board, 100)
    r = conn.execute(
        # ★ 加了 market 維度之後，這裡一定要限定 'all'，
        #   否則三個範圍會被一起數進去，上榜比例直接變成三倍。
        "SELECT AVG(c) FROM (SELECT date, COUNT(*) c FROM rank_daily "
        "WHERE board=? AND market='all' AND rank <= ? GROUP BY date)",
        (board, n)).fetchone()
    members = r[0] if r and r[0] else 0
    r2 = conn.execute(
        "SELECT AVG(n) FROM (SELECT date, COUNT(*) n FROM price_daily "
        "WHERE volume > 0 GROUP BY date)").fetchone()
    universe = r2[0] if r2 and r2[0] else 0
    ratio = (members / universe) if universe else 1.0
    return {"board": board, "members": members, "universe": universe,
            "ratio": ratio, "ok": ratio < 0.6 and universe >= TOP_N[board] * 3}


def selectivity_warning(conn):
    """如果榜單沒有篩選力，回傳一段警告文字；正常則回 None。"""
    s = selectivity(conn, "volume")
    if s["ok"]:
        return None
    return (f"⚠ 榜單目前沒有篩選力：平均每天有行情的股票只有 "
            f"{s['universe']:.0f} 檔，而榜單取前 {TOP_N['volume']} 名 —— "
            f"上榜比例 {s['ratio'] * 100:.0f}%。\n"
            f"   這表示「連續在榜」這類條件幾乎永遠成立，"
            f"榜單策略會退化成「買進持有」，回測數字再漂亮也是假象。\n"
            f"   要讓榜單有意義，需要把階段 2 的價量歷史抓到接近全市場："
            f"python run_history.py --prices")


def streak_series(conn, board, stock_id, top_n=None, market="all"):
    """某檔在某榜的「連續在榜天數」時間序列，供指標與回測使用。

    回傳 {date: streak}。只在它在榜的日子有值。
    這就是 PROGRESS.md 說的「本專案獨有的榜單衍生指標」—— 一般回測框架
    沒有這種資料，因為它需要每天的全市場排名。

    ★ `top_n` 同 `marked()`：連續天數的定義取決於榜單深度。
    """
    n = int(top_n or TOP_N.get(board, 100))
    days = board_days(conn, board, market)
    on = {r[0] for r in conn.execute(
        "SELECT date FROM rank_daily WHERE board=? AND stock_id=? "
        "AND market=? AND rank <= ?", (board, stock_id, market, n))}
    out, run = {}, 0
    for d in days:
        if d in on:
            run += 1
            out[d] = run
        else:
            run = 0
    return out
