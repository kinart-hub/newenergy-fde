# -*- coding: utf-8 -*-
"""
msa.py —— 测量系统分析（Gauge R&R）与「测量能力不足」的处置门槛

为什么必须有这一层：
    前面所有推理都有一个没说出口的前提——**测出来的数就是真值**。
    一旦这条不成立，后面做得再专业都是在给噪声做归因：所谓"某参数导致面密度偏移"，
    偏移量可能有一半只是这台测厚仪自己的重复测量变差。
    行业里这是第一道门槛：先证明尺子准，再谈工件差。评审报告把它列为 P0-2。

算法口径来源：
    采用**交叉型（crossed）双因素 ANOVA 方差分量估计**，是 AIAG
    《Measurement Systems Analysis》参考手册的标准做法。
    K=5.15 对应 99% 正态覆盖（手册常用口径，部分企业用 6.0，**可用参数覆盖**）。
    分级 <10% 可接受 / 10–30% 有条件接受 / >30% 不可接受、ndc ≥ 5，
    同为上述手册公开口径，不是本项目的发明——企业有自家管控口径时传 grading 覆盖。

    注意：阈值属于**公开标准口径**，不属于"某厂产品参数"。
    本项目不固化任何行业尺寸/工艺参数，但公开判定标准要如实引用并允许被覆盖。

零第三方依赖：只用标准库 math / re。
"""

import math
import re

try:
    from . import degrade            # 包模式
    from . import stats as S
    from . import status as ST
    from . import quality_guard as QG
    from . import preprocess as PP
except Exception:                    # 脚本模式（python lib/pipeline.py）无包上下文
    import degrade                   # noqa
    import stats as S                # noqa
    import status as ST              # noqa
    import quality_guard as QG       # noqa
    import preprocess as PP          # noqa

# ------------------------------------------------------------------ 公开判定口径
# AIAG MSA 手册（Measurement Systems Analysis, 4th ed.）公开分级口径。
# 允许调用方用自家/客户管控口径整体覆盖，不写死在算法里。
AIAG_GRADING = {
    "source": "AIAG MSA Reference Manual（4th ed.）公开判定口径，企业可用管控标准覆盖",
    "acceptable_max": 10.0,     # %GRR ≤ 10%：测量系统可接受
    "marginal_max": 30.0,       # 10% < %GRR ≤ 30%：视量具成本/应用场景有条件接受
    "ndc_min": 5,               # 区分类别数 ndc ≥ 5 才够分辨过程变异
    "note": "机理与分级取自公开手册，现场适用性应由企业测量责任人复核",
}

K99 = 5.15   # 99% 正态覆盖宽度（5.15σ）；企业按 6σ 管控时传 k=6.0

_LEVEL_ACTION = {"acceptable": "pass", "marginal": "warn", "unacceptable": "block"}


def _finite(xs):
    out = []
    for x in (xs or []):
        try:
            v = float(x)
        except (TypeError, ValueError):
            # scan:keep 单个读数转不成浮点是**数据清洗的正常路径**（空格/缺失/文字标注），
            # 不是异常降级；丢了几条由调用方在 n_skipped 里记账，不会悄悄少数据
            continue
        if v == v and abs(v) != float("inf"):
            out.append(v)
    return out


# ------------------------------------------------------------------ 核心算法
def gauge_rr(triples, tolerance=None, k=K99, grading=None, drop_interaction=False):
    """交叉型 Gauge R&R（ANOVA 方差分量法）。

    参数
    ----
    triples : 可迭代的 (零件号, 测量者, 测量值)。测量者可以是人，也可以是设备/仪器。
    tolerance : 公差带宽（USL−LSL）。给了就额外算 %Tolerance。
    k : 变差宽度倍数，默认 5.15（99% 覆盖）；有的企业按 6σ 管控，传 6.0。
    grading : 覆盖默认分级阈值 dict，见 AIAG_GRADING。
    drop_interaction : True 时不把「人×零件」交互并进再现性（少数企业口径如此）。

    返回
    ----
    dict：ev/av/interaction/pv/grr/tv/pct_grr/pct_tolerance/ndc/level/action/reason/
          n_parts/n_appraisers/n_repeats/status/notes

    为什么要求平衡设计（每个 零件×测量者 格子重复次数相同）：
        方差分量在平衡设计下有干净的期望均方解；不平衡时各家近似做法不同，
        给出的数能差好几个百分点。**近似一个数比承认算不了更危险**——
        这里直接返回 INSUFFICIENT_DATA 并要求补齐，而不是给一个自己也不信的数。
    """
    gr = dict(AIAG_GRADING)
    if grading:
        gr.update({x: y for x, y in grading.items() if y is not None})

    cells, parts, apprs = {}, [], []
    n_skipped = 0
    for row in (triples or []):
        try:
            p, a, v = row[0], row[1], row[2]
        except (TypeError, IndexError, KeyError):
            n_skipped += 1
            continue
        vals = _finite([v])
        if not vals:
            n_skipped += 1
            continue
        pkey, akey = str(p), str(a)
        cells.setdefault((pkey, akey), []).append(vals[0])
        if pkey not in parts:
            parts.append(pkey)
        if akey not in apprs:
            apprs.append(akey)

    n, o = len(parts), len(apprs)
    if n < 2 or o < 1:
        return _insufficient(
            "零件数 %d、测量者数 %d：Gauge R&R 至少要有 2 个零件、1 名测量者" % (n, o),
            {"n_parts": n, "n_appraisers": o})

    sizes = set(len(v) for v in cells.values())
    missing_cells = n * o - len(cells)
    if missing_cells or len(sizes) != 1 or (sizes and min(sizes) < 2):
        return _insufficient(
            "非平衡设计或缺格子（缺 %d 个 零件×测量者 组合、重复次数不一致、"
            "或每格不足 2 次）：ANOVA 方差分量估计要求每个组合重复次数相同，请补齐后重算"
            % missing_cells,
            {"n_parts": n, "n_appraisers": o, "missing_cells": missing_cells,
             "balanced": len(sizes) == 1})
    r = sizes.pop()

    grand = _mean_of([v for vs in cells.values() for v in vs])
    pmean = {p: _mean_of([v for (pp, _a), vs in cells.items() if pp == p for v in vs])
             for p in parts}
    amean = {a: _mean_of([v for (_p, aa), vs in cells.items() if aa == a for v in vs])
             for a in apprs}

    ss_p, ss_o, ss_po, ss_e = 0.0, 0.0, 0.0, 0.0
    for p in parts:
        for a in apprs:
            vs = cells[(p, a)]
            m_pa = sum(vs) / float(len(vs))
            for v in vs:
                ss_e += (v - m_pa) ** 2
            ss_po += (m_pa - pmean[p] - amean[a] + grand) ** 2
    ss_po *= r
    ss_p = o * r * sum((pmean[p] - grand) ** 2 for p in parts)
    ss_o = n * r * sum((amean[a] - grand) ** 2 for a in apprs)

    df_p, df_o, df_po, df_e = n - 1, o - 1, (n - 1) * (o - 1), n * o * (r - 1)
    if df_e <= 0:
        return _insufficient("重复测量自由度为 0（每格只有 1 次），估不出重复性",
                             {"n_parts": n, "n_appraisers": o, "n_repeats": r})

    ms_e = ss_e / df_e
    ms_po = (ss_po / df_po) if df_po > 0 else 0.0
    ms_o = (ss_o / df_o) if df_o > 0 else 0.0
    ms_p = (ss_p / df_p) if df_p > 0 else 0.0

    notes = []
    # 方差分量按 ANOVA 期望均方解出；抽样波动可能给出负分量，
    # 负数必须截断并**说出来**，不截断会让后面的 sqrt 拿到 nan。
    var_e = max(ms_e, 0.0)
    if ms_e < 0:
        notes.append("重复性均方出现负值（数值舍入），已按 0 处理")
    if df_po > 0:
        var_po = max((ms_po - ms_e) / float(r), 0.0)
        var_o = max((ms_o - ms_po) / float(n * r), 0.0) if df_o > 0 else 0.0
        var_p = max((ms_p - ms_po) / float(o * r), 0.0)
    else:
        var_po, var_o = 0.0, 0.0
        var_p = max((ms_p - ms_e) / float(o * r), 0.0)
        notes.append("仅 1 名测量者，人与零件的交互无法与重复性分离，已并入重复性")

    ev = k * math.sqrt(var_e)
    inter = k * math.sqrt(var_po)
    av = k * math.sqrt(var_o if drop_interaction else var_o + var_po)
    pv = k * math.sqrt(var_p)
    grr = math.sqrt(ev ** 2 + av ** 2)
    tv = math.sqrt(grr ** 2 + pv ** 2)

    if tv <= 0 or grr <= 0:
        return _insufficient("总变差或测量变差为 0：所有读数完全一致，"
                            "无法评价测量能力（多半是数据错位，或量具分辨力根本不够）",
                             {"n_parts": n, "n_appraisers": o, "n_repeats": r})

    pct = 100.0 * grr / tv
    pct_tol = (100.0 * grr / float(tolerance)) if tolerance else None
    ndc = 1.41 * pv / grr
    _lv = classify(pct, ndc, gr)

    return {
        "status": ST.SUCCESS,
        "method": "crossed ANOVA variance components",
        "k": k,
        "grading_source": gr.get("source"),
        "ev": round(ev, 6), "av": round(av, 6), "interaction": round(inter, 6),
        "pv": round(pv, 6), "grr": round(grr, 6), "tv": round(tv, 6),
        "pct_grr": round(pct, 2),
        "pct_tolerance": (round(pct_tol, 2) if pct_tol is not None else None),
        "ndc": round(ndc, 2),
        "level": _lv["level"], "action": _lv["action"], "reason": _lv["reason"],
        "n_parts": n, "n_appraisers": o, "n_repeats": r,
        "n_obs": int(n * o * r), "n_skipped": n_skipped,
        "notes": notes,
        "note": AIAG_GRADING["note"],
    }


def repeatability(groups, k=K99, tolerance=None, grading=None):
    """只估重复性（单人/单设备多次测量）：合并样本标准差 σ_repeatability。

    groups：每个元素是一个零件的多次读数。
    用合并方差而不是极差法 d2 常数表：合并标准差的估计效率更高，
    且不依赖 d2 常数表对子组大小的限制（现场常见 3~5 次重复）。
    """
    num, den, sizes = 0.0, 0, set()
    means = []
    for g in (groups or []):
        vs = _finite(g)
        if len(vs) < 2:
            continue
        sizes.add(len(vs))
        m = sum(vs) / float(len(vs))
        means.append(m)
        num += sum((v - m) ** 2 for v in vs)
        den += len(vs) - 1
    if den <= 0:
        return _insufficient("没有任何零件具备 ≥2 次重复测量，估不出重复性",
                             {"n_groups": 0})

    n_rep = int(den / float(max(len(means), 1))) + 1 if means else 2
    var_e = num / float(den)
    ev = k * math.sqrt(max(var_e, 0.0))

    gr = dict(AIAG_GRADING)
    if grading:
        gr.update({x: y for x, y in grading.items() if y is not None})

    base = {
        "status": ST.PARTIAL_SUCCESS,
        "method": "repeatability only (pooled SD)",
        "k": k,
        "ev": round(ev, 6), "av": None,
        "n_groups": len(means), "n_skipped": 0,
        "note": AIAG_GRADING["note"],
        "notes": [],
    }
    if len(means) < 2:
        base["level"] = "unknown"
        base["action"] = "warn"
        base["reason"] = ("只有 1 个零件的重复读数，既给不出 %GRR，也标定不了过程变差；"
                          "重复性标准差本身可参考")
        base["pct_grr"] = None
        base["ndc"] = None
        base["notes"].append("至少需要 2 个零件才能把重复性与零件间变差分开")
        return base

    pm = sum(means) / float(len(means))
    var_means = sum((m - pm) ** 2 for m in means) / float(len(means) - 1)
    # 均值方差里扣掉重复误差的贡献后才剩零件间变差；扣成负数说明零件差异
    # 小于噪声，属于正常现场现象，按 0 处理并说出来。
    var_p = max(var_means - var_e / float(n_rep), 0.0)
    pv = k * math.sqrt(var_p)
    tv = math.sqrt(ev ** 2 + pv ** 2)
    if tv <= 0:
        base["level"] = "unknown"
        base["action"] = "warn"
        base["reason"] = "读数组合后总变差为 0，无法评价"
        base["pct_grr"] = None
        base["ndc"] = None
        return base
    pct = 100.0 * ev / tv
    ndc = 1.41 * pv / ev
    _lv = classify(pct, ndc, gr)
    base.update({
        "status": ST.SUCCESS,
        "pv": round(pv, 6), "tv": round(tv, 6),
        "pct_grr": round(pct, 2), "ndc": round(ndc, 2),
        "level": _lv["level"], "action": _lv["action"], "reason": _lv["reason"],
        "pct_tolerance": (round(100.0 * ev / float(tolerance), 2) if tolerance else None),
    })
    base["notes"].append("未测量再现性（人/设备间变差），%GRR 只含重复性，属**偏乐观**口径")
    return base


def classify(pct_grr, ndc, grading=None):
    """按分级口径给结论。%GRR 与 ndc 是两条独立的分辨力证据，取严。"""
    gr = dict(AIAG_GRADING)
    if grading:
        gr.update({x: y for x, y in grading.items() if y is not None})
    acc, marg, ndcm = float(gr["acceptable_max"]), float(gr["marginal_max"]), gr["ndc_min"]

    if pct_grr is None:
        return {"level": "unknown", "action": "warn",
                "reason": "缺少完整的变差基准，给不出 %GRR"}
    if pct_grr <= acc:
        level = "acceptable"
        reason = "%%GRR %.1f%% ≤ %.0f%%：测量系统可接受" % (pct_grr, acc)
    elif pct_grr <= marg:
        level = "marginal"
        reason = ("%%GRR %.1f%% 落在 %.0f%%–%.0f%%：有条件接受，"
                  "结论须标注受测量系统限制" % (pct_grr, acc, marg))
    else:
        level = "unacceptable"
        reason = ("%%GRR %.1f%% > %.0f%%：测量系统不可接受，"
                  "观测到的变差里大部分是量具噪声" % (pct_grr, marg))
    if ndc is not None and ndc < ndcm:
        if level == "acceptable":
            level = "marginal"
        reason += ("；区分类别数 ndc=%.1f < %d，分辨力不足，"
                   "过程真实差异可能被埋在量具噪声里" % (ndc, ndcm))
    return {"level": level, "action": _LEVEL_ACTION.get(level, "warn"), "reason": reason}


# ------------------------------------------------------------------ 从产线数据里认结构
_PART_HINT = ("样品", "试样", "样件", "试件", "被测件", "样本", "件号", "样品号",
              "零件", "part", "sample", "specimen", "piece")
_APPRAISER_HINT = ("操作员", "操作者", "测量者", "测量员", "检验员", "测试员", "评价人",
                   "appraiser", "operator", "inspector", "tester", "technician")
_DEVICE_HINT = ("仪器", "设备", "量具", "机台", "测量机", "gauge", "device", "machine",
                "equipment")


def _hint_in(col, hints):
    try:
        n = PP._norm_name(col)
    except Exception:
        n = str(col or "").lower()
    return any(h in n for h in hints)


def detect_msa_structure(table, target_col=None, min_repeat=2):
    """从产线数据里认出「同一件被重复测了」的结构，能算什么就算什么。

    为什么自动识别、而不是只认 --msa-data 专用表：
        现场绝大多数人手上没有独立 MSA 表，只有一份生产数据。若门槛只向
        "先去做个正规 MSA 再回来"敞开，这条门槛等于不存在——分析照旧在噪声上跑。
        能在这里认出「件号 + 设备/检验员 + 同一尺寸的多次读数」，
        就把眼前的数据先用起来：证据强度不及专门的 MSA 计划表，但够用来拉警报。

    返回 dict（part_col / appraiser_col / value_col / n_parts / n_appraisers /
    repeats / kind / confidence / notes）；认不出来返回 None。
    """
    if table is None:
        return None
    cols = [c for c in (getattr(table, "columns", None) or [])
            if not QG.is_internal_column(c)]
    if not cols:
        return None
    try:
        n_rows = len(table.rows)
    except Exception as exc:
        degrade.note("msa.detect@nrows", exc)
        return None
    if n_rows <= 0:
        return None

    def _col(name):
        try:
            return table.column(name)
        except Exception as exc:
            degrade.note("msa.detect@column", exc)
            return []

    part_cols = [c for c in cols if _hint_in(c, _PART_HINT)]
    appr_cols = [c for c in cols
                 if _hint_in(c, _APPRAISER_HINT) or _hint_in(c, _DEVICE_HINT)]

    value_cols = []
    for c in cols:
        if _hint_in(c, _PART_HINT):
            continue
        vals = _col(c)
        nz = [v for v in vals if v not in (None, "")]
        if not nz or len(_finite(nz)) / float(len(nz)) < 0.9:
            continue
        if QG.column_role(c, vals) != "measure":
            continue
        value_cols.append(c)
    if target_col:
        value_cols = ([target_col] if str(target_col) in cols else []) + \
                     [c for c in value_cols if c != target_col]

    def _dup_groups(keys, extra=None):
        cnt = {}
        for i in range(len(keys)):
            k = (keys[i], extra[i] if extra is not None and i < len(extra) else "")
            cnt[k] = cnt.get(k, 0) + 1
        return cnt

    best = None
    for vc in value_cols[:6]:
        if str(vc) not in cols:
            continue
        for pc in part_cols[:4]:
            keys = [str(x) for x in _col(pc)]
            cnt = _dup_groups(keys)
            n_dup = sum(1 for v in cnt.values() if v >= min_repeat)
            if n_dup < 2:
                continue
            combo = {"part_col": pc, "value_col": vc, "n_parts": n_dup,
                     "repeats": max(v for v in cnt.values() if v >= min_repeat),
                     "appraiser_col": None, "kind": "repeat_only",
                     "confidence": "medium", "n_appraisers": 1}
            # 有测量者/设备列才算得完整 crossed 结构；只有重复不算加严依据
            for ac in appr_cols[:6]:
                av = [str(x) for x in _col(ac)]
                cc = _dup_groups(keys, av)
                if sum(1 for v in cc.values() if v >= min_repeat) < 4:
                    continue
                combo["appraiser_col"] = ac
                combo["n_appraisers"] = len(set(av))
                combo["kind"] = "crossed"
                combo["confidence"] = "high"
                break
            if best is None or (combo["kind"] == "crossed" and best["kind"] != "crossed") \
                    or (combo["kind"] == "crossed" and combo["n_parts"] > best["n_parts"]):
                best = combo

    if not best:
        return None
    best["notes"] = [
        "从生产数据里自动识别的重复测量结构，证据强度弱于专门的 MSA 计划表："
        "拉警报够用，作为正式 Gauge R&R 报告不够",
        "零件/测量者/测量值三列若认错，请显式指定后重算",
    ]
    return best


def from_table(table, part_col, appraiser_col=None, value_col=None, tolerance=None,
               k=K99, grading=None):
    """按指定列组成 Gauge R&R。appraiser_col 为空时退化为纯重复性估计。"""
    if not part_col or not value_col:
        return _insufficient("缺必要列：需要「零件/样品列」与「测量值列」", {})
    try:
        pv_raw = table.column(part_col)
        vv_raw = table.column(value_col)
        av_raw = table.column(appraiser_col) if appraiser_col else None
    except Exception as exc:
        degrade.note("msa.from_table@column", exc)
        return _insufficient("列读取失败：%r" % (exc,), {})

    if av_raw is None:
        by_part = {}
        for i in range(len(pv_raw)):
            by_part.setdefault(str(pv_raw[i]), []).append(vv_raw[i])
        res = repeatability(list(by_part.values()), k=k,
                            tolerance=tolerance, grading=grading)
    else:
        triples = [(str(pv_raw[i]), str(av_raw[i]), vv_raw[i])
                   for i in range(min(len(pv_raw), len(av_raw), len(vv_raw)))]
        res = gauge_rr(triples, tolerance=tolerance, k=k, grading=grading)
    res["columns"] = {"part": part_col, "appraiser": appraiser_col,
                      "value": value_col}
    return res


# ------------------------------------------------------------------ 结论侧的门槛
def apply_msa_limit(res, msa):
    """把测量结果换算成对**结论**的限制。这是本模块的牙齿所在。

    各档处置全部由证据推导，不用让人选严/松：
      block（%GRR>30% 或 ndc<5）  观测变异大部分来自量具，在此之上排任何根因榜
          都是在给噪声编故事 → 清空候选、标记降级、要求先整改测量系统。
          与 E07「数据不足就承认」是同一条原则。
      warn（10–30%）  结论可以出，但每条候选挂上"受分辨力限制"的注记，
          避免有人拿这份报告直接去调工艺。
      pass（<10%）    正常出结论，并注明结论可信的前提已被验证。
      unverified      不阻断（否则工具没法干活），但这句提示从报告结尾的免责
          声明提到根因章节**之前**——免责写在末尾，等于告诉读报告的人
          "上面那些你可以放心相信"。
    """
    if not isinstance(res, dict):
        return res
    if not isinstance(msa, dict) or not msa.get("action"):
        return res

    act = msa.get("action")
    res["msa"] = {"level": msa.get("level"), "action": act,
                  "pct_grr": msa.get("pct_grr"), "ndc": msa.get("ndc"),
                  "reason": msa.get("reason")}
    if act == "block":
        res["measurement_limited"] = True
        res["honest_degraded"] = True
        res["status"] = ST.INSUFFICIENT_DATA
        res["candidates"] = []
        res["all_candidates"] = []
        # 5Why 也是结论载体：清空候选却留着"为什么指向某参数"那一层，
        # 等于把结论换个地方又说了一遍。状态说没结论，全文就都得没有结论。
        if res.get("five_why"):
            res["five_why"] = []
            res["five_why_note"] = ("测量系统不合格，5Why 链条一并封存："
                                    "其中的指向性推断同样建立在不可信的变差上")
        res["data_sufficiency"] = {
            "status": ST.INSUFFICIENT_DATA,
            "reason": "测量系统能力不足（%s）：本次观测到的变异里测量误差占主导，"
                      "在此之上排任何根因榜都是给噪声编故事" % msa.get("reason"),
        }
        res["what_to_provide"] = [
            "先整改/校准测量系统，重做 Gauge R&R 达标（%%GRR ≤ %.0f%% 且 ndc ≥ %d）后复测"
            % (AIAG_GRADING["marginal_max"], AIAG_GRADING["ndc_min"]),
            "GRR 超标时优先查：装夹方式、测点一致性、读数分辨力、环境温漂与振动",
            "整改落实前，本报告不得用于调整工艺参数或对外答复客户",
        ]
        res["score_note"] = "本次未产出根因排序：测量系统不合格，结论不予出具"
    elif act == "warn":
        res["measurement_limited"] = True
        res["msa_note"] = ("测量系统仅有条件接受（%s）：下列结论可用来排方向，"
                           "但按它调整工艺前须用更高精度量具或多次重复测量确认"
                           % msa.get("reason"))
        for c in (res.get("candidates") or []):
            if isinstance(c, dict):
                c["msa_note"] = "效应量处于测量系统分辨力边界，须重复测量确认"
    elif act == "pass":
        res["msa_note"] = ("测量系统可接受（%s）：本次结论涉及的变差绝大部分"
                           "可归因于过程本身" % msa.get("reason"))
    return res


def unverified_notice():
    """压根没测过 MSA 时应说的话。放在根因章节之前，不是报告末尾。"""
    return {
        "action": "unverified",
        "level": "unknown",
        "reason": "未登记测量系统 Gauge R&R 结果",
        "note": ("本报告的差异判断建立在「测出来的数就是真值」之上，而这条从未被验证过。"
                 "所谓某参数导致的偏移，其量级可能与这台量具的重复测量变差同量级。"
                 "结论可用于排查方向，不建议据此直接调整工艺参数或答复客户。"),
        "to_verify": [
            "做一次 Gauge R&R：≥10 个零件 × ≥2 名测量者 × ≥2 次重复，"
            "数据里带零件/测量者/测量值三列即可用 --msa-data 计入",
            "做不到完整计划时，至少提供同一件/同批次的重复测量数据",
            "已有 GRR 结果也可作为受控文件登记，过期未复核会与递推证据一并提醒",
        ],
    }


# ------------------------------------------------------------------ 内部工具
def _insufficient(reason, extra=None):
    out = {"status": ST.INSUFFICIENT_DATA, "level": "unknown", "action": "unknown",
           "reason": reason, "notes": [], "note": AIAG_GRADING["note"]}
    if extra:
        out.update(extra)
    return out


def _mean_of(vs):
    vs = list(vs)
    return (sum(vs) / float(len(vs))) if vs else 0.0
