# -*- coding: utf-8 -*-
"""除權息還原股價。

## ★ 為什麼這支很重要

**沒做還原的回測是錯的。** 除息那天股價會憑空掉下來（配 5 元就跌 5 元），
但那不是虧損 —— 錢進你口袋了。不還原的話：

- 高股息股在原始價格上看起來年年下跌，任何「買強勢股」的策略都會自動避開它
- 停損會被除息的價格跳空誤觸發
- 長期報酬被系統性低估

FinMind 免費版沒有還原股價（`TaiwanStockPriceAdj` 是 Backer 以上專屬），
所以要自己算。

## ★ 用官方參考價算，不要自己用股利推

FinMind 的 `TaiwanStockDividendResult` 直接給了：

    before_price  除權息前收盤      125.5
    after_price   除權息參考價      120.74

**還原係數 = after_price / before_price = 0.96207**

這比「自己拿現金股利去減」準確得多，因為 after_price 是交易所算出來的參考價，
已經把股票股利（配股會改變股數）一起處理掉了。自己算股票股利很容易弄錯
（面額 10 元、配股率、增資除權……），而且錯了不會有人告訴你。

## 還原方向：往回調整（back-adjust）

除息那天價格掉下來，所以要把**那天之前**的歷史價格按比例調低，
讓序列連續、報酬率正確：

    adj[t] = raw[t] × Π{ factor[e] : e 的除權息日 > t }

這樣**最新價格永遠等於真實價格**（沒有事件在它之後），
只有歷史被調整。好處是你在介面上看到的今天股價就是真的那個數字。

另一種做法是往前調整（最舊價格不動、後面往上加），
那會讓今天的價格變成一個對不上行情軟體的怪數字，所以不採用。
"""
import pandas as pd


def factors(conn, stock_id):
    """取某檔的還原係數事件。回傳 [(date, factor)]，升冪。

    factor < 1 代表除權息當天價格往下跳（配息）。
    資料不完整（缺 before/after 或值不合理）的事件會被跳過 ——
    寧可少還原一次，也不要用壞資料把整條序列算歪。
    """
    out = []
    for d, b, a in conn.execute(
            "SELECT date, before_px, after_px FROM dividend "
            "WHERE stock_id=? ORDER BY date", (stock_id,)):
        if not b or not a or b <= 0 or a <= 0:
            continue
        f = a / b
        # 合理範圍檢查：單次除權息不該讓價格掉超過一半或反而漲
        if not (0.5 <= f <= 1.0001):
            continue
        out.append((d, f))
    return out


def adjust_frame(df, events):
    """把 OHLC 換成還原價。df 需以 date（字串）為 index，升冪。

    回傳新的 DataFrame，多出 `adj_factor` 欄位方便除錯與驗證。
    """
    if df.empty:
        return df
    out = df.copy()
    cum = pd.Series(1.0, index=out.index)
    # 從最後往前累乘：某一天的係數 = 它之後所有事件的係數相乘
    for d, f in sorted(events, reverse=True):
        cum.loc[cum.index < d] *= f
    out["adj_factor"] = cum
    for c in ("open", "high", "low", "close"):
        if c in out.columns:
            out[c] = out[c] * cum
    return out


def load_prices(conn, stock_id, start=None, end=None, adjusted=True):
    """讀單檔行情，回傳以 date 為 index 的 DataFrame。

    adjusted=True 會套用還原（回測一律要用還原價）。
    """
    q = ("SELECT date, open, high, low, close, volume, amount, trades "
         "FROM price_daily WHERE stock_id=?")
    p = [stock_id]
    if start:
        q += " AND date >= ?"; p.append(start)
    if end:
        q += " AND date <= ?"; p.append(end)
    q += " ORDER BY date"
    rows = conn.execute(q, p).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                     "volume", "amount", "trades"])
    df = df.set_index("date")
    if adjusted:
        df = adjust_frame(df, factors(conn, stock_id))
    return df


def fix_corporate_actions(series, limit=0.10, margin=0.03):
    """★ 用「漲跌幅限制」偵測並修正股票分割等公司行為。

    ## 為什麼需要這支（實測踩到的 bug）

    回測的基準 0050 算出 **總報酬 -30.6%、最大回撤 -77.2%** ——
    這不可能。原因是 **0050 在 2025 年做過 1 股分割成 4 股**，
    價格從約 190 元變成約 47 元。未還原的原始價格把分割當成了崩盤，
    整個基準對照因此完全失真。

    ## 原理

    **台股有 ±10% 的漲跌幅限制。** 所以單日變動超過這個範圍（留 3% 緩衝），
    在定義上就不可能是行情，只能是公司行為：股票分割、大額除權息、
    減資、面額變更。

    這個判準的好處是它不依賴任何股利資料庫 —— 分割（`TaiwanStockDividendResult`
    裡查不到，因為它不是股利）也一樣會被抓到。

    ## 做法

    偵測到跳空就把**那天之前**的價格乘上比例，讓序列連續。
    和 `adjust_frame()` 同一個方向（往回調整），所以最新價格保持真實。

    回傳 (修正後的 Series, 偵測到的事件清單)。
    事件清單要印給使用者看 —— 靜默修正資料是很危險的行為。

    ⚠ 限制：追蹤國外市場的 ETF（例如 00646 標普500）沒有漲跌幅限制，
    這支不適用於那些標的。目前只用在 0050（國內成分，有 ±10% 限制）。
    """
    s = pd.Series(series).astype(float).dropna()
    if len(s) < 3:
        return s, []
    hi, lo = 1.0 + limit + margin, 1.0 - limit - margin
    ratio = s / s.shift(1)
    events = []
    cum = pd.Series(1.0, index=s.index)
    for d, r in ratio.items():
        if pd.isna(r) or r <= 0:
            continue
        if r > hi or r < lo:
            events.append((str(d), float(r)))
            cum.loc[cum.index < d] *= r
    return s * cum, events


def check_continuity(conn, stock_id, threshold=0.06):
    """驗證還原有沒有做對。

    做法：看還原後的**單日報酬率**在除權息那天有沒有異常跳空。
    還原正確的話，除息日的報酬率應該落在正常範圍裡；
    沒還原的話會看到一個接近配息率的負跳空。

    回傳 (事件數, 還原後仍異常的事件清單)。
    異常清單非空不一定是 bug（除息當天也可能真的大跌），
    但如果**每一個**除息日都異常，那就是還原沒生效。
    """
    ev = factors(conn, stock_id)
    if not ev:
        return 0, []
    df = load_prices(conn, stock_id, adjusted=True)
    if df.empty:
        return len(ev), []
    ret = df["close"].pct_change()
    bad = []
    for d, f in ev:
        if d in ret.index:
            r = ret.loc[d]
            if pd.notna(r) and abs(r) > threshold:
                bad.append((d, round(float(r) * 100, 2), round(f, 5)))
    return len(ev), bad
