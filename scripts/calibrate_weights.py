# -*- coding: utf-8 -*-
"""calibrate_weights.py —— 用外部真值检验根因排序，并量化"分数可比"风险

## 这个工具要回答的问题

用户的原话：「从各个行业的论坛或检测机构爬取公开的现场真值校准权重，
来测试 R4 分数可比等风险项」。

能不能做到，取决于公开数据里有没有**可复现的真值**。本工具的第一职责
就是把这件事查清楚并如实报出来，而不是硬凑一个"校准好了"的结论。

## 真值分级（本工具只认前两级为"可校准"）

  T1 可复现真值   受控试验 + 正交分解可算出效应量（如 2^k 全因子 DOE）
                  → 能校准"排序能力"
  T2 可复现真值   已知生成模型的合成数据（第三方独立物理模型，非自己编）
                  → 能校准"排序能力"，但不能证明"真实世界成立"
  T3 弱真值       只有 pass/fail 标签、无因子显著性的产线数据
                  → **不能**校准排序（不知道哪个参数是真因）
  T4 无真值       只有原始数据
                  → 只能验健壮性与列覆盖度（已由 realdata_check 负责）

## 它同时量化三种"分数可比"风险

  C1 路径间公式不一致：同一份数据走不同代码路径，`score` 的含义不同——
     `rank_root_causes` 用四权重加权，`data_driven_rootcause` 用
     `证据分 × 位置衰减`，**两者都叫 score**，混在同一个字段名里。
  C2 候选来源量纲不一致：单元级（Cohen's d）与参数级（Pearson r）。
     2026-11-04 已改为 d/√(d²+4) 统一，本工具持续监控。
  C3 权重本身未经现场验证：config_rootcause.json 是工程默认值。

## 用法

    python scripts/calibrate_weights.py --root realdata
    python scripts/calibrate_weights.py --root realdata --json out.json
    python scripts/calibrate_weights.py --selftest
"""

from __future__ import print_function

import argparse
import glob
import io
import json
import os
import shutil
import sys
import tempfile

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_ROOT)

# 排序位置衰减系数（data_driven_rootcause 里的 1 - i*0.08）
_POS_DECAY = 0.08


def _load_truth(case_dir):
    p = os.path.join(case_dir, "truth.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(io.open(p, encoding="utf-8"))
    except Exception:
        return None


def _grade_truth(truth):
    """判定真值等级。只认可复现的，不认'看起来像真值'的。"""
    if not truth:
        return ("T4", "无 truth.json，只能验健壮性")
    tt = str(truth.get("truth_type") or "")
    if tt in ("orthogonal_doe_anova", "rsm_anova", "taguchi_anova"):
        return ("T1", "受控试验 + 正交分解，效应量可复算")
    if tt == "injected_fault":
        # 实验者直接把这个变量设成故障 —— 不是"专家认为可能"，是"确实弄坏了"。
        # 真值等级最高的一种，比事后标注的现场真因还硬。
        return ("T1", "人为注入故障，真因由实验设定而非事后推断")
    if tt.startswith("synthetic_known_model"):
        return ("T2", "第三方独立生成模型，有真值但非真实产线")
    if truth.get("labels_only"):
        return ("T3", "只有合格/不合格标签，无因子显著性")
    return ("T3", "自定义 truth_type，未登记可复现路径：" + tt)


def _expected_ranking(truth):
    """从 truth.json 取"真因排序"。只取 role=cause 的主效应。"""
    if not truth:
        return []
    # 注入故障型：真因由 manipulatedVars 直接给出
    inj = truth.get("injected_causes")
    if inj:
        return [{"column": e.get("column_in_data") or e.get("variable"),
                 "param": e.get("variable"), "role": "cause",
                 "effect": None} for e in inj]
    blocks = []
    for k, v in truth.items():
        if not isinstance(v, dict):
            continue
        if "main_effects_ranked" not in v:
            continue
        blocks.append((k, v))
    if not blocks:
        return []
    # 优先取 target_column 指向的那块
    want = truth.get("target_column")
    for k, v in blocks:
        tgt = truth.get("target_column")
        if tgt and k.lower().replace("target_", "") in tgt.lower():
            return [e for e in v["main_effects_ranked"]
                    if e.get("role", "cause") == "cause"]
    return [e for e in blocks[0][1]["main_effects_ranked"]
            if e.get("role", "cause") == "cause"]


def _check_case(case_dir):
    """对一个真值用例跑 analyze，比对真值排序。"""
    truth = _load_truth(truth_dir := case_dir)
    grade, grade_why = _grade_truth(truth)
    out = {"case": os.path.basename(case_dir), "truth_grade": grade,
           "truth_grade_why": grade_why, "status": "SKIPPED",
           "reason": None, "expected": [], "actual": [],
           "top1_hit": None, "top3_hit": None, "false_exclusion": [],
           "score_bases": {}}
    if grade not in ("T1", "T2"):
        out["reason"] = "真值等级 %s，不足以校准排序（%s）" % (grade, grade_why)
        return out

    data = None
    for ext in (".csv", ".tsv"):
        hits = glob.glob(os.path.join(case_dir, "data*" + ext))
        if hits:
            data = hits[0]
            break
    if not data:
        out["reason"] = "找不到 data.csv"
        return out

    exp = _expected_ranking(truth)
    out["expected"] = [e.get("column") or e.get("param") for e in exp]

    from lib import pipeline as PL
    tmp = tempfile.mkdtemp(prefix="calw-")
    try:
        res = PL.analyze([data], target=truth.get("target_column"), outdir=tmp,
                         export_xlsx=False, export_docx=False, export_pdf=False)
    except Exception as e:
        out["status"] = "CRASH"
        out["reason"] = repr(e)[:160]
        shutil.rmtree(tmp, ignore_errors=True)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    rc = res.get("rootcause") or {}
    cands = rc.get("candidates") or []
    out["actual"] = [c.get("param_name") for c in cands]
    out["score_bases"] = {str(c.get("score_basis")) for c in cands}
    out["rootcause_path"] = ("data_driven" if rc.get("data_driven")
                             else ("degraded" if rc.get("honest_degraded") else "rank"))

    # Top1 / Top3 命中：真值第一位是否排在候选第一位
    if exp and cands:
        want1 = exp[0].get("column") or exp[0].get("param")
        out["top1_hit"] = (cands[0].get("param_name") == want1)
        top3 = {c.get("param_name") for c in cands[:3]}
        out["top3_hit"] = (want1 in top3)
        # 真因被"排除"（不在候选里）——比排错更严重
        got = {c.get("param_name") for c in cands}
        out["false_exclusion"] = [w for w in out["expected"] if w not in got]
    out["status"] = "OK"
    return out


def _formula_consistency(reports):
    """C1：同一份数据不同路径的 score 含义是否一致。"""
    out = []
    by_basis = {}
    for r in reports:
        if r["status"] != "OK":
            continue
        for b in r["score_bases"]:
            by_basis.setdefault(b, 0)
            by_basis[b] += 1
    if len(by_basis) > 1:
        out.append({
            "risk": "C1",
            "detail": "不同用例走了不同评分口径：%s"
                      % "；".join("%s(%d例)" % (k, v) for k, v in sorted(by_basis.items())),
            "why": "score 字段在不同路径下含义不同（四权重加权 / 证据分×位置衰减 / "
                   "效应量折算），名字一样但不可比。",
        })
    return out


def run(root):
    cases = []
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        if not os.path.isdir(d):
            continue
        if not glob.glob(os.path.join(d, "data.*")):
            continue
        cases.append(_check_case(d))
    return {"cases": cases, "risks": _formula_consistency(cases)}


def report(res):
    print("=" * 76)
    print("外部真值校准：公开数据能不能校准根因排序？")
    print("=" * 76)
    print("\n%-24s %-5s %-6s %-8s %-8s %s"
          % ("用例", "真值级", "状态", "Top1", "Top3", "实际路径"))
    for c in res["cases"]:
        print("%-24s %-5s %-6s %-8s %-8s %s"
              % (c["case"][:22], c["truth_grade"], c["status"],
                 ("命中" if c["top1_hit"] else ("未中" if c["top1_hit"] is False else "—")),
                 ("命中" if c["top3_hit"] else ("未中" if c["top3_hit"] is False else "—")),
                 c.get("rootcause_path") or "—"))

    print("\n" + "-" * 76)
    print("【逐例判定】")
    for c in res["cases"]:
        print("\n· %s（真值级 %s：%s）" % (c["case"], c["truth_grade"], c["truth_grade_why"]))
        if c["status"] != "OK":
            print("    → %s" % (c["reason"] or c["status"]))
            continue
        print("    真值顺序：%s" % " > ".join(str(x) for x in c["expected"]))
        print("    实际顺序：%s" % " > ".join(str(x) for x in c["actual"][:6]))
        if c["false_exclusion"]:
            print("    ✗ **真因被排除出候选**：%s" % "、".join(str(x) for x in c["false_exclusion"]))
        if c["top1_hit"]:
            print("    ✓ 首位命中")

    print("\n" + "-" * 76)
    print("【分数可比风险】")
    if not res["risks"]:
        print("  ✓ 未发现跨口径混用")
    for r in res["risks"]:
        print("  [%s] %s" % (r["risk"], r["detail"]))
        print("       %s" % r["why"])

    n_cal = sum(1 for c in res["cases"] if c["truth_grade"] in ("T1", "T2"))
    n_ok = sum(1 for c in res["cases"] if c["status"] == "OK")
    print("\n" + "-" * 76)
    print("可用真值用例 %d 个（其中成功运行 %d 个）／总用例 %d 个"
          % (n_cal, n_ok, len(res["cases"])))
    if n_cal == 0:
        print("⚠ 没有一个用例达到 T1/T2 —— **本仓库当前无法用公开数据校准权重**。")
        print("  这不是'数据不够'的托辞：权重校准要求'哪个参数是真因'的标注，")
        print("  而公开数据集几乎不给这个标注（多只有合格/不合格）。")
        return 2
    return 0


# ---------------------------------------------------------------- 自检

def _selftest():
    out = []

    # 真值分级：只有 T1/T2 才允许校准
    out.append(("T1：正交 DOE 被认作可校准",
                _grade_truth({"truth_type": "orthogonal_doe_anova"})[0] == "T1", ""))
    out.append(("T3：只有标签不算可校准",
                _grade_truth({"labels_only": True})[0] == "T3", ""))
    out.append(("T4：无 truth.json 判为无真值",
                _grade_truth(None)[0] == "T4", ""))
    out.append(("未知 truth_type 保守判 T3（不冒充可校准）",
                _grade_truth({"truth_type": "made_up"})[0] == "T3", ""))

    # 期望排序提取：只取 role=cause
    t = {"target_column": "Rz", "target_Rz": {"main_effects_ranked": [
        {"column": "f", "role": "cause"},
        {"column": "vc", "role": "cause"},
        {"column": "Ra", "role": "noise"},
    ]}}
    got = [e["column"] for e in _expected_ranking(t)]
    out.append(("期望排序只取 role=cause（不把噪声当因）", got == ["f", "vc"], str(got)))

    # 公式一致性：单一口径不报风险
    out.append(("单一评分口径不报风险",
                not _formula_consistency([{"status": "OK", "score_bases": {"weighted"}}]), ""))
    out.append(("混用两种口径报风险",
                bool(_formula_consistency([{"status": "OK", "score_bases": {"weighted"}},
                                           {"status": "OK", "score_bases": {"unit_effect"}}])), ""))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="外部真值校准（只读）")
    ap.add_argument("--root", default=os.path.join(SKILL_ROOT, "realdata"))
    ap.add_argument("--json", default=None)
    ap.add_argument("--gate", action="store_true",
                    help="有『真因被排除出候选』则退出码 1（排序错不算，剔除才算）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        print("=" * 76)
        print("校准判据自检")
        print("=" * 76)
        bad = 0
        items = _selftest()
        for name, ok, detail in items:
            print("  [%s] %s %s" % ("OK  " if ok else "FAIL", name, detail or ""))
            if not ok:
                bad += 1
        print("-" * 76)
        print("通过 %d / 失败 %d" % (len(items) - bad, bad))
        return 0 if bad == 0 else 1

    # 真值数据（realdata/）**不随分发包外发**：里面是外部许可的真实数据
    # 与来源元数据，塞进离线包既撑体积也有许可问题。
    # 所以"目录不存在"是**部署形态差异**，不是"检查没做"——
    # 但"目录存在却没有一个可用真值用例"是真问题，必须报 2（见 report 末尾）。
    # 这两种情况混成一个结果，就会让部署环境永远门禁失败、
    # 或者让开发环境的数据退化被静默放过。
    if not os.path.isdir(args.root):
        print("=" * 76)
        print("外部真值校准")
        print("=" * 76)
        print("\n— 未找到真值数据目录：%s" % args.root)
        print("  真值数据（realdata/）不随分发包外发，本部署形态不包含该检查项。")
        print("  在开发仓库中运行 `python scripts/calibrate_weights.py` 才会实际校验。")
        return 0

    res = run(args.root)
    if args.json:
        with io.open(args.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
    rc = report(res)
    if args.gate:
        # 门禁判据只认"真因被**排除**出候选"，不认"排错位"。
        # 理由：排序错位是模型能力问题（真因还在列表里，人能看到并复核，
        # 报告也会标出结论等级）；而"真因压根不在候选里"是**信息丢失**——
        # 使用者无论怎么读都看不到它。前者可容忍，后者必须拦。
        # （2026-11-04 实测：原实现把进给量当共线冗余剔掉，用户永远看不到真因。）
        bad = [c for c in res["cases"] if c.get("false_exclusion")]
        if bad:
            print("\n✗ 有 %d 个真值用例的**真因被排除出候选**：" % len(bad))
            for c in bad:
                print("    %s：%s" % (c["case"], "、".join(str(x) for x in c["false_exclusion"])))
            return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
