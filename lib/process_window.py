# -*- coding: utf-8 -*-
"""
process_window.py —— 工艺窗口：多参数联合合格率与可行域

=============================================================================
为什么必须单独一层（单参数 Cpk 会说谎）
=============================================================================
    半导体/精密制造里一个工序同时被 CD、膜厚、Overlay、颗粒…… 多个参数约束。
    常见做法是"每个参数 Cpk 都 ≥1.33 就算窗口够"，这是**错的**：
    参数之间相关，联合合格率不等于各参数合格率的乘积。

    但**偏差方向取决于规格形态**，这一点必须先分清，否则会把结论说反
    （本项目自检第一版就把两者混为一谈，被已知真值抓出来）：

      · 对称双侧规格（如 CD ±3nm，均值居中）：
        ρ=±1 时两个参数"同时进窗口"，联合合格率 = 单参数合格率 p；
        ρ=0 时 = p²。**|ρ| 越大联合越高，各参数相乘（p²）低估真实值。**
        例：p=0.95 → 乘积 0.9025，ρ=0.8 时约 0.93。

      · 单侧同向规格（如两项都要求"越低越好"，只给 USL）：
        ρ=+1 → 联合 = p；ρ=−1 → 联合 = p₁+p₂−1（可能低到 0）。
        **此时各参数相乘会高估。**

    所以"逐参数都合格"和"这一片真的合格"是两件事，差多少既取决于相关结构、
    也取决于规格是单侧还是双侧。工具的价值是**把这个差算出来并说清方向**。

=============================================================================
算法口径
=============================================================================
    · 单参数合格率：正态假设下 P(LSL ≤ X ≤ USL)，用参数自身的 μ/σ 化成标准化上下限。
    · 联合合格率（k=2）：**数值积分**（条件正态 + Simpson），吃**真实的非对称上下限**，
      不做"规格对称"的简化假设。
    · k>2：蒙特卡洛（固定种子，样本数可配），回报 95% 蒙特卡洛误差带。
    · 相关系数缺失时按 0 处理但**必须告警**：默认独立是假设，不是事实。
    · 规格必须来自受控规格书（LSL/USL 由调用方传），本模块不推断规格。

=============================================================================
边界（改代码前先读）
=============================================================================
    1. **不给"窗口够用"的结论。** 只给数字与它在什么假设下成立。
    2. 正态假设不成立时结论不可用——这里只做 Jarque-Bera 体检并告警，
       不自动换分布（换分布需要人为选择，工具不替人猜）。
    3. 相关结构用**样本相关系数**估计，样本量小的时候它自己就不可靠；
       n<30 时告警。
    4. k>2 用蒙特卡洛时给出误差带；两个点的差距小于误差带时不下"谁更好"的判断。

零第三方依赖：stdlib only。
"""

import math
import random

try:
    from . import status as ST
    from . import stats as S
    from . import table as T
except Exception:                       # 脚本模式
    import status as ST                 # noqa
    import stats as S                   # noqa
    import table as T                   # noqa


def _fnum(v):
    """把表格单元格转成 float（单元格通常是字符串，如 '44.12' 或 '108.2 g/m²'）。

    为什么不能直接用 stats.is_num：is_num 只接受**已是数值**的类型，
    表格里的 '44.12' 是字符串 → is_num 返回 False。四个模块的自检都用
    Python float 直接构造数据，所以单测全绿，一接真 CSV 就整列算不出来
    （n=0）——典型的"单测过、集成崩"。统一走 table.to_number 这个已被
    1800 万次调用验证过的解析器，不另造一套。
    """
    n = T.to_number(v)
    return n if (n is not None and S.is_num(n)) else None

# 相关系数估计的最小样本量：低于此值样本 r 本身波动大到没有决策价值
MIN_N_FOR_CORR = 30
# 单参数合格率体检用的正态性最小样本量
MIN_N_FOR_JB = 20


def _norm_ppf(p):
    """标准正态分位（二分求 erf 反函数），避免引入 scipy。"""
    if not (0.0 < p < 1.0):
        raise ValueError("p 必须在 (0,1)")
    lo, hi = -10.0, 10.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        cdf = 0.5 * (1.0 + math.erf(mid / math.sqrt(2.0)))
        if cdf < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _phi(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _Phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def marginal_conformance(values, lsl=None, usl=None):
    """单参数合格率与能力指数（正态假设）。

    只给单参数数字，**不据此宣称窗口够用**——见模块头。
    """
    xs = [x for x in (_fnum(v) for v in (values or [])) if x is not None]
    n = len(xs)
    out = {"n": n, "mean": None, "sigma": None, "p_in_spec": None,
           "cp": None, "cpk": None, "lsl": lsl, "usl": usl,
           "normality_p": None, "std_bounds": None,
           "warnings": [], "status": ST.INSUFFICIENT_DATA}
    if n < 2:
        out["warnings"].append("样本不足 2 个，无法算合格率")
        return out
    mu = S.mean(xs)
    sd = S.stdev(xs)
    out["mean"], out["sigma"] = mu, sd
    if not sd or sd <= 0:
        out["warnings"].append("标准差为 0（全部相同值）：无法算能力指数")
        return out

    # 规格：两端都空时**不得**编出一个合格率来（曾写成 1-0-1=0，把"没规格"
    # 报成"合格率 0%"——方向完全反了，被自检抓出）
    has_spec = (lsl is not None) or (usl is not None)
    if not has_spec:
        out["warnings"].append("没有规格限（LSL/USL 都为空）：只能给分布描述，"
                               "不给合格率（无规格时任何合格率都是编的）")
    else:
        p_lo = _Phi((lsl - mu) / sd) if lsl is not None else 0.0
        p_hi = (1.0 - _Phi((usl - mu) / sd)) if usl is not None else 0.0
        out["p_in_spec"] = max(0.0, min(1.0, 1.0 - p_lo - p_hi))
        # 真实标准化上下限（非对称也照实给，联合窗口要用）
        out["std_bounds"] = [
            ((lsl - mu) / sd) if lsl is not None else float("-inf"),
            ((usl - mu) / sd) if usl is not None else float("inf"),
        ]

    # Cp 需要双侧规格，Cpk 只需要一侧
    if lsl is not None and usl is not None:
        out["cp"] = (usl - lsl) / (6.0 * sd)
        out["cpk"] = min((usl - mu) / (3.0 * sd), (mu - lsl) / (3.0 * sd))
    elif usl is not None:
        out["cpk"] = (usl - mu) / (3.0 * sd)
    elif lsl is not None:
        out["cpk"] = (mu - lsl) / (3.0 * sd)

    if n >= MIN_N_FOR_JB:
        # stats.jarque_bera 返回的是元组 (JB, p)，不是 dict——按元组解包
        jb_stat, jb_p = S.jarque_bera(xs)
        if jb_p is not None:
            out["normality_p"] = jb_p
            if jb_p < 0.05:
                out["warnings"].append(
                    "正态性检验 p=%.4f < 0.05：正态假设不成立，本合格率数字不可作结论。"
                    "（工具不自动换分布——换分布要人来选）" % jb_p)
    else:
        out["warnings"].append("样本 %d < %d：正态性无法检验，合格率仅作参考"
                               % (n, MIN_N_FOR_JB))

    out["status"] = ST.SUCCESS if out["p_in_spec"] is not None else ST.INSUFFICIENT_DATA
    return out


def _bivar_cdf(a, b, rho, panels=400):
    """标准二元正态 P(X≤a, Y≤b)，ρ 给定。

    用条件分解 + Simpson 数值积分：
        P = ∫_{-∞}^{a} φ(x)·Φ((b − ρx)/√(1−ρ²)) dx
    换元到 [0,1] 上做 Simpson，收敛稳定且完全可复现（无随机性）。
    下界截到 −10（φ(−10)≈7e−24，对双精度无影响）。
    """
    if abs(rho) >= 0.999999:
        raise ValueError("退化相关（|ρ|→1）请走 joint_two_bounds 的特例分支")
    s = math.sqrt(1.0 - rho * rho)
    if a <= -10.0 or b <= -10.0:
        return 0.0
    a = min(a, 10.0)
    b = min(b, 10.0)

    def f(x):
        return _phi(x) * _Phi((b - rho * x) / s)

    lo, hi = -10.0, a
    if hi <= lo:
        return 0.0
    n = panels if panels % 2 == 0 else panels + 1
    h = (hi - lo) / n
    total = f(lo) + f(hi)
    for i in range(1, n):
        total += (4.0 if i % 2 else 2.0) * f(lo + i * h)
    return total * h / 3.0


def _clip(z):
    """把 ±inf 夹到 ±10（正态尾部积分到此已无意义）。"""
    if z is None:
        return 10.0
    if z == float("inf"):
        return 10.0
    if z == float("-inf"):
        return -10.0
    return max(-10.0, min(10.0, z))


def joint_two_bounds(bounds_a, bounds_b, rho):
    """两参数联合合格率的**精确**数值解，吃真实（可非对称）标准化上下限。

    bounds_a/bounds_b: (lo, hi)，标准化单位；单侧规格时另一端传 ±inf
    rho: 相关系数

    用四个角的二元正态 CDF 做容斥：
        P(a_lo≤X≤a_hi, b_lo≤Y≤b_hi)
      = F(a_hi,b_hi) − F(a_lo,b_hi) − F(a_hi,b_lo) + F(a_lo,b_lo)
    """
    a_lo, a_hi = _clip(bounds_a[0]), _clip(bounds_a[1])
    b_lo, b_hi = _clip(bounds_b[0]), _clip(bounds_b[1])
    if a_lo >= a_hi or b_lo >= b_hi:
        return 0.0

    if abs(rho) >= 0.999999:
        # 退化：ρ=+1 → Y=X；ρ=−1 → Y=−X。取交集后就是一维概率（这对**非对称**
        # 规格同样成立，所以不能靠"对称"这个假设蒙对）。
        if rho > 0:
            lo, hi = max(a_lo, b_lo), min(a_hi, b_hi)
        else:
            # Y=−X：b_lo ≤ −X ≤ b_hi  ⇔  −b_hi ≤ X ≤ −b_lo
            lo, hi = max(a_lo, -b_hi), min(a_hi, -b_lo)
        return max(0.0, _Phi(hi) - _Phi(lo))

    v = (_bivar_cdf(a_hi, b_hi, rho) - _bivar_cdf(a_lo, b_hi, rho)
         - _bivar_cdf(a_hi, b_lo, rho) + _bivar_cdf(a_lo, b_lo, rho))
    return max(0.0, min(1.0, v))


def joint_two(pa, pb, rho):
    """两参数联合合格率（**对称双侧**简化版，保留供快速估算与自检）。

    注意：这里把规格当对称双侧处理，只适合"双侧且居中"的情形。
    真实非对称规格请用 joint_two_bounds，joint_conformance 内部走的就是它。
    """
    if not (0.0 < pa <= 1.0) or not (0.0 < pb <= 1.0):
        return None
    za = _norm_ppf(0.5 * (1.0 + pa))
    zb = _norm_ppf(0.5 * (1.0 + pb))
    return joint_two_bounds((-za, za), (-zb, zb), rho)


def joint_conformance(params, corr=None, n_mc=60000, seed=20261030):
    """多参数联合合格率。

    params: [{"name": str, "values": [...], "lsl": x|None, "usl": y|None}, ...]
    corr:   相关系数矩阵（list[list]）；缺失时按独立处理**并告警**

    返回 dict：per_param / naive_product / joint / gap / ci / assumptions / warnings
    """
    out = {"status": ST.INSUFFICIENT_DATA, "k": 0, "per_param": [],
           "naive_product": None, "joint": None, "gap": None,
           "joint_ci": None, "method": None,
           "assumptions": [], "warnings": [],
           "note": "联合合格率在『正态 + 给定相关结构』假设下成立（规格用真实上下限，"
                   "不做对称化假设）；不给『窗口够用』的结论。"}
    ps = [p for p in (params or []) if p and p.get("values")]
    k = len(ps)
    out["k"] = k
    if k == 0:
        out["warnings"].append("没有传入任何参数")
        return out
    if k == 1:
        m = marginal_conformance(ps[0]["values"], ps[0].get("lsl"), ps[0].get("usl"))
        out["per_param"] = [dict(m, name=ps[0].get("name"))]
        out["joint"] = m.get("p_in_spec")
        out["naive_product"] = m.get("p_in_spec")
        out["method"] = "单参数（无需联合）"
        out["status"] = m.get("status")
        return out

    marg, usable = [], []
    for p in ps:
        m = marginal_conformance(p["values"], p.get("lsl"), p.get("usl"))
        marg.append(dict(m, name=p.get("name")))
        if m.get("p_in_spec") is not None and 0.0 < m["p_in_spec"] <= 1.0:
            usable.append((p, m))
    out["per_param"] = marg

    if len(usable) < k:
        out["warnings"].append(
            "有 %d 个参数算不出合格率（缺规格、样本不足或标准差为 0），"
            "联合窗口按剩余 %d 个参数算——**这不是完整窗口**"
            % (k - len(usable), len(usable)))
    if len(usable) < 2:
        out["status"] = ST.INSUFFICIENT_DATA
        return out

    naive = 1.0
    for _, m in usable:
        naive *= m["p_in_spec"]
    out["naive_product"] = naive

    # 相关结构
    R = None
    if corr:
        try:
            R = [[float(x) for x in row] for row in corr]
        except (TypeError, ValueError):
            R = None
    if R is None:
        R = [[1.0 if i == j else 0.0 for j in range(len(usable))]
             for i in range(len(usable))]
        out["assumptions"].append(
            "未提供相关系数矩阵，按**独立**处理。参数实际相关时这个假设会显著偏差"
            "（ρ>0 时低估联合合格率，ρ<0 时高估）")
        worst_n = min(m["n"] for _, m in usable)
        if worst_n < MIN_N_FOR_CORR:
            out["warnings"].append(
                "样本仅 %d < %d：即使给了相关系数也不可靠，建议先补样本" %
                (worst_n, MIN_N_FOR_CORR))
    else:
        if len(R) != len(usable) or any(len(r) != len(usable) for r in R):
            out["warnings"].append("相关矩阵维度与参数个数不匹配，已退回独立假设")
            R = [[1.0 if i == j else 0.0 for j in range(len(usable))]
                 for i in range(len(usable))]

    out["assumptions"].append("规格用参数自身的 μ/σ 化成标准化上下限后参与联合计算，"
                             "**不做对称化假设**；单侧规格按实际情况取 ±inf")

    # k=2 精确积分
    if len(usable) == 2:
        j = joint_two_bounds(usable[0][1]["std_bounds"],
                             usable[1][1]["std_bounds"], R[0][1])
        out["joint"] = j
        out["method"] = "二维数值积分（条件正态 + Simpson，无随机性）"
        if j is not None:
            out["gap"] = j - naive
    else:
        j, ci = _mc_joint(usable, R, n_mc, seed)
        out["joint"] = j
        out["joint_ci"] = ci
        out["method"] = "蒙特卡洛（n=%d，seed=%d）" % (n_mc, seed)
        if j is not None:
            out["gap"] = j - naive
            if ci and (ci[1] - ci[0]) > 0.02:
                out["warnings"].append(
                    "蒙特卡洛误差带宽达 %.3f：结论对样本量敏感，加大 n_mc 再判"
                    % (ci[1] - ci[0]))

    if out["joint"] is not None and out["gap"] is not None:
        if abs(out["gap"]) >= 0.01:
            out["warnings"].append(
                "联合合格率与『各参数相乘』相差 %+.4f：逐参数看 Cpk 会%s真实良率"
                % (out["gap"], "低估" if out["gap"] > 0 else "高估"))
    out["status"] = (ST.SUCCESS if out["joint"] is not None
                     and len(usable) == k else ST.PARTIAL_SUCCESS)
    return out


def _mc_joint(usable, R, n_mc, seed):
    """k>2：用 Cholesky 分解做相关抽样。返回 (点估计, 95% MC 误差带)。"""
    k = len(usable)
    # 真实标准化上下限（来自各自的 μ/σ 与 LSL/USL，非对称也照实）
    bounds = [m["std_bounds"] for _, m in usable]
    # Cholesky
    L = [[0.0] * k for _ in range(k)]
    try:
        for i in range(k):
            for j in range(i + 1):
                s = R[i][j] - sum(L[i][t] * L[j][t] for t in range(j))
                if i == j:
                    if s <= 0:
                        raise ValueError("相关矩阵非正定")
                    L[i][j] = math.sqrt(s)
                else:
                    L[i][j] = s / L[j][j]
    except (ValueError, ZeroDivisionError):
        return None, None
    rng = random.Random(seed)
    hit = 0
    for _ in range(n_mc):
        z = [rng.gauss(0.0, 1.0) for _ in range(k)]
        ok = True
        for i in range(k):
            v = sum(L[i][t] * z[t] for t in range(i + 1))
            lo, hi = bounds[i]
            if v < _clip(lo) or v > _clip(hi):
                ok = False
                break
        if ok:
            hit += 1
    p = hit / float(n_mc)
    se = math.sqrt(max(p * (1.0 - p), 1e-12) / n_mc)
    return p, [max(0.0, p - 1.96 * se), min(1.0, p + 1.96 * se)]


# ------------------------------------------------------------------ 可行域

def rsm_feasible_region(predict, bounds, targets, grid=25):
    """用响应面模型圈出**可行域**（工艺窗口的几何形态）。

    predict: callable(list[float]) -> float，通常来自 doe.fit_rsm 的 _rsm_predict
    bounds:  [[lo, hi], ...] 各因子的工程边界（必须由人给，工具不推断）
    targets: {"lsl": x, "usl": y} 或 {"min": x} / {"max": x}

    返回：网格通过率 + 每个因子在"可行点"上的取值范围（这是窗口的投影）

    注意：网格法是**近似**，网格越粗越容易把细窄窗口整个漏掉；回报 resolution。
    """
    out = {"status": ST.INSUFFICIENT_DATA, "n_factors": 0, "grid": grid,
           "pass_rate": None, "feasible": 0, "total": 0,
           "factor_ranges": [], "resolution": None,
           "warnings": [],
           "note": "网格近似：窗口窄于网格步长时会被整片漏掉，须用加密网格复核。"}
    if not bounds:
        out["warnings"].append("没有给因子边界，无法圈窗口（工具不替你推断工程边界）")
        return out
    k = len(bounds)
    out["n_factors"] = k
    g = max(3, int(grid))
    out["resolution"] = [abs(b[1] - b[0]) / (g - 1) for b in bounds]
    total = g ** k
    if total > 200000:
        out["warnings"].append(
            "网格点数 %d 过大（%d 因子 × %d 档），已降为 3 档/因子" % (total, k, g))
        g = 3
        total = g ** k

    pass_pts = []
    pred_fail = 0
    for idx in range(total):
        t = idx
        pt = []
        for i in range(k):
            step = t % g
            t //= g
            lo, hi = bounds[i]
            pt.append(lo + (hi - lo) * step / (g - 1))
        try:
            y = float(predict(pt))
        except Exception:
            # 单点预测失败不能整片放弃，但**必须计数回告**：
            # 静默跳过会让"网格内没可行点"变成一个可疑的假结论
            # （到底是窗口不存在，还是模型在这些点上算不出来？）。
            pred_fail += 1
            continue
        if _passes(y, targets):
            pass_pts.append(pt)
    out["total"] = total
    out["feasible"] = len(pass_pts)
    if pred_fail:
        out["prediction_failures"] = pred_fail
        out["warnings"].append(
            "有 %d/%d 个网格点预测失败（模型越界/数值问题）："
            "这些点被排除在可行域之外，实际窗口可能比这里报的更大"
            % (pred_fail, total))
    out["pass_rate"] = (len(pass_pts) / total) if total else None
    if k and pass_pts:
        ranges = []
        for i in range(k):
            vals = [p[i] for p in pass_pts]
            ranges.append({"factor_index": i, "lo": min(vals), "hi": max(vals)})
        out["factor_ranges"] = ranges
    if not pass_pts:
        out["warnings"].append(
            "整个网格内没有可行点：要么窗口确实不存在，要么网格太粗漏掉了，"
            "要么目标区间设得不可能满足")
        out["status"] = ST.NO_FINDING
        return out
    out["status"] = ST.SUCCESS
    if out["pass_rate"] is not None and out["pass_rate"] < 0.02:
        out["warnings"].append(
            "可行点仅占 %.1f%%：窗口很窄，量产稳健性存疑，建议加密网格确认边界"
            % (out["pass_rate"] * 100))
    return out


def _passes(y, targets):
    t = targets or {}
    if "lsl" in t and t["lsl"] is not None and y < t["lsl"]:
        return False
    if "usl" in t and t["usl"] is not None and y > t["usl"]:
        return False
    if "min" in t and t["min"] is not None and y < t["min"]:
        return False
    if "max" in t and t["max"] is not None and y > t["max"]:
        return False
    return True


# ------------------------------------------------------------------ 自检

def _selftest():
    """已知真值自检。每条断言都必须能因实现改坏而翻红。"""
    import random as _r
    ok = []

    # 1) 相关性与联合合格率的关系（本模块存在的理由，必须精确对上真值）
    #    分两种规格形态分别验——这两者方向相反，混为一谈就会把结论说反。
    p_ = 0.95
    z95 = _norm_ppf(0.5 * (1.0 + p_))          # ±1.96

    # 1a) 对称双侧：ρ=±1 都给 p（两参数同时进窗口），|ρ| 越大越高
    j_pos = joint_two(p_, p_, 1.0)
    j_neg = joint_two(p_, p_, -1.0)
    ok.append(("对称双侧 ρ=+1 时联合 = 单参数合格率",
               abs(j_pos - p_) < 1e-6, "joint=%.6f" % j_pos))
    ok.append(("对称双侧 ρ=−1 时联合**也**等于 p（不是 2p−1）",
               abs(j_neg - p_) < 1e-6, "joint=%.6f" % j_neg))
    j_ind = joint_two(p_, p_, 0.0)
    ok.append(("对称双侧 ρ=0 时联合 = p² = 0.9025",
               abs(j_ind - p_ * p_) < 5e-4, "joint=%.6f" % j_ind))
    j_a, j_b = joint_two(p_, p_, -0.6), joint_two(p_, p_, 0.6)
    ok.append(("对称双侧下 |ρ| 增大联合升高（ρ=±0.6 对称）",
               j_b > j_ind and abs(j_b - j_a) < 1e-9,
               "|ρ|=0.6→%.4f, ρ=0→%.4f" % (j_b, j_ind)))
    ok.append(("对称双侧下各参数相乘会**低估**联合合格率",
               j_ind < p_, "乘积=%.4f < p=%.2f" % (j_ind, p_)))

    # 1b) 单侧同向（都只给 USL=z95，各自合格率 0.975）
    #     ρ=+1 → p；ρ=−1 → p₁+p₂−1 = 0.95；此时相乘(0.9506)会**高估**
    z_one = _norm_ppf(0.975)                   # 1.96，单侧 p=0.975
    p_one = _Phi(z_one)
    one_pos = joint_two_bounds((float("-inf"), z_one), (float("-inf"), z_one), 1.0)
    one_neg = joint_two_bounds((float("-inf"), z_one), (float("-inf"), z_one), -1.0)
    ok.append(("单侧同向 ρ=+1 时联合 = p（=0.975）",
               abs(one_pos - p_one) < 1e-6, "joint=%.4f" % one_pos))
    ok.append(("单侧同向 ρ=−1 时联合 = 2p−1 = 0.95",
               abs(one_neg - (2 * p_one - 1)) < 1e-6, "joint=%.4f" % one_neg))
    ok.append(("单侧同向且强负相关时相乘会**高估**联合合格率",
               one_neg < p_one * p_one,
               "joint=%.4f vs 乘积=%.4f" % (one_neg, p_one * p_one)))

    # 1c) 非对称双侧：ρ=+1 时联合应等于两区间交集的一维概率（不是 min(p)）
    a = (-1.0, 2.5)      # 偏窄偏正
    b = (0.5, 3.5)       # 偏正
    jj = joint_two_bounds(a, b, 1.0)
    expect_overlap = _Phi(min(a[1], b[1])) - _Phi(max(a[0], b[0]))
    ok.append(("非对称规格 ρ=+1 时联合 = 交集的一维概率",
               abs(jj - expect_overlap) < 1e-9,
               "joint=%.6f expect=%.6f" % (jj, expect_overlap)))

    # 2) 二维积分与解析特例一致：ρ=0 时 P = (Φ(a)−Φ(−a))(Φ(b)−Φ(−b))
    z = 1.5
    expect = (_Phi(z) - _Phi(-z)) ** 2
    got = (_bivar_cdf(z, z, 0.0) - _bivar_cdf(-z, z, 0.0)
           - _bivar_cdf(z, -z, 0.0) + _bivar_cdf(-z, -z, 0.0))
    ok.append(("二维积分在 ρ=0 时等于两个一维概率的乘积",
               abs(got - expect) < 1e-6, "got=%.8f expect=%.8f" % (got, expect)))

    # 3) 单参数合格率：造 N(0,1)，规格 ±1.96 → 合格率 0.95
    rng = _r.Random(7)
    xs = [rng.gauss(0.0, 1.0) for _ in range(4000)]
    m = marginal_conformance(xs, -1.96, 1.96)
    ok.append(("单参数合格率≈0.95（N(0,1) 规格±1.96）",
               abs(m["p_in_spec"] - 0.95) < 0.03, "p=%.4f" % m["p_in_spec"]))
    ok.append(("Cp 闭式解 = 规格宽/(6σ)",
               abs(m["cp"] - (3.92 / (6.0 * m["sigma"]))) < 1e-9,
               "Cp=%.4f" % m["cp"]))
    ok.append(("Cpk ≤ Cp（偏离中心时严格小于）",
               m["cpk"] <= m["cp"] + 1e-9, "Cp=%.3f Cpk=%.3f" % (m["cp"], m["cpk"])))
    #    单侧规格：只给 USL 时 Cp 不该被算出来（双侧才定义）
    m1 = marginal_conformance(xs, None, 1.96)
    ok.append(("只有单侧规格时不给 Cp", m1["cp"] is None and m1["cpk"] is not None,
               "cp=%s cpk=%s" % (m1["cp"], m1["cpk"])))

    # 4) 无规格 → 状态必须是不足，不得给出合格率
    m2 = marginal_conformance(xs, None, None)
    ok.append(("没有规格时不编合格率",
               m2["p_in_spec"] is None and m2["warnings"], "p=%s" % m2["p_in_spec"]))

    # 5) 联合窗口：缺相关矩阵必须告警（默认独立是假设，不是事实）
    p1 = {"name": "CD", "values": xs, "lsl": -1.96, "usl": 1.96}
    p2 = {"name": "膜厚", "values": [x * 0.9 + rng.gauss(0, 0.2) for x in xs],
          "lsl": -2.0, "usl": 2.0}
    jc = joint_conformance([p1, p2])
    ok.append(("缺相关矩阵时按独立处理并告警",
               jc["joint"] is not None and jc["assumptions"], jc["method"]))
    ok.append(("k=2 走精确积分而非蒙特卡洛", "数值积分" in (jc["method"] or ""),
               jc["method"]))
    #    给了强正相关，联合合格率应高于独立乘积
    jc2 = joint_conformance([p1, p2], corr=[[1.0, 0.85], [0.85, 1.0]])
    ok.append(("强正相关时联合合格率高于独立乘积",
               jc2["joint"] > jc2["naive_product"],
               "joint=%.4f 乘积=%.4f" % (jc2["joint"], jc2["naive_product"])))

    # 6) k>2 走蒙特卡洛并给误差带，且同种子可复现
    xs3 = [rng.gauss(0, 1) for _ in range(500)]
    ps3 = [{"name": "a%d" % i, "values": xs3, "lsl": -2.0, "usl": 2.0}
           for i in range(3)]
    j3a = joint_conformance(ps3, n_mc=20000)
    j3b = joint_conformance(ps3, n_mc=20000)
    ok.append(("k>2 走蒙特卡洛且给误差带",
               "蒙特卡洛" in (j3a["method"] or "") and j3a["joint_ci"], j3a["method"]))
    ok.append(("同种子蒙特卡洛可复现", j3a["joint"] == j3b["joint"],
               "%s vs %s" % (j3a["joint"], j3b["joint"])))
    #    独立同分布 3 参数：联合应≈p³
    p_single = j3a["per_param"][0]["p_in_spec"]
    ok.append(("3 个独立参数时联合≈单参数合格率的三次方",
               abs(j3a["joint"] - p_single ** 3) < 0.02,
               "joint=%.4f p³=%.4f" % (j3a["joint"], p_single ** 3)))

    # 7) 可行域：已知抛物线响应面，窗口应落在中心附近
    def _pred(pt):
        return -(pt[0] ** 2) - (pt[1] ** 2) + 4.0     # 顶点 (0,0) 值 4

    reg = rsm_feasible_region(_pred, [[-2.0, 2.0], [-2.0, 2.0]],
                              {"min": 3.0}, grid=21)
    ok.append(("可行域非空且落在原点附近",
               reg["feasible"] > 0 and all(abs(r["lo"]) <= 1.2 and abs(r["hi"]) <= 1.2
                                           for r in reg["factor_ranges"]),
               "feasible=%d/%d" % (reg["feasible"], reg["total"])))
    #    目标不可能满足时：必须报"没有可行点"，不是给个空窗口当成功
    reg0 = rsm_feasible_region(_pred, [[-1, 1], [-1, 1]], {"min": 99.0}, grid=9)
    ok.append(("目标不可能满足时报无可行点",
               reg0["feasible"] == 0 and reg0["status"] == ST.NO_FINDING,
               "status=%s" % reg0["status"]))
    #    没给边界 → 拒绝推断
    reg1 = rsm_feasible_region(_pred, [], {"min": 3.0})
    ok.append(("没有因子边界时拒绝圈窗口",
               reg1["status"] == ST.INSUFFICIENT_DATA and reg1["warnings"], ""))
    #    窗口覆盖率必须是真的比例
    ok.append(("通过率 = 可行点数/总点数",
               abs(reg["pass_rate"] - reg["feasible"] / reg["total"]) < 1e-12, ""))
    return ok
