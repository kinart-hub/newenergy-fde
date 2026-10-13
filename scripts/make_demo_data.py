# -*- coding: utf-8 -*-
"""
make_demo_data.py —— 生成六类新能源产线仿真数据

用途：
  1) 验证 newenergy-fde 分析流水线端到端可跑通
  2) 作为企业内部培训/演示样例

数据里刻意注入了真实的工艺相关结构与已知异常，
用于检验算法能否抓到（自检标准见文件末尾 docstring）。

用法： python scripts/make_demo_data.py
"""

import os
import csv
import math
import random
import datetime

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
random.seed(20261010)


def gauss(mu, sd):
    return random.gauss(mu, sd)


def w(path, header, rows):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, path)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(header)
        for r in rows:
            wr.writerow(r)
    print("生成 %s（%d 行）" % (p, len(rows)))


def ts(i, start="2026-09-01 08:00:00", step_min=10):
    d = datetime.datetime.strptime(start, "%Y-%m-%d %H:%M:%S") + datetime.timedelta(minutes=step_min * i)
    return d.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 1. 涂布
def coating(n=180):
    rows = []
    # 已知注入：第 120 行起，浆料固含下滑 2.5pt，导致面密度下降、针孔不良上升
    for i in range(n):
        drift = 1 if i < 120 else 0
        solid = gauss(52.0 - (0 if drift else 2.6), 0.55)
        visco = 3800 + (solid - 52) * 260 + gauss(0, 180)
        speed = gauss(45.0, 0.8)
        z1 = gauss(72.0, 1.2) + (0 if drift else 4.5)   # 一区温度异常升高
        z6 = gauss(105.0, 1.5)
        # 面密度 = f(固含, 速度) + 噪声
        ad = 108.0 + (solid - 52) * 2.4 - (speed - 45) * 1.1 + gauss(0, 0.62)
        moist = 120 + (z1 - 72) * 6 + gauss(0, 14)
        defect = max(0.0, 0.35 + (0 if drift else 2.4) + (52 - solid) * 0.55 + gauss(0, 0.22))
        rows.append([ts(i), "涂布机-%s" % random.choice(["A", "B"]),
                     "甲班" if (i // 40) % 2 == 0 else "乙班",
                     round(solid, 2), round(visco, 0), round(speed, 2),
                     round(z1, 1), round(z6, 1), round(ad, 2),
                     round(moist, 0), round(defect, 2)])
    w("01_涂布工艺数据.csv",
      ["时间", "设备号", "班次", "浆料固含(wt%)", "浆料粘度(mPa·s)", "涂布速度(m/min)",
       "烘箱一区温度(℃)", "烘箱末区温度(℃)", "涂布面密度(g/m²)", "极片含水量(ppm)", "不良率(%)"],
      rows)


# ---------------------------------------------------------------- 2. 卷绕
def winding(n=160):
    rows = []
    for i in range(n):
        mach = random.choice(["W-01", "W-02", "W-03"])
        # 已知注入：W-02 隔膜张力偏高，导致褶皱不良集中在 W-02
        base_sep = 3.2 if mach != "W-02" else 5.8
        ta = gauss(16.0, 0.45)
        ts_ = gauss(base_sep, 0.28)
        sp = gauss(28.0, 0.9)
        mis = gauss(0.28, 0.08) + (0.22 if mach == "W-02" else 0)
        hipot = 0 if random.random() > (0.012 if mach != "W-03" else 0.075) else 1
        prod = random.randint(180, 220)
        rows.append([ts(i, step_min=8), mach, "甲班" if (i // 40) % 2 == 0 else "乙班",
                     round(ta, 2), round(ts_, 2), round(sp, 1),
                     round(mis, 3), hipot, prod])
    w("02_卷绕工艺数据.csv",
      ["时间", "卷绕机号", "班次", "负极张力(N)", "隔膜张力(N)", "卷绕速度(ppm)",
       "极片错位(mm)", "Hi-pot不良数", "生产数"], rows)


# ---------------------------------------------------------------- 3. 化成
def formation(n=200):
    rows = []
    for i in range(n):
        cab = random.choice(["F-01", "F-02", "F-03"])
        # 已知注入：F-03 柜温偏高 4℃，容量与首效下降、内阻上升
        tf = gauss(32.0 + (4.2 if cab == "F-03" else 0), 0.9)
        cur = gauss(0.05, 0.002)
        cap = 52.0 - (tf - 32) * 0.42 + gauss(0, 0.28)
        acir = 0.82 + (tf - 32) * 0.045 + gauss(0, 0.035)
        k = max(0.02, 0.16 + (tf - 32) * 0.06 + gauss(0, 0.05))
        ice = 91.5 - (tf - 32) * 0.55 + gauss(0, 0.35)
        rows.append([ts(i, step_min=6), cab, "CH%d" % random.randint(1, 12),
                     round(tf, 1), round(cur, 4), round(cap, 3),
                     round(acir, 4), round(k, 3), round(ice, 2)])
    w("03_化成分容数据.csv",
      ["时间", "化成柜号", "通道", "化成温度(℃)", "化成电流(C)",
       "分容容量(Ah)", "交流内阻(mΩ)", "自放电K值(mV/d)", "首次库仑效率(%)"], rows)


# ---------------------------------------------------------------- 4. 硅片
def wafer(n=150):
    rows = []
    for i in range(n):
        sup = random.choice(["SP-A", "SP-B", "SP-C"])
        # 已知注入：SP-C 的 TTV 与 Warp 明显差
        th = gauss(150.0, 2.2)
        ttv = gauss(8.5 + (5.5 if sup == "SP-C" else 0), 1.4)
        warp = gauss(28 + (16 if sup == "SP-C" else 0), 4.5)
        res = gauss(0.85, 0.06)
        life = gauss(180, 22)
        rows.append([ts(i, step_min=5), "切片机-%d" % random.randint(1, 4), sup,
                     round(th, 2), round(ttv, 2), round(warp, 1),
                     round(res, 3), round(life, 1)])
    w("04_硅片检测数据.csv",
      ["时间", "机台号", "供应商", "硅片厚度(μm)", "TTV(μm)", "Warp(μm)",
       "电阻率(Ω·cm)", "少子寿命(μs)"], rows)


# ---------------------------------------------------------------- 5. EL 缺陷
def el(n=120):
    rows = []
    types = ["隐裂", "黑心", "断栅", "碎片", "虚焊", "脏污", "边缘漏电"]
    for i in range(n):
        row = [ts(i, step_min=15), "线体-%d" % random.randint(1, 3)]
        prod = random.randint(900, 1100)
        total = 0
        for t in types:
            base = {"隐裂": 6, "黑心": 2, "断栅": 4, "碎片": 3, "虚焊": 2, "脏污": 5, "边缘漏电": 1}[t]
            c = max(0, int(random.gauss(base, base * 0.4)))
            row.append(c)
            total += c
        row.append(prod)
        row.append(round(total / prod * 100, 2))
        rows.append(row)
    w("05_EL缺陷检测数据.csv",
      ["时间", "线体", "隐裂", "黑心", "断栅", "碎片", "虚焊", "脏污", "边缘漏电",
       "生产数", "不良率(%)"], rows)


# ---------------------------------------------------------------- 6. 组件 IV
def module_iv(n=140):
    rows = []
    for i in range(n):
        line = random.choice(["L1", "L2"])
        # 已知注入：L2 的 Rs 偏高（焊接问题），FF 与 Pmax 下降
        rs = gauss(0.34 + (0.16 if line == "L2" else 0), 0.025)
        rsh = gauss(320, 45)
        voc = gauss(49.8, 0.15)
        isc = gauss(13.9, 0.08)
        ff = 79.8 - (rs - 0.34) * 22 + gauss(0, 0.5)
        pmax = voc * isc * ff / 100 + gauss(0, 1.2)
        ctm = gauss(99.2 - (rs - 0.34) * 12, 0.4)
        rows.append([ts(i, step_min=12), line, "M10-182",
                     round(pmax, 2), round(voc, 3), round(isc, 3),
                     round(ff, 2), round(rs, 4), round(rsh, 1), round(ctm, 2)])
    w("06_组件IV测试数据.csv",
      ["时间", "产线", "组件型号", "Pmax(W)", "Voc(V)", "Isc(A)", "FF(%)",
       "Rs(Ω)", "Rsh(Ω)", "CTM(%)"], rows)


if __name__ == "__main__":
    coating()
    winding()
    formation()
    wafer()
    el()
    module_iv()
    print("\n自检标准（算法应能抓到）：")
    print("  涂布：第120行后面密度/固含台阶，固含与面密度强正相关；烘箱一区温度与含水量正相关")
    print("  卷绕：W-02 隔膜张力偏高，按机台分层 ANOVA 应显著，Hi-pot 在 W-03 集中")
    print("  化成：F-03 柜温 +4℃，容量/首效下降、内阻/K值上升，按柜号分层显著")
    print("  硅片：SP-C 的 TTV/Warp 显著偏高，按供应商分层显著")
    print("  EL  ：隐裂+脏污+断栅应进 Pareto A 类")
    print("  组件：L2 的 Rs 偏高，FF/Pmax 下降，Rs 与 FF 强负相关")
