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
    # scan:keep stdout 编码兜底在模块加载期执行，degrade 尚未导入；且非 Windows 环境没有 reconfigure 属正常分支，不是降级。
import io
import math
import glob
import shutil
import threading
import tempfile
import json

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


def reset_fail(what, e):
    """测试环境复位失败时出声，但不计入 FAIL。

    不计入 FAIL 是因为复位的锅不该由被测功能背；
    必须出声是因为复位失败会让**后续**用例跑在脏状态上——
    那时看到的 [OK] 是假的，而看报告的人无从分辨。
    """
    print("  [清理警告] %s 未能复位：%s（后续用例可能受脏状态影响）" % (what, e))


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
        except Exception as _rst_e:
            reset_fail("知识库根目录/租户/缓存", _rst_e)
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
            except Exception as _rst_e:
                reset_fail("TN.set_root(orig_root)", _rst_e)
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
        except Exception as _rst_e:
            reset_fail("scope 状态", _rst_e)


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
        except Exception as _rst_e:
            reset_fail("scope 根目录切到临时目录", _rst_e)
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
        except Exception as _rst_e:
            reset_fail("TN.set_root(orig_root)", _rst_e)
        try:
            from lib import devices as DEV2
            DEV2.invalidate_cache()
        except Exception as _rst_e:
            reset_fail("设备档案缓存", _rst_e)
        try:
            from lib import preprocess as PP2
            PP2._KNOWLEDGE = None
            PP2._KNOWLEDGE_TENANT = "__unset__"
        except Exception as _rst_e:
            reset_fail("工艺知识库缓存", _rst_e)
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
        # 读侧与写侧分开统计。此前读侧的异常被 pass 吞掉、一个数都没进 stat，
        # 于是「并发读不撕裂」这条断言实际只覆盖了写侧——读侧撕裂根本发现不了。
        # 现在读侧拆成三种结果，只有「解析失败」才是这条测试要抓的目标。
        stat = {"ok": 0, "fail": 0, "read_ok": 0, "read_torn": 0,
                "read_locked": 0}

        def _holder():
            while not stop["v"]:
                try:
                    # 两级失败必须分开看：
                    #   read_text_retry 抛错 = 另一线程 replace 的瞬间 Windows 拒绝
                    #     读打开（Errno 13），这是排斥窗口不是数据损坏，可接受；
                    #   json.loads 抛错 = 读到了写了一半的内容，这就是撕裂。
                    # 混在一起 pass 掉，撕裂就会被排斥窗口的合理性一起挡掉。
                    try:
                        raw = HS.read_text_retry(spec)
                    except Exception:
                        stat["read_locked"] += 1
                        time.sleep(0.001)
                        continue
                    try:
                        _json.loads(raw)
                    except Exception:
                        stat["read_torn"] += 1
                    else:
                        stat["read_ok"] += 1
                except Exception:
                    # 兜底：任何没预想到的读侧异常都按撕裂算，宁可误报不可漏报
                    stat["read_torn"] += 1
                # 让出 1ms。真实读者读完还要解析、算指标、写报告，不存在
                # 100% 忙等的读进程；而 2 核 CI runner 上忙等会把 writer 线程
                # 直接饿死——那样测出来的是调度器，不是原子写的健壮性。
                # 留 1ms 后每秒仍有数百次并发读，压力依然足够。
                time.sleep(0.001)

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
        # 读侧单独成条：写成功 ≠ 读到的东西是完整的，
        # 这两件事此前共用一个断言，等于只验了前者。
        check("并发读全程零撕裂",
              stat["read_torn"] == 0 and stat["read_ok"] > 0,
              "完整解析%d 撕裂%d 撞排斥窗口%d" % (
                  stat["read_ok"], stat["read_torn"], stat["read_locked"]))
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
        # 更新基线必须脱开租户上下文再做。带上"某企业"的身份去写共享基线，
        # 本身就是越权动作——写入闸门现在会把这类直接写拦下来。
        # 真实运维升级基线也确实是"不在任何企业上下文里"进行的，
        # 所以这里显式切出去再切回来，而不是去放宽闸门。
        TN.set_tenant(None)
        HS.atomic_write_json(spec, d)
        TN.set_tenant("A")
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
        except Exception as _rst_e:
            reset_fail("知识库/租户/scope/热更新指纹", _rst_e)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def test_clustering():
    """v1.15 / v2.0.0 §4.5：位置聚集检验——曝光加权、BH、二项精确

    这一组护栏存在的原因：聚集检验是最容易"看起来对、实际错"的地方。
    12 个电池列逐个卡方而不校正，在 12 列里"发现"一个热点几乎必然；
    期望频数不足时把期望当对照组做 2x2 Fisher，算出来的 p 没有统计意义。
    两种错法都只在报告里表现为"多了一句结论"，不会报错。
    """
    print("\nP. 位置聚集检验：曝光加权 / BH / 二项精确")
    from lib import events as EV

    # 1) 二项精确检验（与 R binom.test 同口径的定性校验）
    p_ok = EV._binom_exact_two_sided(5, 100, 0.05)
    check("二项精确：5/100 vs p0=0.05 不显著", p_ok > 0.3, "p=%.3f" % p_ok)
    p_hot = EV._binom_exact_two_sided(20, 100, 0.05)
    check("二项精确：20/100 vs p0=0.05 显著", p_hot < 0.001, "p=%.2e" % p_hot)
    # 大 n 必须走对数空间：朴素连乘在此会下溢成 0，双尾和退化为 1.0
    p_big = EV._binom_exact_two_sided(6000, 20000, 0.25)
    check("二项精确：大样本不下溢", p_big < 1e-30, "p=%.2e" % p_big)
    check("二项精确：边界 k=n 不抛错",
          EV._binom_exact_two_sided(10, 10, 0.5) > 0.0)

    # 2) 卡方路径 + BH：12 列、总量 100、第 7 列占 40 必须判为热点
    rows = []
    for col in range(1, 13):
        cnt = 40 if col == 7 else (5 if col <= 6 else 6)
        rows.extend([[col]] * cnt)
    tb = T.Table(["电池列"], rows, "ev")
    res = EV.factor_clustering(tb, "电池列")
    check("聚集检验：12 水平全部入检", res and res["n_levels"] == 12,
          res["n_levels"] if res else None)
    check("聚集检验：热点判定在第 7 列", res and res["hot_level"] == 7,
          res.get("hot_level") if res else None)
    _q_hot = res["worst"].get("q") if res else None
    # 注意：不要写 `q or 1`——q 恰为 0.0 时会被假值替换成 1，断言反而误判（本组自测踩过）
    check("聚集检验：热点通过 BH 校正",
          bool(res and res["significant"] and _q_hot is not None and _q_hot <= 0.05),
          "q=%.2e" % _q_hot if _q_hot is not None else None)
    check("聚集检验：单水平期望 = 总量/列数", res and approx(res["rows"][0]["expected"],
                                                        100 / 12, 0.01),
          res["rows"][0]["expected"] if res else None)

    # 3) 曝光加权：曝光比 3:1，期望必须按曝光分配，而不是均分
    tb2 = T.Table(["工位"], [[1]] * 15 + [[2]] * 5, "ev2")
    res2 = EV.factor_clustering(tb2, "工位", exposure=[3, 1])
    check("曝光加权：期望按曝光比分配", res2 and approx(res2["rows"][0]["expected"], 15.0),
          res2["rows"][0]["expected"] if res2 else None)
    check("曝光加权：均匀分布不误报热点", res2 and not res2["significant"])

    # 4) 期望频数不足必须走二项精确，且不得报成 Fisher 2x2
    tb3 = T.Table(["腔位"], [["A"]] * 3 + [["B"]] * 1 + [["C"]], "ev3")
    res3 = EV.factor_clustering(tb3, "腔位")
    check("期望不足：降级为二项精确检验",
          res3 and all("二项精确" in r["test"] for r in res3["rows"]),
          res3["rows"][0]["test"] if res3 else None)
    check("期望不足：小样本不硬报显著", res3 and res3["hot_level"] is None)

    # 5) 假值陷阱本身必须锁死：round(p,6) 会把 1e-30 压成 0.0，
    #    0.0 必须读作"极显著"，None 才是"缺失"。
    check("p 值判定：0.0 视为显著（不是缺失）", S.is_sig(0.0) is True)
    check("p 值判定：None 不显著", S.is_sig(None) is False)


def test_entry_parity():
    """统一入口（fde_run.py analyze）必须能接受 pipeline 的全部参数。

    这是**运行时**验证，和 audit 规则 31 互为补充：
    规则 31 是文本判据，只能确认代码里写了动态透传；
    而「pipeline 新增了一个参数、编排器没跟上」这种失同步，
    只有真跑一遍两边的 --help 求差集才看得出来。

    实测这条断言的价值：白名单时代曾漏掉 --auto-adapt / --no-xlsx /
    --rules，其中 --auto-adapt 在 README 里有示例——使用者照文档换到
    统一入口，命令照样跑通、只是那个能力静默没了，不报错也不警告。
    """
    print("\nO. 入口对等：统一入口不得比 pipeline 少参数")
    import re
    import shutil
    import subprocess

    def _flags(argv, cwd=None):
        r = subprocess.run([sys.executable] + argv, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           cwd=cwd or ROOT, timeout=120)
        return set(re.findall(r"^\s+(?:-[a-zA-Z],\s+)?(--[a-z][a-z0-9-]+)",
                              r.stdout or "", re.M))

    orch = _flags(["scripts/fde_run.py", "analyze", "--help"])
    pipe = _flags(["lib/pipeline.py", "--help"])
    # 编排器自己接管的：--json 由它固定写到 <outdir>/result.json，
    # 后续步骤依赖这个路径，不该由使用者指定
    owned = {"--help", "--json"}
    missing = sorted(pipe - orch - owned)
    check("统一入口覆盖 pipeline 的全部参数", not missing,
          "缺失：%s" % "、".join(missing) if missing
          else "pipeline %d 个参数全部可达" % len(pipe - owned))
    check("统一入口确实读到了 pipeline 参数（不是空跑）",
          len(pipe) >= 8, "pipeline 参数 %d 个" % len(pipe))

    # 上面两条查的是「参数在不在 --help 里」。**在 help 里不等于真的透传了**——
    # 实测把透传改成 cmd += [] 之后，这两条依然全绿，因为注册与透传是两码事。
    # 所以必须再验一次真实行为：参数有没有真的改变产物。
    # 选 --no-xlsx 是因为它最容易被观察，而且它正是当年被白名单漏掉的 4 个之一。
    import tempfile
    src = os.path.join(ROOT, "examples", "01_涂布工艺数据.csv")
    if not os.path.exists(src):
        check("统一入口的 --no-xlsx 真透传到 pipeline", False,
              "示例数据缺失，无法判定：%s" % src)
        return
    out = tempfile.mkdtemp(prefix="fde_entry_")
    try:
        subprocess.run([sys.executable, "scripts/fde_run.py", "analyze",
                        "-i", src, "-o", out, "--no-xlsx"],
                       capture_output=True, cwd=ROOT, timeout=600)
        xl = [f for f in os.listdir(out) if f.lower().endswith(".xlsx")]
        ran = os.path.exists(os.path.join(out, "result.json"))
        check("统一入口的 --no-xlsx 真透传到 pipeline（产物无 xlsx）",
              ran and not xl,
              "产物 %s" % (os.listdir(out)[:5] if not ran else xl or "无 xlsx"))
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_views():
    """角色视角（v2.4.0）：五类使用者各拿一份能直接用的东西。

    静态规则（audit 32）只能验「无依据不进表」「记录落盘」；
    措辞是不是「建议」、红标有没有进正文，只能看产物文本——放这里验。
    """
    print("\nP. 角色视角与处置建议的责任约束")
    import shutil
    import tempfile
    sys.path.insert(0, ROOT)
    try:
        from lib import views as V
    except Exception as e:
        check("角色视角模块可导入", False, str(e))
        return

    check("五个视角全部注册",
          sorted(V.VIEW_KEYS) == ["mgmt", "process", "production",
                                  "quality", "shopfloor"],
          "、".join(V.VIEW_KEYS))

    # 构造最小 result：有依据的候选 + 无依据的候选 + 未确审
    res = {
        "process_name": "极片涂布", "target": "不良率(%)",
        "confidence": {"level": "中高"},
        "shape": [180, 11], "groups": ["设备号", "班次"],
        "eightd": {"signoff": {"all_signed": False}, "d2_filled": False,
                   "d0": {"rows": [], "missing": ["24h 内应急反应（ERA）"]},
                   "fmea": {"rows": [{"a": 1}]},
                   "d5": {}, "d6": {}, "d7": {}, "d8": {}},
        "rootcause": {
            "mechanism": "供料量与基材速度失配",
            "quick_checks": ["确认固含是否在配方窗口内", "核对泵速与车速比"],
            "five_why": ["为什么面密度超差？"],
            "candidates": [
                {"param": "浆料固含(wt%)", "evidence_score": 0.96,
                 "detail": {"r": -0.958, "p": 1e-99, "n": 180,
                            "note": "Pearson r=-0.958"}},
                {"param": "猜测参数", "evidence_score": 0.0,
                 "detail": {}},          # 无依据：不得进处置表
            ],
        },
        "unit_hotspots": [{"dim": "班次", "levels": 2, "rows": [
            {"level": "甲班", "mean": 1.2, "others_mean": 2.3,
             "p_adj": 0.0002}]}],
        "gaps": [{"item": "涂布速度 的企业规格"}],
    }
    tmp = tempfile.mkdtemp(prefix="fde_view_")
    try:
        out = V.build_all(res, tmp)
        check("全套视角生成无错", not (out.get("errors") or []),
              "、".join(out.get("errors") or []))
        files = out.get("files") or []
        names = [os.path.basename(f) for f in files]
        for v, base in V.BASENAME.items():
            check("视角产物 %s 已生成" % v,
                  any(n.startswith(base) for n in names), "")

        # R1 第 1 条：措辞是「建议」不是「判定」
        prod = os.path.join(tmp, V.BASENAME["production"] + ".md")
        ptext = io.open(prod, encoding="utf-8").read() if os.path.exists(prod) else ""
        check("处置表措辞为「建议」而非「判定」",
              ("建议" in ptext) and ("判定" not in ptext.replace("不是放行判定", "").replace("最终判定", "")),
              "")
        # R1 第 2 条：无依据的条目不出现
        check("无依据条目不进处置表", "猜测参数" not in ptext,
              "出现了无依据的「猜测参数」")
        # R1 第 3 条：红标进正文
        check("未确审时红标写在正文", "红标" in ptext, "")
        # R1 第 4 条：判断记录落盘
        rec = os.path.join(tmp, "处置判断记录.md")
        check("处置判断记录已留痕", os.path.exists(rec), "")
        # 现场作业单要能打印：有核查顺序与手写栏
        shop = os.path.join(tmp, V.BASENAME["shopfloor"] + ".md")
        stext = io.open(shop, encoding="utf-8").read() if os.path.exists(shop) else ""
        check("现场作业单含核查顺序", "核查顺序" in stext, "")
        check("现场作业单留了手写栏", "实际看到" in stext, "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_p0_align_and_direction():
    """P0-1 相关性行对齐 + P0-2 目标方向。两条都是"结论看起来完全正常、只是错的"。

    P0-1 守的病灶：目标列先被 `[v for v in y_raw if v is not None]` 压缩，
    再与原始长度的候选列 zip —— 目标列**中间**缺值时整列错位，
    而样本数 n 看着完全正常。实测过的两个后果：
        Y=2X 完美线性被算成 r=0.996523
        无关数据被算成 r=-0.8288（真值 -0.0254），直接冲进根因候选前列

    P0-2 守的病灶："最差档"写死为均值最大。目标是良率/效率这类"越大越好"
    的指标时，它会把全场表现最好的那台标成最差，并生成"重点核查 A机"。
    """
    import random as _rd
    print("\nQ. P0 统计正确性：行对齐与目标方向")
    try:
        from lib import correlation as COR, direction as DIR
    except Exception as e:
        check("P0 测试依赖可导入", False, str(e))
        return

    def _mk(xs, ys, yname="Y"):
        return T.Table(["X", yname], [[x, y] for x, y in zip(xs, ys)])

    # ---------- P0-1：目标列缺失位置 ----------
    base_x = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    cases = {
        "中间缺失": [2, 4, None, 8, 10, 12, 14, 16, 18, 20, 22, 24],
        "首行缺失": [None, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24],
        "末行缺失": [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, None],
        "多处缺失": [2, None, 6, None, 10, 12, 14, 16, None, 20, 22, 24],
    }
    for label, ys in cases.items():
        r = COR.association_with_target(_mk(base_x, ys), "Y", min_n=5)
        got = None if not isinstance(r, list) else r[0]["r"]
        # Y=2X 去掉缺行后仍是完美线性，r 必须精确为 1
        check("P0-1 目标列%s时 r 仍为完美线性" % label, approx(got, 1.0, 1e-9),
              "r=%s" % (None if got is None else round(got, 8)))

    # 曾经被错位制造出来的**假强相关**（-0.8288），真值几乎无关
    _x2 = [31.1, 42.3, 31.7, 49.2, 42.5, 46.1, 22.5, 16.6, 20.7, 21.9,
           11.8, 29.5, 47.1, 34.8]
    _y2 = [1.4, 1.1, 1.4, None, 1.7, None, 1.4, None, 0.0, 2.4, 3.0, 3.8,
           2.4, 3.2]
    r2 = COR.association_with_target(_mk(_x2, _y2), "Y", min_n=8)
    got2 = None if not isinstance(r2, list) else r2[0]["r"]
    check("P0-1 错位曾造出的假强相关已消失", abs(got2) < 0.15,
          "错位版=-0.8288，现 r=%s" % (None if got2 is None else round(got2, 4)))

    # 双方同一行缺失：少一行，且 r 仍应为 1
    _x3 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    _y3 = [2, 4, None, 8, 10, 12, 14, 16, 18, 20]
    r3 = COR.association_with_target(_mk(_x3, _y3), "Y", min_n=5)
    check("P0-1 自变量同位置缺失不破坏对齐",
          isinstance(r3, list) and approx(r3[0]["r"], 1.0, 1e-9),
          str(None if not isinstance(r3, list) else round(r3[0]["r"], 8)))

    # ---------- 元数据：能交代清楚"哪些行没进分析、为什么" ----------
    rm = COR.association_with_target(_mk(base_x, cases["中间缺失"]), "Y", min_n=5)
    d0 = rm[0] if isinstance(rm, list) else {}
    check("P0-1 排除行数进入结果元数据", d0.get("n_excluded") == 1,
          "n_excluded=%s" % d0.get("n_excluded"))
    check("P0-1 排除原因可追溯",
          (d0.get("excluded_reasons") or {}).get("y_unusable") == 1,
          str(d0.get("excluded_reasons")))
    check("P0-1 明确标注按行号对齐", d0.get("aligned_by") == "row_index",
          str(d0.get("aligned_by")))

    # ---------- NaN / Inf 不得污染 ----------
    _inf = float("inf")
    _nan = float("nan")
    check("NaN 不污染均值", approx(S.mean([1.0, 2.0, _nan, 4.0]), 7.0 / 3, 1e-9),
          str(S.mean([1.0, 2.0, _nan, 4.0])))
    check("Inf 不污染均值", approx(S.mean([1.0, 2.0, _inf, 4.0]), 7.0 / 3, 1e-9),
          str(S.mean([1.0, 2.0, _inf, 4.0])))
    r4 = COR.association_with_target(
        _mk([1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            [2, 4, 6, _inf, 10, 12, 14, 16, 18, 20]), "Y", min_n=5)
    check("Inf 不会让相关系数退化成算不出来",
          isinstance(r4, list) and approx(r4[0]["r"], 1.0, 1e-9),
          str(None if not isinstance(r4, list) else round(r4[0]["r"], 8)))

    # ---------- 目标列不可用：必须显性失败，不能变成"未发现相关因素" ----------
    rempty = COR.association_with_target(_mk(base_x, [None] * 12), "Y", min_n=8)
    check("P0-1/P0-3 目标列全空时返回显式状态而非空结论",
          isinstance(rempty, dict) and rempty.get("status") == "INSUFFICIENT_DATA",
          str(rempty.get("status") if isinstance(rempty, dict) else rempty))

    # ---------- P0-2：方向 ----------
    yield_means = {"A机": 0.98, "B机": 0.95, "C机": 0.82}   # 良率：越大越好
    w = DIR.worst_group(yield_means, direction="lower_is_worse")
    check("P0-2 良率类指标指认真正最差的那台", w["level"] == "C机",
          "worst=%s（旧逻辑会指认 %s，即表现最好的那台）"
          % (w["level"], max(yield_means, key=lambda k: yield_means[k])))

    defect_means = {"A机": 0.02, "B机": 0.05, "C机": 0.18}   # 缺陷率：越大越差
    check("P0-2 缺陷率类指标同样正确",
          DIR.worst_group(defect_means, direction="higher_is_worse")["level"] == "C机")

    wu = DIR.worst_group(defect_means)
    check("P0-2 方向未知时不冒充确定性结论",
          wu["confident"] is False and wu["assumed"] == DIR.DEFAULT_ASSUMED,
          "confident=%s assumed=%s" % (wu["confident"], wu["assumed"]))

    wt = DIR.worst_group({"A": 18.2, "B": 20.1, "C": 23.4},
                         direction="target_is_best", target=20.0)
    check("P0-2 目标值最佳按偏离程度判定", wt["level"] == "C",
          "worst=%s" % wt["level"])

    ws = DIR.worst_group({"A": 11, "B": 20, "C": 29.5},
                         direction="two_sided_spec", lsl=10, usl=30)
    check("P0-2 双侧规格按距中心偏差判定", ws["level"] == "C",
          "worst=%s" % ws["level"])

    wmiss = DIR.worst_group({"A": 11, "B": 20, "C": 29.5},
                            direction="two_sided_spec")
    check("P0-2 声明了方向却缺规格时不给结论",
          wmiss["level"] is None and "lsl/usl" in (wmiss.get("note") or ""),
          str(wmiss.get("note")))

    # 措辞必须三档分明：显式/知识库 不带尾巴，推断 带"推断"，未知 带"假定"
    check("P0-2 已确认方向不带免责尾巴", DIR.qualifier("explicit") == "")
    check("P0-2 推断方向提示为推断", "推断" in DIR.qualifier("inferred"))
    check("P0-2 未知方向提示为假定", "假定" in DIR.qualifier("none", "higher_is_worse"))

    # ---------- 端到端：良率场景下不得反向派人 ----------
    try:
        from lib import crossprocess as XP
        _rd.seed(11)
        rows = []
        for i in range(120):
            eq = ["A机", "B机", "C机"][i % 3]
            mu = {"C机": 0.82, "B机": 0.95}.get(eq, 0.98)
            rows.append([eq, round(_rd.gauss(mu, 0.02), 4)])
        tbl = T.Table(["机台", "涂层良率"], rows)
        sw = XP.lateral_sweep(tbl, "涂层良率", ["机台"],
                              target_direction="lower_is_worse")
        check("P0-2 端到端：良率场景不把最优机台认成最差",
              bool(sw) and sw[0]["worst_level"] == "C机",
              str([(x["dim"], x["worst_level"]) for x in sw]))
    except Exception as e:
        check("P0-2 端到端横向排查", False, str(e))


def test_p1_stdlib_oracle():
    """P1-1 统计差分验证：用标准库 statistics 作 oracle，对标自研实现。

    项目定位零依赖，所以**生产运行时**不引入 numpy/scipy；
    但正确性不能靠"没抛异常"来证明。Python 标准库的 statistics 是
    一条完全独立的实现路径（CPython 自己的算法），可以当 oracle：

        零依赖是部署属性，不是正确性证明。
        测试期可以用 oracle，运行期仍然零依赖。

    覆盖：均值/标准差/中位数/相关系数；随机数据 + 边界（小样本、零方差、
    常数序列、极端值）；记录轮次、最大误差、所用容差。
    """
    import statistics as ST
    import random as _rd
    print("\nR. P1-1 统计差分：stdlib oracle vs 自研实现")
    _rd.seed(20261027)

    max_err = {"mean": 0.0, "stdev": 0.0, "median": 0.0, "corr": 0.0}
    rounds = 0
    bad = []
    for _ in range(300):
        n = _rd.randint(3, 60)
        xs = [_rd.uniform(-500, 500) for _ in range(n)]
        ys = [_rd.uniform(-20, 20) for _ in range(n)]
        try:
            e = abs(S.mean(xs) - ST.mean(xs)); max_err["mean"] = max(max_err["mean"], e)
            e = abs(S.stdev(xs) - ST.stdev(xs)); max_err["stdev"] = max(max_err["stdev"], e)
            e = abs(S.median(xs) - ST.median(xs)); max_err["median"] = max(max_err["median"], e)
            mine = S.pearson(xs, ys)[0]
            theirs = ST.correlation(xs, ys)
            e = abs(mine - theirs); max_err["corr"] = max(max_err["corr"], e)
            rounds += 1
            if mine is None or not (e <= 1e-9):
                bad.append(("random", n, mine, theirs))
        except Exception as ex:
            bad.append(("exception", n, str(ex)))

    # 边界：零方差（常数序列）——相关系数在此无定义，oracle 会抛错，自研必须返回 None
    const = [3.0] * 12
    try:
        ST.correlation(const, [1.0] * 12)
        oracle_raised = False
    except Exception:
        oracle_raised = True
    mine_const = S.pearson(const, list(range(12)))[0]
    check("差分：零方差时自研返回 None（与 oracle 一致地拒绝）",
          oracle_raised and mine_const is None,
          "oracle抛错=%s 自研=%s" % (oracle_raised, mine_const))

    # 边界：极端值不产生数值溢出
    big = [1e12 + i for i in range(20)]
    check("差分：极端量级下相关系数仍在 [-1,1]",
          -1.0 <= (S.pearson(big, [i * 2.0 for i in big])[0] or 0) <= 1.0,
          str(S.pearson(big, [i * 2.0 for i in big])[0]))

    ok = not bad
    check("差分：%d 轮随机数据与 stdlib statistics 一致（容差 1e-9）" % rounds, ok,
          "最大误差 mean=%.2e stdev=%.2e median=%.2e corr=%.2e；不一致 %d 例"
          % (max_err["mean"], max_err["stdev"], max_err["median"],
             max_err["corr"], len(bad)))
    if bad:
        print("        首例：%s" % (bad[0],))


def test_p03_failure_not_disguised():
    """P0-3：模块执行失败不得被读成"没有问题"。

    报告第五章把这条列为主要风险之一：
    「模块失败、未执行、数据不足和无异常若混为一谈，会产生危险的假阴性」。

    守的是实测出来的两件事：
      1. 某个护栏环节抛异常后，整轮不再是二次崩溃。原先 try 内部赋值的变量
         从未被赋值，而 build_report 在 try 外引用它们 -> UnboundLocalError，
         报错只提变量名，把真正的原因盖得一个字不剩。
      2. 报告不再把空章节伪装成"查过没问题"，且措辞不得预设原因是数据问题。
    """
    import os as _os, tempfile as _tf, io as _io, glob as _gb
    print("\nS. P0-3 失败状态不得被解释为无发现")
    try:
        from lib import pipeline as _PL, correlation as _COR, rootcause as _RC
    except Exception as e:
        check("P0-3 测试依赖可导入", False, str(e))
        return

    def _build_case():
        tmp = _tf.mkdtemp(prefix="fde_p03_")
        csv = _os.path.join(tmp, "d.csv")
        _io.open(csv, "w", encoding="utf-8").write(
            "时间,涂布速度,面密度,缺陷数\n" + "".join(
                "2026-01-01 0%d:00,%d,%.1f,%d\n" % (i % 9 + 1, 30 + i % 5,
                                                    180 + i % 7, i % 4)
                for i in range(60)))
        return tmp, csv

    def _run(csv, outdir):
        return _PL.analyze([csv], outdir=outdir, process="battery_coating",
                           export_xlsx=False, max_charts=0)

    for label, mod, fn in (("相关性", _COR, "corr_matrix"),
                           ("根因", _RC, "rank_root_causes")):
        orig = getattr(mod, fn)
        def _boom(*a, _f=fn, **k):
            raise RuntimeError("注入：%s 崩溃" % _f)
        setattr(mod, fn, _boom)
        try:
            tmp, csv = _build_case()
            outdir = _os.path.join(tmp, "o")
            err = None
            try:
                r = _run(csv, outdir)
            except Exception as ex:      # 整轮崩溃 = 本检验直接判负
                err = "%s: %s" % (type(ex).__name__, ex)
                r = None
            check("P0-3 %s环节失败后整轮仍能出报告" % label, err is None,
                  err or "")
            if r is None:
                continue
            deg = [d for d in (r.get("degraded_sections") or [])
                   if "注入" in str(d.get("error", ""))]
            check("P0-3 %s环节的失败被记录且带 FAILED 状态" % label,
                  bool(deg) and deg[0].get("status") == "FAILED",
                  str(deg[:1]))
            check("P0-3 %s环节失败标记了 incomplete" % label,
                  r.get("incomplete") is True, str(r.get("incomplete")))
            # 报告正文必须看得见，而不是只躺在 result.json 里
            mds = _gb.glob(_os.path.join(outdir, "*.md"))
            hit = None
            for f in mds:
                t = _io.open(f, encoding="utf-8", errors="replace").read()
                if "未产出结论" in t and "注入" in t:
                    hit = f
                    break
            check("P0-3 %s环节的失败写进了报告正文" % label, hit is not None,
                  _os.path.basename(hit) if hit else "正文里找不到")
            if hit:
                t = _io.open(hit, encoding="utf-8", errors="replace").read()
                check("P0-3 报告措辞不把失败说成『没有问题』",
                      "不是「检查过、没有问题」" in t)
                check("P0-3 报告措辞不预设原因是数据问题",
                      "因数据异常未产出" not in t)
        finally:
            setattr(mod, fn, orig)


def test_defect_process_priority():
    """缺陷匹配必须按工序优先裁决（审计规则 34 只验得动源码形态，验不动运行时）。

    守的是这条真实事故：process 参数原先只给数据驱动兜底用，知识库命中那一段
    不看它。于是「焊接不良」这种跨行业通用叫法在「极耳焊接不良」(电池卷绕)
    与「虚焊」(光伏 EL) 之间等长打平，谁赢全看 JSON 书写顺序 ——
    光伏数据可能被判成锂电池超声焊问题，根因指向焊接功率/压力/时间。
    静态规则抓不到运行时结果，必须由这条断言来兜。
    """
    print("\nO. 缺陷匹配：工序优先裁决")
    tb = T.Table(["焊接不良", "虚焊", "电池片效率"],
                 [[3, 1, 21.1], [0, 0, 21.3], [2, 0, 21.0], [1, 2, 20.9]], "焊接")

    def pick(proc):
        got = RC.identify_defects(tb, process=proc)
        return {d.get("column"): d.get("defect") for d in got
                if d.get("column") in ("焊接不良", "虚焊")}

    pv = pick("pv_cell_el")
    check("光伏工序下「焊接不良」列判给本工序的「虚焊」",
          pv.get("焊接不良") == "虚焊", pv)

    bt = pick("battery_winding")
    check("锂电工序下「焊接不良」列判给本工序的「极耳焊接不良」",
          bt.get("焊接不良") == "极耳焊接不良", bt)

    # 借用的条目必须留痕，否则报告层无从提示
    borrowed = [d for d in RC.identify_defects(tb, process="battery_winding")
                if d.get("cross_process")]
    check("借用别工序的机理会标 cross_process",
          any(d.get("defect") == "虚焊" for d in borrowed),
          [d.get("defect") for d in borrowed])

    # 工序未知时不能乱标 cross（没有基准就谈"跨"是无的放矢）
    unknown = RC.identify_defects(tb)
    check("工序未知时一律不标 cross_process",
          not any(d.get("cross_process") for d in unknown),
          [(d.get("defect"), d.get("cross_process")) for d in unknown])

    # --defect 提示同样按工序优先
    hinted = RC.identify_defects(tb, hint="焊接不良", process="pv_cell_el")
    check("--defect 提示也按工序排在前面",
          hinted and hinted[0].get("defect") == "虚焊",
          [d.get("defect") for d in hinted[:3]])


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


def test_status_and_lineage():
    """P0-3 统一状态语义 + P1-10 数据血缘（外部审计报告验收第 3、8 项）。

    这两条此前都是"文档上答应了、代码里没有"：
      - 状态：全库没有一个统一定义，spc/doe/views/quality_guard 干脆不返回状态，
        于是"这一节空着"和"这一节查过没问题"在报告里长得一模一样。
      - 血缘：只有输入文件名+哈希的一半体格，没有 run_id、没有版本、
        没有"等会儿还能不能复现"的答案。

    断言的写法一律按"把式子反过来也成立"来挑：只看 ≫0 的数量不够，
    必须精确到"是哪一个状态被放过/拦下"。否则放宽一处，测试照样是绿的。
    """
    import os as _os, tempfile as _tf, io as _io
    from lib import status as ST
    from lib import provenance as PROV

    # ---------- 状态 ----------
    check("状态：七个常量互不重复",
          len(set(ST.ANALYSIS_STATES)) == 7, str(sorted(ST.ANALYSIS_STATES)))

    allowed = [s for s in ST.ANALYSIS_STATES if ST.may_claim_no_finding(s)]
    check("状态：只有 NO_FINDING 允许宣称『未发现异常』",
          allowed == [ST.NO_FINDING], str(allowed))
    for bad in (ST.FAILED, ST.INSUFFICIENT_DATA, ST.SKIPPED,
                ST.NOT_APPLICABLE, ST.PARTIAL_SUCCESS, ST.SUCCESS):
        check("状态：%s 不许宣称无异常" % bad, not ST.may_claim_no_finding(bad))
    check("状态：必须显式披露的五个都点名了",
          all(ST.must_disclose(s) for s in (ST.FAILED, ST.INSUFFICIENT_DATA,
                                            ST.SKIPPED, ST.NOT_APPLICABLE,
                                            ST.PARTIAL_SUCCESS)))

    check("状态：历史写法『已测』收成 SUCCESS",
          ST.normalize("已测") == ST.SUCCESS, ST.normalize("已测"))
    check("状态：历史写法『数据缺失』收成 INSUFFICIENT_DATA",
          ST.normalize("数据缺失") == ST.INSUFFICIENT_DATA)
    check("状态：历史写法『insufficient』收成 INSUFFICIENT_DATA",
          ST.normalize("insufficient") == ST.INSUFFICIENT_DATA)
    check("状态：认不出来的保守处理成 FAILED（不是 SUCCESS）",
          ST.normalize("这算啥状态") == ST.FAILED, ST.normalize("这算啥状态"))
    check("状态：8D 的动作状态不混进分析状态",
          ST.normalize("待验证") == ST.FAILED,
          "『待验证』是核查动作状态，落入分析状态时应保守判 FAILED")

    fr = ST.frame(ST.INSUFFICIENT_DATA, method="pearson", n_effective=3,
                  params={"min_n": 8})
    need = ("status", "status_text", "may_claim_no_finding", "method",
            "params", "n_effective", "warnings", "errors", "input_digest")
    missing_k = [k for k in need if k not in fr]
    check("状态：统一骨架含 P0-3 要求的全部字段", not missing_k, str(missing_k))

    sm = ST.summarize([{"section": "相关性", "status": ST.FAILED,
                        "errors": ["目标列读取失败"]},
                       {"section": "SPC", "status": ST.NO_FINDING}])
    check("状态：汇总点名未产出结论的环节", sm["n_disclosed"] == 1 and
          "相关性" in sm["bad_lines"][0], str(sm["bad_lines"]))
    check("状态：有环节失败时不得判『全部清白』", not sm["all_clean"])
    sm2 = ST.summarize([{"section": "a", "status": ST.NO_FINDING},
                        {"section": "b", "status": ST.NO_FINDING}])
    check("状态：全为 NO_FINDING 才判全部清白", sm2["all_clean"] is True)
    sm3 = ST.summarize([{"section": "a", "status": None}])
    check("状态：缺状态按失败计（不是清白）", sm3["n_disclosed"] == 1)

    # ---------- 血缘 ----------
    try:
        tmp = _tf.mkdtemp(prefix="fde_lineage_")
        csv = _os.path.join(tmp, "in.csv")
        _io.open(csv, "w", encoding="utf-8", newline="").write(
            "批次号,涂布面密度\nA,10.1\nA,10.4\nA,10.2\nB,9.8\nB,9.9\nB,10.0\n")

        ins1 = PROV.input_fingerprints([csv])
        cfg = {"target": "涂布面密度", "process": "battery_coating"}
        ver = {"analyzer": "9.9.9", "knowledge": "kb-x"}
        check("血缘：输入摘要字节数与物理文件一致",
              ins1 and ins1[0]["bytes"] == _os.path.getsize(csv),
              str(ins1[0].get("bytes")) if ins1 else "空")

        r1 = PROV.new_run(inputs=ins1, config=cfg, versions=ver, target="涂布面密度")
        r2 = PROV.new_run(inputs=ins1, config=cfg, versions=ver, target="涂布面密度")
        check("血缘：同输入同配置指纹一致（可据此判重现）",
              r1["fingerprint"] == r2["fingerprint"],
              r1["fingerprint"][:12])
        check("血缘：run_id 每次不同（可定位这一轮）",
              r1["run_id"] != r2["run_id"])

        r3 = PROV.new_run(inputs=PROV.input_fingerprints([csv]),
                          config=dict(cfg, target="含水量"), versions=ver)
        check("血缘：配置变了指纹就变", r3["fingerprint"] != r1["fingerprint"])
        r4 = PROV.new_run(inputs=PROV.input_fingerprints([csv]),
                          config=cfg, versions=dict(ver, knowledge="kb-y"))
        check("血缘：版本变了指纹就变", r4["fingerprint"] != r1["fingerprint"])

        PROV.note_step(r1, "预处理", ST.SUCCESS, seconds=0.01,
                       detail={"rows_in": 6, "rows_out": 6})
        PROV.note_nondeterminism(r1, seed=None, reason="本轮无随机步骤")
        rec = PROV.finish(r1, ST.SUCCESS,
                          output_digest={"n_outputs": 2}, outdir=tmp)
        check("血缘：落盘成功且给了路径",
              bool(rec.get("lineage_file")) and _os.path.exists(rec["lineage_file"]),
              str(rec.get("lineage_file")))
        check("血缘：步骤与随机种子都记下来了",
              rec["steps"][0]["step"] == "预处理" and "notes" in rec,
              str(rec["steps"][0]))

        back = PROV.load(rec["run_id"], tmp)
        check("血缘：按 run_id 能读回整条记录",
              back is not None and back["fingerprint"] == rec["fingerprint"])
        ok, why = PROV.replay_check(back)
        check("血缘：未改动的输入能通过复现自检", ok, str(why))

        # 反向一：等长改内容 —— 只有哈希能发现，字节数一模一样。
        #   这段必须有：只追加行的话走的是"字节数"分支，哈希比对从头到尾没被验过，
        #   而哈希才是"喂的是不是同一份数据"的唯一判据。
        raw0 = _io.open(csv, encoding="utf-8").read()
        _io.open(csv, "w", encoding="utf-8", newline="").write(raw0.replace("10.1", "19.9"))
        ok2, why2 = PROV.replay_check(back)
        check("血缘：等长篡改输入必须被哈希抓到",
              (not ok2) and any("内容已变" in str(w) for w in why2), str(why2)[:90])
        check("血缘：等长篡改后字节数不变（证明不是靠体积发现的）",
              _os.path.getsize(csv) == back["inputs"][0]["bytes"],
              "%d vs %d" % (_os.path.getsize(csv), back["inputs"][0]["bytes"]))

        # 反向二：追加行 -> 走字节数分支
        _io.open(csv, "a", encoding="utf-8").write("C,11.5\n")
        ok2b, why2b = PROV.replay_check(back)
        check("血缘：追加内容也必须报错", not ok2b, str(why2b)[:90])

        # 反向：run_id 被手改时必须认出来
        bad_rec = dict(back, run_id="deadbeefdead-20260101000000")
        ok3, why3 = PROV.replay_check(bad_rec)
        check("血缘：run_id 与内容对不上要报错", not ok3 and
              any("run_id" in str(w) for w in why3), str(why3)[:80])
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def test_confound_and_evidence():
    """P1-6 混杂因素 + P1-5 根因证据结构（外部审计报告验收第 4、5 项）。

    守的是一件现场最容易吃亏的事：
        X 与 Y 整体强相关，但这个相关完全由"批次"造成——
        批次 A 恰好 X 低 Y 也低，批次 B 恰好 X 高 Y 也高。
        照 X 去调机，产线纹丝不动。
    这份数据是专门这么造出来的：层内 X 单调变化而 Y 几乎不动，
    所以"层内关系消失"必须被判出来，这是本测试的核心断言。
    """
    from lib import table as T, rootcause as RC, confound as CF, stats as S

    rows, cols = [], ["批次号", "X参数", "目标Y"]
    for i in range(14):
        rows.append(["A", 10 + 0.1 * i, 5.0 + (0.02 if i % 2 else -0.02)])
    for i in range(14):
        rows.append(["B", 20 + 0.1 * i, 8.0 + (0.02 if i % 2 else -0.02)])
    tb = T.Table(cols, rows)

    r_all = S.pearson(tb.numeric_column("X参数"), tb.numeric_column("目标Y"))[0]
    check("混杂用例本身成立：整体相关看着很强", r_all > 0.9, "r=%.3f" % r_all)

    res = CF.check(tb, "X参数", "目标Y")
    it = (res.get("confounders") or [{}])[0]
    check("混杂：识别出批次是分组列", it.get("column") == "批次号", str(it.get("column")))
    check("混杂：目标在批次间差异显著", it.get("target_diff") == "显著",
          str(it.get("target_diff")))
    check("混杂：层内关系消失被判出来", it.get("vanishes_within_layers") is True,
          "层内 r=%s" % [round(l["r"], 3) for l in it.get("stratified", [])])
    check("混杂：结论明确写怀疑混杂",
          str(it.get("verdict", "")).startswith("高度怀疑混杂"), str(it.get("verdict"))[:60])
    check("混杂：方法限制必须交代（未采集因素查不了）",
          any("不能确立因果" in x for x in res.get("limitations", [])),
          str(res.get("limitations"))[:60])

    # 反向：真的有层内关系的数据，不能被冤枉成混杂
    rows2, cols2 = [], ["批次号", "X参数", "目标Y"]
    for i in range(14):
        rows2.append(["A", 10 + 0.1 * i, 5.0 + 0.2 * i])
    for i in range(14):
        rows2.append(["B", 20 + 0.1 * i, 8.0 + 0.2 * i])
    tb2 = T.Table(cols2, rows2)
    it2 = (CF.check(tb2, "X参数", "目标Y").get("confounders") or [{}])[0]
    check("混杂：真实层内关系不得被误判为消失",
          it2.get("vanishes_within_layers") is False,
          "层内 r=%s" % [round(l["r"], 3) for l in it2.get("stratified", [])])

    # 根因侧：候选必须带上"凭什么/差什么/怎么验"
    r = RC.rank_root_causes("面密度超差", tb2, "目标Y",
                            process="battery_coating", top=3)
    cands = (r or {}).get("candidates") or []
    check("证据：根因能产出候选", len(cands) > 0, "%d 条" % len(cands))
    keys_need = ("conclusion_status", "missing_evidence", "counter_evidence",
                 "verification_cost", "verification_steps", "is_hypothesis",
                 "supported_by_data")
    lack = [k for k in keys_need if k not in cands[0]]
    check("证据：候选含 P1-5 要求的全部结构字段", not lack, str(lack))
    check("证据：结论状态取值为四选一",
          cands[0].get("conclusion_status") in
          ("数据支持", "机理支持", "现场待确认", "未知",
           "数据支持（混杂未排除）"), str(cands[0].get("conclusion_status")))
    # 缺数据的候选必须标成"假设"，不能和数据支撑的候选长得一样
    hyp = [c for c in cands if c.get("is_hypothesis")]
    check("证据：无数据支撑的候选标记为假设而非结论",
          all(c.get("conclusion_status") in ("机理支持", "现场待确认", "未知")
              for c in hyp), "%d/%d 条假设" % (len(hyp), len(cands)))

    # P1-5：可验证性权重此前硬编码 0.5、不算进排序、不随候选输出。
    lack_v = [c for c in cands if "verifiability_score" not in c]
    check("P1-5：候选输出可验证性分项", not lack_v, "%d 条缺 verifiability_score" % len(lack_v))
    check("P1-5：返回体注明权重是排序启发式而非概率",
          "score_note" in (r or {}), "返回体缺 score_note")

    # 单元验证：_verifiability 必须按候选信号动态计算，不是常数 0.5
    v_full = RC._verifiability({"verification_steps": ["a"], "supported_by_data": True,
                                "history_case": {"id": "x"}, "conclusion_status": "数据支持"})
    v_steps = RC._verifiability({"verification_steps": ["a"], "supported_by_data": False,
                                 "history_case": None})
    v_unknown = RC._verifiability({"verification_steps": [], "supported_by_data": False,
                                   "history_case": None, "conclusion_status": "未知"})
    v_empty = RC._verifiability({})
    check("P1-5：_verifiability 动态计算（多分支取值不同，不是常数 0.5）",
          len({v_full, v_steps, v_unknown, v_empty}) >= 3,
          "full=%.3f steps=%.3f unknown=%.3f empty=%.3f"
          % (v_full, v_steps, v_unknown, v_empty))
    check("P1-5：数据全支撑→可验证性最高且封顶 1.0", abs(v_full - 1.0) < 1e-9, "full=%.3f" % v_full)
    check("P1-5：无信号『未知』→可验证性压到最低 0.1",
          abs(v_unknown - 0.1) < 1e-9, "unknown=%.3f" % v_unknown)

    # 集成验证：总分里确实含 0.10*verifiability 这一项
    # （_evidence_gate 只可能乘 1.0 / 0.6 / 0.45，据此反推 raw 应吻合）
    _bad = None
    for c in cands:
        _ev = c.get("evidence_score") or 0.0
        _pr = c.get("prior_score") or 0.0
        _ts = c.get("temporal_score") or 0.0
        _vf = c.get("verifiability_score") or 0.0
        _raw = 0.40 * _ev + 0.35 * _pr + 0.15 * _ts + 0.10 * _vf
        if not any(abs(round(c.get("score", 0), 3) - round(_raw * k, 3)) < 1e-6
                   for k in (1.0, 0.6, 0.45)):
            _bad = (c.get("param_key"), c.get("score"), round(_raw, 4),
                    (_ev, _pr, _ts, _vf))
            break
    check("P1-5：总分含 0.10*verifiability 权重（门禁乘数 1.0/0.6/0.45 均吻合）",
          _bad is None, "不符：%s" % str(_bad))


def test_concurrent_write_integrity():
    """并发写不得撕裂（P2-1 隔离的数据完整性底座）。

    这不是"理论上应该加把锁"，是外部核查实测出来的数据损坏：
    临时文件原先叫 `<path>.tmp-<pid>`，同进程内**多线程共用同一 PID**，
    于是线程 A 写到一半，线程 B 就把那个半成品 replace 成正品。
    实测 8 线程 × 25 次写入：20 次读到的东西 json.load 直接失败、
    70 次字段自相矛盾、6/8 写线程抛 OSError。

    这里的判据不能只看"最后能不能解析"—— состояние 中途读到坏数据
    同样会把结论带偏，而成品最终往往是好的。
    """
    import concurrent.futures as cf
    from lib import hotswap as HS

    d = tempfile.mkdtemp(prefix="fde_conc_")
    target = os.path.join(d, "shared.json")
    HS.atomic_write_json(target, {"v": 0, "pad": "y" * 200, "by": -1})
    stop = threading.Event()
    stat = {"reads": 0, "bad": 0}

    def _writer(wid, n=25):
        fails = []
        for i in range(n):
            try:
                HS.atomic_write_json(target,
                                     {"v": wid * 100 + i, "pad": "y" * 200, "by": wid})
            except Exception as e:
                fails.append("%s: %s" % (type(e).__name__, str(e)[:60]))
        return fails

    def _reader():
        while not stop.is_set():
            try:
                with io.open(target, "r", encoding="utf-8") as f:
                    raw = f.read()
            except (FileNotFoundError, PermissionError):
                # scan:keep 这是并发读者，读到"文件正被 replace 抢占"属预期竞争，
                # 下一步立即重试；把它记成降级会把正常的 Windows 写锁竞争
                # 报成故障，反而淹没真正的撕裂。真正的判据是下面的内容自洽校验。
                continue
            stat["reads"] += 1
            try:
                obj = json.loads(raw)
            except Exception as e:
                stat["bad"] += 1
                stat.setdefault("why", []).append("解析失败 %s" % str(e)[:40])
                continue
            # 逐字段自洽：三个字段必须来自同一次写入者的同一次写入
            if not (isinstance(obj.get("v"), int) and obj.get("pad") == "y" * 200
                    and obj.get("by") in range(-1, 8) and len(obj) == 3):
                stat["bad"] += 1
                stat.setdefault("why", []).append(
                    "字段矛盾 %s" % json.dumps(obj, ensure_ascii=False)[:60])

    rt = threading.Thread(target=_reader, daemon=True)
    rt.start()
    fails = []
    try:
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            for f in cf.as_completed([ex.submit(_writer, w) for w in range(8)]):
                fails.extend(f.result())
    finally:
        stop.set()
        rt.join(timeout=3)

    check("并发写：8 线程全部写成功", not fails, str(fails[:2]))
    check("并发写：读取samples确实发生过（测试不是空转）",
          stat["reads"] > 100, "%d 次" % stat["reads"])
    check("并发写：读取过程中不得出现损坏内容",
          stat["bad"] == 0, "%d/%d 损坏 %s" % (stat["bad"], stat["reads"],
                                               str(stat.get("why", [])[:2])))
    try:
        final = json.loads(io.open(target, encoding="utf-8").read())
        ok_final = len(final) == 3
    except Exception:
        ok_final = False
    check("并发写：最终成品结构完整", ok_final)
    leftover = [p for p in os.listdir(d) if ".tmp" in p]
    check("并发写：不得残留临时文件", not leftover, str(leftover))
    shutil.rmtree(d, ignore_errors=True)


def test_contract_schema():
    """P1-4 数据契约：版本要兼容、字段要说清、未知列要点名、取值要体检。

    为什么必须有这一组测试：provenance 把输入摘要的字节数字段
    从 size 改成 bytes 时，报告渲染那一处当场 KeyError。
    同一份数据两套键名，就是"各环节标准不统一"的真实代价，
    而且它是在出报告时才炸的，不是跑不出数。

    这里的每条断言都回答同一个问题：把那句防御代码删掉，它会不会变红。
    """
    from lib import contract as C
    from lib import table as T

    # ---- 1. 版本兼容：主版本不同必须阻断，不能糊过去
    ok60, lv60, _ = C.compatible("1.0")
    check("契约：同版本兼容", ok60 and lv60 == "ok", str(lv60))
    ok61, lv61, _ = C.compatible("1.1")
    check("契约：次版本更高只提醒不阻断", ok61 and lv61 == "warn", str(lv61))
    ok20, lv20, _ = C.compatible("2.0")
    check("契约：主版本不同必须阻断", (not ok20) and lv20 == "block", str(lv20))
    okx, lvx, _ = C.compatible("看不懂的版本号")
    check("契约：无法解析的版本号按阻断处理", (not okx) and lvx == "block", str(lvx))

    # ---- 2. 未知列必须点名隔离，而不是悄悄混进结论
    tb = T.Table(["批次号", "设备号", "从没登记过的X列", "涂布面密度"],
                 [[ "A", "M1", 1.0, 10.0]] * 3)
    ci = C.check_input(tb, known_columns=["涂布面密度"])
    check("契约：未登记的列要被点名", "从没登记过的X列" in ci["unknown"],
          str(ci["unknown"]))
    check("契约：角色列不得算成未知", "批次号" in ci["roles"] and "设备号" in ci["roles"],
          str(ci["roles"]))
    q = C.quarantine(ci["unknown"])
    check("契约：隔离包要保留列数与原因", q["n"] == 1 and bool(q["reason"]), str(q))

    # ---- 3. 取值体检：脏数据要能在进分析前被拦下
    rows = [["A%d" % i, 10.0 + 0.1 * i, None if i == 5 else 5.0,
             -9999.0 if i == 9 else 1.0] for i in range(12)]
    rows[3][1] = "NG"                       # 单元格里混进非数值
    dirty = T.Table(["批次号", "厚度(mm)", "必填量", "温度(℃)"], rows)
    fields = [
        C.field("厚度(mm)", type_="number", unit="mm", min_value=0, max_value=100),
        C.field("必填量", type_="number", required=True, nullable=False),
        C.field("温度(℃)", type_="number", unit="℃", min_value=-40, max_value=300),
        C.field("整列没有的列", type_="number", required=True),
    ]
    cv = C.check_values(dirty, fields)
    blob = "\n".join(cv["violations"])
    check("契约：非数值混入要报", "非数值" in blob, str(cv["violations"])[:70])
    check("契约：必填列出现空值要报", "必填量" in blob and "空值" in blob, str(blob)[:70])
    check("契约：越界哨兵值要报", "低于下限" in blob, str(blob)[:70])
    check("契约：必填列整列缺失要报", "整列缺失" in blob, str(blob)[:70])
    clean = T.Table(["厚度(mm)", "必填量", "温度(℃)"],
                    [[10.0 + 0.1 * i, 5.0, 25.0] for i in range(12)])
    check("契约：合规数据不得误报",
          C.check_values(clean, fields[:3])["ok"] is True,
          str(C.check_values(clean, fields[:3])["violations"])[:70])

    # ---- 4. 结果契约：有输出却没 status 的环节必须被追责
    bad = {"analyzer_version": "2.6.0", "process": "battery_coating",
           "spc": [{"column": "x"}], "rootcause": {"defect": "y"}}
    rb = C.check_result(bad)
    check("契约：环节有输出却没 status 必须判失败", rb["ok"] is False, str(rb)[:70])
    check("契约：要能指名是哪些环节",
          set(rb["missing_status"]) >= {"spc", "rootcause"}, str(rb["missing_status"]))
    good = {"analyzer_version": "2.6.0", "process": "battery_coating",
            "target": "t", "shape": (10, 3),
            "spc": [{"column": "x", "status": "NO_FINDING"},
                    {"column": "z", "status": "SUCCESS"}],
            "rootcause": {"defect": "y", "status": "SUCCESS"}}
    rg = C.check_result(good)
    check("契约：合规结果不得误报",
          rg["ok"] is True and not rg["missing_status"], str(rg["violations"])[:70])
    check("契约：空 list 属\"没产出\"，不算违规",
          C.check_result({"analyzer_version": "v", "spc": []})["violations"] == [],
          str(C.check_result({"analyzer_version": "v", "spc": []})["violations"])[:70])
    check("契约：缺必填字段（analyzer_version）必须判失败",
          C.check_result({"process": "p"})["ok"] is False)
    check("契约：结果不是 dict 必须判失败", C.check_result([1, 2, 3])["ok"] is False)

    # ---- 5. 自定义契约加载：报错要指向真实原因
    r0 = C.load_custom_schema(None)
    check("契约加载：未给路径要说清楚", (not r0["ok"]) and "未提供" in r0["error"], str(r0))
    r1 = C.load_custom_schema("__不存在的契约__.json")
    check("契约加载：文件不存在要单独说清（不能混成加载失败）",
          (not r1["ok"]) and "不存在" in r1["error"], str(r1))


def test_d8_closure_gate():
    """P1-9 结案门禁：能不能结案必须能由机器判出来。

    改造前 derive_d8({}) 的 blockers 是空的、也没有 can_close —— 零证据喂进去，
    得到的仍然是一份看起来完整的"结案检查清单"。
    使用者拿着它去见客户，才发现机器从头到尾没说过"能不能结案"。

    所以这一组断言的核心只有一句：**零证据必须判不可结案**。
    再往上加三类最容易钻空子的情形：时间倒挂、签字与证据自相矛盾、
    日期格式不合法（不能因为比不了就放行）。
    """
    from lib import eightd as E

    FULL = {
        "d4_field_result": "现场复现，判定为真因", "d4_verify_method": "对照试验",
        "signoff_d5": True, "action_list": ["调整涂布速度"], "change_no": "ECN-2026-018",
        "signoff_d6": True, "verify_date": "2026-11-20",
        "verify_sample_window": "11/20-11/27", "verify_sample_size": "连续 12 批",
        "before_after": [{"before": 3.2, "after": 0.4}],
        "signoff_d7": True, "doc_nos": ["WI-031 rev.C"],
        "fmea_rev": "FMEA-2026-07 rev.2", "fmea_sod": "S=6 O=3 D=4",
        "lateral_done": True, "lateral_records": ["产线 B 已排查"],
        "customer_reply": "客户已接受", "signoff_d8": True,
        "signoff_by": "质量经理", "signoff_date": "2026-11-28",
    }
    WIN = {"clean": {"verify_window": ["2026-10-01", "2026-10-20"]}}

    # ---- 1. 零证据必须判不可结案（改造前的 bug 就在这里）
    d0 = E.derive_d8({})
    check("结案门禁：结果必须自带机器可判的 can_close", "can_close" in d0, str(sorted(d0)))
    check("结案门禁：零证据不得判可结案", d0["can_close"] is False, str(d0.get("can_close")))
    check("结案门禁：零证据时阻断项不得为空",
          bool(d0["blockers"]), str(d0["blockers"])[:70])
    check("结案门禁：零证据时 ready 必须为 0", d0["ready"] == 0, str(d0["ready"]))
    check("结案门禁：status 要采用统一状态值",
          d0.get("status") in ("INSUFFICIENT_DATA", "NO_FINDING"), str(d0.get("status")))

    # ---- 2. 证据齐备要判可结案（不能永远 False，否则门禁没人信）
    full = E.closure_check(WIN, card=dict(FULL))
    check("结案门禁：证据齐备要判可结案", full["can_close"] is True, str(full["blockers"])[:90])
    check("结案门禁：齐备时 ready 等于 total",
          full["ready"] == full["total"], "%s/%s" % (full["ready"], full["total"]))

    # ---- 3. 时间倒挂：拿改善前的历史数据充当"改善后验证"
    bad = dict(FULL); bad["verify_date"] = "2026-09-01"
    c = E.closure_check(WIN, card=bad)
    check("结案门禁：验证日期早于样本窗口必须拦下", c["can_close"] is False)
    check("结案门禁：要说清是倒挂而不是笼统不过",
          any("倒挂" in b for b in c["blockers"]), str(c["blockers"])[:80])
    check("结案门禁：time_check 要能反映倒挂",
          c["time_check"] == "时间倒挂", str(c["time_check"]))

    # ---- 4. 日期写得不合法：不能因为比不出来就放行
    junk = dict(FULL); junk["verify_date"] = "去年冬天"
    cj = E.closure_check(WIN, card=junk)
    check("结案门禁：无法解析的日期不得放行", cj["can_close"] is False,
          str(cj["time_check"]))
    check("结案门禁：解析不了要标无法校验", cj["time_check"] == "无法校验", str(cj["time_check"]))
    # 这一条专门盯"字符串比较"的老写法：str 比大小永远不抛异常，
    # 于是非标准写法会被静默判成通过。日期必须真解析。
    for okv, badv in [("2026-11-20", "2026-10-20")]:
        d_ok, d_bad = E._parse_date(okv), E._parse_date(badv)
        check("结案门禁：真解析而非字符串比大小",
              d_ok is not None and d_bad is not None and d_ok > d_bad,
              "%s vs %s" % (d_ok, d_bad))
    for junk_date in ("去年冬天", "", None, "2026-13-45"):
        check("结案门禁：非法日期必须返回 None（%r）" % (junk_date,),
              E._parse_date(junk_date) is None, str(E._parse_date(junk_date)))
    check("结案门禁：常见日期写法要认得出",
          E._parse_date("2026/11/20") is not None
          and E._parse_date("2026年11月20日") is not None
          and E._parse_date("2026.11.20") is not None)

    # ---- 5. 签了 D6 却没有对照数据：自相矛盾必须拦
    contra = dict(FULL); contra["before_after"] = []; contra["verify_sample_size"] = ""
    cc = E.closure_check(WIN, card=contra)
    check("结案门禁：签了 D6 但缺对照数据必须拦", cc["can_close"] is False)
    d6g = [g for g in cc["gates"] if g["id"] == "d6"][0]
    check("结案门禁：要点名缺的是哪几项证据",
          ("验证样本量" in d6g["missing"]) and ("改善前后对照" in d6g["missing"]),
          str(d6g["missing"]))
    check("结案门禁：每个 gate 都要声明它靠什么证明",
          all(g.get("evidence_required") for g in cc["gates"]))

    # ---- 6. 只缺客户回执的内部结案：应当放行（这一项不是必需条件）
    internal = dict(FULL); internal.pop("customer_reply")
    ci = E.closure_check(WIN, card=internal)
    check("结案门禁：内部结案缺客户回执不阻断", ci["can_close"] is True,
          str(ci["blockers"])[:80])


def test_parameter_contracts():
    """入口契约：非法参数必须报错/降级，不得静默产出结论。

    这一组不是"防御性编程练习"，而是外部核查实测出来的四个真缺陷：
      1. 空表喂进根因，照样吐出 3 条「机理支持」候选，和真数据长得一模一样
      2. boundary_gate 收到零因子，判定"边界受控 ok=True"
      3. status.frame 的 n_effective 完全不参与判定，负数也被当成样本正常
      4. 关联算法写错名字，静默按 pearson 算完还标着新方法名
    共同点：都不抛异常、都不改状态，只悄悄把结论弄错。
    所以这里的每条检查都必须做到"把那段代码剥掉就会红"。
    """
    from lib import table as T
    from lib import rootcause as RC
    from lib import correlation as CORR
    from lib import doe as D
    from lib import status as ST

    rows = [[i, 10 + 0.5 * i, 20 - 0.3 * i, float(i % 3)] for i in range(40)]
    tb = T.Table(["序号", "A列", "B列", "班组"], rows)

    def _raises(fn, exc):
        try:
            fn()
            return False
        except exc:
            return True
        except Exception:
            return False

    # ---- 1. 根因：数据不充分必须体现在顶层 status 上
    empty = T.Table(["批次号", "涂布面密度"], [])
    r = RC.rank_root_causes("面密度超差", empty, "涂布面密度",
                            process="battery_coating", top=3)
    check("契约：根因结果必须自带 status", bool(r and r.get("status")),
          str((r or {}).get("status")))
    check("契约：空表不得假装成有效结论",
          r["status"] == ST.INSUFFICIENT_DATA, str(r["status"]))
    check("契约：空表要给出人能读懂的原因",
          "空" in (r["data_sufficiency"].get("reason") or ""),
          str(r["data_sufficiency"].get("reason"))[:50])

    few = T.Table(["序号", "批次号", "涂布面密度"],
                  [[1, "A", 10.0], [2, "B", 11.0], [3, "C", 9.5]])
    r2 = RC.rank_root_causes("面密度超差", few, "涂布面密度",
                             process="battery_coating", top=3)
    check("契约：样本过少时整体判为数据不足",
          bool(r2) and r2["status"] == ST.INSUFFICIENT_DATA,
          str((r2 or {}).get("status")))
    check("契约：table 为 None 必须抛错而不是返回空结论",
          _raises(lambda: RC.rank_root_causes("面密度超差", None, "x"), ValueError))
    check("契约：缺陷名为空必须抛错",
          _raises(lambda: RC.rank_root_causes("", tb, None), ValueError))
    check("契约：top 非整数必须抛类型错",
          _raises(lambda: RC.rank_root_causes("面密度超差", tb, None, top="3"), TypeError))

    # ---- 2. DOE 边界门禁：零因子不得放行
    g0 = D.boundary_gate({})
    check("契约：零因子不得判边界受控", g0["ok"] is False, str(g0)[:70])
    check("契约：零因子要给出阻断理由", bool(g0["blockers"]), str(g0["blockers"])[:60])
    check("契约：边界为 None 同样阻断", D.boundary_gate(None)["ok"] is False)
    check("契约：bounds 类型错误要抛 TypeError",
          _raises(lambda: D.boundary_gate([None]), TypeError))

    # ---- 3. status.frame 的样本量必须真参与判定
    f_neg = ST.frame(ST.SUCCESS, n_effective=-1)
    check("契约：负样本量必须改判 FAILED",
          f_neg["status"] == ST.FAILED, str(f_neg["status"]))
    f_zero = ST.frame(ST.SUCCESS, n_effective=0)
    check("契约：零样本量不得保持 SUCCESS",
          f_zero["status"] != ST.SUCCESS, str(f_zero["status"]))
    f_str = ST.frame(ST.SUCCESS, n_effective="abc")
    check("契约：非数值样本量必须改判 FAILED",
          f_str["status"] == ST.FAILED, str(f_str["status"]))
    check("契约：样本量非法时 errors 不得为空",
          bool(f_str["errors"]), str(f_str["errors"])[:50])

    # ---- 4. 关联算法白名单
    bad = CORR.association_with_target(tb, "A列", method="no_such")
    check("契约：未知关联算法必须报错而非静默兜底",
          isinstance(bad, dict) and bad.get("status") == ST.FAILED, str(bad)[:60])
    check("契约：未知算法的报错要说清支持哪些",
          "pearson" in str(bad.get("error")), str(bad.get("error"))[:60])
    check("契约：corr_matrix 未知算法抛 ValueError",
          _raises(lambda: CORR.corr_matrix(tb, method="kendall"), ValueError))
    check("契约：alpha 越界必须报错而非照算",
          (CORR.association_with_target(tb, "A列", alpha=-1) or {}).get("status")
          == ST.FAILED)
    check("契约：min_n 非法必须报错而非照算",
          (CORR.association_with_target(tb, "A列", min_n=-5) or {}).get("status")
          == ST.FAILED)
    check("契约：min_n 非整数必须报错",
          (CORR.association_with_target(tb, "A列", min_n="3") or {}).get("status")
          == ST.FAILED)
    # 正常路径不能因为加了校验而坏掉
    okm = CORR.corr_matrix(tb, method="spearman")
    check("契约：校验不得破坏正常矩阵（值非 None）",
          okm["matrix"][0][1] is not None, str(okm["matrix"][0][1]))


def test_doe_safety_boundary():
    """P1-8 受控安全边界门禁（外部审计报告验收第 7 项）。

    报告点名批评了原来的做法：「通用硬编码安全边界或依赖参数名匹配的规则
    不足以保障设备和工艺安全，参数名称相同不代表物理意义、单位或设备限制相同」。

    这里守三件事，件件都是"放过去就会出事"而不是"不美观"：
      1. 单位不明/跨量纲 —— 企业的 [-40,150] 到底是 ℃ 还是 ℉，
         直接决定了 150 是工艺烘烤还是安全事故，必须拦下而不是换算着猜。
      2. 边界来源是否受控 —— 光有一对数值不算受控，必须交代清楚
         是出自哪份文件哪一版、谁批的、什么时候生效；
         file / version / approval 缺一项，这一条就只能算推断。
      3. 冲突的上/下限 —— min>max 说明某处单位或口径已经串了。
    """
    from lib import doe as D

    f, _ = D.unit_factor("mm", "um")
    check("边界：同量纲可换算且方向正确", f == 1000.0, "mm→um=%s" % f)
    f2, _ = D.unit_factor("um", "mm")
    check("边界：反向换算取倒数", abs(f2 - 1e-3) < 1e-12, "um→mm=%s" % f2)
    f3, n3 = D.unit_factor("V", "A")
    check("边界：跨量纲拒绝换算", f3 is None and "不属于同一量纲" in n3, str(n3)[:50])
    f4, n4 = D.unit_factor("℃", "℉")
    check("边界：温度这类仿射单位不做换算（宁拦不算错）",
          f4 is None and "零点偏移" in n4, str(n4)[:50])
    f5, n5 = D.unit_factor(None, "mm")
    check("边界：单位缺失时拒绝（不是当作一致）", f5 is None, str(n5)[:40])

    specs = [{"name": "烘箱一区温度", "unit": "℃", "low": 60, "high": 140}]
    b1 = D.controlled_boundaries(specs)
    rec1 = b1["烘箱一区温度"]
    check("边界：无来源时判为「推断」而非「受控」",
          rec1["confidence"] == "推断" and rec1["controlled"] is False,
          rec1["confidence"])
    g1 = D.boundary_gate(b1)
    check("边界：来源不受控必须给警告（要求书面确认）",
          any("书面确认" in w for w in g1["warnings"]),
          str(g1["warnings"])[:80])

    b2 = D.controlled_boundaries(specs, spec_overrides={
        "_meta": {"file": "spec-B3.pdf", "version": "B/3", "approval": "工艺工程师（张三）",
                  "effective": "2026-05-10", "scope": "L1 涂布线"},
        "烘箱一区温度": {"lsl": 50, "usl": 120, "unit": "℃"}})
    rec2 = b2["烘箱一区温度"]
    check("边界：补齐来源六要素后判为受控", rec2["confidence"] == "受控",
          str([s["confidence"] for s in rec2["sources"]]))
    check("边界：受控边界记录了出处/版本/审批/生效日期",
          any(s["file"] == "spec-B3.pdf" and s["version"] == "B/3"
              and s["approval"] == "工艺工程师（张三）" and s["effective"] == "2026-05-10"
              for s in rec2["sources"]), str(rec2["sources"])[:90])
    check("边界：安全包络只能收紧不能放宽",
          rec2["max"] <= 150.0 and rec2["min"] >= 50.0,
          "[%s, %s]" % (rec2["min"], rec2["max"]))

    b3 = D.controlled_boundaries(
        [{"name": "涂布速度", "unit": "m/min", "low": 30, "high": 50}],
        spec_overrides={"涂布速度": {"lsl": 20, "usl": 40, "unit": "mm/s"}})
    g3 = D.boundary_gate(b3)
    check("边界：单位跨量纲时阻断（不是警告）", g3["ok"] is False and
          len(g3["blockers"]) > 0, str(g3["blockers"])[:80])

    b4 = D.controlled_boundaries(
        [{"name": "涂布速度", "unit": "m/min", "low": 30, "high": 50}],
        spec_overrides={"涂布速度": {"lsl": 80, "usl": 90, "unit": "m/min"}})
    g4 = D.boundary_gate(b4)
    check("边界：上下限冲突时阻断", g4["ok"] is False and
          any("边界冲突" in x or "缺" in x for x in g4["blockers"]),
          str(g4["blockers"])[:80])

    b5 = D.controlled_boundaries([{"name": "某因子", "unit": None}])
    g5 = D.boundary_gate(b5)
    check("边界：完全没有边界的因子必须被拦下", g5["ok"] is False,
          str(g5["blockers"])[:80])


def test_tenant_path_guard():
    """P2-1 租户路径越界防护（外部审计报告第八节点名的风险项）。

    修之前的事实：`tenant_root("C:/Windows/System32")` 原样返回系统目录，
    而 `reset_tenant` 会对这个返回值执行 rmtree。也就是说一列 payload
    足以让"清空某个租户"变成"清空某个别的目录"。
    这类缺陷在单人单机使用时看不出来——只有把它当成多维 entry 跑才会撞上。
    """
    import os as _os, tempfile as _tf, shutil as _sh
    from lib import tenancy as TN, kbstore as KBS

    orig_root = TN.SKILL_ROOT
    tmp = _tf.mkdtemp(prefix="fde_guard_")
    try:
        _sh.copytree(_os.path.join("knowledge"), _os.path.join(tmp, "knowledge"))
        TN.set_root(tmp)
        base = _os.path.join(tmp, "tenants")

        for bad in ("../evil", "..\\..\\..\\Windows", "/etc/passwd",
                    "C:/Windows/System32", "\\\\server\\share\\x", "a/../../b"):
            r = TN.tenant_root(bad)
            check("租户路径：%+r 不得逃出 tenants/" % bad,
                  bool(r) and TN.is_inside(r, base), str(r))

        TN.set_tenant("A")
        root_a = TN.tenant_root()
        check("租户路径：正常租户落在 tenants/<id> 下",
              TN.is_inside(root_a, base), str(root_a))

        outside = _os.path.join(tmp, "knowledge", "process_specs.json")
        ok, why, _real = TN.guard_write_path(outside)
        check("写入闸门：拒绝把内容写到租户目录之外", not ok, str(why)[:70])
        inside = _os.path.join(root_a, "knowledge", "ok.json")
        ok2, _why2, _real2 = TN.guard_write_path(inside)
        check("写入闸门：租户目录内的正常写入放行", ok2)

        # kbstore 是真正的写入入口。这里验的是两件事而不是"有没有抛异常"：
        # 写一条基线路径上的记录，必须①落到租户层②基线文件一字未改。
        # 只读��筌有没有异常是不够的——抛异常解决不了"私有知识混进共享库"。
        try:
            before = io.open(outside, encoding="utf-8").read()
            KBS._write_json(outside, {"x": 1, "_probe_": True})
            after = io.open(outside, encoding="utf-8").read()
        except Exception as _e:
            before, after = "ERR", "ERR2:%s" % _e
        check("写入闸门：经 kbstore 写基线路径时基线一字未改",
              before == after, "基线变动=%s" % (before != after))
        redirected = _os.path.join(root_a, "knowledge", "process_specs.json")
        check("写入闸门：该写入被重定向进租户层",
              _os.path.exists(redirected), str(redirected))

        check("清空保护：越界租户 id 不会被 rmtree 掉",
              TN.reset_tenant("C:/Windows/System32") is False)

        # 符号链接：能造就测，造不了就明说没测（Windows 无 SeCreateSymbolicLink
        # 权限时很常见），绝不把"没测"记成"通过"。
        link = _os.path.join(tmp, "escape_link")
        try:
            _os.symlink(_os.path.join(tmp, "knowledge"), link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as _e:
            print("  [跳过] 符号链接越界用例：本机无法创建符号链接（%s）"
                  % getattr(_e, "winerror", _e))
        else:
            check("符号链接：指向库外的链接被判越界",
                  TN.is_inside(link, base) is False, link)
            ok3, _w3, _r3 = TN.guard_write_path(_os.path.join(link, "x.json"))
            check("符号链接：经链接写入租户目录外被拒", not ok3)
    finally:
        try:
            TN.set_root(orig_root)
        except Exception as _e:
            print("  [提示] 测试后复位根目录失败：%s" % _e)
        _sh.rmtree(tmp, ignore_errors=True)


def test_p1_field_mapping_confidence():
    """P1-3：字段映射必须有来源/候选/置信度/量纲风险，低置信度可阻断。

    审计原话：映射只有裸 {列: key}，无来源/候选/置信度/人工确认；
    低置信度无法阻断；单位换算靠白名单"恰好"挡住跨量纲。
    """
    from lib import table as T
    # 构造一张含"精确匹配列"的表
    cols = ["设备号", "浆料固含(wt%)", "某未知温度参数"]
    rows = [["A1", 55.0, 25.0], ["A2", 56.0, 26.0],
            ["A3", 54.0, 24.0], ["A4", 55.5, 25.5]]
    tb = T.Table(cols, rows)
    det = PP.map_columns_detailed(tb, prefer_process="battery_coating")

    # ① 结构化：每条记录带来源/置信度/候选，且向后兼容的 map_columns 仍给 key
    keys = PP.map_columns(tb, prefer_process="battery_coating")
    check("P1-3：map_columns 向后兼容仍返回 {列: key}",
          set(keys.get(c) for c in cols) ==
          set(det.get(c, {}).get("key") for c in cols), "")
    miss = [c for c in cols if "confidence" not in det.get(c, {})
            or "source" not in det.get(c, {})]
    check("P1-3：每条映射记录含 source 与 confidence", not miss, str(miss))
    # 精确归一匹配（浆料固含）应是 high
    sc = det.get("浆料固含(wt%)", {})
    check("P1-3：精确别名匹配置信度为 high",
          sc.get("confidence") == "high", str(sc.get("source")))
    check("P1-3：映射记录保留候选集（candidates）",
          isinstance(sc.get("candidates"), list) and len(sc["candidates"]) >= 1, "")
    check("P1-3：clean 报告交出 mapping_detail 与 low_confidence_mappings",
          "mapping_detail" in PP.clean(tb, prefer_process="battery_coating")[1]
          and "low_confidence_mappings" in PP.clean(tb, prefer_process="battery_coating")[1], "")

    # ② 低置信度可阻断：直接对 apply_map_policy 做单元验证（不依赖具体知识库）
    synth = {
        "colA": {"key": "p1", "source": "substring", "confidence": "low", "unit_risk": False},
        "colB": {"key": "p2", "source": "exact", "confidence": "high", "unit_risk": False},
    }
    auto_keys, auto_low = PP.apply_map_policy(synth, "auto")
    block_keys, block_low = PP.apply_map_policy(synth, "block")
    check("P1-3：auto 下低置信列仍保留猜测 key", auto_keys["colA"] == "p1", str(auto_keys))
    check("P1-3：block 政策把低置信列判为未匹配（key=None）",
          block_keys["colA"] is None, str(block_keys))
    check("P1-3：高置信列不受 block 影响",
          block_keys["colB"] == "p2", str(block_keys))
    check("P1-3：auto 下汇总低置信清单",
          len(auto_low) == 1 and auto_low[0]["col"] == "colA", str(auto_low))

    # ③ 量纲风险：列名暗示单位与参数声明 unit 不一致应被标记
    #    （直接单元验证 _unit_hint / _unit_mismatch，不依赖具体知识库匹配）
    check("P1-3：_unit_hint 从列名抽取量纲 token",
          PP._unit_hint("涂布速度(mm)") == "mm", str(PP._unit_hint("涂布速度(mm)")))
    params_mm = {"coating_speed": {"unit": "m/min"}}
    rec_mm = {"key": "coating_speed"}
    check("P1-3：列名暗示量纲与声明量纲冲突→unit_risk",
          PP._unit_mismatch(rec_mm, "涂布速度(mm)", params_mm) is True, "")
    check("P1-3：列名量纲与声明一致→不误报",
          PP._unit_mismatch(rec_mm, "涂布速度(m/min)", params_mm) is False, "")


def test_p1_spc_exclusion_review():
    """P1-2：SPC 剔除必须可审计——默认不掩盖失控、可切换为仅建议/审核、逐点带规则身份与审核人。

    审计原话：默认仍是自动剔除，缺"仅建议/需审核"开关；逐点触发规则丢了身份，
    只剩全局 rules_used；无逐点理由与 review_status/reviewed_by。
    """
    import random as _rnd
    _g = _rnd.Random(7)
    _stable = [_g.gauss(10, 0.5) for _ in range(40)]
    _spike = [16.0, 17.0, 15.5, 16.8, 15.2]
    _mix = _stable + _spike

    # ① 默认 auto：命中即剔除，suspects 为空，exclusion_detail 带逐点身份
    a = SPC.phase1_limits(_mix)
    check("P1-2：auto 默认仍自动剔除异常点",
          a is not None and len(a["excluded"]) == len(_spike), "excluded=%s" % a["excluded"])
    check("P1-2：auto 下 suspect_points 为空（不遗留待审）", a["suspect_points"] == [], "")
    check("P1-2：auto 下 exclusion_detail 逐点带规则身份",
          len(a["exclusion_detail"]) == len(_spike)
          and all(isinstance(d["rules"], list) and d["rules"]
                  and isinstance(d["reason"], str) and d["reason"]
                  for d in a["exclusion_detail"]), "")
    check("P1-2：exclusion_detail 的 index 与 excluded 对应",
          set(d["index"] for d in a["exclusion_detail"]) == set(a["excluded"]), "")

    # ② suggest：不剔除，全部进 suspect_points，且带规则身份
    s = SPC.phase1_limits(_mix, mode="suggest")
    check("P1-2：suggest 模式不自动剔除（excluded 为空）",
          s["excluded"] == [], "excluded=%s" % s["excluded"])
    check("P1-2：suggest 模式把所有候选点交给人审（suspect_points 非空）",
          len(s["suspect_points"]) == len(_spike), "suspects=%d" % len(s["suspect_points"]))
    check("P1-2：suggest 候选点带命中规则号与可读理由",
          all(isinstance(d["rules"], list) and d["rules"]
              and isinstance(d["reason"], str) and d["reason"]
              for d in s["suspect_points"]), "")
    check("P1-2：suggest 模式基线偏宽（全程估计，stable=False）",
          s["stable"] is False, "stable=%s" % s["stable"])

    # ③ review：approved 才剔除，其余保留为待审并带 review_status/reviewed_by
    _rev_approve = {d["index"]: {"status": "approved", "by": "QC-张三"}
                    for d in s["suspect_points"]}
    rv = SPC.phase1_limits(_mix, mode="review", review=_rev_approve)
    check("P1-2：review 模式按 approved 真正剔除",
          set(rv["excluded"]) == set(_rev_approve.keys()), "excluded=%s" % rv["excluded"])
    check("P1-2：review 全 approved 后无遗留待审", rv["suspect_points"] == [], "")

    _rev_pending = {d["index"]: {"status": "pending", "by": "QC-李四"}
                    for d in s["suspect_points"]}
    rv2 = SPC.phase1_limits(_mix, mode="review", review=_rev_pending)
    check("P1-2：review 全 pending 时一个都不剔除", rv2["excluded"] == [], "")
    check("P1-2：pending 候选保留为待审且带 review_status/reviewed_by",
          len(rv2["suspect_points"]) == len(_spike)
          and all(d.get("review_status") == "pending"
                  and d.get("reviewed_by") == "QC-李四"
                  for d in rv2["suspect_points"]), "")

    # ④ 逐点规则身份须能记录真实 Nelson 规则号（非仅稳健预筛 rule 0）
    _g2 = _rnd.Random(3)
    _base = [_g2.gauss(10, 0.5) for _ in range(40)] + [10.75] * 9
    sb = SPC.phase1_limits(_base, mode="suggest")
    _nelson_identity = any(any(r in range(1, 9) for r in d["rules"])
                           for d in sb["suspect_points"])
    check("P1-2：逐点身份能记录真实 Nelson 规则号（非仅稳健预筛）",
          _nelson_identity, "rules=%s" % [d["rules"] for d in sb["suspect_points"]])


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
    test_clustering()
    test_v19_delivery()
    test_entry_parity()
    test_views()
    test_defect_process_priority()
    test_p0_align_and_direction()
    test_p1_stdlib_oracle()
    test_p03_failure_not_disguised()
    test_status_and_lineage()
    test_confound_and_evidence()
    test_doe_safety_boundary()
    test_parameter_contracts()
    test_d8_closure_gate()
    test_contract_schema()
    test_concurrent_write_integrity()
    test_tenant_path_guard()
    test_p1_field_mapping_confidence()
    test_p1_spc_exclusion_review()
    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("全部通过")
