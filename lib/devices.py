# -*- coding: utf-8 -*-
"""
设备档案引擎：识别导出文件来自哪台设备，把不同版式归一成标准宽表。

不同厂商/型号的检测设备导出的差异主要有四类，本模块逐一处理：
  1. 版式     竖表(Key-Value) / 宽表 / 多行表头 / 单位行 / 统计尾行 / 前导说明行
  2. 列名     每台设备一套私有列名，且同名在不同设备上含义可能不同
  3. 单位     mm↔μm、mA↔A、Ω↔mΩ、%↔ppm 等，混用会导致数值差好几个数量级
  4. 口径     合格判定用 OK/NG、PASS/FAIL、合格/不合格、1/0 各种写法

设计原则：设备档案优先，通用侦测兜底。认不出设备时不能报错，降级处理并如实标注。
"""

try:
    from . import degrade          # 包模式
except Exception:                  # 脚本模式（python lib/pipeline.py）无包上下文
    import degrade                 # noqa
import os
import re
import json
import io

from . import table as T
from . import preprocess as PP

KB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge")
PROFILE_FILE = os.path.join(KB_DIR, "device_profiles.json")

_p_cache = {}


def set_root(path):
    """切换技能包根目录（自检隔离用）。

    必须连设备档案路径一起切：PROFILE_FILE 是模块级常量，只切 tenancy 的 root
    会让它仍指向原目录，于是「写租户私有层」会写进真实共享基线 ——
    自检里就真发生过一次：测试设备 test_coater 被写进了 knowledge/device_profiles.json。
    """
    global KB_DIR, PROFILE_FILE
    KB_DIR = os.path.join(path, "knowledge")
    PROFILE_FILE = os.path.join(KB_DIR, "device_profiles.json")
    invalidate_cache()
    return PROFILE_FILE


def _watch(p):
    """基线设备档案 + 租户覆盖层，任一层变了都要重读"""
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


# ---------------------------------------------------------------- 载入

def load_profiles(path=None):
    """
    设备档案同样分层：基线共享 + 租户私有（每家企业的仪器不一样）
    ——再按行业作用域裁剪（锂电厂看不到光伏组件 IV 仪）。

    缓存 key 带上了行业作用域，切行业会自动 miss，不会串味。
    """
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
    scope_key = tuple(SC.current() or [])
    # 设备档案也要盯文件变更：基线新增了一台设备的档案，长驻进程得能看见
    if path is None and TN.enabled():
        key = ("tenant", TN.current(), scope_key)
        if key in _p_cache:
            if not HS.changed("dev", _watch(PROFILE_FILE)):
                return _p_cache[key]
        data = TN.layered_read(PROFILE_FILE, TN.map_to_tenant(PROFILE_FILE),
                               {"devices": {}, "unit_conversions": {}, "_fallback": {}})
        data = SC.filter_devices(data)[0]
        _p_cache[key] = data
        return data
    p = path or PROFILE_FILE
    key = (path, scope_key)
    if key in _p_cache:
        if not HS.changed("dev", _watch(p)):
            return _p_cache[key]
    try:
        with io.open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"devices": {}, "unit_conversions": {}, "_fallback": {}}
    data = SC.filter_devices(data)[0]
    _p_cache[key] = data
    return data


def invalidate_cache():
    _p_cache.clear()


# scan:keep 对外 API：供 kb_manage.py devices 子命令与人工排查时列出已登记设备，当前由 get_profile 逐个取用，未走列表接口。
def list_devices():
    d = load_profiles()
    return [(k, v.get("name", k), v.get("vendor", ""), v.get("category", ""))
            for k, v in d.get("devices", {}).items()]


def get_profile(device_key):
    d = load_profiles()
    return d.get("devices", {}).get(device_key) or d.get("_fallback", {})


# ---------------------------------------------------------------- 设备识别

def _head_meta_text(path, max_lines=12):
    """读取文件顶部若干行原文，用于抓设备型号/厂商字符串"""
    if not path or not os.path.exists(path):
        return ""
    try:
        with io.open(path, "rb") as f:
            raw = f.read(65536)
        for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
            try:
                return raw.decode(enc)[:4000]
            # scan:keep 探测型容错：逐个编码试解是正常路径，不是降级；末位 latin-1
            # 能解任意字节流，所以"全部失败"实际不可达；真正的读失败（权限/被删）
            # 由本函数外层 handler 记台账 devices._head_meta_text@L134。
            except (UnicodeDecodeError, LookupError):
                continue
    except Exception as _dg_e:
        degrade.note("devices._head_meta_text@L134", _dg_e)
    return ""


_UNIT_TOKEN = re.compile(
    r"^(μm|um|µm|nm|mm|cm|m|℃|°C|C|%|ppm|ppb|V|mV|kV|A|mA|µA|kA|"
    r"Ω|ohm|mΩ|kΩ|MΩ|W|mW|kW|Wh|Ah|mAh|kPa|Pa|MPa|bar|"
    r"N|kN|kg|g|mg|s|ms|min|h|Hz|kHz|rpm|m/min|mm/min|g/m2|mg/cm2|LSB|gray)$",
    re.I)

_TIME_TOKEN = re.compile(r"^(时间|日期|timestamp|date|time|datetime|取样时间|测试时间|记录时间)$", re.I)


def identify_device(table, path=None, head_text=None):
    """
    综合元信息、列名、文件名三路证据打分识别设备。
    返回 {"device": key, "name": str, "score": float, "confidence": str,
          "evidence": [str], "runner_up": (key, score) or None}
    """
    profiles = load_profiles()
    devices = profiles.get("devices", {})
    if not devices:
        return {"device": None, "name": "无设备档案", "score": 0.0,
                "confidence": "无", "evidence": [], "runner_up": None}

    cols = list(table.columns)
    col_norm = [PP._norm_name(c) for c in cols]
    meta = head_text if head_text is not None else _head_meta_text(path)
    fname = os.path.basename(path or "")

    # 多级表头时真正的检测项名在第 2、3 行里（第 1 行往往只是「工艺参数」这种分组标签），
    # 因此把表头下方前 2 行的文本也纳入列名证据——否则这类文件永远识别不到设备。
    extra = set()
    for r in table.rows[:2]:
        for v in r:
            if v is None:
                continue
            s = str(v).strip()
            if s and T.to_number(s) is None:
                extra.add(PP._norm_name(s))
    col_dev = set(col_norm) | extra

    scored = []
    for key, prof in devices.items():
        sig = prof.get("signature") or {}
        weights = sig.get("weights") or {}
        score, evid = 0.0, []

        for pat in sig.get("meta_regex", [])[:8]:
            if re.search(re.escape(pat), meta, re.I):
                score += weights.get("meta_regex", 2.0)
                evid.append("文件含设备标识「%s」" % pat)

        hits = [p for p in sig.get("col_any", []) if PP._norm_name(p) in col_dev]
        if hits:
            score += weights.get("col_any", 1.0) * min(3, len(hits))
            evid.append("命中特征列 %s" % "、".join(hits[:3]))

        for pat in sig.get("filename_regex", []):
            if re.search(pat, fname, re.I):
                score += weights.get("filename_regex", 1.0)
                evid.append("文件名匹配 %s" % pat)
                break

        scored.append((key, score, evid))

    scored.sort(key=lambda x: -x[1])
    best_key, best_score, best_evid = scored[0]
    runner = (scored[1][0], scored[1][1]) if len(scored) > 1 and scored[1][1] > 0 else None

    # 需要足够证据才敢认：单条弱证据不够
    MIN = 2.0
    if best_score < MIN:
        # 特征列没对上，再试一次列指纹：设备固件升级后常多出/少掉几列，
        # 精确的特征列匹配会漏掉「明明是同一台机器」的情况，于是又退回猜版式，
        # 而猜的结果随数据浮动 —— 这正是同一台设备报告结论来回跳的根源。
        try:
            from . import deviceid as DID
        except Exception:
            import deviceid as DID                   # noqa
        try:
            fp = DID.fingerprint(table, path=path)
            fh = DID.match_fingerprint(fp, profiles)
            if fh.get("device"):
                return {"device": fh["device"],
                        "name": get_profile(fh["device"]).get("name", fh["device"]),
                        "score": float(best_score),
                        "confidence": fh.get("confidence") or "指纹匹配",
                        "evidence": (best_evid or []) + [
                            "列指纹相似度 %.0f%%（%s）" % (
                                (fh.get("similarity") or 0) * 100,
                                "列名集合重合" )],
                        "runner_up": runner,
                        "matched_by": "fingerprint",
                        "similarity": fh.get("similarity"),
                        "fingerprint": {"col_sig": fp.get("col_sig"), "n_cols": fp.get("n_cols")}}
        except Exception as _dg_e:
            degrade.note("devices.identify_device@L231", _dg_e)
        return {"device": None, "name": "未识别设备（走通用版式处理）", "score": best_score,
                "confidence": "未识别", "evidence": best_evid, "runner_up": runner,
                "note": "未匹配到已知设备档案，已按通用规则处理。可执行 "
                        "scripts/kb_manage.py devices --interview <文件> 生成录入问卷，"
                        "登记后这台设备下次会被自动认出，不再每次重新推断版式与别名。"}

    if best_score >= 4.0:
        conf = "高"
    elif best_score >= 2.5:
        conf = "中"
    else:
        conf = "低"

    return {"device": best_key, "name": get_profile(best_key).get("name", best_key),
            "score": best_score, "confidence": conf, "evidence": best_evid,
            "runner_up": runner}


# ---------------------------------------------------------------- 版式侦测（无档案时兜底）

def looks_like_unit_row(row_values):
    """整行几乎全是单位符号 -> 是单位行"""
    vals = [str(v).strip() for v in row_values if v is not None and str(v).strip() != ""]
    if len(vals) < 2:
        return False
    ok = sum(1 for v in vals if _UNIT_TOKEN.match(v))
    return ok / len(vals) >= 0.7


def looks_like_detail_row(table, n_check=6):
    """
    判断第 0 行是不是「次级表头行」。

    场景：文件真正的上一级标签（如「工艺参数」「检测结果」）成了表头，
    明细列名（如「固含量(%)」「面密度(g/m²)」）掉进了第 1 行数据。
    判据两条同时成立：① 第 0 行几乎没有数值；② 第 1 行往后出现数值行。
    """
    if not table.rows:
        return False
    r0 = table.rows[0]
    vals = [v for v in r0 if v is not None and str(v).strip() != ""]
    if len(vals) < max(2, int(len(table.columns) * 0.5)):
        return False
    if sum(1 for v in vals if T.to_number(v) is not None) > len(vals) * 0.3:
        return False
    for r in table.rows[1:1 + n_check]:
        if any(T.to_number(v) is not None for v in r if v is not None):
            return True
    return False


def detect_footer_rows(table, markers=None):
    """尾部的合计/均值/标准差行，返回行索引集合（只看最后 15 行）"""
    markers = markers or get_profile(None).get("format", {}).get("footer_markers", [])
    out = set()
    n = len(table.rows)
    if n < 3:
        return out
    for i in range(max(0, n - 15), n):
        row = table.rows[i]
        first = str(row[0]).strip() if row and row[0] is not None else ""
        if not first:
            continue
        if any(m.lower() == first.lower() or first.lower().startswith(m.lower())
               for m in markers):
            out.add(i)
            continue
        # 退化情形：整行只有首列有文字、其余为空或横线
        rest = [v for v in row[1:] if v is not None and str(v).strip() not in ("", "-", "--", "—")]
        if not rest and first:
            out.add(i)
    return out


_UNIT_IN_NAME = re.compile(r"[（(]([^）()]{1,10})[）)]\s*$")


def split_unit_from_name(name):
    """'厚度(μm)' -> ('厚度', 'μm')"""
    m = _UNIT_IN_NAME.search(str(name))
    if not m:
        return str(name), None
    tok = m.group(1).strip()
    if _UNIT_TOKEN.match(tok):
        return str(name)[:m.start()].strip(), tok
    return str(name), None


# ---------------------------------------------------------------- 版式归一

def _merge_header(table, n_rows, join="_"):
    """多行表头合并：上级在前、下级在后，如「工艺参数」「面密度」-> 「工艺参数_面密度」"""
    if n_rows < 2 or len(table.rows) < n_rows - 1:
        return table
    head_rows = [table.rows[i] for i in range(n_rows - 1)]
    names = []
    for c in range(len(table.columns)):
        parts = []
        for r in head_rows:
            v = r[c] if c < len(r) else None
            if v is not None and str(v).strip():
                parts.append(str(v).strip())
        base = [str(table.columns[c]).strip()] if str(table.columns[c]).strip() else []
        allp = base + parts          # 顺序必须是 表头行 -> 明细行
        dedup = []
        for p in allp:
            if p not in dedup:
                dedup.append(p)
        names.append(join.join(dedup) if len(dedup) > 1 else (dedup[0] if dedup else "col_%d" % (c + 1)))
    rows = table.rows[n_rows - 1:]
    return T.Table(names, rows, table.name, table.source_path)


def _detail_header(table, n_rows):
    """
    多级表头只取明细行做列名。适用于上级只是分组标签（如「工艺参数」「检测结果」）、
    真正的检测项在明细行的情形——保留上级前缀反而会污染列名匹配。
    """
    if n_rows < 2 or len(table.rows) < n_rows - 1:
        return table
    detail = table.rows[n_rows - 2]
    names = []
    for c in range(len(table.columns)):
        v = detail[c] if c < len(detail) else None
        v = str(v).strip() if v is not None else ""
        names.append(v if v else str(table.columns[c]))
    rows = table.rows[n_rows - 1:]
    return T.Table(names, rows, table.name, table.source_path)


def _absorb_unit_row(table, mode="suffix"):
    """
    表头下一行若是单位行：
      suffix   -> 并入列名，如 厚度_μm 写成 厚度(μm)
      separate -> 丢弃该行，另记回 unit 清单
    """
    if not table.rows:
        return table, {}
    row = table.rows[0]
    vals = [str(v).strip() if v is not None else "" for v in row]
    if not looks_like_unit_row(vals):
        return table, {}
    units = {}
    if mode == "suffix":
        newcols = []
        for c in range(len(table.columns)):
            u = vals[c] if c < len(vals) else ""
            nm, _ = split_unit_from_name(table.columns[c])
            if u:
                newcols.append("%s(%s)" % (nm, u))
                units[nm] = u
            else:
                newcols.append(nm)
        colnames = newcols
    else:
        colnames = []
        for c in range(len(table.columns)):
            u = vals[c] if c < len(vals) else ""
            nm, _ = split_unit_from_name(table.columns[c])
            if u:
                units[nm] = u
            colnames.append(nm)
    return T.Table(colnames, table.rows[1:], table.name, table.source_path), units


def _looks_pivotable(table, key_col, value_col, group_col=None):
    """
    是否真的需要 pivot。判据：同一 group 内出现多个不同 key（即纵向堆叠）。
    若一行一样品（key 唯一或 key 就是样本名），则本来就是宽表，不该转。
    """
    ki = table.col_index(key_col)
    vi = table.col_index(value_col)
    if ki is None or vi is None:
        return False
    keys = [str(r[ki]).strip() for r in table.rows if ki < len(r) and r[ki] is not None]
    if not keys:
        return False
    uniq = len(set(keys))
    # 同一 key 反复出现 -> 竖表
    return uniq < len(keys) * 0.5


def _pick_col(table, candidates):
    """从候选名里挑一个真实存在的列"""
    nm = {PP._norm_name(c): c for c in table.columns}
    for cand in candidates or []:
        k = PP._norm_name(cand)
        if k in nm:
            return nm[k]
    # 退一步：包含匹配
    for cand in candidates or []:
        k = PP._norm_name(cand)
        for norm, real in nm.items():
            if k and (k in norm or norm in k):
                return real
    return None


def long_to_wide(table, fmt):
    """
    竖表 -> 宽表。按 group 列聚合：key 做列名，value 做单元格。
    无 group 列时，按 key 的重复周期切块。
    """
    key_col = _pick_col(table, fmt.get("long_key_col"))
    val_col = _pick_col(table, fmt.get("long_value_col"))
    grp_col = _pick_col(table, fmt.get("long_group_col"))
    if key_col is None or val_col is None:
        return None, "未能确定竖表的「项目列」与「数值列」，放弃转换"

    ki = table.col_index(key_col)
    vi = table.col_index(val_col)
    gi = table.col_index(grp_col) if grp_col else None

    buckets = []
    order = []
    if gi is not None:
        by_g = {}
        for r in table.rows:
            if ki >= len(r) or vi >= len(r):
                continue
            g = r[gi] if gi < len(r) else None
            g = str(g).strip() if g is not None else ""
            if not g:
                continue
            if g not in by_g:
                by_g[g] = {}
                order.append(g)
            k = str(r[ki]).strip() if r[ki] is not None else ""
            if k:
                by_g[g][k] = r[vi]
        for g in order:
            buckets.append(by_g[g])
    else:
        # 按 key 的重复周期切块：遇到已见过的 key 就开新块
        cur, seen = {}, set()
        for r in table.rows:
            if ki >= len(r) or vi >= len(r):
                continue
            k = str(r[ki]).strip() if r[ki] is not None else ""
            if not k:
                continue
            if k in seen:
                buckets.append(cur)
                cur, seen = {}, set()
            cur[k] = r[vi]
            seen.add(k)
        if cur:
            buckets.append(cur)

    if not buckets:
        return None, "竖表未解析出任何记录"

    allkeys = []
    for b in buckets:
        for k in b.keys():
            if k and k not in allkeys:
                allkeys.append(k)
    rows = [[b.get(k) for k in allkeys] for b in buckets]
    newname = (grp_col or "%s宽表" % table.name)
    nt = T.Table(allkeys, rows, newname, table.source_path)
    if gi is not None:
        nt.add_column(newname, order)
    return nt, "竖表已转宽表：%d 行 × %d 项（按%s分组）" % (
        len(rows), len(allkeys), "「%s」" % grp_col if grp_col else "检测项重复周期")


def apply_format(table, profile):
    """
    按设备档案归一遍版式。返回 (新表, notes, meta)
    注意：不删除任何疑似异常的数据点，只处理结构性噪声（表头/尾行/說明行）。
    """
    notes = []
    meta = {}
    fmt = profile.get("format") or {}

    # 1) 跳过前导噪声行（如设备导出的分隔线、报表标题行）
    skips = fmt.get("skip_row_regex") or []
    keep = []
    for i, r in enumerate(table.rows):
        first = str(r[0]).strip() if r and r[0] is not None else ""
        if first and any(re.match(p, first, re.I) for p in skips):
            continue
        keep.append(r)
    if len(keep) != len(table.rows):
        notes.append("剔除 %d 行前置说明/分隔行" % (len(table.rows) - len(keep)))
    table = T.Table(table.columns, keep, table.name, table.source_path)

    # 2) 多级表头
    nhead = fmt.get("header_rows") or 1
    if nhead == "auto":
        nhead = 2 if looks_like_detail_row(table) else 1
    nhead = int(nhead)
    if nhead > 1 and len(table.rows) >= nhead - 1:
        if fmt.get("header_detail_only"):
            table = _detail_header(table, nhead)
            notes.append("检测到 %d 行多级表头，按档案取明细行为列名" % nhead)
        else:
            table = _merge_header(table, nhead, fmt.get("header_join", "_"))
            notes.append("合并 %d 行多级表头" % nhead)

    # 3) 单位行
    unit_mode = fmt.get("unit_row")
    units = {}
    if unit_mode is True or unit_mode == "suffix" or unit_mode == "auto":
        table, units = _absorb_unit_row(table, "suffix" if unit_mode != "auto" else "suffix")
        if units:
            notes.append("识别到单位行并并入列名：%s" % "、".join(list(units.items())[:4]))
            meta["unit_row"] = units
    elif unit_mode == "separate":
        table, units = _absorb_unit_row(table, "separate")
        if units:
            notes.append("识别到单位行（独立模式，已记录单位映射）")
            meta["unit_row"] = units

    # 4) 剔除尾部统计行（合计/均值/σ）。必须在 pivot 之前做：
    #    否则竖表里的「平均」「σn-1」会被当成一个样本参与聚合，直接污染统计结果。
    bad = detect_footer_rows(table, fmt.get("footer_markers"))
    if bad:
        keep_rows = [r for i, r in enumerate(table.rows) if i not in bad]
        notes.append("剔除 %d 行统计/合计尾行（合计、均值、σ 不是观测值，留着会带偏均值与 σ）"
                     % len(bad))
        table = T.Table(table.columns, keep_rows, table.name, table.source_path)

    # 5) 竖表 -> 宽表
    layout = fmt.get("layout") or "wide"
    if layout == "auto":
        key_col = _pick_col(table, fmt.get("long_key_col"))
        val_col = _pick_col(table, fmt.get("long_value_col"))
        if key_col and val_col and _looks_pivotable(table, key_col, val_col):
            layout = "long"
    if layout == "long":
        nt, msg = long_to_wide(table, fmt)
        if nt is not None:
            table = nt
            notes.append(msg)
        else:
            notes.append("档案标注为竖表，但" + msg + "，按原宽表继续")

    # 6. 丢弃无用列
    drops = [c for c in (profile.get("drop_cols") or []) if c in table.columns]
    if drops:
        table.drop_columns(drops)
        notes.append("丢弃档案标注的无用列：%s" % "、".join(drops[:6]))

    # 7. 列错位检测。设备导出时若某几行少写了几个字段，会被静默补空，
    #    表现为某列"看着存在但全是空值"，分析结果会离谱却毫无提示。
    if table.rows:
        nrow = len(table.rows)
        empties = []
        for c in table.columns:
            vals = [r[table.col_index(c)] for r in table.rows
                    if table.col_index(c) < len(r)]
            blank = sum(1 for v in vals if v is None or str(v).strip() in ("", "-", "--", "—"))
            if blank > nrow * 0.9:
                empties.append(c)
        if empties:
            meta["empty_cols"] = empties
            notes.append("发现 %d 个几乎全空的列，疑似导出时字段错位：%s。"
                         "请核对原始文件的列对齐，这些列不参与分析"
                         % (len(empties), "、".join(empties[:6])))

    return table, notes, meta


# ---------------------------------------------------------------- 单位换算

_UNIT_ALIAS_CACHE = {}


def _canon_unit(u):
    """把各种单位写法归到标准写法：wt%/%/wt.%、μm/um/µm、Ω/ohm 都是同一个单位。"""
    if not u:
        return ""
    key = str(u).strip().lower()
    if key in _UNIT_ALIAS_CACHE:
        return _UNIT_ALIAS_CACHE[key]
    try:
        aliases = load_profiles().get("unit_aliases") or {}
    except Exception:
        aliases = {}
    hit = None
    for canon, forms in aliases.items():
        if key in [str(f).lower() for f in forms] or key == str(canon).lower():
            hit = canon
            break
    if hit is None:
        hit = str(u).strip()
    _UNIT_ALIAS_CACHE[key] = hit
    return hit


def _conv_factor(convs, key, src_unit, dst_unit):
    if not src_unit or not dst_unit:
        return None
    su, du = _canon_unit(src_unit), _canon_unit(dst_unit)
    if su == du:
        return 1.0
    for c in convs.values():
        if _canon_unit(c.get("from", "")) == su and _canon_unit(c.get("to", "")) == du:
            return float(c["factor"])
        # 反向：to->from 时取倒数
        if _canon_unit(c.get("to", "")) == su and _canon_unit(c.get("from", "")) == du:
            return 1.0 / float(c["factor"])
    return None


def apply_units(table, profile, col_map=None):
    """
    统一单位到知识库标准单位。

    单位来源有两个，都要支持：
      a) 写在列名括号里 ——「厚度(mm)」这种，最常见
      b) 竖表转出来的列名通常不带单位（如「厚み」），单位只能靠设备档案的 key_unit_map 补
    目标单位优先取档案显式指定，否则取该参数在工艺知识库里的标准单位。
    """
    profiles = load_profiles()
    convs = profiles.get("unit_conversions", {})
    spec_map = PP.load_knowledge().get("params", {})
    applied, warns = [], []
    key_units = profile.get("key_unit_map") or {}
    explicit_map = profile.get("unit_map") or {}

    for c in list(table.columns):
        base, src_unit = split_unit_from_name(c)
        key = (col_map or {}).get(c)
        spec = spec_map.get(key, {}) if key else {}

        ku = key_units.get(base) or key_units.get(c)
        if not src_unit and ku and ku.get("read_unit"):
            src_unit = ku["read_unit"]
        if not src_unit:
            continue

        dst_unit = None
        if ku and ku.get("target_unit"):
            dst_unit = ku["target_unit"]
        exp = explicit_map.get(c) or (ku and ku.get("conversion")) or None
        if exp and isinstance(exp, dict) and exp.get("target_unit"):
            dst_unit = exp["target_unit"]
        if not dst_unit:
            dst_unit = spec.get("unit")

        if key and not dst_unit:
            warns.append("「%s」的标准单位在知识库中未定义，未做换算" % base)
            continue

        f = _conv_factor(convs, None, src_unit, dst_unit)
        if isinstance(exp, dict) and exp.get("conversion") in convs:
            cv = convs[exp["conversion"]]
            f = cv["factor"] if cv["from"] == src_unit else f
        if f is None:
            if dst_unit and src_unit != dst_unit:
                warns.append("「%s」单位 %s → %s 无换算路径，未换算（请人工确认单位写法）"
                             % (base, src_unit, dst_unit))
            continue
        if f == 1.0:
            continue

        vals = [T.to_number(v) for v in table.column(c)]
        newvals = [(v * f) if v is not None else None for v in vals]
        i = table.col_index(c)
        for r, v in zip(table.rows, newvals):
            if i < len(r):
                r[i] = None if v is None else round(v, 6)
        newname = "%s(%s)" % (base, dst_unit) if dst_unit else base
        # 撞名保护：表里已有同名列时（例如同时导出了 mm 与 μm 两版厚度），
        # 直接改名会让两列合成一个名字，后面谁也分不清用的是哪一列
        others = [x for j, x in enumerate(table.columns) if j != i]
        if newname in others:
            newname = "%s(%s→%s)" % (base, src_unit, dst_unit)
            warns.append("「%s」换算后与已有列同名，已保留来源单位以便区分：%s"
                         % (base, newname))
        table.columns[i] = newname
        applied.append({"column": c, "new_column": newname,
                        "from": src_unit, "to": dst_unit, "factor": f,
                        "source": "列名" if split_unit_from_name(c)[1] else "设备档案"})
    return applied, warns


# ---------------------------------------------------------------- 列名映射（设备级优先）

def map_columns_device(table, device_key=None, prefer_process=None):
    """
    设备档案的私有别名优先于通用别名池，避免同名在不同设备上含义不同时被误判。
    返回 {"map": {原列名: key or None}, "source": {原列名: 'device'|'generic'|None}}
    """
    prof = get_profile(device_key) if device_key else {}
    dev_alias = {PP._norm_name(k): v for k, v in (prof.get("aliases") or {}).items() if v}
    result, source = {}, {}
    for c in table.columns:
        n = PP._norm_name(c)
        base, _u = split_unit_from_name(c)
        nb = PP._norm_name(base)
        hit = None
        if n in dev_alias:
            hit = dev_alias[n]
        elif nb in dev_alias:
            hit = dev_alias[nb]
        if hit:
            result[c], source[c] = hit, "device"
            continue
        g = PP.match_param(c, prefer_process=prefer_process)
        result[c], source[c] = g, ("generic" if g else None)
    return {"map": result, "source": source}


def apply_attr_maps(table, profile, col_map=None):
    """把 OK/NG、PASS/FAIL、合格/不合格等编码成 0/1，供 SPC 属性图与 Pareto 使用"""
    maps = profile.get("attr_maps") or {}
    applied = []
    for std_key, defs in maps.items():
        target = None
        for c, k in (col_map or {}).items():
            if k == std_key:
                target = c
                break
        if target is None:
            for c in table.columns:
                if PP._norm_name(c) == PP._norm_name(std_key):
                    target = c
                    break
        if target is None:
            continue
        ok_set = set(PP._norm_name(x) for x in (defs.get("ok") or []))
        ng_set = set(PP._norm_name(x) for x in (defs.get("ng") or []))
        vals = table.column(target)
        out, hitcnt = [], 0
        for v in vals:
            s = PP._norm_name(v) if v is not None else ""
            if s in ok_set:
                out.append(1); hitcnt += 1
            elif s in ng_set:
                out.append(0); hitcnt += 1
            else:
                out.append(v)
        if hitcnt == 0:
            continue
        i = table.col_index(target)
        for r, v in zip(table.rows, out):
            if i < len(r):
                r[i] = v
        table.columns[i] = "%s(1=合格)" % target
        applied.append({"column": target, "coded": hitcnt, "of": len(vals)})
    return applied


# ---------------------------------------------------------------- 统一入口

def ingest(path, sheet=None, prefer_process=None, verbose=False):
    """
    读一个文件 -> 识别设备 -> 归一版式 -> 单位换算 -> 列名映射。
    返回 dict，任何一步失败都降级并记 note，绝不抛异常中断主流程。
    """
    out = {"path": path, "device": None, "device_info": {}, "table": None,
           "notes": [], "warnings": [], "units_applied": [],
           "attr_applied": [], "col_map": {}, "col_source": {}, "profile_note": ""}

    try:
        tb = T.read_any(path, sheet=sheet)
    except Exception as e:
        out["warnings"].append("读取失败：%s" % e)
        return out

    ident = identify_device(tb, path=path)
    out["device"] = ident.get("device")
    out["device_info"] = ident
    prof_key = ident.get("device")
    prof = get_profile(prof_key) if prof_key else get_profile(None)
    if prof_key:
        out["notes"].append("识别为「%s」（置信度%s，得分%.1f）"
                            % (prof.get("name", prof_key), ident.get("confidence"),
                               ident.get("score") or 0))
    else:
        out["notes"].append(ident.get("note") or "未匹配到设备档案，按通用版式处理")

    if prof.get("export_note"):
        out["profile_note"] = prof["export_note"]

    try:
        tb, notes, meta = apply_format(tb, prof)
        out["notes"].extend(notes)
        out.update({"format_flags": meta})
    except Exception as e:
        out["warnings"].append("版式归一失败，按原始表继续：%s" % e)

    mres = map_columns_device(tb, prof_key, prefer_process=prefer_process)
    out["col_map"] = mres["map"]
    out["col_source"] = mres["source"]

    try:
        applied, warns = apply_units(tb, prof, col_map=mres["map"])
        out["units_applied"], out["warnings"] = applied, out["warnings"] + warns
        for a in applied:
            # 换算后列名变了，映射要跟着迁
            if a["column"] in out["col_map"]:
                out["col_map"][a["new_column"]] = out["col_map"].pop(a["column"])
            if a["column"] in out["col_source"]:
                out["col_source"][a["new_column"]] = out["col_source"].pop(a["column"])
    except Exception as e:
        out["warnings"].append("单位换算失败：%s" % e)

    try:
        out["attr_applied"] = apply_attr_maps(tb, prof, col_map=out["col_map"])
    except Exception as e:
        out["warnings"].append("合格判定口径编码失败：%s" % e)

    out["table"] = tb
    if len(tb.rows) == 0:
        out["warnings"].append("归一端到零行数据，可能是版式判断错误，请检查原始文件")
    return out


# scan:keep 批量入口：多设备文件一次性归一。当前流水线逐个文件 ingest，接批量导入需求时启用。
def ingest_many(paths, prefer_process=None):
    return [ingest(p, prefer_process=prefer_process) for p in paths]
