# -*- coding: utf-8 -*-
"""
stats.py —— 零依赖统计基础库

实现 SPC / 相关性 / DOE / 根因分析所需的全部统计量，不依赖 scipy。
包含：描述统计、正态性(Jarque-Bera)、t 检验(含 Welch)、单因素 ANOVA、
卡方检验、Pearson/Spearman 相关、多元最小二乘回归、岭回归兜底、
直方图分箱、游程检验。
分布尾概率用 Numerical Recipes 的 betai / gammap 连分数算法实现。
"""

import math

# ------------------------------------------------------------------ 基础数学


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def variance(xs, ddof=1):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n <= ddof:
        return None
    m = sum(xs) / n
    return sum((x - m) ** 2 for x in xs) / (n - ddof)


def stdev(xs, ddof=1):
    v = variance(xs, ddof)
    return math.sqrt(v) if v is not None and v >= 0 else None


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if n == 0:
        return None
    if n % 2:
        return xs[n // 2]
    return (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def quantile(xs, q):
    """q in [0,1]，线性插值"""
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if n == 0:
        return None
    if n == 1:
        return xs[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def mad(xs):
    """中位数绝对偏差（稳健 sigma 估计 = 1.4826*MAD）"""
    m = median(xs)
    if m is None:
        return None
    devs = [abs(x - m) for x in xs if x is not None]
    return median(devs)


def robust_sigma(xs):
    m = mad(xs)
    return 1.4826 * m if m is not None else None


def skewness(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    s = math.sqrt(sum((x - m) ** 2 for x in xs) / n)
    if s == 0:
        return 0.0
    return sum((x - m) ** 3 for x in xs) / n / (s ** 3)


def kurtosis(xs, excess=True):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 4:
        return None
    m = sum(xs) / n
    s2 = sum((x - m) ** 2 for x in xs) / n
    if s2 == 0:
        return 0.0
    k = sum((x - m) ** 4 for x in xs) / n / (s2 ** 2)
    return k - 3.0 if excess else k


def covariance(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 2:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    return sum((p[0] - mx) * (p[1] - my) for p in pairs) / (n - 1)


# ------------------------------------------------------------------ 分布函数


def _log_gamma(x):
    """Lanczos 近似"""
    g = [
        676.5203681218851, -1259.1392167224028, 771.32342877765313,
        -176.61502916214059, 12.507343278686905, -0.13857109526572012,
        9.9843695780195716e-6, 1.5056327351493116e-7,
    ]
    if x < 0.5:
        return math.log(math.pi / math.sin(math.pi * x)) - _log_gamma(1 - x)
    x -= 1
    a = 0.99999999999980993
    t = x + 7.5
    for i, gi in enumerate(g):
        a += gi / (x + i + 1)
    return 0.5 * math.log(2 * math.pi) + (x + 0.5) * math.log(t) - t + math.log(a)


def _betacf(a, b, x):
    """连分数，用于不完全 beta"""
    MAXIT, EPS, FPMIN = 200, 3.0e-12, 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < EPS:
            break
    return h


def betai(a, b, x):
    """不完全 beta 函数 I_x(a,b)"""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(_log_gamma(a + b) - _log_gamma(a) - _log_gamma(b)
                  + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1 - x) / b


def norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def t_sf(t, df):
    """t 分布双尾 p 值"""
    if df is None or df <= 0:
        return None
    t = abs(float(t))
    if t == 0:
        return 1.0
    x = df / (df + t * t)
    return betai(0.5 * df, 0.5, x)


def _gser(a, x):
    """下不完全 gamma 级数"""
    ap, s, delt = a, 1.0 / a, 1.0 / a
    for _ in range(1000):
        ap += 1
        delt *= x / ap
        s += delt
        if abs(delt) < abs(s) * 3.0e-12:
            break
    return s * math.exp(-x + a * math.log(x) - _log_gamma(a))


def _gcf(a, x):
    """上不完全 gamma 连分数"""
    FPMIN, EPS = 1.0e-300, 3.0e-12
    b, c, d = x + 1.0 - a, 1.0 / FPMIN, 1.0 / (x + 1.0 - a)
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < FPMIN:
            d = FPMIN
        c = b + an / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < EPS:
            break
    return math.exp(-x + a * math.log(x) - _log_gamma(a)) * h


def gamma_p(a, x):
    if x < 0 or a <= 0:
        return None
    if x == 0:
        return 0.0
    if x < a + 1:
        return _gser(a, x)
    return 1.0 - _gcf(a, x)


def chi2_sf(x, df):
    """卡方分布上尾概率"""
    if df <= 0 or x < 0:
        return None
    return 1.0 - gamma_p(df / 2.0, x / 2.0)


def f_sf(f, df1, df2):
    """F 分布上尾概率"""
    if f <= 0 or df1 <= 0 or df2 <= 0:
        return None
    x = df2 / (df2 + df1 * f)
    return betai(df2 / 2.0, df1 / 2.0, x)


# ------------------------------------------------------------------ 检验


def pearson(xs, ys):
    """返回 (r, p, n)"""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None, None, n
    xs2 = [p[0] for p in pairs]
    ys2 = [p[1] for p in pairs]
    sx = stdev(xs2)
    sy = stdev(ys2)
    if not sx or not sy:
        return None, None, n
    r = covariance(xs2, ys2) / (sx * sy)
    r = max(-1.0, min(1.0, r))
    if abs(r) >= 1.0:
        return r, 0.0, n
    df = n - 2
    t = r * math.sqrt(df / (1 - r * r))
    return r, t_sf(t, df), n


def _rank(values):
    """平均秩"""
    idx = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and values[idx[j + 1]] == values[idx[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[idx[k]] = avg
        i = j + 1
    return ranks


def spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None, None, len(pairs)
    xs2 = [p[0] for p in pairs]
    ys2 = [p[1] for p in pairs]
    return pearson(_rank(xs2), _rank(ys2))


def jarque_bera(xs):
    """正态性检验，返回 (JB统计量, p值)；p<0.05 拒绝正态"""
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 8:
        return None, None
    s = skewness(xs)
    k = kurtosis(xs, excess=True)
    if s is None or k is None:
        return None, None
    jb = n / 6.0 * (s * s + k * k / 4.0)
    return jb, chi2_sf(jb, 2)


def t_test_1sample(xs, mu0):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 2:
        return None
    m, s = mean(xs), stdev(xs)
    if not s:
        return None
    t = (m - mu0) / (s / math.sqrt(n))
    return {"t": t, "df": n - 1, "p": t_sf(t, n - 1), "mean": m, "n": n}


def t_test_2sample(xs, ys, equal_var=False):
    """Welch 默认；返回 dict"""
    xs = [x for x in xs if x is not None]
    ys = [y for y in ys if y is not None]
    n1, n2 = len(xs), len(ys)
    if n1 < 2 or n2 < 2:
        return None
    m1, m2 = mean(xs), mean(ys)
    v1, v2 = variance(xs), variance(ys)
    if v1 is None or v2 is None:
        return None
    if equal_var:
        df = n1 + n2 - 2
        sp = ((n1 - 1) * v1 + (n2 - 1) * v2) / df
        if sp <= 0:
            return None
        t = (m1 - m2) / math.sqrt(sp * (1.0 / n1 + 1.0 / n2))
    else:
        se = math.sqrt(v1 / n1 + v2 / n2)
        if se == 0:
            return None
        t = (m1 - m2) / se
        df = (v1 / n1 + v2 / n2) ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    # Cohen's d
    sp2 = math.sqrt(((n1 - 1) * v1 + (n2 - 1) * v2) / max(1, n1 + n2 - 2)) if n1 + n2 > 2 else None
    d = (m1 - m2) / sp2 if sp2 else None
    return {"t": t, "df": df, "p": t_sf(t, df), "mean1": m1, "mean2": m2,
            "n1": n1, "n2": n2, "cohens_d": d}


def anova_oneway(groups):
    """groups: [[..],[..]]  返回 F / p / eta2"""
    groups = [[x for x in g if x is not None] for g in groups]
    groups = [g for g in groups if len(g) >= 2]
    k = len(groups)
    n = sum(len(g) for g in groups)
    if k < 2 or n - k < 1:
        return None
    grand = mean([x for g in groups for x in g])
    ssb = sum(len(g) * (mean(g) - grand) ** 2 for g in groups)
    ssw = sum(sum((x - mean(g)) ** 2 for x in g) for g in groups)
    dfb, dfw = k - 1, n - k
    if dfw <= 0 or ssw <= 0:
        return None
    msb, msw = ssb / dfb, ssw / dfw
    f = msb / msw if msw > 0 else None
    return {"F": f, "df1": dfb, "df2": dfw, "p": f_sf(f, dfb, dfw),
            "eta2": ssb / (ssb + ssw) if (ssb + ssw) > 0 else None}


def chi2_test(table2d):
    """列联表 [[a,b],[c,d]...] 独立性检验"""
    rows = len(table2d)
    cols = len(table2d[0]) if rows else 0
    total = sum(sum(r) for r in table2d)
    if total == 0 or rows < 2 or cols < 2:
        return None
    chi2 = 0.0
    rs = [sum(r) for r in table2d]
    cs = [sum(table2d[i][j] for i in range(rows)) for j in range(cols)]
    for i in range(rows):
        for j in range(cols):
            e = rs[i] * cs[j] / total
            if e > 0:
                chi2 += (table2d[i][j] - e) ** 2 / e
    df = (rows - 1) * (cols - 1)
    return {"chi2": chi2, "df": df, "p": chi2_sf(chi2, df)}


# ------------------------------------------------------------------ 回归


def _solve(matrix, rhs):
    """高斯-约当消元求解 Ax=b，奇异返回 None"""
    n = len(matrix)
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            return None
        a[col], a[piv] = a[piv], a[col]
        pv = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= pv
        for r in range(n):
            if r != col and a[r][col] != 0:
                f = a[r][col]
                for j in range(col, n + 1):
                    a[r][j] -= f * a[col][j]
    return [a[i][n] for i in range(n)]


def linear_regression(X, y, ridge=1e-10):
    """多元最小二乘。X: [[x1,x2..]]（不含截距，自动加）。
    返回 {coef, intercept, r2, adj_r2, tvals, pvals, resid, fitted, std_err}"""
    rows = [(list(xr), yv) for xr, yv in zip(X, y)
            if yv is not None and all(v is not None for v in xr)]
    n = len(rows)
    p = len(X[0]) if X else 0
    if n < p + 2 or p < 1:
        return None
    Y = [r[1] for r in rows]
    Xm = [[1.0] + [float(v) for v in r[0]] for r in rows]
    k = p + 1
    xtx = [[sum(Xm[i][a] * Xm[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    # 轻微岭正则化：不改变结果量级，仅避免共线导致的矩阵奇异
    scale = sum(xtx[a][a] for a in range(1, k)) / max(1, p) or 1.0
    for a in range(1, k):
        xtx[a][a] += ridge * scale
    xty = [sum(Xm[i][a] * Y[i] for i in range(n)) for a in range(k)]
    beta = _solve(xtx, xty)
    if beta is None:
        return None
    fitted = [sum(beta[a] * Xm[i][a] for a in range(k)) for i in range(n)]
    resid = [Y[i] - fitted[i] for i in range(n)]
    ybar = mean(Y)
    sse = sum(r * r for r in resid)
    sst = sum((v - ybar) ** 2 for v in Y)
    r2 = 1 - sse / sst if sst > 0 else None
    adj = 1 - (1 - r2) * (n - 1) / (n - k) if r2 is not None and n > k else None
    df = n - k
    mse = sse / df if df > 0 else None
    std_err, tvals, pvals = [], [], []
    if mse and mse > 0:
        try:
            inv = _invert(xtx)
            for a in range(k):
                se = math.sqrt(max(0.0, mse * inv[a][a]))
                std_err.append(se)
                t = beta[a] / se if se > 0 else 0.0
                tvals.append(t)
                pvals.append(t_sf(t, df))
        except Exception:
            pass
    return {"coef": beta[1:], "intercept": beta[0], "r2": r2, "adj_r2": adj,
            "tvals": tvals[1:], "pvals": pvals[1:], "intercept_t": tvals[0] if tvals else None,
            "intercept_p": pvals[0] if pvals else None,
            "resid": resid, "fitted": fitted, "n": n, "sse": sse, "mse": mse,
            "std_err": std_err[1:] if std_err else []}


def _invert(m):
    n = len(m)
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            raise ValueError("singular")
        a[col], a[piv] = a[piv], a[col]
        pv = a[col][col]
        for j in range(2 * n):
            a[col][j] /= pv
        for r in range(n):
            if r != col and a[r][col] != 0:
                f = a[r][col]
                for j in range(2 * n):
                    a[r][j] -= f * a[col][j]
    return [row[n:] for row in a]


# ------------------------------------------------------------------ 其他


def histogram(xs, bins=20):
    xs = [x for x in xs if x is not None]
    if not xs:
        return [], []
    lo, hi = min(xs), max(xs)
    if hi == lo:
        return [lo], [len(xs)]
    w = (hi - lo) / bins
    counts = [0] * bins
    for x in xs:
        i = int((x - lo) / w)
        if i >= bins:
            i = bins - 1
        counts[i] += 1
    edges = [lo + w * (i + 0.5) for i in range(bins)]
    return edges, counts


def moving_average(xs, window=5):
    out = []
    for i in range(len(xs)):
        seg = [x for x in xs[max(0, i - window + 1):i + 1] if x is not None]
        out.append(sum(seg) / len(seg) if seg else None)
    return out


def runs_count(xs, center=None):
    """以 center 为界统计符号游程数（趋势/振荡判异用）"""
    if center is None:
        center = mean(xs)
    if center is None:
        return 0
    signs = [1 if (x is not None and x > center) else (-1 if x is not None else 0) for x in xs]
    signs = [s for s in signs if s != 0]
    if not signs:
        return 0
    runs = 1
    for i in range(1, len(signs)):
        if signs[i] != signs[i - 1]:
            runs += 1
    return runs


def zscore(xs):
    m, s = mean(xs), stdev(xs)
    if m is None or not s:
        return [None] * len(xs)
    return [None if x is None else (x - m) / s for x in xs]


def minmax_scale(xs):
    vals = [x for x in xs if x is not None]
    if not vals:
        return [None] * len(xs)
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return [0.0 if x is not None else None for x in xs]
    return [None if x is None else (x - lo) / (hi - lo) for x in xs]


def is_sig(p, alpha=0.05):
    """p 值是否达到显著性水平 —— 全库唯一的判定入口（v1.15）。

    为什么必须统一：此前多处写成 `(p or 1) < 0.05`。
    `round(p, 6)` 会把 1e-30 压成 0.0，而 `0.0 or 1` 得到 1 —— **最强的证据被判为不显著**。
    这类错误不会报错，只会让报告少一句结论、多一句"未见显著"，
    属于本库最忌讳的静默错结论。0.0 必须读作"小于任何可显示精度"，不是"缺失"。
    """
    return p is not None and p <= alpha


def fmt_p(p, digits=4):
    """p 值显示：缺失写 —，极小值不失真（不要用 `p or 1` 兜底）"""
    if p is None:
        return "—"
    if p < 10 ** (-digits):
        return "<1e-%d" % digits
    return "%.*g" % (digits, p)


def bh_adjust(pvals, alpha=0.05):
    """Benjamini-Hochberg 多重比较校正，返回 q 值（与原顺序一致）。

    一次分析往往要检验几十个相关/分组，0.05 水准下必然挑出几个假阳性。
    12 个电池列逐个卡方就是典型场景：不校正几乎必然"发现"一个热点。
    BH 比 Bonferroni 更适合这种"控制错误发现比例而非族系错误率"的工程场景
    ——Bonferroni 太保守，会把真正的位置热点一并砍掉。
    """
    idx = [i for i, p in enumerate(pvals) if p is not None]
    out = list(pvals)
    m = len(idx)
    if m == 0:
        return out
    ordered = sorted(idx, key=lambda i: pvals[i])
    prev = 1.0
    for rank, i in enumerate(reversed(ordered), start=1):
        k = m - rank + 1
        q = min(1.0, prev, pvals[i] * m / float(k))
        out[i] = round(q, 6)
        prev = out[i]
    return out
