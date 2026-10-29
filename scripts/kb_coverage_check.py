# -*- coding: utf-8 -*-
"""
kb_coverage_check.py —— 知识库覆盖度自检（2.19.10 闭合剩余风险 #1）

问题本质：缺陷-参数映射的完整性只能靠公开分类口径做差集校准，现场适用性仍需复核。
这条不是代码 bug，是「依赖外部口径」的固有局限。本脚本把它从「看不见」变成「看得见的行动项」：

  1) 载入知识库（process_specs.json 的 processes + defect_rootcause）；
  2) 对照一份「参考分类口径」（默认内置、明确标注非权威；可用 --taxonomy 换成贵厂/行业标准）；
  3) 输出每道工序的覆盖数 / 缺口数 / 缺口清单，并列出"待复核"条目；
  4) 结论只回答"差集是什么、哪些条目还没校准来源"，**不杜撰已覆盖**——
     真正确认覆盖度，必须拿贵司受控的缺陷分类清单来替换内置参考。

用法：
  python scripts/kb_coverage_check.py
  python scripts/kb_coverage_check.py --taxonomy 贵厂缺陷分类.json
  python scripts/kb_coverage_check.py --json out.json   # 机器可读输出
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import kbstore as KB  # noqa: E402


# 内置参考分类口径（明确非权威，仅作起点）。每条带 pending_source，提示"需对照权威口径核实"。
# 真正确认覆盖度，请用 --taxonomy 换成贵厂/行业受控清单。
REFERENCE_TAXONOMY = {
    "pv_cell_el": {
        "source": "光伏 EL 常见缺陷类别（公开资料归纳，非任一标准原文，需对照贵厂分类核实）",
        "categories": [
            "断栅/栅线断裂", "黑斑/黑心", "隐裂/裂纹", "虚印/印刷不良", "划伤/擦伤",
            "污染/色斑", "低效片", "边缘/崩边", "明暗片/色差", "电极不良",
        ],
    },
    "pv_wafer": {
        "source": "硅片常见缺陷（公开资料归纳，非权威，需对照贵厂分类核实）",
        "categories": [
            "翘曲/弯曲", "厚度不均", "裂纹/碎片", "表面划伤", "杂质/污染",
            "电阻率异常", "边缘缺陷", "凹坑/麻点",
        ],
    },
    "pv_module_iv": {
        "source": "组件 IV 常见缺陷（公开资料归纳，非权威，需对照贵厂分类核实）",
        "categories": [
            "功率衰减", "热斑", "接线盒不良", "脱层/气泡", "隐裂", "低效组件",
        ],
    },
    "battery_coating": {
        "source": "锂电涂布常见缺陷（公开资料归纳，非权威，需对照贵厂分类核实）",
        "categories": [
            "面密度超差/不均", "厚边", "针孔/气泡", "划痕/漏涂", "横向厚度波动",
            "粘结不良", "颗粒/异物", "干燥不足",
        ],
    },
    "battery_calendering": {
        "source": "锂电辊压常见缺陷（公开资料归纳，非权威，需对照贵厂分类核实）",
        "categories": [
            "压实密度不均", "厚度超差", "表面亮线/暗线", "褶皱/打皱", "边缘开裂",
            "粘辊", "横向厚度波动",
        ],
    },
    "battery_winding": {
        "source": "锂电卷绕常见缺陷（公开资料归纳，非权威，需对照贵厂分类核实）",
        "categories": [
            "对齐度超差", "隔膜褶皱", "极耳错位", "短路/毛刺", "张力不良", "卷芯变形",
        ],
    },
    "battery_filling": {
        "source": "锂电注液常见缺陷（公开资料归纳，非权威，需对照贵厂分类核实）",
        "categories": [
            "注液量超差", "电解液泄漏", "浸润不良", "气密性不良", "真空度异常",
        ],
    },
    "battery_formation": {
        "source": "锂电化成常见缺陷（公开资料归纳，非权威，需对照贵厂分类核实）",
        "categories": [
            "化成容量不足", "电压异常", "内阻偏高", "析锂", "温度异常", "SOC 偏差",
        ],
    },
}


def _load_taxonomy(path):
    if not path:
        return REFERENCE_TAXONOMY
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # 允许两种形态：{process: [cat,...]} 或 {process: {"source":..,"categories":[...]}}
    out = {}
    for proc, val in data.items():
        if isinstance(val, dict) and "categories" in val:
            out[proc] = val
        else:
            out[proc] = {"source": "用户提供的外部分类口径", "categories": list(val)}
    return out


def _match(cat, known):
    """参考类别名 与 KB 缺陷名做关键词包含匹配（宽松，避免漏报缺口也避免误报覆盖）。"""
    c = cat.lower()
    for k in known:
        kl = k.lower()
        if c in kl or kl in c or any(w in kl for w in c.split() if len(w) >= 2):
            return k
    return None


def run(taxonomy_path=None):
    specs = KB.load("specs") or {}
    procs = specs.get("processes", {})
    dr = specs.get("defect_rootcause", {})

    # 按工序聚合 KB 已知缺陷名
    known_by_proc = {}
    pending_review = []
    for key, rec in dr.items():
        proc = rec.get("process")
        known_by_proc.setdefault(proc, []).append(key)
        note = rec.get("note") or ""
        src = rec.get("source") or ""
        if ("待复核" in note) or ("待确认" in note) or ("待复核" in src):
            pending_review.append(key)

    tax = _load_taxonomy(taxonomy_path)
    report = {
        "knowledge_base": {
            "n_processes": len(procs),
            "n_defect_entries": len(dr),
            "pending_review_entries": pending_review,
        },
        "coverage": [],
        "gap_total": 0,
        "limitation": (
            "覆盖度差集依赖所对照的参考口径：内置口径非权威，仅作起点。"
            "真正确认覆盖度，请用 --taxonomy 换成贵司受控的缺陷分类清单（或行业/标准原文）。"
        ),
    }
    for proc, meta in tax.items():
        known = known_by_proc.get(proc, [])
        gaps = []
        covered = 0
        for cat in meta["categories"]:
            hit = _match(cat, known)
            if hit:
                covered += 1
            else:
                gaps.append(cat)
        report["gap_total"] += len(gaps)
        report["coverage"].append({
            "process": proc,
            "process_name": procs.get(proc, {}).get("name", "") if isinstance(procs.get(proc), dict) else "",
            "ref_source": meta.get("source", ""),
            "ref_categories": len(meta["categories"]),
            "covered": covered,
            "gaps": gaps,
        })
    return report


def main():
    ap = argparse.ArgumentParser(description="知识库覆盖度自检（对照参考分类做差集）")
    ap.add_argument("--taxonomy", help="外部权威分类口径 JSON 路径（覆盖内置非权威参考）")
    ap.add_argument("--json", help="把机器可读报告写到该路径")
    args = ap.parse_args()

    rep = run(args.taxonomy)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)

    print("=== 知识库覆盖度自检 ===")
    kb = rep["knowledge_base"]
    print("知识库：%d 道工序 / %d 条缺陷-参数映射；待复核条目 %d 条"
          % (kb["n_processes"], kb["n_defect_entries"], len(kb["pending_review_entries"])))
    print("—— 覆盖度差集（对照参考口径；内置口径非权威，须换成贵厂/行业标准核实）——")
    for c in rep["coverage"]:
        flag = "✓" if not c["gaps"] else "△ 缺口"
        print("  [%s] %s（%s）：覆盖 %d/%d  %s"
              % (flag, c["process"], c["process_name"] or c["process"],
                 c["covered"], c["ref_categories"],
                 "" if not c["gaps"] else "缺口=" + "、".join(c["gaps"])))
    print("缺口合计：%d 条（均属『对照口径后待校准』，非已确认缺陷）" % rep["gap_total"])
    print("局限说明：%s" % rep["limitation"])
    if kb["pending_review_entries"]:
        print("待复核条目：%s" % "、".join(kb["pending_review_entries"][:10]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
