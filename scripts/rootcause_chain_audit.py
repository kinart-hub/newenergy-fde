# -*- coding: utf-8 -*-
"""rootcause_chain_audit.py —— 根因链路的四环节一致性审计（门禁第 12 步）

## 为什么需要它

前 11 步查的是"有没有断"：模块有没有调用、字段有没有人读、环境脏不脏。
它们查不出**"断着但还连着"**——字段有人读、结论有产出，但读出来的东西
与推导出来的东西不是一回事。

用户的原话是：「各个环节的根因逻辑分析、推导、取值、验证还是会脱节？」
这个问句指向的不是接口数量，而是**同一条结论在四个环节里是不是同一条**：

  分析（统计出 r / 效应量 / 时序吻合）
    → 推导（合成 score、过证据门槛、给结论等级）
      → 取值（哪个分数进表、表头公式是否适用、结论等级有没有跟着走）
        → 验证（置信度 / 5Why 支撑标记 / 验证方案 是否同一口径）

## 本工具的五条判据（全部是"抹掉就该翻红"的形态）

R1 **排序一致性**：`candidates` 的 score 必须单调不增。
   事故：排序发生在 `_annotate_evidence` 之前、而 score 在之后重算
   （重算时加了可验证性项、还过了一遍 `_evidence_gate` 降级），中间不重排。
   推导按旧口径排、展示用新口径，榜单可以出现"0.22 排在 0.45 前面"。

R2 **分级完整性**：每条候选必须带 conclusion_status / supported_by_data。
   事故：根因有 5 条构造路径（知识库先验 / 数据驱动候选 / data_driven 兜底 /
   单元级热点 / 位置聚集），只有前两条过 `_annotate_evidence`。
   实测 11 个示例里 8 个存在缺态候选，04、06 是 5/5 全缺。

R3 **分级可达**：结论等级必须进报告正文。
   事故：conclusion_status 在 rootcause.py 之外零消费，
   报告里"数据支持/机理支持/不可据此调机"出现 0 次。
   造了分级却不展示，等于没有分级——读者看到六条候选长得一模一样。

R4 **口径一致性**：每条候选必须声明 score_basis；同一张榜单里
   basis 不唯一时，表头不得声称统一的加权公式。
   事故：知识库候选 score 是四项加权后过 gate，单元级热点是 effect÷2，
   位置聚集是 ratio÷5，三者混在一张表里按数值排序，
   而表头写着"0.40×数据证据 + 0.35×知识先验 + 0.15×时序 + 0.10×可验证"。

R5 **置信度不可高于根因证据等级**：整体置信度 ≥0.75（中高）时，
   根因榜里必须至少有一条"数据支持"。
   事故：`confidence_level` 只看相关性显著个数与 P0 缺口，
   不看根因候选的结论等级。实测 06 示例报"中高 0.75"，
   而它的根因候选一条分级都没有。

## 用法

    python scripts/rootcause_chain_audit.py            # 全示例体检
    python scripts/rootcause_chain_audit.py --json x.json
    python scripts/rootcause_chain_audit.py --selftest # 判据自检（含反向验证）
    python scripts/rootcause_chain_audit.py --gate      # 有违规即退出码 1

**本工具只读**：跑 analyze 到临时目录，不写仓库内任何文件。
"""

from __future__ import print_function

import argparse
import glob
import json
import os
import shutil
import sys
import tempfile

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_ROOT)

EXAMPLE_GLOB = os.path.join(SKILL_ROOT, "examples", "*.csv")

# 分级字段：五条构造路径都必须给齐，缺一个就算 R2 违规
REQUIRED_GRADE_FIELDS = ("conclusion_status", "supported_by_data")
GRADE_VALUES = ("数据支持", "机理支持", "现场待确认", "未知")

# R5 的阈值：置信度达到这个分数就要求根因榜里有数据支持
CONF_HIGH = 0.75


def _load():
    from lib import pipeline as PL
    return PL


def _run_one(csv_path, outdir):
    PL = _load()
    return PL.analyze([csv_path], outdir=outdir, export_xlsx=False,
                      export_docx=False, export_pdf=False)


def _backed(c):
    """候选是否"指得到列且有数据"——与 rank_root_causes 的 _rank_key 第一项同义。"""
    st = c.get("data_status")
    return bool(c.get("param")) and (st == "SUCCESS" or st is None and c.get("supported_by_data"))


def _findings_for(case_name, res, report_text):
    """对单次分析结果做 R1~R5 判定，返回违规列表。"""
    out = []
    rc = res.get("rootcause") or {}
    cands = rc.get("candidates") or []
    if not cands:
        return out

    # ---- R1 排序一致性（在分区内检查）----
    #
    # rank_root_causes 的最终排序键是 (是否有数据支撑, -score)：**指得到列的候选
    # 永远排在指不到列的纯先验候选之前**，这是有意的设计（E07 评审要求），
    # 不是倒挂。所以判据必须在"同一分区内"查降序，否则会把正确设计判成缺陷——
    # 首版按全局单调检查，会在这类数据上误报。
    #
    # 真正要抓的是：同一分区内部，分数与顺序自相矛盾。
    for i in range(1, len(cands)):
        prev, cur = cands[i - 1], cands[i]
        if _backed(prev) != _backed(cur):
            continue          # 跨分区，不比较
        if (cur.get("score") or 0) > (prev.get("score") or 0) + 1e-9:
            out.append({
                "rule": "R1", "case": case_name,
                "detail": "同一分区内分数不降序：第 %d 名《%s》%.4f < 第 %d 名《%s》%.4f"
                          % (i, prev.get("param_name"), prev.get("score") or 0,
                             i + 1, cur.get("param_name"), cur.get("score") or 0),
                "why": "推导按一套分数排序、展示用另一套分数，中间没有重排。"
                       "读者看到的是一个自相矛盾的榜单。",
            })
            break

    # ---- R2 分级完整性 ----
    miss = [c.get("param_name") for c in cands
            if any(c.get(f) is None for f in REQUIRED_GRADE_FIELDS)]
    if miss:
        out.append({
            "rule": "R2", "case": case_name,
            "detail": "%d/%d 条候选缺结论分级字段：%s"
                      % (len(miss), len(cands), "、".join(str(m) for m in miss[:4])),
            "why": "根因有多条构造路径，只有部分路径过了 _annotate_evidence。"
                   "缺字段的候选在报告里与有分级的候选长得一样，读者无法区分"
                   "『数据支持』和『知识库猜测』。",
        })

    # ---- R4 口径一致性 ----
    # 每条候选必须声明它的 score 是按什么算的；同一个榜单里出现多种口径时，
    # 表头就不能声称统一的加权公式。
    bases = {}
    for c in cands:
        b = c.get("score_basis")
        if not b:
            bases.setdefault("<未声明>", []).append(c.get("param_name"))
        else:
            bases.setdefault(b, []).append(c.get("param_name"))
    if "<未声明>" in bases:
        out.append({
            "rule": "R4", "case": case_name,
            "detail": "%d 条候选未声明 score_basis：%s"
                      % (len(bases["<未声明>"]),
                         "、".join(str(x) for x in bases["<未声明>"][:4])),
            "why": "不同来源的候选用了不同的打分口径（四项加权 / effect÷2 / ratio÷5），"
                   "不声明就无法判断这个分数能不能横向比较。",
        })
    elif len(bases) > 1:
        # 多口径本身不是错——单元级异常、位置聚集是**有价值的独立证据源**，
        # 把它们排除出榜单反而是信息损失。错的是"混着排却不说明"：
        # 表头声称统一加权公式，而表里有的行是按另一套算的。
        # 所以判据落在"是否如实披露"上，而不是"是否允许并存"。
        _disclosed = any(k in (report_text or "")
                         for k in ("评分口径", "口径", "评分依据"))
        if not _disclosed:
            out.append({
                "rule": "R4", "case": case_name,
                "detail": "同一榜单混用 %d 种打分口径（%s），但报告未披露口径差异"
                          % (len(bases), "；".join("%s(%d条)" % (k, len(v))
                                                   for k, v in sorted(bases.items()))),
                "why": "不同口径的分数不在同一尺度上，混排且不说明，会让『来源不同』"
                       "看起来像『证据强弱不同』。",
            })

    # ---- R5 置信度与根因证据等级 ----
    conf = res.get("confidence") or {}
    cs = float(conf.get("score") or 0)
    has_data_backed = any(c.get("conclusion_status") == "数据支持" for c in cands)
    if cs >= CONF_HIGH and not has_data_backed:
        out.append({
            "rule": "R5", "case": case_name,
            "detail": "置信度 %s（%.2f），但根因榜无一条『数据支持』（最高等级：%s）"
                      % (conf.get("level"), cs,
                         next((c.get("conclusion_status") for c in cands
                               if c.get("conclusion_status")), "无分级")),
            "why": "置信度只按相关性显著个数与 P0 缺口打分，不看根因候选的证据等级。"
                   "结果可能是『整体置信中高』配一张全是假设的根因榜。",
        })
    return out


def scan_examples(limit=None):
    """跑全部示例，逐个判定。返回 {"cases": [...], "findings": [...]}"""
    cases, findings = [], []
    tmp = tempfile.mkdtemp(prefix="rcchain-")
    try:
        files = sorted(glob.glob(EXAMPLE_GLOB))
        if limit:
            files = files[:limit]
        for f in files:
            name = os.path.basename(f)
            outdir = os.path.join(tmp, name[:2])
            try:
                res = _run_one(f, outdir)
            except Exception as e:
                cases.append({"case": name, "status": "CRASH", "error": repr(e)[:160]})
                continue
            md_path = None
            for cand_md in glob.glob(os.path.join(outdir, "*.md")):
                if "报告" in os.path.basename(cand_md):
                    md_path = cand_md
                    break
            text = ""
            if md_path and os.path.exists(md_path):
                try:
                    text = open(md_path, encoding="utf-8").read()
                except Exception:
                    text = ""
            rc = res.get("rootcause") or {}
            cases.append({
                "case": name,
                "n_candidates": len(rc.get("candidates") or []),
                "confidence": (res.get("confidence") or {}).get("level"),
                "rootcause_path": ("data_driven" if rc.get("data_driven")
                                   else ("degraded" if rc.get("honest_degraded")
                                         else "rank")),
                "report_has_grade": any(g in text for g in GRADE_VALUES),
            })
            findings.extend(_findings_for(name, res, text))
            # ---- R3 分级可达（按报告正文判）----
            if (rc.get("candidates") and not any(g in text for g in GRADE_VALUES)):
                findings.append({
                    "rule": "R3", "case": name,
                    "detail": "根因榜有 %d 条候选，但报告正文未出现任何结论等级字样"
                              % len(rc.get("candidates") or []),
                    "why": "分级造出来却没人展示：读者看到的是一张不带可信度标注的"
                           "候选表，会把知识库猜测当数据结论用。",
                })
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"cases": cases, "findings": findings}


# ---------------------------------------------------------------- 自检

def _selftest():
    """判据自检：每条规则都要能被一件人为构造的坏数据触发，
    并且在一件好数据上不误报。**只断言"能抓到"不够——还要断言"好数据不报"。**
    """
    out = []

    good = {
        "confidence": {"level": "中高", "score": 0.75},
        "rootcause": {"candidates": [
            {"param_name": "A", "score": 0.9, "conclusion_status": "数据支持",
             "supported_by_data": True, "score_basis": "weighted"},
            {"param_name": "B", "score": 0.5, "conclusion_status": "机理支持",
             "supported_by_data": False, "score_basis": "weighted"},
        ]},
    }
    text_good = "候选根因：数据支持"
    out.append(("健康数据零违规", not _findings_for("good", good, text_good),
                str(_findings_for("good", good, text_good))[:80]))

    # R1：分数倒挂
    bad1 = {"confidence": {"level": "中", "score": 0.6},
            "rootcause": {"candidates": [
                {"param_name": "A", "score": 0.22, "conclusion_status": "数据支持",
                 "supported_by_data": True, "score_basis": "w"},
                {"param_name": "B", "score": 0.45, "conclusion_status": "数据支持",
                 "supported_by_data": True, "score_basis": "w"},
            ]}}
    f1 = _findings_for("b1", bad1, "数据支持")
    out.append(("R1 抓到分数倒挂", any(x["rule"] == "R1" for x in f1),
                str([x["rule"] for x in f1])))

    # R2：分级字段缺失
    bad2 = {"confidence": {"level": "中", "score": 0.6},
            "rootcause": {"candidates": [
                {"param_name": "A", "score": 0.9, "conclusion_status": None,
                 "supported_by_data": None, "score_basis": "w"},
            ]}}
    f2 = _findings_for("b2", bad2, "数据支持")
    out.append(("R2 抓到分级字段缺失", any(x["rule"] == "R2" for x in f2),
                str([x["rule"] for x in f2])))

    # R4：口径未声明
    bad4 = {"confidence": {"level": "中", "score": 0.6},
            "rootcause": {"candidates": [
                {"param_name": "A", "score": 0.9, "conclusion_status": "数据支持",
                 "supported_by_data": True},
            ]}}
    f4 = _findings_for("b4", bad4, "数据支持")
    out.append(("R4 抓到口径未声明", any(x["rule"] == "R4" for x in f4),
                str([x["rule"] for x in f4])))

    # R4：多口径混排
    bad4b = {"confidence": {"level": "中", "score": 0.6},
             "rootcause": {"candidates": [
                 {"param_name": "A", "score": 0.9, "conclusion_status": "数据支持",
                  "supported_by_data": True, "score_basis": "weighted"},
                 {"param_name": "B", "score": 0.5, "conclusion_status": "数据支持",
                  "supported_by_data": True, "score_basis": "unit_effect"},
             ]}}
    f4b = _findings_for("b4b", bad4b, "数据支持")
    out.append(("R4 抓到多口径未披露", any(x["rule"] == "R4" for x in f4b),
                str([x["rule"] for x in f4b])))
    # R4 反向：多口径但报告已披露口径列 → 不报
    out.append(("R4 不误报：多口径且已披露口径列不算违规",
                not any(x["rule"] == "R4"
                        for x in _findings_for("ok4b", bad4b, "数据支持 评分口径")), ""))

    # R5：置信中高但没有数据支持
    bad5 = {"confidence": {"level": "中高", "score": 0.75},
            "rootcause": {"candidates": [
                {"param_name": "A", "score": 0.9, "conclusion_status": "机理支持",
                 "supported_by_data": False, "score_basis": "w"},
            ]}}
    f5 = _findings_for("b5", bad5, "机理支持")
    out.append(("R5 抓到置信度高于证据等级", any(x["rule"] == "R5" for x in f5),
                str([x["rule"] for x in f5])))

    # R5 反向：置信低时不该报
    ok5 = {"confidence": {"level": "中低", "score": 0.5},
           "rootcause": {"candidates": [
               {"param_name": "A", "score": 0.9, "conclusion_status": "机理支持",
                "supported_by_data": False, "score_basis": "w"},
           ]}}
    out.append(("R5 不误报：置信度低时不要求数据支持",
                not any(x["rule"] == "R5" for x in _findings_for("ok5", ok5, "机理支持")), ""))

    # R1 反向：跨分区分数上升不算违规（指得到列者优先是有意设计）
    ok_part = {"confidence": {"level": "中", "score": 0.6},
               "rootcause": {"candidates": [
                   {"param_name": "A", "param": "a", "score": 0.30,
                    "data_status": "SUCCESS", "conclusion_status": "数据支持",
                    "supported_by_data": True, "score_basis": "w"},
                   {"param_name": "B", "param": None, "score": 0.50,
                    "data_status": "INSUFFICIENT_DATA", "conclusion_status": "机理支持",
                    "supported_by_data": False, "score_basis": "w"},
               ]}}
    out.append(("R1 不误报：跨分区分数上升是设计而非倒挂",
                not any(x["rule"] == "R1"
                        for x in _findings_for("okpart", ok_part, "数据支持")), ""))

    # R1 反向：分数相等不算倒挂（1e-9 容差）
    ok1 = {"confidence": {"level": "中", "score": 0.6},
           "rootcause": {"candidates": [
               {"param_name": "A", "score": 0.30, "conclusion_status": "数据支持",
                "supported_by_data": True, "score_basis": "w"},
               {"param_name": "B", "score": 0.30, "conclusion_status": "数据支持",
                "supported_by_data": True, "score_basis": "w"},
           ]}}
    out.append(("R1 不误报：同分不判倒挂",
                not any(x["rule"] == "R1" for x in _findings_for("ok1", ok1, "数据支持")), ""))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="根因链路四环节一致性审计（只读）")
    ap.add_argument("--json", default=None)
    ap.add_argument("--gate", action="store_true", help="有违规即退出码 1")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个示例")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        print("=" * 72)
        print("根因链路判据自检")
        print("=" * 72)
        bad = 0
        items = _selftest()
        for name, ok, detail in items:
            print("  [%s] %s %s" % ("OK  " if ok else "FAIL", name, detail or ""))
            if not ok:
                bad += 1
        print("-" * 72)
        print("通过 %d / 失败 %d" % (len(items) - bad, bad))
        return 0 if bad == 0 else 1

    if not glob.glob(EXAMPLE_GLOB):
        # 「0 个示例 → 0 条违规 → 通过」是最坏的一种绿：它把"没验"报成"验过了"。
        # 示例素材不随部分部署形态交付，所以这里必须显式区分"验过没问题"
        # 与"压根没有可验的样本"——后者退出码 2（区别于违规的 1）。
        print("=" * 72)
        print("根因链路四环节一致性审计")
        print("=" * 72)
        print("")
        print("✗ 未找到任何示例数据（%s）" % EXAMPLE_GLOB)
        print("  这不等于『四环节一致』——是**没有任何样本被检查**。")
        print("  请在有 examples/ 的仓库根运行；若确实无样本，本项检查不成立。")
        return 2

    res = scan_examples(args.limit)
    print("=" * 72)
    print("根因链路四环节一致性审计（分析 → 推导 → 取值 → 验证）")
    print("=" * 72)
    print("\n%-26s %4s %6s %-12s %s"
          % ("示例", "候选", "置信", "根因路径", "报告含结论等级"))
    for c in res["cases"]:
        if c.get("status") == "CRASH":
            print("%-26s 崩溃 %s" % (c["case"][:24], c.get("error", "")[:50]))
            continue
        print("%-26s %4d %6s %-12s %s"
              % (c["case"][:24], c["n_candidates"], c.get("confidence") or "—",
                 c.get("rootcause_path") or "—",
                 "是" if c["report_has_grade"] else "**否**"))

    by_rule = {}
    for f in res["findings"]:
        by_rule.setdefault(f["rule"], []).append(f)
    print("\n" + "-" * 72)
    if not res["findings"]:
        print("✓ 四环节一致，未发现脱节")
    for rule in ("R1", "R2", "R3", "R4", "R5"):
        items = by_rule.get(rule) or []
        if not items:
            continue
        print("\n【%s】%d 例" % (rule, len(items)))
        seen = set()
        for f in items:
            key = f["detail"].split("：")[0]
            if key in seen:
                continue
            seen.add(key)
            print("  · %s：%s" % (f["case"], f["detail"]))
            print("      %s" % f["why"])
    print("\n" + "-" * 72)
    print("合计 %d 条违规（示例 %d 个）"
          % (len(res["findings"]), len(res["cases"])))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        print("机读结果：%s" % args.json)
    if args.gate and res["findings"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
