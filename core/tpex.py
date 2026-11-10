# -*- coding: utf-8 -*-
"""證券櫃檯買賣中心（上櫃）官方 OpenAPI。

授權同證交所：政府資料開放授權條款，可再散布但必須標示來源。
公開發布時標示：證券櫃檯買賣中心 [年份] [資料集名稱]

## ★ 櫃買給三大法人，證交所不給

掃過證交所全部 150 個端點，法人相關只有「外資及陸資**持股比率**」與
「持股前 20 名」—— **上市個股的三大法人買賣超，官方不提供**。
但櫃買有 `tpex_3insti_daily_trading`（實測 911 筆，全市場上櫃個股）。
有點諷刺，但這就是現況：上櫃的法人免費且可散布，上市的只能靠 FinMind。

## ★ 這裡的欄位名稱是個災難（2026-11-10 實測）

`tpex_3insti_daily_trading` 的英文欄位名有多餘空白、拼寫不一致，
甚至同時存在 `Dealers-TotalSell` 和 `Dealers -TotalSell`（中間多一個空格）。
實際欄位名長這樣：

    'Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Buy'
    ' Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Sell'
      ^ 開頭還有一個空格
    'ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy'
    'ForeignInvestorsInclude MainlandAreaInvestors-Difference'
    'Dealers-TotalSell'
    'Dealers -TotalSell'      ← 重複，中間多空格

**所以絕對不能硬編碼欄位名**，一律正規化後比對（去掉所有非英數字元）。
而且找不到就要大聲丟錯，不能靜默回 0。

另外 `tpex_mainboard_margin_balance` 的 `ShortConvering` 是拼錯的
（應為 ShortCovering），照抄官方的拼法比較不會對不上。

## ★ 上櫃日行情有 13,674 筆

包含權證、債券 ETF 等等，而上櫃「公司」只有 897 檔。
不過白名單一定要用，差了 15 倍。
"""
from core import netio

BASE = "https://www.tpex.org.tw/openapi/v1"

EP_PRICE = f"{BASE}/tpex_mainboard_daily_close_quotes"
EP_MARGIN = f"{BASE}/tpex_mainboard_margin_balance"
EP_CHIP = f"{BASE}/tpex_3insti_daily_trading"
EP_COMPANY = f"{BASE}/mopsfin_t187ap03_O"
EP_VALUATION = f"{BASE}/tpex_mainboard_peratio_analysis"

SOURCE_CREDIT = "證券櫃檯買賣中心"

_limiter = netio.RateLimiter(1.0)


def _norm(k):
    return "".join(c for c in str(k).lower() if c.isalnum())


def _pick(row, *patterns, required=True, exclude=()):
    """正規化比對取值。見模組說明 —— 櫃買的欄位名不能硬編碼。

    exclude: 正規化後若包含這些子字串就跳過，用來區分
             「外資（不含外資自營商）」和「外資合計」這種只差幾個字的欄位。
    """
    nk = {}
    for k, v in row.items():
        nk.setdefault(_norm(k), v)
    for p in patterns:
        if p in nk and not any(x in p for x in exclude):
            return nk[p]
    for p in patterns:
        for k, v in nk.items():
            if p in k and not any(x in k for x in exclude):
                return v
    if required:
        raise netio.FetchError(
            f"找不到欄位 {patterns!r}（排除 {exclude!r}）；"
            f"實際欄位：{sorted(row.keys())}")
    return None


def _num0(v):
    n = netio.num(v, 0)
    return 0 if n is None else n


# ---------------- 日行情 ----------------

def fetch_price():
    """當日全市場上櫃行情（含權證等，未過濾）。回傳 (date, rows)。

    `Change` 帶正負號（'+0.32'），`netio.num` 會處理。
    """
    data = netio.get_json(EP_PRICE, limiter=_limiter)
    if not data:
        raise netio.FetchError("上櫃日行情回傳空清單")
    date = netio.roc_to_iso(data[0].get("Date"))
    if not date:
        raise netio.FetchError(f"無法解析日期：{data[0].get('Date')!r}")
    out = []
    for r in data:
        sid = str(_pick(r, "securitiescompanycode", "code") or "").strip()
        if not sid:
            continue
        out.append({
            "stock_id": sid,
            "name": str(_pick(r, "companyname", required=False) or "").strip(),
            "open": netio.num(_pick(r, "open", required=False)),
            "high": netio.num(_pick(r, "high", required=False)),
            "low": netio.num(_pick(r, "low", required=False)),
            "close": netio.num(_pick(r, "close", required=False)),
            "volume": _num0(_pick(r, "tradingshares", required=False)),
            "amount": _num0(_pick(r, "transactionamount", required=False)),
            "trades": _num0(_pick(r, "transactionnumber", required=False)),
        })
    return date, out


# ---------------- 融資融券 ----------------

def fetch_margin():
    """當日全市場上櫃融資融券（單位：張）。回傳 (date, rows)。"""
    data = netio.get_json(EP_MARGIN, limiter=_limiter)
    if not data:
        return None, []
    date = netio.roc_to_iso(data[0].get("Date"))
    out = []
    for r in data:
        sid = str(_pick(r, "securitiescompanycode") or "").strip()
        if not sid:
            continue
        out.append((
            sid, date,
            int(_num0(_pick(r, "marginpurchasebalance",
                            exclude=("previousday", "belong", "quota",
                                     "utilizationrate")))),
            int(_num0(_pick(r, "marginpurchasebalancepreviousday"))),
            int(_num0(_pick(r, "shortsalebalance",
                            exclude=("previousday", "belong", "quota",
                                     "utilizationrate")))),
            int(_num0(_pick(r, "shortsalebalancepreviousday"))),
            "tpex",
        ))
    return date, out


# ---------------- 三大法人 ----------------

def fetch_chip():
    """當日全市場上櫃三大法人買賣超（單位：股）。回傳 (date, rows)。

    ★ 外資有兩組欄位，差別很重要：
        含「excluded」的 = 外資不含外資自營商
        不含「excluded」的 = 外資合計（含外資自營商）
      我們取**合計**，和 FinMind 的 `Foreign_Investor` 定義對齊，
      否則上市上櫃的數字會是兩種不同定義，加總起來毫無意義。
    """
    data = netio.get_json(EP_CHIP, limiter=_limiter)
    if not data:
        return None, []
    date = netio.roc_to_iso(data[0].get("Date"))
    out = []
    for r in data:
        sid = str(_pick(r, "securitiescompanycode") or "").strip()
        if not sid:
            continue
        # 外資合計：要 foreigninvestors + difference，但排除 excluded 與 foreigndealers
        fo = _pick(r, "foreigninvestorsincludemainlandareainvestorsdifference",
                   exclude=("excluded",))
        tr = _pick(r, "securitiesinvestmenttrustcompaniesdifference")
        de = _pick(r, "dealersdifference", exclude=("foreign",))
        to = _pick(r, "totaldifference", required=False)
        fo_i, tr_i, de_i = int(_num0(fo)), int(_num0(tr)), int(_num0(de))
        to_i = int(_num0(to)) if to is not None else fo_i + tr_i + de_i
        out.append((sid, date, fo_i, tr_i, de_i, to_i, "tpex"))
    return date, out


# ---------------- 上櫃公司基本資料（白名單來源） ----------------

def fetch_companies():
    """上櫃股票基本資料（實測 897 筆）。白名單的上櫃來源。"""
    data = netio.get_json(EP_COMPANY, limiter=_limiter)
    out = []
    for r in data:
        sid = str(_pick(r, "securitiescompanycode") or "").strip()
        if not sid:
            continue
        out.append({
            "stock_id": sid,
            "name": str(_pick(r, "companyabbreviation", "companyname",
                              required=False) or "").strip(),
            "industry": str(_pick(r, "securitiesindustrycode",
                                  required=False) or "").strip(),
            "listed_date": _iso8(_pick(r, "dateoflisting", required=False)),
        })
    return out


def _iso8(s):
    t = str(s or "").strip()
    if len(t) == 8 and t.isdigit():
        return f"{t[:4]}-{t[4:6]}-{t[6:]}"
    return netio.roc_to_iso(t)


# ---------------- 本益比／殖利率／股價淨值比 ----------------

def fetch_valuation():
    """當日全市場上櫃的本益比、殖利率、股價淨值比。回傳 (date, rows)。

    ★ 這是**官方、免費、可散布**的來源（政府資料開放授權條款），
    所以之後的網頁版可以用它。

    對照組：證交所的 `BWIBBU_ALL`（上市版）被安全防護擋掉，回傳 HTML
    「因為安全性考量，您所執行的頁面無法呈現」。我們不嘗試偽裝繞過，
    所以**上市的本益比只能靠 FinMind**（逐檔、不可散布）。
    這個不對稱要記在心裡：將來網頁版只會有上櫃的本益比。

    欄位（實測 894 筆）：
      PriceEarningRatio、DividendPerShare、YieldRatio、PriceBookRatio
    """
    data = netio.get_json(EP_VALUATION, limiter=_limiter)
    if not data:
        return None, []
    date = netio.roc_to_iso(data[0].get("Date"))
    out = []
    for r in data:
        sid = str(_pick(r, "securitiescompanycode") or "").strip()
        if not sid:
            continue
        out.append((
            sid, date,
            netio.num(_pick(r, "priceearningratio", required=False)),
            netio.num(_pick(r, "pricebookratio", required=False)),
            netio.num(_pick(r, "yieldratio", required=False)),
            "tpex"))
    return date, out
