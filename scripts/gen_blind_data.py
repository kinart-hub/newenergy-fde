# -*- coding: utf-8 -*-
"""盲测数据生成器（v1.10）：6 套角色扮演企业数据，与 E01–E07 不重叠，每套植入已知真因与陷阱。
用法：python scripts/gen_blind_data.py  -> examples/blind/"""
import csv, json, random, os, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "examples", "blind")
os.makedirs(OUT, exist_ok=True)
R = random.Random(20261010)
def g(m, s): return R.gauss(m, s)
def w(name, header, rows, enc="utf-8", delim=","):
    with open(os.path.join(OUT, name), "w", encoding=enc, newline="") as f:
        cw = csv.writer(f, delimiter=delim)
        cw.writerow(header)
        cw.writerows(rows)
def truth(name, d):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

# ---------- S1 软包锂电涂布：β射线面密度仪，英文列名+gsm单位，单行表头，'—'缺失，斜杠日期
N1 = 180
rows = []; t0 = dt.datetime(2026, 9, 1, 8, 0)
for i in range(N1):
    ts = t0 + dt.timedelta(minutes=30 * i)
    die_clog = i >= 95           # 模头2号间歇堵塞 -> 边缘厚边上升
    edge = g(6.0, 0.8) + (max(0, i - 95) * 0.06 if die_clog else 0)
    cw = g(92.0, 0.9)            # gsm (单面)
    visc = g(4200, 150)
    solid = 0.38 + 0.00004 * (visc - 4200) + g(0, 0.004)  # 固含与粘度强相关（陷阱）
    rows.append([
        ts.strftime("%Y/%m/%d %H:%M"), "Z%03d" % (i // 6 + 1), "L%02d" % (i % 6 + 1),
        "%.2f" % cw, "%.1f" % edge, "%.0f" % visc, "%.3f" % solid,
        "%.1f" % g(38, 1.2), "%.1f" % g(85, 1.0) if i % 37 else "—",
        "%d" % R.randint(0, 3), "%d" % R.randint(0, 2)])
w("S1_软包涂布_Beta面密度仪.csv",
  ["Date Time ", "Lot", "Slot", "CoatWt(gsm) ", "EdgeThk(um)", "Visc(mPa.s)", "Solid(%)", "Speed(m/min)", "Dryer2(C)", "Mark", "Scratch"],
  rows)
truth("S1_truth.json", {
  "title": "软包极片涂布（β射线面密度仪，英文列名，gsm 单位，斜杠日期，'—' 缺失）",
  "primary_cause": "模头2号自第95条记录起间歇堵塞，边缘厚边 EdgeThk 线性上升（6→约11 um）",
  "expected": ["识别为 EdgeThk/厚边 上升趋势（SPC 判异）", "面密度 CoatWt 本身受控，不应判为主因",
               "'—' 缺失值处理为缺失而非 0", "gsm 与 g/m2 视为同一单位不误报"],
  "traps": ["固含 Solid 与 粘度 Visc 强相关 r≈0.9，看似关键但属同一浆料性质，非本工序可调主因",
            "Dryer2 缺失若当 0 会产生假异常"]})

# ---------- S2 卷绕：GBK + 分号 + 全角数字 + 隔膜供应商为带入源（上游，无单独文件）
N2 = 200
rows = []
for i in range(N2):
    sup = "S1" if i < 120 else "S2"
    sup_thk = g(16.0, 0.3) if sup == "S1" else g(16.9, 0.3)  # 隔膜 S2 偏厚
    tension = g(18.0, 0.8)
    dia = 11.5 + 0.9 * (sup_thk - 16.0) + g(0, 0.05)            # 卷径随隔膜厚度
    thick = 6.2 + 0.55 * (sup_thk - 16.0) * 1.0 + g(0, 0.06)     # 电芯厚度(mm)
    rows.append([
        "W%05d" % (i + 1), "2026-09-%02d" % (1 + i // 25), "%02d:%02d" % (8 + i % 10, (i * 7) % 60),
        sup + "-" + "%03d" % (i // 3), "%.2f" % sup_thk, "%.1f" % tension, "%.2f" % dia,
        ("%.3f" % thick).replace(".", "．") if i % 50 == 0 else "%.3f" % thick,   # 个别全角小数点
        "%.2f" % g(0.15, 0.02)])
w("S2_卷绕_GBK分号.csv",
  ["卷号", "日期", "时间", "隔膜批号", "隔膜厚度(um)", "卷绕张力(N)", "卷径(mm)", "电芯厚度(mm)", "极片对齐度(mm)"],
  rows, enc="gbk", delim=";")
truth("S2_truth.json", {
  "title": "卷绕（GBK 编码、分号分隔、少量全角小数点、无上游文件、隔膜批号带入）",
  "primary_cause": "隔膜供应商 S2 来料厚度 +0.9um，导致卷径与电芯厚度系统性偏高；卷绕本身参数正常",
  "expected": ["GBK 与分号正确解析", "全角小数点 '．' 被修正为 '.' 而非整行丢弃",
               "分组集中度指向隔膜批号/供应商（带入源）", "不应把卷绕张力当主因"],
  "traps": ["卷绕张力与 卷径 有相关，但张力本身无漂移"]})

# ---------- S3 方形化成：长表（一行一个工步采样点）+ 工步列 + 柜B CV截止电流设定偏高
cells = 40
rows = []
for c in range(cells):
    cab = "A" if c < 20 else "B"
    cv_cut = 0.05 if cab == "B" else 0.02      # C 截止电流（柜B误设为0.05C）
    cap = 50.0 + g(0, 0.25) - (3.5 * (cv_cut - 0.02) / 0.03 if cab == "B" else 0)  # 截止过早 -> 容量偏低
    for step, dur in [("恒流充电", 60), ("恒压充电", 40), ("静置", 30), ("恒流放电", 50)]:
        for k in range(4):
            t = k * 15
            if step == "恒流充电": v = 3.2 + 0.2 * k / 3; cur = 0.5
            elif step == "恒压充电": v = 3.65 + 0.001 * k; cur = max(cv_cut, 0.5 * (1 - k / 4)) if k < 3 else cv_cut
            elif step == "静置": v = 3.9 - 0.002 * k; cur = 0
            else: v = 3.4 - 0.05 * k; cur = 0.5
            rows.append(["CELL-%03d" % (c + 1), cab, step, "%d" % (t + 1), "%.3f" % v, "%.3f" % cur,
                         "%.2f" % (cap if step == "恒流放电" and k == 3 else 0.0)])
w("S3_方形化成_长表工步.csv",
  ["电芯编号", "柜号", "工步", "采样序号", "电压(V)", "电流(C)", "分容容量(Ah)"], rows)
truth("S3_truth.json", {
  "title": "方形电芯化成（长表：每行一个工步采样点，工步列区分阶段）",
  "primary_cause": "柜B 恒压段截止电流误设为 0.05C（标准 0.02C），截止过早，分容容量低约 3.5Ah（约 7%）",
  "expected": ["识别为长表（一行一个采样点）而非'每行一个电芯'", "按电芯+工步聚合后再判定，而不是把工步行当样本",
               "柜 B 分组差异显著", "分容容量 Cpk 与 柜B 对应"],
  "traps": ["静置段电压与容量弱相关，属正常自放电物理过程"]})

# ---------- S4 硅片：硅锭头/中/尾位置，少子寿命随位置变化（尾部偏低为真因）；同名异物：厚度 mm vs 厚度 μm
rows = []
for ing in range(12):
    for pos_i, pos in enumerate(["头部", "中部", "尾部"]):
        for k in range(10):
            life = g(12.0, 1.0) - (4.5 if pos == "尾部" else 0)  # 尾部少子寿命低（真因）
            thk_mm = g(0.170, 0.001)                             # mm 单位（同名：厚度）
            rows.append(["ING%02d" % (ing + 1), pos, "%.3f" % thk_mm, "%.2f" % life,
                         "%.1f" % g(8, 1.2), "%.1f" % g(1.5, 0.3)])
w("S4_硅片_少子寿命_位置.csv",
  ["硅锭号", "位置", "厚度(mm)", "少子寿命(us)", "TTV(um)", "线痕深度(um)"], rows)
truth("S4_truth.json", {
  "title": "单晶硅片（硅锭头/中/尾位置；'厚度'为 mm，与 TTV(um) 同文件）",
  "primary_cause": "硅锭尾部少子寿命系统性低约 4.5 us（位置效应，非切割参数）",
  "expected": ["厚度 mm 与 μm 标准单位正确换算，不误报超差", "按位置（头/中/尾）分组发现尾部偏低",
               "判定为来料/晶体位置问题，而非切割参数"],
  "traps": ["厚度(mm)若不换算会被当作 0.17 μm 判超差（假警报）", "TTV 与线痕无关，不应被当主因"]})

# ---------- S5 组件EL：每个缺陷带 (片序, 电池列, 电池行, x_mm, y_mm)；隐裂集中在第7列（串焊机 7 号焊头）
rows = []
mods = 150
for m in range(mods):
    n_cr = 0
    for col in range(1, 13):
        for row in range(1, 7):
            base_p = 0.02
            p = base_p + (0.12 if col == 7 and m >= 60 else 0)  # 第60片后7号焊头压力异常
            if R.random() < p:
                rows.append(["M%04d" % (m + 1), "隐裂", col, row, "%.0f" % (col * 150 + R.uniform(-10, 10)), "%.0f" % (row * 150 + R.uniform(-10, 10))])
            if R.random() < 0.01:
                rows.append(["M%04d" % (m + 1), "黑斑", col, row, "%.0f" % (col * 150), "%.0f" % (row * 150)])
w("S5_EL缺陷_坐标.csv", ["组件号", "缺陷类型", "电池列", "电池行", "x(mm)", "y(mm)"], rows)
truth("S5_truth.json", {
  "title": "组件 EL 缺陷（每条缺陷带电池列/行与坐标，隐裂呈列向聚集）",
  "primary_cause": "串焊 7 号焊头自第 60 片后压力异常，第 7 列电池隐裂率显著高于其它列",
  "expected": ["做缺陷位置聚集性分析（按电池列）", "定位到第 7 列/7 号焊头，而非泛泛归为层压",
               "黑斑为随机分布，不应与隐裂合并"],
  "traps": ["总隐裂率整体上升可能被误判为全局工艺问题"]})

# ---------- S6 组件IV：模拟器辐照度（参考电池）自第150条后衰减 -4%，IV 数值被整体拉低；未做温度/辐照修正
rows = []
for i in range(260):
    irr = 1000 * (0.96 if i >= 150 else 1.0) + g(0, 3)       # W/m2，参考电池实测
    tmod = g(25.0, 0.8)                                       # 组件温度 ℃
    pmax_ref = 545 * (irr / 1000) * (1 - 0.0035 * (tmod - 25)) + g(0, 1.5)  # 理想修正后应稳定
    pmax_meas = pmax_ref * (irr / 1000) ** -1 * (1000 / 1000) * 0 + pmax_ref   # 已含辐照影响
    rows.append(["P%04d" % (i + 1), "%.1f" % tmod, "%.1f" % irr, "%.2f" % pmax_meas,
                 "%.3f" % g(0.6, 0.02), "%.1f" % g(47, 0.4)])
w("S6_组件IV_无修正.csv", ["组件号", "组件温度(C)", "辐照度(W/m2)", "Pmax(W)", "Isc(A)", "Voc(V)"], rows)
truth("S6_truth.json", {
  "title": "组件 IV 终测（未做温度/辐照修正；模拟器辐照自第150条后衰减约4%）",
  "primary_cause": "测试台辐照度漂移（非组件质量问题）：Pmax 在第150条后整体下移约 4%",
  "expected": ["识别出 Pmax 与 辐照度/组件温度 的关系", "提示未做 STC 温度/辐照修正，不得直接做趋势分析",
               "归因为测试设备/标准化问题，而非组件工艺"],
  "traps": ["Pmax 下移若直接归因于焊带/层压会误导工艺调整"]})
print("ok", sorted(os.listdir(OUT)))
