# -*- coding: utf-8 -*-
"""
fmea.py —— FMEA 风险排序引擎（RPN + 严重度闸门 + 逻辑一致性审计）

=============================================================================
为什么需要这一层
=============================================================================
    招聘 JD 里 FMEA 出现频率极高（宁德时代 PE/PTO、比亚迪 DQE、隆基过程质量、
    阳光电源封装工艺、中芯国际良率岗的"沉淀为 Checklist 或 FMEA"），
    但本项目此前只有 **PFMEA 空表单模板**，没有计算能力——填完 S/O/D 之后
    "谁排第一、哪些必须改、哪些填得自相矛盾"，全靠人肉。

    工具在这里能提供的价值是确定性的三件事：
      1. 算 RPN 并按**严重度优先**排序（RPN 排序本身有公认缺陷，见下）
      2. **严重度闸门**：S≥9 的风险不论 RPN 多低都必须处置
      3. **填报逻辑审计**：S 高却说没预防控制、D 低却说没探测手段、
         整表 S/O/D 复制粘贴——这些是评审时最常被挑、又最难人眼发现的

=============================================================================
口径与边界
=============================================================================
    · RPN = S×O×D，S/O/D 各取 1–10 整数（AIAG FMEA 手册经典 1–10 打分表口径）。
    · **RPN 阈值法（如 >100 必改）是实践惯例，不是标准强制条款。**
      本模块默认只排序不设阈值；调用方给 rpn_threshold 才按阈值分组。
      原因：RPN 是三个序数量表的乘积，100 分可以由 10×10×1 或 5×5×4 得到，
      两者风险性质完全不同，用同一个阈值一刀切会掩盖前者。
    · **不实现 AIAG-VDA 的 AP（Action Priority）H/M/L 表。** 那张表是逐格给定的
      三维查表，未取得手册原表就凭记忆复现＝编造标准。此处只用两条公开且
      不依赖记忆的原则：S 优先于 RPN；S=9/10 一律进必改清单。
    · **工具不代填 S/O/D。** 严重度取决于后果（安全/法规/功能），频度取决于
      历史数据，探测度取决于控制手段的有效性——三者都需要现场依据。
      seed_from_defects 只给骨架，数值一律留空，并在 note 里点名"待人工评分"。

零第三方依赖：stdlib only。
"""

import math as _math                 # 用于 isnan/isinf 校验打分格

try:
    from . import status as ST          # 包模式
except Exception:                       # 脚本模式
    import status as ST                 # noqa

SOD_MIN, SOD_MAX = 1, 10

# 严重度闸门：S≥9 视为安全/法规/重大功能丧失级，不论 RPN 高低都必须处置。
# 这条不是本项目发明——AIAG 与 VDA 的公开立场一致：严重度不可由频度/探测度抵消。
SEVERITY_GATE = 9

# 常用实践阈值（仅当调用方显式传入才生效，不作为默认判定）
DEFAULT_RPN_THRESHOLD = None

# 列名别名（现场叫法差异极大，这里只做归一化匹配，不做模糊猜测）
_ALIAS = {
    "s": ("严重度", "严重性", "severity", "s", "严重度s"),
    "o": ("频度", "频次", "发生度", "发生率", "occurrence", "o"),
    "d": ("探测度", "检出度", "检测度", "detection", "d"),
    "step": ("工序", "过程", "工序名称", "process", "process_step", "工步"),
    "mode": ("失效模式", "潜在失效模式", "模式", "failure_mode", "fm"),
    "effect": ("后果", "失效后果", "影响", "effect", "impact"),
    "prevent": ("预防控制", "预防措施", "现行预防控制", "prevention",
                "prevention_control"),
    "detect": ("探测控制", "检测控制", "现行探测控制", "detection_control",
               "detect_control"),
    "param": ("参数", "相关参数", "工艺参数", "param", "parameter"),
}


def _norm(s):
    """归一化列名/键名：去空白、去括号单位、小写。"""
    if s is None:
        return ""
    t = str(s).strip().lower()
    for ch in (" ", "\t", "_", "-", "（", "）", "(", ")", "：", ":", "/", "％", "%"):
        t = t.replace(ch, "")
    return t


_NORM_ALIAS = {}
for _k, _vs in _ALIAS.items():
    for _v in _vs:
        _NORM_ALIAS[_norm(_v)] = _k


def _pick(row, field):
    """从一行 dict 里按别名取字段值（取不到返回 None）。"""
    if field in row:
        return row[field]
    for k, v in row.items():
        if _NORM_ALIAS.get(_norm(k)) == field:
            return v
    return None


def _to_sod(v):
    """把单元格转成 1–10 整数；非法返回 (None, 原因)。"""
    if v is None:
        return None, "空"
    s = str(v).strip()
    if not s:
        return None, "空"
    try:
        f = float(s.replace("分", "").strip())
    except (TypeError, ValueError):
        return None, "非数值（%r）" % s[:12]
    if _math.isnan(f) or _math.isinf(f):
        return None, "非有限值"
    if abs(f - round(f)) > 1e-9:
        return None, "非整数（%s）——1–10 打分表为整数档" % s[:12]
    i = int(round(f))
    if not (SOD_MIN <= i <= SOD_MAX):
        return None, "超范围（%d，应 %d–%d）" % (i, SOD_MIN, SOD_MAX)
    return i, None


def rpn(s, o, d):
    """RPN = S×O×D。三值任一非法则返回 usable=False（不返回 0 冒充"低风险"）。

    返回 0 是最危险的降级方式：它会让人以为"这条风险很低"，
    而实际是"这条没填对"。宁可 usable=False 让下游报"缺分"。
    """
    vals, errs = {}, {}
    for name, v in (("S", s), ("O", o), ("D", d)):
        iv, why = _to_sod(v)
        vals[name] = iv
        if iv is None:
            errs[name] = why
    if errs:
        return {"usable": False, "rpn": None, "S": vals["S"], "O": vals["O"],
                "D": vals["D"], "errors": errs}
    r = vals["S"] * vals["O"] * vals["D"]
    return {"usable": True, "rpn": r, "S": vals["S"], "O": vals["O"],
            "D": vals["D"], "severity_gate": vals["S"] >= SEVERITY_GATE,
            "errors": {}}


def evaluate(rows, top_n=10, rpn_threshold=DEFAULT_RPN_THRESHOLD):
    """评估一张 FMEA 表（list[dict]），返回风险排序与填报审计。

    参数
        rows          : 每行一个失效模式；键名可用中文或英文别名
        top_n         : 输出前 N 条（默认 10）
        rpn_threshold : 给了才按阈值分组；None 只排序（见模块头口径）

    返回 dict：status / rows（逐行结果）/ top / aggregates / warnings / audit
    """
    out = {"status": ST.INSUFFICIENT_DATA, "n_rows": 0,
           "rows": [], "top": [], "aggregates": {}, "warnings": [],
           "audit": {"missing_score": [], "range_errors": [], "logic_flags": [],
                     "duplicate_pattern": None},
           "rpn_threshold": rpn_threshold,
           "severity_gate": SEVERITY_GATE,
           "note": "S/O/D 由现场依据评分，本工具只做排序与一致性审计，不代填、"
                   "不替代评审。RPN 阈值法为实践惯例，非标准强制。"}
    if not rows:
        out["warnings"].append("没有可评估的 FMEA 行（表为空或列名一个都没匹配上）")
        return out

    evaluated = []
    for idx, r in enumerate(rows, start=1):
        if not isinstance(r, dict):
            continue
        mode = _pick(r, "mode") or ""
        step = _pick(r, "step") or ""
        effect = _pick(r, "effect") or ""
        prevent = _pick(r, "prevent") or ""
        detect = _pick(r, "detect") or ""
        param = _pick(r, "param") or ""
        calc = rpn(_pick(r, "s"), _pick(r, "o"), _pick(r, "d"))

        item = {"row": idx, "step": str(step).strip(), "mode": str(mode).strip(),
                "effect": str(effect).strip(), "param": str(param).strip(),
                "prevent": str(prevent).strip(), "detect": str(detect).strip(),
                "S": calc["S"], "O": calc["O"], "D": calc["D"],
                "rpn": calc["rpn"], "usable": calc["usable"],
                "severity_gate": bool(calc.get("severity_gate"))}
        evaluated.append(item)

        if not calc["usable"]:
            out["audit"]["missing_score"].append(
                {"row": idx, "mode": item["mode"],
                 "errors": calc["errors"]})
            for why in calc["errors"].values():
                if "超范围" in why or "非整数" in why or "非数值" in why:
                    out["audit"]["range_errors"].append(
                        {"row": idx, "mode": item["mode"], "why": why})
                    break
            continue

        # 填报逻辑一致性（评审高频质疑点）
        flags = []
        if item["S"] >= SEVERITY_GATE and not item["prevent"]:
            flags.append("S=%d（≥%d，安全/法规级）却没有填预防控制——"
                         "严重度不能靠'没写控制'来成立" % (item["S"], SEVERITY_GATE))
        if item["D"] <= 3 and not item["detect"]:
            flags.append("D=%d 声称高检出能力，但没填探测控制（靠什么检出来？）"
                         % item["D"])
        if item["S"] >= 7 and not item["effect"]:
            flags.append("S=%d 但'后果'为空——严重度缺依据，评审会被追问"
                         % item["S"])
        if flags:
            out["audit"]["logic_flags"].append(
                {"row": idx, "mode": item["mode"], "flags": flags})

    out["n_rows"] = len(evaluated)
    usable = [x for x in evaluated if x["usable"]]
    out["rows"] = evaluated

    # 整表 S/O/D 复制粘贴检测（三列取值组合去重后只剩 1 种且行数≥3）
    combos = set((x["S"], x["O"], x["D"]) for x in usable)
    if len(usable) >= 3 and len(combos) == 1:
        only = usable[0]
        out["audit"]["duplicate_pattern"] = (
            "全部 %d 行的 S/O/D 都是 %s/%s/%s：几乎可以确定是模板复制未逐条评分。"
            "FMEA 的价值全在逐条差异上，这种表评审必被打回"
            % (len(usable), only["S"], only["O"], only["D"]))

    if not usable:
        out["status"] = ST.INSUFFICIENT_DATA
        out["warnings"].append("没有一行能算出 RPN（S/O/D 全部缺分或非法）")
        return out

    # 排序：严重度闸门优先 → RPN 降序 → 工序/模式名（保证可复现）
    usable.sort(key=lambda x: (0 if x["severity_gate"] else 1,
                               -(x["rpn"] or 0),
                               x["step"], x["mode"], x["row"]))
    out["top"] = usable[:max(1, int(top_n))]

    # 聚合：按工序、按失效模式
    by_step, by_mode = {}, {}
    for x in usable:
        st = x["step"] or "（未填工序）"
        by_step.setdefault(st, {"n": 0, "gate": 0, "max_rpn": 0})
        by_step[st]["n"] += 1
        by_step[st]["gate"] += 1 if x["severity_gate"] else 0
        by_step[st]["max_rpn"] = max(by_step[st]["max_rpn"], x["rpn"])
        mk = x["mode"] or "（未填模式）"
        by_mode.setdefault(mk, {"n": 0, "max_rpn": 0})
        by_mode[mk]["n"] += 1
        by_mode[mk]["max_rpn"] = max(by_mode[mk]["max_rpn"], x["rpn"])
    out["aggregates"] = {"by_step": by_step, "by_mode": by_mode}

    if rpn_threshold is not None:
        try:
            th = float(rpn_threshold)
        except (TypeError, ValueError):
            th = None
        if th is not None:
            over = [x for x in usable if x["rpn"] > th]
            out["over_threshold"] = {"threshold": th, "n": len(over),
                                     "rows": [x["row"] for x in over]}

    n_gate = sum(1 for x in usable if x["severity_gate"])
    if n_gate:
        out["warnings"].append(
            "%d 条 S≥%d：不论 RPN 高低都必须处置（严重度不可被频度/探测度抵消）"
            % (n_gate, SEVERITY_GATE))
    if out["audit"]["missing_score"]:
        out["warnings"].append(
            "%d 行 S/O/D 有缺分或非法，未参与排序——缺分不等于低风险，"
            "须补齐后重算" % len(out["audit"]["missing_score"]))
    if out["audit"]["duplicate_pattern"]:
        out["warnings"].append(out["audit"]["duplicate_pattern"])

    out["status"] = ST.SUCCESS if len(usable) == len(evaluated) else ST.PARTIAL_SUCCESS
    return out


def seed_from_defects(defects):
    """把 FDE 识别到的缺陷转成 FMEA 骨架行（S/O/D 留空，供人工评分）。

    为什么留空：严重度取决于失效后果（安全/法规/客户可感知度），频度取决于
    历史发生率，探测度取决于现场控制手段——三者都不是数据能替我算出来的。
    工具只负责把"要评哪几条"整理好，避免漏项。
    """
    rows = []
    for d in (defects or []):
        if not isinstance(d, dict):
            continue
        rows.append({
            "工序": d.get("process_name") or d.get("process") or "",
            "失效模式": d.get("defect") or "",
            "参数": d.get("column") or d.get("param") or "",
            "后果": "",
            "严重度": "",
            "频度": "",
            "探测度": "",
            "预防控制": "",
            "探测控制": "",
        })
    return rows


def _selftest():
    """已知答案自检：排序、闸门、审计都必须按设计逐条翻。"""
    ok = []

    r = rpn(9, 3, 2)
    ok.append(("RPN = S*O*D", r["rpn"] == 54, str(r["rpn"])))
    ok.append(("S≥9 触发严重度闸门", r["severity_gate"] is True, ""))

    bad = rpn(11, 2, 3)
    ok.append(("S 超范围时不可用且不给 0",
               bad["usable"] is False and bad["rpn"] is None, str(bad["errors"])))
    bad2 = rpn(5, 2.5, 3)
    ok.append(("非整数打分被拒", bad2["usable"] is False, str(bad2["errors"])))

    # 关键场景：S=10 但 RPN 只有 10，必须排在 S=5/O=6/D=6（RPN=180）之前
    rows = [
        {"工序": "化成", "失效模式": "安全阀失效", "后果": "起火",
         "严重度": 10, "频度": 1, "探测度": 1, "预防控制": "双阀冗余",
         "探测控制": "压力巡检"},
        {"工序": "涂布", "失效模式": "面密度超差", "后果": "容量偏低",
         "严重度": 5, "频度": 6, "探测度": 6, "预防控制": "闭环控制",
         "探测控制": "在线测厚"},
        {"工序": "涂布", "失效模式": "划伤", "后果": "外观不良",
         "严重度": 3, "频度": 4, "探测度": 4, "预防控制": "张力控制",
         "探测控制": "目检"},
    ]
    out = evaluate(rows)
    ok.append(("S=10 低 RPN 排在 S=5 高 RPN 之前",
               out["top"][0]["mode"] == "安全阀失效",
               "top=%s" % out["top"][0]["mode"]))
    ok.append(("排序可复现（同输入同序）",
               [x["mode"] for x in evaluate(rows)["top"]]
               == [x["mode"] for x in out["top"]], ""))
    ok.append(("全部行可用时 status=SUCCESS", out["status"] == ST.SUCCESS,
               out["status"]))

    # 逻辑审计：S=9 没预防控制 / D=2 没探测控制 / S=8 无后果
    audit_rows = [
        {"工序": "注液", "失效模式": "漏液", "后果": "", "严重度": 9,
         "频度": 3, "探测度": 2, "预防控制": "", "探测控制": ""},
    ]
    a = evaluate(audit_rows)
    flags = " ".join(" ".join(f["flags"]) for f in a["audit"]["logic_flags"])
    ok.append(("S=9 无预防控制被抓", "没有填预防控制" in flags, ""))
    ok.append(("D=2 无探测控制被抓", "探测控制" in flags, ""))
    ok.append(("S=8 无后果被抓", "后果'为空" in flags or "后果" in flags, ""))

    # 缺分：不能被算成低风险
    miss = evaluate([{"工序": "x", "失效模式": "y", "严重度": "", "频度": 3, "探测度": 4}])
    ok.append(("缺分不参与排序且报缺分",
               miss["status"] == ST.INSUFFICIENT_DATA
               and miss["audit"]["missing_score"], miss["status"]))
    ok.append(("缺分时不返回 top 列表", miss["top"] == [], str(miss["top"])))

    # 复制粘贴检测
    same = evaluate([{"工序": "a", "失效模式": "m%d" % i, "严重度": 7,
                      "频度": 5, "探测度": 5} for i in range(4)])
    ok.append(("整表 S/O/D 复制粘贴被识别",
               bool(same["audit"]["duplicate_pattern"]), ""))

    # 阈值分组
    thr = evaluate(rows, rpn_threshold=100)
    ok.append(("显式阈值才分组", thr.get("over_threshold", {}).get("n") == 1,
               str(thr.get("over_threshold"))))
    ok.append(("默认不给阈值", evaluate(rows).get("over_threshold") is None, ""))

    # 空表
    e = evaluate([])
    ok.append(("空表报数据不足", e["status"] == ST.INSUFFICIENT_DATA and e["warnings"], ""))

    # 骨架：不允许代填分数
    seeds = seed_from_defects([{"defect": "隐裂", "process_name": "层压",
                                "column": "EL不良率"}])
    ok.append(("骨架行 S/O/D 留空",
               seeds and seeds[0]["严重度"] == "" and seeds[0]["频度"] == "", ""))
    ok.append(("骨架行带出缺陷与工序",
               seeds[0]["失效模式"] == "隐裂" and seeds[0]["工序"] == "层压", ""))

    # 英文列名别名
    en = evaluate([{"process": "coating", "failure_mode": "scratch",
                    "severity": 6, "occurrence": 4, "detection": 3,
                    "prevention": "张力控制", "detection_control": "目检"}])
    ok.append(("英文列名可识别", en["n_rows"] == 1 and en["rows"][0]["rpn"] == 72,
               str(en["rows"][0]["rpn"])))
    return ok
