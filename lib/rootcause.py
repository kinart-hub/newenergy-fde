# -*- coding: utf-8 -*-
"""
rootcause.py —— 不良根因定位引擎

融合两条证据链，避免"纯统计排序"的误导：
  A. 知识库先验：缺陷类型 → 候选参数 → 机理 → 验证清单（process_specs.json）
  B. 数据证据  ：候选参数在实际数据中的关联强度 / 分组差异 / 时序吻合度

综合得分（排序启发式，非概率）= 0.40*数据证据 + 0.35*知识先验 + 0.15*时序吻合 + 0.10*可验证性
可验证性（P1-5）原为硬编码 0.5，现按候选真实信号动态计算并随候选输出。
输出：候选根因排序、5Why 链条、良率损失 Pareto、数据缺口清单。
"""

try:
    from . import degrade          # 包模式
except Exception:                  # 脚本模式（python lib/pipeline.py）无包上下文
    import degrade                 # noqa
import os
import json
from . import quality_guard as QG
from . import stats as S
from . import preprocess as PP
from . import spc as SPC
from . import status as ST
from . import confound as CF

_KB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "knowledge", "process_specs.json")
_KB = None

# 根因排序权重（评审 P1-5）：此前 0.10 的可验证性权重硬编码为 0.5 且未进排序，
# 现已按候选真实信号动态计算并统一引用这组常量。集中管理便于调参与灵敏度验证，
# 不随行业/产品固化——调整权重改这里即可，无需触碰评分逻辑。
#
# 外部评审 P1-5（2026-10-29）：权重只有硬编码、没有配置文件也没有敏感性分析脚本，
# 于是"发现更优权重也改不了"，更没法说明这组数是验证过的还是手调的。
# 现在权重可从 lib/config_rootcause.json 覆盖，但**内置默认值仍是唯一兜底**，
# 且配置读不出来必须显式告知（不许静默回落——静默回落等于让人以为按企业口径配好了）。
ROOTCAUSE_WEIGHTS = {
    "evidence": 0.40,        # 数据证据强度
    "prior": 0.35,           # 知识库先验
    "temporal": 0.15,        # 时序吻合度
    "verifiability": 0.10,   # 可验证性（按候选真实信号动态计算）
}

_WEIGHT_KEYS = ("evidence", "prior", "temporal", "verifiability")
_WEIGHTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "config_rootcause.json")


def normalize_weights(raw):
    """校验并归一化权重。返回 (weights, error)。

    校验不通过一律返回 (None, 原因)，**不做"缺哪个就补默认"的静默修补**：
    缺一个键的配置说明写配置的人漏了，补默认等于把他没做的决定替他做了，
    而他永远不会知道。宁可报错让他看见。
    """
    if not isinstance(raw, dict):
        return None, "权重不是字典"
    lack = [k for k in _WEIGHT_KEYS if k not in raw]
    if lack:
        return None, "缺权重项：%s" % "、".join(lack)
    try:
        vals = {k: float(raw[k]) for k in _WEIGHT_KEYS}
    except (TypeError, ValueError) as exc:
        return None, "权重含非数值：%s" % exc
    neg = [k for k, v in vals.items() if v < 0]
    if neg:
        return None, "权重不得为负：%s" % "、".join(neg)
    tot = sum(vals.values())
    if tot <= 0:
        return None, "权重之和必须大于 0（当前 %g）" % tot
    return {k: v / tot for k, v in vals.items()}, None


def load_weights(path=None):
    """读取根因权重配置，返回 (weights, source_note)。

    source_note 是给报告用的**人话**，必须让读者知道这次排序用的是哪套权重：
      - 配置文件读到且合法 → "配置文件（路径）"
      - 文件不存在        → "内置默认（未提供配置文件）"
      - 文件存在但读不出/不合法 → "内置默认（配置文件有问题：…）"
    第三种情况最要紧：它是事故而不是事实，必须能被看见。
    """
    p = path or _WEIGHTS_PATH
    if not os.path.isfile(p):
        w, _ = normalize_weights(ROOTCAUSE_WEIGHTS)
        return w, "内置默认（未提供配置文件 %s）" % os.path.basename(p)
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as exc:
        # 不在这里再包 try：degrade.note 自身已整体兜底（它的注释写明
        # "诊断通道不许搞崩主流程"），外层再吞一层就变成静默——S1 静态扫描会抓。
        degrade.note("rootcause.load_weights@read", exc)
        w, _ = normalize_weights(ROOTCAUSE_WEIGHTS)
        return w, "内置默认（配置文件读取失败：%s）" % exc
    raw = cfg.get("scoring_weights") if isinstance(cfg, dict) else None
    w, err = normalize_weights(raw)
    if err:
        degrade.note("rootcause.load_weights@schema", ValueError(err))
        w2, _ = normalize_weights(ROOTCAUSE_WEIGHTS)
        return w2, "内置默认（配置文件不合规：%s）" % err
    return w, "配置文件 %s" % os.path.basename(p)


def weights_note(w, source):
    """把权重渲染成一句可核对的说明（不手工维护数字，全部由实际权重生成）。"""
    labels = {"evidence": "数据证据", "prior": "知识先验",
              "temporal": "时序吻合", "verifiability": "可验证性"}
    order = ("evidence", "prior", "temporal", "verifiability")
    parts = ["%.2f %s" % (w[k], labels[k]) for k in order if k in w]
    return "综合得分为排序启发式权重之和（%s；来源：%s），不是概率" % (
        " + ".join(parts), source)


def _kb():
    global _KB
    if _KB is None:
        with open(_KB_PATH, "r", encoding="utf-8") as f:
            _KB = json.load(f)
    return _KB


# ------------------------------------------------------------------ 缺陷识别

def _is_count_series(vals):
    """判断一列是否是'缺陷计数'而非连续测量量。

    工艺数据里常见陷阱：列名叫「极片错位」，值是 0.28 mm 这种连续测量值，
    它不是缺陷计数，不能进 Pareto 求和——会把量纲搞错、结论带偏。
    """
    if not vals:
        return False
    if any(v < 0 for v in vals):
        return False
    ints = sum(1 for v in vals if abs(v - round(v)) < 1e-9)
    return ints / len(vals) >= 0.95 and max(vals) >= 1


# 企业自定义的缺陷叫法（知识库不可能穷举）。命中这些词且是计数型列，就纳入 Pareto。
_BAD_HINT = ("不良", "缺陷", "报废", "返工", "次品", "reject", "defect", "ng数", "ng数",
             "掉料", "气泡", "针孔", "划痕", "厚边", "隐裂", "断栅", "虚焊", "过焊",
             "碎片", "破片", "脏污", "污渍", "崩边", "缺角", "黑心", "黑斑", "漏电",
             "裂纹", "毛刺", "短路", "起皱", "褶皱", "色差", "氧化", "水印", "缺口")


# 「现场叫法 → 标准条目」的别名索引（懒构建 + 缓存）
_DEFECT_NAMES_CACHE = {}


def _defect_alias_names(dk, dv):
    """返回某缺陷的全部叫法（已归一化）：键名 + 自身 aliases + param_key 的 aliases。

    为什么需要这一层：知识库按**机理**命名（「隔膜褶皱」「黑心」），
    现场按**现象**叫（「极片起皱」「黑斑」）。同一个缺陷两个名字对不上号，
    匹配就断链 —— 库里明明有机理，却走了「未收录」兜底，
    工程师拿到的 8D 报告 D4 栏只能写着「机理未收录」。

    别名有三个来源，合并去重：
      1. 条目键名本身
      2. 条目的 aliases 字段（现场叫法，最直接）
      3. param_key 指向的 params 条目的 aliases（复用，避免两处维护）

    **但第 3 条有个前提**：param_key 必须是一个「缺陷计量列」。
    「功率衰减」「内阻偏高」「容量一致性差」这类缺陷是由**结果指标本身的漂移**
    定义的，它们的 param_key 指向 pmax / acir / capacity —— 那是**被分析的目标量**，
    不是缺陷计数列。把目标量的别名（Pmax / 功率 / 峰值功率）当成缺陷叫法，
    会让 identify_defects 把功率列本身识别成「功率衰减」缺陷列，
    进而让目标列 == 缺陷列、共线排除判据失效（实测踩过：E06 组件IV 用例直接翻红）。
    所以这类条目的别名只认键名与显式 aliases，不继承结果指标的别名。
    """
    hit = _DEFECT_NAMES_CACHE.get(dk)
    if hit is not None:
        return hit
    kb = _kb()
    names = [dk] + list(dv.get("aliases") or [])
    pk = dv.get("param_key")
    if pk and not dv.get("response_defect"):
        pv = (kb.get("params") or {}).get(pk) or {}
        names += list(pv.get("aliases") or [])
    out, seen = [], set()
    for n in names:
        if not n:
            continue
        nn = PP._norm_name(n)
        if nn and nn not in seen:
            seen.add(nn)
            out.append(nn)
    _DEFECT_NAMES_CACHE[dk] = out
    return out


def _name_hits(alias_norm, col_norm):
    """别名是否命中列名：精确相等，或别名是列名的子串（长度≥2 防单字误命中）。"""
    if alias_norm == col_norm:
        return True
    return len(alias_norm) >= 2 and alias_norm in col_norm


def identify_defects(table, hint=None, process=None):
    """从数据列中识别缺陷类型（含 EL 缺陷分类），返回 [(缺陷名, 列名, 计数/率)]

    知识库只登记了常见缺陷名，企业自己的叫法（如「掉料」）不在库里就会漏。
    所以除了知识库命中，还按「列名含缺陷语义 + 确为计数型」补一轮数据驱动识别。
    """
    kb = _kb()
    found = []
    # 1. 命中知识库缺陷名（含「现场叫法」别名反查）
    #
    #    先做一次「列 → 最具体条目」的裁决，再按条目顺序收录。
    #    为什么必须先裁决：别名一多就会出现交叉命中 ——
    #    「边缘短路」同时含「短路」（内短路的别名）和「边缘漏电」，
    #    不裁决就会把同一列算进两个缺陷，Pareto 总量虚高、根因指向也跟着错。
    #
    #    裁决规则（v2.4 修的两处）：
    #      ① **工序优先**。原来 process 参数只用在下面 1b 的数据驱动兜底里，
    #         知识库命中这一段压根没看它 —— 于是光伏 EL 数据里出现「焊接不良」列时，
    #         「极耳焊接不良」与「虚焊」两条的别名等长，谁赢全看 JSON 书写顺序，
    #         结果可能把光伏串焊缺陷判成锂电池超声焊问题，
    #         根因指向 welding power/pressure/time —— 跨行业串机理。
    #         这比「未收录」糟得多：它看起来很专业，没人会怀疑它。
    #      ② 同工序内**最长别名优先**，别名越长越具体。
    #      ③ 仍平局按键名排序，保证确定性（不再取决于 JSON 里的书写顺序）。
    dr_all = kb["defect_rootcause"]
    in_proc = {dk: dv for dk, dv in dr_all.items()
               if process and dv.get("process") == process}

    def _best_for(nc):
        """返回 (条目键, 是否跨工序借用)；没命中返回 (None, False)。"""
        for scope in (in_proc, dr_all):
            if not scope:
                continue
            best_dk, best_len = None, 0
            for dk in sorted(scope):
                for nn in _defect_alias_names(dk, scope[dk]):
                    if _name_hits(nn, nc) and len(nn) > best_len:
                        best_dk, best_len = dk, len(nn)
            if best_dk:
                # 从全局兜底命中的，说明本工序没登记这条机理
                return best_dk, bool(process) and scope is dr_all
        return None, False

    col_best = {}
    for c in table.columns:
        nc = PP._norm_name(c)
        dk, cross = _best_for(nc)
        if dk:
            col_best[c] = (dk, cross)

    for dk, dv in dr_all.items():
        for c in table.columns:
            if not col_best.get(c) or col_best[c][0] != dk:
                continue
            vals = table.numeric_column(c)
            valid = [v for v in vals if v is not None]
            if valid:
                found.append({"defect": dk, "column": c,
                              "total": sum(valid), "n_records": len(valid),
                              # v1.13：不良「件数」是"有多少个件出现了该缺陷"，
                              # 不是缺陷计数之和。一个组件上有 3 处隐裂，
                              # 那是 1 件不良、不是 3 件 —— 用 total 算不良率会
                              # 算出 200%+ 的荒唐值（8D D2 直接踩到）。
                              "n_positive": len([v for v in valid if v > 0]),
                              "mean": S.mean(valid), "process": dv["process"],
                              # 本工序没登记这条机理、借用了别的工序的条目。
                              # 报告层据此提示"机理来源与当前工序不符，请确认"，
                              # 不直接当成结论（R1：没依据的不下判定）。
                              "cross_process": col_best[c][1],
                              "is_count": _is_count_series(valid)})
    # 1b. 数据驱动补充：知识库没有的缺陷叫法（企业自定义），只要确实是计数列就纳入。
    #     否则「掉料」这类现场常用名会被整列忽略，Pareto 与根因都会跑偏。
    known_cols = {f.get("column") for f in found}
    for c in table.columns:
        if c in known_cols:
            continue
        vals = table.numeric_column(c)
        valid = [v for v in vals if v is not None]
        if len(valid) < 5:
            continue
        nc = PP._norm_name(c)
        if not any(w in nc for w in _BAD_HINT):
            continue
        if not _is_count_series(valid):
            # 叫「不良率」却是比率列：不能当计数求和，量纲会错
            continue
        found.append({"defect": c, "column": c, "total": sum(valid),
                      "n_records": len(valid),
                      "n_positive": len([v for v in valid if v > 0]),
                      "mean": S.mean(valid),
                      "process": process or "", "is_count": True,
                      "from_data": True})
    # 2. 分类列中包含缺陷名称作为取值
    for c in table.text_columns():
        vals = [str(v) for v in table.column(c) if v not in (None, "")]
        for dk in sorted(dr_all):
            dv = dr_all[dk]
            dnames = _defect_alias_names(dk, dv)
            hits = sum(1 for v in vals
                       if any(_name_hits(nn, PP._norm_name(v)) for nn in dnames))
            if hits:
                found.append({"defect": dk, "column": c, "count": hits,
                              "n_records": len(vals), "as_category": True,
                              "process": dv["process"],
                              "cross_process": bool(process)
                              and dv.get("process") != process})
    if hint:
        nd = PP._norm_name(hint)
        # 与列匹配同一套优先级：本工序条目排前面，其次是键名长的（更具体），
        # 最后按键名排序保证确定性。insert(0) 会反序，所以倒着插。
        cands = []
        for dk in sorted(dr_all):
            dv = dr_all[dk]
            if any(nn in nd or nd in nn for nn in _defect_alias_names(dk, dv)):
                cands.append((0 if dk in in_proc else 1, -len(dk), dk, dv))
        cands.sort()
        for _pri, _nlen, dk, dv in reversed(cands):
            found.insert(0, {"defect": dk, "column": None, "from_hint": True,
                             "process": dv["process"],
                             "cross_process": bool(process)
                             and dv.get("process") != process})
    # 去重
    seen, out = set(), []
    for f in found:
        k = (f["defect"], f.get("column"))
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def pick_primary_defect(defects, table):
    """在多个缺陷项里挑出"首要问题"。

    不能简单取总量最大的那个：产线上绝对量最大的往往是长期存在的老问题，
    真正需要处理的是**正在恶化**的那一项（增量）。按总量选会把注意力引到
    稳定噪声上，真正的异常反而被盖过去。
    """
    if not defects:
        return None
    scored = []
    for d in defects:
        col = d.get("column")
        if not col or d.get("as_category") or d.get("from_hint"):
            scored.append((0.0, d))
            continue
        vals = [v for v in table.numeric_column(col) if v is not None]
        if len(vals) < 12:
            scored.append((0.0, d))
            continue
        k = max(3, len(vals) // 3)
        head, tail = vals[:k], vals[-k:]
        a, b = S.mean(head), S.mean(tail)
        trend = (b - a) / a if a > 0 else (b - a)
        scored.append((trend, d))
    scored.sort(key=lambda x: -x[0])
    best_trend, best = scored[0]
    # 全都平稳（无恶化）时才退回按总量选
    if best_trend < 0.15:
        cnt = [d for d in defects if d.get("total")]
        if cnt:
            return max(cnt, key=lambda d: d["total"])["defect"]
    return best["defect"]


# ------------------------------------------------------------------ Pareto

def pareto(categories, weights=None):
    """categories: {名称: 数量} 或 [(名称, 数量)] -> Pareto 表 + ABC 分级"""
    items = categories.items() if isinstance(categories, dict) else categories
    items = [(k, float(v)) for k, v in items if v is not None]
    items.sort(key=lambda kv: -kv[1])
    total = sum(v for _, v in items) or 1.0
    rows, cum = [], 0.0
    for k, v in items:
        cum += v
        pct = v / total * 100
        cum_pct = cum / total * 100
        if cum_pct <= 80:
            cls = "A"
        elif cum_pct <= 95:
            cls = "B"
        else:
            cls = "C"
        rows.append({"item": k, "value": v, "pct": pct, "cum_pct": cum_pct, "abc": cls})
    return {"rows": rows, "total": total,
            "top": rows[0] if rows else None,
            "vital_few": [r for r in rows if r["abc"] == "A"]}


# ------------------------------------------------------------------ 证据计算

def _data_evidence(table, param_key, target_col, defect_proc):
    """计算某参数对目标的证据强度，返回 (score 0-1, detail)"""
    kb = _kb()
    spec = kb["params"].get(param_key, {})
    aliases = spec.get("aliases", []) + [spec.get("name", "")]
    col = None
    for c in table.columns:
        # 内部衍生列不是原始测量列，不得参与"找列"：
        # encode_judgement 造的 __判定_NG__ 名字里带「判定」，用别名模糊匹配
        # 必然命中 qa_judgement，于是"目标列的编码"被当成某个参数的真实列，
        # 甚至作为它自己的原因上榜（E06 实测：__判定_NG__(0.22) 排第三）。
        if QG.is_internal_column(c) or (target_col and c == target_col):
            continue
        nc = PP._norm_name(c)
        for a in aliases:
            if PP._norm_name(a) and (PP._norm_name(a) == nc or PP._norm_name(a) in nc
                                     or nc in PP._norm_name(a)):
                col = c
                break
        if col:
            break
    if col is None:
        return 0.0, {"status": ST.INSUFFICIENT_DATA, "param": param_key,
                     "need": spec.get("name", param_key)}
    vals = table.numeric_column(col)
    y = table.numeric_column(target_col) if target_col else None
    detail = {"status": ST.SUCCESS, "param": param_key, "column": col}
    score = 0.0
    if y:
        r, p, n = S.pearson(vals, y)
        if r is not None:
            detail["r"] = r
            detail["p"] = p
            detail["n"] = n
            score = min(1.0, abs(r))
            if p is not None and p > 0.05:
                score *= 0.5
            detail["note"] = "与目标 %s 的 Pearson r=%.3f (p=%.4f)" % (target_col, r, p or 0)
    else:
        # 无目标列时，用其自身超出规格/波动程度作为证据
        valid = [v for v in vals if v is not None]
        ctrl = spec.get("control", {})
        if valid:
            cv = S.stdev(valid) / S.mean(valid) * 100 if S.mean(valid) else None
            detail["cv_pct"] = cv
            lsl, usl, tgt = SPC.resolve_spec(ctrl, S.mean(valid))
            if usl is not None or lsl is not None:
                oos = sum(1 for v in valid if (usl is not None and v > usl)
                          or (lsl is not None and v < lsl))
                detail["out_of_spec"] = oos
                detail["oos_pct"] = oos / len(valid) * 100
                score = min(1.0, oos / len(valid) * 5)
            elif cv is not None:
                lim = ctrl.get("cv_limit_pct")
                score = min(1.0, cv / lim) if lim else min(1.0, cv / 5.0)
            detail["note"] = "未指定目标列，按自身波动/超规格程度评估"
    return score, detail


def _temporal_match(table, param_col, target_col):
    """参数异常时段与指标异常时段的重合度（0-1）"""
    x = table.numeric_column(param_col)
    y = table.numeric_column(target_col) if target_col else None
    if y is None:
        return 0.0, None
    pairs = [(v, yy) for v, yy in zip(x, y) if v is not None and yy is not None]
    if len(pairs) < 10:
        return 0.0, None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, sx = S.mean(xs), S.stdev(xs)
    my, sy = S.mean(ys), S.stdev(ys)
    if not sx or not sy:
        return 0.0, None
    # 双方同时偏离各自均值 1.5σ 以上的点占比
    both = sum(1 for a, b in zip(xs, ys)
               if abs(a - mx) > 1.5 * sx and abs(b - my) > 1.5 * sy)
    ratio = both / len(pairs)
    score = min(1.0, ratio * 3)
    # 时序判定：参数与指标同时异常占比高 → 时序吻合（支持因果时序）；
    # 占比低 → 本批数据里两者异常不重合，对"同节拍因果"主张构成时序层面的不支持。
    # 必须显式给出 verdict，否则消费方 820 行查不存在的 verdict 键，整段证据逻辑是死分支。
    verdict = "支持" if score >= 0.3 else "不支持"
    note = ("参数与指标同时异常占比 %.1f%%（%d/%d），时序吻合"
            % (ratio * 100, both, len(pairs))) if verdict == "支持" \
        else ("本批数据里参数与指标异常时段不重合（同时异常占比 %.1f%%），"
              "对「同节拍因果」主张构成时序层面的不支持"
              % (ratio * 100))
    return score, {"both_abnormal": both, "n": len(pairs),
                   "ratio_pct": ratio * 100, "score": score,
                   "verdict": verdict, "note": note}


def _evidence_gate(score, det):
    """知识库先验不能单独撑起排名。

    曾经出现过：某参数 r=0.022、p=0.76（统计上毫无关联），只因在知识库
    likely_params 里排第一（先验 1.0）就被列为首要根因。现场照着去调这个参数，
    真因一点没动。所以凡是"有数据却查不到关联"的候选，一律降级。
    """
    if det.get("status") != ST.SUCCESS:
        return score * 0.60, "数据缺失，按知识库先验推断（需补测才能确认）"
    r = det.get("r")
    p = det.get("p")
    if r is None:
        return score, None
    if (p is not None and p > 0.05) or abs(r) < 0.15:
        return score * 0.45, "统计上未发现关联，仅凭知识库先验（不可据此调机）"
    return score, None


def _verifiability(c):
    """可验证性（P1-5）：这个候选被提出后，能不能被一套明确步骤确认或推翻。

    不是概率，是排序启发式的一项权重输入，取值 [0.05, 1.0]。
    原实现硬编码为 0.5，既不算进排序、也不随候选输出——等于这一项权重从没生效。
    现在由"提出后能否被验证"的信号决定：
      + 有可执行的验证步骤（verification_steps 非空）
      + 有数据支撑（supported_by_data，可重复复核）
      + 有历史验证路径（history_case，已被现场证实过可验）
    基线 0.2；全无信号、且连验证步骤都没有"未知"假设压到 0.1。
    """
    v = 0.20
    if c.get("verification_steps"):
        v += 0.30
    if c.get("supported_by_data"):
        v += 0.30
    if c.get("history_case"):
        v += 0.20
    if c.get("conclusion_status") == "未知" and not c.get("verification_steps"):
        v = 0.10
    return max(0.05, min(1.0, v))


def _chain_processes(proc):
    """本工序 + 上下游链内的工序集合，用于把跨工序串味的候选挡在外面"""
    if not proc:
        return None
    out = {proc}
    try:
        from . import crossprocess as XP
        kb = _kb()
        procs = kb.get("processes", {})
        if proc in procs:
            # 签名是 upstream_of(process, chain_hops=4)：内部自己 load_kb，
            # 不接第二个位置参数。此前多传了一个 procs，
            # 每次调用都抛 TypeError 并被下面的 except 吞掉 ——
            # 于是横向排查清单每次都只剩本工序自己，而全库自检照常全绿。
            for p in XP.upstream_of(proc, chain_hops=4):
                out.add(p)
            for p in XP.downstream_of(proc, chain_hops=4):
                out.add(p)
    except Exception as _dg_e:
        # 上下游追溯失败时 out 只剩本工序自己，与"该工序确无上下游"无法区分。
        # 横向排查清单会因此少一大片，所以要留痕。
        degrade.note("rootcause.related_processes", _dg_e)
    return out

def rank_root_causes(defect_name, table, target_col=None, top=5,
                     col_map=None, process=None, exclude_cols=None,
                     weights=None, weights_path=None):
    """返回候选根因排序 list

    候选有两个来源，缺一不可：
      A. 知识库 likely_params（带先验）
      B. 数据驱动：实际数据里与目标显著相关、且角色为可调控因子的参数
    只留 A 会漏掉知识库没预设的真因（例如烘箱二区温度漂移）；
    只留 B 又会掉进"相关不等于因果"。所以两条都进候选，再按同一把尺子排序。

    返回 dict 里**一定有 status**：此项 microstructure 不是装饰。
    实测踩过的坑：喂一张空表进来，本函数照样吐出 3 条「机理支持」的候选、
    score 排得整整齐齐，和喂真数据时长得一模一样——
    使用者拿到的是一份看起来很专业的根因报告，而它背后一行数据都没有。
    所以本函数必须自己说清"这次排序站在什么数据基础上"。
    """
    kb = _kb()
    # 入参契约：缺缺陷名/缺表是**调用方的 bug**，不是"数据不足"。
    # 若把它混成 INSUFFICIENT_DATA 返回，使用者会以为再喂点数据就能出来，
    # 于是反复重试一条永远走不通的路。type 不同、处理动作不同，必须分开。
    if not defect_name or not str(defect_name).strip():
        raise ValueError("rank_root_causes 必须给出缺陷名称（defect_name）")
    if table is None:
        raise ValueError("rank_root_causes 必须给出数据表（table）")
    if target_col is not None and (not isinstance(target_col, str)
                                   or not target_col.strip()):
        raise ValueError("target_col 必须是非空列名字符串或 None")
    if not isinstance(top, int) or isinstance(top, bool):
        raise TypeError("top 必须是整数")
    if top <= 0:
        raise ValueError("top 必须为正整数（当前 %r）；想要全部候选请传足够大的值" % top)

    # 权重来源（外部评审 P1-5）：调用方显式传入 > 配置文件 > 内置默认。
    # 无论走哪条路，最终都会把"这次用了哪套权重、从哪来"写进结果的 weights_source，
    # 供报告直接引用——排序依据不可见，读者就只能信或不信，没法核对。
    if weights is not None:
        _w, _werr = normalize_weights(weights)
        if _werr:
            raise ValueError("调用方传入的根因权重不合规：%s" % _werr)
        _wsrc = "调用方显式传入"
    else:
        _w, _wsrc = load_weights(weights_path)
    if not _w:
        # 内置默认都坏掉只可能是代码被人改错，属于程序 bug，不能当成"数据不足"糊过去
        raise ValueError("根因权重无法确定：内置默认权重不合规")

    info = kb["defect_rootcause"].get(defect_name)
    proc = process or (info or {}).get("process")
    if not info and not col_map:
        return None
    cands = []
    n_param = len((info or {}).get("likely_params", []))
    try:
        from . import kbstore as _KBS
    except Exception:
        import kbstore as _KBS
    hist = {}       # pk -> 历史验证对策摘要
    for i, pk in enumerate((info or {}).get("likely_params", [])):
        spec = kb["params"].get(pk, {})
        prior = 1.0 - i / max(1, n_param) * 0.6  # 越靠前先验越高
        # 现场已验证对策带来的先验加成：把过去的经验折算成先验，避免每次从零起步
        try:
            bonus, case = _KBS.recommend_prior(defect_name, pk)
        except Exception:
            bonus, case = 0.0, None
        if case:
            hist[pk] = case
            prior = min(1.3, prior + bonus)
        ev, det = _data_evidence(table, pk, target_col, proc or info["process"])
        tscore, tdet = 0.0, None
        if det.get("column") and target_col:
            tscore, tdet = _temporal_match(table, det["column"], target_col)
        # 注意：可验证性（P1-5）不再硬编码 0.5，留到 _annotate_evidence 之后
        # 按候选真实信号统一计算并计入总分（见下方 post-annotation 重算段）。
        # 这里先只累加前三项，verifiability 分项在收尾时补全。
        base = (_w["evidence"] * ev
                + _w["prior"] * prior
                + _w["temporal"] * tscore)
        gate_note = _evidence_gate(base, det)[1]
        cands.append({
            "param_key": pk,
            "param": det.get("column"),  # 原始列名，供后续取规格与实际数据用
            "param_name": spec.get("name", pk),
            "unit": spec.get("unit", ""),
            "score": round(base, 4),     # 占位，post-annotation 重算为含可验证性的总分
            "evidence_score": round(ev, 3),
            "prior_score": round(prior, 3),
            "temporal_score": round(tscore, 3),
            "data_status": det.get("status"),
            "gate_note": gate_note,
            "source": "知识库先验",
            "detail": det,
            "temporal": tdet,
            "note": spec.get("note", ""),
            "history_case": ({
                "id": hist[pk].get("id"),
                "level": hist[pk].get("evidence_level"),
                "action": (hist[pk].get("countermeasure") or {}).get("action"),
                "result": (hist[pk].get("verification") or {}).get("result"),
                "doc_no": (hist[pk].get("standardization") or {}).get("doc_no"),
                "source": (hist[pk].get("_kbmeta") or {}).get("source"),
            } if pk in hist else None),
        })
    # ---- 候选来源 B：数据驱动 ------------------------------------------
    # 知识库的 likely_params 是"常见原因"清单，不可能穷举每条产线的真因。
    # 数据里明明与目标强相关、且确实是可调控因子的参数，必须有资格进候选，
    # 否则真因永远排不上号——哪怕它的 r 高达 0.8。
    kb_keys = {c["param_key"] for c in cands}
    if col_map and target_col:
        allowed = _chain_processes(proc)
        for c, pk in col_map.items():
            if not pk or pk in kb_keys:
                continue
            # 护栏：编号/时间/坐标/计数列与测试条件（辐照、组件温度）不是产品原因（v1.10）
            if QG.is_test_condition(c) or QG.role_of(table, c) not in ("measure", None):
                continue
            pspec = kb["params"].get(pk, {})
            if pspec.get("role") == "response":
                continue                       # 结果指标不能当原因（否则把 TTV 当成 TTV 超差的原因）
            pproc = pspec.get("process")
            if allowed and pproc and pproc not in allowed:
                continue                       # 挡掉跨工序串味的参数
            ev, det = _data_evidence(table, pk, target_col, proc or pproc or "")
            if det.get("status") != ST.SUCCESS:
                continue
            r = det.get("r")
            p = det.get("p")
            if r is None or abs(r) < 0.20:
                continue
            if p is not None and p > 0.05:
                continue
            tscore, tdet = (0.0, None)
            if det.get("column"):
                tscore, tdet = _temporal_match(table, det["column"], target_col)
            prior = 0.35                       # 先验低于知识库候选，但证据硬
            base = (_w["evidence"] * ev
                + _w["prior"] * prior
                + _w["temporal"] * tscore)
            cands.append({
                "param_key": pk,
                "param": det.get("column"),
                "param_name": pspec.get("name", pk),
                "unit": pspec.get("unit", ""),
                "score": round(base, 4),     # 占位，post-annotation 重算为含可验证性的总分
                "evidence_score": round(ev, 3),
                "prior_score": round(prior, 3),
                "temporal_score": round(tscore, 3),
                "data_status": det.get("status"),
                "gate_note": None,
                "source": "数据驱动",
                "detail": det, "temporal": tdet,
                "note": pspec.get("note", ""),
                "history_case": None,
            })
    # 护栏：知识库先验候选若对应的是测试条件或非测量列，同样剔除
    # v1.10：目标列本身（或与目标同一参数键）不能作为它自己的原因
    _tgt_key = (col_map or {}).get(target_col) if target_col else None
    _excl = set(exclude_cols or [])
    cands = [d for d in cands
             if not (target_col and d.get("param") == target_col)
             and d.get("param") not in _excl
             and not (_tgt_key and d.get("param_key") == _tgt_key)
             and not (QG.is_test_condition(d.get("param") or "") or QG.is_test_condition(d.get("param_name") or "")
                     or d.get("param_key") in ("irradiance", "module_temp"))
             and (d.get("param") is None or QG.role_of(table, d.get("param")) == "measure")]
    if not cands:
        return None

    cands.sort(key=lambda d: -d["score"])
    _annotate_evidence(cands, table, target_col, info or {})

    # P1-5：可验证性权重此前硬编码 0.5、不算进排序、不随候选输出。
    # 现在统一按候选真实信号计算，并计入总分 + 输出分项（verifiability_score）。
    # 外部评审 P1-5（2026-10-29）：权重必须可配置、且**用了哪套要能被核对**，
    # 所以每条候选同时带 weights_used 与 score_breakdown——读者能自己乘一遍验算，
    # 而不是只能"信这个分数"。
    for c in cands:
        _ev = c.get("evidence_score") or 0.0
        _pr = c.get("prior_score") or 0.0
        _ts = c.get("temporal_score") or 0.0
        _vf = _verifiability(c)
        _parts = {
            "evidence": _w["evidence"] * _ev,
            "prior": _w["prior"] * _pr,
            "temporal": _w["temporal"] * _ts,
            "verifiability": _w["verifiability"] * _vf,
        }
        _final = sum(_parts.values())
        _final, _gate = _evidence_gate(_final, c.get("detail") or {})
        c["verifiability_score"] = round(_vf, 3)
        c["score"] = round(_final, 4)
        c["gate_note"] = _gate
        c["weights_used"] = {k: round(v, 4) for k, v in _w.items()}
        c["score_breakdown"] = {k: round(v, 4) for k, v in _parts.items()}
    # 排序分区：**在表里指得到列、且拿到数据支撑**的候选永远排在前，
    # 指不到列的纯先验候选一律在后。
    #
    # 不这么做的话，真因一旦不够显著，纯先验候选就会顶到榜首
    # （外部评审 E07 的"浆料固含 0.24"就是这么来的）。
    # 保留而不是删除：它们仍是知识库对该缺陷的常见原因，只是本批数据里没有对应列，
    # 属于"该去核实"而不是"本次结论"——报告里按 data_status 区分呈现。
    def _rank_key(d):
        _backed = bool(d.get("param")) and d.get("data_status") == ST.SUCCESS
        return (0 if _backed else 1, -d["score"])
    cands.sort(key=_rank_key)

    # 数据充分性：**排序这件事本身**能不能站得住，和每条候选的证据，是两回事。
    # 一条候选可以诚实地标着"机理支持"，但如果整张表只有两行，
    # 那这份排序整体就不能被当成"本轮数据的结论"来读。
    suf = _data_sufficiency(table, target_col, cands)

    # ---- 一条候选都没拿到数据支撑时，不得输出根因榜单 ----------------------
    #
    # 外部评审 E07 抓的就是这里：候选全靠 prior 那一项权重排出来，
    # status 虽然标了 NO_FINDING，榜单却照出不误（浆料固含 0.24、浆料粘度 0.22…），
    # 数字规整、措辞专业，现场照着去调机，真因一点没动。
    # **状态与结论必须一致**：既然都说"没查到"，就不该同时递上一份 Top 榜单。
    #
    # 候选不丢——它们仍是知识库对该缺陷的"常见候选"，只是显式降级为
    # 未经本批数据验证的线索（prior_only_hints），不得当作本批数据的结论。
    # 注意 all_candidates 同样要清空：views.py 有 `candidates or all_candidates`
    # 的兜底取法，只清前者会被那条路径原样泄露出去。
    if not suf.get("n_data_backed"):
        # 只要零候选拿到数据支撑（无论样本够不够/有没有目标列），就不得输出根因榜单。
        # 原守卫叠加了 n_effective>=MIN_N 的门槛，漏掉了 target_col=None 与 n<8 的
        # INSUFFICIENT_DATA 场景——这两类同样"没查到"，却仍吐出带分数的 Top 榜单，
        # 下游 views.py 因 honest_degraded=None 把假榜单渲染进报告（评审 E07 同类缺陷）。
        return {
            "defect": defect_name,
            "process": proc,
            "process_name": kb["processes"].get(proc, {}).get("name", "") if proc else "",
            "mechanism": (info or {}).get("mechanism"),
            "verify_data": (info or {}).get("verify_data", []),
            "quick_checks": (info or {}).get("quick_checks", []),
            "candidates": [],
            "all_candidates": [],
            "prior_only_hints": [{
                "param_key": c.get("param_key"),
                "param_name": c.get("param_name"),
                "unit": c.get("unit") or "",
                "source": c.get("source"),
                "why_not_conclusion": "未经本批数据验证（%s），只作线索，不得据此调机"
                                      % (c.get("gate_note") or "该候选未拿到数据支持"),
            } for c in cands],
            "history_cases": [hist[k] for k in hist],
            "status": suf["status"],
            "data_sufficiency": suf,
            "honest_degraded": True,
            "what_to_provide": [
                "至少一个可被认出的工艺参数列（标准列名，或贵厂通用叫法）",
                "该参数的规格上下限或受控出处",
                "若为自定义列名：登记设备档案/字段映射（列名→参数名→量纲）",
            ],
            "score_note": "本批数据未能支撑任何根因候选，故不给出排序；"
                          "prior_only_hints 为知识库常见候选，属待核实线索而非结论",
            # 即便没有榜单，也如实交代"如果排序会用哪套权重"，便于复核配置是否生效
            "weights_used": {k: round(v, 4) for k, v in _w.items()},
            "weights_source": _wsrc,
        }
    return {
        "defect": defect_name,
        "process": proc,
        "process_name": kb["processes"].get(proc, {}).get("name", "") if proc else "",
        "mechanism": (info or {}).get("mechanism"),
        "verify_data": (info or {}).get("verify_data", []),
        "quick_checks": (info or {}).get("quick_checks", []),
        "candidates": cands[:top],
        "all_candidates": cands,
        "history_cases": [hist[k] for k in hist],
        "status": suf["status"],
        "data_sufficiency": suf,
        # P1-5：明确权重是排序启发式而非概率，避免被读成"该参数确为根因的概率"。
        # 权重数字**由实际使用的权重生成**，不手写常数——否则一旦改成配置文件，
        # 这句话就会继续报着旧数字，变成一条看起来精确的假信息。
        "weights_used": {k: round(v, 4) for k, v in _w.items()},
        "weights_source": _wsrc,
        "score_note": weights_note(_w, _wsrc) + "；不代表该参数确为根因的概率，"
                       "各候选可验证性分项见 verifiability_score、分项贡献见 score_breakdown",
    }


# 排序这件事本身所需的最低有效样本量。
# 依据：n 小于此值时相关系数本身极不稳定（几个点就能摆出任意斜率），
# 所谓"排序"实际是知识库先验的复读机，与数据无关。
# 它不是统计学推出的精确门限，是「低于它就必须明着说站不住」的工程约定，
# 所以写成具名常量，不允许在各处散落成字面量再各改各的。
MIN_N_FOR_RANKING = 8


def _data_sufficiency(table, target_col, cands):
    """评估"本次排序"整体能否声称由本轮数据得出。

    返回 {status, n_rows, n_effective, target_present, n_data_backed, reason}。
    status 取值用 lib.status 统一枚举，不允许在这里自造第四种写法。
    """
    rows = 0
    try:
        rows = len(getattr(table, "rows", None) or [])
    except Exception as exc:
        degrade.note("rootcause.sufficiency@rows", exc)

    target_present = False
    if target_col:
        try:
            vals = table.numeric_column(target_col) or []
            target_present = any(v is not None for v in vals)
        except Exception as exc:
            degrade.note("rootcause.sufficiency@target", exc)

    n_effective = rows
    if target_col and target_present:
        try:
            from . import stats as S
            # 走 finite_list 而不是手写 `is not None`：
            # NaN/inf 既不是 None 也不是有效观测值，行数会把它们算进去，
            # 于是"180 行数据"里真正能用的可能只有 12 个。
            n_effective = len(S.finite_list(table.numeric_column(target_col) or []))
        except Exception as exc:
            degrade.note("rootcause.sufficiency@finite", exc)

    n_data_backed = sum(1 for c in (cands or []) if c.get("supported_by_data"))

    if rows == 0:
        return {"status": ST.INSUFFICIENT_DATA, "n_rows": 0, "n_effective": 0,
                "target_present": False, "n_data_backed": 0,
                "reason": "数据表为空（0 行），本次结果全部来自知识库先验，"
                          "**不能当作这批数据的分析结论**"}
    if not target_present:
        return {"status": ST.INSUFFICIENT_DATA, "n_rows": rows, "n_effective": 0,
                "target_present": False, "n_data_backed": n_data_backed,
                "reason": "目标列「%s」在本轮数据里没有可用数值，"
                          "所有候选均无法用数据验证" % (target_col or "未指定")}
    if n_effective < MIN_N_FOR_RANKING:
        return {"status": ST.INSUFFICIENT_DATA, "n_rows": rows,
                "n_effective": n_effective, "target_present": True,
                "n_data_backed": n_data_backed,
                "reason": "有效样本仅 %d 个（低于 %d），相关系数不稳定，"
                          "本次排序顺序不可靠，只能作线索清单用"
                          % (n_effective, MIN_N_FOR_RANKING)}
    if n_data_backed == 0:
        return {"status": ST.NO_FINDING, "n_rows": rows, "n_effective": n_effective,
                "target_present": True, "n_data_backed": 0,
                "reason": "数据已够（%d 个有效样本），但没有任何候选拿到数据支持——"
                          "真因可能不在知识库收录范围内，"
                          "而不是「检查过、都不是原因」" % n_effective}
    return {"status": ST.SUCCESS, "n_rows": rows, "n_effective": n_effective,
            "target_present": True, "n_data_backed": n_data_backed,
            "reason": ""}


def _annotate_evidence(cands, table, target_col, info, confound_top=3):
    """给候选补上"这条结论凭什么、差什么、怎么验"（审计报告 P1-5 / P1-6）。

    为什么不放在构造候选时就带上：混杂检查要重扫全表，给几十个候选都跑一遍
    会把排序阶段拖成分钟级。现场真正会被送去验证的只有前几条，
    所以混杂只做前 confound_top 条，**并如实标注其余未检查**。
    ——"没检查"必须写出来，不写出来就会被读成"检查过、没问题"。

    结论状态只有四种取值：数据证实 / 机理支持 / 现场待确认 / 未知。
    这条口径来自报告要求，不许为了好看改成五级六级。
    """
    for i, c in enumerate(cands):
        d = c.get("detail") or {}
        missing, counter = [], []
        st = d.get("status")

        if st == ST.INSUFFICIENT_DATA:
            missing.append("本轮数据里没有「%s」的有效数据列，"
                           "这一条目前**没有数据证据**（不是证据为负，是没有证据）"
                           % c.get("param_name"))
            data_backed = False
        elif st == ST.SUCCESS and d.get("r") is not None:
            r, p = d.get("r"), d.get("p")
            if abs(r) >= 0.15 and (p is None or p <= 0.05):
                data_backed = True
            else:
                data_backed = False
                counter.append("统计上未见关联（r=%.3f，p=%s），"
                               "这条不是被数据否定的因果关系，"
                               "而是「本轮数据看不出这条線索」"
                               % (r, ("%.4g" % p) if p is not None else "—"))
        else:
            missing.append("该参数未取到可分析的数据列")
            data_backed = False

        beforeafter = (c.get("temporal") or {})
        if beforeafter.get("verdict") == "不支持":
            counter.append("时序上不支持：%s" % beforeafter.get("note", ""))

        c["missing_evidence"] = missing
        c["counter_evidence"] = counter
        c["is_hypothesis"] = (not data_backed)
        c["supported_by_data"] = data_backed

        if data_backed:
            c["conclusion_status"] = "数据支持"
        elif c.get("source") == "知识库先验" or c.get("history_case"):
            c["conclusion_status"] = "机理支持"
        elif (c.get("quick_checks") or info.get("quick_checks")):
            c["conclusion_status"] = "现场待确认"
        else:
            c["conclusion_status"] = "未知"

        # 验证步骤：知识库里写过的现场动作直接用；没写过的只给流程骨架，
        # **不替任何工艺编具体做法**（跨工序照抄做法是会被现场笑话的那种错）。
        steps = list(info.get("quick_checks") or [])
        gen = []
        if data_backed and target_col:
            gen.append("复算：用同一份数据按班次/设备/批次分层重算该项相关关系，"
                       "确认不是由某一批数据单独撑起来的")
        if st == ST.INSUFFICIENT_DATA:
            gen.append("补数据：补齐「%s」的测量列后重跑，缺列情况下无法验证"
                       % c.get("param_name"))
        gen.append("现场核对：确认该参数的量纲、采样位置与报告假定一致后再下结论")
        seen = set()
        all_steps = []
        for s in steps + gen:
            if s and s not in seen:
                seen.add(s)
                all_steps.append(s)
        c["verification_steps"] = all_steps

        # 验证成本只按"数据要不要重采"分级，不猜现场工时：
        # 猜出来的数字一旦被当成承诺，就是给使用者挖坑。
        if st == ST.INSUFFICIENT_DATA:
            c["verification_cost"] = "需补数据（成本由 sampling 方案决定，此处不估计）"
        elif data_backed:
            c["verification_cost"] = "低：现有数据即可复核"
        else:
            c["verification_cost"] = "中：需现场取数确认"

        if i < confound_top and c.get("param") and target_col:
            try:
                cf = CF.check(table, c["param"], target_col)
                c["confounders"] = cf.get("confounders") or []
                c["confound_lines"] = CF.summary_lines(cf)
                c["confound_limitation"] = (
                    "；".join(cf.get("limitations") or []))
                c["confound_technique"] = cf.get("technique") or []
                strong = [x for x in (cf.get("confounders") or [])
                          if x.get("verdict", "").startswith(("高度怀疑", "强警告"))]
                if strong:
                    c["conclusion_status"] = "数据支持（混杂未排除）"
                    c.setdefault("warnings", []).append(
                        "混杂嫌疑：%s —— 结论需按该列分层复核，"
                        "在未排除前不得按此条下调整机指令" % strong[0]["column"])
            except Exception as _cf_e:
                degrade.note("rootcause.confound@%s" % c.get("param_key"), _cf_e)
                c["confounders"] = []
                c["confound_unavailable"] = "混杂检查未跑成（%s）：**未检查，不等于没有混杂**" % _cf_e
        else:
            c["confounders"] = None
            c["confound_note"] = ("本条未做混杂检查（只对评分前 %d 条做，"
                                  "为的是不把排序拖成分钟级）；**未检查 ≠ 没有混杂**"
                                  % confound_top)


# ------------------------------------------------------------------ 5Why

def _unregistered_hint(defect_name, layer="defect"):
    """缺陷未收录时的可行动提示（分「工序层」与「机理层」两种措辞）。

    只回一句「机理未收录」等于把问题原样丢回给工程师 —— 他早就看见了，
    他真正需要知道的是两件事：
      1. 这份报告还能信到哪一步（答：第 3 层以下的数据证据不受影响）
      2. 怎么把这条补进知识库（答：给可直接复制的命令）

    两层用不同措辞：第 1 层说「定位不到工序」，第 2 层说「机理缺失」，
    否则工程师会看到同一句话印两遍，反而以为报告出错了。
    """
    if layer == "process":
        return ("「%s」尚未收录进根因知识库，无法自动定位所属工序，"
                "故本层无工序机理可引用。可运行时用 `--process <工序>` 显式指定，"
                "或按第 2 层提示补录该缺陷条目。" % defect_name)
    return ("「%s」尚未收录进根因知识库，机理先验缺失。"
            "但请注意：第 3 层以下的数据证据不受影响 —— 关联强度、分组差异、"
            "时序吻合度都是从你的数据直接算出的，仍然可用。"
            "补录方式：先 `python scripts/kb_manage.py scaffold --type defect_new` 取骨架，"
            "填 key / process / mechanism 后 propose，"
            "再由负责人 review --approve 并 sync 生效。" % defect_name)


def build_5why(defect_name, top_causes, table=None, target_col=None):
    """生成 5Why 链条（基于知识库机理 + 数据事实），并标注哪些层有数据支撑"""
    kb = _kb()
    info = kb["defect_rootcause"].get(defect_name, {})
    proc = kb["processes"].get(info.get("process", ""), {})
    lead = top_causes[0] if top_causes else None
    chain = []
    pnotes = proc.get("notes")
    known = bool(info)
    # 未收录时不能只回「机理未收录」——那是把问题原样丢回给工程师。
    # 要同时说清：本报告剩余部分还能信到哪一步、以及怎么把这条补上。
    fallback = _unregistered_hint(defect_name, "process") if not known else \
        "该工序的工艺机理尚未在知识库中细化"

    chain.append({
        "level": 1,
        "why": "为什么出现「%s」？" % defect_name,
        "because": pnotes or fallback,
        "evidence": "知识库工序机理" if pnotes else "知识库未收录（可补录）",
        "supported": bool(pnotes),
    })
    chain.append({
        "level": 2,
        "why": "为什么该工序会出现这种状态？",
        "because": info.get("mechanism") or _unregistered_hint(defect_name),
        "evidence": "知识库缺陷机理",
        "supported": bool(info.get("mechanism")),
    })
    if lead:
        d = lead.get("detail", {})
        ev = []
        if d.get("r") is not None:
            ev.append("与目标相关性 r=%.3f (p=%.4g, n=%d)" % (d["r"], d.get("p") or 0, d.get("n", 0)))
        if d.get("oos_pct") is not None:
            ev.append("超规格 %.1f%%（%d 点）" % (d["oos_pct"], d.get("out_of_spec", 0)))
        if d.get("cv_pct") is not None:
            ev.append("CV=%.2f%%" % d["cv_pct"])
        if d.get("status") == ST.INSUFFICIENT_DATA:
            ev.append("该参数在当前数据中不存在，需补充：%s" % d.get("need"))
        chain.append({
            "level": 3,
            "why": "为什么指向「%s」？" % lead.get("param_name"),
            "because": "；".join(ev) if ev else "证据不足，需补充数据",
            "evidence": "数据证据（得分 %.2f）" % lead.get("score", 0),
            "supported": bool(ev) and d.get("status") != ST.INSUFFICIENT_DATA,
        })
        chain.append({
            "level": 4,
            "why": "为什么「%s」会发生异常？" % lead.get("param_name"),
            "because": lead.get("note", "") or "需结合设备状态、来料批次、操作变更追溯",
            "evidence": "参数机理（需现场确认）",
            "supported": bool(lead.get("note")),
        })
        chain.append({
            "level": 5,
            "why": "为什么没有被及时发现/拦截？",
            "because": "现行控制计划对该参数的监控频次/SPC 判异规则未覆盖，或拦截点设置在该工序之后",
            "evidence": "需核对 Control Plan 与检测点布局",
            "supported": False,
        })
    return chain


# ------------------------------------------------------------------ 数据缺口

def assess_gaps(table, defect_name=None, target_col=None, groups=None):
    """评估数据缺口，返回 P0/P1/P2 清单（用于向用户弹窗索取）"""
    kb = _kb()
    gaps = []
    # 时间戳（或可排序序列）
    tc, times = PP.parse_time_column(table)
    has_time = tc is not None and sum(1 for t in (times or []) if t) > 0
    if not has_time:
        gaps.append({"level": "P0", "item": "时间戳 / 批次序号",
                     "why": "无法做时序分析与滞后相关，无法定位异常起始点",
                     "impact": "高"})
    # 分组维度：优先使用流水线已识别的分组列，避免两处关键词表不一致造成漏检
    group_like = list(groups or [])
    if not group_like:
        group_like = [c for c in table.columns
                      if any(k in PP._norm_name(c) for k in
                             ["设备", "机台", "机号", "machine", "线体", "产线", "生产线", "line",
                              "班次", "shift", "批次", "batch", "lot", "卷号", "供应商",
                              "supplier", "通道", "channel", "柜号", "柜", "模", "腔", "tool",
                              "刀具", "刀号", "班组", "工单", "订单"])]
    if not group_like:
        gaps.append({"level": "P0", "item": "分组维度（设备号/批次/班次）",
                     "why": "无法做分层 Pareto 与组间 ANOVA，无法区分是全局问题还是单台设备问题",
                     "impact": "高"})
    # 目标指标
    if not target_col:
        gaps.append({"level": "P0", "item": "被分析指标（不良率/测量值）",
                     "why": "缺少响应变量，无法计算相关性",
                     "impact": "高"})
    # 工艺参数
    col_map = PP.map_columns(table)
    matched = [v for v in col_map.values() if v]
    if len(matched) < 2:
        gaps.append({"level": "P1", "item": "对应工序的工艺参数实测值",
                     "why": "当前可识别的标准工艺参数不足 2 个，根因只能给出知识库先验",
                     "impact": "中"})
    # 规格
    if defect_name:
        info = kb["defect_rootcause"].get(defect_name, {})
        for pk in info.get("likely_params", [])[:3]:
            spec = kb["params"].get(pk, {})
            ctrl = spec.get("control", {})
            if ctrl.get("mode") in ("usl", "lsl", "range"):
                continue
            gaps.append({"level": "P1",
                         "item": "%s 的企业规格上下限（USL/LSL）" % spec.get("name", pk),
                         "why": "知识库只提供行业典型值，用典型值算 Cpk 会失真",
                         "impact": "中"})
    # 历史基线
    if table and len(table) < 30:
        gaps.append({"level": "P2", "item": "历史基线数据（≥30 点）",
                     "why": "样本量 %d 偏少，控制限与能力指数置信度不足" % len(table),
                     "impact": "中"})
    return gaps


def confidence_level(gaps, evidence_count):
    """根据缺口与证据数给出置信度"""
    p0 = sum(1 for g in gaps if g["level"] == "P0")
    if p0 >= 2:
        return {"level": "低", "score": 0.3,
                "note": "关键维度缺失较多，结论以知识库先验为主，须现场验证"}
    if p0 == 1:
        return {"level": "中低", "score": 0.5,
                "note": "存在 1 项关键缺失，部分结论需现场确认"}
    if evidence_count >= 3:
        return {"level": "中高", "score": 0.75,
                "note": "数据维度基本齐备，有多项统计证据支撑"}
    return {"level": "中", "score": 0.6,
            "note": "数据维度齐备但统计证据偏少，建议补充样本后复算"}
