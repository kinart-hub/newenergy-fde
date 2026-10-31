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
from . import preprocess as PP
try:
    from . import direction as DIR       # 包模式
except Exception:                        # 脚本模式无包上下文
    import direction as DIR              # noqa


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

    # ------------------------------------------------------------------
    # defect / current_process：过去这两个参数**收了但从不读**，
    # 于是"焊接不良"与"面密度超差"拿到的是**一模一样**的横向排查清单
    # ——排查动作不带缺陷语义，现场只能照单全做一遍。
    # 这里用知识库把两条线索接上：
    #   · carry_over_from  → 该缺陷已知会从哪些上游工序带进来（向上游追溯）
    #   · clearing_evidence → 判定"确实清除了"需要哪些证据（否则以为改好了）
    # 取知识库一律走 PP.load_knowledge()：它是分析侧唯一收口，
    # 已叠加租户覆盖与行业作用域；裸读 process_specs.json 会绕过这两层。
    _kb_note = ""
    try:
        _kb = PP.load_knowledge() or {}
        _defs = _kb.get("defect_rootcause") or {}
        _entry = _defs.get(defect) if defect else None
        if defect and _entry is None:
            # 缺陷名可能给的是现场叫法，按 aliases 再找一次
            for _k, _v in _defs.items():
                if defect in list(_v.get("aliases") or []) or defect == _k:
                    _entry, defect = _v, _k
                    break
        if _entry:
            _carry = list(_entry.get("carry_over_from") or [])
            _clear = list(_entry.get("clearing_evidence") or [])
            _entry_proc = _entry.get("process")
            if _carry:
                out["checklist"].append({
                    "scope": "物料横向·上游追溯",
                    "action": "缺陷「%s」已知可从 %s 工序带入：先确认本批上游对应批次，"
                              "再判断问题是否同源（只查本工序会漏掉真正来源）"
                              % (defect, "、".join(_carry)),
                    "basis": "知识库 defect_rootcause[%s].carry_over_from" % defect})
            if _clear:
                out["checklist"].append({
                    "scope": "清除证据",
                    "action": "判定「%s」已清除必须具备：%s"
                              "（缺这些证据就只是'看起来好了'）"
                              % (defect, "、".join(_clear)),
                    "basis": "知识库 defect_rootcause[%s].clearing_evidence" % defect})
            if current_process and _entry_proc and _entry_proc != current_process:
                out["notes"].append(
                    "知识库里「%s」登记的工序是 %s，与当前工序 %s 不一致："
                    "横向排查按当前工序做，但机理条目可能借自另一工序，结论需复核"
                    % (defect, _entry_proc, current_process))
        elif defect:
            _kb_note = ("知识库里没有「%s」的机理条目：横向排查只能按通用路径做，"
                        "建议先补条目或确认缺陷叫法" % defect)
            out["notes"].append(_kb_note)
    except Exception as _e:
        # 知识库不可用不能拖垮横向排查，但必须留痕（静默兜底会让清单悄悄变通用）
        out["notes"].append("知识库读取失败（%s）：本次横向排查未带缺陷语义"
                            % str(_e)[:60])

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
            # 方向来自"登记/指定"才算已确认；推断或假定的必须把前提写进句子里，
            # 否则读的人不知道这个"重点核查"是踩在一个未经核实的假定上的。
            _qual = DIR.qualifier(r.get("direction_source"),
                                  r.get("direction_assumed"))
            out["checklist"].append({
                "scope": "空间横向",
                "action": "按「%s」逐档对比：重点核查 %s，%s%s"
                          % (r["dim"], r["worst_level"], quant, _qual),
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
    # 单文件场景专用。**不能套用"本工序主导"**：那个结论的意思是
    # "看过上游数据、排除了上游"，而单文件时上游数据根本不存在，
    # 硬说"本工序主导"是把"没查"说成"查过没问题"——这是本工具最不能犯的错。
    "仅本工序（未做跨工序追溯）": [
        "把已识别的本工序关键参数纳入控制计划并按 SPC 纳管，重点是触发判异的那一项",
        "若人机料法中与该项相关的设定可固化（工艺卡 / 配方版本），则写死并加锁",
        "**本次只有本工序数据**：上游来料是否同源尚未验证。要区分'本工序造成'与'上游带入'，"
        "必须补上上游工序的检测数据再跑一次跨工序追溯",
    ],
}


def self_trace_from_result(result, target_col=None):
    """把单表分析的结果拼成一份"只有本工序"的追溯输入。

    为什么需要它
    ------------------------------------------------------------------
    `permanent_actions` / `preventive` 的入参原本只有 `crossprocess.trace()`
    的产物，而 trace 要求**至少两张表**。于是只给一份数据时（最常见的用法），
    整块行动建议被跳过 —— 报告里"永久对策""预防措施""横向展开"三节
    从来不出现，使用者以为这个工具只会算 Cpk。

    实测数据：单文件场景下这三块**全部为空**，而双文件场景下永久对策出 5 条。
    同一份数据形态，只因为多给了第二个工序文件，行动建议就从无到有。

    这里用 `strong_pairs`（参数-目标的相关性）构造等价的 contributions，
    让行动建议在单文件下也能出，并且**在 verdict 里明确写"仅本工序"**，
    避免把"没查上游"说成"排除上游"。
    """
    pairs = (result or {}).get("strong_pairs") or []
    if not pairs:
        return None
    contribs = []
    for p in pairs:
        # strong_pairs 是 (a, b) 对；只取"以目标为 b"的那些
        col, tgt = p.get("a"), p.get("b")
        r, pv = p.get("r"), p.get("p")
        if col is None or r is None:
            continue
        if target_col and tgt != target_col:
            continue
        contribs.append({"column": col, "role": "current",
                         "param_process": None,
                         "corr": {"r": r, "p": pv},
                         # 没有对比探针（那要两份数据才有），如实置空
                         "probe": None})
    if not contribs:
        return None
    contribs.sort(key=lambda c: -abs((c.get("corr") or {}).get("r") or 0))
    return {"applicable": False, "self_only": True,
            "verdict": "仅本工序（未做跨工序追溯）",
            "verdict_reason": "只有单道工序的数据，无法区分'本工序造成'与'上游带入'",
            "contributions": contribs,
            "top_param": contribs[0]["column"] if contribs else None,
            "notes": ["本次只给了单道工序的数据：行动建议基于本工序归因，"
                      "上游来源未经验证"]}


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
    # current_process 必须参与匹配：改善措施库里每条案例都带 process，
    # 不按工序筛就会出现"涂布的问题套用焊接的对策"——机理不同、参数不同，
    # 对策照抄过去不但无效还可能引入新问题（与 identify_defects 跨工序串机理
    # 是同一类错误）。同工序优先；库里只有别工序的，明确标 cross_process 并
    # 要求先验证，绝不静默当成同工序已验证的对策推给现场。
    _same, _cross = [], []
    for c in (cases or []):
        (_same if (not current_process or c.get("process") == current_process)
         else _cross).append(c)
    if current_process and _cross and not _same:
        out["notes"].append(
            "改善措施库里没有「%s」工序的历史验证对策；以下 %d 条来自**其它工序**，"
            "机理与参数未必适用，**必须先做小批验证**再考虑固化。"
            % (current_process, len(_cross)))
    elif current_process and _cross:
        out["notes"].append(
            "另有 %d 条其它工序的历史对策未列出（机理不同，不作为本工序措施）。"
            % len(_cross))
    for c in (_same or _cross):
        cm = c.get("countermeasure") or {}
        res = ((c.get("verification") or {}).get("result") or {})
        _is_cross = c in _cross
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
            "recommend": bool(c.get("can_recommend")) and not _is_cross,
            "process": c.get("process"),
            "cross_process": _is_cross,
            # 跨工序借用的对策不进"已验证"口径：recommend 关掉还不够，
            # 必须在同一条记录上写明为什么不能当作已验证。
            "caveat": ("该对策来自其它工序（%s），机理与参数未必适用于本工序，"
                       "须先做单因子小批验证" % (c.get("process") or "未标注工序"))
            if _is_cross else None,
        })
    if not (cases or []):
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
    """预防性建议：落到具体参数、具体方向、具体监控方式。

    逻辑：把判定为"有区分力"的参数（含上游），按其均值位势给出收严/放宽方向，
    并区分"要纳管的新参数"与"只需调整现有窗口的参数"。

    `process` / `kb` / `table` / `target_col` 的用途（原先四个参数收了全不用，
    于是"涂布"和"焊接"拿到的预防建议一模一样）：

      · `process`  —— 定位工序名与工序专属的纳管对象（知识库里该工序的
        典型参数），让建议落到"这张工艺卡"而不是"某个工艺卡"；
      · `kb`      —— 允许调用方传入已加载的知识库（租户覆盖后的版本），
        不传时才自己去读；**必须用调用方给的那份**，否则租户覆盖会被绕过；
      · `table`/`target_col` —— 用于给出"目标列当前处在窗口什么位置"，
        没有这层信息就无法说"该收严还是还有余量"。
    """
    out = {"params": [], "monitoring": [], "templates": list(PREVENT_TEMPLATE),
           "notes": []}

    # 工序上下文：名称 + 该工序在知识库里登记的参数（用于把建议落到具体工艺卡）
    _proc_name, _proc_keys = None, []
    try:
        _k = kb if isinstance(kb, dict) else PP.load_knowledge()
        _pentry = ((_k or {}).get("processes") or {}).get(process) or {}
        _proc_name = _pentry.get("name")
        _proc_keys = list((_pentry.get("params") or []))
    except Exception as _pe:
        out["notes"].append("工序上下文读取失败（%s）：以下建议未落到具体工序" % str(_pe)[:60])
    if process and not _proc_name:
        out["notes"].append(
            "工序 key「%s」在知识库里没有对应条目：预防建议只能给通用形态，"
            "请确认工序名或补该工序的知识条目" % process)
    if _proc_name:
        out["templates"] = [
            "**工序：%s**——下面的窗口与门限请写进该工序的工艺卡/控制计划，"
            "而不是笼统的'加强管理'" % _proc_name] + list(PREVENT_TEMPLATE)

    # 目标列当前位势：决定"该收严"还是"还有余量"
    _tgt_pos = None
    if table is not None and target_col:
        try:
            _vals = [v for v in table.numeric_column(target_col) if v is not None]
            if len(_vals) >= 5:
                _m = S.mean(_vals)
                _sp = getattr(table, "spec_for", None)
                _tgt_pos = {"mean": round(_m, 4), "n": len(_vals)}
                if callable(_sp):
                    _sc = _sp(target_col) or {}
                    if _sc.get("lsl") is not None and _sc.get("usl") is not None:
                        _lo, _hi = _sc["lsl"], _sc["usl"]
                        _tgt_pos["position"] = round((_m - _lo) / float(_hi - _lo), 3)
        except Exception as _te:
            out["notes"].append("目标列位势计算失败（%s）" % str(_te)[:50])
    if _tgt_pos:
        out["target_position"] = _tgt_pos
    contribs = (trace_result or {}).get("contributions") or []
    hit = [c for c in contribs
           if (c.get("corr") or {}).get("r") is not None
           and abs(c["corr"]["r"]) >= 0.4 and S.is_sig((c["corr"] or {}).get("p"))]
    for c in hit[:5]:
        probe = c.get("probe") or {}
        # 方向只能来自对比探针（好件/坏件的均值差）。没有探针时**不许默认一个方向**：
        # 原实现写 `0 < 0` 恒假 → 一律输出"偏高"，等于替现场编了个方向。
        # 单文件场景没有探针，这条分支会经常走到，所以必须诚实置空。
        if probe.get("delta") is None:
            direction = None
        else:
            direction = "偏低" if probe["delta"] < 0 else "偏高"
        if direction:
            sug = ("把「%s」纳入该工序的日常监控并设内控线；坏件一侧靠%s，"
                   "窗口应向不敏感的一侧收" % (c["column"], direction))
        else:
            sug = ("把「%s」纳入该工序的日常监控并设内控线；"
                   "**收严还是放宽需现场确认**——本次没有好件/坏件对比数据，"
                   "不替你定方向（方向搞反会让问题更严重）" % c["column"])
        out["params"].append({
            "column": c["column"], "role": c["role"],
            "param_process": c.get("param_process"),
            "r": c["corr"]["r"], "p": c["corr"]["p"],
            "direction": direction,
            "suggestion": sug,
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
