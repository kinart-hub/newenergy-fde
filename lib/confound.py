# -*- coding: utf-8 -*-
"""confound.py —— 混杂因素检查（审计报告 P1-6）

报告原文：排查时至少考虑批次、设备、班次、产品型号、原材料批号、供应商、
操作员、环境、时间趋势、工单/配方版本等可能的混杂因素。按数据条件使用
分层、分组比较、回归/协变量控制或敏感性分析，并明确说明方法限制。

为什么必须有这个模块：
    此前"某个参数与目标强相关 → 列入候选根因"是一步到位的。
    但现场数据几乎总是嵌套的：不同批次用不同设备、不同班次用不同原料批号。
    这时候 r=0.8 很可能只是"批次 A 恰好同时具备高 X 和高 Y"，
    X 从未影响过 Y。照着去调 X，产线一动不动，人还会怀疑是自己的操作问题。
    报告把"相关性冒充因果"列为硬约束之一，这是本模块的唯一目的。

三道检查，成本由低到高，结论强度也随之：
    1. 分组比较：目标量在混杂列各水平间是否有显著差异（ANOVA）
       —— 只说明"这个因子确实和目标有关"，是混杂的必要条件不是充分条件。
    2. 分层稳定性：把数据按混杂列切开，看每一层内部 X→Y 的关系还成不成立，
       各层符号是否一致。层内关系普遍消失 ⇒ 原相关很可能是混杂造成的。
       符号翻转 ⇒ Simpson 悖论，必须显式告警。
    3. 协变量控制：Y ~ X 对比 Y ~ X + 混杂哑变量，看 X 的效应变化多少。
       变化超过阈值 ⇒ 这条结论对该混杂敏感，必须写明。

方法局限（报告要求"明确说明方法限制"），一律写进每条结论里：
    - 观察性数据，不能确立因果，只能排除/提示混杂
    - 层内样本不足时分层的 r 不可靠
    - 哑变量回归对层内样本量同样敏感
    - 只能检查"数据里带了这一列"的混杂；没采集的因素无从检查，
      这一条必须让使用者知道，否则"没检出混杂"会被读成"没有混杂"。
"""

from . import stats as S

# 混杂列的识别按"字段名含义"而不是按行业写死列表：
# 关键词表是**流程角色**（批次/设备/班次/供应商...），报告点名的类别就这些，
# 具体哪些中文字段名属于哪一类由调用方的列名决定，本模块不替任何行业
# 编造字段。
_ROLE_KEYWORDS = (
    ("批次", ("批次", "批号", "batch", "lot", "Lot")),
    ("设备机台", ("设备", "机台", "machine", "device", "equip", "机号", "柜号",
                "腔体", "腔", "线体", "产线", "工位")),
    ("班次班组", ("班次", "班组", "班别", "shift", "group", "小组")),
    ("产品型号", ("型号", "规格型号", "产品型号", "品类", "model", "sku")),
    ("物料来源", ("供应商", "原料批号", "material", "supplier", "vendor",
                "来料批次", "牌号")),
    ("人员", ("操作员", "操作工", "作业员", "人员", "工号", "operator")),
    ("时间", ("日期", "时间", "timestamp", "date", "time", "生产日期")),
    ("工单版本", ("工单", "配方", "版本", "订单", "recipe", "version", "wo")),
    ("环境", ("环境", "温湿度", "室温", "湿度", "env", "cleanroom", "洁净度")),
    ("工艺位置", ("工序", "站点", "站别", "stage", "step", "pass")),
)

_MAX_LEVELS = 24          # 超过这么多水平就当连续/高基数列，不做分层（层内必然没样本）
_MIN_LEVEL_N = 3          # 每层至少这么多点，层内相关系数才有意义
_EFFECT_CHANGE_ALERT = 0.30   # 控制混杂后效应变化超过 30% 视为敏感


def role_of(col_name):
    """判断一列的混杂角色；不属于任何已知角色返回 None。"""
    n = (col_name or "").strip()
    if not n:
        return None
    low = n.lower()
    for role, kws in _ROLE_KEYWORDS:
        for k in kws:
            if k.lower() in low:
                return role
    return None


def candidate_columns(table, exclude=None, max_levels=_MAX_LEVELS):
    """从表里挑出可能充当混杂因素的列。

    判据是"取值个数适中"（既不是每行一个值的流水号，也不是只有一个值的常量），
    加上"列名落在已知角色里"。两者都要满足：只看基数会把 concatenate
    参数列也挑进来，只看名字会挑到全空的列。
    """
    exclude = set(exclude or [])
    out = []
    try:
        names = list(table.columns)
    except Exception:
        return out
    for nm in names:
        if nm in exclude:
            continue
        try:
            vals = table.column(nm)
        except Exception:
            continue
        vs = [v for v in (vals or []) if v not in (None, "")]
        if not vs:
            continue
        uniq = set(str(v) for v in vs)
        if len(uniq) < 2 or len(uniq) > max_levels:
            continue
        role = role_of(nm)
        if role is None:
            continue
        out.append({"column": nm, "role": role, "n_levels": len(uniq)})
    return out


def _groups_by(table, col, y):
    """按 col 分组取 y，返回 {水平: [数值]}。"""
    try:
        keys = table.column(col)
    except Exception:
        return {}
    g = {}
    for k, v in zip(keys or [], y or []):
        if not S.is_num(v):
            continue
        g.setdefault(str(k), []).append(float(v))
    return g


def stratified_relation(table, param_col, target_col, by):
    """按 by 分层，返回每层内部 param→target 的相关。

    这是三道检查里最能说明问题的一道：整体相关很强、但每层内部都接近 0，
    说明两个量的共变来自"层"而不是来自参数本身。
    """
    layers = []
    try:
        x = table.numeric_column(param_col) or []
        y = table.numeric_column(target_col) or []
        keys = table.column(by) or []
    except Exception:
        return layers, {"note": "取列失败，无法分层"}
    rows = [[k, a, b] for k, a, b in zip(keys, x, y)
            if k is not None and S.is_num(a) and S.is_num(b)]
    g = {}
    for k, a, b in rows:
        g.setdefault(str(k), ([], []))
        g[str(k)][0].append(float(a))
        g[str(k)][1].append(float(b))
    for level in sorted(g):
        xs, ys = g[level]
        if len(xs) < _MIN_LEVEL_N:
            layers.append({"level": level, "n": len(xs), "r": None,
                           "note": "层内样本 < %d，相关系数不作数" % _MIN_LEVEL_N})
            continue
        r, p, n = S.pearson(xs, ys)
        layers.append({"level": level, "n": n, "r": r,
                       "p": (round(p, 4) if p is not None else None)})
    return layers, {}


def _effect_change(table, param_col, target_col, by):
    """协变量控制：对比 Y~X 与 Y~X+dummy(混杂) 下 X 效应的变化比例。

    线性模型在这里只用于"变了多少"的敏感度分析，不用于预测：
    报告同等要求"明确方法限制"，这里是纯粹的协变量调整提示。
    """
    try:
        x = [float(v) for v in (table.numeric_column(param_col) or []) if S.is_num(v)]
        y = [float(v) for v in (table.numeric_column(target_col) or []) if S.is_num(v)]
        keys = [str(k) for k in (table.column(by) or [])]
    except Exception:
        return None, "取列失败"
    n = min(len(x), len(y), len(keys))
    if n < _MIN_LEVEL_N * 3:
        return None, "样本不足（%d 行），不做协变量控制" % n
    x, y, keys = x[:n], y[:n], keys[:n]
    levels = sorted(set(keys))
    if len(levels) < 2 or len(levels) > _MAX_LEVELS:
        return None, "水平数不适合作哑变量回归（%d 个）" % len(levels)
    try:
        base = levels[0]
        X0 = [[xx] for xx in x]
        m0 = S.linear_regression(X0, y)
        b0 = (m0.get("coefs") or [None])[0]
        X1 = []
        for xx, k in zip(x, keys):
            row = [xx] + [1.0 if k == lv else 0.0 for lv in levels[1:]]
            X1.append(row)
        m1 = S.linear_regression(X1, y)
        b1 = (m1.get("coefs") or [None])[0]
    except Exception as exc:
        return None, "回归失败：%s" % exc
    if b0 is None or b1 is None:
        return None, "系数不可解（可能奇异）"
    if abs(b0) < 1e-12:
        return None, "原效应接近 0，无从比较"
    return (abs(b1 - b0) / abs(b0)), None


def check(table, param_col, target_col, exclude=None, alpha=0.05):
    """对 (param_col → target_col) 这对关系做混杂检查。

    返回 dict：
      confounders —— 逐列的检查结论
      echo_limit  —— 必须向使用者交代的方法限制
      technique   —— 本次实际用上了哪些手段
      has_data_limit —— 有没有某列因数据不足没查成（没查成 ≠ 查过了）
    """
    res = {"confounders": [], "technique": [], "limitations": [],
           "has_data_limit": False}
    if not param_col or not target_col:
        res["limitations"].append("缺少参数列或目标列，无法做混杂检查")
        return res

    try:
        y = [v for v in (table.numeric_column(target_col) or [])]
    except Exception:
        res["limitations"].append("目标列读取失败，无法做混杂检查")
        return res

    cands = candidate_columns(table, exclude=set(exclude or []) |
                              {param_col, target_col})
    res["technique"].append("分组比较（ANOVA）")
    if not cands:
        res["limitations"].append(
            "数据里没有识别到批次/设备/班次/型号/供应商/操作员/时间等分组列，"
            "**本次无法排除未采集的混杂因素**——『没有检出混杂』不等于『不存在混杂』。")
        return res

    for cd in cands:
        col, role = cd["column"], cd["role"]
        groups = _groups_by(table, col, y)
        usable = {k: v for k, v in groups.items() if len(v) >= 2}
        item = {"column": col, "role": role, "n_levels": cd["n_levels"],
                "level_counts": {k: len(v) for k, v in
                                 sorted(groups.items())[:12]}}

        # ① 目标量在该列水平间是否有差异
        if len(usable) >= 2:
            try:
                aov = S.anova_oneway([usable[k] for k in sorted(usable)])
                p = aov.get("p") if isinstance(aov, dict) else None
            except Exception:
                p = None
            item["target_diff_p"] = p
            item["target_diff"] = ("显著" if (p is not None and p < alpha)
                                   else ("不显著" if p is not None else "无法检验"))
        else:
            item["target_diff_p"] = None
            item["target_diff"] = "水平样本不足，未检验"
            item["data_limit"] = True
            res["has_data_limit"] = True

        # ② 分层稳定性
        layers, note = stratified_relation(table, param_col, target_col, col)
        item["stratified"] = layers
        rs = [l["r"] for l in layers if l.get("r") is not None]
        if rs:
            item["n_layers_testable"] = len(rs)
            item["r_min"], item["r_max"] = round(min(rs), 3), round(max(rs), 3)
            signs = set(1 if r >= 0 else -1 for r in rs)
            item["simpson_risk"] = (len(signs) > 1 and
                                    max(abs(min(rs)), abs(max(rs))) > 0.15)
            item["vanishes_within_layers"] = (
                max(abs(r) for r in rs) < 0.20)
        else:
            item["n_layers_testable"] = 0
            item["simpson_risk"] = False
            item["vanishes_within_layers"] = False
            item["data_limit"] = True
            res["has_data_limit"] = True

        # ③ 协变量控制后的效应变化
        if item.get("target_diff") == "显著" or item.get("simpson_risk"):
            chg, err = _effect_change(table, param_col, target_col, col)
            item["effect_change"] = round(chg, 3) if chg is not None else None
            item["effect_change_note"] = err
            if chg is not None and chg > _EFFECT_CHANGE_ALERT:
                item["sensitive"] = True
            res["technique"].append("协变量控制（哑变量回归）")

        # 结论口径：只说"观察到的关系提示/未提示"，不说"因果成立/不成立"
        if item.get("simpson_risk"):
            item["verdict"] = ("强警告：不同 %s 下 X→Y 关系方向不一致"
                               "（Simpson 悖论风险），整体相关系数不可直接使用" % role)
        elif item.get("vanishes_within_layers") and \
                item.get("target_diff") == "显著":
            item["verdict"] = ("高度怀疑混杂：%s 与目标显著相关，"
                               "但分层后 X→Y 在各个 %s 内均不明显，"
                               "原相关很可能是共同原因造成的" % (col, role))
        elif item.get("sensitive"):
            item["verdict"] = ("对该混杂敏感：控制 %s 后 X 的效应变化 %.0f%%，"
                               "结论需按 %s 分层复核" % (col, item["effect_change"] * 100, role))
        elif item.get("target_diff") == "显著":
            item["verdict"] = ("该 %s 与目标显著相关，属必须交代的混杂背景；"
                               "当前未见它改变 X→Y 的结论" % role)
        else:
            item["verdict"] = "未见该列对本次关系构成混杂"
        res["confounders"].append(item)

    if len(res["confounders"]) and "协变量控制（哑变量回归）" not in res["technique"]:
        pass
    res["limitations"].append(
        "以上基于观察性数据，只能提示或排除混杂，不能确立因果。"
        "未采集的潜在因素（环境条件、原材料的未记录批次等）无从检查。")
    return res


def summary_lines(res):
    """给报告用的一行式混杂提示。没有可说的就返回空列表——不硬凑结论。"""
    lines = []
    for it in (res.get("confounders") or []):
        if it.get("verdict", "").startswith("未见"):
            continue
        lines.append("混杂检查 · %s（%s）：%s"
                     % (it.get("column"), it.get("role"), it.get("verdict")))
    if res.get("has_data_limit"):
        lines.append("混杂检查：有分组列因层内样本不足未检验，"\
                     "这部分『没查』不能被读成『查过没问题』")
    return lines
