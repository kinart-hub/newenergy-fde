# -*- coding: utf-8 -*-
"""
views.py —— 角色视角层（v2.4.0）

**这里只做筛选与措辞，不做任何重新计算。**

引擎原本只有一份「大而全」的报告，五类使用者拿到的都是同一份东西，
各自要的答案埋在不同位置：工艺工程师想找"调哪个参数"，
现场想找"去哪看、按什么顺序"，生产主管想找"这批货怎么办"，
质量想找"能不能发给客户"。同一份报告对谁都不够直接。

视角层从同一个 result 里取数，按角色重排与改措辞——
计算层仍然唯一（一份 result.json），不存在五套算法各自漂移的风险。
这是刻意的设计约束（架构文档 R3：视角膨胀失控的缓解措施）：
新增视角只加一个函数，不动任何统计代码。

视角清单：
  process    工艺工程师  调哪个参数、区间多少、怎么验证
  shopfloor  现场技术    去现场看什么、按什么顺序（可打印 A4 作业单）
  quality    质量管理    能不能发给客户：证据链与 8D 完整性
  production 生产主管    这批货放不放行（处置建议表，带责任约束）
  mgmt       客户/管理层 单页摘要，对外口径

产物文件（均在 outdir 下）：
  视角_工艺参数建议.md / .html
  视角_现场作业单.md / .html
  视角_交付合规检查.md / .html
  视角_批次处置建议.md / .html
  视角_客户单页摘要.md / .html
  术语速查附录.md / .html
  处置判断记录.md        （R1：留痕，谁在什么依据下建议了什么）
"""

import datetime
import io
import os

try:                                    # 包模式
    from . import degrade
except Exception:                       # 脚本模式无包上下文
    import degrade                      # noqa
try:
    from .report import Report, save_report
except Exception:
    from report import Report, save_report

VIEW_KEYS = ("process", "shopfloor", "quality", "production", "mgmt")

VIEW_LABEL = {
    "process": "工艺工程师",
    "shopfloor": "现场技术人员",
    "quality": "质量管理人员",
    "production": "生产主管",
    "mgmt": "客户/管理层",
}

BASENAME = {
    "process": "视角_工艺参数建议",
    "shopfloor": "视角_现场作业单",
    "quality": "视角_交付合规检查",
    "production": "视角_批次处置建议",
    "mgmt": "视角_客户单页摘要",
}

# 术语速查：同一概念在不同角色嘴里叫法不同（架构文档 R4）。
# 单一事实源放在这里，报告附录直接读，不在各视角里各写一份。
GLOSSARY = [
    ("CPK", "过程能力指数", "衡量参数落进规格区间的能力。>1.33 通常算够；<1.0 说明能力不足。"),
    ("SPC", "统计过程控制", "用控制图看过程是否稳定，不稳时谈能力没有意义。"),
    ("Nelson 规则", "控制图判异规则", "8 条，判断控制图上的点是不是异常模式。开太多会误报泛滥。"),
    ("MAD", "绝对中位差", "一种比标准差更抗离群点的离散度算法，用来判离群。"),
    ("BH-FDR", "多重比较校正", "同时检验很多参数时，控制「假阳性」比例的方法。"),
    ("VIF", "方差膨胀因子", "衡量参数之间是否互相纠缠。>5 说明这两个参数分不开谁影响谁。"),
    ("DOE", "试验设计", "用最少的试验次数找出参数怎么配最好。"),
    ("8D", "八步问题解决法", "客户投诉的标准处理流程，D0–D8。"),
    ("ERA", "紧急反应措施", "8D 的 D0，先把客户保护起来，24 小时内要展开。"),
    ("PFMEA", "过程失效模式分析", "把失效模式、后果、探测度列成表并打分，8D 结束后必须更新。"),
    ("确审", "人工确认审核", "D2 录入卡填回后由责任工程师签字确认，未经确审的结论带红标。"),
    ("红标", "未确审标记", "结论未经人工确认，对外交付前必须处理掉。"),
    ("Pareto", "帕累托分析", "按贡献排序找出「关键的少数」，通常看前 20% 的项目。"),
    ("三现主义", "现场/现物/现实", "到现场看实物、用现实数据判断，不靠推测。"),
]


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _meta(result, view):
    return {
        "视角": "%s（%s）" % (VIEW_LABEL.get(view, view), view),
        "工序": result.get("process_name") or result.get("process") or "—",
        "目标列": result.get("target") or "—",
        "置信度": (result.get("confidence") or {}).get("level") or "—",
        "生成时间": _now(),
        "引擎版本": result.get("analyzer_version") or "—",
    }


def _cand_name(c):
    return c.get("param") or c.get("param_name") or c.get("param_key") or "—"


def _cand_evidence(c):
    """候选根因的数据依据。取不到就返回空——空依据的条目不得进入处置建议。"""
    d = c.get("detail") or {}
    for key in ("note", "reason"):
        if d.get(key):
            return str(d[key])
    if c.get("note"):
        return str(c["note"])
    return ""


def _cand_stat(c):
    """把候选的统计证据压成一句话，供工艺/处置视角展示。"""
    d = c.get("detail") or {}
    bits = []
    if d.get("r") is not None:
        bits.append("r=%.3f" % d["r"])
    if d.get("p") is not None:
        try:
            bits.append("p=%.2g" % float(d["p"]))
        except Exception as _dg_e:
            # 展示层也不许无痕吞异常：格式化失败说明 p 的类型不对，
            # 那是上游数据结构变了，静默略过会让这条依据看起来"没有 p 值"
            degrade.note("views._cand_stat", _dg_e)
    if d.get("n") is not None:
        bits.append("n=%s" % d["n"])
    # evidence_score=0 表示这条候选没有任何证据支撑。
    # 不能因为它"是个数字"就算有依据——实测因此让一个纯猜测的条目
    # 混进了处置建议表，而那张表是放行依据。
    es = c.get("evidence_score")
    if es not in (None, 0, 0.0):
        bits.append("证据分=%.2f" % float(es))
    return "，".join(bits) or "—"


def _hotspots(result):
    """分组热点：哪个设备/班次/批次的水平明显偏离。

    注意这是「分组维度上的集中」，不是物理坐标上的聚集——
    现场按这个去找"哪台机台、哪个班次"，而不是去找"第几列电池片"。
    """
    out = []
    for h in (result.get("unit_hotspots") or []):
        dim = h.get("dim") or "—"
        for r in (h.get("rows") or [])[:3]:
            if r.get("p_adj") is not None and float(r.get("p_adj") or 1) > 0.05:
                continue          # 校正后不显著的，不值得让现场跑一趟
            out.append({
                "dim": dim,
                "level": r.get("level") or "—",
                "mean": r.get("mean"),
                "others_mean": r.get("others_mean"),
                "p_adj": r.get("p_adj"),
            })
    return out


def _red(result):
    """是否处于红标状态：结论未经人工确审，或置信度偏低。

    红标不是装饰——它决定这份东西能不能对外。带红标的处置建议
    如果被当成正式判定下发，出了质量事故就是责任事故。
    """
    conf = (result.get("confidence") or {}).get("level") or ""
    e8 = result.get("eightd") or {}
    signed = ((e8.get("signoff") or {}).get("all_signed"))
    if conf in ("低", "偏低"):
        return True, "结论置信度偏低（%s），未经确审不得对外" % conf
    if signed is False:
        return True, "8D 签署栏未全部签署，结论处于未确审状态"
    if signed is None:
        return True, "8D 未生成，结论未经确审流程"
    return False, ""


# ------------------------------------------------------------------ 1 工艺
def view_process(result):
    """工艺工程师：调哪个参数、区间多少、怎么验证。"""
    rep = Report("工艺参数建议（工艺工程师视角）", _meta(result, "process"))
    rc = result.get("rootcause") or {}
    # 诚实降级时不得用 all_candidates 兜底：那条路径会把"没查到"重新渲染成
    # 一份看起来正常的候选清单（与外部评审 E07 同源：状态说没证据，结论照给）。
    cands = ([] if rc.get("honest_degraded")
             else (rc.get("candidates") or rc.get("all_candidates") or []))
    lead = rc.get("lead") or (cands[0] if cands else None)

    s = rep.section("1. 结论：优先动哪个参数")
    if lead:
        rep.kv(s, [("参数", _cand_name(lead)),
                    ("统计证据", _cand_stat(lead)),
                    ("机理", rc.get("mechanism") or lead.get("note") or "—")])
    else:
        rep.text(s, "本次未定位到可行动的候选根因。"
                    "不要为了凑一个结论硬调参数——先补数据缺口。")
    gaps = result.get("gaps") or []
    if gaps:
        rep.callout(s, "数据缺口 %d 项，会限制结论可靠性：%s"
                    % (len(gaps), "；".join(str(g.get("item") or g)[:40]
                                            for g in gaps[:3])), "warn")

    s = rep.section("2. 参数调整建议")
    rows = []
    for c in cands[:8]:
        rows.append([
            _cand_name(c),
            _cand_stat(c),
            # 建议区间不在这里编——知识库备注里的典型区间是行业值，
            # 不是这家企业的窗口；写出来极易被当成下发的设定值。
            "见下方「怎么验证」，须现场试验确定",
            _cand_evidence(c)[:70] or "—",
        ])
    if rows:
        rep.table(s, ["参数", "统计证据", "调整建议", "依据/备注"], rows)
    else:
        rep.text(s, "（无可给出的参数建议）")
    # R2：建议区间被当成设定值直接下发，是最常见的误用
    rep.callout(s, "「建议验证区间」是**试验范围**，不是可直接下发的设定值。"
                   "设定值必须在现场验证后确定——区间中点不等于最优值。", "warn")

    s = rep.section("3. 怎么验证")
    qc = rc.get("quick_checks") or []
    if qc:
        rep.bullets(s, [str(q if isinstance(q, str) else
                            (q.get("item") or q.get("action") or q))
                        for q in qc[:8]], ordered=True)
    else:
        rep.text(s, "（知识库未给出该缺陷的现场核查项）")
    return rep


# ------------------------------------------------------------------ 2 现场
def view_shopfloor(result):
    """现场技术人员：去现场看什么、按什么顺序。可打印 A4。"""
    rep = Report("现场作业单（现场技术人员视角）", _meta(result, "shopfloor"))
    rc = result.get("rootcause") or {}
    hs = result.get("unit_hotspots") or []

    s = rep.section("1. 先去哪看")
    hot = _hotspots(result)
    if hot:
        rows = []
        for h in hot[:10]:
            rows.append([h["dim"], h["level"],
                         ("%.4g" % h["mean"]) if h.get("mean") is not None else "—",
                         ("%.4g" % h["others_mean"])
                         if h.get("others_mean") is not None else "—",
                         ("%.3g" % h["p_adj"]) if h.get("p_adj") is not None else "—"])
        rep.table(s, ["维度", "水平", "该水平均值", "其他水平均值", "校正后 p"], rows,
                  caption="先跑偏离最大的那一台/那一班——这是数据指出的方向，"
                          "不是全部范围，现场看到别的异常同样要记")
    else:
        rep.text(s, "本次未检出显著的分组偏离——按下面的通用顺序逐项核查。")

    s = rep.section("2. 核查顺序（按此顺序执行，不要跳）")
    qc = rc.get("quick_checks") or []
    rows = []
    for i, q in enumerate(qc[:12], 1):
        txt = q if isinstance(q, str) else (q.get("item") or q.get("action") or str(q))
        rows.append([i, txt, "", ""])
    if rows:
        rep.table(s, ["#", "核查项", "实际看到", "是否异常"], rows,
                  caption="「实际看到」与「是否异常」两栏现场手写")
    else:
        rep.text(s, "（知识库未给出该缺陷的现场核查项——"
                    "建议按人/机/料/法/环逐项排查，并把结果补进知识库）")

    s = rep.section("3. 5Why（现场核对用）")
    fw = rc.get("five_why") or []
    if fw:
        rep.bullets(s, [str(w if isinstance(w, str) else (w.get("why") or w))
                        for w in fw[:6]], ordered=True)
    else:
        rep.text(s, "（未生成 5Why）")

    s = rep.section("4. 记录")
    rep.kv(s, [("执行人", "＿＿＿＿＿＿"), ("日期", "＿＿＿＿＿＿"),
               ("班组长确认", "＿＿＿＿＿＿")])
    rep.text(s, "作业单填完后交工艺工程师；若有与结论不符的现场实况，"
                "**以现场为准**，并把不符项反馈回来——数据取得偏了比算错更常见。")
    return rep


# ------------------------------------------------------------------ 3 质量
def view_quality(result):
    """质量管理人员：能不能发给客户。"""
    rep = Report("交付合规检查（质量管理人员视角）", _meta(result, "quality"))
    e8 = result.get("eightd") or {}
    is_red, why = _red(result)

    s = rep.section("1. 能不能发给客户")
    if is_red:
        rep.callout(s, "**暂不可对外**：%s" % why, "warn")
    else:
        rep.text(s, "未见阻断项。对外前仍建议由责任工程师确认签署栏。")

    s = rep.section("2. 8D 完整性")
    st = e8.get("signoff") or {}
    d0 = e8.get("d0") or {}
    fmea = e8.get("fmea") or {}
    rows = [
        # 「有」不等于「填完了」：D0 每项都要落到文件编号，只写「已处理」
        # 在客户审核时是退稿项，所以这里把未填项数一并报出来。
        ["D0 应急反应",
         ("有，%d 项未填：%s" % (len(d0.get("missing") or []),
                             "、".join((d0.get("missing") or [])[:3]))
          if d0.get("missing") else ("有" if d0 else "缺"))],
        ["D1–D8 各节", "有" if all(e8.get(k) for k in ("d5", "d6", "d7", "d8"))
         else "缺：%s" % "、".join(
             k.upper() for k in ("d5", "d6", "d7", "d8") if not e8.get(k))],
        ["PFMEA 已更新", "有" if fmea.get("rows") else "缺"],
        ["签署栏全签", "是" if st.get("all_signed") else "否"],
        ["D2 已确审", "是" if e8.get("d2_filled") else "否"],
    ]
    rep.table(s, ["检查项", "状态"], rows)
    miss = [r[0] for r in rows if r[1] in ("缺", "否")]
    if miss:
        rep.callout(s, "缺失项在客户审核时是退稿项：%s" % "、".join(miss), "warn")

    s = rep.section("3. 证据链")
    rows = []
    for c in (result.get("rootcause") or {}).get("candidates") or []:
        ev = _cand_evidence(c)
        stat = _cand_stat(c)
        rows.append([_cand_name(c),
                     "有" if ev or stat != "—" else "**无依据**",
                     (ev[:60] or stat)[:70] or "—"])
    if rows:
        rep.table(s, ["结论项", "数据依据", "依据摘要"], rows)
    else:
        rep.text(s, "（本次未给出根因候选，无证据链可核）")
    rep.callout(s, "每条结论必须能指回数据。写不出依据的结论，"
                   "客户追问时答不上来——那种结论在审核现场等于没写。", "warn")

    s = rep.section("4. 数据缺口（对外须如实声明）")
    gaps = result.get("gaps") or []
    if gaps:
        rep.bullets(s, [str(g.get("item") if isinstance(g, dict) else g)[:90]
                        for g in gaps[:8]])
    else:
        rep.text(s, "（无缺口）")
    return rep


# ------------------------------------------------------- 4 生产主管（R1）
def view_production(result):
    """生产主管：这批货放不放行。

    R1 责任约束（四条，缺一不可）：
      1. 措辞一律"建议"，不写"判定/结论"——决定权在人不在工具
      2. 每条建议必带依据列，没依据的条目不出现在这张表里
      3. 未确审时继承红标，红标状态写在表头，不是脚注
      4. 建议落盘成《处置判断记录》，谁在什么依据下建议了什么，可追溯
    """
    rep = Report("批次处置建议（生产主管视角）", _meta(result, "production"))
    is_red, why = _red(result)
    rc = result.get("rootcause") or {}
    cands = rc.get("candidates") or []

    s = rep.section("0. 先看这里")
    if is_red:
        rep.callout(s, "**红标状态**：%s。本表给出的是**建议**，"
                       "不是放行判定；未经责任工程师确审不得据此放行。" % why, "warn")
    else:
        rep.callout(s, "本表是**建议**不是判定。最终放行由责任工程师签批，"
                       "工具只负责把依据摆清楚。", "info")

    s = rep.section("1. 处置建议表")
    rows = []
    for c in cands[:8]:
        ev = _cand_evidence(c)
        stat = _cand_stat(c)
        if not ev and stat == "—":
            continue          # 没依据的条目不进这张表（R1 第 2 条）
        rows.append([
            _cand_name(c),
            "建议针对该参数排查后再定",
            (ev[:70] or stat)[:80],
            (result.get("confidence") or {}).get("level") or "—",
            "红标" if is_red else "—",
        ])
    if rows:
        rep.table(s, ["对象", "建议处置", "依据", "置信度", "状态"], rows,
                  caption="处置选项：继续生产 / 挑选 / 隔离待判 / 停产排查"
                          "（由责任工程师勾选）")
    else:
        rep.text(s, "没有带数据依据的处置对象——本表不给无依据的建议。"
                    "这种情况请先补数据或走人工评审。")

    s = rep.section("2. 影响范围")
    shape = result.get("shape") or []
    rep.kv(s, [("数据规模", "%s 行 × %s 列" % (shape[0], shape[1])
                if len(shape) >= 2 else "—"),
               ("分组维度", "、".join(result.get("groups") or []) or "—"),
               ("热点位置/机台", "、".join(
                   str(h.get("unit") or h.get("column") or "")
                   for h in (result.get("unit_hotspots") or [])[:5]) or "—")])

    s = rep.section("3. 签批栏（必须手写）")
    rep.kv(s, [("建议处置", "☐ 继续生产　☐ 挑选　☐ 隔离待判　☐ 停产排查"),
               ("责任工程师", "＿＿＿＿＿＿"), ("签批日期", "＿＿＿＿＿＿"),
               ("生产主管", "＿＿＿＿＿＿")])
    return rep


def write_disposition_record(result, outdir):
    """R1 第 4 条：留存判断记录。

    没有记录的建议，事后无法回答"当时依据什么做的决定"——
    而质量事故追责时第一个被问的就是这个。
    """
    is_red, why = _red(result)
    rc = result.get("rootcause") or {}
    lines = ["# 处置判断记录", "",
             "> 本文件由工具自动生成，用于留存「建议了什么、依据是什么」。",
             "> 工具只给建议，放行决定由责任工程师签批。", "",
             "- 生成时间：%s" % _now(),
             "- 工序：%s" % (result.get("process_name") or result.get("process") or "—"),
             "- 目标列：%s" % (result.get("target") or "—"),
             "- 置信度：%s" % ((result.get("confidence") or {}).get("level") or "—"),
             "- 红标状态：%s" % ("是（%s）" % why if is_red else "否"), "",
             "## 建议清单", ""]
    n = 0
    for c in (rc.get("candidates") or []):
        ev = c.get("evidence") or c.get("basis")
        if not ev:
            continue
        n += 1
        lines.append("%d. **%s**" % (n, c.get("name") or c.get("param") or "—"))
        lines.append("   - 建议：针对该参数排查后再定处置")
        lines.append("   - 依据：%s" % ev)
    if not n:
        lines.append("（无带依据的建议项）")
    lines += ["", "## 签批", "",
              "- 处置决定：☐ 继续生产　☐ 挑选　☐ 隔离待判　☐ 停产排查",
              "- 责任工程师：＿＿＿＿＿＿",
              "- 签批日期：＿＿＿＿＿＿", ""]
    p = os.path.join(outdir, "处置判断记录.md")
    try:
        io.open(p, "w", encoding="utf-8").write("\n".join(lines))
        return p
    except Exception:
        return None


# ------------------------------------------------------------------ 5 对外
def view_mgmt(result):
    """客户/管理层：单页摘要，对外口径。"""
    rep = Report("单页摘要（对外）", _meta(result, "mgmt"))
    is_red, why = _red(result)
    rc = result.get("rootcause") or {}
    cands = rc.get("candidates") or []
    lead = cands[0] if cands else None

    s = rep.section("结论")
    if lead:
        rep.text(s, "首要方向：**%s**。%s"
                 % (lead.get("name") or lead.get("param") or "—",
                    rc.get("mechanism") or ""))
    else:
        rep.text(s, "本次分析未定位到可行动根因，正在补充数据后复判。")
    if is_red:
        rep.callout(s, "本摘要含未确审内容，**正式对外前须经责任工程师确认**。", "warn")

    s = rep.section("当前状态")
    shape = result.get("shape") or []
    rep.kv(s, [("分析范围", "%s 行数据" % shape[0] if shape else "—"),
               ("置信度", (result.get("confidence") or {}).get("level") or "—"),
               ("已采取措施", "见 8D D0 应急反应" if (result.get("eightd") or {}).get("d0")
                else "（未生成）")])

    s = rep.section("下一步")
    rep.bullets(s, [
        "责任工程师确认根因方向是否符合现场认知",
        "按建议区间做验证试验，确定设定值",
        "更新 PFMEA 与作业标准",
        "完成 8D 签署后对外提交",
    ], ordered=True)

    s = rep.section("声明")
    rep.text(s, "本摘要由数据分析自动生成，结论基于所提供的数据样本；"
                "最终判定以现场验证与责任工程师签批为准。")
    return rep


# ------------------------------------------------------------- 术语速查
def view_glossary(result):
    rep = Report("术语速查附录", _meta(result, "all"))
    s = rep.section("术语对照")
    rep.table(s, ["术语", "中文名", "一句话说明"],
              [[a, b, c] for a, b, c in GLOSSARY],
              caption="同一概念在不同角色嘴里叫法不同，这里做单一事实源")
    s = rep.section("红标是什么")
    rep.text(s, "红标 = 结论未经人工确审。带红标的内容**不得直接对外**，"
                "也不得作为放行依据。确审流程：填写 D2 录入卡 → "
                "责任工程师确认 → 8D 签署栏齐全。")
    return rep


# ------------------------------------------------------------------ 入口
BUILDERS = {
    "process": view_process,
    "shopfloor": view_shopfloor,
    "quality": view_quality,
    "production": view_production,
    "mgmt": view_mgmt,
}


def build_view(result, view, outdir=None, with_glossary=True):
    """生成一个视角的 Report（outdir 给定时同时落盘）。

    返回 {"report": Report, "files": [路径...], "error": str|None}
    """
    fn = BUILDERS.get(view)
    if fn is None:
        return {"error": "未知视角：%s（可选：%s）"
                % (view, "、".join(VIEW_KEYS)), "report": None, "files": []}
    rep = fn(result)
    if with_glossary:
        # 术语附录并入同一份文件，而不是另出一份让人找不到
        g = view_glossary(result)
        s = rep.section("术语速查")
        rep.table(s, ["术语", "中文名", "说明"],
                  [[a, b, c] for a, b, c in GLOSSARY])
    files = []
    if outdir:
        saved = save_report(rep, outdir, BASENAME[view]) or {}
        # save_report 返回 {"md","html","charts","dir"}：只要两个正文档，
        # charts 是列表、dir 是目录，混进来会让产物清单里出现不是文件的项
        files = [p for p in (saved.get("md"), saved.get("html")) if p]
    out = {"report": rep, "files": files, "error": None}
    if view == "production" and outdir:
        rec = write_disposition_record(result, outdir)
        if rec:
            files.append(rec)
    return out


def build_all(result, outdir, views=None, with_glossary=True):
    """一次出全套。返回 {"files": [...], "errors": [...]}"""
    views = list(views or VIEW_KEYS)
    files, errors = [], []
    for v in views:
        r = build_view(result, v, outdir, with_glossary=with_glossary)
        if r.get("error"):
            errors.append(r["error"])
            continue
        files.extend(r.get("files") or [])
    return {"files": files, "errors": errors}
