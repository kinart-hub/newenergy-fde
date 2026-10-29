# -*- coding: utf-8 -*-
"""
复核修复验证（rev fix verification）—— 审计证据脚本，可入库。

覆盖本轮 4 处真实缺陷修复：
  F1  rootcause 诚实降级泄漏：无数据支撑时不得输出根因榜单（只降级为 prior_only_hints）
  F2  rootcause 时序反证死分支：_temporal_match 必须返回 verdict，消费方读得到并出反证
  F3  doe decode_design 2 水平高端点：默认 2 水平 DOE 解不出高角点
  F4  crossprocess probe_margin 参考量程：用真实 pkey 而非写死空 meta 拿参考量程

用法：
  python scripts/verify_rev_fixes.py            # 仅打印
  python scripts/verify_rev_fixes.py --check    # 门禁模式，任一 FAIL 返回非 0
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import doe, crossprocess as CP, rootcause as RC, table as T  # noqa: E402


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print("[%s] %s %s" % (mark, name, ("—— " + detail) if detail else ""))
    return cond


def test_f1_rootcause_honest_degradation():
    """target_col=None 且无匹配数据列 → 候选榜单必须清空，只给 prior_only_hints。"""
    # 表列故意不与任何 likely_param 别名匹配，确保全部 INSUFFICIENT_DATA
    tb = T.Table(["时间", "批号", "备注"],
                 [[1, "A01", "x"], [2, "A02", "y"], [3, "A03", "z"]])
    res = RC.rank_root_causes("极片错位", tb, target_col=None)
    cands = (res or {}).get("candidates") or []
    hints = (res or {}).get("prior_only_hints") or []
    ok = (res is not None) and (len(cands) == 0) and (len(hints) > 0)
    return check("F1 rootcause 诚实降级（无数据支撑不吐榜单）", ok,
                 "candidates=%d prior_only_hints=%d" % (len(cands), len(hints)))


def test_f2_temporal_verdict():
    """_temporal_match 返回 verdict 结构；反相数据判"不支持"并经消费方生成时序反证。"""
    cols = ["p", "y"]
    # 反相：参数与指标异常时段不重合 → 应判"不支持"
    rows_inv = [[100.0 if i < 15 else 0.0, 0.0 if i < 15 else 100.0] for i in range(30)]
    tb_inv = T.Table(cols, rows_inv)
    sc_inv, det_inv = RC._temporal_match(tb_inv, "p", "y")
    struct_ok = (det_inv.get("verdict") in ("支持", "不支持")) and ("note" in det_inv)
    inv_ok = (det_inv.get("verdict") == "不支持")
    # 整合验证：构造一个 data_backed 候选，temporal 带"不支持" verdict，
    # 走 _annotate_evidence 看是否产出时序反证（修复前该分支查不到 verdict 键，死分支）。
    cand = {
        "param_key": "p1", "param_name": "某参数", "detail": {
            "status": RC.ST.SUCCESS, "r": 0.6, "p": 0.01, "column": "p"},
        "temporal": det_inv,
    }
    RC._annotate_evidence([cand], tb_inv, "y", {})
    counter_has_temporal = any("时序上不支持" in c for c in (cand.get("counter_evidence") or []))
    ok = struct_ok and inv_ok and counter_has_temporal
    return check("F2 _temporal_match 返回 verdict 且消费方生成反证", ok,
                 "verdict=%s 反证=%s" % (det_inv.get("verdict"), counter_has_temporal))


def test_f3_doe_decode():
    """2 水平设计：水平号 2 即高；3 水平设计：2 仍是中点。"""
    fs = [{"name": "A", "low": 3.0, "high": 5.0},
          {"name": "B", "low": 10.0, "high": 20.0}]
    mat2 = doe.full_factorial([2, 2])  # 2^2，水平号仅 1/2
    dec2 = doe.decode_design(mat2, fs)
    has_high = any(d["A"] == 5.0 and d["B"] == 20.0 for d in dec2)
    fs3 = [{"name": "A", "low": 3.0, "mid": 4.0, "high": 5.0}]
    dec3 = doe.decode_design([[1], [2], [3]], fs3)
    ok3 = dec3[0]["A"] == 3.0 and dec3[1]["A"] == 4.0 and dec3[2]["A"] == 5.0
    return check("F3 decode_design 2水平解高角点 / 3水平中点正确", has_high and ok3,
                 "2level_has_high=%s 3level_ok=%s" % (has_high, ok3))


def test_f4_probe_margin_pkey():
    """probe_margin 用真实 pkey 才能拿到知识库参考量程（旧写死空 meta 拿不到）。

    关键差异：param_col 用中文别名「一区温度」，其裸名不等于 kb 键 zone1_temp。
    旧代码写死 _base_param_of(param_col, [{}]) 只能剥出裸列名「一区温度」，
    永远查不到 kb 键 → 参考量程恒定缺失；修复后由 trace 传入真实 pkey=zone1_temp。
    """
    pc = "一区温度"  # 裸名≠ kb 键
    rows, bad_idx, good_idx = [], [], []
    for i in range(40):
        if i < 20:
            bad_idx.append(i)
            rows.append([180.0 + (i % 3)])
        else:
            good_idx.append(i)
            rows.append([120.0 + (i % 3)])
    tb = T.Table([pc], rows)
    kb = {"zone1_temp": {"control": {"lsl": 100.0, "usl": 200.0}, "process": "烘箱"}}
    out = CP.probe_margin(tb, pc, bad_idx, good_idx, spec=None,
                          kb_params=kb, pkey="zone1_temp")
    ref_ok = bool(out and out.get("reference"))
    # 旧路径对照：pkey=None → 写死空 meta 只能剥出「一区温度」，查不到 kb 键
    out_old = CP.probe_margin(tb, pc, bad_idx, good_idx, spec=None, kb_params=kb, pkey=None)
    ref_old = bool(out_old and out_old.get("reference"))
    ok = ref_ok and (out.get("verdict") == "no_spec_cannot_clear") and (not ref_old)
    detail = "ref(真实pkey)=%s verdict=%s | ref(旧空meta兜底)=%s(应为None)" % (
        out.get("reference") if out else None,
        out.get("verdict") if out else None, ref_old)
    return check("F4 probe_margin 真实 pkey 拿参考量程", ok, detail)


def main():
    print("=== 复核修复验证 ===")
    results = [
        test_f1_rootcause_honest_degradation(),
        test_f2_temporal_verdict(),
        test_f3_doe_decode(),
        test_f4_probe_margin_pkey(),
    ]
    n_fail = sum(1 for r in results if not r)
    print("=== 汇总：%d 项，%d 失败 ===" % (len(results), n_fail))
    return n_fail


if __name__ == "__main__":
    fail = main()
    if "--check" in sys.argv:
        sys.exit(1 if fail else 0)
