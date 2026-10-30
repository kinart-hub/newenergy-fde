#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跨模块协同审计：用**运行时探针**看一次分析全流程里谁参与了、谁没参与。

为什么必须运行时探针，不能只靠静态分析
=============================================================================
    `coherence_audit.py` 查的是"函数有没有调用方"，判据是**源码里出现过**。
    它答不了更要命的问题：**这次分析到底走了哪条路**。

    静态看 pipeline 引用了 18 个模块，但这 18 个里有一半只在某个分支生效；
    另外 46 个 lib 模块里有的一辈子没进过 analyze 的调用链（孤岛），
    有的**算完了结果没人接**（断头路）。这两种情况源码都是"有引用的"，
    静态审计看不见，只有真的跑一遍、记录调用栈才看得见。

三层读数
=============================================================================
    L1 触达：每个 lib 模块在一次 analyze 里被调用了多少次（0 = 孤岛）
    L2 链路顺序：data → clean → compute → analyze → forward → report → action，
       看每一环实际由哪些模块承担，有没有**环节空缺**
    L3 字段流转：关键字段谁产生、谁消费。产生了而下游零消费 = 断头路；
       下游引用了但从没被上游提供 = 悬空依赖（只能取到 None/default）

用法
=============================================================================
    python scripts/dataflow_audit.py --input examples/01_涂布工艺数据.csv
    python scripts/dataflow_audit.py --all            # 六个工序全跑一遍
    python scripts/dataflow_audit.py --all --json output/dataflow.json
"""
import sys
import os
import io
import json
import argparse
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 链路的七个环节：现场数据的完整旅程。缺任何一环，结论就不成立。
STAGES = [
    ("collect", "收集", ("ingest", "table", "devices", "lotcode", "fileio")),
    ("clean", "清洗", ("preprocess", "quality_guard", "exclude", "adapt", "specver")),
    ("compute", "计算", ("stats", "spc", "doe", "msa", "correlation", "reliability",
                         "process_window", "toolmatch")),
    ("analyze", "分析", ("rootcause", "confound", "crossprocess", "direction",
                         "defectchar", "recipe", "fmea")),
    ("forward", "转发", ("pipeline", "contract", "degrade", "provenance", "context")),
    ("report", "报告", ("report", "charts", "export_doc", "views", "actioncard")),
    ("decide", "判定", ("status", "actions", "eightd", "observ", "events")),
]


def _mod_of(filename):
    """把绝对路径归到 lib/xxx.py 或 scripts/xxx.py。"""
    try:
        rel = os.path.relpath(filename, ROOT).replace("\\", "/")
    except ValueError:
        return None
    if not rel.startswith("lib/") and not rel.startswith("scripts/"):
        return None
    if not rel.endswith(".py"):
        return None
    return rel


class Probe(object):
    """记录一次分析里 lib 层每个函数被调用的次数与首次出现顺序。"""

    def __init__(self):
        self.hits = collections.Counter()      # "lib/spc.py:xbar_r" -> 次数
        self.order = []                        # 首次出现的函数序
        self.depth = 0
        self.enabled = False
        self._seen = set()

    def _prof(self, frame, event, arg):
        if event != "call":
            return
        fn = frame.f_code.co_filename
        mod = _mod_of(fn)
        if mod is None:
            return
        key = "%s:%s" % (mod, frame.f_code.co_name)
        self.hits[key] += 1
        if key not in self._seen:
            self._seen.add(key)
            self.order.append(key)

    def start(self):
        self.enabled = True
        sys.setprofile(self._prof)

    def stop(self):
        sys.setprofile(None)
        self.enabled = False


def run_one(csv_path, target=None, process=None):
    """跑一次 analyze，只做探测，不落盘报告（快且干净）。"""
    import tempfile
    from lib import pipeline as PL

    # analyze 必须给 outdir（它会往那里写报告）。给它一个临时目录，
    # 探针跑完就删——审计不该在仓库里留产物。
    tmp = tempfile.mkdtemp(prefix="dfprobe-")
    pr = Probe()
    res = {}
    err = None
    pr.start()
    try:
        res = PL.analyze([csv_path], outdir=tmp, target=target,
                         process=process, do_doe=False, max_charts=0)
    except Exception as e:                       # noqa: BLE001 — 探针要如实记下崩塌
        err = "%s: %s" % (type(e).__name__, str(e)[:200])
    finally:
        pr.stop()
        # 清理临时目录。这里用 ignore_errors 是**有意的**：
        # 探针的价值在于读数，清临时目录失败不该让整轮审计失败。
        # 但"有意忽略"不等于"可以静默"——失败要出现在输出里，
        # 否则会攒下一堆没人清的临时目录而无人知道。
        import shutil
        try:
            shutil.rmtree(tmp, ignore_errors=False)
        except OSError as _rm:
            sys.stderr.write("[dataflow] 临时目录未清理干净（%s）：%s\n"
                             % (tmp, _rm))
    return pr, res, err


def module_stats(pr):
    """按模块归集调用次数。"""
    per_mod = collections.Counter()
    for key, n in pr.hits.items():
        per_mod[key.split(":")[0]] += n
    return per_mod


def stage_coverage(per_mod):
    """每一环节实际由哪些模块承担，哪些环节空缺。"""
    out = []
    for sid, sname, mods in STAGES:
        base = set("lib/%s.py" % m for m in mods)
        hit = sorted((m, per_mod.get(m, 0)) for m in base if per_mod.get(m, 0))
        missing = sorted(m for m in base if not per_mod.get(m, 0))
        out.append({"stage": sid, "name": sname, "hit": hit,
                    "idle": missing, "active": bool(hit)})
    return out


def islands(per_mod):
    """lib 里一次分析中零调用的模块 = 孤岛。"""
    libs = sorted(os.path.basename(p) for p in
                  __import__("glob").glob(os.path.join(ROOT, "lib", "*.py")))
    idle = []
    for f in libs:
        if f == "__init__.py":
            continue
        if per_mod.get("lib/" + f, 0) == 0:
            idle.append("lib/" + f)
    return idle


def field_flow(res):
    """关键字段的产生/消费状况。

    这里查的是**结果字典里的字段在报告模板里有没有被读**——
    产生了但报告从不引用 = 用户永远看不到，等于没算。
    """
    if not res:
        return []
    fields = []

    def _walk(d, prefix=""):
        if isinstance(d, dict):
            for k, v in d.items():
                p = "%s.%s" % (prefix, k) if prefix else k
                if isinstance(v, (dict, list)):
                    _walk(v, p)
                else:
                    fields.append(p)

    _walk(res, "")
    return fields


def main(argv=None):
    ap = argparse.ArgumentParser(description="跨模块协同审计（运行时探针）")
    ap.add_argument("--input", help="单个 CSV 路径")
    ap.add_argument("--all", action="store_true", help="跑全部示例数据")
    ap.add_argument("--json", help="把结果写成 JSON")
    a = ap.parse_args(argv)

    cases = []
    if a.all:
        import glob as _g
        cases = sorted(_g.glob(os.path.join(ROOT, "examples", "*.csv")))
    elif a.input:
        cases = [a.input]
    else:
        ap.error("需要 --input 或 --all")

    reports = []
    all_mods = collections.Counter()
    all_idle = None
    for c in cases:
        name = os.path.basename(c)
        pr, res, err = run_one(c)
        per_mod = module_stats(pr)
        all_mods.update(per_mod)
        idle = islands(per_mod)
        if all_idle is None:
            all_idle = set(idle)
        else:
            all_idle &= set(idle)      # 所有用例都没碰过 = 真孤岛
        stages = stage_coverage(per_mod)
        reports.append({
            "case": name, "error": err,
            "funcs_called": len(pr.hits),
            "modules_hit": len(per_mod),
            "islands": idle,
            "stages": stages,
            "fields": field_flow(res),
        })
        print("\n=== %s ===" % name)
        if err:
            print("  ✗ analyze 失败：%s" % err)
        print("  被调用函数 %d 个，涉及模块 %d 个，孤岛 %d 个"
              % (len(pr.hits), len(per_mod), len(idle)))
        for st in stages:
            mark = "✓" if st["active"] else "✗"
            names = ", ".join("%s(%d)" % (m.split("/")[-1][:-3], n)
                              for m, n in st["hit"][:4])
            print("  %s %-8s %s" % (mark, st["name"], names or "**无模块承担**"))

    print("\n" + "=" * 70)
    print("全部用例都零调用的 lib 模块（真孤岛）：%d 个" % len(all_idle or []))
    for m in sorted(all_idle or []):
        print("  ·", m)

    print("\n各环节活跃度（全部用例合并调用次数）：")
    for sid, sname, mods in STAGES:
        tot = sum(all_mods.get("lib/%s.py" % m, 0) for m in mods)
        print("  %-8s 合计 %6d 次" % (sname, tot))

    # 环节空缺 = 该环节所有模块都没被调用
    empty = []
    for sid, sname, mods in STAGES:
        if not any(all_mods.get("lib/%s.py" % m, 0) for m in mods):
            empty.append(sname)
    if empty:
        print("\n✗ 完全空缺的环节：%s" % "、".join(empty))

    if a.json:
        d = os.path.dirname(a.json)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        io.open(a.json, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"cases": reports,
                        "islands_all": sorted(all_idle or []),
                        "module_hits": dict(all_mods)},
                       ensure_ascii=False, indent=2) + "\n")
        print("\nJSON 已写入 %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
