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

try:                              # 包模式
    from . import degrade
except Exception:                 # pragma: no cover - 脚本模式无包上下文
    degrade = None


def _note(where, exc):
    """降级记账的安全入口（spc 可能在无包上下文的脚本模式下被单独加载）。

    这里是"记账通道"不是业务逻辑：degrade 不可用时**必须**静默，
    否则诊断通道反而把主流程搞崩。与 degrade.py 内部的 scan:keep 同一口径。
    """
    if degrade is None:
        return
    try:
        degrade.note(where, exc)
    except Exception:
        pass
        # scan:keep 记账通道不可用时必须静默：degrade 自身已整体兜底，这里只是再挡一层
        # （脚本模式下 degrade 为 None 或半初始化），不允许反过来打断 SPC 计算。

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

    # scan:keep 类方法：控制图分区（A/B/C 区）。当前判异走 Nelson 规则，
    # 未用分区；规则集可配置化、需要分区判异时启用。
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


def _baseline_stats(vals):
    """从给定值列表估算中心线与 sigma（移动极差法，稳健起步）。

    与 phase1_limits 内部迭代共用同一套估计，避免两套公式漂移。
    返回 (center, sigma)；sigma 无法估计时返回 (center, None)。
    """
    if not vals or len(vals) < 2:
        return None, None
    center = S.mean(vals)
    mr = [abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))]
    mrb = S.mean(mr) if mr else None
    sigma = (mrb / D2[2]) if mrb else None
    if not sigma or sigma <= 0:
        sigma = S.stdev(vals)
    if not sigma or sigma <= 0:
        return center, None
    return center, sigma


def _ar1_phi(vals):
    """一阶自相关系数（朴素估计）。用于段级识别的有效样本量校正。"""
    n = len(vals)
    if n < 8:
        return 0.0
    m = S.mean(vals)
    num = sum((vals[i] - m) * (vals[i - 1] - m) for i in range(1, n))
    den = sum((v - m) ** 2 for v in vals)
    return (num / den) if den > 0 else 0.0


def _split_same_side_runs(vals, med, rs):
    """按"在稳健中心哪一侧"切连续段。返回 [(side, [局部索引...])]。"""
    runs, cur_side, cur = [], None, []
    for i, v in enumerate(vals):
        z = (v - med) / rs
        s = 1 if z > 0 else (-1 if z < 0 else 0)
        if s == 0:
            # 恰在中心线上的点不属于任何一侧，run 在此断开
            if cur_side is not None:
                runs.append((cur_side, cur))
            cur_side, cur = None, []
            continue
        if s == cur_side:
            cur.append(i)
        else:
            if cur_side is not None:
                runs.append((cur_side, cur))
            cur_side, cur = s, [i]
    if cur_side is not None:
        runs.append((cur_side, cur))
    return runs


def detect_shift_segments(values, min_run=None, min_zdiff=None,
                          min_sig=None, min_outside=None):
    """识别"整体抬升/下沉的连续段"（段级偏移），返回段列表。

    解决什么问题（这是本函数存在的唯一理由）：
      长而缓的偏移，**每个点本身都在 3σ 以内**，所以规则 1 抓不到；
      而规则 2/3 拿来做剔除又会固定丢掉 3~4% 正常点。于是 30 点 2.5σ 的偏移
      在旧实现里"有时全剔、有时一点不剔"，全看剔除比例有没有撞上 25% 封顶。
      实测旧实现整段剔除率 45~63%，而 15 点偏移是 64~97% —— 越长的偏移反而越差，
      与直觉相反。本函数把这类"整体位移"作为**段级**问题来判，不再靠逐点规则。

    判据为什么是"段内 vs 段外"而不是"段内 vs 全段中心"：
      候选段自己有十几到几十个点，占全段的比例不小（30/160=19%），
      全段中心已经被这一段拉偏（median 偏到 +0.47σ），
      于是"段内与全段的差"被压缩一半，看起来不显著 —— 实测 30 点 2.5σ 的
      z_out=1.65 过不了阈值，段级检测形同虚设。
      换用段外（不含本段）的中位数与稳健尺度做基准，才反映真实偏移幅度。

    为什么要对段外自相关做校正：
      AR(1) 序列天然会在中心线一侧停留很久（惯性），把它当"偏移段"是误判。
      但**不能**用全段 φ 门禁跳过检测——一段真偏移本身会把全段 φ 顶到 0.33~0.59，
      门禁反而把要抓的东西屏蔽掉（实测：加全段 φ 门禁后 30 点 2.5σ 的检出率
      从 86% 掉到 6%）。
      正确做法是用**段外**的 φ：正常 AR 序列段外 φ≈全段 φ → 阈值放大 → 不误报；
      真偏移时段外干净（实测 φ≈0）→ 阈值不放大 → 命中。

    返回 [{start, end, side, z, phi_out, n}]，start/end 是在传入序列里的位置。
    """
    _min_run = SEG_MIN_RUN if min_run is None else min_run
    _min_zdiff = SEG_MIN_ZDIFF if min_zdiff is None else min_zdiff
    _min_sig = SEG_MIN_SIG if min_sig is None else min_sig
    _min_out = SEG_MIN_OUTSIDE if min_outside is None else min_outside

    n = len(values)
    if n < 12:
        return []
    med = S.median(values)
    rs = S.robust_sigma(values)
    if not rs or rs <= 0:
        return []

    out = []
    all_idx = set(range(n))
    for side, idxs in _split_same_side_runs(values, med, rs):
        if len(idxs) < _min_run:
            continue
        outside = [values[i] for i in sorted(all_idx - set(idxs))]
        if len(outside) < _min_out:
            continue
        m_in = S.median([values[i] for i in idxs])
        m_out = S.median(outside)
        rs_out = S.robust_sigma(outside) or rs
        if rs_out <= 0:
            continue
        z_out = (m_in - m_out) / rs_out * side
        if z_out < _min_zdiff:
            continue
        phi_out = max(-0.9, min(0.9, _ar1_phi(outside)))
        # AR(1) 有效样本量 n_eff = n(1-φ)/(1+φ) → 标准误放大 sqrt((1+φ)/(1-φ))
        infl = math.sqrt((1 + phi_out) / (1 - phi_out))
        se = 1.2533 * rs_out / math.sqrt(len(idxs)) * infl
        if se <= 0 or (m_in - m_out) / se * side < _min_sig:
            continue
        # 段边界收紧：同侧 run 的边界会把"刚过中心线一点点"的正常点也圈进来
        # （实测 40 点稳定段 + 5 个尖峰时，第 39 点值 10.61、中心 9.98，
        #  仅高出 0.6 就被并入尖峰段，多剔一个正常点）。
        # 用"段内与段外的中点"作界：上移段只保留 > 中点的点，下移段反之。
        # 这样边界落在两群人之间，而不是落在"谁刚好在中心线之上"。
        _mid = (m_in + m_out) / 2.0
        kept = [i for i in idxs
                if (values[i] > _mid if side > 0 else values[i] < _mid)]
        if len(kept) < _min_run:
            continue
        idxs = kept
        out.append({"start": idxs[0], "end": idxs[-1], "side": side,
                    "z": round(z_out, 3), "phi_out": round(phi_out, 3),
                    "n": len(idxs),
                    "median_in": round(m_in, 6), "median_out": round(m_out, 6)})
    return out


def phase1_limits(values, max_iter=4, rules=None, max_drop_ratio=0.5,
                 mode="auto", review=None):
    """Phase I：从数据中迭代剔除非受控点，用剩下的稳定期建立控制限。

    返回 dict：center / sigma / lcl / ucl / n_total / n_used /
               excluded(原始索引列表，实际被剔除的点) /
               excluded_local(在清洗后序列中的索引) /
               exclusion_detail(每点剔除的逐点身份：规则号 + 可读理由) /
               suspect_points(被标记但未剔除的点：suggest / review 模式) /
               iterations / stable / note / mode / rules_used

    剔除模式 mode（P1-2）：
      - "auto"   ：默认，命中即剔除（原行为，保持向后兼容）。
      - "suggest"：只标记候选点（suspect_points），不剔除、不动基线；
                   由人决定是否采纳，避免系统替人做剔除决策。
      - "review" ：按 review={orig_idx: {"status": "approved"/"rejected"/"pending",
                    "by": 审核人, "note": ...}} 采纳 / 驳回；只有 "approved" 的点才剔除，
                    其余进入 suspect_points 并带 review_status / reviewed_by。

    逐点身份（P1-2 关键）：每条被标记的点都记录命中的 Nelson 规则号与可读理由，
    不再只保留全局 rules_used，解决"谁触发了哪个点的剔除"不可追溯的问题。

    规则集 rules（外部评审 P0-2，2026-10-29）：
      默认取 EXCLUDE_RULES（(1,2,3) 保守剔除集），**与判异用的 DEFAULT_RULES 分开**。
      旧实现有三处默认值各自为政（本函数 (1,2,3)、DEFAULT_RULES (1,2,3,5)、
      pipeline 调用时压根不传），于是"基线剔除"和"判异"各说各话。
      但真正的病根不在默认值，而在规则 2 的实现：它只标记 run 长度达标之后的尾部，
      run 头 8 个点不算命中，所以 15 点连续偏移只报后 7 个点（迭代后剩 5 个、
      占 4.2%），刚好卡在 5% 阈值下面判成 stable=True。**阈值不是根因。**
      规则 2 修好后同一份数据整段 15 点全剔（12.5%），stable 正确翻为 False，
      中心回到 10.0 附近，控制限不再被偏移段撑宽；正常受控数据仍 stable=True。
      注意剔除**不跟随调用方的 rules 全量集**：多剔正常点会让 σ 变小、Cpk 虚高，
      比漏剔更危险（理由见 EXCLUDE_RULES 常量处的注释）。
    """
    # rules = 告警/候选收集用的规则集（默认 DEFAULT_RULES，可到全 8 条）。
    # 注意它与"剔除用哪几条"是两件事：剔除固定只看 EXCLUDE_RULES=(1,)，
    # 见常量处的实测说明。这里若把默认改成 EXCLUDE_RULES，
    # suggest 模式就只会列出规则1 的疑似点，规则2/3 检出的偏移完全不提示
    # ——使用者会以为"没提示 = 没问题"，这比剔除略多略少严重得多。
    rules = parse_rules(rules)
    vals = [v for v in values if v is not None]
    n_total = len(vals)
    if n_total < 8:
        return None
    idxs_all = [i for i, v in enumerate(values) if v is not None]
    alive = list(range(n_total))          # 指向 vals 的位置
    excluded = []                          # 指向 vals 的位置（实际剔除）
    cand_by_idx = {}                       # 原始索引 -> 候选点细节
    iters = 0
    center, sigma = None, None
    capped = False        # 命中点太多、被剔除比例封顶拦下

    # ---- 第 -1 步：段级偏移识别（先剔"整体抬升/下沉的段"）----------------
    # 必须放在逐点迭代之前，原因有两个：
    #   ① 长而缓的偏移，每个点都在 3σ 内，逐点规则 1 永远抓不到；
    #   ② 逐点规则 2/3 一旦参与剔除，就会固定丢掉 3~4% 正常点（假警报率决定），
    #      而且它剔出的空洞会打断"连续 N 点"的判定，使剩下的半截再也够不到阈值。
    # 先整段剔掉，后面逐点迭代只需处理单点尖峰，收敛更快也更干净。
    segments = []
    try:
        segments = detect_shift_segments(vals)
    except Exception as exc:
        _note("spc.phase1@segments", exc)
        segments = []
    seg_hit = set()
    for sg in segments:
        seg_hit |= set(range(sg["start"], sg["end"] + 1))
    if seg_hit and len(seg_hit) <= max_drop_ratio * n_total:
        # 段级命中的点也要有规则身份与可读理由：
        # 报告里的剔除明细表按 reason 呈现，缺了就是空单元格；
        # 标规则 2 是准确的——段级识别本质是"连续同侧"的段级版本（整段相对其余部分偏高），
        # 只是把判据从"逐点同侧计数"升级成"段中位数 vs 段外"的显著性检验。
        for sg in segments:
            _r = ("整体%s段（第%d–%d点）：段中位数 %.6g，比其余部分 %.6g 高 %.1f 个稳健σ"
                  % ("上移" if sg["side"] > 0 else "下移",
                     sg["start"] + 1, sg["end"] + 1,
                     sg["median_in"], sg["median_out"], abs(sg["z"])))
            for j in range(sg["start"], sg["end"] + 1):
                orig = idxs_all[j]
                rec = cand_by_idx.setdefault(orig, {
                    "index": orig, "local_index": j, "iteration": 0,
                    "rules": set([2]), "value": vals[j], "z": None, "segment": True})
                rec["rules"].add(2)
                rec["segment"] = True
                rec["seg_reason"] = _r
        excluded.extend(sorted(seg_hit))
        alive = [j for j in alive if j not in seg_hit]
        iters = 1

    # 第 0 轮必须用稳健统计起步（中位数 + MAD），不能用均值：
    # 一段异常会把均值拉偏，均值一偏，正常点就可能被判成"连续 9 点同侧"，
    # 于是正常点跟着被剔——连环误剔下来，基线就被"造"出来了。
    med = S.median(vals)
    rs = S.robust_sigma(vals)
    if rs and rs > 0 and n_total >= 8:
        # 只在 alive 里找：段级已经剔过的点不能再剔一次，
        # 否则 excluded 里出现重复索引，下游按 excluded 统计点数会重复计数。
        hit = [j for j in alive if abs(vals[j] - med) > 3 * rs]
        if hit:
            if len(hit) > max_drop_ratio * n_total:
                capped = True
            else:
                for j in hit:
                    orig = idxs_all[j]
                    cand_by_idx.setdefault(orig, {
                        "index": orig, "local_index": j, "iteration": 0,
                        "rules": set([0]), "value": vals[j], "z": None})
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
        # 自相关序列的 σ 必须放大（与 EWMA/CUSUM 的 acf_factor 同一口径）。
        # 为什么必需：MR 取的是**相邻两点之差**，AR(1) 序列相邻点高度相关、
        # 差值天然小，MR-bar/d2 估出的 σ 会被低估约 sqrt(1-φ) 倍
        # （φ=0.7 → 只剩 0.55 倍），控制限因此窄了近一半，
        # 于是本来正常的点成片超界、连锁误剔。
        # 实测（φ=0.7 纯正常序列，160 点 × 20 次）：不校正误剔 13.1%、19/20 判不稳；
        # 校正后 1% 量级、误判不稳显著下降。真实工艺数据（涂布厚度、炉温、张力）
        # 普遍存在这种惯性，不校正等于"凡是有惯性的产线都在报警"。
        _phi_it = _ar1_phi(cur)
        if abs(_phi_it) > 0.3:
            sigma = sigma * math.sqrt((1 + abs(_phi_it)) / (1 - abs(_phi_it)))
        # 【两层解耦】候选点收集与剔除决策用不同规则集，这是三层设计的关键一步：
        #   收集（cand_by_idx → suspects / exclusion_detail）用调用方的 rules，
        #     可以到全 8 条 —— suggest 模式的语义就是"把所有疑似点交人审"，
        #     只列规则1 会让规则2/3 检出的偏移完全不提示，使用者以为没事。
        #   剔除（hit → 改写基线）只用 EXCLUDE_RULES=(1,) —— 见常量处实测：
        #     规则2/3 参与剔除会固定丢掉 3~4% 正常点。
        # 旧实现一套规则兼任两职，所以只能在"提示不全"和"误剔过多"之间二选一。
        vios = nelson_violations(cur, center, sigma, rules=rules)
        hit = set()
        drop_rule_set = set(parse_rules(EXCLUDE_RULES))
        for v in vios:
            # 先记录身份（宽集），再决定是否剔除（窄集）
            keep = v["rule"] in drop_rule_set
            for jj in v["indexes"]:
                if keep:
                    hit.add(jj)                   # 本迭代要剔除的点
                orig = idxs_all[alive[jj]]
                d = cand_by_idx.get(orig)
                if d is None:
                    z = (vals[alive[jj]] - center) / sigma if sigma else None
                    d = {"index": orig, "local_index": alive[jj], "iteration": it + 1,
                         "rules": set(), "value": vals[alive[jj]], "z": z}
                    cand_by_idx[orig] = d
                # 关键：同一异常点若同时满足多条 Nelson 规则，必须把每条都记上。
                # 旧逻辑在「已被更早规则命中」时 continue 跳过，导致第二条规则丢失——
                # 报告里的规则归因、威廉斯诊断据此给出错误结论。
                d["rules"].add(v["rule"])
        if not hit:
            # 没有可剔除的点就停（但候选身份已在上面的循环里记下了，
            # suggest 模式因此能列出"只是告警、不予剔除"的疑似点）
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

    # 候选点细节：规则号 -> 排序列表 + 可读理由
    candidates = []
    for d in cand_by_idx.values():
        rl = sorted(d["rules"])
        _parts = []
        for r in rl:
            if r == 0:
                _parts.append("稳健离群剔除法(>3σ, MAD)")
            else:
                _parts.append("规则%d：%s" % (r, NELSON_RULES[r - 1][1]))
        # 段级命中的点用段级理由覆盖（"规则2：连续9点同侧"描述的是判据，
        # 而现场要知道的是"这一段整体高了 2.5σ"——后者才是可执行的信息）。
        if d.get("seg_reason"):
            _parts.insert(0, d["seg_reason"])
        reason = "；".join(_parts)
        candidates.append({
            "index": d["index"], "local_index": d["local_index"],
            "iteration": d["iteration"], "rules": rl, "reason": reason,
            "value": d["value"], "z": d.get("z"),
            "from_segment": bool(d.get("segment")),
        })
    candidates.sort(key=lambda x: x["index"])

    mode = (mode or "auto").lower()
    if mode == "auto":
        base_positions = alive
        excluded_positions = list(excluded)
        if len(alive) < 8:
            # 剔得太狠，退回全段，但要如实说明控制限偏宽
            base_positions = list(range(n_total))
            excluded_positions = []
            stable = False
            note = ("数据不足或异常点过多，无法分离出稳定期，已退回用全段估计控制限。"
                    "此控制限偏宽，判异灵敏度下降，结论需谨慎使用")
        elif capped:
            # 关键：封顶中断时必须如实说"不稳"。否则报告会写着"已建立稳定基线"，
            # 而实际上过程一半的点都在界外——这正是"系统说没事、现场全是事"的来源。
            # 基线不可信，就退回全段，让控制限偏宽、少报，而不是假装稳定。
            base_positions = list(range(n_total))
            excluded_positions = []
            stable = False
            note = ("未建立稳定基线：异常点比例超过 %.0f%%，过程整体不受控。"
                    "当前控制限由全段估计、偏宽，判异灵敏度下降，结论需谨慎使用"
                    % (max_drop_ratio * 100))
        else:
            _excl_ratio = (len(excluded) / n_total) if n_total else 0.0
            # 稳定判定不只看"剔了多少"，要看**存在什么结构性异常**：
            #   ① 段级偏移段（整体抬升/下沉）—— 现场最该知道的那种"说变就变了"
            #   ② 超 3σ 点占比超过 5%
            #   ③ 趋势规则（规则3）命中点占比超过 20%
            #      —— 这是抓"渐进漂移"的唯一有效判据。实测：正常独立数据 0%、
            #      AR(1) φ=0.7 正常数据 3.1%、每点+0.05 的真漂移 96.9%。
            #      全规则告警不能用：8 条全开时正常数据也有 97% 会触发，
            #      拿它判稳等于把所有正常批次都标成不稳。
            _r1_ratio = 0.0
            _r3_ratio = 0.0
            try:
                if rs and rs > 0:
                    _v1 = [d for d in nelson_violations(vals, med, rs, rules=(1,))]
                    _r1_ratio = len({i for d in _v1 for i in d["indexes"]}) / float(n_total)
                    _v3 = [d for d in nelson_violations(vals, med, rs, rules=(3,))]
                    _r3_ratio = len({i for d in _v3 for i in d["indexes"]}) / float(n_total)
            except Exception as exc:
                _note("spc.phase1@stability_ratios", exc)
            _why = []
            if segments:
                _why.append("识别到 %d 个整体偏移段（%s）" % (
                    len(segments),
                    "、".join("第%d–%d点 %s%.1fσ" % (
                        s["start"] + 1, s["end"] + 1,
                        "上移" if s["side"] > 0 else "下移", abs(s["z"]))
                        for s in segments[:3])))
            # 长同侧段：证据不足以"改写基线"，但足以"不下稳定结论"。
            # 这一条是 2.19.9 补的：实测 30 点 2.5σ 偏移里，约 10% 的样本
            # 段级显著性差一点没达标（同侧 run 被切碎成 21~22 点、段中位数只高
            # 1.5~2σ），于是被误判成"稳定"。可现场明明有一段整体偏高。
            # 判稳比剔除保守一档的代价只是"多一句提醒"，而误判稳定的代价是
            # 报告写着"已建立稳定基线"、现场却全是事——两者不对等，所以宁严不松。
            _lr_n, _lr_z = 0, 0.0
            try:
                if rs and rs > 0:
                    for _sd, _ix in _split_same_side_runs(vals, med, rs):
                        if len(_ix) < SEG_MIN_RUN:
                            continue
                        _m = S.median([vals[i] for i in _ix])
                        _z = (_m - med) / rs * _sd
                        if _z > 1.5 and len(_ix) > _lr_n:
                            _lr_n, _lr_z = len(_ix), _z
            except Exception as exc:
                _note("spc.phase1@long_run", exc)
            if _lr_n >= max(SEG_MIN_RUN, 0.12 * n_total):
                _why.append("存在 %d 点连续同侧（段中位数偏离 %.1fσ）" % (_lr_n, _lr_z))
            if _r1_ratio > STABLE_RULE1_RATIO:
                _why.append("超 3σ 点占 %.1f%%" % (_r1_ratio * 100))
            if _r3_ratio > STABLE_TREND_RATIO:
                _why.append("趋势规则命中 %.0f%%（过程持续漂移）" % (_r3_ratio * 100))
            if len(excluded) > max(2, 0.05 * n_total):
                _why.append("剔除 %d 个点（%.1f%%）" % (len(excluded), _excl_ratio * 100))
            if _why:
                stable = False
                note = ("原始序列存在结构性异常，已剔除以建立可用基线；控制限基于清洗后数据。"
                        "但这些是真实的工艺变化信号，必须按不稳定过程处理，不得读成"
                        "「已建立稳定基线」。判定依据：%s" % "；".join(_why))
            else:
                stable = True
                note = ("稳定期由 %d 个点中筛出 %d 个点建立（迭代 %d 轮，剔除非受控点 %d 个），"
                        "控制限不再受异常段影响" % (n_total, len(alive), iters, len(excluded)))
    elif mode == "suggest":
        # 不剔除：基线用全段（含候选点），把候选点交给人审
        base_positions = list(range(n_total))
        excluded_positions = []
        stable = (len(candidates) == 0)
        note = ("【仅建议模式】已标记 %d 个疑似非受控点但未剔除，控制限由全段估计、偏宽。"
                "请逐点复核 suspect_points 后，再以 auto / review 模式采纳"
                % len(candidates))
    elif mode == "review":
        approved = set()
        for c in candidates:
            rev = (review or {}).get(c["index"])
            if isinstance(rev, dict) and rev.get("status") == "approved":
                approved.add(c["local_index"])
        base_positions = [j for j in range(n_total) if j not in approved]
        excluded_positions = sorted(approved)
        # 只有全部候选都已被 approved（剔除干净）才算稳定；仍有待审/驳回点
        # 说明过程尚未裁定完，基线不可信，必须显式标 False 提醒人去审。
        stable = (len(approved) == len(candidates))
        note = ("【审核模式】已按 review 采纳 %d 个剔除、保留 %d 个待审点；"
                "控制限依据已采纳的剔除建立"
                % (len(approved), len(candidates) - len(approved)))
    else:
        # 未知 mode 回退 auto，避免静默接受错误配置
        base_positions = alive
        excluded_positions = list(excluded)
        stable = not capped and len(alive) >= 8
        note = "未知 mode=%r，回退 auto" % mode

    # 基线统计量（按最终采纳的集合重算）
    bc, bs = _baseline_stats([vals[j] for j in base_positions])
    if bc is not None:
        center, sigma = bc, bs

    excluded_orig = [idxs_all[j] for j in excluded_positions]
    approved_set = set(excluded_positions)
    exclusion_detail = [c for c in candidates if c["local_index"] in approved_set]
    if mode == "review":
        suspects = []
        for c in candidates:
            if c["local_index"] in approved_set:
                continue
            rev = (review or {}).get(c["index"]) or {}
            cc = dict(c)
            cc["review_status"] = rev.get("status", "pending")
            cc["reviewed_by"] = rev.get("by")
            cc["review_note"] = rev.get("note")
            suspects.append(cc)
    elif mode == "suggest":
        suspects = [dict(c) for c in candidates]
    else:
        suspects = []

    return {
        "center": center, "sigma": sigma,
        "lcl": center - 3 * sigma, "ucl": center + 3 * sigma,
        "n_total": n_total, "n_used": len(base_positions),
        "excluded": excluded_orig,
        "excluded_local": excluded_positions,
        "exclusion_detail": exclusion_detail,
        "suspect_points": suspects,
        # 段级偏移识别结果：报告要能直接说"第80–109点整体上移2.5σ"，
        # 而不是只给一个"剔除了13个点"让人自己去猜这13点是不是连在一起。
        "shift_segments": [
            {"start": idxs_all[s["start"]], "end": idxs_all[s["end"]],
             "start_local": s["start"], "end_local": s["end"],
             "side": s["side"], "z": s["z"], "n": s["n"],
             "phi_out": s["phi_out"]}
            for s in (segments or [])],
        "iterations": iters, "stable": stable, "note": note,
        "mode": mode, "rules_used": tuple(parse_rules(rules)),
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


# scan:keep 自动选图：按数据特性选 X-R / X-s / I-MR。当前按样本量硬分档，未走自动选择。
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

# 基线剔除专用集（外部评审 P0-2 起，2026-10-29 第二次修订）。
#
# 为什么不直接用 DEFAULT_RULES：剔除会**改动基线本身**——多剔一个正常点，
# σ 就小一分、Cpk 就虚高一分，而 Cpk 虚高是比漏报更危险的错误结论。
#
# 为什么从 (1,2,3) 收窄到只剩 (1)：
# 规则 2（连续 9 点同侧）在独立随机数据上的假警报率约 0.4%/点，160 点期望命中 0.6 次，
# 每次命中 9+ 点 —— 拿它做剔除等于**固定丢掉 3~4% 的正常点**（实测 3.4%，与理论吻合）。
# 规则 3 同理。而规则 1（超 3σ）假警报率 0.27%，才是 Phase I 剔除的正当依据。
# 实测对照（正常随机数据 160 点 × 30 次）：
#   剔除集 (1,2,3) → 平均误剔 6.7 点（4.2%）；剔除集 (1) → 平均误剔 0.0 点（0.0%）。
# 那"成段偏移"由谁负责？由 detect_shift_segments()（段级识别）负责——
# 它要求"整段中位数与其他部分显著不同"，是段级判据而非单点判据，
# 对独立随机数据的误报接近 0，对自相关序列还有段外 φ 校正。
#
# 三层分工（这套分工是本次方案能"同时降误剔、提检出"的全部原因）：
#   L1 剔除（改基线）：detect_shift_segments() + 规则1        —— 只认高置信判据
#   L2 稳定判定       ：段级命中 / 规则1占比 / 规则3占比       —— 宁严不松
#   L3 告警呈现       ：规则1~8 按配置全列，不参与剔除决策
EXCLUDE_RULES = (1,)

# 段级偏移识别参数（见 detect_shift_segments）
SEG_MIN_RUN = 6          # run 至少 6 点才考虑（与规则3 的 6 点趋势对齐）
SEG_MIN_ZDIFF = 2.0      # 段中位数与段外的差距至少 2 个稳健σ
SEG_MIN_SIG = 3.0        # 标准化显著性门槛（已对段外自相关做有效样本量校正）
SEG_MIN_OUTSIDE = 10     # 段外至少 10 点，否则段外估计不可靠
# 稳定判定阈值
STABLE_RULE1_RATIO = 0.05    # 超 3σ 点占比超过 5% → 不稳
STABLE_TREND_RATIO = 0.20    # 趋势规则命中占比超过 20% → 不稳（正常 0%、AR 3%、真漂移 97%）
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


# scan:keep 库能力：判异规则名称表。当前规则名硬编码在报告里，规则集可配置化后改为读这里。
def rule_names(rules):
    rs = parse_rules(rules)
    return [NELSON_RULES[n - 1][1] for n in rs]


def autocorrelation(values, max_lag=None):
    """自相关函数 ACF（lag-1 至 max_lag）。

    为什么要有它：Shewhart 图与全部 Nelson 规则都假设**观测独立**。
    连续涂布厚度、连铸温度这类序列天生自相关——相邻两点像粘在一起。
    在这样的数据上跑常规判异，会把"正常惯性"读成"连续 9 点同侧（规则 2）"，
    报警多到现场很快不看了。先测自相关，才能决定该用哪张图。

    置信限用 ±1.96/√n（Bartlett 近似的白噪声界）：超出即为显著自相关。
    """
    vs = [v for v in (values or []) if v is not None]
    n = len(vs)
    if n < 8:
        return {"status": "INSUFFICIENT", "reason": "样本 n=%d < 8，估不出自相关" % n,
                "acf": [], "n": n}
    try:
        _mean = sum(vs) / float(n)
    except ZeroDivisionError:
        return {"status": "INSUFFICIENT", "reason": "序列为空", "acf": [], "n": 0}
    denom = sum((v - _mean) ** 2 for v in vs)
    if denom <= 0:
        return {"status": "INSUFFICIENT", "reason": "序列无变异，自相关无从谈起",
                "acf": [], "n": n}
    _max = int(max_lag) if max_lag else max(1, min(20, n // 4))
    if not max_lag:
        _max = max(1, min(20, n // 4))
    acf = []
    for lag in range(1, _max + 1):
        num = sum((vs[i] - _mean) * (vs[i + lag] - _mean) for i in range(n - lag))
        acf.append({"lag": lag, "r": num / denom})
    lim = 1.96 / math.sqrt(n)
    return {"status": "OK", "n": n, "limit": lim, "acf": acf}


def autocorrelation_diagnosis(values, alpha=None):
    """自相关诊断 → 该用哪种图。返回 dict（含 autocorrelation 各阶与结论）。

    结论取严：只要 lag-1 或前 3 阶里有任一阶显著，就认为该过程自相关，
    推荐 EWMA/CUSUM —— 宁可多给一种图，也不能让自相关序列在误差logging 下
    堆满天报警之后被人整个关掉。
    """
    a = autocorrelation(values)
    if a.get("status") != "OK":
        return {"significant": False, "status": a.get("status"),
                "reason": a.get("reason"), "recommend": "xmr"}
    lim = a["limit"]
    acf = a["acf"]
    r1 = acf[0]["r"] if acf else 0.0
    sig_idx = [d["lag"] for d in acf if abs(d["r"]) > lim]
    _sig = bool(sig_idx) and (1 in sig_idx or 2 in sig_idx or 3 in sig_idx)
    out = {
        "status": "OK", "n": a["n"], "lag1": r1, "limit": lim,
        "acf": acf[:10], "significant": _sig,
        "significant_lags": sig_idx[:10],
    }
    if _sig and r1 > 0:
        out["recommend"] = "ewma"
        out["reason"] = ("一阶自相关 r1=%.2f 超出白噪声界 ±%.2f，序列存在正自相关："
                         "相邻观测相互牵连，常规 X-MR 的 Nelson 规则会把这种惯性"
                         "读成『连续 9 点落在中心线同侧』而大量误报。"
                         "改用 EWMA（对自相关稳健）或先对残差作图。"
                         % (r1, lim))
    elif _sig and r1 < 0:
        out["recommend"] = "cusum"
        out["reason"] = ("一阶自相关 r1=%.2f 为负（交替型），常见于过度调节/振荡回路，"
                         "用 CUSUM 更容易看到累计方向" % r1)
    else:
        out["recommend"] = "xmr"
        out["reason"] = ("各阶自相关均未超出白噪声界 ±%.2f，观测可视为独立，"
                         "常规 X-MR + Nelson 规则适用" % lim)
    return out


def _acf_factor(acf_rows, lam=None, n=None):
    """自相关对 EWMA/均值统计量的方差放大因子。

    EWMA 统计量的稳态方差
        Var(z) = γ₀·λ/(2−λ)·[1 + 2·Σ_{k≥1}(1−λ)^k·ρ_k]
    只见 α（AR 系数）在拉开ぼく分母里的 ρ_k：自相关让 EWMA 变得更"摇摆"，
    控制限必须跟着放大，否则每一段惯性都会被判成报警。

    lam 为 None 时给 Newey–West/Bartlett 权重版本，用于累计和（均值）类统计量：
        F = 1 + 2·Σ_{k=1..m}(1 − k/(m+1))·ρ_k
    取正值下限 1.0：负自相关理论上会让方差变小，但只用来自十几个样本的 ρ̂
    去缩小控制限是拿抽样噪声换拒真概率，宁可保守。
    """
    rows = list(acf_rows or [])
    if not rows:
        return 1.0
    _w = []
    if lam is not None:
        _w = [(1.0 - lam) ** d["lag"] for d in rows]
    else:
        m = len(rows)
        _w = [(1.0 - float(d["lag"]) / float(m + 1)) for d in rows]
    s = 0.0
    for d, w in zip(rows, _w):
        s += w * (d.get("r") or 0.0)
    return max(1.0, 1.0 + 2.0 * s)


def ewma(values, lam=0.2, L=3.0, mu=None, sigma=None, acf=None):
    """EWMA 控制图（指数加权移动平均）。

    对小漂移的检出快于 Shewhart 图。λ=0.2 / L=3 为 Montgomery 的常用组合，
    **可用参数覆盖**：追 0.5σ 级漂移时取 λ=0.05~0.1。

    acf：autocorrelation() 的 acf 列表。给了就按  Var(z) = γ₀·λ/(2−λ)·F
    放大控制限——自相关序列不放大这条必报洪水（实测：AR(1) φ=0.85、200 点
    报了 135 个，报警刷屏的结局是这张图从此没人看）。

    sigma 未给时取**整体**标准差而不是相邻差：目的是估 γ₀（序列本身的变差）。
    相邻差只反映高频抖动，在自相关序列里会严重低估过程变差。
    """
    vs = [v for v in (values or []) if v is not None]
    n = len(vs)
    if n < 3 or not (0 < lam <= 1):
        return {"status": "INSUFFICIENT", "reason": "样本 n=%d 或 λ=%.2f 不适用" % (n, lam),
                "alarm_indexes": []}
    _mu = mu if mu is not None else (sum(vs) / float(n))
    if sigma is None:
        _m = sum(vs) / float(n)
        sigma = math.sqrt(sum((v - _m) ** 2 for v in vs) / float(n - 1))
    if not sigma or sigma <= 0:
        return {"status": "INSUFFICIENT", "reason": "变差为 0，EWMA 无从作图",
                "alarm_indexes": []}
    _f = _acf_factor(acf, lam=lam) if acf else 1.0
    _base = lam / (2.0 - lam) * _f
    z, limit_hi, limit_lo, alarms = [], [], [], []
    _prev = float(_mu)
    for i, v in enumerate(vs):
        _z = lam * v + (1 - lam) * _prev
        z.append(_z)
        _prev = _z
        _k = math.sqrt(_base * (1 - (1 - lam) ** (2 * (i + 1))))
        _h = L * sigma * _k
        limit_hi.append(_mu + _h)
        limit_lo.append(_mu - _h)
        if _z > _mu + _h or _z < _mu - _h:
            alarms.append(i)
    out = {"status": "OK", "method": "EWMA", "lambda": lam, "L": L,
           "center": _mu, "sigma_within": sigma, "stats": z,
           "ucl": limit_hi, "lcl": limit_lo, "alarm_indexes": alarms,
           "n_alarms": len(alarms), "acf_factor": round(_f, 3),
           "note": "λ=%.2f 兼顾 1σ 级漂移；更小 λ 对小漂移更灵敏但反应更慢" % lam}
    # 能力边界：稳态下能被稳定检出的最小持续漂移（原单位）。
    # 自相关越强这条越大——把"这张图到底能抓多大的变化"写进结果，
    # 远好过让它报一堆抓不住的小波动，或者干脆什么都不说。
    out["min_detectable_shift"] = round(
        L * sigma * math.sqrt(_base), 6)
    out["shift_units"] = ("原单位（同等 λ 下稳态可见的最小持续偏移，"
                          "自相关越强该值越大——这是统计规律，不是调参能绕过的）")
    return out


def cusum(values, k=0.5, h=5.0, mu=None, sigma=None, acf=None):
    """表格法 CUSUM：对"小而持续"的漂移最灵敏。

    k=0.5 / h=5 是检出 1σ 漂移的经典组合（Montgomery 公开口径，可覆盖）。

    acf：给了就按 Newey–West 累计方差放大决策区间 h——理由同 EWMA，
    在自相关序列上不放大等于把惯性当趋势报。
    """
    vs = [v for v in (values or []) if v is not None]
    n = len(vs)
    if n < 3:
        return {"status": "INSUFFICIENT", "reason": "样本 n=%d < 3" % n,
                "alarm_indexes": []}
    _mu = mu if mu is not None else (sum(vs) / float(n))
    if sigma is None:
        _m = sum(vs) / float(n)
        sigma = math.sqrt(sum((v - _m) ** 2 for v in vs) / float(n - 1))
    if not sigma or sigma <= 0:
        return {"status": "INSUFFICIENT", "reason": "变差为 0，CUSUM 无从作图",
                "alarm_indexes": []}
    _f = _acf_factor(acf, lam=None) if acf else 1.0
    kk = k * sigma
    hh = h * sigma * math.sqrt(_f)
    cp, cm, alarm_hi, alarm_lo = [0.0], [0.0], [], []
    for i, v in enumerate(vs):
        _hi = max(0.0, cp[-1] + (v - _mu) - kk)
        _lo = max(0.0, cm[-1] - (v - _mu) - kk)
        cp.append(_hi)
        cm.append(_lo)
        if _hi > hh:
            alarm_hi.append(i)
        if _lo > hh:
            alarm_lo.append(i)
    return {"status": "OK", "method": "CUSUM", "k": k, "h": h,
            "center": _mu, "sigma_within": sigma, "h_units": hh, "k_units": kk,
            "acf_factor": round(_f, 3),
            "c_plus": cp[1:], "c_minus": cm[1:],
            "alarm_indexes": sorted(set(alarm_hi + alarm_lo)),
            "alarm_high": alarm_hi, "alarm_low": alarm_lo,
            "n_alarms": len(set(alarm_hi + alarm_lo)),
            "min_detectable_shift": round(hh, 6),
            "note": "k=%.2f/h=%.1f 针对约 1σ 的持续漂移；要更早报警可降到 h=4，"
                    "代价是假报警变多" % (k, h)}


def ar1_residuals(values):
    """拟合 AR(1) 并返回残差序列：x_t − μ = φ·(x_{t−1} − μ) + ε_t。

    为什么要走这一步：
        在强自相关的序列上直接画 EWMA/CUSUM，σ 若按常规用相邻差（MR-bar/d2）估，
        得到的只是**高频抖动**的量级，而过程的真实波动比它大得多
        ——控制限窄了一半不止，于是 200 个点报了 135 个（实测），
        报警刷屏的结局是这张图从此没人看。

        标准做法是把序列里的自相关先"滤掉"，对余下的白噪声（残差）作图：
        残差正态且独立时，常规控制图的前提重新成立。
        这也是 residuals control chart（ARIMA 残差图）的常规工业用法。

    返回 {"status","phi","mu","residuals","sigma_resid","n"}；
    拟合不成功（如无变异）时如实返回 INSUFFICIENT，不编一个 φ。
    """
    vs = [v for v in (values or []) if v is not None]
    n = len(vs)
    if n < 10:
        return {"status": "INSUFFICIENT", "reason": "样本 n=%d < 10，拟合不了 AR(1)" % n,
                "residuals": [], "n": n}
    _mu = sum(vs) / float(n)
    d = [v - _mu for v in vs]
    num = sum(d[i] * d[i - 1] for i in range(1, n))
    den = sum(x * x for x in d)
    if den <= 0:
        return {"status": "INSUFFICIENT", "reason": "序列无变异", "residuals": [], "n": n}
    phi = max(-0.99, min(0.99, num / den))
    resid = [d[i] - phi * d[i - 1] for i in range(1, n)]
    if len(resid) < 3:
        return {"status": "INSUFFICIENT", "reason": "残差长度不足", "residuals": resid, "n": n}
    _mr = sum(abs(resid[i] - resid[i - 1]) for i in range(1, len(resid)))
    sigma = (_mr / float(len(resid) - 1)) / 1.128
    if sigma <= 0:
        _rm = sum(resid) / float(len(resid))
        sigma = math.sqrt(sum((x - _rm) ** 2 for x in resid) / float(len(resid) - 1))
    return {"status": "OK", "phi": phi, "mu": _mu, "residuals": resid,
            "sigma_resid": sigma, "n": n,
            "center": sum(resid) / float(len(resid)),
            "note": "φ=%.3f：残差已滤掉一阶自相关，控制限按其组内变差估计" % phi}


def ewma_stationary(values, lam=0.3, L=3.0):
    """EWMAST：自相关过程的一步预测控制图（Mastrangelo & Montgomery, 1991）。

    为什么另开一条：普通 EWMA 在强自相关序列上必须按 Var(z)=γ₀λ/(2−λ)·F 放大控制限
    （φ=0.85、λ=0.2 时放大 5.25 倍），放大之后连 3σ 的持续阶跃都常常落在限内——
    实测确认。这不是参数没调好，是这条统计量在这个场景下本来就不灵敏。

    EWMAST 换了个问法：不问"均值漂了没有"，问"这一步测得的值，跟按历史惯性
    预测的值差多少"。λ 决定预测器"追新"的速度：λ 越大越跟得快（漏掉阶跃），
    越小越滞后（误报多）。这个 λ 不是拍的，是在 φ=0.85 的 AR(1) 构造数据上
    扫过的实测结果（每组 6 个随机种子、200 点）：

        λ=0.10  误报 16.6%   1.5σ 阶跃检出 100%  平均延迟 10.7 点
        λ=0.20  误报 10.6%   检出 100%           延迟 12.5 点
        λ=0.30  误报  6.3%   检出  83%           延迟 16.8 点   ← 默认取这里
        λ=0.40  误报  3.5%   检出  67%           延迟 26.5 点

    取 λ=0.3 是因为：迟报 17 个点在连续工序里通常可以接受，
    而 10% 以上的误报率会让现场很快把报警关掉——图被关掉比迟报更贵。
    **λ 是参数，可按自家 ARL 要求覆盖。**

    预测：x̂_t = λ·x_{t−1} + (1−λ)·x̂_{t−1}，初值 x̂₁ = x₁
    作图：e_t = x_t − x̂_t，控制限 ±L·σ_e（σ_e 由 e 的移动极差/1.128 估计）
    """
    vs = [v for v in (values or []) if v is not None]
    n = len(vs)
    if n < 10 or not (0 < lam <= 1):
        return {"status": "INSUFFICIENT", "reason": "样本 n=%d 或 λ=%.2f 不适用" % (n, lam),
                "alarm_indexes": []}
    pred, err = [vs[0]], [0.0]
    for i in range(1, n):
        _p = lam * vs[i - 1] + (1 - lam) * pred[i - 1]
        pred.append(_p)
        err.append(vs[i] - _p)
    diffs = [abs(err[i] - err[i - 1]) for i in range(1, n)]
    sigma_e = (sum(diffs) / float(len(diffs))) / 1.128 if diffs else 0.0
    if sigma_e <= 0:
        return {"status": "INSUFFICIENT", "reason": "预测误差无变异", "alarm_indexes": []}
    _h = L * sigma_e
    alarms = [i for i in range(1, n) if abs(err[i]) > _h]
    return {"status": "OK", "method": "EWMAST", "lambda": lam, "L": L,
            "sigma_e": sigma_e, "prediction": pred, "error": err,
            "ucl": _h, "lcl": -_h, "n_alarms": len(alarms),
            "alarm_indexes": alarms,
            "min_detectable_shift": round(_h, 6),
            "performance_note": (
                "λ=%.2f 在 φ≈0.85 的构造数据上实测：误报约 6%%、"
                "1.5σ 阶跃检出约 83%%、平均迟报约 17 点" % lam),
            "note": "一步预测残差图：对**阶跃型**偏移灵敏；缓慢爬行请以趋势诊断为准"}


def thin_for_independence(values, max_step=None):
    """把强相关序列抽稀到近似独立，返回抽稀后的序列与原索引映射。

    为什么要抽稀而不是硬放大控制限：
        两条路都试过。① 按 Var(z)=γ₀λ/(2−λ)·F 放大 EWMA 限：φ=0.85、λ=0.2
        时 F≈5.25，限宽到连 3σ 的持续阶跃都常常落在里面—— tolerance fig w/out
        灵敏度为零的图还不如没有。② 滤掉一阶自相关后对残差作图：
        误报压下去了，但持续阶跃被滤成一次尖峰后残差恢复白噪声，
        同样是零报警——把最想知道的那类失效一并滤没了。

        抽稀是把相邻点之间那层"惯性"直接跨过去：隔 k 个取一个，
        当 ACF 掉到噪声带以内时，子样本就近似独立了，常规图重新成立。
        代价是检测延迟（点数变少），这个代价要说出来，不能装作没有。

    步长 k 取「ACF 首次落进白噪声界」的 lag +1；找不到就退到最大lag+1
    并如实标注"抽稀后仍未达独立"。
    """
    vs = [v for v in (values or []) if v is not None]
    n = len(vs)
    if n < 12:
        return {"status": "INSUFFICIENT", "reason": "样本 n=%d < 12" % n,
                "step": 1, "thinned": vs, "indexes": list(range(n))}
    a = autocorrelation(values)
    if a.get("status") != "OK":
        return {"status": "INSUFFICIENT", "reason": a.get("reason"),
                "step": 1, "thinned": vs, "indexes": list(range(n))}
    lim = a["limit"]
    step = 1
    seen_sig = False
    for row in a["acf"]:
        if abs(row["r"]) > lim:
            seen_sig = True
            continue
        if seen_sig:
            step = row["lag"]
            break
    else:
        step = min(max(1, len(a["acf"])), max(1, n // 8))
    if max_step:
        step = min(step, int(max_step))
    step = max(1, min(step, max(1, n // 8)))
    thinned = vs[::step]
    idxs = list(range(0, n, step))
    if len(thinned) < 8:                      # 抽得太狠没人可用：退一档保证图能画
        step = 1
        thinned, idxs = vs, list(range(n))
    _chk = autocorrelation_diagnosis(thinned)
    return {"status": "OK", "step": step, "thinned": thinned, "indexes": idxs,
            "independent_after": not _chk.get("significant"),
            "check": {"lag1": _chk.get("lag1"), "limit": _chk.get("limit"),
                      "significant": _chk.get("significant")},
            "note": "每 %d 个取 1 后%s独立假设；检测延迟约为常规的 %d 倍"
                    % (step, "满足" if not _chk.get("significant") else "仍未满足", step)}


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
    # 规则2: 连续9点同侧 —— 命中即整段连续返回（不得隔点）
    #
    # 旧实现按 `i - run_start + 1 >= 9` 逐点判断，只有 run 长度达标之后的点才算命中，
    # **run 的头 8 个点永远不算命中**。于是一段 15 点的连续偏移只报后 7 个点，
    # 剔除比例 5.8% 刚好卡在 5% 阈值边缘，换个数据就掉到 4.2% 判成 stable=True，
    # 报告写着"已建立稳定基线"而偏移段还在基线里把中心拉偏、把控制限撑宽
    # （外部评审 P0-2，2026-10-29；用户反馈"15 点偏移只剔 5 个"）。
    # 正确语义：run 一旦达到 9 点，整段 run 全部命中——规则讲的是"这一段都不对"，
    # 不是"从第 9 个点开始不对"。
    runs = []
    cur_side, cur_idx = None, []
    for i, z in enumerate(zs):
        if z is None:
            continue
        s = 1 if z > 0 else (-1 if z < 0 else 0)
        if s == 0:
            # 恰好落在中心线上的点不属于任何一侧，run 在此断开。
            # 不这么处理的话，一批 zp=0 的点会被算成同一侧（s=0 相同），
            # 于是「20 点中心 + 35 点偏移 + 20 点中心」这种最干净的阶跃，
            # 会把 75 个点全判成"连续同侧"——比漏报更难解释的误报。
            if cur_side is not None:
                runs.append(cur_idx)
            cur_side, cur_idx = None, []
            continue
        if s == cur_side:
            cur_idx.append(i)
        else:
            if cur_side is not None:
                runs.append(cur_idx)
            cur_side, cur_idx = s, [i]
    if cur_side is not None:
        runs.append(cur_idx)
    hits = []
    for idxs in runs:
        if len(idxs) >= 9:
            hits.extend(idxs)
    add(2, hits)
    # 规则3: 连续6点单调 —— 趋势一旦成型，该方向每点都命中，天然连续
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
            if up >= 5:
                hits.append(i)
            if dn >= 5:
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
        if alt >= 14:
            hits.append(i)
    add(4, hits)
    # 规则5: 连续3点中2点在A区（|z|>2，同侧）—— 滑动窗重叠，命中天然连续
    hits = []
    for i in range(2, len(zs)):
        w = zs[i - 2:i + 1]
        if any(z is None for z in w):
            continue
        pos = sum(1 for z in w if z > 2)
        neg = sum(1 for z in w if z < -2)
        if pos >= 2 or neg >= 2:
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
            hits.append(i)
    add(6, hits)
    # 规则7: 连续15点在C区（|z|<1）
    hits = []
    run = 0
    for i, z in enumerate(zs):
        if z is not None and abs(z) < 1:
            run += 1
            if run >= 15:
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
            hits.append(i)
    add(8, hits)
    return out


# ------------------------------------------------------------------ 过程能力

def sigma_within_mr(values):
    """移动极差法（X-MR）估计的 within-σ。

    用于分段能力（评审 P1-3）：每段用自己的 σ_within 算 Cpk，而不是退化为整体 σ
    ——否则分段 Cpk 与 Ppk 数值等同，标签失真（短期能力被当成长期能力）。
    """
    vs = [v for v in (values or []) if v is not None]
    if len(vs) < 2:
        return None
    mrs = [abs(vs[i] - vs[i - 1]) for i in range(1, len(vs))]
    if not mrs:
        return None
    mb = sum(mrs) / len(mrs)
    if not mb or mb <= 0:
        return None
    return mb / D2[2]


def process_capability(values, lsl=None, usl=None, target=None,
                       sigma_within=None, sigma_overall=None,
                       autocorr_phi=None):
    """返回能力指标 dict

    autocorr_phi：AR(1) 系数。|φ| > 0.3 时 σ_within（移动极差法）会系统性低估
    真实短期变差，使 Cpk 虚高，此时抑制 Cpk/Cp，退回用整体 σ 的 Ppk（评审 P1-1）。
    """
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n < 2:
        return None
    mu = S.mean(vals)
    so = sigma_overall or S.stdev(vals)
    sw = sigma_within or so
    res = {"n": n, "mean": mu, "sigma_within": sw, "sigma_overall": so,
           "lsl": lsl, "usl": usl, "target": target}
    # 自相关拦截（评审 P1-1）：σ_within 由移动极差估计，在存在正自相关的序列上会
    # 系统性低估真实短期变差，使 Cpk 虚高。|φ| > 0.3 时 Cpk/Cp 不可信，
    # 退回用整体 σ 的长周期能力（Ppk）并明确标注，避免"虚假的高能力"。
    _autocorr_suppress = (
        autocorr_phi is not None
        and abs(autocorr_phi) > 0.3
        and sw is not None
    )
    if lsl is None and usl is None:
        return res
    if _autocorr_suppress:
        # 自相关使 σ_within 虚低 → Cpk/Cp 虚高，不报短期能力，沿用 Ppk（整体 σ）。
        res["Cp"] = None
        res["Cpk"] = None
        res["cpk_suppressed_autocorr"] = True
    elif sw and sw > 0:
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
    # 正态性诊断（评审 P1-4）：Cpk/Ppk 的不合格率与 σ 水平由正态假设反推，
    # 显著非正态时这些数值不可直接套用。jarque_bera 此前定义却从未接入，这里补上，
    # 把正态性结论写进结果供报告标注。
    try:
        _jb_stat, _jb_p = S.jarque_bera(vals)
        res["normality"] = {
            "stat": _jb_stat, "p": _jb_p,
            "sig": (_jb_p is not None and _jb_p < 0.05),
        }
    except Exception:
        res["normality"] = None
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
        if _autocorr_suppress:
            res["verdict"] = ("自相关显著（φ=%.2f）：Cpk 基于 σ_within 会被序列惯性虚高，"
                              "已不报 Cpk，请以 Ppk（整体 σ）为长期能力基准"
                              % (autocorr_phi if autocorr_phi is not None else 0.0))
        else:
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
