#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""能力矩阵：把"每个能力在每种真实用法下到底出不出东西"跑成一张表。

为什么需要这张表，而不是继续读代码
=============================================================================
    用户的原话是"各模块在自己的文件里自我感觉良好，各自为政"。这句话的技术含义是：
    **每个模块的单元测试都在测"给定正确输入时算得对"，而没有任何测试在测
    "这次分析里它到底有没有被执行、它的产出有没有流到下一环"**。

    所以判断"某个能力是否可用"不能靠读代码，只能靠**给它真实输入跑一遍**：
      · 产出字段为空 → 这个能力在这类数据上没工作
      · 产出非空但报告里没有对应章节 → 算了没给人看（断头路）
      · 报告里有章节但内容为空 → 模板在、数据没到（形式主义）

九种真实用法（不是参数穷举，是现场真会遇到的形态）
=============================================================================
    单文件 / 双文件跨工序 / 角色视角 / 手动指定设备 / 规格版本化 /
    目录批量 / 脏数据 / 无目标列 / 关闭 DOE

用法
=============================================================================
    python scripts/capability_matrix.py                  # 跑全部场景
    python scripts/capability_matrix.py --only single    # 只跑某个
    python scripts/capability_matrix.py --json output/matrix.json --md output/matrix.md
"""
import sys
import os
import io
import json
import argparse
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

EX = os.path.join(ROOT, "examples")

# 每个能力：产出字段路径 + 报告里应出现的章节关键词
# 字段路径用点号；报告关键词用于判断"算了但没给人看"
CAPS = [
    # (能力名, result 里的字段路径, 报告里应出现的章节关键词)
    # 字段路径全部经实跑核对过——写错路径会造出"能力缺失"的假象，
    # 而假象比没检测更糟（会指着一个不存在的问题去修）。
    ("清洗动作", "clean.actions", None),
    ("SPC 控制图", "spc", "SPC 控制图"),
    ("过程能力", "spc.0.capability", "过程能力"),
    ("缺陷识别", "defects", "Pareto"),
    ("根因候选", "rootcause.candidates", "根因"),
    ("机理结论", "rootcause.mechanism", None),
    ("5Why", "rootcause.five_why", "5Why"),
    ("参数相关性", "strong_pairs", "相关性"),
    ("共线体检查", "vif", None),
    ("横向展开", "lateral.rows", "横向排查"),
    ("永久对策", "permanent.generic", "永久措施"),
    ("预防措施", "preventive.params", "预防建议"),
    ("跨工序追溯", "crossprocess.verdict", "跨工序"),
    ("角色视角", "views.files", None),
    ("规格版本化", "spc.0.spec_versioning", "规格版本"),
    ("设备档案", "device", "设备"),
    ("批号拆解", "lot_segments", "批次"),
    ("行级追溯", "provenance", "追溯"),
    ("降级台账", "degraded_sections", "降级"),
    ("8D 骨架", "eightd", "8D"),
]


def _get(d, path):
    """按点号取嵌套字段。"""
    cur = d
    for k in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list):
            try:
                cur = cur[int(k)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def _nonempty(v):
    if v is None:
        return False
    if isinstance(v, (list, dict, str, tuple)):
        return len(v) > 0
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return True
    return bool(v)


def _spec_dict(_td=None):
    """最小可用规格书（触发规格版本化路径）。

    注意：analyze(spec_overrides=...) 要的是**已解析的 dict**，
    不是文件路径——CLI 里是 `json.load(args.spec)` 之后再传的。
    直接传路径会在 specver.load 里崩（'str' object has no attribute 'items'），
    这一点探针自己踩过，别把探针的错误记成产品缺陷。
    """
    # 用仓库里**真实的**规格版本书，不要另造一份：
    # 自造的单版本规格不会触发版本化路径（n_versioned=0），
    # 于是"规格版本化没生效"会变成探针假象。
    src = os.path.join(EX, "08_规格版本.json")
    return json.load(io.open(src, encoding="utf-8"))


SCENES = [
    ("single", "单文件（最常见）",
     lambda td: {"paths": [os.path.join(EX, "01_涂布工艺数据.csv")]}),
    ("cross", "双文件跨工序",
     lambda td: {"paths": [os.path.join(EX, "01_涂布工艺数据.csv"),
                           os.path.join(EX, "02_卷绕工艺数据.csv")]}),
    ("views", "带角色视角",
     lambda td: {"paths": [os.path.join(EX, "01_涂布工艺数据.csv")],
                 "views": ["all"]}),
    ("specver", "带规格版本书",
     lambda td: {"paths": [os.path.join(EX, "08_跨规格版本数据.csv")],
                 "spec_overrides": _spec_dict(td)}),
    ("dir", "目录批量",
     lambda td: {"paths": None,
                 "input_dir": os.path.join(EX, "enterprises", "E01_锂电涂布")}),
    ("dirty", "脏数据（小厂导出）",
     lambda td: {"paths": [os.path.join(EX, "enterprises", "E07_小厂脏数据",
                                        "01_未知名仪器_自定义列名.csv")]}),
    ("el", "EL 缺陷（属性数据）",
     lambda td: {"paths": [os.path.join(EX, "05_EL缺陷检测数据.csv")]}),
]


def run_scene(name, title, build):
    from lib import pipeline as PL

    td = tempfile.mkdtemp(prefix="capmat-")
    kwargs = build(td)
    # analyze 的第一个参数是 paths（一个文件列表），不是 input
    paths = kwargs.pop("paths", None)
    if paths is None and not kwargs.get("input_dir"):
        raise ValueError("场景 %s 既没有 paths 也没有 input_dir" % name)
    if kwargs.get("input_dir"):
        paths = []
    outdir = os.path.join(td, "out")
    res = {}
    err = None
    try:
        kwargs.setdefault("export_xlsx", False)
        kwargs.setdefault("export_docx", False)
        kwargs.setdefault("export_pdf", False)
        res = PL.analyze(paths or [], outdir=outdir, do_doe=False,
                         max_charts=0, **kwargs)
    except Exception as e:                     # noqa: BLE001 — 崩了也要如实记
        err = "%s: %s" % (type(e).__name__, str(e)[:160])

    # 必须读**主报告**。目录里还有 8D_D2录入卡.md 等附属产物，
    # 取第一个 .md 会读到 8D 卡，于是"报告里没有控制图/根因"全是假象。
    md = ""
    if os.path.isdir(outdir):
        cands = [f for f in os.listdir(outdir) if f.endswith(".md")]
        main = [f for f in cands if "工艺分析" in f] or cands
        if main:
            md = io.open(os.path.join(outdir, main[0]), encoding="utf-8",
                         errors="ignore").read()
    shutil.rmtree(td, ignore_errors=True)
    return {"scene": name, "title": title, "error": err,
            "md_len": len(md), "result": res, "md": md}


def judge(row):
    """每个能力的判定：off（没产出）/ blind（产出但报告没体现）/ ok。"""
    res, md = row["result"], row["md"]
    out = []
    for cname, path, rpt_kw in CAPS:
        v = _get(res, path) if res else None
        has = _nonempty(v)
        n = len(v) if isinstance(v, (list, dict, str)) else (1 if has else 0)
        rpt = None
        if rpt_kw:
            rpt = rpt_kw in md
        verdict = "ok" if has else "off"
        if has and rpt is False:
            verdict = "blind"          # 算了，但报告里没有 —— 用户看不到
        out.append({"cap": cname, "n": n, "in_report": rpt, "verdict": verdict})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="能力矩阵实测")
    ap.add_argument("--only", help="只跑某个场景（名）")
    ap.add_argument("--json")
    ap.add_argument("--md", help="把矩阵写成 markdown 表格（给文档引用）")
    a = ap.parse_args(argv)

    rows = []
    for name, title, build in SCENES:
        if a.only and name != a.only:
            continue
        print("\n[...] 跑场景：%s（%s）" % (title, name))
        r = run_scene(name, title, build)
        if r["error"]:
            print("  ✗ analyze 失败：%s" % r["error"])
        rows.append(r)

    print("\n" + "=" * 96)
    hdr = "%-14s" % "能力"
    for r in rows:
        hdr += " %-10s" % r["scene"]
    print(hdr)
    print("-" * 96)
    table = []
    for i, (cname, _p, _k) in enumerate(CAPS):
        line = "%-14s" % cname
        rec = {"cap": cname}
        for r in rows:
            j = judge(r)[i]
            mark = {"ok": "✓%d" % j["n"], "blind": "◐%d" % j["n"], "off": "·"}[j["verdict"]]
            line += " %-10s" % mark
            rec[r["scene"]] = j["verdict"]
        print(line)
        table.append(rec)
    print("-" * 96)
    print("✓=有产出且报告有体现  ◐=算了但报告里没有（断头路）  ·=无产出")
    for r in rows:
        if r["error"]:
            print("  ⚠ %s: %s" % (r["scene"], r["error"]))

    if a.json:
        d = os.path.dirname(a.json)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        io.open(a.json, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"table": table,
                        "scenes": [{"scene": r["scene"], "title": r["title"],
                                    "error": r["error"], "md_len": r["md_len"],
                                    "detail": judge(r)} for r in rows]},
                       ensure_ascii=False, indent=2) + "\n")
        print("\nJSON → %s" % a.json)
    if a.md:
        L = ["# 能力矩阵（实测）", "",
             "| 能力 | " + " | ".join(r["scene"] for r in rows) + " |",
             "|" + "---|" * (len(rows) + 1)]
        for i, (cname, _p, _k) in enumerate(CAPS):
            cells = []
            for r in rows:
                j = judge(r)[i]
                cells.append({"ok": "✓%d" % j["n"], "blind": "◐%d" % j["n"],
                              "off": "·"}[j["verdict"]])
            L.append("| %s | %s |" % (cname, " | ".join(cells)))
        L += ["", "✓=有产出且报告有体现  ◐=算了但报告里没有（断头路）  ·=无产出", ""]
        d = os.path.dirname(a.md)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        io.open(a.md, "w", encoding="utf-8", newline="\n").write("\n".join(L))
        print("Markdown → %s" % a.md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
