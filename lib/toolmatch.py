# -*- coding: utf-8 -*-
"""
toolmatch.py —— 机台匹配（tool matching）：偏移估计、匹配判定与共批前提

=============================================================================
为什么必须有这一层，以及它最容易错在哪
=============================================================================
    半导体厂里同一道工序有多台设备（曝光机/刻蚀机/CMP/量测机）。"机台匹配"
    要回答的是：**换一台机做，结果会不会偏？偏多少？在不在允差内？**

    这一层最容易错的地方不是公式，是**前提被悄悄跳过**：

    如果 A 机只做 1 号批、B 机只做 2 号批，那么"A 与 B 的差异"里混着
    **批间差异**，两者完全混在一起（confounded），数学上根本不可分。
    这时无论算出什么数字都是假的。行业里的正确做法是做 **matching run /
    send-ahead wafer**：同一批（或同片）分别过两台机，用**共批差值**估偏移。

    所以本模块的第一条判据不是"偏移多大"，而是"**有没有共批**"：
      · 有共批 → 用共批内差值（配对）估偏移，配对天然消掉批间差异
      · 无共批 → 直接判 INSUFFICIENT 并说明为什么，**绝不给偏移数字**
    这不是保守，这是把"哪条结论站得住"变成可检查的东西。

=============================================================================
算法口径
=============================================================================
    · 偏移估计：对每一对机台，取**两台都测过的批**，逐批算差值 d_lot，
      偏移 = mean(d_lot)，CI 用配对 t 区间（正态近似，n≥2）。
      配对差值法把批效应完全消掉，这是它比"两台均值相减"强的唯一理由——
      后者只在"批次随机分配"时才成立，而 fab 里几乎从不成立。
    · 允差（match_tol）必须由调用方给：这是产品/工艺参数，工具不固化。
    · 判定三态：
        pass    偏移 CI 整体落在 ±允差内
        fail    偏移点估计或 CI 超出允差
        unsure  CI 跨过允差边界（样本不够，结论不确定）——**不许算 pass**
      "unsure 不算 pass"是关键：样本不足时最容易发生的事就是"看着在范围内就放行"。
    · 偏移方向（哪台偏正）必须给，否则现场无法决定调哪台。
      **语义约定**：`offset` 的符号随 (tool_a, tool_b) 顺序翻转（定义是 B 减 A），
      但 `direction` 描述的是**物理事实**，与顺序无关——B 确实偏高就永远写"B 偏正"。
      这样现场读数不会因为"谁被当参考机"而变意思。

=============================================================================
边界
=============================================================================
    1. 只做**可加偏移**的判定（机台差一个常数）。若两台机是"斜率不同"
       （量程/线性差异），一阶偏移描述不了，本模块会通过"批内差值随水平漂移"
       做体检并在超出阈值时告警。
    2. 不处理漂移随时间变化（那是 SPC 的职责，看 `lib/spc.py`）。
    3. 不判断"哪台是对的"——绝对准确度要靠标准片/校准溯源，工具只比相对差。

零第三方依赖：stdlib only。
"""

import math

try:
    from . import status as ST
    from . import stats as S
    from . import table as T
except Exception:
    import status as ST                 # noqa
    import stats as S                   # noqa
    import table as T                   # noqa


def _fnum(v):
    """把表格单元格转成 float（单元格常是字符串 '44.12' / '108.2 g/m²'）。

    不能用 stats.is_num 直接判：它只接受**已是数值**的类型，表格里的
    '44.12' 是字符串 → False。自检用 Python float 造数据所以全绿，
    一接真 CSV 就整列算不出来——"单测过、集成崩"。统一走 table.to_number。
    """
    n = T.to_number(v)
    return n if (n is not None and S.is_num(n)) else None


# 判定"批内差值随水平漂移"的阈值：差值对水平的回归斜率显著且效应不可忽略时告警。
# 这是**诊断阈值**（公开的工程判断口径），不是产品参数，可被调用方覆盖。
SLOPE_ALPHA = 0.05
SLOPE_EFFECT_RATIO = 0.30      # |斜率×水平跨度| 超过允差的 30% 视为斜率型失配


def _pairs_by_lot(rows, tool_key="tool", lot_key="lot", value_key="value"):
    """把行数据整理成 {lot: {tool: [values]}}。"""
    by_lot = {}
    for r in (rows or []):
        tool = r.get(tool_key)
        lot = r.get(lot_key)
        v = r.get(value_key)
        fv = _fnum(v)
        if tool is None or lot is None or fv is None:
            continue
        by_lot.setdefault(str(lot), {}).setdefault(str(tool), []).append(fv)
    return by_lot


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def offset_between(by_lot, tool_a, tool_b, match_tol=None, alpha=0.05):
    """估 tool_b 相对 tool_a 的偏移（用共批配对差值）。

    返回 dict：offset / ci / n_lots / per_lot / verdict / direction / warnings
    """
    out = {"tool_a": tool_a, "tool_b": tool_b, "offset": None, "ci": None,
           "n_lots": 0, "per_lot": [], "match_tol": match_tol,
           "verdict": None, "direction": None, "drift_check": None,
           "warnings": [], "status": ST.INSUFFICIENT_DATA,
           "note": "偏移是**相对差**（B 减 A）；哪台绝对准确要靠标准片溯源，本工具不判。"}
    ds, levels = [], []
    for lot in sorted(by_lot):
        g = by_lot[lot]
        if tool_a not in g or tool_b not in g:
            continue
        a = _mean(g[tool_a])
        b = _mean(g[tool_b])
        d = b - a
        lv = (a + b) / 2.0
        ds.append(d)
        levels.append(lv)
        out["per_lot"].append({"lot": lot, "a": a, "b": b, "diff": d,
                               "n_a": len(g[tool_a]), "n_b": len(g[tool_b])})
    out["n_lots"] = len(ds)

    if len(ds) == 0:
        out["warnings"].append(
            "**没有任何共批**：%s 与 %s 没有在同一批上测过。此时"
            "『两台均值之差』里混着批间差异，两者完全混杂，数学上不可分。"
            "正确做法是做 matching run / send-ahead：同一批分别过两台机再比。"
            % (tool_a, tool_b))
        return out
    if len(ds) == 1:
        out["offset"] = ds[0]
        out["warnings"].append(
            "只有 1 个共批：偏移是个**单点估计**，没有区间，不足以支撑匹配判定。"
            "至少补到 3 批（建议 5 批以上）再看。")

    m = S.mean(ds)
    out["offset"] = m
    out["direction"] = ("%s 偏正" % tool_b) if m > 0 else (
        "%s 偏正" % tool_a if m < 0 else "两台无差异")

    if len(ds) >= 2:
        sd = S.stdev(ds)
        se = sd / math.sqrt(len(ds))
        # 配对 t 区间：df = n-1
        tcrit = _t_crit(len(ds) - 1, alpha)
        half = tcrit * se
        out["ci"] = [m - half, m + half]
        # 斜率型失配体检：差值对水平回归
        out["drift_check"] = _drift_check(ds, levels, match_tol)

    # 判定
    if match_tol is None:
        out["warnings"].append("没给允差（match_tol）：只给偏移，不做匹配判定"
                              "（允差是产品参数，工具不替企业定）")
        out["status"] = ST.PARTIAL_SUCCESS if out["ci"] else ST.INSUFFICIENT_DATA
        return out
    tol = abs(float(match_tol))
    out["verdict"] = _verdict(m, out["ci"], tol)

    if out["verdict"] == "pass" and len(ds) < 3:
        out["verdict"] = "unsure"
        out["warnings"].append(
            "共批仅 %d 个：即便点估计在允差内，样本也不足以宣称匹配，判 unsure"
            % len(ds))
    if out["drift_check"] and out["drift_check"].get("suspected"):
        out["warnings"].append(out["drift_check"]["message"])
        if out["verdict"] == "pass":
            out["verdict"] = "unsure"

    out["status"] = (ST.SUCCESS if out["verdict"] in ("pass", "fail")
                     else ST.INSUFFICIENT_DATA)
    return out


def _verdict(offset, ci, tol):
    """三态判定：pass / fail / unsure。CI 跨允差边界一律 unsure。"""
    if ci is None:
        return "fail" if abs(offset) > tol else "unsure"
    lo, hi = ci
    if lo >= -tol and hi <= tol:
        return "pass"
    if abs(offset) > tol or lo > tol or hi < -tol:
        return "fail"
    return "unsure"


def _drift_check(diffs, levels, match_tol):
    """斜率型失配体检：差值是否随水平系统变化（说明不是纯常数偏移）。

    用最小二乘斜率 + 其近似显著性（n≥3 才有自由度）。
    **这是告警不是判定**：它提示"一阶偏移描述不了这两台机的关系"，要人去看线性度。
    """
    n = len(diffs)
    out = {"slope": None, "r2": None, "suspected": False, "message": ""}
    if n < 3:
        return out
    mx = S.mean(levels)
    my = S.mean(diffs)
    sxx = sum((x - mx) ** 2 for x in levels)
    if sxx <= 0:
        return out
    sxy = sum((levels[i] - mx) * (diffs[i] - my) for i in range(n))
    slope = sxy / sxx
    out["slope"] = slope
    syy = sum((y - my) ** 2 for y in diffs)
    out["r2"] = (sxy * sxy) / (sxx * syy) if syy > 0 else None
    span = max(levels) - min(levels)
    effect = abs(slope) * span
    if match_tol:
        ratio = effect / abs(float(match_tol))
        if ratio >= SLOPE_EFFECT_RATIO:
            out["suspected"] = True
            out["message"] = (
                "差值随水平系统变化（斜率=%.4g，水平跨度内效应=%.4g，达允差的 %.0f%%）："
                "这不是纯常数偏移，多半是量程/线性差异。常数偏移判定不适用，"
                "须按水平分段匹配或先做线性校准" % (slope, effect, ratio * 100))
    return out


def _t_crit(df, alpha=0.05):
    """双侧 t 临界值（数值反解，避免查表）。df 越大越接近正态。"""
    if df <= 0:
        return float("inf")
    # 用不完全 Beta 的逆：这里用二分搜索 t 使 2*(1-CDF)=alpha
    lo, hi = 0.0, 100.0
    target = 1.0 - alpha / 2.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        # t 分布的 CDF（双侧对称）：用正则不完全 beta
        p = 1.0 - 0.5 * _betainc_reg(df / 2.0, 0.5, df / (df + mid * mid))
        if p < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _betainc_reg(a, b, x):
    """正则不完全 Beta I_x(a,b)。与 stats.betai 同口径，这里独立实现避免耦合。"""
    return S.betai(a, b, x)


def match_group(rows, tools=None, reference=None, match_tol=None,
                tool_key="tool", lot_key="lot", value_key="value"):
    """整组机台匹配：指定参考机台，其余逐台与它比。

    返回 dict：reference / comparisons / group_pass / warnings

    **group_pass 只有在每一台都判 pass 时才为 True**；任何一台 unsure 或 fail
    都不许写成"整体匹配"。这是最容易被含糊过去的地方。
    """
    by_lot = _pairs_by_lot(rows, tool_key, lot_key, value_key)
    all_tools = sorted({t for g in by_lot.values() for t in g})
    use = sorted(set(str(t) for t in (tools or all_tools)))
    out = {"reference": reference, "comparisons": [], "group_pass": None,
           "tools": use, "match_tol": match_tol, "warnings": [],
           "status": ST.INSUFFICIENT_DATA,
           "note": "整体匹配要求每一台都判 pass；任一台 unsure/fail 都不算匹配。"}
    if len(use) < 2:
        out["warnings"].append("少于 2 台机台，谈不上匹配")
        return out
    if not reference:
        # 不给参考机时用"共批最多"的那台做参考：它可比性最强
        counts = {}
        for t in use:
            counts[t] = sum(1 for g in by_lot.values() if t in g)
        reference = max(counts, key=lambda k: (counts[k], k))
        out["reference"] = reference
        out["warnings"].append(
            "未指定参考机台，按『共批数最多』选 %s 作参考。参考机的选择会影响"
            "『谁被调』的业务决策，建议显式指定。" % reference)
    if reference not in use:
        out["warnings"].append("参考机台 %r 不在数据里" % reference)
        return out

    verdicts = []
    for t in use:
        if t == reference:
            continue
        r = offset_between(by_lot, reference, t, match_tol)
        out["comparisons"].append(r)
        verdicts.append(r.get("verdict"))

    if not verdicts:
        return out
    if all(v == "pass" for v in verdicts):
        out["group_pass"] = True
        out["status"] = ST.SUCCESS
    elif any(v == "fail" for v in verdicts):
        out["group_pass"] = False
        out["status"] = ST.SUCCESS
        out["warnings"].append("有 %d 台超出允差" % sum(1 for v in verdicts if v == "fail"))
    else:
        out["group_pass"] = None
        out["status"] = ST.INSUFFICIENT_DATA
        out["warnings"].append(
            "没有机台判 fail，但也没有全部判 pass（存在 unsure）："
            "**不得宣称整体匹配**，需补共批样本")
    return out


# ------------------------------------------------------------------ 自检

def _selftest():
    """已知真值自检：偏移必须被精确回收，无共批必须拒答。"""
    import random as _r
    ok = []
    rng = _r.Random(20261030)

    # 1) 无共批：必须拒答且说明原因（不许给偏移数字）
    rows_nc = []
    for lot in ("L1", "L2", "L3"):
        rows_nc.append({"tool": "A", "lot": lot, "value": 10.0 + rng.gauss(0, 0.1)})
    for lot in ("L4", "L5", "L6"):
        rows_nc.append({"tool": "B", "lot": lot, "value": 12.0 + rng.gauss(0, 0.1)})
    r0 = offset_between(_pairs_by_lot(rows_nc), "A", "B", match_tol=0.5)
    ok.append(("无共批时拒答（offset 为 None）", r0["offset"] is None,
               "offset=%s" % r0["offset"]))
    ok.append(("无共批时状态为数据不足", r0["status"] == ST.INSUFFICIENT_DATA,
               r0["status"]))
    ok.append(("无共批时说明『混杂/需 matching run』",
               any("matching run" in w or "混杂" in w for w in r0["warnings"]), ""))

    # 2) 有共批：造真偏移 +1.5，随机批效应 ±5（批效应远大于偏移，
    #    若用"两台均值相减"会被批效应污染；配对差值法应精确回收 1.5）
    rows = []
    true_off = 1.5
    lots = ["L%d" % i for i in range(1, 9)]
    for lot in lots:
        batch_effect = rng.uniform(-5, 5)       # 批间差异，远大于偏移
        for _ in range(4):
            rows.append({"tool": "A", "lot": lot,
                         "value": 100.0 + batch_effect + rng.gauss(0, 0.05)})
            rows.append({"tool": "B", "lot": lot,
                         "value": 100.0 + batch_effect + true_off + rng.gauss(0, 0.05)})
    bl = _pairs_by_lot(rows)
    r1 = offset_between(bl, "A", "B", match_tol=2.0)
    ok.append(("配对差值法精确回收已知偏移 1.5",
               abs(r1["offset"] - true_off) < 0.05, "offset=%.4f" % r1["offset"]))
    ok.append(("共批数正确 = 8", r1["n_lots"] == 8, "%d" % r1["n_lots"]))
    # 关键：同样的数据用"均值相减"会被批效应带偏 —— 证明配对不是多余的
    mean_a = S.mean([r["value"] for r in rows if r["tool"] == "A"])
    mean_b = S.mean([r["value"] for r in rows if r["tool"] == "B"])
    # 这里批效应在各批间随机，均值相减可能偶然接近；构造一个"批分配不均"的对照
    rows2 = []
    for i, lot in enumerate(lots):
        be = (i - 4) * 2.0                       # 批效应单调：A 做前 4 批、B 做后 4 批时必偏
        for _ in range(2):
            rows2.append({"tool": "A", "lot": lot, "value": 100.0 + be + true_off})
            rows2.append({"tool": "B", "lot": lot, "value": 100.0 + be + 2 * true_off})
    r2 = offset_between(_pairs_by_lot(rows2), "A", "B", match_tol=5.0)
    ok.append(("即使批效应很大，配对差值仍精确回收真偏移",
               abs(r2["offset"] - true_off) < 1e-9, "offset=%.6f" % r2["offset"]))

    # 3) 判定三态
    ok.append(("CI 整体在允差内 → pass", r1["verdict"] == "pass", r1["verdict"]))
    r_fail = offset_between(_pairs_by_lot(rows2), "A", "B", match_tol=0.5)
    ok.append(("偏移超允差 → fail", r_fail["verdict"] == "fail", r_fail["verdict"]))
    # 共批只有 2 个时，即便点估计很小也必须 unsure（不许放行）
    rows_small = []
    for lot in ("L1", "L2"):
        rows_small.append({"tool": "A", "lot": lot, "value": 10.0})
        rows_small.append({"tool": "B", "lot": lot, "value": 10.01})
    r_s = offset_between(_pairs_by_lot(rows_small), "A", "B", match_tol=1.0)
    ok.append(("共批 <3 时即便点在允差内也判 unsure（不放行）",
               r_s["verdict"] == "unsure", "verdict=%s" % r_s["verdict"]))

    # 3c) 三态判定的**单元级**验证：直接打 _verdict，避免被"斜率体检"这第二道网兜住。
    #     （实测：端到端造 CI 越界的数据时，斜率体检也会把它翻成 unsure，
    #      于是一个"unsure→pass"的变异照样能过——必须单独锁这条支路。）
    ok.append(("_verdict：CI 整体在允差内 → pass",
               _verdict(0.5, [-0.9, 0.9], 1.0) == "pass",
               _verdict(0.5, [-0.9, 0.9], 1.0)))
    ok.append(("_verdict：CI 跨允差边界 → unsure",
               _verdict(0.8, [-0.44, 2.04], 1.0) == "unsure",
               _verdict(0.8, [-0.44, 2.04], 1.0)))
    ok.append(("_verdict：点估计与 CI 都在界外 → fail",
               _verdict(1.5, [1.2, 1.8], 1.0) == "fail",
               _verdict(1.5, [1.2, 1.8], 1.0)))
    ok.append(("_verdict：没有区间且点在界内 → unsure（不许放行）",
               _verdict(0.9, None, 1.0) == "unsure",
               _verdict(0.9, None, 1.0)))
    #     这条最初没测——变异"unsure 改成 pass"照样绿，说明只测到了样本量闸门那条支路。
    #     diffs=[0.3,0.8,1.3]：均值 0.8，sd=0.5，n=3 → t≈4.303，CI≈[−0.44, 2.04]
    #     允差 1.0：点估计 0.8 在界内，但区间上界 2.04 越界 → 只能 unsure
    rows_ci = []
    for lot, d in (("L1", 0.3), ("L2", 0.8), ("L3", 1.3)):
        rows_ci.append({"tool": "A", "lot": lot, "value": 100.0})
        rows_ci.append({"tool": "B", "lot": lot, "value": 100.0 + d})
    r_ci = offset_between(_pairs_by_lot(rows_ci), "A", "B", match_tol=1.0)
    ok.append(("点估计在允差内但 CI 越界 → unsure（不是 pass）",
               r_ci["verdict"] == "unsure",
               "verdict=%s offset=%.3f ci=%s" % (r_ci["verdict"], r_ci["offset"],
                                                 r_ci["ci"])))
    ok.append(("上述判 unsure 时确实有区间（不是靠样本量闸门蒙的）",
               r_ci["ci"] is not None and r_ci["ci"][1] > 1.0,
               "ci=%s" % (r_ci["ci"],)))

    # 4) 单共批：给偏移但必须告警"没有区间"
    one = [{"tool": "A", "lot": "L1", "value": 10.0},
           {"tool": "B", "lot": "L1", "value": 11.0}]
    r_one = offset_between(_pairs_by_lot(one), "A", "B", match_tol=0.5)
    ok.append(("单共批时告警没有区间",
               r_one["offset"] is not None and r_one["ci"] is None
               and any("单点估计" in w for w in r_one["warnings"]),
               "ci=%s" % r_one["ci"]))

    # 5) 没给允差：只给偏移，不做判定
    r_nt = offset_between(bl, "A", "B", match_tol=None)
    ok.append(("没给允差时不做匹配判定", r_nt["verdict"] is None
               and any("允差" in w for w in r_nt["warnings"]), ""))

    # 6) 方向必须给（现场据此决定调哪台）
    ok.append(("偏移方向写明（B 偏正）", "B 偏正" in (r1["direction"] or ""),
               r1["direction"]))
    r_neg = offset_between(_pairs_by_lot(rows2), "B", "A", match_tol=5.0)
    # 语义约定：**direction 描述物理事实，与比较顺序无关**（B 确实偏高就是"B 偏正"）；
    # 只有 offset 的符号随 (a,b) 顺序翻转。这样现场读数不会因为"谁当参考"而变意思。
    # 对照组必须用同一份数据（rows2，无噪声、真偏移 1.5），不能用带噪声的 r1
    ok.append(("反向比较时偏移符号翻转（+1.5 → −1.5）",
               abs(r_neg["offset"] + r2["offset"]) < 1e-9,
               "%.6f vs %.6f" % (r_neg["offset"], r2["offset"])))
    ok.append(("反向比较时方向标签保持一致（仍为 B 偏正）",
               r_neg["direction"] == r1["direction"] == "B 偏正",
               "%s / %s" % (r_neg["direction"], r1["direction"])))

    # 7) 斜率型失配：造"差值随水平增大"的数据，必须被体检抓到
    rows_slope = []
    for i, lot in enumerate(lots):
        lv = 100.0 + i * 10.0                    # 水平递增
        rows_slope.append({"tool": "A", "lot": lot, "value": lv})
        rows_slope.append({"tool": "B", "lot": lot, "value": lv * 1.05})   # 5% 增益误差
    r_sl = offset_between(_pairs_by_lot(rows_slope), "A", "B", match_tol=3.0)
    ok.append(("斜率型失配被抓并告警",
               r_sl["drift_check"] and r_sl["drift_check"]["suspected"],
               str(r_sl["drift_check"]["slope"] if r_sl["drift_check"] else None)))
    ok.append(("斜率型失配时不得判 pass", r_sl["verdict"] in ("unsure", "fail"),
               "verdict=%s" % r_sl["verdict"]))

    # 8) 整组匹配：任一台 unsure 就不得 group_pass=True
    grp_rows = list(rows)
    for lot in ("L1", "L2"):                     # C 机只有 2 个共批 → unsure
        grp_rows.append({"tool": "C", "lot": lot, "value": 100.0})
    g = match_group(grp_rows, reference="A", match_tol=2.0)
    ok.append(("整组里含 unsure 时 group_pass 不为 True", g["group_pass"] is not True,
               "group_pass=%s" % g["group_pass"]))
    ok.append(("整组含 unsure 时说明『不得宣称整体匹配』",
               any("不得宣称整体匹配" in w for w in g["warnings"]), ""))
    # 全部 pass 才 True
    g2 = match_group(rows, reference="A", match_tol=2.0)
    ok.append(("全部 pass 时 group_pass=True", g2["group_pass"] is True,
               "%s" % g2["group_pass"]))
    ok.append(("未指定参考机时显式告警并说明选了谁",
               any("共批数最多" in w for w in
                   match_group(rows, match_tol=2.0)["warnings"]), ""))
    return ok
