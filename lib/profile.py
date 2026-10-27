# -*- coding: utf-8 -*-
"""产品档与租户配置解析：多产品并存时，配置默认值怎么合并出一套能用的。

为什么要单独做这个模块
----------------------
一个租户只做一个产品时，"配置默认值"是一张平表，读出来直接用即可。
但综合性租户同时做两个以上产品时，问题立刻变成：

    产品 A 说边界无出处要阻断，产品 B 说只警告；
    产品 A 开 4 条判异规则，产品 B 开 6 条；
    产品 A 关注涂布面密度，产品 B 关注 EL 缺陷数。

这时候"叠加"（数值相加）是错的——两个产品的最小样本量相加没有任何含义；
"后面的覆盖前面的"更危险——谁赢取决于字典顺序，而不是取决于这个参数
该听谁的。后者正是 tenancy.deep_merge 的行为，它对"租户覆盖基线"是对的
（单一覆盖源），对"多个平级来源"就是静默抉择：系统替你选了一个，还不告诉你。

本模块的三条硬原则
------------------
1. **按语义合并，不按位置合并**。每个参数声明自己的合并语义
   （取严 / 取宽 / 取大 / 取小 / 取并集 / 交人决断），合并结果由语义决定，
   与产品书写顺序无关。
2. **语义之外的参数一律交人**。不在语义表里的参数发生冲突时不猜，
   标 needs_decision 并说清楚候选来自谁——"不知道就问"比"猜一个"安全。
3. **显式配置永远最高**。优先级 显式 > 角色 > 产品 > 基线。
   角色视角不得推翻使用人写死的值，那是越权；只能补他没有的部分。

参数值绝不内置
--------------
本文件只固化**合并语义与角色的方法论倾向**（逻辑，跨行业通用）。
某个产品具体取什么值，一律来自 knowledge/product_profiles.json（或租户层同名文件），
由企业侧按受控口径填写；没填就回落基线，不在这里替他编。
"""

import datetime
import io
import json
import os

try:
    from . import degrade
except Exception:      # pragma: no cover
    degrade = None

PRODUCT_PROFILE_FILE = os.path.join("knowledge", "product_profiles.json")


def _note(where, exc):
    if degrade is not None:
        try:
            degrade.note(where, exc)
            return
        except Exception:
            # scan:keep 降级台账本身就是"最后一处能记的地方"，它再失败已无处可记；
            # 配置解析是旁路能力，绝不能因为留痕失败而拖垮主流程。
            pass


# ------------------------------------------------------------------ 合并语义表
#
# 每条：参数 -> "怎么从多个候选里挑一个"
#   rank      按严度排序取最大（边界越严越靠后）
#   max/min   数值取最大/最小
#   set_union 集合取并集（规则、视图：多开一个不亏，漏一个要命）
#   first     取第一个（种子这类"身份值"，取大取小取并集都无意义）
#   manual    无法自动判定，交人（目标列、关注工序这类"关注对象"）
#
# 不在表里的参数，默认按 manual 处理：宁可让人选，也不替他选。
MERGE_SEMANTICS = {
    "boundary_policy": {
        "kind": "rank", "rank": {"warn": 0, "block": 1}, "pick": "max",
        "why": "无受控出处时是否阻断：取严。多产品并存时只要有一个要求阻断，"
               "就必须阻断——放行一个没出处的产品，等于给整批报告开了口子",
    },
    "spc_exclusion_mode": {
        "kind": "rank", "rank": {"auto": 0, "suggest": 1, "review": 2}, "pick": "max",
        "why": "剔点是否需人审核：取严。auto 是工具自己决定剔谁，"
               "review 是工具只提名、人来定；涉及基线可信度时不让工具自己说了算",
    },
    "rules": {
        "kind": "set_union",
        "why": "判异规则取并集：多报一条是提醒，漏报一条是事故，宁可多报",
    },
    "view": {
        "kind": "set_union",
        "why": "角色视图取并集：同一份结果要给工艺、质量、现场三种人看，不必互相裁剪",
    },
    "doe_replicates": {
        "kind": "max",
        "why": "重复数取大：重复样越多功效越足，省下的那几次试验换不来结论可信度",
    },
    "doe_blocks": {
        "kind": "max",
        "why": "区组数取大：区组划得更细，跨班次/跨设备的干扰更不容易混进效应估计",
    },
    "doe_seed": {
        "kind": "first",
        "why": "随机种子取第一个：它是身份值不是量值，取大取小都无意义，"
               "但必须稳定——同一组合每次算出来要一样，否则无法复现",
    },
    "target": {
        "kind": "manual",
        "why": "分析对象（目标列）各产品不同，无法自动取：取了就是替使用人决定看什么",
    },
    "process": {
        "kind": "manual",
        "why": "关注工序各产品不同，必须由使用人指名",
    },
    "defect": {
        "kind": "manual",
        "why": "关注缺陷各产品不同，必须由使用人指名",
    },
}

DEFAULT_SEMANTIC = {
    "kind": "manual",
    "why": "该参数未声明合并语义，冲突时不猜：候选来源不同即交使用人决断",
}


def semantics_of(param):
    return MERGE_SEMANTICS.get(param, DEFAULT_SEMANTIC)


# ------------------------------------------------------------------ 角色视角
#
# 角色不是改物理参数，是改**判严尺度与呈现重点**。
# 同一份数据，工艺工程师要灵敏度（早点看见异常），
# 质量工程师要证据链（出去的东西经得起问），
# 检测分析/设备工程师要量纲与分辨力（数本身站得住）。
#
# 这里给的是**方法论倾向**，跨产品通用，所以可以内置；
# 每条都带 reason，让使用人知道为什么被这么配，而不是黑箱。
#
# 前缀 "metrology" 是本轮补的：现有视图体系（process/shopfloor/quality/
# production/mgmt）里没有"检测分析/设备工程师"的位置，而这类使用者的关注点
# （量纲、分辨力、测量系统噪声）与工艺、质量都不同，混在一起会被稀释。
ROLE_LABEL = {
    "process": "工艺工程师",
    "shopfloor": "现场技术人员",
    "quality": "质量管理人员",
    "production": "生产主管",
    "mgmt": "客户/管理层",
    "metrology": "检测分析/设备工程师",
}

ROLE_PROFILES = {
    "process": {
        "rules": {"value": "1,2,3,5,6",
                  "reason": "工艺视角要灵敏度：在基本集上补规则 6（连续 5 点中 4 点落 B 区外），"
                            "均值刚起步偏移时就能看见，不等到 9 点同侧才报警"},
        "spc_exclusion_mode": {"value": "suggest",
                               "reason": "工艺视角先看后剔：可疑点先提名给工程师确认，"
                                         "避免工具把一段真实的工艺偏移当成异常剔掉"},
        "view": {"value": "process,shopfloor",
                 "reason": "工艺工程师要参数建议，现场要能打印带走的作业单"},
    },
    "quality": {
        "boundary_policy": {"value": "block",
                            "reason": "质量视角要证据链：边界没有受控出处就不出方案，"
                                      "否则 Cpk 拿去答客户时经不起追问"},
        "spc_exclusion_mode": {"value": "review",
                               "reason": "剔点必须留审核人与理由：基线被谁改过要说得出，"
                                         "这是审核时第一个被问的问题"},
        "rules": {"value": "all",
                  "reason": "质量视角宁可多报：漏判的代价远高于复核几条误报"},
        "doe_replicates": {"value": 2,
                           "reason": "无重复的试验估不出纯误差，功效与置信区间都无从谈起"},
        "view": {"value": "quality,production",
                 "reason": "质量要合规检查，生产要批次处置建议并留判断记录"},
    },
    "metrology": {
        "boundary_policy": {"value": "block",
                            "reason": "计量视角不认无出处的边界：量纲与量程未声明时，"
                                      "任何「合格/不合格」判定都是假判定"},
        "doe_replicates": {"value": 3,
                           "reason": "重复样用于估计测量系统噪声与分辨力："
                                     "分辨力不足时，再多的试验也分不出因子效应"},
        "view": {"value": "process,quality",
                 "reason": "检测分析工程师既要知道参数怎么调，也要知道判定怎么下的"},
    },
}

ROLE_KEYS = tuple(sorted(set(list(ROLE_LABEL.keys()))))


# ------------------------------------------------------------------ 受控规格证据
#
# 边界政策为什么不该留给使用者拍板
# --------------------------------
# 「企业侧一旦给了受控规格文件，就该收紧成 block」——这句话里真正需要人做的
# 只有一件事：**登记那份文件**（编号 / 版本 / 生效日）。政策是登记的推论，
# 不是他的选项。把政策做成旋钮交给他转，是把技术判断抛回给非专业的使用者：
# 他不知道什么时候该转，转晚了报告已经出去了，转早了连正常探索都被拦下，
# 而工具全程看着那份受控文件躺在目录里、知道自己本该收紧。
#
# 所以本模块固化的是**推导逻辑**（有受控生效证据 → block），
# 不固化任何产品的文件编号——那些是企业侧录入的客观事实，一律来自外部登记。
#
# 反向同样重要：只把状态改成 active 却说不出文件编号，不算受控。
# 否则"我说是受控的就是受控的"，正是政策旋钮本来要防的事。
CONTROLLED_SPEC_STATUS = ("absent", "draft", "active")


def _spec_usable(spec):
    """一份登记算不算"受控生效"：状态为 active **且** 说得出文件编号。"""
    if not isinstance(spec, dict):
        return False
    if spec.get("status") != "active":
        return False
    if not str(spec.get("doc_id") or "").strip():
        return False
    return True


def _spec_in_scope(spec, products):
    """这份登记是否管辖本次涉及的产品。scope 为空或 * 表示全域适用。"""
    sc = str(spec.get("scope") or "").strip()
    if not sc or sc in ("*", "all"):
        return True
    if not products:
        return True
    return sc in [str(p) for p in products]


def _spec_expired(spec, today=None):
    """过了复核期的受控文件：仍按最严处置，但必须提醒——否则它会被无限期沿用。"""
    rb = str(spec.get("review_by") or "").strip()
    if not rb:
        return False
    try:
        d = datetime.date.fromisoformat(rb[:10])
    except Exception:
        return False
    return d < (today or datetime.date.today())


def collect_controlled_specs(products=(), profiles=None, extra=(), root=None):
    """汇总本次适用的受控规格登记：产品档内的 + 使用人临时传入的。"""
    pr = profiles if profiles is not None else load_profiles(root)
    out = []
    for p in (products or []):
        e = (pr.get("products") or {}).get(p)
        if not isinstance(e, dict):
            continue
        cs = e.get("controlled_specs")
        if cs is None:
            one = e.get("controlled_spec")
            cs = [one] if isinstance(one, dict) else []
        for s in (cs or []):
            if isinstance(s, dict):
                d = dict(s)
                d.setdefault("scope", p)
                out.append(d)
    for s in (extra or []):
        if isinstance(s, dict):
            out.append(s)
    return out


def load_controlled_spec(src):
    """把使用人给的受控规格登记归一化成列表。

    接受：list[dict] / 单份 dict / {"controlled_specs": [...]} / JSON 文件路径。

    **读不出来时返回 None，不返回 []**：这两者后果完全不同。
    "企业没给受控文件" → 政策 warn，是事实；
    "企业给了但文件读不出来" → 也变 warn，是事故——
    使用人看着松掉的政策，只会以为自家规格还没纳入，
    不会想到是路径写错了。返回 None 让调用方必须出声。
    """
    if src is None:
        return []
    if isinstance(src, (list, tuple)):
        return [s for s in src if isinstance(s, dict)]
    if isinstance(src, dict):
        ls = src.get("controlled_specs")
        if isinstance(ls, list):
            return [s for s in ls if isinstance(s, dict)]
        return [src] if (src.get("doc_id") or src.get("status")) else []
    if isinstance(src, str):
        try:
            with io.open(src, encoding="utf-8") as f:
                obj = json.load(f)
        except Exception as _e:
            _note("profile.load_controlled_spec", _e)
            return None
        if isinstance(obj, list):
            return [s for s in obj if isinstance(s, dict)]
        if isinstance(obj, dict):
            ls = obj.get("controlled_specs")
            if isinstance(ls, list):
                return [s for s in ls if isinstance(s, dict)]
            return [obj]
        return None
    return None


def derive_boundary_policy(specs, products=(), today=None):
    """从受控规格证据推边界政策 -> (policy, 理由, 已过复核期的文件编号列表)

    三条推论，全部基于登记事实，不含价值判断：
      active 且说得出编号 -> block（缺受控出处的边界一律阻断）
      只有 draft          -> warn（有文件但未生效，按无受控计，不得冒充受控）
      未登记 / 全 absent  -> warn（无出处，警告并要求书面确认）

    多产品并存时取严：只要其中一个产品有受控生效规格，整批按 block。
    与 MERGE_SEMANTICS 里 boundary_policy 的"取严"语义一致——
    这里只是把"取严"的判据从"产品写了什么"换成"证据支持什么"。
    """
    items = [s for s in (specs or []) if isinstance(s, dict)]
    ins = [s for s in items if _spec_in_scope(s, products)]
    usable, drafts, nameless, issues = [], [], [], []
    for s in ins:
        _sid = str(s.get("doc_id") or "").strip() or "(未编号)"
        if _spec_usable(s):
            usable.append(s)
            if _spec_expired(s, today):
                issues.append({"doc_id": _sid, "kind": "expired",
                               "text": "受控规格 %s 已过复核期（review_by 早于今天）："
                                       "政策仍按最严处置，但结论出具前需先复核" % _sid})
            continue
        _st = str(s.get("status") or "absent")
        if _st == "active":
            # 状态改成 active 却说不出编号：这不是"没登记"，是"登记得不成立"。
            # 两者都导致 warn，但混为一谈就等于给了伪造受控一条通路：
            # 把状态一改就"受控"了，还说不清凭什么。
            nameless.append(s)
            issues.append({"doc_id": _sid, "kind": "no_doc_id",
                           "text": "受控规格登记状态为 active 但未填 doc_id，不按受控计"
                                   "（说不出文件编号的「受控」不算受控）：请先补登记文件编号"})
        elif _st == "draft":
            drafts.append(s)
    if usable:
        why = ("受控规格已生效（%s）：缺受控出处的边界一律阻断，不出方案"
               % "、".join(str(s.get("doc_id") or "(未编号)") for s in usable))
        _exp = [i["doc_id"] for i in issues if i.get("kind") == "expired"]
        if _exp:
            why += ("；⚠ 其中 %s 已过复核期，政策仍按最严处置，但结论出具前需先复核"
                    % "、".join(_exp))
        return ("block", why, issues)
    if nameless:
        return ("warn", "登记了 %d 份受控规格但都未填文件编号，不按受控计："
                        "仅警告并要求书面确认，请先补登记 doc_id" % len(nameless), issues)
    if drafts:
        return ("warn",
                "已登记规格文件（%s）但状态为 draft/未生效，不按受控计："
                "仅警告并要求书面确认"
                % "、".join(str(s.get("doc_id") or "(未编号)") for s in drafts),
                issues)
    return ("warn", "未登记受控规格：边界缺出处时仅警告并要求书面确认", issues)


# ------------------------------------------------------------------ 产品档读写

def _read_json(path):
    try:
        with io.open(path, encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception as _e:
        _note("profile._read_json", _e)
        return {}


def load_profiles(root=None):
    """读产品档。租户层同名文件优先（与 tenancy 的分层读法一致）。"""
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = _read_json(os.path.join(root, PRODUCT_PROFILE_FILE))
    out = {"base": base, "tenant": {}, "products": {}}
    for src in ("base", "tenant"):
        sec = (out[src] or {}).get("products") or {}
        for k, v in sec.items():
            if not isinstance(v, dict):
                continue
            merged = dict(out["products"].get(k) or {})
            merged.update(v)          # 租户层覆盖基线层：单一覆盖源，可以覆盖
            out["products"][k] = merged
    return out


def defaults_of(product, profiles=None):
    """取某个产品的配置默认值。没配过就是空——空不是错，是"回落基线"。"""
    pr = profiles if profiles is not None else load_profiles()
    e = (pr.get("products") or {}).get(product)
    if not isinstance(e, dict):
        return {}
    d = e.get("defaults")
    return d if isinstance(d, dict) else {}


# ------------------------------------------------------------------ 规则集归一化

def _rules_to_set(spec):
    """规则串/预设名 -> 编号集合。解析不了就按字符串原样保留（不静默丢）。"""
    if spec is None:
        return None
    try:
        from . import spc as SPC
        t = SPC.parse_rules(spec)
        return set(int(x) for x in t)
    except Exception as _e:
        _note("profile._rules_to_set", _e)
        return None


def _rules_to_str(rs):
    """编号集合 -> 尽量用预设名表达，表达不了就排序列号。"""
    if rs is None:
        return None
    nums = sorted(int(x) for x in rs)
    if not nums:
        return None
    try:
        from . import spc as SPC
        for name, tup in (getattr(SPC, "RULE_PRESETS", None) or {}).items():
            if sorted(int(x) for x in tup) == nums:
                return name
    except Exception as _e:
        _note("profile._rules_to_str", _e)
    return ",".join(str(x) for x in nums)


def _split_set(v):
    if isinstance(v, (list, tuple, set)):
        return set(str(x).strip() for x in v if str(x).strip())
    if v is None:
        return set()
    return set(p.strip() for p in str(v).replace(";", ",").split(",") if p.strip())


# ------------------------------------------------------------------ 合并核心

def _merge_candidates(param, cands):
    """cands: [(来源, 值), ...] -> (值 or None, 语义, 说明, 是否需人决断)

    返回 value=None 且 needs_decision=True 时，调用方不得自行填空。
    """
    sem = semantics_of(param)
    kind = sem.get("kind")
    vals = [(s, v) for s, v in cands if v is not None]
    if not vals:
        return (None, kind, "无候选值", False)
    uniq = []
    for _s, v in vals:
        if v not in [x[1] for x in uniq]:
            uniq.append((_s, v))
    if len(uniq) == 1:
        return (uniq[0][1], kind, "唯一候选（来自 %s）" % uniq[0][0], False)

    if kind == "manual":
        detail = "；".join("%s 给 %r" % (s, v) for s, v in uniq)
        return (None, kind, "各来源不一致且该参数无法自动取：%s" % detail, True)

    if kind == "rank":
        rank = sem.get("rank") or {}
        pick = sem.get("pick", "max")
        known = [(s, v) for s, v in uniq if v in rank]
        unknown = [(s, v) for s, v in uniq if v not in rank]
        if not known:
            return (None, kind, "取值都不在严度表内，无法判定谁更严：%s"
                    % "；".join("%s 给 %r" % (s, v) for s, v in uniq), True)
        best = max(known, key=lambda x: rank[x[1]]) if pick == "max" \
            else min(known, key=lambda x: rank[x[1]])
        note = "取严（候选：%s）" % "、".join("%s=%s" % (s, v) for s, v in uniq)
        if unknown:
            note += "；⚠ %s 给的 %s 不在严度表内，已按已知项判定，需复核" % (
                "、".join(s for s, _ in unknown), "、".join(repr(v) for _, v in unknown))
        return (best[1], kind, note, bool(unknown))

    if kind in ("max", "min"):
        nums, bad = [], []
        for s, v in uniq:
            try:
                nums.append((s, float(v)))
            except Exception:
                bad.append((s, v))
        if not nums:
            return (None, kind, "取值都不是数值，无法比较大小：%s"
                    % "；".join("%s 给 %r" % (s, v) for s, v in uniq), True)
        pick = max if kind == "max" else min
        _s, val = pick(nums, key=lambda x: x[1])
        if val == int(val):
            val = int(val)
        note = "取%s（候选：%s）" % ("大" if kind == "max" else "小",
                                 "、".join("%s=%s" % (s, v) for s, v in uniq))
        if bad:
            note += "；⚠ %s 给的不是数值，未参与比较" % "、".join(s for s, _ in bad)
        return (val, kind, note, bool(bad))

    if kind == "set_union":
        if param == "rules":
            sets = []
            bad = []
            for s, v in uniq:
                rs = _rules_to_set(v)
                if rs is None:
                    bad.append((s, v))
                else:
                    sets.append((s, rs))
            if not sets:
                # 解析不了也要给个说法，不能把规则丢掉
                merged = set()
                for _s, v in uniq:
                    merged |= _split_set(v)
                return (",".join(sorted(merged)) or None, kind,
                        "规则串无法按预设解析，已按字面取并集，需复核", True)
            u = set()
            for _s, rs in sets:
                u |= rs
            out = _rules_to_str(u)
            note = "取并集（%s → %s）" % (
                "、".join("%s=%s" % (s, v) for s, v in uniq), out)
            if bad:
                note += "；⚠ %s 给的规则串无法解析，未并入" % "、".join(s for s, _ in bad)
            return (out, kind, note, bool(bad))
        u = set()
        for _s, v in uniq:
            u |= _split_set(v)
        joined = ",".join(sorted(u))
        return (joined or None, kind,
                "取并集（%s → %s）" % ("、".join("%s=%s" % (s, v) for s, v in uniq), joined),
                False)

    if kind == "first":
        return (uniq[0][1], kind,
                "取第一个（候选：%s；该参数是身份值，取大取小都无意义）"
                % "、".join("%s=%s" % (s, v) for s, v in uniq), False)

    return (None, kind, "未知合并语义 %r，交人决断" % kind, True)


# ------------------------------------------------------------------ 解析入口

def resolve_config(products=(), role=None, explicit=None, baseline=None,
                   profiles=None, root=None, controlled_specs=None):
    """把"多个产品 + 一个角色 + 使用人显式指定"解析成一套配置。

    products  产品（行业域）标识列表，如 ("battery", "pv")
    role      使用者角色：process / quality / metrology / ...
    explicit  使用人显式指定的值，优先级最高，任何情况下不得被覆盖
    baseline  基线默认值，由调用方传入（本模块不内置，避免手抄失同步）

    返回 dict：
      config       最终配置（需人决断的参数**不出现在这里**）
      trace        逐参数来源与判定理由（给使用人看"为什么是这个值"）
      needs_decision  必须由使用人指定的参数清单
      conflicts    自动合并时发现的可疑之处（如取值不在严度表内）
      notes        该怎么读这份结果

    关键约定：config 里少一项，是因为"没人能替他定"，不是因为漏了。
    """
    products = [p for p in (products or []) if p]
    explicit = explicit if isinstance(explicit, dict) else {}
    baseline = baseline if isinstance(baseline, dict) else {}
    pr = profiles if profiles is not None else load_profiles(root)

    cands = {}          # param -> [(来源, 值), ...]
    param_order = []

    def _add(param, src, val):
        if val is None:
            return
        if param not in cands:
            cands[param] = []
            param_order.append(param)
        cands[param].append((src, val))

    for p in baseline:
        _add(p, "基线", baseline[p])
    for p in products:
        for k, v in (defaults_of(p, pr) or {}).items():
            _add(k, "产品 %s" % p, v)

    # 受控规格证据：政策由登记事实推导，不让人拍板。
    # 没有登记时也要给结论（derive 回落 warn），否则"未登记"和"登记失败"
    # 在结果里长得一模一样——都只是没有 block，看不出是没配还是配了没生效。
    _ev_reason, _ev_issues = {}, []
    _pol, _ev_why = None, ""
    # 连产品都没指定时不推导：那时候既没有登记可查，"按 warn 处理"也是句空话，
    # 凭空往配置里塞一项只会让"没配"和"配了"看起来一样。
    if products or controlled_specs:
        if controlled_specs is None:
            _specs = collect_controlled_specs(products, pr)
        else:
            # 临时传入的与产品档登记的一并作为证据：多一份受控文件只会更严，不会更松
            _specs = ([s for s in (controlled_specs or []) if isinstance(s, dict)]
                      + collect_controlled_specs(products, pr))
        _pol, _ev_why, _ev_issues = derive_boundary_policy(_specs, products)
        _ev_reason["boundary_policy"] = _ev_why
        # 证据只在**能收紧**时才发声。无受控登记时它也推出 warn，
        # 但把那个 warn 塞进候选是多余的（基线本就是 warn）——
        # 副作用是"没配"和"配了"在结果里长得一样，工具凭空替人选了政策。
        if _pol == "block":
            _add("boundary_policy", "受控规格证据", _pol)

    if role:
        for k, spec in (ROLE_PROFILES.get(role) or {}).items():
            if isinstance(spec, dict) and "value" in spec:
                _add(k, "角色 %s" % ROLE_LABEL.get(role, role), spec.get("value"))

    trace, needs, conflicts = [], [], []
    config = {}
    for param in param_order:
        srcs = cands[param]
        val, kind, why, suspicious = _merge_candidates(param, srcs)
        if param in explicit:
            ev = explicit[param]
            trace.append({"param": param, "value": ev, "semantics": kind,
                          "from": "使用人显式指定",
                          "candidates": {s: v for s, v in srcs},
                          "reason": "显式指定优先：已覆盖 %s"
                                    % "、".join("%s=%s" % (s, v) for s, v in srcs),
                          "decision": "explicit"})
            config[param] = ev
            continue
        if val is None:
            needs.append({"param": param, "semantics": kind,
                          "candidates": {s: v for s, v in srcs},
                          "reason": why})
            continue
        reason = why
        if role and any(s.startswith("角色") for s, _ in srcs):
            spec = (ROLE_PROFILES.get(role) or {}).get(param)
            if isinstance(spec, dict) and spec.get("reason"):
                reason = "%s。角色理由：%s" % (why, spec["reason"])
        if any(s == "受控规格证据" for s, _ in srcs) and _ev_reason.get(param):
            # 证据依据必须能被看见：政策是算出来的，使用人有权知道凭什么收紧
            reason = "%s。证据依据：%s" % (reason, _ev_reason[param])
        trace.append({"param": param, "value": val, "semantics": kind,
                      "from": "、".join(s for s, _ in srcs),
                      "candidates": {s: v for s, v in srcs},
                      "reason": reason, "decision": "auto"})
        config[param] = val
        if suspicious:
            conflicts.append({"param": param, "value": val, "reason": why})

    notes = []
    if len(products) > 1:
        notes.append(
            "综合性租户（%d 个产品：%s）：配置按参数语义合并，与书写顺序无关——"
            "取严的取严、取并集的取并集、无法自动判定的不猜"
            % (len(products), "、".join(str(p) for p in products)))
    if needs:
        notes.append(
            "%d 个参数必须由使用人指定（%s）：这些参数各产品关注对象不同，"
            "工具替他选一个等于替他决定看什么，所以留空并点名"
            % (len(needs), "、".join(n["param"] for n in needs)))
    if conflicts:
        notes.append(
            "%d 个参数合并时出现可疑取值（不在严度表内 / 不是数值 / 规则串无法解析），"
            "已按可判定的部分合并，但需人工复核" % len(conflicts))
    _ev_note = _ev_reason.get("boundary_policy") or ""
    if _ev_note:
        notes.append(
            "边界政策由受控规格证据推导，无需使用人指定：%s。"
            "要换政策就改登记（文件编号/状态），不要在结果上事后推翻" % _ev_note)
    for _iss in _ev_issues:
        conflicts.append({"param": "boundary_policy", "value": _pol,
                          "reason": _iss.get("text") or ""})
    if role:
        notes.append(
            "已按「%s」视角施加判严尺度：角色只补使用人没指定的部分，"
            "不覆盖他写死的值" % ROLE_LABEL.get(role, role))
    if not products and not role and not explicit and not baseline:
        notes.append("未指定产品、角色与基线：结果为空属正常，配置全部回落基线")

    return {"config": config, "trace": trace, "needs_decision": needs,
            "conflicts": conflicts, "notes": notes,
            "products": list(products), "role": role,
            "boundary_evidence": {"policy": _pol,
                                  "reason": _ev_why or "未指定产品，未做受控规格判定",
                                  "issues": list(_ev_issues),
                                  "n_specs": len(_specs) if _pol else 0}}


# ------------------------------------------------------------------ 交叉搭配映射表

def cross_matrix(products, role=None, baseline=None, profiles=None,
                 max_products=6, root=None):
    """生成产品组合 → 配置的交叉映射表（含需人决断项）。

    组合按位掩码枚举，保证顺序稳定可复现；超过 max_products 个产品时拒绝，
    因为 2^n 会爆炸，而一张 128 行的表没人会看——不如让他先收敛产品范围。
    这张表是**算出来的**，不是手抄的：手抄的表必然与代码失同步，
    而失同步的配置表比没有表更糟（照着它配了一个早就改名的值还以为生效了）。
    """
    products = [p for p in (products or []) if p]
    if len(products) > int(max_products or 0):
        return {"ok": False,
                "error": "产品数 %d 超过上限 %d：组合数 %d 行无人会看，"
                         "请先收敛产品范围" % (len(products), max_products, 2 ** len(products) - 1),
                "rows": []}
    pr = profiles if profiles is not None else load_profiles(root)
    rows = []
    for mask in range(1, 1 << len(products)):
        combo = [products[i] for i in range(len(products)) if mask & (1 << i)]
        r = resolve_config(combo, role=role, baseline=baseline, profiles=pr)
        rows.append({"products": combo, "key": "+".join(combo),
                     "n_products": len(combo),
                     "config": r["config"],
                     "needs_decision": [n["param"] for n in r["needs_decision"]],
                     "conflicts": [c["param"] for c in r["conflicts"]]})
    return {"ok": True, "role": role, "products": products,
            "n_combinations": len(rows), "rows": rows,
            "note": "每行都是按参数语义实时算出的，不是抄来的；"
                    "needs_decision 非空表示该组合下必须由使用人补指定"}


# ------------------------------------------------------------------ 待填模板

_TEMPLATE_FIELDS = (
    ("rules", "启用的 Nelson 判异规则，如 1,2,3,5 / all / basic"),
    ("spc_exclusion_mode", "剔点模式：auto=自动剔 / suggest=只提名 / review=人审核"),
    ("boundary_policy", "边界政策：warn=警告放行 / block=无受控出处即阻断"),
    ("doe_replicates", "DOE 重复数（整数，估纯误差用）"),
    ("doe_blocks", "DOE 区组数（整数，跨班次/设备划区组）"),
    ("view", "默认出哪些角色视图，逗号分隔"),
)


def init_template(products, root=None):
    """生成产品档待填模板。

    模板里的值**一律留空并标注待确认**：本工具不替企业编参数。
    编出来的默认值会被当成"系统给的"直接交付，那比留空危险得多。
    """
    products = [p for p in (products or []) if p]
    tpl = {
        "meta": {
            "version": "1.0.0",
            "note": "产品配置档。defaults 内为该产品的配置默认值；"
                    "未填的项回落基线，不会报错。值必须由企业侧按受控口径填写——"
                    "本模板不预填任何数值，编出来的默认值会被误当成已验证的。",
        },
        "products": {},
    }
    for p in products:
        d = {}
        for k, _desc in _TEMPLATE_FIELDS:
            d[k] = None
        tpl["products"][p] = {
            "name": p,
            # 受控规格登记：边界政策从这里推导，不让人在命令行上拍板。
            # 企业给了受控规格文件，就把编号/版本/生效日填进来并把 status 改 active；
            # 工具据此自动收紧为 block。没给就保持 absent——那是事实，不是遗漏。
            "controlled_specs": [{
                "doc_id": None,
                "version": None,
                "status": "absent",
                "verified_at": None,
                "review_by": None,
                "scope": p,
                "note": "受控规格文件登记。doc_id 为空时即使 status=active 也不按受控计——"
                        "说不出文件编号的「受控」不算受控。status 取自 "
                        "absent(未登记)/draft(有文件未生效)/active(受控生效)",
            }],
            "defaults": d,
            "field_help": {k: desc for k, desc in _TEMPLATE_FIELDS},
            "lifecycle": {
                "status": "draft",
                "evidence_level": "unknown",
                "verified_at": None,
                "invalidates_if": None,
                "owner": None,
                "note": "与知识条目同一套生命周期（见 kbstore.LIFECYCLE_FIELDS）；"
                        "未复核的配置档只能当草稿，不得直接用于交付判定",
            },
        }
    return tpl


def write_template(products, path=None, root=None):
    tpl = init_template(products, root)
    path = path or PRODUCT_PROFILE_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(tpl, f, ensure_ascii=False, indent=2)
    return path
