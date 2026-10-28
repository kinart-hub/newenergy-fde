# -*- coding: utf-8 -*-
"""
质量护栏（v1.10.0 新增）

把「最容易让现场误判」的几类情形集中在这里处理，每一类都有明确的降级或提示行为：

  1. 列角色识别    column_role：区分 测量 / 编号 / 时间 / 坐标 / 计数 / 文本。
                   只有「测量」列才能被自动选为分析目标或进入根因候选。
  2. 长表汇总      collapse_longform：一行一个采样点的化成/测试表，按实体取末行汇总。
  3. 测试条件归一  stc_normalize：组件 Pmax 按辐照度（与温度）做 STC 归一化后再判定。
                   辐照/温度是测试条件，不是产品原因，不进入根因候选。
  4. 证据门槛      rc_strong：根因证据低于门槛时，不得填写 8D 的 D4 根因。
  5. 工序护栏      pick_process：判定证据不足或存在歧义时不静默选定工序。

所有函数仅依赖标准库。
"""
import re
import copy

EVIDENCE_GATE = 0.20      # 根因证据门槛（evidence_score）

# 组件 Pmax 温度系数 γ 的**默认值**（-0.35 %/℃）。
# 它只是兜底：不同技术路线的组件该系数不同（晶硅约 -0.35%/℃，部分薄膜产品明显更小），
# 判不合格时差这一点就可能翻结论。因此 stc_normalize() 接受 gamma 实参覆盖，
# 调用方应优先从设备档案/组件规格书取值，取不到才回落到本常量。
STC_GAMMA = 0.0035

# STC 线性温度修正 P_stc = P_meas / (1 - γ(T-25)) 是围绕 25℃ 的一阶近似，
# 只有当温度修正量远小于 1 时才成立。|γ(T-25)| 一旦达到这个比例，继续做除法
# 就不再是"修得更准"，而是把测量值放大成天文数字（或越过因子零点变成负功率）。
# 届时正确做法是**不做温度修正并出声**，让人核查测温通道。
# 该阈值来自模型本身的适用前提，与具体产品无关，因此不做成可配参数。
_STC_MAX_CORRECTION = 0.5

_ID_RE = re.compile(r"(编号|编码|序号|卷号|批号|批次|流水|工位号|lot|slot|serial|^sn$|^id$|no$|号$)", re.I)
# 时间戳类才算「时间列」；「搁置时间」「静置时间」这类持续时长是测量值，不能排除
_TIME_RE = re.compile(r"(日期|时刻|timestamp|datetime|^date$|^time$|^时间$|时间戳|采样时间|测量时间|记录时间|开始时间|结束时间|^date|^time)", re.I)
_COUNT_NAME_RE = re.compile(r"(数量|计数|count|cnt|片数|个数|次数|不良数|ng数)", re.I)
_STEP_RE = re.compile(r"(工步|step|阶段|stage|工序段)", re.I)
_ENTITY_RE = re.compile(r"(编号|编码|电芯|卷号|片号|组件号|硅片|sn$|serial|^id$|cell|lot|批)", re.I)
_TEST_COND_RE = re.compile(r"(辐照|irradiance|^irr$|光强|组件温度|测试温度|模块温度|module.?temp|^tmod$)", re.I)
_TEMP_RE = re.compile(r"(组件温度|测试温度|模块温度|module.?temp|^tmod$)", re.I)
_IRR_RE = re.compile(r"(辐照|irradiance|^irr$|光强)", re.I)


def _norm(name):
    s = str(name or "")
    s = re.sub(r"[\(（][^)）]*[\)）]", "", s)
    s = re.sub(r"[\s_\-/、,，.。:：\[\]【】]+", "", s)
    return s.lower()


def _to_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("．", ".").replace("，", "")
    if s in ("", "-", "—", "NA", "N/A", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def column_role(name, values=None):
    """返回列角色：measure / id / time / coord / count / text。"""
    n = _norm(name)
    if _TIME_RE.search(n):
        return "time"
    if n in ("x", "y", "z") or "坐标" in n or "coord" in n:
        return "coord"
    # 电池列/行、片位等位置索引（整数、以列/行结尾）不是测量值
    if re.search(r"(列|行|位置号|片位)$", n) and values is not None:
        nums = [_to_float(v) for v in values if v not in (None, "")]
        nums = [x for x in nums if x is not None]
        if nums and all(abs(x - round(x)) < 1e-9 for x in nums):
            return "coord"
    if _ID_RE.search(n):
        return "id"
    if values is not None:
        vals = [v for v in values if v not in (None, "")]
        nums = [_to_float(v) for v in vals]
        nums = [x for x in nums if x is not None]
        if not nums:
            return "text"
        if len(nums) >= 0.8 * max(1, len(vals)):
            all_int = all(abs(x - round(x)) < 1e-9 for x in nums)
            if all_int and min(nums) >= 0 and (
                    _COUNT_NAME_RE.search(n) or (len(set(nums)) <= 8 and max(nums) <= 30)):
                return "count"
    return "measure"


def role_of(table, col):
    if not col or table is None or table.col_index(col) is None:
        return None
    return column_role(col, table.column(col))


def is_test_condition(name):
    """辐照度、组件/测试温度等测试条件（不是产品原因）"""
    return bool(_TEST_COND_RE.search(_norm(name)))


INTERNAL_COL_PREFIX = "__"


def is_internal_column(name):
    """内部衍生列（__ 前缀）：由本工具编码/派生而来，不是原始观测。

    为什么必须单独立一条：`encode_judgement` 会把 OK/NG 判定列编成
    `__判定_NG__`（1/0）。它与目标常常是同义反复——组件 IV 里
    "功率低" 与 "判定=NG" 本来就是同一件事。可它的数值形态（0/1）
    会被 role 判成 measure、会被低基数规则当成理想的分组维度、
    还会在按别名找列时命中「判定」这个别名。
    任何"找原因 / 找分组 / 找列"的路径都必须先把它排除，
    否则必然出现「功率低是因为功率低」排进根因（E06 实测排第三）。
    """
    return str(name or "").startswith(INTERNAL_COL_PREFIX)


# ------------------------------------------------------------------ 长表

def collapse_longform(table):
    """一行一个采样点（含工步列）的长表 -> 每个实体取末行。返回 (table, note)。"""
    if table is None or not table.columns or not table.rows:
        return table, None
    step_cols = [c for c in table.columns if _STEP_RE.search(_norm(c))]
    step = None
    for c in step_cols:
        levels = set(str(v) for v in table.column(c) if v not in (None, ""))
        if 2 <= len(levels) <= 12:
            step = c
            break
    if not step:
        return table, None
    nrow = len(table.rows)
    best = None
    for c in table.columns:
        if c == step:
            continue
        vals = [v for v in table.column(c) if v not in (None, "")]
        uniq = len(set(map(str, vals)))
        if uniq < 2 or uniq > nrow / 2.0:
            continue
        per = nrow / float(uniq)
        if per < 2:
            continue
        hint = bool(_ENTITY_RE.search(_norm(c)))
        score = (1 if hint else 0, per)
        if best is None or score > best[0]:
            best = (score, c)
    if not best:
        return table, ("检测到工步列「%s」，但找不到能区分实体的编号列，未做长表汇总；"
                       "结果可能被多工步的采样点稀释，请提供每个电芯/卷的编号列。" % step)
    ent = best[1]
    last = {}
    order = []
    ei = table.col_index(ent)
    for r in table.rows:
        key = r[ei] if ei < len(r) else None
        if key in (None, ""):
            continue
        if key not in last:
            order.append(key)
            last[key] = r
    # v1.11：不再整行照搬「最后一行」。化成/分容的长表里，末工步常常是静置或搁置，
    # 这时候电压电流全是 0 —— 直接取末行等于把全 feasible 的测量值换成了一行零。
    # 规则：标识类列取末行，测量类列在实体内部取「最后一个有效观测」，
    # 且把非适用工步的哨兵 0 当作缺失（判据：该列绝大部分非缺失值不为 0）。
    last = {k: list(v) for k, v in last.items()}
    _groups = {}
    for r in table.rows:
        key = r[ei] if ei < len(r) else None
        if key in (None, ""):
            continue
        _groups.setdefault(key, []).append(r)
    for c in table.columns:
        ci = table.col_index(c)
        if ci == ei:
            continue
        if role_of(table, c) not in ("measure", "count"):
            continue
        vals = [_to_float(v) for v in table.column(c)]
        real = [v for v in vals if v is not None and abs(v) > 0]
        sentinel_zero = bool(vals) and len(real) >= 0.5 * len(
            [v for v in vals if v is not None])
        for key, rows in _groups.items():
            picked = None
            for r in rows:
                v = _to_float(r[ci] if ci < len(r) else None)
                if v is None:
                    continue
                if sentinel_zero and abs(v) < 1e-12:
                    continue
                picked = v
            if picked is not None:
                row = last.get(key)
                if row is not None and ci < len(row):
                    row[ci] = picked
    nt = copy.deepcopy(table)
    nt.rows = [last[k] for k in order]
    note = ("检测到长表：每个「%s」对应多行，工步列「%s」共 %d 个工步，已按实体汇总为 %d 行。"
            "汇总规则：标识列取末行，测量列在实体内取**最后一个有效观测**，"
            "非适用工步的哨兵 0（静置/搁置段的电压电流）按缺失处理，不参与均值与 σ。"
            "若须按某一特定工步分析，请先筛选工步后再跑。"
            % (ent, step, len(set(str(v) for v in table.column(step) if v not in (None, ""))),
               len(nt.rows)))
    return nt, note


# ------------------------------------------------------------------ STC

def stc_normalize(table, col_map, target, gamma=None):
    """组件 Pmax 按辐照度/温度做 STC 归一化。返回 (table, col_map, note, new_target)。

    gamma 为 None 时用 STC_GAMMA 兜底；调用方应传设备档案/规格书里的真实温度系数，
    不同组件该系数差异足以翻转合格判定，不该在通用库里写死成行业常数。
    """
    col_map = dict(col_map or {})
    pmax_col = next((c for c, k in col_map.items() if k == "pmax" and table.col_index(c) is not None), None)
    if not pmax_col:
        return table, col_map, None, None
    if target and target != pmax_col and col_map.get(target) != "pmax":
        return table, col_map, None, None   # 用户指定了别的目标，不替换
    irr_col = next((c for c, k in col_map.items() if k == "irradiance" and table.col_index(c) is not None), None)
    if not irr_col:
        irr_col = next((c for c in table.columns if _IRR_RE.search(_norm(c))
                        and table.col_index(c) is not None), None)
    if not irr_col:
        return table, col_map, None, None
    temp_col = next((c for c, k in col_map.items() if k == "module_temp" and table.col_index(c) is not None), None)
    if not temp_col:
        temp_col = next((c for c in table.columns if _TEMP_RE.search(_norm(c))
                         and table.col_index(c) is not None), None)
    P = [_to_float(v) for v in table.column(pmax_col)]
    G = [_to_float(v) for v in table.column(irr_col)]
    T = [_to_float(v) for v in table.column(temp_col)] if temp_col else [None] * len(P)
    try:
        _gamma = STC_GAMMA if gamma is None else float(gamma)
    except (TypeError, ValueError):
        _gamma = STC_GAMMA
    out, skipped, bad_t = [], 0, 0
    for p, g, t in zip(P, G, T):
        if p is None or g is None or g < 600 or g > 1200:
            out.append(None)
            skipped += 1
            continue
        # 实测值 P_meas = P_stc · (G/1000) · (1 - γ(T-25))，故 STC 值需除以温度因子
        val = p * (1000.0 / g)
        if t is not None:
            # 温度修正量超过阈值时，线性近似已不成立：继续除下去会把测量值
            # 放大成天文数字（或越过因子零点变成负功率），而这不是"修得更准"。
            # 此时只出声、不做温度修正（仍保留辐照修正），把行标出来让人核查。
            if abs(_gamma * (t - 25.0)) >= _STC_MAX_CORRECTION:
                out.append(None)
                skipped += 1
                bad_t += 1
                continue
            val /= (1.0 - _gamma * (t - 25.0))
        out.append(round(val, 3))
    new_name = "Pmax_STC(W)"
    table.add_column(new_name, out)
    col_map[pmax_col] = None
    col_map[new_name] = "pmax"
    note = ("检测到辐照度列「%s」%s，已把 Pmax 归一化到 STC（1000 W/m²%s）作为分析目标「%s」。"
            "原始列「%s」保留但不参与判定；无法归一的行 %d 条%s。"
            "温度系数 γ=%s%s。"
            % (irr_col, "" if not temp_col else "与组件温度「%s」" % temp_col,
               "，并做温度修正" if temp_col else "，仅做辐照修正", new_name, pmax_col, skipped,
               "" if not temp_col else
               "（辐照度不在 600–1200 W/m² / 读数缺失 / 温度读数异常）",
               "%.4f" % _gamma,
               "（来自调用方，非默认值）" if gamma is not None
               else "（通用默认值，应以组件规格书或设备档案为准）"))
    if bad_t:
        note += ("其中有 %d 行的组件温度读数使修正量超出线性近似的适用范围"
                 "（|γ(T-25)| ≥ %.2f），按公式归一会让结果发散或变成负功率，"
                 "已单独剔除而不是给出一个荒谬的数值——这类读数通常是测温通道故障，建议一并核查。"
                 % (bad_t, _STC_MAX_CORRECTION))
    return table, col_map, note, new_name


# ------------------------------------------------------------------ 证据与工序

def rc_strong(rc):
    """根因结论是否有资格写进报告的 D4。"""
    if not rc or not rc.get("candidates"):
        return False
    if rc.get("evidence_insufficient"):
        return False
    lead = rc["candidates"][0]
    return float(lead.get("evidence_score") or 0) >= EVIDENCE_GATE


def pick_process(proc_scores):
    """自动工序判定的护栏。返回 (process_key or None, note or None)。"""
    if not proc_scores:
        return None, "工序未能判定：没有识别到任何工序特征，请用 --process 指定工序。"
    top_key, top = proc_scores[0]
    if top < 1.0:
        return None, ("工序判定证据不足（最高得分 %.1f），未自动指定工序；请用 --process 指定，"
                      "否则本次结论不可靠。" % top)
    if len(proc_scores) > 1:
        second_key, second = proc_scores[1]
        if second >= 1.0 and top - second < 0.5:
            return top_key, ("工序判定存在歧义（%s 与 %s 得分接近），暂按 %s 处理，请确认；"
                             "必要时用 --process 指定。" % (top_key, second_key, top_key))
    return top_key, None


def test_condition_warnings(assoc, result):
    """测试条件与目标强相关时给出提示（不作为根因）"""
    notes = []
    for d in assoc or []:
        name = d.get("param") or ""
        r = d.get("r")
        if r is not None and is_test_condition(name) and abs(r) >= 0.8:
            notes.append("测试条件「%s」与目标高度相关（r=%.2f）：目标的变化可能主要来自测试条件本身，"
                         "而非产品。请核查测试台标定与参考电池，并以归一化后的数值判定。" % (name, r))
    if notes:
        result.setdefault("warnings", []).extend(notes)


# ------------------------------------------------------------------ 目标选择（v1.11）

# 质量响应 vs 可调因子：自动选目标时，"多变的旋钮"不是"该被管的指标"。
# 反面教材：涂布数据里 PumpRPM 的变异系数远大于 CoatWt，按 CV 兜底就会挑泵速当目标，
# 于是整份报告围着"泵速为什么变化"转，而真正失控的是面密度。
# 三级提示越好 Tools 权重的依据：例行送这个词不是指标。
# T1 = 该工序对外承诺的核心指标；T2 = 一般质量特性。
_T1_HINT = re.compile(r"(pmax|最大功率|峰值功率|功率|容量|capacity|良率|yield|"
                      r"不良率|ng率|转换效率|隐裂率|碎片率|报废率|废品率)", re.I)
_T2_HINT = re.compile(
    r"(面密度|涂布量|重量|厚度|寿命|少子|电阻率|能量|效率|"
    r"不良|缺陷|ng数|翘曲|warp|错位|overhang|violation|内阻|acir|k值|K值|"
    r"延率|伸长|水份|残水|ttv|tv)", re.I)
_BAD_COUNT_HINT = re.compile(
    r"(不良|缺陷|ng数|ng率|返工|报废|次品|reject|defect|scrap|rework|失数|失效数)", re.I)
_CONTROL_HINT = re.compile(
    r"(速度|转速|rpm|泵速|泵频|给料|进给|设定|setpoint|设定值|目标值|target\.|"
    r"加热设定|张力设定|辊压间隙设定|刀距设定|输出%)", re.I)

# 超过这个规模的宽表不再做两两解释度估算（避免 O(n²) 拖慢，且通常也没必要）
_MAX_EXPLAIN_COLS = 40
_MAX_EXPLAIN_ROWS = 4000


def _numeric_pairs(table, cols, rows_cap=_MAX_EXPLAIN_ROWS):
    """取指定列的数值矩阵，行超限时间隔抽样"""
    data, keep = [], []
    for c in cols:
        vals = table.column(c) if table.col_index(c) is not None else []
        vals = list(vals)
        if len(vals) > rows_cap:
            step = int(len(vals) / rows_cap) or 1
            vals = vals[::step]
        nums = [_to_float(v) for v in vals]
        if sum(1 for v in nums if v is not None) < 0.3 * len(nums):
            continue
        data.append(nums)
        keep.append(c)
    return keep, data


def _corr_of(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 8:
        return None
    mx = sum(p[0] for p in pairs) / len(pairs)
    my = sum(p[1] for p in pairs) / len(pairs)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    syy = sum((p[1] - my) ** 2 for p in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    r = sxy / ((sxx * syy) ** 0.5)
    if r > 1:
        r = 1.0
    if r < -1:
        r = -1.0
    return r


def eta_sq(table, group_col, y_col):
    """分组列对数值列的解释度 η²（组间平方和 / 总平方和）"""
    g = table.column(group_col) if table.col_index(group_col) is not None else []
    y = [_to_float(v) for v in (table.column(y_col) or [])]
    groups = {}
    for gv, yv in zip(g, y):
        if gv in (None, "") or yv is None:
            continue
        groups.setdefault(gv, []).append(yv)
    if len(groups) < 2:
        return None
    allv = [v for vs in groups.values() for v in vs]
    n = len(allv)
    if n < 8:
        return None
    gm = sum(allv) / n
    sst = sum((v - gm) ** 2 for v in allv)
    if sst <= 0:
        return None
    ssb = sum(len(vs) * ((sum(vs) / len(vs)) - gm) ** 2 for vs in groups.values())
    return max(0.0, min(1.0, ssb / sst))


def explainability(table, col, col_map=None, include_test_condition=False):
    """这一列被其它变量解释得有多好（R²）——回答"它是不是那个被影响的结果"。

    三条边界：
      - 测试条件列（辐照、温度）**不作为解释者**：它们既不是工艺因子，也不是产品特性。
        否则 Voc 会因为"被组件温度解释得很好"而拿到满分，抢走 Pmax 的位置。
      - 编号/时间/坐标不参与解释。
      - 面密度被泵速解释掉 64%，泵速反过来对面密度也有同样的 r²：单看解释度分不清
        方向，所以它必须和「响应/控制」先验联合使用。
    """
    roles = {}
    numeric, textual = [], []
    for c in table.columns:
        if c == col:
            continue
        r = role_of(table, c)
        roles[c] = r
        if r == "measure" and (include_test_condition or not is_test_condition(c)):
            numeric.append(c)
        elif r == "text":
            vals = [v for v in table.column(c) if v not in (None, "")]
            lv = len(set(map(str, vals)))
            if 2 <= lv <= 12:
                textual.append(c)
    best = 0.0
    if len(numeric) <= _MAX_EXPLAIN_COLS and numeric:
        keep, data = _numeric_pairs(table, numeric)
        if table.col_index(col) is not None:
            y = [_to_float(v) for v in table.column(col)]
            if len(y) > _MAX_EXPLAIN_ROWS:
                step = int(len(y) / _MAX_EXPLAIN_ROWS) or 1
                y = y[::step]
            for xs in data:
                r = _corr_of(xs, y)
                if r is not None:
                    best = max(best, r * r)
    for g in textual:
        e = eta_sq(table, g, col)
        if e is not None:
            best = max(best, e)
    return round(best, 4)


def test_condition_dependence(table, col):
    """这一列被测试条件解释的比例。否则会被选中当作分析目标——实质上是在分析测量台账。"""
    best = 0.0
    for c in table.columns:
        if c == col or not is_test_condition(c):
            continue
        if role_of(table, c) != "measure":
            continue
        r = None
        try:
            y = [_to_float(v) for v in (table.column(col) or [])]
            x = [_to_float(v) for v in (table.column(c) or [])]
            r = _corr_of(x, y)
        except Exception:
            r = None
        if r is not None:
            best = max(best, r * r)
    return round(best, 4)


def target_score(table, col, col_map=None, explain=None, include_test_condition=False):
    """单个候选列的"适合当分析目标"得分。"""
    n = _norm(col)
    kb = None
    try:
        from . import preprocess as PP
        kb = PP.load_knowledge()
    except Exception:
        kb = None
    role = None
    key = (col_map or {}).get(col) if col_map else None
    if kb and key:
        role = (kb.get("params") or {}).get(key, {}).get("role")
    score = 0.0
    # 先验只占一小半：目标 selection 的主导证据应当是"这列真的有结构可分析"
    if role == "response":
        score += 1.5
    elif role == "control":
        score -= 2.5
    if _T1_HINT.search(col) or _T1_HINT.search(n):
        score += 3.0
    elif _T2_HINT.search(col) or _T2_HINT.search(n):
        score += 1.0
    if _CONTROL_HINT.search(col) or _CONTROL_HINT.search(n):
        score -= 1.5
    # 缺陷计数是产线最常用的目标形态（Pareto 就是按它做的），必须保留候选资格
    if column_role(col, table.column(col) if table.col_index(col) is not None else None) == "count":
        score += 2.0 if (_BAD_COUNT_HINT.search(col) or _BAD_COUNT_HINT.search(n)) else 0.2
    if explain is None:
        explain = explainability(table, col, col_map,
                                 include_test_condition=include_test_condition)
    score += 3.5 * explain
    # 几乎只由测试条件决定的列，本质是测量台账而不是质量特性
    if not include_test_condition:
        dep = test_condition_dependence(table, col)
        if dep >= 0.5:
            score -= 1.5
    return round(score, 3), explain


def pick_response_column(table, cols=None, col_map=None, explain_cache=None):
    """在候选列里挑最适合当分析目标的那一列，返回 (col, details)。

    三条硬规则：
      1. measure / count 角色才有资格（编号、时间、坐标、坐标索引不参与）；
      2. 优先"被别的东西影响的那一个"，而不是"最会变的旋钮"；
      3. 得分都接近时宁可交给下游兜底，也不要自作主张选一个控制因子。
    """
    if cols is None:
        cols = [c for c in table.numeric_columns()
                if role_of(table, c) in ("measure", "count")]
    if not cols:
        return None, {}
    out = {}
    for c in cols[:_MAX_EXPLAIN_COLS]:
        e = explain_cache.get(c) if explain_cache is not None else None
        sc, e = target_score(table, c, col_map, explain=e)
        if explain_cache is not None:
            explain_cache[c] = e
        out[c] = {"score": sc, "explainability": e}
    ranked = sorted(out.items(), key=lambda kv: -kv[1]["score"])
    return ranked[0][0], {"ranking": out, "top": ranked[0][1]}


# ------------------------------------------------------------------ 混杂与负例（v1.11）

def order_confound(table, group_col):
    """分组维度与行序（通常即时间顺序）的混杂程度，返回 Spearman rho。

    批次号按时间发放时，任何批次效应都与时间漂移完全不可区分。
    这时把批次当根因报出去，和报"时间造成的"没区别，都属于伪结论。
    """
    vals = table.column(group_col) if table.col_index(group_col) is not None else []
    vals = [v for v in vals]
    first = {}
    order = []
    for v in vals:
        if v in (None, ""):
            order.append(None)
            continue
        if v not in first:
            first[v] = len(first)
        order.append(first[v])
    x, y = [], []
    for i, v in enumerate(order):
        if v is None:
            continue
        x.append(float(i))
        y.append(float(v))
    rho = _corr_of(x, y)
    return None if rho is None else round(rho, 4)


def negative_verdict(assoc, exclude_cols=None, gate=0.20, response_cols=None):
    """判定"这一批差异其实解释得掉、没有可行动的工艺根因"。

    适用于 STC 归一化之后：归一化前目标跟着辐照/温度走，归一化后所有工艺参数都与
    目标无关 —— 那真相就不是"某个参数调错了"，而是"测试方法没归一"。
    这种情况必须明说没有根因，而不是勉强排一个最强的出来充数。

    response_cols：本身也是**结果指标**（Voc/Isc/FF 这类）的列。它们和目标同源，
    相关很正常，但它们不可被"调整"，所以不能算作可行动的根因。
    少了这一步，负例会被一个"不可调的结果参数"顶掉。
    """
    skip = set(exclude_cols or [])
    resp = set(response_cols or [])
    proc, cond = [], []
    for d in assoc or []:
        nm = d.get("param")
        r = d.get("r")
        if nm is None or r is None:
            continue
        if nm in skip or is_test_condition(nm) or nm in resp:
            if nm in skip or is_test_condition(nm):
                cond.append((nm, abs(r)))
            continue
        proc.append((nm, abs(r), d.get("p")))
    proc_max = max([p[1] for p in proc], default=0.0)
    if proc_max >= gate:
        return None
    strongest = max(proc, key=lambda t: t[1])[0] if proc else None
    if proc:
        text = ("归一化后目标与所有工艺参数的关联均低于 %.2f（最强 %.2f，共检验 %d 项工艺参数"
                "，另有 %d 项因属测试条件或结果指标被排除），本次差异已由测试条件解释，"
                "**没有可行动的工艺根因**。请把结论写成「补充 STC 归一与测试台标定」，"
                "而不是去调某个参数。" % (gate, proc_max, len(proc), len(cond)))
    else:
        text = ("参与检验的 %d 个候选列全部是测试条件或结果指标（工艺参数 0 项），"
                "差异已被测试条件解释，**没有可行动的工艺根因**。"
                "请把结论写成「补充 STC 归一与测试台标定」，而不是去调某个参数。"
                % len(cond))
    return {
        "verdict": "no_actionable_cause",
        "max_process_r": round(proc_max, 3),
        "gate": gate,
        "n_process_tests": len(proc),
        "n_condition_tests": len(cond),
        "strongest_process": strongest,
        "strongest_condition": max(cond, key=lambda t: t[1])[0] if cond else None,
        "text": text,
    }
