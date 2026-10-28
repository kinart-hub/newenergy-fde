# -*- coding: utf-8 -*-
"""增量摄入：把 MES 反复导出的多批次文件，安全累积成一份可信历史。

为什么这里没有一个"连 MES"的客户端
------------------------------------
各厂 MES 的取数形态（OPC UA / 中间库 / REST / 定时导表）没有通用协议，
在这里写一个"某厂客户端"既跑不起来也维护不了，换个厂就废。真正通用的是
**落盘之后那一步**：现场用任意手段把数据导出到一个目录（或一个能追加的
CSV），本模块负责回答三个问题——哪些文件是新的、哪些行是重复的、
上次处理到哪。这样既不锁死厂商，也不要求现场改他们的导出流程。

三件做错了比不做更糟的事
------------------------
1) **去重**。MES 常按时间窗导出，相邻批次必然重叠（今天导 1-7 号，明天
   导 4-10 号）。重复行会让 σ 被低估、Cpk 虚高，还会把 SPC 的"连续 N 点"
   规则打成假报警。但键**猜错**会把两条本来不同的观测当重复删掉——那是
   丢数据，比重复严重得多。所以：自动模式只做"整行完全相同"去重；按业务
   键去重必须由使用人显式指定。
2) **键冲突**。同一个业务键出现两个不同的值，说明源数据被改过（补录、
   返修、修正）。此时不覆盖、不丢弃、两行都留，单独报冲突交人确认——
   替谁做主都可能把真相删掉。
3) **游标**。游标坏了不能静默少算。读不出来就退回全量并明说，
   不能让使用者以为"本次只有 3 行新增"。

对外只暴露三个动作：
    scan(dir)                  → 目录里有哪些数据文件
    merge(tables, ...)         → 把同构多批次合成一张表，并去重记账
    load_cursor / save_cursor  → 增量水位线
"""

import hashlib
import io
import json
import os
import tempfile

try:
    from .table import Table, parse_datetime
except Exception:                                    # 直接跑脚本时的兜底
    from table import Table, parse_datetime         # noqa

DATA_EXT = (".csv", ".txt", ".tsv", ".xlsx", ".xlsm", ".xls")

# 同一张表的判定阈值：列名集合的 Jaccard 相似度。
# 0.8 的依据：MES 不同批次之间偶尔多一列（新增设备采集点）或少一列（人工填的
# 备注），仍属同一张表；而"涂布"与"卷绕"两张工艺表的列名重合度通常不到一半。
# 阈值不设 1.0，是因为那样一列差异就会把同一工序的批次判成两张表，
# 最后退化成"每个文件单独分析一遍"——正是本模块要消灭的人工切分。
SAME_SHAPE_JACCARD = 0.8

CURSOR_VERSION = 1


# --------------------------------------------------------------- 目录与指纹
def scan(dirpath, recursive=False):
    """列出目录下的数据文件（排序稳定，保证两次运行顺序一致）。

    排序按 (修改时间, 文件名)：MES 导出文件的文件名里常有时间戳，
    但不同厂格式不一；按 mtime 排能保证"早批次在前"，报告的批次表读起来
    才符合人的直觉。
    """
    if not dirpath or not os.path.isdir(dirpath):
        return []
    found = []
    if recursive:
        for root, _dirs, files in os.walk(dirpath):
            for f in files:
                if f.lower().endswith(DATA_EXT) and not f.startswith("~$"):
                    found.append(os.path.join(root, f))
    else:
        for f in os.listdir(dirpath):
            p = os.path.join(dirpath, f)
            if os.path.isfile(p) and f.lower().endswith(DATA_EXT) \
                    and not f.startswith("~$"):
                found.append(p)

    def _key(p):
        try:
            mt = os.path.getmtime(p)
        except OSError:
            mt = 0
        return (mt, os.path.basename(p))

    return sorted(found, key=_key)


def fingerprint(path):
    """文件指纹：内容 sha256 前 16 位 + 字节数。

    只用 mtime 不够——现场拷贝/同步会改 mtime 而不改内容，会导致整批重算；
    只用大小又挡不住同尺寸的修订。
    """
    h = hashlib.sha256()
    try:
        with io.open(path, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                h.update(b)
        size = os.path.getsize(path)
    except (IOError, OSError):
        return None
    return {"sha": h.hexdigest()[:16], "bytes": size,
            "file": os.path.basename(path)}


# --------------------------------------------------------------- 同构判定
def _colset(tb):
    return set(c.strip() for c in tb.columns if c is not None)


def shape_similarity(a, b):
    """两张表的列名 Jaccard 相似度（0~1）。空表返回 0。"""
    sa, sb = _colset(a), _colset(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return float(inter) / float(len(sa | sb))


def is_same_shape(a, b, threshold=SAME_SHAPE_JACCARD):
    return shape_similarity(a, b) >= threshold


# --------------------------------------------------------------- 去重
def _norm_key(v):
    """键值归一：去空白、数值统一形态。

    不归一的话 "120" / "120.0" / " 120 " 会被当成三个键，去重完全失效——
    MES 与 Excel 混用时这种差异必然出现。
    """
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    try:
        f = float(s)
        # 只对"看起来是数值"的做归一，避免把 "001" 这样的批次号改成 "1"
        if s.replace(".", "", 1).replace("-", "", 1).isdigit():
            if f == int(f):
                return str(int(f))
        return repr(f)
    except (TypeError, ValueError):
        return s


def _row_key(row, idxs):
    return tuple(_norm_key(row[i]) if i < len(row) else "" for i in idxs)


def _same_value(a, b):
    """两个单元格是否"实质相同"。

    数值用相对容差：MES 重导同一段时间窗，末位常差 1e-9（浮点重算），
    逐字比较会把它判成"数据被改过"，于是每批都刷一堆冲突——
    噪音一多，真正的改写（补录/返修）就被埋掉了。
    """
    if a is None and b is None:
        return True
    sa = "" if a is None else str(a).strip()
    sb = "" if b is None else str(b).strip()
    if sa == sb:
        return True
    try:
        fa, fb = float(sa), float(sb)
    except (TypeError, ValueError):
        return False
    if fa == fb:
        return True
    return abs(fa - fb) <= 1e-9 * max(1.0, abs(fa), abs(fb))


def _same_row(a, b):
    if len(a) != len(b):
        return False
    return all(_same_value(a[i], b[i]) for i in range(len(a)))


def dedup_rows(rows, columns, key_cols=None, on_conflict="last"):
    """按键去重，返回 (保留行下标, 记账)。

    - key_cols 为空/None：键 = 整行（保守模式，只删完全相同的行）。
      这是**唯一**可以自动做的去重：整行一字不差才删，不会误删真观测。
    - key_cols 给定：按键去重。同键不同值＝**源数据被改过**（补录/返修/修正），
      这时工具无法自己判断哪个是对的，三种处置都必须由调用方选定并记账：

        first  保留先出现的（早批次为准）
        last   保留后出现的（新导出覆盖旧导出）——默认，符合增量语义
        keep   两行都留，交给人工核对（会如实报告"数据里有重复业务键"）

      默认取 last 的理由：MES 按时间窗重导时，后一批通常才是修正后的值。
      但这是业务约定，不是普适真理——所以它被打成参数，且在报告里写明用了哪种。
    """
    if on_conflict not in ("first", "last", "keep"):
        on_conflict = "last"
    idxs = []
    if key_cols:
        cmap = {c.strip(): i for i, c in enumerate(columns)}
        miss = [c for c in key_cols if c not in cmap]
        if miss:
            return list(range(len(rows))), {
                "key_mode": "none", "key_cols": None, "on_conflict": None,
                "n_dup": 0, "n_conflict": 0, "n_overwritten": 0,
                "conflicts": [],
                "warning": "去重键里有不存在的列（%s），本次未去重——"
                           "按错的键去重会删掉真实观测" % "、".join(miss),
            }
        idxs = [cmap[c] for c in key_cols]
        mode = "explicit"
    else:
        idxs = list(range(len(columns)))
        mode = "whole_row"

    held = {}          # key -> [存活的原行下标]
    conflicts = []
    n_dup = 0
    n_over = 0
    for i, r in enumerate(rows):
        k = _row_key(r, idxs)
        prev = held.get(k)
        if not prev:
            held[k] = [i]
            continue
        if mode == "whole_row" or _same_row(rows[prev[-1]], r):
            n_dup += 1
            continue
        # 同键不同值：源数据被改过
        if on_conflict == "first":
            kept_idx, n_over = prev[0], n_over + 1
        elif on_conflict == "keep":
            prev.append(i)
            kept_idx = None                       # 两行都在，没有"胜出者"
        else:                                     # last：新导出覆盖旧导出
            held[k] = [i]
            kept_idx, n_over = i, n_over + 1
        conflicts.append({"key": list(k), "rows": [prev[0], i],
                          "kept_row": kept_idx})

    keep = sorted(i for v in held.values() for i in v)
    return keep, {
        "key_mode": mode,
        "key_cols": list(key_cols) if key_cols else None,
        "on_conflict": on_conflict if mode == "explicit" else None,
        "n_dup": n_dup,
        "n_conflict": len(conflicts),
        "n_overwritten": n_over,
        "conflicts": conflicts[:20],
        "n_conflict_total": len(conflicts),
        "warning": None if not conflicts else
                   ("有 %d 处同键不同值：同一业务键出现了两个版本的数据，"
                    "已按 %s 处置（覆盖 %d 行），明细见报告——"
                    "请回源文件确认哪个才是对的"
                    % (len(conflicts),
                       {"first": "保留早批次", "last": "保留晚批次",
                        "keep": "两行都保留"}[on_conflict], n_over)),
    }


# --------------------------------------------------------------- 合并
def merge(tables, key_cols=None, time_col=None, dedup=True,
          on_conflict="last"):
    """把同构多批次合成一张表，保留行级出处，并给出摄入记账。

    出处必须逐行保住：报告里的"第 N 点异常"要能回到**哪个文件第几行**。
    合并后只剩一个文件名的话，客户那边的核对就断了。
    """
    tables = [t for t in (tables or []) if t is not None and len(t.rows) > 0]
    if not tables:
        return {"table": None, "n_in": 0, "batches": [], "key_mode": "none"}

    if len(tables) == 1:
        tb = tables[0]
        keep = list(range(len(tb.rows)))
        info = {"key_mode": "single", "key_cols": None, "n_dup": 0,
                "n_conflict": 0, "conflicts": [], "warning": None}
        return {"table": tb, "n_in": len(tb.rows),
                "batches": [_batch_meta(tb, len(tb.rows), len(tb.rows), time_col)],
                "n_dup": 0, "n_conflict": 0, "conflicts": [],
                "key_mode": "single", "unions": [c for c in tb.columns]}

    # 列并集（保持首次出现顺序；后出现的表缺列填 None）
    cols = []
    for t in tables:
        for c in t.columns:
            if c not in cols:
                cols.append(c)

    rows, src_map = [], []
    for t in tables:
        pos = {c: i for i, c in enumerate(cols)}
        tpos = {c: i for i, c in enumerate(t.columns)}
        for k, r in enumerate(t.rows):
            out = [None] * len(cols)
            for c, j in tpos.items():
                out[pos[c]] = r[j] if j < len(r) else None
            rows.append(out)
            # 逐行出处：优先用该表自己的 (文件, 原始行号)
            f, ln = t.src_of(k) if hasattr(t, "src_of") else (t.src_file(), None)
            src_map.append((f, ln))

    if dedup:
        keep, info = dedup_rows(rows, cols, key_cols=key_cols,
                                on_conflict=on_conflict)
    else:
        keep = list(range(len(rows)))
        info = {"key_mode": "off", "key_cols": None, "n_dup": 0,
                "n_conflict": 0, "n_overwritten": 0, "conflicts": [],
                "warning": None}

    out_rows = [rows[i] for i in keep]
    out_src = [src_map[i] for i in keep]

    merged = Table(cols, out_rows, name=tables[0].name)
    merged.src_map = out_src
    if tables[0].source_path:
        merged.source_path = tables[0].source_path
    merged.src_rows = [ln for (_f, ln) in out_src]

    # 各批次记账（去重后的行数按"存活行来自哪个文件"统计）
    per = {}
    for i in keep:
        f = src_map[i][0] or "?"
        per[f] = per.get(f, 0) + 1
    batches = []
    for t in tables:
        f = t.src_file() or "?"
        batches.append(_batch_meta(t, len(t.rows), per.get(f, 0), time_col))
    return {"table": merged, "n_in": len(rows), "n_out": len(out_rows),
            "batches": batches, "n_dup": info["n_dup"],
            "n_conflict": info["n_conflict"],
            "n_overwritten": info.get("n_overwritten", 0),
            "on_conflict": info.get("on_conflict"),
            "conflicts": info["conflicts"], "key_mode": info["key_mode"],
            "key_cols": info.get("key_cols"),
            "warning": info.get("warning"),
            "columns_union": cols,
            "n_columns_added": len(cols) - len(tables[0].columns)}


def _batch_meta(tb, n_rows, n_kept, time_col):
    """单批记账：行数、存活行数、时间覆盖范围。

    时间覆盖范围要**按该批自己的行**算：它回答"这批数据覆盖了哪段时间"，
    是现场判断"有没有漏导"的直接依据。所以这里不要求整列都是时间
    （小批表常常只有两三行，按比例卡阈值会一块都认不出来）。
    """
    lo = hi = None
    idx = None
    if time_col and tb.col_index(time_col) is not None:
        idx = tb.col_index(time_col)
    else:
        for c in tb.columns:
            j = tb.col_index(c)
            probe = tb.rows[:30]
            if not probe:
                break
            hits = sum(1 for r in probe
                       if j < len(r) and parse_datetime(r[j]) is not None)
            # 至少两行能解析、且过半，才认这一列是时间列
            if hits >= 2 and hits * 2 >= len(probe):
                idx = j
                break
    if idx is not None:
        vals = [parse_datetime(r[idx]) for r in tb.rows[:5000] if idx < len(r)]
        vals = [v for v in vals if v is not None]
        if vals:
            lo, hi = min(vals), max(vals)
    return {"file": tb.src_file() or tb.name, "rows": n_rows,
            "rows_kept": n_kept,
            "rows_dup": max(0, n_rows - n_kept),
            "from": lo.isoformat(sep=" ") if lo else None,
            "to": hi.isoformat(sep=" ") if hi else None}


# --------------------------------------------------------------- 游标
def load_cursor(path):
    """读增量水位线。读不出来返回 None —— 由调用方出声，不在这里猜。"""
    if not path or not os.path.exists(path):
        return None
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
    except (IOError, OSError, ValueError):
        return None
    if not isinstance(st, dict) or st.get("version") != CURSOR_VERSION:
        return None
    return st


def save_cursor(path, files_meta, n_rows=0):
    """原子写游标（临时文件 + os.replace）。

    直接覆盖写在断电/并发时会留下半截 JSON，下次读到坏游标要么报错、
    要么被当成"第一次运行"全量重算——后者更糟，因为没人会注意到。
    """
    if not path:
        return None
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    st = {"version": CURSOR_VERSION,
          "files": {k: {"sha": v.get("sha"), "bytes": v.get("bytes"),
                        "rows": v.get("rows")} for k, v in files_meta.items()},
          "n_rows": n_rows}
    fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".cursor-", suffix=".tmp")
    try:
        with io.open(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(st, ensure_ascii=False, indent=1))
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            # scan:keep 这里是失败路径上的临时文件清理：删不掉只会在临时目录留个
            # 残片，而原异常必须原样抛出——用清理失败去覆盖真正的失败原因更糟。
            pass
        raise
    return path


def diff_cursor(state, files_meta):
    """对比游标，分出 新增 / 变更（追加行数） / 未变。

    返回的结构直接进报告——现场要的是"这批数据里哪些是新的"，
    而不是一个"已处理"的勾。
    """
    if state is None:
        return {"status": "no_cursor", "new": sorted(files_meta),
                "changed": [], "unchanged": [],
                "n_new_files": len(files_meta), "rows_added": None}
    old = state.get("files") or {}
    new, changed, unchanged = [], [], []
    rows_added = 0
    for name, meta in files_meta.items():
        o = old.get(name)
        if o is None:
            new.append(name)
            rows_added += int(meta.get("rows") or 0)
        elif o.get("sha") != meta.get("sha"):
            changed.append(name)
            rows_added += max(0, int(meta.get("rows") or 0)
                              - int(o.get("rows") or 0))
        else:
            unchanged.append(name)
    return {"status": "ok", "new": sorted(new), "changed": sorted(changed),
            "unchanged": sorted(unchanged), "n_new_files": len(new),
            "rows_added": rows_added}
