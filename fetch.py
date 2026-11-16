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
         （排程每 2 小時跑一次，就是靠這個避免產生重複的 commit）
    3. 抓官方資料 → SQLite
    4. 用和本機版完全相同的 ranking.py 算榜單
    5. 匯出 JSON

用法：
    python fetch.py                 正常執行
    python fetch.py --force         即使沒有新資料也重跑（修資料時用）
    python fetch.py --rebuild       重算所有歷史榜單後再匯出
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
import traceback
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from core import (db, fundamental as fd, netio, ranking, tpex, twse,
                  universe, webstore as ws)


def log(m):
    """★ 印不出來不可以變成「抓到一半爆掉」

    Windows 主控台預設編碼是 cp950，印到 U+26A0（警告符號）這類字元會丟
    UnicodeEncodeError。實際踩過：抓完資料、JSON 匯出到一半時才爆，
    留下沒有 meta.json 的半套 data/。

    顯示問題不該毀掉資料，所以這裡一律降級處理。
    （GitHub Actions 上是 UTF-8，只有本機執行才踩得到。）
    """
    line = f"[{datetime.now():%H:%M:%S}] {m}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)


def open_db():
    """暫時的 SQLite，只活在這次執行期間。JSON 才是真正的存檔。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    return conn


PROBE_LOG = os.path.join("data", "probe_log.csv")
PROBE_COLS = ("run_at_tpe", "scheduled", "twse_date", "twse_modified_tpe",
              "tpex_date", "tpex_modified_tpe", "action")


def _gmt_to_tpe(s):
    """把 HTTP 的 Last-Modified（GMT）轉成台北時間。"""
    if not s:
        return ""
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%d %H:%M:%S")


def probe(url):
    """打一個端點，同時取回「資料日期」與「這個檔案幾點生成」。

    ★ last-modified 才是關鍵
    只看內容的日期，知道的是「現在是哪一天的資料」，不知道它何時變成這樣。
    官方的檔案帶 last-modified，直接說出那個檔案是幾點生成的。
    實測 2026-11-16：證交所 143 支端點全部在台北 05:20~05:29 同一批次生成，
    櫃買則是 14:00 —— 這是推不出來、只能讀標頭才知道的事。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": netio.UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60,
                                context=netio._ssl_context()) as r:
        lm = r.headers.get("last-modified", "")
        data = json.loads(r.read().decode("utf-8-sig"))
    d = netio.roc_to_iso(data[0].get("Date")) if data else None
    return d, _gmt_to_tpe(lm)


def append_probe(row):
    """★ 空跑也要留下痕跡

    原本只有「抓到新資料」才 commit，所以沒命中的執行完全沒有紀錄 ——
    結果是只看得到成功的那幾次，官方更新時間的窗口因此寬到 32 分鐘，
    還是用手動觸發才湊巧夾到的。

    這份 CSV 每次執行都會多一行，不管有沒有新資料。有了否定樣本，
    才夾得出官方實際幾點更新。
    """
    new = not os.path.exists(PROBE_LOG)
    os.makedirs(os.path.dirname(PROBE_LOG), exist_ok=True)
    with open(PROBE_LOG, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        if new:
            w.writerow(PROBE_COLS)
        w.writerow([row.get(c, "") for c in PROBE_COLS])


def missing_days(conn, latest):
    """找出「照理說該有、但我們沒有」的交易日。

    ★ 為什麼需要這個
    官方 OpenAPI 只提供**最新一個交易日**，不支援指定日期（實測確認：
    傳 date 參數會被忽略）。所以一旦某天沒抓到，那天就**永久補不回來**。

    2026-11-13（週五）就是這樣掉的 —— 舊排程只跑週一到週五，而週五的
    資料要到週六清晨才出現。當時完全沒有任何警告，meta.json 上只會看到
    trading_days 少一個，沒人會發現。

    ★ 判斷依據與限制
    「平日扣掉休市日曆」。颱風假這類臨時休市不會事先出現在日曆上，
    所以這裡回報的是**疑似**漏掉，不是確定漏掉 —— 寧可多報也不要漏報，
    因為漏報的代價是資料永久消失。
    """
    have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM price_daily")}
    if not have or not latest:
        return []
    holi = {r[0] for r in conn.execute("SELECT date FROM holiday")}
    d, end = date.fromisoformat(min(have)), date.fromisoformat(latest)
    out = []
    while d <= end:
        s = d.isoformat()
        if d.weekday() < 5 and s not in holi and s not in have:
            out.append(s)
        d += timedelta(days=1)
    return out


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
    n_eps, n_rev = ws.load_fundamental(conn)
    have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM price_daily")}
    part = db.partial_days(conn)
    log(f"  已有 {n_days} 個交易日"
        + (f"（{min(have)} ~ {max(have)}）" if have else "（全新的 repo）"))
    if n_eps or n_rev:
        log(f"  基本面：財報 {n_eps:,} 筆、月營收 {n_rev:,} 筆")
    if part:
        log("  ⚠ 只有單一市場的日期："
            + "、".join(f"{d}（只有{m}）" for d, m in part))

    log("查官方最新交易日…")
    try:
        latest, twse_lm = probe(twse.EP_PRICE)
    except Exception as e:
        log(f"✖ 連線失敗：{e}")
        traceback.print_exc()
        return 1
    try:
        tpex_date, tpex_lm = probe(tpex.EP_PRICE)
    except Exception as e:
        tpex_date, tpex_lm = None, ""
        log(f"  （上櫃探測失敗，不影響判斷：{e}）")
    log(f"  上市 {latest}　檔案生成於 {twse_lm or '（未提供）'}")
    log(f"  上櫃 {tpex_date}　檔案生成於 {tpex_lm or '（未提供）'}")
    # ★ 這裡原本每次都會寫一條「官方資料仍停在 X（今天是 Y）」的註記。
    # 但官方是隔天清晨才發布，latest 永遠不可能等於今天 —— 等於每跑必出，
    # 結果就是網頁上的警告區永遠有東西，真正要緊的警告（漏掉交易日、
    # 榜單鑑別度不足）反而被稀釋掉。改由 meta 的 age_days 表達落後幾天。

    # ★ 這裡**不能**用 `latest in have`。
    # 櫃買 16:00 就發布當日資料，所以 price_daily 裡可能已經有一個
    # 只含上櫃的 `latest`。用 have 判斷的話，隔天證交所出同一天的資料時
    # 會被當成「已經有了」而跳過 —— 上市那 1,090 檔永遠抓不到，
    # 而且完全無聲無息，只會看到榜單停住。
    done = set(db.complete_days(conn))
    skip = latest in done and not a.force and not a.rebuild
    append_probe({
        "run_at_tpe": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scheduled": a.scheduled or "手動",
        "twse_date": latest or "", "twse_modified_tpe": twse_lm,
        "tpex_date": tpex_date or "", "tpex_modified_tpe": tpex_lm,
        "action": "skip" if skip else "fetch"})
    if skip:
        log(f"  {latest} 的資料已經有了 → 結束（探測紀錄已留存）")
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
            ("本益比（兩市）", lambda: _valuations(conn, allow)),
            ("EPS 與財報", lambda: _eps(conn, allow)),
            ("月營收", lambda: _revenue(conn, allow)),
            ("除權息", lambda: _dividends(conn, allow)),
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
    ws.dump_fundamental(conn)
    log("算技術指標…")
    ws.dump_technical(conn, progress=log)
    ws.dump_glossary()          # 名詞說明與本機 GUI 共用同一份

    part2 = db.partial_days(conn)
    if part2:
        notes.append(
            "；".join(f"{d} 目前只有{'上市' if m == 'twse' else '上櫃'}的資料"
                      for d, m in part2)
            + " —— 櫃買台北 16:00 就發布當日收盤，證交所要等隔天清晨，"
              "所以傍晚到清晨之間會有半天的落差。等證交所補上就會自動完整。")
    miss = missing_days(conn, d1)
    if miss:
        log(f"⚠ 疑似漏掉 {len(miss)} 個交易日：{'、'.join(miss)}")
        notes.append(
            f"疑似漏掉 {len(miss)} 個交易日（{'、'.join(miss)}）—— "
            f"官方 API 不支援查詢過去的日期，這些日子補不回來")
    ws.dump_meta(conn, d1, scheduled=a.scheduled, notes=notes, missing=miss)
    ws.history_index()

    total = 0
    for root, _, files in os.walk("data"):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    log(f"完成。資料夾大小 {total / 1024 / 1024:.2f} MB")
    print(f"::notice::updated {d1}")
    conn.close()
    return 0


def _dividends(conn, allow):
    """除權息事件。★ 還原股價要用，不抓的話除息當天會變成假跌破訊號。

    證交所只給股利金額、櫃買直接給除權息前後價 ——
    兩種格式的差異由 core/technical.py 的 factors_all() 吸收。
    """
    out = []
    for name, fn in (("上市", twse.fetch_dividends),
                     ("上櫃", tpex.fetch_dividends)):
        rows = [r for r in fn() if r[0] in allow]
        conn.executemany(
            "INSERT INTO dividend (stock_id,date,cash,stock_div,"
            "before_px,after_px) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(stock_id,date) DO UPDATE SET "
            "cash=excluded.cash, stock_div=excluded.stock_div, "
            "before_px=COALESCE(excluded.before_px, dividend.before_px), "
            "after_px=COALESCE(excluded.after_px, dividend.after_px)", rows)
        out.append(f"{name} {len(rows)} 筆")
    return "、".join(out)


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
    """★ 2026-11-16 起兩市都有了。

    證交所的 BWIBBU_ALL 在 2026-11-10 曾回「因為安全性考量…」的 HTML，
    專案因此一度記載「上市本益比只能靠 FinMind」。複測已經正常，
    所以這裡把上市也加進來。
    """
    out = []
    for name, fn in (("上市", twse.fetch_valuation),
                     ("上櫃", tpex.fetch_valuation)):
        try:
            _d, rows = fn()
            rows = [r for r in rows if r[0] in allow]
            db.upsert_valuation(conn, rows)
            out.append(f"{name} {len(rows)} 筆")
        except Exception as e:
            out.append(f"{name} 失敗（{e}）")
    return "、".join(out)


def _eps(conn, allow):
    """EPS 與單季財報（官方開放資料，可散布）。

    ★ misses 要印出來
    欄位對不上時 .get() 只會回 None，不報錯，整欄靜悄悄變空白 ——
    看起來像「這些公司沒公布」。所以把對不到的數量印出來：
    個位數是正常的（金控、保險業本來就不報「營業利益」），
    若接近總筆數就代表欄位名又改了。
    """
    out = []
    for mk in ("twse", "tpex"):
        period, rows, miss = fd.fetch_eps(mk)
        rows = [r for r in rows if r[0] in allow]
        db.upsert_fundamental(conn, rows)
        extra = f"（欄位對不到：{miss}）" if miss else ""
        out.append(f"{mk} {period} {len(rows)} 筆{extra}")
    return "、".join(out)


def _revenue(conn, allow):
    """每月營業收入（含官方自己算的月增率、年增率、累計）。

    ★ 年增率不自己算
    官方在併購、會計年度變更時的分母處理我們無從得知，
    自己算會和官方公布的數字對不起來 —— 那種不一致最難解釋。
    """
    out = []
    for mk in ("twse", "tpex"):
        ym, rows, miss = fd.fetch_revenue(mk)
        rows = [r for r in rows if r[0] in allow]
        db.upsert_revenue(conn, rows)
        extra = f"（欄位對不到：{miss}）" if miss else ""
        out.append(f"{mk} {ym} {len(rows)} 筆{extra}")
    return "、".join(out)


def _holidays(conn):
    rows = twse.fetch_holidays()
    conn.executemany(
        "INSERT OR REPLACE INTO holiday (date,name,note) VALUES (?,?,?)", rows)
    return f"{len(rows)} 天"


if __name__ == "__main__":
    sys.exit(main())
