#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JD 词频采集器 —— newenergy-fde 数据采集工具包

用途：分析手动保存的招聘 JD 文本（txt，UTF-8），产出三类采集物：
  1. 词频 Top50          —— 痛点的原始语言（文案原料）
  2. 别名缺口表          —— JD 数据词汇 vs FDE 别名库的差集（喂 preprocess.py）
  3. 公司名单            —— 公司/岗位/工序关键词（行业地图 + 产能信号）

设计约束（方案红线）：
  - 纯离线文本分析，不含任何网络请求/爬虫逻辑
  - 只处理用户手动保存的公开 JD 文本，不接触个人信息字段
  - 零第三方依赖（与主库一致，标准库实现）

用法：
  python jd_scan.py [--raw DIR] [--out DIR]
  默认读同目录 jd_raw/*.txt，写同目录 out/
"""
import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

# ---- 分词用停用词（够用即可，不求全）----
STOP = set(
    """的 了 和 与 及 或 等 在 为 对 由 从 到 把 使 将 是 有 无 不 需 能 会 可 应
    我们 你们 他们 公司 岗位 工作 职责 要求 任职 优先 负责 以上 以下 相关 进行
    完成 参与 制定 建立 推动 协助 配合 其他 具有 具备 熟练 熟悉 了解 良好 较强
    1 2 3 4 5 年 以上及以上 者等 及以上 经验 学历 本科 大专 硕士 博士""".split()
)

# ---- 工序/数据词汇词典：JD 原始语言 → FDE 标准参数/工序 ----
# 维护规则：jd_scan 每周跑出的"未命中词"人工研判后补进这里，别名库同步更新
TERM_MAP = {
    # 工序（锂电）
    "涂布": "coating", "辊压": "calendering", "卷绕": "winding", "叠片": "stacking",
    "注液": "electrolyte", "化成": "formation", "分容": "capacity_grading",
    "老化": "aging", "焊接": "welding", "封装": "sealing",
    # 工序（光伏）
    "制绒": "texturing", "扩散": "diffusion", "丝网": "screen_printing",
    "烧结": "firing", "层压": "laminating", "iv测试": "pv_module_iv",
    "iv 测试": "pv_module_iv", "el检测": "pv_el", "el 检测": "pv_el",
    "硅片": "pv_wafer",
    # 高频数据/质量词汇
    "面密度": "coating_weight", "压实密度": "compaction_density", "水分含量": "moisture",
    "敷料量": "areal_loading", "对齐度": "alignment", "ttv": "ttv", "翘曲": "warp",
    "线痕": "wire_marks", "隐裂": "el_crack", "断栅": "broken_grid", "虚焊": "cold_solder",
    "明暗片": "el_shading", "功率衰减": "power_degradation", "rs": "rs", "rsh": "rsh",
    "cpk": "cpk", "spc": "spc", "8d": "8d", "minitab": "minitab", "doe": "doe",
    "良率": "yield", "不良率": "defect_rate", "直通率": "first_pass_yield",
    "fmea": "pfmea", "msa": "msa", "capa": "capa", "六西格玛": "six_sigma",
    "首件": "first_article", "巡检": "patrol_inspection", "出货检验": "oqc",
    "iqc": "iqc", "oqc": "oqc", "ipqc": "ipqc", "sqe": "sqe", "pqe": "pqe", "cqe": "cqe",
}
# 别名库（preprocess.py）已覆盖词的占位表——首版人工维护，后续可直接 import
KNOWN_ALIASES = set(TERM_MAP.values())

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-/]*|[\u4e00-\u9fff]+")


def read_jds(raw_dir: Path):
    for p in sorted(raw_dir.glob("*.txt")):
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(encoding="gbk", errors="ignore")
        yield p.stem, text


def tokenize(text: str):
    """只提取英文/数字词。中文领域词另走 TERM_MAP 词典计数
    （零依赖无中文分词，连续汉字整段成词会失真）。"""
    for raw in WORD_RE.findall(text):
        w = raw.lower().strip("-/")
        if len(w) < 2 or w in STOP or re.search(r"[\u4e00-\u9fff]", w):
            continue
        yield w


def match_terms(text_lower: str):
    found = {}
    for term, std in TERM_MAP.items():
        if term in text_lower:
            found[term] = std
    return found


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="JD 词频采集器（离线）")
    ap.add_argument("--raw", type=Path, default=here / "jd_raw")
    ap.add_argument("--out", type=Path, default=here / "out")
    args = ap.parse_args()

    if not args.raw.exists():
        print(f"[提示] 未找到 {args.raw}，请先手动保存 JD 文本到该目录（UTF-8 txt）。")
        return 1
    jds = list(read_jds(args.raw))
    if not jds:
        print(f"[提示] {args.raw} 里没有 .txt 文件。")
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    freq = Counter()        # 英文词频
    cn_hits = Counter()     # 中文领域词命中（词典口径）
    term_std = {}           # JD原词 → FDE标准名
    term_count = Counter()  # JD原词 → 出现JD数
    companies = []

    for name, text in jds:
        freq.update(tokenize(text))
        low = text.lower()
        hits = match_terms(low)
        cn_hits.update(hits.keys())   # 只传 key——Counter.update 传 dict 会把 value 当计数
        for k, v in hits.items():
            term_std[k] = v
        term_count.update(hits.keys())
        comp = name.split("-")[0].strip()
        procs = ",".join(sorted({s for _, s in hits.items()
                                 if s in {"coating", "calendering", "winding", "stacking",
                                          "formation", "capacity_grading", "pv_wafer",
                                          "pv_el", "pv_module_iv", "texturing",
                                          "diffusion", "screen_printing", "firing",
                                          "laminating"}})) or "-"
        companies.append({"文件": name, "公司(取自文件名)": comp,
                          "工序关键词": procs, "产能信号备注": ""})

    # 1. 词频：中文领域词（词典口径）+ 英文词（零依赖无中文分词，
    #    连续汉字整段成词会失真——领域词典计数才是诚实口径）
    with open(args.out / "词频Top50.txt", "w", encoding="utf-8") as f:
        f.write("中文领域词命中 Top50（痛点原始语言——写文案用他们的词）\n")
        f.write("=" * 46 + "\n")
        for w, c in cn_hits.most_common(50):
            f.write(f"{w}\t{c}\n")
        f.write("\n英文词频 Top50\n" + "=" * 46 + "\n")
        for w, c in freq.most_common(50):
            f.write(f"{w}\t{c}\n")

    # 2. 别名缺口表
    with open(args.out / "别名缺口表.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["JD 原词", "FDE 标准名", "出现JD数", "别名库状态", "处理建议"])
        for term, count in term_count.most_common():
            std = term_std[term]
            status = "已覆盖" if std in KNOWN_ALIASES else "疑似缺口"
            advice = "-" if status == "已覆盖" else "人工研判→补进 TERM_MAP 与 preprocess 别名库"
            w.writerow([term, std, count, status, advice])

    # 3. 公司名单
    with open(args.out / "公司名单.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["文件", "公司(取自文件名)", "工序关键词", "产能信号备注"])
        w.writeheader()
        w.writerows(companies)

    print(f"[完成] 处理 {len(jds)} 份 JD → {args.out}/")
    print("  词频Top50.txt / 别名缺口表.csv / 公司名单.csv")
    print("[下一步] 缺口表人工研判后，把新词补进本脚本 TERM_MAP 并同步 preprocess 别名库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
