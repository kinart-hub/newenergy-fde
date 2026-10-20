# -*- coding: utf-8 -*-
"""
【参考实现 / 非运行时代码】pandas 版 FDE 分析流程

这份代码是**参考实现**，不是 newenergy-fde 的运行时组成部分。它存在的意义是
提供一条"pandas 生态写法"的对照路径，方便理解和二次开发，**不会被打进分发包**
（见 scripts/package.py 的 EXCLUDE_DIRS）。

为什么主线引擎不这么做
------------------------
本项目铁律之一是**零第三方依赖**：全部用 Python 标准库实现 CSV/XLSX 读写、
统计、SPC、DOE、SVG 图表。原因不是不会用 pandas / numpy / scipy，而是：

  · 企业内网装包是真实成本。见过太多"分析方案很好，但客户机器上装不上"的情况。
  · 分发包要做到"拷过去就能跑，数据不出厂"。

所以本文件仅供以下场景使用：
  1. 内部已有 pandas 环境，做一次性探索分析；
  2. 想把本项目的方法论嵌进自己的 pandas 流水线；
  3. 对照阅读两种实现的取舍。

已知局限（相对主线引擎）
------------------------
  1. 控制限用全段数据计算（含异常点），异常会把 σ 撑大、限变宽，
     最该报警的点反而落在限内。主线引擎已用 SPC Phase I/II 分离修掉。
  2. 参数阈值（涂布 80~120、硅片 140~180）写死在代码里。
     主线引擎要求规格由调用方通过 --spec 传入，不做行业特例。
  3. 输出只有 Excel，没有工艺分析报告 / 8D 初稿 / 图表。
  4. 不做工序自动判定、设备档案归一、跨工序追溯、确审闸门。
  5. TMP_OUT_PATH 用了 /tmp，在 Windows 上不存在。

运行前需要：pip install pandas numpy scipy openpyxl
"""

import os
import pandas as pd
import numpy as np
from scipy import stats

# ====================== 【配置区 - 按需修改参数】======================
# WorkBuddy容器固定临时目录，请勿修改
TMP_OUT_PATH = "/tmp/fde_analysis_result.xlsx"
SUPPORT_EXT = [".xlsx", ".csv"]
PROCESS_LIST = ["涂布", "卷绕", "化成", "硅片检测", "组件EL检测"]
# SPC 8项判异开关
ENABLE_SPC_RULES = True
# Pareto 缺陷累计阈值
PARETO_CUM_THRESHOLD = 0.8
# ====================================================================

def spc_8rules_check(series: pd.Series, usl=None, lsl=None) -> pd.DataFrame:
    """
    SPC 八大判异规则，返回每个数据点异常标记
    规则：
    1. 任一点超出UCL/LCL
    2. 连续9点在中心线同一侧
    3. 连续6点递增或递减
    4. 连续14点上下交替
    5. 连续3点中有2点在A区外（同侧）
    6. 连续5点中有4点在B区外（同侧）
    7. 连续15点在C区上下
    8. 连续8点在中心线两侧，无一点在C区
    """
    data = series.dropna().reset_index(drop=True)
    n = len(data)
    if n < 20:
        return pd.DataFrame()
    x_bar = data.mean()
    sigma = data.std(ddof=1)
    ucl = x_bar + 3 * sigma
    lcl = x_bar - 3 * sigma
    # 分区
    zoneA_upper = x_bar + 2*sigma
    zoneB_upper = x_bar + 1*sigma
    zoneB_lower = x_bar - 1*sigma
    zoneA_lower = x_bar - 2*sigma

    result = []
    for idx, val in data.items():
        rule_hits = []
        # Rule1
        if val > ucl or val < lcl:
            rule_hits.append("R1")
        # R2:连续9同侧
        if idx >=8:
            window = data.iloc[idx-8:idx+1]
            if all(window > x_bar) or all(window < x_bar):
                rule_hits.append("R2")
        # R3:连续6递增递减
        if idx >=5:
            w = data.iloc[idx-5:idx+1].values
            inc = all(np.diff(w) > 0)
            dec = all(np.diff(w) < 0)
            if inc or dec:
                rule_hits.append("R3")
        # R4:连续14交替
        if idx >=13:
            w = data.iloc[idx-13:idx+1].values
            diff = np.sign(np.diff(w))
            if all(diff[::2] != diff[1::2]):
                rule_hits.append("R4")
        # R5:3中2在A外同侧
        if idx >=2:
            w = data.iloc[idx-2:idx+1]
            aboveA = ((w>zoneA_upper).sum() >=2)
            belowA = ((w<zoneA_lower).sum() >=2)
            if aboveA or belowA:
                rule_hits.append("R5")
        # R6:5中4在B外同侧
        if idx >=4:
            w = data.iloc[idx-4:idx+1]
            aboveB = ((w>zoneB_upper).sum()>=4)
            belowB = ((w<zoneB_lower).sum()>=4)
            if aboveB or belowB:
                rule_hits.append("R6")
        # R7:连续15在C区
        if idx >=14:
            w = data.iloc[idx-14:idx+1]
            if ((w >= zoneB_lower) & (w <= zoneB_upper)).sum() ==15:
                rule_hits.append("R7")
        # R8:连续8点在两侧，无C区
        if idx >=7:
            w = data.iloc[idx-7:idx+1]
            inC = ((w >= zoneB_lower) & (w <= zoneB_upper)).sum()
            side = np.sign(w - x_bar)
            if inC ==0 and (any(side>0) and any(side<0)):
                rule_hits.append("R8")
        result.append({
            "index": idx,
            "value": val,
            "CL":x_bar, "UCL":ucl, "LCL":lcl,
            "abnormal_rules": ",".join(rule_hits) if rule_hits else "normal"
        })
    return pd.DataFrame(result)


def pareto_analysis(df, defect_col="缺陷类型"):
    """Pareto柏拉图统计，计算频次、占比、累计占比"""
    cnt_df = df[defect_col].value_counts().reset_index()
    cnt_df.columns = [defect_col, "数量"]
    total = cnt_df["数量"].sum()
    cnt_df["占比"] = cnt_df["数量"] / total
    cnt_df["累计占比"] = cnt_df["占比"].cumsum()
    cnt_df["Pareto分类"] = np.where(cnt_df["累计占比"] <= PARETO_CUM_THRESHOLD, "A类关键", "B/C类次要")
    return cnt_df


def fde_skill_main(input_file_path: str) -> str:
    """
    WorkBuddy Skill入口函数，平台自动调用
    :param input_file_path: 平台上传文件临时路径
    :return: 输出文件路径，给到WorkBud下载接口
    """
    try:
        # ========== 阶段1：读取原始文件 + 埋点日志【定位读取空数据问题】 ==========
        print(f"【FDE Phase3日志】开始读取输入文件: {input_file_path}")
        file_ext = os.path.splitext(input_file_path)[1].lower()

        if file_ext == ".csv":
            try:
                df_raw = pd.read_csv(input_file_path, encoding="utf-8-sig")
            except:
                df_raw = pd.read_csv(input_file_path, encoding="gbk")
        elif file_ext == ".xlsx":
            df_raw = pd.read_excel(input_file_path, engine="openpyxl")
        else:
            raise Exception(f"不支持文件类型{file_ext}，仅支持xlsx/csv")

        print(f"【FDE Phase3日志】原始数据读取完成 | 总行数:{df_raw.shape[0]}, 总列数:{df_raw.shape[1]}")
        print(f"【FDE Phase3日志】原始列名: {list(df_raw.columns)}")

        if df_raw.empty:
            raise ValueError("原始上传文件读取结果为空！检查文件、表头、合并单元格、文件损坏")

        # ========== 阶段2：工艺数据清洗（锂电/光伏通用，按需修改过滤规则） ==========
        df_clean = df_raw.copy()
        print(f"【FDE Phase3日志】清洗前行数：{df_clean.shape[0]}")

        # ===== 【业务区：你的工艺过滤、参数映射、工序识别，在这里修改】=====
        # 示例：空值剔除（只剔除关键工艺参数为空的行，按需注释/修改）
        key_cols = ["工序","设备ID","涂布厚度","极片张力","化成电压","硅片厚度","EL缺陷"]
        exist_key = [c for c in key_cols if c in df_clean.columns]
        if exist_key:
            df_clean = df_clean.dropna(subset=exist_key)

        # 异常标记：根据工艺阈值标记不良，按需修改阈值
        if "涂布厚度" in df_clean.columns:
            df_clean["异常标记"] = np.where((df_clean["涂布厚度"] <80) | (df_clean["涂布厚度"]>120), "超差", "正常")
        elif "硅片厚度" in df_clean.columns:
            df_clean["异常标记"] = np.where((df_clean["硅片厚度"] <140) | (df_clean["硅片厚度"]>180), "超差", "正常")
        else:
            df_clean["异常标记"] = "未判定"
        # =====================================================================

        print(f"【FDE Phase3日志】清洗&工艺处理完成，剩余有效数据行数: {df_clean.shape[0]}")
        if df_clean.empty:
            raise Warning("⚠️工艺过滤后数据为空，请检查筛选条件，参数过滤把所有数据剔除")

        # ========== 阶段3：SPC分析模块 ==========
        df_spc_out = pd.DataFrame()
        spc_target_col = None
        # 自动选择优先分析的工艺参数
        for col in ["涂布厚度","硅片厚度","极片张力","化成电压"]:
            if col in df_clean.columns:
                spc_target_col = col
                break
        if ENABLE_SPC_RULES and spc_target_col and len(df_clean)>=20:
            df_spc_out = spc_8rules_check(df_clean[spc_target_col])
            df_spc_out["分析参数"] = spc_target_col
            print(f"【FDE Phase3日志】SPC分析完成，参数:{spc_target_col}, 检出异常点数：{len(df_spc_out[df_spc_out['abnormal_rules']!='normal'])}")

        # ========== 阶段4：Pareto柏拉图分析模块 ==========
        df_pareto = pd.DataFrame()
        defect_col = None
        for c in ["EL缺陷","异常标记","缺陷类型"]:
            if c in df_clean.columns:
                defect_col = c
                break
        if defect_col is not None:
            df_pareto = pareto_analysis(df_clean, defect_col)
            print(f"【FDE Phase3日志】Pareto分析完成，缺陷分类数量：{len(df_pareto)}")

        # ========== 阶段5：异常汇总表 ==========
        df_abnormal_summary = df_clean[df_clean["异常标记"]=="超差"].copy()
        print(f"【FDE Phase3日志】异常汇总：超差行数={len(df_abnormal_summary)}")

        # ========== 阶段6：多Sheet写入Excel（5个工作表） ==========
        with pd.ExcelWriter(TMP_OUT_PATH, engine="openpyxl") as writer:
            df_raw.to_excel(writer, sheet_name="1_原始数据", index=False)
            df_clean.to_excel(writer, sheet_name="2_清洗数据", index=False)
            df_abnormal_summary.to_excel(writer, sheet_name="3_异常汇总", index=False)
            df_spc_out.to_excel(writer, sheet_name="4_SPC统计", index=False)
            df_pareto.to_excel(writer, sheet_name="5_Pareto柏拉图", index=False)

        # 文件校验，防止空文件
        file_size = os.path.getsize(TMP_OUT_PATH)
        print(f"【FDE Phase3日志】输出多Sheet文件写入完成，文件字节大小: {file_size}")
        if file_size <= 100:
            raise Exception("生成文件过小，大概率是空文件！写入异常")

        return TMP_OUT_PATH

    except Exception as e:
        err_msg = f"【FDE Phase3异常】{str(e)}"
        print(err_msg)
        # 异常兜底：输出报错信息Excel
        err_df = pd.DataFrame([[err_msg]], columns=["FDE运行报错信息"])
        err_df.to_excel(TMP_OUT_PATH, index=False, engine="openpyxl")
        return TMP_OUT_PATH


# ========= WorkBuddy平台Skill调用入口（平台固定调用，不要删除）=========
if __name__ == "__main__":
    # 本地调试用，部署WorkBuddy不会执行
    test_file = "/tmp/test_input.xlsx"
    result_file = fde_skill_main(test_file)
    print(f"本地调试，输出文件：{result_file}")
