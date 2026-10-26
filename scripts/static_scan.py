#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""静态全库扫描器 —— 把「跑一遍才发现一个」变成「一次扫穿，成批修」。

为什么要有这个脚本
------------------
2026-10-26 的教训：连续几轮改动都是「改完跑一遍 → 发现一个新问题 → 补一个补丁 →
再跑一遍 → 又发现一个」。每轮都只看见当前改动那一小块，看不见全库。
靠 grep 摸底同样不可靠——`except:\\n    pass` 这种跨行写法 grep 直接漏掉，
于是「没有静默吞异常」的结论是假的。

本脚本用 AST 而不是正则去读代码，覆盖七类问题。判断规则全部写在代码里并附理由，
出的是**带文件行号的全量清单**，不是"当前这次改动有没有问题"。

七类检查
--------
S1 静默吞异常：except 分支里只有 pass/continue，异常被吃掉且不落任何痕迹。
               ——最危险的一类：不崩、不报、不留痕，问题只在结论里表现。
S2 定义未调用：函数/常量定义了，全库找不到任何一处引用（"脑袋有知识代码却没运行"）。
S3 知识库悬空：knowledge/*.json 里的条目，代码里从未读取过。
S4 参数透传链：analyze() 形参 ↔ pipeline CLI ↔ fde_run 透传，三处必须对齐。
S5 文档承诺：SKILL.md / README 里写的命令与参数，必须在代码里真实存在。
S6 不可达分支：if False / 恒真恒假条件 / 死循环无出口。
S7 硬编码副本：同一份清单数据在两处各写一份（EXCLUDE_DIRS 踩过两次）。

用法
----
    python scripts/static_scan.py                 # 全量扫描，按严重度排序输出
    python scripts/static_scan.py --only S1,S2    # 只跑指定检查
    python scripts/static_scan.py --json out.json # 落盘（供 CI 与回归对比）
    python scripts/static_scan.py --strict        # 有 HIGH 级问题则以退出码 1 结束

退出码：0=无 HIGH 级问题（或没开 --strict）；1=存在 HIGH 级问题且开了 --strict。
"""

import ast
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCAN_DIRS = ["lib", "scripts"]
SKIP_FILES = {"__init__.py"}

# 动态调用/导出约定下的名字不算死代码
DUNDER_OK = re.compile(r"^__\w+__$")
DECORATOR_OK = {"property", "staticmethod", "classmethod", "abstractmethod",
                "setter", "getter", "functools.wraps", "contextmanager"}

SEV_ORDER = {"HIGH": 0, "MED": 1, "LOW": 2, "INFO": 3}


# ----------------------------------------------------------------- 基础设施

def _py_files():
    out = []
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for fn in sorted(os.listdir(base)):
            if fn.endswith(".py") and fn not in SKIP_FILES:
                out.append(os.path.join(d, fn))
    return out


def _read(rel):
    try:
        with io.open(os.path.join(ROOT, rel), "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


class Finding(object):
    __slots__ = ("sid", "sev", "rel", "line", "what", "why", "hint")

    def __init__(self, sid, sev, rel, line, what, why, hint=""):
        self.sid = sid
        self.sev = sev
        self.rel = rel
        self.line = line
        self.what = what
        self.why = why
        self.hint = hint

    def as_dict(self):
        return {"id": self.sid, "sev": self.sev, "file": self.rel,
                "line": self.line, "what": self.what, "why": self.why,
                "hint": self.hint}


_KEPT = []        # 被 scan:keep 豁免的条目，与 findings 分开报


# 豁免标记：源码里写 `# scan:keep <理由>` 表示"这条我确认过，保留"。
# 用途是把「已知但暂不处理」与「不知道它存在」分开——
# 前者是决策，后者是盲区。豁免必须带理由，无理由的豁免不予承认。
KEEP_RE = re.compile(r"#\s*scan:keep\s+(\S.*)$")


def _keep_reason(text, def_lineno):
    """取定义处附近的 scan:keep 理由（定义行本身或紧邻的前 3 行内）。"""
    lines = text.splitlines()
    for i in range(def_lineno - 1, max(-1, def_lineno - 5), -1):
        m = KEEP_RE.search(lines[i])
        if m:
            return m.group(1).strip()
    return ""


def _keep_reason_range(lines, lo_1based, hi_1based, pad=2):
    """按行区间取 scan:keep 理由（1 起行号，两端再外扩 pad 行）。

    用 handler 的真实起止行而不是「except 行 ±3」：多行 handler
    （except + 若干行说明 + pass）按固定行数算会整个落在窗口外，
    豁免就静默失效——而豁免失效的后果是它又被报一遍，
    逼着人要么重复标注要么放弃使用豁免机制。
    """
    lo = max(0, lo_1based - 1 - pad)
    hi = min(len(lines), hi_1based + pad)
    for i in range(lo, hi):
        m = KEEP_RE.search(lines[i])
        if m:
            return m.group(1).strip()
    return ""


# ------------------------------------------------------------------- S1
def scan_silent_except():
    """静默吞异常：except 分支只有 pass/continue。

    判据是「异常被吃掉且不留痕迹」：
      - 只有 pass / continue                    -> HIGH（彻底无声）
      - 有 pass 但同一 try 里有 else/finally 做补偿 -> 降级
      - 只有一句 print/日志                      -> MED（有痕但可能不足以定位）
    """
    out = []
    for rel in _py_files():
        text = _read(rel)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            body = [b for b in node.body if not isinstance(b, ast.Pass)]
            if body:
                # 不是空 except：再看是不是只 print 了一句（有痕但弱）
                continue
            # 已接降级台账的 except 不算「静默」——它留痕了，报告里看得见。
            # 判断方式是看 handler 里有没有调用 degrade.note / note_current。
            logged = False
            for b in ast.walk(node):
                if (isinstance(b, ast.Call) and isinstance(b.func, ast.Attribute)
                        and b.func.attr in ("note", "note_current")):
                    logged = True
                    break
            if logged:
                continue
            # 找这个 handler 所属的 try，看有没有 finally 兜底
            has_finally = False
            for anc in ast.walk(tree):
                if isinstance(anc, ast.Try) and node in anc.handlers:
                    has_finally = bool(anc.finalbody)
                    break
            # 多行写法（except:\n    pass）在源码里定位到 handler 行
            ln = getattr(node, "lineno", 0)
            # 若 handler 行本身是 `except X:`，pass 在下一行，定位更准
            src_lines = text.splitlines()
            if ln <= len(src_lines) and ln >= 1:
                if "pass" not in src_lines[ln - 1] and ln < len(src_lines):
                    if "pass" in src_lines[ln]:
                        ln = ln + 1
            # 豁免：源码里写明「这条确认过、保留」的，不再报。
            # 注释可能写在 except 之前（解释为什么吞）或之后（解释为什么保留），
            # 所以按 handler 的真实起止行取窗口，再两端各外扩 2 行。
            kr = _keep_reason_range(src_lines, node.lineno,
                                    getattr(node, "end_lineno", None)
                                    or node.lineno)
            if kr:
                _KEPT.append({"id": "S1", "file": rel, "line": ln, "reason": kr})
                continue
            out.append(Finding(
                "S1", "MED" if has_finally else "HIGH", rel, ln,
                "except 分支静默吞掉异常（只有 pass）",
                "异常被吃掉且不留任何痕迹：不崩、不报、不落日志，"
                "问题只会在最终结论里表现为「算不出来」或「未检出」。"
                + ("（同 try 有 finally，风险略低）" if has_finally else ""),
                "至少记入 result['warnings'] 或打一行日志；"
                "确实可忽略的，写明为什么可忽略"))
            _ = body
    return out


# ------------------------------------------------------------------- S2
def _collect_defs():
    """收集全库的函数/类定义名，以及类方法名。"""
    defs = {}          # name -> [(rel, lineno, kind)]
    for rel in _py_files():
        text = _read(rel)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault(node.name, []).append((rel, node.lineno, "func"))
            elif isinstance(node, ast.ClassDef):
                defs.setdefault(node.name, []).append((rel, node.lineno, "class"))
    return defs


def _collect_names_used():
    """收集全库出现过的所有标识符（含属性访问与字符串常量里的名字）。"""
    used = set()
    for rel in _py_files():
        text = _read(rel)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            # 解析不了就退化成词法扫描，宁可误报为"用过"也不漏报
            for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", text):
                used.add(m.group(0))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # 动态分派（getattr(x, "name")）与配置化调用靠字符串出现
                for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]{2,}", node.value):
                    used.add(m.group(0))
            elif isinstance(node, ast.alias):
                used.add(node.name.split(".")[0])
    return used


def scan_dead_code():
    """定义未调用：写了函数/类，全库找不到任何一处引用。

    这正是「脑袋有知识、代码却没运行」的那一类：
    知识库有条目、代码里有函数，但没有任何一条执行路径走到它。
    """
    out = []
    defs = _collect_defs()
    used = _collect_names_used()
    # 文档里提到的也算"被用"——至少说明它有对外承诺，不能悄悄删
    doc_text = " ".join(_read(d) for d in
                        ["SKILL.md", "README.md", "CHANGELOG.md"])
    for name, locs in sorted(defs.items()):
        if DUNDER_OK.match(name):
            continue
        if name in used or name in doc_text:
            continue
        for rel, ln, kind in locs:
            text = _read(rel)
            reason = _keep_reason(text, ln)
            if reason:
                _KEPT.append({"id": "S2", "file": rel, "line": ln,
                              "reason": reason})
                continue      # 已确认保留并写明理由，不再报
            out.append(Finding(
                "S2", "MED", rel, ln,
                "%s「%s」定义了但全库找不到任何引用" % (
                    "函数" if kind == "func" else "类", name),
                "定义存在、执行路径不存在。知识在代码里躺着，"
                "但没有任何一条调用链会走到它——功能等于没做。",
                "确认是预留接口就在定义行上方写 `# scan:keep <理由与启用条件>`；"
                "是废弃的就删掉，别让下一个人以为它生效"))
    return out


# ------------------------------------------------------------------- S3
def scan_dangling_knowledge():
    """知识库悬空：JSON 里定义了的条目，代码里从未读取。

    只扫「标识性」字段（defect 键名、process 键名、param key、device id），
    不做全字段匹配——描述性文案不出现在代码里是正常的。
    """
    out = []
    kdir = os.path.join(ROOT, "knowledge")
    if not os.path.isdir(kdir):
        return out
    code_text = ""
    for rel in _py_files():
        code_text += _read(rel) + "\n"
    for fn in sorted(os.listdir(kdir)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(kdir, fn)
        try:
            data = json.loads(_read(os.path.join("knowledge", fn)) or "{}")
        except Exception:
            continue
        # 缺陷条目
        for key in (data.get("defect_rootcause") or {}):
            if isinstance(key, str) and len(key) > 3 and key not in code_text:
                out.append(Finding(
                    "S3", "LOW", os.path.join("knowledge", fn), 0,
                    "缺陷条目「%s」在代码中零命中" % key,
                    "知识库有这条缺陷的定义，但代码里没有任何地方引用它——"
                    "可能只是没走到，也可能别名索引没覆盖到它。",
                    "确认别名已登记；确认 identify_defects 能命中它"))
    return out


# ------------------------------------------------------------------- S4
def scan_param_chain():
    """参数透传链：analyze() 形参 ↔ pipeline CLI argparse ↔ fde_run 透传。

    三处任何一处漏了，使用者的 `--xxx` 就是石沉大海：
    要么报 unrecognized arguments，要么参数被静默忽略。
    """
    out = []
    pipe = _read(os.path.join("lib", "pipeline.py"))
    run = _read(os.path.join("scripts", "fde_run.py"))

    # 1) analyze() 的形参
    m = re.search(r"^def analyze\((.*?)\)\s*:", pipe, re.M | re.S)
    if not m:
        return out
    sig = [p.split("=")[0].strip()
           for p in re.split(r",(?![^\[\(]*[\]\)])", m.group(1).replace("\n", " "))
           if p.strip()]
    sig = [p for p in sig if p and p not in ("self",)]

    # 2) pipeline CLI 注册的参数（去掉 -- 前缀与 - 短名）
    cli = set(re.findall(r'add_argument\(\s*["\'](--[a-z][a-z0-9-]+)', pipe))
    # 3) fde_run 透传给子进程的参数
    passed = set()
    for mm in re.finditer(r'cmd \+?= \["(--[a-z][a-z0-9-]+)"\]', run):
        passed.add(mm.group(1))
    for mm in re.finditer(r'cmd \+= \["(--[a-z][a-z0-9-]+)", ', run):
        passed.add(mm.group(1))
    for mm in re.finditer(r'cmd\.append\("(--[a-z][a-z0-9-]+)"\)', run):
        passed.add(mm.group(1))

    # analyze 形参里，应当在 CLI 暴露的那些（排除内部对象型参数）
    internal = {"spec_overrides", "d2_card", "emit_d2_card", "export_xlsx",
                "do_doe", "max_charts", "auto_adapt", "sheet"}
    for p in sig:
        flag = "--" + p.replace("_", "-")
        if p in internal:
            continue
        if flag not in cli:
            out.append(Finding(
                "S4", "MED", os.path.join("lib", "pipeline.py"), 0,
                "analyze() 形参「%s」在 CLI 里没有对应参数" % p,
                "库调用者能传，命令行使用者传不了——同一份能力只对一半人开放。",
                "在 argparse 里补上 %s，或明确标注它仅供库调用" % flag))
    for flag in sorted(cli):
        if flag in passed:
            continue
        # 布尔开关与 -i 位置参数不强制要求透传
        if flag in ("--input", "-i"):
            continue
        out.append(Finding(
            "S4", "LOW", os.path.join("scripts", "fde_run.py"), 0,
            "CLI 参数「%s」未被编排器透传" % flag,
            "走 fde_run.py 编排入口时这个参数会丢失，"
            "两个入口行为不一致——使用者会以为自己设了，其实没生效。",
            "在 flow_analyze 里补上透传"))
    return out


# ------------------------------------------------------------------- S5
def scan_doc_promise():
    """文档承诺：SKILL.md / README 里出现的命令与参数，必须在代码里存在。

    文档是给使用者的承诺。承诺了不存在的入口，比不写更糟——
    使用者照做只会碰壁（--verbose 事件就是这么来的）。
    """
    out = []
    pipe = _read(os.path.join("lib", "pipeline.py"))
    run = _read(os.path.join("scripts", "fde_run.py"))
    registered = set(re.findall(r'add_argument\(\s*["\'](--[a-z][a-z0-9-]+)', pipe))
    registered |= set(re.findall(r'add_argument\(\s*["\'](--[a-z][a-z0-9-]+)', run))
    # 编排器子命令。两种实现都要认：argparse 子解析器 add_parser，
    # 以及位置参数 + choices（fde_run.py 实际用的是后者）。
    subs = set(re.findall(r'add_parser\(\s*["\']([a-z][a-z0-9_-]+)', run))
    for m in re.finditer(r'choices=\[([^\]]+)\]', run):
        subs |= set(re.findall(r'["\']([a-z][a-z0-9_-]+)["\']', m.group(1)))

    for doc in ("SKILL.md", "README.md"):
        text = _read(doc)
        if not text:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if ".py" not in line and "python" not in line:
                continue
            # 跨脚本引用（README 里的 kb_manage.py --file 之类）不归本检查管：
            # 那是另一个脚本的参数，在本文档里出现是正常的。
            if re.search(r"[\w/\\-]+\.py", line) and "fde_run.py" not in line \
                    and "lib.pipeline" not in line:
                continue
            for m in re.finditer(r"(--[a-z][a-z0-9-]+)", line):
                if m.group(1) not in registered:
                    out.append(Finding(
                        "S5", "HIGH", doc, i,
                        "文档提到的参数「%s」在代码里不存在" % m.group(1),
                        "使用者的第一动作是照文档抄命令，抄到不存在的参数就卡死在第一步，"
                        "还会连带怀疑整份文档的可信度。",
                        "补上参数实现，或改掉文档"))
            for m in re.finditer(r"fde_run\.py\s+([a-z][a-z0-9_-]+)", line):
                if m.group(1) not in subs:
                    out.append(Finding(
                        "S5", "HIGH", doc, i,
                        "文档提到的子命令「%s」在 fde_run.py 里不存在" % m.group(1),
                        "SKILL.md 把编排器固化为第一动作，"
                        "第一动作指向一个空入口等于整条链路断在起点。",
                        "补上子命令，或改掉文档"))
    return out


# ------------------------------------------------------------------- S6
def scan_unreachable():
    """不可达分支：恒假条件、恒真条件、无出口死循环。"""
    out = []
    for rel in _py_files():
        text = _read(rel)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test = node.test
                # 恒假：if False:
                if isinstance(test, ast.Constant) and test.value is False:
                    out.append(Finding(
                        "S6", "HIGH", rel, node.lineno,
                        "恒假分支，整段代码永不执行",
                        "很可能是调试时临时关掉的分支忘了恢复，"
                        "也可能是刻意留的死代码——两种都得处理掉。",
                        "恢复条件，或删除该分支"))
                elif isinstance(test, ast.Constant) and test.value is True:
                    out.append(Finding(
                        "S6", "LOW", rel, node.lineno,
                        "恒真条件，if 判断多余", "条件永远成立，等同于把代码提到 if 外。",
                        "确认是不是漏写了判断条件"))
            if isinstance(node, ast.While):
                is_true = (isinstance(node.test, ast.Constant)
                           and node.test.value is True)
                if is_true:
                    # 出口不止 break：return / raise 同样是出口。
                    # 只认 break 的话，「while True: ... return x」会被误报成死循环
                    # （实测 kbstore.next_cid 就是这种写法）。
                    has_break = any(isinstance(b, (ast.Break, ast.Return,
                                                   ast.Raise))
                                    for b in ast.walk(node))
                    if not has_break:
                        out.append(Finding(
                            "S6", "HIGH", rel, node.lineno,
                            "while True 无出口", "死循环，跑起来会挂住整个任务。",
                            "补 break / return / raise"))
                _ = node
    return out


# ------------------------------------------------------------------- S7
def scan_hardcoded_duplicate():
    """硬编码副本：同一份「清单型」数据在两处各写一份。

    判据：模块级列表/元组常量，长度 >= 4 且元素全是短字符串，
    且在两个以上文件里出现内容高度重合的同类常量。
    （EXCLUDE_DIRS 这个坑踩过两次：改了 A 处忘了 B 处。）
    """
    out = []
    buckets = {}
    for rel in _py_files():
        text = _read(rel)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, (ast.List, ast.Tuple)):
                continue
            elts = node.value.elts
            if len(elts) < 4:
                continue
            vals = [e.value for e in elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if len(vals) != len(elts) or not vals:
                continue
            for t in node.targets:
                if isinstance(t, ast.Name):
                    buckets.setdefault(t.id, []).append((rel, node.lineno, set(vals)))
    for name, locs in sorted(buckets.items()):
        if len(locs) < 2:
            continue
        for i in range(len(locs)):
            for j in range(i + 1, len(locs)):
                a, b = locs[i][2], locs[j][2]
                inter = a & b
                if not inter:
                    continue
                ratio = float(len(inter)) / max(1, len(a | b))
                if ratio >= 0.5:
                    out.append(Finding(
                        "S7", "HIGH", locs[j][0], locs[j][1],
                        "清单常量「%s」在两处重复定义（重合度 %.0f%%）" % (
                            name, ratio * 100),
                        "改一处忘一处是必然的，不是意外。"
                        "两处迟早失同步，而失同步后两边都还在正常跑。",
                        "抽到单一来源（知识库或公共常量），另一处改为引用"))
    return out


def scan_call_signature():
    """跨模块调用的实参/形参不匹配。

    2026-10-26 实测抓到的真 bug：
        XP.upstream_of(proc, procs, chain_hops=4)
    而签名是 upstream_of(process, chain_hops=4) —— 多传了一个位置参数，
    每次调用都抛 TypeError，然后被同一函数里的 `except: pass` 吞掉。
    结果：跨工序追溯的上下游查找一直是死的，横向排查清单只剩本工序，
    而 293 项主自检全绿 —— 被调用了、崩了、没人知道。

    这类 bug 的特征是：异常被静默吞掉 + 调用形态错误，两者叠加。
    本检查直接盯调用形态，不依赖运行时有没有崩。
    """
    out = []
    # 1) 收集全库函数签名
    sigs = {}          # (module, funcname) -> (argnames, has_vararg, defaults_n)
    for rel in _py_files():
        mod = os.path.splitext(os.path.basename(rel))[0]
        text = _read(rel)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                names = [x.arg for x in a.posonlyargs] + [x.arg for x in a.args]
                if a.vararg:
                    names.append("*" + a.vararg.arg)
                sigs[(mod, node.name)] = (names, bool(a.vararg), len(a.defaults))

    # 2) 检查形如 <别名>.func(...) 的跨模块调用
    alias_map = {}     # 文件内局部别名 -> 模块名
    for rel in _py_files():
        text = _read(rel)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        alias_map[rel] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for al in node.names:
                    alias_map[rel][al.asname or al.name] = al.name
                    if al.name.endswith(" as " + (al.asname or "")):
                        pass
        # `from . import crossprocess as XP`
        for m in re.finditer(r"from\s+\.?\s*import\s+(\w+)\s+as\s+(\w+)", text):
            alias_map[rel][m.group(2)] = m.group(1)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, ast.Name):
                continue
            target_mod = alias_map.get(rel, {}).get(fn.value.id)
            if not target_mod:
                continue
            key = (target_mod, fn.attr)
            if key not in sigs:
                continue
            names, has_vararg, n_def = sigs[key]
            if has_vararg:
                continue
            n_pos = len(node.args)
            kw_only = set(k.arg for k in node.keywords if k.arg)
            max_pos = len([n for n in names if not n.startswith("*")])
            min_pos = max_pos - n_def
            # 位置实参过多，或位置实参不够且没用关键字补齐
            too_many = n_pos > max_pos
            too_few = n_pos + len(kw_only) < min_pos
            dup = kw_only & set(names[:n_pos])      # 集合，下面要遍历
            why = None
            if too_many:
                why = "位置实参 %d 个，形参最多 %d 个" % (n_pos, max_pos)
            elif dup:
                why = "同一参数既按位置又按关键字传入：%s" % "、".join(sorted(dup))
            elif too_few:
                why = "必填参数没给够（给了 %d 个，至少需要 %d 个）" % (
                    n_pos + len(kw_only), min_pos)
            if why:
                out.append(Finding(
                    "S8", "HIGH", rel, node.lineno,
                    "调用 %s.%s() 的实参/形参不匹配：%s" % (target_mod, fn.attr, why),
                    "这种调用 100% 抛 TypeError。若它被同一函数里的 except 吞掉，"
                    "功能就变成死的——调用了、崩了、没人知道，自检还全绿。",
                    "按签名 %s(%s) 改正调用" % (fn.attr, ", ".join(names))))
    return out


CHECKS = [
    ("S1", "静默吞异常", scan_silent_except),
    ("S2", "定义未调用（死代码）", scan_dead_code),
    ("S3", "知识库条目悬空", scan_dangling_knowledge),
    ("S4", "参数透传链断裂", scan_param_chain),
    ("S5", "文档承诺不存在的入口", scan_doc_promise),
    ("S6", "不可达分支/死循环", scan_unreachable),
    ("S7", "清单型硬编码副本", scan_hardcoded_duplicate),
    ("S8", "跨模块调用签名不匹配", scan_call_signature),
]


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="静态全库扫描器")
    ap.add_argument("--only", default="", help="只跑指定检查，如 S1,S2")
    ap.add_argument("--json", default=None, help="结果落盘路径")
    ap.add_argument("--strict", action="store_true",
                    help="存在 HIGH 级问题时以退出码 1 结束（供 CI 用）")
    ap.add_argument("--quiet", action="store_true", help="只打印汇总行")
    args = ap.parse_args(argv)

    wanted = set(x.strip().upper() for x in args.only.split(",") if x.strip())
    findings = []
    ran = []
    for sid, title, fn in CHECKS:
        if wanted and sid not in wanted:
            continue
        try:
            got = fn()
        except Exception as e:
            got = [Finding(sid, "MED", "<scanner>", 0,
                           "检查项 %s 自身异常" % sid, str(e))]
        findings.extend(got)
        ran.append((sid, title, len(got)))

    findings.sort(key=lambda f: (SEV_ORDER.get(f.sev, 9), f.sid, f.rel, f.line))

    if not args.quiet:
        print("=" * 72)
        print("静态全库扫描：%d 项检查，共 %d 条发现" % (len(ran), len(findings)))
        print("=" * 72)
        for sid, title, n in ran:
            print("  %s %s：%d 条" % (sid, title, n))
        print("-" * 72)
        cur = None
        for f in findings:
            if f.sev != cur:
                cur = f.sev
                print("\n【%s】" % cur)
            print("  [%s] %s:%s" % (f.sid, f.rel, f.line or "-"))
            print("       %s" % f.what)
            print("       为什么算问题：%s" % f.why)
            if f.hint:
                print("       怎么改：%s" % f.hint)
        if not findings:
            print("\n  全库扫描未发现问题。")
        print("\n" + "=" * 72)

    n_high = sum(1 for f in findings if f.sev == "HIGH")
    n_med = sum(1 for f in findings if f.sev == "MED")
    print("汇总：HIGH %d / MED %d / LOW %d —— 共 %d 条" % (
        n_high, n_med,
        sum(1 for f in findings if f.sev == "LOW"), len(findings)))
    # 豁免数单独报：它衡量的是「有多少盲区被主动确认过」。
    # 不报的话，把静默异常标注成豁免就成了另一种「让它消失」。
    if _KEPT:
        print("已豁免 %d 条（源码里写明 scan:keep 理由，属决策不是盲区）" % len(_KEPT))
        for k in _KEPT[:20]:
            print("    · %s:%s  %s" % (k["file"], k["line"], k["reason"][:60]))

    if args.json:
        try:
            d = os.path.dirname(args.json)
            if d:
                os.makedirs(d, exist_ok=True)
            with io.open(args.json, "w", encoding="utf-8") as f:
                json.dump({"summary": {"high": n_high, "med": n_med,
                                       "total": len(findings),
                                       "kept": len(_KEPT)},
                           "findings": [x.as_dict() for x in findings],
                           "kept": _KEPT},
                          f, ensure_ascii=False, indent=2)
            print("落盘：" + args.json)
        except Exception as e:
            print("落盘失败：%s" % e)

    if args.strict and n_high:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
