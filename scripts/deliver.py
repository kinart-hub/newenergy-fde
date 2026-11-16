# -*- coding: utf-8 -*-
"""端到端连贯交付入口：result.json -> 四份互相印证的交付物 + 证据链。

用法：
  python scripts/deliver.py --result <dir-or-result.json> --out <out-dir>
  python scripts/deliver.py --check     # 判据自检（不产出文件）
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import deliver as D


def _check():
    """判据自检：每条断言都必须能被注入缺陷翻红。"""
    passed = 0
    failed = []

    def ck(cond, name):
        nonlocal passed
        if cond:
            passed += 1
        else:
            failed.append(name)

    # 造一个最小事实源（结构真实，值中性）
    res = {
        "target": "X(mm)", "process_name": "工序A", "process": "proc_a",
        "shape": [10, 3], "summary": ["s1"],
        "spc": [{"column": "X(mm)", "violations": [
            {"rule": 1, "indexes": [1, 2], "count": 2}]}],
        "pareto": {"top": "尺寸超差"},
        "rootcause": {"candidates": [
            {"param_name": "温度", "score": 0.7, "evidence_score": 0.72,
             "conclusion_status": "数据支持", "supported_by_data": True},
            {"param_name": "压力", "score": 0.4, "evidence_score": 0.35,
             "conclusion_status": "假设", "supported_by_data": False}]},
        "eightd": {"d4_ok": True,
                   "d5": {"rows": [{"category": "设备", "action": "点检温控"}]},
                   "d7": {"rows": [{"action": "修订SOP", "doc_no": "WI-01"}]},
                   "fmea": {"rows": [{"item": "失效模式", "content": "温度漂移"}]}},
    }
    chain = D.build_chain(res, source_path="result.json")
    ck(chain["d4_ok"] is True, "d4_ok 继承")
    ck(chain["lead_fact_id"] == "F-CAU-01", "榜首证据ID稳定")
    ck("F-SIG-01" in chain["facts"], "SPC判异入账")
    ck("F-SIG-P1" in chain["facts"], "Pareto入账")
    ck(chain["facts"]["F-ACT-01"]["responds_to"] == "F-CAU-01",
       "措施响应榜首根因")
    ck(chain["facts"]["F-SOP-01"]["responds_to"] == "F-CAU-01",
       "SOP响应榜首根因")

    ok, vio = D.validate_chain(chain, res)
    ck(ok and not vio, "一致事实源应通过校验")

    # 翻红用例1：d4_ok=False 却声称响应根因 → 编造，必须拦
    # 注意：直接篡改 chain（模拟 build_chain 写错），否则 build_chain 自己
    # 已经清空 responds_to，V2 根本没被触达 —— 假绿。
    bad = json.loads(json.dumps(res))
    bad["eightd"]["d4_ok"] = False
    c2 = D.build_chain(bad, source_path="result.json")
    c2["facts"]["F-ACT-01"]["responds_to"] = "F-CAU-01"  # 注入缺陷
    ok2, vio2 = D.validate_chain(c2, bad)
    ck((not ok2) and any("V2" in x for x in vio2), "未确认根因不得声称响应")

    # 翻红用例2：source 指向不存在的字段 → 新造事实，必须拦
    c3 = D.build_chain(res, source_path="result.json")
    c3["facts"]["F-ACT-01"]["source"] = "nonexistent.field"
    ok3, vio3 = D.validate_chain(c3, res)
    ck((not ok3) and any("V3" in x for x in vio3), "出处必须可解析")

    # 翻红用例3：chain_id 未出现在渲染正文 → 证据链不可见，必须拦
    c4 = D.build_chain(res, source_path="result.json")
    ok4, vio4 = D.validate_chain(c4, res, rendered={"01": "没有链号"})
    ck((not ok4) and any("V5" in x for x in vio4), "交付物须标注证据链")

    # 翻红用例4：D5措施目标 ≠ 榜首根因（跨阶段脱节），必须拦
    broken = json.loads(json.dumps(res))
    broken["eightd"]["d5"]["rows"][0]["target"] = "另一个根因"
    c6 = D.build_chain(broken, source_path="result.json")
    ok6, vio6 = D.validate_chain(c6, broken)
    ck((not ok6) and any("V7" in x for x in vio6), "D5措施须与榜首根因同源")

    # 无候选时不得假装有榜首根因
    empty = json.loads(json.dumps(res))
    empty["rootcause"]["candidates"] = []
    empty["eightd"]["d4_ok"] = False
    c5 = D.build_chain(empty, source_path="result.json")
    ck(c5["lead_fact_id"] is None, "无候选时榜首为空")

    # 事实源必须归档进交付目录（出处不能指向会消失的临时文件）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.json")
        with open(src, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False)
        out = os.path.join(td, "deliver")
        r = D.deliver(src, out)
        archived = os.path.join(out, "_source_result.json")
        ck(os.path.exists(archived), "事实源归档进交付目录")
        ch = json.load(open(r["chain_path"], encoding="utf-8"))
        ck(os.path.abspath(ch["source_result"]) == os.path.abspath(archived),
           "证据链出处指向归档副本")

    print("证据链判据自检：%d 通过 / %d 失败" % (passed, len(failed)))
    for name in failed:
        print("  [FAIL] %s" % name)
    return 0 if not failed else 2


def main(argv=None):
    ap = argparse.ArgumentParser(description="端到端连贯交付")
    ap.add_argument("--result", help="result.json 或其所在目录")
    ap.add_argument("--out", help="四份交付物输出目录")
    ap.add_argument("--check", action="store_true", help="判据自检")
    args = ap.parse_args(argv)

    if args.check:
        return _check()
    if not args.result or not args.out:
        ap.error("需给 --result 与 --out（或用 --check）")

    rp = args.result
    if os.path.isdir(rp):
        rp = os.path.join(rp, "result.json")
    if not os.path.exists(rp):
        print("结果文件不存在：%s" % rp)
        return 2

    r = D.deliver(rp, args.out)
    print("=" * 68)
    print("端到端连贯交付：证据链 %s" % r["chain_id"])
    print("=" * 68)
    print("登记事实 %d 条｜校验 %s" % (
        r["fact_count"], "通过" if r["ok"] else "未通过"))
    for x in r["violations"]:
        print("  ✗ %s" % x)
    for k, p in r["files"].items():
        print("  · %-10s %s" % (k, p))
    print("  · chain      %s" % r["chain_path"])
    print("=" * 68)
    # 校验不通过必须让调用方看见（退出码非0），不许静默出四份矛盾报告
    return 0 if r["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
