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
- **`BWIBBU_ALL`（本益比）被安全防護擋掉**，回傳 HTML
  「因為安全性考量，您所執行的頁面無法呈現」。其他端點用同一個 User-Agent 都正常，
  所以不是被封鎖，是這個端點本身的問題。**不嘗試偽裝繞過**，待查。
- **`MI_MARGN` 回傳的資料裡沒有日期欄位**，要用同一輪 `STOCK_DAY_ALL` 的日期。
- 證交所的「上市個股日成交資訊」含 ETF 與特別股（1,389 筆中有 245 筆 00 開頭），
  所以一定要過白名單。
"""
from core import netio

BASE = "https://openapi.twse.com.tw/v1"

EP_PRICE = f"{BASE}/exchangeReport/STOCK_DAY_ALL"
EP_MARGIN = f"{BASE}/exchangeReport/MI_MARGN"
EP_COMPANY = f"{BASE}/opendata/t187ap03_L"
EP_INDEX = f"{BASE}/exchangeReport/MI_INDEX"
EP_HOLIDAY = f"{BASE}/holidaySchedule/holidaySchedule"
EP_PER = f"{BASE}/exchangeReport/BWIBBU_ALL"      # 目前被擋，見模組說明

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

def fetch_price():
    """當日全市場日成交資訊（含 ETF、特別股，未過濾）。

    回傳 (date, rows)，rows 是 dict 清單：
      stock_id, name, open, high, low, close, volume, amount, trades
    """
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
        })
    return date, out


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
