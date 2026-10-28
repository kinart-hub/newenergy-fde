#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端接线验证：主流程跑完，报告里必须真的有那几块能力。

为什么需要它（2026-10-29 踩出来的教训）
--------------------------------------
`lib/pipeline.py` 里同一个别名 `AC` 被绑了两次：`actions as AC`（顶部）与
`actioncard as AC`（P2-1 那节）。后绑的顶掉先绑的，于是

    result["lateral"] = AC.lateral_plan(...)

实际打到 actioncard 上 → AttributeError → 被跨工序那节的 `except` 吞掉 →
写进 read_errors 一句「跨工序追溯降级」就完事。

后果：横向展开（D7 预防再发）、永久措施、预防措施**三块能力在真实报告里
从来没有出现过**，而 selftest 全绿——因为 selftest 是 `from lib import actions`
直接调函数，压根不走 pipeline 那条线。

所以这个脚本不测函数对不对，只测一件事：
**从 `analyze()` 出来的 result 里，这些键到底在不在。**

用法
----
    python scripts/e2e_wiring_check.py             # 跑全部用例
    python scripts/e2e_wiring_check.py --json o.json
"""

import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

EX = os.path.join(ROOT, "examples")

# (用例名, 目录, 文件顺序, 目标列, 本工序, 缺陷, 规格覆盖)
CASES = [
    ("B-上游不合格流入", "crossprocess/B",
     ["01_上游-注液.csv", "02_本工序-化成.csv"],
     "自放电(mV/d)", "battery_formation", "自放电超标",
     {"moisture_before_filling": {"lsl": 0, "usl": 25}}),
    ("C-上游合格但边缘", "crossprocess/C",
     ["01_上游-涂布.csv", "02_本工序-化成.csv"],
     "容量标准差(%)", "battery_formation", "容量一致性差",
     {"areal_density": {"lsl": 102, "usl": 108}}),
    # 单文件流程：跨工序那节不适用，走的是"本工序内根因"那条路径。
    # 不放进来，单文件这条主路径上的能力块就没人盯着。
    ("单文件-涂布", "", ["01_涂布工艺数据.csv"], "面密度", "battery_coating",
     None, None),
    ("单文件-含停机换卷段", "", ["10_含停机换卷段数据.csv"], "面密度",
     "battery_coating", None, None),
]

# 单文件用例没有上游，跨工序三块本就不适用，只盯这些
REQUIRED_SINGLE = [
    ("spc", "SPC 判异", lambda r: bool(r.get("spc"))),
    ("corr_columns", "参与相关的列", lambda r: bool(r.get("corr_columns"))),
    ("rootcause", "根因候选", lambda r: bool(r.get("rootcause"))),
]

# 降级记录里出现这些字样，说明**代码错了**而不是数据不好。
# 数据类降级（样本不足/规格缺失）是正常业务信号；代码类降级是 bug，
# 而且它长得和前者一模一样——都只是一句"某节降级"。
CODE_ERROR_MARKS = ("has no attribute", "AttributeError", "NameError",
                    "TypeError", "ImportError", "not defined",
                    "module '", "unexpected keyword", "takes")

# 主流程必须产出的能力块：键 -> (人话, 判定函数)
REQUIRED = [
    ("crossprocess", "跨工序追溯", lambda r: bool(r.get("crossprocess"))),
    ("lateral", "横向展开（D7 预防再发）",
     lambda r: bool((r.get("lateral") or {}).get("rows"))),
    ("permanent", "永久措施候选",
     lambda r: bool((r.get("permanent") or {}).get("generic"))),
    ("preventive", "预防措施参数",
     lambda r: bool((r.get("preventive") or {}).get("params"))),
]


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="主流程接线端到端验证")
    ap.add_argument("--json", default=None, help="结果落盘")
    args = ap.parse_args(argv)

    from lib import pipeline

    rows = []
    for name, sub, files, target, cur, defect, spec in CASES:
        base = os.path.join(EX, sub) if sub else EX
        paths = [os.path.join(base, f) for f in files]
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            rows.append({"case": name, "ok": False,
                         "detail": "样例文件缺失：%s" % missing})
            continue
        single = len(files) == 1
        tag = name.split("-")[0].replace("/", "_")
        outdir = os.path.join(ROOT, "output", "_wiring_e2e", tag)
        try:
            res = pipeline.analyze(paths, outdir=outdir, target=target,
                                   process=cur, defect=defect,
                                   spec_overrides=spec, do_doe=False,
                                   max_charts=0)
        except Exception as e:
            rows.append({"case": name, "ok": False,
                         "detail": "analyze 抛异常：%r" % e})
            continue
        # 降级记录：数据类降级是业务信号，代码类降级是 bug，必须分开看
        notes = []
        for key in ("read_errors", "degraded_sections"):
            for e in (res.get(key) or []):
                txt = str(e.get("error") or e.get("detail") or e)
                notes.append(txt)
        for w in (res.get("warnings") or []):
            notes.append(str(w))
        code_errs = [t for t in notes
                     if any(m in t for m in CODE_ERROR_MARKS)]
        want = REQUIRED_SINGLE if single else REQUIRED
        got = {}
        for key, human, test in want:
            got[key] = bool(test(res))
        rows.append({"case": name,
                     "ok": all(got.values()) and not code_errs,
                     "blocks": got,
                     "degraded": sorted(set(code_errs))[:5],
                     "all_notes": len(notes)})

    n_ok = sum(1 for r in rows if r.get("ok"))
    print("=" * 72)
    print("主流程接线端到端验证：%d/%d 用例通过" % (n_ok, len(rows)))
    print("=" * 72)
    for r in rows:
        print("  [%s] %s" % ("OK" if r.get("ok") else "FAIL", r["case"]))
        if r.get("detail"):
            print("        %s" % r["detail"])
        for key, human, _t in (REQUIRED_SINGLE if r["case"].startswith("单文件")
                               else REQUIRED):
            if "blocks" in r:
                print("        %s %s：%s" % (
                    "有" if r["blocks"].get(key) else "缺", human, key))
        if r.get("degraded"):
            print("        降级记录：%s" % r["degraded"])
    if args.json:
        with io.open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    return 0 if n_ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
