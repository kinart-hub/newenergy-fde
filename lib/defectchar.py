# -*- coding: utf-8 -*-
"""
defectchar.py —— 缺陷物理表征结果的**接入**与工序关联（不解读图像）

=============================================================================
必须先把边界钉死：工具不读 SEM 图，也不解 EDS 谱
=============================================================================
    SEM/EDS/CT 的判读是**有资质的人 + 设备**做的事：看形貌特征、判元素峰、
    从断层序列重建体积。本工具零依赖、纯统计，**没有也不该有**这种能力。
    任何声称"自动识别 SEM 缺陷类型"的功能，在不接模型的情况下都是编的。

    那这一层做什么？做**实测到的真实瓶颈**：
    实验室出了表征结论，通常是一张表（缺陷编号/类别/尺寸/所在层/元素组成/
    发生工序/机台/批次）。这张表躺在报告里，**没有人把它和生产数据关联起来**。
    于是"这批划伤是刻蚀带来的还是 CMP 带来的"这种问题，只能靠开会猜。

    本模块就把这张表变成可计算的：
      · 缺陷类别的 Pareto（按层/工序/机台拆）
      · "某工序的缺陷构成是否与其它工序不同"的独立性检验（卡方 + 期望频数体检）
      · 表征数据自身的质量体检（类别缺失/命名不一致/元素写成同一串/尺寸异常）
      · 表征结论与"声称的机理"是否对得上（**证据门禁**）

=============================================================================
设计要点
=============================================================================
    1. **输入是结构化结果，不是原始谱图。** 字段由调用方给，工具按列名别名识别，
       识别不到就列表可选列——绝不猜"这一列可能是缺陷类型"。
    2. 卡方检验前必须做**期望频数体检**：期望 <5 的格子超过 20% 时卡方近似失效，
       此时只报描述性占比，不报 p 值（这是最容易出假显著的地方）。
    3. **证据门禁**：如果一条表征结论声称某种机理，但表里没有支持该机理的
       证据字段（元素组成/尺寸/位置/层），就明确标"证据不足以支撑该机理"。
       这比"给个高置信度"有用得多。
    4. 类别命名不一致会直接毁掉 Pareto。工具只做**字面归一化**后的查重
       —— 去掉空格/大小写/标点后相同的（"划伤" / "划伤 " / "划 伤"）会提示。
       **不做语义同义词识别**（"划伤" vs "刮伤" vs "scratch" 不会被自动认成同一类）：
       那需要一份行业词表，而词表每个厂不同，工具现编一份就是编造。
       同义词请在现场用统一的类别字典维护后再传入。
       并且**绝不自动合并**——把真不同的类别并掉，比不合并更糟。

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


# 列名别名（现场叫法差异大，只做归一化匹配）
_ALIAS = {
    "id": ("缺陷编号", "缺陷id", "id", "defect_id", "编号"),
    "class": ("缺陷类别", "缺陷类型", "类别", "类型", "defect_class",
              "defect_type", "class"),
    "size": ("尺寸", "缺陷尺寸", "size", "defect_size", "长度", "直径"),
    "layer": ("层", "所在层", "层级", "膜层", "layer", "level"),
    "step": ("工序", "步骤", "制程", "process", "step", "process_step"),
    "tool": ("机台", "设备", "机台号", "tool", "equipment", "eqp"),
    "lot": ("批次", "批号", "lot", "lot_id"),
    "elements": ("元素", "元素组成", "成分", "eds", "edx", "element",
                 "elements", "composition"),
    "location": ("位置", "缺陷位置", "location", "position", "坐标"),
    "mode": ("表征手段", "手段", "设备类型", "modality", "method",
             "instrument"),
    "mechanism": ("机理", "声称机理", "mechanism", "root_cause", "cause"),
}

# 卡方检验的最小期望频数（Cochran 经验准则的公开口径）
MIN_EXPECTED = 5.0
# 允许的"期望<5 的格子"占比上限（同上准则的宽松版：不超过 20%）
MAX_LOW_EXPECTED_RATIO = 0.20

# 声称机理 → 必须存在的支撑证据字段
MECHANISM_EVIDENCE = {
    "颗粒": ("elements", "size"),
    "污染": ("elements",),
    "划伤": ("size", "location"),
    "残留": ("elements", "location"),
    "空洞": ("size", "location"),
    "裂纹": ("size", "location"),
    "凹陷": ("size", "layer"),
}


def _norm(s):
    if s is None:
        return ""
    t = str(s).strip().lower()
    for ch in (" ", "\t", "_", "-", "（", "）", "(", ")", "：", ":", "%"):
        t = t.replace(ch, "")
    return t


_NORM_ALIAS = {}
for _k, _vs in _ALIAS.items():
    for _v in _vs:
        _NORM_ALIAS[_norm(_v)] = _k


def map_columns(columns):
    """把实际列名映射到标准字段。返回 (mapping, unmapped)。"""
    mapping, unmapped = {}, []
    for c in (columns or []):
        f = _NORM_ALIAS.get(_norm(c))
        if f and f not in mapping:
            mapping[f] = c
        else:
            unmapped.append(c)
    return mapping, unmapped


def _get(row, mapping, field):
    col = mapping.get(field)
    if not col:
        return None
    return row.get(col)


def summarize(rows, columns=None):
    """缺陷表征表的 Pareto 与数据质量体检。

    rows: list[dict]（键为原始列名）
    返回 dict：mapping / pareto / by_step / quality / warnings
    """
    out = {"status": ST.INSUFFICIENT_DATA, "n": len(rows or []),
           "mapping": {}, "unmapped": [], "pareto": [], "by_step": {},
           "quality": {}, "warnings": [],
           "note": "只消费实验室给出的结构化表征结果；**不解读 SEM 图像/EDS 谱**，"
                   "不替代有资质的判读。"}
    if not rows:
        out["warnings"].append("没有表征记录")
        return out
    cols = columns or sorted({k for r in rows for k in r})
    mapping, unmapped = map_columns(cols)
    out["mapping"], out["unmapped"] = mapping, unmapped
    if "class" not in mapping:
        out["warnings"].append(
            "找不到『缺陷类别』列。可选列：%s —— 请确认哪一列是类别"
            "（工具不猜）" % "、".join(sorted(cols)))
        return out
    if "mode" in mapping:
        modes = sorted({str(_get(r, mapping, "mode") or "").strip()
                        for r in rows} - {""})
        out["modalities"] = modes
        if any(m.upper().startswith(("SEM", "EDS", "EDX", "CT")) for m in modes):
            out["note"] += "（检测到 SEM/EDS/CT 表征手段；工具只处理其结论表，不处理原始图像/谱图）"

    # Pareto（按类别）
    counter = {}
    for r in rows:
        k = str(_get(r, mapping, "class") or "").strip()
        if not k:
            counter.setdefault("（未填）", 0)
            counter["（未填）"] += 1
        else:
            counter[k] = counter.get(k, 0) + 1
    total = sum(counter.values())
    pareto = [{"defect_class": k, "n": v, "share": v / total if total else None}
              for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]
    out["pareto"] = pareto

    # 按工序拆
    if "step" in mapping:
        by_step = {}
        for r in rows:
            step = str(_get(r, mapping, "step") or "（未填）").strip()
            cls = str(_get(r, mapping, "class") or "（未填）").strip()
            by_step.setdefault(step, {})
            by_step[step][cls] = by_step[step].get(cls, 0) + 1
        out["by_step"] = by_step

    # 数据质量体检
    q = {"missing_class": counter.get("（未填）", 0),
         "n_classes": len([k for k in counter if k != "（未填）"]),
         "duplicate_ids": 0, "bad_size": 0, "same_element_string": None}
    if "id" in mapping:
        ids = [str(_get(r, mapping, "id") or "").strip() for r in rows]
        ids = [i for i in ids if i]
        q["duplicate_ids"] = len(ids) - len(set(ids))
    if "size" in mapping:
        bad = 0
        for r in rows:
            v = _get(r, mapping, "size")
            if v in (None, ""):
                continue
            fv = _fnum(v)
            if fv is None or fv <= 0:
                bad += 1
        q["bad_size"] = bad
    if "elements" in mapping:
        vals = [str(_get(r, mapping, "elements") or "").strip() for r in rows]
        vals = [v for v in vals if v]
        if len(vals) >= 3 and len(set(vals)) == 1:
            q["same_element_string"] = vals[0]
            out["warnings"].append(
                "所有记录的元素组成都是同一串 %r：多半是整列复制粘贴，"
                "这样的『成分证据』不能用来区分缺陷来源" % vals[0])
    out["quality"] = q
    if q["missing_class"]:
        out["warnings"].append("有 %d 条记录没填缺陷类别，已单列为『（未填）』——"
                              "未填不等于不重要，占比会失真" % q["missing_class"])
    if q["duplicate_ids"]:
        out["warnings"].append("缺陷编号有 %d 个重复：编号重复会让『同一缺陷被数两次』，"
                              "Pareto 直接偏" % q["duplicate_ids"])
    if q["bad_size"]:
        out["warnings"].append("有 %d 条尺寸字段非数值或 ≤0：尺寸是判断『颗粒 vs 划伤』"
                              "的关键依据，须先修正" % q["bad_size"])

    # 疑似同义类：归一化后相同但字面不同
    groups = {}
    for k in counter:
        if k == "（未填）":
            continue
        groups.setdefault(_norm(k), []).append(k)
    dups = {n: v for n, v in groups.items() if len(v) > 1}
    if dups:
        out["warnings"].append(
            "疑似同义类别（归一化后相同）：%s —— **请人工确认是否合并**。"
            "工具不自动合并：把真不同的类别并掉，比不合并更糟"
            % "；".join("/".join(v) for v in dups.values()))
        q["synonym_suspects"] = dups

    out["status"] = ST.SUCCESS if total else ST.INSUFFICIENT_DATA
    return out


def step_association(rows, alpha=0.05):
    """缺陷类别构成是否与工序相关（卡方独立性检验 + 期望频数体检）。

    回答的是："这批里划伤的比例，在 CMP 与刻蚀之间是否真的不同"。
    **不是**因果结论：相关可能是因为这两道工序本来就跑不同的产品。
    """
    out = {"status": ST.INSUFFICIENT_DATA, "table": None, "chi2": None,
           "p": None, "low_expected_ratio": None, "warnings": [],
           "note": "独立性检验，不做因果；工序与产品/机台混杂时须分层再看 "
                   "（见 lib/confound.py）。"}
    cols = sorted({k for r in rows for k in r}) if rows else []
    mapping, _ = map_columns(cols)
    if "step" not in mapping or "class" not in mapping:
        out["warnings"].append("需要『工序』与『缺陷类别』两列才能做关联检验")
        return out
    # 交叉表
    steps, classes = [], []
    cell = {}
    for r in rows:
        st = str(_get(r, mapping, "step") or "").strip()
        cl = str(_get(r, mapping, "class") or "").strip()
        if not st or not cl:
            continue
        if st not in steps:
            steps.append(st)
        if cl not in classes:
            classes.append(cl)
        cell[(st, cl)] = cell.get((st, cl), 0) + 1
    if len(steps) < 2 or len(classes) < 2:
        out["warnings"].append("工序或类别只有 1 类，无法做关联检验")
        out["status"] = ST.NOT_APPLICABLE
        return out
    grid = [[cell.get((s, c), 0) for c in classes] for s in steps]
    out["steps"], out["classes"], out["table"] = steps, classes, grid

    # 期望频数体检
    row_t = [sum(r) for r in grid]
    col_t = [sum(grid[i][j] for i in range(len(steps))) for j in range(len(classes))]
    n = sum(row_t)
    low, total_cells = 0, len(steps) * len(classes)
    min_exp = None
    for i in range(len(steps)):
        for j in range(len(classes)):
            e = row_t[i] * col_t[j] / n if n else 0
            if min_exp is None or e < min_exp:
                min_exp = e
            if e < MIN_EXPECTED:
                low += 1
    ratio = low / total_cells if total_cells else None
    out["low_expected_ratio"] = ratio
    out["min_expected"] = min_exp

    res = S.chi2_test(grid)
    out["chi2"] = res.get("chi2") if isinstance(res, dict) else res
    out["p"] = res.get("p") if isinstance(res, dict) else None

    if ratio is not None and ratio > MAX_LOW_EXPECTED_RATIO:
        # 关键：这里必须把 p 清掉。曾经只改了 status 却把 p 留着，
        # 下游照样能读到那个不可用的 p 并拿去当结论（自检抓出）。
        out["p_unusable"] = out["p"]
        out["p"] = None
        out["warnings"].append(
            "有 %.0f%% 的格子期望频数 <%.0f（最小 %.2f）：卡方近似失效，"
            "**不报 p 值**（原计算值 %.4g 已标记为不可用），只给描述性占比。"
            "补样本或把稀有类别合并后再检验"
            % (ratio * 100, MIN_EXPECTED, min_exp or 0, out["p_unusable"] or 0))
        out["status"] = ST.INSUFFICIENT_DATA
        return out
    if out["p"] is not None:
        out["status"] = ST.SUCCESS if out["p"] < alpha else ST.NO_FINDING
        if out["p"] >= alpha:
            out["warnings"].append(
                "p=%.3f 不显著：没有证据说明缺陷构成与工序相关" % out["p"])
    return out


def evidence_gate(rows, claimed_mechanism=None):
    """证据门禁：声称某机理时，表里必须有支撑该机理的证据字段。

    这是本模块最有价值的一条。质量场景里最常见的坏结论是
    "这批是颗粒污染"——但表里连元素组成和尺寸都没记，
    那个结论就没有证据支撑，不能写进 8D。
    """
    out = {"status": ST.INSUFFICIENT_DATA, "claimed": claimed_mechanism,
           "present_fields": [], "missing_evidence": [], "verdict": None,
           "warnings": [],
           "note": "只判断『证据字段在不在』，不判断证据本身是否成立"
                   "（那要人看形貌/谱图）。"}
    cols = sorted({k for r in rows for k in r}) if rows else []
    mapping, _ = map_columns(cols)
    out["present_fields"] = sorted(mapping.keys())
    if not claimed_mechanism:
        out["warnings"].append("没有声称机理，无需门禁")
        out["status"] = ST.NOT_APPLICABLE
        return out
    key = None
    for m in MECHANISM_EVIDENCE:
        if m in str(claimed_mechanism):
            key = m
            break
    if key is None:
        out["warnings"].append(
            "机理 %r 不在已登记的证据要求表内（现登记：%s）。"
            "**不替它临时编一套证据要求**——请显式登记后再用。"
            % (claimed_mechanism, "、".join(MECHANISM_EVIDENCE)))
        out["status"] = ST.NOT_APPLICABLE
        return out
    need = MECHANISM_EVIDENCE[key]
    missing = [f for f in need if f not in mapping]
    out["missing_evidence"] = missing
    if missing:
        out["verdict"] = "insufficient_evidence"
        out["warnings"].append(
            "声称『%s』但缺少支撑证据字段：%s。"
            "**这条结论不能写进正式报告/8D**，须补齐表征证据或降级为待验证假设"
            % (claimed_mechanism, "、".join(missing)))
        out["status"] = ST.INSUFFICIENT_DATA
    else:
        out["verdict"] = "evidence_present"
        out["status"] = ST.SUCCESS
    return out


# ------------------------------------------------------------------ 自检

def _selftest():
    """已知真值自检：构造已知构成的数据，看占比/关联/门禁是否判对。"""
    ok = []

    cols = ["缺陷编号", "缺陷类别", "尺寸", "元素组成", "工序", "机台", "表征手段"]

    def rows_of(spec):
        """spec: [(class, step, size, elements, tool)]"""
        out = []
        for i, (cl, st, sz, el, tl) in enumerate(spec, start=1):
            out.append({"缺陷编号": "D%03d" % i, "缺陷类别": cl, "尺寸": sz,
                        "元素组成": el, "工序": st, "机台": tl,
                        "表征手段": "SEM-EDS"})
        return out

    # 1) Pareto：构造 6/3/1 的构成，占比必须精确对上
    spec = ([("划伤", "CMP", 20, "Si,O", "T1")] * 6
            + [("颗粒", "刻蚀", 5, "Al,O", "T2")] * 3
            + [("残留", "薄膜", 3, "C,F", "T3")] * 1)
    rows = rows_of(spec)
    s1 = summarize(rows)
    shares = {x["defect_class"]: x["share"] for x in s1["pareto"]}
    ok.append(("Pareto 占比精确（划伤 6/10=0.6）", abs(shares["划伤"] - 0.6) < 1e-12,
               "%.3f" % shares["划伤"]))
    ok.append(("Pareto 按数量降序",
               [x["defect_class"] for x in s1["pareto"]] == ["划伤", "颗粒", "残留"],
               str([x["defect_class"] for x in s1["pareto"]])))
    ok.append(("识别到表征手段含 SEM-EDS 并声明边界",
               "SEM" in s1["note"] and "不解读" in s1["note"], ""))
    ok.append(("条目数正确 = 10", s1["n"] == 10, str(s1["n"])))

    # 2) 缺类别列 → 必须拒绝并列出可选列，不许猜
    rows_nc = [{"编号": "D1", "大小": 3, "在哪里": "x"}]
    s2 = summarize(rows_nc)
    ok.append(("缺『缺陷类别』列时拒答并列出可选列",
               s2["status"] == ST.INSUFFICIENT_DATA
               and any("可选列" in w for w in s2["warnings"]), ""))

    # 3) 数据质量：重复编号 / 尺寸非数值 / 元素整列复制
    rows_bad = rows_of([("划伤", "CMP", 20, "Si,O", "T1")] * 3)
    rows_bad[1]["缺陷编号"] = "D001"        # 重复
    rows_bad[2]["尺寸"] = "看不清"           # 非数值
    s3 = summarize(rows_bad)
    ok.append(("重复编号被发现", s3["quality"]["duplicate_ids"] == 1,
               str(s3["quality"]["duplicate_ids"])))
    ok.append(("非数值尺寸被发现", s3["quality"]["bad_size"] == 1,
               str(s3["quality"]["bad_size"])))
    ok.append(("元素整列复制粘贴被发现且告警",
               s3["quality"]["same_element_string"] is not None
               and any("复制粘贴" in w for w in s3["warnings"]), ""))

    # 4) 字面归一化重复：带空格/全角括号的写法应被提示，且**不自动合并**
    #    （注意：工具不做语义同义词识别，"划伤"vs"刮伤"不会被认成同类——
    #     这是有意为之，已在模块头声明，不是缺陷）
    rows_syn = rows_of([("划伤", "CMP", 20, "Si,O", "T1"),
                        ("划 伤", "CMP", 21, "Si,N", "T1")])
    s4 = summarize(rows_syn)
    ok.append(("字面归一化重复被提示但不自动合并",
               s4["quality"].get("synonym_suspects")
               and len(s4["pareto"]) == 2
               and any("请人工确认" in w for w in s4["warnings"]),
               "类数=%d suspects=%s" % (len(s4["pareto"]),
                                        s4["quality"].get("synonym_suspects"))))
    #    语义同义词**不应**被自动识别（声明边界，防止以后有人加个词表就"优化"成自动合并）
    s4b = summarize(rows_of([("划伤", "CMP", 20, "Si,O", "T1"),
                             ("刮伤", "CMP", 21, "Si,N", "T1")]))
    ok.append(("语义同义词不被自动识别（工具不编词表）",
               not s4b["quality"].get("synonym_suspects"), ""))

    # 5) 工序关联：构造"划伤几乎全在 CMP"的强关联，p 必须显著
    spec5 = ([("划伤", "CMP", 20, "Si,O", "T1")] * 20
             + [("颗粒", "刻蚀", 5, "Al,O", "T2")] * 20
             + [("划伤", "刻蚀", 19, "Si,O", "T2")] * 2
             + [("颗粒", "CMP", 6, "Al,O", "T1")] * 1)
    a5 = step_association(rows_of(spec5))
    ok.append(("强关联被检出显著", a5["p"] is not None and a5["p"] < 0.001,
               "p=%.2g" % (a5["p"] or 1)))
    #    无关联：两工序构成相同
    spec6 = ([("划伤", "CMP", 20, "Si,O", "T1")] * 20
             + [("颗粒", "CMP", 5, "Al,O", "T1")] * 20
             + [("划伤", "刻蚀", 20, "Si,O", "T2")] * 20
             + [("颗粒", "刻蚀", 6, "Al,O", "T2")] * 20)
    a6 = step_association(rows_of(spec6))
    ok.append(("构成相同时判无关联", a6["status"] == ST.NO_FINDING,
               "p=%.3f" % (a6["p"] or 1)))
    #    样本太少 → 期望频数体检必须拦住，不给 p
    spec7 = ([("划伤", "CMP", 20, "Si,O", "T1")] * 2
             + [("颗粒", "刻蚀", 5, "Al,O", "T2")] * 1)
    a7 = step_association(rows_of(spec7))
    ok.append(("期望频数过低时不给 p 值",
               a7["status"] == ST.INSUFFICIENT_DATA and a7["p"] is None,
               "status=%s p=%s" % (a7["status"], a7["p"])))
    ok.append(("期望频数体检回报最小期望值与比例",
               a7["min_expected"] is not None and a7["low_expected_ratio"] is not None,
               "min=%.2f ratio=%.2f" % (a7["min_expected"], a7["low_expected_ratio"])))

    # 6) 证据门禁：声称"颗粒"但没有元素组成列 → 判证据不足
    cols_no_el = ["缺陷编号", "缺陷类别", "尺寸", "工序"]
    rows_no_el = [{"缺陷编号": "D1", "缺陷类别": "颗粒", "尺寸": 5, "工序": "刻蚀"}]
    g1 = evidence_gate(rows_no_el, "颗粒污染")
    ok.append(("声称颗粒但无元素组成 → 证据不足",
               g1["verdict"] == "insufficient_evidence"
               and "elements" in g1["missing_evidence"],
               str(g1["missing_evidence"])))
    ok.append(("证据不足时明确说不能写进报告",
               any("不能写进正式报告" in w for w in g1["warnings"]), ""))
    #    证据齐备 → 通过
    g2 = evidence_gate(rows_of([("颗粒", "刻蚀", 5, "Al,O", "T2")]), "颗粒污染")
    ok.append(("证据齐备时门禁通过", g2["verdict"] == "evidence_present",
               g2["verdict"]))
    #    未登记的机理 → 不替它编证据要求
    g3 = evidence_gate(rows_of([("划伤", "CMP", 20, "Si,O", "T1")]), "某种没登记过的机理")
    ok.append(("未登记机理不临时编证据要求",
               g3["status"] == ST.NOT_APPLICABLE
               and any("不替它临时编" in w for w in g3["warnings"]), ""))
    #    划伤需要 尺寸+位置，缺位置要报
    g4 = evidence_gate(rows_of([("划伤", "CMP", 20, "Si,O", "T1")]), "划伤")
    ok.append(("划伤缺『位置』时判证据不足",
               "location" in g4["missing_evidence"], str(g4["missing_evidence"])))
    return ok
