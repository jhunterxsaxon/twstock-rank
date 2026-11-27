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
import re
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


SRC_ZH = {"opendata": "開放資料平臺．當日",
          "openapi": "openapi．T+1（備援）"}

# ★ 回補過的日子要記下來，而且要記在**會進 git 的檔案**裡。
#   price_daily.src 不行 —— 月檔 JSON 根本沒存 src，載回來全變 'web'，
#   所以標在 src 上的來源標記活不過一次來回。
#   dump_meta() 會讀這個檔，把回補過的日子寫進 meta.json 的註記，
#   這樣公開頁面上就講得出來，而且每次重跑都還在。
BACKFILL_LOG = os.path.join("data", "backfilled.json")

PROBE_LOG = os.path.join("data", "probe_log.csv")
PROBE_COLS = ("run_at_tpe", "scheduled", "twse_date", "twse_modified_tpe",
              "tpex_date", "tpex_modified_tpe",
              "opendata_twse_date", "opendata_twse_modified_tpe", "action")

# ★ 另一條上市的路：證交所登記在政府資料開放平臺的 CSV（資料集 11549）。
# openapi 確定是 T+1（每天台北 05:20:44~45 一次批次），這條還沒量過。
# 如果它當天就更新，「下午看到當天上市資料」就成立 —— 而且完全合規：
#   第 8 條  「但臺灣證券交易所已授權『政府資料開放平臺』提供公眾使用之
#             本網站資料，不在此限」-> 可散布
#   第 6 條  禁止的是「非依證交所同意之方式」-> 這是他們公告的取用網址
# ★ 所以「不碰 www.twse.com.tw」這條舊規則劃得太廣：它對任意爬網站是對的，
#   但不該涵蓋證交所自己登記為開放資料的網址。
EP_OPENDATA_TWSE = ("https://www.twse.com.tw/exchangeReport/"
                    "STOCK_DAY_ALL?response=open_data")


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


def probe_csv(url):
    """探測一個 CSV 端點，回傳 (資料日期, last-modified 台北)。

    ★ 和 probe() 分開的兩個理由
    1. 這個端點回傳 CSV 不是 JSON。
    2. **它沒有 last-modified 標頭**（實測只有 Date），所以標頭那條
       獨立證據在這裡不存在，只能比對內容日期。

    ★ 日期在第一個「資料列」的第一欄，不是標題列：
         日期,證券代號,證券名稱,成交股數,成交金額,...
         "1151119","0050","元大台灣50",...
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": netio.UA, "Accept": "text/csv, */*"})
    with urllib.request.urlopen(req, timeout=60,
                                context=netio._ssl_context()) as r:
        lm = r.headers.get("last-modified", "")
        text = r.read().decode("utf-8-sig", "replace")
    date = None
    for line in text.splitlines()[1:4]:
        m = re.match(r'^\s*"?(\d{7,8})"?\s*,', line)
        if m:
            date = netio.roc_to_iso(m.group(1))
            if date:
                break
    return date, _gmt_to_tpe(lm)


def append_probe(row):
    """★ 空跑也要留下痕跡

    原本只有「抓到新資料」才 commit，所以沒命中的執行完全沒有紀錄 ——
    結果是只看得到成功的那幾次，官方更新時間的窗口因此寬到 32 分鐘，
    還是用手動觸發才湊巧夾到的。

    這份 CSV 每次執行都會多一行，不管有沒有新資料。有了否定樣本，
    才夾得出官方實際幾點更新。
    """
    os.makedirs(os.path.dirname(PROBE_LOG), exist_ok=True)
    # ★ 欄位增加時要先把舊檔案升級。
    # 這份 CSV 已經在 repo 裡累積了十幾列、表頭是舊的 7 欄。直接追加
    # 新的 9 欄會和表頭對不齊，而 probe_report.py 用 DictReader 讀 ——
    # 欄位錯位不會報錯，只會讓分析結果靜悄悄變成垃圾。
    if os.path.exists(PROBE_LOG):
        with open(PROBE_LOG, encoding="utf-8", newline="") as f:
            old_rows = list(csv.reader(f))
        if old_rows and tuple(old_rows[0]) != PROBE_COLS:
            head = old_rows[0]
            kept = [dict(zip(head, r)) for r in old_rows[1:]]
            with open(PROBE_LOG, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f, lineterminator="\n")
                w.writerow(PROBE_COLS)
                for k in kept:
                    w.writerow([k.get(c, "") for c in PROBE_COLS])
    new = not os.path.exists(PROBE_LOG)
    with open(PROBE_LOG, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        if new:
            w.writerow(PROBE_COLS)
        w.writerow([row.get(c, "") for c in PROBE_COLS])


def read_backfilled():
    """讀回補紀錄。回傳 {date: {"market": ..., "basis": ...}}。"""
    try:
        return json.load(open(BACKFILL_LOG, encoding="utf-8"))
    except Exception:
        return {}


def write_backfilled(d):
    with open(BACKFILL_LOG, "w", encoding="utf-8") as f:
        f.write(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True))


def price_gaps(conn):
    """找出只有單一市場行情的交易日。回傳 [(date, 缺的市場), ...]。

    db.partial_days() 回的是「有哪個市場」，這裡轉成「缺哪個市場」——
    回補要知道的是後者。
    """
    out = []
    for d, have in db.partial_days(conn):
        out.append((d, "tpex" if have == "twse" else "twse"))
    return out


def backfill_prices(conn, only=None):
    """補半套交易日的行情。**只有上櫃補得回來。**

    ★ 上市補不回來
    2026-11-27 查遍 data.gov.tw：上市的登記資料集只有
    `STOCK_DAY_ALL?response=open_data`（只給最新一天）、個股年成交、
    大盤統計、前二十名、盤後定價 —— 沒有依日期查詢的個股日成交。
    平臺上甚至有一個建議案（136936）正是在要這個，狀態「已回復」。
    所以漏掉上市那半，仍然是永久損失。

    ★★ 上櫃回補的來源和每日路徑**基礎不同**，這件事一定要講出來 ★★
    資料集 11371（stk_wn1430 = 14:30 收盤行情）只含**整股**；
    每日路徑走的 openapi 含整股＋零股。2026-11-24 逐檔比對 895 檔：

        開/高/低/收、官方漲跌價差   逐檔完全相同
        成交股數/金額/筆數          CSV 永遠較低
                                   中位數低 1.73%、平均 3.8%
                                   最大 100%（那天只有零股成交的檔）

    所以回補那天的**漲幅榜、跌幅榜、振幅榜完全正確**（只用高低收與
    官方漲跌價差），**成交量榜與成交金額榜會略低**，而且只有零股成交的
    檔會從榜上消失。比「那天完全沒有資料」好得多，但不是同一份資料 ——
    這就是為什麼要寫 data/backfilled.json 並在 meta 註記。
    """
    ZH = {"twse": "上市", "tpex": "上櫃"}
    allow = db.whitelist(conn)
    if not allow:
        log("✖ 白名單是空的，無法繼續")
        return 1

    gaps = price_gaps(conn)
    if only:
        gaps = [g for g in gaps if g[0] == only]
        if not gaps:
            log(f"{only} 不是半套交易日，沒有要補的")
            return 0
    if not gaps:
        log("沒有半套交易日，不用補")
        return 0

    log(f"半套交易日 {len(gaps)} 天：")
    for d, mk in gaps:
        log(f"  {d}  缺 {ZH[mk]}")

    fixed, failed, unfixable = [], [], []
    for d, mk in gaps:
        if mk == "twse":
            # ★ 不要靜悄悄跳過 —— 使用者會以為補好了
            unfixable.append(d)
            log(f"  ✖ {d} 缺上市 —— 官方沒有依日期查詢的上市個股日成交"
                f"資料集，補不回來")
            continue
        try:
            got, rows = tpex.fetch_price_by_date(d)
        except Exception as e:
            failed.append(f"{d}（{e}）")
            log(f"  ✖ {d} 上櫃：{e}")
            continue
        keep = [r for r in rows if r["stock_id"] in allow]
        n = db.upsert_price(conn, [
            (r["stock_id"], d, r["open"], r["high"], r["low"], r["close"],
             int(r["volume"] or 0), r["amount"] or 0, int(r["trades"] or 0),
             "tpex-od", r.get("chg"))
            for r in keep])
        fixed.append(d)
        log(f"  ✔ {d} 上櫃　{n} 筆（對方回報 {got}，原始 {len(rows)} 筆）")
    conn.commit()

    if not fixed:
        log("✖ 一天都沒補到"
            + (f"；{len(unfixable)} 天缺上市（補不回來）" if unfixable else ""))
        return 1

    # ★★ 一定要從最早被補的那天開始往後全部重算榜單 ★★
    # 「連續在榜天數」與「排名變化」是拿前一個交易日的快照推出來的。
    # 在中間插進一個新的完整交易日，它**後面每一天**的 streak 與 move
    # 就都錯了 —— 而那些數字看起來完全正常。
    complete = db.complete_days(conn)
    start = min(fixed)
    todo = [d for d in complete if d >= start]
    log(f"重算榜單（{todo[0]} ~ {todo[-1]}，共 {len(todo)} 天）…")
    for i, d in enumerate(todo, 1):
        ranking.build_date(conn, d, allow=allow)
        if i % 50 == 0:
            log(f"  {i}/{len(todo)}")
    conn.commit()

    # ★ 把回補過的日子記進會進 git 的檔案，meta 才講得出來
    rec = read_backfilled()
    for d in fixed:
        rec[d] = {"market": "tpex", "source": "data.gov.tw 11371",
                  "basis": "整股（不含零股）"}
    write_backfilled(rec)

    log("匯出 JSON…")
    for ym in sorted({ws.month_of(d) for d in todo}):
        ws.dump_month_prices(conn, ym)
        ws.dump_month_boards(conn, ym)
        log(f"  {ym}")
    rank_date = complete[-1]
    ws.dump_latest(conn, rank_date)
    ws.dump_markets(conn, rank_date)
    log("算技術指標…")      # 價量變了，指標跟著變
    ws.dump_technical(conn, progress=log)
    ws.dump_meta(conn, rank_date, notes=[], missing=missing_days(conn, rank_date))
    ws.history_index()
    log(f"完成。補了 {len(fixed)} 天：{'、'.join(fixed)}"
        + (f"；{len(unfixable)} 天缺上市補不回來：{'、'.join(unfixable)}"
           if unfixable else "")
        + (f"；{len(failed)} 項失敗：{'；'.join(failed)}" if failed else ""))
    return 0


def valuation_gaps(conn):
    """找出估值不完整的交易日。回傳 [(date, ['twse', ...]), ...]。

    ★ 分母用「同一天該市場有價量的檔數」，不是寫死 1088/894。
    檔數會變（上市上櫃、新上市、下市），寫死的話某天多一檔就誤判成缺。

    ★ 門檻用一半，不是「等於 0」。
    抓到一半失敗（例如寫了 300 筆就斷線）也要補 ——
    那種情況比完全沒有更難發現，因為頁面上看起來有資料。
    """
    out = []
    for d, in conn.execute(
            "SELECT DISTINCT date FROM price_daily ORDER BY date"):
        need = []
        for mk in ("twse", "tpex"):
            px = conn.execute(
                "SELECT COUNT(*) FROM price_daily p JOIN stock s"
                " ON s.stock_id=p.stock_id WHERE p.date=? AND s.market=?",
                (d, mk)).fetchone()[0]
            vl = conn.execute(
                "SELECT COUNT(*) FROM valuation_daily v JOIN stock s"
                " ON s.stock_id=v.stock_id WHERE v.date=? AND s.market=?",
                (d, mk)).fetchone()[0]
            if px and vl < px * 0.5:
                need.append(mk)
        if need:
            out.append((d, need))
    return out


def backfill_valuations(conn, only=None):
    """補估值。only 給 'YYYY-MM-DD' 就只補那天（兩市都抓）。

    ★ 為什麼這支存在
    兩市的估值發布時間差兩小時，排程落在哪個時段決定當天抓到哪一半。
    以前漏掉就永久消失（openapi 不吃 date），現在補得回來。

    ★ 失敗不中止其他天
    一天抓失敗就記下來繼續 —— 補 10 天時因為第 3 天假日而整批停掉，
    代價比多印幾行錯誤大得多。
    """
    ZH = {"twse": "上市", "tpex": "上櫃"}
    allow = db.whitelist(conn)
    if not allow:
        log("✖ 白名單是空的，無法過濾")
        return 1

    if only:
        todo = [(only, ["twse", "tpex"])]
        log(f"指定補 {only}（兩市都抓）")
    else:
        todo = valuation_gaps(conn)
        if not todo:
            log("估值沒有缺口，不用補")
            return 0
        log(f"估值有 {len(todo)} 天不完整：")
        for d, mks in todo:
            log(f"  {d}  缺 {'、'.join(ZH[m] for m in mks)}")

    fns = {"twse": twse.fetch_valuation, "tpex": tpex.fetch_valuation}
    done, failed, months = 0, [], set()
    for d, mks in todo:
        for mk in mks:
            try:
                got, rows = fns[mk](d)
            except Exception as e:
                failed.append(f"{d} {ZH[mk]}（{e}）")
                log(f"  ✖ {d} {ZH[mk]}：{e}")
                continue
            rows = [r for r in rows if r[0] in allow]
            n = db.upsert_valuation(conn, rows)
            done += n
            months.add(ws.month_of(d))
            log(f"  ✔ {d} {ZH[mk]}　{n} 筆（對方回報 {got}）")
    conn.commit()

    if not months:
        log("✖ 一天都沒補到")
        return 1

    # ★ 估值出現在三個檔，少寫一個就是「補了但頁面沒變」：
    #     月檔            歷史估值（本機版 sync_from_web 讀這個）
    #     fundamental.json  基本面頁顯示的那份（最新交易日）
    #     latest.json       首頁快照裡的 valuations
    log("匯出 JSON…")
    for ym in sorted(months):
        ws.dump_month_prices(conn, ym)
        log(f"  {ym}")
    ws.dump_fundamental(conn)
    complete = db.complete_days(conn)
    if complete:
        ws.dump_latest(conn, complete[-1])
        log(f"  latest.json（{complete[-1]}）")
    log(f"完成。寫入 {done:,} 筆估值"
        + (f"；{len(failed)} 項失敗：{'；'.join(failed)}" if failed else ""))
    return 0


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
    ap.add_argument("--backfill-valuation", nargs="?", const="auto",
                    default=None, metavar="YYYY-MM-DD",
                    help="補估值不完整的交易日（兩市的估值都吃日期）。"
                         "給日期只補那天，不給就自動找出缺口")
    ap.add_argument("--backfill-price", nargs="?", const="auto",
                    default=None, metavar="YYYY-MM-DD",
                    help="補只有單一市場的交易日。★ 只有上櫃補得回來，"
                         "而且來源只含整股（見 backfill_prices 說明）")
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

    # ★ 回補要在探測之前分支出去。
    #   探測底下有「兩市都沒有新資料 -> 結束」的提早返回，
    #   而回補正是要在沒有新資料的時候也能跑。
    if a.backfill_price:
        rc = backfill_prices(
            conn, None if a.backfill_price == "auto" else a.backfill_price)
        conn.close()
        return rc

    if a.backfill_valuation:
        rc = backfill_valuations(
            conn,
            None if a.backfill_valuation == "auto" else a.backfill_valuation)
        conn.close()
        return rc

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
    # ★ 另一條上市的路，只探測不使用 —— 先量出它幾點更新再決定要不要接。
    try:
        od_date, od_lm = probe_csv(EP_OPENDATA_TWSE)
    except Exception as e:
        od_date, od_lm = None, ""
        log(f"  （開放資料平臺端點探測失敗，不影響判斷：{e}）")
    log(f"  上市 {latest}　檔案生成於 {twse_lm or '（未提供）'}")
    log(f"  上市(開放資料平臺) {od_date}"
        + ("　★ 比 openapi 新，當日資料走這條路拿得到"
           if od_date and latest and od_date > latest else ""))
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
    # ★★ 要不要抓，看的是「任一市場有我們還沒有的日期」★★
    #
    # 原本寫 `latest in db.complete_days(conn)`，而 latest 來自 **openapi**
    # （T+1）。上市來源換成開放資料平臺之後，這個判斷每次都會說
    # 「11-19 已經有了 → 結束」，當日資料永遠抓不到 —— 換來源等於白換。
    #
    # 而且它把兩件事混在一起：「這個日期完整嗎」和「有沒有新東西可抓」。
    # 兩市發布時間差兩小時，必須逐市場看：
    #     上市有 11-20 但我們只有到 11-19  -> 抓
    #     上櫃有 11-20 但我們只有到 11-19  -> 抓
    #     兩邊都沒有新的                   -> 跳過
    # 這樣 14:00~16:00 會先抓到上市那半，16:00 之後再補上櫃。
    twse_new = bool(od_date) and od_date not in db.market_dates(conn, "twse")
    tpex_new = bool(tpex_date) and tpex_date not in db.market_dates(conn, "tpex")
    skip = not (twse_new or tpex_new) and not a.force and not a.rebuild
    if twse_new or tpex_new:
        log(f"  有新資料：{'上市 ' + od_date if twse_new else ''}"
            f"{'　' if twse_new and tpex_new else ''}"
            f"{'上櫃 ' + tpex_date if tpex_new else ''}")
    append_probe({
        "run_at_tpe": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scheduled": a.scheduled or "手動",
        "twse_date": latest or "", "twse_modified_tpe": twse_lm,
        "tpex_date": tpex_date or "", "tpex_modified_tpe": tpex_lm,
        "opendata_twse_date": od_date or "",
        "opendata_twse_modified_tpe": od_lm,
        "action": "skip" if skip else "fetch"})
    if skip:
        log(f"  兩市都沒有新資料（上市 {od_date or latest}、上櫃 {tpex_date}）"
            f" → 結束（探測紀錄已留存）")
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
    d1, rows1, src1 = twse.fetch_price(
        on_fallback=lambda e: log(f"  ⚠ 開放資料平臺失敗，退回 openapi：{e}"))
    d2, rows2 = tpex.fetch_price()
    log(f"  上市 {d1}（{SRC_ZH.get(src1, src1)}）原始 {len(rows1)} 筆"
        f"｜上櫃 {d2} 原始 {len(rows2)} 筆")
    if src1 != "opendata":
        # ★ 備援拿到的是**前一個交易日**的資料，而它看起來完全正常。
        #   不講出來的話，「今天的榜單其實是昨天的」就無聲無息。
        notes.append("上市資料來自備援端點（openapi，隔天清晨才更新），"
                     "不是當日的開放資料平臺")
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
             int(r["volume"] or 0), r["amount"] or 0, int(r["trades"] or 0),
             src, r.get("chg"))
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
    # ★★ 榜單只能算在「兩市都有資料」的日期上 ★★
    # 上市來源換成開放資料平臺之後，上市當天約 14:00 就有、上櫃要等 16:00，
    # 所以**每天 14:00~16:00 都會有一個只有上市的半套日期**。
    # 拿它算榜單會得到只有 1,091 檔的榜 —— 而且數字看起來完全正常。
    # （2026-11-20 14:52 實測到：所有榜單的市場都只有 twse。）
    #
    # ★ --rebuild 也要過濾。這裡的半套日期不是建庫途中的暫時現象，
    #   而是發布時間差造成的真實資料，回溯重算一樣不能用它。
    complete = db.complete_days(conn)
    if not complete:
        log("✖ 還沒有任何「兩市都有資料」的交易日，無法算榜單")
        return 1
    rank_date = complete[-1]
    if rank_date != d1:
        log(f"  * 榜單算 {rank_date}（兩市都有），不是 {d1}")
    dates = [rank_date] if not a.rebuild else complete
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
    # ★ d1 的月份也要匯出：今天的上市資料雖然還不能算榜單，
    #   但它已經寫進 price_daily 了，月檔要跟著更新，
    #   否則等上櫃補上時會發現上市那半沒存到。
    touched = sorted({ws.month_of(d) for d in dates}
                     | {ws.month_of(d1), ws.month_of(d2)})
    for ym in touched:
        ws.dump_month_prices(conn, ym)
        ws.dump_month_boards(conn, ym)
        log(f"  {ym}")
    if a.rebuild:
        for ym in sorted({ws.month_of(d) for d in db.trading_days(conn)}):
            ws.dump_month_prices(conn, ym)
            ws.dump_month_boards(conn, ym)
    ws.dump_benchmark(conn)
    ws.dump_latest(conn, rank_date)
    ws.dump_markets(conn, rank_date)
    ws.dump_fundamental(conn)
    log("算技術指標…")
    ws.dump_technical(conn, progress=log)
    ws.dump_glossary()          # 名詞說明與本機 GUI 共用同一份

    part2 = db.partial_days(conn)
    if part2:
        # ★ 2026-11-20 起方向反過來了：上市比較快。
        #   上市走開放資料平臺，當天約 14:00 就有；上櫃要等 16:00。
        #   所以每天 14:00~16:00 之間會看到「只有上市」的半套日期。
        notes.append(
            "；".join(f"{d} 目前只有{'上市' if m == 'twse' else '上櫃'}的資料"
                      for d, m in part2)
            + " —— 上市當天約 14:00 就發布、上櫃要等 16:00，"
              "所以下午那兩個小時會有半邊的落差。等另一邊補上就會自動完整，"
              "榜單在那之前仍以前一個完整交易日為準。")
    miss = missing_days(conn, rank_date)
    if miss:
        log(f"⚠ 疑似漏掉 {len(miss)} 個交易日：{'、'.join(miss)}")
        notes.append(
            f"疑似漏掉 {len(miss)} 個交易日（{'、'.join(miss)}）—— "
            f"官方 API 不支援查詢過去的日期，這些日子補不回來")
    ws.dump_meta(conn, rank_date, scheduled=a.scheduled, notes=notes,
                 missing=miss)
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
