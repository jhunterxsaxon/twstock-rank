# -*- coding: utf-8 -*-
"""GitHub Actions 每日抓取。

## ★★ 這個 repo 只抓官方 OpenAPI，不碰 FinMind ★★

`core/` 底下**沒有 finmind.py** —— 程式碼裡根本沒有抓它的能力，
就不可能不小心把它的資料發布出去。這比寫註解提醒可靠得多：
註解會被忽略，不存在的程式碼不會。

FinMind 四個方案全部禁止再散布原始資料，而且無 token 的額度按 IP 計算，
在 Actions 的共用 IP 上跑一定會 402，甚至觸發 IP 封鎖。

公開的資料全部來自：
  臺灣證券交易所、證券櫃檯買賣中心（政府資料開放授權條款第 1 版）
★ 該條款規定**未標示來源者，授權視為自始未取得**，所以頁尾一定要標示。

## 執行流程

    1. 讀 data/*.json → 灌進暫時的 SQLite
    2. 先打一個端點看「最新交易日是哪天」
       → 如果那天我們已經有了，**立刻結束、不 commit**
         （22:30 的保險排程就是靠這個，有新資料才做事）
    3. 抓官方資料 → SQLite
    4. 用和本機版完全相同的 ranking.py 算榜單
    5. 匯出 JSON

用法：
    python fetch.py                 正常執行
    python fetch.py --force         即使沒有新資料也重跑（修資料時用）
    python fetch.py --rebuild       重算所有歷史榜單後再匯出
"""
import argparse
import os
import sqlite3
import sys
import traceback
from datetime import datetime

from core import db, netio, ranking, tpex, twse, universe, webstore as ws


def log(m):
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def open_db():
    """暫時的 SQLite，只活在這次執行期間。JSON 才是真正的存檔。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    return conn


def peek_latest_date():
    """只打一個端點，看官方最新的交易日是哪天。

    ★ 這是「要不要做事」的判斷依據，成本只有 1 次請求。
    22:30 的保險排程靠它決定要不要繼續 —— 沒有新資料就立刻結束，
    不會產生空的 commit。
    """
    data = netio.get_json(twse.EP_PRICE)
    if not data:
        return None
    return netio.roc_to_iso(data[0].get("Date"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="每日抓取（官方 OpenAPI）")
    ap.add_argument("--force", action="store_true",
                    help="即使沒有新資料也重跑")
    ap.add_argument("--rebuild", action="store_true",
                    help="重算所有歷史榜單")
    ap.add_argument("--scheduled", default=os.environ.get("SCHEDULED_AT", ""),
                    help="排程設定的時間（給延遲統計用）")
    a = ap.parse_args(argv)

    notes = []
    log("讀取既有資料…")
    conn = open_db()
    n_days = ws.load_into_db(conn)
    have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM price_daily")}
    log(f"  已有 {n_days} 個交易日"
        + (f"（{min(have)} ~ {max(have)}）" if have else "（全新的 repo）"))

    log("查官方最新交易日…")
    try:
        latest = peek_latest_date()
    except Exception as e:
        log(f"✖ 連線失敗：{e}")
        traceback.print_exc()
        return 1
    log(f"  官方目前提供 {latest}")
    today = datetime.now().strftime("%Y-%m-%d")
    if latest != today:
        notes.append(f"官方資料仍停在 {latest}（今天是 {today}）—— "
                     f"這是正常的，官方不是收盤後馬上更新")

    if latest in have and not a.force and not a.rebuild:
        log(f"  {latest} 的資料已經有了，沒有新東西 → 結束，不產生 commit")
        print("::notice::no-new-data")
        return 0

    # ---- 白名單 ----
    log("更新白名單…")
    try:
        kept, dropped, detail = universe.build(
            conn, progress=lambda t: log("  " + t))
        log(f"  {kept} 檔（上市原始 {detail['twse_raw']}、"
            f"上櫃原始 {detail['tpex_raw']}，位數過濾掉 {dropped}）")
    except Exception as e:
        log(f"  ✖ 白名單更新失敗，沿用舊的：{e}")
        notes.append(f"白名單更新失敗：{e}")
    allow = db.whitelist(conn)
    if not allow:
        log("✖ 白名單是空的，無法繼續")
        return 1

    # ---- 行情 ----
    log("抓兩市行情…")
    d1, rows1 = twse.fetch_price()
    d2, rows2 = tpex.fetch_price()
    log(f"  上市 {d1} 原始 {len(rows1)} 筆｜上櫃 {d2} 原始 {len(rows2)} 筆")
    if d1 != d2:
        notes.append(f"兩市日期不一致（上市 {d1}／上櫃 {d2}）")
    bench = [("0050", d1, r["close"]) for r in rows1
             if r["stock_id"] == "0050" and r["close"]]
    if bench:
        conn.executemany(
            "INSERT OR REPLACE INTO benchmark_daily (code,date,close) "
            "VALUES (?,?,?)", bench)
    n = 0
    for date, rows, src in ((d1, rows1, "twse"), (d2, rows2, "tpex")):
        keep = universe.filter_rows(rows, allow)
        n += db.upsert_price(conn, [
            (r["stock_id"], date, r["open"], r["high"], r["low"], r["close"],
             int(r["volume"] or 0), r["amount"] or 0, int(r["trades"] or 0), src)
            for r in keep])
        log(f"  {src} 過白名單後 {len(keep)} 筆")
    log(f"  共寫入 {n} 筆")

    # ---- 其餘官方資料（失敗不影響榜單）----
    for label, fn in (
            ("加權指數", lambda: _indices(conn)),
            ("融資融券", lambda: _margins(conn, d1, allow)),
            ("上櫃三大法人", lambda: _chips(conn, allow)),
            ("上櫃本益比", lambda: _valuations(conn, allow)),
            ("休市日曆", lambda: _holidays(conn))):
        log(f"抓{label}…")
        try:
            log("  " + str(fn()))
        except Exception as e:
            log(f"  ✖ 失敗（不影響榜單）：{e}")
            notes.append(f"{label}抓取失敗：{e}")

    # ---- 榜單 ----
    dates = [d1] if not a.rebuild else db.trading_days(conn)
    log(f"算榜單（{len(dates)} 天）…")
    for i, d in enumerate(dates, 1):
        ranking.build_date(conn, d, allow=allow)
        if a.rebuild and i % 100 == 0:
            log(f"  {i}/{len(dates)}")
    conn.commit()

    w = ranking.selectivity_warning(conn)
    if w:
        notes.append(w.replace("\n", " ").strip())
        log("⚠ " + w.split("\n")[0])

    # ---- 匯出 ----
    log("匯出 JSON…")
    ws.dump_universe(conn)
    touched = sorted({ws.month_of(d) for d in dates} | {ws.month_of(d2)})
    for ym in touched:
        ws.dump_month_prices(conn, ym)
        ws.dump_month_boards(conn, ym)
        log(f"  {ym}")
    if a.rebuild:
        for ym in sorted({ws.month_of(d) for d in db.trading_days(conn)}):
            ws.dump_month_prices(conn, ym)
            ws.dump_month_boards(conn, ym)
    ws.dump_benchmark(conn)
    ws.dump_latest(conn, d1)
    ws.dump_glossary()          # 名詞說明與本機 GUI 共用同一份
    ws.dump_meta(conn, d1, scheduled=a.scheduled, notes=notes)
    ws.history_index()

    total = 0
    for root, _, files in os.walk("data"):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    log(f"完成。資料夾大小 {total / 1024 / 1024:.2f} MB")
    print(f"::notice::updated {d1}")
    conn.close()
    return 0


def _indices(conn):
    r = twse.fetch_indices()
    conn.executemany(
        "INSERT OR REPLACE INTO benchmark_daily (code,date,close) "
        "VALUES (?,?,?)", [(c, d, v) for c, (d, v) in r.items()])
    return "、".join(f"{c} {v:,.2f}" for c, (_, v) in r.items()) or "沒抓到"


def _margins(conn, date, allow):
    a = [r for r in twse.fetch_margin(date) if r[0] in allow]
    db.upsert_margin(conn, a)
    d2, b = tpex.fetch_margin()
    b = [r for r in b if r[0] in allow]
    db.upsert_margin(conn, b)
    return f"上市 {len(a)} 筆、上櫃 {len(b)} 筆"


def _chips(conn, allow):
    d, rows = tpex.fetch_chip()
    rows = [r for r in rows if r[0] in allow]
    db.upsert_chip(conn, rows)
    return f"{len(rows)} 筆（官方只有上櫃，上市的要靠本機）"


def _valuations(conn, allow):
    d, rows = tpex.fetch_valuation()
    rows = [r for r in rows if r[0] in allow]
    db.upsert_valuation(conn, rows)
    return f"{len(rows)} 筆（官方只有上櫃）"


def _holidays(conn):
    rows = twse.fetch_holidays()
    conn.executemany(
        "INSERT OR REPLACE INTO holiday (date,name,note) VALUES (?,?,?)", rows)
    return f"{len(rows)} 天"


if __name__ == "__main__":
    sys.exit(main())
