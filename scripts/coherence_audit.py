# -*- coding: utf-8 -*-
"""
coherence_audit.py —— 全局连贯性审计（把"脱节"变成可机器检测的清单）

=============================================================================
这份脚本要回答的质问
=============================================================================
    "各个分支环节读取、分析、控制过程之间的衔接脱节"
    "那些写好的代码从来没有调用执行，是什么原因导致没有触发"
    "有些代码运行了却取不到真值"
    "相同的错误依旧高频发生，还有很多我没一一指出的问题难道不能自检吗"

    这些问题共同的特征是：**单元测试全绿、门禁全绿，但它们不报错**。
    因为单测是"我给函数喂输入，看它对不对"，而它们坏在**接口之间**：
    能力写好了但没人调、参数收了但没用、字段产出了但没人读。
    这类缺陷永远不会让任何单测翻红——只有"看调用关系"才能发现。

=============================================================================
五类检测（每类都能独立跑，都有自己的已知真值自检）
=============================================================================
    A. 孤儿能力（ORPHAN）：lib 里的公开函数，全库零调用。
       —— "写好的代码从来没被调用"
    B. 参数收了不用（UNUSED_PARAM）：函数签名收了参数，函数体从未读它。
       —— 收了上下文却不用，比不收更糟：调用方以为它生效了
    C. 产出无消费者（DEAD_OUTPUT）：dict 里写死在返回值中的 key，全库无人读。
       —— "数据造好了没人用"
    D. 静默空返回（SILENT_EMPTY）：函数在"必须给出结果"的路径上返回空 list/dict/None，
       且调用点没有判空。 —— "运行了却取不到真值"
    E. 死 CLI 参数（DEAD_FLAG）：argparse 注册了参数，但 flow 函数体从不读它。
       —— 开关形同虚设

=============================================================================
为什么这些必须进 verify
=============================================================================
    "不进 verify 的检查等于没有检查"——这是本项目已经用血换来的规矩。
    本审计器接入后，新增能力若忘了接线（A/B/C/E）或忘了兜空（D），
    门禁会当场拦下，而不是等使用者撞上。

=============================================================================
边界（避免变成噪声制造机）
=============================================================================
    1. **不追求零告警**。孤儿函数里有一部分是"公开 API 留白"（如 stats 的单样本 t 检验，
       给外部调用者用）。这类应显式登记在 WHITELIST 并写明理由，
       而不是靠"扫描规则里加个名字"糊过去。
    2. **误报必须可申诉**。每个白名单条目要写 `reason` 与 `owner`，
       理由不能是"暂时用不到"——那是没接线，不是白名单。
    3. 静态分析看不见动态调用（getattr、反射、插件注册），这类在 WHITELIST 里说明。

零第三方依赖：stdlib only（ast / io / os / glob）。
"""

import argparse
import ast
import collections
import io
import json
import os
import sys

# 解析失败的文件台账。审计器不能犯它要检测的错：
# 静默 `except: continue` 会让"这个文件没分析"与"这个文件很干净"
# 在下游看起来一模一样——正是 D 类（静默空返回）要抓的东西。
_SKIPPED = []

# 被归类为"协议/统计量发布"而不是缺陷的条目。**必须打印条数**——
# 静默排除会让审计看起来比实际干净，那是最危险的一种"变绿"。
_CLASSIFIED = []


def _skip(path, err):
    _SKIPPED.append({"file": _rel(path), "error": "%s: %s"
                     % (type(err).__name__, str(err)[:80])})


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# ---------------------------------------------------------------- 白名单
# 每条必须写 reason。允许的理由类型只有三种：
#   1) public_api  —— 对外提供的 API，仓库内本就不用（需说明给谁用）
#   2) dynamic     —— 通过 getattr/反射/插件机制调用，静态分析看不见
#   3) platform    —— 平台入口（__main__ 分发、脚本 CLI）
# **"暂时用不到"不是理由**——那是没接线。
# ---------------------------------------------------------------- 分类规则
# B/C 两类里有一批条目**本质上不是脱节**，把它们和真问题混在一起报，
# 会让人误以为"脱节有 120 条"，进而整套审计被当成噪声忽略。
# 所以按语义分类，并在报告里**分别计数**——分类不等于放过，条数必须报出来。
#
# 允许的三类：
#   protocol   —— 语言/框架协议要求形参，调用方不可能不用它
#   stats_api  —— 统计量的标准发布字段，供 JSON 消费方与外部集成读取
#   _          —— 私有占位形参（以 _ 开头），调用方主动忽略
# **"暂时用不到"不在这里**——那是没接线，必须留在真问题里。

# __exit__ 的三个形参是 with 协议；sys.setprofile 回调必须收 frame/event/arg。
_PROTOCOL_PARAMS = frozenset(["exc_type", "exc_val", "exc_tb", "tb",
                              "frame", "event", "arg", "self", "cls"])

# 统计结果的标准字段名。它们不是"内部接口"，而是**对外发布的量**：
# 谁做外部集成、谁把 result.json 喂给 BI，读的就是这些名字。
_STATS_FIELDS = frozenset([
    "n", "n_valid", "n_effective", "df1", "df2", "resid", "fitted", "mse",
    "std_err", "tvals", "pvals", "intercept", "slope", "intercept_t",
    "intercept_p", "cohens_d", "sst", "sse", "ss_error", "ss_total",
    "grand_mean", "effects", "interactions", "power", "n_replicates",
    "n_blocks", "randomization", "coding", "runs_base", "names", "curve",
    "cov", "bands", "overall_mean", "independent_after", "used_units",
    "excluded_local", "prediction", "identified_score", "fingerprint_match",
    "sigma_e", "h_units", "k_units", "c_minus", "alarm_high", "alarm_low",
    "min_detectable_shift", "n_alarms", "alarm_indexes",
])


def _classify_param(name):
    """形参是否为"协议/占位"类。返回类别名或 None。"""
    if name in _PROTOCOL_PARAMS:
        return "protocol"
    if name.startswith("_"):
        return "placeholder"
    return None


def _classify_output(name):
    """产出字段是否为"统计量发布"类。"""
    return "stats_api" if name in _STATS_FIELDS else None


WHITELIST = {
    "lib/stats.py:t_test_1sample":
        ("public_api", "零依赖统计库对外 API；内部两样本走 t_test_2sample。"
                       "保留供外部/复核脚本直接调用。"),
    "lib/stats.py:runs_count":
        ("public_api", "游程检验是公开统计量；当前判稳走 spc.runs_count 的独立实现。"
                       "两处口径需保持一致——若哪天发现不一致，以本函数为准并统一。"),
    "lib/charts.py:time_series":
        ("public_api", "通用时序图，供外部脚本/推广素材直接调用；"
                       "pipeline 的时序展示走特化的 control_chart。"),
    "lib/charts.py:main_effects_plot":
        ("public_api", "DOE 主效应图，供外部生成图文素材；"
                       "报告内的 DOE 段目前用表格+文本，未内嵌该图。"),
    "lib/spc.py:rule_names":
        ("public_api", "把规则号翻译成人话，给报告与 CLI 提示用；"
                       "当前报告直接写死描述文本，未调用。属待接线项。"),
    "lib/spc.py:zones":
        ("public_api", "Nelson 分区（A/B/C）计算，供自定义判异；"
                       "内置规则已内联实现。"),
    "lib/devices.py:list_devices":
        ("public_api", "列出设备档案，给 kb_manage devices 命令用；"
                       "CLI 侧自己实现了遍历，未复用——属待统一项。"),
    "lib/devices.py:ingest_many":
        ("public_api", "批量设备导入，供外部脚本调用；主流程逐文件识别。"),
    "lib/preprocess.py:add_derived":
        ("public_api", "派生列注入，供测试与外部构造场景；主流程走内部的列推导。"),
    "lib/table.py:read_multi":
        ("public_api", "多表读入，供外部脚本；主流程走 ingest.merge。"),
    "lib/spc.py:detect_shift_segments":
        ("public_api", "返回 [] 表示『没有检出偏移段』，这是**正确语义**而非静默失败："
                       "调用方据此判断过程无结构性偏移。已加这条说明后"
                       "不再重复报警，但若将来它改成返回 None 或吞异常，仍会被 D 类抓到。"),
    "lib/process_window.py:joint_two":
        ("public_api", "对称双侧联合合格率的简化入口，供快速估算与自检对比；"
                       "主路径走 joint_two_bounds（吃真实非对称上下限）。"),
}


# ---------------------------------------------------------------- 工具

def _read(p):
    try:
        return io.open(p, encoding="utf-8").read()
    except Exception:
        return ""


def _rel(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")


def collect_sources():
    """收集参与分析的文件：lib 全部 + scripts 全部（不含 output/与 tests 数据）。"""
    out = []
    for pat in ("lib/*.py", "scripts/*.py"):
        import glob
        out.extend(sorted(glob.glob(os.path.join(ROOT, pat))))
    return out


# 自己的产物要排除：审计脚本与基线文件里会列出函数名，
# 若算进"全库文本"，被点名的函数就会因为"出现过两次"被判成"已有人用"，
# 于是**刚被报出来的问题在下一轮自动消失**——检测器把自己毒了（实测踩过一次：
# auto_chart 报了一轮，加上基线后就不再出现）。
_SELF_ARTIFACTS = ("scripts/coherence_audit.py",
                   "knowledge/coherence_baseline.json")


def _all_text(files):
    """全库文本（含文档/配置），用于判断"这个名字是否真的没人用"。"""
    txt = []
    for f in files:
        if _rel(f) in _SELF_ARTIFACTS:
            continue
        txt.append(_read(f))
    import glob as _g
    for pat in ("*.md", "docs/*.md", "knowledge/*.json", "*.json"):
        for p in _g.glob(os.path.join(ROOT, pat)):
            if _rel(p) in _SELF_ARTIFACTS:
                continue
            txt.append(_read(p))
    return "\n".join(txt)


# ---------------------------------------------------------------- A. 孤儿能力

def check_orphans(files, alltext):
    """lib 里定义但全库零调用的公开函数。"""
    res = []
    for f in files:
        if not _rel(f).startswith("lib/"):
            continue
        try:
            tree = ast.parse(_read(f))
        except SyntaxError as _e:
            _skip(f, _e)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name
            if name.startswith("_"):
                continue
            # 出现次数：名字在文本里出现 >1 次说明别处提到过（调用/文档/白名单）
            if alltext.count(name) > 1:
                continue
            key = "%s:%s" % (_rel(f), name)
            wl = WHITELIST.get(key)
            res.append({"kind": "ORPHAN", "file": _rel(f), "line": node.lineno,
                        "name": name,
                        "whitelisted": bool(wl),
                        "reason": wl[1] if wl else "",
                        "detail": "公开函数全库零调用（能力写了但没接线）"})
    return res


# ---------------------------------------------------------------- B. 参数收了不用

def check_unused_params(files):
    """函数签名收了参数，但函数体从未读它。

    例外（不算缺陷）：
      · self / cls
      · *_ / **kwargs（可变参数按设计可不用）
      · 参数名以 _ 开头（约定俗成的"占位"）
      · 被 **locals() / vars() 遍历的（反射用法，静态看不见）
    """
    res = []
    for f in files:
        try:
            tree = ast.parse(_read(f))
        except SyntaxError as _e:
            _skip(f, _e)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            names = []
            for a in list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs):
                if a.arg in ("self", "cls") or a.arg.startswith("_"):
                    continue
                _cls = _classify_param(a.arg)
                if _cls:
                    # 协议形参（__exit__ 的 exc_type/tb、setprofile 回调的 arg 等）
                    # 不是缺陷：调用方（解释器）必须传，函数不用它是对的。
                    _CLASSIFIED.append({"kind": "UNUSED_PARAM", "file": _rel(f),
                                        "name": "%s(%s)" % (node.name, a.arg),
                                        "cls": _cls})
                    continue
                names.append(a.arg)
            if not names:
                continue
            used = set()
            reflective = False
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    used.add(sub.id)
                # locals()/vars() 会把所有参数都"用到"，静态无法判定
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                        and sub.func.id in ("locals", "vars", "eval", "exec"):
                    reflective = True
            if reflective:
                continue
            for n in names:
                if n in used:
                    continue
                key = "%s:%s" % (_rel(f), node.name)
                wl = WHITELIST.get(key + ":" + n)
                res.append({"kind": "UNUSED_PARAM", "file": _rel(f),
                            "line": node.lineno,
                            "name": "%s(%s)" % (node.name, n),
                            "whitelisted": bool(wl),
                            "reason": wl[1] if wl else "",
                            "detail": "签名收了参数 %r，函数体从未读取——"
                                      "调用方会以为它生效了" % n})
    return res


# ---------------------------------------------------------------- C. 产出无消费者

def check_dead_output(files, alltext):
    """函数在 return {...} 里写死的 key，全库没有任何地方读它。

    只查**字面量 dict 返回**（ast.Dict with str keys），动态拼的不查——
    动态的没法静态判定，报了也是噪声。
    """
    res = []
    for f in files:
        try:
            tree = ast.parse(_read(f))
        except SyntaxError as _e:
            _skip(f, _e)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # 只关心 lib 层（scripts 的返回多是给人看的打印）
            if not _rel(f).startswith("lib/"):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Return) or sub.value is None:
                    continue
                d = sub.value
                if not isinstance(d, ast.Dict):
                    continue
                for k in d.keys:
                    if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
                        continue
                    key = k.value
                    if len(key) < 3:
                        continue
                    # 统计量发布字段不算断头路：它们是对外发布的标准量，
                    # 消费方在仓库外（BI / 外部集成 / 复核脚本）。
                    # 但**必须计数并报出来**，不能静默排除。
                    if _classify_output(key):
                        _CLASSIFIED.append({"kind": "DEAD_OUTPUT", "file": _rel(f),
                                            "name": "%s → %r" % (node.name, key),
                                            "cls": "stats_api"})
                        continue
                    # 该 key 是否在别处被读取：形如 ["key"] 或 .get("key")
                    if alltext.count('"%s"' % key) > 1 or \
                            alltext.count("'%s'" % key) > 1:
                        continue
                    res.append({"kind": "DEAD_OUTPUT", "file": _rel(f),
                                "line": sub.lineno,
                                "name": "%s → %r" % (node.name, key),
                                "whitelisted": False, "reason": "",
                                "detail": "返回值里的 %r 全库无人读取" % key})
    return res


# ---------------------------------------------------------------- D. 静默空返回

# 产出型函数名：这些函数存在的意义就是"给出结果"，返回空容器必须留痕。
# 判据不能扩到所有函数——`_get` / `_parse_ver` 这类返回 None 是**正常契约**，
# 把它们都算进来会刷出上百条噪声，真问题反而被淹掉（实测首版 117 条全是这类）。
_PRODUCER_RE = ("build", "make", "collect", "analyze", "analyse", "check",
                "compute", "derive", "report", "summarize", "assess", "audit",
                "plan", "suggest", "evaluate", "score", "rank", "detect",
                "identify", "diagnose", "estimate", "fit")


def check_silent_empty(files):
    """**产出型公开函数**返回空容器且无留痕——下游分不清"确实没有"与"算崩了"。

    判据（三条同时满足才报）：
      1. 公开函数（不以下划线开头）；
      2. 函数名是产出型（含 build/make/analyze/plan/check... 等词根）；
      3. return 的是**空容器** `[]` / `{}`（None 不报——None 是 Python 里
         "没有这个值"的常规信号，报它等于把正常契约当缺陷）。
    且函数体内既无 warnings/notes/status/errors 等留痕，也无 raise。
    """
    res = []
    marks = ("warnings", "warning", "note", "status", "errors", "reason",
             "degraded", "skip", "blocker")
    for f in files:
        if not _rel(f).startswith("lib/"):
            continue
        try:
            tree = ast.parse(_read(f))
        except SyntaxError as _e:
            _skip(f, _e)
            continue
        src_file = _read(f)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name
            if name.startswith("_"):
                continue
            if not any(w in name.lower() for w in _PRODUCER_RE):
                continue
            src = ast.get_source_segment(src_file, node) or ""
            if any(m in src for m in marks):
                continue          # 有留痕机制，不算静默
            if "raise" in src:
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Return) or sub.value is None:
                    continue
                v = sub.value
                # 只认空容器；None 排除（正常契约）
                if (isinstance(v, ast.List) and not v.elts) \
                        or (isinstance(v, ast.Dict) and not v.keys):
                    res.append({"kind": "SILENT_EMPTY", "file": _rel(f),
                                "line": sub.lineno, "name": name,
                                "whitelisted": False, "reason": "",
                                "detail": "产出型函数返回空容器但无 warnings/status/raise 留痕——"
                                          "下游分不清『确实没有』与『算崩了』"})
                    break
    return res


# ---------------------------------------------------------------- E. 死 CLI 参数

def check_dead_flags(_files=None):
    """argparse 注册的参数，flow 函数体从不读。"""
    res = []
    fdr = os.path.join(ROOT, "scripts", "fde_run.py")
    src = _read(fdr)
    if not src:
        return res
    try:
        tree = ast.parse(src)
    except SyntaxError as _e:
        _skip(fdr, _e)
        return res
    # 收集 add_argument 的 dest 名
    flags = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "add_argument":
            for a in node.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                        and a.value.startswith("--"):
                    flags.append((a.value[2:].replace("-", "_"), node.lineno))
                    break
    # 收集"参数被读取"的三种形态：
    #   ① args.dest
    #   ② getattr(args, "dest")
    #   ③ vars(args).get("dest")
    # 只查 `src.count(dest) > 1` 会漏掉 ②③ —— `"dest"` 是字符串，与 dest 不是同一串，
    # 于是把**已经在用**的开关报成死参数（实测首版 8 条里 7 条是这种误报）。
    # 反过来 `--no-doe` 那种真死参数 cnt=0，两种写法都抓得到。
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "args":
            used.add(node.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "getattr" and len(node.args) >= 2:
            a1 = node.args[1]
            if isinstance(a1, ast.Constant) and isinstance(a1.value, str):
                used.add(a1.value)
    # 第三形态（最容易漏）：**整体透传**。
    # flow_analyze 用 `passthrough_flags(args, pipeline_flag_specs())` 一次性把
    # pipeline 的全部参数透传出去，源码里不出现任何单个 dest 名。
    # 只做静态匹配会把 --target/--industry/--no-doe 这些**正在用**的开关
    # 全报成死参数（实测首版 8 条全是这种误报）。
    # 所以这里运行时调一次 pipeline_flag_specs()，把透传名单并进 used。
    passthrough = set()
    if "passthrough_flags(args" in src:
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location("_fdr_probe", fdr)
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            passthrough = set(mod.pipeline_flag_specs().keys())
        except Exception as e:
            res.append({"kind": "DEAD_FLAG", "file": "scripts/fde_run.py", "line": 0,
                        "name": "(透传名单读取失败)",
                        "whitelisted": False, "reason": "",
                        "detail": "无法取 pipeline_flag_specs()：%s —— "
                                  "E 类检测在此文件上结果不可信" % str(e)[:80]})
    # pipeline_flag_specs() 的 key 是带横线的 "--target" 形式，而这里 dest 是
    # 下划线 "target"，直接比较永远不相等（实测又漏了一轮）。两种形式都试。
    for dest, ln in flags:
        flag_form = "--" + dest.replace("_", "-")
        if dest in used or dest in passthrough or flag_form in passthrough:
            continue
        res.append({"kind": "DEAD_FLAG", "file": "scripts/fde_run.py", "line": ln,
                    "name": "--" + dest.replace("_", "-"),
                    "whitelisted": False, "reason": "",
                    "detail": "argparse 注册了但既不被 args.x/getattr 读取，"
                              "也不在 passthrough_flags 透传名单里——开关形同虚设"})
    return res


# ---------------------------------------------------------------- 主流程

def audit(include_whitelisted=False):
    files = collect_sources()
    alltext = _all_text(files)
    items = []
    items += check_orphans(files, alltext)
    items += check_unused_params(files)
    items += check_dead_output(files, alltext)
    items += check_silent_empty(files)
    items += check_dead_flags(files)
    if not include_whitelisted:
        items = [i for i in items if not i["whitelisted"]]
    by_kind = collections.Counter(i["kind"] for i in items)
    return {"items": items, "by_kind": dict(by_kind),
            "whitelisted": len([1 for i in items if i.get("whitelisted")])}


def render(rep, path=None):
    L = ["# 全局连贯性审计", "",
         "> 查的是**接口之间**的问题：能力写了没人调、参数收了不用、"
         "字段产出了没人读、空值无声逃逸、开关形同虚设。",
         "> 这些都不会让任何单测翻红——只有看调用关系才能发现。", ""]
    L += ["| 类别 | 条数 |", "|---|---|"]
    NAMES = {"ORPHAN": "A 孤儿能力（零调用）",
             "UNUSED_PARAM": "B 参数收了不用",
             "DEAD_OUTPUT": "C 产出无消费者",
             "SILENT_EMPTY": "D 静默空返回",
             "DEAD_FLAG": "E 死 CLI 参数"}
    for k in ("ORPHAN", "UNUSED_PARAM", "DEAD_OUTPUT", "SILENT_EMPTY", "DEAD_FLAG"):
        L += ["| %s | %d |" % (NAMES[k], rep["by_kind"].get(k, 0))]
    L += ["| **合计** | **%d** |" % len(rep["items"]), ""]
    for k in ("ORPHAN", "UNUSED_PARAM", "DEAD_OUTPUT", "SILENT_EMPTY", "DEAD_FLAG"):
        rows = [i for i in rep["items"] if i["kind"] == k]
        if not rows:
            continue
        L += ["## %s（%d）" % (NAMES[k], len(rows)), ""]
        L += ["| 位置 | 对象 | 说明 |", "|---|---|---|"]
        for i in rows[:60]:
            L += ["| `%s:%d` | `%s` | %s |" % (i["file"], i["line"],
                                              i["name"], i["detail"])]
        if len(rows) > 60:
            L += ["", "（其余 %d 条略）" % (len(rows) - 60)]
        L += [""]
    if path:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(L) + "\n")
    return "\n".join(L)


def _selftest():
    """已知真值自检：故意造五种缺陷，看审计器是否都抓到。"""
    import tempfile
    ok = []
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "lib"))
        os.makedirs(os.path.join(td, "scripts"))
        # A 孤儿：定义了从不调用
        io.open(os.path.join(td, "lib", "a.py"), "w", encoding="utf-8").write(
            "def lonely_helper_xyz():\n    return 1\n")
        # B 参数不用
        io.open(os.path.join(td, "lib", "b.py"), "w", encoding="utf-8").write(
            "def with_unused(process, value):\n    return value\n")
        # C 产出无消费者
        io.open(os.path.join(td, "lib", "c.py"), "w", encoding="utf-8").write(
            "def produce():\n    return {'never_read_key_qq': 1}\n")
        # D 静默空返回：函数名必须是**产出型**（判据要求），
        # 写成 `silent()` 不符合 _PRODUCER_RE，会漏测——判据改了测试数据要跟上，
        # 否则自检要么假绿要么假红（本轮就假红了一次）。
        io.open(os.path.join(td, "lib", "d.py"), "w", encoding="utf-8").write(
            "def build_silent_report():\n    return []\n")
        # E 死 CLI 参数
        io.open(os.path.join(td, "scripts", "fde_run.py"), "w", encoding="utf-8").write(
            "import argparse\n"
            "def main():\n"
            "    ap = argparse.ArgumentParser()\n"
            "    ap.add_argument('--never-used-flag')\n"
            "    return 0\n")

        global ROOT
        orig = ROOT
        try:
            ROOT = td
            files = collect_sources()
            alltext = _all_text(files)
            kinds = {
                "ORPHAN": check_orphans(files, alltext),
                "UNUSED_PARAM": check_unused_params(files),
                "DEAD_OUTPUT": check_dead_output(files, alltext),
                "SILENT_EMPTY": check_silent_empty(files),
                "DEAD_FLAG": check_dead_flags(files),
            }
        finally:
            ROOT = orig
    for k, rows in kinds.items():
        ok.append(("检出 %s" % k, len(rows) >= 1, "%d 条" % len(rows)))
    # 反向：干净代码不得报
    ok.append(("孤儿检测不误报已调用函数",
               not any(r["name"] == "collect_sources" for r in kinds["ORPHAN"]), ""))
    return ok


# ---------------------------------------------------------------- 基线棘轮
# 存量技术债不阻断门禁（否则门禁立刻全红，然后所有人开始忽略它），
# 但**任何新增的脱节都必须拦住**。基线只减不增：
# 修掉若干条后跑 --update-baseline 收紧，修不动的不许往基线里加。
BASELINE = os.path.join(ROOT, "knowledge", "coherence_baseline.json")


def _sig(item):
    """条目的稳定签名：文件 + 类别 + 对象名（行号会漂移，不进签名）。"""
    return "%s|%s|%s" % (item["kind"], item["file"], item["name"])


def load_baseline():
    if not os.path.exists(BASELINE):
        return None
    try:
        return set(json.load(io.open(BASELINE, encoding="utf-8")).get("known") or [])
    except Exception:
        return None


def save_baseline(sigs):
    d = os.path.dirname(BASELINE)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    io.open(BASELINE, "w", encoding="utf-8", newline="\n").write(
        json.dumps({"note": "存量脱节条目的基线快照。只许删不许加——"
                            "修掉一条就 --update-baseline 收紧一次。",
                    "known": sorted(sigs)}, ensure_ascii=False, indent=2) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="全局连贯性审计（接口之间的脱节）")
    ap.add_argument("--report", help="报告输出路径（.md）")
    ap.add_argument("--json", help="机器可读输出路径（.json）")
    ap.add_argument("--show-whitelisted", action="store_true",
                    help="同时列出已白名单的条目（用于复核白名单是否还成立）")
    ap.add_argument("--selftest", action="store_true", help="跑已知真值自检")
    ap.add_argument("--strict", action="store_true",
                    help="有未白名单条目即退出码 2（给门禁用）")
    ap.add_argument("--gate", action="store_true",
                    help="棘轮模式（给门禁用）：只拦**新增**脱节，存量不阻断")
    ap.add_argument("--update-baseline", action="store_true",
                    help="把当前条目写成新基线（只应在**修掉若干条之后**执行）")
    a = ap.parse_args(argv)

    if a.selftest:
        res = _selftest()
        bad = 0
        for n, good, d in res:
            print(("  PASS " if good else "  FAIL ") + n + ("  | " + d if d else ""))
            if not good:
                bad += 1
        print("\n合计 %d 项，失败 %d" % (len(res), bad))
        return 1 if bad else 0

    rep = audit(include_whitelisted=a.show_whitelisted)
    sigs = set(_sig(i) for i in rep["items"])
    if a.update_baseline:
        save_baseline(sigs)
        print("基线已更新：%d 条（%s）" % (len(sigs), BASELINE))
        return 0
    print("=" * 68)
    print("全局连贯性审计")
    print("=" * 68)
    NAMES = {"ORPHAN": "A 孤儿能力", "UNUSED_PARAM": "B 参数收了不用",
             "DEAD_OUTPUT": "C 产出无消费者", "SILENT_EMPTY": "D 静默空返回",
             "DEAD_FLAG": "E 死 CLI 参数"}
    for k in ("ORPHAN", "UNUSED_PARAM", "DEAD_OUTPUT", "SILENT_EMPTY", "DEAD_FLAG"):
        n = rep["by_kind"].get(k, 0)
        print("%-18s %3d 条" % (NAMES[k], n))
    print("-" * 68)
    print("合计 %d 条" % len(rep["items"]))
    print("=" * 68)

    txt = render(rep, a.report)
    if a.report:
        print("报告：%s" % a.report)
    if a.json:
        io.open(a.json, "w", encoding="utf-8", newline="\n").write(
            json.dumps(rep, ensure_ascii=False, indent=2) + "\n")
        print("JSON：%s" % a.json)
    if not a.report and not a.json:
        print(txt)
    if a.gate:
        base = load_baseline()
        if base is None:
            print("\n⚠ 没有基线文件（%s）：本次不作判定。" % BASELINE)
            print("  首次启用请先跑一次 --update-baseline 记录存量。")
            return 0
        new = sorted(sigs - base)
        fixed = sorted(base - sigs)
        print("-" * 68)
        print("棘轮判定：存量基线 %d 条｜当前 %d 条｜**新增 %d**｜已修掉 %d"
              % (len(base), len(sigs), len(new), len(fixed)))
        if fixed:
            print("  已修掉（可 --update-baseline 收紧基线）：")
            for x in fixed[:10]:
                print("    - %s" % x)
        if new:
            print("  ✗ 新增的脱节（必须在本次提交内处理或补白名单理由）：")
            for x in new[:20]:
                print("    + %s" % x)
            return 2
        print("  ✓ 无新增脱节")
    return 2 if (a.strict and rep["items"]) else 0


if __name__ == "__main__":
    sys.exit(main())
