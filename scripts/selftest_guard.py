# -*- coding: utf-8 -*-
"""
v1.10 质量护栏回归测试（独立于 selftest.py，便于快速验证护栏本身）

用法：python scripts/selftest_guard.py
"""
import os
import sys

# 同 selftest.py：Windows 重定向输出时强制 UTF-8，避免中文被 cp1252 卡死。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import re
import json
import shutil
import random
import tempfile
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from lib import quality_guard as QG
from lib import preprocess as PP
from lib import table as T
from lib import hotswap as HS

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("  [%s] %s %s" % ("OK" if cond else "FAIL", name, detail))


print("A. 别名子串护栏")
check("Visc 不误配 isc", PP.match_param("Visc(mPa.s)") == "viscosity", str(PP.match_param("Visc(mPa.s)")))
check("电池列 不误配泛化别名「电池」", PP.match_param("电池列") is None, str(PP.match_param("电池列")))
check("Dryer2 不误配 Dryer 残水", PP.match_param("Dryer2(C)") == "oven_zone2_temp", str(PP.match_param("Dryer2(C)")))
check("不良率 仍能配到 EL 不良率（中文 ≥3 字）",
      PP.match_param("不良率(%)", prefer_process="pv_cell_el") == "el_defect_rate")

print("B. 列角色识别")
check("编号列为 id", QG.column_role("组件号", ["M0001"]) == "id")
check("时间戳为 time", QG.column_role("Date Time", []) == "time")
check("持续时长（搁置时间）为 measure", QG.column_role("搁置时间(h)", ["24.5", "30.1", "48.0"]) == "measure")
check("x(mm)/y(mm) 为 coord", QG.column_role("x(mm)", ["10"]) == "coord")
check("电池列 整数位置为 coord", QG.column_role("电池列", ["1", "5", "12"]) == "coord")
check("计数列 Mark 为 count", QG.column_role("Mark", ["1", "2", "0", "3"]) == "count")
check("连续量为 measure", QG.column_role("EdgeThk(um)", ["6.1", "7.2", "5.5", "6.9"]) == "measure")

print("C. 测试条件识别")
check("辐照度 为测试条件", QG.is_test_condition("辐照度(W/m2)"))
check("组件温度 为测试条件", QG.is_test_condition("组件温度(C)"))
check("Pmax 不是测试条件", not QG.is_test_condition("Pmax(W)"))

print("D. 证据门槛与工序护栏")
check("证据不足不算 strong", not QG.rc_strong({"candidates": [{"evidence_score": 0.12}]}))
check("证据充分算 strong", QG.rc_strong({"candidates": [{"evidence_score": 0.35}]}))
check("标记 insufficient 不算 strong",
      not QG.rc_strong({"evidence_insufficient": True, "candidates": [{"evidence_score": 0.9}]}))
p, n = QG.pick_process([])
check("无工序特征时不选工序", p is None and n)
p, n = QG.pick_process([("pv_cell_el", 0.5)])
check("弱证据时不选工序", p is None and n)
p, n = QG.pick_process([("battery_formation", 2.0), ("pv_cell_el", 1.0)])
check("证据清楚时选定工序", p == "battery_formation" and n is None)

print("E. 长表汇总")
rows = []
for c in range(3):
    for step in ["恒流充电", "恒压充电", "恒流放电"]:
        for k in range(2):
            cap = 50.0 if (step == "恒流放电" and k == 1) else 0.0
            rows.append(["C%d" % c, step, str(k), "%.2f" % cap])
tb = T.Table([], [])
tb.columns = ["电芯编号", "工步", "采样序号", "分容容量(Ah)"]
tb.rows = rows
out, note = QG.collapse_longform(tb)
check("长表按实体取末行（3 电芯 → 3 行）", len(out.rows) == 3, str(len(out.rows)))
check("汇总后容量取末工步值", all(float(r[3]) == 50.0 for r in out.rows))
check("给出长表说明", bool(note) and "长表" in note)

print("F. STC 归一化")
tb2 = T.Table([], [])
tb2.columns = ["组件号", "Pmax(W)", "辐照度(W/m2)", "组件温度(C)"]
# 构造：实测 P = Pstc * (G/1000) * (1 - 0.0035*(T-25))
tb2.rows = [["M1", "540.0", "900", "35"], ["M2", "540.0", "1000", "25"]]
cm = {"Pmax(W)": "pmax", "辐照度(W/m2)": "irradiance", "组件温度(C)": "module_temp", "组件号": "module_id"}
out2, cm2, note2, newt = QG.stc_normalize(tb2, cm, None)
check("识别出 STC 目标", newt == "Pmax_STC(W)")
vals = [float(r[out2.col_index("Pmax_STC(W)")]) for r in out2.rows]
expect_m1 = 540.0 * (1000 / 900.0) / (1 - 0.0035 * 10)
check("M1 归一值正确", abs(vals[0] - expect_m1) < 0.01, "%.3f vs %.3f" % (vals[0], expect_m1))
check("M2（已在 STC 条件）不变", abs(vals[1] - 540.0) < 0.01, "%.3f" % vals[1])
check("原始 Pmax 从映射中摘除", cm2.get("Pmax(W)") is None)

print("G. 目标选择：响应优先，不是「变异最大的旋钮」")
_tb = T.Table([], [])
_tb.columns = ["CoatWt(gsm)", "PumpRPM(rpm)", "Gap(um)"]
_tb.rows = []
for i in range(60):
    rpm = 1200.0 + (i - 30) * 1.1
    _tb.rows.append(["%.2f" % (164.0 + 0.021 * (rpm - 1200.0)), "%.1f" % rpm, "180.5"])
_cm = {"CoatWt(gsm)": "areal_density"}
_pick, _det = QG.pick_response_column(_tb, col_map=_cm)
check("涂布面密度优先于泵速当目标", _pick == "CoatWt(gsm)", str(_pick))
check("泵速被判为控制因子（得分低于响应）",
      _det["ranking"]["PumpRPM(rpm)"]["score"] < _det["ranking"]["CoatWt(gsm)"]["score"])

_tb2 = T.Table([], [])
_tb2.columns = ["少子寿命(μs)", "TTV(μm)", "位置"]
_tb2.rows = []
for i in range(60):
    pos = ["头", "中", "尾"][i % 3]
    _tb2.rows.append(["%.2f" % ({"头": 12.4, "中": 10.2, "尾": 5.1}[pos] + (i % 5) * 0.1),
                      "%.2f" % (12.0 + (i % 7) * 1.3), pos])
_pick2, _ = QG.pick_response_column(_tb2, col_map={"少子寿命(μs)": "minority_lifetime",
                                                   "TTV(μm)": "ttv"})
check("有结构的质量特性优先于噪声列", _pick2 == "少子寿命(μs)", str(_pick2))

print("H. 混杂检验：批次与时间的不可区分性")
_tb3 = T.Table([], [])
_tb3.columns = ["Lot", "Y"]
_tb3.rows = [["L%03d" % (i // 5), "%.2f" % (10 + i * 0.05)] for i in range(60)]
_rho = QG.order_confound(_tb3, "Lot")
check("顺序发放的批号与行序高度混杂", _rho is not None and abs(_rho) > 0.85, str(_rho))
_tb4 = T.Table([], [])
_tb4.columns = ["Group", "Y"]
import random as _rnd
_r = _rnd.Random(7)
_tb4.rows = [[_r.choice(["A", "B", "C"]), "%.2f" % _r.gauss(10, 1)] for _ in range(60)]
_rho2 = QG.order_confound(_tb4, "Group")
check("随机分组不与行序混杂", _rho2 is not None and abs(_rho2) < 0.5, str(_rho2))

print("I. 多重比较校正")
from lib import stats as S
check("BH 单调不减", all(a <= b + 1e-9 for a, b in
                         zip(S.bh_adjust([0.01, 0.02, 0.03, 0.9]),
                             S.bh_adjust([0.01, 0.02, 0.03, 0.9])[1:]) or [(True, True)]))
_q = S.bh_adjust([0.01, 0.02, 0.03, 0.04])
check("BH q 不小于 p", all(q >= p - 1e-9 for p, q in zip([0.01, 0.02, 0.03, 0.04], _q)))
check("BH 会收紧最小 p 的判定阈值", S.bh_adjust([0.001, 0.04])[0] > 0.001,
      str(S.bh_adjust([0.001, 0.04])))

print("J. 缺陷事件表与位置聚集")
from lib import events as EV
_tb5 = T.Table([], [])
_tb5.columns = ["组件编号", "EL缺陷类型", "电池列", "电池行"]
_rows = []
_r2 = _rnd.Random(11)
for m in range(60):
    for k in range(_r2.randint(0, 3)):
        col = 7 if _r2.random() < 0.7 else _r2.randint(1, 12)
        _rows.append(["M%03d" % m, "隐裂", str(col), str(_r2.randint(1, 6))])
_tb5.rows = _rows
_ok, _info = EV.looks_like_event_table(_tb5)
check("识别为缺陷事件表", _ok)
check("实体列是组件编号而不是缺陷类型", _info and _info.get("entity") == "组件编号", str(_info))
_agg, _ei = EV.aggregate_events(_tb5)
check("按组件汇总（伪重复消除）", _agg.shape()[0] < len(_rows), "%d -> %d" % (len(_rows), _agg.shape()[0]))
_cl = EV.factor_clustering(_tb5, "电池列")
check("找到第 7 列聚集", _cl and str(_cl["hot_level"]) == "7", str(_cl.get("hot_level") if _cl else None))
check("聚集结论标注了多重比较校正", _cl and "BH" in (EV.cluster_report(_cl) or ""))
_uni = T.Table([], [])
_uni.columns = ["组件编号", "EL缺陷类型", "电池列"]
_uni.rows = [["M%03d" % i, "断栅", str(_r2.randint(1, 12))] for i in range(60)]
_cl2 = EV.factor_clustering(_uni, "电池列")
check("均匀分布不误报热点", _cl2 is None or not _cl2.get("significant"))

print("K. 负例判定")
_nv = QG.negative_verdict([{"param": "Voc(V)", "r": 0.45}, {"param": "Isc(A)", "r": 0.3}],
                          exclude_cols=["Pmax(W)"], response_cols={"Voc(V)", "Isc(A)"})
check("结果类参数不算可行动根因 → 判负例", _nv and _nv["verdict"] == "no_actionable_cause")
_nv2 = QG.negative_verdict([{"param": "烘箱二区温度", "r": 0.55}], response_cols=set())
check("存在强关联时不判负例", _nv2 is None)
_nv3 = QG.negative_verdict([{"param": "组件温度(℃)", "r": 0.98}, {"param": "Voc(V)", "r": 0.44}],
                           response_cols={"Voc(V)"})
check("候选全是测试条件/结果指标时文案不自相矛盾",
      _nv3 and "共检验 0 项" not in _nv3["text"] and _nv3.get("n_process_tests") == 0,
      (_nv3 or {}).get("text", "")[:80])

print("L. 长表：非适用工步的哨兵 0 不当观测值")
_tb6 = T.Table([], [])
_tb6.columns = ["电芯编号", "工步", "电压(V)", "电流(A)"]
_rows6 = []
for c in range(3):
    _rows6.append(["C%d" % c, "恒流充电", "3.75", "-26.0"])
    _rows6.append(["C%d" % c, "恒压充电", "3.79", "-8.5"])
    _rows6.append(["C%d" % c, "静置", "0", "0"])
_tb6.rows = _rows6
_out6, _note6 = QG.collapse_longform(_tb6)
_vi = _out6.col_index("电压(V)")
check("静置段的 0 不被当成最终电压",
      all(float(r[_vi]) > 3.0 for r in _out6.rows),
      str([r[_vi] for r in _out6.rows]))
check("长表说明写明哨兵 0 按缺失处理", bool(_note6) and "缺失" in _note6)

print("M. 入口一致性：脚本模式（CLI）不能比包模式少能力")
# 教训：新增模块若只用相对导入，python lib/pipeline.py 这条官方 CLI 入口会
# 静默降级（_HAS_EVENTS=False），而评测脚本走的是 import lib.pipeline 的包模式，
# 所以两边结果不一致。这里用子进程跑真实 CLI，强制断言能力齐平。
import subprocess as _SP
import tempfile as _TF
_CLI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "lib", "pipeline.py")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmpd = _TF.mkdtemp(prefix="fde_cli_")
_csv = os.path.join(_tmpd, "el_events.csv")
with open(_csv, "w", encoding="utf-8") as _f:
    _f.write("组件编号,EL缺陷类型,电池列,电池行\n")
    _r3 = random.Random(4242)
    for i in range(24):
        for j in range(_r3.randint(0, 4)):
            _f.write("M%03d,%s,%d,%d\n" % (
                i, _r3.choice(["隐裂", "断栅", "黑斑"]),
                7 if (i < 8 and j == 0) else _r3.randint(1, 12),
                _r3.randint(1, 6)))
_ojson = os.path.join(_tmpd, "r.json")
_pr = _SP.run([sys.executable, _CLI, "--input", _csv, "--outdir",
               os.path.join(_tmpd, "out"), "--json", _ojson],
              cwd=_ROOT, capture_output=True, encoding="utf-8", errors="replace")
_cli_ok = os.path.exists(_ojson)
_cli_res = {}
if _cli_ok:
    try:
        with open(_ojson, encoding="utf-8") as _f:
            _cli_res = json.load(_f)
    except Exception:
        _cli_ok = False
check("CLI（脚本模式）能跑通并出 JSON", _cli_ok, (_pr.stderr or "")[-300:])
check("CLI 模式下缺陷事件表同样被识别（不静默降级）",
      bool(_cli_res.get("event_table")),
      "process=%s target=%s" % (_cli_res.get("process"), _cli_res.get("target")))
check("CLI 模式目标列不是坐标/面积列",
      _cli_res.get("target") not in ("面积占比(%)", "电池列", "电池行"),
      str(_cli_res.get("target")))
# 同一个坑会再踩一次：8D 引擎也是新模块 + try/except 兜底。
# CLI 下若 eightd 静默降级，报告会退回 D1–D4 而没有报错 —— 必须断言它真的渲染了。
check("CLI 模式下 8D 引擎同样生效且不静默降级",
      bool((_cli_res.get("eightd") or {}).get("d8")),
      "eightd=%s" % (list((_cli_res.get("eightd") or {}).keys())[:6]))
try:
    shutil.rmtree(_tmpd)
except Exception:
    pass

print("N. 性能护栏：不能出现「同一份数据被反复解析」")
# 教训：企业端一条涂布线一个班次的导出就是几万行。此前 Table.numeric_columns() /
# numeric_column() 每次被调用都重新扫描整列，2 万行 × 16 列的数据里 to_number 被调用了
# 1828 万次（每单元格平均 57 次），真正的统计计算只占一小部分。
# 这里用「调用次数」而不是墙钟时间做断言，结果不依赖机器性能。
_r4 = random.Random(99)
_tbN = T.Table([], [])
_tbN.columns = ["批次号", "设备编号"] + ["参数%02d" % i for i in range(13)]
_tbN.rows = [["B%03d" % _r4.randint(1, 50), "EQ-%02d" % _r4.randint(1, 6)] +
             [str(round(_r4.gauss(100, 5), 3)) for _ in range(13)]
             for _ in range(3000)]
_calls = {"n": 0}
_orig_tonum = T.to_number


def _counting_to_number(v):
    _calls["n"] += 1
    return _orig_tonum(v)


T.to_number = _counting_to_number     # table.py 内部按模块全局名解析，可被替换
_tblguard = T.Table(_tbN.columns, [list(r) for r in _tbN.rows])
for _ in range(20):                      # 模拟流水线里各模块反复取同一批列
    _tblguard.numeric_columns()
    _tblguard.text_columns()
    for _c in _tbN.columns[:6]:
        _tblguard.numeric_column(_c)
T.to_number = _orig_tonum
_ncell = len(_tbN.rows) * len(_tbN.columns)
_upper = 4 * _ncell
check("重复取列会走缓存（to_number 调用次数受控）",
      _calls["n"] <= _upper, "调用 %d 次 / 上限 %d（单元格数 %d）"
      % (_calls["n"], _upper, _ncell))
_tblguard2 = T.Table(_tbN.columns, [list(r) for r in _tbN.rows])
_nc1 = _tblguard2.numeric_columns()
_tblguard2.add_column("新列", [1] * len(_tblguard2.rows))
_nc2 = _tblguard2.numeric_columns()
check("改表后缓存自动失效（新增数值列被看见）", "新列" in _nc2 and "新列" not in _nc1,
      "%s -> %s" % (len(_nc1), len(_nc2)))

# ---------------------------------------------------------------- O 组：8D 引擎
print("O. 8D 引擎（v1.13）")
try:
    from lib import eightd as E8
    _HAS8 = True
except Exception as _e:
    E8 = None
    _HAS8 = False
check("eightd 模块可导入", _HAS8, str(_e) if not _HAS8 else "")

if _HAS8:
    # O1 不良件数口径：件数 ≠ 缺陷计数之和
    _res1 = {"shape": (123, 5),
             "defects": [{"defect": "隐裂", "column": "缺陷_隐裂", "total": 269,
                          "n_records": 123, "n_positive": 116, "is_count": True}],
             "target": "缺陷_隐裂"}
    _c1 = E8._count_defects(_res1)
    check("不良件数取 n_positive 而非 total（269→116）", _c1["defects"] == 116, str(_c1["defects"]))
    check("不良率不超 100%（94.3% 而非 218%）",
          _c1["rate"] is not None and 90 < _c1["rate"] < 100, str(_c1["rate"]))
    # 口径算错时不写假数字
    _res2 = {"shape": (10, 3),
             "defects": [{"defect": "x", "column": "x", "total": 50,
                          "n_records": 10, "n_positive": 50, "is_count": True}],
             "target": "x"}
    check("不良率超 100% 时留空而不写假数", E8._count_defects(_res2)["rate"] is None)

    # O2 录入卡 round-trip
    _tmpd = tempfile.mkdtemp()
    _cp = os.path.join(_tmpd, "card.md")
    with open(_cp, "w", encoding="utf-8") as _f:
        _f.write("# 注释\nwhat: 隐裂\nhow_many_defects: 30\nsignoff_d5: yes\nsignoff_d6: no\n")
    _card = E8.parse_intake_card(_cp)
    check("录入卡可解析", _card.get("what") == "隐裂", str(_card))
    check("数字被解析为数值", E8._num(_card.get("how_many_defects")) == 30.0)
    check("yes/no 被解析为布尔", _card.get("signoff_d5") is True and _card.get("signoff_d6") is False)
    check("注释行不进入解析结果", "what" in _card and len(_card) == 4, str(len(_card)))

    # O3 交叉校验必须能报冲突
    _cf = E8.cross_check(_card, _res1)
    check("填报 30 与核算 116 报出冲突",
          any(c["field"] == "how_many_defects" for c in _cf), str([c["field"] for c in _cf]))
    check("完全一致时不报冲突",
          not E8.cross_check({"how_many_defects": 116}, _res1))

    # O4 确审闸门
    _st0 = E8.signoff_state({})
    _st4 = E8.signoff_state({"signoff_d5": True, "signoff_d6": True,
                             "signoff_d7": True, "signoff_d8": True})
    check("未确审时 all_signed=False", _st0["all_signed"] is False)
    check("四项全确审后 all_signed=True", _st4["all_signed"] is True)
    check("缺一项即不算确审",
          E8.signoff_state({"signoff_d5": True, "signoff_d6": True,
                            "signoff_d7": True})["all_signed"] is False)

    # O5 D5 不得含参数设定值（这是三条"不做"里第 1 条的可执行断言）
    _lead = {"param_name": "烘箱二区温度", "score": 0.6, "param_key": "oven_zone2_temp"}
    _d5 = E8.derive_d5({"spc": []}, _lead)
    _blob = " ".join(r["action"] for r in _d5["rows"])
    # 形如「调到 92℃」「设定为 95」这类具体数值指令
    _bad = re.search(r"(调到|设定为|改为|调整至)\s*\d", _blob)
    check("D5 不出现具体参数设定值", _bad is None, _bad.group(0) if _bad else "")
    check("D5 根因未确认时先要求验证",
          E8.derive_d5({}, None)["rows"][0]["category"] == "前置条件")

    # O6 位置类根因能被归类，而不是掉进 unknown
    check("「电池列 = 7」归为 position",
          E8.classify_root({"param_name": "电池列 = 7"}, {}) == "position",
          E8.classify_root({"param_name": "电池列 = 7"}, {}))
    check("设备类归为 equipment",
          E8.classify_root({"param_name": "设备号 = CT-05"}, {}) == "equipment")

    # O7 D6 验证方案
    _d6 = E8.derive_d6({"spc": [{"column": "面密度", "sigma_within": 2.0}],
                        "target": "面密度", "shape": (100, 4), "defects": []})
    check("D6 给出 MDD 表", len(_d6["mdd_table"]) == 3)
    check("MDD 随样本量增大而变小",
          _d6["mdd_table"][0]["mdd"] > _d6["mdd_table"][-1]["mdd"])
    check("D6 明确声明是方案不是结果", "验证方案" in _d6["note"])

    # O8 D8 结案清单 +  blockers
    _d8 = E8.derive_d8({"confidence": {"level": "低"}}, None)
    # 不再断言死数量：每加一条结案门槛就得改一次数字，等于把能力变更的责任
    # 推给测试——D7 加 PFMEA 门槛时就是这样漏掉的（6 变 7，断言还写着 6）。
    # 改判「必备项是否都在」，缺哪个报哪个。
    _gates_txt = " ".join(str(g.get("item", "")) for g in (_d8["gates"] or []))
    _need = ["D4", "D5", "D6", "D7", "PFMEA", "横向展开", "客户"]
    _miss = [k for k in _need if k not in _gates_txt]
    check("D8 结案条件覆盖全部必备项", not _miss, "、".join(_miss))
    check("低置信度会阻塞结案", len(_d8["blockers"]) >= 1)
    _d8n = E8.derive_d8({"negative_verdict": {"verdict": "no_actionable_cause"}}, None)
    check("负例结论会阻塞结案", len(_d8n["blockers"]) >= 1)

    shutil.rmtree(_tmpd, ignore_errors=True)

# --------------------------------------------------------------- P. 并发读写
# 这条守护的是一个很容易被误判的现象。Windows 上另一进程做 tmp+os.replace 时，
# 极短窗口内读打开会被拒（Errno 13）。实测约 1.2%，而**内容从不撕裂**。
# 真正的坑不在 os.replace，在于 kbstore 原来用裸 except 兜住它、
# 于是 PermissionError 被当成"文件坏了"静默返回空知识库——
# 报告照出、结论全错、还没有报错。所以守护的是"读必须拿到真实内容"。
if True:
    import threading as _TH

    _pdir = tempfile.mkdtemp()
    _pfile = os.path.join(_pdir, "specs.json")
    with open(_pfile, "w", encoding="utf-8") as _f:
        _f.write(json.dumps({"meta": {"revision": 1}}))
    _pstop = {"v": False}
    _ps = {"reads": 0, "err": 0, "torn": 0, "writes": 0, "wfail": 0}

    def _p_reader():
        while not _pstop["v"]:
            try:
                _raw = HS.read_text_retry(_pfile)
                if not _raw.startswith("{") or not _raw.rstrip().endswith("}"):
                    _ps["torn"] += 1
                else:
                    _ps["reads"] += 1
            except Exception:
                _ps["err"] += 1

    def _p_writer():
        _n = 0
        while not _pstop["v"]:
            try:
                _dd = json.loads(HS.read_text_retry(_pfile))
                _dd["meta"]["revision"] = 2000 + _n
                HS.atomic_write_json(_pfile, _dd)
                _ps["writes"] += 1
            except Exception:
                _ps["wfail"] += 1
            _n += 1
            time.sleep(0.001)

    _pr = _TH.Thread(target=_p_reader)
    _pw = _TH.Thread(target=_p_writer)
    _pr.start(); _pw.start()
    time.sleep(1.2)
    _pstop["v"] = True
    _pr.join(); _pw.join()

    check("并发读期间原子写不失败", _ps["wfail"] == 0 and _ps["writes"] > 0,
          "ok=%d fail=%d" % (_ps["writes"], _ps["wfail"]))
    check("并发写不会撕裂内容（读到的一律是完整 JSON）", _ps["torn"] == 0,
          "torn=%d / reads=%d" % (_ps["torn"], _ps["reads"]))
    check("并发读撞 replace 窗口时不会抛错（原会静默退化为空知识库）",
          _ps["err"] == 0, "err=%d / reads=%d" % (_ps["err"], _ps["reads"]))
    check("确实发生了足够多次读（保证上一条不是因为根本没读到）",
          _ps["reads"] > 200, "reads=%d" % _ps["reads"])

    shutil.rmtree(_pdir, ignore_errors=True)


# --------------------------------------------------------------- Q 组
print("\nQ. 流程代码化：编排器与锁健壮性")

_RUN = os.path.join(ROOT, "scripts", "fde_run.py")
check("编排器脚本存在（多步流程必须有唯一入口）", os.path.exists(_RUN), _RUN)

_rc = subprocess.run(
    [sys.executable, _RUN, "audit",
     "--report", os.path.join(ROOT, "output", "_guard_audit.json")],
    cwd=ROOT, capture_output=True, text=True,
    encoding="utf-8", errors="replace", timeout=300)
check("编排器 audit 全通过（约定落在代码里，不是只写在文档里）",
      _rc.returncode == 0, "退出码=%s" % _rc.returncode)

# PARTIAL 语义：跳过就是跳过，不许报成通过
sys.path.insert(0, os.path.join(ROOT, "scripts"))
try:
    import importlib
    _fr = importlib.import_module("fde_run")
    _st, _code, _ = _fr._status_of([{"status": "PASS"}, {"status": "SKIP"}])
    check("有步骤被跳过时判定 PARTIAL（退出码 3）而非 PASS",
          _st == "PARTIAL" and _code == 3, "%s/%s" % (_st, _code))
    _st2, _code2, _ = _fr._status_of([{"status": "PASS"}, {"status": "FAIL"}])
    check("有步骤失败时判定 FAIL（退出码 1）",
          _st2 == "FAIL" and _code2 == 1, "%s/%s" % (_st2, _code2))
except Exception as _e:
    check("编排器可导入且状态判定可用", False, "%s: %s" % (type(_e).__name__, _e))

# 锁：死进程残留锁必须立即破除。产线上 Ctrl+C 后立刻重跑是常态，
# 让人白等 15 秒再报错，这种"莫名其妙卡一下"最伤信任。
from lib import tenancy as _TN
_lk = tempfile.mkdtemp(prefix="guardlk-")
_TN.set_root(_lk)
_ld = os.path.join(_lk, ".lock", "kb")
os.makedirs(_ld, exist_ok=True)
_dead = subprocess.run([sys.executable, "-c", "import os;print(os.getpid())"],
                       capture_output=True, text=True).stdout.strip()
with open(os.path.join(_ld, "owner"), "w", encoding="utf-8") as _f:
    _f.write(_dead)
_t0 = time.time()
with _TN.lock("kb", timeout=15.0):
    pass
_cost = time.time() - _t0
check("死进程残留锁立即破除（不得让人白等 15 秒）", _cost < 3.0, "%.2fs" % _cost)
check("锁实现了 PID 存活探测", hasattr(_TN, "_pid_alive"))
check("锁实现了心跳（防长任务被 stale 规则误抢）", hasattr(_TN, "_Heartbeat"))
shutil.rmtree(_lk, ignore_errors=True)

print("")
fails = [r for r in RESULTS if not r[1]]
print("通过 %d 项，失败 %d 项" % (len(RESULTS) - len(fails), len(fails)))
sys.exit(1 if fails else 0)
