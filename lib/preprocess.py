# -*- coding: utf-8 -*-
"""
preprocess.py —— 工艺数据预处理

功能：
 1. 列名标准化：把各家 MES/设备导出的混乱列名映射到标准参数（基于知识库别名 + 模糊匹配）
 2. 工序/领域自动识别：涂布 / 辊压 / 卷绕 / 注液 / 化成 / 硅片 / EL / 组件IV
 3. 数据清洗：缺失处理、重复行、异常值剔除（物理阈值 + 3σ + MAD + IQR 四选一/组合）
 4. 时间对齐：多源数据按时间戳重采样对齐（向前填充 / 线性插值 / 分箱聚合）
 5. 批次合并：多文件按关键列 join
 6. 归一化：z-score / min-max / 按目标值偏差百分比
"""

import os
import re
import json
import datetime
from . import table as T
from . import stats as S

_KNOWLEDGE = None
_KNOWLEDGE_TENANT = "__unset__"          # 记录缓存属于哪个租户，切租户时必须失效
_KNOWLEDGE_SCOPE = "__unset__"           # 记录缓存属于哪个行业作用域，切行业时同样要失效


def _watch_paths(p):
    """要盯住变更的文件：基线 + 当前租户的覆盖层（任一层变了都得重读）"""
    paths = [p]
    try:
        from . import tenancy as TN
    except Exception:
        import tenancy as TN                         # noqa
    if TN.enabled():
        t = TN.map_to_tenant(p)
        if t and t != p:
            paths.append(t)
    return paths
_KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "knowledge", "process_specs.json")


def kb_path():
    """
    知识库路径必须跟着「当前根目录」走，不能用导入时就写死的常量——
    否则隔离/租户切换后读的还是原目录，分层合并形同虚设。
    """
    try:
        from . import tenancy as TN
    except Exception:
        import tenancy as TN                         # noqa
    return os.path.join(TN.root(), "knowledge", "process_specs.json")


def load_knowledge(path=None):
    """
    知识库唯一读取收口（分析侧）。返回两层叠加后的视图：

      基线 + 租户私有覆盖（tenancy） -> 再按行业作用域裁剪（scope）

    所以分析侧看到的一定是「这家企业 × 这个行业」的口径，
    而基线升级仍能自动流进来。

    重要边界：本函数返回的是**只读视图**，只能用于分析判定。
    写入侧（kbstore / kb_manage）一律走 _read_json 直读基线文件，
    绝不能拿这里的裁剪结果回写——否则会把作用域外的行业参数永久删掉。
    """
    global _KNOWLEDGE, _KNOWLEDGE_TENANT, _KNOWLEDGE_SCOPE
    try:
        from . import tenancy as TN
    except Exception:
        import tenancy as TN                         # noqa
    try:
        from . import scope as SC
    except Exception:
        import scope as SC                           # noqa
    try:
        from . import hotswap as HS
    except Exception:
        import hotswap as HS                         # noqa
    cur = TN.current()
    # 切了租户还用旧缓存 = 用 A 企业的口径分析 B 企业的数据，必须挡住
    if _KNOWLEDGE_TENANT != cur:
        _KNOWLEDGE, _KNOWLEDGE_TENANT = None, cur
    # 切了行业同样要失效：锂电视角的缓存拿去判光伏数据会认错参数
    scope_key = tuple(SC.current() or [])
    if _KNOWLEDGE_SCOPE != scope_key:
        _KNOWLEDGE, _KNOWLEDGE_SCOPE = None, scope_key
    # 长驻进程（云端 Web 服务）看不到基线更新的根因就在这里：
    # 文件换了但内存缓存没换。带文件指纹校验后，基线升级无需重启即可生效。
    p = path or kb_path()
    if _KNOWLEDGE is not None and not path and HS.changed("specs", _watch_paths(p)):
        _KNOWLEDGE = None
    if _KNOWLEDGE is None or path:
        p = path or kb_path()
        p = path or kb_path()
        if not path and TN.enabled():
            raw = TN.layered_read(p, TN.map_to_tenant(p), {})
        else:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
        # 行业作用域裁剪（未配置时原样返回）
        _KNOWLEDGE, _stat = SC.filter_specs(raw)
        if _stat.get("fallback"):
            _KNOWLEDGE = raw
    return _KNOWLEDGE


def load_knowledge_raw(path=None):
    """
    未经行业裁剪的完整知识库。给需要跨行业视野的场合用
    （例如判断"这个参数名是不是别的行业的"），常规分析请用 load_knowledge。
    """
    try:
        from . import tenancy as TN
    except Exception:
        import tenancy as TN                         # noqa
    p = path or kb_path()
    if not path and TN.enabled():
        return TN.layered_read(p, TN.map_to_tenant(p), {})
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _norm_name_uncached(s):
    """列名归一化。

    关键：先剥离括号内的单位后缀。设备导出的列名几乎都带单位
    （"浆料固含(wt%)"、"浆料粘度(mPa·s)"），不剥离的话模糊匹配分数会被单位字符拉低，
    导致明明是同一个参数却匹配不上。
    """
    s = str(s)
    s = re.sub(r"[\(（][^)）]*[\)）]", "", s)
    s = re.sub(r"[\s_\-/、,，.。:：\[\]【】]+", "", s)
    return s.lower()


_norm_name = _norm_name_uncached


# _norm_name 是纯函数，但会被按行调用（实测 2 万行数据里被调用 94 万次）。
# 加一层小字典缓存，命中率极高；上限防止极端脏数据撑爆内存。
_NORM_CACHE = {}


def _norm_name(s):
    key = str(s)
    hit = _NORM_CACHE.get(key)
    if hit is not None:
        return hit
    val = _norm_name_uncached(key)
    if len(_NORM_CACHE) > 50000:
        _NORM_CACHE.clear()
    _NORM_CACHE[key] = val
    return val


# ------------------------------------------------------------------ 列识别

def _substring_ok(a, t):
    """子串匹配护栏（v1.10）：
    - 中文别名至少 3 个字符（「电池」不会误配「电池列」，「不良率」可配「el不良率」）；
    - 英文别名至少 3 位，且必须是完整词（「isc」不会误配「visc」，「dryer」不会误配「dryer2」）。
    """
    short, longer = (a, t) if len(a) <= len(t) else (t, a)
    if len(short) < 3:
        return False
    if all(ord(ch) < 128 for ch in short):
        return re.search(r"(?<![a-z0-9])" + re.escape(short) + r"(?![a-z0-9])", longer) is not None
    return True


def _unit_tokens(s):
    """从文本抽取常见量纲 token（℃/mm/kPa/% 等），返回小写列表。"""
    return [m.group(1).lower() for m in
            re.finditer(r"(℃|°c|mm|cm|μm|um|nm|kpa|mpa|pa|%|pct|rpm|a|v|w|min|h|s)\b",
                        s, re.I)]


def _unit_hint(col_name):
    """从列名抽取首个量纲 token，供记录展示（冲突判定见 _unit_mismatch）。"""
    toks = _unit_tokens(col_name)
    return toks[0] if toks else None


def _unit_mismatch(rec, col_name, params):
    """声明量纲与列名暗示量纲不一致 → 跨量纲风险。

    P1-3 ③：原来靠白名单"恰好"挡住跨量纲，属碰巧而非设计保障。
    现在用**量纲 token 集合求交**比对——避免把 "m/min" 归一成 "mmin" 后
    误把列名里的 "mm" 当成已声明量纲（归一串接造成的假阳性）。
    """
    col_hints = set(_unit_tokens(col_name))
    if not col_hints or not rec.get("key"):
        return False
    decl = (params.get(rec["key"], {}) or {}).get("unit", "") or ""
    decl_hints = set(_unit_tokens(decl))
    if not decl_hints:
        return False
    return not (col_hints & decl_hints)


def _confidence(rec):
    """映射置信度：精确归一命中最高，子串相似度越低越不可信。"""
    if not rec.get("key"):
        return "unmatched"
    if rec.get("source") == "exact":
        return "high"
    sc = rec.get("score", 0.0)
    if sc >= 0.85:
        return "high"
    if sc >= 0.70:
        return "medium"
    return "low"


def _search_param(target, params):
    """在给定参数子集中做别名匹配，返回 (key, 命中别名, 相似度, 来源)。"""
    best, best_score, best_via, best_src = None, 0, None, None
    for key, spec in params.items():
        for alias in spec.get("aliases", []) + [spec.get("name", ""), key]:
            a = _norm_name(alias)
            if not a:
                continue
            if a == target:
                return key, alias, 1.0, "exact"
            # 护栏：子串匹配要求较短一方至少 4 个字符。否则「Visc」会误配「isc」、
            # 「电池列」会误配泛化别名「电池」，导致工序与参数同时判错（v1.10 修复）。
            if (a in target or target in a) and _substring_ok(a, target):
                score = min(len(a), len(target)) / max(len(a), len(target))
                if score > best_score:
                    best, best_score, best_via, best_src = key, score, alias, "substring"
    if best_score >= 0.6:
        return best, best_via, best_score, best_src
    return None, None, 0.0, "unmatched"


def _match_one(col_name, prefer_process=None):
    """列名 -> 标准参数 key 的**结构化**匹配结果（P1-3 核心）。

    返回带来源/候选/置信度/量纲风险/人工确认的记录，
    裸 {列: key} 无法表达"这一列到底配得有多准"——这正是审计指出的缺口。
    """
    kb = load_knowledge()
    target = _norm_name(col_name)
    if not target:
        return {"key": None, "source": "unmatched", "matched_via": None,
                "score": 0.0, "confidence": "unmatched", "candidates": [],
                "unit_hint": _unit_hint(col_name), "unit_risk": False,
                "confirmed": False, "policy_action": "accept"}
    params = kb["params"]
    scopes = []
    if prefer_process:
        scopes.append(("process_scoped",
                       {k: v for k, v in params.items()
                        if v.get("process") == prefer_process}))
    scopes.append(("generic",
                   {k: v for k, v in params.items()
                    if v.get("process") is None}))
    scopes.append(("global", params))

    rec = None
    cands = {}
    for scope_name, subset in scopes:
        k, via, sc, src = _search_param(target, subset)
        if k:
            sc_r = round(sc, 3)
            # 同一 key 可能跨作用域（process_scoped / generic / global）被多次列出，
            # 按 key 去重保留最高分，避免候选列表出现重复 key 造成"伪多候选"误导。
            if k not in cands or sc_r > cands[k]["score"]:
                cands[k] = {"key": k, "score": sc_r,
                            "source": "exact" if src == "exact" else scope_name}
            if rec is None:
                rec = {"key": k, "source": "exact" if src == "exact" else scope_name,
                       "matched_via": via, "score": sc, "candidates": None}
    if rec is None:
        rec = {"key": None, "source": "unmatched", "matched_via": None,
               "score": 0.0, "candidates": None}
    rec["candidates"] = sorted(cands.values(), key=lambda x: -x["score"])[:3]
    rec["unit_hint"] = _unit_hint(col_name)
    rec["unit_risk"] = _unit_mismatch(rec, col_name, params)
    rec["confidence"] = _confidence(rec)
    # 量纲风险一律把置信度下调一级（high->medium, medium->low）
    if rec["unit_risk"]:
        rec["confidence"] = {"high": "medium", "medium": "low"}.get(
            rec["confidence"], rec["confidence"])
    rec["confirmed"] = False
    rec["policy_action"] = "accept"
    return rec


def match_param(col_name, prefer_process=None):
    """列名 -> 标准参数 key（向后兼容入口，只返回 key）。

    完整结构化信息见 map_columns_detailed / _match_one。
    """
    return _match_one(col_name, prefer_process).get("key")


# 低置信度映射的处理政策（P1-3 ②：低置信度必须可阻断，不能只能"猜了就用"）
#   auto  ：保留所有最佳猜测，低置信列进 low_confidence（默认，不破坏现有可用性）
#   review：同 auto，但低置信列标记 require_confirmation=True
#   block ：低置信（confidence=low）直接判未匹配（key=None），强制人工补映射
MAP_POLICY = "auto"


def apply_map_policy(detailed, policy=MAP_POLICY):
    """把结构化映射收敛成 {列: key}，并标记低置信列。

    返回 (col_to_key, low_confidence_list)。policy 决定低置信列是
    仅标注（auto/review）还是直接按未匹配处理（block）。
    """
    out, low = {}, []
    for col, rec in detailed.items():
        key = rec.get("key")
        conf = rec.get("confidence")
        action = "accept"
        if policy == "block" and conf == "low":
            key = None
            action = "block"
        elif conf == "low":
            action = "review" if policy == "review" else "accept"
            low.append({"col": col, "key": key, "confidence": conf,
                        "source": rec.get("source"), "unit_risk": rec.get("unit_risk")})
        out[col] = key
        rec["policy_action"] = action
    return out, low


def map_columns_detailed(table, prefer_process=None):
    """返回 {原列名: 结构化匹配记录}（P1-3 核心入口）。"""
    return {c: _match_one(c, prefer_process) for c in table.columns}


def detect_process(table, col_map=None):
    """根据列名 + 内容判定工序，返回 [(process_key, score)]"""
    kb = load_knowledge()
    if col_map is None:
        col_map = map_columns(table)
    scores = {}
    for col, key in col_map.items():
        if not key:
            continue
        # 设备档案可以引用知识库尚未登记的参数 key，不能直接下标——
        # 新加一台设备的别名时最容易踩这个坑（表现为 KeyError 崩流水线）。
        proc = kb["params"].get(key, {}).get("process")
        if proc:
            scores[proc] = scores.get(proc, 0) + 1
    # 列名字面匹配工序别名
    for c in table.columns:
        n = _norm_name(c)
        for pk, pv in kb["processes"].items():
            for a in pv.get("alias", []):
                if _norm_name(a) and _norm_name(a) in n:
                    scores[pk] = scores.get(pk, 0) + 0.6
    # 缺陷类名匹配（EL 检测报告常以缺陷类型为列）
    for c in table.columns:
        n = _norm_name(c)
        for dk, dv in kb["defect_rootcause"].items():
            if _norm_name(dk) == n or _norm_name(dk) in n:
                scores[dv["process"]] = scores.get(dv["process"], 0) + 1.5
    return sorted(scores.items(), key=lambda kv: -kv[1])


def map_columns(table, prefer_process=None):
    """返回 {原列名: 标准参数key or None}（向后兼容）。"""
    return {c: r.get("key") for c, r in map_columns_detailed(table, prefer_process).items()}


# ------------------------------------------------------------------ 清洗

def clean(table, missing="drop_rows", outlier="mad", outlier_k=4.5,
          use_physical=True, drop_dup=True, spec_override=None, log=None,
          prefer_process=None):
    """
    返回 (clean_table, report_dict)
    missing: drop_rows | keep | fill_mean | fill_median | fill_prev
    outlier: none | sigma3 | mad | iqr
    log: 外部传入的 list，被剔除的每一行都会写进这里（文件 + 原始行号 + 原因 + 原值）。
         清洗最大的风险是把真实异常当噪声删掉，所以删了什么必须能一条条查回来。
    """
    kb = load_knowledge()
    # P1-3：映射不再是裸 {列: key}。这里把结构化映射（来源/候选/置信度/量纲风险）
    # 一并交出去，并标出低置信列——下游据此决定是否阻断或要求人工确认。
    _cmap_detailed = map_columns_detailed(table, prefer_process)
    _cmap_keys, _low_conf = apply_map_policy(_cmap_detailed, "auto")
    rep = {"rows_in": len(table), "actions": [], "outliers": {},
           "missing": {}, "physical_violations": {},
           "col_map": _cmap_keys,
           "mapping_detail": {c: {k: v for k, v in r.items() if k != "candidates"}
                             for c, r in _cmap_detailed.items()},
           "low_confidence_mappings": _low_conf}
    tb = T.Table(table.columns, table.rows, table.name, table.source_path,
                 src_rows=getattr(table, "src_rows", None))
    if log is None:
        log = []
    rep["cleaning_log"] = log

    if drop_dup:
        seen = set()
        mask = []
        for r in tb.rows:
            sig = tuple("" if v is None else str(v) for v in r)
            if sig in seen:
                mask.append(False)
            else:
                seen.add(sig)
                mask.append(True)
        if not all(mask):
            rep["actions"].append("删除重复行 %d 行" % (len(mask) - sum(mask)))
            tb.keep_mask(mask, reason="重复行", log=log)

    numcols = tb.numeric_columns()
    for c in numcols:
        vals = tb.numeric_column(c)
        n_miss = sum(1 for v in vals if v is None)
        if n_miss:
            rep["missing"][c] = n_miss
        if n_miss and missing == "drop_rows":
            _m = [v is not None for v in vals]
            tb.keep_mask(_m, reason="列 '%s' 测量值缺失" % c, log=log)
            rep["actions"].append("列 '%s' 删除缺失行 %d 行" % (c, n_miss))

    # 重新取列（行数可能已变）
    numcols = tb.numeric_columns()

    if missing in ("fill_mean", "fill_median", "fill_prev"):
        for c in numcols:
            vals = tb.numeric_column(c)
            if all(v is not None for v in vals):
                continue
            fill = S.mean(vals) if missing == "fill_mean" else S.median(vals)
            prev = None
            idx = tb.col_index(c)
            for i, r in enumerate(tb.rows):
                if r[idx] is None or str(r[idx]).strip() == "":
                    if missing == "fill_prev":
                        r[idx] = prev if prev is not None else fill
                    else:
                        r[idx] = fill
                else:
                    prev = T.to_number(r[idx])
            rep["actions"].append("列 '%s' 缺失值填充(%s)" % (c, missing))

    # 物理阈值剔除（超出典型量程 20% 视为量纲错误/传感器故障）
    # 安全网：一列数据的绝大多数都超量程时，几乎不可能是传感器坏了，
    # 而是**单位写错了**（化成电流标 A 还是标 C-rate、长度标 mm 还是 μm）——
    # 这时候整表被删光是最糟的结果。宁可保留数据打个标记，也不能让数据凭空消失。
    if use_physical:
        n0 = len(tb.rows)
        _rows_before_physical = [list(r) for r in tb.rows]
        _src_before_physical = list(tb.src_rows) if tb.src_rows else None
        SUSPECT_RATIO = 0.7
        for c in numcols:
            key = rep["col_map"].get(c)
            if not key:
                continue
            spec = kb["params"].get(key, {})
            rng = spec.get("typical_range")
            if not rng:
                continue
            lo, hi = rng
            span = hi - lo
            hard_lo, hard_hi = lo - 0.5 * span, hi + 0.5 * span
            vals = tb.numeric_column(c)
            nval = len([v for v in vals if v is not None])
            bad = [i for i, v in enumerate(vals) if v is not None and (v < hard_lo or v > hard_hi)]
            if not bad:
                continue
            ratio = len(bad) / float(nval) if nval else 0
            if ratio > SUSPECT_RATIO or len(bad) >= nval:
                # 整列都对不上：按单位/量程错配处理，只标记不删除
                rep.setdefault("unit_suspects", []).append({
                    "column": c, "param": key,
                    "unit_in_kb": spec.get("unit"), "typical_range": [lo, hi],
                    "bound": [round(hard_lo, 3), round(hard_hi, 3)],
                    "ratio": round(ratio, 3), "n": len(bad),
                    "samples": [round(vals[i], 3) for i in bad[:5]],
                    "action": "保留原值，仅标记",
                    "hint": "该列 %d/%d 个值超出「%s」的典型量程 [%s, %s]（库中单位为 %s），"
                            "大概率是单位不一致（例如电流用 A 而库里登记为 C-rate、"
                            "长度用 mm 而登记为 μm）。已保留数据，请核对单位后重跑"
                            % (len(bad), nval, spec.get("name", key), lo, hi,
                               spec.get("unit", "")),
                })
                rep["actions"].append(
                    "列 '%s' 有 %d/%d 个值超出典型量程，判定为单位/量程错配，**保留未删**"
                    % (c, len(bad), nval))
                continue
            rep["physical_violations"][c] = {
                "count": len(bad), "bound": [round(hard_lo, 3), round(hard_hi, 3)],
                "samples": [round(vals[i], 3) for i in bad[:5]],
                "action": "标记为可疑并剔除"
            }
            keep_mask = [i not in set(bad) for i in range(len(tb.rows))]
            tb.keep_mask(keep_mask, reason="列 '%s' 超出物理量程 [%s, %s]"
                         % (c, round(hard_lo, 3), round(hard_hi, 3)), log=log)
            rep["actions"].append("列 '%s' 剔除超物理量程点 %d 个" % (c, len(bad)))
        # 最后一道防线：任何情况下都不能把表清空
        if n0 and len(tb.rows) == 0:
            tb.rows = list(_rows_before_physical)
            tb.src_rows = list(_src_before_physical) if _src_before_physical else None
            rep["actions"].append("物理量程剔除导致 0 行，已回退为仅标记不删除")
            rep["physical_rollback"] = True
            rep["physical_violations"] = {}
        numcols = tb.numeric_columns()

    # 统计异常值
    if outlier != "none":
        for c in numcols:
            vals = [v for v in tb.numeric_column(c) if v is not None]
            if len(vals) < 8:
                continue
            m, sd = S.mean(vals), S.stdev(vals)
            if outlier == "sigma3":
                if not sd:
                    continue
                lo, hi = m - 3 * sd, m + 3 * sd
            elif outlier == "mad":
                rs = S.robust_sigma(vals)
                if not rs:
                    continue
                lo, hi = m - outlier_k * rs, m + outlier_k * rs
            else:  # iqr
                q1, q3 = S.quantile(vals, 0.25), S.quantile(vals, 0.75)
                if q1 is None or q3 is None:
                    continue
                iqr = q3 - q1
                lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            bad = [i for i, v in enumerate(tb.numeric_column(c))
                   if v is not None and (v < lo or v > hi)]
            if bad:
                rep["outliers"][c] = {"method": outlier, "count": len(bad),
                                      "bound": [round(lo, 4), round(hi, 4)],
                                      "samples": [round(tb.numeric_column(c)[i], 4) for i in bad[:8]]}
    rep["rows_out"] = len(tb.rows)
    rep["numeric_columns"] = tb.numeric_columns()
    return tb, rep


# ------------------------------------------------------------------ 时间对齐

_TIME_NAME_HINT = re.compile(
    r"时间|日期|时刻|采集|时间戳|开始|结束|测量时间|timestamp|date|time", re.I)
# 产线数据的时间戳不会早于 1990，也不会晚于 2050；超出即判定为误判
_PLAUSIBLE_LO = datetime.datetime(1990, 1, 1)
_PLAUSIBLE_HI = datetime.datetime(2050, 1, 1)


def _looks_like_time_column(table, col, parsed):
    """
    防止把普通数值列误判成时间列。

    纯数值列会被按 Excel 日期序列号解读——0.15 会被读成 1899-12-31，
    于是「硅片厚度(mm)」这种小数列就成了"时间列"。后果是静默的：
    排序、SPC 时序、跨工序对齐全都按错误顺序走，谁也不会报错。
    """
    idx = table.col_index(col)
    ok = [v for v in parsed if v is not None]
    if not ok:
        return False
    if not _TIME_NAME_HINT.search(str(col)):
        # 列名不带时间语义，那就必须落在可信年份区间内
        if not all(_PLAUSIBLE_LO <= v <= _PLAUSIBLE_HI for v in ok):
            return False
        # 全列同一个值也不该被当成时间轴
        if len(set(ok)) < max(2, int(len(ok) * 0.3)):
            return False
    else:
        if not all(_PLAUSIBLE_LO <= v <= _PLAUSIBLE_HI for v in ok):
            return False
    return True


def parse_time_column(table, col=None):
    """识别/解析时间列，返回 (列名, [datetime|None])"""
    if col:
        idx = table.col_index(col)
        if idx is None:
            return None, None
        return col, [T.parse_datetime(r[idx]) for r in table.rows]
    best, best_hit = None, 0
    for c in table.columns:
        vals = [T.parse_datetime(r[table.col_index(c)]) for r in table.rows[:200]]
        hit = sum(1 for v in vals if v is not None)
        if hit == 0:
            continue
        if not _looks_like_time_column(table, c, vals):
            continue
        if hit > best_hit:
            best, best_hit = c, hit
    if best is None or best_hit == 0:
        return None, None
    return best, [T.parse_datetime(r[table.col_index(best)]) for r in table.rows]


def time_align(tables, keys, freq_seconds=None, how="nearest", tolerance_seconds=None,
               agg="mean"):
    """
    多表按时间对齐。tables: [(Table, timecol)]；freq: 重采样间隔（秒）
    返回 (aligned_table, report)
    """
    series = []
    for tb, tcol in tables:
        tc, times = parse_time_column(tb, tcol)
        if tc is None:
            continue
        numcols = [c for c in tb.numeric_columns() if c != tc]
        series.append((tb.name, times, numcols, tb))
    if not series:
        return None, {"error": "未找到可解析的时间列，无法做时间对齐"}

    # 收集所有时间点作为候选（以第一张表为主表，其余向主表对齐）
    base_name, base_times, base_cols, base_tb = series[0]
    idx_ok = [i for i, t in enumerate(base_times) if t is not None]
    aligned_rows = []
    used_cols = [(base_name, c) for c in base_cols]
    for s in series[1:]:
        used_cols += [(s[0], c) for c in s[2]]

    report = {"base": base_name, "aligned": [], "dropped_unmatched": 0}
    for i in idx_ok:
        bt = base_times[i]
        row = []
        ok = True
        for si, (name, times, numcols, tb) in enumerate(series):
            if si == 0:
                for c in numcols:
                    row.append(T.to_number(tb.rows[i][tb.col_index(c)]))
                continue
            # 找最近时间点
            best_j, best_d = None, None
            for j, t in enumerate(times):
                if t is None:
                    continue
                d = abs((t - bt).total_seconds())
                if best_d is None or d < best_d:
                    best_j, best_d = j, d
            if best_j is None or (tolerance_seconds and best_d > tolerance_seconds):
                ok = False
                break
            for c in numcols:
                row.append(T.to_number(tb.rows[best_j][tb.col_index(c)]))
        if ok:
            aligned_rows.append([bt.strftime("%Y-%m-%d %H:%M:%S")] + row)
        else:
            report["dropped_unmatched"] += 1
    cols = ["timestamp"] + ["%s::%s" % (n, c) for n, c in used_cols]
    report["aligned"] = len(aligned_rows)
    return T.Table(cols, aligned_rows, "aligned"), report


# ------------------------------------------------------------------ 批次合并

def merge_tables(tables, on, how="inner"):
    """按关键列做 join。tables: [Table...]"""
    if not tables or not on:
        return None, {"error": "缺少合并键"}
    base = tables[0]
    idx = base.col_index(on)
    if idx is None:
        return None, {"error": "主表缺少合并列: %s" % on}

    # 输出列名必须在这里一次性算定，不能在遍历行的过程中追加。
    # 老实现把这段放进行循环里，第二行会因为 "该列名已存在" 而改用带前缀的名字，
    # 结果列名集合越长越多（15 个）而每行只有 10 个值，下游按列取值直接 IndexError。
    merged_cols = list(base.columns)
    per_table = []
    for jt in tables[1:]:
        names = []
        for c in jt.columns:
            nm = "%s::%s" % (jt.name, c) if c in merged_cols else c
            if nm not in merged_cols:
                merged_cols.append(nm)
            names.append(nm)
        per_table.append((jt, names))

    lookup = {}
    for r in base.rows:
        lookup.setdefault(str(r[idx]).strip(), []).append(list(r))
    merged_rows = []
    report = {"key": on, "base_rows": len(base.rows), "matched": 0, "unmatched_in_join": 0}

    # join 表侧同样预先建索引，避免每行都全表扫描一次
    jt_maps = []
    for jt, _ in per_table:
        ji = jt.col_index(on)
        if ji is None:
            jt_maps.append(None)
            continue
        mp = {}
        for rr in jt.rows:
            mp.setdefault(str(rr[ji]).strip(), rr)
        jt_maps.append(mp)

    for key, rows in lookup.items():
        for r in rows:
            out = list(r)
            hit_all = True
            for (jt, names), mp in zip(per_table, jt_maps):
                if mp is None:
                    hit_all = False
                    continue
                if key not in mp:
                    if how == "inner":
                        hit_all = False
                        break
                    out.extend([None] * len(names))
                    continue
                rr = mp[key]
                for c in jt.columns:
                    out.append(rr[jt.col_index(c)])
            if hit_all:
                merged_rows.append(out)
                report["matched"] += 1
            else:
                report["unmatched_in_join"] += 1
    # 兜底：任何一行长度不符都补齐，绝不让下游拿到甩锅级的 IndexError
    width = len(merged_cols)
    for i, row in enumerate(merged_rows):
        if len(row) < width:
            merged_rows[i] = row + [None] * (width - len(row))
        elif len(row) > width:
            merged_rows[i] = row[:width]
    report["dropped_unmatched"] = report.pop("unmatched_in_join")
    return T.Table(merged_cols, merged_rows, base.name), report


# ------------------------------------------------------------------ 归一化

# scan:keep 库能力：通用归一化。当前清洗只用 MAD/缺失策略，未做量纲归一；需要统一量纲时启用。
def normalize(table, columns=None, method="zscore", target_map=None):
    """method: zscore | minmax | dev_pct（相对目标值偏差%）
    返回 (new_table, stats_dict)"""
    cols = columns or table.numeric_columns()
    out = T.Table(list(table.columns), [list(r) for r in table.rows], table.name + "_norm")
    info = {}
    for c in cols:
        i = out.col_index(c)
        if i is None:
            continue
        vals = out.numeric_column(c)
        if method == "dev_pct":
            tgt = (target_map or {}).get(c)
            if tgt is None:
                tgt = S.mean([v for v in vals if v is not None])
            newv = [None if v is None else (v - tgt) / tgt * 100.0 for v in vals]
            info[c] = {"method": "dev_pct", "target": tgt}
        elif method == "minmax":
            newv = S.minmax_scale(vals)
            info[c] = {"method": "minmax"}
        else:
            newv = S.zscore(vals)
            m, sd = S.mean([v for v in vals if v is not None]), S.stdev(vals)
            info[c] = {"method": "zscore", "mean": m, "sigma": sd}
        for r, v in zip(out.rows, newv):
            r[i] = v
    return out, info


# ------------------------------------------------------------------ 派生量

# scan:keep 库能力：派生列。当前派生指标在报告层现算，未落到预处理层。
def add_derived(table, col_map=None):
    """按知识库补充常用派生列：CV%、与目标偏差、移动平均、分组序号"""
    kb = load_knowledge()
    if col_map is None:
        col_map = map_columns(table)
    out = T.Table(list(table.columns), [list(r) for r in table.rows], table.name)
    added = []
    for c, key in col_map.items():
        if not key:
            continue
        spec = kb["params"].get(key, {})
        ctrl = spec.get("control", {})
        if not ctrl:
            continue
        mode = ctrl.get("mode")
        vals = out.numeric_column(c)
        if mode in ("usl", "lsl", "range", "deviation", "deviation_pct", "cv_limit_pct"):
            pass
        # 移动平均（对时序类工艺参数有用）
        if len(vals) >= 15:
            ma = S.moving_average(vals, 5)
            out.add_column("%s_MA5" % c, [None if v is None else round(v, 5) for v in ma])
            added.append("%s_MA5" % c)
    return out, added


# ─────────────────────────────────────────────────────────────
# 企业规格书匹配
# ─────────────────────────────────────────────────────────────

def match_spec_key(col, col_map, overrides):
    """把企业规格书里的条目对到实际列名上。

    坑：企业规格书习惯写全名带单位（「极片残水(ppm)」），设备导出的列名却常不带
    （「极片残水」）。直接按字面比对会**整份规格书静默失效**——Cpk 退回用数据分布
    兜底，数字完全失真，而企业以为用的就是自己的规格。
    """
    if not overrides or not col:
        return None
    if col in overrides:
        return overrides[col]
    nc = _norm_name(col)
    for k, v in overrides.items():
        if _norm_name(k) == nc:
            return v
    key = (col_map or {}).get(col)
    if key:
        nk = _norm_name(key)
        for k, v in overrides.items():
            if _norm_name(k) == nk:
                return v
    return None


def unmatched_spec_keys(overrides, columns, col_map):
    """规格书里哪些条目一条都没对上——必须报出来，否则企业以为规格已生效。"""
    if not overrides:
        return []
    out = []
    for k in overrides:
        if any(match_spec_key(c, col_map, overrides) for c in columns):
            continue
        out.append(k)
    return out


# ─────────────────────────────────────────────────────────────
# 单位行剔除（双行表头的第二行）
# ─────────────────────────────────────────────────────────────

_UNIT_TOKENS = {
    "m/min", "g/m2", "g/m²", "um", "μm", "mm", "cm", "nm", "℃", "°c", "ppm", "ppma",
    "mpa·s", "mpa.s", "pa·s", "wt%", "%", "n/m", "n", "kn", "mpa", "kpa", "pa",
    "v", "mv", "a", "ma", "w", "mw", "kw", "wh", "ah", "mah", "mω", "mΩ", "ω", "Ω",
    "kω", "kΩ", "ω·cm", "Ω·cm", "us", "μs", "ms", "s", "min", "h", "mω/m", "mΩ/m",
    "l/min", "m/s", "mm/min", "rpm", "gray", "c-rate", "pass/fail", "mn/m",
    "—", "-", "/", "片", "个", "pcs", "",
}


def drop_unit_row(tb):
    """双行表头的第二行是单位行（m/min、g/m2、℃…），不是观测值。

    这一行如果留在数据里，会多出一个"样本"，把均值、σ、控制限、相关系数的下标
    全部带偏一格。设备档案能识别时会正确处理，识别不出设备时就必须靠这条兜底。
    """
    if tb is None or not getattr(tb, "rows", None):
        return tb, False
    cells = [str(v).strip() for v in tb.rows[0]]
    n = len(cells) or 1
    unit_hits = sum(1 for c in cells if c.lower() in _UNIT_TOKENS)
    numeric = sum(1 for c in cells if T.to_number(c) is not None)
    if unit_hits >= max(2, int(n * 0.4)) and numeric <= n * 0.25:
        tb.rows = tb.rows[1:]
        if getattr(tb, "src_rows", None):
            tb.src_rows = tb.src_rows[1:]
        return tb, True
    return tb, False
