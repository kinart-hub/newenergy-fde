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
S3 知识库悬空：条目引用了不存在的参数、或没有任何现场叫法能命中它、
              或别名被多个条目抢用。（不是「键名在代码里出现」——见函数内说明）
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

# 向上寻找 scan:keep 时最多回溯多少行。12 行足以覆盖一段完整的理由注释
# （实测最长的一段是 4 行），又不至于把上一段代码的注释误认进来——
# 中间一旦遇到真实代码行就会停。这里写死上限而不是无限回溯，
# 是为了让"注释块偶然紧贴着上一行代码"时不会一路吃到文件头。
_KEEP_LOOKUP = 12


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

    按 handler 的真实起止行而不是「except 行 ±3」：多行 handler
    （except + 若干行说明 + pass）按固定行数算会整个落在窗口外，
    豁免就静默失效——而豁免失效的后果是它又被报一遍，
    逼着人要么重复标注要么放弃使用豁免机制。

    但光有真实行号还不够，这个坑**同一个文件踩过第三次**：
    实测 lib/devices.py 里 `# scan:keep ……` 那一段注释写了 4 行，
    handler 落在第 139 行，窗口按 pad=2 只覆盖到 137，
    恰好把首行（唯一带 scan:keep 的那行）隔在外面 → 豁免失效。
    根因是"注释有几行"不固定，用固定 pad 去猜注定要对错 blessed。
    所以这里改成**向上吞掉整段连续注释**：只要上一行是注释或空行就继续上溯，
    上限 _KEEP_LOOKUP 行防止极端情况失控。
    """
    # 向上：吞掉整段连续注释。注释行数不定，按行数是猜不准的，
    # 所以一路吃到"非注释且非空行"为止。
    up = lo_1based - 1          # 先取 handler 首行
    i = up - 1
    for _ in range(_KEEP_LOOKUP):
        if i < 0:
            break
        raw = lines[i].strip()
        if not raw or raw.startswith("#"):
            up = i
            i -= 1
            continue
        break                             # 撞到真正的代码行，停
    # 主搜索：handler 的**整个行区间** + 下方 pad。
    # 上一版只从 end_lineno 往下找 2 行，漏掉了"理由写在 handler 体内、
    # 且在 pass 之前"的写法（lib/direction.py、lib/pipeline.py 都是这种），
    # 于是明明写了理由照样被报。这里一次性覆盖整段，不再靠 pad 猜。
    for i in range(up, min(len(lines), hi_1based + pad)):
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

    !!! 这里踩过一个足以让整条规则失效的坑（2026-10-27 外部核查对拍发现）!!!
    旧判据是 `if not isinstance(b, ast.Pass)`——只把 pass 当成"没做事"，
    于是 `except Exception: continue` 的 handler body 里剩下一个 Continue 节点，
    body 非空 → 直接跳过 → **这条规则看不见 continue 型的静默吞异常**。
    实测：外部核查用独立脚本扫出 16 条无台账静默，本扫描器报 0 条，
    两边差异几乎全落在这个盲区里。
    更糟的是它不报错、"全库扫描未发现问题"照常打印——
    **扫描器对自己的盲区保持安静，比没这个扫描器更危险。**
    """
    out = []
    for rel in _py_files():
        text = _read(rel)
        try:
            tree = ast.parse(text)
        except SyntaxError as _e:
            # 这个文件解析不了 -> S1 对它一行都没检查。不写出来的话，
            # "S1 未发现问题"会被读成"全库都没有静默吞异常"，实际漏了一整份文件。
            # 审查工具不能对自己的漏检保持安静，所以这里必须显式留痕。
            sys.stderr.write("[S1] 跳过无法解析的文件 %s：%r\n" % (rel, _e))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            # Pass 与 Continue 都代表"什么都不留下"，两者必须同等对待。
            # 漏掉 Continue，循环里的静默吞异常就永远扫不出来。
            body = [b for b in node.body
                    if not isinstance(b, (ast.Pass, ast.Continue))]
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
        except SyntaxError as _e:
            # 解析不了的文件不会被收进 defs，它的函数名会被 S2 判成"定义了没调用"
            # ——报的是这份文件根本没被看懂，不是真的没人调用，必须留痕区分。
            sys.stderr.write("[S2] 跳过无法解析的文件 %s：%r\n" % (rel, _e))
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
def _norm(s):
    """列名归一化。与 lib/preprocess._norm_name 同源，导入失败时用等价实现兜底。

    为什么要导入而不是复制一份：别名是否撞车、能否命中，完全取决于归一化结果。
    扫描器自己抄一份判据，两侧一旦漂移，扫描器就会用另一套口径下结论——
    「扫出来没问题」就成了假的。
    """
    try:
        sys.path.insert(0, ROOT)
        from lib import preprocess as _PP
        return _PP._norm_name(s)
    except Exception:
        s = re.sub(r"[\(（][^)）]*[\)）]", "", str(s))
        s = re.sub(r"[\s_\-/、,，.。:：\[\]【】]+", "", s)
        return s.lower()


def scan_dangling_knowledge():
    """知识库悬空：条目定义了，但**现场叫法命中不到它**，或它引用了不存在的条目。

    ⚠️ 这里踩过一个判据错误，写新判据前先看这段：
    旧判据是「缺陷键名在代码文本里出现」。这是错的——缺陷匹配是**数据驱动**的，
    键名与 aliases 都由知识库读进来参与列名匹配，代码里本来就不会写这些名字。
    所以旧判据实际测的是「这条碰巧被硬编码进 _BAD_HINT 了吗」，
    结果：新补的 6 条正常条目被判悬空，而真正无别名可命中的「极片错位」反而没报。

    真正会断链的是下面三类，判据就按这三类写：
      1. param_key / likely_params 指向的 params 条目不存在 —— 引用悬空，
         取参数时拿到空字典，Cpk/规格全部退回推定值还不报错。
      2. 没有任何别名来源 —— 既无 aliases，param_key 在 params 里也没有别名。
         只有列名与条目全名一字不差才命中，现场叫法一律匹配不上，
         现场数据进来走「未收录」兜底，D4 机理栏写「机理未收录」。
      3. 归一化后两个不同缺陷条目共用同一别名 —— 匹配歧义，
         命中哪一条全看字典遍历顺序，同份数据换台机器结果可能不同。
    """
    out = []
    kdir = os.path.join(ROOT, "knowledge")
    if not os.path.isdir(kdir):
        return out
    alias_owner = {}       # 归一化别名 -> [(文件, 条目键)]
    for fn in sorted(os.listdir(kdir)):
        if not fn.endswith(".json"):
            continue
        try:
            data = json.loads(_read(os.path.join("knowledge", fn)) or "{}")
        except Exception as _e:
            # 知识库文件坏掉时 S3 直接跳过它：悬空/抢别名的检查对这份文件一行都没做，
            # 输出却是"S3 无发现"。这类文件本身是运行时必崩的，越早露面越好。
            sys.stderr.write("[S3] 跳过无法解析的知识库文件 %s：%r\n" % (fn, _e))
            continue
        dr = data.get("defect_rootcause") or {}
        if not dr:
            continue
        params = data.get("params") or {}
        for key in dr:
            if not isinstance(key, str) or not key:
                continue
            dv = dr[key] or {}
            where = os.path.join("knowledge", fn)

            # --- 1. 引用悬空：param_key / likely_params 指向不存在的 params 条目
            refs = []
            if dv.get("param_key"):
                refs.append(dv["param_key"])
            refs += [p for p in (dv.get("likely_params") or []) if isinstance(p, str)]
            missing = [r for r in refs if r and r not in params]
            if missing:
                out.append(Finding(
                    "S3", "MED", where, 0,
                    "缺陷条目「%s」引用的参数在 params 里不存在：%s"
                    % (key, "、".join(sorted(set(missing))[:5])),
                    "取规格时拿到空字典，Cpk 与上下限全部退回知识库推定值，"
                    "报告照出、数字是猜的、还不报错。",
                    "在 params 里补上这些条目，或把引用改成已有的参数键"))

            # --- 2. 无别名可命中
            names = [key] + list(dv.get("aliases") or [])
            pk = dv.get("param_key")
            if pk and not dv.get("response_defect"):
                names += list((params.get(pk) or {}).get("aliases") or [])
            normed = sorted({_norm(n) for n in names if n and _norm(n)})
            # 键名本身就只有一个来源、且没有更短的现场叫法可命中
            if len(normed) <= 1:
                out.append(Finding(
                    "S3", "MED", where, 0,
                    "缺陷条目「%s」没有任何现场叫法别名，只有键名本身可命中" % key,
                    "现场列名不会跟条目全名一字不差（叫「水分超标」不叫「电芯水分超标」），"
                    "于是这条机理永远匹配不上，数据进来走「未收录」兜底，"
                    "工程师拿到的 D4 栏只能写着「机理未收录」。",
                    "在条目的 aliases 里补上现场叫法（短名、口语名、英文名）"))

            # --- 3. 别名撞车
            # 只在**同一工序内**才算真歧义。跨工序撞车（「焊接不良」同时是
            # 电池卷绕的「极耳焊接不良」和光伏 EL 的「虚焊」）是正常现象：
            # 现场叫法本来就跨行业通用，运行时由 identify_defects 的
            # 「工序优先裁决」化解（审计规则 34 守着这条裁决不许退化）。
            # 若这里不分工序一律报警，报的全是运行时已经处理好的情况，
            # 反而把同工序内的真歧义淹掉——那才是没人管的。
            for n in normed:
                scope_key = (fn, dv.get("process") or "")
                alias_owner.setdefault((scope_key, n), []).append(key)

    for (scope_key, n), owners in sorted(alias_owner.items(),
                                         key=lambda kv: str(kv[0])):
        uniq = sorted(set(owners))
        if len(uniq) > 1:
            out.append(Finding(
                "S3", "MED", os.path.join("knowledge", scope_key[0]), 0,
                "工序「%s」内别名「%s」被多个缺陷条目同时占用：%s"
                % (scope_key[1] or "未标工序", n, "、".join(uniq)),
                "两个机理抢同一个列名，而运行时裁决的第一级就是工序——"
                "同工序内撞车意味着工序优先也救不了，命中哪一条仍看遍历顺序，"
                "同一份数据在不同机器上可能给出不同根因。",
                "给其中一条改别名，或在条目上标明优先级"))
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
    # 以及形参名与实际 flag 不一致的（单复数/位置参数），逐个写清对应关系，
    # 不能靠"名字差不多"自动放行——放行一个就再也看不见真的漏暴露。
    internal = {"spec_overrides", "d2_card", "emit_d2_card", "export_xlsx",
                "do_doe", "max_charts", "auto_adapt", "sheet"}
    alias = {
        "paths": "-i/--input（位置参数，不是 --paths）",
        "views": "--view（单数，nargs='+'）",
        "on_conflict": "--dedup-conflict（形参叫 on_conflict 是因为它是去重函数的"
                       "通用参数；命令行上归到 --dedup-key 那一组里更好找）",
        "msa_spec": "--msa-data / --msa-part / --msa-appraiser / --msa-value / "
                    "--msa-pct-grr / --msa-ndc / --msa-tolerance（一个 dict 由这组参数"
                    "组装；不收单个 JSON 串，免得现场在命令行里手写转义过的 JSON）",
        # 反向开关：默认导出、要关才传。写成 --no-xxx 是因为"默认给全"才符合
        # 现场习惯（报告本来就该带 Word/PDF），不写 --export-docx 那种正向开关
        # 免得多一次"忘了传就没有"的踩坑。
        "export_docx": "--no-docx（反向开关：默认导出 Word，传了才不导）",
        "export_pdf": "--no-pdf（反向开关：默认导出 PDF，传了才不导）",
    }
    for p in sig:
        flag = "--" + p.replace("_", "-")
        if p in internal:
            continue
        if flag in cli:
            continue
        if p in alias:
            _KEPT.append({"id": "S4", "file": os.path.join("lib", "pipeline.py"),
                          "line": 0,
                          "reason": "形参「%s」的 CLI 入口是 %s，不是 %s" % (p, alias[p], flag)})
            continue
        out.append(Finding(
            "S4", "MED", os.path.join("lib", "pipeline.py"), 0,
            "analyze() 形参「%s」在 CLI 里没有对应参数" % p,
            "库调用者能传，命令行使用者传不了——同一份能力只对一半人开放。",
            "在 argparse 里补上 %s，或明确标注它仅供库调用" % flag))

    # 动态透传：编排器不再手写 flag 白名单，改为运行时从 pipeline 的 argparse 取。
    # 静态扫不到具体 flag，所以判据换成两层：
    #   ① 动态构造必须还在（被删了就退回逐条静态核对，漏透传立刻显形）
    #   ② 必须有运行时校验兜底（selftest 里真的跑一遍子进程比对），
    #      否则"我说我全透传了"就是一句没人验证的话。
    m_dyn = re.search(r"cmd\s*\+=\s*passthrough_flags\(\s*args\s*,\s*pipeline_flag_specs\(\)\s*\)", run)
    dyn_line = run[:m_dyn.start()].count("\n") + 1 if m_dyn else 0
    if not m_dyn:
        for flag in sorted(cli):
            if flag in passed:
                continue
            if flag in ("--input", "-i"):
                continue
            out.append(Finding(
                "S4", "LOW", os.path.join("scripts", "fde_run.py"), 0,
                "CLI 参数「%s」未被编排器透传" % flag,
                "走 fde_run.py 编排入口时这个参数会丢失，"
                "两个入口行为不一致——使用者会以为自己设了，其实没生效。",
                "在 flow_analyze 里补上透传"))
        return out

    guard = _read(os.path.join("scripts", "selftest.py"))
    if "def test_entry_parity" not in guard:
        out.append(Finding(
            "S4", "MED", os.path.join("scripts", "fde_run.py"), dyn_line,
            "参数透传改成动态生成，但 selftest 里没有运行时校验兜底",
            "透传是不是真的生效，静态已经看不出来了。没有运行时比对，"
            "哪天透传断了无人知晓——参数被静默丢弃。",
            "在 scripts/selftest.py 里补 test_entry_parity："
            "子进程比对两个入口的 --help，并端到端验证一个开关真的生效"))
    for flag in sorted(cli):
        if flag in ("--input", "-i"):
            continue
        _KEPT.append({"id": "S4", "file": os.path.join("scripts", "fde_run.py"),
                      "line": dyn_line,
                      "reason": "「%s」由 passthrough_flags() 动态透传，"
                                "静态看不到，已由 selftest test_entry_parity 运行时校验" % flag})
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
        except SyntaxError as _e:
            # 解析不了 -> S6 对这份文件完全没跑，"没有不可达分支"的结论里
            # 不含它。结论覆盖范围缺一角却看不出来，所以这里必须显式留痕。
            sys.stderr.write("[S6] 跳过无法解析的文件 %s：%r\n" % (rel, _e))
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
        except SyntaxError as _e:
            # 解析不了 -> S7 从没看过这份文件里的清单常量，"没有硬编码副本"
            # 实际上没把它算进去。S7 以前就踩过两副本的坑，漏检不能静默。
            sys.stderr.write("[S7] 跳过无法解析的文件 %s：%r\n" % (rel, _e))
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
        except SyntaxError as _e:
            # S8 靠各自文件的 AST 比对调用签名：某文件解析不了时，它既没有被检查，
            # 也不会出现在对面文件的调用统计里，签名不匹配会被整体漏掉。必须留痕。
            sys.stderr.write("[S8] 跳过无法解析的文件 %s：%r\n" % (rel, _e))
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
        except SyntaxError as _e:
            # 别名表缺这一份文件 -> 该文件里的 <别名>.func(...) 调用不再被核对，
            # S8 会报"签名匹配"，实际是这块从没比过。必须留痕。
            sys.stderr.write("[S8] 跳过无法解析的文件 %s：%r\n" % (rel, _e))
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
