# -*- coding: utf-8 -*-
"""补齐光伏组件侧失效机理缺口（黄变 / LID 光致衰减 / 接线盒失效 / 蜗牛纹 / 焊带疲劳）

为什么补这几条：
    这些不是拍脑袋想出来的，是**招聘 JD 原文点名**的失效机理——
    光伏组件测试岗 JD（猎聘，组件标板标定/失效分析岗）明确写：
    「失效机理分析（PID、LID/LeTID、隐裂、黄变、焊带疲劳、接线盒失效等）」。
    而本工具知识库里 PID 只作为"功率衰减"条目 mechanism 里的一句话存在，
    LID、黄变、焊带疲劳、接线盒失效、蜗牛纹**完全没有独立条目**，
    现场报「黄变」时拿不到机理与验证数据清单，只能落到"未收录"兜底。

数据来源（公开、可追溯）：
    · IEC 61215（组件设计鉴定与定型）/ IEC 61730（安全）中的老化与失效判据框架
    · 招聘 JD 原文所列失效机理清单（见上）
    · 公开失效分析资料对机理的通用描述（不含任何企业阈值）
    典型值一律不写死：条目只给机理、验证数据与查证动作，
    判定阈值由受控规格书/企业内控给出。

**别名安全约束（踩过坑，必须遵守）**：
    新增条目的别名不得是现有识别列的**子串**误命中源。
    实测教训：给"热导率不达标"继承 param_key 别名 "TC"，
    子串命中光伏列 "Pmax_STC(W)"，把功率列误判成热导率缺陷，
    进而让共线排除判据空转、E06 用例翻红。
    本脚本对每个新别名做**现有示例数据的冲突扫描**，冲突则直接报错退出。

用法：
    python scripts/build_pv_defect_gap_slice.py --check   # 只预览，不写
    python scripts/build_pv_defect_gap_slice.py           # 注入（幂等）
"""

import argparse
import glob
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = os.path.join(ROOT, "knowledge", "process_specs.json")

SRC = ("失效机理清单取自光伏组件测试/失效分析岗招聘 JD 原文所列项（PID、LID/LeTID、"
       "隐裂、黄变、焊带疲劳、接线盒失效）与 IEC 61215 / IEC 61730 的老化试验框架；"
       "机理描述为公开通用口径，不含任何企业阈值，判定以受控规格书为准")

# 新增条目：param_key 只在「别名不会误命中现有列」时才挂（见脚本头的别名安全约束）
NEW_DEFECTS = {
    "组件黄变": {
        "process": "pv_module_iv",
        "likely_params": ["eva_transmittance", "laminate_temp", "laminate_time"],
        # 不挂 param_key=eva_transmittance：它的别名含"透光率"，
        # 而 E06 组件 IV 示例数据里就有一列叫「透光率」，
        # 挂了会把该列直接判成"黄变"缺陷（别名误命中，实测踩过同类）。
        "param_key": None,
        "mechanism": "EVA/POE 封装胶膜在紫外与热作用下发生降解，产生共轭双键导致发黄，"
                     "透光率下降进而降低 Isc 与 Pmax；层压温度过高/时间过长、"
                     "胶膜抗 UV 配方不足、助剂析出都会加速",
        "verify_data": ["紫外老化（IEC 61215 UV 预处理）前后黄色指数或透光率对比",
                        "层压温度/时间与抽真空曲线记录",
                        "胶膜批次与抗 UV 助剂配方（来料）",
                        "EL 与外观照片（区分黄变与脏污/水渍）"],
        "quick_checks": ["是整片均匀发黄还是局部（局部多为助剂析出或污染）",
                         "同批胶膜是否集中在某几卷",
                         "层压机各区温度实测与设定是否一致"],
        "clearing_evidence": ["同批次胶膜的 UV 老化报告", "层压参数受控记录"],
        "aliases": ["黄变", "EVA黄变", "封装黄变", "胶膜黄变", "browning"],
    },
    "LID光致衰减": {
        "process": "pv_module_iv",
        "likely_params": ["pmax", "cell_efficiency", "ctm"],
        "param_key": "pmax",
        "response_defect": True,
        "mechanism": "硼氧对（B-O）在光照下形成复合中心导致初期功率快速衰减；"
                     "与硅片氧含量、硼掺杂浓度、退火工艺相关。LeTID 为热辅助光致衰减，"
                     "在多晶/部分单晶体系更显著，需与 LID 分开验证",
        "verify_data": ["初始与光注入/光照后 IV 复测（同温度同辐照度）",
                        "硅片氧含量与电阻率（来料）",
                        "暗态退火后恢复程度（区分 LID 与 LeTID）"],
        "quick_checks": ["衰减是否集中在首几十小时（LID 特征）",
                         "暗放置或退火后是否部分恢复",
                         "是否集中于某供应商硅片"],
        "carry_over_from": ["pv_wafer"],
        "clearing_evidence": ["硅片氧含量与掺杂记录", "光注入退火（LIR）工序参数"],
        "aliases": ["LID", "光致衰减", "光衰", "LID衰减", "LeTID"],
    },
    "接线盒失效": {
        "process": "pv_module_iv",
        "likely_params": ["bypass_diode_vf", "insulation_resistance", "solder_resistance"],
        "param_key": "bypass_diode_vf",
        "mechanism": "旁路二极管热击穿或虚焊、接线盒灌胶不满导致爬电与进水、"
                     "端子压接不良引起接触电阻升高与局部过热，严重时烧毁",
        "verify_data": ["二极管正向压降与反向漏电流",
                        "绝缘电阻/湿漏电流（IEC 61730）",
                        "接线盒灌胶与端子压接截面",
                        "红外热像（满载/遮挡工况）"],
        "quick_checks": ["热像是否在接线盒局部形成热点",
                         "灌胶是否有气泡/未充满",
                         "同批接线盒是否集中失效"],
        "clearing_evidence": ["接线盒与二极管来料批次", "灌胶工艺参数与首件确认"],
        "aliases": ["接线盒失效", "接线盒虚接", "接线盒烧毁", "二极管击穿",
                    "junction box failure"],
    },
    "蜗牛纹": {
        "process": "pv_cell_el",
        "likely_params": ["el_defect_rate", "el_gray_mean", "solder_temp"],
        "param_key": None,
        "mechanism": "电池片银浆微裂纹或栅线附近微损伤在湿热/光照下发生电化学腐蚀，"
                     "沿栅线呈蜗牛爬痕状变色；与浆料、烧结、微裂和封装水汽通道相关",
        "verify_data": ["EL 与外观同位置比对", "湿热老化（IEC 61215 DH）后复检",
                        "烧结峰值温度与银浆批次"],
        "quick_checks": ["痕迹是否沿栅线走向（区别于脏污）",
                         "是否集中在某台印刷/烧结设备",
                         "老化后是否加重"],
        "clearing_evidence": ["银浆批次与烧结曲线", "EL 微裂统计"],
        "aliases": ["蜗牛纹", "蜗牛印", "snail trail", "snail mark"],
    },
    "焊带疲劳": {
        "process": "pv_module_iv",
        "likely_params": ["solder_peel_strength", "solder_resistance", "string_tension"],
        "param_key": "solder_peel_strength",
        "mechanism": "温循下焊带与电池片/汇流条之间因热膨胀系数差异产生剪切应力，"
                     "焊点累积疲劳裂纹，表现为串联电阻上升、FF 下降，"
                     "严重时焊带断裂形成开路或热斑",
        "verify_data": ["温循（IEC 61215 TC）前后 FF/Rs 与 EL 对比",
                        "焊带剥离力与焊点截面（IMC 厚度）",
                        "串焊张力与焊接温度记录"],
        "quick_checks": ["FF 下降是否伴随 Rs 上升",
                         "EL 上焊点处是否出现连续暗线",
                         "是否集中于某台串焊机/某批焊带"],
        "clearing_evidence": ["焊带批次与镀层厚度", "串焊温度/张力受控记录"],
        "aliases": ["焊带疲劳", "焊带断裂", "ribbon fatigue", "焊点疲劳"],
    },
}


def _load(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def _existing_columns():
    """收集示例数据列名，用于别名误命中扫描（宁可多扫，不可漏扫）。"""
    cols = set()
    for pat in ("examples/**/*.csv", "examples/**/*.xlsx"):
        for p in glob.glob(os.path.join(ROOT, pat), recursive=True):
            try:
                sys.path.insert(0, ROOT)
                from lib import table as T
                cols.update(T.read_any(p).columns)
            except Exception as e:      # 读不了就跳过，但不能静默：打印出来
                print("  （跳过 %s：%s）" % (os.path.basename(p), e))
    return cols


def check_alias_collisions(cols):
    """别名不得是现有识别列的**子串**（这条就是 E06 翻红的根因）。"""
    bad = []
    for dk, dv in NEW_DEFECTS.items():
        names = [dk] + list(dv.get("aliases") or [])
        for n in names:
            if not n or len(n) < 2:
                continue
            for c in cols:
                if n in c:
                    bad.append((dk, n, c))
    return bad


def build(dry=False):
    spec = _load(SPEC)
    defs = spec.setdefault("defect_rootcause", {})
    cols = _existing_columns()

    bad = check_alias_collisions(cols)
    if bad:
        print("别名误命中现有列，拒绝写入（这正是 E06 翻红那类缺陷）：")
        for dk, n, c in bad:
            print("  · %s 的别名 %r 是列 %r 的子串" % (dk, n, c))
        raise SystemExit(2)

    added, skipped = [], []
    for dk, dv in NEW_DEFECTS.items():
        if dk in defs:
            skipped.append(dk)
            continue
        entry = dict(dv)
        entry["source"] = SRC
        entry["note"] = ("机理取自公开资料与 JD 所列失效项，现场适用性待复核；"
                         "判定阈值以受控规格书为准，本条不含企业数值")
        defs[dk] = entry
        added.append(dk)

    # 参数引用必须真实存在（悬空引用会让 rootcause 静默少算候选）
    params = spec.get("params") or {}
    dangling = []
    for dk in added:
        for pk in (defs[dk].get("likely_params") or []):
            if not params.get(pk):
                dangling.append((dk, pk))
    if dangling:
        print("悬空参数引用，拒绝写入：")
        for dk, pk in dangling:
            print("  · %s -> %s" % (dk, pk))
        raise SystemExit(2)

    print("现有缺陷条目 %d 条" % len(defs))
    print("新增：%s" % ("、".join(added) if added else "（无，已存在）"))
    if skipped:
        print("跳过（已存在）：%s" % "、".join(skipped))
    if dry:
        print("--check：只预览，未写入")
        return 0
    if not added:
        print("无需写入（幂等）")
        return 0
    tmp = SPEC + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(spec, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, SPEC)
    print("已写入 %s（缺陷条目 %d 条）" % (SPEC, len(defs)))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="补齐光伏失效机理缺口（幂等）")
    ap.add_argument("--check", action="store_true", help="只预览不写")
    a = ap.parse_args()
    sys.exit(build(dry=a.check))
