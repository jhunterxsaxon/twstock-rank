# -*- coding: utf-8 -*-
"""共用的 HTTP 取得：SSL 修正、限速、錯誤處理。

**為什麼不用 requests 或 FinMind 官方套件：**
需要的功能很窄（GET 一個 JSON），但需要很精確的控制 ——
自我限速、402 立刻停、403 立刻停、4xx 不重試。自己寫兩百行換來零依賴，
而且出事的時候看得懂是哪一行擋下來的。

## SSL 的坑（2026-11-10 實測）

Python 3.13 起 `ssl.create_default_context()` 預設開啟 `VERIFY_X509_STRICT`。
證交所與櫃買的憑證少了 Subject Key Identifier 這個欄位，過不了嚴格檢查，
會噴 `CERTIFICATE_VERIFY_FAILED: Missing Subject Key Identifier`。

**只關掉那一個嚴格旗標，憑證驗證與主機名稱驗證都保留**（見 `_ssl_context()`）。
不要用 `ssl._create_unverified_context()` —— 那會連中間人攻擊都擋不住，
為了一個格式細節放棄全部安全性是不划算的交易。

curl 之所以沒事，是因為它不做這項 RFC 5280 的嚴格檢查。
"""
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "1151109s-stock-analysis/0.1 (personal research)"

_ctx = None


def _ssl_context():
    """見模組說明。只清掉 VERIFY_X509_STRICT，其餘驗證全部保留。"""
    global _ctx
    if _ctx is None:
        c = ssl.create_default_context()
        c.verify_flags &= ~ssl.VERIFY_X509_STRICT
        # 確認沒有不小心把驗證關掉 —— 這兩行是防呆，不是裝飾
        assert c.verify_mode == ssl.CERT_REQUIRED
        assert c.check_hostname is True
        _ctx = c
    return _ctx


class FetchError(Exception):
    """一般性的抓取失敗，可以重試。"""


class StopFetching(Exception):
    """**必須立刻停止整批抓取**，不可重試。

    用在額度用盡（402）或 IP 被封（403）。FinMind 的 IP 封鎖政策寫明
    短時間大量 4xx 會被封 30 分鐘，所以撞到這兩個就要整批停，
    不能讓迴圈繼續把剩下的一千筆打完。
    """


class RateLimiter:
    """最簡單的間隔限速器。

    不做 token bucket，因為我們要的不是「平均速率」而是
    「兩次請求之間至少隔多久」—— 對方在意的是瞬間併發。
    """

    def __init__(self, min_interval=0.0):
        self.min_interval = float(min_interval)
        self._last = 0.0

    def wait(self):
        if self.min_interval <= 0:
            return
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()


def get_json(url, params=None, headers=None, timeout=90,
             limiter=None, retries=2, backoff=2.0):
    """GET 一個 JSON。回傳已解析的物件。

    - 4xx **不重試**（除了 429）：請求本身有問題，重試只會讓對方更想封你
    - 402 / 403 → 丟 StopFetching，呼叫端要整批中止
    - 5xx 與網路錯誤 → 指數退避重試，但有次數上限
    - 回傳的不是 JSON（例如被 WAF 擋掉而回 HTML）→ 丟 FetchError 並附前 300 字
    """
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)

    last = None
    for attempt in range(retries + 1):
        if limiter:
            limiter.wait()
        req = urllib.request.Request(url, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=_ssl_context()) as r:
                raw = r.read()
                ctype = r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read(600).decode("utf-8", errors="replace")
            except Exception:
                pass
            if e.code in (402, 403):
                raise StopFetching(
                    f"HTTP {e.code} —— 額度用盡或 IP 被封，整批中止。{body[:200]}")
            if 400 <= e.code < 500 and e.code != 429:
                raise FetchError(f"HTTP {e.code}（4xx 不重試）{body[:200]}")
            last = FetchError(f"HTTP {e.code} {body[:200]}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = FetchError(f"{type(e).__name__}: {e}")
        else:
            txt = raw.decode("utf-8", errors="replace")
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                # 典型情況：被 WAF 擋掉回了 HTML 錯誤頁。這不是暫時性故障，不重試。
                raise FetchError(
                    f"回傳不是 JSON（Content-Type: {ctype}），"
                    f"前 300 字：{txt[:300]!r}")
        if attempt < retries:
            time.sleep(backoff * (2 ** attempt))
    raise last or FetchError("未知錯誤")


# ---------------- 民國年轉換 ----------------

def roc_to_iso(s):
    """民國日期字串轉西元 ISO。

    官方 API 一律回民國 7 碼（'1151109'）或帶斜線（'115/11/09'）。
    資料庫一律存西元 'YYYY-MM-DD' —— 混用兩種紀年是以後一定會踩到的雷，
    所以在入口就轉掉，資料庫裡不存民國。
    """
    if not s:
        return None
    t = str(s).strip().replace("/", "").replace("-", "")
    if not t.isdigit():
        return None
    if len(t) == 7:          # 1151109
        y, md = int(t[:3]) + 1911, t[3:]
    elif len(t) == 6:        # 991109（民國 99 年以前）
        y, md = int(t[:2]) + 1911, t[2:]
    elif len(t) == 8:        # 已經是西元 20261109
        y, md = int(t[:4]), t[4:]
    else:
        return None
    return f"{y:04d}-{md[:2]}-{md[2:4]}"


def num(s, default=None):
    """把官方 API 的數字字串轉成 float。

    官方資料的數字有一堆花樣，全部要處理：
      '1,234,567'  千分位逗號
      '+0.32'      帶正號
      ''           空字串代表 0 或無資料（例如融券買進沒人買就是空的）
      '--' 'X' '－' 當日無成交或不適用
    """
    if s is None:
        return default
    t = str(s).strip().replace(",", "").replace("+", "")
    if t in ("", "--", "-", "X", "x", "N/A", "－", "---"):
        return default
    try:
        return float(t)
    except ValueError:
        return default
