# -*- coding: utf-8 -*-
"""
adapt.py —— 字段匹配不上 / 互相冲突时的自动适配层

现场数据永远不会照着知识库长。这一层的职责不是"匹配得更好"，
而是**当匹配失败或自相矛盾时，让分析继续跑完，并把每一次自动决策写清楚**。

核心原则（顺序不能颠倒）：
  1. 宁可少判，不可错判 —— 认不出来的列不参与工艺判定，但不拦着报告出
  2. 能确定性解决的当场解决（重复列名、重复命中）—— 只改本次会话，不改库
  3. 需要新知识才能解决的，进候选区排队 —— 机器不直接改知识库
  4. 任何自动写入都只落在**租户私有层**，基线对所有企业保持一致

本模块所有函数保证不抛异常：适配层自己崩了，是最讽刺的失败方式。
"""

import re
import os
import json

try:
    from . import table as T
    from . import preprocess as PP
    from . import tenancy as TN
except Exception:                        # 直接当脚本跑时回退
    import table as T                    # noqa
    import preprocess as PP              # noqa
    import tenancy as TN                 # noqa


_UNIT_IN_NAME = re.compile(r"[（(]([^)）]+)[)）]\s*$")


def _unit_of(col):
    """从列名尾部括号里取单位：'硅片厚度(mm)' -> 'mm'"""
    m = _UNIT_IN_NAME.search(str(col))
    return m.group(1).strip() if m else None


def _missing_count(table, col):
    idx = table.col_index(col)
    if idx is None:
        return 10 ** 9
    n = 0
    for r in table.rows:
        v = r[idx] if idx < len(r) else None
        if v is None or str(v).strip() == "":
            n += 1
    return n


def _cardinality(table, col):
    idx = table.col_index(col)
    if idx is None:
        return 0
    vals = set()
    for r in table.rows:
        v = r[idx] if idx < len(r) else None
        if v is None or str(v).strip() == "":
            continue
        vals.add(str(v).strip())
    return len(vals)


def _numeric_ratio(table, col):
    idx = table.col_index(col)
    if idx is None:
        return 0.0
    tot = hit = 0
    for r in table.rows:
        v = r[idx] if idx < len(r) else None
        if v is None or str(v).strip() == "":
            continue
        tot += 1
        if T.to_number(v) is not None:
            hit += 1
    return (hit / float(tot)) if tot else 0.0


# ------------------------------------------------------------------ 诊断

def diagnose(table, col_map, kb=None, process=None):
    """
    列出这张表在字段层面的所有问题。只描述，不改任何东西。
    col_map: {列名: 参数key 或 None}
    """
    issues = []
    if table is None:
        return issues
    kb = kb if kb is not None else (PP.load_knowledge() if PP else {})
    params = (kb or {}).get("params", {})

    # 1) 表内重复列名：csv 里很常见，下游取列会静默取到第一个
    seen = {}
    for i, c in enumerate(table.columns):
        seen.setdefault(c, []).append(i)
    for c, positions in seen.items():
        if len(positions) > 1:
            issues.append({"type": "dup_column_name", "column": c,
                           "count": len(positions),
                           "detail": "表内第 %s 列重名，后续同名列会被静默忽略"
                                     % "、".join(str(p + 1) for p in positions),
                           "severity": "high", "fixable": "auto"})

    # 2) 多个列命中同一个参数（典型：同一参数导出了 mm 与 μm 两版）
    by_param = {}
    for c, k in (col_map or {}).items():
        if k:
            by_param.setdefault(k, []).append(c)
    for k, cols in by_param.items():
        if len(cols) > 1:
            want = (params.get(k) or {}).get("unit")
            # 仲裁：优先单位与知识库一致的；其次缺失少；其次数值比例高
            def _score(c):
                return ((_unit_of(c) == want) if want else False,
                        -_missing_count(table, c),
                        _numeric_ratio(table, c))
            ranked = sorted(cols, key=_score, reverse=True)
            keep = ranked[0]
            issues.append({"type": "dup_param_hit", "param": k, "columns": cols,
                           "keep": keep, "drop": [c for c in cols if c != keep],
                           "unit_expected": want,
                           "detail": "「%s」等 %d 列都命中参数 %s，保留 %s，其余退出工艺判定"
                                     % ("、".join(cols), len(cols), k, keep),
                           "severity": "high", "fixable": "auto"})

    # 3) 单位冲突：列带单位但与库里登记的不一致，且没有可用换算
    for c, k in (col_map or {}).items():
        if not k:
            continue
        spec = params.get(k) or {}
        want = spec.get("unit")
        got = _unit_of(c)
        if not want or not got:
            continue
        # 设备层已经换算过的列名形如「硅片厚度(mm→μm)」，别再判一次单位冲突
        if "→" in got or "->" in got:
            continue
        if _canon(got) == _canon(want):
            continue
        conv = _conversion_factor(got, want)
        issues.append({"type": "unit_conflict", "column": c, "param": k,
                       "unit_in_file": got, "unit_expected": want,
                       "convertible": conv is not None, "factor": conv,
                       "detail": ("可按 ×%g 换算到 %s" % (conv, want)) if conv is not None
                                 else ("单位 %s 与库里登记的 %s 不一致且无换算规则，"
                                       "该列退出工艺判定" % (got, want)),
                       "severity": "mid" if conv is not None else "high",
                       "fixable": "auto" if conv is not None else "need_knowledge"})

    # 4) 认不出来的列
    for c in table.columns:
        if (col_map or {}).get(c):
            continue
        if _numeric_ratio(table, c) == 0.0:
            # 机台号/班次这类文本列不是"没用"，它们是横向排查的分组维度，
            # 跟真正无法解析的垃圾列混为一谈会让用户以为数据有问题
            card = _cardinality(table, c)
            n = len(table.rows) or 1
            if 1 < card <= max(2, int(n * 0.6)):
                issues.append({"type": "group_dimension", "column": c, "levels": card,
                               "detail": "文本维度列（%d 个取值），可用于横向分组对比，"
                                         "但不作为测量值参与统计" % card,
                               "severity": "low", "fixable": "auto"})
            else:
                issues.append({"type": "not_measurable", "column": c,
                               "detail": "该列没有可解析的数值，不参与统计",
                               "severity": "low", "fixable": "auto"})
            continue
        issues.append({"type": "unmatched_column", "column": c,
                       "unit": _unit_of(c),
                       "detail": "列名「%s」未匹配到任何标准参数，本次不参与工艺判定"
                                 % c,
                       "severity": "mid", "fixable": "need_knowledge"})

    return issues


def _canon(u):
    return re.sub(r"[\s·]", "", str(u or "")).lower()


def _conversion_factor(src, dst):
    try:
        from . import devices as DEV
    except Exception:
        try:
            import devices as DEV
        except Exception:
            return None
    try:
        prof = DEV.load_profiles() or {}
        conv = prof.get("unit_conversions") or {}
        # 注意签名是 (convs, key, src, dst)，别按直觉写成 (src, dst, convs)
        return DEV._conv_factor(conv, None, src, dst)
    except Exception:
        return None


# ------------------------------------------------------------------ 自动适配

def apply_safe(table, issues, col_map):
    """
    执行确定性适配（不需要新知识、不改变语义的那部分），返回新的 (table, col_map)。
    不做的事：猜新知识。猜错的代价远高于少判一条。
    """
    applied = []
    col_map = dict(col_map or {})
    if table is None:
        return table, col_map, applied

    # 重复列名 → 去重命名
    dup = [i for i in issues if i["type"] == "dup_column_name"]
    if dup:
        counts = {}
        new_cols = []
        for c in table.columns:
            counts[c] = counts.get(c, 0) + 1
            new_cols.append(c if counts[c] == 1 else "%s#%d" % (c, counts[c]))
        if new_cols != list(table.columns):
            # 列名改了，col_map 的键必须跟着改，否则整列会静默失联
            rename = dict(zip(table.columns, new_cols))
            col_map = {rename.get(c, c): v for c, v in col_map.items()}
            table = T.Table(new_cols, [list(r) for r in table.rows], table.name)
            applied.append({"type": "dup_column_name",
                            "action": "重名列已加序号区分：%s"
                                      % "、".join(i["column"] for i in dup)})

    # 重复命中 → 只保留仲裁胜出的那一列
    for i in [x for x in issues if x["type"] == "dup_param_hit"]:
        for c in i.get("drop", []):
            if c in col_map:
                col_map.pop(c)
        applied.append({"type": "dup_param_hit",
                        "action": "参数 %s 保留「%s」，「%s」退出工艺判定（避免同参数双列互相干扰）"
                                  % (i["param"], i["keep"], "、".join(i.get("drop", [])))})

    # 不可换算的单位冲突 / 不可测列 → 退出判定
    for i in issues:
        if i["type"] == "unit_conflict" and not i.get("convertible"):
            if i["column"] in col_map:
                col_map.pop(i["column"])
            applied.append({"type": "unit_conflict",
                            "action": "「%s」单位 %s 与库内 %s 无换算规则，退出工艺判定（数据保留）"
                                      % (i["column"], i["unit_in_file"], i["unit_expected"])})
        elif i["type"] == "not_measurable" and i["column"] in col_map:
            col_map.pop(i["column"])

    return table, col_map, applied


def queue_candidates(issues, kb=None, source=None, auto_approve=False):
    """
    把"需要新知识才能解决"的问题变成候选，投进（租户的）待审区。
    auto_approve=True 时也会走 sync，但**只落到租户层**——基线不受影响。
    """
    queued = []
    try:
        from . import kbstore as KBS
    except Exception:
        import kbstore as KBS                        # noqa
    src = dict(source or {})
    src.setdefault("source", "auto_adapt")

    for i in issues:
        if i.get("fixable") != "need_knowledge":
            continue
        try:
            if i["type"] == "unmatched_column":
                guess = _guess_param(i["column"], kb)
                if guess:
                    payload = {"param_key": guess[0], "alias": i["column"],
                               "note": "自动适配提议：把「%s」登记为参数 %s 的别名（相似度 %.2f）"
                                       % (i["column"], guess[0], guess[1])}
                    ctype = "param_alias"
                else:
                    payload = {"key": _slug(i["column"]), "name": i["column"],
                               "unit": i.get("unit") or "",
                               "aliases": [i["column"]],
                               "note": "自动适配提议：新增参数（库内无相近条目）"}
                    ctype = "param"
            elif i["type"] == "unit_conflict":
                payload = {"from": i["unit_in_file"], "to": i["unit_expected"],
                           "factor": None,
                           "note": "自动适配提议：补充单位换算规则 %s→%s"
                                   % (i["unit_in_file"], i["unit_expected"])}
                ctype = "unit_conversion"
            else:
                continue
            rec, err = KBS.add_candidate(ctype, payload, source=src, by="auto_adapt")
            if not rec:
                queued.append({"type": i["type"], "ok": False, "error": err})
                continue
            if auto_approve:
                KBS.update_candidate(rec["id"], {"status": "approved",
                                                 "review": {"reviewer": "auto_adapt",
                                                            "note": "自动适配（仅租户层生效）"}})
                r = KBS.sync(reviewer="auto_adapt")
                queued.append({"type": i["type"], "ok": True, "id": rec["id"],
                               "auto_approved": True, "sync": r.get("merged")})
            else:
                queued.append({"type": i["type"], "ok": True, "id": rec["id"],
                               "auto_approved": False})
        except Exception as e:
            queued.append({"type": i.get("type"), "ok": False, "error": str(e)})
    return queued


def _slug(name):
    s = re.sub(r"[（(][^)）]*[)）]", "", str(name))
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", s).strip("_")
    return s or "unknown_param"


def _guess_param(col, kb, floor=0.72):
    """给认不出来的列找个最像的参数，用于生成别名候选（不是判定）"""
    try:
        hit = PP.match_param(col, prefer_process=None)
        if hit and hit[1] >= floor:
            return hit[0], hit[1]
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ 统一入口

def adapt(table, col_map, kb=None, process=None, auto_approve=False,
          queue=True, source=None):
    """
    一次做完：诊断 → 确定性适配 → 需要新知识的排队。

    返回 {"table","col_map","issues","applied","queued","notes","degraded"}
    degraded=True 表示这张表有列被排除在工艺判定之外，报告必须写明。
    """
    out = {"issues": [], "applied": [], "queued": [], "notes": [], "degraded": False}
    try:
        issues = diagnose(table, col_map, kb=kb, process=process)
        out["issues"] = issues
        table2, col_map2, applied = apply_safe(table, issues, col_map)
        out["table"], out["col_map"], out["applied"] = table2, col_map2, applied
        if queue:
            out["queued"] = queue_candidates(issues, kb=kb, source=source,
                                             auto_approve=auto_approve)
        unmatched = [i for i in issues if i["type"] in ("unmatched_column",)
                     or (i["type"] == "unit_conflict" and not i.get("convertible"))]
        if unmatched:
            out["degraded"] = True
            names = "、".join(i["column"] for i in unmatched)
            out["notes"].append(
                "以下列未纳入工艺判定（数据保留，结论不含它们的证据）：%s。"
                "如需纳入，请登记别名/参数后重跑" % names)
        if not [k for k in col_map2.values() if k]:
            out["degraded"] = True
            out["notes"].append(
                "没有任何一列匹配到标准参数，本次只能做通用统计，不输出工艺结论")
    except Exception as e:
        # 适配层自身异常绝不能把整轮分析带走
        out["notes"].append("字段适配层异常（已跳过，按原始列继续）：%s" % e)
        out["error"] = str(e)
    return out
