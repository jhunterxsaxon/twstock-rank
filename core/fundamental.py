# -*- coding: utf-8 -*-
"""基本面：EPS、月營收（官方開放資料，可散布）。

## ★ 為什麼不用 FinMind

本機版（1151109s）的財報與月營收是從 FinMind 來的 —— 那些資料**禁止再散布**，
所以不能出現在這個公開 repo 裡。

但官方自己就有全套，而且是「政府資料開放授權條款」授權，**可以公開**：

    上市  openapi.twse.com.tw/v1/opendata/t187ap14_L   各年度 EPS 統計
          openapi.twse.com.tw/v1/opendata/t187ap05_L   每月營業收入彙總表
    上櫃  www.tpex.org.tw/openapi/v1/mopsfin_t187ap14_O
          www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O

而且**一次請求就是全市場**（上市 1,020 筆、上櫃 848 筆），
不像 FinMind 免費版要逐檔抓 1,985 次。

## ★★ 兩市的欄位名稱不一樣 ★★

這是會默默算錯的那種陷阱 —— 欄位抓不到時 `.get()` 回 None，
不會報錯，只會讓整欄變空白，看起來像「這些公司沒有公布」。

    項目      上市                  上櫃
    EPS       基本每股盈餘(元)      基本每股盈餘      ← 少了「(元)」
    年度      年度                  Year              ← 換成英文
    代號      公司代號              SecuritiesCompanyCode

月營收兩邊倒是完全一致。

所以這裡一律用 `pick()` 做**多名稱查找**，不寫死欄位名。
本機版在 ROE 上踩過同型的坑（`EquityAttributableToOwnersOfParent`
在損益表和資產負債表裡都有、意義不同，算出 316% 的 ROE），
教訓是：**欄位對應要能容錯，而且對不到要看得出來**。

## 單位

官方的金額單位是**千元**。EPS 是元。這裡原封不動存，
顯示時才換算 —— 在儲存層做單位換算，日後很難查出數字是哪裡被乘過。

## 時點

- EPS 的期別時點用季末日（Q1=03-31、Q2=06-30、Q3=09-30、Q4=12-31）
  ★ 數值是**年初至今累計**，不是單季 —— 見 EPS_ITEMS 上方的驗證
- 月營收的 `ym` 用**營收所屬月份**，不是公布月份
  （資料年月 11510 = 民國 115 年 10 月 = 2026-10）
"""
from core import netio

TWSE = "https://openapi.twse.com.tw/v1"
TPEX = "https://www.tpex.org.tw/openapi/v1"

EP_EPS = {"twse": f"{TWSE}/opendata/t187ap14_L",
          "tpex": f"{TPEX}/mopsfin_t187ap14_O"}
EP_REVENUE = {"twse": f"{TWSE}/opendata/t187ap05_L",
              "tpex": f"{TPEX}/mopsfin_t187ap05_O"}

_limiter = netio.RateLimiter(1.0)

QUARTER_END = {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}

# ★★ 這些數字是「年初至今累計」，不是單季 ★★
#
# 端點叫「各年度EPS統計資訊」，欄位叫「基本每股盈餘」，沒有任何地方
# 寫著累計 —— 很容易被當成單季。實際交叉驗證（2026-11-16）：
#
#   t187ap14_L 給台積電的營業收入          3,898,726,974 千元
#   月營收的「累計(1~10月)」減掉「10月」    3,898,726,974 千元  ← 即 1~9 月
#   差額                                    0
#
# 完全相等，所以確定是年初至今累計。台積電那筆 EPS 78.75 是
# **2026 前三季累計**，不是單季賺 78 元。
#
# 所以 item 名稱一律帶 _ytd 後綴，讓它不可能被誤讀。
#
# ⚠ 推論：
#   - 「近四季 EPS」不能把四筆相加，要用相鄰期別**相減**還原單季
#     （Q3單季 = Q3累計 − Q2累計；Q1累計本身就是單季）
#   - 本機版（1151109s）的 FinMind 財報是**單季**，語意相反。
#     兩邊的數字不要直接比較。
EPS_ITEMS = (
    ("eps_ytd", ("基本每股盈餘(元)", "基本每股盈餘")),
    ("revenue_ytd", ("營業收入",)),
    ("op_income_ytd", ("營業利益",)),
    ("non_op_ytd", ("營業外收入及支出",)),
    ("net_income_ytd", ("稅後淨利",)),
)

REVENUE_ITEMS = (
    ("revenue", ("營業收入-當月營收",)),
    ("last_month", ("營業收入-上月營收",)),
    ("last_year", ("營業收入-去年當月營收",)),
    ("mom_pct", ("營業收入-上月比較增減(%)",)),
    ("yoy_pct", ("營業收入-去年同月增減(%)",)),
    ("acc", ("累計營業收入-當月累計營收",)),
    ("acc_last_year", ("累計營業收入-去年累計營收",)),
    ("acc_yoy_pct", ("累計營業收入-前期比較增減(%)",)),
)


def _norm(k):
    """欄位名正規化：去掉空白與標點，容忍 '(元)' 這類差異之外的雜訊。"""
    return "".join(c for c in str(k) if c.isalnum()).lower()


def pick(row, *names):
    """多名稱查找。★ 任何一個名字對上就回傳，全部對不上回 None。

    不寫死欄位名，是因為兩市的命名不一致而且會改版。
    """
    norm = {_norm(k): v for k, v in row.items()}
    for n in names:
        v = norm.get(_norm(n))
        if v not in (None, ""):
            return v
    return None


def _code(row):
    return str(pick(row, "公司代號", "SecuritiesCompanyCode") or "").strip()


def quarter_end(year_roc, quarter):
    """民國年＋季別 → 季末日（西元 ISO）。"""
    try:
        y = int(str(year_roc).strip()) + 1911
    except (TypeError, ValueError):
        return None
    tail = QUARTER_END.get(str(quarter).strip())
    return f"{y}-{tail}" if tail else None


def roc_ym(s):
    """民國年月（11510）→ 'YYYY-MM'。"""
    t = str(s or "").strip()
    if len(t) < 5 or not t.isdigit():
        return None
    try:
        return f"{int(t[:-2]) + 1911}-{int(t[-2:]):02d}"
    except ValueError:
        return None


def fetch_eps(market):
    """回傳 (期別時點, fundamental 表的 rows, 對不到欄位的統計)。

    rows: (stock_id, date, kind, item, value)
    """
    data = netio.get_json(EP_EPS[market], limiter=_limiter)
    rows, misses, period = [], {}, None
    for r in data:
        sid = _code(r)
        d = quarter_end(pick(r, "年度", "Year"), pick(r, "季別", "Quarter"))
        if not sid or not d:
            continue
        period = period or d
        for item, names in EPS_ITEMS:
            v = netio.num(pick(r, *names))
            if v is None:
                misses[item] = misses.get(item, 0) + 1
                continue
            rows.append((sid, d, "ytd", item, v))
    return period, rows, misses


def fetch_revenue(market):
    """回傳 (資料月份, revenue_month 表的 rows, 對不到欄位的統計)。"""
    data = netio.get_json(EP_REVENUE[market], limiter=_limiter)
    rows, misses, ym_seen = [], {}, None
    for r in data:
        sid = _code(r)
        ym = roc_ym(pick(r, "資料年月", "YearMonth"))
        if not sid or not ym:
            continue
        ym_seen = ym_seen or ym
        vals = {}
        for item, names in REVENUE_ITEMS:
            v = netio.num(pick(r, *names))
            if v is None:
                misses[item] = misses.get(item, 0) + 1
            vals[item] = v
        if vals.get("revenue") is None:
            continue
        rows.append((sid, ym, vals["revenue"], vals["last_month"],
                     vals["last_year"], vals["mom_pct"], vals["yoy_pct"],
                     vals["acc"], vals["acc_last_year"],
                     vals["acc_yoy_pct"]))
    return ym_seen, rows, misses
