#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_materials_testing_slice.py —— 向 newenergy-fde 知识库注入「材料测试」行业切片骨架。

设计原则（对齐项目纪律）：
- 真实条目全部带 source（公开检测机构资料 / 国标 / 行标 / 国军标），不编造。
- 材料性能典型值因牌号差异极大，typical_range 仅作“量级兜底”，正式判定以受控规格书为准；
  不固化任何材料真值常量。
- 幂等：已存在的工序 / 参数 / 缺陷机理不重复注入。
- --check 仅校验并打印将注入的内容，不写盘。

用法：
    python scripts/build_materials_testing_slice.py --check    # 预览
    python scripts/build_materials_testing_slice.py            # 注入并写盘
"""
import argparse
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "knowledge", "process_specs.json")
IND = os.path.join(ROOT, "knowledge", "industries.json")

DOMAIN = "materials_testing"
SRC_GENERIC = ("机理与标准取自公开检测机构资料（北检/中科院上海硅酸盐所 CNAS 认可项目/"
               "赛宝实验室）与国家标准/行业标准/国军标；现场适用性待复核，正式判定以受控规格书为准")

# ---------------------------------------------------------------------------
# 行业登记表条目
# ---------------------------------------------------------------------------
INDUSTRY_ENTRY = {
    "name": "材料测试",
    "alias": ["材料测试", "材料表征", "高分子测试", "复合材料测试", "材料检测",
              "materials testing", "material characterization", "polymer testing",
              "composite testing"],
    "domain": DOMAIN,
    "processes": [
        "material_thermal", "material_spectroscopy", "material_mechanical",
        "material_electrical", "material_reliability",
    ],
    "docs": ["knowledge/材料测试工序知识库.md"],
    "note": ("高分子/导热界面/电磁屏蔽/吸波/复合材料等的实验室表征测试"
             "（热分析 DSC/TGA/TMA/DMA、成分结构 FTIR/GC-MS/NMR/XRD/SEM、"
             "力学、电学与电磁屏蔽、老化环境）。域参数与标准口径取自公开检测机构资料与"
             "国标/行标/国军标，现场适用性待复核；FDE 提供统计/DOE/根因/报告方法论，"
             "不替代设备操作与标准符合性判定。"),
}

# ---------------------------------------------------------------------------
# 工序（processes）
# ---------------------------------------------------------------------------
PROCESSES = {
    "material_thermal": {
        "domain": DOMAIN, "name": "热分析与热性能",
        "alias": ["热分析", "热性能", "thermal analysis", "DSC", "TGA", "TMA", "DMA"],
        "upstream": ["材料合成/配料"], "downstream": ["材料性能评价"],
        "key_params": [
            "thermal_conductivity", "thermal_resistance", "specific_heat",
            "thermal_diffusivity", "glass_transition_temp", "melting_point",
            "crystallinity", "decomposition_temp", "thermal_expansion_coeff", "oit",
        ],
        "common_defects": ["热导率不达标", "Tg偏移", "分解温度偏低", "结晶度异常", "热失重异常"],
        "doe_factors": ["升温速率", "气氛", "样品厚度", "压力"],
        "notes": ("DSC 测 Tg/熔融/结晶度/氧化诱导期；TGA 测热稳定性/分解温度/填料含量；"
                  "TMA 测热膨胀系数；DMA 测粘弹与 Tg。界面材料热阻用稳态热流法 ASTM D5470，"
                  "薄片/高导热用激光闪射 ASTM E1461。"),
        "upstream_keys": [], "downstream_keys": ["material_spectroscopy", "material_mechanical"],
    },
    "material_spectroscopy": {
        "domain": DOMAIN, "name": "成分与结构表征",
        "alias": ["成分分析", "结构表征", "spectroscopy", "FTIR", "GC-MS", "NMR", "XRD", "SEM"],
        "upstream": ["材料合成/配料"], "downstream": ["材料性能评价"],
        "key_params": ["filler_content", "molecular_weight_Mw", "residual_monomer", "ftir_anomaly"],
        "common_defects": ["FTIR谱峰异常", "残留单体超标", "填料含量偏差", "分子量分布异常"],
        "doe_factors": ["制样方法", "分辨率", "扫描范围"],
        "notes": ("FTIR 定性鉴别官能团与添加剂；GC-MS 定量残留单体/小分子挥发物；"
                  "GPC 测分子量及分布；XRD 分析结晶结构；SEM/EDS 观察形貌与元素。"),
        "upstream_keys": [], "downstream_keys": ["material_mechanical", "material_electrical"],
    },
    "material_mechanical": {
        "domain": DOMAIN, "name": "力学性能",
        "alias": ["力学", "mechanical", "拉伸", "弯曲", "冲击", "硬度"],
        "upstream": ["材料制备"], "downstream": ["结构设计"],
        "key_params": [
            "tensile_strength", "tensile_modulus", "elongation_at_break",
            "flexural_strength", "flexural_modulus", "impact_strength",
            "hardness_shore", "hardness_rockwell", "interlaminar_shear",
        ],
        "common_defects": ["拉伸强度不达标", "冲击强度不足", "弯曲模量偏低", "硬度偏差", "层间剪切失效"],
        "doe_factors": ["应变速率", "温度", "样条取向", "缺口类型"],
        "notes": ("电子万能试验机做拉伸/弯曲/压缩；摆锤/落锤做冲击；邵氏/洛氏测硬度；"
                  "短梁剪切 ASTM D2344 测层间剪切。"),
        "upstream_keys": [], "downstream_keys": [],
    },
    "material_electrical": {
        "domain": DOMAIN, "name": "电学与电磁性能",
        "alias": ["电学", "电磁", "electrical", "EMI", "介电", "屏蔽"],
        "upstream": ["材料制备"], "downstream": ["电磁设计"],
        "key_params": [
            "volume_resistivity", "surface_resistivity", "dielectric_constant",
            "dissipation_factor", "dielectric_strength", "emi_se", "magnetic_permeability",
        ],
        "common_defects": ["电磁屏蔽效能不达标", "体积电阻率超标", "介电常数偏差", "击穿电压不足"],
        "doe_factors": ["导电填料含量", "厚度", "频率", "压力"],
        "notes": ("高阻计测体积/表面电阻率；LCR 测介电常数与损耗；耐压仪测击穿强度；"
                  "矢量网络分析仪经同轴/法兰/弓形法测电磁屏蔽效能与复电磁参数。"),
        "upstream_keys": [], "downstream_keys": [],
    },
    "material_reliability": {
        "domain": DOMAIN, "name": "老化与环境可靠性",
        "alias": ["老化", "可靠性", "reliability", "UV", "盐雾", "湿热"],
        "upstream": ["材料制备"], "downstream": ["寿命评估"],
        "key_params": ["uv_aging_retention", "thermal_aging_retention",
                       "salt_spray_resistance", "hydrolysis_resistance"],
        "common_defects": ["紫外老化变色/衰减", "湿热老化分层", "盐雾腐蚀", "热氧老化降解"],
        "doe_factors": ["温度", "湿度", "辐照强度", "时长"],
        "notes": ("紫外/氙灯老化 GB-T 16422/ISO 4892/ASTM G154；湿热 GB-T 2423；"
                  "盐雾 ASTM B117/GB-T 10125；热空气老化 GB-T 3512。"),
        "upstream_keys": [], "downstream_keys": [],
    },
}

# ---------------------------------------------------------------------------
# 参数（params）—— 全部带 source，typical_range 仅作量级兜底
# ---------------------------------------------------------------------------
CTRL = {"mode": "deviation_pct", "limit_pct": 5.0, "cv_limit_pct": 5.0}


def P(name, process, unit, aliases, trange, note, source):
    return {
        "name": name, "process": process, "unit": unit,
        "aliases": aliases, "typical_range": trange,
        "control": dict(CTRL),
        "note": note + "（量级兜底，以受控规格为准）",
        "source": source,
    }


PARAMS = {
    # --- 热分析 ---
    "thermal_conductivity": P("热导率", "material_thermal", "W/(m·K)",
        ["热导率", "导热系数", "thermal conductivity", "k-value", "TC"],
        [0.1, 50], "聚合物~0.1 至导热界面材料~10-50",
        "ASTM E1461(激光闪射)/ASTM D5470(界面稳态热流)/GB-T 22588；北检燃料电池导热界面材料测试"),
    "thermal_resistance": P("热阻", "material_thermal", "K·m²/W",
        ["热阻", "thermal resistance", "R-value", "界面热阻"],
        [0.1, 10], "界面材料热阻随压力变化",
        "ASTM D5470；北检界面接触热阻导热测试"),
    "specific_heat": P("比热容", "material_thermal", "J/(kg·K)",
        ["比热容", "比热", "specific heat", "Cp"],
        [500, 2500], "激光闪射法结合密度与热扩散系数计算",
        "ASTM E1269；GB-T 19466 系列"),
    "thermal_diffusivity": P("热扩散系数", "material_thermal", "mm²/s",
        ["热扩散系数", "thermal diffusivity", "α"],
        [0.01, 10], "瞬态法(激光闪射)直接测得",
        "ASTM E1461/GB-T 22588"),
    "glass_transition_temp": P("玻璃化转变温度", "material_thermal", "°C",
        ["玻璃化转变温度", "玻璃化温度", "Tg", "glass transition"],
        [-50, 300], "DSC 测相变区间，典型 ±0.1℃ 精度",
        "GB-T 19466/ISO 11357(DSC)；北检高分子材料测试"),
    "melting_point": P("熔融温度", "material_thermal", "°C",
        ["熔点", "熔融温度", "melting point", "Tm"],
        [50, 400], "DSC 熔融峰",
        "ISO 11357(DSC)"),
    "crystallinity": P("结晶度", "material_thermal", "%",
        ["结晶度", "crystallinity"],
        [0, 100], "DSC 或 XRD 法",
        "ISO 11357(DSC)/XRD"),
    "decomposition_temp": P("分解温度", "material_thermal", "°C",
        ["分解温度", "热分解温度", "Td", "decomposition temp"],
        [200, 600], "TGA 失重拐点",
        "TGA: ISO 11358/ASTM E1131；北检"),
    "thermal_expansion_coeff": P("热膨胀系数", "material_thermal", "10^-6/K",
        ["热膨胀系数", "CTE", "coefficient of thermal expansion"],
        [1, 200], "TMA 测得",
        "TMA: ISO 11359"),
    "oit": P("氧化诱导期", "material_thermal", "min",
        ["氧化诱导期", "OIT", "oxidative induction time"],
        [1, 60], "DSC 等温氧化",
        "ISO 11357-6(DSC)"),
    # --- 成分结构 ---
    "filler_content": P("填料含量", "material_spectroscopy", "%",
        ["填料含量", "填充量", "filler content"],
        [0, 90], "TGA 灰分法",
        "TGA；北检复合材料检测"),
    "molecular_weight_Mw": P("重均分子量", "material_spectroscopy", "g/mol",
        ["重均分子量", "Mw", "molecular weight"],
        [1000, 1000000], "GPC 测分子量及分布",
        "GPC: ISO 13885/GB-T 21863"),
    "residual_monomer": P("残留单体", "material_spectroscopy", "%",
        ["残留单体", "residual monomer"],
        [0, 5], "GC-MS 定量",
        "GC-MS；北检高分子材料检测"),
    "ftir_anomaly": P("FTIR异常", "material_spectroscopy", "(定性)",
        ["FTIR异常峰", "红外异常", "官能团偏移"],
        [0, 0], "FTIR 全谱比对，属定性判据",
        "FTIR: GB-T 6040；北检"),
    # --- 力学 ---
    "tensile_strength": P("拉伸强度", "material_mechanical", "MPa",
        ["拉伸强度", "抗拉强度", "tensile strength", "TS"],
        [5, 200], "准静态拉伸",
        "ASTM D638/ISO 527/GB-T 1040；北检"),
    "tensile_modulus": P("拉伸模量", "material_mechanical", "MPa",
        ["拉伸模量", "杨氏模量", "tensile modulus"],
        [100, 10000], "拉伸初始斜率",
        "ASTM D638/ISO 527"),
    "elongation_at_break": P("断裂伸长率", "material_mechanical", "%",
        ["断裂伸长率", "伸长率", "elongation at break"],
        [1, 1000], "断裂时标距伸长",
        "ASTM D638/ISO 527/GB-T 1040"),
    "flexural_strength": P("弯曲强度", "material_mechanical", "MPa",
        ["弯曲强度", "抗弯强度", "flexural strength"],
        [20, 400], "三点/四点弯曲",
        "ASTM D790/ISO 178/GB-T 9341"),
    "flexural_modulus": P("弯曲模量", "material_mechanical", "MPa",
        ["弯曲模量", "flexural modulus"],
        [500, 20000], "弯曲初始斜率",
        "ASTM D790/ISO 178"),
    "impact_strength": P("冲击强度", "material_mechanical", "kJ/m²",
        ["冲击强度", "简支梁冲击", "悬臂梁冲击", "impact strength", "Izod", "Charpy"],
        [1, 100], "摆锤冲击",
        "ASTM D256/ISO 180/GB-T 1043(简支梁)/ASTM D4812(悬臂梁)"),
    "hardness_shore": P("邵氏硬度", "material_mechanical", "Shore",
        ["邵氏硬度", "邵氏", "Shore hardness", "邵A", "邵D"],
        [0, 100], "压痕法",
        "ASTM D2240/GB-T 2411"),
    "hardness_rockwell": P("洛氏硬度", "material_mechanical", "HR",
        ["洛氏硬度", "Rockwell hardness"],
        [0, 130], "压痕深度差",
        "ISO 2039-2"),
    "interlaminar_shear": P("层间剪切强度", "material_mechanical", "MPa",
        ["层间剪切强度", "ILSS", "interlaminar shear"],
        [10, 120], "短梁剪切",
        "ASTM D2344(短梁剪切)"),
    # --- 电学/电磁 ---
    "volume_resistivity": P("体积电阻率", "material_electrical", "Ω·cm",
        ["体积电阻率", "volume resistivity", "VR"],
        [1e6, 1e18], "高阻计",
        "ASTM D257/GB-T 1410/IEC 60093；北检"),
    "surface_resistivity": P("表面电阻率", "material_electrical", "Ω/sq",
        ["表面电阻率", "surface resistivity"],
        [1e3, 1e18], "高阻计",
        "ASTM D257/GB-T 1410/IEC 60093"),
    "dielectric_constant": P("介电常数", "material_electrical", "(无量纲)",
        ["介电常数", "介电系数", "dielectric constant", "εr", "permittivity"],
        [1, 10], "LCR 表",
        "ASTM D150/IEC 60250；北检"),
    "dissipation_factor": P("介电损耗角正切", "material_electrical", "(无量纲)",
        ["介电损耗", "损耗角正切", "dissipation factor", "tan δ"],
        [0, 1], "LCR 表",
        "ASTM D150/IEC 60250"),
    "dielectric_strength": P("介电强度", "material_electrical", "kV/mm",
        ["介电强度", "击穿电压", "电气强度", "dielectric strength", "breakdown voltage"],
        [1, 100], "耐压仪阶梯升压",
        "ASTM D149/GB-T 1408/IEC 60243"),
    "emi_se": P("电磁屏蔽效能", "material_electrical", "dB",
        ["电磁屏蔽效能", "屏蔽效能", "EMI SE", "shielding effectiveness"],
        [0, 120], "同轴/法兰/弓形/屏蔽室法",
        "ASTM D4935(同轴)/GB-T 30142(法兰同轴)/GJB 6190/IEEE 299；北检屏蔽效率检测/中科光析吸波检测"),
    "magnetic_permeability": P("磁导率", "material_electrical", "(相对,无量纲)",
        ["磁导率", "复磁导率", "permeability", "μr"],
        [1, 10], "同轴/波导法测复磁导率",
        "GJB 2038A(雷达吸波材料反射率)/IEEE 1128"),
    # --- 可靠性 ---
    "uv_aging_retention": P("紫外老化保留率", "material_reliability", "%",
        ["紫外老化保留率", "UV老化后强度保留率"],
        [0, 100], "老化前后性能比",
        "GB-T 16422/ISO 4892/ASTM G154(紫外老化)；北检"),
    "thermal_aging_retention": P("热老化保留率", "material_reliability", "%",
        ["热氧老化保留率", "热老化保留率"],
        [0, 100], "热空气老化前后性能比",
        "GB-T 3512(热空气老化)"),
    "salt_spray_resistance": P("盐雾耐腐蚀", "material_reliability", "h",
        ["盐雾", "中性盐雾", "salt spray"],
        [0, 1000], "中性盐雾时长或评级",
        "ASTM B117/GB-T 10125"),
    "hydrolysis_resistance": P("耐水解", "material_reliability", "%",
        ["耐水解", "吸水率"],
        [0, 5], "吸水率或水解后性能保持",
        "ISO 4611(耐水解)/GB-T 1034(吸水)"),
}

# ---------------------------------------------------------------------------
# 缺陷机理（defect_rootcause）—— 全部带 source + note
# ---------------------------------------------------------------------------
def D(process, likely, mechanism, verify, quick, clear, param_key, aliases):
    return {
        "process": process,
        "likely_params": likely,
        "mechanism": mechanism,
        "verify_data": verify,
        "quick_checks": quick,
        "carry_over_from": [],
        "clearing_evidence": clear,
        "param_key": param_key,
        "aliases": aliases,
        "source": SRC_GENERIC,
        "note": "机理取自公开检测机构资料，现场适用性待复核；最终判定须以实测数据为准。",
    }


DEFECTS = {
    "热导率不达标": D("material_thermal",
        ["thermal_conductivity", "filler_content", "thermal_resistance"],
        "导热填料分散不均/团聚、界面热阻高、填料含量低于配方、树脂基体本征导热低",
        ["配方填料含量(TGA)", "分散工艺记录", "粒径分布", "界面热阻随压力曲线"],
        ["核对填料含量(TGA)是否达标", "检查分散均匀性(SEM)", "对比不同批次浆料状态"],
        ["本工序为材料本征性能，追溯指向配料/分散工艺（未在本库建模）"],
        "thermal_conductivity", ["导热系数不达标", "导热差"]),
    "Tg偏移": D("material_thermal",
        ["glass_transition_temp", "melting_point", "crystallinity"],
        "固化不完全、共聚物组成变化、热历史(退火/冷却速率)差异、增塑剂迁移",
        ["DSC 升/降温曲线", "固化工艺窗口", "原料批次"],
        ["核对固化度(DSC 焓变)", "对比冷却速率记录", "检查原料牌号是否替换"],
        ["本工序为材料本征性能，追溯指向聚合/固化工艺"],
        "glass_transition_temp", ["玻璃化转变温度异常", "Tg漂移"]),
    "电磁屏蔽效能不达标": D("material_electrical",
        ["emi_se", "volume_resistivity", "magnetic_permeability"],
        "导电填料含量不足或分布不均、厚度不够、导电网络未形成、界面氧化",
        ["填料含量", "厚度", "截面形貌(SEM)", "表面电阻率"],
        ["核对导电填料含量与厚度", "检查截面导电网络连续性(SEM)", "测表面电阻率辅助判断"],
        ["追溯指向配料/成型工艺"],
        "emi_se", ["屏蔽效能不足", "EMI SE 低"]),
    "拉伸强度不达标": D("material_mechanical",
        ["tensile_strength", "filler_content", "molecular_weight_Mw"],
        "填料团聚导致应力集中、界面结合差、固化不足或过度、分子量偏低/分布宽",
        ["拉伸断面形貌", "填料分散状态", "固化度", "分子量(GPC)"],
        ["观察断面脆性/韧性形貌", "核对填料分散(SEM)", "检查固化窗口"],
        ["追溯指向配料/固化工艺"],
        "tensile_strength", ["抗拉强度不足", "拉伸差"]),
    "残留单体超标": D("material_spectroscopy",
        ["residual_monomer", "filler_content"],
        "聚合转化率低、后处理不充分、原料/催化剂残留",
        ["GC-MS 谱图", "反应时间温度", "后处理工艺记录"],
        ["核对反应时长与温度是否达转化窗口", "检查后处理(洗涤/脱挥)步骤"],
        ["追溯指向聚合/后处理工艺"],
        "residual_monomer", ["残留单体高", "小分子挥发物超标"]),
    "老化衰减过快": D("material_reliability",
        ["uv_aging_retention", "thermal_aging_retention", "salt_spray_resistance"],
        "抗氧剂/稳定剂不足、主链易降解、添加剂迁移析出",
        ["老化前后性能对比", "稳定剂含量", "FTIR 氧化峰"],
        ["对比老化前后性能保留率", "检查稳定剂添加量", "FTIR 看氧化峰增长"],
        ["追溯指向配方稳定体系"],
        "uv_aging_retention", ["耐候性差", "老化快"]),
    "FTIR谱峰异常": D("material_spectroscopy",
        ["ftir_anomaly", "residual_monomer"],
        "批次原料替换、添加剂变化、固化度差异、污染",
        ["FTIR 全谱比对", "原料红外", "GC-MS"],
        ["与基准谱图逐峰比对", "核对原料是否替换", "GC-MS 排查污染物"],
        ["追溯指向原料/批次"],
        "ftir_anomaly", ["红外异常", "官能团偏移"]),
    "冲击强度不足": D("material_mechanical",
        ["impact_strength", "interlaminar_shear"],
        "韧性相缺失、界面弱、缺口敏感、分子量低",
        ["冲击断面", "橡胶相分布", "分子量(GPC)"],
        ["观察断面脆性形貌", "检查增韧相分布", "核对分子量"],
        ["追溯指向配方增韧体系"],
        "impact_strength", ["抗冲击差", "韧不足"]),
}


# ---------------------------------------------------------------------------
# 注入逻辑（幂等）
# ---------------------------------------------------------------------------
def _load(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(path, obj):
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build(dry):
    spec = _load(SPEC)
    ind = _load(IND)
    added = {"processes": [], "params": [], "defects": [], "industry": False}

    # 行业登记
    inds = ind.setdefault("industries", {})
    if DOMAIN not in inds:
        inds[DOMAIN] = INDUSTRY_ENTRY
        added["industry"] = True
    else:
        # 即便已存在，也补全 processes 列表（幂等）
        cur = set(inds[DOMAIN].get("processes", []))
        miss = [p for p in INDUSTRY_ENTRY["processes"] if p not in cur]
        if miss:
            inds[DOMAIN]["processes"] = INDUSTRY_ENTRY["processes"]
            added["industry"] = True

    procs = spec.setdefault("processes", {})
    for k, v in PROCESSES.items():
        if k not in procs:
            procs[k] = v
            added["processes"].append(k)

    params = spec.setdefault("params", {})
    for k, v in PARAMS.items():
        if k not in params:
            params[k] = v
            added["params"].append(k)

    dr = spec.setdefault("defect_rootcause", {})
    for k, v in DEFECTS.items():
        if k not in dr:
            dr[k] = v
            added["defects"].append(k)

    # 引用一致性自检：process.key_params 必须都是已登记 param
    bad = []
    for pk, pv in procs.items():
        for kp in pv.get("key_params", []) or []:
            if kp not in params:
                bad.append("%s.key_params 含未知参数 %s" % (pk, kp))
    if bad:
        raise SystemExit("引用不一致：\n" + "\n".join(bad))

    if dry:
        print("[CHECK] 将注入：")
        print("  行业登记 materials_testing:", added["industry"])
        print("  工序:", added["processes"])
        print("  参数(共%d,新增%d):" % (len(params), len(added["params"])), added["params"])
        print("  缺陷机理(共%d,新增%d):" % (len(dr), len(added["defects"])), added["defects"])
        print("[CHECK] 引用一致性: OK")
        return 0

    _write(SPEC, spec)
    _write(IND, ind)
    print("已注入：行业=%s 工序=%d 参数=%d 缺陷=%d"
          % (added["industry"], len(added["processes"]),
             len(added["params"]), len(added["defects"])))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="仅校验并打印将注入内容，不写盘")
    args = ap.parse_args()
    return build(args.check)


if __name__ == "__main__":
    sys.exit(main())
