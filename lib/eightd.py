# -*- coding: utf-8 -*-
"""
8D 报告引擎（D1–D8 完整版）

=============================================================================
设计边界 —— 这一节比代码重要，改代码前先读
=============================================================================

8D 是**对客户/对内正式结案**的文件。AI 一旦把 D5–D8 写得像结论，
质量工程师没细看就发出去，客户收到的就是一份「AI 编的 8D」。
所以本模块只做三件事，且每件都有硬约束：

1. **D2 录入卡**：不是表单，是**交叉校验器**。
   工程师填的 5W2H 会与数据算出来的事实逐项比对，不一致就报冲突，
   要求人工裁定并写下差异原因。
   —— 直接采信填报数字等于把主观描述当事实基线，是「编数据」的另一种形态。

2. **D5–D8 只生成「待验证方案 / 动作清单 / 检查清单」，不生成结论。**
   - D5 给对策**方向与类别**，**绝不给具体参数设定值**（那是决策，且 DOE 边界外下单参数有风险）。
     具体数值只能来自第 10 节 DOE 的**待验证实验方案**。
   - D6 给**验证方案**（样本量、判定标准、周期、副作用观察项），不是验证结果。
   - D7 给**文件化动作清单**（根因类型 → 必改文件映射），这是确定性映射，最适合自动化。
   - D8 给**结案条件**，未全部满足不得结案。

3. **确审闸门**：D5–D8 每项带「已人工确审 ☐」。未确审时报告头部打红标，
   明确写「不得对外发出」。这是把用户说的「人工确审」从一句提醒做成**导出闸门**。

=============================================================================
零第三方依赖：stdlib only。
"""

import math
import os
import re

try:                       # 包模式
    from . import quality_guard as QG
except Exception:          # 脚本模式（python lib/pipeline.py）
    try:
        from lib import quality_guard as QG
    except Exception:
        import quality_guard as QG

# ---------------------------------------------------------------------------
# D2 字段定义
# ---------------------------------------------------------------------------
# computable=True 的字段，脚本会从数据里算一个「数据核算参考值」。
# **参考值不是事实**：数据可能只是本次导出的切片，现场掌握的信息可能更全。
# 所以填报与参考不一致时不判对错，只报冲突并要求写明差异原因。

D2_FIELDS = [
    {"key": "what",           "w": "What",     "label": "什么异常",        "required": True,
     "hint": "缺陷/异常的具体名称，不要写「品质不良」这类笼统词", "computable": True},
    {"key": "where_process",  "w": "Where",    "label": "发生工序",        "required": True,
     "hint": "工序名称，若与数据分析判定不一致请写明", "computable": True},
    {"key": "where_equip",    "w": "Where",    "label": "设备/通道/工位",  "required": True,
     "hint": "具体到机台号、通道号、柜号；写「全线」也要说明依据", "computable": True},
    {"key": "when_first",     "w": "When",     "label": "首次发现时间",    "required": True,
     "hint": "YYYY-MM-DD HH:MM；这是 8D 追溯起点，务必与变更点对齐", "computable": True},
    {"key": "when_window",    "w": "When",     "label": "异常时间段",      "required": False,
     "hint": "起止时间；断续发生请写各段", "computable": False},
    {"key": "who_found",      "w": "Who",      "label": "谁发现",          "required": True,
     "hint": "岗位 + 检验方式（自检/专检/巡检）", "computable": False},
    {"key": "why_problem",    "w": "Why",      "label": "为何是问题",      "required": True,
     "hint": "违反哪项规格（编号/限值）或对客户的实际影响", "computable": False},
    {"key": "how_found",      "w": "How",      "label": "如何发现",        "required": True,
     "hint": "SPC 报警 / 抽检 / 客户反馈 / 下工序反馈", "computable": False},
    {"key": "how_many_defects", "w": "How many", "label": "不良件数",      "required": True,
     "hint": "整数件数", "computable": True, "kind": "number"},
    {"key": "how_many_rate",  "w": "How many", "label": "不良率(%)",       "required": True,
     "hint": "百分比数值，不含 % 号", "computable": True, "kind": "number"},
    {"key": "how_many_batches", "w": "How many", "label": "涉及批次数",    "required": True,
     "hint": "整数批次数", "computable": True, "kind": "number"},
    {"key": "lot_ids",        "w": "How many", "label": "涉及批次号",      "required": False,
     "hint": "逗号分隔", "computable": False},
    {"key": "product_model",  "w": "What",     "label": "产品型号",        "required": True,
     "hint": "对外 8D 必填", "computable": False},
    {"key": "customer",       "w": "Who",      "label": "客户名称",        "required": False,
     "hint": "对外 8D 必填；内部结案可留空", "computable": False},
    {"key": "baseline_rate",  "w": "How many", "label": "历史基线不良率(%)", "required": False,
     "hint": "用于判定「异常」而非「常态」；没有基线就无法说清问题是否真实存在",
     "computable": False, "kind": "number"},
    {"key": "change_point",   "w": "When",     "label": "同期变更点",      "required": False,
     "hint": "异常发生前后 72h 内的 5M1E 变更（人/机/料/法/环/测）", "computable": False},
]

# 确审字段：D5–D8 每一节都要有人签字才算数
SIGNOFF_FIELDS = [
    ("signoff_d5", "D5 永久对策已由工艺/设备负责人确认"),
    ("signoff_d6", "D6 验证数据已由质量工程师核对原始记录"),
    ("signoff_d7", "D7 预防动作已确认文件编号并落地"),
    ("signoff_d8", "D8 结案已由质量经理批准"),
]


# ---------------------------------------------------------------------------
# 数据核算参考值
# ---------------------------------------------------------------------------

def _num(v):
    """宽松转数值，失败返回 None。用于解析人填的数字。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("%", "").replace(",", "")
    m = re.match(r"^([+-]?\d*\.?\d+)", s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _count_defects(res):
    """
    数据里能核算出的不良件数 / 率 / 批次数。

    **件数 = 出现该缺陷的「件」数，不是缺陷计数之和。**
    一个组件上有 3 处隐裂，那是 1 件不良而不是 3 件；用 total 求和会算出 200%+ 的不良率。
    """
    rows = (res.get("shape") or [0, 0])[0] or 0
    total = None
    ds = res.get("defects") or []
    cnt = [d for d in ds if d.get("is_count")]
    if cnt:
        # 优先取「本次分析目标/指定缺陷」那一列，其次取件数最多的
        want = res.get("target") or res.get("defect")
        pick = None
        if want:
            for d in cnt:
                if d.get("column") == want or d.get("defect") == want:
                    pick = d
                    break
        if pick is None:
            pick = max(cnt, key=lambda d: (d.get("n_positive") or d.get("total") or 0))
        # n_positive 才是件数；没有该字段（旧数据）时退回 total 并标记口径存疑
        total = pick.get("n_positive")
        if total is None:
            total = pick.get("total")
        rows = pick.get("n_records") or rows
    rate = (total / rows * 100.0) if (total is not None and rows) else None
    if rate is not None and rate > 100.0:
        # 算出来超过 100% 说明口径错了，宁可留空也不写一个假数字
        rate = None

    # 批次数：分组维度里挑一个最像批次号的，取其水平数
    batches = None
    for hs in (res.get("unit_hotspots") or []):
        dim = hs.get("dim") or ""
        if any(w in dim for w in ("批", "卷", "lot", "batch", "工单", "订单")):
            lv = hs.get("levels") or []
            if lv:
                batches = len(lv)
                break
    if batches is None:
        for gc in (res.get("group_compare") or []):
            dim = gc.get("dimension") or ""
            if any(w in dim for w in ("批", "卷", "lot", "batch")):
                g = gc.get("groups") or []
                if g:
                    batches = len(g)
                    break
    return {"defects": total, "rate": rate, "batches": batches, "rows": rows}


def computed_reference(res):
    """生成 D2 各字段的「数据核算参考值」。全部可能为 None —— 算不出就如实说算不出。"""
    c = _count_defects(res)

    ref = {
        "what": res.get("defect") or ((res.get("defects") or [{}])[0].get("defect")),
        "where_process": res.get("process_name"),
        "how_many_defects": c["defects"],
        "how_many_rate": (round(c["rate"], 3) if c["rate"] is not None else None),
        "how_many_batches": c["batches"],
    }

    # 设备/通道：优先取单元热点的最差水平，其次取第一个分组维度
    eq = None
    for hs in (res.get("unit_hotspots") or []):
        if hs.get("significant") and hs.get("worst"):
            eq = "%s = %s" % (hs.get("dim"), hs["worst"].get("level"))
            break
    if eq is None:
        for gc in (res.get("group_compare") or []):
            if gc.get("dimension"):
                eq = "按「%s」分层（详见报告第 9 节）" % gc["dimension"]
                break
    ref["where_equip"] = eq

    # 首次异常时间：取 SPC 判异最早的点；没有时间列就如实留空
    first = None
    for d in (res.get("spc") or []):
        vios = d.get("violations") or []
        if vios:
            idxs = []
            for v in vios:
                idxs.extend(v.get("indexes") or [])
            if idxs:
                first = ("「%s」的第 %d 个样本点出现判异（需换算到实际时间戳）"
                         % (d.get("column"), min(idxs) + 1))
                break
    ref["when_first"] = first
    return ref


# ---------------------------------------------------------------------------
# 录入卡：生成 / 解析 / 交叉校验
# ---------------------------------------------------------------------------

CARD_NAME = "8D_D2录入卡.md"


def emit_intake_card(res, outdir, filename=CARD_NAME):
    """写出 D2 录入卡。纯文本 `key: value`，零依赖、可版本化、可入审计留痕。"""
    ref = computed_reference(res)
    man = res.get("run_manifest") or {}
    ins = man.get("inputs") or []
    fp = ", ".join((i.get("sha256") or "")[:8] for i in ins if i.get("sha256")) or "—"

    L = []
    L.append("# 8D D2 录入卡（问题描述）")
    L.append("")
    L.append("> 用法：把本文件复制到本地，在 `key:` 后面填值，原样回传给 FDE 即可。")
    L.append("> `#` 开头的行是说明，不会被引擎读取。留空的必填项会在报告中记为缺口。")
    L.append(">")
    L.append("> **【数据核算参考】是脚本从本次数据算出来的，只作参考，不是事实。**")
    L.append("> 若与你掌握的情况不一致，**以你填的为准** —— 但必须在下方 `diff_note` 写明差异原因，")
    L.append("> 因为差异本身就是线索（数据只覆盖了某个切片 / 不良另有统计口径 / 存在未纳入的产线）。")
    L.append("")
    L.append("本次分析指纹：输入 %s ｜ 分析器 %s ｜ 生成于 %s"
             % (fp, res.get("analyzer_version") or "—", res.get("generated_at") or "—"))
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 一、5W2H 问题描述")
    L.append("")

    for f in D2_FIELDS:
        r = ref.get(f["key"])
        req = "**必填**" if f["required"] else "选填"
        L.append("# [%s] %s（%s｜%s）：%s" % (f["w"], f["label"], req, f["key"], f["hint"]))
        if r is not None:
            L.append("#   【数据核算参考】%s" % r)
        L.append("%s: " % f["key"])
        L.append("")

    L.append("## 二、差异说明")
    L.append("")
    L.append("# 若你填的值与【数据核算参考】不一致，在这里写原因。没有差异就留空。")
    L.append("diff_note: ")
    L.append("")
    L.append("## 三、D5–D8 人工确审（对外发出前必须完成）")
    L.append("")
    L.append("# 填 `yes` 才算确审。任何一项不是 yes，报告会打上「不得对外发出」标记。")
    for k, lab in SIGNOFF_FIELDS:
        L.append("%s: " % k)
    L.append("signoff_by: ")
    L.append("signoff_date: ")
    L.append("")
    L.append("---")
    L.append("")
    L.append("> 本卡只解决 D2。D5–D8 由 FDE 生成**待验证方案与动作清单**，")
    L.append("> 经验证并由你逐项确审后方可对外发出。**本 Skill 不下发任何参数到设备。**")

    p = os.path.join(outdir, filename)
    os.makedirs(outdir, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    return p


def parse_intake_card(path):
    """解析录入卡。`key: value` 行；`#` 开头忽略；`yes/true/是` 视为 True。"""
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#") or s.startswith(">"):
                continue
            if s.startswith("|"):            # 兼容表格写法 | key | value |
                cells = [c.strip() for c in s.strip("|").split("|")]
                if len(cells) >= 2 and re.match(r"^[a-z_][a-z0-9_]*$", cells[0]):
                    out[cells[0]] = cells[-1]
                continue
            if ":" not in s:
                continue
            k, v = s.split(":", 1)
            k = k.strip()
            v = v.strip().strip("|").strip()
            if not k or k.startswith("#"):
                continue
            low = v.lower()
            if low in ("yes", "true", "是", "y"):
                out[k] = True
            elif low in ("no", "false", "否", "n"):
                out[k] = False
            else:
                out[k] = v
    return out


def cross_check(card, res):
    """
    D2 填报值 vs 数据核算值 的交叉校验。

    返回 conflicts: [{field, label, reported, computed, gap, severity, question}]
    severity: high = 数量级/范围不一致，必须裁定；mid = 偏离超阈值；low = 仅提示。
    """
    ref = computed_reference(res)
    conflicts = []
    if not card:
        return conflicts

    def _cmp(key, rep_v, cmp_v, tol_rel=0.20, label=None):
        a, b = _num(rep_v), _num(cmp_v)
        if a is None or b is None:
            return
        if b == 0:
            return
        rel = abs(a - b) / abs(b)
        if rel <= tol_rel:
            return
        sev = "high" if rel >= 1.0 else ("mid" if rel >= 0.5 else "low")
        conflicts.append({
            "field": key,
            "label": label or next((f["label"] for f in D2_FIELDS if f["key"] == key), key),
            "reported": a, "computed": b, "rel": round(rel, 3), "severity": sev,
            "question": "填报值 %s 与数据核算值 %s 相差 %.0f%%。" % (
                _fmtnum(a), _fmtnum(b), rel * 100),
        })

    _cmp("how_many_defects", card.get("how_many_defects"), ref.get("how_many_defects"),
         label="不良件数")
    _cmp("how_many_rate", card.get("how_many_rate"), ref.get("how_many_rate"),
         label="不良率(%)")
    _cmp("how_many_batches", card.get("how_many_batches"), ref.get("how_many_batches"),
         label="涉及批次数")

    # 文本类：只做包含/方向性提示，不做对错判断
    wp = (card.get("where_process") or "").strip()
    if wp and ref.get("where_process") and wp.replace(" ", "") not in ref["where_process"].replace(" ", "") \
            and ref["where_process"].replace(" ", "") not in wp.replace(" ", ""):
        conflicts.append({
            "field": "where_process", "label": "发生工序",
            "reported": wp, "computed": ref["where_process"], "rel": None, "severity": "mid",
            "question": "填报工序「%s」与数据分析判定「%s」不一致。"
                        "若确认分析判定有误，请在报告中人工修正工序标签后再复跑。" % (wp, ref["where_process"]),
        })

    wq = (card.get("where_equip") or "").strip()
    if wq and ref.get("where_equip") and "=" in ref["where_equip"]:
        lv = str(ref["where_equip"].split("=", 1)[1]).strip()
        if lv and lv.replace(" ", "") not in wq.replace(" ", ""):
            conflicts.append({
                "field": "where_equip", "label": "设备/通道",
                "reported": wq, "computed": ref["where_equip"], "rel": None, "severity": "mid",
                "question": "数据指向的最差单元是「%s」，但你填的是「%s」。"
                            "若你填的更全（例如数据只覆盖了部分机台），保留你的并在 diff_note 说明。" % (ref["where_equip"], wq),
            })
    return conflicts


def _fmtnum(v):
    if v is None:
        return "—"
    if isinstance(v, float) and abs(v) < 1e-9:
        return "0"
    if isinstance(v, float):
        return ("%.4g" % v)
    return str(v)


def missing_required(card):
    """必填项缺口。"""
    miss = []
    for f in D2_FIELDS:
        if not f["required"]:
            continue
        v = (card or {}).get(f["key"])
        if v is None or (isinstance(v, str) and not v.strip()):
            miss.append({"key": f["key"], "label": f["label"], "w": f["w"]})
    return miss


def signoff_state(card):
    """D5–D8 确审状态。任一未确审 → 不得对外。"""
    card = card or {}
    st = {}
    for k, lab in SIGNOFF_FIELDS:
        st[k] = bool(card.get(k) is True)
    st["all_signed"] = all(st[k] for k, _ in SIGNOFF_FIELDS)
    st["by"] = card.get("signoff_by") or None
    st["date"] = card.get("signoff_date") or None
    st["labels"] = dict(SIGNOFF_FIELDS)
    return st


# ---------------------------------------------------------------------------
# D4 证据
# ---------------------------------------------------------------------------

def d4_status(res):
    """D4 能不能写根因。返回 (ok, text, lead)。"""
    root = res.get("rootcause") or {}
    cands = root.get("candidates") or []
    nv = res.get("negative_verdict") or {}
    if nv.get("verdict") == "no_actionable_cause":
        return False, (nv.get("text") or
                       "归一化后无可解释的工艺因子，D4 留空。"), None
    if not cands:
        return False, "未识别出候选根因（数据量或字段不足），D4 留空。", None
    if not QG.rc_strong(root):
        top = cands[0]
        return False, ("首要候选「%s」证据强度不足（得分 %.2f，低于闸门 %.2f）。"
                       "**D4 不得填写根因**，先完成现场验证或补数据后复跑。"
                       % (top.get("param_name") or "—", top.get("score") or 0,
                          getattr(QG, "EVIDENCE_GATE", 0.20))), top
    return True, "", cands[0]


# ---------------------------------------------------------------------------
# D5 永久对策候选（方向 + 类别，**不含参数设定值**）
# ---------------------------------------------------------------------------

# 根因归属 → 对策类别。这是确定性映射，不涉及任何参数数值。
PCA_BY_KIND = {
    "param": [
        ("参数窗口", "收窄该参数的受控窗口并写入 Control Plan；"
                     "**具体设定值只能来自 DOE 待验证方案，本 Skill 不下发**"),
        ("过程监控", "把该参数纳入 SPC 监控项，选定判异规则与抽样频次"),
    ],
    "equipment": [
        ("设备维护", "锁定该单元（机台/通道/柜号）做点检与保养，查易损件寿命与校准记录"),
        ("状态监控", "为该单元增加独立的状态采集点，避免与相邻单元共用一个传感器"),
    ],
    "material": [
        ("来料管控", "对该供应商/批次加严入厂检验项目与频次"),
        ("供应商协同", "向来料方发起 8D 或变更确认，锁定其变更点"),
    ],
    "method": [
        ("作业标准", "修订作业指导书，明确操作顺序与判定基准"),
        ("人员培训", "对相关岗位做专项培训并留培训记录编号"),
    ],
    "measurement": [
        ("测量系统", "先做 MSA/量具 R&R 确认测量能力；**测量系统不合格时，"
                     "所有相关性分析结论都不可用**"),
        ("检测方法", "复核检测方法与抽样方案是否能检出该类缺陷"),
    ],
    "position": [
        ("设备维护", "该位置对应的工装/压针/加热板/排版单元做点检与保养；"
                     "位置聚集通常指向**局部硬件状态**，而不是全局参数"),
        ("来料管控", "反查该位置所用电池片/硅片的供应商与批次，确认是否与来料批次重合"),
    ],
    "unknown": [
        ("验证优先", "根因归属未定，**先做验证实验再谈对策**；"
                     "在根因确认前写下任何永久对策都是猜测"),
    ],
}

_KIND_HINT = (
    ("设备", "equipment"), ("机台", "equipment"), ("通道", "equipment"), ("柜", "equipment"),
    ("通道号", "equipment"), ("模具", "equipment"), ("刀具", "equipment"),
    ("供应商", "material"), ("来料", "material"), ("浆料", "material"), ("批次", "material"),
    ("班次", "method"), ("班组", "method"), ("操作", "method"), ("作业", "method"),
    ("测量", "measurement"), ("检验", "measurement"), ("量具", "measurement"), ("msa", "measurement"),
)

# 位置类：组件 EL 的「电池列=7」、硅片的「第 3 行」这种坐标聚集。
# 它既不是参数也不是设备编号，但落在 5M1E 里最可能是**机（工装/加热板/压针的局部状态）**
# 或**料（该位置对应的那批电池片）**。归成 unknown 会让 D5/D7 掉进"什么都说不了"的分支，
# 反而丢信息；归成 position 则能给出两条可验证的方向。
_POSITION_WORDS = ("列", "行", "位", "区", "zone", "col", "row", "string", "位置")


def classify_root(lead, res):
    """把首要根因归到 5M1E 的一类。归不出来就返回 unknown —— 归不出来也是结论。"""
    if not lead:
        return "unknown"
    name = str(lead.get("param_name") or lead.get("param") or "")
    nn = re.sub(r"[\s]", "", name).lower()
    for kw, kind in _KIND_HINT:
        if kw in nn:
            return kind
    # 位置类要在 param 判定之前判： 「电池列」是坐标，不是工艺参数
    if any(w in nn for w in _POSITION_WORDS):
        return "position"
    # 参数类：名字能在本次 SPC 列里找到，就算参数
    for d in (res.get("spc") or []):
        if d.get("column") and str(d["column"]) in name:
            return "param"
    if lead.get("param_key"):
        return "param"
    # 单元热点（设备=xx）也算设备类
    if lead.get("dim") and "设备" in str(lead["dim"]):
        return "equipment"
    return "unknown"


def derive_d5(res, lead=None):
    """D5 候选：只给方向与类别，不给设定值。"""
    if lead is None:
        ok, _t, lead = d4_status(res)
        if not ok:
            lead = None
    kind = classify_root(lead, res)
    rows = []
    for cat, detail in PCA_BY_KIND.get(kind, PCA_BY_KIND["unknown"]):
        rows.append({
            "category": cat,
            "target": (lead.get("param_name") or "—") if lead else "（根因未确认）",
            "action": detail,
            "verify_by": "小批验证 / DOE（见报告第 10 节）",
            "approver": "工艺负责人 + 设备负责人 + 质量经理",
            "signed": False,
        })
    if not lead:
        rows.insert(0, {
            "category": "前置条件",
            "target": "—",
            "action": "**D4 未确认根因前，D5 不应产生任何永久对策。**"
                      "本节其余条目仅为根因确认后的方向预置，不是建议。",
            "verify_by": "—", "approver": "—", "signed": False,
        })
    return {"kind": kind, "rows": rows,
            "note": "本节全部条目为**候选方向**，不是措施；"
                    "具体参数设定值必须由 DOE 待验证方案给出并经审批。"}


# ---------------------------------------------------------------------------
# D6 验证方案
# ---------------------------------------------------------------------------

def derive_d6(res, lead=None):
    """
    D6 验证方案。给的是「怎么才算证明有效」，不是「已验证有效」。

    连续型指标用**最小可检出差异（MDD）**：按手上的样本量反推能检出的最小改善幅度。
    这个方向比"建议做 30 批"诚实 —— 样本量是产能决定的，不是我拍的。
    不良率用**三法则（rule of three）**：要宣称不良率 ≤ p 且 95% 可信，
    需连续观察 n ≥ 3/p 件无不良。
    """
    sigma = None
    tgt = res.get("target")
    for d in (res.get("spc") or []):
        if d.get("column") == tgt and d.get("sigma_within"):
            sigma = d["sigma_within"]
            break
    if sigma is None:
        for d in (res.get("spc") or []):
            if d.get("sigma_within"):
                sigma = d["sigma_within"]
                tgt = d.get("column")
                break

    rows = []
    if sigma:
        for n in (10, 30, 100):
            # 两样本 t，α=0.05 双侧、power=0.8 → Δ = σ·sqrt(2·(1.96+0.8416)²/n)
            mdd = sigma * math.sqrt(2 * (1.96 + 0.8416) ** 2 / n)
            rows.append({"n_per_group": n,
                         "mdd": round(mdd, 4),
                         "mdd_sigma": round(mdd / sigma, 3) if sigma else None})
    rate_ref = (_count_defects(res).get("rate") or 0) / 100.0
    # 三法则：宣称不良率降到 p1 且 95% 可信，需连续观察 n ≥ 3/p1 件无不良
    r3 = []
    if rate_ref > 0:
        for frac in (0.5, 0.2):
            p1 = rate_ref * frac
            if p1 > 0:
                r3.append({"target_rate_pct": round(p1 * 100, 4),
                           "n_needed_zero_defect": int(math.ceil(3.0 / p1))})

    return {
        "metric": tgt, "sigma_within": sigma,
        "mdd_table": rows,
        "rule_of_three": r3,
        "current_rate_pct": round(rate_ref * 100, 4) if rate_ref else None,
        "criteria": [
            "连续 N 批（N 由 MDD 表按你的产能选定）指标回到控制限内且无判异",
            "过程能力 Cpk 达到目标值，**且过程处于统计受控状态**（不受控时 Cpk 无意义）",
            "不良率验证满足三法则样本量（见上表）",
            "**副作用观察**：同期其他关键参数不得出现新的判异或 Cpk 下降（按下葫芦浮起瓢）",
        ],
        # 派生列（各型之和）不算副作用观察项：它由被观察项算出来，观察它等于观察自己
        "side_effects": [d.get("column") for d in (res.get("spc") or [])
                         if d.get("column") != tgt
                         and d.get("column") not in set(res.get("derived_cols") or [])][:6],
        "note": "本节是**验证方案**，不是验证结果。结果栏必须留空等现场填。",
    }


# ---------------------------------------------------------------------------
# D7 预防再发（根因类型 → 文件化动作，确定性映射）
# ---------------------------------------------------------------------------

D7_MAP = {
    "param": [
        ("更新 Control Plan", "把该参数的控制方法与频次写入受控文件"),
        ("纳入 SPC 监控项", "指定判异规则、抽样频次、超出后响应动作"),
        ("修订作业指导书（SOP）", "明确设定范围与调整权限"),
    ],
    "equipment": [
        ("设备保养项调整", "新增/加密该单元的保养项与周期"),
        ("点检表更新", "把该失效模式纳入开班点检"),
        ("备件与寿命管理", "易损件按寿命强制更换，不按故障后更换"),
    ],
    "material": [
        ("入厂检验标准更新", "新增该项目与 AQL"),
        ("供应商变更管理", "要求来料方变更提前通知并备案"),
        ("物料追溯粒度", "细化到批号/卷号，保证可反查"),
    ],
    "method": [
        ("作业指导书修订", "写清操作顺序、判定基准、异常上报路径"),
        ("人员培训与资格", "培训记录编号 + 上岗资格确认"),
        ("防错（Poka-Yoke）", "评估能否用物理防错替代人工判断"),
    ],
    "measurement": [
        ("MSA / 量具 R&R", "先证明测量系统能力合格"),
        ("检验方案复核", "复核抽样方案对该缺陷的检出能力"),
        ("留样与复检规则", "定义留样期限与争议时的复检路径"),
    ],
    "position": [
        ("设备保养项按位置细化", "把该位置纳入开班点检项，保养记录要能定位到具体位置"),
        ("来料批次追溯", "记录每个位置所用电池片/硅片的批次，保证位置聚集可反查到料"),
        ("增加位置维度的 SPC/EL 抽检", "按位置分层监控，避免整体均值掩盖局部聚集"),
    ],
    "unknown": [
        ("根因确认后再文件化", "**根因未确认就改文件，等于把猜测固化成标准**"),
        ("横向展开暂缓", "根因不明时横向展开会放大错误方向"),
    ],
}


def derive_d7(res, lead=None):
    kind = classify_root(lead, res)
    proc = res.get("process_name") or "—"
    rows = []
    for action, detail in D7_MAP.get(kind, D7_MAP["unknown"]):
        rows.append({"action": action, "detail": detail,
                     "doc_no": "（待填写文件编号）", "owner": "（待指定）",
                     "due": "（待定）", "signed": False})
    # 横向展开：只给范围，不给结论。范围由工序上下游关系推出，不是猜的。
    lateral = []
    try:
        import json
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "knowledge", "process_specs.json")
        with open(p, encoding="utf-8") as fh:
            specs = json.load(fh).get("processes", {})
        cur = specs.get(res.get("process")) or {}
        for k in (cur.get("downstream_keys") or [])[:3]:
            lateral.append(specs.get(k, {}).get("name") or k)
        for k in (cur.get("upstream_keys") or [])[:2]:
            lateral.append(specs.get(k, {}).get("name") or k)
    except Exception:
        pass
    return {"kind": kind, "rows": rows, "lateral_scope": lateral, "process": proc,
            "note": "每一项都必须落到**文件编号**，只写「已培训」「已更新」在客户审核时是退稿项。"}


# ---------------------------------------------------------------------------
# D8 结案条件
# ---------------------------------------------------------------------------

def derive_d8(res, lead=None):
    conf = (res.get("confidence") or {}).get("level")
    nv = bool((res.get("negative_verdict") or {}).get("verdict") == "no_actionable_cause")
    gates = [
        {"item": "D4 根因已通过现场验证（真因/伪因已判定）", "status": "待验证",
         "evidence": "对照报告第 8 节 quick_checks 的现场验证记录"},
        {"item": "D5 措施已实施且**参数变更有审批记录**", "status": "待执行",
         "evidence": "变更申请单编号"},
        {"item": "D6 验证数据满足判定标准（含连续批次数与 Cpk）", "status": "待验证",
         "evidence": "改善前后数据对照表"},
        {"item": "D7 全部预防动作已有文件编号并生效", "status": "待执行",
         "evidence": "文件编号清单"},
        {"item": "横向展开范围内已完成同类排查", "status": "待执行",
         "evidence": "排查记录"},
        {"item": "客户已反馈接受（对外 8D）", "status": "待确认",
         "evidence": "客户回执"},
    ]
    blockers = []
    if nv:
        blockers.append("本次分析为**负例结论**（无可行动工艺根因），"
                        "在补充 STC 归一与测试台标定之前不具备结案条件。")
    if conf in ("低", "较低", "low"):
        blockers.append("结论置信度为「%s」，P0 数据缺口未补齐前不得结案。" % conf)
    return {"gates": gates, "blockers": blockers,
            "note": "任一 gate 未满足即不得结案。本节是检查清单，不是结案声明。"}


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def render_8d(rep, res, card=None, conflicts=None, parent=None):
    """渲染完整 D1–D8。card 为已解析的录入卡。parent 为 §12 的 section 对象（闸门横幅挂它下面）。"""
    card = card or {}
    conflicts = conflicts or []
    st = signoff_state(card)
    miss = missing_required(card)
    root = res.get("rootcause") or {}
    ok4, why4, lead = d4_status(res)

    # ---- 闸门横幅 ----
    if not st["all_signed"]:
        rep.callout(
            parent if parent is not None else rep.section("8D 确审状态", level=3),
            "**本报告 D5–D8 未经人工确审，不得对外发出。** "
            "已确审项：%d/%d。未确审的项目在报告中标记为「待确审」，"
            "须由现场质量工程师逐项核对原始记录后签字。"
            % (sum(1 for k, _ in SIGNOFF_FIELDS if st[k]), len(SIGNOFF_FIELDS)),
            "warn")

    # ---- D1 ----
    s = rep.section("D1 成立团队", level=3)
    rep.kv(s, [("组长（工艺/质量）", "（待指定）"),
               ("工艺工程师", "参数分析与对策"),
               ("设备工程师", "设备状态与保养追溯"),
               ("生产班长", "现场执行与变更记录"),
               ("质量工程师", "检验数据与客户沟通")])
    rep.text(s, "团队为建议构成，实际成员由企业指定；本 Skill 不代替企业确定责任人。")

    # ---- D2 ----
    s = rep.section("D2 描述问题（5W2H）", level=3)
    _ref = computed_reference(res)
    if not card:
        rep.callout(s,
            "尚未收到 D2 录入卡。已随本次分析生成 `%s`，"
            "请填写后回传（CLI：`--d2-card <路径>`）。"
            "**在 D2 补齐前，本报告不具备对外发出条件。**" % CARD_NAME, "warn")
    rows = []
    for f in D2_FIELDS:
        v = card.get(f["key"])
        if v is True:
            v = "是"
        shown = v if (v not in (None, "")) else "　**待填**　"
        ref = _ref.get(f["key"])
        rows.append([f["w"], f["label"] + (" *" if f["required"] else ""),
                     str(shown), (str(ref) if ref is not None else "—")])
    rep.table(s, ["维度", "项目（*必填）", "现场填报", "数据核算参考"], rows,
              caption="5W2H 问题描述。数据核算参考由脚本从本次数据算出，"
                      "与填报不一致时以填报为准，但须写明差异原因。")
    if miss:
        rep.callout(s, "D2 仍有 %d 项必填未补齐：%s。"
                    "缺少 5W2H 的 8D 在客户审核时是直接退稿项。"
                    % (len(miss), "、".join(m["label"] for m in miss)), "warn")

    if conflicts:
        rep.text(s, "**填报与数据的交叉校验冲突（必须逐项裁定，不得忽略）：**")
        rep.table(s, ["严重度", "项目", "现场填报", "数据核算", "需回答的问题"],
                  [[{"high": "高", "mid": "中", "low": "低"}.get(c["severity"], c["severity"]),
                    c["label"], str(c["reported"]), str(c["computed"]), c["question"]]
                   for c in conflicts],
                  caption="冲突不代表谁错。数据可能只是某个切片，现场可能掌握更全的信息；"
                          "但**必须写下差异原因** —— 差异本身就是线索。")
    dn = card.get("diff_note")
    if dn:
        rep.kv(s, [("差异原因说明", dn)])
    elif conflicts:
        rep.callout(s, "存在冲突但未填写 `diff_note`，D2 不成立。", "warn")

    # ---- D3 ----
    s = rep.section("D3 临时遏制措施（ICA）", level=3)
    hs = res.get("unit_hotspots") or []
    scope = "按第 9 节分层结论锁定嫌疑单元" if hs else "按第 7/9 节 Pareto 与分层结果界定"
    rep.table(s, ["#", "措施", "责任人", "完成日期", "验证结果"],
              [["1", "隔离异常时段/批次产品（范围：%s）" % scope, "（待指定）", "（待定）", "（待填）"],
               ["2", "加严检验：该参数抽检频次提至 2 倍或 100%", "（待指定）", "（待定）", "（待填）"],
               ["3", "锁定嫌疑设备/通道/模具，暂停或降速运行", "（待指定）", "（待定）", "（待填）"],
               ["4", "客户端风险评估与沟通（对外 8D）", "（待指定）", "（待定）", "（待填）"]],
              caption="遏制措施为通用模板，**范围必须按本次分层结论收窄**；"
                      "「验证结果」栏必须有数据，只写「已执行」无效。")

    # ---- D4 ----
    s = rep.section("D4 根本原因", level=3)
    if ok4 and lead:
        rep.kv(s, [("最可能根因", "%s（综合得分 %.2f）" % (lead.get("param_name") or "—",
                                                    lead.get("score") or 0)),
                   ("机理", root.get("mechanism") or "—"),
                   ("归属（5M1E）", classify_root(lead, res))])
    else:
        rep.kv(s, [("D4 最可能根因", "【留空】%s" % why4)])
    # 5Why
    fw = root.get("five_why") or []
    if fw:
        rows = []
        for i, item in enumerate(fw[:5], 1):
            if isinstance(item, dict):
                q = item.get("question") or item.get("q") or "—"
                a = item.get("answer") or item.get("a") or "—"
                ev = item.get("evidence") or "（待补）"
            else:
                q, a, ev = "—", str(item), "（待补）"
            rows.append(["Why %d" % i, q, a, ev, "☐"])
        rep.table(s, ["层级", "提问", "回答", "证据", "已验证"], rows,
                  caption="5Why 每层都必须有**证据**，没有证据的那一层是推测，不是分析。")
    rep.text(s, "**流出原因（Escape）**：为什么该不良逃过了现有检测点？"
                "现有控制点 ______，检出能力 ______。（此项必须由质量工程师填写，"
                "数据里通常没有检测点配置）")

    # ---- D5 ----
    s = rep.section("D5 永久纠正措施（PCA）候选", level=3)
    d5 = derive_d5(res, lead)
    rep.table(s, ["类别", "作用点", "对策方向（**不含参数设定值**）", "验证方式", "审批", "已确审"],
              [[r["category"], r["target"], r["action"], r["verify_by"], r["approver"],
                "☑" if (r["signed"] or st.get("signoff_d5")) else "☐"]
               for r in d5["rows"]],
              caption=d5["note"])
    rep.callout(s, "**本 Skill 不下发任何参数到设备。** "
                "具体设定值只能来自第 10 节 DOE 的待验证实验方案，并经企业审批流程。", "warn")

    # ---- D6 ----
    s = rep.section("D6 实施与验证（方案）", level=3)
    d6 = derive_d6(res, lead)
    rep.kv(s, [("验证指标", d6["metric"] or "—"),
               ("组内标准差 σ", _fmtnum(d6["sigma_within"])),
               ("当前不良率", ("%.3f%%" % d6["current_rate_pct"]) if d6["current_rate_pct"] else "—")])
    if d6["mdd_table"]:
        rep.table(s, ["每组样本量 n", "最小可检出差异 Δ", "折合 σ 倍数"],
                  [[r["n_per_group"], r["mdd"], r["mdd_sigma"]] for r in d6["mdd_table"]],
                  caption="两样本 t 检验、α=0.05 双侧、power=0.80。"
                          "**按你的产能选 n**：n=10 只能检出 1.25σ 以上的改善，"
                          "小改善会被判为「无效果」而误杀有效对策。")
    if d6["rule_of_three"]:
        rep.table(s, ["目标不良率(%)", "需连续观察件数（零不良）"],
                  [[r["target_rate_pct"], r["n_needed_zero_defect"]] for r in d6["rule_of_three"]],
                  caption="三法则：要宣称不良率降到 p 且 95% 可信，需连续观察 n ≥ 3/p 件无不良。")
    rep.bullets(s, d6["criteria"], ordered=True)
    if d6["side_effects"]:
        rep.text(s, "**副作用观察项**：%s。改善一个参数把另一个参数搞坏，"
                    "是现场最常见的「假改善」。" % "、".join(d6["side_effects"]))
    rep.callout(s, "本节为**验证方案**。实施日期、验证批次、改善前后数据、结论"
                "均为待现场填写，脚本不代填。", "warn")

    # ---- D7 ----
    s = rep.section("D7 预防再发", level=3)
    d7 = derive_d7(res, lead)
    rep.table(s, ["预防动作", "具体要求", "文件编号", "责任人", "完成日期", "已确审"],
              [[r["action"], r["detail"], r["doc_no"], r["owner"], r["due"],
                "☑" if (r["signed"] or st.get("signoff_d7")) else "☐"]
               for r in d7["rows"]],
              caption=d7["note"])
    if d7["lateral_scope"]:
        rep.text(s, "**横向展开范围（由工序上下游关系推出，不是猜测）**：%s"
                 % "、".join(d7["lateral_scope"]))

    # ---- D8 ----
    s = rep.section("D8 结案检查清单", level=3)
    d8 = derive_d8(res, lead)
    rep.table(s, ["#", "结案条件", "状态", "证据要求"],
              [[i, g["item"], g["status"], g["evidence"]]
               for i, g in enumerate(d8["gates"], 1)],
              caption=d8["note"])
    if d8["blockers"]:
        for b in d8["blockers"]:
            rep.callout(s, b, "warn")
    rep.kv(s, [("结案批准人", st.get("by") or "（待签字）"),
               ("结案日期", st.get("date") or "（待填）")])

    return {"signoff": st, "missing_d2": miss, "conflicts": conflicts,
            "d5": d5, "d6": d6, "d7": d7, "d8": d8, "d4_ok": ok4}
