# -*- coding: utf-8 -*-
"""
reliability.py —— 寿命数据分析（Weibull / MTBF / 加速寿命 / 可用度）

=============================================================================
为什么必须有这一层（不是"锦上添花"）
=============================================================================
    工艺/质量分析回答的是"这批为什么不良"；可靠性回答的是**"能撑多久"**。
    两者不能互相替代：一台组件出厂全项合格，第 7 年开始批量功率衰减，
    用 SPC 只能看到"这一段数据有漂移"，看不到"寿命分布的形状变了"。

    招聘 JD 里这块是硬指标，且几乎每家电芯/PCS/组件厂都写：
      · 中芯国际 良率提升工程师：Process 周期性检查、制程风险**预判**
      · 阳光电源/隆基 质量岗：产品**全生命周期**质量管理、可靠性验证
      · 储能/电芯 可靠性工程师：**Weibull 分析、MTBF/MTTF、加速寿命（ALT/HALT/HASS）、
        RBD、FTA**、寿命建模
    此前本工具在此处是**空白**：算得了 CPK，答不了 B10 寿命。故新增本模块。

=============================================================================
算法口径（公开来源，不是本项目发明）
=============================================================================
    · Weibull 两参数极大似然：形状 β、尺度 η。含**右删失**（未失效/中途撤出）时的
      似然方程按 Nelson《Applied Life Data Analysis》与 Meeker & Escobar
      《Statistical Methods for Reliability Data》的公开形式实现：
          ∂lnL/∂β = r/β + Σ_失效 ln t_i − r·(Σ_全部 t^β·ln t)/(Σ_全部 t^β) = 0
    · 置信区间用**剖面似然（profile likelihood）**：2·(lnL_max − lnL(β)) = χ²₀.₉₅,df=1，
      比正态近似更适用于小样本、含删失的场景；χ² 分位由 df=1 的公开恒等式换算，
      不查表、不硬编码。
    · B10 = η·(−ln0.9)^(1/β)；MTBF = η·Γ(1+1/β)（Weibull 期望，非 η 本身——
      把 η 当 MTBF 报出去是行业里最常见的口径错误之一）。
    · 加速模型：Arrhenius（温度，ln life ∝ 1/T）、逆幂律（电压/功率/载荷，
      ln life ∝ ln S）、Coffin-Manson（温循 ΔT，ln life ∝ ln ΔT）。
      三者形式见 IEC 62506 / IEC 61709 所引用的经典工程模型。
    · 可用度 A = MTBF/(MTBF+MTTR)，MTTR 为可修产品的平均修复时间。

=============================================================================
边界（比代码重要，改代码前先读）
=============================================================================
    1. **不给"寿命 = X 年"的单点结论。** 只给区间 + 数据充分性判定。
       JD 要的是"能解释寿命结论的人"，不是"敢编一个数的人"。
    2. **失效数不足就拒答 B10。** 默认 min_failures=5（公开工程实践下限，
       调用方可覆盖）。少于这个数时仍给点估计供参考，但 status 必须是
       INSUFFICIENT_DATA 且 b10_usable=False——不能让人拿 2 个失效点去写质保承诺。
    3. **加速模型不替用户选机理。** 只按调用方指定的模型拟合，并回告该模型
       适用于哪类应力；拿 Arrhenius 拟湿度数据会得到漂亮的 R² 和错误的结论，
       所以模型名必须由人指定，而不是我猜。
    4. 删失比例过高（>50%）时给警告：此时 B10 由极少数失效点决定，外推很危险。
    5. 本模块只做**统计推断**，不做失效物理判定（金相/SEM/CT 的结论得有人给）。

零第三方依赖：stdlib only（math / random 仅用于自检）。
"""

import math

try:
    from . import status as ST          # 包模式
except Exception:                       # 脚本模式（python lib/reliability.py）
    import status as ST                 # noqa

# 玻尔兹曼常数（eV/K），Arrhenius 换算用。物理常数，不是产品参数。
K_BOLTZMANN_EV = 8.617333262e-5

# 默认最少失效数：低于此数不宣称 B10。公开工程实践下限，可被调用方覆盖。
DEFAULT_MIN_FAILURES = 5

ALT_MODELS = ("arrhenius", "inverse_power", "coffin_manson")

_MODEL_DESC = {
    "arrhenius": "Arrhenius（温度应力，寿命 ∝ exp(Ea/kT)）；适用：热老化、"
                 "电解液分解、绝缘热劣化。**不适用于湿度/电压**",
    "inverse_power": "逆幂律（电压/功率/载荷应力，寿命 ∝ S^-n）；适用：电应力、"
                     "功率循环、机械载荷。**不适用于温度**",
    "coffin_manson": "Coffin-Manson（温循 ΔT，寿命 ∝ ΔT^-m）；适用：热疲劳、"
                     "焊点/焊带疲劳、功率模块温循。应力列填 ΔT（不是绝对温度）",
}

_BETA_HINT = (
    (0.0, 1.0, "β<1：早期失效（浴盆曲线左段）。方向是**筛查/剔除**"
               "（来料、工艺引入缺陷），不是延长寿命；做预防性更换无效。"),
    (0.9, 1.1, "β≈1：随机失效（指数段）。失效与已运行时间无关，"
               "预防性更换**无效**，应做冗余设计、备件与快速修复。"),
    (1.1, 4.0, "β>1：耗损失效（浴盆曲线右段）。可据此制定预防性更换/检修周期，"
               "按 B10 或目标可靠度定更换点。"),
    (4.0, float("inf"), "β≫1：强耗损（磨损/疲劳类），寿命分布很窄，"
                        "集中在 η 附近失效，适合定期更换且窗口可算得很准。"),
)


# ------------------------------------------------------------------ 基础工具

def _norm_ppf(p):
    """标准正态分位（二分求 erf 反函数）。避免引入 scipy。"""
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


def _chi2_q_df1(q):
    """df=1 的卡方分位。恒等式：χ²_q,df=1 = (z_{(1+q)/2})²（公开恒等式）。"""
    z = _norm_ppf(0.5 * (1.0 + q))
    return z * z


def _clean(times, censored):
    """清洗寿命数据：丢掉非正/非数值，返回 (t, c, 丢弃数)。

    寿命 <=0 不是"很小"，是**录入错误**（换行串列、日期被当成时长）。
    静默当 0 处理会让 β 被拉到极端值，所以这里必须计数并回告。
    """
    if censored is None:
        censored = [False] * len(times)
    if len(censored) != len(times):
        raise ValueError("times 与 censored 长度不一致（%d vs %d）"
                         % (len(times), len(censored)))
    t, c, dropped = [], [], 0
    for i, v in enumerate(times):
        try:
            f = float(v)
        except (TypeError, ValueError):
            dropped += 1
            continue
        if not (f > 0) or math.isinf(f) or math.isnan(f):
            dropped += 1
            continue
        t.append(f)
        c.append(bool(censored[i]))
    return t, c, dropped


def _beta_g(beta, u, r, s1_fail):
    """MLE 方程 g(β) = r/β + Σ_失效 ln u − r·(Σ u^β ln u)/(Σ u^β)。

    u 已按最大观测值归一化到 (0,1]，避免 t^β 溢出（β 大时原值会炸成 inf）。
    g 在 (0,∞) 上单调递减且两端异号，故可二分求根。
    """
    s = 0.0
    sp = 0.0
    for ui in u:
        p = ui ** beta
        s += p
        sp += p * math.log(ui)
    if s <= 0:
        return float("nan")
    return r / beta + s1_fail - r * sp / s


def _solve_beta(u, r, s1_fail):
    """二分求 β。返回 (β, 收敛与否)。

    用显式传参而非模块级游标：后者在多线程/并发调用下会互相踩，
    而且这种"隐藏状态"是典型的不易发现的缺陷来源。
    """
    lo, hi = 1e-4, 1e4
    g_lo = _beta_g(lo, u, r, s1_fail)
    g_hi = _beta_g(hi, u, r, s1_fail)
    # 两端同号说明数据退化（全部同时刻失效等），退回 β=1 并标记未收敛
    if not (g_lo > 0 > g_hi):
        return 1.0, False
    mid = 1.0
    for _ in range(300):
        mid = math.sqrt(lo * hi)          # β 取对数尺度二分，收敛更快
        g = _beta_g(mid, u, r, s1_fail)
        if g > 0:
            lo = mid
        else:
            hi = mid
        if hi / lo < 1.0 + 1e-12:
            break
    return mid, True


def _loglik(beta, u, r, s1_fail):
    """Weibull 对数似然（归一化尺度下）。

    lnL = r·lnβ − rβ·ln η_u + (β−1)·Σ_失效 ln u_i − r
    （最后一项来自 Σ(u/η)^β = r 这一恒等式）
    """
    s = 0.0
    for ui in u:
        s += ui ** beta
    if s <= 0 or r <= 0 or beta <= 0:
        return float("-inf")
    eta_u = (s / r) ** (1.0 / beta)
    return (r * math.log(beta) - r * beta * math.log(eta_u)
            + (beta - 1.0) * s1_fail - r)


def _eta_from_beta(beta, u, r, scale):
    s = 0.0
    for ui in u:
        s += ui ** beta
    if s <= 0 or r <= 0 or beta <= 0:
        return float("nan")
    return ((s / r) ** (1.0 / beta)) * scale


def _profile_ci(u, r, s1_fail, scale, beta_hat, ll_max, conf):
    """剖面似然区间：解 2(lnL_max − lnL(β)) = χ²_q,df=1 的两侧根。"""
    target = ll_max - 0.5 * _chi2_q_df1(conf)

    def bisect(a, b):
        """在 [a,b] 内找 lnL(β)=target 的点；a 靠近 β_hat，b 向外。"""
        fa = _loglik(a, u, r, s1_fail) - target
        fb = _loglik(b, u, r, s1_fail) - target
        if fa * fb > 0:
            return None
        for _ in range(200):
            mid = math.sqrt(a * b)
            fm = _loglik(mid, u, r, s1_fail) - target
            if fa * fm <= 0:
                b, fb = mid, fm
            else:
                a, fa = mid, fm
            if b / a < 1.0 + 1e-10:
                break
        return math.sqrt(a * b)

    # 向下探
    lo_b = beta_hat
    step = 1.6
    while lo_b > 1e-6:
        cand = beta_hat / step
        if cand <= 1e-6:
            cand = 1e-6
        if _loglik(cand, u, r, s1_fail) <= target:
            lo_b = cand
            break
        lo_b = cand
        step *= 1.6
    beta_lo = bisect(lo_b, beta_hat)

    # 向上探
    hi_b = beta_hat
    step = 1.6
    while hi_b < 1e6:
        cand = beta_hat * step
        if _loglik(cand, u, r, s1_fail) <= target:
            hi_b = cand
            break
        hi_b = cand
        step *= 1.6
    beta_hi = bisect(beta_hat, hi_b)

    lo = beta_lo if beta_lo else beta_hat
    hi = beta_hi if beta_hi else beta_hat
    return [lo, hi]


def _beta_text(beta):
    for lo, hi, txt in _BETA_HINT:
        if lo <= beta < hi:
            return txt
    return "β 解读不适用（值异常，先查数据单位是否混用）"


# ------------------------------------------------------------------ 主入口

def weibull_fit(times, censored=None, conf=0.95,
                min_failures=DEFAULT_MIN_FAILURES):
    """两参数 Weibull 拟合（含右删失）。

    参数
        times      : 寿命/失效时间序列（小时、循环数、次数均可，单位由调用方统一）
        censored   : 同长度布尔序列，True 表示"到观察期结束仍未失效"（右删失）
        conf       : 置信水平，默认 0.95
        min_failures: 宣称 B10 所需的最少失效数，默认 5

    返回 dict；**任何字段都不构成寿命承诺**，见模块头"边界"。
    """
    t, c, dropped = _clean(times, censored)
    n = len(t)
    n_fail = sum(1 for x in c if not x)
    n_cens = n - n_fail
    warnings = []
    if dropped:
        warnings.append("丢弃 %d 条非法寿命值（≤0 或非数值）——先查单位/串列，"
                        "这类值不是'很小'而是录入错误" % dropped)

    out = {
        "status": ST.INSUFFICIENT_DATA,
        "n": n, "n_fail": n_fail, "n_censored": n_cens,
        "censoring_pct": (100.0 * n_cens / n) if n else None,
        "beta": None, "eta": None,
        "beta_ci": None, "eta_ci": None,
        "mtbf": None, "mtbf_ci": None,
        "b10": None, "b10_ci": None, "b10_usable": False,
        "beta_interpret": None,
        "goodness_r2": None,
        "warnings": warnings,
        "conf": conf,
        "note": ("统计推断结果，不含失效物理判定；寿命承诺须结合加速试验设计与"
                 "现场数据复核。"),
    }

    if n == 0:
        warnings.append("没有可用寿命数据（全被清洗掉）")
        return out
    if n_fail == 0:
        out["status"] = ST.INSUFFICIENT_DATA
        warnings.append("全部为删失（无一条失效）：Weibull 无法识别 β，"
                        "只能给下限估计。补观察期或提高应力。")
        return out

    scale = max(t)
    u = [x / scale for x in t]
    s1_fail = sum(math.log(u[i]) for i in range(n) if not c[i])
    r = n_fail

    beta, ok = _solve_beta(u, r, s1_fail)
    if not ok:
        warnings.append("MLE 未收敛（数据退化：多为多条同时刻失效或样本量极小）；"
                        "β 退化为 1.0，结论不可用于决策。")
    eta = _eta_from_beta(beta, u, r, scale)
    ll_max = _loglik(beta, u, r, s1_fail)

    out["beta"] = beta
    out["eta"] = eta
    out["beta_interpret"] = _beta_text(beta)
    out["goodness_r2"] = _weibull_plot_r2(t, c, beta, eta)

    try:
        b_lo, b_hi = _profile_ci(u, r, s1_fail, scale, beta, ll_max, conf)
        out["beta_ci"] = [b_lo, b_hi]
        e_lo = _eta_from_beta(b_lo, u, r, scale)
        e_hi = _eta_from_beta(b_hi, u, r, scale)
        if not (math.isnan(e_lo) or math.isnan(e_hi)):
            out["eta_ci"] = [min(e_lo, e_hi), max(e_lo, e_hi)]
    except (ValueError, OverflowError, ZeroDivisionError):
        warnings.append("置信区间计算失败（小样本或删失过重），只给点估计")

    if beta and eta and not math.isnan(eta):
        out["mtbf"] = eta * math.gamma(1.0 + 1.0 / beta)
        out["b10"] = eta * (-math.log(0.9)) ** (1.0 / beta)
    # 区间：由 β 的剖面似然区间**逐端点重算** η/B10/MTBF 再排序。
    # 不能假设"eta_ci[0] 配 beta_hi"——η 随 β 单调下降这一关系在小样本/重删失下
    # 并不总成立（自检实测踩过：B10 区间上下界颠倒成 [492, 436]）。
    # 排序后取 min/max 是唯一与单调性假设无关的写法。
    b10_ci_raw = []
    mtbf_ci_raw = []
    for b in (out["beta_ci"] or []):
        e = _eta_from_beta(b, u, r, scale)
        if math.isnan(e):
            continue
        b10_ci_raw.append(e * (-math.log(0.9)) ** (1.0 / b))
        mtbf_ci_raw.append(e * math.gamma(1.0 + 1.0 / b))
    if out["b10"] is not None and len(b10_ci_raw) == 2:
        out["b10_ci"] = [min(b10_ci_raw), max(b10_ci_raw)]
    if out["mtbf"] is not None and len(mtbf_ci_raw) == 2:
        out["mtbf_ci"] = [min(mtbf_ci_raw), max(mtbf_ci_raw)]

    # 数据充分性判定
    if n_fail < min_failures:
        warnings.append("失效数 %d < 下限 %d：B10 不得用于质保承诺/客户承诺，"
                        "只作趋势参考。补样本或延长观察期。"
                        % (n_fail, min_failures))
    elif out["censoring_pct"] is not None and out["censoring_pct"] > 50.0:
        warnings.append("删失比例 %.0f%%（>50%%）：B10 由少数失效点决定，"
                        "外推到长寿命端风险高" % out["censoring_pct"])
    else:
        out["b10_usable"] = True

    if ok and out["b10_usable"]:
        out["status"] = ST.SUCCESS
    elif ok:
        out["status"] = ST.INSUFFICIENT_DATA
    else:
        out["status"] = ST.FAILED
    return out


def _weibull_plot_r2(t, c, beta, eta):
    """Weibull 概率图上的 R²（中位秩 Bernard 近似）。

    注意：R² 高**不代表 Weibull 合适**（小样本时会虚高），只作为"明显不吻合"的
    快速体检指标。真要判拟合优劣需做 Anderson-Darling（暂未实现，见模块尾注）。
    """
    idx = sorted(range(len(t)), key=lambda i: t[i])
    n = len(t)
    if n < 3 or not beta or not eta:
        return None
    xs, ys = [], []
    for rank, i in enumerate(idx, start=1):
        # Bernard 中位秩：F = (i-0.3)/(n+0.4)，对删失较稳健
        f = (rank - 0.3) / (n + 0.4)
        if not (0 < f < 1):
            continue
        xs.append(math.log(t[i]))
        ys.append(math.log(-math.log(1.0 - f)))
    if len(xs) < 3:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    sxy = sum((xs[k] - mx) * (ys[k] - my) for k in range(len(xs)))
    sxx = sum((xs[k] - mx) ** 2 for k in range(len(xs)))
    syy = sum((ys[k] - my) ** 2 for k in range(len(ys)))
    if sxx <= 0 or syy <= 0:
        return None
    return (sxy * sxy) / (sxx * syy)


def reliability_at(fit, t):
    """给定拟合结果，返回时刻 t 的可靠度 R(t) 与累积失效概率 F(t)。"""
    beta = (fit or {}).get("beta")
    eta = (fit or {}).get("eta")
    if not beta or not eta or t is None or t <= 0:
        return {"t": t, "R": None, "F": None,
                "reason": "拟合无效或 t<=0"}
    f = 1.0 - math.exp(-((float(t) / eta) ** beta))
    return {"t": t, "R": 1.0 - f, "F": f, "beta": beta, "eta": eta}


# ------------------------------------------------------------------ 加速寿命

def alt_fit(levels, model="arrhenius", use_stress=None):
    """加速寿命模型拟合（两水平以上）。

    参数
        levels     : [(应力, 该应力下的特征寿命 η)]。应力单位：
                     arrhenius → 绝对温度 K（**不是 ℃**，传 ℃ 会算出负 Ea）
                     inverse_power → 电压/功率/载荷（任意一致单位）
                     coffin_manson → ΔT（温循幅度）
        model      : ALT_MODELS 之一，**必须由调用方指定**（不替人猜机理）
        use_stress : 使用应力（与服务寿命预测同单位）；给了才算外推寿命与 AF

    返回 dict：模型参数、R²、外推寿命（若给 use_stress）、加速因子。
    """
    out = {
        "status": ST.INSUFFICIENT_DATA,
        "model": model,
        "model_desc": _MODEL_DESC.get(model),
        "params": None, "r2": None,
        "levels": len(levels or []),
        "life_at_use": None, "acceleration_factor": None,
        "warnings": [],
        "note": "外推寿命依赖'失效机理在应力区间内不变'这一前提；机理变了结论作废。",
    }
    if model not in ALT_MODELS:
        out["status"] = ST.NOT_APPLICABLE
        out["warnings"].append("未知模型 %r；可选：%s" % (model, ", ".join(ALT_MODELS)))
        return out

    pts = []
    dropped = 0
    for s, life in (levels or []):
        try:
            sv, lv = float(s), float(life)
        except (TypeError, ValueError):
            dropped += 1
            continue
        if sv <= 0 or lv <= 0:
            dropped += 1
            continue
        pts.append((sv, lv))
    if dropped:
        out["warnings"].append("丢弃 %d 个应力水平（非数值或 ≤0）——"
                               "应力为 0 会让 1/T 或 ln S 发散，不是'应力很小'" % dropped)
    if len(pts) < 2:
        out["warnings"].append("有效应力水平 < 2 个：加速模型无法识别，"
                               "至少要两个不同应力下的特征寿命")
        return out

    # 线性化：arrhenius 用 1/T；另两个用 ln S
    xs = [(1.0 / s) if model == "arrhenius" else math.log(s) for s, _ in pts]
    ys = [math.log(l) for _, l in pts]
    if len(set(round(x, 12) for x in xs)) < 2:
        out["warnings"].append("两个应力水平换算后相同（或温度填了℃导致同值）："
                               "无法识别斜率")
        return out
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    sxy = sum((xs[k] - mx) * (ys[k] - my) for k in range(len(xs)))
    sxx = sum((xs[k] - mx) ** 2 for k in range(len(xs)))
    if sxx <= 0:
        out["warnings"].append("应力全相同，无法拟合")
        return out
    slope = sxy / sxx
    intercept = my - slope * mx

    syy = sum((ys[k] - my) ** 2 for k in range(len(ys)))
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else None
    out["r2"] = r2

    if model == "arrhenius":
        ea = slope * K_BOLTZMANN_EV
        out["params"] = {"Ea_eV": ea, "ln_A": intercept,
                         "interpret": "活化能 Ea=%.3f eV（公开口径：常见电子器件 "
                                      "0.3–0.7 eV、电解液分解 0.5–0.9 eV 量级，"
                                      "仅作合理性对照，不作判定依据）" % ea}
        if ea < 0:
            out["warnings"].append("Ea 为负：温度应力填成了℃而非 K，或寿命随温度"
                                   "升高而变长（违背机理）——先查单位")
    elif model == "inverse_power":
        out["params"] = {"n": -slope, "ln_A": intercept,
                         "interpret": "逆幂指数 n=%.3f" % (-slope)}
    else:
        out["params"] = {"m": -slope, "ln_A": intercept,
                         "interpret": "Coffin-Manson 指数 m=%.3f" % (-slope)}

    if use_stress is not None:
        try:
            us = float(use_stress)
        except (TypeError, ValueError):
            us = None
        if us and us > 0:
            xu = (1.0 / us) if model == "arrhenius" else math.log(us)
            life_use = math.exp(intercept + slope * xu)
            out["life_at_use"] = life_use
            out["use_stress"] = us
            # AF = 使用应力下寿命 / 试验应力下寿命（试验应力取最大者，即最温和的一档）
            s_test = max(pts, key=lambda p: p[1])[0]
            xt = (1.0 / s_test) if model == "arrhenius" else math.log(s_test)
            life_test = math.exp(intercept + slope * xt)
            out["acceleration_factor"] = (life_use / life_test) if life_test > 0 else None
            out["test_stress"] = s_test
        else:
            out["warnings"].append("use_stress 非法（须 >0）")

    out["status"] = ST.SUCCESS if len(pts) >= 3 or (r2 is not None and r2 > 0.9) \
        else ST.PARTIAL_SUCCESS
    if len(pts) == 2:
        out["warnings"].append("只有两个应力水平：直线必然过两点，R² 恒为 1，"
                               "不能作为模型可信度证据。至少三点才能验证线性。")
    return out


# ------------------------------------------------------------------ 可修系统

def mttr(repair_times):
    """平均修复时间 MTTR。返回 dict（含中位数——修复时间通常右偏，
    均值会被少数长停机拉高，两个都要看）。"""
    vals = []
    dropped = 0
    for v in (repair_times or []):
        try:
            f = float(v)
        except (TypeError, ValueError):
            dropped += 1
            continue
        if f < 0 or math.isnan(f) or math.isinf(f):
            dropped += 1
            continue
        vals.append(f)
    if not vals:
        return {"status": ST.INSUFFICIENT_DATA, "mttr": None, "median": None,
                "n": 0, "dropped": dropped,
                "warnings": ["没有可用修复时间数据"]}
    vals.sort()
    n = len(vals)
    mean = sum(vals) / n
    med = vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])
    warn = []
    if med > 0 and mean > 2 * med:
        warn.append("修复时间右偏（均值 %.1f 远高于中位 %.1f）：报均值会掩盖"
                    "多数快速修复，考核口径须先明确" % (mean, med))
    return {"status": ST.SUCCESS, "mttr": mean, "median": med,
            "n": n, "dropped": dropped, "max": vals[-1], "warnings": warn}


def availability(mtbf, mttr_val):
    """固有可用度 A = MTBF/(MTBF+MTTR)。"""
    try:
        m1, m2 = float(mtbf), float(mttr_val)
    except (TypeError, ValueError):
        return {"availability": None, "reason": "输入非数值"}
    if m1 <= 0 or m2 < 0:
        return {"availability": None, "reason": "MTBF 须 >0，MTTR 须 ≥0"}
    return {"availability": m1 / (m1 + m2), "mtbf": m1, "mttr": m2,
            "downtime_pct": 100.0 * m2 / (m1 + m2)}


def rbd_reliability(kind, reliabilities):
    """可靠性框图（RBD）的串/并联可靠度。

    kind: "series"（串联，全好才好）| "parallel"（并联冗余，全坏才坏）
    这是 RBD 的最小子集：复杂网络需最小路集/割集分解，见模块尾注。
    """
    rs = []
    dropped = 0
    for v in (reliabilities or []):
        try:
            f = float(v)
        except (TypeError, ValueError):
            dropped += 1
            continue
        if 0.0 <= f <= 1.0:
            rs.append(f)
        else:
            dropped += 1
    if not rs:
        return {"status": ST.INSUFFICIENT_DATA, "R": None,
                "warnings": ["没有有效的可靠度输入（须 0..1）；丢弃 %d 个非法值"
                             % dropped]}
    if kind == "series":
        r = 1.0
        for x in rs:
            r *= x
        return {"status": ST.SUCCESS, "kind": "series", "R": r, "n": len(rs),
                "note": "串联系统可靠度恒低于任一单元——多一个环节就多一处失效点"}
    if kind == "parallel":
        q = 1.0
        for x in rs:
            q *= (1.0 - x)
        return {"status": ST.SUCCESS, "kind": "parallel", "R": 1.0 - q, "n": len(rs),
                "note": "并联冗余按独立失效计算；共因失效（同一电源/同一批次）"
                        "会让实际可靠度显著低于此值"}
    return {"status": ST.NOT_APPLICABLE, "R": None,
            "warnings": ["未知框图类型 %r（series/parallel）" % kind]}


# ------------------------------------------------------------------ 组间对比

def group_life_table(groups):
    """按应力/批次分组，各自拟合 Weibull，返回特征寿命表（供 ALT 使用）。

    groups: {组名: (times, censored)} 或 {组名: times}
    组内失效数不足时该组标 INSUFFICIENT_DATA，不参与 ALT —— 宁可少一组，
    也不拿 1 个失效点的 η 去拟加速模型。
    """
    rows, usable, non_numeric = [], [], []
    for name in sorted(groups):
        g = groups[name]
        if isinstance(g, (list, tuple)) and len(g) == 2 and isinstance(g[1], (list, tuple)):
            times, cens = g[0], g[1]
        else:
            times, cens = g, None
        fit = weibull_fit(times, cens)
        rows.append({"group": name, "status": fit["status"],
                     "n": fit["n"], "n_fail": fit["n_fail"],
                     "beta": fit["beta"], "eta": fit["eta"],
                     "b10": fit["b10"], "mtbf": fit["mtbf"]})
        # ALT 用 η（Weibull 尺度参数），要求该组拟合有效
        if fit["eta"] and fit["beta"] and fit["status"] in (ST.SUCCESS,):
            try:
                usable.append((float(name), fit["eta"]))
            except (TypeError, ValueError):
                # 组名不是数值（如"高温/常温"）→ 无法进加速模型，必须点名回告，
                # 不能悄悄少一组：少一组会让 ALT 只剩两水平而 R² 假性为 1
                non_numeric.append(name)
    out = {"status": ST.SUCCESS if rows else ST.INSUFFICIENT_DATA,
           "rows": rows, "alt_points": usable}
    if non_numeric:
        out["warnings"] = [
            "这些组名不是数值，未参与加速模型（改用数值应力列，如绝对温度 K）：%s"
            % "、".join(str(x) for x in non_numeric)]
    return out


# ------------------------------------------------------------------ 自检

def _selftest():
    """已知真值自检：造 β/η 已知的 Weibull 样本，看能否回收。

    **造数据必须先绑定 Random 再循环**——写成
    [random.Random(seed).gauss(...) for _ in range(n)] 会返回 n 个相同值（σ=0），
    据此会得出"拟合完美"的假结论（本项目已踩过）。
    """
    import random
    rng = random.Random(20261030)
    beta_true, eta_true, n = 2.4, 1200.0, 400
    xs = []
    for _ in range(n):
        uu = 1.0 - rng.random()
        xs.append(eta_true * (-math.log(uu)) ** (1.0 / beta_true))
    fit = weibull_fit(xs)
    ok = []
    ok.append(("β 回收误差 < 12%", abs(fit["beta"] - beta_true) / beta_true < 0.12,
               "β=%.3f (真值 %.2f)" % (fit["beta"], beta_true)))
    ok.append(("η 回收误差 < 12%", abs(fit["eta"] - eta_true) / eta_true < 0.12,
               "η=%.1f (真值 %.0f)" % (fit["eta"], eta_true)))
    ok.append(("MTBF 用 Γ 修正而非直接取 η",
               abs(fit["mtbf"] - fit["eta"]) > 1e-6,
               "MTBF=%.1f vs η=%.1f" % (fit["mtbf"], fit["eta"])))
    ok.append(("B10 小于 η", fit["b10"] < fit["eta"],
               "B10=%.1f" % fit["b10"]))
    ok.append(("样本充足时判定可用", fit["b10_usable"] and fit["status"] == ST.SUCCESS,
               "status=%s" % fit["status"]))
    ok.append(("β 区间覆盖真值",
               fit["beta_ci"] and fit["beta_ci"][0] <= beta_true <= fit["beta_ci"][1],
               "CI=%s" % (fit["beta_ci"],)))

    # 右删失：一半样本人为设成未失效，β 应仍可估（可能偏高/偏低但须收敛）
    cens = [i % 2 == 0 for i in range(n)]
    fit_c = weibull_fit(xs, cens)
    ok.append(("含删失仍收敛", fit_c["beta"] and 0.5 < fit_c["beta"] < 8.0,
               "β=%.3f" % (fit_c["beta"] or -1)))

    # 失效数不足必须拒答
    few = weibull_fit(xs[:3])
    ok.append(("失效数不足拒答 B10",
               few["status"] == ST.INSUFFICIENT_DATA and not few["b10_usable"],
               "status=%s usable=%s" % (few["status"], few["b10_usable"])))

    # 全删失必须报不足
    allc = weibull_fit(xs[:10], [True] * 10)
    ok.append(("全删失不硬凑 β", allc["beta"] is None
               and allc["status"] == ST.INSUFFICIENT_DATA, "beta=%s" % allc["beta"]))

    # 非法值清洗
    dirty = weibull_fit([0, -1, "abc"] + xs[:60])
    ok.append(("非法寿命值被清洗并计数", dirty["n"] == 60 and dirty["warnings"],
               "n=%d" % dirty["n"]))

    # Arrhenius：造 Ea 已知的数据，看回收。
    # 口径：life ∝ exp(Ea/(k·T))，故 life(T)=life(T_use)·exp(Ea/k·(1/T − 1/T_use))。
    # 注意符号：T 升高 → 1/T 减小 → 指数为负 → 寿命变短。
    # 自检首版这里写反了（写成 1/T_use − 1/T），结果 Ea 回收成 −0.55 eV，
    # 正好被"Ea 为负就告警"的功能抓出来——造数据的公式本身也要验。
    ea_true = 0.55
    k = K_BOLTZMANN_EV
    t_use = 273.15 + 25.0
    lv = []
    for tc in (85.0, 105.0, 125.0):
        tk = tc + 273.15
        life = 2000.0 * math.exp(ea_true / k * (1.0 / tk - 1.0 / t_use))
        lv.append((tk, life))
    alt = alt_fit(lv, "arrhenius", use_stress=t_use)
    ok.append(("Arrhenius Ea 回收误差 < 2%",
               abs(alt["params"]["Ea_eV"] - ea_true) / ea_true < 0.02,
               "Ea=%.4f eV" % alt["params"]["Ea_eV"]))
    ok.append(("给定使用应力给出外推寿命",
               alt["life_at_use"] and alt["life_at_use"] > 0,
               "life=%.0f" % (alt["life_at_use"] or 0)))
    ok.append(("AF 大于 1（加速试验确实加速）", alt["acceleration_factor"] > 1,
               "AF=%.2f" % (alt["acceleration_factor"] or 0)))

    # 温度误填 ℃ 必须给出负 Ea 警告
    bad = alt_fit([(85.0, 100.0), (125.0, 300.0)], "arrhenius")
    ok.append(("温度填 ℃ 会被识别为负 Ea 并警告",
               bad["params"]["Ea_eV"] < 0 and bad["warnings"], "warnings=%d"
               % len(bad["warnings"])))

    # 两水平 R² 恒为 1 必须警示
    two = alt_fit([(300.0, 100.0), (350.0, 50.0)], "arrhenius")
    ok.append(("两水平时警示 R² 不可作证据",
               any("两个应力水平" in w for w in two["warnings"]), "")) 

    # MTTR / 可用度 / RBD
    mt = mttr([2, 4, 2, 30, 3])
    ok.append(("MTTR 中位数抗长停机拉高", mt["median"] == 3 and mt["mttr"] > 5,
               "mean=%.1f median=%.1f" % (mt["mttr"], mt["median"])))
    av = availability(1000.0, 10.0)
    ok.append(("可用度 = 1000/1010", abs(av["availability"] - 1000.0 / 1010.0) < 1e-12,
               "A=%.5f" % av["availability"]))
    ok.append(("串联可靠度相乘", abs(rbd_reliability("series", [0.9, 0.9])["R"] - 0.81) < 1e-12, ""))
    ok.append(("并联可靠度 1-∏(1-R)",
               abs(rbd_reliability("parallel", [0.9, 0.9])["R"] - 0.99) < 1e-12, ""))
    ok.append(("R 随 t 单调下降",
               reliability_at(fit, 100)["R"] > reliability_at(fit, 5000)["R"], ""))

    # 区间覆盖：B10 区间应包住点估计
    ok.append(("B10 区间覆盖点估计",
               fit["b10_ci"] and fit["b10_ci"][0] <= fit["b10"] <= fit["b10_ci"][1],
               "%s" % (fit["b10_ci"],)))
    return ok


# 尾注（未实现，登记为已知边界，避免被误当成"能力上限"）：
#   1. Anderson-Darling 拟合优度检验（判断 Weibull 是否适用）——现只有概率图 R²，
#      小样本会虚高，不能作为"分布选对了"的证据。
#   2. 三参数 Weibull（位置参数 γ，用于有"安全寿命期"的场景）。
#   3. 复杂 RBD 的最小路集/割集分解，以及 FTA 最小割集（MOCUS）。
#   4. 竞争失效（多失效模式并存）与混合 Weibull。
# 以上均为公开方法，缺的是实现工作量与真值校验样本，不是原理不清；
# 未实现时宁可报边界，也不给看起来专业的假结论。
