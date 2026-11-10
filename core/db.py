# -*- coding: utf-8 -*-
"""SQLite schema 與共用查詢。

## 幾個貫穿全專案的約定（違反的話以後一定出事）

**日期**一律西元 `YYYY-MM-DD`。官方 API 回民國年，在 `netio.roc_to_iso()` 就轉掉，
資料庫裡不存民國 —— 混用兩種紀年是遲早會踩到的雷。

**單位**：
  成交量 volume  → 股（不是張）
  成交金額 amount → 元
  融資融券       → 張（官方就是張，不轉）
  法人買賣超     → 股

**交易日**不另外維護行事曆，直接從 `price_daily` 的 `DISTINCT date` 推。
這樣永遠不會跟實際資料不一致 —— 有行情就是有開市，不需要別的真理來源。
（另外有 `holiday` 表，但那只用來交叉驗證與顯示，不參與計算。）

**榜單**只存每天的快照（`rank_daily`），
連續在榜天數／新進榜／回榜／掉榜**一律即時推算，不存欄位**。
存成欄位的話，只要補抓一次歷史或漏跑一天就會對不起來，而且永遠修不回去。
"""
import os
import shutil
import sqlite3
from datetime import datetime

SCHEMA = """
-- ============ 白名單：真正的「個股」 ============
-- ETF／權證／特別股／TDR 不會進這張表，見 universe.py
CREATE TABLE IF NOT EXISTS stock (
  stock_id    TEXT PRIMARY KEY,
  name        TEXT,
  market      TEXT,        -- 'twse' 上市 | 'tpex' 上櫃
  industry    TEXT,        -- 產業別代碼或名稱
  listed_date TEXT,        -- 上市/上櫃日（西元）
  updated     TEXT
);

-- ============ 每日行情 ============
CREATE TABLE IF NOT EXISTS price_daily (
  stock_id TEXT NOT NULL,
  date     TEXT NOT NULL,
  open     REAL,
  high     REAL,
  low      REAL,
  close    REAL,
  volume   INTEGER,        -- 成交股數
  amount   REAL,           -- 成交金額（元）
  trades   INTEGER,        -- 成交筆數
  src      TEXT,           -- 'twse' | 'tpex' | 'finmind'
  PRIMARY KEY (stock_id, date)
);
CREATE INDEX IF NOT EXISTS ix_price_date ON price_daily(date);

-- ============ 四張榜單的每日快照 ============
-- board: 'volume' | 'amount' | 'swing' | 'chip'
-- 加新榜單只是多一個 board 值，標記邏輯完全不用動
CREATE TABLE IF NOT EXISTS rank_daily (
  date     TEXT NOT NULL,
  board    TEXT NOT NULL,
  stock_id TEXT NOT NULL,
  rank     INTEGER NOT NULL,
  metric   REAL,           -- 該榜的排序值
  PRIMARY KEY (date, board, stock_id)
);
CREATE INDEX IF NOT EXISTS ix_rank_bs ON rank_daily(board, stock_id, date);

-- ============ 融資融券（單位：張） ============
CREATE TABLE IF NOT EXISTS margin_daily (
  stock_id   TEXT NOT NULL,
  date       TEXT NOT NULL,
  mp_balance INTEGER,      -- 融資今日餘額
  mp_prev    INTEGER,      -- 融資前日餘額
  ss_balance INTEGER,      -- 融券今日餘額
  ss_prev    INTEGER,      -- 融券前日餘額
  src        TEXT,
  PRIMARY KEY (stock_id, date)
);

-- ============ 三大法人買賣超（單位：股） ============
CREATE TABLE IF NOT EXISTS chip_daily (
  stock_id    TEXT NOT NULL,
  date        TEXT NOT NULL,
  foreign_net INTEGER,     -- 外資（含陸資）買賣超
  trust_net   INTEGER,     -- 投信
  dealer_net  INTEGER,     -- 自營商
  total_net   INTEGER,     -- 三大法人合計
  src         TEXT,
  PRIMARY KEY (stock_id, date)
);

-- ============ 基準指數與 0050（回測對照用） ============
-- 白名單把 ETF 排掉了，所以 0050 單獨存在這裡，不進榜單池
CREATE TABLE IF NOT EXISTS benchmark_daily (
  code  TEXT NOT NULL,     -- 'TAIEX' | '0050'
  date  TEXT NOT NULL,
  close REAL,
  PRIMARY KEY (code, date)
);

-- ============ 休市日曆（只用來交叉驗證與顯示） ============
CREATE TABLE IF NOT EXISTS holiday (
  date TEXT PRIMARY KEY,
  name TEXT,
  note TEXT
);

-- ============ 除權息（自己算還原股價用） ============
-- 免費版的 FinMind 沒有還原股價，要靠這張表自己算還原係數
CREATE TABLE IF NOT EXISTS dividend (
  stock_id    TEXT NOT NULL,
  date        TEXT NOT NULL,   -- 除權息交易日
  cash        REAL,            -- 現金股利
  stock_div   REAL,            -- 股票股利（元/股，面額 10 元）
  before_px   REAL,            -- 除權息前收盤
  after_px    REAL,            -- 除權息參考價
  PRIMARY KEY (stock_id, date)
);

-- ============ 基本面（階段 5） ============
-- long format：一列一個會計科目，不同報表共用同一張表
CREATE TABLE IF NOT EXISTS fundamental (
  stock_id TEXT NOT NULL,
  date     TEXT NOT NULL,      -- 財報期別的截止日
  kind     TEXT NOT NULL,      -- 'income' | 'balance' | 'cashflow'
  item     TEXT NOT NULL,      -- 會計科目
  value    REAL,
  PRIMARY KEY (stock_id, date, kind, item)
);

CREATE TABLE IF NOT EXISTS revenue_month (
  stock_id TEXT NOT NULL,
  ym       TEXT NOT NULL,      -- 'YYYY-MM'，營收所屬月份（不是公布月份）
  revenue  REAL,
  PRIMARY KEY (stock_id, ym)
);

-- 本益比／股價淨值比／殖利率（日頻）
-- 上市只能靠 FinMind（證交所的 BWIBBU_ALL 被安全防護擋掉）
-- 上櫃有官方端點（免費、可散布）
CREATE TABLE IF NOT EXISTS valuation_daily (
  stock_id  TEXT NOT NULL,
  date      TEXT NOT NULL,
  per       REAL,            -- 本益比
  pbr       REAL,            -- 股價淨值比
  yield_pct REAL,            -- 殖利率 %
  src       TEXT,
  PRIMARY KEY (stock_id, date)
);
CREATE INDEX IF NOT EXISTS ix_val_date ON valuation_daily(date);

-- ============ 分群（階段 4.5） ============
-- ★ 這張表絕不含任何回測結果。分群依據必須獨立於要驗證的東西，
--   否則就變成「因為 A 策略賺錢所以這檔適合 A」的循環論證。
CREATE TABLE IF NOT EXISTS stock_profile (
  stock_id     TEXT PRIMARY KEY,
  group_id     TEXT,
  hurst        REAL,
  autocorr     REAL,
  atr_pct      REAL,
  vol_annual   REAL,           -- 年化波動度
  liquidity    REAL,           -- 成交金額中位數（元）
  mkt_cap_tier TEXT,           -- 'large' | 'mid' | 'small'
  bars         INTEGER,        -- 有效樣本天數
  updated      TEXT
);

-- ============ 策略配對與可信度（階段 4.5） ============
CREATE TABLE IF NOT EXISTS strategy_fit (
  stock_id   TEXT NOT NULL,
  strategy   TEXT NOT NULL,
  in_sample  REAL,
  out_sample REAL,             -- ★ 真正該看的
  trades     INTEGER,
  win_rate   REAL,
  mdd        REAL,
  p_value    REAL,             -- 置換檢定
  grade      TEXT,             -- 'A' | 'B' | 'C' | 'X'
  reason     TEXT,             -- ★ 分級理由，給人看的（不是分數）
  verified   TEXT,             -- 驗證日期，會過期
  PRIMARY KEY (stock_id, strategy)
);

-- 群組 × 策略的研究模式結果
CREATE TABLE IF NOT EXISTS group_fit (
  group_id   TEXT NOT NULL,
  strategy   TEXT NOT NULL,
  in_sample  REAL,
  out_sample REAL,
  n_stocks   INTEGER,
  pos_ratio  REAL,             -- 群組內多少比例的股票是正報酬
  verified   TEXT,
  PRIMARY KEY (group_id, strategy)
);

-- ============ 抓取進度（★ 讓 6 小時的建庫可以中斷續跑） ============
-- 建庫要跑好幾個小時，中途一定會想關掉或不小心斷線。
-- 每抓完一檔就記一筆，重跑時已完成的直接跳過。
CREATE TABLE IF NOT EXISTS fetch_state (
  dataset  TEXT NOT NULL,
  stock_id TEXT NOT NULL,
  start    TEXT,          -- 已涵蓋的起日
  end      TEXT,          -- 已涵蓋的迄日
  rows     INTEGER,
  at       TEXT,
  PRIMARY KEY (dataset, stock_id)
);

-- ============ 抓取紀錄 ============
CREATE TABLE IF NOT EXISTS fetch_log (
  at      TEXT,
  source  TEXT,                -- 'twse' | 'tpex' | 'finmind'
  dataset TEXT,
  ok      INTEGER,
  rows    INTEGER,
  note    TEXT
);
"""

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stock.db")


def connect(path=None):
    path = path or DEFAULT_PATH
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def backup(path=None):
    """寫入前備份，檔名帶時間戳。回傳備份檔路徑（沒有原檔則回 None）。"""
    path = path or DEFAULT_PATH
    if not os.path.exists(path):
        return None
    dst = f"{path}.bak_{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(path, dst)
    return dst


def log(conn, source, dataset, ok, rows=0, note=""):
    conn.execute(
        "INSERT INTO fetch_log (at, source, dataset, ok, rows, note) "
        "VALUES (?,?,?,?,?,?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), source, dataset,
         1 if ok else 0, rows, note))


# ---------------- 寫入 ----------------

def upsert_price(conn, rows):
    """rows: (stock_id, date, open, high, low, close, volume, amount, trades, src)"""
    conn.executemany(
        "INSERT INTO price_daily "
        "(stock_id,date,open,high,low,close,volume,amount,trades,src) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(stock_id,date) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume, amount=excluded.amount, "
        "trades=excluded.trades, src=excluded.src", rows)
    return len(rows)


def upsert_margin(conn, rows):
    conn.executemany(
        "INSERT INTO margin_daily "
        "(stock_id,date,mp_balance,mp_prev,ss_balance,ss_prev,src) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(stock_id,date) DO UPDATE SET "
        "mp_balance=excluded.mp_balance, mp_prev=excluded.mp_prev, "
        "ss_balance=excluded.ss_balance, ss_prev=excluded.ss_prev, "
        "src=excluded.src", rows)
    return len(rows)


def upsert_chip(conn, rows):
    conn.executemany(
        "INSERT INTO chip_daily "
        "(stock_id,date,foreign_net,trust_net,dealer_net,total_net,src) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(stock_id,date) DO UPDATE SET "
        "foreign_net=excluded.foreign_net, trust_net=excluded.trust_net, "
        "dealer_net=excluded.dealer_net, total_net=excluded.total_net, "
        "src=excluded.src", rows)
    return len(rows)


def upsert_valuation(conn, rows):
    """rows: (stock_id, date, per, pbr, yield_pct, src)"""
    conn.executemany(
        "INSERT INTO valuation_daily (stock_id,date,per,pbr,yield_pct,src) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(stock_id,date) DO UPDATE SET "
        "per=excluded.per, pbr=excluded.pbr, yield_pct=excluded.yield_pct, "
        "src=excluded.src", rows)
    return len(rows)


def upsert_stock(conn, rows):
    """rows: (stock_id, name, market, industry, listed_date, updated)"""
    conn.executemany(
        "INSERT INTO stock (stock_id,name,market,industry,listed_date,updated) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(stock_id) DO UPDATE SET "
        "name=excluded.name, market=excluded.market, industry=excluded.industry, "
        "listed_date=excluded.listed_date, updated=excluded.updated", rows)
    return len(rows)


def replace_ranks(conn, date, board, rows):
    """寫入某一天某一張榜。

    ★ `rows` 的格式是 **[(stock_id, metric), ...]，而且必須已經排好序**。
      排名是由「在清單中的位置」決定的（第一筆 = 第 1 名），
      不是從 rows 裡讀出來的。傳 (stock_id, rank) 進來不會報錯，
      但 rank 會被當成 metric 存起來，排名則變成清單順序 —— 靜默錯誤。

    先刪再寫，因為榜單是「當天的完整排名」—— 如果重抓時某檔掉出榜外，
    只做 UPSERT 會讓它留在表裡，變成已經掉榜卻還在榜上的鬼資料。
    """
    conn.execute("DELETE FROM rank_daily WHERE date=? AND board=?", (date, board))
    conn.executemany(
        "INSERT INTO rank_daily (date,board,stock_id,rank,metric) VALUES (?,?,?,?,?)",
        [(date, board, sid, i + 1, m) for i, (sid, m) in enumerate(rows)])
    return len(rows)


# ---------------- 查詢 ----------------

def trading_days(conn, start=None, end=None):
    """交易日清單（升冪）。直接從行情資料推，不靠行事曆。"""
    q = "SELECT DISTINCT date FROM price_daily"
    w, p = [], []
    if start:
        w.append("date >= ?"); p.append(start)
    if end:
        w.append("date <= ?"); p.append(end)
    if w:
        q += " WHERE " + " AND ".join(w)
    q += " ORDER BY date"
    return [r[0] for r in conn.execute(q, p)]


def last_trading_day(conn):
    r = conn.execute("SELECT MAX(date) FROM price_daily").fetchone()
    return r[0] if r else None


def prev_trading_day(conn, date):
    r = conn.execute(
        "SELECT MAX(date) FROM price_daily WHERE date < ?", (date,)).fetchone()
    return r[0] if r else None


def whitelist(conn):
    """白名單的 stock_id 集合。"""
    return {r[0] for r in conn.execute("SELECT stock_id FROM stock")}


def stock_names(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT stock_id, name FROM stock")}


def done_set(conn, dataset, start, end):
    """已經抓過、而且涵蓋範圍包含 [start, end] 的 stock_id 集合。

    建庫動輒數千次請求、數小時，中斷續跑是必備的 —— 不然每次中斷
    都要從頭再花六小時。
    """
    return {r[0] for r in conn.execute(
        "SELECT stock_id FROM fetch_state WHERE dataset=? "
        "AND start <= ? AND end >= ?", (dataset, start, end))}


def mark_done(conn, dataset, stock_id, start, end, rows):
    conn.execute(
        "INSERT INTO fetch_state (dataset,stock_id,start,end,rows,at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(dataset,stock_id) DO UPDATE SET "
        "start=MIN(start,excluded.start), end=MAX(end,excluded.end), "
        "rows=excluded.rows, at=excluded.at",
        (dataset, stock_id, start, end, rows,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def freshness(conn):
    """資料新鮮度。

    ★★ 「資料日期」和「抓取時間」是兩件完全不同的事，兩個都要顯示 ★★

        資料日期   資料本身的交易日。週六去抓，拿到的還是週五的資料。
        抓取時間   你上次按下更新按鈕的時刻。

    只看資料日期 → 週末會以為「怎麼沒更新」，其實是沒開市。
    只看抓取時間 → 以為抓到最新的了，但可能官方當天還沒結算完。
    所以介面上一定要並列。

    回傳 dict：
      data_date    最後一個有行情的交易日
      fetched_at   最後一次成功抓取的時間
      items        [(類別, 最後抓取時間, 資料範圍)]
    """
    q = lambda s, *a: (conn.execute(s, a).fetchone() or [None])[0]
    items = []

    # 官方 OpenAPI（每日更新，走 fetch_log）
    for src, ds, zh in (("twse", "price", "上市行情"),
                        ("tpex", "price", "上櫃行情"),
                        ("twse", "margin", "上市融資券"),
                        ("tpex", "margin", "上櫃融資券"),
                        ("tpex", "chip", "上櫃三大法人"),
                        ("twse", "index", "加權指數"),
                        ("twse+tpex", "companies", "白名單")):
        at = q("SELECT MAX(at) FROM fetch_log WHERE source=? AND dataset=? "
               "AND ok=1", src, ds)
        if at:
            items.append((zh, at, "官方 OpenAPI"))

    # FinMind 歷史（走 fetch_state）
    for ds, zh in (("TaiwanStockPrice", "價量歷史"),
                   ("TaiwanStockInstitutionalInvestorsBuySell", "三大法人歷史"),
                   ("TaiwanStockMarginPurchaseShortSale", "融資券歷史"),
                   ("TaiwanStockDividendResult", "除權息"),
                   ("TaiwanStockMonthRevenue", "月營收"),
                   ("TaiwanStockFinancialStatements", "綜合損益表"),
                   ("TaiwanStockBalanceSheet", "資產負債表"),
                   ("TaiwanStockCashFlowsStatement", "現金流量表")):
        r = conn.execute(
            "SELECT MAX(at), COUNT(*), MIN(start), MAX(end) FROM fetch_state "
            "WHERE dataset=?", (ds,)).fetchone()
        if r and r[0]:
            items.append((zh, r[0], f"FinMind．{r[1]} 檔．{r[2]} ~ {r[3]}"))

    # 基準
    for code, zh in (("0050", "基準 0050"), ("TAIEX", "基準 加權報酬指數"),
                     ("TAIEX_TR", "基準 報酬指數(證交所)"),
                     ("TAIEX_PRICE", "基準 價格指數(證交所)")):
        r = conn.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM benchmark_daily "
            "WHERE code=?", (code,)).fetchone()
        if r and r[0]:
            items.append((zh, "—", f"{r[0]} 筆．{r[1]} ~ {r[2]}"))

    # 分群與策略配對（會過期，所以要看得到日期）
    at = q("SELECT MAX(updated) FROM stock_profile")
    if at:
        items.append(("分群", at, f"{q('SELECT COUNT(*) FROM stock_profile')} 檔"))
    at = q("SELECT MAX(verified) FROM strategy_fit")
    if at:
        items.append(("策略配對", at,
                      f"{q('SELECT COUNT(*) FROM strategy_fit')} 組"
                      f"（可信度會過期）"))

    items.sort(key=lambda x: x[1], reverse=True)
    return {
        "data_date": q("SELECT MAX(date) FROM price_daily"),
        "rank_date": q("SELECT MAX(date) FROM rank_daily"),
        "fetched_at": q("SELECT MAX(at) FROM fetch_log WHERE ok=1"),
        "history_at": q("SELECT MAX(at) FROM fetch_state"),
        "items": items,
    }


def coverage(conn):
    """資料庫現況摘要，給 GUI 和命令列顯示。"""
    q = lambda s, *a: conn.execute(s, a).fetchone()[0]
    return {
        "stocks": q("SELECT COUNT(*) FROM stock"),
        "price_rows": q("SELECT COUNT(*) FROM price_daily"),
        "price_first": q("SELECT COALESCE(MIN(date),'') FROM price_daily"),
        "price_last": q("SELECT COALESCE(MAX(date),'') FROM price_daily"),
        "trading_days": q("SELECT COUNT(DISTINCT date) FROM price_daily"),
        "rank_days": q("SELECT COUNT(DISTINCT date) FROM rank_daily"),
        "chip_rows": q("SELECT COUNT(*) FROM chip_daily"),
        "chip_first": q("SELECT COALESCE(MIN(date),'') FROM chip_daily"),
        "margin_rows": q("SELECT COUNT(*) FROM margin_daily"),
        "fund_rows": q("SELECT COUNT(*) FROM fundamental"),
        "rev_rows": q("SELECT COUNT(*) FROM revenue_month"),
        "profiled": q("SELECT COUNT(*) FROM stock_profile"),
        "fits": q("SELECT COUNT(*) FROM strategy_fit"),
    }
