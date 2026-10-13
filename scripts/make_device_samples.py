# -*- coding: utf-8 -*-
"""
生成「同一批物理真值、不同厂商设备、不同导出格式」的样例组。

目的不是造好看的数据，而是验证设备适配层的正确性：
同一批工艺数据被 5 台不同设备用 5 种版式导出后，
分析系统应当识别出各自的设备与版式，归一之后给出**一致的统计结论**。
若结论不一致，说明适配层或单位换算有 bug。
"""

import os
import io
import sys
import csv
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import table as T  # noqa

HERE = os.path.dirname(os.path.abspath(__file__))
EX = os.path.join(os.path.dirname(HERE), "examples")
OUT = os.path.join(EX, "devices")
os.makedirs(OUT, exist_ok=True)

random.seed(20261010)


def write_csv(path, header, rows, preamble=None):
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        if preamble:
            for line in preamble:
                f.write(line + "\n")
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print("  %-52s %d 行 x %d 列" % (os.path.basename(path), len(rows), len(header)))


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0


def stdev(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return 0
    m = mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


print("生成多设备多版式样例组（底层同一批真值）")

# ============================================================ 真值源：涂布
src = T.read_any(os.path.join(EX, "01_涂布工艺数据.csv"))
times = src.column("时间")
areal = [T.to_number(v) for v in src.column("涂布面密度(g/m²)")]
solid = [T.to_number(v) for v in src.column("浆料固含(wt%)")]
speed = [T.to_number(v) for v in src.column("涂布速度(m/min)")]
z1 = [T.to_number(v) for v in src.column("烘箱一区温度(℃)")]
n = len(src.rows)

# ---------------------------------------------------------- A：基恩士测厚 竖表
# 设备导出惯例：测点纵向堆叠，末尾带统计行
rows = []
for i in range(0, n, 2):
    grp = "WP-%04d" % (i // 2 + 1)
    rows.append([grp, "厚み", "%.4f" % (areal[i] / 1000.0)])   # 故意用 mm
    rows.append([grp, "電池電圧", "%.4f" % (3.7 + random.gauss(0, 0.01))])
    if i + 1 < n:
        rows.append(["WP-%04d" % (i // 2 + 1), "", ""])
rows.append(["平均", "", "%.4f" % (mean(areal) / 1000.0)])
rows.append(["最大", "", "%.4f" % (max(areal) / 1000.0)])
rows.append(["最小", "", "%.4f" % (min(areal) / 1000.0)])
write_csv(os.path.join(OUT, "10_基恩士KEYENCE-LK-G3000_涂布测厚.csv"),
          ["No.", "項目", "測定値"],
          rows, preamble=["KEYENCE LK-G3000 Series, Controller Firmware Ver.1.23",
                          "Measurement Mode: THICKNESS / OUT1",
                          "Export: sample trigger"])

# ---------------------------------------------------------- B：涂布 MES 双行表头 + 合计行
# 两行表头：第一行工艺分组、第二行明细；末行合计；必须从 private 列名识别
rows = []
for i in range(n):
    # 7 列必须与二级表头一一对应：时间/固含量/走带速度/一区温度/面密度2/面密度/判定
    second = areal[i] * 0.998 + (0.4 if i % 7 == 0 else 0)
    rows.append([times[i], "%.2f" % solid[i], "%.1f" % speed[i], "%.1f" % z1[i],
                 "%.2f" % second, "%.2f" % areal[i],
                 "NG" if areal[i] > 108.5 else "OK"])
rows.append(["合计", "", "", "", "%.2f" % sum(areal), "%d NG" % sum(1 for a in areal if a > 108.5)])
preamble = ["浩能涂布机 MES 报表", "线体=涂布线-3  制表=自动导出"]
head1 = ["工艺参数", "工艺参数", "工艺参数", "工艺参数", "检测结果", "检测结果"]
head2 = ["固含量(%)", "走带速度(m/min)", "一区温度(℃)", "面密度2(g/m²)", "面密度(g/m²)", "判定"]
p = os.path.join(OUT, "11_涂布MES_浩能_双行表头带合计.csv")
with io.open(p, "w", encoding="utf-8", newline="") as f:
    for line in preamble:
        f.write(line + "\n")
    w = csv.writer(f)
    w.writerow(["时间"] + head1)
    w.writerow([""] + head2)
    w.writerows(rows)
print("  %-52s %d 行 x 6 列（双行表头）" % (os.path.basename(p), len(rows)))

# ---------------------------------------------------------- C：日置内阻仪 竖表 + 统计尾行
rows = []
for i in range(0, n, 2):
    grp = "CELL-%04d" % (i // 2 + 1)
    base_ir = 0.62 + (areal[i] - mean(areal)) * 0.004
    rows.append([grp, "内部抵抗(Ω)", "%.6f" % (base_ir / 1000.0)])  # 故意用 Ω
    rows.append([grp, "電池電圧(V)", "%.4f" % (3.72 - i * 0.00002)])
    rows.append([grp, "判定", "OK" if base_ir < 0.75 else "NG"])
rows.append(["平均", "", "%.6f" % 0.00062])
rows.append(["σn-1", "", "%.6f" % 0.00004])
write_csv(os.path.join(OUT, "12_日置HIOKI-BT3563_内阻电压.csv"),
          [" battery No.", "項目", "測定値"],
          rows, preamble=["HIOKI BT3563 BATTERY HiTESTER Ver 1.10",
                          "Comparator mode: Hi/Lo"])

# ============================================================ 真值源：硅片
src2 = T.read_any(os.path.join(EX, "04_硅片检测数据.csv"))
thk = [T.to_number(v) for v in src2.column("硅片厚度(μm)")]
ttv = [T.to_number(v) for v in src2.column("TTV(μm)")]
warp = [T.to_number(v) for v in src2.column("Warp(μm)")]
supp = src2.column("供应商")
n2 = len(src2.rows)

# ---------------------------------------------------------- D：硅片分选机 厚度单位 mm
rows = []
for i in range(n2):
    rows.append(["SL-%05d" % (i + 1), supp[i], "%.4f" % (thk[i] / 1000.0),
                 "%.4f" % (ttv[i] / 1000.0), "%.3f" % (warp[i] / 1000.0),
                 "A" if ttv[i] < 8 else "B"])
rows.append(["平均", "", "%.4f" % (mean(thk) / 1000.0), "%.4f" % (mean(ttv) / 1000.0),
             "%.4f" % (mean(warp) / 1000.0), ""])
write_csv(os.path.join(OUT, "13_硅片分选机_TTV翘曲_mm单位.csv"),
          ["硅片编号", "来料供应商", "THK(mm)", "TTV(mm)", "Warpage(mm)", "等级代码"], rows)

# ============================================================ 真值源：组件 IV
src3 = T.read_any(os.path.join(EX, "06_组件IV测试数据.csv"))
pmax = [T.to_number(v) for v in src3.column("Pmax(W)")]
voc = [T.to_number(v) for v in src3.column("Voc(V)")]
isc = [T.to_number(v) for v in src3.column("Isc(A)")]
ff = [T.to_number(v) for v in src3.column("FF(%)")]
rs = [T.to_number(v) for v in src3.column("Rs(Ω)")]
line = src3.column("产线")
n3 = len(src3.rows)

# ---------------------------------------------------------- E：HALM IV，Isc 用 mA
rows = []
for i in range(n3):
    rows.append(["MOD%06d" % (i + 1), line[i], "%.2f" % pmax[i], "%.4f" % voc[i],
                 "%.1f" % (isc[i] * 1000.0), "%.3f" % ff[i], "%.4f" % rs[i],
                 "%.1f" % (1000 + random.gauss(0, 8)), "%.1f" % (25 + random.gauss(0, 1.2))])
rows.append(["Mean", "", "%.2f" % mean(pmax), "%.4f" % mean(voc), "%.1f" % (mean(isc) * 1000),
             "%.3f" % mean(ff), "%.4f" % mean(rs), "", ""])
write_csv(os.path.join(OUT, "14_HALM-IV组件测试_Isc用mA.csv"),
          ["Serial No", "Line", "Pmax(W)", "Voc(V)", "Isc(mA)", "FF(%)", "Rs(Ω)",
           "Irr(W/m²)", "Tmod(℃)"], rows)

# ---------------------------------------------------------- F：未知设备，通用宽表兜底
rows = []
for i in range(0, n, 3):
    rows.append([times[i], "%.2f" % solid[i], "%.2f" % areal[i], "%.1f" % z1[i],
                 "%.2f" % (0.9 + abs(areal[i] - mean(areal)) * 0.12)])
write_csv(os.path.join(OUT, "15_某未知仪器_自定义列名.csv"),
          ["Timestamp", "SlurrySC", "CoatWeight", "OvenT1", "DefectPct"], rows)

print("\n样例组生成完毕 ->", OUT)
