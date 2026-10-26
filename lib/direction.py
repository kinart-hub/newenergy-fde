# -*- coding: utf-8 -*-
"""
direction.py —— 目标指标的「方向」元数据（P0-2）

为什么单独成模块
----------------
"哪一档最差" 这个问题本身没有答案 —— 必须先知道指标是越大越差还是越小越差。

- 缺陷率、EL 不良率、水分、内阻、毛刺高度 → 越大越差
- 良率、效率、容量、首效、绝缘电阻、附着力 → 越大越好
- 温度、压力、速度、间隙 → 有目标窗口，偏离目标即劣化

方向搞反时，"重点核查 X 机台" 会指向全场表现最好的那台。
**这比算不出来危险得多**：算不出来会明说不知道，方向反了给的是自信的错误答案 ——
且它带着 η²、p 值和一整套看起来专业的话术，没人会怀疑方向。

本模块被 correlation / crossprocess / actions 三个消费点共用，
所以必须是不依赖任何其他 lib 模块的叶子，避免循环导入。

设计原则
--------
方向是**产品参数**，不是先验的行业知识 —— 同一名词在不同企业、不同测量口径下
方向可能相反。因此优先级为：

    1. 调用方显式指定（--spec / target_direction）
    2. 知识库 params[*].direction（企业维护，带 source 追溯）
    3. 按通用质量语义词推断（见 _INFER_RULES）
    4. 推断不出 → UNKNOWN，此时**退化为"带假定的建议"**，绝不冒充已确认结论
"""

try:
    from . import degrade          # 包模式
except Exception:                  # 脚本模式无包上下文
    degrade = None                 # noqa

UNKNOWN = "UNKNOWN_DIRECTION"
DEFAULT_ASSUMED = "higher_is_worse"   # 方向彻底未知时的默认假定（必须在结论里声明）

# 六种标准方向 + 归一化用的别名
VALID = (
    "higher_is_worse",     # 越大越差：缺陷率、不良数、水分、内阻
    "lower_is_worse",      # 越小越差：良率、效率、容量
    "higher_is_better",    # 越大越好：与 lower_is_worse 同判定、语义相反
    "lower_is_better",     # 越小越好：与 higher_is_worse 同判定、语义相反
    "target_is_best",      # 有目标值，偏离即劣化：需传 target
    "two_sided_spec",      # 双侧规格，按距规格中心的偏差判定：需传 lsl/usl
)

_ALIASES = {
    "larger_worse": "higher_is_worse", "bigger_worse": "higher_is_worse",
    "max_worse": "higher_is_worse", "higherworse": "higher_is_worse",
    "smaller_worse": "lower_is_worse", "min_worse": "lower_is_worse",
    "larger_better": "higher_is_better", "bigger_better": "higher_is_better",
    "max_better": "higher_is_better",
    "smaller_better": "lower_is_better", "min_better": "lower_is_better",
    "target": "target_is_best", "nominal_is_best": "target_is_best",
    "two_sided": "two_sided_spec", "both_sided": "two_sided_spec",
}

# 通用质量语义词推断表。
#   只放跨行业通用的质量术语，不放具体产品参数 ——
#   具体参数的方向由调用方/知识库给，写在这里就是替用户拍板。
#   (关键词, 方向)
_INFER_RULES = (
    (("缺陷率", "不良率", "废品率", "返修率", "失效率", "报废率",
      "defect_rate", "fail_rate", "reject", "scrap"), "higher_is_worse"),
    (("缺陷数", "不良数", "异物", "黑斑", "隐裂", "碎片", "凹坑",
      "defect_count", "defects"), "higher_is_worse"),
    (("良率", "合格率", "通过率", "优率", "成品率", "直通率",
      "yield", "pass_rate"), "lower_is_worse"),
    (("效率", "转化率", "efficiency"), "lower_is_worse"),
    (("容量", "capacitance", "capacity"), "lower_is_worse"),
    (("burr", "毛刺", "高度", "粗粒度", "roughness"), "higher_is_worse"),
    (("偏差", "偏移", "错位", "纵向错位", "cv", "deviation"), "higher_is_worse"),
)


def normalize(raw):
    """把各种写法归一到标准枚举；认不出返回 None。"""
    if not raw:
        return None
    s = str(raw).strip().lower().replace("-", "_").replace(" ", "")
    if s in VALID:
        return s
    return _ALIASES.get(s)


def infer(col_name):
    """按列名/语义词推断方向。推断不出返回 None（不是 UNKNOWN，交回上层降级）。

    注意：推断只是"没配置时的兜底"，命中率有限。
    **标错方向比不标更危险**，所以这里只收跨行业通用的质量术语。
    """
    if not col_name:
        return None
    n = str(col_name).strip().lower()
    for words, direction in _INFER_RULES:
        for w in words:
            if str(w).lower() in n:
                return direction
    return None


def from_knowledge(col_name, aliases_of_col=None, kb_params=None):
    """从知识库 params 里查这一列的方向。

    kb_params：knowledge/process_specs.json 的 params 段。
    匹配依据是列名本身和它的别名（与企业给的目标列名对齐）。
    """
    if not kb_params or not col_name:
        return None
    key = str(col_name).strip().lower()
    for pk, pv in (kb_params or {}).items():
        if not isinstance(pv, dict):
            continue
        names = {str(pk).lower()}
        for a in (pv.get("aliases") or []):
            names.add(str(a).strip().lower())
        for extra in (aliases_of_col or []):
            names.add(str(extra).strip().lower())
        if key in names:
            d = normalize(pv.get("direction"))
            if d:
                return d
    return None


def resolve(col_name, explicit=None, kb_params=None, warn=True):
    """确定一列的方向。返回 (direction, source, baseplicable)。

    source ∈ {"explicit","knowledge","inferred","none"}
    方向无法确定时返回 (None, "none", False) —— 调用方必须降级措辞。
    """
    d = normalize(explicit)
    if d:
        return d, "explicit", True
    d = from_knowledge(col_name, None, kb_params)
    if d:
        return d, "knowledge", True
    d = infer(col_name)
    if d:
        return d, "inferred", True
    if warn and degrade is not None:
        try:
            degrade.note("direction.resolve",
                         LookupError("目标列「%s」方向未登记，"
                                     "按「越大越差」假定给出并已在结论中标注" % col_name))
        except Exception:
            # scan:keep 台账自身必须静默：记账失败不应反过来打断方向解析——
            # 方向解析在相关性/横向排查的热路径上，宁可少一条台账记录，
            # 也不能让「这条建议是基于什么前提」的判定整个失败。
            # 降级路径是结论里的 assumed 标记，不是崩溃。
            pass
    return None, "none", False


def _deviation(k_mean, direction, target=None, lsl=None, usl=None):
    """把"均值"折算成"不利程度"：值越大越该被挑出来。"""
    m = k_mean
    if direction == "higher_is_worse" or direction == "lower_is_better":
        return m
    if direction in ("lower_is_worse", "higher_is_better"):
        return -m
    if direction == "target_is_best":
        if target is None:
            return None
        return abs(m - float(target))
    if direction == "two_sided_spec":
        if lsl is None or usl is None:
            return None
        center = (float(lsl) + float(usl)) / 2.0
        return abs(m - center)
    return None


def qualifier(source, assumed=None):
    """给结论附的方向前提说明。

    三种来源必须说三句不同的话 —— 合成一句会误导：
    推断(inferred) 出来的方向是**有依据**的（列名里写了"良率"），
    把它说成"未登记"会让使用者过度怀疑；
    而完全未知(assumed) 才是真的踩在假定上，必须说清楚踩了什么。
    """
    if source in ("explicit", "knowledge"):
        return ""
    if source == "inferred":
        return "（方向由指标名称推断，建议人工确认）"
    return "（方向未登记，暂按「%s」假定给出，若实际相反则「最差档」应反过来读）" % (
        assumed or DEFAULT_ASSUMED)


def worst_group(means, direction=None, target=None, lsl=None, usl=None,
                assumed=None):
    """从 {level: mean} 里挑出**最不利**的那一档。

    means： {水平名: 该水平下目标列的均值}
    direction： 标准枚举；None 表示未知

    返回 dict：
      {
        "level": 最差水平 或 None,
        "mean":  它的均值,
        "best_level"/"best_mean": 最优水平,
        "direction": 使用的方向,
        "confident": True/False,     # 方向已知 -> True
        "assumed":   "higher_is_worse" 或 None,   # 方向未知时的假定
        "note":      面向现场读的说明
      }

    方向未知时的处置是本项目最关键的约束：
    **不给确定性结论**。仍然提供候选（否则能力凭空消失），
    但必须带 `assumed` 标记，消费方措辞要降级为"假定/待确认"。
    """
    means = {k: v for k, v in (means or {}).items()
             if v is not None and isinstance(v, (int, float))}
    if not means:
        return {"level": None, "mean": None, "direction": direction or UNKNOWN,
                "confident": False, "assumed": None, "note": "无可用分组均值"}

    used = normalize(direction) or (assumed or DEFAULT_ASSUMED)
    assumed_flag = None if normalize(direction) else used

    scores = {}
    for k, m in means.items():
        d = _deviation(m, used, target=target, lsl=lsl, usl=usl)
        if d is None:
            # 方向需要参数（target/lsl/usl）却没给 —— 这是配置缺口，必须显性出来
            return {"level": None, "mean": None, "direction": used,
                    "confident": False, "assumed": assumed_flag,
                    "note": "方向「%s」需要 %s 才能判定，未提供"
                            % (used, "target 目标值" if used == "target_is_best"
                               else "lsl/usl 规格上下限")}
        scores[k] = d

    wlevel = max(scores, key=lambda k: scores[k])
    blevel = min(scores, key=lambda k: scores[k])
    note = ""
    if not normalize(direction):
        note = ("方向未登记，暂按「%s」假定给出；若该指标实际相反，"
                "下面标注的「最差档」应反过来读。" % used)
    return {
        "level": wlevel, "mean": means[wlevel],
        "best_level": blevel, "best_mean": means[blevel],
        "worst_mean": means[wlevel],
        "direction": normalize(direction) or used,
        "confident": bool(normalize(direction)),
        "assumed": assumed_flag,
        "note": note,
    }
