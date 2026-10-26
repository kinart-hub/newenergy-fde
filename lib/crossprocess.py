# -*- coding: utf-8 -*-
"""
crossprocess.py —— 跨工序追溯引擎

回答一个现场最常见也最容易扯皮的问题：
    「上一道工序检测没问题，为什么到我这工序就出不良？」

答案通常不是谁甩锅给谁，而是三种情形之一，本模块用数据区分它们：

  A. 本工序主导     —— 上游来料平稳且居中，问题只与本工序某项参数相关
  B. 上游带入       —— 来料批次 / 供应商 / 上游参数与目标显著相关，且已越过临界
  C. 合格但边缘     —— 最阴险的一种：上游参数**始终在其规格内**，但均值已经漂到
                       对本工序不利的一侧（问题件对应的上游均值 ≠ 正常件，且差异显著）。
                       上游检测当然"没问题"，因为它只对自己的规格负责。
                       这才是"上一道没问题、这一道出不良"最常见的技术成因。

判定用三类证据，不做单一指标拍板：
  1. 方差解释度 η²（比相关系数更能说明"贡献份额"）
  2. 分组集中度：是否按来料批次/供应商聚集（指向带入），还是按本工序设备聚集（指向本工序）
  3. 裕度探针：把样本按目标二分（不良 vs 合格，或上下四分位），对上游参数做两样本检验，
     并核对该偏差是否仍在上游自己的规格内 —— 这是情形 C 的唯一判据

设计原则：任何一步拿不到数据就降级并如实说明，绝不假装算出来了。
"""

try:
    from . import degrade          # 包模式
except Exception:                  # 脚本模式（python lib/pipeline.py）无包上下文
    import degrade                 # noqa
import os
import math

from . import table as T
from . import stats as S
from . import preprocess as PP

KB_CACHE = {}


# ------------------------------------------------------------------ 工序链

def load_kb(path=None):
    return PP.load_knowledge(path)


def upstream_of(process, chain_hops=4):
    """
    取指定工序的上游工序 key。

    默认一路走到源头（4 跳足够覆盖我们最长的链条）。刻意不默认 2 跳——
    化成的问题常常源头在涂布，隔了 3 跳，只取 2 跳会把真凶漏掉。
    """
    kb = load_kb()
    procs = kb.get("processes", {})
    if process not in procs:
        return []
    cur, out, seen = [process], [], {process}
    out_of_scope = []
    for _ in range(chain_hops):
        nxt = []
        for p in cur:
            for up in (procs.get(p, {}).get("upstream_keys") or []):
                if up in procs and up not in seen:
                    seen.add(up)
                    out.append(up)
                    nxt.append(up)
                elif up not in procs and up not in out_of_scope:
                    # 只在真的被行业作用域裁掉时留痕；本来就不存在的 key 不必打扰用户
                    try:
                        from . import scope as SC
                    except Exception:
                        import scope as SC                   # noqa
                    if SC.active():
                        try:
                            raw = PP.load_knowledge_raw()
                        except Exception:
                            raw = None
                        if raw and up in (raw.get("processes") or {}):
                            out_of_scope.append(up)
        cur = nxt
        if not cur:
            break
    if out_of_scope:
        # 追溯链被作用域截断必须让用户知道，不然会误以为"已排查完全部上游"
        try:
            from . import scope as SC
        except Exception:
            import scope as SC                               # noqa
        SC.add_note("以下上游工序不在本企业行业作用域内，未纳入本次追溯：%s"
                    % "、".join(out_of_scope))
    return out


def downstream_of(process, chain_hops=4):
    """取指定工序的下游工序 key（沿着 downstream_keys 往下走 chain_hops 跳）"""
    return _downstream(process, chain_hops)


def _process_of_alias(kb, name):
    """参数表里查不到时，尝试用工序别名反查（如「一区温度」登记在 coating 的 aliases 里）"""
    if not name:
        return None
    n = PP._norm_name(str(name))
    for pk, pv in (kb.get("params") or {}).items():
        for a in (pv.get("aliases") or []):
            if PP._norm_name(a) == n:
                return pv.get("process")
    return None


def _pick_spec(spec, col, table_meta, params=None):
    """
    取某列的上下限 —— **只认企业/用户提供的规格**。

    刻意不用知识库的 typical_range / control 兜底：那些是行业典型值，
    本项目自己的铁律就是"用典型值算的结论不得用于正式判定"。
    拿它当合格线去判"上游是否越限"，等于拿参考值产出假结论。
    知识库的值只作为 reference 展示，并在报告里提示需要索取真实规格。
    """
    base = col.split("::", 1)[1] if "::" in col else col
    pkey = _base_param_of(col, table_meta)
    for k in (col, base, pkey):
        s = (spec or {}).get(k)
        if s and (s.get("lsl") is not None or s.get("usl") is not None):
            return s
    return None


def _kb_reference(pkey, params=None):
    """知识库的典型量程，仅作参考展示，不参与合格判定"""
    ps = (params or {}).get(pkey) or {}
    ctrl = ps.get("control") or {}
    if ctrl.get("lsl") is not None or ctrl.get("usl") is not None:
        return {"lsl": ctrl.get("lsl"), "usl": ctrl.get("usl"), "source": "知识库控制口径"}
    tr = ps.get("typical_range")
    if tr and isinstance(tr, (list, tuple)) and len(tr) == 2:
        return {"lsl": tr[0], "usl": tr[1], "source": "知识库典型量程"}
    return None


def _downstream(process, chain_hops=1):
    """downstream_of 的实现；单独放是为了让 upstream_of / downstream_of 的调用形式对称"""
    kb = load_kb()
    procs = kb.get("processes", {})
    if process not in procs:
        return []
    cur, out, seen = [process], [], {process}
    for _ in range(chain_hops):
        nxt = []
        for p in cur:
            for dn in (procs.get(p, {}).get("downstream_keys") or []):
                if dn in procs and dn not in seen:
                    seen.add(dn)
                    out.append(dn)
                    nxt.append(dn)
        cur = nxt
        if not cur:
            break
    return out


def carry_over_defects(defect, current_process=None):
    """某缺陷有可能是从哪些上游工序带入的（嫌疑清单，不是结论）"""
    kb = load_kb()
    node = kb.get("defect_rootcause", {}).get(defect)
    if not node:
        return {"carry_over_from": [], "clearing_evidence": [], "note": "该缺陷未在知识库登记"}
    return {"carry_over_from": list(node.get("carry_over_from") or []),
            "clearing_evidence": list(node.get("clearing_evidence") or []),
            "note": "carry_over_from 是嫌疑清单，必须以实测数据证实或排除"}


# ------------------------------------------------------------------ 多表对齐

BATCH_KEY_HINT = ("批次", "批号", "卷号", "卷包", "lot", "batch", "coil", "roll",
                  "序列号", "序号", "序列号 ", "serial", "sn", "组件编号", "电芯编号",
                  "wafer", "硅片编号", "供应商", "来料")


def _norm(s):
    return PP._norm_name(str(s or ""))


def is_batch_key(colname):
    n = _norm(colname)
    return any(h in n for h in BATCH_KEY_HINT)


def detect_join_key(tables):
    """
    在若干张表里找一个能用于跨工序对齐的键。

    优先级刻意把「批次/编号」放在「时间」前面：
      批次号是件-件精确的物料对应关系；时间对齐只是就近近似，
      会把相邻批次的数据张冠李戴。有序列号/批次号就必须用它，
      只有在完全没有批次类键时才退而求其次用时间。
    """
    if not tables or len(tables) < 2:
        return {"mode": "none", "key": None, "reason": "表数量不足，无需对齐"}

    # 1) 批次 / 编号 / 来料类键：取所有表共有的规范化列名
    common = None
    for tb in tables:
        cur = {_norm(c): c for c in tb.columns}
        common = cur if common is None else {k: v for k, v in common.items() if k in cur}
    common = common or {}
    for nk, name in common.items():
        if is_batch_key(name):
            return {"mode": "batch", "key": name,
                    "reason": "多表共有批次/编号列「%s」，按件进行精确追溯" % name}
    for nk, name in common.items():
        idx = tables[0].col_index(name)
        vals = [str(r[idx]).strip() for r in tables[0].rows]
        if vals and 1 < len(set(vals)) <= max(2, len(vals) * 0.7):   # 必须是分组键而非唯一 ID
            return {"mode": "batch", "key": name,
                    "reason": "多表共有分组列「%s」" % name}

    # 2) 退到时间：必须每张表都真的能解析出时间列，缺一张都不用（否则会静默丢表）
    time_cols, failed = [], []
    for tb in tables:
        tc, _ = PP.parse_time_column(tb)
        if tc:
            time_cols.append(tc)
        else:
            failed.append(tb.name)
    if not failed:
        return {"mode": "time", "key": time_cols,
                "reason": "各表均有可解析的时间列，按时间就近对齐（精度不如批次号）"}
    return {"mode": "none", "key": None,
            "reason": "缺少共同的批次号/序列号，且 %s 没有可解析的时间列，无法跨工序对齐"
                      % "、".join(failed)}


def align(tables, join=None, tolerance_seconds=None):
    """
    把多张工序表对齐成一张宽表。返回 (aligned_table or None, report)
    report 里带 mode / owner（每列来自哪张表）/ 未匹配行数。
    """
    info = join or detect_join_key(tables)
    mode = info.get("mode")
    if mode == "time":
        pairs = [(tb, tc) for tb, tc in zip(tables, info.get("key") or []) if tc]
        try:
            aligned, rep = PP.time_align(pairs, None, tolerance_seconds=tolerance_seconds)
        except Exception as e:
            return None, {"mode": "time", "error": "时间对齐失败：%s" % e}
        if aligned is None or len(aligned.rows) == 0:
            return None, {"mode": "time", "error": (rep or {}).get("error") or "对齐后无有效行"}
        owner = {}
        for c in aligned.columns:
            if "::" in c:
                owner[c] = c.split("::", 1)[0]
        _post_check(aligned, tables, owner)
        return aligned, {"mode": "time", "owner": owner, "detail": rep,
                         "reason": info.get("reason"),
                         "dropped_tables": owner.get("__dropped__") or [],
                         "warning": owner.pop("__warning__", None)}
    if mode == "batch":
        key = info.get("key")
        try:
            aligned, rep = PP.merge_tables(tables, key, how="inner")
        except Exception as e:
            return None, {"mode": "batch", "error": "批次合并失败：%s" % e}
        if aligned is None or len(aligned.rows) == 0:
            return None, {"mode": "batch", "error": (rep or {}).get("error") or "合并后无有效行"}
        owner = {}
        for c in aligned.columns:
            if "::" in c:
                owner[c] = c.split("::", 1)[0]
        owner = _build_owner(aligned, tables)
        _post_check(aligned, tables, owner)
        return aligned, {"mode": "batch", "owner": owner, "detail": rep,
                         "key": key, "reason": info.get("reason"),
                         "dropped_tables": owner.get("__dropped__") or [],
                         "warning": owner.pop("__warning__", None)}
    return None, {"mode": "none", "error": info.get("reason")}


def _build_owner(aligned, tables):
    """
    确定每一列来自哪张表。

    不能只认 "表名::列名" 这种带前缀的写法：merge_tables 只在列名冲突时才加前缀，
    大多数列其实是"裸名"，只靠前缀会把它们全部误判成主表的（也就是本工序的）数据。
    """
    if not tables:
        return {}
    cols = set(aligned.columns)
    base_cols = set(tables[0].columns)
    owner = {}
    for c in cols:
        if "::" in c:
            owner[c] = c.split("::", 1)[0]
    join_tables = tables[1:]
    for tb in join_tables:
        for c in tb.columns:
            if c not in cols or c in base_cols:
                continue
            # 只有这一张 join 表有这个名字，才能安全地把裸名归给它
            if not any(c in other.columns for other in join_tables if other is not tb):
                owner.setdefault(c, tb.name)
    return owner


def _post_check(aligned, tables, owner):
    """
    对齐后必须每张表都留下了数据，否则等于某道工序根本没参与分析。
    这个坑踩过：一张表被误判"有时间列"后在 time_align 里被整表跳过，
    真凶（上游含水量）从对齐结果里消失，追溯却"正常"地给出一个无害结论。
    """
    present = set(owner.values())
    # 主表（join 的左侧）的列不带表名前缀，它本身不算丢失
    dropped = [tb.name for tb in tables[1:] if tb.name not in present]
    if dropped:
        owner["__dropped__"] = dropped
        owner["__warning__"] = ("对齐后 %s 的数据没有进入分析（缺批次号或时间列），"
                                "本次追溯只覆盖了其余工序，结论可能失真"
                                % "、".join(dropped))


# ------------------------------------------------------------------ 统计基础

def residualize(table, target_col, cov_col):
    """
    把目标列对某个协变量做一元线性回归，返回残差序列。

    为什么要这一步：当某个主因子（如张力阶跃、线速切换）造成的波动远大于设备间差异时，
    直接做横向对比，所有设备看起来都"差不多"——真有偏差的那台会被淹没掉。
    剔除主效应之后再比，设备的固有差异才浮出来。这是横向排查里最容易漏报的一环。
    """
    ti, ci = table.col_index(target_col), table.col_index(cov_col)
    if ti is None or ci is None:
        return None
    pairs = []
    for r in table.rows:
        y = T.to_number(r[ti])
        x = T.to_number(r[ci])
        if x is None or y is None:
            pairs.append((None, None))
        else:
            pairs.append((x, y))
    pts = [(x, y) for x, y in pairs if x is not None and y is not None]
    if len(pts) < 12:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx, my = S.mean(xs), S.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    inter = my - slope * mx
    res = []
    for x, y in pairs:
        res.append(None if y is None else y - (inter + slope * (x or 0) if x is not None else my))
    return {"residuals": res, "slope": slope, "intercept": inter, "cov": cov_col}


def eta_squared(table, group_col, value_col, max_levels=12, adjust_df=0):
    """
    单因素方差解释度 η²：组间平方和 / 总平方和，并自行给出 F 检验的 p。

    adjust_df —— 目标列已经做过残差化处理时要减掉被消耗的自由度，
                 否则 p 值偏乐观（虚报显著）。
    """
    gi, vi = table.col_index(group_col), table.col_index(value_col)
    if gi is None or vi is None:
        return None
    buckets = {}
    for r in table.rows:
        k = str(r[gi]).strip()
        v = T.to_number(r[vi])
        if v is None or k == "":
            continue
        buckets.setdefault(k, []).append(v)
    if len(buckets) < 2 or len(buckets) > max_levels:
        return None
    groups = [g for g in buckets.values() if len(g) >= 2]
    if len(groups) < 2:
        return None
    allv = [x for g in groups for x in g]
    n = len(allv)
    gm = S.mean(allv)
    ssb = sum(len(g) * (S.mean(g) - gm) ** 2 for g in groups)
    sst = sum((x - gm) ** 2 for x in allv)
    if sst <= 0:
        return None
    eta = ssb / sst
    df1 = len(groups) - 1
    df2 = n - len(groups) - adjust_df
    if df1 <= 0 or df2 <= 0:
        return {"eta_sq": eta, "p": None, "levels": len(groups), "n": n}
    sse = sst - ssb
    f = (ssb / df1) / (sse / df2) if sse > 0 else float("inf")
    try:
        p = S.f_sf(f, df1, df2)
    except Exception:
        p = None
    return {"eta_sq": eta, "p": p, "levels": len(groups), "n": n, "F": f, "df": [df1, df2]}


def binary_split(table, target_col, mode="quartile", judge_col=None):
    """
    把样本按目标二分，返回 (bad_idx, good_idx)。
    mode=judge   —— 有合格判定列时用它（OK/NG）
    mode=quartile—— 用上下四分位（目标列是连续量时的通用做法）
    """
    vi = table.col_index(target_col)
    if vi is None:
        return [], []
    if mode == "judge" and judge_col:
        ji = table.col_index(judge_col)
        if ji is not None:
            bad, good = [], []
            for i, r in enumerate(table.rows):
                v = str(r[ji]).strip().upper()
                if T.to_number(r[vi]) is None:
                    continue
                if v in ("NG", "FAIL", "不合格", "0", "N"):
                    bad.append(i)
                elif v in ("OK", "PASS", "合格", "1", "Y"):
                    good.append(i)
            if len(bad) >= 5 and len(good) >= 5:
                return bad, good
    vals = [(i, T.to_number(r[vi])) for i, r in enumerate(table.rows)]
    vals = [(i, v) for i, v in vals if v is not None]
    if len(vals) < 20:
        return [], []
    vals.sort(key=lambda x: x[1])
    k = max(5, len(vals) // 4)
    return [i for i, _ in vals[-k:]], [i for i, _ in vals[:k]]


# ------------------------------------------------------------------ 裕度探针

def probe_margin(table, param_col, bad_idx, good_idx, spec=None, kb_params=None):
    """
    情形 C 的判据：把样本按目标好坏二分，比较该参数的均值差异，
    并核对差异是否仍落在它自己的规格窗口内。

    返回 dict：
      delta / p / eta —— 统计显著性
      inside_spec     —— 两组均值是否都在上游自己的规格内（True 时就是"合格但边缘"）
      verdict         —— resident/upstream_marginal/insufficient
    """
    pi = table.col_index(param_col)
    if pi is None:
        return None
    bad = [T.to_number(table.rows[i][pi]) for i in bad_idx]
    good = [T.to_number(table.rows[i][pi]) for i in good_idx]
    bad = [x for x in bad if x is not None]
    good = [x for x in good if x is not None]
    if len(bad) < 5 or len(good) < 5:
        return {"param": param_col, "verdict": "insufficient",
                "reason": "有效样本不足（坏 %d / 好 %d）" % (len(bad), len(good))}
    tt = S.t_test_2sample(bad, good)
    mb, mg = S.mean(bad), S.mean(good)
    p = (tt or {}).get("p")
    out = {"param": param_col, "mean_bad": mb, "mean_good": mg,
           "delta": mb - mg, "p": p, "n_bad": len(bad), "n_good": len(good)}
    lsl, usl = None, None
    if spec:
        lsl, usl = spec.get("lsl"), spec.get("usl")
    have_spec = (lsl is not None or usl is not None)
    def _inside(v):
        if v is None:
            return None
        return (lsl is None or v >= lsl) and (usl is None or v <= usl)
    ib, ig = _inside(mb), _inside(mg)
    out["inside_spec"] = {"bad_mean": ib, "good_mean": ig,
                          "lsl": lsl, "usl": usl} if have_spec else None
    sig = (p is not None and p < 0.05)
    if not sig:
        out["verdict"] = "not_discriminating"
        out["reason"] = "好坏两组的均值差异不显著（p=%.3f），该参数对本次不良无区分力" % (p or 1)
    elif not have_spec:
        # 拿不到规格就不能判"合格但边缘"——否则所有没规格的场景都会被误报成上游问题
        ref = _kb_reference(_base_param_of(param_col, [{}]), kb_params)
        out["reference"] = ref
        out["verdict"] = "no_spec_cannot_clear"
        out["reason"] = ("好坏两组差异显著（Δ=%.4g, p=%.4f），但没有拿到该参数的**企业规格/内控线**，"
                         "无法判断它是否一直在合格区内。这是最容易误判的情形——"
                         "%s**请先索取上游该参数的出厂规格或内控线再定性**"
                         % (out["delta"], p,
                            ("知识库参考量程 [%s, %s] 不能当作合格线；"
                             % (ref["lsl"], ref["usl"])) if ref else ""))
    elif ib is False:
        out["verdict"] = "upstream_violating"
        out["reason"] = ("坏件对应的该参数均值已超出规格（%.4g 超出 [%s, %s]），属上游不合格流入"
                         % (mb, lsl, usl))
    elif ib is True and ig is True:
        out["verdict"] = "upstream_marginal"
        out["reason"] = ("两组均值均在上游规格内，但坏件一侧显著偏高/偏低（Δ=%.4g, p=%.4f）——"
                         "典型『上游合格但边缘』，上游检测不会报警，本工序却在买单" % (out["delta"], p))
    return out


# ------------------------------------------------------------------ 主入口

def trace(tables, table_meta, target_col, current_process, defect=None,
          judge_col=None, spec=None, max_sections=8):
    """
    跨工序追溯：把多张工序表对齐后判定问题归属。

    tables     —— 已清洗 + 列名已映射的 Table 列表（顺序：当前工序在前或任意，靠 meta 区分）
    table_meta —— 与 tables 对应的 [{"name":..., "process":..., "columns": {...列名→参数key}}]
    target_col —— 当前工序的目标列（不良率 / 缺陷计数 / 关键测量值）
    """
    out = {"applicable": False, "verdict": None, "evidence": [],
           "contributions": [], "upstream_probes": [], "gaps": [], "notes": []}

    # 上游一律走到源头：化成的问题常常源头在涂布（隔 3-4 跳），取太浅会把真凶漏掉
    ups = upstream_of(current_process)
    carry = carry_over_defects(defect) if defect else {"carry_over_from": [], "clearing_evidence": []}
    out["upstream_processes"] = ups
    out["carry_over"] = carry

    aligned, rep = align(tables)
    if aligned is None:
        out["notes"].append("跨工序对齐失败：%s" % (rep or {}).get("error"))
        out["gaps"].append({"level": "P1", "item": rep.get("error") or "缺少可对齐的批次/时间列",
                            "why": "没有批次号或时间戳就没法把上游数据接到当前工序的每一件产品上"})
        return out
    out["applicable"] = True
    out["alignment"] = {"mode": rep.get("mode"),
                        "key": rep.get("key") or (rep.get("detail") or {}).get("base"),
                        "rows": len(aligned.rows), "reason": rep.get("reason")}
    owner = rep.get("owner") or {}

    if target_col not in aligned.columns:
        cand = [c for c in aligned.columns if target_col in c]
        if not cand:
            out["notes"].append("对齐后的表里找不到目标列「%s」，无法做归属判定" % target_col)
            return out
        target_col = cand[0]

    # 1) 归属归类：按参数在知识库里登记的所属工序来定责，而不是靠表名猜。
    #    知识库里 113/116 个参数都带 process 字段，这才是工艺上正确的口径；
    #    只有未登记参数才退回用"它来自哪张表"兜底。
    names = _names_of(tables)
    owner_proc = {}
    for i, nm in enumerate(names):
        owner_proc[nm] = (table_meta[i].get("process") if i < len(table_meta) else None)
    kb = load_kb()
    params = kb.get("params", {})

    # 2) 二分样本
    bad_idx, good_idx = binary_split(aligned, target_col, mode="judge", judge_col=judge_col)
    if not bad_idx:
        bad_idx, good_idx = binary_split(aligned, target_col, mode="quartile")
    if not bad_idx:
        out["notes"].append("样本量不足以做好坏二分（需 ≥20 行），归属判定降级为相关性排序")
    out["split"] = {"bad": len(bad_idx), "good": len(good_idx)}

    # 3) 逐列贡献度
    contrib = []
    for c in aligned.numeric_columns():
        if c == target_col or _norm(c) == "timestamp":
            continue
        pkey = _base_param_of(c, table_meta)
        pspec = params.get(pkey) or {}
        pproc = pspec.get("process") or _process_of_alias(kb, pkey)
        if pproc == current_process:
            role = "current"
        elif pproc in ups:
            role = "upstream"
        elif pproc:
            role = "unrelated"      # 属于链外的工序：保留展示，但不参与归属判定
        else:
            # 未登记参数：退回按"这一列来自哪张表"兜底
            oname = owner.get(c)
            if oname in owner_proc:
                role = "current" if owner_proc[oname] == current_process else (
                    "upstream" if owner_proc[oname] in ups else "unrelated")
            else:
                role = "current" if owner_proc.get(names[0]) == current_process else (
                    "upstream" if owner_proc.get(names[0]) in ups else "unrelated")
        corr = None
        try:
            x = [T.to_number(r[aligned.col_index(c)]) for r in aligned.rows]
            y = [T.to_number(r[aligned.col_index(target_col)]) for r in aligned.rows]
            pairs = [(a, b) for a, b in zip(x, y) if a is not None and b is not None]
            if len(pairs) >= 10:
                r_, p_, n_ = S.pearson([a for a, _ in pairs], [b for _, b in pairs])
                corr = {"r": r_, "p": p_, "n": n_}
        except Exception as _dg_e:
            # corr 为 None 时，跨工序结论里这一段会静默消失。
            degrade.note("crossprocess.align_corr", _dg_e)
        probe = None
        if bad_idx:
            sp = _pick_spec(spec, c, table_meta, params)
            probe = probe_margin(aligned, c, bad_idx, good_idx, sp, kb_params=params)
            if probe:
                probe["param_process"] = pproc      # 责任工序要精确到参数所属的那一道
                probe["reference"] = _kb_reference(pkey, params)
        contrib.append({"column": c, "role": role, "owner": owner.get(c),
                        "param": pkey, "param_process": pproc,
                        "corr": corr, "probe": probe})
    contrib.sort(key=lambda x: abs((x["corr"] or {}).get("r") or 0), reverse=True)
    out["contributions"] = contrib[:max_sections * 2]
    out["unrelated_columns"] = [c["column"] for c in contrib if c["role"] == "unrelated"]

    # 4) 上游裕度探针汇总（只有确属当前/上游的参数才允许参与归属判定）
    probes = [c["probe"] for c in contrib if c["role"] == "upstream" and c["probe"]]
    out["upstream_probes"] = probes

    # 5) 判定
    verdict, reason, conf = _decide(contrib, probes, ups, bad_idx)
    out["verdict"] = verdict
    out["verdict_reason"] = reason
    out["confidence"] = conf
    # 把对齐后的宽表带出去供横向排查复用同一份数据（调用方记得在 JSON 序列化前用 pop_aligned 摘掉）
    out["aligned"] = aligned
    return out


def pop_aligned(result):
    """序列化之前把 Table 对象摘掉"""
    return result.pop("aligned", None) if isinstance(result, dict) else None


def _names_of(tables):
    return [tb.name for tb in tables]


def _base_param_of(col, table_meta):
    """把 '表名::列名' 还原成参数 key（取不到就返回原列名）"""
    base = col.split("::", 1)[1] if "::" in col else col
    for meta in table_meta:
        m = (meta.get("columns") or {})
        if base in m and m[base]:
            return m[base]
    return base


def _decide(contrib, probes, ups, bad_idx):
    """综合证据给出结论。拿不准就明说拿不准。"""
    if not contrib:
        return "证据不足", ["对齐后没有可用于判定的共同参数"], "低"
    strong_up = [c for c in contrib if c["role"] == "upstream"
                 and abs((c["corr"] or {}).get("r") or 0) >= 0.4
                 and S.is_sig((c["corr"] or {}).get("p"))]
    strong_cur = [c for c in contrib if c["role"] == "current"
                  and abs((c["corr"] or {}).get("r") or 0) >= 0.4
                  and S.is_sig((c["corr"] or {}).get("p"))]
    marginal = [p for p in (probes or []) if p.get("verdict") == "upstream_marginal"]
    violating = [p for p in (probes or []) if p.get("verdict") == "upstream_violating"]

    if not bad_idx:
        top = contrib[0]
        return "仅相关性排序（样本不足以二分）", [
            "与目标最相关的列是「%s」（%s，r=%.3f）" % (
                top["column"], "上游" if top["role"] == "upstream" else "本工序",
                (top["corr"] or {}).get("r") or 0)], "低"

    if violating:
        owners = []
        for p in violating:
            nm = p.get("param_process") or "未知工序"
            if nm not in owners:
                owners.append(nm)
        return "上游不合格流入", [
            "参数 %s 在坏件一侧已越出自己的规格，而对该参数负责的是上游工序 %s"
            % ("、".join([p["param"] for p in violating]), "、".join(owners)),
            "这不是本工序能靠调参消化的：需要在上游拦截，并复核该批次已流出的产品"], "中高"
    if marginal and (not strong_cur or len(strong_cur) <= len(marginal)):
        return "上游合格但边缘（本工序窗口被吃掉）", [
            p["reason"] for p in marginal[:2]] + [
            "处置方向有两条：收严上游的内控窗口，或提高本工序对该来料波动的容忍度；"
            "只修本工序参数通常治不了本"], "中高"
    if strong_up and not strong_cur:
        return "上游带入嫌疑高", [
            "与目标显著相关且同批量级的参数全部来自上游：%s"
            % "、".join([c["column"] for c in strong_up[:3]]),
            "建议按来料批次/供应商做分层复核"], "中"
    if strong_cur and not strong_up:
        return "本工序主导", [
            "与目标显著相关的参数全部来自本工序：%s"
            % "、".join([c["column"] for c in strong_cur[:3]]),
            "上游同批次数据未显示区分力"], "中高"
    if strong_cur and strong_up:
        return "混合作用（两者叠加）", [
            "本工序参数 %s 与上游参数 %s 都与目标相关"
            % ("、".join([c["column"] for c in strong_cur[:2]]),
               "、".join([c["column"] for c in strong_up[:2]])),
            "必须做 DOE 或分组试验才能分离两者贡献，不要在此直接下结论"], "中"
    return "尚无显著证据", [
        "所有参数与目标的相关性均未达到 |r|≥0.4 且 p<0.05",
        "优先怀疑未被记录的变量（来料批次、环境、换型、人员），先补数据"], "低"


# ------------------------------------------------------------------ 横向排查

def lateral_sweep(table, target_col, group_cols, min_group=5, max_levels=12,
                  adjust_for=None):
    """
    横向排查：按各分组维度扫描，找出"只有某些机台/批次/班次才有问题"的热点。

    adjust_for —— 指定一个主导因子，先做残差化剥离它再比较分组。
    存在强主因子时这个参数不是选修课：线速切换、温度阶跃造成的波动远大于设备间差异，
    不剥离的话所有设备看起来都一样，真有偏差的那台会被淹没掉（漏报）。
    """
    work, used_cov, adjust_df = table, None, 0
    if adjust_for and adjust_for != target_col and table.col_index(adjust_for) is not None:
        rz = residualize(table, target_col, adjust_for)
        if rz:
            rows_ext = [list(r) + [rz["residuals"][i]] for i, r in enumerate(table.rows)]
            work = T.Table(list(table.columns) + ["__resid__"], rows_ext, table.name)
            used_cov, adjust_df = adjust_for, 1
    value_col = "__resid__" if used_cov else target_col

    rows = []
    for g in group_cols:
        if work.col_index(g) is None:
            continue
        es = eta_squared(work, g, value_col, max_levels=max_levels, adjust_df=adjust_df)
        if not es:
            continue
        gi, vi = work.col_index(g), work.col_index(value_col)
        buckets = {}
        for r in work.rows:
            k = str(r[gi]).strip()
            v = T.to_number(r[vi])
            if v is None or k == "":
                continue
            buckets.setdefault(k, []).append(v)
        buckets = {k: v for k, v in buckets.items() if len(v) >= min_group}
        if len(buckets) < 2:
            continue
        means = {k: S.mean(v) for k, v in buckets.items()}
        wk = max(means, key=lambda k: means[k])
        bk = min(means, key=lambda k: means[k])
        rows.append({"dim": g, "worst_level": wk, "worst_mean": means[wk],
                     "best_level": bk, "best_mean": means[bk],
                     "ratio": (means[wk] / means[bk]) if means[bk] else None,
                     "gap": means[wk] - means[bk],
                     "eta_sq": es["eta_sq"], "p": es["p"], "levels": len(buckets),
                     "adjusted_for": used_cov,
                     "note": ("已剔除主因子「%s」的影响，下列数值为残差" % used_cov)
                             if used_cov else ""})
    rows.sort(key=lambda x: x["eta_sq"], reverse=True)
    for x in rows:
        if x["eta_sq"] >= 0.14 and S.is_sig(x.get("p")):
            x["verdict"] = "局部性问题：集中在「%s」，建议先查这一台/一批" % x["worst_level"]
        elif x["eta_sq"] >= 0.06:
            x["verdict"] = "有一定聚集性，值得复核"
        else:
            x["verdict"] = "该维度不区分，可排除"
    return rows


# ─────────────────────────────────────────────────────────────
# 单元级异常检验（某个通道 / 卷针 / 腔体 / 班次单独有问题）
# ─────────────────────────────────────────────────────────────

def bad_direction(value_col, spec=None):
    """
    判断目标列往哪个方向走是「变坏」。

    不做这一步就会闹笑话：某通道不良率是 0，统计上极其显著地偏离其它通道，
    于是被当成首要根因 —— 可它是表现最好的那一个。质量分析要找的是变差的方向。
    """
    n = str(value_col or "")
    if any(k in n for k in ("NG", "不良", "缺陷", "报废", "返修", "失败", "FAIL",
                            "DPPM", "PPM", "不良率", "ng", "defect")):
        return "high"
    if any(k in n for k in ("良率", "合格率", "Pmax", "功率", "效率", "FF",
                            "容量", "强度", "yield", "pass")):
        return "low"
    if isinstance(spec, dict):
        if spec.get("usl") is not None and spec.get("lsl") is None:
            return "high"
        if spec.get("lsl") is not None and spec.get("usl") is None:
            return "low"
    return None


def outlier_levels(table, group_col, value_col, min_n=5, alpha=0.05, max_levels=30,
                   direction=None):
    """
    找出「单独某一个水平明显跑偏」的情况。

    为什么不能只用单因素 ANOVA：产线上最常见的单元级问题，是 16 个通道里只有
    CH-07 这一个坏、32 个卷针里只有 N-03 这一个坏。这种时候组间平方和被另外
    15 个正常通道的内部波动摊薄，η² 只有 0.05、p=0.41，ANOVA 判「不显著」——
    可它恰恰就是真因。逐水平做「该水平 vs 其余全部」的两两比较才能抓到。

    min_n   —— 水平样本太少不做（n<5 的均值没有意义）
    alpha   —— 显著性水平；比较了 m 个水平就要 Bonferroni 校正，否则
               水平越多越容易撞出一个「显著」的假阳性
    """
    gi, vi = table.col_index(group_col), table.col_index(value_col)
    if gi is None or vi is None:
        return None
    buckets = {}
    for r in table.rows:
        k = str(r[gi]).strip()
        v = T.to_number(r[vi])
        if v is None or k == "":
            continue
        buckets.setdefault(k, []).append(v)
    buckets = {k: v for k, v in buckets.items() if len(v) >= min_n}
    # 两个水平也要能查（供应商 A/B、班次 白/夜）——这类二分维度在产线很常见，
    # 只做 3 个以上水平的分组会漏掉它们
    if len(buckets) < 2 or len(buckets) > max_levels:
        return None

    allv = [x for v in buckets.values() for x in v]
    gsd = S.stdev(allv) if len(allv) > 2 else 0.0
    if not gsd:
        return None

    out = []
    m = len(buckets)
    for k, v in buckets.items():
        others = [x for kk, vv in buckets.items() if kk != k for x in vv]
        if len(others) < min_n:
            continue
        n1, n2 = len(v), len(others)
        m1, m2 = S.mean(v), S.mean(others)
        s1 = S.variance(v) if n1 > 1 else 0.0
        s2 = S.variance(others) if n2 > 1 else 0.0
        se = (s1 / n1 + s2 / n2) ** 0.5
        t = (m1 - m2) / se if se > 0 else 0.0
        # Welch–Satterthwaite 自由度
        den = ((s1 / n1) ** 2 / max(n1 - 1, 1) + (s2 / n2) ** 2 / max(n2 - 1, 1))
        df = ((s1 / n1 + s2 / n2) ** 2 / den) if den > 0 else float(n1 + n2 - 2)
        p_raw = 2 * S.t_sf(abs(t), df) if df > 0 else 1.0
        out.append({"level": k, "n": n1, "mean": m1, "others_mean": m2,
                    "delta": m1 - m2, "effect": abs(m1 - m2) / gsd,
                    "t": t, "p_raw": p_raw, "p_adj": min(1.0, p_raw * m)})
    if not out:
        return None
    out.sort(key=lambda x: x["p_adj"])
    # 只看往「坏」的方向偏的水平：不良率异常低、良率异常高的那台不是问题所在
    if direction == "high":
        pool = [x for x in out if x["delta"] > 0]
    elif direction == "low":
        pool = [x for x in out if x["delta"] < 0]
    else:
        pool = list(out)
    worst = max(pool, key=lambda x: x["effect"]) if pool else None
    flagged = [x for x in pool if x["p_adj"] < alpha]
    flagged.sort(key=lambda x: -x["effect"])
    return {"dim": group_col, "levels": len(buckets), "rows": out,
            "worst": worst, "flagged": flagged, "direction": direction,
            "significant": bool(flagged),
            "note": ("「%s」显著偏%s（p_adj=%.4g，偏离 %.2fσ）"
                     % (worst["level"], "高" if (worst["delta"] > 0) else "低",
                        worst["p_adj"], worst["effect"])
                     if (flagged and worst) else "")}
