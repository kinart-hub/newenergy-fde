# -*- coding: utf-8 -*-
"""
设备身份层 —— 让「同一台设备」第二次上传时被认出来，而不是重新猜一遍。

为什么需要这一层
────────────────
没有设备档案时，Agent 对每份导出都要现场猜三件事：版式（几行表头、有没有单位行、
有没有合计尾行）、列名对哪个标准参数、上下限取多少。这三件事是**跟着数据内容浮动**
的：同一台涂布机，这次导出带合计行、下次不带；这批数据里某列恰好全空；样本量从 200
掉到 60。猜的结果一变，进分析的列就变，规格上下限就变，Cpk 跟着变——
用户看到的就是「同一台设备的报告结论跳来跳去」。

把设备身份固定下来之后，这三件事不再猜，而是查档案。这是稳定性的根本来源。

三个能力
────────
1. fingerprint —— 从导出文件抽出稳定签名（列名集合 + 版式特征），下次靠相似度认出
2. interview   —— 认不出时，生成一份待填问卷（能自动推断的先预填，只问真正缺的）
3. register    —— 把问卷答案变成设备档案，写进**该企业私有层**，并生成设备记忆文档

边界：本模块只写租户私有层，永远不碰共享基线。
"""

import os
import io
import re
import json
import time
import hashlib

try:
    from . import table as T
    from . import preprocess as PP
    from . import devices as DEV
    from . import tenancy as TN
except Exception:                                    # 直接脚本调用
    import table as T                                # noqa
    import preprocess as PP                          # noqa
    import devices as DEV                            # noqa
    import tenancy as TN                             # noqa


# ──────────────────────────────────────────────────────────── 指纹

_UNIT_BRACKET = re.compile(r"[（(]\s*[^）()]{1,12}\s*[）)]\s*$")


def _canon_col(name):
    """列名归一：去单位括号、去空白与分隔符、转小写。

    「涂布速度(m/min)」和「涂布速度」必须是同一个东西，否则设备换了个导出模板
    就认不出来了。
    """
    s = str(name or "").strip()
    s = _UNIT_BRACKET.sub("", s)
    return PP._norm_name(s)


def fingerprint(table, path=None):
    """
    生成设备的稳定签名。

    只用「结构」不用「数值」：样本量变化、批次变化都不应改变指纹，
    否则指纹本身就变成了另一个不稳定源。
    """
    cols = [_canon_col(c) for c in table.columns]
    cols = [c for c in cols if c]
    col_sig = hashlib.md5("|".join(sorted(cols)).encode("utf-8")).hexdigest()[:12]

    # 版式特征：能自动判出来的就固定下来，下次不再猜
    fmt = {}
    try:
        if DEV.looks_like_unit_row([str(v) for v in (table.rows[0] if table.rows else [])]):
            fmt["unit_row"] = True
    except Exception:
        pass
    try:
        foot = DEV.detect_footer_rows(table)
        if foot:
            fmt["footer_rows"] = len(foot)
    except Exception:
        pass
    fmt["n_cols"] = len(table.columns)
    fmt["has_text_dim"] = bool(table.text_columns())

    # 数值列的量级带（只取粗粒度，避免逐批浮动）
    bands = []
    for c in table.numeric_columns()[:12]:
        vals = [v for v in table.numeric_column(c) if v is not None]
        if len(vals) >= 5:
            mean = sum(vals) / float(len(vals))
            bands.append(int(min(9, max(0, len(str(int(abs(mean))))) )) if mean else 0)
    return {
        "col_sig": col_sig,
        "cols": sorted(cols),
        "n_cols": len(table.columns),
        "format": fmt,
        "bands": bands,
        "file": os.path.basename(path or ""),
    }


def _jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def match_fingerprint(fp, profiles=None, threshold=0.72):
    """
    用指纹在档案库里找设备。

    用 Jaccard 相似度而不是精确相等：设备固件升级后常多出/少掉一两列，
    精确匹配会让「明明是同一台机器」认不出来，又退回猜的状态。
    """
    profiles = profiles if profiles is not None else DEV.load_profiles()
    devices = profiles.get("devices", {}) or {}
    cols = fp.get("cols") or []
    best, best_sim, second = None, 0.0, None
    sims = []
    for key, prof in devices.items():
        sig = prof.get("signature") or {}
        known = sig.get("cols") or []
        sim = 1.0 if (fp.get("col_sig") and sig.get("col_sig") == fp.get("col_sig")) \
            else _jaccard(cols, known)
        # 版式一致再加权：列名像但版式不同，多半不是同一台
        if sig.get("format") and fp.get("format"):
            same = sum(1 for k, v in fp["format"].items()
                       if k in ("unit_row", "footer_rows", "n_cols") and sig["format"].get(k) == v)
            sim += 0.05 * same
        sims.append((key, round(sim, 3)))
        if sim > best_sim:
            second = (best, best_sim)
            best, best_sim = key, sim
        elif sim > (best_sim if second is None else second[1]):
            second = (key, sim)
    sims.sort(key=lambda x: -x[1])
    if best_sim < threshold:
        return {"device": None, "similarity": round(best_sim, 3),
                "candidates": sims[:3], "threshold": threshold}
    conf = "高" if best_sim >= 0.9 else ("中" if best_sim >= 0.8 else "指纹低置信")
    return {"device": best, "similarity": round(best_sim, 3), "confidence": conf,
            "runner_up": second, "candidates": sims[:3]}


# ──────────────────────────────────────────────────────────── 访谈问卷

REQUIRED_FIELDS = [
    ("vendor", "设备厂商/品牌", "如 HIOKI、基恩士、先导智能"),
    ("model", "设备型号", "如 BT3562、LS-9000、自研涂布机 No.3"),
    ("processes", "所属工序", "battery_coating / battery_winding / battery_formation / pv_wafer / pv_cell_el / pv_module_iv"),
    ("layout", "导出版式", "wide（一行一个样品）还是 long（一行一个检测项）"),
    ("units", "各检测项单位", "如 涂布速度=m/min、面密度=g/m2"),
    ("aliases", "列名 → 标准参数", "如 极片残水 → moisture_after_dry"),
    ("default_specs", "默认规格上下限（可选但强烈建议）", "如 单面面密度=[190,210]"),
]


def interview(table, path=None, device_id=None):
    """
    未知设备时生成待填问卷。能自动推断的先预填，只问真正缺的——
    让企业填十项他们会放弃，填三项他们愿意。
    """
    fp = fingerprint(table, path)
    hit = match_fingerprint(fp)
    identified = DEV.identify_device(table, path=path)

    # 自动推断版式
    layout = "long" if _looks_long(table) else "wide"
    fmt = {"layout": layout, "header_rows": 1}
    try:
        if DEV.looks_like_unit_row([str(v) for v in (table.rows[0] if table.rows else [])]):
            fmt["unit_row"] = True
    except Exception:
        pass
    try:
        foot = DEV.detect_footer_rows(table)
        if foot:
            fmt["footer_markers"] = ["合计", "平均", "汇总", "Total", "Mean", "Average"]
    except Exception:
        pass

    # 自动推断列名→参数（能匹配上的直接给出，匹配不上的列出来让人确认）
    col_map = PP.map_columns(table)
    known, unknown = {}, []
    for c in table.columns:
        k = (col_map or {}).get(c)
        if k:
            known[c] = k
        else:
            unknown.append(c)

    # 自动推断单位（列名括号里的 + 单位行）
    units = {}
    for c in table.columns:
        m = _UNIT_BRACKET.search(str(c))
        if m:
            units[c] = m.group(1).strip("（）() ")
    if not units and table.rows:
        for i, c in enumerate(table.columns):
            v = table.rows[0][i] if i < len(table.rows[0]) else None
            if v is not None and T.to_number(str(v)) is None and str(v).strip():
                units[c] = str(v).strip()

    proc = None
    try:
        # 必须带上已推断出的列映射，否则多数列对不上参数，工序判定直接落空
        scores = PP.detect_process(table, col_map)
        if scores:
            proc = scores[0][0] if isinstance(scores[0], (list, tuple)) else scores[0]
    except Exception:
        pass

    filled = {"vendor": None, "model": None, "processes": [proc] if proc else None,
              "layout": layout, "units": units or None,
              "aliases": known or None, "default_specs": None}
    missing = [f for f, _lbl, _hint in REQUIRED_FIELDS if not filled.get(f)]

    return {
        "file": os.path.basename(path or ""),
        "fingerprint": {"col_sig": fp["col_sig"], "n_cols": fp["n_cols"],
                        "cols": fp["cols"][:30]},
        "identified_as": (identified or {}).get("device"),
        "identified_score": (identified or {}).get("score"),
        "fingerprint_match": hit,
        "auto_filled": filled,
        "missing": missing,
        "unmapped_columns": unknown,
        "suggested_format": fmt,
        "questions": [{"field": f, "label": lbl, "hint": hint}
                      for f, lbl, hint in REQUIRED_FIELDS if f in missing],
        "note": ("已能自动认出，无需录入" if (identified or {}).get("device")
                 else "未识别设备：请把 missing 里的项问清楚，再执行 devices --register"),
    }


def _looks_long(table):
    txt = table.text_columns()
    num = table.numeric_columns()
    if not txt or len(num) > 3:
        return False
    c = txt[0]
    vals = [str(v) for v in table.column(c) if v not in (None, "")]
    return len(set(vals)) < len(vals) * 0.6


# ──────────────────────────────────────────────────────────── 档案构建

def build_profile(answers, table=None, path=None, device_id=None):
    """
    把问卷答案变成设备档案。缺失项不臆造——宁可留空，也不填一个看起来合理的错值。
    """
    fp = fingerprint(table, path) if table is not None else {}
    vendor = (answers.get("vendor") or "").strip()
    model = (answers.get("model") or "").strip()
    procs = answers.get("processes") or []
    if isinstance(procs, str):
        procs = [p.strip() for p in re.split(r"[,，/、]", procs) if p.strip()]
    name = answers.get("name") or ("%s %s" % (vendor, model)).strip() or "未命名设备"

    fmt = dict(answers.get("format") or (fp.get("format") or {}))
    fmt.setdefault("layout", answers.get("layout") or "wide")
    fmt.setdefault("header_rows", 1)

    aliases = {}
    for k, v in (answers.get("aliases") or {}).items():
        if v:
            aliases[str(k)] = v

    unit_map = {}
    for k, v in (answers.get("units") or {}).items():
        if v:
            unit_map[str(k)] = v

    prof = {
        "name": name,
        "vendor": vendor or "unknown",
        "model": model or "",
        "category": answers.get("category") or "unknown",
        "processes": procs,
        "export_note": answers.get("export_note") or "",
        "format": fmt,
        "aliases": aliases,
        "unit_map": unit_map,
        "drop_cols": answers.get("drop_cols") or [],
        "default_specs": answers.get("default_specs") or {},
        "signature": {
            "col_sig": fp.get("col_sig"),
            "cols": fp.get("cols"),
            "format": fp.get("format"),
            "meta_regex": [x for x in ([vendor, model] + list(answers.get("meta_regex") or [])) if x],
            "col_any": (fp.get("cols") or [])[:12],
            "filename_regex": list(answers.get("filename_regex") or []),
        },
        "source": {
            "tier": answers.get("source_tier") or "site_provided",
            "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "registered_by": answers.get("registered_by") or "",
            "evidence": answers.get("evidence") or [],
            "web_refs": answers.get("web_refs") or [],
        },
    }
    return prof


def enrich(profile, findings):
    """
    把全网检索到的参数标准回填进档案。

    findings: {"aliases": {...}, "units": {...}, "default_specs": {...},
               "web_refs": [{"title","url","note"}], "evidence": [...]}

    只**补充空缺**，不覆盖企业已确认的内容——企业自己的口径优先级最高，
    网上查到的只是候选，而且必须留下出处以便日后复核。
    """
    added = {"aliases": 0, "units": 0, "specs": 0, "refs": 0}
    for k, v in (findings.get("aliases") or {}).items():
        if v and k not in profile.setdefault("aliases", {}):
            profile["aliases"][k] = v
            added["aliases"] += 1
    for k, v in (findings.get("units") or {}).items():
        if v and k not in profile.setdefault("unit_map", {}):
            profile["unit_map"][k] = v
            added["units"] += 1
    for k, v in (findings.get("default_specs") or {}).items():
        if v and k not in profile.setdefault("default_specs", {}):
            profile["default_specs"][k] = v
            added["specs"] += 1
    src = profile.setdefault("source", {})
    for r in (findings.get("web_refs") or []):
        src.setdefault("web_refs", []).append(r)
        added["refs"] += 1
    for e in (findings.get("evidence") or []):
        src.setdefault("evidence", []).append(e)
    if findings.get("source_tier"):
        src["tier"] = findings["source_tier"]
    return added


# ──────────────────────────────────────────────────────────── 注册与记忆文档

def _tenant_profile_path():
    """设备档案在租户私有层的落点；未启用租户时返回 None（禁止写基线）"""
    if not TN.enabled():
        return None
    return TN.map_to_tenant(DEV.PROFILE_FILE)


def register(profile, key, tenant=None, force=False):
    """
    把设备档案写进该企业私有层。

    - 只动这家企业的私有层，共享基线一字不改
    - 合并而不是覆盖：这家企业可能已经登记过别的设备
    - 原子写 + 写前备份
    """
    if tenant:
        TN.set_tenant(tenant)
    target = _tenant_profile_path()
    if not target:
        return {"ok": False, "error": "未启用企业隔离（未指定 --tenant），拒绝写入共享基线。"
                                      "请带 --tenant <企业id> 注册为私有设备档案。"}
    # 双保险：落点必须在企业私有层之内。曾经因为设备档案路径没跟随 set_root，
    # 「写私有层」实际写进了共享基线；这道检查能在同类路径 bug 再出现时挡住它。
    try:
        _root = os.path.abspath(TN.root())
        _tgt = os.path.abspath(target)
        if os.path.commonpath([_root, _tgt]) != _root or "tenants" not in _tgt.replace("\\", "/"):
            return {"ok": False, "error": "写入落点不在企业私有层内（%s），已拒绝，共享基线未被改动。"
                                          % target}
    except Exception:
        pass

    os.makedirs(os.path.dirname(target), exist_ok=True)
    data = {"devices": {}, "unit_conversions": {}, "unit_aliases": {}}
    if os.path.exists(target):
        try:
            data = json.load(io.open(target, encoding="utf-8")) or data
        except Exception:
            pass
    for k in ("devices", "unit_conversions", "unit_aliases"):
        data.setdefault(k, {})

    existed = key in (data.get("devices") or {})
    if existed and not force:
        return {"ok": False, "error": "设备档案 %s 已存在，加 --force 覆盖" % key,
                "path": target}

    if existed:
        try:
            bak = target + ".bak-%s" % time.strftime("%Y%m%d%H%M%S")
            io.open(bak, "w", encoding="utf-8").write(
                json.dumps(data["devices"][key], ensure_ascii=False, indent=2))
        except Exception:
            pass

    profile = dict(profile)
    profile["source"] = dict(profile.get("source") or {})
    profile["source"]["layer"] = "tenant_private"
    profile["source"]["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["devices"][key] = profile

    tmp = "%s.tmp-%d" % (target, os.getpid())
    io.open(tmp, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=2))
    try:
        os.replace(tmp, target)
    except Exception:                                 # Windows 并发读会拒绝 replace
        try:
            from . import hotswap as HS
        except Exception:
            import hotswap as HS                      # noqa
        HS.atomic_write_json(target, data)
    try:
        DEV.invalidate_cache()
    except Exception:
        pass
    return {"ok": True, "key": key, "path": target, "created": not existed,
            "tenant": TN.current(), "devices_total": len(data["devices"])}


def dossier_md(key, profile, extra=None):
    """
    生成设备记忆文档。

    这不是给人看的说明书，是给**下一次分析**看的契约：这台设备怎么解析、
    列名对哪个参数、上下限取多少、依据是什么。写下来之后，
    同一台设备的第 100 次分析和第 1 次分析应该用同一套口径。
    """
    extra = extra or {}
    fmt = profile.get("format") or {}
    src = profile.get("source") or {}
    L = []
    L.append("# 设备记忆档案 · %s" % (profile.get("name") or key))
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append("| 档案 key | `%s` |" % key)
    L.append("| 厂商 / 型号 | %s / %s |" % (profile.get("vendor") or "—", profile.get("model") or "—"))
    L.append("| 所属工序 | %s |" % ("、".join(profile.get("processes") or []) or "—"))
    L.append("| 版式 | %s（表头 %s 行%s） |" % (
        fmt.get("layout") or "—", fmt.get("header_rows") or 1,
        "，含单位行" if fmt.get("unit_row") else ""))
    L.append("| 归属层 | %s |" % (src.get("layer") or "基线共享"))
    L.append("| 登记时间 | %s |" % (src.get("registered_at") or "—"))
    L.append("| 证据等级 | %s |" % (src.get("tier") or "—"))
    L.append("")

    L.append("## 1. 识别特征")
    sig = profile.get("signature") or {}
    L.append("- 列指纹：`%s`（%d 列）" % (sig.get("col_sig") or "—", len(sig.get("cols") or [])))
    if sig.get("meta_regex"):
        L.append("- 文件内标识：%s" % "、".join(str(x) for x in sig["meta_regex"] if x))
    if sig.get("filename_regex"):
        L.append("- 文件名特征：%s" % "、".join(sig["filename_regex"]))
    L.append("")

    al = profile.get("aliases") or {}
    if al:
        L.append("## 2. 列名 → 标准参数")
        L.append("")
        L.append("| 导出列名 | 标准参数 |")
        L.append("|---|---|")
        for k, v in al.items():
            L.append("| %s | `%s` |" % (k, v))
        L.append("")

    um = profile.get("unit_map") or {}
    if um:
        L.append("## 3. 单位")
        L.append("")
        for k, v in um.items():
            L.append("- %s：`%s`" % (k, v))
        L.append("")

    ds = profile.get("default_specs") or {}
    if ds:
        L.append("## 4. 默认规格（锁定后不再随数据浮动）")
        L.append("")
        L.append("| 列名 | 下限 | 上限 | 目标 |")
        L.append("|---|---|---|---|")
        for k, v in ds.items():
            if isinstance(v, dict):
                L.append("| %s | %s | %s | %s |" % (k, v.get("lsl", "—"),
                                                    v.get("usl", "—"), v.get("target", "—")))
            elif isinstance(v, list) and len(v) == 2:
                L.append("| %s | %s | %s | — |" % (k, v[0], v[1]))
        L.append("")
        L.append("> 这些上下限来自设备档案，分析时直接采用，不再用数据分布推定——")
        L.append("> 推定值会随样本量变化，是同一台设备报告结论跳动的常见来源。")
        L.append("")

    if src.get("web_refs"):
        L.append("## 5. 参考来源")
        L.append("")
        for r in src["web_refs"]:
            if isinstance(r, dict):
                L.append("- %s%s" % (r.get("title") or r.get("url") or "",
                                     (" — <%s>" % r["url"]) if r.get("url") else ""))
                if r.get("note"):
                    L.append("  - %s" % r["note"])
            else:
                L.append("- %s" % r)
        L.append("")
    if src.get("evidence"):
        L.append("## 6. 现场证据")
        L.append("")
        for e in src["evidence"]:
            L.append("- %s" % e)
        L.append("")

    L.append("## 7. 待补充")
    L.append("")
    gaps = [f for f in ("vendor", "model", "processes") if not profile.get(f)]
    if not (profile.get("default_specs") or {}):
        gaps.append("default_specs")
    if not (profile.get("aliases") or {}):
        gaps.append("aliases")
    L.append("- %s" % ("、".join(gaps) if gaps else "（无）"))
    L.append("")
    L.append("---")
    L.append("")
    L.append("下次上传这台设备的导出文件时，Agent 会先比对列指纹自动认出它，")
    L.append("直接套用上面的版式、别名与默认规格，不再重新推断。")
    L.append("")
    return "\n".join(L)


def save_dossier(key, profile, tenant=None, outdir=None):
    """把记忆文档写到 tenants/<id>/devices/<key>.md"""
    if tenant:
        TN.set_tenant(tenant)
    if not TN.enabled():
        return None
    d = os.path.join(os.path.dirname(TN.map_to_tenant(DEV.PROFILE_FILE)), "devices")
    if outdir:
        d = outdir
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "%s.md" % re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "_", key))
    io.open(p, "w", encoding="utf-8").write(dossier_md(key, profile))
    return p
