# -*- coding: utf-8 -*-
"""
spc.py —— 统计过程控制（SPC）

控制图：Xbar-R / Xbar-S / X-MR(单值移动极差) / p / np / c / u
判异规则：Nelson 1-8（含 Western Electric 4 条）
过程能力：Cp / Cpk / Pp / Ppk / Cpm / 西格玛水平 / 预期 ppm

控制限常数 d2、d3 用标准表（n=2..25），c4 用 Gamma 函数精确计算以避免查表误差。
"""

import math
import re
from . import stats as S

D2 = {2: 1.128, 3: 1.693, 4: 2.059, 5: 2.326, 6: 2.534, 7: 2.704, 8: 2.847,
      9: 2.970, 10: 3.078, 11: 3.173, 12: 3.258, 13: 3.336, 14: 3.407, 15: 3.472,
      16: 3.532, 17: 3.588, 18: 3.640, 19: 3.689, 20: 3.735, 21: 3.778, 22: 3.819,
      23: 3.858, 24: 3.895, 25: 3.931}

D3 = {2: 0.8525, 3: 0.8884, 4: 0.8798, 5: 0.8641, 6: 0.8480, 7: 0.8332, 8: 0.8198,
      9: 0.8078, 10: 0.7971, 11: 0.7873, 12: 0.7785, 13: 0.7704, 14: 0.7630, 15: 0.7562,
      16: 0.7499, 17: 0.7441, 18: 0.7386, 19: 0.7335, 20: 0.7287, 21: 0.7242, 22: 0.7199,
      23: 0.7159, 24: 0.7121, 25: 0.7084}


def c4(n):
    """c4 = sqrt(2/(n-1)) * Γ(n/2)/Γ((n-1)/2)"""
    if n < 2:
        return None
    return math.sqrt(2.0 / (n - 1)) * math.exp(S._log_gamma(n / 2.0) - S._log_gamma((n - 1) / 2.0))


def A2(n):
    d = D2.get(n)
    return 3.0 / (d * math.sqrt(n)) if d else None


def D3_factor(n):
    d2, d3 = D2.get(n), D3.get(n)
    if not d2 or d3 is None:
        return 0.0
    return max(0.0, 1 - 3 * d3 / d2)


def D4_factor(n):
    d2, d3 = D2.get(n), D3.get(n)
    if not d2 or d3 is None:
        return None
    return 1 + 3 * d3 / d2


# ------------------------------------------------------------------ 规格解析

def resolve_spec(control, measured_mean=None, measured_sigma=None, target=None):
    """把知识库的 control 描述解析为 (lsl, usl, target)。
    control: {"mode":..., ...}"""
    if not control:
        return None, None, None
    mode = control.get("mode")
    if mode == "range":
        return control.get("lsl"), control.get("usl"), control.get("target")
    if mode == "usl":
        return None, control.get("usl"), None
    if mode == "lsl":
        return control.get("lsl"), None, None
    if mode == "deviation":
        lim = control.get("limit")
        if lim is None or measured_mean is None:
            return None, None, None
        return measured_mean - lim, measured_mean + lim, measured_mean
    if mode == "deviation_pct":
        pct = control.get("limit_pct")
        if pct is None or measured_mean is None:
            return None, None, None
        if target is None:
            target = measured_mean
        return target * (1 - pct / 100.0), target * (1 + pct / 100.0), target
    if mode == "cv_limit_pct":
        return None, control.get("cv_limit_pct"), None
    if mode == "attribute":
        return None, None, None
    return None, None, None


# ------------------------------------------------------------------ 控制图

class ControlChart(object):
    def __init__(self, chart_type, center, lcl, ucl, values, sigma_within=None,
                 sigma_overall=None, subgroup_size=1, extra=None):
        self.chart_type = chart_type
        self.center = center
        self.lcl = lcl
        self.ucl = ucl
        self.values = values
        self.sigma_within = sigma_within
        self.sigma_overall = sigma_overall
        self.subgroup_size = subgroup_size
        self.extra = extra or {}

    def zones(self):
        """返回 A/B/C 区边界（相对中心线的 sigma 倍数）"""
        s = self.sigma_within or self.sigma_overall
        if not s:
            return None
        rng = min(abs(self.ucl - self.center), abs(self.center - self.lcl)) if \
            (self.ucl is not None and self.lcl is not None) else 3 * s
        step = rng / 3.0
        return {"c": step, "b": 2 * step, "a": 3 * step}


def xbar_r(subgroups):
    """subgroups: [[v,...], ...] 返回 (ControlChart(Xbar), ControlChart(R))"""
    k = len(subgroups)
    if k < 2:
        return None, None
    n = len(subgroups[0])
    if n < 2 or n > 25:
        return None, None
    xbars = [S.mean(g) for g in subgroups]
    ranges = [(max(g) - min(g)) for g in subgroups if len(g) > 1]
    xbb = S.mean(xbars)
    rb = S.mean(ranges)
    d2 = D2.get(n, 2.0)
    sigma_w = rb / d2 if rb else None
    a2 = A2(n)
    x_chart = ControlChart("Xbar-R(Xbar)", xbb, xbb - a2 * rb if a2 else None,
                           xbb + a2 * rb if a2 else None, xbars, sigma_within=sigma_w,
                           subgroup_size=n)
    r_chart = ControlChart("Xbar-R(R)", rb, D3_factor(n) * rb, D4_factor(n) * rb,
                           ranges, subgroup_size=n)
    return x_chart, r_chart


def x_mr(values):
    """单值-移动极差图"""
    vals = [v for v in values if v is None or True]
    clean = [v for v in values if v is not None]
    if len(clean) < 8:
        return None, None
    mr = [abs(values[i] - values[i - 1]) for i in range(1, len(values))
          if values[i] is not None and values[i - 1] is not None]
    xb = S.mean(clean)
    mrb = S.mean(mr) if mr else None
    sigma_w = mrb / D2[2] if mrb else None
    x_chart = ControlChart("X-MR(X)", xb, xb - 2.66 * mrb if mrb else None,
                           xb + 2.66 * mrb if mrb else None, values,
                           sigma_within=sigma_w, sigma_overall=S.stdev(clean))
    mr_chart = ControlChart("X-MR(MR)", mrb, 0.0, D4_factor(2) * mrb if mrb else None,
                            mr)
    return x_chart, mr_chart


def x_mr_with_limits(values, center, sigma_within, lcl=None, ucl=None):
    """Phase II：用 Phase I 建立的中心线与控制限监控全段。
    关键区别：控制限不是由监控段自己算出来的。把包含异常的全段一起算限，
    异常本身会把 sigma 撑大、把限撑宽，结果是"异常判不出来"——这正是 SPC
    落地后被人说"没用"的头号原因。"""
    clean = [v for v in values if v is not None]
    if not clean:
        return None, None
    mr = [abs(values[i] - values[i - 1]) for i in range(1, len(values))
          if values[i] is not None and values[i - 1] is not None]
    mrb = S.mean(mr) if mr else None
    x_chart = ControlChart("X-MR(X)", center,
                           (center - 3 * sigma_within) if lcl is None else lcl,
                           (center + 3 * sigma_within) if ucl is None else ucl,
                           values, sigma_within=sigma_within,
                           sigma_overall=S.stdev(clean))
    mr_chart = ControlChart("X-MR(MR)", mrb, 0.0,
                            D4_factor(2) * mrb if mrb else None, mr)
    return x_chart, mr_chart


def phase1_limits(values, max_iter=4, rules=(1, 2, 3), max_drop_ratio=0.25):
    """Phase I：从数据中迭代剔除非受控点，用剩下的稳定期建立控制限。

    返回 dict：center / sigma / lcl / ucl / n_total / n_used /
               excluded(原始索引列表) / iterations / stable / note
    """
    vals = [v for v in values if v is not None]
    n_total = len(vals)
    if n_total < 8:
        return None
    idxs_all = [i for i, v in enumerate(values) if v is not None]
    alive = list(range(n_total))          # 指向 vals 的位置
    excluded = []
    iters = 0
    center, sigma = None, None
    capped = False        # 命中点太多、被剔除比例封顶拦下

    # 第 0 轮必须用稳健统计起步（中位数 + MAD），不能用均值：
    # 一段异常会把均值拉偏，均值一偏，正常点就可能被判成"连续 9 点同侧"，
    # 于是正常点跟着被剔——连环误剔下来，基线就被"造"出来了。
    med = S.median(vals)
    rs = S.robust_sigma(vals)
    if rs and rs > 0 and n_total >= 8:
        hit = [j for j, v in enumerate(vals) if abs(v - med) > 3 * rs]
        if hit:
            if len(hit) > max_drop_ratio * n_total:
                capped = True
            else:
                excluded.extend(hit)
                alive = [j for j in alive if j not in hit]
                iters = 1

    for it in range(max_iter):
        cur = [vals[j] for j in alive]
        if len(cur) < 8:
            break
        center = S.mean(cur)
        mr = [abs(cur[i] - cur[i - 1]) for i in range(1, len(cur))]
        mrb = S.mean(mr) if mr else None
        sigma = (mrb / D2[2]) if mrb else None
        if not sigma or sigma <= 0:
            sigma = S.stdev(cur)
        if not sigma or sigma <= 0:
            break
        vios = nelson_violations(cur, center, sigma, rules=rules)
        hit = set()
        for v in vios:
            hit.update(v["indexes"])
        if not hit:
            break
        iters = it + 1
        # 剔除比例封顶：超过 25% 说明过程整体不受控，再剔就是在"制造"稳定假象
        if (len(excluded) + len(hit)) > max_drop_ratio * n_total:
            capped = True
            break
        excluded += [alive[j] for j in sorted(hit)]
        alive = [alive[j] for j in range(len(cur)) if j not in hit]
    if not alive or center is None or not sigma:
        return None
    if len(alive) < 8:
        # 剔得太狠，退回全段，但要如实说明控制限偏宽
        alive = list(range(n_total))
        excluded = []
        center = S.mean(vals)
        mr = [abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))]
        mrb = S.mean(mr) if mr else None
        sigma = (mrb / D2[2]) if mrb else S.stdev(vals)
        iters = 0
        stable = False
        note = ("数据不足或异常点过多，无法分离出稳定期，已退回用全段估计控制限。"
                "此控制限偏宽，判异灵敏度下降，结论需谨慎使用")
    elif capped:
        # 关键：封顶中断时必须如实说"不稳"。否则报告会写着"已建立稳定基线"，
        # 而实际上过程一半的点都在界外——这正是"系统说没事、现场全是事"的来源。
        # 基线不可信，就退回全段，让控制限偏宽、少报，而不是假装稳定。
        alive = list(range(n_total))
        excluded = []
        center = S.mean(vals)
        mr = [abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))]
        mrb = S.mean(mr) if mr else None
        sigma = (mrb / D2[2]) if mrb else S.stdev(vals)
        iters = 0
        stable = False
        note = ("未建立稳定基线：异常点比例超过 %.0f%%，过程整体不受控。"
                "当前控制限由全段估计、偏宽，判异灵敏度下降，结论需谨慎使用"
                % (max_drop_ratio * 100))
    else:
        stable = True
        note = ("稳定期由 %d 个点中筛出 %d 个点建立（迭代 %d 轮，剔除非受控点 %d 个），"
                "控制限不再受异常段影响" % (n_total, len(alive), iters, len(excluded)))
    return {
        "center": center, "sigma": sigma,
        "lcl": center - 3 * sigma, "ucl": center + 3 * sigma,
        "n_total": n_total, "n_used": len(alive),
        "excluded": [idxs_all[j] for j in excluded],
        "excluded_local": excluded,
        "iterations": iters, "stable": stable, "note": note,
        "rules_used": tuple(parse_rules(rules)),
    }


def p_chart(defects, sizes):
    """不良率控制图"""
    rates = [d / s for d, s in zip(defects, sizes) if s]
    pbar = sum(defects) / sum(sizes) if sum(sizes) else None
    if pbar is None:
        return None
    limits = []
    for d, s in zip(defects, sizes):
        if not s:
            limits.append((None, None))
            continue
        h = 3 * math.sqrt(pbar * (1 - pbar) / s)
        limits.append((max(0.0, pbar - h), pbar + h))
    return _mk_attr("p", pbar, rates, limits)


def _mk_attr(kind, center, values, limits):
    cc = ControlChart(kind, center, limits[0][0] if limits else None,
                      limits[0][1] if limits else None, values)
    cc.extra["limits"] = limits
    return cc


def c_chart(counts):
    cbar = S.mean([c for c in counts if c is not None])
    if cbar is None:
        return None
    h = 3 * math.sqrt(cbar)
    return ControlChart("c", cbar, max(0.0, cbar - h), cbar + h, counts)


def auto_chart(values, subgroup_col=None, defect_count=None, sample_size=None):
    """自动选择控制图类型"""
    if defect_count is not None:
        if sample_size is not None:
            return p_chart(defect_count, sample_size)
        return c_chart(defect_count)
    if subgroup_col is not None:
        groups = {}
        for sg, v in zip(subgroup_col, values):
            if v is None:
                continue
            groups.setdefault(sg, []).append(v)
        subs = [g for g in groups.values() if len(g) >= 2]
        if len(subs) >= 10 and len(set(len(g) for g in subs)) == 1:
            return xbar_r(subs)
        return x_mr(values)
    return x_mr(values)


# ------------------------------------------------------------------ 判异规则（Nelson 1-8）

NELSON_RULES = [
    (1, "1 点落在 ±3σ 之外（A 区外）", "特殊原因：突发异常，立即停机/隔离该批"),
    (2, "连续 9 点落在中心线同一侧", "过程均值偏移：换料、换刀、参数调整、班次差异"),
    (3, "连续 6 点持续上升或下降", "趋势性漂移：刀具磨损、浆料沉降、温度漂移、传感器老化"),
    (4, "连续 14 点上下交替", "系统性振荡：过度调整（操作人员频繁干预）、周期性干扰"),
    (5, "连续 3 点中有 2 点落在 A 区（同侧，>2σ）", "较大的特殊原因，需查近期变更"),
    (6, "连续 5 点中有 4 点落在 B 区或更远（同侧，>1σ）", "中等偏移，常是规则 2 的早期信号"),
    (7, "连续 15 点落在 C 区（±1σ 内）", "数据分层或控制限过宽：可能混入了多批次/多设备数据未分组"),
    (8, "连续 8 点在 C 区两侧且均不在 C 区", "双峰混合分布：两台设备/两个供应商/两个配方混在一起"),
]


# 规则集预设。八条全开会误报泛滥——现场两天就会把报警当噪音关掉，
# 所以默认只开 Western Electric 基本集（超界 / 偏移 / 趋势 / 2-of-3 超 2σ）。
ALL_RULES = (1, 2, 3, 4, 5, 6, 7, 8)
DEFAULT_RULES = (1, 2, 3, 5)
RULE_PRESETS = {
    "basic": (1, 2, 3, 5),      # Western Electric 基本集（默认）
    "weco": (1, 2, 3, 5),
    "all": ALL_RULES,
    "nelson": ALL_RULES,
    "shift": (1, 2, 5, 6),      # 只盯均值偏移
    "trend": (1, 3, 5),         # 只盯趋势与漂移
    "none": (),
}


def parse_rules(spec):
    """'1,2,5' / 'basic' / 'all' / None -> 规则号元组"""
    if spec is None:
        return DEFAULT_RULES
    if isinstance(spec, (list, tuple, set)):
        got = tuple(int(x) for x in spec if str(x).isdigit() and 1 <= int(x) <= 8)
        return tuple(sorted(set(got))) or DEFAULT_RULES
    s = str(spec).strip().lower()
    if not s:
        return DEFAULT_RULES
    if s in RULE_PRESETS:
        return RULE_PRESETS[s]
    got = []
    for part in re.split(r"[,\s]+", s):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= 8:
            got.append(int(part))
    return tuple(sorted(set(got))) or DEFAULT_RULES


def rule_names(rules):
    rs = parse_rules(rules)
    return [NELSON_RULES[n - 1][1] for n in rs]


def nelson_violations(chart_values, center, sigma, rules=None):
    """返回 [{"rule":n,"desc":..,"hint":..,"indexes":[..]}]
    rules: 启用的规则号，None = 默认基本集 (1,2,3,5)，'all' = 八条全开"""
    if center is None or not sigma or sigma <= 0:
        return []
    active = set(parse_rules(rules))
    vs = chart_values
    zs = [(None if v is None else (v - center) / sigma) for v in vs]
    out = []

    def add(rule, idxs):
        if idxs and rule in active:
            r = NELSON_RULES[rule - 1]
            out.append({"rule": rule, "desc": r[1], "hint": r[2],
                        "indexes": idxs, "count": len(idxs)})

    # 规则1
    add(1, [i for i, z in enumerate(zs) if z is not None and abs(z) > 3])
    # 规则2: 连续9点同侧
    run_side, run_start = None, 0
    hits = []
    for i, z in enumerate(zs):
        if z is None:
            continue
        s = 1 if z > 0 else (-1 if z < 0 else 0)
        if s == run_side:
            if i - run_start + 1 >= 9 and (not hits or hits[-1] != i - 1):
                hits.append(i)
        else:
            run_side, run_start = s, i
    add(2, hits)
    # 规则3: 连续6点单调
    hits = []
    up = dn = 0
    prev = None
    for i, v in enumerate(vs):
        if v is None:
            continue
        if prev is not None:
            if v > prev:
                up, dn = up + 1, 0
            elif v < prev:
                dn, up = dn + 1, 0
            else:
                up = dn = 0
            if up >= 5 and (not hits or hits[-1] != i - 1):
                hits.append(i)
            if dn >= 5 and (not hits or hits[-1] != i - 1):
                hits.append(i)
        prev = v
    add(3, hits)
    # 规则4: 连续14点交替
    hits = []
    alt = 0
    prev_s = None
    for i, z in enumerate(zs):
        if z is None:
            continue
        s = 1 if z >= 0 else -1
        if prev_s is not None and s != prev_s:
            alt += 1
        else:
            alt = 1
        prev_s = s
        if alt >= 14 and (not hits or hits[-1] != i - 1):
            hits.append(i)
    add(4, hits)
    # 规则5: 连续3点中2点在A区（|z|>2，同侧）
    hits = []
    for i in range(2, len(zs)):
        w = zs[i - 2:i + 1]
        if any(z is None for z in w):
            continue
        pos = sum(1 for z in w if z > 2)
        neg = sum(1 for z in w if z < -2)
        if pos >= 2 or neg >= 2:
            if not hits or hits[-1] != i - 1:
                hits.append(i)
    add(5, hits)
    # 规则6: 连续5点中4点在B区外（|z|>1，同侧）
    hits = []
    for i in range(4, len(zs)):
        w = zs[i - 4:i + 1]
        if any(z is None for z in w):
            continue
        pos = sum(1 for z in w if z > 1)
        neg = sum(1 for z in w if z < -1)
        if pos >= 4 or neg >= 4:
            if not hits or hits[-1] != i - 1:
                hits.append(i)
    add(6, hits)
    # 规则7: 连续15点在C区（|z|<1）
    hits = []
    run = 0
    for i, z in enumerate(zs):
        if z is not None and abs(z) < 1:
            run += 1
            if run >= 15 and (not hits or hits[-1] != i - 1):
                hits.append(i)
        else:
            run = 0
    add(7, hits)
    # 规则8: 连续8点两侧且都不在C区
    hits = []
    for i in range(7, len(zs)):
        w = zs[i - 7:i + 1]
        if any(z is None for z in w):
            continue
        if all(abs(z) > 1 for z in w) and any(z > 0 for z in w) and any(z < 0 for z in w):
            if not hits or hits[-1] != i - 1:
                hits.append(i)
    add(8, hits)
    return out


# ------------------------------------------------------------------ 过程能力

def process_capability(values, lsl=None, usl=None, target=None,
                       sigma_within=None, sigma_overall=None):
    """返回能力指标 dict"""
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n < 2:
        return None
    mu = S.mean(vals)
    so = sigma_overall or S.stdev(vals)
    sw = sigma_within or so
    res = {"n": n, "mean": mu, "sigma_within": sw, "sigma_overall": so,
           "lsl": lsl, "usl": usl, "target": target}
    if lsl is None and usl is None:
        return res
    if sw and sw > 0:
        if usl is not None and lsl is not None:
            res["Cp"] = (usl - lsl) / (6 * sw)
            res["Cpk"] = min(usl - mu, mu - lsl) / (3 * sw)
            res["Cpm"] = (usl - lsl) / (6 * math.sqrt(sw ** 2 + (mu - (target if target is not None else (usl + lsl) / 2.0)) ** 2))
            res["Cr"] = 1.0 / res["Cp"] if res["Cp"] else None
        else:
            u = usl if usl is not None else lsl
            d = (u - mu) if usl is not None else (mu - lsl)
            res["Cp"] = None
            res["Cpk"] = d / (3 * sw)
    if so and so > 0:
        if usl is not None and lsl is not None:
            res["Pp"] = (usl - lsl) / (6 * so)
            res["Ppk"] = min(usl - mu, mu - lsl) / (3 * so)
        else:
            u = usl if usl is not None else lsl
            d = (u - mu) if usl is not None else (mu - lsl)
            res["Ppk"] = d / (3 * so)
    # 预期不合格率（正态假设）
    p_below = S.norm_cdf((lsl - mu) / so) if lsl is not None and so else 0.0
    p_above = 1 - S.norm_cdf((usl - mu) / so) if usl is not None and so else 0.0
    res["ppm_total"] = (p_below + p_above) * 1e6
    res["ppm_below_lsl"] = p_below * 1e6
    res["ppm_above_usl"] = p_above * 1e6
    # 西格玛水平（含 1.5σ 漂移的行业惯例）
    p = res["ppm_total"] / 1e6
    if 0 < p < 1:
        z = _inv_norm(1 - p)
        res["sigma_level"] = z + 1.5
    else:
        res["sigma_level"] = 6.0 if p == 0 else 0.0
    # 判定
    cpk = res.get("Cpk")
    if cpk is None:
        res["verdict"] = "无双侧规格，仅计算单侧能力"
    elif cpk >= 1.67:
        res["verdict"] = "能力充足（A 级）"
    elif cpk >= 1.33:
        res["verdict"] = "能力满足（B 级，量产可接受）"
    elif cpk >= 1.0:
        res["verdict"] = "能力不足（C 级，需改进）"
    else:
        res["verdict"] = "能力严重不足（D 级，必须整改）"
    return res


def _inv_norm(p):
    """正态分位数（Acklam 近似）"""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl = 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - pl:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# ------------------------------------------------------------------ 分组对比

def group_compare(values, groups, min_n=3):
    """按分组维度做 ANOVA + 组间均值对比，找出显著异常组"""
    buckets = {}
    for v, g in zip(values, groups):
        if v is None or g is None:
            continue
        buckets.setdefault(str(g), []).append(v)
    buckets = {k: v for k, v in buckets.items() if len(v) >= min_n}
    if len(buckets) < 2:
        return None
    keys = sorted(buckets.keys())
    an = S.anova_oneway([buckets[k] for k in keys])
    stats = []
    for k in keys:
        vs = buckets[k]
        stats.append({"group": k, "n": len(vs), "mean": S.mean(vs),
                      "stdev": S.stdev(vs), "min": min(vs), "max": max(vs)})
    overall = S.mean([x for g in buckets.values() for x in g])
    for s in stats:
        s["delta_vs_overall"] = (s["mean"] - overall) / overall * 100 if overall else None
    stats.sort(key=lambda s: -abs(s["delta_vs_overall"] or 0))
    return {"anova": an, "groups": stats, "overall_mean": overall,
            "n_groups": len(keys)}
