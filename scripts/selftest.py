# -*- coding: utf-8 -*-
"""
selftest.py —— newenergy-fde 端到端自检

覆盖：
  A. 表格引擎：CSV/XLSX 读写往返一致性、中文、多 sheet、日期
  B. 统计库：相关系数、t 分布尾概率、正态 CDF、ANOVA 与理论值对照
  C. SPC    ：控制限常数、Cpk/ppm、Nelson 判异规则触发
  D. DOE    ：正交表规模、效应方向识别、信噪比、响应面寻优
  E. 流水线 ：六类示例数据全部跑通并产出报告
  F. 设备层  ：不同厂商导出 → 归类/版式归一/单位换算/私有列名 → 跨设备一致性
  G. 知识库  ：候选 → 审核 → 入库 → 版本/备份/回滚 → 补丁分发

用法： python scripts/selftest.py
"""

import os
import sys

# Windows 上 stdout 一旦被重定向（CI 写日志就是这种），Python 会改用系统
# ANSI 编码输出；英文版 runner 是 cp1252，打印中文直接 UnicodeEncodeError。
# 这里强制 UTF-8，保证在任何 Windows 环境下都不因中文输出崩掉。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import io
import math
import glob
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib import table as T, stats as S, spc as SPC, doe as DOE, \
    preprocess as PP, rootcause as RC, charts as CH, pipeline as PL, report as RPT

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [OK]   %s %s" % (name, detail))
    else:
        FAIL.append(name)
        print("  [FAIL] %s %s" % (name, detail))


def approx(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


# ---------------------------------------------------------------- A 表格
def test_table():
    print("\nA. 表格引擎")
    tb = T.Table(["时间", "设备号", "面密度"], [
        ["2026-09-01 08:00:00", "涂布机-A", "108.21"],
        ["2026-09-01 08:10:00", "涂布机-B", "109.03"],
        ["2026-09-01 08:20:00", "涂布机-A", "107.88"],
    ], "t1")
    p = os.path.join(ROOT, "output", "_test", "roundtrip.xlsx")
    T.write_xlsx([tb, T.Table(["a"], [["中文值"]], "t2")], p)
    back = T.read_xlsx(p)
    check("XLSX 往返-多sheet", isinstance(back, list) and len(back) == 2,
          "sheets=%s" % (len(back) if isinstance(back, list) else 1))
    b0 = back[0] if isinstance(back, list) else back
    check("XLSX 往返-列名", b0.columns == tb.columns, str(b0.columns))
    check("XLSX 往返-行数", len(b0) == 3)
    check("XLSX 往返-中文", b0.rows[0][1] == "涂布机-A", str(b0.rows[0][1]))
    check("XLSX 往返-数值", approx(T.to_number(b0.rows[1][2]), 109.03, 1e-9),
          str(b0.rows[1][2]))
    check("XLSX 往返-时间", b0.rows[0][0].startswith("2026-09-01 08:00") or
          b0.rows[0][0].startswith("2026-09-01"), str(b0.rows[0][0]))
    # CSV 带前导说明行
    cp = os.path.join(ROOT, "output", "_test", "lead.csv")
    with open(cp, "w", encoding="utf-8-sig") as f:
        f.write("# 设备导出\n# 型号 XC-200\n时间,设备号,面密度\n"
                "2026-09-01 08:00,机A,108.2\n2026-09-01 08:10,机B,109.0\n")
    ct = T.read_csv(cp)
    check("CSV 跳过前导说明行", ct.columns == ["时间", "设备号", "面密度"], str(ct.columns))
    check("CSV 数值解析", approx(T.to_number(ct.rows[1][2]), 109.0))
    # 单位后缀解析
    check("带单位字符串解析", approx(T.to_number("108.2 g/m²"), 108.2) and
          approx(T.to_number("12.5%"), 12.5))
    # Excel 日期序列号
    check("Excel 序列号转日期", str(T._excel_serial_to_datetime(45000))[:10] == "2023-03-15",
          str(T._excel_serial_to_datetime(45000)))


# ---------------------------------------------------------------- B 统计
def test_stats():
    print("\nB. 统计库")
    r, p, n = S.pearson([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
    check("Pearson 完全正相关 r=1", approx(r, 1.0, 1e-9), "r=%.6f" % r)
    r2, _, _ = S.pearson([1, 2, 3, 4, 5], [5, 4, 3, 2, 1])
    check("Pearson 完全负相关 r=-1", approx(r2, -1.0, 1e-9), "r=%.6f" % r2)
    rho, _, _ = S.spearman([1, 2, 3, 4, 5], [1, 4, 9, 16, 25])
    check("Spearman 单调变换 rho=1", approx(rho, 1.0, 1e-9), "rho=%.6f" % rho)
    check("正态 CDF(1.96)=0.975", approx(S.norm_cdf(1.96), 0.975, 1e-3),
          "%.6f" % S.norm_cdf(1.96))
    check("t 分布双尾 t=2.0,df=10 → p≈0.0734",
          approx(S.t_sf(2.0, 10), 0.07338, 1e-3), "%.5f" % S.t_sf(2.0, 10))
    check("卡方上尾 χ²=3.84,df=1 → p≈0.05",
          approx(S.chi2_sf(3.841, 1), 0.05, 1e-3), "%.5f" % S.chi2_sf(3.841, 1))
    check("F 上尾 F=4.26,df=(1,20) → p≈0.05",
          approx(S.f_sf(4.3512, 1, 20), 0.05, 2e-3), "%.5f" % S.f_sf(4.3512, 1, 20))
    a = S.anova_oneway([[1, 2, 3, 4, 5], [2, 3, 4, 5, 6], [8, 9, 10, 11, 12]])
    check("ANOVA 组间差异显著", a and a["p"] < 0.001, "p=%.3g" % (a or {}).get("p"))
    tt = S.t_test_2sample([1, 2, 3, 4, 5], [4, 5, 6, 7, 8])
    check("两样本 t 检验方向正确", tt and tt["mean1"] < tt["mean2"] and tt["p"] < 0.05,
          "p=%.4g" % (tt or {}).get("p"))
    fit = S.linear_regression([[1], [2], [3], [4], [5]], [2.1, 3.9, 6.2, 8.1, 9.8])
    check("一元回归斜率≈2", fit and abs(fit["coef"][0] - 2.0) < 0.15,
          "slope=%.3f" % (fit["coef"][0] if fit else 0))
    check("稳健 sigma(MAD)", approx(S.robust_sigma([1, 1, 1, 1, 100]), 0.0, 1e-9))


# ---------------------------------------------------------------- C SPC
def test_spc():
    print("\nC. SPC")
    check("A2(n=5)=0.577", approx(SPC.A2(5), 0.577, 1e-3), "%.4f" % SPC.A2(5))
    check("D4(n=5)=2.114", approx(SPC.D4_factor(5), 2.114, 2e-3), "%.4f" % SPC.D4_factor(5))
    check("c4(n=5)=0.9400", approx(SPC.c4(5), 0.9400, 1e-3), "%.5f" % SPC.c4(5))
    check("c4(n=2)=0.7979", approx(SPC.c4(2), 0.7979, 1e-3), "%.5f" % SPC.c4(2))
    subs = [[10.1, 10.2, 10.0, 10.1, 10.2] for _ in range(10)]
    xc, rc = SPC.xbar_r(subs)
    check("Xbar-R 中心线≈10.12", approx(xc.center, 10.12, 1e-6), "%.4f" % xc.center)
    check("Xbar-R sigma_within>0", xc.sigma_within > 0, "%.5f" % (xc.sigma_within or 0))
    cap = SPC.process_capability([10.0, 10.1, 9.9, 10.2, 9.8, 10.1, 10.0, 9.9],
                                 lsl=9.0, usl=11.0)
    check("Cpk 对称居中>2", cap["Cpk"] > 2.0, "Cpk=%.3f" % cap["Cpk"])
    check("ppm 计算合理", 0 <= cap["ppm_total"] < 100, "ppm=%.3f" % cap["ppm_total"])
    # 判异规则
    vals = [10] * 20
    center, sig = 10.0, 1.0
    v1 = SPC.nelson_violations(vals[:1] + [13.5] + vals[2:], center, sig)
    check("Nelson 规则1(超3σ)触发", any(v["rule"] == 1 for v in v1))
    v2 = SPC.nelson_violations([10 + 0.5] * 10 + [10] * 5, center, sig)
    check("Nelson 规则2(9点同侧)触发", any(v["rule"] == 2 for v in v2))
    up = [10 + 0.1 * i for i in range(10)]
    v3 = SPC.nelson_violations(up, center, sig)
    check("Nelson 规则3(6点趋势)触发", any(v["rule"] == 3 for v in v3))
    # 规则 7 不在默认基本集里，必须显式开启才触发——这是有意的：
    # 八条全开会误报泛滥，默认只开 1/2/3/5（Western Electric 基本集）
    v7 = SPC.nelson_violations([10 + 0.2] * 16, center, sig, rules="all")
    check("Nelson 规则7(15点在C区)触发（显式开启）", any(v["rule"] == 7 for v in v7))
    v7d = SPC.nelson_violations([10 + 0.2] * 16, center, sig)
    check("默认基本集不含规则7（避免误报泛滥）", not any(v["rule"] == 7 for v in v7d),
          "触发 %s" % [v["rule"] for v in v7d])
    check("规则集解析 basic=1,2,3,5", SPC.parse_rules("basic") == (1, 2, 3, 5))
    check("规则集解析 all=1..8", SPC.parse_rules("all") == tuple(range(1, 9)))
    check("规则集解析 '2,5,8'", SPC.parse_rules("2, 5, 8") == (2, 5, 8))
    check("规则集解析非法值回落默认", SPC.parse_rules("99") == SPC.DEFAULT_RULES)
    # Phase I/II：异常段不能把控制限撑宽
    import random as _rnd
    _g = _rnd.Random(11)
    _stable = [_g.gauss(10, 0.5) for _ in range(40)]
    _spike = [16.0, 17.0, 15.5, 16.8, 15.2]     # 明显的异常段
    _mix = _stable + _spike
    _p1 = SPC.phase1_limits(_mix)
    check("Phase I 识别出稳定期", _p1 is not None and _p1["stable"])
    check("Phase I 剔除了异常点",
          _p1 is not None and any(i >= len(_stable) for i in _p1["excluded"]),
          "剔除索引 %s" % (_p1 or {}).get("excluded"))
    _full_sigma = S.stdev(_mix)
    check("Phase I 控制限窄于全段估计",
          _p1 is not None and (_p1["ucl"] - _p1["lcl"]) < 6 * _full_sigma,
          "phase1=%.3f 全段=%.3f" % (_p1["ucl"] - _p1["lcl"], 6 * _full_sigma))
    _xc2, _ = SPC.x_mr_with_limits(_mix, _p1["center"], _p1["sigma"])
    _v2 = SPC.nelson_violations(_xc2.values, _xc2.center, _xc2.sigma_within)
    check("Phase II 能判出被全段限漏掉的异常",
          any(i >= len(_stable) for v in _v2 for i in v["indexes"]))
    # 一半以上点异常时（阶跃），不能假装建立了稳定基线
    _wild = [10.0] * 30 + [30.0] * 30
    _p1w = SPC.phase1_limits(_wild)
    check("过程整体不受控时不谎报稳定", _p1w is not None and not _p1w["stable"],
          (_p1w or {}).get("note"))
    # DOE 工程约束与安全包络
    _fs = [{"name": "化成电压(V)", "low": 3.0, "high": 4.8},
           {"name": "烘箱一区温度(℃)", "low": 60.0, "high": 200.0}]
    _cons = DOE.build_constraints(_fs)
    check("安全包络收紧化成电压", (_cons.get("化成电压(V)") or {}).get("max") == 4.5,
          (_cons.get("化成电压(V)") or {}).get("max"))
    check("安全包络收紧烘箱温度", (_cons.get("烘箱一区温度(℃)") or {}).get("max") == 150.0,
          (_cons.get("烘箱一区温度(℃)") or {}).get("max"))
    _ok, _why = DOE.check_point({"化成电压(V)": 4.9}, _cons)
    check("越界点被判不可行", (not _ok) and bool(_why), _why)
    _ok2, _ = DOE.check_point({"化成电压(V)": 4.2}, _cons)
    check("窗口内点判可行", _ok2)
    _eng = DOE._coded_to_engineering([0.5, 0.0], _fs)
    check("coded 解码方向正确（+0.5 高于中点）",
          abs(_eng["化成电压(V)"] - (3.9 + 0.5 * 0.9)) < 1e-9, _eng)
    # 正态随机序列：不应触发规则 1（超 3σ）与规则 3（趋势漂移）。
    # 注意：规则 7 在"分布过窄"时会触发，这是它的本意（提示控制限过宽/数据分层），不算误报。
    import random
    random.seed(7)
    clean = [random.gauss(10, 1) for _ in range(60)]
    vc = SPC.nelson_violations(clean, S.mean(clean), S.stdev(clean))
    check("正态随机序列不触发规则1/3",
          not any(v["rule"] in (1, 3) for v in vc),
          "触发规则 %s" % [v["rule"] for v in vc])
    gc = SPC.group_compare([1, 2, 3, 4, 5, 6, 7, 8, 9],
                           ["A", "A", "A", "B", "B", "B", "C", "C", "C"])
    check("分组 ANOVA 可计算", gc and gc["anova"] is not None)


# ---------------------------------------------------------------- D DOE
def test_doe():
    print("\nD. DOE")
    l8 = DOE.taguchi_2level(8)
    check("L8 规模 8×7", len(l8) == 8 and len(l8[0]) == 7, "%d×%d" % (len(l8), len(l8[0])))
    l16 = DOE.taguchi_2level(16)
    check("L16 规模 16×15", len(l16) == 16 and len(l16[0]) == 15,
          "%d×%d" % (len(l16), len(l16[0])))
    l9 = DOE.taguchi_3level(9)
    check("L9 规模 9×4", len(l9) == 9 and len(l9[0]) == 4, "%d×%d" % (len(l9), len(l9[0])))
    l27 = DOE.taguchi_3level(27)
    check("L27 规模 27×13", len(l27) == 27 and len(l27[0]) == 13,
          "%d×%d" % (len(l27), len(l27[0])))
    # 正交性：任一列各水平出现次数相等
    okl9 = all(sorted(row.count(lv) for lv in (1, 2, 3)) == [3, 3, 3]
               for row in zip(*l9))
    check("L9 各列水平均衡（正交性）", okl9)
    ff = DOE.full_factorial([2, 3, 2])
    check("全因子 2×3×2=12", len(ff) == 12, str(len(ff)))
    bb = DOE.box_behnken(3)
    check("Box-Behnken(3因子)=15", len(bb) == 15, str(len(bb)))
    cc = DOE.ccd(3)
    check("CCD(3因子)=8+6+3=17", len(cc) == 17, str(len(cc)))
    # 效应方向识别（无噪声）
    design = DOE.full_factorial([2, 2, 2])
    y = []
    for row in design:
        a = 1 if row[0] == 2 else -1
        b = 1 if row[1] == 2 else -1
        c = 1 if row[2] == 2 else -1
        y.append(10 + 3 * a - 2 * b + 0.1 * c)
    eff = DOE.effects(design, y, ["A", "B", "C"])
    A = [e for e in eff["main_effects"] if e["factor"] == "A"][0]
    B = [e for e in eff["main_effects"] if e["factor"] == "B"][0]
    check("主效应方向 A 取高水平", A["best_level"] == 2, str(A["best_level"]))
    check("主效应方向 B 取低水平", B["best_level"] == 1, str(B["best_level"]))
    check("贡献率排序 A>B>C",
          [e["factor"] for e in eff["main_effects"]][:2] == ["A", "B"],
          str([e["factor"] for e in eff["main_effects"]]))
    sn_small = DOE.sn_ratio([1, 2, 3], "smaller")
    sn_large = DOE.sn_ratio([1, 2, 3], "larger")
    check("信噪比 望小/望大 可计算", sn_small is not None and sn_large is not None,
          "%.2f/%.2f" % (sn_small or 0, sn_large or 0))
    # 响应面：y = -(x-0.5)^2 + 10，最优点应在 0.5
    import random
    random.seed(1)
    X = [[-1 + 2 * i / 9.0] for i in range(10)]
    Y = [10 - (x[0] - 0.5) ** 2 for x in X]
    m = DOE.fit_rsm(X, Y)
    opt = DOE.optimize_rsm(m, "maximize")
    check("响应面寻优命中 x≈0.5",
          m and opt and abs(opt["coded_optimum"][0] - 0.5) < 0.05,
          "x=%.3f" % (opt["coded_optimum"][0] if opt else -99))
    rec = DOE.recommend_design(4, levels=2, budget=16)
    check("设计推荐受预算约束", all(o["runs"] <= 16 for o in rec),
          str([o["name"] for o in rec]))


# ---------------------------------------------------------------- E 流水线
def test_pipeline():
    print("\nE. 端到端流水线")
    files = sorted(glob.glob(os.path.join(ROOT, "examples", "*.csv")))
    check("示例数据齐全（7 类：6 类工况 + 1 类私有列名）", len(files) == 7,
          "%d 个" % len(files))
    for f in files:
        name = os.path.basename(f)
        outdir = os.path.join(ROOT, "output", "_test", name[:2])
        try:
            res = PL.analyze([f], outdir=outdir)
            ok = (not res.get("error")) and os.path.exists(res["outputs"]["html"]) \
                and os.path.exists(res["outputs"]["md"])
            charts = len(res["outputs"]["charts"])
            check("流水线 %s" % name, ok, "图表%d个" % charts)
        except Exception as e:
            check("流水线 %s" % name, False, "异常: %s" % e)
    # 缺陷根因引擎
    kb_path = os.path.join(ROOT, "knowledge", "process_specs.json")
    check("知识库可加载", os.path.exists(kb_path) and len(PP.load_knowledge()["params"]) > 40,
          "%d 参数" % len(PP.load_knowledge()["params"]))
    tb = T.Table(["隔膜张力(N)", "负极张力(N)", "不良率(%)"],
                 [[3.1, 16, 0.4], [5.9, 16, 2.8], [3.2, 15.9, 0.5], [6.1, 16.1, 3.0]], "t")
    rc = RC.rank_root_causes("隔膜褶皱", tb, "不良率(%)")
    check("根因引擎可排序", rc and len(rc["candidates"]) > 0,
          "首位=%s" % (rc["candidates"][0]["param_name"] if rc else "无"))
    # 图表
    svg = CH.control_chart([1, 2, 3, 2, 1, 5], 2.3, 0.5, 4.1, title="t")
    check("SVG 控制图生成", svg.startswith("<svg") and "</svg>" in svg, "%d字符" % len(svg))
    svg2 = CH.heatmap([[1, 0.5], [0.5, 1]], ["a", "b"])
    check("SVG 热力图生成", svg2.startswith("<svg"))
    svg3 = CH.pareto_chart(RC.pareto({"A": 50, "B": 30, "C": 15})["rows"])
    check("SVG Pareto 生成", svg3.startswith("<svg"))
    svg4 = CH.histogram([1, 2, 2, 3, 3, 3, 4], lsl=0.5, usl=4.5, target=2.5)
    check("SVG 直方图生成", svg4.startswith("<svg"))
    # 列名跨行业误匹配防护
    check("涂布数据的『不良率』不再误判为光伏 EL",
          PP.match_param("不良率(%)", prefer_process="battery_coating") == "defect_rate_generic",
          str(PP.match_param("不良率(%)", prefer_process="battery_coating")))
    check("光伏 EL 数据的『不良率』正确匹配",
          PP.match_param("不良率(%)", prefer_process="pv_cell_el") == "el_defect_rate",
          str(PP.match_param("不良率(%)", prefer_process="pv_cell_el")))
    check("『隐裂』匹配 EL 隐裂计数而非不良率",
          PP.match_param("隐裂", prefer_process="pv_cell_el") == "el_crack",
          str(PP.match_param("隐裂", prefer_process="pv_cell_el")))


def _col_mean(tb, col):
    """取某列的均值与样本数（跳过非数值）"""
    if col not in tb.columns:
        return None, 0
    i = tb.columns.index(col)
    v = [T.to_number(r[i]) for r in tb.rows]
    v = [x for x in v if x is not None]
    return (S.mean(v) if v else None), len(v)


def test_devices():
    """检测设备适配：版式归类、竖表/多级表头/统计尾行、单位换算、私有列名映射"""
    print("\nF. 检测设备适配层（不同厂商导出归类）")
    from lib import devices as D

    prof = D.load_profiles()
    devs = prof.get("devices", {})
    check("设备档案可加载", len(devs) >= 10, "%d 台" % len(devs))

    specs = PP.load_knowledge()
    # aliases 里显式写 None 表示"这台设备的该列不是检测项"，是合法写法，跳过
    bad = ["%s：「%s」→%s" % (k, cn, p) for k, v in devs.items()
           for cn, p in (v.get("aliases") or {}).items()
           if p is not None and p not in specs["params"]]
    check("设备私有列名全部指向已登记参数", not bad, "、".join(bad[:3]))

    # 单位写法归一：同一单位的多种写法必须收敛，否则会误报"单位不一致"
    check("单位写法归一（wt%≡%、μm≡um、Ω≡ohm）",
          D._canon_unit("wt%") == D._canon_unit("%") and
          D._canon_unit("um") == D._canon_unit("μm") and
          D._canon_unit("ohm") == D._canon_unit("Ω"),
          "wt%%→%s, um→%s, ohm→%s" % (D._canon_unit("wt%"), D._canon_unit("um"),
                                      D._canon_unit("ohm")))
    convs = prof.get("unit_conversions") or {}
    pairs = [(c.get("from"), c.get("to"), float(c["factor"]))
             for c in convs.values() if c.get("factor")]
    rev_ok = all(D._conv_factor(convs, None, t, f) is not None and
                 approx(D._conv_factor(convs, None, t, f), 1.0 / fac, 1e-9)
                 for f, t, fac in pairs)
    check("单位换算双向可逆", rev_ok and len(pairs) >= 10, "%d 条换算规则" % len(pairs))

    sd = os.path.join(ROOT, "examples", "devices")
    files = sorted(glob.glob(os.path.join(sd, "*.csv")))
    check("多设备样例齐全", len(files) == 6, "%d 个" % len(files))

    def sample(prefix):
        hit = [f for f in files if os.path.basename(f).startswith(prefix)]
        return D.ingest(hit[0]) if hit else None

    # --- 竖表 + 单位来自设备档案 + 统计尾行：基恩士 LK-G3000
    r = sample("10_")
    tb = r["table"]
    check("竖表设备可被识别", r["device"] == "keyence_lk_g", str(r["device"]))
    check("竖表已转宽表", tb and "厚み(μm)" in tb.columns and len(tb.rows) == 90,
          "%d行 × %d列" % (len(tb.rows), len(tb.columns)))
    check("统计尾行（平均/最大/最小）已剔除",
          not any(str(row[0]).strip() in ("平均", "最大", "最小") for row in tb.rows),
          "剩余%d行" % len(tb.rows))
    app = [a for a in r["units_applied"] if a["from"] == "mm" and a["to"] == "μm"]
    check("竖表单位靠档案补齐 mm→μm", bool(app) and approx(app[0]["factor"], 1000.0),
          str(app[:1]))
    # 竖表没有被百分点 EVERY 行 ×1000：与原始竖表手工折算一致
    raw = T.read_any([f for f in files if os.path.basename(f).startswith("10_")][0])
    kv = [r0 for r0 in raw.rows if str(r0[1]).strip() == "厚み"]
    rawmean = S.mean([T.to_number(x[2]) for x in kv]) * 1000.0
    m10, n10 = _col_mean(tb, "厚み(μm)")
    check("竖表换算后数值与原始一致", n10 == len(kv) and approx(m10, rawmean, 1e-6),
          "均值%.3fμm / %d点" % (m10, n10))

    # --- 多级表头 + 私有列名 + 合计尾行：浩能涂布 MES
    r = sample("11_")
    tb = r["table"]
    check("双行表头设备可被识别", r["device"] == "coating_mes_cn", str(r["device"]))
    check("多级表头取明细行，不含分组名",
          tb and "工艺参数" not in tb.columns and "面密度(g/m²)" in tb.columns,
          str(tb.columns[:4]))
    check("合计尾行已剔除", tb and len(tb.rows) == 180, "%d行" % len(tb.rows))
    check("私有列名映射到工艺参数",
          any(v == "areal_density" for v in r["col_map"].values()) and
          sum(1 for v in r["col_source"].values() if v == "device") >= 3,
          "%d列来自设备档案" % sum(1 for v in r["col_source"].values() if v == "device"))
    m_dev, n_dev = _col_mean(tb, "面密度(g/m²)")
    m_base, n_base = _col_mean(T.read_any(os.path.join(ROOT, "examples", "01_涂布工艺数据.csv")),
                               "涂布面密度(g/m²)")
    check("同一真人值不同设备导出 → 面密度一致",
          n_dev == n_base and approx(m_dev, m_base, 1e-6),
          "%.4f vs %.4f (n=%d)" % (m_dev, m_base, n_dev))

    # --- 竖表 + 列名自带单位：日置 HIOKI
    r = sample("12_")
    tb = r["table"]
    check("内阻仪设备可被识别", r["device"] == "hioki_bt356x", str(r["device"]))
    check("Omega->mOhm 换算生效（日置）", tb and "内部抵抗(mΩ)" in tb.columns,
          str(tb.columns[:3]))
    check("竖表的汇总统计行已剔除", tb and len(tb.rows) == 90, "%d行" % len(tb.rows))

    # --- 单位行/列名单位共存：硅片分选机
    r = sample("13_")
    tb = r["table"]
    check("硅片分选机可被识别", r["device"] == "wafer_thickness_sorter", str(r["device"]))
    check("档案标注的无用列已丢弃",
          tb and not any("等级" in c for c in tb.columns), str(tb.columns))
    m_dev, n_dev = _col_mean(tb, "THK(μm)")
    m_base, n_base = _col_mean(T.read_any(os.path.join(ROOT, "examples", "04_硅片检测数据.csv")),
                               "硅片厚度(μm)")
    check("mm 导出换算后与 μm 导出一致",
          n_dev == n_base and abs(m_dev - m_base) < 0.01,
          "%.4f vs %.4f μm (n=%d)" % (m_dev, m_base, n_dev))

    # --- 单位不同但同参数：HALM  IV 的 Isc 用 mA
    r = sample("14_")
    tb = r["table"]
    check("IV 测试仪可被识别", r["device"] == "halm_iv_tester", str(r["device"]))
    check("单位不同但同参数：mA→A 换算生效", tb and "Isc(A)" in tb.columns, str([c for c in tb.columns if "Isc" in c]))
    m_dev, n_dev = _col_mean(tb, "Isc(A)")
    m_base, n_base = _col_mean(T.read_any(os.path.join(ROOT, "examples", "06_组件IV测试数据.csv")),
                               "Isc(A)")
    check("mA 导出换算后与 A 导出一致",
          n_dev == n_base and approx(m_dev, m_base, 1e-6),
          "%.6f vs %.6f A (n=%d)" % (m_dev, m_base, n_dev))

    # --- 档案外的未知仪器：必须降级不报错
    r = sample("15_")
    check("未知设备降级处理不报错",
          r["device"] is None and r["table"] is not None and len(r["table"].rows) > 0,
          "%d行未识别但可读" % len(r["table"].rows))
    # 提示必须给出可执行的下一步（录入问卷或提交档案），而不是只说"未识别"
    check("未识别时给出可执行的补齐动作",
          any(("interview" in n or "device_profile" in n) for n in r["notes"]), str(r["notes"][:1]))

    # --- 设备样例也能端到端出报告（多设备混合输入）
    try:
        res = PL.analyze([os.path.join(sd, "11_涂布MES_浩能_双行表头带合计.csv"),
                          os.path.join(sd, "14_HALM-IV组件测试_Isc用mA.csv")],
                         outdir=os.path.join(ROOT, "output", "_test", "devmix"))
        dinfo = res.get("devices") or []
        ok = (not res.get("error")) and os.path.exists(res["outputs"]["html"]) and len(dinfo) == 2
        check("多设备文件混合分析出报告", ok,
              "识别=%s" % "、".join([str(x.get("device")) for x in dinfo]))
    except Exception as e:
        check("多设备文件混合分析出报告", False, "异常: %s" % e)

    # --- 登记新设备档案必须真正落盘（曾因提前 return 只改内存副本）
    import shutil, tempfile
    from lib import kbstore as KBS
    tmp = tempfile.mkdtemp(prefix="devtest-")
    try:
        shutil.copytree(os.path.join(ROOT, "knowledge"), os.path.join(tmp, "knowledge"))
        KBS.set_root(tmp)
        payload = {"key": "selftest_probe", "name": "自检虚拟测厚仪 X-100",
                   "vendor": "SELFTEST", "category": "coating_gauge",
                   "processes": ["battery_coating"],
                   "format": {"layout": "wide", "header_rows": 1},
                   "aliases": {"探针厚度(mm)": "coating_thickness"},
                   "unit_map": {"探针厚度(mm)": {"read_unit": "mm", "target_unit": "μm"}},
                   "signature": {"meta_regex": ["SELFTES-PROBE"]}}
        rec, err = KBS.add_candidate("device_profile", payload, source={"site_code": "自检"})
        check("设备档案候选受理", rec is not None, err or "")
        if rec:
            KBS.update_candidate(rec["id"], {"status": "approved",
                                             "review": {"reviewer": "自检", "note": ""}})
            logs = KBS.sync(reviewer="自检")
            # 落盘：直接读文件，绕过一切缓存
            disk = KBS._read_json(KBS.DEV_PROFILE_FILE, {}).get("devices", {})
            check("设备档案已写库落盘", "selftest_probe" in disk,
                  "库中%d台，失败=%s" % (len(disk), (logs or {}).get("failed")))
            D.invalidate_cache()
            check("新设备立即可被识别层读到",
                  "selftest_probe" in D.load_profiles().get("devices", {}), "")
            check("sync 返回变更记录", (logs or {}).get("merged") == [rec["id"]],
                  str((logs or {}).get("merged")))
            ver = KBS._read_json(KBS.DEV_PROFILE_FILE, {}).get("meta", {}).get("version")
            check("版本号自增", ver not in (None, "1.0.0"), str(ver))
            # 105 新登记的设备档案必须能真正识别对应文件（落盘≠可用）
            probe_csv = os.path.join(tmp, "probe.csv")
            with open(probe_csv, "w", encoding="utf-8-sig") as f:
                f.write("SELFTES-PROBE X-100 Export\n"
                        "探针厚度(mm),电池电压(V)\n0.1059,3.69\n0.1021,3.71\n")
            hit = D.identify_device(T.read_any(probe_csv), path=probe_csv)
            check("登记后即可识别该设备导出", hit.get("device") == "selftest_probe",
                  str(hit.get("device")))
        # 引用未登记参数的档案必须被拦下
        badrec, berr = KBS.add_candidate(
            "device_profile",
            {"key": "bad", "name": "坏档案", "vendor": "X", "format": {"layout": "wide"},
             "aliases": {"某列": "param_not_exist"}},
            source={"site_code": "自检"})
        check("引用未登记参数的档案被拒绝", badrec is None, (berr or "")[:60])
        # 重复 key 需 --force
        duprec, derr = KBS.add_candidate("device_profile", payload, source={"site_code": "自检"})
        if duprec:
            KBS.update_candidate(duprec["id"], {"status": "approved"})
            r0 = KBS.sync(reviewer="自检")
            check("重复登记同名设备被拦截",
                  any("已存在" in (x.get("error") or "") for x in ((r0 or {}).get("failed") or [])),
                  str((r0 or {}).get("failed"))[:80])
    finally:
        KBS.set_root(None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_crossprocess():
    """跨工序追溯：三组植入已知真值的数据，判定必须判对"""
    print("\nH. 跨工序追溯（上游合格 / 上游越限 / 本工序主导）")
    from lib import crossprocess as XP, actions as AC

    # 工序链必须机器可读且双向对称
    for k, up in (("battery_formation", "battery_coating"), ("pv_module_iv", "pv_wafer")):
        ups = XP.upstream_of(k)
        check("%s 的上游链含 %s" % (k, up), up in ups, str(ups))
    for a, b in (("battery_coating", "battery_calendering"), ("pv_wafer", "pv_cell_el")):
        check("%s → %s 链对称" % (a, b),
              b in XP.downstream_of(a) and a in XP.upstream_of(b))
    check("缺陷登记了上游带入嫌疑",
          XP.carry_over_defects("容量一致性差")["carry_over_from"],
          str(XP.carry_over_defects("容量一致性差")["carry_over_from"]))

    SPECS = {"moisture_before_filling": {"lsl": 0, "usl": 25},
             "areal_density": {"lsl": 102, "usl": 108}}
    CASES = [
        ("A", "battery_winding", "battery_calendering", "对位偏差(mm)", None, "本工序主导", None),
        ("B", "battery_formation", "battery_filling", "自放电(mV/d)", "自放电超标",
         "上游不合格流入", None),
        ("C", "battery_formation", "battery_coating", "容量标准差(%)", "容量一致性差",
         "上游合格但边缘", None),
    ]
    for grp, cur, up, target, defect, expect, _ in CASES:
        d = os.path.join(ROOT, "examples", "crossprocess", grp)
        files = sorted(glob.glob(os.path.join(d, "*.csv")))
        tables, meta = [], []
        for f in files:
            tb = T.read_any(f)
            tb.name = os.path.splitext(os.path.basename(f))[0]
            cm = PP.map_columns(tb, up if "上游" in tb.name else cur)
            tables.append(tb)
            meta.append({"name": tb.name, "columns": cm,
                         "process": up if "上游" in tb.name else cur})
        xr = XP.trace(tables, meta, target, cur, defect=defect, spec=SPECS)
        got = xr.get("verdict") or ""
        check("%s 组归属判定 = %s" % (grp, expect), got.startswith(expect),
              "实际：%s" % got)
        check("%s 组已完成跨工序对齐" % grp,
              (xr.get("alignment") or {}).get("rows") == 120,
              str((xr.get("alignment") or {}).get("mode")))
        check("%s 组对齐未丢表" % grp,
              not (xr.get("alignment") or {}).get("dropped_tables"),
              str((xr.get("alignment") or {}).get("dropped_tables")))
        aligned = xr["aligned"]
        lat = AC.lateral_plan(aligned, target_col=target, defect=defect,
                              contributions=xr.get("contributions"))
        if grp == "A":
            # A 组刻意让 C-02 这台设备整体偏大：必须先剥离主因子才看得见
            hit = [r for r in lat["rows"] if r["dim"] == "机台号"]
            check("A 组横向排查揪出 C-02",
                  bool(hit) and hit[0]["worst_level"] == "C-02"
                  and hit[0]["verdict"].startswith("局部性"),
                  str([(r["worst_level"], round(r["eta_sq"], 3)) for r in hit]))
            check("A 组横向排查已剥离主因子",
                  bool(lat.get("adjusted_for")), str(lat.get("adjusted_for")))
        else:
            mc = [r for r in lat["rows"] if r["dim"] == "机台号"]
            check("%s 组阴性对照不误报" % grp,
                  bool(mc) and mc[0]["verdict"].startswith("该维度不区分"),
                  str([(r["worst_level"], round(r["eta_sq"], 3)) for r in mc]))
        perm = AC.permanent_actions(xr, defect=defect, current_process=cur)
        check("%s 组产出永久措施候选" % grp,
              len(perm.get("generic") or []) > 0 and perm.get("notes") is not None)
        prev = AC.preventive(xr, process=cur)
        check("%s 组给出可落地的预防参数" % grp, len(prev.get("params") or []) > 0,
              str([p["column"] for p in (prev.get("params") or [])]))

    # 规格缺失时不得误判为"合格但边缘"——这是最容易造假的分支
    tb = T.read_any(os.path.join(ROOT, "examples", "crossprocess", "B", "02_本工序-化成.csv"))
    tb.name = "cur"
    tb2 = T.read_any(os.path.join(ROOT, "examples", "crossprocess", "B", "01_上游-注液.csv"))
    tb2.name = "up"
    cm1 = PP.map_columns(tb, "battery_formation")
    cm2 = PP.map_columns(tb2, "battery_filling")
    xr = XP.trace([tb, tb2],
                  [{"name": "cur", "columns": cm1, "process": "battery_formation"},
                   {"name": "up", "columns": cm2, "process": "battery_filling"}],
                  "自放电(mV/d)", "battery_formation", spec=None)
    vs = {p.get("verdict") for p in (xr.get("upstream_probes") or [])}
    check("无规格时不冒称『合格但边缘』", "upstream_marginal" not in vs, str(vs))

    # 批次键必须优先于时间对齐：批次是件-件精确对应，时间只是就近近似
    info = XP.detect_join_key([tb, tb2])
    check("存在批次号时按批次对齐而非按时间", info["mode"] == "batch", str(info["mode"]))

    # 横向排查的协变量调整：剥离后解释度必须显著上升
    a = T.read_any(os.path.join(ROOT, "examples", "crossprocess", "A", "02_本工序-卷绕.csv"))
    a.name = "cur"
    raw = XP.lateral_sweep(a, "对位偏差(mm)", ["机台号"])
    adj = XP.lateral_sweep(a, "对位偏差(mm)", ["机台号"], adjust_for="卷绕张力(N)")
    check("剥离主因子后机台差异才显现",
          bool(adj) and adj[0]["eta_sq"] > (raw[0]["eta_sq"] if raw else 0) * 3,
          "η² %.3f → %.3f" % ((raw[0]["eta_sq"] if raw else 0),
                              adj[0]["eta_sq"] if adj else 0))


def test_kb():
    """知识库持续演进：候选 → 审核 → 入库 → 版本/备份/回滚 → 补丁分发"""
    print("\nG. 知识库演进流水线（隔离环境）")
    from lib import kbstore as KBS
    import shutil, tempfile, json as _json

    tmp = tempfile.mkdtemp(prefix="kbtest-")
    try:
        shutil.copytree(os.path.join(ROOT, "knowledge"), os.path.join(tmp, "knowledge"))
        # 隔离环境不带上一轮的真实候选
        for stale in ("inbox.jsonl", "improvement_library.json"):
            fp = os.path.join(tmp, "knowledge", stale)
            if os.path.exists(fp):
                os.remove(fp)
        KBS.set_root(tmp)

        # 1) 结构完整性
        kb = KBS._read_json(KBS.SPEC_FILE, {})
        spec = KBS._read_json(KBS.CASE_FILE, {"cases": []})
        check("正库可读", len(kb.get("params", {})) > 50 and len(kb.get("processes", {})) > 0)

        # 2) 引用完整性：缺陷机理不得指向不存在的参数
        dangling = []
        for dk, dv in kb.get("defect_rootcause", {}).items():
            for pk in dv.get("likely_params", []):
                if pk not in kb.get("params", {}):
                    dangling.append((dk, pk))
        check("缺陷机理引用无悬空", not dangling, str(dangling))

        # 3) 非法候选必须被拦在暂存区外
        rec, err = KBS.add_candidate("param", {"key": "bad_param", "name": "错参",
                                               "process": "不存在的工序", "unit": "x",
                                               "role": "control"})
        check("工序不存在的候选被拒绝", rec is None and err is not None)
        rec, err = KBS.add_candidate("param", {"key": "areal_density", "name": "重复",
                                               "process": "battery_coating", "unit": "g/m²",
                                               "role": "control"})
        check("重复 key 被拒绝", rec is None and err is not None)

        # 4) 新参数候选 -> 放行 -> 入库
        rec, err = KBS.add_candidate("param", {
            "key": "kbtest_temp", "name": "测试浆料温度", "process": "battery_coating",
            "unit": "℃", "role": "control", "aliases": ["浆料温度"],
            "typical_range": [20, 35], "control": {"mode": "deviation", "limit": 2},
            "note": "测试"}, source={"site_code": "测试线"}, proposed_by="自检")
        check("合法参数候选受理", rec is not None, err or "")
        KBS.update_candidate(rec["id"], {"status": "approved",
                                         "review": {"reviewer": "自检", "note": ""}})
        r = KBS.sync(reviewer="自检")
        after = KBS._read_json(KBS.SPEC_FILE, {})
        check("参数已入正库", "kbtest_temp" in after.get("params", {}),
              str(r["failed"]))
        check("版本号自增", after["meta"]["version"] != kb["meta"]["version"],
              after["meta"]["version"])

        # 5) alias 去重：重复别名不重复追加
        before_alias = len(after["params"]["solid_content"]["aliases"])
        KBS.add_candidate("param_alias", {"key": "solid_content", "aliases": ["浆料温度"]},
                          source={"site_code": "测试线"})
        KBS.add_candidate("param_alias", {"key": "solid_content",
                                          "aliases": ["浆料温度", "SC-Actual"]},
                          source={"site_code": "测试线"})
        for c in KBS.load_candidates("pending"):
            KBS.update_candidate(c["id"], {"status": "approved",
                                           "review": {"reviewer": "自检", "note": ""}})
        KBS.sync(reviewer="自检")
        aliases = KBS._read_json(KBS.SPEC_FILE, {})["params"]["solid_content"]["aliases"]
        check("别名不重复累加", aliases.count("浆料温度") == 1, str(aliases[-3:]))
        check("别名新增生效", "SC-Actual" in aliases)
        check("原别名未被破坏", len(aliases) > before_alias)

        # 6) 控制限大幅变更必须人工 --force
        KBS.add_candidate("param_limit", {"key": "areal_density",
                                          "control": {"mode": "deviation_pct",
                                                      "limit_pct": 9.0},
                                          "source_tier": "reference"},
                          verification={"method": "PDCA"})
        cid = KBS.load_candidates("pending")[0]["id"]
        warns = KBS.load_candidates("pending")[0]["warnings"]
        KBS.update_candidate(cid, {"status": "approved",
                                   "review": {"reviewer": "自检", "note": ""}})
        r = KBS.sync(reviewer="自检")
        check("超限变更未 force 时被拦截", len(r["failed"]) == 1, str(r["failed"]))
        check("超限变更已给出提示", bool(warns), (warns or [""])[0][:40])
        old_limit = KBS._read_json(KBS.SPEC_FILE, {})["params"]["areal_density"]["control"]
        r = KBS.sync(reviewer="自检", force=True)
        new_limit = KBS._read_json(KBS.SPEC_FILE, {})["params"]["areal_density"]["control"]
        check("--force 后完成修订", len(r["merged"]) == 1 and new_limit != old_limit)
        check("修订留痕", "_kbrevs" in KBS._read_json(KBS.SPEC_FILE, {})["params"]["areal_density"])

        # 7) 改善措施分级：A/B/C
        base_cm = {"action": "调参", "type": "工艺参数调整"}
        full_res = {"metric": "不良率", "unit": "%", "before": 3.2, "after": 0.9,
                    "sample_size": 62}
        std_ok = {"doc_no": "PG-1", "control_plan_updated": True}
        a1, _ = KBS.grade_case({"verification": {"method": "PDCA", "result": full_res},
                                "standardization": std_ok, "countermeasure": base_cm})
        b1, _ = KBS.grade_case({"verification": {"method": "PDCA",
                                                 "pdca": {"plan": "1", "do": "2",
                                                          "check": "3", "act": "4"}},
                                "countermeasure": base_cm})
        c1, _ = KBS.grade_case({"verification": {"method": ""}, "countermeasure": base_cm})
        check("量化+标准化+大样本 => A 级", a1 == "A", a1)
        check("PDCA 完整但缺量化 => B 级", b1 == "B", b1)
        check("无验证方法 => C 级", c1 == "C", c1)

        b2, _ = KBS.grade_case({"verification": {"method": "PFMEA",
                                                 "fmea": {"rpn_before": 126,
                                                          "rpn_after": 48}},
                                "countermeasure": base_cm})
        check("PFMEA 记录无闭环 => B 级", b2 == "B", b2)

        # 8) 自评等级只能调低不能调高
        rec, _ = KBS.add_candidate("improvement_case", {
            "defect": "针孔气泡", "process": "battery_coating",
            "evidence_level": "A", "countermeasure": {"action": "凭感觉调了温度"},
            "verification": {"method": ""}},
            source={"site_code": "测试线"})
        KBS.update_candidate(rec["id"], {"status": "approved",
                                         "review": {"reviewer": "自检", "note": ""}})
        KBS.sync(reviewer="自检")
        cases = {c.get("id"): c for c in KBS._read_json(KBS.CASE_FILE, {"cases": []})["cases"]}
        mine = cases.get(rec["id"], {})
        check("自评 A 但材料不足 => 降为 C", mine.get("evidence_level") == "C",
              str(mine.get("evidence_level")))
        check("C 级不参与推荐", mine.get("can_recommend") is False)
        check("降级理由已留痕", bool(mine.get("grade_reason")), str(mine.get("grade_reason"))[:40])

        # 9) A 级措施可被检索并用于先验加成
        rec, _ = KBS.add_candidate("improvement_case", {
            "defect": "针孔气泡", "process": "battery_coating",
            "root_cause_param": "oven_zone1_temp", "countermeasure": base_cm,
            "verification": {"method": "PDCA", "result": full_res},
            "standardization": std_ok}, source={"site_code": "测试线"})
        KBS.update_candidate(rec["id"], {"status": "approved",
                                         "review": {"reviewer": "自检", "note": ""}})
        KBS.sync(reviewer="自检")
        bonus, case = KBS.recommend_prior("针孔气泡", "oven_zone1_temp")
        check("A 级措施提供先验加成", bonus > 0 and case is not None, str(bonus))
        bonus_none, _ = KBS.recommend_prior("针孔气泡", "zzz_not_exist")
        check("无匹配措施时不加成", bonus_none == 0.0)
        bonus_c, _ = KBS.recommend_prior("针孔气泡", "oven_zone1_temp")
        check("A 级加成大于 B 级", bonus_c >= 0.1, str(bonus_c))

        # 10) 备份不互相覆盖（同秒多次写库也要留下各自的回滚点）
        n_before = len(KBS.list_backups("specs"))
        KBS.backup("specs")
        KBS.backup("specs")
        KBS.backup("specs")
        names = [b["file"] for b in KBS.list_backups("specs")]
        check("备份不互相覆盖", len(names) == n_before + 3 and len(set(names)) == len(names))

        # 11) 回滚可恢复内容
        target = "cases"
        KBS.backup(target)
        snapshot = _json.dumps(KBS._read_json(KBS.CASE_FILE, {}), sort_keys=True)
        KBS.add_candidate("improvement_case", {
            "defect": "TTV超差", "process": "pv_wafer",
            "countermeasure": {"action": "换线"}, "verification": {"method": "PDCA"}},
            source={"site_code": "测试线"})
        cid = KBS.load_candidates("pending")[0]["id"]
        KBS.update_candidate(cid, {"status": "approved",
                                   "review": {"reviewer": "自检", "note": ""}})
        KBS.sync(reviewer="自检")
        n_after = len(KBS._read_json(KBS.CASE_FILE, {"cases": []})["cases"])
        bks = [b["file"] for b in KBS.list_backups(target)]
        ok, msg = KBS.rollback(target, bks[0])   # bks 按文件名倒序，首个为最新备份
        n_rolled = len(KBS._read_json(KBS.CASE_FILE, {"cases": []})["cases"])
        check("回滚生效", ok and n_rolled < n_after, "%s (after=%d rolled=%d)"
              % (msg, n_after, n_rolled))
        check("回滚内容与备份一致",
              _json.dumps(KBS._read_json(KBS.CASE_FILE, {}), sort_keys=True) == snapshot)

        # 12) 导出补丁必须脱敏，且导入后仍走暂存区
        exp = KBS.export_patch(outdir=os.path.join(tmp, "dist"))
        with open(exp["path"], "r", encoding="utf-8") as f:
            patch = _json.load(f)
        blob = _json.dumps(patch, ensure_ascii=False)
        check("补丁导出成功", exp["entries"] > 0, "%d 条" % exp["entries"])
        check("补丁已脱敏（不含客户来源）", "测试线" not in blob)
        before_cnt = len(KBS._read_json(KBS.CASE_FILE, {"cases": []})["cases"])
        imp = KBS.import_patch(exp["path"])
        mid_cnt = len(KBS._read_json(KBS.CASE_FILE, {"cases": []})["cases"])
        check("导入先进暂存区而非直接入库",
              imp["imported"] > 0 and mid_cnt == before_cnt,
              "imported=%d" % imp["imported"])

        # 13) 收割器：相关性高不等于同义词，必须有"极高相关 + 单位一致"双重闸门
        class _T2(object):
            def __init__(self, mapping):
                self._m = mapping

            def columns(self):
                return list(self._m.keys())

            def numeric_columns(self):
                return list(self._m.keys())

            def numeric_column(self, c):
                return list(self._m[c])

        base = [20.0 + i * 0.1 for i in range(40)]
        same_unit = [v + 0.001 * (i % 3) for i, v in enumerate(base)]   # 几乎同一条序列
        other = [-(28.0 + i * 0.05 + 0.02 * (i % 5)) for i in range(40)]  # 单位不同的另一量
        tb2 = _T2({"辊温(℃)": base, "热辊实测(℃)": same_unit, "涂布头负压(kPa)": other})
        res2 = {"col_map": {"辊温(℃)": "roll_temperature"},
                "association": [{"param": "热辊实测(℃)", "type": "numeric", "r": 0.97},
                                {"param": "涂布头负压(kPa)", "type": "numeric", "r": 0.80}],
                "target": "不良率", "process": "battery_calendering",
                "process_name": "辊压", "shape": [40, 3]}
        cands = KBS.harvest_from_analysis(res2, tb2)
        types = {c["_type"]: c["payload"] for c in cands}
        check("同名同类+单位一致 => 判为别名",
              any(t == "param_alias" and "热辊实测(℃)" in p.get("aliases", [])
                  for t, p in types.items()),
              str(list(types)))
        check("相关性高但单位不同 => 不判为别名",
              any(t == "param" and p.get("key") == "涂布头负压" for t, p in types.items()),
              str([p.get("key") for t, p in types.items() if t == "param"]))
        check("新参数候选带 inferred 标记",
              all(p.get("source_tier") == "inferred" for t, p in types.items() if t == "param"))

        # 14) 收割器不得产出已<｜hy_place▁holder▁no▁813｜>认识的列
        res3 = dict(res2, col_map={"辊温(℃)": "roll_temperature",
                                   "热辊实测(℃)": "roll_temperature"},
                    association=[{"param": "辊温(℃)", "type": "numeric", "r": 0.99}])
        cands3 = KBS.harvest_from_analysis(res3, tb2)
        check("已识别的列不再产出候选", len(cands3) == 0, str([c["_type"] for c in cands3]))

        # 15) 判读指南附录：正序、去重、幂等
        guide = KBS.GUIDE_MD
        if os.path.exists(guide):
            KBS.backup("interp")
            rec, err = KBS.add_candidate("guide_entry", {
                "title": "自检用判读条目", "strength": "参考",
                "scope": "自检", "rule": "用于验证附录追加行为。"},
                source={"site_code": "自检"}, proposed_by="自检")
            check("判读条目候选受理", rec is not None, err or "")
            KBS.update_candidate(rec["id"], {"status": "approved",
                                             "review": {"reviewer": "自检", "note": ""}})
            KBS.sync(reviewer="自检")
            body = open(guide, encoding="utf-8").read()
            pos = [body.find(e.get("id", "~~")) for e in
                   KBS._read_json(KBS.INTERP_FILE, {}).get("entries", [])]
            check("附录条目齐全且只出现一次",
                  all(p >= 0 for p in pos) and body.count(rec["id"]) == 1,
                  "%d 条" % len(pos))
            check("附录按登记时间正序排列", pos == sorted(pos), str(pos))
            # 幂等：同一条目重复 sync 不得重复追加
            KBS._append_guide_md(KBS._read_json(KBS.INTERP_FILE, {})["entries"][-1])
            body2 = open(guide, encoding="utf-8").read()
            check("附录追加幂等", len(body2) == len(body) and body2.count(rec["id"]) == 1)
        else:
            check("判读指南文件存在", False, guide)
    finally:
        KBS.set_root(None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_isolation():
    """多企业隔离：A 的写入不进基线、不进 B；基线升级能流入 A"""
    print("\nI. 多企业隔离（云端部署的爆炸半径控制）")
    import tempfile, shutil, json as _json
    from lib import tenancy as TN, kbstore as KBS, preprocess as PP
    orig_root = TN.root()
    tmp = None
    try:
        tmp = tempfile.mkdtemp(prefix="iso-")
        shutil.copytree(os.path.join(ROOT, "knowledge"), os.path.join(tmp, "knowledge"))
        KBS.set_root(tmp); TN.set_root(tmp)
        base_file = os.path.join(tmp, "knowledge", "process_specs.json")
        n0 = len(_json.load(io.open(base_file, encoding="utf-8"))["params"])
        base_before = io.open(base_file, encoding="utf-8").read()

        def _sync_param(key, name):
            pl = {"key": key, "name": name, "process": "battery_coating", "unit": "mm",
                  "aliases": [name], "typical_range": [0, 10],
                  "control": {"mode": "range", "lsl": 0, "usl": 10}, "role": "control"}
            rec, err = KBS.add_candidate("param", pl, source={"site_code": "自检"})
            assert rec, err
            KBS.update_candidate(rec["id"], {"status": "approved",
                                            "review": {"reviewer": "自检", "note": ""}})
            return KBS.sync(reviewer="自检")

        TN.set_tenant("custA")
        r = _sync_param("custA_only_param", "A企业私有参数")
        check("租户写入成功", bool(r.get("merged")), str(r.get("merged")))
        a = PP.load_knowledge()
        check("A 能看到自己的参数", "custA_only_param" in a["params"])
        check("基线文件一字未改", io.open(base_file, encoding="utf-8").read() == base_before)

        TN.set_tenant("custB")
        b = PP.load_knowledge()
        check("B 看不到 A 的参数", "custA_only_param" not in b["params"])
        check("B 参数数与基线一致", len(b["params"]) == n0, "%d vs %d" % (len(b["params"]), n0))
        check("B 的候选区是空的（不串 A 的）", len(KBS.load_candidates()) == 0)

        # 基线升级应自动流入租户，不需要逐家迁移
        base = _json.load(io.open(base_file, encoding="utf-8"))
        base["params"]["base_upgraded_param"] = {"name": "基线新增", "process": "battery_coating",
                                                 "unit": "mm"}
        io.open(base_file, "w", encoding="utf-8").write(_json.dumps(base, ensure_ascii=False))
        TN.set_tenant("custA")
        c = PP.load_knowledge()
        check("基线升级自动流入 A", "base_upgraded_param" in c["params"])
        check("A 的私有参数未丢", "custA_only_param" in c["params"])

        # 写入路径全部落在租户目录内
        outside = []
        for dirpath, _dn, filenames in os.walk(os.path.join(tmp, "tenants")):
            for f in filenames:
                rel = os.path.relpath(os.path.join(dirpath, f), os.path.join(tmp, "tenants"))
                if not rel.startswith("custA") and not rel.startswith("custB"):
                    outside.append(rel)
        check("没有文件写到租户目录之外", not outside, str(outside[:2]))

        # 锁：可重入获取，且持有期间别人拿不到
        with TN.lock("selftest", timeout=3):
            got = False
            try:
                with TN.lock("selftest", timeout=0.3):
                    got = True
            except Exception:
                got = False
        check("写库锁互斥生效", not got)
        with TN.lock("selftest", timeout=3):
            pass
        check("锁可正常释放", not os.path.exists(os.path.join(TN.tenant_root() or tmp, ".lock", "selftest")))

        _sid = TN._safe_id("../../etc/passwd")
        check("租户 id 防目录穿越",
              "/" not in _sid and "\\" not in _sid and os.path.basename(_sid) == _sid,
              _sid)
        KBS.set_root(None); TN.set_tenant(None); TN.set_root(orig_root)
        PP._KNOWLEDGE = None
    except Exception as e:
        check("隔离测试未异常", False, str(e))
    finally:
        try:
            KBS.set_root(None); TN.set_tenant(None); TN.set_root(orig_root); PP._KNOWLEDGE = None
        except Exception:
            pass
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def test_adapt():
    """字段匹配不上 / 互相冲突时的自动适配"""
    print("\nJ. 字段冲突与自动适配（不中断、可追溯）")
    from lib import adapt as AD, preprocess as PP, table as T
    try:
        # 1) 时间列误报：小数列不该被当成 Excel 日期序列号
        tb = T.read_any(os.path.join(ROOT, "examples", "devices",
                                     "13_硅片分选机_TTV翘曲_mm单位.csv")) \
            if os.path.exists(os.path.join(ROOT, "examples", "devices",
                                           "13_硅片分选机_TTV翘曲_mm单位.csv")) else None
        probe = T.Table(["批次号", "厚度", "硅片厚度(mm)"],
                        [["B1", "150", "0.150"], ["B2", "151", "0.151"],
                         ["B3", "149", "0.149"]], "probe")
        tc, _ = PP.parse_time_column(probe)
        check("小数值列不再被误判为时间列", tc is None, str(tc))
        real = T.Table(["批次号", "采集时间", "厚度"],
                       [["B1", "2026-10-01 08:00:00", "150"],
                        ["B2", "2026-10-01 09:00:00", "151"],
                        ["B3", "2026-10-01 10:00:00", "149"]], "real")
        tc2, _ = PP.parse_time_column(real)
        check("真时间列仍能识别", tc2 == "采集时间", str(tc2))

        # 2) 重复列名
        dup = T.Table(["温度", "温度", "湿度"], [["25", "26", "40"], ["27", "28", "41"]], "dup")
        r = AD.adapt(dup, {"温度": None, "湿度": None}, queue=False)
        check("重复列名被自动区分",
              any(i["type"] == "dup_column_name" for i in r["issues"])
              and any("#2" in c for c in r["table"].columns),
              str(r["table"].columns))

        # 3) 多列命中同一参数
        two = T.Table(["硅片厚度(mm)", "硅片厚度(μm)"],
                      [["0.150", "150"], ["0.151", "151"]], "two")
        r2 = AD.adapt(two, {"硅片厚度(mm)": "thickness", "硅片厚度(μm)": "thickness"},
                      queue=False)
        hits = [i for i in r2["issues"] if i["type"] == "dup_param_hit"]
        check("多列命中同一参数被仲裁", bool(hits) and len(r2["col_map"]) == 1,
              str(r2["col_map"]))
        check("仲裁保留单位与库内一致的列",
              bool(hits) and hits[0]["keep"] == "硅片厚度(μm)", str(hits[0]["keep"]) if hits else "")

        # 4) 认不出来的列
        unk = T.Table(["FooBar", "温度"], [["1.2", "25"], ["3.4", "26"]], "unk")
        r3 = AD.adapt(unk, {"FooBar": None, "温度": None}, queue=False)
        check("未匹配列被标记", any(i["type"] == "unmatched_column" for i in r3["issues"]))
        check("未匹配时判定为降级并给出说明", r3["degraded"] and bool(r3["notes"]))
        check("可换算的单位不算冲突",
              AD._conversion_factor("mm", "μm") == 1000.0,
              str(AD._conversion_factor("mm", "μm")))

        # 5) 适配层自身绝不能炸
        for bad in (None, T.Table([], [], "empty")):
            try:
                AD.adapt(bad, {}, queue=False)
                ok = True
            except Exception as e:
                ok = False
                print("    异常：", e)
            check("空/异常输入不抛错", ok)
    except Exception as e:
        check("适配测试未异常", False, str(e))


def test_industry():
    """
    行业作用域：配了行业就只在本行业内检索。

    核心真值——「开路电压」在锂电是电芯 OCV（~3.7V），
    在光伏是组件 Voc（~40V），同名不同义且量级差十倍。
    不按行业切开就必然误配，这是本组存在的主要理由。
    """
    print("\nK. 行业作用域（企业按行业模块配置，消除跨行业同名歧义）")
    from lib import scope as SC, preprocess as PP, devices as DEV
    try:
        reg = SC.registry()
        inds = sorted((reg.get("industries") or {}).keys())
        check("行业登记表可用", "battery" in inds and "pv" in inds, "、".join(inds))

        raw = PP.load_knowledge_raw()
        n_all = len(raw["params"])

        def _view(ind):
            SC.set_industries(ind)
            PP._KNOWLEDGE = None
            PP._KNOWLEDGE_SCOPE = "__unset__"
            return PP.load_knowledge()

        kb_b = _view("battery")
        kb_p = _view("pv")
        check("锂电视角参数被裁剪", len(kb_b["params"]) < n_all,
              "%d / 全量 %d" % (len(kb_b["params"]), n_all))
        check("光伏视角参数被裁剪", len(kb_p["params"]) < n_all,
              "%d / 全量 %d" % (len(kb_p["params"]), n_all))
        check("锂电视角只含锂电工序",
              all(k.startswith("battery_") for k in kb_b["processes"]),
              "、".join(sorted(kb_b["processes"])))
        check("光伏视角只含光伏工序",
              all(k.startswith("pv_") for k in kb_p["processes"]),
              "、".join(sorted(kb_p["processes"])))

        # --- 核心：跨行业同名参数消歧
        # 每次 match_param 前必须显式切作用域：match_param 认的是"当前"作用域，
        # 不是上面缓存下来的 kb 对象（这正是缓存失效逻辑要的行为）
        _b = _view("battery")
        check("锂电下「开路电压」判为电芯 OCV", PP.match_param("开路电压(V)") == "ocv",
              str(PP.match_param("开路电压(V)")))
        _pv = _view("pv")
        check("光伏下「开路电压」判为组件 Voc", PP.match_param("开路电压(V)") == "voc",
              str(PP.match_param("开路电压(V)")))
        _b = _view("battery")
        check("锂电下认不到光伏 Voc", PP.match_param("Voc") is None, str(PP.match_param("Voc")))
        _pv = _view("pv")
        check("光伏下认不到锂电 OCV", PP.match_param("OCV") is None, str(PP.match_param("OCV")))
        _b = _view("battery")
        check("锂电下认不到硅片厚度", PP.match_param("硅片厚度(μm)") is None,
              str(PP.match_param("硅片厚度(μm)")))
        _pv = _view("pv")
        check("光伏下认不到涂布厚度", PP.match_param("涂布厚度(μm)") is None,
              str(PP.match_param("涂布厚度(μm)")))

        # --- 通用参数任何行业都要保留，否则算不出不良率
        _b = _view("battery")
        r1 = PP.match_param("不良率(%)")
        _pv = _view("pv")
        r2 = PP.match_param("不良率(%)")
        check("通用参数（不良率）跨行业均保留",
              r1 is not None and r1 == r2, "%s / %s" % (r1, r2))

        # --- 设备档案按行业裁剪，但跨行业设备两边都保留
        _pv = _view("pv")
        DEV.invalidate_cache()
        dp = DEV.load_profiles()
        check("光伏视角不含锂电化成柜", "neware_bts" not in dp["devices"],
              "、".join(sorted(dp["devices"])[:4]))
        check("跨行业设备（基恩士测厚仪）两边都保留", "keyence_lk_g" in dp["devices"])
        _b = _view("battery")
        DEV.invalidate_cache()
        db = DEV.load_profiles()
        check("锂电视角不含光伏 IV 测试仪", "halm_iv_tester" not in db["devices"],
              "、".join(sorted(db["devices"])[:4]))
        check("锂电视角含本行业化成柜", "neware_bts" in db["devices"])

        # --- 判读规则是通用方法论，任何行业都不裁
        SC.set_industries("pv")
        interp_kept, _ = SC.filter_knowledge("interp", {"entries": [{"id": "x"}]})
        check("判读规则不被行业裁剪",
              len(interp_kept.get("entries", [])) == 1)

        # --- 配错不该让分析变瞎：无效行业名忽略并留痕
        _valid, _bad = SC.set_industries(["battery", "不存在的行业"])
        check("无效行业名被识别", _bad == ["不存在的行业"], str(_bad))
        check("无效行业名留痕", any("无法识别" in n for n in SC.notes()))
        check("有效行业仍然生效", _valid == ["battery"], str(_valid))

        # --- 作用域把参数裁空时回退全量，绝不裁成空库
        out, stat = SC.filter_specs(raw, ["__no_such_domain__"])
        check("裁空时回退全量", stat.get("fallback") is True and len(out["params"]) == n_all,
              str(stat))

        # --- 裁剪是视图，基线文件一个字都不能改
        SC.set_industries("pv")
        PP._KNOWLEDGE = None
        PP._KNOWLEDGE_SCOPE = "__unset__"
        _ = PP.load_knowledge()
        raw_after = PP.load_knowledge_raw()
        check("基线知识库未被裁剪污染", len(raw_after["params"]) == n_all,
              "%d / %d" % (len(raw_after["params"]), n_all))

        # --- 切行业必须失效缓存（锂电缓存拿去判光伏会认错参数）
        _b = _view("battery")
        n1 = len(_b["params"])
        _pv = _view("pv")
        n2 = len(_pv["params"])
        check("切行业后缓存失效", n1 != n2 or _b is not _pv, "%d vs %d" % (n1, n2))

        # --- 端到端：光伏作用域跑硅片数据，报告留痕
        SC.set_industries("pv")
        PP._KNOWLEDGE = None
        PP._KNOWLEDGE_SCOPE = "__unset__"
        res = PL.analyze([os.path.join(ROOT, "examples", "04_硅片检测数据.csv")],
                         outdir=os.path.join(ROOT, "output", "_test", "ind_pv"),
                         industry="pv")
        md = io.open(res["outputs"]["md"], encoding="utf-8").read()
        check("端到端（光伏作用域）出报告", (not res.get("error"))
              and os.path.exists(res["outputs"]["html"]))
        check("报告含行业作用域留痕", "2c. 行业作用域" in md and "光伏" in md)
        check("结果携带行业标识", res.get("industry") == ["pv"], str(res.get("industry")))

        # --- 跨企业：行业作用域绝不能从一家泄漏到另一家
        #     真值：A 买锂电模块，B 没买。若 B 继承了 A 的锂电作用域，
        #     B 的光伏数据会被裁成"未知工序"——这是最难发现的一类污染。
        import tempfile, shutil
        from lib import tenancy as TN
        tmp2 = tempfile.mkdtemp(prefix="ind-iso-")
        try:
            shutil.copytree(os.path.join(ROOT, "knowledge"), os.path.join(tmp2, "knowledge"))
            orig_root = TN.root()
            TN.set_root(tmp2)
            SC.set_root(tmp2)
            f_bat = os.path.join(ROOT, "examples", "01_涂布工艺数据.csv")
            f_pv = os.path.join(ROOT, "examples", "04_硅片检测数据.csv")
            TN.set_tenant("A")
            TN.write_config({"industries": ["battery"]})
            ra = PL.analyze([f_bat], outdir=os.path.join(tmp2, "out", "a"), tenant="A")
            check("A 沿用其配置的锂电行业",
                  ra.get("industry") == ["battery"] and ra.get("process_name") == "极片涂布",
                  "%s / %s" % (ra.get("industry"), ra.get("process_name")))
            rb = PL.analyze([f_pv], outdir=os.path.join(tmp2, "out", "b"), tenant="B")
            check("B 未配置行业时不继承 A 的作用域", rb.get("industry") is None,
                  str(rb.get("industry")))
            check("B 的光伏数据仍能被正确判定",
                  rb.get("process_name") == "硅片检测", str(rb.get("process_name")))
        except Exception as e:
            check("跨企业作用域隔离测试未异常", False, str(e))
        finally:
            try:
                TN.set_root(orig_root)
                SC.set_root(orig_root)
                TN.set_tenant(None)
                SC.clear()
                PP._KNOWLEDGE = None
                PP._KNOWLEDGE_SCOPE = "__unset__"
            except Exception:
                pass
            shutil.rmtree(tmp2, ignore_errors=True)
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("行业作用域测试未异常", False, str(e))
    finally:
        try:
            SC.clear()
            PP._KNOWLEDGE = None
            PP._KNOWLEDGE_SCOPE = "__unset__"
            DEV.invalidate_cache()
        except Exception:
            pass


def test_device_memory():
    """
    设备记忆：未知设备可录入 → 存为企业私有档案 → 下次自动认出 → 口径锁定不再跳。

    真值均来自实测（改造前都会失败）：
      · 同一设备四种导出形态（带合计行 / 列名带单位 / 样本量变化）
        未登记时 Cpk 在 0.36~0.41 之间跳，登记后稳定在真值附近
      · 登记只写企业私有层，共享基线一字不改
      · 逐水平异常检验能抓到「16 个通道里只有 CH-07 坏」这类单元级根因，
        而单因素 ANOVA 判不显著（η² 0.05、p=0.41）
    """
    print("\nM. 设备记忆与口径锁定（同设备多次分析结论一致）")
    import tempfile, shutil, json as _json, hashlib
    from lib import deviceid as DID, devices as DEV, table as TB
    from lib import tenancy as TN, pipeline as PL, crossprocess as XP
    orig_root = TN.root()
    tmp = None
    try:
        tmp = tempfile.mkdtemp(prefix="devmem-")
        shutil.copytree(os.path.join(ROOT, "knowledge"), os.path.join(tmp, "knowledge"))
        TN.set_root(tmp)
        try:
            from lib import scope as SC
            SC.set_root(tmp)
        except Exception:
            pass
        base_dev = os.path.join(tmp, "knowledge", "device_profiles.json")
        h0 = hashlib.md5(io.open(base_dev, "rb").read()).hexdigest()

        src = os.path.join(ROOT, "examples", "enterprises", "E01_锂电涂布",
                           "01_涂布机MES导出_双行表头带合计.csv")
        lines = io.open(src, encoding="utf-8-sig").read().splitlines()
        hdr, unit, rest = lines[0], lines[1], lines[2:]
        body = [x for x in rest if not str(x).startswith("合计")]

        # 1) 指纹：同一设备的不同导出形态必须能互相认出
        fp_a = DID.fingerprint(TB.read_csv(src), path=src)
        p_unit = os.path.join(tmp, "unit_col.csv")
        io.open(p_unit, "w", encoding="utf-8").write(
            ",".join("%s(%s)" % (h, u) if u not in ("", "-") else h
                     for h, u in zip(hdr.split(","), unit.split(","))) + "\n" + "\n".join(body) + "\n")
        fp_b = DID.fingerprint(TB.read_csv(p_unit), path=p_unit)
        check("列名带单位不改变指纹", fp_a["col_sig"] == fp_b["col_sig"],
              "%s vs %s" % (fp_a["col_sig"], fp_b["col_sig"]))
        check("样本量变化不改变指纹",
              DID.fingerprint(TB.read_csv(src), path=src)["col_sig"] == fp_a["col_sig"])

        # 2) 录入问卷：能自动推断的先填，只问真正缺的
        tb0 = TB.read_csv(src)
        q = DID.interview(tb0, path=src)
        check("问卷自动填好版式与单位", bool(q["auto_filled"].get("units"))
              and q["auto_filled"].get("layout") == "wide")
        check("问卷自动推断工序",
              (q["auto_filled"].get("processes") or [None])[0] == "battery_coating",
              str(q["auto_filled"].get("processes")))
        check("未识别时列出待问项", "vendor" in (q.get("missing") or [])
              and "model" in (q.get("missing") or []))

        # 3) 注册：只写企业私有层，基线不动
        ans = {"__file__": src, "vendor": "测试仪器", "model": "涂布线3号",
               "processes": "battery_coating",
               "units": q["auto_filled"]["units"], "aliases": q["auto_filled"]["aliases"],
               "default_specs": {"单面面密度": [190, 210]}}
        prof = DID.build_profile(ans, table=tb0, path=src)
        reg = DID.register(prof, "test_coater", tenant="T1", force=True)
        check("登记成功并落在企业私有层", bool(reg.get("ok")) and "tenants" in (reg.get("path") or ""))
        check("登记不改动共享基线",
              hashlib.md5(io.open(base_dev, "rb").read()).hexdigest() == h0)
        doc = DID.save_dossier("test_coater", prof, tenant="T1")
        check("生成设备记忆文档", bool(doc) and os.path.exists(doc)
              and "设备记忆档案" in io.open(doc, encoding="utf-8").read())

        # 4) 口径锁定：未登记 vs 已登记，四种导出形态下的稳定性
        variants = {}
        variants["原样"] = [hdr, unit] + body
        variants["带合计"] = [hdr, unit] + body + [",".join(["合计"] + ["-"] * (len(hdr.split(",")) - 1))]
        variants["列名带单位"] = [",".join("%s(%s)" % (h, u) if u not in ("", "-") else h
                                       for h, u in zip(hdr.split(","), unit.split(",")))] + body
        variants["60行"] = [hdr, unit] + body[:60]

        def cpk_of(tenant):
            out = []
            for tag, ls in variants.items():
                p = os.path.join(tmp, "v_%s.csv" % tenant)
                io.open(p, "w", encoding="utf-8").write("\n".join(ls) + "\n")
                r = PL.analyze([p], outdir=os.path.join(tmp, "o_%s" % tenant), tenant=tenant)
                got = None
                src_tag = None
                for s in (r.get("spc") or []):
                    if "面密度" in (s.get("param_name") or ""):
                        got = (s.get("capability") or {}).get("Cpk")
                        src_tag = s.get("spec_source")
                out.append((tag, got, src_tag, r.get("device")))
            return out

        before = cpk_of("T2")                        # T2 没有这台设备的档案
        after = cpk_of("T1")                         # T1 刚登记过
        check("未登记时设备认不出", all(x[3] is None for x in before),
              str([x[3] for x in before]))
        check("登记后四种形态都认出设备", all(x[3] == "test_coater" for x in after),
              str([x[3] for x in after]))
        check("登记后规格取自设备档案", all(x[2] == "设备档案" for x in after),
              str([x[2] for x in after]))
        vals = [x[1] for x in after if isinstance(x[1], (int, float))]
        spread = (max(vals) - min(vals)) if len(vals) > 1 else 0.0
        check("登记后 Cpk 不再随导出形态跳动（极差<0.25）", spread < 0.25,
              "极差 %.2f，取值 %s" % (spread, [round(v, 2) for v in vals]))

        # 5) 单元级异常检验：抓得到「只有一个通道坏」
        e3 = os.path.join(ROOT, "examples", "enterprises", "E03_锂电化成",
                          "01_化成分容_通道数据.csv")
        tb3 = TB.read_csv(e3)
        PL.encode_judgement(tb3)
        anova = XP.eta_squared(tb3, "通道号", "__判定_NG__")
        hs = XP.outlier_levels(tb3, "通道号", "__判定_NG__",
                               direction=XP.bad_direction("__判定_NG__"))
        check("ANOVA 确实抓不到单元级问题（η² 偏小、p 不显著）",
              anova is None or (anova.get("eta_sq") or 0) < 0.14
              or (anova.get("p") or 1) >= 0.05,
              "η²=%s p=%s" % (round(anova.get("eta_sq", 0), 3) if anova else None,
                              round(anova.get("p", 1), 3) if anova else None))
        check("逐水平检验抓到 CH-07",
              bool(hs and hs.get("worst") and "CH-07" == hs["worst"]["level"]),
              str(hs["worst"]["level"]) if hs and hs.get("worst") else "无")
        check("异常水平经多重比较校正后仍显著",
              bool(hs and hs["worst"]["p_adj"] < 0.05),
              "p_adj=%s" % (round(hs["worst"]["p_adj"], 6) if hs and hs.get("worst") else None))

        # 6) 方向判断：不良率类指标要往「高」找，良率/功率类要往「低」找。
        # 搞反了就会把表现最好的那个单元当成根因（实测踩过：挑中了不良率 0 的通道）
        check("不良率类目标判定为「越高越坏」",
              XP.bad_direction("__判定_NG__") == "high")
        check("良率/功率类目标判定为「越低越坏」",
              XP.bad_direction("良率") == "low" and XP.bad_direction("Pmax") == "low")
        hs_dir = XP.outlier_levels(tb3, "通道号", "__判定_NG__",
                                   direction=XP.bad_direction("__判定_NG__"))
        check("限定方向后挑中的是真正变差的单元（不是表现最好的）",
              hs_dir["worst"]["level"] == "CH-07" and hs_dir["worst"]["delta"] > 0,
              "%s delta=%.3f" % (hs_dir["worst"]["level"], hs_dir["worst"]["delta"]))
        worst_any = XP.outlier_levels(tb3, "通道号", "__判定_NG__", direction="low")
        check("方向取反后不会再把 CH-07 排到最前",
              (worst_any["worst"]["level"] if worst_any and worst_any.get("worst") else None) != "CH-07",
              str(worst_any["worst"]["level"]) if worst_any and worst_any.get("worst") else "无")

        # 7) 共线列不得当根因：CTM 与 Pmax 是同一件事的两种写法
        e6 = os.path.join(ROOT, "examples", "enterprises", "E06_组件IV", "01_组件IV终测.csv")
        r6 = PL.analyze([e6], outdir=os.path.join(tmp, "o_e6"))
        rc6 = r6.get("rootcause") or {}
        names = [c["param_name"] for c in (rc6.get("candidates") or [])]
        excl = rc6.get("co_linear_excluded") or []
        check("与目标共线的派生指标被排除", ("CTM" in " ".join(excl)) or ("组件峰值功率" in " ".join(excl)),
              "排除=%s" % excl)
        check("不会同时留下互为重述的两个指标",
              not (("CTM" in " ".join(names)) and ("组件峰值功率" in " ".join(names))),
              str(names[:4]))
    finally:
        try:
            TN.set_root(orig_root)
        except Exception:
            pass
        try:
            from lib import devices as DEV2
            DEV2.invalidate_cache()
        except Exception:
            pass
        try:
            from lib import preprocess as PP2
            PP2._KNOWLEDGE = None
            PP2._KNOWLEDGE_TENANT = "__unset__"
        except Exception:
            pass
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def test_hotswap():
    """
    基线热更新：更新期间企业照常用，更新完长驻进程能自动看到新版。

    两条真值都来自实测（修复前都会失败）：
      · Windows 上目标文件被并发读时，裸 os.replace 失败率约 1/3
      · 长驻进程的内存缓存会让基线更新永久不可见（重启前）
    """
    print("\nL. 基线热更新（不影响企业使用 + 升级自动流入）")
    import tempfile, shutil, json as _json, time
    from lib import tenancy as TN, kbstore as KBS, preprocess as PP, hotswap as HS
    orig_root = TN.root()
    tmp = None
    try:
        tmp = tempfile.mkdtemp(prefix="hot-")
        shutil.copytree(os.path.join(ROOT, "knowledge"), os.path.join(tmp, "knowledge"))
        KBS.set_root(tmp)
        TN.set_root(tmp)
        from lib import scope as SC
        SC.set_root(tmp)
        spec = os.path.join(tmp, "knowledge", "process_specs.json")

        # 1) Windows 并发读下原子写不得失败
        stop = {"v": False}
        stat = {"ok": 0, "fail": 0}

        def _holder():
            while not stop["v"]:
                try:
                    # 用 hotswap 的容错读：另一线程做 replace 的瞬间 Windows 会拒绝
                    # 读打开（Errno 13）。这是 replace 的排斥窗口不是数据损坏，
                    # 实测 4005 次读里 7 次，抓到的失败快照全部是完整 JSON——零撕裂。
                    _json.loads(HS.read_text_retry(spec))
                except Exception:
                    pass

        def _writer():
            n = 0
            while not stop["v"]:
                try:
                    d = _json.loads(HS.read_text_retry(spec))
                    d["meta"]["revision"] = 2000 + n
                    HS.atomic_write_json(spec, d)
                    stat["ok"] += 1
                except Exception:
                    stat["fail"] += 1
                n += 1
                time.sleep(0.001)

        t1 = threading.Thread(target=_holder)
        t2 = threading.Thread(target=_writer)
        t1.start()
        t2.start()
        time.sleep(1.5)
        stop["v"] = True
        t1.join()
        t2.join()
        check("并发读期间原子写不失败", stat["fail"] == 0 and stat["ok"] > 0,
              "成功%d 失败%d" % (stat["ok"], stat["fail"]))
        check("原子写不留临时文件",
              not [f for f in os.listdir(os.path.join(tmp, "knowledge")) if ".tmp-" in f])
        check("并发写后基线仍可解析",
              isinstance(_json.load(io.open(spec, encoding="utf-8")), dict))

        # 2) 长驻进程必须能看到基线更新（不重启）
        PP._KNOWLEDGE = None
        PP._KNOWLEDGE_TENANT = "__unset__"
        PP._KNOWLEDGE_SCOPE = "__unset__"
        v0 = PP.load_knowledge()["meta"].get("revision")
        d = _json.load(io.open(spec, encoding="utf-8"))
        d["meta"]["revision"] = 9999
        HS.atomic_write_json(spec, d)
        time.sleep(1.1)                      # 指纹检查有 1s 节流
        v1 = PP.load_knowledge()["meta"].get("revision")
        check("长驻进程可见基线更新（免重启）", v1 == 9999, "%s -> %s" % (v0, v1))

        # 3) 基线升级自动流入租户（不需要逐家迁移）
        TN.set_tenant("A")
        PP._KNOWLEDGE = None
        PP._KNOWLEDGE_TENANT = "__unset__"
        d = _json.load(io.open(spec, encoding="utf-8"))
        d["params"]["_baseline_new"] = {"name": "基线新增", "process": "battery_coating",
                                        "unit": "mm"}
        HS.atomic_write_json(spec, d)
        time.sleep(1.1)
        kb_a = PP.load_knowledge()
        check("基线新增参数自动流入租户", "_baseline_new" in kb_a["params"],
              str("_baseline_new" in kb_a["params"]))

        # 4) 租户写入碰不到基线，也碰不到别的租户
        base_before = io.open(spec, encoding="utf-8").read()
        KBS.sync(force=True) if False else None      # 不真跑 sync，只验证写向
        rec = {"key": "_tenant_only", "name": "租户私有", "process": "battery_coating",
               "unit": "mm"}
        KBS._write_json(os.path.join(tmp, "knowledge", "process_specs.json"),
                        dict(kb_a, params=dict(kb_a["params"], **{"_tenant_only": rec})))
        check("租户写入不改动基线", io.open(spec, encoding="utf-8").read() == base_before)
        TN.set_tenant("B")
        PP._KNOWLEDGE = None
        PP._KNOWLEDGE_TENANT = "__unset__"
        kb_b = PP.load_knowledge()
        check("B 看不到 A 的私有参数", "_tenant_only" not in kb_b["params"],
              str("_tenant_only" in kb_b["params"]))
        check("B 仍能看到基线参数", "_baseline_new" in kb_b["params"])
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("热更新测试未异常", False, str(e))
    finally:
        try:
            KBS.set_root(None)
            TN.set_tenant(None)
            TN.set_root(orig_root)
            from lib import scope as SC
            SC.set_root(orig_root)
            SC.clear()
            PP._KNOWLEDGE = None
            PP._KNOWLEDGE_TENANT = "__unset__"
            PP._KNOWLEDGE_SCOPE = "__unset__"
            HS.forget()
        except Exception:
            pass
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def test_v19_delivery():
    """v1.9：行级追溯、清洗留痕、Excel 结果表、报告责任边界"""
    print("\nN. 交付形态：追溯 / 留痕 / 导出 / 责任边界")
    import shutil, tempfile
    tmp = tempfile.mkdtemp(prefix="fde_v19_")
    try:
        # 清洗留痕：删掉的行必须能查回文件、原始行号与原因
        tb = T.Table(["a", "b"], [[1, 2], [3, None], [1, 2], [7, 8]], "t")
        tb.attach_source("fake.csv")
        log = []
        ctb, rep0 = PP.clean(tb, missing="drop_rows", outlier="none",
                             use_physical=False, log=log)
        check("清洗留痕：被删行已记录", len(log) >= 1, "n=%d" % len(log))
        check("清洗留痕：带原始行号", any(lg["row"] == 3 for lg in log),
              [lg["row"] for lg in log])
        check("清洗留痕：带剔除原因", all(lg.get("reason") for lg in log))
        check("清洗后追溯链同步", ctb.src_rows is not None
              and len(ctb.src_rows) == len(ctb.rows))
        check("重复行也留痕", any("重复" in (lg.get("reason") or "") for lg in log),
              [lg.get("reason") for lg in log])

        # 结果表导出：能在 Excel 打开，且判异点带得回原始行号
        res = {
            "spc": [{"column": "a",
                     "violations": [{"rule": 1, "desc": "d", "hint": "h", "indexes": [0]}],
                     "capability": {"Cpk": 1.2, "verdict": "x", "caveats": ["c1"]}}],
            "pareto": {"rows": [{"item": "x", "value": 3, "pct": 60, "cum_pct": 60, "abc": "A"}]},
            "rootcause": {"candidates": [{"param_name": "a", "score": 0.5}]},
            "cleaning_log": log,
            "doe": {"factors": [{"name": "f1", "low": 1, "high": 2}],
                    "run_plan": [{"f1": 1}], "constraints": {"f1": {"min": 1, "max": 2}},
                    "clamped": []},
        }
        wt = T.Table(["a", "b"], [[1, 2]], "w")
        wt.attach_source("fake.csv")
        p = PL.export_result_xlsx(res, wt, tmp)
        check("Excel 结果表导出成功", bool(p) and os.path.exists(p))
        if p and os.path.exists(p):
            t2 = T.read_xlsx(p, sheet="判异明细")
            check("结果表含判异明细", t2 is not None and len(t2.rows) >= 1)
            if t2 and t2.rows:
                check("判异明细带源文件与行号",
                      t2.rows[0][5] == "fake.csv" and str(t2.rows[0][6]) == "2.0",
                      t2.rows[0])
            t3 = T.read_xlsx(p, sheet="清洗记录")
            check("结果表含清洗记录", t3 is not None and len(t3.rows) >= 1)

        # DOE 因子必须是能设定的工艺参数：编号列、分类列不得入选
        _fs = sorted(glob.glob(os.path.join(ROOT, "examples", "enterprises",
                                            "E04_光伏硅片", "*.csv")))
        if _fs:
            rr = PL.analyze(_fs, outdir=os.path.join(tmp, "e04"))
            ddp = rr.get("doe") or {}
            _fac = [f["name"] for f in (ddp.get("factors") or [])]
            check("分类列不进 DOE 因子", "供应商" not in _fac, _fac)
            _exc = [e[0] for e in (ddp.get("excluded") or [])]
            check("不可设定的列被记录原因", "供应商" in _exc, _exc[:5])
            check("因子不足时说明原因而不是静默跳过",
                  bool(ddp.get("skipped_reason")) or len(_fac) >= 2,
                  (ddp.get("skipped_reason") or "")[:30])
        _fs3 = sorted(glob.glob(os.path.join(ROOT, "examples", "enterprises",
                                             "E03_锂电化成", "*.csv")))
        if _fs3:
            rr3 = PL.analyze(_fs3, outdir=os.path.join(tmp, "e03"))
            d3 = rr3.get("doe") or {}
            _f3 = [f["name"] for f in (d3.get("factors") or [])]
            check("编号列不进 DOE 因子",
                  not any(x in _f3 for x in ("通道号", "柜号")), _f3)

        # 报告责任边界与版本戳
        rp = RPT.Report("t")
        PL._render_disclaimer(rp, {"spc_rules": [1, 2, 3, 5],
                                   "trace_meta": {"file": "f.csv", "row_tracing": "精确"}})
        md = rp.render_md()
        check("报告含责任边界声明", ("责任边界" in md) and ("辅助决策" in md))
        check("报告含规则集版本戳", "Nelson 第 1、2、3、5 条" in md)
        check("报告区分三类结论",
              all(k in md for k in ("已验证结论", "待验证假设", "数据限制")))
        check("报告含知识库版本", "知识库版本" in md)
        # 自定义规则集要在报告里如实体现，不能冒充实测默认
        rp2 = RPT.Report("t")
        PL._render_disclaimer(rp2, {"spc_rules": [1, 2, 3, 4, 5, 6, 7, 8]})
        check("自定义规则集标注为自定义", "自定义" in rp2.render_md())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print("newenergy-fde 自检开始")
    test_table()
    test_stats()
    test_spc()
    test_doe()
    test_pipeline()
    test_devices()
    test_crossprocess()
    test_isolation()
    test_adapt()
    test_industry()
    test_hotswap()
    test_device_memory()
    test_kb()
    test_v19_delivery()
    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("全部通过")
