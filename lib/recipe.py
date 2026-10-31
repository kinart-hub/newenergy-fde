# -*- coding: utf-8 -*-
"""
recipe.py —— 配方（recipe）对比、切换影响与配方级漂移

=============================================================================
先说清楚边界：配方不是知识库，是现场标签
=============================================================================
    配方（曝光剂量/刻蚀时间/研磨压力……的组合）是**企业资产**，本工具
    **不存配方、不给配方、不推荐配方取值**。理由与"参数绝不固化"同源：
    配方是产品与设备的耦合结果，脱离机台、材料批次、厂房条件抄来的配方是陷阱。

    工具能做且应该做的是**把配方当分组变量做统计**：
      · 同产品、同机台下，配方 A 与配方 B 的产出有没有真差异（不是均值差一点就算）
      · 配方切换那一刻，产出有没有跳变（切换本身引入的风险）
      · 某个配方内的产出是否在漂移（配方没变、结果变了 → 去查设备/材料，不是改配方）

    这三条都是"配方变更管理"的必答题，且都不需要工具知道配方内容。

=============================================================================
口径
=============================================================================
    · 组间比较：ANOVA + **效应量**（组间差 / 组内 σ），三种都有才报——
      只报 p 值会让人把"样本够大导致的显著"当成"重要"。
    · 两两比较：与参考配方逐对做 Welch t，并按 **BH-FDR** 校正
      （多组两两比不做校正 = 制造成假阳性，这是本项目已有的硬规矩）。
    · 配方切换：对"切换点前后"做分段比较，并回报**切换点位置是否由数据判定**
      （人给的点 vs 工具找的点，结论强度不同）。
    · 组内漂移：复用 `lib/spc.py` 的判稳/判异，不重复实现。

边界：不做因果推断。配方 A 比 B 好，可能是 A 恰好跑在新设备上——
混杂要另看（`lib/confound.py`）。

零第三方依赖：stdlib only。
"""

import math

try:
    from . import status as ST
    from . import stats as S
    from . import spc
    from . import table as T
except Exception:
    import status as ST                 # noqa
    import stats as S                   # noqa
    import spc                          # noqa
    import table as T                   # noqa


def _fnum(v):
    """把表格单元格转成 float（单元格常是字符串 '44.12' / '108.2 g/m²'）。

    不能用 stats.is_num 直接判：它只接受**已是数值**的类型，表格里的
    '44.12' 是字符串 → False。自检用 Python float 造数据所以全绿，
    一接真 CSV 就整列算不出来——"单测过、集成崩"。统一走 table.to_number。
    """
    n = T.to_number(v)
    return n if (n is not None and S.is_num(n)) else None


# 两两比较的最小每组样本量：低于此值不报显著性，只报描述
MIN_N_PER_GROUP = 5


def _group(values_by_recipe):
    """清洗：只保留样本量≥2 的组，返回 ({name: [floats]}, 丢弃说明)。"""
    out, dropped = {}, []
    for name, vals in (values_by_recipe or {}).items():
        xs = [x for x in (_fnum(v) for v in (vals or [])) if x is not None]
        if len(xs) < 2:
            dropped.append((name, len(xs)))
            continue
        out[str(name)] = xs
    return out, dropped


def _bh_fdr(pvals):
    """Benjamini-Hochberg 校正。返回与输入同序的校正后 p。

    不收 alpha：BH 产出的是 q 值，与 alpha 无关（alpha 属判定环节）。
    原先签名有 alpha=0.05 而函数体从不读它，等于在 API 上写假承诺。
    判显著由调用方做：`p_adj <= alpha`。
    """
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [1.0] * m
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        val = pvals[i] * m / (rank + 1)
        prev = min(prev, val)
        adj[i] = min(1.0, prev)
    return adj


def compare_recipes(values_by_recipe, reference=None, alpha=0.05):
    """比较多个配方的产出差异。

    values_by_recipe: {"R1": [...], "R2": [...]}
    reference: 参考配方名（默认取样本量最大的那组）

    返回 dict：groups / anova / effect_size / pairwise / reference / warnings
    """
    g, dropped = _group(values_by_recipe)
    out = {"status": ST.INSUFFICIENT_DATA, "groups": [], "anova": None,
           "effect_size": None, "pairwise": [], "reference": reference,
           "warnings": [],
           "note": "只做统计差异，不做因果；『A 比 B 好』可能因配方恰好跑在更好"
                   "的设备/批次上，混杂须另查。"}
    for name, n in dropped:
        out["warnings"].append("配方 %s 样本仅 %d 个（<2），未参与比较" % (name, n))
    if len(g) < 2:
        out["warnings"].append("有效配方组少于 2 个，无法比较")
        return out

    names = sorted(g)
    out["groups"] = [{"name": k, "n": len(g[k]), "mean": S.mean(g[k]),
                      "sd": S.stdev(g[k])} for k in names]
    if not reference or reference not in g:
        reference = max(names, key=lambda k: (len(g[k]), k))
        out["reference"] = reference
        if reference:
            out["warnings"].append(
                "未指定参考配方，按『样本量最大』选 %s 作参考——"
                "参考组的选择会影响『谁被判定为偏离』，建议显式指定。" % reference)

    an = S.anova_oneway([g[k] for k in names])
    out["anova"] = an

    # 效应量：最大组间差 / 合池组内 σ
    grand_sd = _pooled_sd([g[k] for k in names])
    if grand_sd and grand_sd > 0 and an:
        means = [S.mean(g[k]) for k in names]
        spread = max(means) - min(means)
        out["effect_size"] = {"spread": spread, "pooled_sd": grand_sd,
                              "ratio": spread / grand_sd,
                              "note": "组间极差 / 合池组内 σ；<1 通常意味着差异"
                                      "小于噪声，实务上不值得动配方"}

    # 两两：与参考配方比，BH-FDR 校正
    if reference in g:
        ref_vals = g[reference]
        others = [k for k in names if k != reference]
        raw_p, rows = [], []
        for k in others:
            if len(g[k]) < MIN_N_PER_GROUP or len(ref_vals) < MIN_N_PER_GROUP:
                rows.append({"recipe": k, "p": None, "p_adj": None,
                             "diff": S.mean(g[k]) - S.mean(ref_vals),
                             "skipped": "样本不足 %d，不报显著性" % MIN_N_PER_GROUP})
                raw_p.append(None)
                continue
            tt = S.t_test_2sample(g[k], ref_vals, equal_var=False)
            p = tt.get("p") if tt else None
            raw_p.append(p)
            rows.append({"recipe": k, "p": p, "p_raw": p,
                         "diff": S.mean(g[k]) - S.mean(ref_vals)})
        valid = [p for p in raw_p if p is not None]
        adj = _bh_fdr(valid)
        it = iter(adj)
        for r in rows:
            if r.get("p") is None:
                continue
            r["p_adj"] = next(it)
            r["significant"] = r["p_adj"] is not None and r["p_adj"] < alpha
            r.pop("significant_raw", None)
        out["pairwise"] = rows
        if len(valid) > 1:
            n_raw = sum(1 for p in valid if p < alpha)
            n_adj = sum(1 for r in rows
                        if r.get("significant") is True)
            if n_raw > n_adj:
                out["warnings"].append(
                    "%d 组在未校正时显著，BH-FDR 校正后只剩 %d 组——"
                    "多组两两比不校正会制造假阳性" % (n_raw, n_adj))

    if an and an.get("p") is not None:
        out["status"] = ST.SUCCESS if an["p"] < alpha else ST.NO_FINDING
        if an["p"] >= alpha:
            out["warnings"].append(
                "总体 ANOVA p=%.3f ≥ %.2f：**没有证据说明配方之间真有差异**。"
                "逐对比较里出现的零星显著，多半是多重比较噪声，不要据此改配方"
                % (an["p"], alpha))
    return out


def _pooled_sd(groups):
    num, den = 0.0, 0
    for grp in groups:
        if len(grp) < 2:
            continue
        v = S.variance(grp)
        num += (len(grp) - 1) * v
        den += len(grp) - 1
    if den <= 0:
        return None
    return math.sqrt(num / den)


def change_impact(values, change_index, alpha=0.05):
    """配方切换的影响：切换点前后产出是否跳变。

    values: 按时间顺序的序列
    change_index: 切换位置（前一值为旧配方最后一点，该位置起为新配方）

    **切换点必须由人给**（配方的实际切换时刻只有 MES/人知道）。
    工具不去猜切换点——猜出来的"跳变"多半是把趋势当成了事件。
    """
    raw = list(values or [])
    out = {"status": ST.INSUFFICIENT_DATA, "change_index": change_index,
           "n_before": 0, "n_after": 0, "shift": None, "p": None,
           "vs_baseline_noise": None, "warnings": [],
           "note": "切换点由调用方给定；工具只比较前后，不替人猜事件时刻。"}
    xs = [x for x in (_fnum(v) for v in raw) if x is not None]
    if len(xs) != len(raw):
        out["warnings"].append("序列含 %d 个非数值，已按位置对齐后剔除——"
                               "若删点会错位，请先清洗数据" % (len(raw) - len(xs)))
    try:
        ci = int(change_index)
    except (TypeError, ValueError):
        out["warnings"].append("change_index 非法（须为整数位置）")
        return out
    if ci <= 0 or ci >= len(xs):
        out["warnings"].append("change_index=%d 越界（序列长 %d，须在 1..%d）"
                               % (ci, len(xs), len(xs) - 1))
        return out
    before, after = xs[:ci], xs[ci:]
    out["n_before"], out["n_after"] = len(before), len(after)
    if len(before) < 2 or len(after) < 2:
        out["warnings"].append("切换一侧样本 <2，无法比较")
        return out
    out["shift"] = S.mean(after) - S.mean(before)
    tt = S.t_test_2sample(after, before, equal_var=False)
    out["p"] = tt.get("p") if tt else None
    # 与"基线自身波动"比：跳变是否大于切换前的常规波动
    sd_before = S.stdev(before)
    if sd_before and sd_before > 0:
        out["vs_baseline_noise"] = abs(out["shift"]) / sd_before
        if out["vs_baseline_noise"] < 1.0:
            out["warnings"].append(
                "跳变量 %.3g 小于切换前自身波动 1σ（%.3g）：这个『跳变』"
                "在常规噪声范围内，不足以归因于配方切换"
                % (abs(out["shift"]), sd_before))
    if out["p"] is not None:
        out["status"] = ST.SUCCESS if out["p"] < alpha else ST.NO_FINDING
        if out["p"] >= alpha:
            out["warnings"].append(
                "前后差异 p=%.3f 不显著：切换未观察到可归因的产出跳变" % out["p"])
    return out


def in_recipe_drift(values, recipe_name=None):
    """某个配方**内部**的产出漂移（配方没变、结果变了）。

    这类情况最容易被误判成"配方不行"，从而去改配方——实际上多半是设备漂移、
    材料批次变化或环境变化。所以这里明确回报"配方本身未变"这一事实。
    """
    xs = [x for x in (_fnum(v) for v in (values or [])) if x is not None]
    out = {"status": ST.INSUFFICIENT_DATA, "recipe": recipe_name, "n": len(xs),
           "stable": None, "violations": 0, "shift_segments": [],
           "warnings": [],
           "note": "配方未变的条件下产出漂移，应先查设备/材料/环境，"
                   "而不是改配方。"}
    if len(xs) < 10:
        out["warnings"].append("样本 %d < 10，不足以判稳" % len(xs))
        return out
    try:
        base = spc.phase1_limits(xs)
    except Exception as e:              # 判稳本身失败要留痕，不能静默
        out["warnings"].append("基线判定失败：%s" % str(e)[:80])
        return out
    out["stable"] = (base or {}).get("stable")
    out["baseline"] = base
    try:
        _scan = {}
        segs = spc.detect_shift_segments(xs, report=_scan)
        out["shift_scan"] = _scan
    except Exception as e:
        segs = []
        out["shift_scan"] = {"status": ST.FAILED,
                             "reason": "偏移段检测抛异常：%s" % str(e)[:120]}
        out["warnings"].append("偏移段检测失败：%s" % str(e)[:80])
    out["shift_segments"] = segs or []
    # 「没检出偏移段」与「样本太少没法判」是两回事，后者必须显式说出来，
    # 否则配方对比的读者会把"没做"当成"做了、没问题"。
    _ss = out.get("shift_scan") or {}
    if _ss.get("status") == ST.INSUFFICIENT_DATA:
        out["warnings"].append("未做偏移段检测：%s。配方对比期间设备是否漂移，"
                               "本条不提供判断" % (_ss.get("reason") or ""))
    if out["shift_segments"]:
        out["warnings"].append(
            "配方 %s 内部检测到 %d 个偏移段：配方参数没变而产出变了，"
            "优先查设备漂移/材料批次/环境，不要把原因归到配方上"
            % (recipe_name or "", len(out["shift_segments"])))
    out["status"] = ST.SUCCESS
    if out["stable"] is False:
        out["warnings"].append("基线不稳定：任何『配方对比』结论都要打折看待"
                               "（不稳定过程上比配方，比的是噪声）")
    return out


# ------------------------------------------------------------------ 自检

def _selftest():
    """已知真值自检：造已知均值的配方组，看差异能否被正确识别与校正。"""
    import random as _r
    ok = []
    rng = _r.Random(20261030)

    # 1) 三组，只有 R2 真的偏 +3σ，R1/R3 同分布
    def grp(mu, n=30, sd=1.0):
        return [mu + rng.gauss(0, sd) for _ in range(n)]

    g = {"R1": grp(100.0), "R2": grp(103.0), "R3": grp(100.0)}
    c = compare_recipes(g, reference="R1")
    ok.append(("总体 ANOVA 显著", c["anova"]["p"] < 0.05,
               "p=%.2g" % c["anova"]["p"]))
    r2 = [x for x in c["pairwise"] if x["recipe"] == "R2"][0]
    r3 = [x for x in c["pairwise"] if x["recipe"] == "R3"][0]
    ok.append(("真偏离的 R2 被识别为显著", r2.get("significant") is True,
               "p_adj=%.2g" % (r2.get("p_adj") or 1)))
    ok.append(("同分布的 R3 不被误判显著", r3.get("significant") is False,
               "p_adj=%.2g" % (r3.get("p_adj") or 1)))
    ok.append(("R2 的差值≈+3（精确回收）", abs(r2["diff"] - 3.0) < 0.6,
               "diff=%.2f" % r2["diff"]))
    # 效应量：3σ 差异，ratio 应接近 3
    ok.append(("效应量 ratio≈3（3σ 差异）",
               abs(c["effect_size"]["ratio"] - 3.0) < 0.6,
               "ratio=%.2f" % c["effect_size"]["ratio"]))
    # 闭式解锚点：ratio 必须严格等于 spread / pooled_sd（少了这条，
    # 把 ratio 写成 spread 的变异照样绿——实测漏抓）
    es = c["effect_size"]
    ok.append(("效应量 ratio = spread / pooled_sd（闭式解）",
               abs(es["ratio"] - es["spread"] / es["pooled_sd"]) < 1e-12,
               "%.6f vs %.6f" % (es["ratio"], es["spread"] / es["pooled_sd"])))
    # 且 spread 必须等于各组均值的极差
    mans = [g[k] and S.mean(g[k]) for k in g]
    ok.append(("效应量 spread = 组均值极差（闭式解）",
               abs(es["spread"] - (max(mans) - min(mans))) < 1e-12,
               "%.6f vs %.6f" % (es["spread"], max(mans) - min(mans))))

    # 2) 全部同分布：必须报 NO_FINDING，且明确说"没有证据说明有差异"
    g_same = {"A": grp(100.0), "B": grp(100.0), "C": grp(100.0)}
    c2 = compare_recipes(g_same, reference="A")
    ok.append(("三组同分布时判 NO_FINDING", c2["status"] == ST.NO_FINDING,
               c2["status"]))
    ok.append(("无差异时明确说明不要据此改配方",
               any("不要据此改配方" in w for w in c2["warnings"]), ""))

    # 3) BH-FDR 校正：真做校正吗（构造多组都有边缘 p 的情形）
    pv = [0.01, 0.02, 0.03, 0.04, 0.049]
    adj = _bh_fdr(pv)
    ok.append(("BH-FDR 校正后 p 不小于原 p", all(a >= p - 1e-12 for a, p in zip(adj, pv)),
               "%s" % [round(a, 3) for a in adj]))
    ok.append(("BH-FDR 结果单调且封顶 1", all(a <= 1.0 for a in adj) and
               adj[-1] == max(adj), "%s" % [round(a, 3) for a in adj]))
    # 单个假设时不应改变 p（m=1 → 校正=原值）
    ok.append(("只比 2 组时校正不改变 p", abs(_bh_fdr([0.03])[0] - 0.03) < 1e-12, ""))
    # **闭式解锚点**：m=2、p=[0.01,0.02] → 原始 = p*m/rank = [0.02,0.02]，
    # 单调化后仍 [0.02,0.02]。少了这条，"去掉 BH 校正"的变异照样绿（实测漏抓）。
    bh2 = _bh_fdr([0.01, 0.02])
    ok.append(("BH-FDR m=2 时闭式解 = [0.02, 0.02]",
               abs(bh2[0] - 0.02) < 1e-12 and abs(bh2[1] - 0.02) < 1e-12,
               "%s" % bh2))
    # m=3、p=[0.01,0.02,0.30] → rank 乘子 [3,1.5,1] → [0.03,0.03,0.30]
    bh3 = _bh_fdr([0.01, 0.02, 0.30])
    ok.append(("BH-FDR m=3 时闭式解 = [0.03, 0.03, 0.30]",
               abs(bh3[0] - 0.03) < 1e-12 and abs(bh3[1] - 0.03) < 1e-12
               and abs(bh3[2] - 0.30) < 1e-12, "%s" % bh3))
    # m=4、p=[0.001,0.02,0.03,0.5] → [0.004,0.04,0.04,0.5]（第3项 0.03*4/3=0.04）
    bh4 = _bh_fdr([0.001, 0.02, 0.03, 0.5])
    ok.append(("BH-FDR m=4 时闭式解 = [0.004,0.04,0.04,0.5]",
               abs(bh4[0] - 0.004) < 1e-12 and abs(bh4[1] - 0.04) < 1e-12
               and abs(bh4[2] - 0.04) < 1e-12 and abs(bh4[3] - 0.5) < 1e-12,
               "%s" % bh4))

    # 3b) **闭式解交叉核对**：p_adj 必须等于"对所有原始 p 做一次 BH"的结果。
    #     少了这条，"两两比较不用 BH 校正"的变异照样绿（实测漏抓两次：
    #     第一次变异串打到了自检里的同名变量，第二次只靠"某组不显著"间接判断）。
    raws = [r["p_raw"] for r in c["pairwise"] if r.get("p_raw") is not None]
    expects = _bh_fdr(raws)
    got_adj = [r["p_adj"] for r in c["pairwise"] if r.get("p_adj") is not None]
    # 注意：p 值可以小到 1e-18，用**绝对容差**1e-12 会把 1e-18 与 2e-18 判成相等，
    # 变异因此漏抓过一次。概率量比较必须用**相对误差**。
    def _rel_eq(a, e, tol=1e-9):
        scale = max(abs(e), 1e-300)
        return abs(a - e) / scale < tol
    ok.append(("两两比较的 p_adj 等于对原始 p 做 BH（相对误差核对）",
               len(raws) == len(got_adj) and
               all(_rel_eq(a, e) for a, e in zip(got_adj, expects)),
               "got=%s expect=%s" % ([round(x, 4) for x in got_adj],
                                     [round(x, 4) for x in expects])))
    ok.append(("原始 p 被保留下来（可审计，不是只给校正值）",
               all("p_raw" in r for r in c["pairwise"]), ""))

    # 4) 参考配方缺省：按样本量最大选，并告警说明
    g3 = {"small": grp(100.0, n=6), "big": grp(100.0, n=40)}
    c3 = compare_recipes(g3)
    ok.append(("未给参考时选样本量最大者", c3["reference"] == "big", c3["reference"]))
    ok.append(("未给参考时告警说明可能影响判定",
               any("建议显式指定" in w for w in c3["warnings"]), ""))

    # 5) 样本不足的组被剔除并告警（不是静默丢掉）
    c4 = compare_recipes({"A": grp(100, n=20), "B": [1.0]})
    ok.append(("样本<2 的组被剔除且告警",
               any("样本仅 1" in w for w in c4["warnings"]), c4["warnings"]))

    # 6) 切换影响：造"切换后跳 +5"的序列
    xs = grp(100.0, n=30) + grp(105.0, n=30)
    ci = change_impact(xs, 30)
    ok.append(("切换后跳变被检出（≈+5）", abs(ci["shift"] - 5.0) < 0.6,
               "shift=%.2f" % ci["shift"]))
    ok.append(("切换显著", ci["status"] == ST.SUCCESS, ci["status"]))
    ok.append(("跳变量与基线噪声的比值被算出", ci["vs_baseline_noise"] > 3,
               "ratio=%.2f" % ci["vs_baseline_noise"]))
    # 闭式解锚点：比值必须严格等于 |shift| / 切换前的 σ
    # （少了这条，把比值写成 |shift| 的变异照样绿——实测漏抓）
    sd_before = S.stdev(xs[:30])
    ok.append(("比值 = |shift| / 切换前 σ（闭式解）",
               abs(ci["vs_baseline_noise"] - abs(ci["shift"]) / sd_before) < 1e-12,
               "%.6f vs %.6f" % (ci["vs_baseline_noise"],
                                 abs(ci["shift"]) / sd_before)))
    #    无跳变时不得报显著
    xs_flat = grp(100.0, n=60)
    ci2 = change_impact(xs_flat, 30)
    ok.append(("无跳变时判 NO_FINDING", ci2["status"] == ST.NO_FINDING, ci2["status"]))
    #    切换点越界要拒答
    ci3 = change_impact(xs, 0)
    ok.append(("切换点越界时拒答", ci3["status"] == ST.INSUFFICIENT_DATA
               and ci3["warnings"], ""))
    ci4 = change_impact(xs, 999)
    ok.append(("切换点超出序列长度时拒答",
               ci4["status"] == ST.INSUFFICIENT_DATA, ""))
    # **负索引**才是真危险：不拦的话 xs[:-3] 与 xs[-3:] 都够长，
    # 会拿"整段 vs 末尾3点"算出一个荒谬的跳变还报成功（实测漏抓）
    ci5 = change_impact(xs, -3)
    ok.append(("切换点为负索引时拒答（不许切成整段 vs 末尾）",
               ci5["status"] == ST.INSUFFICIENT_DATA and ci5["warnings"],
               "status=%s" % ci5["status"]))

    # 7) 配方内漂移：配方没变而产出漂 → 必须提示"别改配方"
    xs_drift = grp(100.0, n=40) + grp(106.0, n=40)
    d = in_recipe_drift(xs_drift, "R1")
    ok.append(("配方内漂移被判出偏移段", len(d["shift_segments"]) >= 1,
               "%d 段" % len(d["shift_segments"])))
    ok.append(("配方内漂移时提示优先查设备/材料而非改配方",
               any("改配方" in w or "不要把原因归到配方" in w for w in d["warnings"]), ""))
    #    稳定序列不该报偏移段
    d2 = in_recipe_drift(grp(100.0, n=80), "R2")
    ok.append(("稳定序列不误报偏移段", len(d2["shift_segments"]) == 0,
               "%d 段" % len(d2["shift_segments"])))
    return ok
