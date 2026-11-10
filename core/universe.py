# -*- coding: utf-8 -*-
"""白名單：決定什麼算「個股」。

## ★ 用白名單，不用黑名單

上市／上櫃「公司」基本資料表（`t187ap03_L`、`mopsfin_t187ap03_O`）就是答案 ——
**ETF 不是公司、權證不是公司，根本不會出現在裡面。**

實測驗證（2026-11-10）：
  證交所公司清單 1,099 筆 → **00 開頭 0 筆**，代號長度 {4: 1093, 6: 6}
  證交所日行情   1,389 筆 → 00 開頭 245 筆（ETF 全在這）
  櫃買公司清單     897 筆
  櫃買日行情    13,674 筆（含權證，差 15 倍）

為什麼不用代號規則：**`0050` 本身就是 4 碼純數字**，純靠位數會把它漏進來。
而「00 開頭就排除」這種規則，碰到主動式 ETF `00400A`、債券 ETF `00687B`
還要一直補例外，永遠補不完。白名單一次解決。

## 第二層保險：代號恰好 4 位純數字

公司清單裡還是會有少數非一般股票（實測證交所有 6 筆 6 碼的），
所以再疊一層位數過濾。兩層之後自動掃掉：

  ETF      0050、00632R、00400A   → 不在公司清單
  權證     6 碼                   → 不在公司清單
  特別股   2881A                  → 5 碼含字母
  TDR／ETN                        → 位數不符
  興櫃                            → 不在上市/上櫃公司清單

白名單每天隨行情一起更新，新上市的股票自動納入。
"""
from datetime import datetime

from core import db, tpex, twse


def is_plain_stock(stock_id):
    """第二層：代號恰好 4 位純數字。"""
    s = str(stock_id).strip()
    return len(s) == 4 and s.isdigit()


def build(conn, progress=None):
    """抓上市＋上櫃公司清單，過濾後寫入 `stock` 表。

    回傳 (寫入筆數, 被位數過濾掉的筆數, 細節 dict)。
    """
    def say(t):
        if progress:
            progress(t)

    rows, dropped = [], []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    detail = {}

    say("抓上市公司清單…")
    tw = twse.fetch_companies()
    detail["twse_raw"] = len(tw)
    for c in tw:
        if is_plain_stock(c["stock_id"]):
            rows.append((c["stock_id"], c["name"], "twse",
                         c["industry"], c["listed_date"], now))
        else:
            dropped.append(("twse", c["stock_id"], c["name"]))

    say("抓上櫃公司清單…")
    tp = tpex.fetch_companies()
    detail["tpex_raw"] = len(tp)
    for c in tp:
        if is_plain_stock(c["stock_id"]):
            rows.append((c["stock_id"], c["name"], "tpex",
                         c["industry"], c["listed_date"], now))
        else:
            dropped.append(("tpex", c["stock_id"], c["name"]))

    detail["dropped"] = dropped
    db.upsert_stock(conn, rows)
    db.log(conn, "twse+tpex", "companies", True, len(rows),
           f"白名單 {len(rows)} 檔，位數過濾掉 {len(dropped)} 筆")
    conn.commit()
    return len(rows), len(dropped), detail


def filter_rows(rows, allow):
    """把行情列表過濾成只剩白名單內的個股。

    rows 是 twse/tpex `fetch_price()` 回傳的 dict 清單。
    """
    return [r for r in rows if r["stock_id"] in allow]
