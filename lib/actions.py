# -*- coding: utf-8 -*-
"""
actions.py —— 横向排查、永久措施候选、预防性参数优化

三条自律，写这条文件时反复权衡过：

1. 不制造"正确的废话"。每一条建议都必须挂来源：
   —— 来自改善措施库的历史验证案例（带实测改善幅度、样本量、文件号），或
   —— 来自本次数据的统计量（附 r / p / η²），或
   —— 明确标为"知识库无匹配，需自行设计并验证"。
   拼不出来源的建议宁可不写。

2. 永久措施永远写成"待验证候选"，不写成结论。是否可行只有现场验证说了算，
   脚本无权替工程师下这个判断。

3. 预防措施落到可执行的东西上：具体参数、具体方向（收严/放宽/纳管）、
   具体监控方式。写"加强管控"等于没写。
"""

from . import crossprocess as XP
from . import kbstore as KBS
from . import stats as S


# ------------------------------------------------------------------ 横向排查

def lateral_plan(table, target_col=None, defect=None, current_process=None,
                 adjust_for=None, contributions=None):
    """
    横向排查：回答"别处会不会也有这个问题"。

    两个层次缺一不可：
      a) 空间横向 —— 同产线的其它机台/班次/线体有没有同样的聚集性
      b) 物料横向 —— 同一来料批次喂给别的产线时会不会换个名字重新出现

    adjust_for —— 主导因子。不传时自动取与目标最相关且显著的那个数值列，
                  这是必要的：主因子的方差会把设备间差异完全淹没。
    """
    out = {"rows": [], "checklist": [], "notes": []}
    if table is None or not target_col:
        out["notes"].append("缺少目标列，无法做横向排查")
        return out

    group_cols = []
    for c in table.columns:
        if XP.is_batch_key(c) or any(k in c for k in ("设备", "机台", "线体", "产线", "班次",
                                                      "班组", "班别", "head", "供应商", "来料")):
            group_cols.append(c)
    if not group_cols:
        out["notes"].append("数据里没有设备号/班次/批次类分组列，只能做全局趋势，做不了横向对比——"
                            "建议索取带机台号与班次的分层数据")

    for c in group_cols:
        idx = table.col_index(c)
        vals = [str(r[idx]).strip() for r in table.rows]
        levels = set(v for v in vals if v)
        if 1 < len(levels) <= 12:
            continue
        group_cols = [x for x in group_cols if x != c]   # 层级太多（如唯一序列号）不做分组

    if adjust_for is None and contributions:
        cand = [c for c in contributions
                if abs((c.get("corr") or {}).get("r") or 0) >= 0.5
                and S.is_sig((c.get("corr") or {}).get("p"))]
        if cand:
            adjust_for = cand[0]["column"]
    rows = XP.lateral_sweep(table, target_col, group_cols, adjust_for=adjust_for)
    out["rows"] = rows
    out["adjusted_for"] = rows[0].get("adjusted_for") if rows else None
    if out["adjusted_for"]:
        out["notes"].append("已先剔除主因子「%s」的影响再比较，否则它的方差会把设备间差异淹没"
                            % out["adjusted_for"])

    for r in rows:
        if r["verdict"].startswith("局部性问题"):
            # 残差场景下均值可能为负，"倍数"没有物理意义，要用绝对差
            if r.get("adjusted_for"):
                quant = "相对最优档高出 %.4g（已剔除「%s」影响后的残差）" % (
                    r["gap"], r["adjusted_for"])
            elif r.get("ratio"):
                quant = "均值 %.4g，是最优档 %.4g 的 %.2f 倍" % (
                    r["worst_mean"], r["best_mean"], r["ratio"])
            else:
                quant = "均值 %.4g（最优档 %.4g）" % (r["worst_mean"], r["best_mean"])
            out["checklist"].append({
                "scope": "空间横向",
                "action": "按「%s」逐档对比：重点核查 %s，%s"
                          % (r["dim"], r["worst_level"], quant),
                "basis": "η²=%.3f, p=%s（该维度解释度 %.1f%%）%s"
                         % (r["eta_sq"], S.fmt_p(r["p"], 3), r["eta_sq"] * 100, r["note"]),
            })

    # 物料横向：凡是来料/供应商/批次维度，都要往回追到上游的同批产品。
    # 这条最容易被漏掉：只查自己这几台设备不够，同一批物料喂给别的产线时，
    # 问题会换个名字重新出现。
    for r in rows:
        if any(k in r["dim"] for k in ("供应商", "来料", "批次", "批号", "卷号")):
            out["checklist"].append({
                "scope": "物料横向",
                "action": "追溯「%s」=%s 这一批次在上游工序的留存样品与同批流向，"
                          "确认问题是否同源并锁定影响范围"
                          % (r["dim"], r["worst_level"]),
                "basis": "该维度 η²=%.3f" % r["eta_sq"],
            })
    return out


# ------------------------------------------------------------------ 永久措施

PERMANENT_BY_VERDICT = {
    "本工序主导": [
        "把已识别的本工序关键参数纳入控制计划并按 SPC 纳管，重点是触发判异的那一项",
        "若人机料法中与该项相关的设定可固化（工艺卡 / 配方版本），则写死并加锁",
    ],
    "上游不合格流入": [
        "在上游增设拦截点（SPC 或全检），并把越限批次纳入追溯；只靠本工序筛选成本更高且必然漏检",
        "与上游约定该参数的出厂规格，写进双方的技术协议——这是能长期止血的唯一办法",
    ],
    "上游合格但边缘（本工序窗口被吃掉）": [
        "与上游协商收严该参数的内控窗口（注意：不是收严出厂规格，是内控），并约定异常时的通报机制",
        "提高本工序对该来料波动的容忍度（例如放宽配方的敏感辅料、增加工艺补偿环节）",
        "两边同时动的方案要分开做试验，否则无法分离各自的贡献",
    ],
    "混合作用（两者叠加）": [
        "必须先分离贡献：做一次分组试验或正交 DOE，把上游因子与本工序因子放进同一张表",
        "在贡献分离之前不要急着改参数——混合归因下的调整常常改错地方",
    ],
    "上游带入嫌疑高": [
        "先补上游的完整批次数据再定性（当前证据只到'相关'，还没到'越限'）",
    ],
}


def permanent_actions(trace_result, defect=None, current_process=None, param_key=None):
    """
    永久措施候选。

    **这里返回的每一条都是候选，不是结论。** 是否真的有效要经过小批验证或 DOE。
    来自改善措施库的条目会带上实测证据；库里没有匹配的，如实说明并要求自行设计。
    """
    out = {"from_library": [], "from_knowledge_save": [], "generic": [], "notes": []}
    verdict = (trace_result or {}).get("verdict")

    # 1) 改善措施库：历史已经验证过的对策，这才是最值钱的部分
    try:
        cases = KBS.find_cases(defect=defect, param_key=param_key) or []
    except Exception:
        cases = []
    for c in (cases or []):
        cm = c.get("countermeasure") or {}
        res = ((c.get("verification") or {}).get("result") or {})
        out["from_library"].append({
            "action": cm.get("action"),
            "type": cm.get("type"),
            "detail": cm.get("detail"),
            "evidence_level": c.get("evidence_level"),
            "evidence": "%s %s → %s %s（n=%s，%s）" % (
                res.get("metric", ""), res.get("before"), res.get("after"),
                res.get("unit", ""), res.get("sample_size"), res.get("period", ""))
            if res else "未提供量化结果",
            "standardization": c.get("standardization"),
            "recommend": bool(c.get("can_recommend")),
        })
    if not cases:
        out["notes"].append("改善措施库中没有「%s」的历史验证对策，本次只能给出方向性候选，"
                            "**必须自行设计验证方案**" % (defect or "该缺陷"))

    # 2) 按归属结论给出方向性建议（明说是方向，不是措施）
    for line in PERMANENT_BY_VERDICT.get(verdict, []):
        out["generic"].append({"verdict": verdict, "action": line})

    # 3) 由本次数据的归因结果指向的具体作用点
    contribs = (trace_result or {}).get("contributions") or []
    for c in contribs[:3]:
        cr = c.get("corr") or {}
        if abs(cr.get("r") or 0) >= 0.4 and S.is_sig(cr.get("p")):
            out["generic"].append({
                "verdict": verdict,
                "action": "优先锁定「%s」（%s）作为永久措施的作用点：先做单因子小批验证，"
                          "确认能复现改善后再固化" % (c["column"],
                                                "本工序" if c["role"] == "current" else "上游"),
                "basis": "r=%+.3f, p=%.4g" % (cr["r"], cr["p"]),
            })
    return out


# ------------------------------------------------------------------ 预防

PREVENT_TEMPLATE = [
    "参数窗口：把下面这些参数的目标窗口写成工艺卡里的**内控线**（比出厂规格严），"
    "并约定越界即停机确认，而不是等到检验发现",
    "来料门限：对上游来料的关键指标设确认门限，超过即退回或降级使用",
    "监控强化：纳入 SPC 的不仅仅是不良率本身，更是「前兆参数」——"
    "等到不良率变差再动手，已经产生了一整批废件",
]


def preventive(trace_result, table=None, target_col=None, kb=None, process=None):
    """
    预防性建议：落到具体参数、具体方向、具体监控方式。

    逻辑：把判定为"有区分力"的参数（含上游），按其均值位势给出收严/放宽方向，
    并区分"要纳管的新参数"与"只需调整现有窗口的参数"。
    """
    out = {"params": [], "monitoring": [], "templates": PREVENT_TEMPLATE, "notes": []}
    contribs = (trace_result or {}).get("contributions") or []
    hit = [c for c in contribs
           if (c.get("corr") or {}).get("r") is not None
           and abs(c["corr"]["r"]) >= 0.4 and S.is_sig((c["corr"] or {}).get("p"))]
    for c in hit[:5]:
        probe = c.get("probe") or {}
        direction = "偏低" if (probe.get("delta") or 0) < 0 else "偏高"
        out["params"].append({
            "column": c["column"], "role": c["role"],
            "param_process": c.get("param_process"),
            "r": c["corr"]["r"], "p": c["corr"]["p"],
            "suggestion": "把「%s」纳入该工序的日常监控并设内控线；坏件一侧靠%s，"
                          "窗口应向不敏感的一侧收" % (c["column"], direction),
        })
    if not hit:
        out["notes"].append("本次没有找到与目标显著相关的参数，无法给出具体的预防窗口——"
                            "这通常意味着关键变量没有被记录（环境、换型、人员、来料批次），"
                            "先补这些数据比调参数更有用")

    for p in (trace_result or {}).get("upstream_probes") or []:
        if p.get("verdict") in ("upstream_marginal", "upstream_violating", "no_spec_cannot_clear"):
            out["monitoring"].append({
                "column": p["param"],
                "reason": p.get("reason"),
                "action": "对该上游参数设**来料门限**并逐批记录，不要只在本工序看结果指标",
            })
    return out
