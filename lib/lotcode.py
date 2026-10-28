# -*- coding: utf-8 -*-
"""批号拆解：从编码里提取出真正能分组的段。

现场数据里的批号往往不是"一个批一行"，而是一个编码里压了三层信息：

    SUP01-20240301-A001       供应商 / 日期 / 流水
    C01_2403_07               产线 / 年月 / 班次
    A2403010007               前缀 + 日期 + 序号

按整列分组时，每个批号自成一组建不了模；直接丢掉，又扔掉了唯一能区分
"哪家料、哪条线、哪一天"的信息。这一层做的就是把它拆开，并且**验证
拆出来的段能不能当分组维度**——不是拆完就当分组用。

两条必须守住的底线（做错了比不做更糟）
--------------------------------------
1) **流水号不是分组维度**。流水号每段只有一两行，组内没有重复就无法估计
   组内波动；按它分组，η² 会接近 1，看上去"解释力极强"，实际只是给每行
   贴了个编号。这类段一律判 identifier，绝不进分组候选。这是本模块最容
   易犯的错，也是最难被发现的错——报告会因此给出一个"极其显著"的假根因。
2) **拆不准就不拆**。段数不一致、分隔符混用时，硬按位置切会把不同位置的
   字段混成一段（供应商代码被切掉一半）。这时返回 no_split / irregular 并
   说明原因，让使用人给 --lot-pattern，而不是硬拆一个看起来整齐的结果出来。

同样不做的事：不给段起名字。"第 1 段有 4 个取值、形如 SUP0x" 是事实；
"第 1 段是供应商" 是猜测。猜错了，后面追究根因时会一路错下去。
"""

import re
from collections import Counter

# 批号/卷号/工单一类的列名词根。命中即视为"编码型标识列"的候选。
HINTS = ("批号", "批次", "批", "lot", "batch", "卷号", "卷芯", "serial",
         "sn", "料号", "工单", "订单", "托盘", "箱号", "cod")

# 常见分隔符，按"更像分隔符"的顺序试
SEPARATORS = ("-", "_", "/", ".", " ", "#", "|", ":", "—")

# 段被判定为"可分组"的门槛
MAX_GROUPS = 60          # 组数上限：超过就把噪声当维度
MIN_MEDIAN_SIZE = 3      # 组内中位样本数：太少无法估计组内波动
MIN_GROUPS = 2

# 序号段 η² 超过这个值时，报告必须写明"这个 η² 不能直接采信"。
# 0.06 是 Cohen 对 η² 的"中等效应"门槛——低于它的差别连解释力都谈不上；
# 高于它则很可能来自与其它因素的混杂（同一序号在多条产线上各出现一次，
# 就把产线差异借过来了），必须点出来而不是当成显著分组依据。
SEQ_ETA2_WARN = 0.06

_DATE8 = re.compile(r"^(19|20)\d{6}$")
_DATE6 = re.compile(r"^\d{6}$")
_ALPHA_NUM = re.compile(r"^([A-Za-z]+)[-_ ]?(\d.*)$")
_NUM_IN = re.compile(r"\d+")


def _max_run(vals):
    """同一个值最长连续出现几次。

    区分"分组块"与"序号段"的关键：同一家供应商/同一条产线的数据是**连续成块**
    的（连做几十批），而序号段每行一个、不会连续重复。光看"是不是纯数字"不够——
    `A001/A002` 这种带字母前缀的序号遍地都是。
    """
    mx = run = 1
    for i in range(1, len(vals)):
        run = run + 1 if vals[i] == vals[i - 1] else 1
        if run > mx:
            mx = run
    return mx if vals else 0


def _is_sequence(seg):
    """这一段是不是序号（流水号），而不是分类码。

    两条一起看：
      1) 不连续成块（连续同值很少）——成块的是分组维度；
      2) 数字部分基本随行号递增——递增说明它是"第几个"，不是"哪一类"。
    """
    vals = [_s(v) for v in seg if _s(v)]
    if len(vals) < 4:
        return False
    if _max_run(vals) >= 3:
        return False
    nums = []
    for v in vals:
        m = _NUM_IN.search(v)
        if not m:
            return False
        nums.append(int(m.group(0)))
    if len(set(nums)) < 2:
        return False
    inc = sum(1 for i in range(1, len(nums)) if nums[i] > nums[i - 1])
    return inc >= len(nums) * 0.8


def _s(v):
    return "" if v is None else str(v).strip()


def find_columns(table, hints=None):
    """挑出可能是编码型标识的列（列名命中词根）。"""
    hints = tuple(h.lower() for h in (hints or HINTS))
    out = []
    for i, c in enumerate(table.columns):
        low = str(c).lower()
        if any(h in low for h in hints):
            vals = [_s(v) for v in table.column(c)]
            vals = [v for v in vals if v]
            if len(vals) >= max(8, len(table.rows) // 4):
                out.append(c)
    return out


def split_values(values, pattern=None):
    """把一列编码拆成若干段。

    返回 {"status": ok|irregular|bad_pattern|empty|no_split,
          "sep"/"pattern", "n_parts", "parts": [[段值...] 或 None],
          "n_matched", "note"}
    """
    vals = [_s(v) for v in values]
    ok = [v for v in vals if v]
    if not ok:
        return {"status": "empty", "note": "该列没有有效值"}

    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return {"status": "bad_pattern",
                    "note": "给出的模式不是合法正则（%s）" % e}
        parts = []
        miss = 0
        for v in vals:
            if not v:
                parts.append(None)
                continue
            m = rx.match(v) or rx.search(v)
            if not m or not m.groups():
                parts.append(None)
                miss += 1
            else:
                parts.append([g if g is not None else "" for g in m.groups()])
        hit = len([p for p in parts if p])
        if hit < max(2, len(ok) // 2):
            return {"status": "irregular", "pattern": pattern,
                    "n_matched": hit,
                    "note": "自定义模式只匹配到 %d/%d 个值，不足以拆段"
                            % (hit, len(ok))}
        return {"status": "ok", "pattern": pattern,
                "n_parts": len(parts[0]) if parts and parts[0] else 0,
                "parts": parts, "n_matched": hit, "n_unmatched": miss,
                "note": None}

    # 1) 分隔符：要求**每个**非空值都含同一个分隔符，且拆出的段数一致
    for sep in SEPARATORS:
        if not all(sep in v for v in ok):
            continue
        sp = [v.split(sep) for v in vals if v]
        widths = set(len(p) for p in sp)
        if len(widths) != 1 or list(widths)[0] < 2:
            continue
        if any(any(x == "" for x in p) for p in sp):
            continue
        return {"status": "ok", "sep": sep, "n_parts": list(widths)[0],
                "parts": [v.split(sep) if v else None for v in vals],
                "n_matched": len(ok), "n_unmatched": len(vals) - len(ok),
                "note": None}

    # 2) 无分隔符但"字母前缀 + 数字体"：只取前缀那一层（后半段是日期/序号）
    ms = [_ALPHA_NUM.match(v) for v in ok]
    if all(ms):
        prefixes = [m.group(1) for m in ms]
        n_pre = len(set(prefixes))
        if 1 < n_pre <= MAX_GROUPS:
            parts = []
            for v in vals:
                m = _ALPHA_NUM.match(v) if v else None
                parts.append([m.group(1), m.group(2)] if m else None)
            return {"status": "ok", "sep": "(字母前缀)", "n_parts": 2,
                    "parts": parts, "n_matched": len(ok),
                    "n_unmatched": len(vals) - len(ok),
                    "note": "无分隔符，按「字母前缀 + 数字体」拆；"
                            "前缀段才是可能有分组意义的那个"}

    # 3) 等长：按固定位置切。只在所有值等长时才敢做——不等长硬切会错位
    lens = set(len(v) for v in ok)
    if len(lens) == 1 and list(lens)[0] >= 4:
        return {"status": "no_split",
                "note": "该列是等长编码（%d 位）但没有分隔符，按位置硬切会切错字段。"
                        "请用 --lot-pattern 给出正则（如 `^(\\w{5})(\\d{8})(\\d+)$`）"
                        % list(lens)[0]}
    return {"status": "no_split",
            "note": "分隔符不统一或长度参差，无法可靠拆段。"
                    "请用 --lot-pattern 给出正则。"}


def segment_stats(seg, total=None):
    """单段的画像：取值数、组内样本中位数、能不能当分组维度。

    kind 的含义：
      identifier  —— 几乎每行不同（流水号），**不能**当分组
      sequence    —— 不连续成块、数字部分随行号递增（如 001/002/003 或 A001/A002）：
                     这是批号里的序号，按它分组等于问"第 7 个和第 12 个有没有区别"，
                     对追根因无用
      too_few     —— 只有一个取值，没有对比
      too_many    —— 组数超过上限，把噪声当维度
      too_sparse  —— 组内中位样本 < 3，组内波动估不出来
      groupable   —— 可以当分组维度

    sequence **不**按 η² 复活。实测踩过：三段数据里"序号 000–029"的 η² 高达
    0.21，那不是序号的作用——同一序号在三条产线上各出现一次，它把产线差异
    借了过来。按混杂出来的 η² 复活一个序号维度，根因会指向"第 12 个和第 7 个
    不一样"，现场无从下手。真正是产线编号的数字段，靠"连续成块"就能认出来
    （见 _is_sequence），不需要 η² 兜这一层。
    """
    vals = [_s(v) for v in seg]
    ok = [v for v in vals if v]
    n = len(ok)
    if n == 0:
        return {"n_uniq": 0, "kind": "too_few", "median_size": 0,
                "sample": [], "date_format": None, "note": "该段没有有效值"}
    uniq = Counter(ok)
    k = len(uniq)
    sizes = sorted(uniq.values())
    med = sizes[len(sizes) // 2]
    dfmt = _date_like(seg)
    all_digits = all(v.isdigit() for v in ok)
    if k <= 1:
        kind = "too_few"
    elif k >= max(MAX_GROUPS + 1, int(n * 0.6)):
        kind = "identifier"
    elif med < MIN_MEDIAN_SIZE:
        kind = "too_sparse" if k <= MAX_GROUPS else "identifier"
    elif k > MAX_GROUPS:
        kind = "too_many"
    elif dfmt:
        kind = "groupable"          # 日期段：按天比较是有意义的
    elif _is_sequence(seg):
        # 序号段：不连续成块 + 数字部分随行号递增。按它分组＝问
        # "第 7 个和第 12 个有没有区别"，对追根因毫无用处。
        kind = "sequence"
    else:
        kind = "groupable"
    top = [v for v, _c in uniq.most_common(5)]
    return {"n_uniq": k, "kind": kind, "median_size": med,
            "min_size": sizes[0], "max_size": sizes[-1],
            "sample": top, "n": n, "date_format": dfmt}


def _date_like(seg):
    ok = [_s(v) for v in seg if _s(v)]
    if not ok:
        return None
    hit8 = sum(1 for v in ok if _DATE8.match(v))
    if hit8 >= len(ok) * 0.9:
        return "YYYYMMDD"
    hit6 = sum(1 for v in ok if _DATE6.match(v))
    if hit6 >= len(ok) * 0.9:
        return "YYMMDD 或 YYYYMM（6 位）"
    return None


def _eta2(seg_vals, y_vals):
    """段对目标的解释力（单因素 η²）。算不出来返回 None。"""
    try:
        from . import stats as S
    except Exception:
        try:
            import stats as S          # noqa
        except Exception:
            return None
    buckets = {}
    for g, y in zip(seg_vals, y_vals):
        if g in (None, "") or y is None:
            continue
        buckets.setdefault(str(g), []).append(float(y))
    groups = [v for v in buckets.values() if len(v) >= 2]
    if len(groups) < 2:
        return None
    a = S.anova_oneway(groups)
    return a                # {"F","p","eta2",...} 或 None


def analyze(table, col, target=None, pattern=None, y_vals=None):
    """拆一列编码，给每段画像，并挑出可分组的那几段。

    target/y_vals 给出时，按"该段对目标的解释力"排序——这是现场真正关心的：
    哪一段跟不良率有关系。没给目标就按取值数排序（只看结构）。
    """
    sv = split_values(table.column(col), pattern=pattern)
    if sv.get("status") != "ok":
        return {"col": col, "status": sv.get("status"),
                "note": sv.get("note"), "segments": []}
    parts = sv["parts"]
    n_parts = sv["n_parts"]
    segs = []
    for k in range(n_parts):
        seg = [p[k] if p and k < len(p) else None for p in parts]
        st = segment_stats(seg)
        st["idx"] = k + 1
        st["name"] = "%s#%d" % (col, k + 1)
        if y_vals is not None and len(y_vals) == len(seg):
            a = _eta2(seg, y_vals)
            if a:
                st["eta2"] = a.get("eta2")
                st["p"] = a.get("p")
                st["F"] = a.get("F")
            # 序号段**不**按 η² 复活。实测踩过：三段数据里"序号 000–029"
            # 的 η² 高达 0.21，但那不是序号的作用——同一序号在三条产线上各出
            # 现一次，它把产线之间的差异借过来了。按混杂出来的 η² 复活一个
            # 序号维度，根因会指向"第 12 个和第 7 个不一样"，现场无从下手。
            # 真正是产线编号的数字段，靠"连续成块"就能认出来（见 _is_sequence），
            # 不需要 η² 兜这一层。
            if st["kind"] == "sequence" and st.get("eta2") is not None \
                    and st["eta2"] >= SEQ_ETA2_WARN:
                st["eta2_note"] = ("该段 η²=%.2f 看起来显著，但它是序号段，"
                                   "η² 很可能来自与其它因素的混杂，未纳入分组"
                                   % st["eta2"])
        st["values"] = seg
        segs.append(st)
    # 可分组的段：有 η² 按它降序（解释力强的在前）；没有目标时按**段的位置**
    # 升序，而不是按取值数。理由：批号编码的惯例是"前缀＝类别、后缀＝序号"，
    # 按取值数排会把序号段顶到前面——序号段分组的结果是"第 7 个和第 12 个
    # 不一样"，对追根因毫无用处。
    cands = [s for s in segs if s["kind"] == "groupable"]
    if any(s.get("eta2") is not None for s in cands):
        cands.sort(key=lambda s: (-(s.get("eta2") or 0), s["idx"]))
    else:
        cands.sort(key=lambda s: s["idx"])
    return {"col": col, "status": "ok", "sep": sv.get("sep"),
            "pattern": sv.get("pattern"),
            "n_parts": n_parts, "segments": segs,
            "candidates": [s["name"] for s in cands],
            "note": sv.get("note")}


def enrich(table, target_col=None, pattern=None, max_derive=2,
           hints=None, y_vals=None):
    """把能分组的段加成派生列，交给后续分组分析直接用。

    刻意跳过"本身已经能分组"的列：如果批号本身只有 5 个取值，
    pick_groups 早就拿它当维度了，再拆一遍只会多出一堆同义列。
    """
    added, rejected, notes = [], [], []
    y = y_vals
    if y is None and target_col and table.col_index(target_col) is not None:
        y = table.numeric_column(target_col)
    for col in find_columns(table, hints):
        vals = [_s(v) for v in table.column(col)]
        ok = [v for v in vals if v]
        if not ok:
            continue
        # "本身已经能直接分组"的判据必须与 pick_groups 一致：它要求取值数
        # 既不超过 60、也不超过行数的 40%。只判 ≤60 的话，一个 40 行 40 个
        # 唯一值的批号列会被这里当成"已被分组"跳过，而 pick_groups 那边
        # 又因为太散把它排除——两头都不管，使用者看到的是"批号列被忽略了"。
        _u = len(set(ok))
        if _u <= MAX_GROUPS and _u <= len(ok) * 0.4 and len(ok) >= 4:
            continue
        info = analyze(table, col, target=target_col, pattern=pattern,
                       y_vals=y)
        if info.get("status") != "ok":
            if info.get("note"):
                notes.append("「%s」未拆解：%s" % (col, info["note"]))
            rejected.append({"col": col, "reason": info.get("status"),
                             "note": info.get("note")})
            continue
        taken = 0
        _by_name = {s["name"]: s for s in info["segments"]}
        # 按 analyze 排好的候选顺序取（有 η² 时解释力优先，否则前缀优先）
        for name in info["candidates"]:
            s = _by_name.get(name)
            if s is None or s["kind"] != "groupable":
                continue
            if taken >= max_derive:
                break
            if table.col_index(name) is not None:
                continue
            table.add_column(name, s["values"])
            taken += 1
            added.append({"col": col, "name": name,
                          "n_uniq": s["n_uniq"],
                          "median_size": s["median_size"],
                          "eta2": s.get("eta2"), "p": s.get("p"),
                          "sample": s["sample"],
                          "date_format": s.get("date_format")})
        if taken == 0:
            kinds = {}
            for s in info["segments"]:
                kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
            notes.append("「%s」拆出 %d 段，但没有一段适合分组（%s）"
                         % (col, info["n_parts"],
                            "、".join("%s×%d" % (k, v)
                                      for k, v in sorted(kinds.items()))))
        # 被挡掉的段也要交代：不然使用者会问"我批号里明明有流水号/日期，
        # 为什么没用上"——答案必须写在报告里，而不是靠他去猜
        for s in info["segments"]:
            if s["kind"] == "sequence":
                notes.append("「%s」第 %d 段像是流水号（取值 %d 个、随行号递增），"
                             "没有作为分组维度：按它分组的结论会是「第几个和第几个"
                             "不一样」，现场据此无法行动"
                             % (col, s["idx"], s["n_uniq"]))
            elif s["kind"] == "identifier":
                notes.append("「%s」第 %d 段基本每行都不同（%d 个取值），"
                             "是标识不是分类，已排除"
                             % (col, s["idx"], s["n_uniq"]))
            elif s["kind"] == "too_sparse":
                notes.append("「%s」第 %d 段每组只有 %d 行（中位），"
                             "组内波动估不出来，已排除"
                             % (col, s["idx"], s["median_size"]))
    return {"added": added, "rejected": rejected, "notes": notes}


def describe(added):
    """把"加进来的段"写成现场能读的一句话（只讲事实，不给名字）。"""
    out = []
    for a in added:
        bits = ["取值 %d 个" % a["n_uniq"],
                "组内中位 %d 行" % a["median_size"]]
        if a.get("date_format"):
            bits.append("形如日期（%s）" % a["date_format"])
        elif a.get("sample"):
            bits.append("如 " + "/".join(str(x) for x in a["sample"][:3]))
        if a.get("eta2") is not None:
            bits.append("对目标 η²=%.2f" % a["eta2"])
        out.append("「%s」（来自 %s）：%s"
                   % (a["name"], a["col"], "，".join(bits)))
    return out
