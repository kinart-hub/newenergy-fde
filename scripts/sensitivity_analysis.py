# -*- coding: utf-8 -*-
"""
sensitivity_analysis.py —— 根因排序权重的敏感性分析

回答一个问题：**换一套权重，根因排名会不会翻？**
会翻 = 这份排序对权重敏感，报告里就不该把第一名当成"结论"；
不会翻 = 排序由数据本身撑着，权重只是微调。

⚠ 能力边界（不要过度解读，这是本脚本必须自己说清的事）：
  它证明的是"排序稳不稳"，**不是"权重对不对"**。
  判断权重是否正确需要现场真值（谁才是真因），本脚本没有也不需要那个数据。
  所以输出里绝不出现"最优权重""已验证"这类词——那是要靠真值才配说的话。

用法：
  # 用默认网格，对一份数据做权重敏感性扫描
  python scripts/sensitivity_analysis.py --input examples/01_涂布工艺数据.csv \
      --defect 极片错位 --target 面密度

  # 只检查当前配置文件会给出什么，不扫网格
  python scripts/sensitivity_analysis.py --show-config

  # 指定输出目录（默认写到 stdout，不落盘）
  python scripts/sensitivity_analysis.py --input ... --outdir output/sens

退出码：0 正常；2 参数/数据问题（不能当成"排序稳定"）。
"""

import argparse
import itertools
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
    # scan:keep stdout 编码兜底在模块加载期执行，degrade 尚未导入；且非 Windows 环境没有 reconfigure 属正常分支，不是降级。

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib import rootcause as RC          # noqa: E402
from lib import table as T               # noqa: E402
from lib import preprocess as PP         # noqa: E402


# 权重网格：不是"随便取几组"，而是按"证据 vs 先验"这条主轴排布。
# 根因排序最容易出错的地方，就是数据证据不足时先验把候选顶到榜首，
# 所以真正要测的是"证据权重往下压时，榜首会不会被先验候选抢走"。
GRID = [
    {"evidence": 0.40, "prior": 0.35, "temporal": 0.15, "verifiability": 0.10},  # 内置默认
    {"evidence": 0.60, "prior": 0.20, "temporal": 0.10, "verifiability": 0.10},  # 重证据
    {"evidence": 0.25, "prior": 0.50, "temporal": 0.15, "verifiability": 0.10},  # 重先验
    {"evidence": 0.50, "prior": 0.30, "temporal": 0.10, "verifiability": 0.10},  # 证据略重
    {"evidence": 0.34, "prior": 0.33, "temporal": 0.23, "verifiability": 0.10},  # 时序抬高
    {"evidence": 0.45, "prior": 0.30, "temporal": 0.15, "verifiability": 0.10},  # 微调中位
]

GRID_NAMES = ["默认(0.40/0.35/0.15/0.10)", "重证据(0.60/0.20)", "重先验(0.25/0.50)",
              "证据略重(0.50/0.30)", "时序抬高(0.34/0.33/0.23)", "微调中位(0.45/0.30)"]


def _load_table(path):
    """读成 Table。用工程自带的 T.read_any，不另写一套解析口径——
    另写一套就会和主流程的编码/分隔符/表头推断规则分叉，
    敏感性分析得出的结论与真实报告不是同一份数据。"""
    if not os.path.isfile(path):
        raise SystemExit("× 找不到输入文件：%s" % path)
    return T.read_any(path)


def _pick_table(maybe):
    """ingest 的返回可能是 (table, meta) 或 table 列表，这里做一次归一。"""
    if maybe is None:
        return None
    if getattr(maybe, "columns", None) is not None:
        return maybe
    if isinstance(maybe, (list, tuple)):
        for item in maybe:
            if getattr(item, "columns", None) is not None:
                return item
            if isinstance(item, (list, tuple)):
                t = _pick_table(item)
                if t is not None:
                    return t
        return None
    if isinstance(maybe, dict):
        for v in maybe.values():
            t = _pick_table(v)
            if t is not None:
                return t
    return None


def rank_with(table, defect, target, col_map, w):
    """用指定权重跑一次排序，返回 [(param_name, score)] 按分数降序。"""
    res = RC.rank_root_causes(defect, table, target, top=10,
                              col_map=col_map, weights=w)
    if not isinstance(res, dict):
        return None, "排序未返回结果（缺陷名不在知识库且无列映射？）"
    cands = res.get("candidates") or []
    if not cands:
        return None, "没有候选可排（status=%s）" % (res.get("status"),)
    return [(c.get("param_name") or c.get("param_key"), c.get("score")) for c in cands], None


def top1_agree(seqs):
    """各权重组合之间，Top-1 的一致比例（以第一个组合为基准）。"""
    tops = [s[0][0] for s in seqs if s and s[0]]
    if not tops:
        return 0.0, []
    base = tops[0]
    same = sum(1 for t in tops if t == base)
    return same / len(tops), tops


def kendall_tau(a, b):
    """两个排名的 Kendall τ（只看共同出现的元素）。

    比"Top-1 是否相同"细一档：Top-1 不变但 2、3 名互换，也算轻微不稳。
    """
    common = [x for x in a if x in b]
    if len(common) < 2:
        return None
    ia = {x: i for i, x in enumerate(a)}
    ib = {x: i for i, x in enumerate(b)}
    conc = disc = 0
    for x, y in itertools.combinations(common, 2):
        s = (ia[x] - ia[y]) * (ib[x] - ib[y])
        if s > 0:
            conc += 1
        elif s < 0:
            disc += 1
    tot = conc + disc
    return (conc - disc) / tot if tot else None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="根因排序权重的敏感性分析（只回答『排序稳不稳』，不回答『权重对不对』）")
    ap.add_argument("--input", help="数据文件（CSV/Excel）")
    ap.add_argument("--defect", help="缺陷名称（须在知识库中，或配合 --col-map）")
    ap.add_argument("--target", help="目标列（不良率/面密度等）")
    ap.add_argument("--process", help="工序（可选，留空自动判定）")
    ap.add_argument("--show-config", action="store_true", help="只显示当前生效的权重配置")
    ap.add_argument("--outdir", help="把结果写成 JSON/文本到该目录（默认只打印）")
    args = ap.parse_args(argv)

    if args.show_config:
        w, src = RC.load_weights()
        print("当前生效权重：%s" % json.dumps(w, ensure_ascii=False))
        print("来源：%s" % src)
        print("说明：%s" % RC.weights_note(w, src))
        return 0

    if not (args.input and args.defect and args.target):
        ap.error("需要 --input、--defect、--target（或用 --show-config 只看配置）")

    loaded = _load_table(args.input)
    table = _pick_table(loaded)
    if table is None:
        print("× 读不出数据表：ingest 返回结构里没有 Table", file=sys.stderr)
        return 2

    col_map = PP.map_columns(table)
    seqs, names, errs = [], [], []
    for name, w in zip(GRID_NAMES, GRID):
        seq, err = rank_with(table, args.defect, args.target, col_map, w)
        if err:
            errs.append("%s：%s" % (name, err))
            continue
        seqs.append(seq)
        names.append(name)

    if not seqs:
        print("× 没有任何权重组合能排出结果，敏感性无从谈起。", file=sys.stderr)
        for e in errs:
            print("   - " + e, file=sys.stderr)
        return 2

    agree, tops = top1_agree(seqs)
    base_seq = seqs[0]
    taus = []
    for i in range(1, len(seqs)):
        tau = kendall_tau([x[0] for x in base_seq], [x[0] for x in seqs[i]])
        taus.append(tau)

    print("=" * 68)
    print("根因排序权重敏感性分析")
    print("=" * 68)
    print("数据：%s" % os.path.basename(args.input))
    print("缺陷：%s  目标列：%s" % (args.defect, args.target))
    print()
    print("%-26s %-28s" % ("权重组合", "Top-3 候选（得分）"))
    for name, seq in zip(names, seqs):
        top3 = " > ".join("%s(%.3f)" % (p, s) for p, s in seq[:3])
        print("%-26s %s" % (name, top3))
    print()
    print("Top-1 一致率：%.0f%%（%d/%d 个组合给出同一榜首）"
          % (agree * 100, sum(1 for t in tops if t == tops[0]), len(tops)))
    valid_tau = [t for t in taus if t is not None]
    if valid_tau:
        print("与默认权重的排序一致度（Kendall τ 均值）：%.2f"
              % (sum(valid_tau) / len(valid_tau)))
    if errs:
        print("\n未能参与的组合：")
        for e in errs:
            print("   - " + e)
    print()
    if agree >= 0.8:
        print("结论：**排序稳健**。换权重榜首不变，说明第一名是数据撑起来的，"
              "不是权重凑出来的。")
    elif agree > 0:
        print("结论：**排序部分不稳**。榜首会随权重变化，报告里第一名只能当"
              "『候选之一』读，不能写成定论；建议一并核对 Top-3。")
    else:
        print("结论：**排序高度不稳**。每种权重都换一个榜首，说明本批数据不足以"
              "分辨主因——此时任何『首要根因』的表述都属于过度解读，"
              "应补数据（更多行/更明确的参数列/受控规格）后再判。")
    print()
    print("⚠ 本结论只说明『排序对权重敏不敏感』，不能证明任何一套权重是正确的；"
          "后者需要现场真值（实际根因）。")

    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)
        payload = {
            "input": os.path.abspath(args.input), "defect": args.defect,
            "target": args.target, "top1_agreement": agree,
            "rankings": [{"weights": w, "name": n,
                          "order": [{"param": p, "score": s} for p, s in seq]}
                         for n, w, seq in zip(names, GRID[:len(names)], seqs)],
            "kendall_tau_vs_default": taus,
            "caveat": "只反映排序对权重的敏感性，不代表任何权重是正确的；"
                      "判断正确性需要现场真值。",
        }
        p = os.path.join(args.outdir, "sensitivity.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print("\n结果已写入：%s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
