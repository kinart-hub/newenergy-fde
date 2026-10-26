# -*- coding: utf-8 -*-
"""
缺陷事件表处理（v1.11.0 新增）

现场有一类表不是「一行一个产品」，而是「一行一个缺陷」：

    组件编号, EL缺陷类型, 电池列, 电池行, x(mm), y(mm), 面积占比(%)

这种表直接跑常规分析会得出三个荒唐结论：
  - 坐标列 x(mm)/y(mm) 变异系数最大，被当成分析目标；
  - 每个组件重复出现十几行，n 从 140 虚增到 269（伪重复）；
  - 真正的信号是「缺陷在电池第 7 列聚集」——这是**位置**)不是相关，普通相关矩阵算不出来。

本模块做三件事：
  1. 识别事件表并按产品汇总为「每组件各类缺陷计数」；
  2. 对位置/机构类维度做聚集性检验，按曝光加权、并对多个位置同时检验做 BH 校正；
  3. 给出可写进报告的结论文案（含期望频数与 q 值）。
"""
import math
from . import quality_guard as QG


def _to_float(v):
    return QG._to_float(v)


def looks_like_event_table(table):
    """一行一个缺陷的判断依据：存在实体列重复 + 存在缺陷类型/坐标/工位类维度列"""
    if not table or not table.columns or not table.rows:
        return False, None
    ent = entity_column(table)
    if not ent:
        return False, None
    ei = table.col_index(ent)
    vals = [r[ei] if ei < len(r) else None for r in table.rows]
    vals = [v for v in vals if v not in (None, "")]
    if len(vals) < 10:
        return False, None
    uniq = len(set(map(str, vals)))
    if uniq >= len(vals) * 0.9:
        return False, None          # 几乎每行一个 ID：不是事件表
    type_col = defect_type_column(table)
    if type_col:
        return True, {"entity": ent, "type": type_col}
    return False, None


def entity_column(table):
    """找"产品/试件"编号列：重复出现、能代表一个实体。

    只看"平均几行一个值"会把缺陷类型列当成实体（隐裂只有两种取值、每种上百行），
    所以必须先过一遍实体命名：组件编号/电芯编号/硅片编号/卷号/module/cell/wafer…
    """
    import re
    ent_hint = re.compile(r"(编号|编码|serial|sn|id|组件|电芯|硅片|卷芯|卷号|片号|"
                          r"module|cell|wafer|panel|批次单|工单)", re.I)
    best = None
    for c in table.columns:
        r = QG.role_of(table, c)
        if r not in ("id", "text"):
            continue
        vals = [v for v in (table.column(c) or []) if v not in (None, "")]
        if len(vals) < 10:
            continue
        uniq = len(set(map(str, vals)))
        ratio = len(vals) / float(max(1, uniq))
        if not (1.2 <= ratio <= 200):
            continue
        named = 1 if ent_hint.search(QG._norm(c)) else 0
        # 实体选中优先：命名命中 > 每个实体平均行数越多越好 > 取值不要太多
        score = (named, min(ratio, 50), -min(uniq, 9999))
        if best is None or score > best[0]:
            best = (score, c)
    return best[1] if best else None


def defect_type_column(table):
    """缺陷类型/模式列：低基数文本，命中缺陷语义词"""
    hint = ("缺陷", "类型", "模式", "mode", "type", "class", "类别", "不良", "原因是")
    for c in table.columns:
        if QG.role_of(table, c) != "text":
            continue
        vals = [v for v in (table.column(c) or []) if v not in (None, "")]
        uniq = set(map(str, vals))
        if not (1 <= len(uniq) <= 30):
            continue
        n = QG._norm(c)
        if any(h in n for h in hint):
            return c
    return None


def position_columns(table, max_levels=40):
    """位置/机构类维度：电池列/电池行/工位/刀位/腔位 —— 需要做聚集检验的对象"""
    out = []
    for c in table.columns:
        r = QG.role_of(table, c)
        vals = [v for v in (table.column(c) or []) if v not in (None, "")]
        uniq = set(map(str, vals))
        if not (2 <= len(uniq) <= max_levels):
            continue
        n = QG._norm(c)
        if r == "coord" or any(k in n for k in ("列", "行", "位", "pos", "row", "col",
                                                "工位", "腔", "pattern", "刀位")):
            out.append(c)
    return out


# ------------------------------------------------------------------ 汇总

def aggregate_events(table, entity=None, type_col=None):
    """事件表 -> 每个实体一行。返回 (agg_table, info)。

    输出列：实体列 + 各类缺陷计数 + 缺陷总数；其余非计数列取该实体的首个/最末取值。
    """
    from . import table as T
    entity = entity or entity_column(table)
    type_col = type_col or defect_type_column(table)
    if not entity:
        return table, None
    ei = table.col_index(entity)
    order, seen = [], {}
    for r in table.rows:
        k = r[ei] if ei < len(r) else None
        if k in (None, ""):
            continue
        if k not in seen:
            seen[k] = []
            order.append(k)
        seen[k].append(r)
    types = []
    if type_col:
        ti = table.col_index(type_col)
        for rows in seen.values():
            for r in rows:
                v = r[ti] if ti < len(r) else None
                if v not in (None, "") and v not in types:
                    types.append(v)
    counts = {k: {t: 0 for t in types} for k in seen}
    if type_col:
        ti = table.col_index(type_col)
        for k, rows in seen.items():
            for r in rows:
                v = r[ti] if ti < len(r) else None
                if v in counts[k]:
                    counts[k][v] += 1
    cols = [entity] + ["缺陷_%s" % t for t in types] + ["缺陷总数"]
    rows = []
    for k in order:
        rs = [sum(counts[k][t] for t in types)] if types else [len(seen[k])]
        rows.append([k] + [counts[k][t] for t in types] +
                    [sum(counts[k].values()) if types else len(seen[k])])
    agg = T.Table(cols, rows, table.name, table.source_path)
    info = {
        "entity": entity, "type_col": type_col, "types": types,
        "n_events": len(table.rows), "n_units": len(order),
        "count_columns": ["缺陷_%s" % t for t in types] or ["缺陷总数"],
        "note": ("识别为缺陷事件表：每个「%s」原本平均 %.1f 行（伪重复），"
                 "已按组件汇总为 %d 行、按缺陷类型分列计数。"
                 % (entity, len(table.rows) / float(max(1, len(order))), len(order))),
    }
    return agg, info


# ------------------------------------------------------------------ 聚集检验

def _chi2_counts(observed, expected):
    """卡方统计量（expected 已经可以做曝光加权）"""
    stat, df = 0.0, 0
    for o, e in zip(observed, expected):
        if e is None or e <= 0:
            continue
        stat += (o - e) ** 2 / e
        df += 1
    return stat, max(0, df - 1)


def _binom_exact_two_sided(k, n, p0):
    """二项精确检验双尾 p —— 事件表期望频数不足时的**唯一合法**精确检验。

    为什么不能用 2x2 Fisher（v1.15 修正）：
    缺陷事件表只有「缺陷发生数」，没有「非缺陷对照」的逐格计数。
    把期望频数当作对照组塞进 2x2 列联表，行列边际与自由度都不成立，
    算出来的是一个"看起来像 Fisher"的数——这正是本项目最反对的静默错结论。

    正确模型：H0 = 该水平缺陷占比等于整体占比 p0，观测 k/n。
    双尾定义为所有 P(X=x) ≤ P(X=k) 的概率之和（与 R binom.test 同口径）。

    数值实现走对数空间：大 n（上万行）下 comb*p^x 会下溢成 0，
    连 p_obs 都变 0，双尾和会退化成 1.0 —— 这是另一条静默错路。
    """
    if n <= 0 or k < 0 or k > n:
        return 1.0
    if p0 <= 0.0 or p0 >= 1.0:
        return 1.0
    lg = math.lgamma
    lp0, lq0 = math.log(p0), math.log(1.0 - p0)

    def log_pr(x):
        return (lg(n + 1) - lg(x + 1) - lg(n - x + 1)
                + x * lp0 + (n - x) * lq0)

    l_obs = log_pr(k)
    # 用对数比较，避免 p_obs 下溢后"全部 ≤ 0"的退化
    tot = 0.0
    for x in range(0, n + 1):
        lx = log_pr(x)
        if lx <= l_obs + 1e-9:
            tot += math.exp(lx)
    return min(1.0, tot)


def factor_clustering(table, factor_col, target=None, exposure=None, min_expected=5):
    """按某个位置/机构维度做聚集检验。

    factor_col  电池列/电池行/工位这类维度
    target      数值目标（连续则用"该水平的均值 vs 其余"，计数型用事件数）
    exposure    每个水平的曝光数（例如每组件该位置上的电池片数）。给不出时按均匀期望。

    多个水平同时检验，p 值必须做 BH 校正，否则 12 个电池列里"发现"一个热点几乎是必然。
    """
    from . import stats as S
    fi = table.col_index(factor_col)
    levels, order = {}, []
    for r in table.rows:
        v = r[fi] if fi < len(r) else None
        if v in (None, ""):
            continue
        if v not in levels:
            levels[v] = []
            order.append(v)
        levels[v].append(r)
    if len(order) < 2:
        return None
    ti = table.col_index(target) if target and table.col_index(target) is not None else None
    per_level = []
    for lv in order:
        rows = levels[lv]
        if ti is not None:
            vals = [_to_float(r[ti]) for r in rows if ti < len(r)]
            vals = [v for v in vals if v is not None]
            per_level.append(sum(vals) if vals else 0.0)
        else:
            per_level.append(float(len(rows)))
    total = sum(per_level)
    if total <= 0:
        return None
    if exposure and len(exposure) == len(order):
        exp_sum = float(sum(exposure))
        expected = [total * e / exp_sum for e in exposure]
    else:
        expected = [total / float(len(order))] * len(order)
    raw = []
    for lv, o, e in zip(order, per_level, expected):
        rest_o = total - o
        rest_e = total - e
        if min(o, rest_o, e, rest_e) < 0:
            continue
        if min(e, rest_e) < min_expected:
            # 期望频数不足：用二项精确检验（p0 = 该水平期望占比），
            # 不用 2x2 Fisher —— 事件表没有非缺陷对照，列联表不成立（v1.15 修正）
            p = _binom_exact_two_sided(int(round(o)), int(round(total)),
                                       (e / total) if total else 1.0)
            test = "二项精确检验（期望频数不足）"
        else:
            stat, df = _chi2_counts([o, rest_o], [e, rest_e])
            p = S.chi2_sf(stat, 1) if df >= 1 else 1.0
            test = "卡方（曝光加权）"
        raw.append({"level": lv, "observed": round(o, 3), "expected": round(e, 3),
                    "ratio": round(o / e, 3) if e else None, "n_units": len(levels[lv]),
                    "p": round(p, 6), "test": test})
    if not raw:
        return None
    qs = S.bh_adjust([d["p"] for d in raw])
    for d, q in zip(raw, qs):
        d["q"] = q
    worst = max(raw, key=lambda d: d.get("ratio") or 0)
    # 不要写 `q or 1`：q=0.0 是"极显著"，不是"缺失"（v1.15 修正）
    significant = [d for d in raw if S.is_sig(d.get("q"))]
    return {
        "factor": factor_col,
        "test": raw[0]["test"],
        "n_levels": len(order),
        "rows": raw,
        "worst": worst,
        "hot_level": worst["level"] if (worst.get("q")
                                       if worst.get("q") is not None else 1.0) <= 0.05 else None,
        "significant": bool(significant),
        "exposure_weighted": bool(exposure),
        "note": ("按「%s」做聚集检验（%d 个水平，%s，p 值已做 BH 多重比较校正）："
                 % (factor_col, len(order), raw[0]["test"])),
    }


def cluster_report(res, entity_label="组件"):
    """把聚集检验结果翻译成人话"""
    if not res:
        return None
    if not res.get("significant"):
        return ("按「%s」做聚集检验（%d 个水平）未发现 BH 校正后仍显著的热点，"
                "缺陷分布看起来是弥散的，应查工艺参数而不是某定点的机构问题。"
                % (res["factor"], res["n_levels"]))
    w = res["worst"]
    wq = res["worst"].get("q")
    wq = 1.0 if wq is None else wq
    return ("%s=%s 的缺陷数是均匀期望的 %.2f 倍（实测 %s vs 期望 %s，"
            "BH 校正后 q=%.4f）。指向该位置的机构性问题（排版/焊接/层压缓冲/压针），"
            "建议先查这一个定点，而不是全工序铺开整改。"
            % (res["factor"], w["level"], w["ratio"], w["observed"],
               w["expected"], wq))
