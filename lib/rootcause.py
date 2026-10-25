# -*- coding: utf-8 -*-
"""
rootcause.py —— 不良根因定位引擎

融合两条证据链，避免"纯统计排序"的误导：
  A. 知识库先验：缺陷类型 → 候选参数 → 机理 → 验证清单（process_specs.json）
  B. 数据证据  ：候选参数在实际数据中的关联强度 / 分组差异 / 时序吻合度

综合得分 = 0.40*数据证据 + 0.35*知识先验 + 0.15*时序吻合 + 0.10*可验证性
输出：候选根因排序、5Why 链条、良率损失 Pareto、数据缺口清单。
"""

import os
import json
import math
from . import table as T
from . import quality_guard as QG
from . import stats as S
from . import preprocess as PP
from . import correlation as CORR
from . import spc as SPC

_KB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "knowledge", "process_specs.json")
_KB = None


def _kb():
    global _KB
    if _KB is None:
        with open(_KB_PATH, "r", encoding="utf-8") as f:
            _KB = json.load(f)
    return _KB


# ------------------------------------------------------------------ 缺陷识别

def _is_count_series(vals):
    """判断一列是否是'缺陷计数'而非连续测量量。

    工艺数据里常见陷阱：列名叫「极片错位」，值是 0.28 mm 这种连续测量值，
    它不是缺陷计数，不能进 Pareto 求和——会把量纲搞错、结论带偏。
    """
    if not vals:
        return False
    if any(v < 0 for v in vals):
        return False
    ints = sum(1 for v in vals if abs(v - round(v)) < 1e-9)
    return ints / len(vals) >= 0.95 and max(vals) >= 1


# 企业自定义的缺陷叫法（知识库不可能穷举）。命中这些词且是计数型列，就纳入 Pareto。
_BAD_HINT = ("不良", "缺陷", "报废", "返工", "次品", "reject", "defect", "ng数", "ng数",
             "掉料", "气泡", "针孔", "划痕", "厚边", "隐裂", "断栅", "虚焊", "过焊",
             "碎片", "破片", "脏污", "污渍", "崩边", "缺角", "黑心", "黑斑", "漏电",
             "裂纹", "毛刺", "短路", "起皱", "褶皱", "色差", "氧化", "水印", "缺口")


# 「现场叫法 → 标准条目」的别名索引（懒构建 + 缓存）
_DEFECT_NAMES_CACHE = {}


def _defect_alias_names(dk, dv):
    """返回某缺陷的全部叫法（已归一化）：键名 + 自身 aliases + param_key 的 aliases。

    为什么需要这一层：知识库按**机理**命名（「隔膜褶皱」「黑心」），
    现场按**现象**叫（「极片起皱」「黑斑」）。同一个缺陷两个名字对不上号，
    匹配就断链 —— 库里明明有机理，却走了「未收录」兜底，
    工程师拿到的 8D 报告 D4 栏只能写着「机理未收录」。

    别名有三个来源，合并去重：
      1. 条目键名本身
      2. 条目的 aliases 字段（现场叫法，最直接）
      3. param_key 指向的 params 条目的 aliases（复用，避免两处维护）

    **但第 3 条有个前提**：param_key 必须是一个「缺陷计量列」。
    「功率衰减」「内阻偏高」「容量一致性差」这类缺陷是由**结果指标本身的漂移**
    定义的，它们的 param_key 指向 pmax / acir / capacity —— 那是**被分析的目标量**，
    不是缺陷计数列。把目标量的别名（Pmax / 功率 / 峰值功率）当成缺陷叫法，
    会让 identify_defects 把功率列本身识别成「功率衰减」缺陷列，
    进而让目标列 == 缺陷列、共线排除判据失效（实测踩过：E06 组件IV 用例直接翻红）。
    所以这类条目的别名只认键名与显式 aliases，不继承结果指标的别名。
    """
    hit = _DEFECT_NAMES_CACHE.get(dk)
    if hit is not None:
        return hit
    kb = _kb()
    names = [dk] + list(dv.get("aliases") or [])
    pk = dv.get("param_key")
    if pk and not dv.get("response_defect"):
        pv = (kb.get("params") or {}).get(pk) or {}
        names += list(pv.get("aliases") or [])
    out, seen = [], set()
    for n in names:
        if not n:
            continue
        nn = PP._norm_name(n)
        if nn and nn not in seen:
            seen.add(nn)
            out.append(nn)
    _DEFECT_NAMES_CACHE[dk] = out
    return out


def _name_hits(alias_norm, col_norm):
    """别名是否命中列名：精确相等，或别名是列名的子串（长度≥2 防单字误命中）。"""
    if alias_norm == col_norm:
        return True
    return len(alias_norm) >= 2 and alias_norm in col_norm


def identify_defects(table, hint=None, process=None):
    """从数据列中识别缺陷类型（含 EL 缺陷分类），返回 [(缺陷名, 列名, 计数/率)]

    知识库只登记了常见缺陷名，企业自己的叫法（如「掉料」）不在库里就会漏。
    所以除了知识库命中，还按「列名含缺陷语义 + 确为计数型」补一轮数据驱动识别。
    """
    kb = _kb()
    found = []
    # 1. 命中知识库缺陷名（含「现场叫法」别名反查）
    #
    #    先做一次「列 → 最具体条目」的裁决，再按条目顺序收录。
    #    为什么必须先裁决：别名一多就会出现交叉命中 ——
    #    「边缘短路」同时含「短路」（内短路的别名）和「边缘漏电」，
    #    不裁决就会把同一列算进两个缺陷，Pareto 总量虚高、根因指向也跟着错。
    #    裁决规则：**最长别名优先**，别名越长越具体。
    col_best = {}
    for c in table.columns:
        nc = PP._norm_name(c)
        best_dk, best_len = None, 0
        for dk, dv in kb["defect_rootcause"].items():
            for nn in _defect_alias_names(dk, dv):
                if _name_hits(nn, nc) and len(nn) > best_len:
                    best_dk, best_len = dk, len(nn)
        if best_dk:
            col_best[c] = best_dk

    for dk, dv in kb["defect_rootcause"].items():
        for c in table.columns:
            if col_best.get(c) != dk:
                continue
            vals = table.numeric_column(c)
            valid = [v for v in vals if v is not None]
            if valid:
                found.append({"defect": dk, "column": c,
                              "total": sum(valid), "n_records": len(valid),
                              # v1.13：不良「件数」是"有多少个件出现了该缺陷"，
                              # 不是缺陷计数之和。一个组件上有 3 处隐裂，
                              # 那是 1 件不良、不是 3 件 —— 用 total 算不良率会
                              # 算出 200%+ 的荒唐值（8D D2 直接踩到）。
                              "n_positive": len([v for v in valid if v > 0]),
                              "mean": S.mean(valid), "process": dv["process"],
                              "is_count": _is_count_series(valid)})
    # 1b. 数据驱动补充：知识库没有的缺陷叫法（企业自定义），只要确实是计数列就纳入。
    #     否则「掉料」这类现场常用名会被整列忽略，Pareto 与根因都会跑偏。
    known_cols = {f.get("column") for f in found}
    for c in table.columns:
        if c in known_cols:
            continue
        vals = table.numeric_column(c)
        valid = [v for v in vals if v is not None]
        if len(valid) < 5:
            continue
        nc = PP._norm_name(c)
        if not any(w in nc for w in _BAD_HINT):
            continue
        if not _is_count_series(valid):
            # 叫「不良率」却是比率列：不能当计数求和，量纲会错
            continue
        found.append({"defect": c, "column": c, "total": sum(valid),
                      "n_records": len(valid),
                      "n_positive": len([v for v in valid if v > 0]),
                      "mean": S.mean(valid),
                      "process": process or "", "is_count": True,
                      "from_data": True})
    # 2. 分类列中包含缺陷名称作为取值
    for c in table.text_columns():
        vals = [str(v) for v in table.column(c) if v not in (None, "")]
        for dk, dv in kb["defect_rootcause"].items():
            dnames = _defect_alias_names(dk, dv)
            hits = sum(1 for v in vals
                       if any(_name_hits(nn, PP._norm_name(v)) for nn in dnames))
            if hits:
                found.append({"defect": dk, "column": c, "count": hits,
                              "n_records": len(vals), "as_category": True,
                              "process": kb["defect_rootcause"][dk]["process"]})
    if hint:
        nd = PP._norm_name(hint)
        for dk, dv in kb["defect_rootcause"].items():
            if any(nn in nd or nd in nn for nn in _defect_alias_names(dk, dv)):
                found.insert(0, {"defect": dk, "column": None, "from_hint": True,
                                 "process": dv["process"]})
    # 去重
    seen, out = set(), []
    for f in found:
        k = (f["defect"], f.get("column"))
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def pick_primary_defect(defects, table):
    """在多个缺陷项里挑出"首要问题"。

    不能简单取总量最大的那个：产线上绝对量最大的往往是长期存在的老问题，
    真正需要处理的是**正在恶化**的那一项（增量）。按总量选会把注意力引到
    稳定噪声上，真正的异常反而被盖过去。
    """
    if not defects:
        return None
    scored = []
    for d in defects:
        col = d.get("column")
        if not col or d.get("as_category") or d.get("from_hint"):
            scored.append((0.0, d))
            continue
        vals = [v for v in table.numeric_column(col) if v is not None]
        if len(vals) < 12:
            scored.append((0.0, d))
            continue
        k = max(3, len(vals) // 3)
        head, tail = vals[:k], vals[-k:]
        a, b = S.mean(head), S.mean(tail)
        trend = (b - a) / a if a > 0 else (b - a)
        scored.append((trend, d))
    scored.sort(key=lambda x: -x[0])
    best_trend, best = scored[0]
    # 全都平稳（无恶化）时才退回按总量选
    if best_trend < 0.15:
        cnt = [d for d in defects if d.get("total")]
        if cnt:
            return max(cnt, key=lambda d: d["total"])["defect"]
    return best["defect"]


# ------------------------------------------------------------------ Pareto

def pareto(categories, weights=None):
    """categories: {名称: 数量} 或 [(名称, 数量)] -> Pareto 表 + ABC 分级"""
    items = categories.items() if isinstance(categories, dict) else categories
    items = [(k, float(v)) for k, v in items if v is not None]
    items.sort(key=lambda kv: -kv[1])
    total = sum(v for _, v in items) or 1.0
    rows, cum = [], 0.0
    for k, v in items:
        cum += v
        pct = v / total * 100
        cum_pct = cum / total * 100
        if cum_pct <= 80:
            cls = "A"
        elif cum_pct <= 95:
            cls = "B"
        else:
            cls = "C"
        rows.append({"item": k, "value": v, "pct": pct, "cum_pct": cum_pct, "abc": cls})
    return {"rows": rows, "total": total,
            "top": rows[0] if rows else None,
            "vital_few": [r for r in rows if r["abc"] == "A"]}


# ------------------------------------------------------------------ 证据计算

def _data_evidence(table, param_key, target_col, defect_proc):
    """计算某参数对目标的证据强度，返回 (score 0-1, detail)"""
    kb = _kb()
    spec = kb["params"].get(param_key, {})
    aliases = spec.get("aliases", []) + [spec.get("name", "")]
    col = None
    for c in table.columns:
        nc = PP._norm_name(c)
        for a in aliases:
            if PP._norm_name(a) and (PP._norm_name(a) == nc or PP._norm_name(a) in nc
                                     or nc in PP._norm_name(a)):
                col = c
                break
        if col:
            break
    if col is None:
        return 0.0, {"status": "数据缺失", "param": param_key,
                     "need": spec.get("name", param_key)}
    vals = table.numeric_column(col)
    y = table.numeric_column(target_col) if target_col else None
    detail = {"status": "已测", "param": param_key, "column": col}
    score = 0.0
    if y:
        r, p, n = S.pearson(vals, y)
        if r is not None:
            detail["r"] = r
            detail["p"] = p
            detail["n"] = n
            score = min(1.0, abs(r))
            if p is not None and p > 0.05:
                score *= 0.5
            detail["note"] = "与目标 %s 的 Pearson r=%.3f (p=%.4f)" % (target_col, r, p or 0)
    else:
        # 无目标列时，用其自身超出规格/波动程度作为证据
        valid = [v for v in vals if v is not None]
        ctrl = spec.get("control", {})
        if valid:
            cv = S.stdev(valid) / S.mean(valid) * 100 if S.mean(valid) else None
            detail["cv_pct"] = cv
            lsl, usl, tgt = SPC.resolve_spec(ctrl, S.mean(valid))
            if usl is not None or lsl is not None:
                oos = sum(1 for v in valid if (usl is not None and v > usl)
                          or (lsl is not None and v < lsl))
                detail["out_of_spec"] = oos
                detail["oos_pct"] = oos / len(valid) * 100
                score = min(1.0, oos / len(valid) * 5)
            elif cv is not None:
                lim = ctrl.get("cv_limit_pct")
                score = min(1.0, cv / lim) if lim else min(1.0, cv / 5.0)
            detail["note"] = "未指定目标列，按自身波动/超规格程度评估"
    return score, detail


def _temporal_match(table, param_col, target_col):
    """参数异常时段与指标异常时段的重合度（0-1）"""
    x = table.numeric_column(param_col)
    y = table.numeric_column(target_col) if target_col else None
    if y is None:
        return 0.0, None
    pairs = [(v, yy) for v, yy in zip(x, y) if v is not None and yy is not None]
    if len(pairs) < 10:
        return 0.0, None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, sx = S.mean(xs), S.stdev(xs)
    my, sy = S.mean(ys), S.stdev(ys)
    if not sx or not sy:
        return 0.0, None
    # 双方同时偏离各自均值 1.5σ 以上的点占比
    both = sum(1 for a, b in zip(xs, ys)
               if abs(a - mx) > 1.5 * sx and abs(b - my) > 1.5 * sy)
    ratio = both / len(pairs)
    return min(1.0, ratio * 3), {"both_abnormal": both, "n": len(pairs),
                                 "ratio_pct": ratio * 100}


def _evidence_gate(score, det):
    """知识库先验不能单独撑起排名。

    曾经出现过：某参数 r=0.022、p=0.76（统计上毫无关联），只因在知识库
    likely_params 里排第一（先验 1.0）就被列为首要根因。现场照着去调这个参数，
    真因一点没动。所以凡是"有数据却查不到关联"的候选，一律降级。
    """
    if det.get("status") != "已测":
        return score * 0.60, "数据缺失，按知识库先验推断（需补测才能确认）"
    r = det.get("r")
    p = det.get("p")
    if r is None:
        return score, None
    if (p is not None and p > 0.05) or abs(r) < 0.15:
        return score * 0.45, "统计上未发现关联，仅凭知识库先验（不可据此调机）"
    return score, None


def _chain_processes(proc):
    """本工序 + 上下游链内的工序集合，用于把跨工序串味的候选挡在外面"""
    if not proc:
        return None
    out = {proc}
    try:
        from . import crossprocess as XP
        kb = _kb()
        procs = kb.get("processes", {})
        if proc in procs:
            for p in XP.upstream_of(proc, procs, chain_hops=4):
                out.add(p)
            for p in XP.downstream_of(proc, procs, chain_hops=4):
                out.add(p)
    except Exception:
        pass
    return out


def rank_root_causes(defect_name, table, target_col=None, top=5,
                     col_map=None, process=None, exclude_cols=None):
    """返回候选根因排序 list

    候选有两个来源，缺一不可：
      A. 知识库 likely_params（带先验）
      B. 数据驱动：实际数据里与目标显著相关、且角色为可调控因子的参数
    只留 A 会漏掉知识库没预设的真因（例如烘箱二区温度漂移）；
    只留 B 又会掉进"相关不等于因果"。所以两条都进候选，再按同一把尺子排序。
    """
    kb = _kb()
    info = kb["defect_rootcause"].get(defect_name)
    proc = process or (info or {}).get("process")
    if not info and not col_map:
        return None
    cands = []
    n_param = len((info or {}).get("likely_params", []))
    try:
        from . import kbstore as _KBS
    except Exception:
        import kbstore as _KBS
    hist = {}       # pk -> 历史验证对策摘要
    for i, pk in enumerate((info or {}).get("likely_params", [])):
        spec = kb["params"].get(pk, {})
        prior = 1.0 - i / max(1, n_param) * 0.6  # 越靠前先验越高
        # 现场已验证对策带来的先验加成：把过去的经验折算成先验，避免每次从零起步
        try:
            bonus, case = _KBS.recommend_prior(defect_name, pk)
        except Exception:
            bonus, case = 0.0, None
        if case:
            hist[pk] = case
            prior = min(1.3, prior + bonus)
        ev, det = _data_evidence(table, pk, target_col, proc or info["process"])
        tscore, tdet = 0.0, None
        if det.get("column") and target_col:
            tscore, tdet = _temporal_match(table, det["column"], target_col)
        verifiability = 0.5
        score = 0.40 * ev + 0.35 * prior + 0.15 * tscore + 0.10 * verifiability
        score, gate_note = _evidence_gate(score, det)
        cands.append({
            "param_key": pk,
            "param": det.get("column"),  # 原始列名，供后续取规格与实际数据用
            "param_name": spec.get("name", pk),
            "unit": spec.get("unit", ""),
            "score": round(score, 4),
            "evidence_score": round(ev, 3),
            "prior_score": round(prior, 3),
            "temporal_score": round(tscore, 3),
            "data_status": det.get("status"),
            "gate_note": gate_note,
            "source": "知识库先验",
            "detail": det,
            "temporal": tdet,
            "note": spec.get("note", ""),
            "history_case": ({
                "id": hist[pk].get("id"),
                "level": hist[pk].get("evidence_level"),
                "action": (hist[pk].get("countermeasure") or {}).get("action"),
                "result": (hist[pk].get("verification") or {}).get("result"),
                "doc_no": (hist[pk].get("standardization") or {}).get("doc_no"),
                "source": (hist[pk].get("_kbmeta") or {}).get("source"),
            } if pk in hist else None),
        })
    # ---- 候选来源 B：数据驱动 ------------------------------------------
    # 知识库的 likely_params 是"常见原因"清单，不可能穷举每条产线的真因。
    # 数据里明明与目标强相关、且确实是可调控因子的参数，必须有资格进候选，
    # 否则真因永远排不上号——哪怕它的 r 高达 0.8。
    kb_keys = {c["param_key"] for c in cands}
    if col_map and target_col:
        allowed = _chain_processes(proc)
        for c, pk in col_map.items():
            if not pk or pk in kb_keys:
                continue
            # 护栏：编号/时间/坐标/计数列与测试条件（辐照、组件温度）不是产品原因（v1.10）
            if QG.is_test_condition(c) or QG.role_of(table, c) not in ("measure", None):
                continue
            pspec = kb["params"].get(pk, {})
            if pspec.get("role") == "response":
                continue                       # 结果指标不能当原因（否则把 TTV 当成 TTV 超差的原因）
            pproc = pspec.get("process")
            if allowed and pproc and pproc not in allowed:
                continue                       # 挡掉跨工序串味的参数
            ev, det = _data_evidence(table, pk, target_col, proc or pproc or "")
            if det.get("status") != "已测":
                continue
            r = det.get("r")
            p = det.get("p")
            if r is None or abs(r) < 0.20:
                continue
            if p is not None and p > 0.05:
                continue
            tscore, tdet = (0.0, None)
            if det.get("column"):
                tscore, tdet = _temporal_match(table, det["column"], target_col)
            prior = 0.35                       # 先验低于知识库候选，但证据硬
            score = 0.40 * ev + 0.35 * prior + 0.15 * tscore + 0.10 * 0.5
            cands.append({
                "param_key": pk,
                "param": det.get("column"),
                "param_name": pspec.get("name", pk),
                "unit": pspec.get("unit", ""),
                "score": round(score, 4),
                "evidence_score": round(ev, 3),
                "prior_score": round(prior, 3),
                "temporal_score": round(tscore, 3),
                "data_status": det.get("status"),
                "gate_note": None,
                "source": "数据驱动",
                "detail": det, "temporal": tdet,
                "note": pspec.get("note", ""),
                "history_case": None,
            })
    # 护栏：知识库先验候选若对应的是测试条件或非测量列，同样剔除
    # v1.10：目标列本身（或与目标同一参数键）不能作为它自己的原因
    _tgt_key = (col_map or {}).get(target_col) if target_col else None
    _excl = set(exclude_cols or [])
    cands = [d for d in cands
             if not (target_col and d.get("param") == target_col)
             and d.get("param") not in _excl
             and not (_tgt_key and d.get("param_key") == _tgt_key)
             and not (QG.is_test_condition(d.get("param") or "") or QG.is_test_condition(d.get("param_name") or "")
                     or d.get("param_key") in ("irradiance", "module_temp"))
             and (d.get("param") is None or QG.role_of(table, d.get("param")) == "measure")]
    if not cands:
        return None
    cands.sort(key=lambda d: -d["score"])
    return {
        "defect": defect_name,
        "process": proc,
        "process_name": kb["processes"].get(proc, {}).get("name", "") if proc else "",
        "mechanism": (info or {}).get("mechanism"),
        "verify_data": (info or {}).get("verify_data", []),
        "quick_checks": (info or {}).get("quick_checks", []),
        "candidates": cands[:top],
        "all_candidates": cands,
        "history_cases": [hist[k] for k in hist],
    }


# ------------------------------------------------------------------ 5Why

def _unregistered_hint(defect_name, layer="defect"):
    """缺陷未收录时的可行动提示（分「工序层」与「机理层」两种措辞）。

    只回一句「机理未收录」等于把问题原样丢回给工程师 —— 他早就看见了，
    他真正需要知道的是两件事：
      1. 这份报告还能信到哪一步（答：第 3 层以下的数据证据不受影响）
      2. 怎么把这条补进知识库（答：给可直接复制的命令）

    两层用不同措辞：第 1 层说「定位不到工序」，第 2 层说「机理缺失」，
    否则工程师会看到同一句话印两遍，反而以为报告出错了。
    """
    if layer == "process":
        return ("「%s」尚未收录进根因知识库，无法自动定位所属工序，"
                "故本层无工序机理可引用。可运行时用 `--process <工序>` 显式指定，"
                "或按第 2 层提示补录该缺陷条目。" % defect_name)
    return ("「%s」尚未收录进根因知识库，机理先验缺失。"
            "但请注意：第 3 层以下的数据证据不受影响 —— 关联强度、分组差异、"
            "时序吻合度都是从你的数据直接算出的，仍然可用。"
            "补录方式：先 `python scripts/kb_manage.py scaffold --type defect_new` 取骨架，"
            "填 key / process / mechanism 后 propose，"
            "再由负责人 review --approve 并 sync 生效。" % defect_name)


def build_5why(defect_name, top_causes, table=None, target_col=None):
    """生成 5Why 链条（基于知识库机理 + 数据事实），并标注哪些层有数据支撑"""
    kb = _kb()
    info = kb["defect_rootcause"].get(defect_name, {})
    proc = kb["processes"].get(info.get("process", ""), {})
    lead = top_causes[0] if top_causes else None
    chain = []
    pnotes = proc.get("notes")
    known = bool(info)
    # 未收录时不能只回「机理未收录」——那是把问题原样丢回给工程师。
    # 要同时说清：本报告剩余部分还能信到哪一步、以及怎么把这条补上。
    fallback = _unregistered_hint(defect_name, "process") if not known else \
        "该工序的工艺机理尚未在知识库中细化"

    chain.append({
        "level": 1,
        "why": "为什么出现「%s」？" % defect_name,
        "because": pnotes or fallback,
        "evidence": "知识库工序机理" if pnotes else "知识库未收录（可补录）",
        "supported": bool(pnotes),
    })
    chain.append({
        "level": 2,
        "why": "为什么该工序会出现这种状态？",
        "because": info.get("mechanism") or _unregistered_hint(defect_name),
        "evidence": "知识库缺陷机理",
        "supported": bool(info.get("mechanism")),
    })
    if lead:
        d = lead.get("detail", {})
        ev = []
        if d.get("r") is not None:
            ev.append("与目标相关性 r=%.3f (p=%.4g, n=%d)" % (d["r"], d.get("p") or 0, d.get("n", 0)))
        if d.get("oos_pct") is not None:
            ev.append("超规格 %.1f%%（%d 点）" % (d["oos_pct"], d.get("out_of_spec", 0)))
        if d.get("cv_pct") is not None:
            ev.append("CV=%.2f%%" % d["cv_pct"])
        if d.get("status") == "数据缺失":
            ev.append("该参数在当前数据中不存在，需补充：%s" % d.get("need"))
        chain.append({
            "level": 3,
            "why": "为什么指向「%s」？" % lead.get("param_name"),
            "because": "；".join(ev) if ev else "证据不足，需补充数据",
            "evidence": "数据证据（得分 %.2f）" % lead.get("score", 0),
            "supported": bool(ev) and d.get("status") != "数据缺失",
        })
        chain.append({
            "level": 4,
            "why": "为什么「%s」会发生异常？" % lead.get("param_name"),
            "because": lead.get("note", "") or "需结合设备状态、来料批次、操作变更追溯",
            "evidence": "参数机理（需现场确认）",
            "supported": bool(lead.get("note")),
        })
        chain.append({
            "level": 5,
            "why": "为什么没有被及时发现/拦截？",
            "because": "现行控制计划对该参数的监控频次/SPC 判异规则未覆盖，或拦截点设置在该工序之后",
            "evidence": "需核对 Control Plan 与检测点布局",
            "supported": False,
        })
    return chain


# ------------------------------------------------------------------ 数据缺口

def assess_gaps(table, defect_name=None, target_col=None, groups=None):
    """评估数据缺口，返回 P0/P1/P2 清单（用于向用户弹窗索取）"""
    kb = _kb()
    gaps = []
    # 时间戳（或可排序序列）
    tc, times = PP.parse_time_column(table)
    has_time = tc is not None and sum(1 for t in (times or []) if t) > 0
    if not has_time:
        gaps.append({"level": "P0", "item": "时间戳 / 批次序号",
                     "why": "无法做时序分析与滞后相关，无法定位异常起始点",
                     "impact": "高"})
    # 分组维度：优先使用流水线已识别的分组列，避免两处关键词表不一致造成漏检
    group_like = list(groups or [])
    if not group_like:
        group_like = [c for c in table.columns
                      if any(k in PP._norm_name(c) for k in
                             ["设备", "机台", "机号", "machine", "线体", "产线", "生产线", "line",
                              "班次", "shift", "批次", "batch", "lot", "卷号", "供应商",
                              "supplier", "通道", "channel", "柜号", "柜", "模", "腔", "tool",
                              "刀具", "刀号", "班组", "工单", "订单"])]
    if not group_like:
        gaps.append({"level": "P0", "item": "分组维度（设备号/批次/班次）",
                     "why": "无法做分层 Pareto 与组间 ANOVA，无法区分是全局问题还是单台设备问题",
                     "impact": "高"})
    # 目标指标
    if not target_col:
        gaps.append({"level": "P0", "item": "被分析指标（不良率/测量值）",
                     "why": "缺少响应变量，无法计算相关性",
                     "impact": "高"})
    # 工艺参数
    col_map = PP.map_columns(table)
    matched = [v for v in col_map.values() if v]
    if len(matched) < 2:
        gaps.append({"level": "P1", "item": "对应工序的工艺参数实测值",
                     "why": "当前可识别的标准工艺参数不足 2 个，根因只能给出知识库先验",
                     "impact": "中"})
    # 规格
    if defect_name:
        info = kb["defect_rootcause"].get(defect_name, {})
        for pk in info.get("likely_params", [])[:3]:
            spec = kb["params"].get(pk, {})
            ctrl = spec.get("control", {})
            if ctrl.get("mode") in ("usl", "lsl", "range"):
                continue
            gaps.append({"level": "P1",
                         "item": "%s 的企业规格上下限（USL/LSL）" % spec.get("name", pk),
                         "why": "知识库只提供行业典型值，用典型值算 Cpk 会失真",
                         "impact": "中"})
    # 历史基线
    if table and len(table) < 30:
        gaps.append({"level": "P2", "item": "历史基线数据（≥30 点）",
                     "why": "样本量 %d 偏少，控制限与能力指数置信度不足" % len(table),
                     "impact": "中"})
    return gaps


def confidence_level(gaps, evidence_count):
    """根据缺口与证据数给出置信度"""
    p0 = sum(1 for g in gaps if g["level"] == "P0")
    if p0 >= 2:
        return {"level": "低", "score": 0.3,
                "note": "关键维度缺失较多，结论以知识库先验为主，须现场验证"}
    if p0 == 1:
        return {"level": "中低", "score": 0.5,
                "note": "存在 1 项关键缺失，部分结论需现场确认"}
    if evidence_count >= 3:
        return {"level": "中高", "score": 0.75,
                "note": "数据维度基本齐备，有多项统计证据支撑"}
    return {"level": "中", "score": 0.6,
            "note": "数据维度齐备但统计证据偏少，建议补充样本后复算"}
