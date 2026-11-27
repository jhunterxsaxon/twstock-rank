# -*- coding: utf-8 -*-
"""證交所（上市）官方 OpenAPI。

授權：swagger 寫「歡迎各位介接使用」，授權指向政府資料開放授權條款
（永久、免授權金、允許再散布與再轉授權，但**未標示來源視為自始未授權**）。
公開發布時必須標示：臺灣證券交易所 [年份] [資料集名稱]

## 刻意不使用的端點

`www.twse.com.tw/exchangeReport/...?date=` 這類舊網址**技術上可以回溯歷史**
（實測 `/fund/T86?date=20260504` 真的回得出當天全市場資料），
但它們不在 OpenAPI 文件內，而證交所使用條款第 6 條明文禁止
「透過自動化裝置、指令碼、爬蟲程式下載本網站資料」。**所以不用。**

## 已知問題（2026-11-10 實測）

- **OpenAPI 完全不吃 `date` 參數**。實測 `STOCK_DAY_ALL?date=20260504` 回 HTTP 200，
  但資料全是最新交易日 —— 參數被忽略。所以官方 API 只能每日累積，不能回補。
- **`BWIBBU_ALL`（本益比）2026-11-16 複測已經正常**，回 1,086 筆，
  含 PEratio / DividendYield / PBratio。
  ★ 2026-11-10 當時它回 HTML「因為安全性考量，您所執行的頁面無法呈現」，
  所以專案一度判定「上市本益比只能靠 FinMind」。**那個結論已經過時。**
  `BWIBBU_d` 還多給 ClosePrice 與 FiscalYearQuarter。
- **`MI_MARGN` 回傳的資料裡沒有日期欄位**，要用同一輪 `STOCK_DAY_ALL` 的日期。
- 證交所的「上市個股日成交資訊」含 ETF 與特別股（1,389 筆中有 245 筆 00 開頭），
  所以一定要過白名單。
"""
import csv
import io as _io

from core import netio

BASE = "https://openapi.twse.com.tw/v1"

EP_PRICE = f"{BASE}/exchangeReport/STOCK_DAY_ALL"

# ★★ 上市當日資料走這條，不是 openapi ★★
#
# 2026-11-20 實測：
#     13:45  這個網址 -> 2026-11-19
#     14:29  這個網址 -> 2026-11-20   ← 當天（收盤後約 1 小時）
#     14:29  openapi  -> 2026-11-19   ← T+1，連續五天 last-modified 05:20:44~45
#
# 兩邊欄位完全一樣，所以是直接替換。
#
# ★ 合規依據：這是證交所**自己登記在政府資料開放平臺**的資源
#   （資料集 11549，授權為政府資料開放授權條款第 1 版）
#     第 8 條  「但臺灣證券交易所已授權『政府資料開放平臺』提供公眾使用之
#               本網站資料，不在此限」  -> 可以公開散布
#     第 6 條  禁止「**非依**證交所同意之方式」下載
#                                      -> 這是他們公告的取用網址
#   舊規則「不碰 www.twse.com.tw」對「任意爬網站」是對的，
#   但不該涵蓋證交所自己登記為開放資料的網址。
EP_PRICE_OPENDATA = ("https://www.twse.com.tw/exchangeReport/"
                     "STOCK_DAY_ALL?response=open_data")
EP_MARGIN = f"{BASE}/exchangeReport/MI_MARGN"
EP_COMPANY = f"{BASE}/opendata/t187ap03_L"
EP_INDEX = f"{BASE}/exchangeReport/MI_INDEX"
EP_HOLIDAY = f"{BASE}/holidaySchedule/holidaySchedule"
EP_PER = f"{BASE}/exchangeReport/BWIBBU_ALL"      # 本益比／殖利率／股價淨值比

# ★★ 估值走這條，不是 openapi ★★
#
# 2026-11-27 查 data.gov.tw 資料集 11764 得到（★ 搜尋 API 要 Key，
# 但單筆查詢 /api/v2/rest/dataset/{id} 不用 —— 之前以為整個平臺都要）：
#     名稱  盤後資訊 > 個股日本益比、殖利率及股價淨值比（依日期查詢）
#     授權  1（政府資料開放授權條款第 1 版）、每日更新
#
# 和 EP_PRICE_OPENDATA（資料集 11549）**同一個合規基礎** ——
# 證交所自己登記在政府資料開放平臺的網址，不是猜出來的。
#
# ★★ 關鍵差異：這條吃 `&date=YYYYMMDD` ★★
# 2026-11-27 08:18 實測：
#     不帶日期       -> 20261126，1,088 檔（當日發布；當時今天的還沒出）
#     date=20261123  -> 20261123，1,088 檔
#
# 所以漏掉的那天**補得回來**。這推翻了本專案先前寫在好幾處的結論
# 「官方只能每日累積，漏掉就永久消失」—— 那對 openapi 仍然成立，
# 對這條不成立。（價量能不能也這樣補，還沒查，是另一件事。）
#
# 欄位比 openapi 多三個：收盤價、股利年度、財報年/季。我們只取三個值，
# 其餘不存 —— 收盤價自己已經有一份，用別人的對不起來時很難查。
EP_PER_OPENDATA = ("https://www.twse.com.tw/rwd/zh/afterTrading/"
                   "BWIBBU_d?response=open_data")
EP_EPS = f"{BASE}/opendata/t187ap14_L"            # 各年度 EPS 統計
EP_DIVIDEND = f"{BASE}/exchangeReport/TWT48U_ALL"  # 除權息預告表
EP_REVENUE = f"{BASE}/opendata/t187ap05_L"        # 每月營業收入彙總表

SOURCE_CREDIT = "臺灣證券交易所"

_limiter = netio.RateLimiter(1.0)   # 官方沒公告頻率限制，自我約束 1 秒一次


def _norm(k):
    """欄位名正規化，用來容忍空白與大小寫差異。"""
    return "".join(c for c in str(k).lower() if c.isalnum())


def _pick(row, *patterns, required=True):
    """從一列裡找欄位值。patterns 是正規化後的「完整相等」或「包含」條件。

    ★ 找不到就丟錯，並把實際的欄位名列出來。
    絕對不要靜默回 0 —— 官方改欄位名的時候，靜默的 0 會讓整份籌碼資料
    變成一堆看起來正常的零，而且不會有任何人發現。
    """
    nk = {_norm(k): v for k, v in row.items()}
    for p in patterns:
        if p in nk:
            return nk[p]
    for p in patterns:
        for k, v in nk.items():
            if p in k:
                return v
    if required:
        raise netio.FetchError(
            f"找不到欄位 {patterns!r}；實際欄位：{sorted(row.keys())}")
    return None


# ---------------- 日行情 ----------------

# 開放資料 CSV 的欄位名 -> 我們的欄位名。
# ★ 照「欄位名」對應，不是照位置 —— CSV 的欄序和 JSON 不一樣
#   （CSV 是 日期,代號,名稱,股數,金額,開,高,低,收,漲跌,筆數），
#   寫死位置的話對方調整欄序就會靜悄悄全錯。
_CSV_MAP = {
    "證券代號": "stock_id", "證券名稱": "name",
    "開盤價": "open", "最高價": "high", "最低價": "low", "收盤價": "close",
    "成交股數": "volume", "成交金額": "amount", "成交筆數": "trades",
    "漲跌價差": "chg",
}


def _parse_price_csv(text):
    """解析開放資料平臺的 STOCK_DAY_ALL CSV。回傳 (date, rows)。"""
    rdr = csv.DictReader(_io.StringIO(text))
    if not rdr.fieldnames:
        raise netio.FetchError("開放資料 CSV 沒有標題列")
    missing = [k for k in _CSV_MAP if k not in rdr.fieldnames]
    if missing:
        # 欄位名變了就停下來，不要用殘缺的資料繼續 ——
        # 少一個欄位會讓整欄變 None，而榜單還是算得出來，看起來正常。
        raise netio.FetchError(
            f"開放資料 CSV 缺少欄位 {missing}，實際有 {rdr.fieldnames}")
    date, out = None, []
    for r in rdr:
        sid = (r.get("證券代號") or "").strip()
        if not sid:
            continue
        if date is None:
            date = netio.roc_to_iso((r.get("日期") or "").strip())
        row = {"stock_id": sid}
        for src_key, dst in _CSV_MAP.items():
            if dst in ("stock_id", "name"):
                continue
            row[dst] = netio.num(r.get(src_key),
                                 0 if dst in ("volume", "amount", "trades")
                                 else None)
        row["name"] = (r.get("證券名稱") or "").strip()
        out.append(row)
    if not out:
        raise netio.FetchError("開放資料 CSV 沒有資料列")
    if not date:
        raise netio.FetchError("開放資料 CSV 無法解析日期")
    return date, out


def fetch_price_opendata():
    """開放資料平臺的 CSV（**當日**，收盤後約 1 小時）。"""
    return _parse_price_csv(netio.get_text(EP_PRICE_OPENDATA,
                                           limiter=_limiter))


def fetch_price_openapi():
    """openapi 的 JSON（**T+1**，隔天清晨 05:20）。保留當備援。"""
    data = netio.get_json(EP_PRICE, limiter=_limiter)
    if not data:
        raise netio.FetchError("STOCK_DAY_ALL 回傳空清單")
    date = netio.roc_to_iso(data[0].get("Date"))
    if not date:
        raise netio.FetchError(f"無法解析日期：{data[0].get('Date')!r}")
    out = []
    for r in data:
        sid = str(r.get("Code", "")).strip()
        if not sid:
            continue
        out.append({
            "stock_id": sid,
            "name": str(r.get("Name", "")).strip(),
            "open": netio.num(r.get("OpeningPrice")),
            "high": netio.num(r.get("HighestPrice")),
            "low": netio.num(r.get("LowestPrice")),
            "close": netio.num(r.get("ClosingPrice")),
            "volume": netio.num(r.get("TradeVolume"), 0),
            "amount": netio.num(r.get("TradeValue"), 0),
            "trades": netio.num(r.get("Transaction"), 0),
            "chg": netio.num(r.get("Change")),
        })
    return date, out


def fetch_price(on_fallback=None):
    """當日全市場日成交資訊（含 ETF、特別股，未過濾）。

    回傳 (date, rows, source)，source 是 'opendata' 或 'openapi'。
    rows 是 dict 清單：
      stock_id, name, open, high, low, close, volume, amount, trades, chg

    ★ 為什麼要把 source 回傳出去
    備援拿到的是**前一個交易日**的資料，而它看起來完全正常 ——
    榜單照樣算得出來、網頁照樣顯示。如果不告訴呼叫端走了哪一條，
    「今天的榜單其實是昨天的」這件事就會無聲無息。
    """
    try:
        date, rows = fetch_price_opendata()
        return date, rows, "opendata"
    except (netio.FetchError, netio.StopFetching) as e:
        if on_fallback:
            on_fallback(e)
    date, rows = fetch_price_openapi()
    return date, rows, "openapi"


# ---------------- 融資融券 ----------------

def fetch_margin(date):
    """當日全市場融資融券餘額（單位：張）。

    ★ 這個端點回傳的資料**沒有日期欄位**，所以 date 要由呼叫端給
    （用同一輪 STOCK_DAY_ALL 的日期）。

    中文欄位，而且融券相關欄位在沒有交易時是**空字串**不是 0 ——
    `netio.num(..., 0)` 會把空字串轉成 0，這是對的：沒有人融券買進就是 0 張。
    """
    data = netio.get_json(EP_MARGIN, limiter=_limiter)
    out = []
    for r in data:
        sid = str(_pick(r, "股票代號", "代號") or "").strip()
        if not sid:
            continue
        out.append((
            sid, date,
            int(_num0(_pick(r, "融資今日餘額"))),
            int(_num0(_pick(r, "融資前日餘額"))),
            int(_num0(_pick(r, "融券今日餘額"))),
            int(_num0(_pick(r, "融券前日餘額"))),
            "twse",
        ))
    return out


def _num0(v):
    n = netio.num(v, 0)
    return 0 if n is None else n


# ---------------- 上市公司基本資料（白名單來源） ----------------

def fetch_companies():
    """上市公司基本資料。

    ★ 這就是白名單的來源。實測 1,099 筆裡「00 開頭 0 筆」——
    **ETF 不是公司，根本不會出現在這張表**，所以用它當白名單比用代號規則可靠。
    （`0050` 本身是 4 碼純數字，純靠位數會漏掉它。）

    回傳 dict 清單：stock_id, name, industry, listed_date
    """
    data = netio.get_json(EP_COMPANY, limiter=_limiter)
    out = []
    for r in data:
        sid = str(_pick(r, "公司代號", "代號") or "").strip()
        if not sid:
            continue
        out.append({
            "stock_id": sid,
            "name": str(_pick(r, "公司簡稱", "公司名稱") or "").strip(),
            "industry": str(_pick(r, "產業別", required=False) or "").strip(),
            "listed_date": netio.roc_to_iso(
                _pick(r, "上市日期", required=False)) or _iso8(
                _pick(r, "上市日期", required=False)),
        })
    return out


def _iso8(s):
    """t187ap03_L 的上市日期是西元 8 碼（'19620209'），不是民國。"""
    t = str(s or "").strip()
    if len(t) == 8 and t.isdigit():
        return f"{t[:4]}-{t[4:6]}-{t[6:]}"
    return None


# ---------------- 基準指數 ----------------

# ★ 這兩個是**不同的指數**，數值差很多（2026-11-09 實測：41,661 vs 96,338）。
#   價格指數不含股利，報酬指數含股利再投入。
#   回測的策略用的是**還原股價**（等於股利再投入），所以正確的對照是報酬指數 ——
#   拿價格指數比會每年高估策略約 3~4%（台股平均殖利率）。
TAIEX_PRICE_NAME = "發行量加權股價指數"
TAIEX_TR_NAME = "發行量加權股價報酬指數"


def fetch_indices():
    """抓加權「價格指數」與「報酬指數」。回傳 {code: (date, close)}。

    MI_INDEX 一次回全部 267 個指數，從裡面挑出要的兩個。

    ★★ 絕對不要把不同指數存進同一個代碼 ★★
    實測踩過：run_daily 存的是價格指數（41,661），run_history 從 FinMind
    抓的是報酬指數（91,806），兩個被存成同一個 'TAIEX'——
    接縫那天會出現一個 120% 的假跳空，整個基準對照失真。
    所以每個代碼只能有一個來源：
        TAIEX_PRICE  證交所價格指數（這裡，每日累積）
        TAIEX_TR     證交所報酬指數（這裡，每日累積，★ 可公開散布）
        TAIEX        FinMind 報酬指數（run_history，有 5 年歷史，不可散布）
    """
    data = netio.get_json(EP_INDEX, limiter=_limiter)
    want = {TAIEX_PRICE_NAME: "TAIEX_PRICE", TAIEX_TR_NAME: "TAIEX_TR"}
    out = {}
    for r in data:
        name = str(_pick(r, "指數", required=False) or "").strip()
        if name in want:
            d = netio.roc_to_iso(_pick(r, "日期", required=False))
            c = netio.num(_pick(r, "收盤指數", required=False))
            if d and c:
                out[want[name]] = (d, c)
    return out


# ---------------- 休市日曆 ----------------

# 估值 CSV 的欄位對應。**依欄位名**解析，不靠位置 ——
# 對方調整欄序時，寫死位置會靜悄悄地全錯（本益比變成殖利率那種）。
_PER_CSV_MAP = {"本益比": "per", "股價淨值比": "pbr", "殖利率(%)": "yield_pct"}


def _parse_per_csv(text):
    """解析資料集 11764 的 BWIBBU_d CSV。回傳 (date, rows)。

    rows 是 db.upsert_valuation() 要的 tuple：
        (stock_id, date, per, pbr, yield_pct, src)

    ★ 本益比、殖利率可能是空字串（虧損或不配息的公司）。
    netio.num() 會回 None，**不要塞 0** —— 本益比 0 和「沒有本益比」
    在排序與篩選上是完全不同的意思。
    """
    rdr = csv.DictReader(_io.StringIO(text))
    if not rdr.fieldnames:
        raise netio.FetchError("估值 CSV 沒有標題列")
    missing = [k for k in _PER_CSV_MAP if k not in rdr.fieldnames]
    if missing:
        # 欄位名變了就停下來。少一欄會讓整欄變 None，而頁面照樣顯示，
        # 看起來像「這些公司沒有本益比」。
        raise netio.FetchError(
            f"估值 CSV 缺少欄位 {missing}，實際有 {rdr.fieldnames}")
    date, out = None, []
    for r in rdr:
        sid = (r.get("證券代號") or "").strip()
        if not sid:
            continue
        if date is None:
            date = netio.roc_to_iso((r.get("日期") or "").strip())
        out.append((sid, date,
                    netio.num(r.get("本益比")),
                    netio.num(r.get("股價淨值比")),
                    netio.num(r.get("殖利率(%)")),
                    "twse"))
    if not out:
        raise netio.FetchError("估值 CSV 沒有資料列")
    if not date:
        raise netio.FetchError("估值 CSV 無法解析日期")
    return date, out


def fetch_valuation_openapi():
    """openapi 的 BWIBBU_ALL（**只有最新一天**，不吃 date）。留作備援。

    欄位（實測 1,088 筆）：Date、Code、Name、PEratio、DividendYield、PBratio。
    """
    data = netio.get_json(EP_PER, limiter=_limiter)
    if not data:
        raise netio.FetchError("BWIBBU_ALL 回傳空清單")
    date = netio.roc_to_iso(data[0].get("Date"))
    if not date:
        raise netio.FetchError(f"無法解析日期：{data[0].get('Date')!r}")
    out = []
    for r in data:
        sid = str(r.get("Code") or "").strip()
        if not sid:
            continue
        out.append((sid, date,
                    netio.num(r.get("PEratio")),
                    netio.num(r.get("PBratio")),
                    netio.num(r.get("DividendYield")),
                    "twse"))
    return date, out


def fetch_valuation(day=None):
    """上市的本益比、殖利率、股價淨值比。回傳 (date, rows)。

    day=None  -> 最新一個交易日
    day="YYYY-MM-DD" -> 指定那天（**這就是回補的能力**）

    ★ 和 tpex.fetch_valuation() 回傳同樣的形狀，呼叫端不必分兩套。

    ★ 指定日期時**沒有備援**，失敗就讓它拋出來。
    備援（openapi）只給最新一天 —— 拿它去充當「11-23 的資料」
    會寫進一份日期錯誤的估值，而且看起來完全正常。寧可失敗。
    """
    url = EP_PER_OPENDATA
    if day:
        url += "&date=" + str(day).replace("-", "")
    try:
        date, rows = _parse_per_csv(netio.get_text(url, limiter=_limiter))
    except (netio.FetchError, netio.StopFetching):
        if day:
            raise
        return fetch_valuation_openapi()
    # ★ 指定了日期就一定要驗日期。對方忽略參數時會回最新一天，
    #   而那份資料看起來完全正常 —— 只有日期是錯的。
    if day and date != str(day):
        raise netio.FetchError(
            f"要 {day} 的估值，對方回的是 {date} —— 日期參數可能被忽略了")
    return date, rows

def fetch_dividends():
    """上市的除權息預告表。回傳 dividend 表的 rows。

    端點 TWT48U_ALL（上市股票除權息預告表）是**前瞻**的 —— 列的是
    即將到來的除權息日，所以每天抓就能把事件累積起來。

    ★ 它沒有「除權息參考價」
    櫃買的端點直接給除權息前收盤與參考價，證交所這支只給股利金額。
    所以 before_px / after_px 留空，還原係數要在 adjust 時用
    「我們自己的前一日收盤」加上股利金額去推算。

    ★ 現在抓到的 53 筆幾乎全是 ETF（00401A、006208、00690…），
    而 ETF 不在白名單裡。真正的上市個股會在各自的除權息日前出現在這裡。

    欄位：Date、Code、Name、Exdividend（除權息類型）、
          StockDividendRatio（股票股利，實測目前皆空）、CashDividend。
    """
    data = netio.get_json(EP_DIVIDEND, limiter=_limiter)
    out = []
    for r in data:
        sid = str(r.get("Code") or "").strip()
        d = netio.roc_to_iso(r.get("Date"))
        if not sid or not d:
            continue
        cash = netio.num(r.get("CashDividend"))
        stock = netio.num(r.get("StockDividendRatio"))
        if cash is None and stock is None:
            continue
        out.append((sid, d, cash, stock, None, None))
    return out


def fetch_holidays():
    """休市日曆。只用來交叉驗證與顯示，不參與交易日計算。"""
    data = netio.get_json(EP_HOLIDAY, limiter=_limiter)
    out = []
    for r in data:
        d = netio.roc_to_iso(r.get("Date"))
        if d:
            out.append((d, str(r.get("Name", "")).strip(),
                        str(r.get("Description", "")).strip()))
    return out
