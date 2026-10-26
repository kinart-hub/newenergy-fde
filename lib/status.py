# -*- coding: utf-8 -*-
"""status.py —— 全库统一的分析状态语义（审计报告 P0-3 / P1-5）

为什么必须单独出一个模块：
    此前每个模块各写各的状态字面量：pipeline 写 "FAILED"，rootcause 写
    "已测"/"数据缺失"，eightd 写 "待验证"，crossprocess 写 "insufficient"，
    spc / doe / views / quality_guard 干脆不返回状态。

    后果不是"不整齐"，而是**下游无法判断一条结论能不能信**：
    「没返回 status」和「执行成功但没找到问题」在汇总时被同样对待，
    于是「算崩了」悄悄变成「这一节没内容」——最难发现的那种失真。
    外部审计报告把这条列为验收门槛第 3 项。

设计要点：
    1. 状态只有这七个。宁可新增也不能在模块里私造字符串——
       私造的字符串不会出现在 anybody 的判据里。
    2. 「允许宣称无异常」的状态只有一个：NO_FINDING。
       SUCCESS（成功且发现了东西）、PARTIAL_SUCCESS（部分环节没跑）都不允许。
       这条是整个模块存在的理由。
    3. 历史字面量走 normalize() 映射，映射不上按 FAILED 处理。
       宁可把一个不明不白的状态当成失败，也不能当成成功——
       方向反了，害的方向是让人以为查过了。
"""

SUCCESS = "SUCCESS"                     # 分析完成且有结论
NO_FINDING = "NO_FINDING"               # 分析完成，按既定规则未发现异常
PARTIAL_SUCCESS = "PARTIAL_SUCCESS"     # 部分完成且已有部分结论（不得与 SUCCESS 混淆）
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # 数据不足以支撑结论
NOT_APPLICABLE = "NOT_APPLICABLE"       # 该分析方法不适用于这份数据
SKIPPED = "SKIPPED"                     # 被明确跳过（配置关闭/前置未满足）
FAILED = "FAILED"                       # 执行失败

ANALYSIS_STATES = (SUCCESS, NO_FINDING, PARTIAL_SUCCESS, INSUFFICIENT_DATA,
                   NOT_APPLICABLE, SKIPPED, FAILED)

# 只有这个集合里的状态，报告才允许写"未发现异常"。
# 其余一律要求显式披露——含 PARTIAL_SUCCESS：跑了一半的"没问题"最危险。
_NO_FINDING_CLAIMABLE = frozenset([NO_FINDING])

# 这类状态必须在报告里点名：哪个环节没产出结论，为什么。
_MUST_DISCLOSE = frozenset([FAILED, INSUFFICIENT_DATA, SKIPPED, NOT_APPLICABLE,
                            PARTIAL_SUCCESS])

_DESC = {
    SUCCESS: "已分析并得到结论",
    NO_FINDING: "已分析，按既定规则未发现异常",
    PARTIAL_SUCCESS: "部分环节完成（有环节未产出结论，结论不完整）",
    INSUFFICIENT_DATA: "数据不足，不足以支撑结论",
    NOT_APPLICABLE: "该分析方法不适用于这份数据",
    SKIPPED: "本轮被跳过（未执行）",
    FAILED: "执行失败（未产出结论）",
}

# 历史散写字面量 → 统一枚态。只收"确实在表达分析模块状态"的那些：
# 8D 的「待验证/待执行/待确认」表达的是**核查动作的完成度**，不是分析成败，
# 混进来会让"D4 还没验证"被读成"分析失败了"，方向完全不同，故不入此表。
_LEGACY = {
    "已测": SUCCESS,
    "已分析": SUCCESS,
    "ok": SUCCESS,
    "OK": SUCCESS,
    "success": SUCCESS,
    "no_finding": NO_FINDING,
    "no_actionable_cause": NO_FINDING,   # quality_guard 的历史写法
    "无可行动工艺根因": NO_FINDING,
    "未发现异常": NO_FINDING,
    "数据缺失": INSUFFICIENT_DATA,
    "insufficient": INSUFFICIENT_DATA,
    "insufficient_data": INSUFFICIENT_DATA,
    "样本不足": INSUFFICIENT_DATA,
    "failed": FAILED,
    "error": FAILED,
    "异常": FAILED,
    "skipped": SKIPPED,
    "not_applicable": NOT_APPLICABLE,
    "partial": PARTIAL_SUCCESS,
}


def normalize(raw, default=FAILED):
    """把模块里现存的各种写法收成统一枚举。

    认不出来的按 default（默认 FAILED）处理，并记进降级台账：
    一个说不清自己状态的分析结果，最诚实的归类就是"没能证明自己成功"。
    """
    s = (raw or "").strip() if isinstance(raw, str) else raw
    if s in _LEGACY:
        return _LEGACY[s]
    if s in ANALYSIS_STATES:
        return s
    try:
        from . import degrade as DG
        DG.note("status.normalize", "无法归类的状态值 %r，按 %s 处理" % (raw, default))
    except Exception:
        pass                             # scan:keep 台账模块本身不可用时不能反向拖垮状态判定
    return default


def may_claim_no_finding(status):
    """报告能否就这个环节写"未发现异常"。

    这是 P0-3 那条硬要求的唯一落地点：把它判成 True 的途径只有一个，
    就是模块确实返回了 NO_FINDING。缺状态、算崩了、被跳过，一律 False。
    """
    return status in _NO_FINDING_CLAIMABLE


def must_disclose(status):
    """该环节是否必须在报告里显式说明"没产出结论"（不能留成空章节）。"""
    return status in _MUST_DISCLOSE


def describe(status):
    return _DESC.get(status, "未知状态（%s）" % status)


def is_valid(status):
    return status in ANALYSIS_STATES


def frame(status, method="", params=None, n_effective=None, input_digest=None,
          warnings=None, errors=None, **rest):
    """生成统一结果骨架（P0-3 验收：状态+输入摘要+有效样本数+警告+错误+方法+参数）。

    返回值直接 `.update(业务字段)` 即可，避免每个模块各凑一套键名。
    参数取 None 表示"本环节没这个概念"，不填 0 充数——0 是会被当成真值的。
    """
    st = normalize(status)
    warns = list(warnings or [])
    errs = list(errors or [])

    # n_effective 必须校验，否则它是装饰而非判据。
    # 实测：frame(SUCCESS, n_effective=-1 / "abc" / 1e18) 三个都返回 status=SUCCESS、
    # errors 为空——一个声称成功、却连样本量都没看懂的结果，比报错更危险。
    n_eff = n_effective
    if n_effective is not None:
        try:
            n_val = int(n_effective)
        except (TypeError, ValueError):
            n_val = None
        if n_val is None or n_val < 0:
            st = normalize(FAILED)
            n_eff = n_effective
            errs.append("n_effective 非法（%r）：必须是非负整数，该次状态改判 FAILED"
                        % (n_effective,))
        elif n_val == 0:
            n_eff = 0
            # 声称成功却一个有效样本都没有，是内部矛盾；改成明确说"没有数据"，
            # 而不是给出一个带着 SUCCESS 标签的空结论。
            if st == SUCCESS:
                st = INSUFFICIENT_DATA
            warns.append("有效样本数为 0：本环节没有可用于下结论的数据，"
                         "状态已按需保护为 %s" % describe(st))
        else:
            n_eff = n_val

    out = {
        "status": st,
        "status_text": describe(st),
        "may_claim_no_finding": may_claim_no_finding(st),
        "must_disclose": must_disclose(st),
        "method": method,
        "params": dict(params or {}),
        "n_effective": n_eff,
        "input_digest": dict(input_digest or {}),
        "warnings": warns,
        "errors": errs,
    }
    out.update(rest)
    return out


def summarize(sections):
    """把各环节的 {"section":..., "status":...} 汇总成可对外披露的两句话界线。

    返回 dict：
      n_disclosed —— 必须点名"未产出结论"的环节数
      bad_lines   —— 给报告用的披露文案（没有则为空列表）
    目的是让"这一节为什么是空的"由机器生成，不再靠写报告的人自觉。
    """
    disclosed = []
    for s in (sections or []):
        sec = s.get("section") or s.get("name") or "(未命名环节)"
        raw = s.get("status")
        # 先识别"私造的状态值"再归一化。顺序不能反：
        # 一个模块返回 "算了一半" 这种自造字符串，normalize 会安静地把它收成
        # FAILED，于是"这个模块在自造状态"这件事就没有人会知道——
        # 而私造状态正是 P0-3 要消灭的根因。必须在披露里点名。
        if raw is not None and not is_valid(raw):
            disclosed.append("%s：返回了不在统一枚举内的状态值 %r，按失败计"
                             % (sec, raw))
        st = normalize(raw, default=FAILED)
        if must_disclose(st):
            why = (s.get("errors") or s.get("warnings") or [])
            reason = why[0] if why else describe(st)
            disclosed.append("%s：%s（%s）" % (sec, describe(st), reason))
    return {
        "n_disclosed": len(disclosed),
        "bad_lines": disclosed,
        # 有没有任何一个环节够格说"没问题"——只有全部 NO_FINDING 才算
        "all_clean": bool(sections) and all(
            normalize(s.get("status"), default=FAILED) == NO_FINDING
            for s in sections),
    }
