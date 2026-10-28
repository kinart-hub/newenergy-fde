# -*- coding: utf-8 -*-
"""无效段识别：把"不是正常生产"的行从逐点统计里剔出去（评审 P0-3）。

为什么必须有这一段
------------------
连续生产（卷对卷涂布、挤出、拉晶、连铸、纺丝……）的导出数据里，通常混着：

  停机/待机   —— 设备停着，传感器仍在按周期上报，值是一条冻结的直线或漂移；
  换卷/换型   —— 头尾料被切掉的那几十米，工艺参数尚未进入稳态；
  调试/暖机   —— 开机后按工艺文件规定必须报废的过渡品；
  首件        —— 每班/每批开头按文件规定要剔的产品。

这些行一旦进 Cpk，后果是双向的：
  · 冻结段几乎零波动 → σ_within 被压小 → Cpk 虚高，明明过程能力不够却显示 1.67；
  · 恢复段单边漂移 → σ_within 被撑大 → Cpk 虚低，明明受控却判不合格。
两种错都比"不算 Cpk"更糟，因为它们看起来都像算出来了。

更要命的是 **σ_within 靠相邻点差分**（移动极差）。直接把剔除点删掉、把前后两块
强行拼在一起，会在接缝处凭空造出一个巨大的差分 —— 等于用删除动作污染了估计量。
所以本模块不删行，而是输出 **连续块（block）**：控制图只在块内画，绝不跨 FAULT 接缝
计算差分。这是本模块唯一不可妥协的设计约束。

凭什么替使用者决定哪一段该剔
----------------------------
不替他决定，只做两件事：

  1. 结构性事实（无需判断）：相邻采样间隔忽然放大到中位间隔的数倍 ——
     这行本身就是"中间断了一大截"的证据，与什么行业无关；
  2. 客观登记（他给事实）：哪个列是设备状态列、工艺文件规定开机后剔几行。
     状态列里哪个值算是"在产"，由数据自己说：连续出现时间最长、占总行数最多的
     那个取值就是在产状态 —— 这是占优事实，不是猜测。

拿不准的一律不剔。剔了什么、剔了多少、为什么，报告里逐条列出来。
"""

from __future__ import unicode_literals

import math

MIN_BLOCK = 3            # 少于这个行数的残段不单独成段（噪声）
FREEZE_RUN = 5           # 目标值连续冻结多少行算"设备没在动"
MIN_UNIQ_RATIO = 0.25    # 取值数/有效行数低于此值的列按离散列处理，不做冻结判定
GAP_MULT = 6.0           # 间隔 > 中位间隔的多少倍算断流
MIN_KEPT_RATIO = 0.50    # 保留比例低于此值 → 规则显然识别错了，放弃排除
MIN_KEPT_ROWS = 30       # 保留行数少于此值 → 同上


def _median(xs):
    ys = sorted(x for x in xs if x is not None)
    if not ys:
        return None
    n = len(ys)
    mid = n // 2
    if n % 2:
        return float(ys[mid])
    return (float(ys[mid - 1]) + float(ys[mid])) / 2.0


def _mad(xs, center=None):
    """绝对中位差。比标准差稳健： outliers 不会把门槛抬到自己之上。"""
    c = center if center is not None else _median(xs)
    if c is None:
        return None
    return _median([abs(float(x) - c) for x in xs if x is not None])


# ---------------------------------------------------------------- 状态列推断

def _num(x):
    try:
        return float(x)
    except Exception:
        return None


def pick_status_values(values, min_run=3, target_values=None):
    """从设备状态列里推断哪些取值代表"没在生产"。

    判据（全部是数据里的客观事实，不含先验词表）：
      1. 该列基本是文本（含少量数值也不打紧，按字符串比）；
      2. 某个取值单独连续出现过 >= min_run 行 —— 状态不会逐行跳变，
         能成块出现才像工况；逐行乱跳的那是采样噪声或另一类变量；
      3. 占行数最多的那个取值 = 在产状态。正常生产的数据里，绝大部分时间
         就是在产；其余成块出现的取值即非生产段。

    第 3 条有个致命反例必须堵：**设备大部分时间开着?不，是停着的数据**。
    一份 2000 行的导出里若 1800 行写着"停机"，占比最大的就是"停机"，
    按第 3 条会把它推定为在产状态，反过来把真正在产的 200 行剔掉——
    而且报告看起来完全正常（告诉你"剔了 200 行非生产段"）。
    堵法是用读数本身交叉验证：给点 target_values，看被推定为在产的那组
    读数是不是几乎不变（冻结/待机特征）。这样就不需要认识"停机"这两个字。

    返回 {"values": [要排除的取值], "assumed_running": 推定的在产值,
          "levels": [[取值, 行数], ...]}；
    判不出来返回 {"values": [], "reason": "..."}。。
    """
    vals = ["" if v is None else str(v).strip() for v in values]
    n = len(vals)
    if n < MIN_BLOCK:
        return {"values": [], "reason": "行数太少（%d 行），不足以判断状态列" % n}
    cnt = {}
    longest = {}
    for v in vals:
        cnt[v] = cnt.get(v, 0) + 1
    for v in vals:
        longest.setdefault(v, 0)
    run, prev = 0, None
    for i, v in enumerate(vals + [None]):
        if v == prev:
            run += 1
        else:
            if prev is not None and run > longest.get(prev, 0):
                longest[prev] = run
            run, prev = 1, v
    if len(cnt) < 2:
        return {"values": [], "reason": "状态列只有 1 种取值（%s），无从区分在产与非在产"
                              % (list(cnt)[0] if cnt else "")}
    if len(cnt) > 12:
        return {"values": [], "reason": "状态列有 %d 种取值，不像工况状态列（更像流水号或名称）"
                              % len(cnt)}
    # 成块出现是每个工况的必要条件：逐行跳变的不是状态
    blocky = [v for v in cnt if longest.get(v, 0) >= min_run]
    if not blocky:
        return {"values": [], "reason": "没有任何取值连续出现 >= %d 行，判不出成块的工况段"
                              % min_run}
    main = max(blocky, key=lambda v: cnt[v])
    # 推定在产的那个必须真的占多数。否则这份数据基本没在生产，
    # 此时与其猜，不如不剔 —— 使用者看报告里"占比"自己就能发现问题。
    if cnt[main] * 2 < n:
        return {"values": [], "reason": "占比最大的取值（%s）也只占 %.0f%%，"
                                        "这份数据恐怕大部分时间没在生产，不敢自动剔除"
                              % (main, cnt[main] * 100.0 / n)}
    _xv = _cross_check_running(vals, main, target_values)
    if _xv:
        return _xv
    others = sorted((v for v in blocky if v != main), key=lambda v: -cnt[v])
    return {
        "values": others,
        "assumed_running": main,
        "levels": sorted(((v, cnt[v]) for v in cnt), key=lambda kv: -kv[1]),
        "reason": None,
    }


# ---------------------------------------------------------------- 结构规则

def _gap_indexes(times, gap_mult=GAP_MULT):
    """相邻采样间隔突然放大的接缝索引 i：表示第 i 行与第 i+1 行之间断了一大截。"""
    idx = []
    if not times:
        return idx
    ds = []
    for i in range(len(times) - 1):
        a, b = times[i], times[i + 1]
        if a is None or b is None:
            continue
        try:
            ds.append((i, (b - a).total_seconds()))
        except Exception:
            # scan:keep 时间差算不出来（时串是 None/非法/混着文本）是逐点容错的正常路径：
            # 这一行本来就不参与断流判定，跳过即可，记账只会污染逐点热路径。
            continue
    pos = [d for _, d in ds if d > 0]
    if len(pos) < 8:
        return idx                      # 样本太少，估不出"正常间隔"
    med = _median(pos)
    if med is None or med <= 0:
        return idx
    mad = _mad(pos, med)
    # 稳健门槛：取「中位间隔的倍数」与「中位数 + k·MAD」的较大者。
    # 只用倍数时，采样本身就很稀疏（间隔天然波动大）会误判；
    # 只用 MAD 时，间隔恒定到 MAD=0 的场景会把任何微小抖动都算成断流。
    thr = max(med * gap_mult, med + 4.0 * 1.4826 * (mad or 0.0))
    for i, d in ds:
        if d > thr:
            idx.append(i)
    return idx


def _freeze_runs(values, min_run=FREEZE_RUN, min_uniq_ratio=MIN_UNIQ_RATIO):
    """连续冻结段：从第 min_run 个重复值开始的那些行。

    设备停着不乱动时传感器读数会定住；正常生产即便控制得再好也会有末位抖动。
    保护条款：整列取值数太少（二值/档位列）直接不判 —— 那种列的"冻结"是它本身的
    性质，不是停机。
    """
    out = []
    if not values:
        return out
    s = [None if v is None else float(v) for v in values]
    valid = [v for v in s if v is not None]
    uniq = set(valid)
    # 保护条款必须跟着行数走，不能写死绝对个数。
    # 早先写的是「取值数 < 20 就不判」，结果 30 行的小样本里取值本来就不够 20 个，
    # 真实的冻结段全部漏判——判据保护了假想的二值列，却漏掉了真实场景。
    # 改成相对多样性：取值数不足有效行数的四分之一，说明这列本身就是离散的
    # （档位/开关/二值），它的"连续相同"是列的性质，不是停机。
    if len(uniq) < max(8, int(len(valid) * MIN_UNIQ_RATIO)):
        return out
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] is not None and s[j + 1] == s[i]:
            j += 1
        run = j - i + 1
        if s[i] is not None and run >= min_run:
            # 保留段首那一行：它是最后一个真实读数，删了反而丢信息
            out.append((i + 1, j, s[i]))
        i = j + 1
    return out


def _cross_check_running(vals, main, target_values, min_each=5, ratio_gap=0.5):
    """用读数本身复核"main 是不是真在产"。

    占多数不等于在生产：一台长期停机的设备，状态列里最多的那行写的就是"停机"。
    这时若照占比推定，会把仅有的在产数据当成非生产段剔掉，
    而报告只会平静地说"剔除了 200 行"——使用者完全看不出方向反了。

    交叉验证的落脚点是：在产状态下的读数必须真的在变（过程噪声），
    停机/待机下的读数通常是冻住的。判据是两组的"取值多样性"之比，
    与行业、量纲、工艺全都无关。
    """
    if not target_values or len(target_values) != len(vals):
        return None

    def _diversity(keys):
        xs = []
        for i in keys:
            v = _num(target_values[i])
            if v is not None:
                xs.append(v)
        if len(xs) < min_each:
            return None
        return len(set(xs)) * 1.0 / len(xs)

    dm = _diversity([i for i, v in enumerate(vals) if v == main])
    do = _diversity([i for i, v in enumerate(vals) if v != main])
    if dm is None or do is None or do <= 0:
        return None
    if dm < ratio_gap * do:
        return {"values": [],
                "reason": "占行数最多的取值「%s」对应的读数几乎不变（取值多样性 %.2f，"
                          "其余状态 %.2f）——它更像停机/冻结段而不是在产状态。"
                          "不好意思按占比推定，请用 --exclude-status-values "
                          "直接登记哪个才是非生产状态。" % (main, dm, do)}
    return None


def blocks(mask, seams=()):
    """把 mask（True=保留）切成连续块，返回 [(start, end)]（end 含）。

    所有需要相邻差分的统计量（移动极差、EWMA、CUSUM、滞后相关）都只能在块内算。

    seams = 已确认的真实性中断 indexes：第 i 行与第 i+1 行之间隔了一大截
    （中间没采到数）。两侧的行都是真实数据，都该留着画图，
    但它们**不是相邻观测**——跨接缝求差分会得到【两个不同时间断ospheres面的点之差】，
    这个差分既不代表测量误差也不代表过程波动，却在参与 σ_within 的估计。
    所以接缝必须断开 blocks，即便两边都保留。
    """
    seamset = set(seams)
    segs = []
    start = None
    for i, m in enumerate(mask):
        if m:
            # 接缝落在上一行之后：先结掉前面的块，从这一行起算新块
            if start is not None and (i - 1) in seamset:
                segs.append((start, i - 1))
                start = None
            if start is None:
                start = i
        else:
            if start is not None:
                segs.append((start, i - 1))
                start = None
    if start is not None:
        segs.append((start, len(mask) - 1))
    return [s for s in segs if s[1] - s[0] + 1 >= MIN_BLOCK]


def largest_block(segs):
    if not segs:
        return None
    return max(segs, key=lambda s: s[1] - s[0])


# ---------------------------------------------------------------- 主入口

def detect(times=None, status_values=None, status_excluded=None,
           target_values=None, recovery=0, enabled=True,
           gap_mult=GAP_MULT, freeze_run=FREEZE_RUN):
    """识别无效段，返回一行不多、一行不少的交代。

    参数（全部是"登记客观事实"，没有一个是要使用者替我们做统计判断）：
      times           —— 时间列解析结果（list[datetime|None]）；没有也能跑，只是少了断流规则
      status_values   —— 设备状态列的逐行取值
      status_excluded —— 使用者显式登记的非生产取值；给了就不推断
      target_values   —— 目标列的逐行值，用于冻结判定
      recovery        —— 工艺文件规定：每个连续段开头剔几行（开机/换卷过渡品）
      enabled         —— False 表示使用者明确说"这份数据里没有无效段"

    返回 dict：
      status   ok / disabled / no_time / aborted
      mask     list[bool]，True=保留
      segments [(a,b)] 连续保留块
      items    [{kind, start, end, reason, evidence}] 被剔的每一段
      n_total / n_excluded / n_kept
      warnings []
      notes    []  人话说明，直接进报告
    """
    n = None
    for cand in (times, status_values, target_values):
        if cand is not None:
            n = len(cand)
            break
    if n is None:
        return {"status": "aborted", "mask": [], "segments": [], "items": [],
                "n_total": 0, "n_excluded": 0, "n_kept": 0,
                "warnings": [], "notes": ["没有可用于识别无效段的列"], "aborted": True}
    if not enabled:
        return {"status": "disabled", "mask": [True] * n,
                "segments": [(0, n - 1)] if n >= MIN_BLOCK else [],
                "items": [], "n_total": n, "n_excluded": 0, "n_kept": n,
                "warnings": [], "notes": ["本次未做无效段排除（--no-exclude）"],
                "aborted": False}

    mask = [True] * n
    items = []
    notes = []
    warns = []

    # ---- 1. 状态列：客观事实优先 ----
    if status_values is not None:
        if status_excluded:
            bad = set(str(v).strip() for v in status_excluded)
            src = "使用者登记"
        else:
            inf = pick_status_values(status_values, target_values=target_values)
            bad = set(inf.get("values") or [])
            src = "自动推定"
            if inf.get("reason"):
                notes.append("设备状态列没能自动判定出非生产段：%s。若确有多个工况，"
                             "请用 --exclude-status-values 直接登记。" % inf["reason"])
            elif bad:
                notes.append("按设备状态列推定「%s」为在产状态，其余 %d 种取值"
                             "（%s）视为非生产段（依据：它们各自连续出现且占比低于在产值）。"
                             % (inf.get("assumed_running"), len(bad),
                                "、".join(sorted(bad)[:4])))
        if bad:
            run_s = None
            for i in range(n + 1):
                v = (str(status_values[i]).strip() if i < n and status_values[i] is not None
                     else None)
                hit = i < n and v in bad
                if hit and run_s is None:
                    run_s = i
                elif not hit and run_s is not None:
                    a, b = run_s, i - 1
                    for k in range(a, b + 1):
                        mask[k] = False
                    items.append({"kind": "status", "start": a, "end": b,
                                  "reason": "设备状态非在产（%s）"
                                            % (status_values[a] if a < n else ""),
                                  "evidence": "设备状态列显示「%s」（来源：%s）"
                                              % (str(status_values[a]) if a < n else "",
                                                 src)})
                    run_s = None

    # ---- 2. 断流：相邻间隔忽然放大 ----
    if times is not None:
        for i in _gap_indexes(times, gap_mult=gap_mult):
            # 接缝本身不剔行——它是一个事实（中间没数据），不是一个坏数据。
            # 但它必须是控制图的分段边界，否则会在接缝处造出假差分。
            if i + 1 < n:
                items.append({"kind": "gap", "start": i, "end": i + 1,
                              "reason": "相邻两点时间间隔远大于正常采样间隔",
                              "evidence": "第 %d→%d 行之间断流；此处作为控制图分段边界，"
                                          "不跨接缝计算差分" % (i + 1, i + 2),
                              "seam": True})

    # ---- 3. 冻结：设备停着不动 ----
    if target_values is not None:
        for a, b, v in _freeze_runs(target_values, min_run=freeze_run):
            if b < a:
                continue
            for k in range(a, b + 1):
                mask[k] = False
            items.append({"kind": "frozen", "start": a, "end": b,
                          "reason": "目标值连续 %d 行完全不变" % (b - a + 1),
                          "evidence": "冻结值 %s；保留段首第 %d 行作为最后一个真实读数"
                                      % (v, a)})

    # ---- 4. 工艺规定的过渡品（开机/换卷头几行）----
    # seams 里是接缝左侧的行号：第 i 行与第 i+1 行之间隔了一大截。
    # 每个连续生产块的起点因此是 0，或某个接缝的下一行 seam+1。
    seams = sorted(set(it["start"] for it in items if it.get("seam")))
    if recovery > 0:
        starts = [0] + [s + 1 for s in seams if s + 1 < n]
        for a0 in starts:
            b = min(n - 1, a0 + recovery - 1)
            hit = [k for k in range(a0, b + 1) if mask[k]]
            if not hit:
                continue
            for k in hit:
                mask[k] = False
            items.append({"kind": "recovery", "start": hit[0], "end": hit[-1],
                          "reason": "工艺规定：每个连续段开头 %d 行为过渡/调整品" % recovery,
                          "evidence": "共剔除 %d 行（按你的工艺文件登记）" % len(hit)})

    seam_items = [it for it in items if it.get("seam")]
    items = [it for it in items if not it.get("seam")]
    n_ex = n - sum(1 for m in mask if m)
    kept = n - n_ex
    segs = blocks(mask, seams)

    out = {
        "status": "ok", "mask": mask, "segments": segs, "items": items,
        "seams": seams,                    # 真实性中断位置：外部剔除行后要拿它重建块边界
        "n_seams": len(seam_items),
        "n_total": n, "n_excluded": n_ex, "n_kept": kept,
        "warnings": warns, "notes": notes, "aborted": False,
        "recovery": recovery,
        "has_time": times is not None,
    }

    # ---- 安全阀：规则显然识别错了就整体放弃 ----
    # 剔掉一半以上数据还说"这是停机"，那是把产品当成噪声删了。
    # 宁可不剔、让使用者自己捧着报告看，也不能悄悄替他删掉大半个数据集。
    if n_ex and (kept < MIN_KEPT_ROWS or kept * 1.0 / n < MIN_KEPT_RATIO):
        warns.append("结构性规则识别出 %d/%d 行疑似无效段，剔除后只剩 %d 行（%.0f%%），"
                     "低于安全线（保留 ≥ %d 行且 ≥ %.0f%%）—— 这通常是把正常判据套到了"
                     "采样本就稀疏的数据上。本次**未执行剔除**，请人工核对后再决定是否"
                     "用 --exclude-status-col 精确登记。"
                     % (n_ex, n, kept, kept * 100.0 / n,
                        MIN_KEPT_ROWS, MIN_KEPT_RATIO * 100))
        out.update({"aborted": True, "status": "aborted",
                    "mask": [True] * n, "segments": [(0, n - 1)] if n >= MIN_BLOCK else [],
                    "items": [], "n_excluded": 0, "n_kept": n,
                    "attempted": n_ex, "attempted_items": items})
    return out


# ---------------------------------------------------------------- 对照

def merge_items(items):
    """多条规则命中同一段时合并成一条。

    不合并的后果不是难看，是误读：一段 20 行的停机同时被"状态列"和"数值冻结"
    两条规则命中，表里就出现两行 20 与 19，读者会把它们加成 39 行——
    而实际只剔了 20 行。被完全包含的那条降级为"依据补强"，不单独计行数。
    """
    roots = []
    for it in sorted(items, key=lambda x: (x.get("start") or 0,
                                           -(x.get("end") or 0))):
        placed = False
        for root in roots:
            if (it.get("start") or 0) >= (root.get("start") or 0) \
                    and (it.get("end") or 0) <= (root.get("end") or 0):
                root.setdefault("_also", []).append(it)
                placed = True
                break
        if not placed:
            roots.append(dict(it))
    out = []
    for r in roots:
        also = r.pop("_also", [])
        if also:
            labs = sorted(set(LABELS.get(a.get("kind"), a.get("kind") or "")
                              for a in also + [r]))
            r["corroborated_by"] = [a.get("kind") for a in also]
            r["evidence"] = "%s；同一区间另有 %s 项判据同时命中（互为印证，不重复计数）" \
                            % (r.get("evidence") or r.get("reason") or "",
                               "、".join(labs[:-1]) if len(labs) > 1 else labs[0])
        out.append(r)
    return out


LABELS = {"status": "非生产状态", "frozen": "数值冻结",
          "recovery": "过渡/首件", "gap": "断流接缝"}


# scan:keep compare 是留给调用方的诊断工具（被剔行里 NG 占比），当前管线未挂接：
# 等 D6/D7 的"停机段贡献了多少不良"这一问需要时再接线，删了等于丢掉这份对照能力。
def compare(defect_counts, mask):
    """被剔的那些行里有多少判定的 NG —— 一行数字回答"剔了会不会影响结论"。

    如果停机段贡献了 60% 的不良数，那么 Pareto 榜首就是停机造成的，
    把它当作工艺问题的根因去改参数，等于给一个已经报废的时段做优化。
    """
    if not defect_counts or len(defect_counts) != len(mask):
        return None
    tot = sum(defect_counts)
    in_ex = sum(c for c, m in zip(defect_counts, mask) if not m)
    if tot <= 0:
        return {"total": 0, "in_excluded": in_ex, "ratio": None}
    return {"total": tot, "in_excluded": in_ex, "ratio": in_ex * 1.0 / tot}


def describe(d):
    """给报告用的人话。只陈述事实：剔了什么、剔了多少、剩下多少、按什么依据。"""
    if d.get("status") == "disabled":
        return list(d.get("notes") or [])
    lines = []
    if d.get("aborted") and d.get("attempted"):
        lines.append("识别出 %d 行疑似无效段，但剔除后样本不足，本次**未剔除**："
                     "硬剔会把一半以上有效数据带走，那样算出来的控制限毫无意义。"
                     % d["attempted"])
    if d.get("n_excluded"):
        by = {}
        for it in d["items"]:
            by[it["kind"]] = by.get(it["kind"], 0) + 1
        lines.append("共排除 %d/%d 行（%.0f%%）；剩余 %d 行落在 %d 个连续段内，"
                     "控制图以最长段为准。"
                     % (d["n_excluded"], d["n_total"],
                        d["n_excluded"] * 100.0 / max(1, d["n_total"]),
                        d["n_kept"], len(d.get("segments") or [])))
    else:
        lines.append("未识别到需要排除的无效段：相邻采样间隔无异常断流、"
                     "目标列无连续冻结段%s。"
                     % ("，设备状态列也未给出非在产段"
                        if d.get("has_time") is not False else ""))
    for n in (d.get("notes") or []):
        lines.append(n)
    return lines
