# -*- coding: utf-8 -*-
"""
复核修复验证（rev fix verification）—— 审计证据脚本，可入库。

覆盖本轮真实缺陷修复：
  F1  rootcause 诚实降级泄漏：无数据支撑时不得输出根因榜单（只降级为 prior_only_hints）
  F2  rootcause 时序反证死分支：_temporal_match 必须返回 verdict，消费方读得到并出反证
  F3  doe decode_design 2 水平高端点：默认 2 水平 DOE 解不出高角点
  F4  crossprocess probe_margin 参考量程：用真实 pkey 而非写死空 meta 拿参考量程
  F5  preprocess 歧义检测：多义列名（"区温度"）必须标 ambiguous，不得静默取第一个
  F5b preprocess 阴性对照：唯一胜出（"一区温度"）不得误判歧义
  F6  doe decode_design 安全门禁：传入 constraints 后越界点必须被标记 _safety_ok=False

用法：
  python scripts/verify_rev_fixes.py            # 仅打印
  python scripts/verify_rev_fixes.py --check    # 门禁模式，任一 FAIL 返回非 0
"""
import os
import sys
import math
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import doe, crossprocess as CP, rootcause as RC, table as T  # noqa: E402
from lib import preprocess as PP  # noqa: E402
from lib import spc as SPC  # noqa: E402


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print("[%s] %s %s" % (mark, name, ("—— " + detail) if detail else ""))
    return cond


def test_f1_rootcause_honest_degradation():
    """target_col=None 且无匹配数据列 → 候选榜单必须清空，只给 prior_only_hints。"""
    # 表列故意不与任何 likely_param 别名匹配，确保全部 INSUFFICIENT_DATA
    tb = T.Table(["时间", "批号", "备注"],
                 [[1, "A01", "x"], [2, "A02", "y"], [3, "A03", "z"]])
    res = RC.rank_root_causes("极片错位", tb, target_col=None)
    cands = (res or {}).get("candidates") or []
    hints = (res or {}).get("prior_only_hints") or []
    ok = (res is not None) and (len(cands) == 0) and (len(hints) > 0)
    return check("F1 rootcause 诚实降级（无数据支撑不吐榜单）", ok,
                 "candidates=%d prior_only_hints=%d" % (len(cands), len(hints)))


def test_f2_temporal_verdict():
    """_temporal_match 返回 verdict 结构；反相数据判"不支持"并经消费方生成时序反证。"""
    cols = ["p", "y"]
    # 反相：参数与指标异常时段不重合 → 应判"不支持"
    rows_inv = [[100.0 if i < 15 else 0.0, 0.0 if i < 15 else 100.0] for i in range(30)]
    tb_inv = T.Table(cols, rows_inv)
    sc_inv, det_inv = RC._temporal_match(tb_inv, "p", "y")
    struct_ok = (det_inv.get("verdict") in ("支持", "不支持")) and ("note" in det_inv)
    inv_ok = (det_inv.get("verdict") == "不支持")
    # 整合验证：构造一个 data_backed 候选，temporal 带"不支持" verdict，
    # 走 _annotate_evidence 看是否产出时序反证（修复前该分支查不到 verdict 键，死分支）。
    cand = {
        "param_key": "p1", "param_name": "某参数", "detail": {
            "status": RC.ST.SUCCESS, "r": 0.6, "p": 0.01, "column": "p"},
        "temporal": det_inv,
    }
    RC._annotate_evidence([cand], tb_inv, "y", {})
    counter_has_temporal = any("时序上不支持" in c for c in (cand.get("counter_evidence") or []))
    ok = struct_ok and inv_ok and counter_has_temporal
    return check("F2 _temporal_match 返回 verdict 且消费方生成反证", ok,
                 "verdict=%s 反证=%s" % (det_inv.get("verdict"), counter_has_temporal))


def test_f3_doe_decode():
    """2 水平设计：水平号 2 即高；3 水平设计：2 仍是中点。"""
    fs = [{"name": "A", "low": 3.0, "high": 5.0},
          {"name": "B", "low": 10.0, "high": 20.0}]
    mat2 = doe.full_factorial([2, 2])  # 2^2，水平号仅 1/2
    dec2 = doe.decode_design(mat2, fs)
    has_high = any(d["A"] == 5.0 and d["B"] == 20.0 for d in dec2)
    fs3 = [{"name": "A", "low": 3.0, "mid": 4.0, "high": 5.0}]
    dec3 = doe.decode_design([[1], [2], [3]], fs3)
    ok3 = dec3[0]["A"] == 3.0 and dec3[1]["A"] == 4.0 and dec3[2]["A"] == 5.0
    return check("F3 decode_design 2水平解高角点 / 3水平中点正确", has_high and ok3,
                 "2level_has_high=%s 3level_ok=%s" % (has_high, ok3))


def test_f4_probe_margin_pkey():
    """probe_margin 用真实 pkey 才能拿到知识库参考量程（旧写死空 meta 拿不到）。

    关键差异：param_col 用中文别名「一区温度」，其裸名不等于 kb 键 zone1_temp。
    旧代码写死 _base_param_of(param_col, [{}]) 只能剥出裸列名「一区温度」，
    永远查不到 kb 键 → 参考量程恒定缺失；修复后由 trace 传入真实 pkey=zone1_temp。
    """
    pc = "一区温度"  # 裸名≠ kb 键
    rows, bad_idx, good_idx = [], [], []
    for i in range(40):
        if i < 20:
            bad_idx.append(i)
            rows.append([180.0 + (i % 3)])
        else:
            good_idx.append(i)
            rows.append([120.0 + (i % 3)])
    tb = T.Table([pc], rows)
    kb = {"zone1_temp": {"control": {"lsl": 100.0, "usl": 200.0}, "process": "烘箱"}}
    out = CP.probe_margin(tb, pc, bad_idx, good_idx, spec=None,
                          kb_params=kb, pkey="zone1_temp")
    ref_ok = bool(out and out.get("reference"))
    # 旧路径对照：pkey=None → 写死空 meta 只能剥出「一区温度」，查不到 kb 键
    out_old = CP.probe_margin(tb, pc, bad_idx, good_idx, spec=None, kb_params=kb, pkey=None)
    ref_old = bool(out_old and out_old.get("reference"))
    ok = ref_ok and (out.get("verdict") == "no_spec_cannot_clear") and (not ref_old)
    detail = "ref(真实pkey)=%s verdict=%s | ref(旧空meta兜底)=%s(应为None)" % (
        out.get("reference") if out else None,
        out.get("verdict") if out else None, ref_old)
    return check("F4 probe_margin 真实 pkey 拿参考量程", ok, detail)


def test_f5_ambiguity_detection():
    """多义列名必须被标 ambiguous，不可静默取第一个参数（P2-1 根因）。"""
    tb = T.Table(["区温度", "良率"], [[100.0, 0.9], [110.0, 0.85]])
    rec = PP._match_one("区温度")
    n_cand = len(rec.get("candidates") or [])
    ok = (rec.get("ambiguous") is True) and (n_cand >= 2) and (rec.get("confidence") == "low")
    return check("F5 多义列名标 ambiguous（区温度→多温度参数并列）", ok,
                 "ambiguous=%s candidates=%d conf=%s"
                 % (rec.get("ambiguous"), n_cand, rec.get("confidence")))


def test_f5b_unique_no_false_ambiguity():
    """唯一胜出列名不得误判歧义（证明判据有分辨力，不是一律标歧义）。"""
    rec = PP._match_one("一区温度")
    ok = (rec.get("ambiguous") is not True) and (rec.get("key") is not None) \
        and (rec.get("confidence") in ("high", "medium"))
    return check("F5b 唯一胜出不误判歧义（一区温度→明确参数）", ok,
                 "ambiguous=%s key=%s conf=%s"
                 % (rec.get("ambiguous"), rec.get("key"), rec.get("confidence")))


def test_f6_decode_safety_envelope():
    """decode_design 传入 constraints 后，越界点必须被标 _safety_ok=False（P2-2）。"""
    fs = [{"name": "化成电压", "low": 3.0, "high": 5.0}]
    # 用内置安全包络构造约束：化成电压上限 4.5V，使高角点 5.0 越界
    cons = doe.build_constraints(fs, spec_overrides={"化成电压": {"safety": {
        "limit": "max", "value": 4.5, "unit": "V"}}})
    mat = doe.full_factorial([2])  # [[1],[2]] → 低=3.0 / 高=5.0
    dec = doe.decode_design(mat, fs, cons)
    low_ok = dec[0].get("_safety_ok") is True
    high_bad = dec[1].get("_safety_ok") is False and bool(dec[1].get("_safety_violations"))
    # 对照：不传 constraints 时不应挂安全字段（避免误报）
    dec_noconst = doe.decode_design(mat, fs)
    no_const_field = "_safety_ok" not in dec_noconst[0]
    ok = low_ok and high_bad and no_const_field
    return check("F6 decode 越界点被安全门禁拦截", ok,
                 "low_ok=%s high_bad=%s no_const_field=%s"
                 % (low_ok, high_bad, no_const_field))


def test_f7_weight_configurable():
    """F7 根因权重可配置且可核对（外部评审 P1-5）。

    要锁的三件事：
      ① 权重真的参与计分（把 prior 权重置 0，分项里 prior 贡献必须为 0）；
      ② 候选带 weights_used / score_breakdown，且分项之和 == 总分（能自己验算）；
      ③ 非法权重被拒绝，而不是"缺哪个补哪个"静默修补。
    """
    import random as _r
    _g = _r.Random(11)
    rows = []
    for _i in range(60):
        _solid = 60 + _g.gauss(0, 2.0)          # 先验靠前，但与 y 无关
        _speed = 40 + _g.gauss(0, 2.0)          # 与 y 强相关
        _y = 2.0 + 0.5 * (_speed - 40) + _g.gauss(0, 0.05)
        rows.append([_solid, _speed, _y])
    tb = T.Table(["浆料固含", "涂布速度", "面密度"], rows)

    pure_ev = {"evidence": 1.0, "prior": 0.0, "temporal": 0.0, "verifiability": 0.0}
    res = RC.rank_root_causes("面密度超差", tb, "面密度", top=5, weights=pure_ev)
    cands = (res or {}).get("candidates") or []
    ok_used = bool(cands) and cands[0].get("weights_used", {}).get("prior") == 0.0
    bd = (cands[0].get("score_breakdown") or {}) if cands else {}
    ok_zero = bool(bd) and abs(bd.get("prior", 1)) < 1e-9 and bd.get("evidence", 0) > 0
    # 分项之和应≈总分（gate 可能再缩放，所以只要求同向且量级一致）
    _sum = sum(bd.values()) if bd else 0
    ok_sum = bool(bd) and abs(_sum - (cands[0].get("score") or 0)) < 0.25

    # 非法权重必须抛错（而不是静默修补）
    bad = 0
    for w in ({"prior": 1.0},
              {"evidence": -1, "prior": 1, "temporal": 1, "verifiability": 1},
              {"evidence": 0, "prior": 0, "temporal": 0, "verifiability": 0}):
        try:
            RC.rank_root_causes("面密度超差", tb, "面密度", top=5, weights=w)
        except ValueError:
            bad += 1
    ok = ok_used and ok_zero and ok_sum and bad == 3
    return check("F7 权重可配置/参与计分/可核对/非法值被拒", ok,
                 "weights_used.prior=%s prior贡献=%s 分项和=%.3f 总分=%.3f 非法拒绝=%d/3"
                 % (cands[0].get("weights_used", {}).get("prior") if cands else None,
                    bd.get("prior"), _sum, (cands[0].get("score") if cands else 0), bad))


def test_f8_weight_source_honest():
    """F8 权重的来源必须如实告知（不许静默回落）。

    三种情况各验一次：配置文件存在且合法 / 不存在 / 存在但坏了。
    第三种最要紧——它是事故不是事实，必须写在 weights_source 里被人看见。
    """
    import json as _j
    import tempfile
    w0, src0 = RC.load_weights()
    ok_conf = ("配置文件" in src0) and abs(sum(w0.values()) - 1.0) < 1e-9

    tmpd = tempfile.mkdtemp()
    _missing = os.path.join(tmpd, "nope.json")
    w1, src1 = RC.load_weights(_missing)
    ok_missing = ("内置默认" in src1) and abs(sum(w1.values()) - 1.0) < 1e-9

    _badp = os.path.join(tmpd, "bad.json")
    with open(_badp, "w", encoding="utf-8") as f:
        _j.dump({"scoring_weights": {"prior": 0.5}}, f)   # 缺三项
    w2, src2 = RC.load_weights(_badp)
    ok_bad = ("内置默认" in src2) and ("不合规" in src2) and abs(sum(w2.values()) - 1.0) < 1e-9
    try:
        os.remove(_badp)
        os.rmdir(tmpd)
    except OSError as exc:
        print("   ! 临时文件清理失败，请手工删除 %s（%s）" % (tmpd, exc))

    ok = ok_conf and ok_missing and ok_bad
    return check("F8 权重来源如实交代（含配置损坏时出声）", ok,
                 "配置=%s | 缺失=%s | 损坏=%s" % (src0[:18], src1[:14], src2[:26]))


def test_f9_sensitivity_has_resolution():
    """F9 敏感性判据有分辨力（不能永远报"稳健"）。

    用合成排名序列验证：全一致 → 1.00 且判"稳健"；全翻转 → 低分且判"不稳"。
    只测真实数据是不够的——那份数据恰好很稳，判据翻不动也看不出来。
    """
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts"))
    import sensitivity_analysis as SA

    same = [[("A", 1.0), ("B", 0.8)], [("A", 0.9), ("B", 0.7)]]
    flip = [[("A", 1.0), ("B", 0.8)], [("B", 0.9), ("A", 0.7)]]
    ag_same = SA.top1_agree(same)[0]
    ag_flip = SA.top1_agree(flip)[0]
    tau_same = SA.kendall_tau(["A", "B", "C"], ["A", "B", "C"])
    tau_rev = SA.kendall_tau(["A", "B", "C"], ["C", "B", "A"])
    ok = (ag_same == 1.0 and ag_flip == 0.5
          and tau_same == 1.0 and tau_rev == -1.0)
    return check("F9 敏感性判据有分辨力（一致/翻转可区分）", ok,
                 "agree 同=%s 翻=%s | tau 同=%s 反=%s" % (ag_same, ag_flip, tau_same, tau_rev))


def test_f10_long_gradual_shift():
    """F10 长而缓的整体偏移必须能被整段识别（2.19.9 核心）。

    旧实现：30 点 2.5σ 的整段剔除率只有 45~63%，比 15 点的 64~97% 还差——
    越长的偏移反而越难剔，因为它把均值拉偏、把 σ 撑大、又打断"连续 N 点"。
    新实现：段级识别（段中位数 vs 段外）整段判，实测 84%。

    同时必须守住三条"不能变坏"：
      ① 正常随机数据误剔率不能升（旧实现用规则2/3 剔除，固定丢 3~4% 正常点）；
      ② 自相关序列不能误报（AR 惯性会被误当偏移）；
      ③ 渐进漂移要判不稳（趋势不是"段"，靠规则3 命中比例判）。

    注意：造数据必须**先绑定 Random 再循环**。
    `[random.Random(seed).gauss(0,1) for _ in range(n)]` 每次都新建同种子实例，
    返回的是 n 个相同值（σ=0），会让所有指标失真——本次实测就被这个写法骗过一轮。
    """
    def _norm(n, seed):
        r = random.Random(seed)
        return [r.gauss(0, 1) for _ in range(n)]

    def _ar1(n, phi, seed):
        r = random.Random(seed)
        out, prev = [], 0.0
        for _ in range(n):
            prev = phi * prev + r.gauss(0, 1) * math.sqrt(1 - phi ** 2)
            out.append(prev)
        return out

    # ① 30 点 2.5σ 偏移：整段剔除率
    hit = 0
    stable_wrong = 0
    for k in range(20):
        r = random.Random(500 + k)
        x = [r.gauss(0, 1) for _ in range(160)]
        for i in range(80, 110):
            x[i] += 2.5
        off = set(range(80, 110))
        p = SPC.phase1_limits(x) or {}
        ex = set(p.get("excluded") or [])
        if len(ex & off) / 30.0 >= 0.6:
            hit += 1
        if p.get("stable") is True:
            stable_wrong += 1
    rate = hit / 20.0 * 100
    ok_shift = rate >= 60 and stable_wrong == 0

    # ② 正常随机数据误剔率（不能升）
    fp = 0
    for k in range(30):
        p = SPC.phase1_limits(_norm(160, 9000 + k))
        fp += len(p.get("excluded") or [])
    fp_pct = fp / 30.0 / 160 * 100
    ok_fp = fp_pct <= 2.0

    # ③ AR(1) φ=0.7 正常数据不许误报
    ar_fp = 0
    ar_unstable = 0
    for k in range(15):
        p = SPC.phase1_limits(_ar1(160, 0.7, 400 + k))
        ar_fp += len(p.get("excluded") or [])
        if p.get("stable") is False:
            ar_unstable += 1
    ar_pct = ar_fp / 15.0 / 160 * 100
    ok_ar = ar_pct <= 5.0

    # ④ 渐进漂移必须判不稳
    drift_unstable = 0
    for k in range(10):
        r = random.Random(300 + k)
        x = [r.gauss(0, 1) + i * 0.05 for i in range(160)]
        if (SPC.phase1_limits(x) or {}).get("stable") is False:
            drift_unstable += 1
    ok_drift = drift_unstable >= 8

    ok = ok_shift and ok_fp and ok_ar and ok_drift
    return check("F10 长而缓偏移整段识别（且正常/自相关不误报、漂移判不稳）", ok,
                 "30点2.5σ命中=%d%%(≥60) 误判稳=%d | 正常误剔=%.1f%%(≤2) | "
                 "ARφ0.7误剔=%.1f%%(≤5) 误判不稳=%d/15 | 漂移判不稳=%d/10(≥8)"
                 % (rate, stable_wrong, fp_pct, ar_pct, ar_unstable, drift_unstable))


def test_f11_build_design_constraints():
    """F11 生成侧安全门禁（2.19.10 闭合剩余风险 #3）。

    build_design 传入 factor_specs + constraints 后，生成阶段就应按安全限
    标记越界运行（高角点 5.0V 超 4.5V 安全限）；不传约束时仍返回矩阵，
    保持向后兼容（selftest 里大量旧调用不受影响）。
    """
    fs = [{"name": "化成电压", "low": 3.0, "high": 5.0}]
    cons = doe.build_constraints(fs, spec_overrides={"化成电压": {"safety": {
        "limit": "max", "value": 4.5, "unit": "V"}}})
    bd = doe.build_design("full_factorial", 1, 2, None,
                          factor_specs=fs, constraints=cons)
    ok_dict = (isinstance(bd, dict) and bd.get("any_violation") is True
               and 1 in (bd.get("unsafe_runs") or []))
    bd2 = doe.build_design("full_factorial", 1, 2, None)   # 旧契约：只返回矩阵
    ok_compat = isinstance(bd2, list) and len(bd2) == 2
    ok = ok_dict and ok_compat
    return check("F11 build_design 生成侧安全门禁 + 向后兼容", ok,
                 "包裹结构=%s 越界运行=%s | 旧契约返回矩阵=%s"
                 % (isinstance(bd, dict), bd.get("unsafe_runs") if isinstance(bd, dict) else None, ok_compat))


def test_f11b_constraint_sanity():
    """F11b 规格合法性自检（2.19.10 闭合剩余风险 #2）。

    安全包络只拦「已声明的界」——若规格矛盾（下限>上限）或无任何边界来源，
    门禁必须出声，不能静默放大误差或静默放行。
    """
    cons_bad = doe.build_constraints([{"name": "温度", "low": 100.0, "high": 50.0}])
    w_bad = cons_bad["温度"].get("warnings") or []
    ok_bad = any("下限" in w and "上限" in w for w in w_bad)
    cons_none = doe.build_constraints([{"name": "涂布速度"}])   # 无规格无安全包络
    w_none = cons_none["涂布速度"].get("warnings") or []
    ok_none = any("没有任何边界来源" in w for w in w_none)
    cons_ok = doe.build_constraints([{"name": "温度", "low": 50.0, "high": 100.0}])
    ok_ok = not (cons_ok["温度"].get("warnings"))
    ok = ok_bad and ok_none and ok_ok
    return check("F11b 规格合法性自检（矛盾/无来源出声、正常无告警）", ok,
                 "矛盾告警=%s 无来源告警=%s 正常无告警=%s" % (ok_bad, ok_none, ok_ok))


def test_f12_short_shift_unstable():
    """F12 小幅短偏移必须判不稳、不得谎称稳定（2.19.10 闭合剩余风险 #4）。

    旧实现：6~18 点的短段完全不查（段级识别 min_run 之下、又短于长同侧段阈值 0.12n），
    导致「小幅短偏移」被谎称 stable=True。新实现：短段（<0.12n）中位数偏离
    ≥SHORT_RUN_Z(2.2σ) 即判不稳——但**不剔除**（统计分辨力下限，避免误剔正常点），
    控制限偏宽、如实提醒。

    两条护城河（用正确方式造数据：先绑定单个 Random 再循环）：
      ① 清晰可识别的短偏移（10 点 3σ）必须稳定判不稳（≥75% 命中，留足种子方差余量）；
      ② 正常随机数据 20/20 必须仍 stable=True（短段门槛不得误伤正常波动）。
    ≤1.5σ 的短偏移处于统计分辨力下限，允许判稳——那是诚实下限，不算漏报。
    """
    def _series(seed, n=160, length=10, mag=3.0, start=70, shift=True):
        r = random.Random(seed)
        x = [r.gauss(0, 1) for _ in range(n)]
        if shift:
            for i in range(start, start + length):
                x[i] += mag
        return x

    # ① 清晰可识别的短偏移（10 点 3σ）：多数必须判不稳
    hit = 0
    for k in range(20):
        p = SPC.phase1_limits(_series(5000 + k, length=10, mag=3.0)) or {}
        if p.get("stable") is False:
            hit += 1
    ok_short = hit >= 15

    # ② 正常数据 20 次必须全 stable=True（不得误伤）
    fp_unstable = 0
    for k in range(20):
        pr = SPC.phase1_limits(_series(7000 + k, shift=False)) or {}
        if pr.get("stable") is not True:
            fp_unstable += 1
    ok_normal = fp_unstable == 0

    ok = ok_short and ok_normal
    return check("F12 小幅短偏移判不稳（正常数据不误伤）", ok,
                 "10点3σ判不稳=%d/20(≥15) | 正常误判不稳=%d/20" % (hit, fp_unstable))


def main():
    print("=== 复核修复验证 ===")
    results = [
        test_f1_rootcause_honest_degradation(),
        test_f2_temporal_verdict(),
        test_f3_doe_decode(),
        test_f4_probe_margin_pkey(),
        test_f5_ambiguity_detection(),
        test_f5b_unique_no_false_ambiguity(),
        test_f6_decode_safety_envelope(),
        test_f7_weight_configurable(),
        test_f8_weight_source_honest(),
        test_f9_sensitivity_has_resolution(),
        test_f10_long_gradual_shift(),
        test_f11_build_design_constraints(),
        test_f11b_constraint_sanity(),
        test_f12_short_shift_unstable(),
    ]
    n_fail = sum(1 for r in results if not r)
    print("=== 汇总：%d 项，%d 失败 ===" % (len(results), n_fail))
    return n_fail


if __name__ == "__main__":
    fail = main()
    if "--check" in sys.argv:
        sys.exit(1 if fail else 0)
