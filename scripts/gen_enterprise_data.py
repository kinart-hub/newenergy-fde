# -*- coding: utf-8 -*-
"""
生成「企业端仿真数据」——用来检验 Agent 在真实场景下判得准不准。

原则：
1. 每套数据都植入**已知真值**（可控的因果链），没有真值就无法判断 Agent 判得对不对。
2. 每套都埋一个**陷阱**：看起来像真因、其实是正常物理规律或无关变量。
   判不出真值叫漏判，把陷阱当真因叫误判——后者在产线上更致命（会去调不该调的参数）。
3. 格式按真实设备导出的脏样子来：双行表头、合计尾行、BOM、单位混用、空值脏字符。

用法：python scripts/gen_enterprise_data.py
输出：examples/enterprises/<场景>/  （数据 + 企业规格书 + truth.json 真相卡）
"""
import os
import io
import csv
import json
import random
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "examples", "enterprises")


SEED_BASE = 20260901


def _seed(offset=0):
    """场景种子 = 基准 + 偏移；sweep 时换基准即可批量造出不同变体"""
    return SEED_BASE + offset


def R(seed):
    return random.Random(seed)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def g(r, mu, sd, lo=None, hi=None):
    v = r.gauss(mu, sd)
    return clamp(v, lo, hi) if (lo is not None and hi is not None) else v


def wr(path, header, rows, unit_row=None, footer=None, bom=False, dirty=None):
    """写 CSV。unit_row=第二行单位（双行表头）；footer=合计尾行；bom=utf-8-sig"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    enc = "utf-8-sig" if bom else "utf-8"
    with io.open(path, "w", encoding=enc, newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        if unit_row:
            w.writerow(unit_row)
        for r in rows:
            w.writerow(r)
        if footer:
            w.writerow(footer)
    return path


def save_case(cid, files, spec, truth):
    d = os.path.join(OUT, cid)
    os.makedirs(d, exist_ok=True)
    spec_path = os.path.join(d, "spec_企业规格书.json")
    with io.open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    truth["spec_file"] = "spec_企业规格书.json"
    with io.open(os.path.join(d, "truth.json"), "w", encoding="utf-8") as f:
        json.dump(truth, f, ensure_ascii=False, indent=2)
    print("  %-6s %-34s 文件 %s 个 | 真值：%s" % (cid, truth["title"], len(files), truth["primary_cause"]))
    return d


# ─────────────────────────────────────────────────────────────
# E01 锂电涂布 —— 真因：烘箱二区温度渐变漂移；陷阱：涂布速度 vs 面密度
# ─────────────────────────────────────────────────────────────
def gen_e01():
    r = R(_seed(0))
    n = 200
    d0 = datetime.date(2026, 9, 1)
    hdr = ["批次号", "生产日期", "涂布速度", "单面面密度", "涂布厚度", "烘箱一区", "烘箱二区",
           "烘箱三区", "烘箱末区", "浆料粘度", "浆料固含", "极片残水", "剥离强度", "涂布张力",
           "掉料", "气泡", "针孔", "划痕", "厚边"]
    unit = ["", "", "m/min", "g/m2", "um", "℃", "℃", "℃", "℃", "mPa·s", "wt%", "ppm", "N/m", "N",
            "片", "片", "片", "片", "片"]
    rows = []
    for i in range(n):
        v = g(r, 38, 3.0, 30, 46)
        ad = 200 - 0.6 * (v - 38) + r.gauss(0, 2.0)          # 物理规律：速度快→面密度低（陷阱）
        t1, t3, tl = g(r, 85, 1.5), g(r, 105, 1.5), g(r, 75, 1.5)
        # 真因：110 批后二区温度渐变漂移 -12℃（不是突变，考验 SPC 趋势判异）
        t2 = g(r, 95, 1.2) if i < 110 else (95 - 12.0 * (i - 109) / 90.0 + r.gauss(0, 1.2))
        moist = max(60.0, 168 + 26.0 * (95 - t2) + r.gauss(0, 22))   # 残水随二区温度劣化
        adh = 12.2 - 0.0130 * (moist - 168) + r.gauss(0, 0.45)       # 残水高→剥离强度掉
        thk = ad * 0.28 + r.gauss(0, 1.2)
        over = max(0.0, moist - 300) / 40.0
        rows.append([
            "B26-09%03d" % (i + 1), (d0 + datetime.timedelta(days=i // 24)).isoformat(),
            round(v, 1), round(ad, 1), round(thk, 1), round(t1, 1), round(t2, 1), round(t3, 1),
            round(tl, 1), round(r.gauss(3500, 180)), round(g(r, 62.5, 0.5), 2),
            round(moist, 0), round(adh, 2), round(g(r, 45, 2.5), 1),
            int(max(0, 3 + (over ** 1.3) * 9 + r.gauss(0, 1.2))),
            int(max(0, 5 + max(0.0, (moist - 320) / 50.0) * 3 + r.gauss(0, 1.5))),
            int(max(0, 2 + r.gauss(0, 1.0))),
            int(max(0, 4 + r.gauss(0, 1.3))),
            int(max(0, 6 + r.gauss(0, 1.8)))])
    # 合计尾行（真实 MES 导出的常见脏东西）
    foot = ["合计"] + [""] * 12 + [sum(x[14] for x in rows), sum(x[15] for x in rows),
                                  sum(x[16] for x in rows), sum(x[17] for x in rows),
                                  sum(x[18] for x in rows)]
    p = wr(os.path.join(OUT, "E01_锂电涂布", "01_涂布机MES导出_双行表头带合计.csv"),
           hdr, rows, unit_row=unit, footer=foot)
    spec = {"极片残水(ppm)": {"lsl": 0, "usl": 300},
            "剥离强度(N/m)": {"lsl": 10.0},
            "单面面密度(g/m2)": {"lsl": 190, "usl": 210, "target": 200},
            "烘箱二区(℃)": {"lsl": 88, "usl": 100, "target": 95}}
    save_case("E01_锂电涂布", [p], spec, {
        "title": "方形铝壳厂·极片涂布（双行表头+合计尾行）",
        "industry": "battery", "process": "battery_coating",
        "device": "浩能涂布机 MES 导出", "format_traps": ["双行表头", "尾部合计行", "单位在表头第二行"],
        "n": n,
        "primary_cause": "烘箱二区温度自第130批起渐变漂移 -9℃（95→86℃）",
        "mechanism": "二区温度↓ → 极片残水↑（168→约390ppm，超 spec 300）→ 剥离强度↓（12.2→9.4 N/m，跌破 10）→ 掉料不良↑",
        "expected": ["残水判超规格", "二区温度 SPC 趋势判异（渐变漂移，非突变）",
                     "残水与二区温度强负相关", "Pareto 掉料居首", "剥离强度 Cpk 不足"],
        "traps": [{"name": "涂布速度 vs 单面面密度 r≈-0.67 强相关",
                   "why_not": "速度越快涂布量越低是正常物理规律，且面密度 Cpk 合格；把它当真因会去调车速，越调越糟"}],
        "biz_value": "残水超标若未拦截，后工序卷绕/注液会批量起鼓，返工成本极高"})


# ─────────────────────────────────────────────────────────────
# E02 卷绕（跨工序）—— 真因：卷针 N-03 张力异常；陷阱：上游厚边弱相关
# ─────────────────────────────────────────────────────────────
def gen_e02():
    r = R(_seed(1))
    n = 240
    up_rows = []
    thick_edge = {}
    for i in range(n):
        tl = g(r, 56, 1.5); tr = tl + r.gauss(0, 1.2)
        edge = abs(r.gauss(3.2, 1.4))                 # 厚边量（弱干扰因子）
        thick_edge["B26-09%03d" % (i + 1)] = edge
        up_rows.append(["B26-09%03d" % (i + 1), round(tl, 1), round(tr, 1), round(edge, 2)])
    p1 = wr(os.path.join(OUT, "E02_锂电卷绕", "01_上游-涂布抽检.csv"),
            ["批次号", "涂布厚度左", "涂布厚度右", "厚边量"], up_rows,
            unit_row=["", "um", "um", "um"])

    rows = []
    needles = ["N-01", "N-02", "N-03", "N-04"]
    for i in range(n):
        lot = "B26-09%03d" % (i + 1)
        nd = needles[i % 4]
        # 真因：N-03 实测张力比设定高约 8N（硬件/气缸偏差）
        ten = g(r, 30.0, 1.2) if nd == "N-03" else g(r, 22.0, 1.0)
        mis = 0.12 + 0.030 * (ten - 22.0) + 0.020 * thick_edge[lot] + r.gauss(0, 0.05)
        mis = max(0.02, mis)
        ov = g(r, 1.20, 0.15)
        ccd = mis * 0.5 + r.gauss(0, 0.03)
        pfail = 0.18 if nd == "N-03" else 0.02
        hipot = "FAIL" if r.random() < pfail else "PASS"
        rows.append(["JR%04d" % (i + 1), lot, nd, round(ten, 1), round(mis, 3), round(ov, 2),
                     round(ccd, 3), round(g(r, 8, 2.0), 1), round(g(r, 28, 2.5), 0),
                     round(g(r, 6.5, 1.5), 1), hipot])
    p2 = wr(os.path.join(OUT, "E02_锂电卷绕", "02_本工序-卷绕.csv"),
            ["卷芯编号", "批次号", "卷针号", "卷绕张力", "极片对齐度", "Overhang",
             "CCD对位偏差", "隔膜张力", "卷绕速度", "毛刺高度", "Hi-pot"], rows,
            unit_row=["", "", "", "N", "mm", "mm", "mm", "N", "ppm", "um", ""])
    spec = {"极片对齐度(mm)": {"usl": 0.30},
            "卷绕张力(N)": {"lsl": 18, "usl": 26, "target": 22},
            "Overhang(mm)": {"lsl": 0.8, "usl": 1.6, "target": 1.2},
            "毛刺高度(um)": {"usl": 12}}
    save_case("E02_锂电卷绕", [p1, p2], spec, {
        "title": "方形铝壳厂·卷绕（跨工序，上游涂布抽检）",
        "industry": "battery", "process": "battery_winding",
        "device": "卷绕机 + 上游涂布抽检台账", "format_traps": ["两张表需按批次号合并", "双行表头"],
        "n": n,
        "primary_cause": "卷针 N-03 实测卷绕张力偏高约 8N（设定同为 22N）",
        "mechanism": "N-03 张力 30N → 极片对齐度 0.36mm（超 spec 0.30）→ Hi-pot 不良率 18% vs 其它 2%",
        "expected": ["按卷针号分组，定位 N-03", "Hi-pot 不良与卷针对齐度关联",
                     "横向排查建议覆盖其余卷针/机台", "上游厚边量仅为弱相关，不应判定为主因"],
        "traps": [{"name": "上游涂布厚边量与对齐度有弱正相关",
                   "why_not": "量级远小于卷针张力（0.02*3.2≈0.06mm vs 0.24mm），判成上游来料问题会去改涂布，掩盖真正的设备偏差"}],
        "biz_value": "定位到单根卷针，只需检修 N-03 气缸/张力传感器，不必全线停机"})


# ─────────────────────────────────────────────────────────────
# E03 化成 —— 真因：通道 CH-07 接触不良 + 搁置时间不足（双因子）
# ─────────────────────────────────────────────────────────────
def gen_e03():
    r = R(_seed(2))
    n = 320
    hdr = ["电芯编号", "柜号", "通道号", "化成电流", "化成温度", "夹具压力", "搁置时间",
           "分容容量", "交流内阻", "直流内阻", "自放电K值", "首效", "OCV", "能量", "判定"]
    rows = []
    for i in range(n):
        cab = "C01" if i % 2 == 0 else "C02"
        ch = "CH-%02d" % (i % 16 + 1)
        bad_ch = (cab == "C01" and ch == "CH-07")       # 真因1：该通道接触电阻大
        cur = g(r, 0.05, 0.005, 0.02, 0.08)
        temp = g(r, 45, 1.8)
        fp = g(r, 0.6, 0.06)
        stand = g(r, 30, 9, 12, 52)                     # 真因2：搁置时间普遍不足（要求48h）
        kv = max(0.02, 0.15 + 0.015 * (48 - stand) + r.gauss(0, 0.10))
        # 通道效应必须真的体现在「判定」上，否则这条真值在数据里不成立，
        # 评测考的就不是 Agent 的能力而是我的数据没造对（实测：只写进 DCR 时
        # CH-07 不良率 25%，反而是 CH-15/CH-09 的 30% 更高，真因信号被噪声淹没）
        cap = 2.60 + r.gauss(0, 0.035) - (0.14 if bad_ch else 0.0)
        dcr = 38 + r.gauss(0, 2.2) + (7.0 if bad_ch else 0.0)
        acir = 22 + r.gauss(0, 1.2) + (4.0 if bad_ch else 0.0)
        ice = 88.5 - 1.2 * (kv - 0.5) + r.gauss(0, 0.8)
        ocv = g(r, 3.68, 0.012)
        verdict = "自放电不良" if kv > 0.60 else ("容量不良" if cap < 2.50 else "OK")
        rows.append(["CELL%05d" % (i + 1), cab, ch, round(cur, 3), round(temp, 1), round(fp, 2),
                     round(stand, 1), round(cap, 3), round(acir, 1), round(dcr, 1),
                     round(kv, 3), round(ice, 2), round(ocv, 3), round(cap * 3.6, 2), verdict])
    p = wr(os.path.join(OUT, "E03_锂电化成", "01_化成分容_通道数据.csv"), hdr, rows, bom=True)
    spec = {"自放电K值(mV/d)": {"usl": 0.60},
            "分容容量(Ah)": {"lsl": 2.50},
            "直流内阻(mΩ)": {"usl": 45},
            "搁置时间(h)": {"lsl": 48},
            "首效(%)": {"lsl": 86}}
    save_case("E03_锂电化成", [p], spec, {
        "title": "圆柱 18650 厂·化成/分容（多通道柜，UTF-8 BOM）",
        "industry": "battery", "process": "battery_formation",
        "device": "化成柜 2 台 × 16 通道", "format_traps": ["UTF-8 BOM", "通道号为分组维度而非数值"],
        "n": n,
        "primary_cause": "双因子：① C01柜 CH-07 通道接触电阻偏大；② 常温搁置时间普遍不足（均值30h，要求48h）",
        "mechanism": "CH-07：DCR +7mΩ、容量 -0.06Ah、ACIR +4mΩ；搁置不足：K值随搁置时间线性恶化 → 自放电不良",
        "expected": ["按通道号分组定位 CH-07（DCR/容量显著偏离）",
                     "搁置时间与 K 值强负相关", "自放电不良为主要不良类别",
                     "两条因子都要提，不能只报一条"],
        "traps": [{"name": "柜号 C01/C02 看似分组维度",
                   "why_not": "柜间无系统差异，真因在柜内的单通道；按柜号下结论会漏掉 CH-07"}],
        "biz_value": "CH-07 只需换探针/压紧机构；搁置时间补足可消除大量自放电报废"})


# ─────────────────────────────────────────────────────────────
# E04 光伏硅片 —— 真因：进给速度过快 + 供应商B；陷阱：厚度用 mm
# ─────────────────────────────────────────────────────────────
def gen_e04():
    r = R(_seed(3))
    n = 260
    hdr = ["硅片编号", "供应商", "厚度", "TTV", "翘曲", "弯曲度", "电阻率", "少子寿命",
           "线速度", "进给速度", "金刚线张力", "砂浆流量", "线痕", "崩边", "氧含量", "碳含量", "判定"]
    rows = []
    for i in range(n):
        sup = "B" if r.random() < 0.40 else "A"
        feed = g(r, 1.55, 0.20, 1.05, 2.05)             # 真因1：进给速度
        ttv = 8.0 + 13.0 * (feed - 1.50) + (5.0 if sup == "B" else 0.0) + r.gauss(0, 1.8)
        ttv = max(1.0, ttv)
        thk_mm = g(r, 0.150, 0.004, 0.135, 0.165)       # 陷阱：厚度以 mm 计（需换算 μm）
        warp = max(2.0, 18 + 2.2 * (ttv - 8) + r.gauss(0, 6))
        bow = max(1.0, 9 + 1.1 * (ttv - 8) + r.gauss(0, 3))
        rows.append(["W%05d" % (i + 1), sup, round(thk_mm, 4), round(ttv, 2), round(warp, 1),
                     round(bow, 1), round(g(r, 1.2, 0.12), 3), round(g(r, 85, 12), 1),
                     round(g(r, 22, 1.6), 2), round(feed, 3), round(g(r, 28, 2.2), 1),
                     round(g(r, 120, 12), 0), round(abs(r.gauss(6, 2.2)), 1),
                     round(abs(r.gauss(0.35, 0.18)), 2), round(g(r, 12.5, 1.2), 1),
                     round(g(r, 0.8, 0.2), 2),
                     "不合格" if ttv > 15 else "合格"])
    p = wr(os.path.join(OUT, "E04_光伏硅片", "01_硅片分选日报.csv"), hdr, rows)
    spec = {"TTV(um)": {"usl": 15},
            "厚度(mm)": {"lsl": 0.140, "usl": 0.160, "target": 0.150},
            "翘曲(um)": {"usl": 40},
            "线痕(um)": {"usl": 12}}
    save_case("E04_光伏硅片", [p], spec, {
        "title": "单晶硅片厂·硅片分选（厚度以 mm 计）",
        "industry": "pv", "process": "pv_wafer",
        "device": "硅片分选机", "format_traps": ["厚度单位 mm 而库内为 μm（需换算）", "供应商为文本分组维度"],
        "n": n,
        "primary_cause": "双因子：① 进给速度偏快（均值1.55，>1.6 明显恶化）；② 供应商 B 来料 TTV 系统性 +4μm",
        "mechanism": "TTV = 8 + 6.5×(进给-1.50) + 4×(供应商B) → 进给 1.9 时 TTV≈10.6+… 叠加 B 料后频繁突破 spec 15",
        "expected": ["进给速度与 TTV 强正相关", "供应商分组 B 显著偏高",
                     "厚度经 mm→μm 换算后不应被误判超差", "TTV Cpk 不足"],
        "traps": [{"name": "厚度列以 mm 计，值约 0.15",
                   "why_not": "若不换算直接与 μm 规格比对，0.15 会被判为严重超差（假警报）；厚度本身控制良好不是问题"}],
        "biz_value": "降进给速度 + 切换/整改供应商 B，可直接减少 TTV 报废"})


# ─────────────────────────────────────────────────────────────
# E05 组件 EL —— 真因：层压压力被调高（工艺变更未验证）
# ─────────────────────────────────────────────────────────────
def gen_e05():
    r = R(_seed(4))
    n = 180
    hdr = ["组件编号", "层压温度", "层压压力", "层压时间", "串焊温度", "串焊张力",
           "EL不良率", "隐裂", "断栅", "虚焊", "黑心", "碎片", "脏污", "边缘漏电",
           "平均灰度", "暗电流", "判定"]
    rows = []
    for i in range(n):
        # 真因：第100件后工艺变更把层压压力由 65 提到 82 kPa，未做验证
        press = g(r, 82, 3.0) if i >= 100 else g(r, 65, 3.0)
        crack = max(0, round(1.0 + 0.16 * (press - 65) + r.gauss(0, 0.8)))
        grid = max(0, round(2.0 + abs(r.gauss(0, 1.1))))
        # 干扰：串焊温度与虚焊强相关（正常工艺耦合，不是变更）
        stemp = g(r, 215, 6)
        falsew = max(0, round((0.6 + 0.05 * (stemp - 215)) + abs(r.gauss(0, 0.7))))
        black = max(0, round(abs(r.gauss(0.8, 0.6))))
        frag = max(0, round(abs(r.gauss(0.5, 0.5))))
        dirty = max(0, round(abs(r.gauss(1.2, 0.8))))
        leak = max(0, round(abs(r.gauss(0.6, 0.5))))
        tot = crack + grid + falsew + black + frag + dirty + leak
        rows.append(["M26%05d" % (i + 1), round(g(r, 145, 2.5), 1), round(press, 1),
                     round(g(r, 12, 1.2), 1), round(stemp, 1), round(g(r, 12, 1.5), 1),
                     round(tot / 72.0 * 100, 2), crack, grid, falsew, black, frag, dirty, leak,
                     round(g(r, 118, 8), 1), round(g(r, 0.35, 0.09), 3),
                     "不合格" if tot > 6 else "合格"])
    p = wr(os.path.join(OUT, "E05_组件EL", "01_EL检测_层压后.csv"), hdr, rows)
    spec = {"层压压力(kPa)": {"lsl": 60, "usl": 72, "target": 65},
            "EL不良率(%)": {"usl": 6.0},
            "层压温度(℃)": {"lsl": 138, "usl": 152, "target": 145},
            "串焊温度(℃)": {"lsl": 200, "usl": 230, "target": 215}}
    save_case("E05_组件EL", [p], spec, {
        "title": "组件厂·层压后 EL 检测",
        "industry": "pv", "process": "pv_cell_el",
        "device": "EL 检测仪 + 层压机工艺记录", "format_traps": ["无"],
        "n": n,
        "primary_cause": "第100件后层压压力由 65 kPa 调高至 82 kPa（工艺变更未经验证）",
        "mechanism": "压力↑ → 隐裂由 1 片/件升至约 3.7 片/件 → EL 不良率与不合格判定同步抬升",
        "expected": ["层压压力前后段分组差异显著", "隐裂与层压压力强正相关",
                     "识别隐裂为**增量最大**项（全期绝对量断栅仍居首，但断栅未恶化，"
                     "只报断栅=没抓住变更带来的新问题）", "SPC 在约第100件处出现阶跃"],
        "traps": [{"name": "串焊温度与虚焊强相关",
                   "why_not": "这是既有工艺的正常耦合，虚焊绝对量小且未恶化；判成串焊问题会去动不该动的焊接参数"}],
        "biz_value": "回滚层压压力即可止住隐裂，避免整批组件降级"})


# ─────────────────────────────────────────────────────────────
# E06 组件 IV —— 真因：焊带电阻偏高 → Rs↑ → Pmax↓；陷阱：Isc 单位 mA
# ─────────────────────────────────────────────────────────────
def gen_e06():
    r = R(_seed(5))
    n = 200
    hdr = ["组件编号", "Pmax", "Voc", "Isc", "Vmpp", "Impp", "FF", "Rs", "Rsh",
           "焊带电阻", "组件温度", "辐照度", "透光率", "绝缘电阻", "CTM", "判定"]
    rows = []
    for i in range(n):
        rib = g(r, 2.6, 0.35, 1.9, 3.6)                 # 真因：焊带电阻
        rs = 0.28 + 0.09 * (rib - 2.2) + r.gauss(0, 0.018)
        pmax = 545 - 120 * (rs - 0.30) + r.gauss(0, 4.5)
        voc = g(r, 49.5, 0.25)
        isc_ma = g(r, 13500, 130)                       # 陷阱：短路电流以 mA 计
        vmpp = g(r, 41.5, 0.35)
        impp = pmax / vmpp
        ff = pmax / (voc * isc_ma / 1000.0) * 100
        rsh = g(r, 320, 60)
        rows.append(["M26%05d" % (i + 1), round(pmax, 1), round(voc, 2), round(isc_ma, 0),
                     round(vmpp, 2), round(impp, 2), round(ff, 2), round(rs, 4), round(rsh, 0),
                     round(rib, 3), round(g(r, 25, 1.5), 1), round(g(r, 1000, 8), 0),
                     round(g(r, 91.5, 0.8), 2), round(g(r, 850, 120), 0),
                     round(pmax / 550.0 * 100, 2), "降级" if pmax < 535 else "合格"])
    p = wr(os.path.join(OUT, "E06_组件IV", "01_组件IV终测.csv"), hdr, rows)
    spec = {"Pmax(W)": {"lsl": 535},
            "Rs(Ω)": {"usl": 0.38},
            "焊带电阻(mΩ/m)": {"usl": 2.8},
            "FF(%)": {"lsl": 76},
            "CTM(%)": {"lsl": 97}}
    save_case("E06_组件IV", [p], spec, {
        "title": "组件厂·IV 终测（短路电流以 mA 计）",
        "industry": "pv", "process": "pv_module_iv",
        "device": "HALM 类 IV 测试仪", "format_traps": ["Isc 单位 mA 而库内为 A（需换算）"],
        "n": n,
        "primary_cause": "焊带直流电阻偏高（均值 2.6 mΩ/m，spec 2.8）→ 串联电阻 Rs 上升 → Pmax 衰减",
        "mechanism": "Rs = 0.28 + 0.09×(焊带电阻-2.2)；Pmax = 545 - 120×(Rs-0.30)，Rs 0.32→0.42 时 Pmax 掉约 12W",
        "expected": ["焊带电阻与 Rs 强正相关", "Rs 与 Pmax 强负相关", "Pmax/CTM Cpk 不足",
                     "Isc 换算为 A 后不应异常"],
        "traps": [{"name": "Isc 列单位是 mA（值约 13500）",
                   "why_not": "若不换算，Isc 会被当成异常大值；且 Isc 本身与 Pmax 无因果关系"}],
        "biz_value": "换低阻焊带/优化焊接参数可挽回约 10W 功率损失，直接提升组件档位售价"})


# ─────────────────────────────────────────────────────────────
# E07 小厂脏数据 —— 无 MES、自定义列名、缺规格：考验"降级不崩 + 提示补数据"
# ─────────────────────────────────────────────────────────────
def gen_e07():
    r = R(_seed(6))
    n = 120
    hdr = ["LotID", "Op", "T1", "T2", "Speed", "Load", "Thk", "RH", "NG_CNT", "Remark"]
    rows = []
    for i in range(n):
        t2 = g(r, 92, 2.5) if i < 70 else g(r, 84, 3.5)   # 同样埋了漂移，但列名不可识别
        load = 180 + 6.0 * (92 - t2) + r.gauss(0, 5)
        rows.append(["L%04d" % (i + 1), "A" if i % 2 else "B", round(g(r, 80, 3), 1), round(t2, 1),
                     round(g(r, 35, 3), 1), round(load, 1), round(g(r, 52, 2), 1),
                     round(g(r, 45, 6), 1),
                     int(max(0, 2 + max(0.0, (load - 200) / 12.0) * 5 + r.gauss(0, 1))),
                     "" if i % 5 else r.choice(["/", "N/A", "—", "-", "待复测"])])
    # 人为打洞：空值、脏字符
    rows[10][6] = ""          # Thk 缺失
    rows[25][8] = "N/A"       # NG_CNT 非数值
    rows[40][4] = "-"         # Speed 脏字符
    p = wr(os.path.join(OUT, "E07_小厂脏数据", "01_未知名仪器_自定义列名.csv"), hdr, rows)
    save_case("E07_小厂脏数据", [p], {}, {
        "title": "小厂·未知名仪器自定义列名（无规格书）",
        "industry": None, "process": None,
        "device": "未登记仪器", "format_traps": ["自定义列名无法匹配", "空值/脏字符", "无企业规格书"],
        "n": n,
        "primary_cause": "T2 在第70批后由 92 降至 84，Load 随之上升（但列名不可识别，Agent 应当承认认不出）",
        "mechanism": "同上，但列名 T1/T2/Load 不在知识库别名内",
        "expected": ["不崩溃、不报 traceback", "明确标注未识别列",
                     "提示需要补充哪些数据（列名含义/规格上下限）", "不臆造根因"],
        "traps": [{"name": "全部列名均不可识别",
                   "why_not": "此时最危险的行为是硬凑一个根因；正确做法是降级并索要数据"}],
        "biz_value": "考验 Agent 在信息不足时是否诚实——这决定了现场工程师敢不敢信它"})


def main(argv=None):
    """
    生成企业端仿真数据。

    --seed  换一个基准种子就能造出一批不同的随机变体。这是「不依赖真实数据也能
            自我完善」的基础：真值由生成器控制，换种子即可产出大量带真值的考题。
    --out   变体输出到独立目录，避免覆盖 examples/enterprises 的基准用例。
    """
    import argparse as _ap
    ap = _ap.ArgumentParser(description="生成新能源企业端仿真检测数据（带已知真值）")
    ap.add_argument("--seed", type=int, default=None, help="基准随机种子，默认 20260901")
    ap.add_argument("--out", default=None, help="输出目录，默认 examples/enterprises")
    a = ap.parse_args(argv)

    global SEED_BASE, OUT
    if a.seed is not None:
        SEED_BASE = a.seed
    if a.out:
        OUT = a.out

    print("生成企业端仿真数据 -> %s（基准种子 %d）" % (OUT, SEED_BASE))
    os.makedirs(OUT, exist_ok=True)
    for fn in (gen_e01, gen_e02, gen_e03, gen_e04, gen_e05, gen_e06, gen_e07):
        fn()
    print("完成。每个目录含：数据 CSV + spec_企业规格书.json + truth.json（真相卡）")
    return OUT


if __name__ == "__main__":
    main()
