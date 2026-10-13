# -*- coding: utf-8 -*-
"""
correlation.py —— 参数相关性分析

- 全量相关矩阵（Pearson / Spearman）+ 显著性 p 值
- 针对响应变量 Y 的关联强度排序（数值列用 r / p，分类列用 ANOVA eta²）
- 滞后相关（X(t-lag) ~ Y(t)）：识别"上游参数先动、下游指标后动"的因果时序
- 多重共线性 VIF 检查：避免把互为因果的参数同时写进优化建议
- 强相关对聚类：把同源参数归并，减少根因候选数量
"""

import math
from . import table as T
from . import stats as S


def corr_matrix(table, columns=None, method="pearson"):
    """返回 {"columns":[...], "matrix":[[r]], "pvalues":[[p]]}"""
    cols = columns or table.numeric_columns()
    cols = [c for c in cols if len([v for v in table.numeric_column(c) if v is not None]) >= 3]
    n = len(cols)
    matrix = [[None] * n for _ in range(n)]
    pvals = [[None] * n for _ in range(n)]
    data = {c: table.numeric_column(c) for c in cols}
    for i in range(n):
        matrix[i][i] = 1.0
        pvals[i][i] = 0.0
        for j in range(i + 1, n):
            if method == "spearman":
                r, p, _ = S.spearman(data[cols[i]], data[cols[j]])
            else:
                r, p, _ = S.pearson(data[cols[i]], data[cols[j]])
            matrix[i][j] = matrix[j][i] = r
            pvals[i][j] = pvals[j][i] = p
    return {"columns": cols, "matrix": matrix, "pvalues": pvals, "method": method}


def association_with_target(table, target_col, exclude=None, method="pearson",
                            min_n=8, alpha=0.05):
    """
    响应变量 Y 与各参数列的关联强度。数值列 -> (r, p)；分类列 -> ANOVA eta²。
    返回按 |effect| 降序的 list。
    """
    exclude = set(exclude or [])
    y_raw = table.numeric_column(target_col)
    y = [v for v in y_raw if v is not None]
    if len(y) < min_n:
        return {"error": "目标列有效样本不足（需要 >=%d，实际 %d）" % (min_n, len(y))}
    out = []
    for c in table.columns:
        if c == target_col or c in exclude:
            continue
        vals = table.numeric_column(c)
        pairs = [(v, yy) for v, yy in zip(vals, y) if v is not None and yy is not None]
        if len(pairs) < min_n:
            continue
        if T.looks_numeric([p[0] for p in pairs]):
            if method == "spearman":
                r, p, n = S.spearman([p[0] for p in pairs], [p[1] for p in pairs])
            else:
                r, p, n = S.pearson([p[0] for p in pairs], [p[1] for p in pairs])
            if r is None:
                continue
            strength = abs(r)
            out.append({
                "param": c, "type": "numeric", "n": n, "r": r, "p": p,
                "abs_effect": strength,
                "significant": (p is not None and p < alpha),
                "direction": "正相关" if r > 0 else "负相关",
                "r2": r * r,
            })
        else:
            # 分类变量 -> eta²
            buckets = {}
            for v, yy in pairs:
                buckets.setdefault(str(v), []).append(yy)
            buckets = {k: v for k, v in buckets.items() if len(v) >= 2}
            if len(buckets) < 2:
                continue
            an = S.anova_oneway(list(buckets.values()))
            if not an:
                continue
            eta2 = an.get("eta2") or 0
            out.append({
                "param": c, "type": "category", "n": len(pairs),
                "anova_F": an.get("F"), "anova_p": an.get("p"), "eta2": eta2,
                "abs_effect": math.sqrt(max(0.0, eta2)),
                "significant": (an.get("p") is not None and an.get("p") < alpha),
                "levels": len(buckets),
                "worst_level": _worst_level(buckets),
            })
    # v1.11：一次分析要检验十几到几十个关联，逐个用 α=0.05 判显著必然挑出假阳性。
    # 统一做 BH 校正，并把"显著"改成校正后显著；报告里同时保留原始 p 与 q。
    if out:
        qs = S.bh_adjust([d.get("p") if d.get("p") is not None else d.get("anova_p")
                          for d in out])
        for d, q in zip(out, qs):
            d["q"] = q
            d["p_raw_uncorrected"] = d.get("p") if d.get("p") is not None else d.get("anova_p")
            d["significant"] = bool(q is not None and q <= alpha)
    out.sort(key=lambda d: -d["abs_effect"])
    return out


def _worst_level(buckets):
    """分类变量中均值最不利的水平（假设 Y 越大越差，后续由调用方解释）"""
    best_k, best_v = None, None
    for k, v in buckets.items():
        m = S.mean(v)
        if m is None:
            continue
        if best_v is None or m > best_v:
            best_k, best_v = k, m
    return {"level": best_k, "mean": best_v} if best_k else None


def lagged_correlation(table, param_col, target_col, max_lag=5, min_n=10):
    """滞后相关：param(t-lag) vs target(t)。返回按 |r| 最优的 lag。"""
    x = table.numeric_column(param_col)
    y = table.numeric_column(target_col)
    res = []
    for lag in range(0, max_lag + 1):
        xs, ys = [], []
        for i in range(lag, len(x)):
            if x[i - lag] is not None and y[i] is not None:
                xs.append(x[i - lag])
                ys.append(y[i])
        if len(xs) < min_n:
            continue
        r, p, n = S.pearson(xs, ys)
        if r is None:
            continue
        res.append({"lag": lag, "r": r, "p": p, "n": n})
    if not res:
        return None
    best = max(res, key=lambda d: abs(d["r"]))
    return {"param": param_col, "target": target_col, "best_lag": best["lag"],
            "best_r": best["r"], "best_p": best["p"], "curve": res,
            "interpretation": _lag_meaning(best["lag"], best["r"])}


def _lag_meaning(lag, r):
    if lag == 0:
        return "同相位：参数与指标同步变化，可能是直接因果或共同受第三方因素驱动"
    d = "参数变化领先指标 %d 个采样点" % lag
    if abs(r) > 0.5:
        return d + "，且相关性强，符合'上游参数→下游指标'的传导特征"
    return d + "，但相关性偏弱，需结合工艺机理判断"


def vif(table, columns=None, threshold=5.0):
    """方差膨胀因子：VIF = 1/(1-R²_i)。>5 表示共线性严重。"""
    cols = columns or table.numeric_columns()
    cols = [c for c in cols if len([v for v in table.numeric_column(c) if v is not None]) >= 8]
    out = []
    # 索引与列向量必须在循环外取：原写法在每行里调 table.col_index()，
    # 是 O(k²·n) 的列查找。15 列 2 万行时，仅仅找下标就跑 450 万次 list.index。
    idx = {c: table.col_index(c) for c in cols}
    if any(i is None for i in idx.values()):
        return out
    vecs = {c: table.numeric_column(c) for c in cols}
    n = len(table.rows)
    for c in cols:
        others = [x for x in cols if x != c]
        if not others:
            continue
        oy = vecs[c]
        ox = [vecs[o] for o in others]
        ky = len(others)
        X, Y = [], []
        for i in range(n):
            yi = oy[i]
            if yi is None:
                continue
            xr = [ox[j][i] for j in range(ky)]
            if any(v is None for v in xr):
                continue
            Y.append(yi)
            X.append(xr)
        if len(X) < 10:
            continue
        fit = S.linear_regression(X, Y)
        if not fit or fit["r2"] is None:
            continue
        r2 = min(0.9999, max(0.0, fit["r2"]))
        v = 1.0 / (1.0 - r2)
        out.append({"param": c, "vif": v, "r2_with_others": r2,
                    "collinear": v > threshold})
    out.sort(key=lambda d: -d["vif"])
    return out


def strong_pairs(corr, threshold=0.7, p_alpha=0.05):
    """提取强相关对（去重复）"""
    cols, m, pv = corr["columns"], corr["matrix"], corr["pvalues"]
    out = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            r = m[i][j]
            if r is None or abs(r) < threshold:
                continue
            p = pv[i][j]
            if p is not None and p > p_alpha:
                continue
            out.append({"a": cols[i], "b": cols[j], "r": r, "p": p})
    out.sort(key=lambda d: -abs(d["r"]))
    return out
