# -*- coding: utf-8 -*-
"""
scope.py —— 行业作用域（Industry Scoping）

企业端按自己买的行业模块配置作用域（如只配 lithium battery），
之后这家企业的每次分析都只在**本行业的参数/设备/案例**里检索。

设计原则：

  1. 裁剪的是"会误判的东西"，不是"通用的东西"
     · 裁：工序参数、设备档案、缺陷机理、改善案例（这些跨行业同名不同义）
     · 不裁：判读规则、单位换算、通用别名（这些是方法论，裁掉会让人变蠢）
     典型反例：把"相关不等于因果"这条强制规则按行业裁掉，是纯粹的负收益。

  2. 配错不会让分析变瞎，只会退回全量
     行业名拼错、作用域把参数裁空，一律回退到"不限行业"并留下 note。
     宁可多检索一点，也不能因为一条配置错误让整份报告少判一半参数。

  3. 通用参数（process 为 null，如不良率/良率）永远保留
     不然任何行业都算不出不良率。

  4. 作用域是"视图"不是"副本"
     和租户层一样：基线知识库文件一个字都不改，只在读出来后过滤。
     所以基线升级自动流入，也不用为每家行业维护一份库。

没有配置行业时完全不介入，行为与之前一致。
"""

import io
import json
import os

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDUSTRY_FILE = os.path.join(SKILL_ROOT, "knowledge", "industries.json")

_INDUSTRIES = None        # 当前生效行业列表；None 或 [] = 不限
_NOTES = []               # 作用域决议过程的留痕（写进报告）
_LAST_STAT = {}           # 最近一次裁剪的统计，供报告留痕


# --------------------------------------------------------------- 行业登记

def _read(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def registry(root=None):
    """
    行业登记表。优先读 knowledge/industries.json；
    没有该文件时，从 process_specs.json 的 processes[*].domain 自动推导
    ——新增工序不必记得回来改行业表，推导兜底能自动认领。
    """
    r = root or SKILL_ROOT
    reg = _read(os.path.join(r, "knowledge", "industries.json"), None)
    if isinstance(reg, dict) and reg.get("industries"):
        return reg
    specs = _read(os.path.join(r, "knowledge", "process_specs.json"), {}) or {}
    procs = specs.get("processes", {}) or {}
    derived = {}
    for key, val in procs.items():
        dom = None
        if isinstance(val, dict):
            dom = val.get("domain")
            if not dom:
                # domain 缺失时按 key 前缀兜底（battery_coating -> battery）
                if "_" in key:
                    dom = key.split("_", 1)[0]
        dom = dom or "general"
        e = derived.setdefault(dom, {"name": dom, "domain": dom, "processes": [], "docs": []})
        if key not in e["processes"]:
            e["processes"].append(key)
    return {"meta": {"version": "0.0.0", "derived": True}, "industries": derived}


def known_industries(root=None):
    return sorted((registry(root).get("industries") or {}).keys())


def set_root(path):
    global SKILL_ROOT, INDUSTRY_FILE, _INDUSTRIES
    SKILL_ROOT = path
    INDUSTRY_FILE = os.path.join(path, "knowledge", "industries.json")
    _INDUSTRIES = None
    return SKILL_ROOT


# --------------------------------------------------------------- 作用域决议

def _split(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.replace(";", ",").replace(" ", ",").split(",")
    else:
        parts = []
        for x in raw:
            parts.extend(_split(x))
    return [p.strip() for p in parts if p and p.strip()]


def resolve(raw, root=None):
    """
    把外部给进来的行业标识解析成规范 id 列表。
    返回 (valid, invalid)。invalid 非空说明有拼错的，调用方要留痕。
    """
    ids = _split(raw)
    if not ids:
        return [], []
    reg = registry(root).get("industries") or {}
    norm = {}
    for k, v in reg.items():
        norm[str(k).lower()] = k
        for a in [v.get("name")] + list(v.get("alias") or []):
            if a:
                norm[str(a).lower()] = k
    valid, invalid = [], []
    for i in ids:
        hit = norm.get(str(i).lower())
        (valid if hit else invalid).append(hit or i)
    # 去重保序
    seen, out = set(), []
    for v in valid:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out, invalid


def set_industries(raw, root=None):
    """设置当前作用域。返回 (生效列表, 无效列表)。空输入 = 不限行业。"""
    global _INDUSTRIES, _NOTES
    _NOTES = []
    valid, invalid = resolve(raw, root)
    _INDUSTRIES = valid or None
    if invalid:
        # 拼错的行业名不能静默吞掉，但也不该让分析失败 -> 忽略 + 留痕
        _NOTES.append("行业标识无法识别，已忽略：%s（可用：%s）"
                      % ("、".join(invalid), "、".join(known_industries(root))))
    return current(), invalid


def current():
    return list(_INDUSTRIES) if _INDUSTRIES else None


def active():
    """是否启用了行业限定"""
    return bool(_INDUSTRIES)


def notes():
    return list(_NOTES)


def add_note(msg):
    if msg and msg not in _NOTES:
        _NOTES.append(msg)


def clear():
    global _INDUSTRIES, _NOTES
    _INDUSTRIES = None
    _NOTES = []


def describe(root=None):
    """给人看的作用域描述，进报告抬头"""
    if not _INDUSTRIES:
        return "全行业（未限定）"
    reg = registry(root).get("industries") or {}
    names = []
    for i in _INDUSTRIES:
        v = reg.get(i, {})
        names.append("%s(%s)" % (v.get("name", i), i))
    return "、".join(names)


# --------------------------------------------------------------- 工序 -> 行业

def process_domain(procs, pkey):
    """工序属于哪个行业。查不到时按 key 前缀兜底。"""
    v = procs.get(pkey)
    if isinstance(v, dict):
        dom = v.get("domain")
        if dom:
            return dom
        for ind, meta in (registry().get("industries") or {}).items():
            if pkey in (meta.get("processes") or []):
                return meta.get("domain") or ind
    if pkey and "_" in pkey:
        return pkey.split("_", 1)[0]
    return None


def allowed_processes(procs, inds=None):
    """作用域内允许的工序集合"""
    inds = inds or _INDUSTRIES
    if not inds:
        return None                      # None = 不限
    reg = registry().get("industries") or {}
    wanted = set()
    for i in inds:
        meta = reg.get(i, {})
        dom = meta.get("domain") or i
        for p in (meta.get("processes") or []):
            wanted.add(p)
        for p, v in procs.items():
            if isinstance(v, dict) and v.get("domain") == dom:
                wanted.add(p)
            elif process_domain(procs, p) == dom:
                wanted.add(p)
    return wanted


# --------------------------------------------------------------- 过滤

_GENERIC_KEEP = ("unit_conversions", "unit_aliases", "_fallback", "meta",
                 "index", "history", "doc_version", "name")


def last_stat():
    return dict(_LAST_STAT)


def filter_specs(specs, inds=None):
    """
    裁剪工艺规格库：processes / params / defect_rootcause。
    通用参数（process 为 null）保留。
    """
    global _LAST_STAT
    inds = inds or _INDUSTRIES
    if not inds or not isinstance(specs, dict):
        _LAST_STAT = {}
        return specs, {}
    procs = specs.get("processes", {}) or {}
    keep_p = allowed_processes(procs, inds)
    if not keep_p:
        # 一个工序都没命中 -> 配置有问题，回退全量，绝不裁成空库
        add_note("行业作用域未命中任何工序（%s），已回退为全行业" % "、".join(inds))
        _LAST_STAT = {"fallback": True}
        return specs, {"fallback": True}

    out = {}
    for k, v in specs.items():
        out[k] = v
    out["processes"] = {k: v for k, v in procs.items() if k in keep_p}

    params = specs.get("params", {}) or {}
    kept_params, dropped = {}, 0
    for k, v in params.items():
        p = v.get("process") if isinstance(v, dict) else None
        if p is None or p in keep_p:      # 通用参数永远保留
            kept_params[k] = v
        else:
            dropped += 1
    out["params"] = kept_params

    dr = specs.get("defect_rootcause", {}) or {}
    if isinstance(dr, dict):
        out["defect_rootcause"] = {
            k: v for k, v in dr.items()
            if not (isinstance(v, dict) and v.get("process") and v.get("process") not in keep_p)
        }

    # 裁完一个参数都不剩（只剩通用的）说明配错了，回退
    specific = [k for k, v in kept_params.items()
                if isinstance(v, dict) and v.get("process")]
    if not specific:
        add_note("行业作用域裁掉了全部工序参数，已回退为全行业")
        _LAST_STAT = {"fallback": True}
        return specs, {"fallback": True}

    stat = {"kept_processes": len(out["processes"]), "kept_params": len(kept_params),
            "dropped_params": dropped, "industries": list(inds)}
    _LAST_STAT = stat
    return out, stat


def filter_devices(obj, inds=None, procs=None):
    """
    裁剪设备档案。设备只要**有一个**工序落在作用域内就保留
    ——基恩士测厚仪这类锂电/光伏都在用的设备，按行业一刀切会误删。
    """
    inds = inds or _INDUSTRIES
    if not inds or not isinstance(obj, dict):
        return obj, {}
    devs = obj.get("devices", {}) or {}
    if not devs:
        return obj, {}
    if procs is None:
        specs = _read(os.path.join(SKILL_ROOT, "knowledge", "process_specs.json"), {}) or {}
        procs = specs.get("processes", {}) or {}
    keep_p = allowed_processes(procs, inds)
    if not keep_p:
        return obj, {}
    kept, dropped = {}, []
    for k, v in devs.items():
        ps = v.get("processes") or ([] if not isinstance(v, dict) else [])
        if not ps or any(p in keep_p for p in ps):
            kept[k] = v
        else:
            dropped.append(k)
    if not kept:
        # 设备全被裁掉会让"未知设备识别"整体失效，宁可不裁
        add_note("行业作用域会裁掉全部设备档案，已保留完整设备库")
        return obj, {}
    out = {}
    for k, v in obj.items():
        out[k] = v
    out["devices"] = kept
    return out, {"kept_devices": len(kept), "dropped_devices": dropped}


def filter_cases(obj, inds=None, procs=None):
    """裁剪改善案例库（PDCA/PFMEA 验证过的措施）。"""
    inds = inds or _INDUSTRIES
    if not inds or not isinstance(obj, dict):
        return obj, {}
    cases = obj.get("cases", []) or []
    if not cases:
        return obj, {}
    if procs is None:
        specs = _read(os.path.join(SKILL_ROOT, "knowledge", "process_specs.json"), {}) or {}
        procs = specs.get("processes", {}) or {}
    keep_p = allowed_processes(procs, inds)
    if not keep_p:
        return obj, {}
    kept = [c for c in cases
            if not (isinstance(c, dict) and c.get("process") and c.get("process") not in keep_p)]
    out = {}
    for k, v in obj.items():
        out[k] = v
    out["cases"] = kept
    return out, {"kept_cases": len(kept), "dropped_cases": len(cases) - len(kept)}


def filter_knowledge(file_key, obj, inds=None, procs=None):
    """按知识库文件语义分派过滤"""
    if file_key == "specs":
        return filter_specs(obj, inds)
    if file_key == "dev":
        return filter_devices(obj, inds, procs)
    if file_key == "cases":
        return filter_cases(obj, inds, procs)
    return obj, {}          # interp / 单位换算等通用知识不裁
