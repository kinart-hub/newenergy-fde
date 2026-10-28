# -*- coding: utf-8 -*-
"""结构化行动卡片：把 D5 永久对策 + D6 验证方案合并成可分发、可回填、可导出的
统一"验证任务"对象（评审 P2-1）。

为什么要有这一层
----------------
8D 的 D5（永久对策候选）与 D6（验证方案）在 lib/eightd.py 里已经是结构化 dict，
但它们各自为政：D5 讲"做什么、谁来批"，D6 讲"怎么验证、达到什么算成功"。
现场要把这两样拼成一张能贴在白板上、能分给不同负责人、能从 MES/工单回填结果的
卡片，只能手工抄。一旦抄，字段就会漏、口径就会漂。

本模块只做一件事：把散在 result 里的 D5/D6 组装成**一张张独立的行动卡片**，
每张一段可执行验证任务，字段齐全到"拿给一个没读过报告的人也能照做"。

与 8D 纪律一致的两条铁律
------------------------
1. **不知道的字段绝不编**。负责人角色、验证窗口这类"取决于你们厂里谁在岗、
   什么时候排产"的信息，工具不可能知道。不知道就写 None / 标「待填」，
   让负责人在卡片上现场填；编一个假名字（"质量经理"）反而会让现场以为
   已经指派过了，这是最危险的失实。
2. **设定值不替人拍**。D5 明确"不给参数设定值，由 DOE 待验证方案给"。卡片里
   "建议取值/范围"这一栏，有 DOE 结论就填 DOE 结论，没有就写"见报告第 10 节 DOE"，
   不自己拍一个数。

导出：JSON 给 MES/工单系统对接，CSV 给现场打印后在白板上回填。
"""

from __future__ import unicode_literals

import json
import csv
import io
import os

# 卡片字段顺序：固定，导出与渲染都按这个顺序，字段名即 MES 对接的键
CARD_FIELDS = [
    "id",           # 卡片序号，跨导出稳定
    "stage",        # D5 / D6（对应对策阶段还是验证阶段）
    "category",     # 对策类别 / 验证类别
    "target",       # 待验证参数 / 作用点
    "objective",    # 一句话说清这张卡要干什么
    "suggested_value",   # 建议取值/范围（有 DOE 结论才填，否则回指报告）
    "verification_batches",  # 验证批次建议（来自 MDD 表，样本量是产能决定的）
    "success_criteria",      # 成功判据（达成什么算这张卡做完）
    "owner_role",     # 负责人角色（待填：工具不替工厂指派到人）
    "verification_window",   # 验证窗口/截止（待填：排产决定的）
    "side_effects",   # 副作用观察项（按下葫芦浮起瓢的对照）
    "evidence",       # 这条卡片的依据（数据 r/p、案例出处等）
    "signed",         # 是否已人工确审
]

# D5 里每个 category 对应的一个可读"目标"标签，避免直接复用 cat（英文/缩写）
_CAT_LABEL = {
    "前置条件": "根因确认",
    "param": "参数",
    "material": "来料",
    "method": "方法/作业",
    "equipment": "设备",
    "man": "人员/培训",
    "environment": "环境",
    "measurement": "测量系统",
    "unknown": "根因/作用点",
}


def build(res, lead=None):
    """把 result 里的 D5/D6 组装成行动卡片列表。

    返回 {"cards": [...], "n", "notes"}。每张卡片是 CARD_FIELDS 顺序的 dict。
    lead 可以为空——D4 未确认根因时，D5 本来就该是"前置条件"卡。
    """
    cards = []
    notes = []

    d5 = None
    d6 = None
    try:
        from . import eightd as EV8D
        if EV8D is not None:
            try:
                d5 = EV8D.derive_d5(res, lead=lead)
            except Exception:
                d5 = None
            try:
                d6 = EV8D.derive_d6(res, lead=lead)
            except Exception:
                d6 = None
    except Exception:
        d5 = d6 = None

    if d5 is None and d6 is None:
        notes.append("无法从 D5/D6 生成行动卡片：8D 引擎未装载或该环节未产出。")
        return {"cards": [], "n": 0, "notes": notes}

    # ---- D6 先建一张"验证方案"卡（它是最该发给现场做的那张）----
    if d6:
        d6_cards = _d6_cards(d6, res)
        cards.extend(d6_cards)
    else:
        notes.append("D6 验证方案未产出，行动卡片只有 D5 方向性条目。")

    # ---- D5 每行一张"对策方向"卡 ----
    for i, r in enumerate((d5 or {}).get("rows") or []):
        cat = r.get("category") or "unknown"
        cards.append({
            "id": "D5-%02d" % (i + 1),
            "stage": "D5",
            "category": cat,
            "target": r.get("target") or "—",
            "objective": r.get("action") or "",
            "suggested_value": None,       # D5 纪律：不给设定值
            "verification_batches": None,
            "success_criteria": r.get("verify_by") or "小批验证 / DOE",
            "owner_role": r.get("approver") or None,   # D5 已有 approver 角色，可直接用
            "verification_window": None,
            "side_effects": None,
            "evidence": None,
            "signed": bool(r.get("signed")),
        })

    notes.append("行动卡片是**待验证任务**，不是已验证措施：字段里标「待填」的（负责人、"
                 "验证窗口）由现场排产后回填，工具不替工厂指派到人。")
    return {"cards": cards, "n": len(cards), "notes": notes}


def _d6_cards(d6, res):
    """把 D6 验证方案展开成一张（或多张）卡。

    验证批次（样本量）不替人拍——MDD 表按 10/30/100 三个档给出"要检出多少改善
    需要多少样本"，现场按产能选。这里原样照抄，不写死一个数。
    """
    out = []
    mdd = d6.get("mdd_table") or []
    metric = d6.get("metric") or "目标指标"
    batches = None
    if mdd:
        # 用"每档样本量 + 对应最小可检出改善"组合成可读的验证批次建议
        batches = [
            "每组 n=%d，可检出 Δ≥%.4g" % (r.get("n_per_group"), r.get("mdd", 0))
            for r in mdd
        ]
    r3 = d6.get("rule_of_three") or []
    if r3:
        r3txt = [
            "不良率降到 %.4g%% 需连续 %d 件无不良" % (r.get("target_rate_pct"),
                                                      r.get("n_needed_zero_defect"))
            for r in r3
        ]
        if batches is None:
            batches = []
        batches.extend(r3txt)

    out.append({
        "id": "D6-01",
        "stage": "D6",
        "category": "验证方案",
        "target": metric,
        "objective": "按下方成功判据，用足够样本验证永久对策是否真的改善「%s」" % metric,
        "suggested_value": None,     # 设定值由 DOE 给，见报告第 10 节
        "verification_batches": batches,
        "success_criteria": "; ".join(d6.get("criteria") or []),
        "owner_role": None,          # 待填：谁执行验证由现场排产定
        "verification_window": None,
        "side_effects": d6.get("side_effects"),
        "evidence": ("σ_within=%.4g" % d6["sigma_within"]
                     if d6.get("sigma_within") else None),
        "signed": False,
    })
    return out


def to_rows(cards):
    """把卡片列表转成 CARD_FIELDS 对齐的行（导出用）。None → 空串。"""
    rows = []
    for c in cards:
        row = []
        for f in CARD_FIELDS:
            v = c.get(f)
            if isinstance(v, (list, tuple)):
                v = "；".join(str(x) for x in v) if v else ""
            elif v is None:
                v = "待填" if f in ("owner_role", "verification_window", "suggested_value") else ""
            row.append(v)
        rows.append(row)
    return rows


def export(cards, path, fmt=None):
    """导出卡片到 JSON（MES 对接）或 CSV（打印回填）。

    fmt: "json" / "csv"；不给就从扩展名推断。返回 (写出的路径, 行数)。
    任何一步失败都抛出，由调用方决定降级——不许静默产出空文件。
    """
    if not fmt:
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        fmt = ext or "json"
    fmt = (fmt or "json").lower()
    n = len(cards)
    if fmt == "csv":
        with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(CARD_FIELDS)
            for row in to_rows(cards):
                w.writerow(row)
    else:
        payload = {"schema": "action_card_v1", "fields": CARD_FIELDS,
                   "cards": [dict(c) for c in cards], "n": n}
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    return path, n
