# -*- coding: utf-8 -*-
"""搭建「半导体制造」行业切片骨架（光刻/刻蚀/薄膜/CMP/量测良率）

为什么建这个切片：
    名企 JD 检索结果显示，半导体工艺岗（长鑫存储 薄膜/CMP/测试工艺、
    北方华创 MPCVD、京东方 PVD/PE-NPI、中芯国际 良率提升/良率分析）
    的方法要求与本工具高度一致——**SPC / MSA / FMEA / DOE / 5Why / 8D /
    CP / 六西格玛 / JMP**。但本工具此前行业切片只有 battery / pv /
    materials_testing，半导体侧的**工序与参数知识为 0**：
    拿到一张 CD/Overlay/膜厚 的产线数据，工序选不出来、参数认不出来、
    缺陷机理一条都没有，只能走「未收录」兜底。

    所以这里只补**骨架**：工序 → 关键参数 → 参数别名 → 常见缺陷机理，
    让统计层（SPC/DOE/MSA/根因/FMEA）能挂上去。**不补工艺配方与真值**。

数据来源（公开、可追溯，不含任何企业配方）：
    · SEMI 标准体系的量测与统计口径（CD/Overlay/膜厚/颗粒/缺陷密度的一般定义）
    · IRDS（国际器件与系统路线图）公开的工艺与量测术语框架
    · 名企 JD 原文点名的工序与失效项（长鑫薄膜区 CVD/PVD/RTP/铜工艺、
      CMP；北方华创 MPCVD 金刚石；京东方 PVD；中芯良率 Fail Bin/Inline）
    典型值一律标为**量级兜底**，判定阈值必须来自受控规格书（SPC 的
    control limit 由企业按自身历史数据定，本库不给）。

**别名安全约束**（本项目已两次踩坑）：
    别名不得是现有识别列的**子串**。挂 `param_key` 会继承参数别名，
    缩写别名（CD/THK/Rs…）极易子串误命中别工序列。本脚本写库前
    扫描 `examples/**` 全部列名，冲突即拒写。

用法：
    python scripts/build_semiconductor_slice.py --check
    python scripts/build_semiconductor_slice.py
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
IND = os.path.join(ROOT, "knowledge", "industries.json")

SRC = ("工序/参数/机理取自 SEMI 标准体系的量测与统计术语、IRDS 公开路线图框架，"
       "以及名企 JD 原文点名的工序与失效项（长鑫存储薄膜区 CVD/PVD/RTP/铜工艺与 CMP、"
       "北方华创 MPCVD、京东方 PVD、中芯良率 Inline/Fail Bin）；"
       "典型值为量级兜底，控制限须由企业按自身数据与受控规格书确定")

SRC_NOTE = ("机理取自公开资料与 JD 所列失效项，现场适用性待复核；"
            "本条不含企业配方与阈值，判定以受控规格书为准")


def P(name, process, unit, aliases, trange, note, source=SRC, mode="none"):
    ctrl = {"mode": mode}
    return {
        "name": name, "process": process, "unit": unit,
        "aliases": aliases, "typical_range": trange,
        "control": ctrl,
        "note": note + "（量级兜底，控制限以受控规格书为准）",
        "source": source,
    }


def D(process, likely, mechanism, verify, quick, clear, param_key, aliases,
      response=False):
    e = {
        "process": process,
        "likely_params": likely,
        "mechanism": mechanism,
        "verify_data": verify,
        "quick_checks": quick,
        "carry_over_from": [],
        "clearing_evidence": clear,
        "param_key": param_key,
        "aliases": aliases,
        "source": SRC,
        "note": SRC_NOTE,
    }
    if response:
        e["response_defect"] = True
    return e


# ------------------------------------------------------------------ 行业与工序
INDUSTRY = {
    "name": "半导体制造",
    "alias": ["半导体", "集成电路", "晶圆制造", "芯片制造", "晶圆厂",
              "semiconductor", "wafer fab", "fab", "IC manufacturing"],
    "domain": "semiconductor",
    "processes": ["semi_litho", "semi_etch", "semi_thinfilm", "semi_cmp",
                  "semi_metrology"],
    "docs": [],
    "note": ("晶圆制造的光刻/刻蚀/薄膜/CMP/量测良率五个环节。"
             "本切片只提供工序—参数—缺陷机理骨架，供 SPC/DOE/MSA/根因/FMEA "
             "挂载；不含工艺配方、不含控制限。"),
}

PROCESSES = {
    "semi_litho": {
        "domain": "semiconductor", "name": "光刻",
        "alias": ["光刻", "曝光", "涂胶显影", "lithography", "photo", "litho"],
        "upstream": ["薄膜沉积", "清洗"], "downstream": ["刻蚀", "量测"],
        "key_params": ["cd_critical_dim", "overlay", "exposure_dose",
                       "focus_offset", "resist_thickness"],
        "common_defects": ["CD偏移", "Overlay超差", "光刻胶缺陷", "显影不净"],
        "doe_factors": ["exposure_dose", "focus_offset", "resist_thickness"],
        "notes": ("光刻是图形尺寸的第一道关口：CD 决定器件电性，Overlay 决定层间对准。"
                  "CD 与 Overlay 的漂移往往先于电性失效出现，是最值得做 SPC 的两个量。"),
        "upstream_keys": [], "downstream_keys": ["semi_etch", "semi_metrology"],
    },
    "semi_etch": {
        "domain": "semiconductor", "name": "刻蚀",
        "alias": ["刻蚀", "蚀刻", "干法刻蚀", "湿法刻蚀", "etch", "etching"],
        "upstream": ["光刻"], "downstream": ["薄膜沉积", "量测"],
        "key_params": ["etch_rate", "etch_uniformity", "etch_selectivity",
                       "sidewall_angle"],
        "common_defects": ["刻蚀残留", "过刻蚀", "侧壁角度异常", "刻蚀不均"],
        "doe_factors": ["rf_power", "chamber_pressure", "gas_flow"],
        "notes": ("刻蚀要同时控住三件事：刻多快（rate）、刻多匀（uniformity）、"
                  "刻得选不选（selectivity）。三者互相牵制，单参数调到极致通常会让另两个变差，"
                  "所以必须用 DOE 找窗口而不是逐参数试。"),
        "upstream_keys": ["semi_litho"], "downstream_keys": ["semi_thinfilm"],
    },
    "semi_thinfilm": {
        "domain": "semiconductor", "name": "薄膜沉积",
        "alias": ["薄膜", "薄膜沉积", "化学气相沉积", "物理气相沉积", "原子层沉积",
                  "cvd", "pvd", "ald", "thin film"],
        "upstream": ["刻蚀", "清洗"], "downstream": ["CMP", "量测"],
        "key_params": ["film_thickness", "film_uniformity", "film_stress",
                       "refractive_index", "sheet_resistance"],
        "common_defects": ["膜厚不均", "膜应力过大", "薄膜颗粒", "折射率异常"],
        "doe_factors": ["depo_temp", "chamber_pressure", "gas_flow", "rf_power"],
        "notes": ("薄膜的三个基本量：厚度、均匀性、应力。应力过大直接导致翘曲与"
                  "后续光刻对准失败，常被漏查——只看厚度合格就放行是典型盲区。"),
        "upstream_keys": ["semi_etch"], "downstream_keys": ["semi_cmp"],
    },
    "semi_cmp": {
        "domain": "semiconductor", "name": "化学机械研磨",
        "alias": ["化学机械研磨", "化学机械抛光", "研磨", "抛光", "cmp"],
        "upstream": ["薄膜沉积"], "downstream": ["量测", "清洗"],
        "key_params": ["removal_rate", "dishing", "erosion", "down_force",
                       "polish_uniformity"],
        "common_defects": ["碟形凹陷", "侵蚀", "研磨不均", "研磨划伤"],
        "doe_factors": ["down_force", "platen_speed", "slurry_flow"],
        "notes": ("CMP 的两个专有失效：dishing（同材料区凹陷）与 erosion（密集区整体下陷）。"
                  "它们不是「厚度超差」，而是**图形依赖的局部形貌偏差**，"
                  "用常规厚度 SPC 抓不到，必须按图形密度分组看。"),
        "upstream_keys": ["semi_thinfilm"], "downstream_keys": ["semi_metrology"],
    },
    "semi_metrology": {
        "domain": "semiconductor", "name": "量测与良率",
        "alias": ["量测", "良率", "缺陷检测", "metrology", "yield", "inspection"],
        "upstream": ["光刻", "刻蚀", "薄膜沉积", "化学机械研磨"],
        "downstream": ["封装测试"],
        "key_params": ["defect_density", "yield_rate", "cd_uniformity",
                       "particle_count", "wafer_cpk"],
        "common_defects": ["良率骤降", "缺陷密度超标", "颗粒污染", "晶圆划伤"],
        "doe_factors": [],
        "notes": ("良率是结果量（response），不能当因子。良率骤降的第一件事是"
                  "按层/按机台/按批号拆分，而不是直接在整片上算相关——"
                  "整片混算会把「某台设备」的信号稀释成噪声。"),
        "upstream_keys": ["semi_litho", "semi_etch", "semi_thinfilm", "semi_cmp"],
        "downstream_keys": [],
    },
}

PARAMS = {
    # 光刻
    "cd_critical_dim": P("关键尺寸 CD", "semi_litho", "nm",
                         ["关键尺寸CD", "临界尺寸CD", "线宽CD", "critical dimension", "CD值"],
                         [10, 500], "图形线宽的实际尺寸，决定器件电性"),
    "overlay": P("套刻精度 Overlay", "semi_litho", "nm",
                 ["overlay", "套刻", "套刻精度", "对准精度", "对位精度"],
                 [0, 50], "层与层之间的对准偏差，超标直接短路或开路"),
    "exposure_dose": P("曝光剂量", "semi_litho", "mJ/cm²",
                       ["曝光剂量", "剂量", "dose", "exposure dose"],
                       [5, 200], "决定光刻胶接受的能量，是 CD 的主要调节手段"),
    "focus_offset": P("聚焦偏移", "semi_litho", "nm",
                      ["聚焦偏移", "焦距", "focus", "focus offset"],
                      [-200, 200], "离焦会同时恶化 CD 与侧壁形貌"),
    "resist_thickness": P("光刻胶厚度", "semi_litho", "nm",
                          ["光刻胶厚度", "胶厚", "resist thickness"],
                          [50, 5000], "胶厚影响分辨率与抗刻蚀能力"),
    # 刻蚀
    "etch_rate": P("刻蚀速率", "semi_etch", "nm/min",
                   ["刻蚀速率", "蚀刻速率", "etch rate"],
                   [1, 2000], "单位时间去除量，决定工艺时间与产能"),
    "etch_uniformity": P("刻蚀均匀性", "semi_etch", "%",
                         ["刻蚀均匀性", "蚀刻均匀性", "etch uniformity"],
                         [0, 20], "片内/片间去除量的一致性"),
    "etch_selectivity": P("刻蚀选择比", "semi_etch", "比值",
                          ["选择比", "刻蚀选择比", "selectivity"],
                          [0.5, 50], "目标材料与掩膜/底层的去除速率比"),
    "sidewall_angle": P("侧壁角度", "semi_etch", "°",
                        ["侧壁角度", "侧壁角", "sidewall angle"],
                        [70, 90], "过陡或过缓都会影响后续填充"),
    "rf_power": P("射频功率", "semi_etch", "W",
                  ["射频功率", "RF功率", "rf power"],
                  [100, 5000], "等离子体强度主控量"),
    "chamber_pressure": P("腔体压力", "semi_etch", "mTorr",
                          ["腔体压力", "反应腔压力", "chamber pressure"],
                          [1, 500], "影响离子能量分布与均匀性"),
    "gas_flow": P("气体流量", "semi_etch", "sccm",
                  ["气体流量", "气流", "gas flow"],
                  [10, 2000], "反应物供给量"),
    # 薄膜
    "film_thickness": P("薄膜厚度", "semi_thinfilm", "nm",
                        ["薄膜厚度", "沉积膜厚", "film thickness", "thin film thickness"],
                        [1, 5000], "薄膜沉积的核心控制量"),
    "film_uniformity": P("膜厚均匀性", "semi_thinfilm", "%",
                         ["膜厚均匀性", "厚度均匀性", "film uniformity"],
                         [0, 15], "片内厚度分布一致性"),
    "film_stress": P("薄膜应力", "semi_thinfilm", "MPa",
                     ["薄膜应力", "膜应力", "应力", "film stress"],
                     [-1000, 1000], "过大会导致翘曲与光刻对准失败"),
    "refractive_index": P("折射率", "semi_thinfilm", "无量纲",
                          ["折射率", "refractive index", "n值"],
                          [1.3, 2.5], "间接反映膜成分与致密度"),
    "sheet_resistance": P("方块电阻", "semi_thinfilm", "Ω/□",
                          ["方块电阻", "方阻", "sheet resistance", "sheet resistance Rs"],
                          [0.01, 1000], "金属/掺杂层电性表征"),
    "depo_temp": P("沉积温度", "semi_thinfilm", "℃",
                   ["沉积温度", "淀积温度", "deposition temperature"],
                   [150, 1200], "决定膜致密度与应力方向"),
    # CMP
    "removal_rate": P("研磨去除速率", "semi_cmp", "nm/min",
                      ["去除速率", "研磨速率", "removal rate", "MRR"],
                      [10, 2000], "单位时间去除量，决定研磨时间窗口"),
    "dishing": P("碟形凹陷", "semi_cmp", "nm",
                 ["碟形凹陷", "凹陷", "dishing"],
                 [0, 200], "同材料宽结构区的凹陷量，图形依赖"),
    "erosion": P("侵蚀", "semi_cmp", "nm",
                 ["侵蚀", "erosion", "过磨"],
                 [0, 300], "密集图形区整体下陷量，用常规厚度 SPC 抓不到"),
    "down_force": P("研磨压力", "semi_cmp", "psi",
                    ["研磨压力", "下压力", "down force"],
                    [0.5, 10], "主控去除速率，但过大会增大缺陷"),
    "polish_uniformity": P("研磨均匀性", "semi_cmp", "%",
                           ["研磨均匀性", "抛光均匀性", "polish uniformity"],
                           [0, 20], "片内去除量一致性"),
    # 量测/良率
    "defect_density": P("缺陷密度", "semi_metrology", "个/cm²",
                        ["缺陷密度", "defect density", "DD"],
                        [0, 100], "单位面积缺陷数，良率的主要先行指标"),
    "yield_rate": P("良率", "semi_metrology", "%",
                    ["晶圆良率", "总良率", "yield rate", "wafer yield"],
                    [0, 100], "结果量（response），不能当因子用"),
    "cd_uniformity": P("CD 均匀性", "semi_metrology", "%",
                       ["CD均匀性", "线宽均匀性", "cd uniformity"],
                       [0, 15], "片内关键尺寸一致性"),
    "particle_count": P("颗粒数", "semi_metrology", "颗",
                        ["颗粒数", "颗粒", "particle", "particle count"],
                        [0, 10000], "颗粒是良率的头号杀手，且跨所有工序"),
    "wafer_cpk": P("晶圆级 Cpk", "semi_metrology", "无量纲",
                   ["晶圆Cpk", "wafer cpk", "Cpk"],
                   [0, 3], "参数能力指数，需按层/按机台分别算"),
    "platen_speed": P("研磨盘转速", "semi_cmp", "rpm",
                      ["研磨盘转速", "盘转速", "platen speed"],
                      [10, 200], "影响去除速率与均匀性"),
    "slurry_flow": P("研磨液流量", "semi_cmp", "mL/min",
                     ["研磨液流量", "slurry流量", "slurry flow"],
                     [50, 500], "化学作用强度，影响选择比与缺陷"),
}

DEFECTS = {
    "CD偏移": D("semi_litho",
                ["cd_critical_dim", "exposure_dose", "focus_offset",
                 "resist_thickness"],
                "曝光剂量偏离、离焦、胶厚变化或显影异常导致线宽整体偏大/偏小；"
                "也可能是掩膜本身尺寸漂移（来料）",
                ["CD 趋势图与曝光剂量趋势对照", "片内 CD 分布图",
                 "掩膜版来料 CD 数据", "聚焦校准记录"],
                ["是整体偏移还是局部（局部多为离焦或掩膜缺陷）",
                 "同机台不同产品是否都偏", "换批次光刻胶后是否变化"],
                ["掩膜来料 CD 报告", "曝光机校准记录"],
                "cd_critical_dim", ["CD偏移", "线宽超差", "CD超差", "线宽偏移"]),
    "Overlay超差": D("semi_litho",
                     ["overlay", "focus_offset"],
                     "对准系统误差、机台热漂移、前层图形畸变或薄膜应力导致层间错位；"
                     "应力过大的膜会使晶圆翘曲，进而使对准失效",
                     ["Overlay 矢量图（含 X/Y 分量）", "前层薄膜应力数据",
                      "机台对准校准记录", "晶圆翘曲量"],
                     ["误差是平移、旋转还是放大（三者原因不同）",
                      "是否与某台曝光机或某个前层膜相关", "翘曲量是否超限"],
                     ["前层膜应力与翘曲记录", "对准校准记录"],
                     "overlay", ["Overlay超差", "套刻超差", "对准偏移", "套刻偏移"]),
    "膜厚不均": D("semi_thinfilm",
                  ["film_thickness", "film_uniformity", "depo_temp",
                   "chamber_pressure", "gas_flow"],
                  "气流分布不均、温度场不均、喷淋头堵塞或腔体状态漂移导致片内厚度分布偏差",
                  ["片内多点厚度图（等高线）", "温度场与气流场记录",
                   "喷淋头/腔体清洁周期", "同腔体前后批次对比"],
                  ["是中心-边缘型还是单边型（单边多为气流或排气）",
                   "是否集中在某个腔体或某个位置", "清洁后是否恢复"],
                  ["腔体 PM 记录", "温度与气流量校验"],
                  "film_thickness", ["膜厚不均", "厚度不均", "膜厚均匀性差"]),
    "薄膜颗粒": D("semi_thinfilm",
                  ["particle_count", "film_thickness"],
                  "腔体壁沉积物剥落、前驱体残留、进气污染或晶圆搬运引入颗粒；"
                  "颗粒会造成局部遮挡形成针孔或短路",
                  ["颗粒地图与 adders（前后差值）", "腔体清洁周期与颗粒趋势",
                   "前驱体批次", "搬运机械手状态"],
                  ["颗粒是新增（adders）还是原有", "是否集中在晶圆某区域",
                   "清洁后是否下降"],
                  ["腔体清洁记录", "前驱体批次与纯度"],
                  "particle_count", ["薄膜颗粒", "颗粒污染", "颗粒超标"]),
    "刻蚀残留": D("semi_etch",
                  ["etch_rate", "etch_uniformity", "etch_selectivity",
                   "chamber_pressure"],
                  "刻蚀不足（速率偏低/时间不足）、选择比不当或局部气流不足导致底层未完全打开；"
                  "残留会在后续工序变成开路或漏电",
                  ["残留区域分布图", "刻蚀速率与时间记录", "终点检测曲线",
                   "选择比数据"],
                  ["残留集中在边缘还是中心", "终点检测是否提前触发",
                   "同腔体是否连续发生"],
                  ["刻蚀速率校验记录", "终点检测曲线"],
                  "etch_rate", ["刻蚀残留", "蚀刻残留", "刻蚀不净", "残留"]),
    "碟形凹陷": D("semi_cmp",
                  ["dishing", "down_force", "slurry_flow", "platen_speed"],
                  "研磨时同材料宽结构区因图形密度差异被过度去除，形成碟形凹坑；"
                  "与研磨压力、研磨液选择比和图形密度相关",
                  ["按图形密度分组的凹陷量", "研磨压力与转速记录",
                   "研磨液批次与流量", "AFM/台阶仪形貌数据"],
                  ["凹陷是否只出现在宽金属区（图形依赖=典型 dishing）",
                   "同批次不同图形密度是否差异明显", "研磨时间是否偏长"],
                  ["研磨参数受控记录", "研磨液批次"],
                  "dishing", ["碟形凹陷", "碟形", "dishing", "凹陷超差"]),
    "研磨划伤": D("semi_cmp",
                  ["particle_count", "down_force", "slurry_flow"],
                  "研磨液中大颗粒、压力过大或研磨垫状态不良导致表面划伤；"
                  "划伤多为方向性条纹",
                  ["划伤方向与分布图", "研磨液过滤状态与颗粒计数",
                   "研磨垫使用时长", "压力设定与实际值"],
                  ["划伤是否呈同一方向（方向性=研磨垫/颗粒）",
                   "换研磨液批次后是否消失", "研磨垫是否超期"],
                  ["研磨液过滤与颗粒数据", "研磨垫更换记录"],
                  "particle_count", ["研磨划伤", "划伤", "CMP划伤", "刮伤"]),
    "良率骤降": D("semi_metrology",
                  ["yield_rate", "defect_density", "wafer_cpk",
                   "particle_count"],
                  "良率是多因结果：常见为某台设备漂移、某层参数越界、"
                  "颗粒突增或来料批次异常。必须先按层/机台/批号拆分定位，"
                  "不可直接在整片上算相关",
                  ["按层/机台/批号拆分的良率对比", "Fail Bin 分布",
                   "Inline 参数与电性参数关联", "缺陷地图与颗粒 adders"],
                  ["骤降是阶跃还是渐变（阶跃多为设备/批次事件）",
                   "是否集中在某一台设备或某一层",
                   "同期是否有 PM、换料、换程序"],
                  ["设备 PM 与换料记录", "按机台拆分的 Inline 数据"],
                  "yield_rate",
                  ["良率骤降", "良率下降", "良率异常", "yield loss"],
                  response=True),
}


def _load(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def _existing_columns():
    cols = set()
    bad_files = []
    if os.path.join(ROOT, "lib") not in sys.path:
        sys.path.insert(0, ROOT)
    from lib import table as T
    for pat in ("examples/**/*.csv", "examples/**/*.xlsx"):
        for p in glob.glob(os.path.join(ROOT, pat), recursive=True):
            try:
                cols.update(T.read_any(p).columns)
            except Exception as e:
                bad_files.append((os.path.basename(p), str(e)[:40]))
    return cols, bad_files


def check_alias_collisions(cols):
    """别名不得是现有识别列的子串（本项目两次踩坑的根因）。"""
    bad = []
    groups = [("param", {k: v for k, v in PARAMS.items()}),
              ("defect", DEFECTS)]
    for kind, table in groups:
        for key, v in table.items():
            names = [key] + list(v.get("aliases") or [])
            for n in names:
                if not n or len(str(n)) < 2:
                    continue
                for c in cols:
                    if str(n) in c:
                        bad.append((kind, key, str(n), c))
    return bad


def validate():
    """写库前的一致性校验：悬空引用会让 rootcause 静默少算候选。"""
    errs = []
    for proc, pv in PROCESSES.items():
        for k in pv.get("key_params") or []:
            if k not in PARAMS:
                errs.append("工序 %s 的 key_params 引用了未注册参数 %s" % (proc, k))
        for k in pv.get("doe_factors") or []:
            if k not in PARAMS:
                errs.append("工序 %s 的 doe_factors 引用了未注册参数 %s" % (proc, k))
        for k in (pv.get("upstream_keys") or []) + (pv.get("downstream_keys") or []):
            if k not in PROCESSES:
                errs.append("工序 %s 的上下游引用了未注册工序 %s" % (proc, k))
    for dk, dv in DEFECTS.items():
        for k in dv.get("likely_params") or []:
            if k not in PARAMS:
                errs.append("缺陷 %s 的 likely_params 引用了未注册参数 %s" % (dk, k))
        pk = dv.get("param_key")
        if pk and pk not in PARAMS:
            errs.append("缺陷 %s 的 param_key 未注册：%s" % (dk, pk))
        if dv.get("process") not in PROCESSES:
            errs.append("缺陷 %s 的 process 未注册：%s" % (dk, dv.get("process")))
    # 行业注册引用的工序必须都注册
    for proc in INDUSTRY.get("processes") or []:
        if proc not in PROCESSES:
            errs.append("行业切片引用了未注册工序 %s" % proc)
    return errs


def build(dry=False):
    spec = _load(SPEC)
    ind = _load(IND)

    errs = validate()
    if errs:
        print("校验未通过，拒绝写库：")
        for e in errs:
            print("  · " + e)
        raise SystemExit(2)

    cols, bad_files = _existing_columns()
    if bad_files:
        print("以下示例文件读不了（已跳过，不影响列名扫描）：")
        for n, e in bad_files:
            print("  · %s：%s" % (n, e))
    bad = check_alias_collisions(cols)
    if bad:
        print("别名误命中现有列，拒绝写库：")
        for kind, key, n, c in bad[:20]:
            print("  · %s %s 的别名 %r 是列 %r 的子串" % (kind, key, n, c))
        raise SystemExit(2)

    procs = spec.setdefault("processes", {})
    params = spec.setdefault("params", {})
    defs = spec.setdefault("defect_rootcause", {})
    industries = ind.setdefault("industries", {}) if "industries" in ind else ind

    added = {"industry": 0, "processes": [], "params": [], "defects": []}
    if "semiconductor" not in industries:
        industries["semiconductor"] = dict(INDUSTRY)
        added["industry"] = 1
    for k, v in PROCESSES.items():
        if k not in procs:
            procs[k] = dict(v)
            added["processes"].append(k)
    for k, v in PARAMS.items():
        if k not in params:
            params[k] = dict(v)
            added["params"].append(k)
    for k, v in DEFECTS.items():
        if k not in defs:
            defs[k] = dict(v)
            added["defects"].append(k)

    print("行业登记 semiconductor：%s" % ("已新增" if added["industry"] else "已存在"))
    print("工序：新增 %d（%s）" % (len(added["processes"]),
                               "、".join(added["processes"]) or "无"))
    print("参数：新增 %d（总 %d）" % (len(added["params"]), len(params)))
    print("缺陷：新增 %d（总 %d）" % (len(added["defects"]), len(defs)))

    if dry:
        print("--check：只预览，未写库")
        return 0
    if not any(added.values()):
        print("无需写库（幂等）")
        return 0

    for path, obj in ((SPEC, spec), (IND, ind)):
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    print("已写库：%s、%s" % (os.path.basename(SPEC), os.path.basename(IND)))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="搭建半导体行业切片骨架（幂等）")
    ap.add_argument("--check", action="store_true", help="只预览不写库")
    a = ap.parse_args()
    sys.exit(build(dry=a.check))
