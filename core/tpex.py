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
import csv
import io as _io

from core import netio

BASE = "https://www.tpex.org.tw/openapi/v1"

EP_PRICE = f"{BASE}/tpex_mainboard_daily_close_quotes"
EP_MARGIN = f"{BASE}/tpex_mainboard_margin_balance"
EP_CHIP = f"{BASE}/tpex_3insti_daily_trading"
EP_COMPANY = f"{BASE}/mopsfin_t187ap03_O"
EP_VALUATION = f"{BASE}/tpex_mainboard_peratio_analysis"

# ★★ 估值走這條，不是 openapi ★★
#
# 2026-11-27 查 data.gov.tw 資料集 11373 得到：
#     名稱  上櫃股票個股本益比、殖利率、股價淨值比（櫃買中心）
#     授權  1（政府資料開放授權條款第 1 版）
#
# ★ 和上市那條（資料集 11764）一樣**吃日期**，格式是民國帶斜線：
#       &d=115/11/24
#   2026-11-27 實測：
#       不帶日期      -> 1151126，895 檔
#       d=115/11/24   -> 1151124，894 檔
#
# 欄位：資料日期、股票代號、名稱、本益比、每股股利、殖利率、股價淨值比
# ★ 注意欄名是「殖利率」，上市那邊是「殖利率(%)」—— 兩邊不一樣，
#   所以兩個解析函式不能共用一份欄位對應表。
EP_PER_OPENDATA = ("https://www.tpex.org.tw/web/stock/aftertrading/"
                   "peratio_analysis/pera_result.php?l=zh-tw&o=data")
EP_DIVIDEND = f"{BASE}/tpex_exright_daily"

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
            # ★ 官方漲跌價差。做漲跌幅榜一定要用這個，
            #   不能用 (收盤 − 前日收盤) 自己算 —— 除權息當天價格會跳空，
            #   自己算會得到假性大跌，那幾檔會直接佔滿跌幅榜前幾名。
            #   官方這個數字是對**除權息參考價**算的。
            "chg": netio.num(_pick(r, "change", required=False)),
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

def fetch_dividends():
    """上櫃的除權息。回傳 dividend 表的 rows。

    ★ 比證交所那支好用：它直接給「除權息前收盤」與「除權息參考價」，
    還原係數 = 參考價 / 前收盤，不必自己推算，也不會因為
    股票股利的單位解讀錯誤而算歪。

    欄位：Date、SecuritiesCompanyCode、
          ClosePriceBeforeExRightsDiviend（除權息前收盤）、
          ExRightsDiviendQuote（除權息參考價）、
          CashDividend、StockDividend。
    """
    data = netio.get_json(EP_DIVIDEND, limiter=_limiter)
    out = []
    for r in data:
        sid = str(_pick(r, "securitiescompanycode", required=False) or "").strip()
        d = netio.roc_to_iso(r.get("Date"))
        if not sid or not d:
            continue
        out.append((
            sid, d,
            netio.num(_pick(r, "cashdividend", required=False)),
            netio.num(_pick(r, "stockdividend", required=False)),
            netio.num(_pick(r, "closepricebeforeexrightsdiviend",
                            required=False)),
            netio.num(_pick(r, "exrightsdiviendquote", required=False))))
    return out


_PER_CSV_MAP = {"本益比": "per", "股價淨值比": "pbr", "殖利率": "yield_pct"}


def _roc_slash(day):
    """'2026-11-24' -> '115/11/24'（櫃買的日期參數格式）。"""
    y, m, d = str(day).split("-")
    return f"{int(y) - 1911}/{m}/{d}"


def _parse_per_csv(text):
    """解析資料集 11373 的 pera_result CSV。回傳 (date, rows)。

    rows 是 db.upsert_valuation() 要的 tuple：
        (stock_id, date, per, pbr, yield_pct, src)
    """
    rdr = csv.DictReader(_io.StringIO(text))
    if not rdr.fieldnames:
        raise netio.FetchError("上櫃估值 CSV 沒有標題列")
    missing = [k for k in _PER_CSV_MAP if k not in rdr.fieldnames]
    if missing:
        raise netio.FetchError(
            f"上櫃估值 CSV 缺少欄位 {missing}，實際有 {rdr.fieldnames}")
    date, out = None, []
    for r in rdr:
        sid = (r.get("股票代號") or "").strip()
        if not sid:
            continue
        if date is None:
            date = netio.roc_to_iso((r.get("資料日期") or "").strip())
        out.append((sid, date,
                    netio.num(r.get("本益比")),
                    netio.num(r.get("股價淨值比")),
                    netio.num(r.get("殖利率")),
                    "tpex"))
    if not out:
        raise netio.FetchError("上櫃估值 CSV 沒有資料列")
    if not date:
        raise netio.FetchError("上櫃估值 CSV 無法解析日期")
    return date, out


def fetch_valuation_openapi():
    """openapi 的 peratio_analysis（**只有最新一天**）。留作備援。

    欄位（實測 894 筆）：
      PriceEarningRatio、DividendPerShare、YieldRatio、PriceBookRatio
    """
    data = netio.get_json(EP_VALUATION, limiter=_limiter)
    if not data:
        raise netio.FetchError("peratio_analysis 回傳空清單")
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


def fetch_valuation(day=None):
    """上櫃的本益比、殖利率、股價淨值比。回傳 (date, rows)。

    day=None -> 最新一個交易日；day="YYYY-MM-DD" -> 那天（可回補）。

    ★ 2026-11-27 起兩市都能依日期回補，所以專案裡「漏掉就永久消失」
    那個結論對估值已經不成立（對價量的 openapi 仍然成立）。

    ★ 指定日期時沒有備援 —— 備援只給最新一天，拿它充當指定日期的資料
    會寫進一份日期錯誤的估值，而且看起來完全正常。
    """
    url = EP_PER_OPENDATA
    if day:
        url += "&d=" + _roc_slash(day)
    try:
        date, rows = _parse_per_csv(netio.get_text(url, limiter=_limiter))
    except (netio.FetchError, netio.StopFetching):
        if day:
            raise
        return fetch_valuation_openapi()
    if day and date != str(day):
        raise netio.FetchError(
            f"要 {day} 的上櫃估值，對方回的是 {date} —— 日期參數可能被忽略了")
    return date, rows

