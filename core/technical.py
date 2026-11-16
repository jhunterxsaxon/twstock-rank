# -*- coding: utf-8 -*-
"""技術指標：從累積的官方價量算出 MA／KD／MACD／RSI／布林／ATR 等。

## 授權

算出來的指標是**衍生數值**，而原始資料本身（官方 OpenAPI、政府資料
開放授權條款）就允許散布了，指標更沒有疑慮。這裡完全不碰 FinMind。

## ★★ 歷史長度是唯一的瓶頸 ★★

官方 OpenAPI 只提供最新一個交易日，**不支援查詢過去的日期**（實測確認），
所以歷史只能從本站上線那天（2026-11-09）開始一天一天累積，補不回來。

技術指標需要暖機期，資料不夠時算出來的是 NaN 或極不穩定的數字：

    指標                     最少天數   真正穩定
    MA5、乖離                   5          5
    MA10                       10         10
    MA20、布林、量比             20         20
    KD(9,3,3)                  15         30
    RSI(14)                    14         60    <- Wilder 平滑有長記憶
    MACD(12,26,9)              35        100
    MA60、60日新高低、多頭排列    60         60
    MA120                     120        120

★ 所以資料不足時**顯示「還需要 N 天」，不顯示數字**。
  擠出一個暖機期內的 RSI 比留空更糟 —— 那個數字看起來正常，
  但它只是初始值的殘影，而使用者無從得知。

## ★ 還原股價

除息當天價格會跳空下跌，不還原的話 MA 被拉下來、KD 假性轉弱，
看起來像賣壓，其實不是。本機版踩過更嚴重的版本：0050 的 1:4 分割
讓基準報酬從 +175% 變成 -30.6%。

還原係數的來源有三層，依可靠度排序：

1. **櫃買**直接給「除權息前收盤」與「除權息參考價」-> 係數 = 後 / 前
2. **證交所**只給現金股利（TWT48U_ALL 沒有參考價），所以用我們自己的
   前一日收盤推算：(前收 - 現金股利) / (前收 * (1 + 股票股利/10))
3. **漲跌幅規則**（adjust.fix_corporate_actions）：台股有 ±10% 限制，
   單日變動超過這個範圍在定義上就不可能是行情，只能是分割或減資。
   這層不依賴任何股利資料，用來接住前兩層漏掉的事件。

⚠ 第 2 層的股票股利單位（元/股 -> 股/股要除以 10）目前**無法驗證** ——
  TWT48U_ALL 的 StockDividendRatio 實測全部是空的。等真的出現再對帳。
"""
import math

from core import adjust, indicators as ind

# 每個輸出欄位需要的最少天數，以及「真正穩定」所需的天數。
# ★ 兩個都要記：最少天數決定「算不算得出來」，穩定天數決定「能不能信」。
NEED = {
    "ma5": (5, 5), "ma10": (10, 10), "ma20": (20, 20),
    "ma60": (60, 60), "ma120": (120, 120),
    "bias20": (20, 20),
    "k": (15, 30), "d": (15, 30),
    "rsi": (14, 60),
    "dif": (26, 100), "macd_hist": (35, 100),
    "boll_pos": (20, 20), "boll_width": (20, 20),
    "atr_pct": (14, 30),
    "vol_ratio": (20, 20),
    "new_high": (60, 60), "new_low": (60, 60),
    "bull_stack": (60, 60),
    "up_streak": (2, 2), "down_streak": (2, 2),
}

# 輸出順序（JSON 用陣列存，省掉每一列重複的欄位名）
FIELDS = ["stock_id", "close"] + list(NEED)

ZH = {
    "close": "收盤", "ma5": "5日均", "ma10": "10日均", "ma20": "20日均",
    "ma60": "60日均", "ma120": "120日均", "bias20": "20日乖離",
    "k": "K", "d": "D", "rsi": "RSI", "dif": "MACD DIF",
    "macd_hist": "MACD 柱", "boll_pos": "布林位置", "boll_width": "布林寬",
    "atr_pct": "ATR%", "vol_ratio": "量比", "new_high": "60日新高",
    "new_low": "60日新低", "bull_stack": "多頭排列",
    "up_streak": "連漲", "down_streak": "連跌",
}


def factors_all(conn):
    """算出每一檔的還原係數事件：{stock_id: [(date, factor), ...]}。

    ★ 為什麼不直接用 adjust.factors()
    那支要求 dividend 表有 before_px / after_px，但**證交所不提供**
    （TWT48U_ALL 只給股利金額）。本機版的資料來自 FinMind，兩個欄位都有，
    所以那支夠用；這個 repo 不行，必須多一層推算。

    推算需要「除權息前一日的收盤」，所以先把收盤價查出來。
    """
    closes = {}
    for sid, d, c in conn.execute(
            "SELECT stock_id, date, close FROM price_daily "
            "ORDER BY stock_id, date"):
        closes.setdefault(sid, []).append((d, c))

    out = {}
    for sid, d, cash, stock, b, a in conn.execute(
            "SELECT stock_id, date, cash, stock_div, before_px, after_px "
            "FROM dividend ORDER BY stock_id, date"):
        f = None
        if b and a and b > 0 and a > 0:
            f = a / b                       # 第 1 層：櫃買直接給
        elif cash is not None:
            prev = None
            for cd, cc in closes.get(sid, []):
                if cd >= d:
                    break
                if cc:
                    prev = cc               # 除權息日之前最後一筆收盤
            if prev:
                f = (prev - cash) / (prev * (1 + (stock or 0) / 10.0))
        if f is None or not (0.5 <= f <= 1.0001):
            # 合理範圍外就跳過 —— 寧可少還原一次，
            # 也不要用壞資料把整條序列算歪。
            continue
        out.setdefault(sid, []).append((d, f))
    return out


def _round(v, dp=2):
    """None / NaN / inf 一律回 None。

    JSON 不吃 NaN，而且前端必須能分辨「沒有這個數字」和「數字是 0」。
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, dp)


def compute(conn, allow=None, progress=None):
    """算出最新交易日的技術指標。

    回傳 (date, rows, ready)：
      rows  -- [[stock_id, close, ma5, ...], ...]，順序同 FIELDS
      ready -- {欄位: {"need": 最少天數, "stable": 穩定天數,
                       "have": 目前天數, "ok": bool, "trusted": bool}}
    """
    import pandas as pd

    # ★ 只用「兩市都有資料」的日期。
    # 櫃買 16:00 就發布當日收盤、證交所隔天 05:20，所以最新的那個日期
    # 可能只有上櫃的 896 檔。拿它當 days[-1] 會算出半個市場的指標，
    # 而且數字看起來完全正常 —— 這種錯最難發現。
    from core import db as _db
    days = _db.complete_days(conn)
    if not days:
        return None, [], {}
    last = days[-1]
    have = len(days)

    ready = {}
    for k, (need, stable) in NEED.items():
        ready[k] = {"need": need, "stable": stable, "have": have,
                    "ok": have >= need, "trusted": have >= stable}

    facs = factors_all(conn)
    rows = []

    def flush(sid, recs):
        if not sid or not recs or (allow and sid not in allow):
            return
        recs = [r for r in recs if r[0] <= last]   # 丟掉半套的未來日期
        if not recs or recs[-1][0] != last:
            return      # 最新交易日沒有這一檔（停牌、下市），略過
        df = pd.DataFrame(recs, columns=["date", "open", "high", "low",
                                         "close", "volume"]).set_index("date")
        df = adjust.adjust_frame(df, facs.get(sid, []))
        # 第 3 層保險：漲跌幅規則接住股利資料沒涵蓋的分割／減資
        try:
            fixed, _ev = adjust.fix_corporate_actions(df["close"])
            if len(fixed) == len(df):
                ratio = (fixed / df["close"]).fillna(1.0)
                for c in ("open", "high", "low", "close"):
                    df[c] = df[c] * ratio
        except Exception:
            pass        # 還原失敗不該讓整批停下來，原始價仍可用
        e = ind.enrich(df)
        r = e.iloc[-1]
        out = [sid, _round(r.get("close"))]
        for k in NEED:
            # ★ 還沒到暖機期的指標一律填 None，不是只在前端藏起來。
            # pandas 的 rolling/ewm 在資料不足時照樣會吐出數字
            # （實測 5 天就算得出 RSI 92.87、K 50、MACD DIF 24.82），
            # 那些是初始值的殘影，不是訊號。JSON 是公開的，
            # 直接讀檔的人看不到前端的保護，所以要在來源就擋掉。
            if not ready[k]["ok"]:
                out.append(None)
                continue
            v = r.get(k)
            if k in ("new_high", "new_low", "bull_stack"):
                bad = v is None or (isinstance(v, float) and math.isnan(v))
                out.append(None if bad else int(bool(v)))
            elif k in ("up_streak", "down_streak"):
                out.append(_round(v, 0))
            else:
                out.append(_round(v))
        rows.append(out)

    cur, sid_cur, n = [], None, 0
    for sid, d, o, h, l, c, v in conn.execute(
            "SELECT stock_id, date, open, high, low, close, volume "
            "FROM price_daily ORDER BY stock_id, date"):
        if sid != sid_cur:
            flush(sid_cur, cur)
            n += 1
            if progress and n % 400 == 0:
                progress(f"  {n} 檔…")
            sid_cur, cur = sid, []
        cur.append((d, o, h, l, c, v))
    flush(sid_cur, cur)
    rows.sort(key=lambda r: r[0])
    return last, rows, ready
