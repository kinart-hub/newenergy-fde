# -*- coding: utf-8 -*-
"""
make_crossprocess_demo.py —— 生成三组「同批次、跨三道工序」的对照样例

三组数据刻意植入三种不同的技术成因，用于验证跨工序追溯能否判对：

  A 组 PRJ-A（本工序主导）：上游两项参数全程平稳； filament 卷绕张力在 09:40 后漂移，
     错位随之恶化。判定应为「本工序主导」。
  B 组 PRJ-B（上游不合格流入）：上游注液的含水量在后段真的越过 25ppm 上限，
     自放电随之恶化。判定应为「上游不合格流入」。
  C 组 PRJ-C（上游合格但边缘）：最阴险的一组。上游涂布面密度始终在 105±3 的规格内
     （上游检测当然全部合格），但坏件对应的面密度均值显著偏高 1.5 g/m²。
     上游报警不了，容量一致性却在下游崩掉。判定应为「上游合格但边缘」。

三组网同样的 120 个批次号，便于直接 join。
"""

import os
import csv
import math
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "examples", "crossprocess")

N = 120
BATCH = ["B%04d" % (2600 + i) for i in range(N)]


def _w(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _jit(seed, base, sd, n=N):
    r = random.Random(seed)
    return [round(base + r.gauss(0, sd), 4) for _ in range(n)]


def _machines(seed, n=N, bad=None, lift=0.0):
    """
    生成机台号与班次两个横向排查维度。
    bad 指定的那台设备被刻意抬高 lift —— 用来验证横向排查能不能把它揪出来。
    """
    r = random.Random(seed)
    mc = [r.choice(["C-01", "C-02", "C-03"]) for _ in range(n)]
    shift = [("甲班" if i % 2 == 0 else "乙班") for i in range(n)]
    lift_vec = [lift if m == bad else 0.0 for m in mc]
    return mc, shift, lift_vec


def group_a():
    """本工序主导：上游平稳，本工序张力在后段漂移；且 C-02 机台额外抬高（横向排查应命中）"""
    up_tension = _jit(11, 22.0, 0.30)          # 上游（calendering）轧制张力：全程平稳
    up_thick = _jit(12, 148.0, 0.45)           # 上游辊压后厚度：全程平稳
    shift = [0.0] * 70 + [0.55] * 50           # 09:40 起本工序张力阶跃 +0.55 N
    cur_tension = [round(a + b, 3) for a, b in zip(_jit(13, 42.0, 0.35), shift)]
    mc, sh, lift = _machines(41, N, bad="C-02", lift=0.18)   # C-02 这批额外偏大 0.18mm
    r = random.Random(14)
    # 错位(mm) 跟随本工序张力，再叠加 C-02 这台的设备偏差
    mis = [round(0.10 + 0.62 * max(0.0, t - 42.0) * 2.0 + l + r.gauss(0, 0.025), 4)
           for t, l in zip(cur_tension, lift)]
    _w(os.path.join(OUT, "A", "01_上游-辊压.csv"),
        ["批次号", "压实密度(g/cm³)", "轧制张力(N)", "辊压后厚度(μm)"],
        [[BATCH[i], round(3.55 + r.gauss(0, 0.012), 4), up_tension[i], up_thick[i]]
         for i in range(N)])
    _w(os.path.join(OUT, "A", "02_本工序-卷绕.csv"),
        ["批次号", "机台号", "班次", "卷绕张力(N)", "对位偏差(mm)", "CCD闭环延迟(ms)"],
        [[BATCH[i], mc[i], sh[i], cur_tension[i], mis[i], round(18 + abs(r.gauss(0, 3)), 2)]
         for i in range(N)])


def group_b():
    """上游不合格流入：上游含水量在后段真的越过 25ppm 上限"""
    r = random.Random(21)
    # 前 80 件含水量正常；后 40 件含水量失控越过上限 25ppm
    moisture = _jit(22, 14.0, 2.0)[:80] + _jit(23, 33.0, 3.0)[:40]
    inj = _jit(24, 4.35, 0.03)                  # 注液量：平稳
    cur_temp = _jit(25, 45.0, 0.4)              # 本工序化成温度：平稳
    rr = random.Random(26)
    mc, sh, _ = _machines(43, N)               # 刻意不设 bad：B/C 组机台间无差异，作为阴性对照
    selfdis = [round(0.9 + max(0.0, m - 24.0) * 0.13 + rr.gauss(0, 0.05), 4)
               for m in moisture]
    _w(os.path.join(OUT, "B", "01_上游-注液.csv"),
        ["批次号", "极片含水量(ppm)", "注液量(g)", "露点(℃)"],
        [[BATCH[i], round(moisture[i], 2), inj[i], round(-42 + rr.gauss(0, 1.2), 2)]
         for i in range(N)])
    _w(os.path.join(OUT, "B", "02_本工序-化成.csv"),
        ["批次号", "机台号", "班次", "化成温度(℃)", "自放电(mV/d)", "化成压力(MPa)"],
        [[BATCH[i], mc[i], sh[i], cur_temp[i], selfdis[i], round(0.55 + rr.gauss(0, 0.02), 4)]
         for i in range(N)])


def group_c():
    """上游合格但边缘：面密度始终在上游规格 105±3 内，但均值漂到上限侧"""
    r = random.Random(31)
    # 后 60 件：面密度仍在 [102,108] 规格内，但均值由 104.8 抬到 106.6（差 1.8，约 1.7%）
    ad = _jit(32, 104.8, 0.55)[:60] + _jit(33, 106.55, 0.55)[:60]
    ad = [min(107.9, max(102.1, x)) for x in ad]          # 保证始终在规格内，硬约束
    solid = _jit(34, 52.0, 0.30)                           # 浆料固含：平稳
    cur_formation_temp = _jit(35, 45.0, 0.35)              # 本工序化成温度：平稳
    rr = random.Random(36)
    # 容量一致性偏差只跟随上游面密度（即使它完全合格）
    mc, sh, _ = _machines(45, N)               # 阴性对照：C 组的问题是来料带来的，不该在机台上聚集
    cap = [round(abs(a - 105.0) * 0.115 + rr.gauss(0, 0.045), 4) for a in ad]
    _w(os.path.join(OUT, "C", "01_上游-涂布.csv"),
        ["批次号", "涂布面密度(g/m²)", "浆料固含(wt%)", "涂布速度(m/min)"],
        [[BATCH[i], ad[i], solid[i], round(45.0 + rr.gauss(0, 0.4), 2)]
         for i in range(N)])
    # 注意：化成电流在知识库里登记的单位是 C-rate（典型 0.02-0.1C），写成 A 会被判单位错配
    _w(os.path.join(OUT, "C", "02_本工序-化成.csv"),
        ["批次号", "机台号", "班次", "化成温度(℃)", "容量标准差(%)", "化成电流(C)"],
        [[BATCH[i], mc[i], sh[i], cur_formation_temp[i], cap[i], round(0.05 + rr.gauss(0, 0.004), 4)]
         for i in range(N)])


def main():
    group_a()
    group_b()
    group_c()
    print("已生成三组跨工序对照样例：%s" % OUT)
    for g in ("A", "B", "C"):
        d = os.path.join(OUT, g)
        print("  %s 组：%s" % (g, sorted(os.listdir(d))))


if __name__ == "__main__":
    main()
