# -*- coding: utf-8 -*-
"""量價技術指標。

## ★★ 這支的每一個函式都必須通過「截斷測試」 ★★

截斷測試：把資料只餵到第 T 天算出指標，再餵完整資料，
**比對第 T 天的值有沒有變。變了就是用到了未來資料。**

未來函數（look-ahead bias）在指標裡特別隱蔽，而且錯了不會報錯，
只會讓回測績效虛高到離譜。以下寫法一律禁止：

    rolling(..., center=True)    窗口跨到未來
    bfill() / backfill()         拿明天的值補今天
    全期間的 mean()/std() 標準化  整段歷史都知道未來
    shift(-n)                    直接看未來

pandas 的 `rolling(n)` 和 `ewm()` 預設都是「只看過去」，所以安全。
驗證腳本在 `_dev/check_lookahead.py`。

## ★ Hurst 指數：分群的「文化」

使用者的洞察是「不同股票適合不同技術分析」，類比行政學的
「制度不能直接複製，因為文化不同」。那個類比成立的關鍵是
**文化可以事先獨立觀察**，不是從「制度成不成功」反推出來的。

Hurst 就是股票的「文化」—— 它只看價格序列的統計性質，
完全不看任何策略賺不賺錢，所以用它分群不是循環論證。

    > 0.55  趨勢延續型 → 適合順勢（均線、突破）
    < 0.45  均值回歸型 → 適合逆勢（KD、布林反轉）
    ≈ 0.5   隨機漫步   → 技術分析幫助有限
"""
import numpy as np
import pandas as pd


# ---------------- 基本 ----------------

def ma(close, n):
    return close.rolling(n, min_periods=n).mean()


def bias(close, n):
    """乖離率 %：股價偏離均線多少。"""
    m = ma(close, n)
    return (close - m) / m * 100.0


def bull_stack(close, fast=5, mid=20, slow=60):
    """多頭排列：MA5 > MA20 > MA60。"""
    a, b, c = ma(close, fast), ma(close, mid), ma(close, slow)
    return (a > b) & (b > c)


def cross_up(a, b):
    """a 由下往上穿越 b（今天在上、昨天在下）。"""
    return (a > b) & (a.shift(1) <= b.shift(1))


def cross_down(a, b):
    return (a < b) & (a.shift(1) >= b.shift(1))


# ---------------- KD ----------------

def kd(high, low, close, n=9, k_smooth=3, d_smooth=3):
    """隨機指標。回傳 (K, D)。

    用台股常見的 9-3-3。RSV 的分母為 0（N 日內最高＝最低，例如連續
    漲停鎖死）時填 50 —— 那代表沒有資訊，給中性值比給 NaN 好用，
    也比給 0 或 100 誠實。
    """
    hh = high.rolling(n, min_periods=n).max()
    ll = low.rolling(n, min_periods=n).min()
    rng = hh - ll
    rsv = (close - ll) / rng.where(rng != 0) * 100.0
    rsv = rsv.fillna(50.0).where(close.notna())
    k = rsv.ewm(alpha=1.0 / k_smooth, adjust=False).mean()
    d = k.ewm(alpha=1.0 / d_smooth, adjust=False).mean()
    return k, d


# ---------------- MACD ----------------

def macd(close, fast=12, slow=26, signal=9):
    """回傳 (DIF, MACD訊號線, 柱狀體)。"""
    ef = close.ewm(span=fast, adjust=False).mean()
    es = close.ewm(span=slow, adjust=False).mean()
    dif = ef - es
    sig = dif.ewm(span=signal, adjust=False).mean()
    return dif, sig, dif - sig


# ---------------- RSI ----------------

def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
    dn = (-d).clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # dn == 0（期間內只漲沒跌）→ RSI 定義上是 100
    return out.where(dn != 0, 100.0).where(close.notna())


# ---------------- 布林通道 ----------------

def bollinger(close, n=20, k=2.0):
    """回傳 (中線, 上軌, 下軌, 位置 0~1, 帶寬 %)。

    位置 = (收盤 − 下軌) ÷ (上軌 − 下軌)，0 在下軌、1 在上軌。
    """
    m = ma(close, n)
    s = close.rolling(n, min_periods=n).std(ddof=0)
    up, lo = m + k * s, m - k * s
    width = (up - lo)
    pos = (close - lo) / width.where(width != 0)
    return m, up, lo, pos, width / m * 100.0


# ---------------- ATR ----------------

def true_range(high, low, close):
    pc = close.shift(1)
    return pd.concat([(high - low),
                      (high - pc).abs(),
                      (low - pc).abs()], axis=1).max(axis=1)


def atr(high, low, close, n=14):
    """平均真實波幅。停損用它的倍數比固定百分比合理 ——
    固定 7% 對高波動股太緊、對低波動股太鬆。"""
    return true_range(high, low, close).ewm(alpha=1.0 / n, adjust=False).mean()


def atr_pct(high, low, close, n=14):
    return atr(high, low, close, n) / close * 100.0


# ---------------- 量能 ----------------

def vol_ratio(volume, n=20):
    """今日量 ÷ N 日均量。

    ★ 分母刻意**不含今天**（shift(1)）—— 否則爆量那天會把自己的量
    算進均量裡，把倍數稀釋掉。想知道「相對於之前的常態放了多少量」，
    基準就不能包含今天。
    """
    base = volume.shift(1).rolling(n, min_periods=n).mean()
    return volume / base.where(base > 0)


# ---------------- 新高新低 / 連續漲跌 ----------------

def is_high_n(close, n=60):
    """創 N 日新高。比較對象是「過去 N 天」，不含今天。"""
    prev_max = close.shift(1).rolling(n, min_periods=n).max()
    return close > prev_max


def is_low_n(close, n=60):
    prev_min = close.shift(1).rolling(n, min_periods=n).min()
    return close < prev_min


def streak(cond):
    """布林序列的連續 True 計數（False 歸零）。"""
    c = cond.fillna(False).astype(int)
    grp = (c == 0).cumsum()
    return c.groupby(grp).cumsum()


def up_streak(close):
    return streak(close.diff() > 0)


def down_streak(close):
    return streak(close.diff() < 0)


# ---------------- Hurst 與自相關（分群用的事前特徵） ----------------

def hurst(close, min_lag=2, max_lag=60):
    """Hurst 指數，用增量標準差的 scaling 估計。

    原理：std(x[t+lag] − x[t]) 與 lag^H 成正比，
    所以在 log-log 上做線性回歸，斜率就是 H。

    用對數價格，這樣結果不受股價絕對水準影響（2,400 元的台積電和
    20 元的雞蛋股可以直接比）。

    回傳 float，資料不足回 nan。
    """
    s = pd.Series(close).dropna()
    # 取對數前要濾掉非正數。停牌、資料缺漏、或來源給 0 都會讓 log 噴
    # RuntimeWarning 並產生 -inf，再回歸出一個看起來正常的假 H 值。
    s = s[s > 0]
    if len(s) < max_lag * 3:
        return float("nan")
    x = np.log(s.to_numpy(dtype=float))
    lags = np.arange(min_lag, max_lag + 1)
    tau = []
    for lag in lags:
        d = x[lag:] - x[:-lag]
        sd = np.std(d)
        tau.append(sd if sd > 0 else np.nan)
    tau = np.asarray(tau, dtype=float)
    ok = np.isfinite(tau) & (tau > 0)
    if ok.sum() < 10:
        return float("nan")
    h = np.polyfit(np.log(lags[ok]), np.log(tau[ok]), 1)[0]
    return float(h)


def autocorr1(close):
    """日報酬的 lag-1 自相關。正 → 動能延續；負 → 反轉傾向。"""
    s = pd.Series(close).dropna()
    s = s[s > 0]
    r = s.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < 60 or r.std(ddof=0) == 0:
        return float("nan")
    return float(r.autocorr(lag=1))


def annual_vol(close, periods=252):
    s = pd.Series(close).dropna()
    s = s[s > 0]
    r = s.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < 60:
        return float("nan")
    return float(r.std(ddof=0) * np.sqrt(periods) * 100.0)


def safe_median(arr):
    """全是 NaN 的時候回 nan，而不是噴 RuntimeWarning。"""
    a = np.asarray(arr, dtype=float)
    a = a[np.isfinite(a)]
    return float(np.median(a)) if a.size else float("nan")


# ---------------- 一次算好一整張表 ----------------

DEFAULTS = dict(ma_list=(5, 10, 20, 60, 120), kd_n=9, rsi_n=14,
                boll_n=20, boll_k=2.0, atr_n=14, vol_n=20,
                high_n=60, low_n=60)


def enrich(df, **kw):
    """把 OHLCV 的 DataFrame 加上全部指標欄位。

    df 需要 open/high/low/close/volume，以 date 為 index 且升冪。
    回傳新的 DataFrame（不改原物件）。
    """
    o = dict(DEFAULTS); o.update(kw)
    if df.empty:
        return df
    d = df.copy()
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]

    for n in o["ma_list"]:
        d[f"ma{n}"] = ma(c, n)
        d[f"bias{n}"] = bias(c, n)
    d["bull_stack"] = bull_stack(c)

    d["k"], d["d"] = kd(h, l, c, o["kd_n"])
    d["dif"], d["macd_sig"], d["macd_hist"] = macd(c)
    d["rsi"] = rsi(c, o["rsi_n"])

    (d["boll_mid"], d["boll_up"], d["boll_lo"],
     d["boll_pos"], d["boll_width"]) = bollinger(c, o["boll_n"], o["boll_k"])

    d["atr"] = atr(h, l, c, o["atr_n"])
    d["atr_pct"] = d["atr"] / c * 100.0

    d["vol_ratio"] = vol_ratio(v, o["vol_n"])
    d["new_high"] = is_high_n(c, o["high_n"])
    d["new_low"] = is_low_n(c, o["low_n"])
    d["up_streak"] = up_streak(c)
    d["down_streak"] = down_streak(c)
    d["ret"] = c.pct_change()
    return d
