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
import random
import re
try:
    from . import degrade          # 包模式
except Exception:                  # 脚本模式无包上下文
    import degrade                 # noqa
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


def decode_design(matrix, factor_specs, constraints=None):
    """coded/水平矩阵 -> 实际工艺参数值。
    factor_specs: [{"name":..,"low":..,"high":..,"mid":..(可选),"unit":..}]
    constraints: 可选，{参数名: build_constraints 返回的结构}；传入后逐点过 check_point，
        越界点在该条记录上挂 _safety_ok=False / _safety_violations，使 lib 层自带安全门禁——
        不再只有 pipeline 层 clamp 兜底，绕过 pipeline 直调 decode_design 也能拦住越界方案。

    水平号约定（1-based）：1=低, 2=中, 3=高, 0=中点, 负=编码值。
    关键：2 水平设计只有 1/2 两个水平号，此时 2 **就是高**而非中点；
    3 水平设计里 2 才是中点。原实现把 v==2 一律当中点，导致 2 水平设计
    （full_factorial(2^k) / Taguchi L8/L16，即默认推荐路径）永远解不出高角点，
    整条默认 DOE 只在「低/中」上做实验——这是默认 DOE 的正确性缺陷。
    修复：按各因子在矩阵里出现的最高水平号区分（max<=2 → 2 即高）。
    """
    max_lv = [3] * len(factor_specs)
    if matrix:
        for ci in range(len(factor_specs)):
            vals = [float(row[ci]) for row in matrix if ci < len(row)]
            if vals:
                max_lv[ci] = max(vals)
    out = []
    for row in matrix:
        rec = {}
        for ci, (spec, lv) in enumerate(zip(factor_specs, row)):
            v = float(lv)
            if v == 1:
                rec[spec["name"]] = spec.get("low")
            elif v == 3:
                rec[spec["name"]] = spec.get("high")
            elif v == 2:
                # 2 水平设计：水平号只有 1/2，2 就是高；3 水平设计：2 是中点
                if max_lv[ci] <= 2:
                    rec[spec["name"]] = spec.get("high")
                else:
                    rec[spec["name"]] = spec.get(
                        "mid", (spec.get("low", 0) + spec.get("high", 0)) / 2.0)
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
        if constraints:
            # lib 层安全门禁：解码后立刻逐点核验，越界点不让"看起来像合法方案"溜过去。
            _ok, _viol = check_point(rec, constraints)
            rec["_safety_ok"] = _ok
            rec["_safety_violations"] = _viol
        out.append(rec)
    return out


# ------------------------------------------------------------------ 设计结构（P1-7）
#
# 一张设计矩阵本身不是可执行方案：没说先做哪一号、做几次、要不要分区组、
# 哪些效应其实混在一起分不开、这个样本量到底能检出多大的差异。
# 这五件事过去都停在自由文本里，现场拿到矩阵只能靠习惯补——补错了没人发现。
# 这一段把它们变成结构化字段，让"能不能照着做、做出来能不能下结论"可被检查。

def _norm_ppf(p):
    """标准正态分位数（二分法，零依赖；不引 scipy）。p∈(0,1)"""
    try:
        p = float(p)
    except Exception:
        return None
    if not (0.0 < p < 1.0):
        return None
    lo, hi = -12.0, 12.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if S.norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _two_level_cols(matrix):
    """设计矩阵 -> 0/1 列。仅当矩阵确为二水平时返回 (cols, coding)，否则返回 None。

    二水平设计有三种常见写法（0/1、1/2 水平号、-1/+1 编码），别名代数只在
    二水平正规设计上成立，所以这里必须先判清楚，判不清就不做推断。
    """
    if not matrix or not matrix[0]:
        return None
    vals = set()
    for row in matrix:
        for v in row:
            try:
                vals.add(int(round(float(v))))
            except Exception:
                return None
    if not vals:
        return None
    w = len(matrix[0])
    try:
        if vals <= {0, 1}:
            return ([[int(round(float(r[j]))) for r in matrix] for j in range(w)], "0/1")
        if vals <= {1, 2}:
            return ([[int(round(float(r[j]))) - 1 for r in matrix] for j in range(w)], "1/2")
        if vals <= {-1, 1}:
            return ([[0 if int(round(float(r[j]))) < 0 else 1 for r in matrix]
                     for j in range(w)], "-1/+1")
    except Exception as _e:
        degrade.note("doe._two_level_cols", _e)
        return None
    return None


def alias_structure(matrix, factor_names=None):
    """二水平正规设计的别名结构——在矩阵上直接判，不查表、不写死哪张表几级。

    原理：±1 编码下交互列等于主效应列的按位异或（A·B ↔ a^b），
    于是「主效应 j 与交互 (a,b) 混杂」等价于 col_j == col_a ^ col_b。
    判据落在矩阵上，换任何一张二水平表都仍然成立；
    三水平与响应面设计不适用这套代数，如实返回 applicable=False，不硬凑。
    """
    got = _two_level_cols(matrix)
    if got is None:
        return {"applicable": False,
                "reason": "非二水平正规设计（三水平正交表 / 响应面设计不适用二水平别名代数）",
                "resolution": None, "main_aliases": [], "two_fi_aliases": [],
                "clear_main_effects": None,
                "note": "该设计的混杂结构需按其自身模型评估，此处不做别名推断，避免给出看似精确的错结论"}
    cols, coding = got
    n = len(matrix)
    k = len(cols)
    names = [(factor_names[j] if factor_names and j < len(factor_names) and factor_names[j]
              else "X%d" % (j + 1)) for j in range(k)]

    def _xor(idxs):
        acc = [0] * n
        for i in idxs:
            acc = [x ^ y for x, y in zip(acc, cols[i])]
        return acc

    pairs = list(itertools.combinations(range(k), 2))
    pair_col = {}
    for p in pairs:
        pair_col[p] = _xor(p)

    main_aliases = []
    _main_with_2fi = False
    _main_with_3fi = False
    for j in range(k):
        hit2 = ["%s×%s" % (names[a], names[b]) for a, b in pairs
                if a != j and b != j and cols[j] == pair_col[(a, b)]]
        hit3 = []
        if not hit2 and k >= 4:
            hit3 = ["%s×%s×%s" % (names[a], names[b], names[c])
                    for a, b, c in itertools.combinations(range(k), 3)
                    if j not in (a, b, c) and cols[j] == _xor((a, b, c))]
        if hit2:
            _main_with_2fi = True
        elif hit3:
            _main_with_3fi = True
        if hit2 or hit3:
            main_aliases.append({"factor": names[j],
                                 "order": 2 if hit2 else 3,
                                 "aliased_with": (hit2 or hit3)[:6]})

    two_fi = []
    for p in pairs:
        hit = ["%s×%s" % (names[q[0]], names[q[1]]) for q in pairs
               if q != p and not (set(q) & set(p)) and pair_col[p] == pair_col[q]]
        if hit:
            two_fi.append({"interaction": "%s×%s" % (names[p[0]], names[p[1]]),
                           "aliased_with": hit[:6]})

    if _main_with_2fi:
        resolution = "III"
    elif _main_with_3fi or two_fi:
        resolution = "IV"
    else:
        resolution = "≥V"
    notes = {
        "III": "主效应与二因子交互混杂：交互若真实存在，它的贡献会被算进主效应。"
               "只能用于筛选阶段，选出的因子必须经确认试验再下结论",
        "IV": "主效应不与二因子交互混杂（主效应可解释），但二因子交互之间彼此混杂，"
              "交互只能成组判断、不能指认到具体某一对",
        "≥V": "主效应与二因子交互均互不混杂，可分别估计",
    }
    return {"applicable": True, "coding": coding, "resolution": resolution,
            "main_aliases": main_aliases, "two_fi_aliases": two_fi,
            "clear_main_effects": not _main_with_2fi,
            "note": notes[resolution]}


def power_analysis(n_runs, levels=2, alpha=0.05, target_power=0.80, sigma=None):
    """二水平设计的功效评估（正态近似，零依赖）。

    比较两个水平均值时每水平样本量 n_per = n_runs/levels，
    均值差的标准误 = σ·sqrt(1/n_per + 1/n_per) = σ·sqrt(2/n_per)。
    所以「能检出多大的效应」= (z_{1-α/2} + z_{1-β}) · sqrt(2/n_per) · σ。
    这个数字不写进方案，使用者就不知道"没检出显著"到底是真没差异还是样本量不够。
    """
    try:
        n_runs = int(n_runs)
    except Exception:
        return None
    if n_runs < 2 or levels < 2:
        return None
    n_per = n_runs / float(levels)
    if n_per < 1.0:
        return None
    se = math.sqrt(2.0 / n_per)
    z_a = _norm_ppf(1.0 - alpha / 2.0)
    z_b = _norm_ppf(target_power)
    if z_a is None or z_b is None:
        return None
    d_detect = (z_a + z_b) * se

    def _pw(d):
        ncp = d / se
        return S.norm_cdf(ncp - z_a) + S.norm_cdf(-ncp - z_a)

    out = {
        "alpha": alpha, "target_power": target_power,
        "n_per_level": n_per, "se_in_sigma": se,
        "detectable_effect_sigma": d_detect,
        "detectable_effect_abs": (d_detect * sigma) if sigma else None,
        "power_at_1sigma": _pw(1.0), "power_at_2sigma": _pw(2.0),
        "basis": "两水平均值比较的正态近似（z 检验），未计检验的自由度损失，"
                 "小样本下真实功效略低于此值",
    }
    out["note"] = (
        "按 α=%.2f、功效 %.0f%% 计，本方案每水平 %g 次试验，"
        "可检出的最小效应约为 %.2f 倍标准差%s。"
        "效应小于此值的因子大概率检不出——那时\"不显著\"不等于\"没影响\"，只能说本次样本量看不见它"
        % (alpha, target_power * 100, n_per, d_detect,
           ("（约 %.4g）" % (d_detect * sigma)) if sigma else ""))
    return out


def design_structure(matrix, factor_names=None, n_replicates=1, n_blocks=1,
                     seed=0, levels=2, alpha=0.05, target_power=0.80, sigma=None):
    """把设计矩阵升级为可执行、可审计的试验结构（P1-7）。

    返回 dict：runs_base / n_factors / total_runs / run_sheet / randomization /
               replicates / blocks / alias / power / residual_df / caveats
    run_sheet 是可直接打印带走的作业顺序表（seq / replicate / base_run / block）。
    """
    if not matrix:
        return None
    try:
        rep = max(1, int(n_replicates))
    except Exception:
        rep = 1
    try:
        nb = max(1, int(n_blocks))
    except Exception:
        nb = 1
    n = len(matrix)
    k = len(matrix[0]) if matrix[0] else 0
    if n <= 0 or k <= 0:
        return None

    rng = random.Random(seed)
    run_sheet = []
    block_sizes = {}
    seq = 0
    for r in range(1, rep + 1):
        order = list(range(n))
        rng.shuffle(order)      # 每个重复各自独立随机化，不复用同一顺序
        per = max(1, n // nb) if nb > 1 else n
        for pos, base in enumerate(order):
            seq += 1
            blk = min(nb, pos // per + 1) if nb > 1 else 1
            run_sheet.append({"seq": seq, "replicate": r, "base_run": base + 1,
                              "block": blk})
            block_sizes[blk] = block_sizes.get(blk, 0) + 1

    # 残差自由度：试验次数 - 1 - 主效应个数。正交表把列排满时它会归零，
    # 那时没有独立的误差估计，ANOVA 的 F 检验无从谈起——这是最常见的"做了却下不了结论"。
    residual_df = n - 1 - k

    caveats = []
    if rep <= 1:
        caveats.append({"level": "high",
                        "text": "未设重复（每号只做 1 次）：没有纯误差估计，"
                                "显著性只能借用残差自由度，结论不稳。建议至少 2 次重复或增设中心点"})
    if residual_df <= 0:
        caveats.append({"level": "high",
                        "text": "残差自由度为 %d（试验 %d 次 − 1 − 因子 %d ≤ 0）："
                                "误差项无从估计，F 检验与 p 值不可用。"
                                "必须增加重复数、加中心点，或减少因子数重排设计"
                                % (residual_df, n, k)})
    elif residual_df < 3:
        caveats.append({"level": "info",
                        "text": "残差自由度仅 %d，误差估计很粗糙，显著性结论请按趋势看待" % residual_df})
    if nb > 1:
        caveats.append({"level": "info",
                        "text": "已划 %d 个区组（每区组 %s 次）：区组按随机化顺序切分，"
                                "区组效应可能与时间漂移部分混杂。"
                                "若噪声源明确（班次/机台/原料批），建议整区组内跑完一轮完整设计"
                                % (nb, "、".join(str(block_sizes.get(b, 0)) for b in range(1, nb + 1)))})

    alias = alias_structure(matrix, factor_names)
    if alias.get("applicable") and alias.get("resolution") == "III":
        caveats.append({"level": "high",
                        "text": "该设计分辨度 III：主效应与二因子交互混杂。"
                                "只能用于筛选，选出的因子需经确认试验才能作为调参依据"})
    if not alias.get("applicable"):
        caveats.append({"level": "info", "text": alias.get("reason") or "未提供别名结构"})

    power = power_analysis(n * rep, levels=levels, alpha=alpha,
                           target_power=target_power, sigma=sigma)

    return {
        "runs_base": n, "n_factors": k, "n_replicates": rep, "n_blocks": nb,
        "total_runs": n * rep, "residual_df": residual_df,
        "run_sheet": run_sheet,
        "randomization": {
            "enabled": True, "seed": seed,
            "note": "随机化不是为了好看：设备漂移、班次、环境温湿度这类未知干扰若不被打散，"
                    "会系统性地落在某些水平上，冒充成因子效应。"
                    "执行时必须按 run_sheet 的顺序做，不要为了省事把同一水平的点连着做",
        },
        "replicates": {
            "n": rep,
            "note": "重复提供纯误差估计：没有重复就无法区分\"因子真的有影响\""
                    "与\"这次碰巧测成这样\"",
        },
        "blocks": {
            "n": nb, "sizes": block_sizes,
            "note": "区组用于隔离已知噪声源（班次 / 机台 / 原料批），"
                    "让区组内的比较不受该噪声污染",
        },
        "alias": alias,
        "power": power,
        "caveats": caveats,
    }


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
#
# 【重要】下面这张表只是**无外部输入时的参考兜底**，不是通用真理：
#   1) 数值取自动力电池/涂布这类行业的常见做法。换一个产品线、换一台设备，
#      真实上限可能更高也可能更低——所以调用方应当通过
#      build_constraints(spec_overrides={参数名: {"safety": {...}}}
#      传入自己档案里的安全限，**调用方给的优先于本表**。
#   2) 调用方给的值若比本表更松，不会静默覆盖：会在结果里挂
#      looser_than_default 标记并写进报告，让人确认依据。
#   3) "更严的一侧取胜、但不许悄悄放宽"这条规则本身与行业无关，属于安全推理逻辑，
#      这部分才是不该被覆盖的东西。
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


def safety_envelope(param_name, rules=None):
    """按参数名匹配安全硬限，返回 {"min"/"max": v, "unit":.., "note":..} 或 None。

    rules 非空时按其匹配而不查内置表——调用方比通用库更清楚自己设备的极限。
    """
    if not param_name:
        return None
    s = str(param_name)
    for pat, kind, val, unit, note in (_SAFETY_RULES if rules is None else rules):
        _pat = pat if hasattr(pat, "search") else re.compile(str(pat), re.I)
        if _pat.search(s):
            src = "内置安全包络（行业参考值，应以本机/本产品档案为准）" \
                if rules is None else "调用方提供的安全包络"
            return {"limit": kind, "value": val, "unit": unit, "note": note,
                    "source": src}
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
        sf = _safety_from_override(ov, name)
        if sf:
            c["safety"] = sf
            # 安全边界只能比工艺窗口更紧，企业填的宽松值不能覆盖它
            if sf["limit"] == "max":
                c["max"] = sf["value"] if c["max"] is None else min(c["max"], sf["value"])
            else:
                c["min"] = sf["value"] if c["min"] is None else max(c["min"], sf["value"])
            c["sources"].append(sf.get("source") or "安全包络（不可放宽）")
            if sf.get("looser_than_default"):
                # 放宽是调用方的权利（只有他知道自家设备的实际极限），
                # 但放宽必须留痕：报告里要能看出这一步是人拍的，不是工具替他选的。
                c["safety_warning"] = (
                    "调用方给出的安全%s %.4g%s 松于内置行业参考值 %.4g%s；"
                    "已按调用方的值执行，但请确认有设备/产品档案依据。"
                    % ("上限" if sf["limit"] == "max" else "下限",
                       sf["value"], sf.get("unit") or "",
                       sf["looser_than_default"]["builtin"],
                       sf["looser_than_default"].get("unit") or ""))
        out[name] = c
    return out


def _safety_from_override(ov, name):
    """从调用方的参数明细里读安全包络；读不到就回落到内置参考表。"""
    _ovsf = ov.get("safety") if isinstance(ov, dict) else None
    if not (isinstance(_ovsf, dict) and _ovsf.get("value") is not None):
        return safety_envelope(name)
    try:
        _lim = _ovsf.get("limit") or "max"
        if _lim not in ("max", "min"):
            return safety_envelope(name)
        sf = {"limit": _lim, "value": float(_ovsf["value"]),
              "unit": _ovsf.get("unit") or "",
              "note": _ovsf.get("note") or "调用方提供的安全限",
              "source": "调用方提供的安全包络"}
    except (TypeError, ValueError):
        return safety_envelope(name)
    _bi = safety_envelope(name)
    if _bi and _bi["limit"] == sf["limit"]:
        _looser = ((sf["value"] > _bi["value"]) if sf["limit"] == "max"
                   else (sf["value"] < _bi["value"]))
        if _looser:
            sf["looser_than_default"] = {"builtin": _bi["value"],
                                         "unit": _bi.get("unit") or ""}
    return sf


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


# scan:keep RSM 寻优备用实现。当前 DOE 走 optimize（正交/爬山），RSM 在需要响应面模型时启用。
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


# ------------------------------------------------------------------ P1-8 受控边界

# 通用量纲换算表。只收物理量纲的量纲级关系，**不含任何产品的经验阈值**——
# 产品的具体上下限属于企业受控数据，由调用方经 --spec 传入，本模块不替它编。
#
# 温度刻意不给换算：℃↔℉ 是仿射变换（带 32/1.8 偏移），套比例因子必错。
# 与其算错一个 mils 不如明说"请统一写法"——拦下来的代价远小于放过去的代价。
_LINEAR_SCALES = [
    ("长度", {"mm": 1.0, "um": 1e-3, "μm": 1e-3, "nm": 1e-6, "cm": 10.0,
             "m": 1000.0, "mil": 0.0254, "inch": 25.4}),
    ("时间", {"s": 1.0, "min": 60.0, "h": 3600.0, "ms": 1e-3, "minute": 60.0,
             "sec": 1.0, "second": 1.0}),
    ("电压", {"v": 1.0, "mv": 1e-3, "kv": 1e3}),
    ("电流", {"a": 1.0, "ma": 1e-3, "ua": 1e-6}),
    ("质量", {"g": 1.0, "mg": 1e-3, "kg": 1000.0, "ug": 1e-6}),
    ("功率", {"w": 1.0, "mw": 1e-3, "kw": 1e3}),
    ("压力", {"pa": 1.0, "kpa": 1e3, "mpa": 1e6, "bar": 1e5, "mpa·s": None}),
]
_TEMP_UNITS = {"℃", "°c", "c", "摄氏", "℉", "°f", "f", "华氏", "k", "开尔文"}


def unit_factor(src, dst):
    """单位一致性换算。返回 (factor, note)。

    factor 说明如何把"以 src 为单位的值"转成 dst；换算不通返回 (None, 原因)。
    DOE 边界做**单位不明就阻断**的依据就在这里：
    企业给的 [-40, 150] 到底是 ℃ 还是 ℉，决定了 150 是烘烤温度还是安全事故。
    """
    if not src or not dst:
        return None, "单位缺失，无法确认量纲是否一致"
    su, du = str(src).strip().lower(), str(dst).strip().lower()
    if su == du:
        return 1.0, "单位一致"
    if su in _TEMP_UNITS or du in _TEMP_UNITS:
        return None, ("%s 与 %s 之间存在温度换算（含零点偏移），"
                      "DOE 边界换算不处理仿射单位，请统一写法后重来" % (src, dst))
    for _, tbl in _LINEAR_SCALES:
        if su in tbl and du in tbl:
            a, b = tbl[su], tbl[du]
            if a is None or b is None:
                return None, "单位 %s/%s 缺换算基准" % (src, dst)
            return (a / b), "同量纲换算 %s→%s" % (src, dst)
    return None, "「%s」与「%s」不属于同一量纲（或不在通用换算表内），不能混用" % (src, dst)


def _src_record(kind, confidence, file=None, version=None, approval=None,
                effective=None, scope=None, note=None):
    """边界来源的一条档案记录。

    报告 P1-8 点名要"记录来源文件、版本、审批状态、生效日期和适用产品/设备"，
    所以这里五项都是一等字段，不是附注。少哪一项，这一条就只能算"推断"，
    不能算"受控"——这个区分是整个门禁的意义所在。
    """
    return {"kind": kind, "confidence": confidence, "file": file,
            "version": version, "approval": approval, "effective": effective,
            "scope": scope, "note": note}


def controlled_boundaries(factor_specs, spec_overrides=None, kb_control=None):
    """把每个因子的上下限收成"带出处的受控边界"。

    与 build_constraints 的分工：
      build_constraints 负责算出最终 min/max（工程用途）
      这里负责回答"这对 min/max 凭什么"——少一件证据就不能算受控。
    返回 {name: {"min","max","forbid","unit","blockers","warnings","sources"}}
    """
    out = {}
    overrides = spec_overrides or {}
    meta = overrides.get("_meta") if isinstance(overrides, dict) else None
    if not isinstance(meta, dict):
        meta = None

    def _meta_src(kind):
        if not meta:
            return _src_record(kind, "推断",
                               note="未提供 --spec 的来源元信息(_meta)，"
                                    "无法证明该边界来自受控文件")
        missing = [k for k in ("file", "version", "approval")
                   if not meta.get(k)]
        return _src_record(
            kind, "受控" if not missing else "推断",
            file=meta.get("file"), version=meta.get("version"),
            approval=meta.get("approval"), effective=meta.get("effective"),
            scope=meta.get("scope"),
            note=("缺非空检查项：%s" % "、".join(missing)) if missing else None)

    for spec in factor_specs or []:
        name = spec.get("name")
        if not name:
            continue
        rec = {"min": None, "max": None, "forbid": [], "unit": spec.get("unit"),
               "blockers": [], "warnings": [], "sources": [], "unit_conflicts": []}

        low, high = spec.get("low"), spec.get("high")
        if low is not None and high is not None:
            try:
                rec["min"], rec["max"] = float(low), float(high)
                rec["sources"].append(_src_record(
                    "试验水平区间", "推断",
                    note="取自本轮数据的分布/设备归档区间，属试验范围不是量产规格"))
            except (TypeError, ValueError):
                rec["blockers"].append("试验水平区间不是数值（low=%r high=%r）" % (low, high))

        # 企业受控规格
        ov = overrides.get(name)
        if isinstance(ov, dict):
            for key, side in (("lsl", "min"), ("min", "min"),
                              ("usl", "max"), ("max", "max")):
                v = ov.get(key)
                if v is None:
                    continue
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    rec["blockers"].append("企业规格 %s=%r 不是数值" % (key, v))
                    continue
                if ov.get("unit") and rec["unit"] and ov.get("unit") != rec["unit"]:
                    f, note = unit_factor(ov.get("unit"), rec["unit"])
                    if f is None:
                        rec["unit_conflicts"].append(note)
                        continue
                    v = v * f
                if side == "min":
                    rec["min"] = v if rec["min"] is None else max(rec["min"], v)
                else:
                    rec["max"] = v if rec["max"] is None else min(rec["max"], v)
            if any(ov.get(k) is not None for k in ("lsl", "usl", "min", "max")):
                rec["sources"].append(_meta_src("企业受控规格"))
            for fb in (ov.get("forbid") or []):
                if isinstance(fb, (list, tuple)) and len(fb) == 2:
                    rec["forbid"].append([float(fb[0]), float(fb[1])])

        # 设备/知识库控制限
        ctl = (kb_control or {}).get(name)
        if isinstance(ctl, dict):
            for key, side in (("lsl", "min"), ("min", "min"),
                              ("usl", "max"), ("max", "max")):
                v = ctl.get(key)
                if v is None:
                    continue
                try:
                    v = float(v)
                except (TypeError, ValueError) as _dg_e:
                    # 控制限数值没法转成浮点 -> 这条边界被丢掉，而 rec["sources"]
                    # 仍会写"知识库控制限"。下游会把边界当成已受控，实际是没受住。
                    # DOE 边界错一项就可能把工艺推到设备允许范围之外，必须留痕。
                    degrade.note("doe.controlled_boundaries@L728", _dg_e)
                    continue
                if side == "min":
                    rec["min"] = v if rec["min"] is None else max(rec["min"], v)
                else:
                    rec["max"] = v if rec["max"] is None else min(rec["max"], v)
            if any(ctl.get(k) is not None for k in ("lsl", "usl", "min", "max")):
                rec["sources"].append(_src_record("知识库控制限", "推断",
                                                  note="知识库条目当前没有登记出处与审批信息"))

        # 内置安全包络：靠参数名匹配，天然只是兜底，必须标"推断"
        sf = safety_envelope(name)
        if sf:
            rec["sources"].append(_src_record(
                "内置安全包络", "推断", note=sf.get("note"),
                file="lib/doe.py::_SAFETY_RULES"))
            if sf["limit"] == "max":
                rec["max"] = sf["value"] if rec["max"] is None else min(rec["max"], sf["value"])
            else:
                rec["min"] = sf["value"] if rec["min"] is None else max(rec["min"], sf["value"])

        for c in rec["unit_conflicts"]:
            rec["blockers"].append("单位冲突：%s" % c)
        if rec["min"] is not None and rec["max"] is not None and rec["min"] > rec["max"]:
            rec["blockers"].append("边界冲突：下限 %.6g 大于上限 %.6g" % (rec["min"], rec["max"]))
        conf = [s["confidence"] for s in rec["sources"]]
        rec["controlled"] = "受控" in conf
        rec["confidence"] = "受控" if rec["controlled"] else ("推断" if conf else "无来源")
        if not conf:
            rec["blockers"].append("该因子没有任何边界来源（企业规格/设备限制/安全包络均无），"
                                   "不能凭 guess 生成试验水平")
        out[name] = rec
    return out


# 推断边界的政策旋钮（P2 遗留项）。
# 同样的"边界是推断出来的、没有受控出处"，有两种合理处置：
#   warn  —— 出方案 + 警告 + 要求工艺工程师书面确认（默认）
#   block —— 直接不出方案，逼出那份受控规格文件
# 默认 warn 不是"不敢拦"，是因为一刀切硬拦会让没带规格文件的用户完全用不了；
# 但"只能 warn、拦不住"同样是缺陷——企业侧一旦有受控规格，就该能收紧成硬阻断。
BOUNDARY_POLICIES = ("warn", "block")


def boundary_gate(bounds, plan_name="DOE 方案", policy="warn"):
    """P1-8 门禁：缺少有效边界、边界冲突、单位不明，一律阻断方案生成。

    报告原话：「缺少有效边界、边界冲突或单位不明时应阻断方案生成，不能默认放行。」
    所以这里必须是 blocker 而不是 warning——warning 会被人顺手关掉，
    blocker 才会逼出那份受控规格文件。

    policy —— "warn"（默认）："边界无受控来源"只警告并要求书面确认；
              "block"：把这类警告一并提升为阻断，不出方案。
              传其他值直接抛错，不静默回落成 warn——政策旋钮静默回落，
              等于使用者以为收紧了其实没收紧。
    """
    policy = (policy or "warn")
    if policy not in BOUNDARY_POLICIES:
        raise ValueError("未知边界政策 %r，可选：%s"
                         % (policy, "、".join(BOUNDARY_POLICIES)))
    if bounds is None:
        return {"ok": False, "plan_name": plan_name, "n_factors": 0,
                "blockers": ["没有收到任何因子的边界数据（bounds=None）。"
                             "零边界不等于「无约束」，等于「约束未知」，不能放行"],
                "warnings": []}
    if not isinstance(bounds, dict):
        raise TypeError("bounds 必须是 {因子名: 边界记录} 字典，当前为 %s"
                        % type(bounds).__name__)
    if not bounds:
        # 零因子判 ok=True 是这个门禁最容易被骗过去的地方：
        # 上游只要返回空 dict（因子全被过滤、解析失败、传错参数），
        # 这里就会为一份空方案盖上"边界受控"的章。约束未知 ≠ 无约束。
        return {"ok": False, "plan_name": plan_name, "n_factors": 0,
                "blockers": ["方案里没有任何因子（0 个受控边界）。"
                             "可能是上游未产出因子，不能按无约束放行"],
                "warnings": []}
    blockers, warns = [], []
    for name, rec in bounds.items():
        if not isinstance(rec, dict):
            blockers.append("因子「%s」的边界记录不是字典（%s），无法核验"
                            % (name, type(rec).__name__))
            continue
        for b in rec.get("blockers", []):
            blockers.append("因子「%s」：%s" % (name, b))
        if rec.get("min") is None or rec.get("max") is None:
            side = "下限" if rec.get("min") is None else "上限"
            blockers.append("因子「%s」缺%s，无法确证试验水平落在受控窗口内"
                            % (name, side))
        if not rec.get("controlled"):
            warns.append("因子「%s」的边界没有受控来源（现为「%s」），"
                         "仅按 %s 推断，须由工艺工程师书面确认后方可试跑"
                         % (name, rec.get("confidence"),
                            "、".join(s["kind"] for s in rec.get("sources", [])) or "无来源"))
    # 政策收紧：warn 级的"边界无受控来源"在 block 政策下升级为阻断。
    # 升级后 warnings 必须清空并留下 escalated 计数——
    # 否则调用方会一边看到 ok=False，一边还打印"须书面确认"的放行口吻。
    escalated = 0
    if policy == "block" and warns:
        blockers = blockers + ["【已按 block 政策收紧】" + w for w in warns]
        escalated = len(warns)
        warns = []
    return {"ok": not blockers, "blockers": blockers, "warnings": warns,
            "plan_name": plan_name, "n_factors": len(bounds),
            "policy": policy, "escalated": escalated}
