# -*- coding: utf-8 -*-
"""端到端连贯交付编排：从单一 result.json 事实源产出四份互相印证的交付物。

用户反复指出的真缺陷：逻辑散乱 / 证据链断裂 / 各阶段函数无法连贯验证 /
报告结果没有实证支撑。根因不是单个函数写错，而是四份交付物各自为政——
没有统一事实源、没有可追溯的证据编号、没有跨交付物的一致性校验。

不变量（写进自检，改坏了必须翻红）：
  I1 单一事实源：四份交付物的每条结论都必须能在同一 result.json 里指出出处，
     不允许任何一份 report 自己新造事实。
  I2 证据编号贯穿：每个被引用事实分配稳定 F-ID，四份交付物都标注来源 F-ID。
  I3 结论同源：根因报告榜首 == 8D 的 D4 根因 == 措施响应的根因，同一个 F-ID。
  I4 诚实一致：d4_ok=False 时四份交付物一致标注「根因未确认」，不编造措施。
  I5 参数不固化：不写死任何行业 / 工序 / 产品 / 数值 / 阈值。

参数绝不固化——本模块只处理结构与证据关系，不含任何行业知识。
"""
import os
import json
import hashlib
import datetime

from . import status as ST

SCHEMA = "fde-chain-v1"

# 四份交付物的稳定文件名（只定义命名，不含任何内容模板）
NAMES = {
    "process": "01_工艺分析报告.md",
    "rootcause": "02_根因可能性分析报告.md",
    "sop_fmea": "03_SOP与FMEA改善措施.md",
    "eightd": "04_8D整改报告.md",
    "chain": "evidence_chain.json",
}


# --------------------------------------------------------------------------
# 事实账本：从 result.json 抽取客观事实，每条给出 source 路径（可复核）
# --------------------------------------------------------------------------
def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _sha(obj):
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()[:16]


def _shape_text(shape):
    if isinstance(shape, (list, tuple)) and len(shape) >= 2:
        return "%s 行 × %s 列" % (shape[0], shape[1])
    return str(shape or "—")


def _spc_claim(sp):
    """把 Python 字典原文改写成人能读的判异事实（原文仍留在账本里可机查）。"""
    col = sp.get("column") or sp.get("param_name") or "—"
    parts = []
    for v in (sp.get("violations") or sp.get("rules") or []):
        if isinstance(v, dict):
            rule = v.get("rule")
            desc = str(v.get("desc") or v.get("hint") or "").strip()
            n = v.get("count")
            if n is None:
                n = len(v.get("indexes") or [])
            parts.append("规则%s（%s）%s点" % (
                rule if rule is not None else "?", desc or "未命名规则", n))
        else:
            parts.append(str(v))
    sample = ""
    first = next((v for v in (sp.get("violations") or []) if isinstance(v, dict)), None)
    if first and first.get("indexes"):
        sample = "；样例样本序号=%s" % ", ".join(str(i) for i in first["indexes"][:8])
    return "%s 判异：%s%s" % (col, "；".join(parts) or "无判异规则", sample)


def _pareto_is_measure_sum(par):
    note = str((par or {}).get("note") or "")
    return ("累计值" in note
            or ("累计" in note and ("非缺陷数" in note or "非不良数" in note)))


def _pareto_claim(par):
    top = (par or {}).get("top")
    if not top:
        return "本次无可拆解的 Pareto 项"
    if not isinstance(top, dict):
        return "Pareto 首要项＝%s（上游只给了名称，无占比与口径）" % top
    label = "指标累计贡献首位（非不良数）" if _pareto_is_measure_sum(par) else "不良结构首要项"
    return "%s＝%s（占比 %.1f%%；口径：%s）" % (
        label, top.get("item"), float(top.get("pct") or 0),
        (par or {}).get("note") or "未登记口径")


def build_chain(res, source_path=None):
    """从 result.json 建证据账本。事实只从 res 里取，不推断、不新造。"""
    facts = {}

    def add(fid, kind, source, claim, **kw):
        if fid in facts:
            raise ValueError("事实编号重复：%s" % fid)
        facts[fid] = dict(id=fid, kind=kind, source=source,
                          claim=str(claim), **kw)

    rc = res.get("rootcause") or {}
    e8 = res.get("eightd") or {}
    d4_ok = bool(e8.get("d4_ok"))

    # 上下文事实（工序 / 目标 / 缺陷）——证据链shared
    add("F-CTX-01", "context", "target",
        "分析目标＝%s" % (res.get("target") or "—"),
        value=res.get("target"))
    add("F-CTX-02", "context", "process_name",
        "工序判定＝%s" % (res.get("process_name") or "未知工序"),
        value=res.get("process_name"), process_key=res.get("process"))

    # 信号事实：SPC 判异 / Pareto 首要项 / 能力不足
    for i, sp in enumerate(res.get("spc") or []):
        col = sp.get("column") or sp.get("param_name") or "—"
        viol = (sp.get("violations") or sp.get("rules") or [])
        if not viol:
            continue
        add("F-SIG-%02d" % (i + 1), "signal", "spc[%d]" % i,
            _spc_claim(sp), column=col, violations=viol)
    par = res.get("pareto") or {}
    if par.get("top"):
        add("F-SIG-P1", "signal", "pareto.top",
            _pareto_claim(par), item=par["top"], note=par.get("note"))

    # 根因候选事实：榜首固定 F-CAU-01，其余顺延（保证跨报告引用同一个 ID）
    cands = rc.get("candidates") or []
    lead_id = None
    for i, c in enumerate(cands):
        fid = "F-CAU-%02d" % (i + 1)
        add(fid, "rootcause", "rootcause.candidates[%d]" % i,
            "%s（证据分 %s，%s）" % (
                c.get("param_name") or "—", c.get("evidence_score"),
                c.get("conclusion_status") or "—"),
            param_name=c.get("param_name"), score=c.get("score"),
            evidence_score=c.get("evidence_score"),
            supported_by_data=bool(c.get("supported_by_data")),
            conclusion_status=c.get("conclusion_status"))
        if i == 0:
            lead_id = fid
    if lead_id is None:
        add("F-CAU-00", "rootcause", "rootcause.candidates",
            "本次数据未落定根因候选", param_name=None)

    # 措施事实：只在 d4_ok 时声明「响应榜首根因」，否则留空——I4 诚实一致
    resp = lead_id if (d4_ok and lead_id) else None
    d5 = e8.get("d5") or {}
    n_act = 0
    for i, r in enumerate(d5.get("rows") or []):
        act = str(r.get("action") or "").strip()
        if not act or act.startswith("**D4"):
            continue  # D4 未确认的占位行不是措施
        n_act += 1
        add("F-ACT-%02d" % n_act, "action", "eightd.d5.rows[%d]" % i,
            act[:200], category=r.get("category"), responds_to=resp)
    for i, r in enumerate((e8.get("d7") or {}).get("rows") or []):
        act = str(r.get("action") or "").strip()
        if not act:
            continue
        detail = " ".join(str(r.get(k) or "").strip()
                          for k in ("detail",) if r.get(k)).strip()
        add("F-SOP-%02d" % (i + 1), "sop", "eightd.d7.rows[%d]" % i,
            ("%s：%s" % (act, detail)).strip()[:300],
            responds_to=resp, doc_no=r.get("doc_no"),
            owner=r.get("owner"), due=r.get("due"))
    for i, r in enumerate((e8.get("fmea") or {}).get("rows") or []):
        content = str(r.get("content") or "").strip()
        if not content:
            continue
        add("F-FMEA-%02d" % (i + 1), "fmea", "eightd.fmea.rows[%d]" % i,
            "%s：%s" % (r.get("item") or "—", content[:150]),
            responds_to=resp)

    chain = {
        "schema": SCHEMA,
        "source_result": source_path or "",
        "chain_id": _sha({"facts": facts, "d4_ok": d4_ok}),
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "d4_ok": d4_ok,
        "lead_fact_id": lead_id if d4_ok else None,
        "facts": facts,
        "fact_count": len(facts),
        "note": ("本账本只登记 result.json 中已存在的事实及其出处；"
                 "四份交付物必须引用同一 chain_id 与同一批 F-ID。"),
    }
    return chain


# --------------------------------------------------------------------------
# 四份交付物：全部从 chain（=result.json 事实）渲染，标注 F-ID
# --------------------------------------------------------------------------
def _meta(chain):
    f = chain["facts"]
    return {
        "证据链": chain["chain_id"],
        "事实源": os.path.basename(chain.get("source_result") or "result.json"),
        "工序": (f.get("F-CTX-02") or {}).get("value") or "未知工序",
        "分析目标": (f.get("F-CTX-01") or {}).get("value") or "—",
        # 统计候选落定 ≠ 现场真因落定。旧文案只写「已落定」，与摘要里
        # 「置信度中低」并排时像自相矛盾；这里把层次写清楚。
        "根因状态": ("统计候选已落定（待现场验证）" if chain["d4_ok"]
                 else "未确认（不得作 D4）"),
    }


def _stamp(rep):
    """每份交付物末尾加盖证据链指纹，按名读取 meta 字段。

    这不是装饰：它让"证据链号/根因状态"在每份文件正文可核对，也保证这两个
    元信息字段有显式消费者（不是只写不读的死字段）。
    """
    sec = rep.section("附：证据链指纹")
    rep.kv(sec, [("证据链", rep.meta.get("证据链")),
                 ("事实源", rep.meta.get("事实源")),
                 ("根因状态", rep.meta.get("根因状态"))])
    return sec


def _fact_block(rep, chain, title, kinds):
    sec = rep.section(title)
    rows = [[v["id"], v["kind"], v["source"], v["claim"]]
            for v in chain["facts"].values() if v["kind"] in kinds]
    if not rows:
        rep.text(sec, "本次无该类别事实。")
    else:
        rep.table(sec, ["证据ID", "类别", "出处(result.json)", "事实"], rows,
                  caption="每条结论均可按「出处」回到 result.json 逐字段复核")
    return sec


def render_process(chain, res):
    """交付物①：工艺分析报告——只陈述检测事实，不下根因结论。"""
    from lib import report as R
    rep = R.Report("工艺分析报告", _meta(chain))
    s = rep.section("1. 数据与工序")
    rep.kv(s, [("样本规模", _shape_text(res.get("shape"))),
               ("工序", (chain["facts"].get("F-CTX-02") or {}).get("claim")),
               ("分析目标", (chain["facts"].get("F-CTX-01") or {}).get("claim"))])
    s = rep.section("2. 判异与指标结构")
    sig = [v for v in chain["facts"].values() if v["kind"] == "signal"]
    if sig:
        rep.bullets(s, ["[%s] %s" % (v["id"], v["claim"]) for v in sig])
    else:
        rep.text(s, "本次未检出判异信号。")
    for line in (res.get("summary") or [])[:8]:
        rep.text(s, str(line))
    _fact_block(rep, chain, "3. 本报告引用的事实（证据链）", ("context", "signal"))
    rep.callout(rep.section("4. 边界"),
                "本报告只陈述检测事实与判异结果，不指定根因；"
                "根因见《根因可能性分析报告》，二者共用证据链 %s。" % chain["chain_id"],
                "info")
    _stamp(rep)
    return rep


def render_rootcause(chain, res):
    """交付物②：根因可能性分析报告——候选按证据分排序，逐条给 F-ID。"""
    from lib import report as R
    rep = R.Report("根因可能性分析报告", _meta(chain))
    rc = res.get("rootcause") or {}
    s = rep.section("1. 结论")
    if chain["d4_ok"] and chain["lead_fact_id"]:
        lead = (chain["facts"].get(chain["lead_fact_id"]) or {})
        rep.callout(s, "首要候选根因：**%s**（证据分 %s）。证据ID %s。"
                    % (lead.get("param_name"), lead.get("evidence_score"),
                       chain["lead_fact_id"]), "ok")
    else:
        rep.callout(s, "本次数据**支撑不出根因结论**：证据不足或列名识别率过低。"
                    "下面的候选仅供排查，不得写入 8D 的 D4。", "warn")
    s = rep.section("2. 候选与证据")
    rows = []
    for v in chain["facts"].values():
        if v["kind"] != "rootcause":
            continue
        rows.append([v["id"], v.get("param_name") or "—", v.get("score"),
                     v.get("evidence_score"), v.get("conclusion_status") or "—",
                     v["source"]])
    if rows:
        rep.table(s, ["证据ID", "候选", "排序分", "证据分", "结论状态", "出处"], rows,
                  caption="排序分＝综合排序；证据分＝统计证据，二者口径不同不可混用")
    for note in (rc.get("notes") or [])[:6]:
        rep.text(s, str(note))
    if rc.get("five_why"):
        s = rep.section("3. 5Why 链")
        rep.text(s, str(rc["five_why"])[:1200])
    _fact_no = len(rep.sections) + 1
    _fact_block(rep, chain, "%d. 本报告引用的事实（证据链）" % _fact_no,
                ("context", "signal", "rootcause"))
    rep.callout(rep.section("%d. 边界" % (_fact_no + 1)),
                "根因可能性 ≠ 现场真因。本报告与 8D/FMEA/SOP 共用证据链 %s；"
                "最终归因须现场验证并签字。" % chain["chain_id"], "info")
    _stamp(rep)
    return rep


def render_sop_fmea(chain, res):
    """交付物③：SOP 与 FMEA 改善措施——措施必须可追溯到榜首根因。"""
    from lib import report as R
    rep = R.Report("SOP 与 FMEA 改善措施", _meta(chain))
    s = rep.section("1. 措施来源声明")
    if chain["d4_ok"]:
        rep.text(s, "以下措施由榜首根因 %s 推导，逐条标注响应的证据ID；"
                    "未落定根因前不得把任何条目当永久对策执行。"
                    % (chain["lead_fact_id"] or "—"))
    else:
        rep.callout(s, "根因未确认——本节仅为方向预置，**不是**可执行的永久对策；"
                    "S/O/D 与措施责任人须由现场填写，工具不代填。", "bad")
    acts = [v for v in chain["facts"].values() if v["kind"] == "action"]
    s = rep.section("2. 改善措施（FMEA 预防/探测控制来源）")
    if acts:
        rep.table(s, ["证据ID", "类别", "措施", "响应根因", "出处"],
                  [[v["id"], v.get("category") or "—", v["claim"],
                    v.get("responds_to") or "—", v["source"]] for v in acts])
    else:
        rep.text(s, "无可执行措施（根因未落定）。")
    fm = [v for v in chain["facts"].values() if v["kind"] == "fmea"]
    s = rep.section("3. FMEA 修订要点")
    # 失效模式要锚定本次实际分析对象（res.target），否则 FMEA 与工艺分析
    # 说的不是同一个指标——正是"各阶段脱节"的一种。参数由调用方数据带入。
    _target = res.get("target")
    if _target:
        rep.text(s, "本次 FMEA 围绕分析目标「%s」展开，与工艺分析报告同一对象。"
                    % _target)
    if fm:
        rep.bullets(s, ["[%s] %s" % (v["id"], v["claim"]) for v in fm])
    else:
        rep.text(s, "无 FMEA 修订要点。")
    sop = [v for v in chain["facts"].values() if v["kind"] == "sop"]
    s = rep.section("4. SOP / 标准作业修订表")
    if sop:
        rep.table(s, ["证据ID", "作业步骤与内容(来自D7)", "责任人", "时限",
                      "文件编号", "响应根因"],
                  [[v["id"], v["claim"], v.get("owner") or "（现场指定）",
                    v.get("due") or "（现场定）", v.get("doc_no") or "（待填写）",
                    v.get("responds_to") or "—"] for v in sop])
        rep.text(s, "上表作业内容来自 8D 的 D7（预防再发文件化），与根因共用"
                    "证据链 %s。责任人、时限、文件编号属产品参数，"
                    "必须由现场按企业文件体系填写，工具不代填。"
                    % chain["chain_id"])
    else:
        rep.text(s, "无 SOP 修订点（根因未落定时不固化文件）。")
    rep.callout(rep.section("5. 边界"),
                "S/O/D 评分、责任人、文件编号由现场填写；措施与根因共用证据链 %s，"
                "禁止把未验证的措施写进受控文件。" % chain["chain_id"], "warn")
    _stamp(rep)
    return rep


def render_eightd(chain, res):
    """交付物④：8D 整改报告——直接复用已有 render_8d，注入证据链元信息。"""
    from lib import eightd as E
    from lib import report as R
    rep = R.Report("8D 整改报告", _meta(chain))
    s = rep.section("0. 证据链声明", level=2)
    rep.text(s, "本报告 D4 根因、D5 措施、D7 文件化均来自同一事实源 "
                "result.json（证据链 %s）。四份交付物交叉校验结果见证据链文件。"
                % chain["chain_id"])
    E.render_8d(rep, res, parent=rep.sections[-1])
    _stamp(rep)
    return rep


RENDERERS = {
    "process": render_process,
    "rootcause": render_rootcause,
    "sop_fmea": render_sop_fmea,
    "eightd": render_eightd,
}


# --------------------------------------------------------------------------
# 交叉一致性校验：四份交付物之间结论必须互相印证（纯结构判据，无行业知识）
# --------------------------------------------------------------------------
def validate_chain(chain, res, rendered=None):
    """返回 (ok, violations)。每条违规都指出两个交付物之间具体不一致。"""
    import re
    v = []
    f = chain["facts"]
    d4 = chain["d4_ok"]
    lead = chain["lead_fact_id"]
    e8 = res.get("eightd") or {}

    # V1 榜首根因与 8D 结论同源
    cands = (res.get("rootcause") or {}).get("candidates") or []
    if d4 and (not lead or not lead.startswith("F-CAU")):
        v.append("V1 根因已落定但榜首证据ID无效：%s" % lead)
    if d4 and cands:
        top_name = cands[0].get("param_name")
        fid_name = (f.get(lead) or {}).get("param_name")
        if top_name != fid_name:
            v.append("V1 榜首根因不一致：result=%s / chain=%s" % (top_name, fid_name))

    # V2 d4_ok 与措施声明一致（未确认却声称响应根因 = 编造）
    for k, item in f.items():
        if item["kind"] in ("action", "sop", "fmea"):
            if not d4 and item.get("responds_to"):
                v.append("V2 %s 在根因未确认时声称响应 %s"
                         % (k, item["responds_to"]))
            if d4 and item.get("responds_to") != lead:
                v.append("V2 %s 响应 %s ≠ 榜首 %s"
                         % (k, item.get("responds_to"), lead))

    # V3 每条事实的 source 都能在 res 里解析到（不新造事实）
    for k, item in f.items():
        src = item["source"]
        if not _source_resolves(src, res):
            v.append("V3 %s 出处无法在 result.json 中解析：%s" % (k, src))

    # V4 工序 / 目标在四份交付物共用同一个值
    if (f.get("F-CTX-01") or {}).get("value") != res.get("target"):
        v.append("V4 分析目标与 result 不一致")
    if (f.get("F-CTX-02") or {}).get("value") != res.get("process_name"):
        v.append("V4 工序与 result 不一致")

    # V5 已渲染交付物必须在正文里出现同一个 chain_id（证据链可见）
    if rendered:
        for name, text in rendered.items():
            if chain["chain_id"] not in text:
                v.append("V5 交付物 %s 未标注证据链 %s"
                         % (name, chain["chain_id"]))

    # V6 8D 的 D4 状态与账本一致
    if bool(e8.get("d4_ok")) != d4:
        v.append("V6 8D d4_ok 与账本 d4_ok 不一致")

    # V7 措施指向的根因 == rootcause 榜首根因（同一 result 内跨字段同源）。
    # 这是"各阶段函数无法连贯验证"最典型的断法：榜首换了，8D 的 D5/D7
    # 措施文本还停在旧根因上，两份交付物各说各话。纯字段比对，无行业知识。
    if d4 and cands:
        top_name = str(cands[0].get("param_name") or "")
        for i, r in enumerate((e8.get("d5") or {}).get("rows") or []):
            tgt = str(r.get("target") or "")
            if tgt and tgt != top_name:
                v.append("V7 D5措施[%d]目标「%s」≠榜首根因「%s」" % (i, tgt, top_name))

    # V8 标志与告警同源时点：重算证据标志后，旧告警必须撤掉。
    # 否则同一份 result 一边 d4_ok=True、一边 warnings 说「归因不可用」。
    warns = [str(w) for w in (res.get("warnings") or [])]
    if d4 and any(w.startswith("根因归因不可用") for w in warns):
        v.append("V8 根因已落定，但 warnings 仍称归因不可用（标志与告警失配）")
    if not d4 and (res.get("rootcause") or {}).get("status") == ST.SUCCESS \
            and (res.get("rootcause") or {}).get("evidence_insufficient"):
        v.append("V8 根因状态 SUCCESS 却带证据不足标志")

    # V9 交付物可读性：原始 repr / 破损 Markdown / 断号章节，一律不许出关。
    if rendered:
        for name, text in rendered.items():
            if "****" in text:
                v.append("V9 %s 出现破损的 Markdown 强调标记（****）" % name)
            if re.search(r"\{'[^']+':", text):
                v.append("V9 %s 把字典原文当结论输出" % name)
            if re.search(r"样本规模：\[\s*\d+\s*,\s*\d+\s*\]", text):
                v.append("V9 %s 样本规模未格式化" % name)
            _xref = re.findall(r"第\s*(\d+)\s*节", text)
            if _xref:
                v.append("V9 %s 引用了本交付物不存在的章节号：%s" % (name, _xref))
            # 8D 有一节「## 0. 证据链声明」是前缀，不参与正文 1..N 编号
            nums = [int(x) for x in re.findall(r"(?m)^##\s+(\d+)\.", text) if int(x) >= 1]
            if nums and nums != list(range(1, len(nums) + 1)):
                v.append("V9 %s 章节编号断裂：%s" % (name, nums))

    # V10 Pareto 口径诚实：累计测量值不是不良数，不能标「不良结构」。
    par = res.get("pareto") or {}
    if _pareto_is_measure_sum(par):
        _claim = str((f.get("F-SIG-P1") or {}).get("claim") or "")
        if "不良结构" in _claim or "Pareto 首位" in _claim:
            v.append("V10 Pareto 是测量值累计，账本却称不良结构")
        for name, text in (rendered or {}).items():
            if "不良结构首要项" in text or re.search(r"Pareto 首位：", text):
                v.append("V10 %s 把测量值累计写成不良结构/Pareto 首位" % name)

    # V11 FMEA 实证：根因已落定时，至少一行要带上榜首根因或其证据分，
    # 不能整段「（待填写）」还叫「改善措施」。
    if d4 and cands:
        fmea_text = " ".join(str(r.get("content") or "")
                             for r in ((e8.get("fmea") or {}).get("rows") or []))
        lead_name = str(cands[0].get("param_name") or "")
        lead_ev = "%.3f" % float(cands[0].get("evidence_score") or 0)
        if (not lead_name or (lead_name not in fmea_text and lead_ev not in fmea_text)):
            v.append("V11 FMEA 未引用榜首根因及其证据分（无实证的空措施）")

    return (not v), v


def _source_resolves(src, res):
    """校验 source 路径（如 rootcause.candidates[0]）确实指向 res 里的对象。"""
    cur = res
    import re
    for part in src.split("."):
        m = re.match(r"^([^\[]+)((?:\[\d+\])*)$", part)
        if not m:
            return False
        key, idxs = m.group(1), m.group(2)
        if key and key not in (cur if isinstance(cur, dict) else {}):
            return False
        cur = cur[key] if isinstance(cur, dict) else None
        for im in re.finditer(r"\[(\d+)\]", idxs):
            i = int(im.group(1))
            if not isinstance(cur, list) or i >= len(cur):
                return False
            cur = cur[i]
    return cur is not None


# --------------------------------------------------------------------------
# 端到端入口
# --------------------------------------------------------------------------
def deliver(res_path, outdir):
    """从 result.json 产出四份连贯交付物 + 证据链 + 校验结果。

    事实源原件必须随交付物一起归档：证据链里每条事实都写"出处=result.json
    的某字段"，若原件留在临时目录被清掉，出处就指向一个不存在的文件——
    "可复核"变成假的。所以这里强制把 result.json 复制进交付目录，并让
    chain.source_result 指向这份归档副本，保证证据链自洽闭合。
    """
    with open(res_path, encoding="utf-8") as fh:
        res = json.load(fh)
    os.makedirs(outdir, exist_ok=True)
    # 归档事实源原件（不覆盖用户可能提供的同名正式文件）
    import shutil
    archived = os.path.join(outdir, "_source_result.json")
    if os.path.abspath(res_path) != os.path.abspath(archived):
        shutil.copy2(res_path, archived)
    chain = build_chain(res, source_path=archived)

    rendered = {}
    paths = {}
    for key, fn in RENDERERS.items():
        rep = fn(chain, res)
        md = rep.render_md()
        rendered[key] = md
        p = os.path.join(outdir, NAMES[key])
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(md)
        paths[key] = p

    ok, violations = validate_chain(chain, res, rendered=rendered)
    chain["validation"] = {"ok": ok, "violations": violations}
    with open(os.path.join(outdir, NAMES["chain"]), "w", encoding="utf-8") as fh:
        json.dump(chain, fh, ensure_ascii=False, indent=2)

    return {"ok": ok, "violations": violations, "chain_id": chain["chain_id"],
            "fact_count": chain["fact_count"], "files": paths,
            "chain_path": os.path.join(outdir, NAMES["chain"])}
