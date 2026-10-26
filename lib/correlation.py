# -*- coding: utf-8 -*-
"""
correlation.py —— 参数相关性分析

- 全量相关矩阵（Pearson / Spearman）+ 显著性 p 值
- 针对响应变量 Y 的关联强度排序（数值列用 r / p，分类列用 ANOVA eta²）
- 滞后相关（X(t-lag) ~ Y(t)）：识别"上游参数先动、下游指标后动"的因果时序
- 多重共线性 VIF 检查：避免把互为因果的参数同时写进优化建议
- 强相关对聚类：把同源参数归并，减少根因候选数量
"""

try:
    from . import direction as DIR     # 包模式
except Exception:                      # 脚本模式无包上下文
    import direction as DIR            # noqa
import math
from . import table as T
from . import stats as S
from . import status as ST


# 允许的关联度量方法。之所以要白名单而不是让 unknown 兜底成 pearson：
# 实测 method="no_such" 时结果与 pearson 逐位相同、且无任何提示，
# 使用者以为算了 spearman，拿到的是另一个统计量——这种错不会抛异常，
# 只会在结论里安静地错下去。
_SUPPORTED_METHODS = ("pearson", "spearman")
_UNKNOWN_METHOD_MSG = ("不支持的关联度量方法「%s」，当前仅支持 %s"
                       "（若写错，宁肯报错也不能静默换成另一种算法）")


def _checked_method(method):
    """校验 method 并返回规范写法；不支持的一律 ValueError，绝不静默兜底。"""
    m = (method or "").strip().lower()
    if m not in _SUPPORTED_METHODS:
        raise ValueError(_UNKNOWN_METHOD_MSG % (method,
                                                "、".join(_SUPPORTED_METHODS)))
    return m


def corr_matrix(table, columns=None, method="pearson"):
    """返回 {"columns":[...], "matrix":[[r]], "pvalues":[[p]]}"""
    method = _checked_method(method)
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
            # 分支显式穷举，不留 else 兜底：将来新增第三种算法时，
            # 这里会因 r 未定义当场炸掉，而不是静默按 pearson 算完还标着新方法名。
            if method == "spearman":
                r, p, _ = S.spearman(data[cols[i]], data[cols[j]])
            elif method == "pearson":
                r, p, _ = S.pearson(data[cols[i]], data[cols[j]])
            else:
                raise ValueError(_UNKNOWN_METHOD_MSG % method)
            matrix[i][j] = matrix[j][i] = r
            pvals[i][j] = pvals[j][i] = p
    return {"columns": cols, "matrix": matrix, "pvalues": pvals, "method": method}


def association_with_target(table, target_col, exclude=None, method="pearson",
                            min_n=8, alpha=0.05,
                            target_direction=None, target_value=None,
                            spec_limits=None, kb_params=None):
    """
    响应变量 Y 与各参数列的关联强度。数值列 -> (r, p)；分类列 -> ANOVA eta²。
    返回按 |effect| 降序的 list；无法分析时返回带 status 的 dict。

    target_direction：目标列的**方向**（越大越差 / 越大越好 / 有目标值 …）。
        不传则按 知识库 -> 语义推断 逐级确定；都定不了时，
        worst_level 会带 assumed 标记，消费方措辞必须降级。
    spec_limits：{"lsl":.., "usl":..}，方向为 two_sided_spec 时必需。
    """
    spec_limits = spec_limits or {}
    exclude = set(exclude or [])
    # 参数一起校验完再开跑。实测这三档非法值此前全部静默通过：
    #   method="no_such" -> 按 pearson 算完还标着新方法名
    #   min_n=-5         -> 负数把"样本够不够"这道闸变成永真
    #   alpha=-1 / 1e9   -> 显著性比较失去意义，结论照出
    # 三者共同点：不报错、不改状态、只悄悄把结论弄错，所以必须在入口堵住。
    try:
        method = _checked_method(method)
    except ValueError as exc:
        return {"status": ST.FAILED, "target": target_col, "error": str(exc)}
    if not isinstance(min_n, int) or isinstance(min_n, bool):
        return {"status": ST.FAILED, "target": target_col,
                "error": "min_n 必须为整数（当前 %r）；它是最小样本门槛，"
                         "给非整数会让门槛失去意义" % (min_n,)}
    if min_n < 2:
        return {"status": ST.FAILED, "target": target_col,
                "error": "min_n 必须 >= 2（当前 %r）；少于 2 个样本无法估计相关系数"
                         % (min_n,)}
    try:
        alpha = float(alpha)
    except (TypeError, ValueError):
        return {"status": ST.FAILED, "target": target_col,
                "error": "alpha 必须为数值（当前 %r）" % (alpha,)}
    if not (0.0 < alpha < 1.0):
        return {"status": ST.FAILED, "target": target_col,
                "error": "alpha 必须落在 (0, 1) 开区间（当前 %r），"
                         "越界的显著性水平会让 p 值比较失去意义" % (alpha,)}
    try:
        y_raw = table.numeric_column(target_col) or []
    except Exception as exc:
        # 目标列取不出来必须显性失败：调用方 `isinstance(a, list)` 会把 error
        # 静默丢成空列表，报告上就变成"未发现相关因素"。
        return {"status": ST.FAILED, "target": target_col,
                "error": "目标列「%s」读取失败：%s" % (target_col, exc)}
    n_valid_y = sum(1 for v in y_raw if S.is_num(v))
    if n_valid_y < min_n:
        return {"status": ST.INSUFFICIENT_DATA, "target": target_col,
                "error": "目标列有效样本不足（需要 >=%d，实际 %d）"
                         % (min_n, n_valid_y),
                "n_valid": n_valid_y, "min_n": min_n}
    out = []
    target_meta = None
    # 目标列方向只解析一次：它决定"哪一档最差"，解析错了整份报告反向派人。
    direction, dir_source, dir_ok = DIR.resolve(target_col,
                                                explicit=target_direction,
                                                kb_params=kb_params)
    for c in table.columns:
        if c == target_col or c in exclude:
            continue
        vals = table.numeric_column(c)
        # 必须与 **原始行** 的 y_raw 配对。
        #
        #    原写法是 y = [v for v in y_raw if v is not None] 之后再 zip(vals, y)：
        #    只要目标列 **中间** 存在缺失，第 i 行就会被配到第 j 行，
        #    而样本数 n 看着还完全正常 —— 只有配对内容是错的。
        #    实测后果：Y=2X 的完美线性被算成 r=0.9965；
        #    随机数据里更出现过「本质无关 r=-0.025 → 错位后 r=-0.829」的假强相关，
        #    一个毫不相干的参数会被排进根因候选前列。
        pairs, meta = S.finite_pairs(vals, y_raw)
        if target_meta is None:
            target_meta = meta
        if len(pairs) < min_n:
            continue
        if T.looks_numeric([p[0] for p in pairs]):
            if method == "spearman":
                r, p, n = S.spearman([p[0] for p in pairs], [p[1] for p in pairs])
            elif method == "pearson":
                r, p, n = S.pearson([p[0] for p in pairs], [p[1] for p in pairs])
            else:
                raise ValueError(_UNKNOWN_METHOD_MSG % method)
            if r is None:
                continue
            strength = abs(r)
            out.append({
                "param": c, "type": "numeric", "n": n, "r": r, "p": p,
                "abs_effect": strength,
                "significant": (p is not None and p < alpha),
                "direction": "正相关" if r > 0 else "负相关",
                "r2": r * r,
                # 数据血缘：这一条结论是用哪些行算出来的、为什么少了几行。
                # 没有它，"n=9" 这个数字无法向审核员交代。
                "n_raw": meta.get("n_raw"),
                "n_excluded": meta.get("n_excluded"),
                "excluded_reasons": dict(meta.get("excluded_reasons") or {}),
                "aligned_by": meta.get("aligned_by"),
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
                "worst_level": _worst_level(
                    buckets, direction=direction, target=target_value,
                    lsl=spec_limits.get("lsl"), usl=spec_limits.get("usl")),
                "direction_used": direction or DIR.UNKNOWN,
                "direction_source": dir_source,
                "n_raw": meta.get("n_raw"),
                "n_excluded": meta.get("n_excluded"),
                "excluded_reasons": dict(meta.get("excluded_reasons") or {}),
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


def _worst_level(buckets, direction=None, target=None, lsl=None, usl=None):
    """分类变量中**最不利**的水平。

    原实现写死「均值最大 = 最差」，只在"越大越差"的指标上成立。
    良率/效率/容量这类"越大越好"的指标会被反向指认 ——
    报告中"重点核查 X 机台"就会指向全场表现最好的一台。

    现在按 direction 判定；方向未知时退化为**带假定的建议**
    （返回的 dict 带 assumed 标记），而不是冒充已确认结论。
    """
    means = {}
    for k, v in (buckets or {}).items():
        m = S.mean(v)
        if m is not None:
            means[k] = m
    return DIR.worst_group(means, direction=direction, target=target,
                           lsl=lsl, usl=usl)


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
