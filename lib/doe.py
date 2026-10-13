# -*- coding: utf-8 -*-
"""
doe.py —— 试验设计（DOE）与参数寻优

设计生成：
  - 全因子 full_factorial([2,2,3...])
  - Taguchi 正交表 L8(2^7) / L16(2^15) / L9(3^4) / L27(3^13)（按 Yates / GF(3) 规则程序化生成，不查表）
  - Box-Behnken、中心复合设计 CCD（含 α 旋转性选择）
  - 自动推荐：按因子数与水平数选最省试验次数的方案

分析：
  - 主效应 / 交互效应 / 极差分析
  - 因子 ANOVA（SS、F、p、贡献率）
  - Taguchi 信噪比（望大 / 望小 / 望目）
  - 二次响应面拟合 + 网格/坐标下降寻优，给出预测最优参数组合
"""

import math
import itertools
import re
from . import stats as S


# ------------------------------------------------------------------ 设计生成

def full_factorial(levels):
    """levels: [2,3,2] -> 每因子水平数；返回 [[水平(1-based)], ...]"""
    ranges = [range(1, lv + 1) for lv in levels]
    return [list(c) for c in itertools.product(*ranges)]


def _yates_columns(n_runs):
    """生成 2 水平设计的基本列（0/1），n_runs 为 2 的幂"""
    k = int(round(math.log(n_runs, 2)))
    cols = []
    for j in range(k):
        bit = 1 << (k - 1 - j)
        cols.append([(i & bit) >> (k - 1 - j) for i in range(n_runs)])
    return cols, k


def _xor_cols(a, b):
    return [x ^ y for x, y in zip(a, b)]


def taguchi_2level(n_runs, max_factors=None):
    """生成 L8(2^7) / L16(2^15) / L32(2^31)；返回 1-based 水平矩阵"""
    base, k = _yates_columns(n_runs)
    cols = list(base)
    # 交互列：按标准序生成
    for mask in range(3, (1 << k)):
        if bin(mask).count("1") < 2:
            continue
        acc = [0] * n_runs
        first = True
        for j in range(k):
            if mask & (1 << (k - 1 - j)):
                if first:
                    acc = list(base[j])
                    first = False
                else:
                    acc = _xor_cols(acc, base[j])
        cols.append(acc)
    if max_factors:
        cols = cols[:max_factors]
    return [[v + 1 for v in row] for row in zip(*cols)]


def taguchi_3level(n_runs, max_factors=None):
    """生成 L9(3^4) / L27(3^13)：GF(3) 线性组合构造"""
    k = int(round(math.log(n_runs, 3)))
    base = []
    for j in range(k):
        p = 3 ** (k - 1 - j)
        base.append([(i // p) % 3 for i in range(n_runs)])
    cols = list(base)
    # GF(3) 上所有非平凡线性组合：系数 ∈{0,1,2}，首个非零系数为 1，至少两个非零
    for coeffs in itertools.product([0, 1, 2], repeat=k):
        nz = [i for i, c in enumerate(coeffs) if c]
        if len(nz) < 2 or coeffs[nz[0]] != 1:
            continue
        cols.append([sum(coeffs[j] * base[j][i] for j in range(k)) % 3
                     for i in range(n_runs)])
    if max_factors:
        cols = cols[:max_factors]
    return [[v + 1 for v in row] for row in zip(*cols)]


def box_behnken(k, center=3):
    """Box-Behnken 设计，返回 coded 矩阵（-1/0/1）"""
    if k < 3:
        return None
    runs = []
    for i, j in itertools.combinations(range(k), 2):
        for a in (-1, 1):
            for b in (-1, 1):
                row = [0] * k
                row[i] = a
                row[j] = b
                runs.append(row)
    runs += [[0] * k for _ in range(center)]
    return runs


def ccd(k, alpha=None, center=3, face_centered=False):
    """中心复合设计：2^k 因子点 + 2k 轴向点 + 中心点"""
    if k < 2:
        return None
    if face_centered:
        a = 1.0
    else:
        a = alpha if alpha else (2.0 ** k) ** 0.25  # rotatable
    runs = []
    for signs in itertools.product([-1, 1], repeat=k):
        runs.append([float(s) for s in signs])
    for i in range(k):
        for s in (-a, a):
            row = [0.0] * k
            row[i] = s
            runs.append(row)
    runs += [[0.0] * k for _ in range(center)]
    return runs


def recommend_design(n_factors, levels=2, budget=None):
    """按因子数推荐方案，返回说明 + (设计函数, 试验次数)"""
    opts = []
    if levels == 2:
        ff = 2 ** n_factors
        opts.append({"name": "全因子 2^%d" % n_factors, "runs": ff,
                     "kind": "full_factorial",
                     "note": "可估计全部交互作用，试验次数随因子数指数增长"})
        for n, cap in ((8, 7), (16, 15), (32, 31)):
            if n_factors <= cap:
                opts.append({"name": "L%d 正交表(2^%d)" % (n, cap), "runs": n,
                             "kind": "taguchi_2level", "param": n,
                             "note": "主效应清晰，交互与设计列混杂，适合筛选阶段"})
                break
    else:
        ff = levels ** n_factors
        opts.append({"name": "全因子 %d^%d" % (levels, n_factors), "runs": ff,
                     "kind": "full_factorial", "note": "全交互可估"})
        for n, cap in ((9, 4), (27, 13)):
            if n_factors <= cap:
                opts.append({"name": "L%d 正交表(%d^%d)" % (n, levels, cap), "runs": n,
                             "kind": "taguchi_3level", "param": n,
                             "note": "三水平可考察非线性（曲率）"})
                break
    if n_factors >= 3:
        bb = box_behnken(n_factors)
        if bb:
            opts.append({"name": "Box-Behnken", "runs": len(bb), "kind": "box_behnken",
                         "note": "响应面设计，三水平、无角点，适合拟合二次模型寻优"})
        cc = ccd(n_factors)
        if cc:
            opts.append({"name": "中心复合设计 CCD", "runs": len(cc), "kind": "ccd",
                         "note": "响应面经典设计，含轴向点，可估纯二次项"})
    if budget:
        opts = [o for o in opts if o["runs"] <= budget] or opts
    opts.sort(key=lambda o: o["runs"])
    return opts


def build_design(kind, n_factors, levels=2, param=None):
    """返回 coded/水平矩阵 list[list]"""
    if kind == "full_factorial":
        return full_factorial([levels] * n_factors)
    if kind == "taguchi_2level":
        return taguchi_2level(param or 8, n_factors)
    if kind == "taguchi_3level":
        return taguchi_3level(param or 9, n_factors)
    if kind == "box_behnken":
        return box_behnken(n_factors)
    if kind == "ccd":
        return ccd(n_factors)
    return None


def decode_design(matrix, factor_specs):
    """coded/水平矩阵 -> 实际工艺参数值。
    factor_specs: [{"name":..,"low":..,"high":..,"mid":..(可选),"unit":..}]"""
    out = []
    for row in matrix:
        rec = {}
        for spec, lv in zip(factor_specs, row):
            v = float(lv)
            if v == 1:
                rec[spec["name"]] = spec.get("low")
            elif v == 2:
                rec[spec["name"]] = spec.get("mid", (spec.get("low", 0) + spec.get("high", 0)) / 2.0)
            elif v == 3:
                rec[spec["name"]] = spec.get("high")
            elif v == 0:
                rec[spec["name"]] = spec.get("mid", (spec.get("low", 0) + spec.get("high", 0)) / 2.0)
            elif v < 0:
                mid = spec.get("mid", (spec.get("low", 0) + spec.get("high", 0)) / 2.0)
                half = (spec.get("high", 0) - spec.get("low", 0)) / 2.0
                rec[spec["name"]] = mid + v * half
            else:
                mid = spec.get("mid", (spec.get("low", 0) + spec.get("high", 0)) / 2.0)
                half = (spec.get("high", 0) - spec.get("low", 0)) / 2.0
                rec[spec["name"]] = mid + (v - 1) * half
        out.append(rec)
    return out


# ------------------------------------------------------------------ 效应分析

def effects(design, responses, factor_names, levels=2):
    """主效应 + 极差 + ANOVA 贡献率。design: 1-based 水平矩阵"""
    n_run = len(design)
    if n_run != len(responses):
        return {"error": "试验次数与响应数不一致"}
    k = len(factor_names)
    grand = S.mean([r for r in responses if r is not None])
    if grand is None:
        return {"error": "无有效响应数据"}
    sst = sum((r - grand) ** 2 for r in responses if r is not None)
    main = []
    ss_factor = {}
    for j in range(k):
        buckets = {}
        for i in range(n_run):
            if responses[i] is None:
                continue
            lv = int(round(float(design[i][j])))
            buckets.setdefault(lv, []).append(responses[i])
        lv_keys = sorted(buckets.keys())
        means = {lv: S.mean(v) for lv, v in buckets.items()}
        best = max(means.items(), key=lambda kv: kv[1])
        worst = min(means.items(), key=lambda kv: kv[1])
        rng = best[1] - worst[1]
        an = S.anova_oneway([buckets[lv] for lv in lv_keys]) if len(lv_keys) >= 2 else None
        ss = sum(len(buckets[lv]) * (means[lv] - grand) ** 2 for lv in lv_keys)
        ss_factor[j] = ss
        main.append({
            "factor": factor_names[j], "level_means": means,
            "range": rng, "best_level": best[0], "best_mean": best[1],
            "anova_p": an["p"] if an else None, "anova_F": an["F"] if an else None,
            "contribution_pct": (ss / sst * 100) if sst > 0 else None,
        })
    # 二水平交互（仅 2 水平正交表有意义）
    inter = []
    if levels == 2:
        for a, b in itertools.combinations(range(k), 2):
            buckets = {}
            for i in range(n_run):
                if responses[i] is None:
                    continue
                key = (int(round(float(design[i][a]))), int(round(float(design[i][b]))))
                buckets.setdefault(key, []).append(responses[i])
            if len(buckets) < 4:
                continue
            an = S.anova_oneway(list(buckets.values()))
            means = {("%d-%d" % kk): S.mean(v) for kk, v in buckets.items()}
            inter.append({"factors": [factor_names[a], factor_names[b]],
                          "cell_means": means,
                          "anova_F": an["F"] if an else None,
                          "anova_p": an["p"] if an else None})
    main.sort(key=lambda d: -(d["contribution_pct"] or 0))
    inter.sort(key=lambda d: -(d["anova_F"] or 0))
    ss_err = max(0.0, sst - sum(ss_factor.values()))
    return {"grand_mean": grand, "main_effects": main, "interactions": inter,
            "sst": sst, "ss_error": ss_err,
            "r2": (1 - ss_err / sst) if sst > 0 else None}


def sn_ratio(responses, goal="nominal", target=None):
    """Taguchi 信噪比 dB"""
    rs = [r for r in responses if r is not None]
    if not rs:
        return None
    n = len(rs)
    if goal == "smaller":
        return -10 * math.log10(sum(r * r for r in rs) / n)
    if goal == "larger":
        return -10 * math.log10(sum(1.0 / (r * r) for r in rs if r != 0) / n)
    # nominal
    m = S.mean(rs)
    v = S.variance(rs) or 0.0
    if m == 0 or v == 0:
        return None
    return 10 * math.log10(m * m / v)


# ------------------------------------------------------------------ 响应面寻优

def _rsm_features(X):
    """单个 coded 点（一维 list）-> 二次响应面特征向量（含截距）"""
    k = len(X)
    feats = []
    for x in X:
        feats.append(x)
    for i in range(k):
        feats.append(X[i] * X[i])
    for i in range(k):
        for j in range(i + 1, k):
            feats.append(X[i] * X[j])
    return feats


def fit_rsm(coded_X, y):
    """拟合二次响应面 y = b0 + Σbi xi + Σbii xi² + Σbij xi xj"""
    k = len(coded_X[0]) if coded_X else 0
    X = []
    Y = []
    for row, yv in zip(coded_X, y):
        if yv is None or any(v is None for v in row):
            continue
        X.append(_rsm_features(row))
        Y.append(yv)
    if len(X) < k + 2:
        return None
    fit = S.linear_regression(X, Y)
    if not fit:
        return None
    beta = [fit["intercept"]] + list(fit["coef"])
    names = ["intercept"]
    names += ["x%d" % (i + 1) for i in range(k)]
    names += ["x%d^2" % (i + 1) for i in range(k)]
    names += ["x%d*x%d" % (i + 1, j + 1) for i in range(k) for j in range(i + 1, k)]
    return {"beta": beta, "names": names, "r2": fit["r2"], "adj_r2": fit["adj_r2"],
            "k": k, "n": len(X), "sse": fit["sse"]}


def _rsm_predict(model, x):
    b = model["beta"]
    k = model["k"]
    kk = len(x)
    idx = 1
    v = b[0]
    for i in range(kk):
        v += b[idx] * x[i]
        idx += 1
    for i in range(kk):
        v += b[idx] * x[i] * x[i]
        idx += 1
    for i in range(kk):
        for j in range(i + 1, kk):
            v += b[idx] * x[i] * x[j]
            idx += 1
    return v


# ------------------------------------------------------------------ 工程约束与安全边界

def _coded_to_engineering(x, factor_specs):
    """coded(-1..1) → 工程单位值。
    不能复用 decode_design：它对 0<v<1 的连续 coded 值走的是 1-based 水平分支，
    会把 v=0.5 解成 mid-0.5·half（方向反了），约束校验就会放过真正的越界点。"""
    out = {}
    for spec, v in zip(factor_specs, x):
        low, high = spec.get("low"), spec.get("high")
        if low is None or high is None:
            out[spec.get("name")] = None
            continue
        mid = spec.get("mid", (float(low) + float(high)) / 2.0)
        half = (float(high) - float(low)) / 2.0
        out[spec.get("name")] = mid + float(v) * half
    return out


# 安全包络：设备与工艺的物理/安全硬限。企业规格可以收紧，不能放宽。
# 化成、高压、高温这类参数越界，代价不是"良率差一点"，是安全事故。
_SAFETY_RULES = [
    (re.compile(r"化成.*电压|充电.*电压|截止电压|上限电压", re.I), "max", 4.5, "V",
     "锂电芯充电/化成电压上限，越界有析锂与热失控风险"),
    (re.compile(r"化成.*温度|电芯.*温度|电池.*温度|烘烤温度", re.I), "max", 60.0, "℃",
     "电芯温度上限，越界加速副反应并可能引发热失控"),
    (re.compile(r"倍率|充电电流.*C|化成电流.*C", re.I), "max", 3.0, "C",
     "充电/化成倍率上限，过大电流导致析锂"),
    (re.compile(r"干燥.*温度|涂布.*温度|烘箱.*温度|烘干.*温度|烘道.*温度", re.I), "max", 150.0, "℃",
     "涂布干燥温度上限，受设备与浆料体系限制"),
    (re.compile(r"张力", re.I), "min", 0.0, "N",
     "张力不能为负，负值意味着机构反向受力"),
]


def safety_envelope(param_name):
    """按参数名匹配安全硬限，返回 {"min"/"max": v, "unit":.., "note":..} 或 None"""
    if not param_name:
        return None
    s = str(param_name)
    for pat, kind, val, unit, note in _SAFETY_RULES:
        if pat.search(s):
            return {"limit": kind, "value": val, "unit": unit, "note": note,
                    "source": "内置安全包络"}
    return None


def build_constraints(factor_specs, spec_overrides=None, kb_control=None):
    """汇总每个因子的工程约束：工艺窗口(low/high) + 企业规格 + 禁区 + 安全包络。
    返回 {参数名: {"min","max","forbid":[], "safety":.., "sources":[]}}"""
    out = {}
    for spec in factor_specs or []:
        name = spec.get("name")
        if not name:
            continue
        c = {"min": None, "max": None, "forbid": [], "safety": None, "sources": []}
        low, high = spec.get("low"), spec.get("high")
        if low is not None and high is not None:
            c["min"], c["max"] = float(low), float(high)
            c["sources"].append("试验水平区间")
        ov = (spec_overrides or {}).get(name)
        if isinstance(ov, dict):
            for kk in ("lsl", "min"):
                if ov.get(kk) is not None:
                    c["min"] = float(ov[kk]) if c["min"] is None else max(c["min"], float(ov[kk]))
                    c["sources"].append("企业规格下限")
            for kk in ("usl", "max"):
                if ov.get(kk) is not None:
                    c["max"] = float(ov[kk]) if c["max"] is None else min(c["max"], float(ov[kk]))
                    c["sources"].append("企业规格上限")
            for fb in (ov.get("forbid") or []):
                if isinstance(fb, (list, tuple)) and len(fb) == 2:
                    c["forbid"].append([float(fb[0]), float(fb[1])])
        ctl = (kb_control or {}).get(name)
        if isinstance(ctl, dict):
            for kk in ("lsl", "min"):
                if ctl.get(kk) is not None and c["min"] is None:
                    c["min"] = float(ctl[kk])
            for kk in ("usl", "max"):
                if ctl.get(kk) is not None and c["max"] is None:
                    c["max"] = float(ctl[kk])
        sf = safety_envelope(name)
        if sf:
            c["safety"] = sf
            # 安全边界只能比工艺窗口更紧，企业填的宽松值不能覆盖它
            if sf["limit"] == "max":
                c["max"] = sf["value"] if c["max"] is None else min(c["max"], sf["value"])
            else:
                c["min"] = sf["value"] if c["min"] is None else max(c["min"], sf["value"])
            c["sources"].append("安全包络（不可放宽）")
        out[name] = c
    return out


def check_point(point, constraints):
    """工程单位下逐条检查一个候选点。返回 (ok, violations:list[str])"""
    bad = []
    for name, v in (point or {}).items():
        if v is None:
            continue
        c = (constraints or {}).get(name)
        if not c:
            continue
        if c.get("min") is not None and v < c["min"]:
            bad.append("%s=%.4g 低于下限 %.4g" % (name, v, c["min"]))
        if c.get("max") is not None and v > c["max"]:
            bad.append("%s=%.4g 高于上限 %.4g" % (name, v, c["max"]))
        for fb in c.get("forbid") or []:
            if fb[0] <= v <= fb[1]:
                bad.append("%s=%.4g 落在禁止区间 [%.4g, %.4g]" % (name, v, fb[0], fb[1]))
    return (not bad), bad


def optimize_rsm_constrained(model, factor_specs, goal="maximize", target=None,
                             grid=21, bounds=(-1, 1), constraints=None):
    """带工程约束的响应面寻优。

    与 optimize_rsm 的本质区别：候选点先解码成工程单位，过不了约束就直接判死。
    在编码空间里"最优"的点，工程上常常是物理不可行或违反安全条件的——
    这类结果一旦被当成调机参数下发，责任边界就守不住了。
    """
    if not model:
        return None
    k = model["k"]

    def score(y):
        if goal == "maximize":
            return y
        if goal == "minimize":
            return -y
        return -abs(y - (target if target is not None else 0))

    def feasible(x):
        if not constraints or not factor_specs:
            return True, []
        return check_point(_coded_to_engineering(x, factor_specs), constraints)

    best_x, best_y = None, None
    best_free_x, best_free_y = None, None   # 无视约束的最优点，用于对比说明
    blocked = []
    starts = [[0.0] * k, [-0.5] * k, [0.5] * k]
    starts += [[1.0 if i == j else 0.0 for i in range(k)] for j in range(k)]
    starts += [[-1.0 if i == j else 0.0 for i in range(k)] for j in range(k)]
    steps = [bounds[0] + (bounds[1] - bounds[0]) * i / (grid - 1) for i in range(grid)]
    for st in starts:
        x = list(st)
        for _ in range(60):
            improved = False
            for d in range(k):
                cur = _rsm_predict(model, x)
                cand_best, cand_val = x[d], cur
                for s in steps:
                    xt = list(x)
                    xt[d] = s
                    ok, _ = feasible(xt)
                    if not ok:
                        continue
                    yv = _rsm_predict(model, xt)
                    if score(yv) > score(cand_val):
                        cand_val, cand_best = yv, s
                if abs(cand_best - x[d]) > 1e-9:
                    x[d] = cand_best
                    improved = True
            if not improved:
                break
        yv = _rsm_predict(model, x)
        if best_free_y is None or score(yv) > score(best_free_y):
            best_free_x, best_free_y = list(x), yv
        ok, why = feasible(x)
        if ok and (best_y is None or score(yv) > score(best_y)):
            best_x, best_y = list(x), yv
        elif not ok:
            blocked.append({"coded": list(x), "violations": why[:4]})
    res = {"goal": goal, "target": target, "constrained": bool(constraints)}
    if best_x is None:
        res.update({"feasible_optimum": None, "predicted_response": None,
                    "note": "所有候选点都越界：当前工艺窗口内不存在可行的最优点，"
                            "需要放宽窗口（须走变更评审）或调整目标",
                    "blocked_examples": blocked[:3],
                    "unconstrained_optimum": _coded_to_engineering(best_free_x, factor_specs)
                    if factor_specs else None})
        return res
    eng = _coded_to_engineering(best_x, factor_specs) if factor_specs else None
    res.update({"coded_optimum": best_x, "engineering_optimum": eng,
                "predicted_response": best_y, "feasible_optimum": True,
                "active_constraints": {n: (constraints or {}).get(n)
                                       for n in (eng or {})},
                "note": "推荐值已落在工艺窗口与安全包络内，仍需小批验证后方可固化"})
    if best_free_x and best_free_x != best_x:
        res["unconstrained_optimum"] = _coded_to_engineering(best_free_x, factor_specs)
        res["constraint_note"] = ("无约束最优点被工程窗口/安全包络挡下，"
                                  "已给出窗口内的最优可行点")
    return res


def optimize_rsm(model, goal="maximize", target=None, grid=21, bounds=(-1, 1)):
    """网格 + 坐标下降寻优，返回最优 coded 点与预测响应"""
    if not model:
        return None
    k = model["k"]

    def score(y):
        if goal == "maximize":
            return y
        if goal == "minimize":
            return -y
        return -abs(y - (target if target is not None else 0))

    # 坐标下降（多起点）
    best_x, best_y = None, None
    starts = [[0.0] * k, [-0.5] * k, [0.5] * k]
    starts += [[1.0 if i == j else 0.0 for i in range(k)] for j in range(k)]
    starts += [[-1.0 if i == j else 0.0 for i in range(k)] for j in range(k)]
    for st in starts:
        x = list(st)
        for _ in range(60):
            improved = False
            for d in range(k):
                cur = _rsm_predict(model, x)
                cand_best, cand_val = x[d], cur
                steps = [bounds[0] + (bounds[1] - bounds[0]) * i / (grid - 1) for i in range(grid)]
                for s in steps:
                    xt = list(x)
                    xt[d] = s
                    yv = _rsm_predict(model, xt)
                    if score(yv) > score(cand_val):
                        cand_val, cand_best = yv, s
                if abs(cand_best - x[d]) > 1e-9:
                    x[d] = cand_best
                    improved = True
            if not improved:
                break
        yv = _rsm_predict(model, x)
        if best_y is None or score(yv) > score(best_y):
            best_x, best_y = list(x), yv
    return {"coded_optimum": best_x, "predicted_response": best_y,
            "goal": goal, "target": target}
