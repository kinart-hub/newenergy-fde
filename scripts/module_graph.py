# -*- coding: utf-8 -*-
"""
module_graph.py —— 模块协作图与「孤岛模块」检测

=============================================================================
这份脚本要回答的质问（使用者的原话）
=============================================================================
    "整个 skill 工具内的所有模块，如果有孤岛模块出现，那证明这个孤岛模块
     既没有数据的分发，甚至连其它模块的推导反馈结果也无法取得，
     对于这个模块存在的必要性也要进行质疑和验证。
     同时依据这条也可以反证这个 skill 工具本身是有缺陷问题的——
     模块与模块之间至少会包含一条（甚至更多）的关联属性才是正确的，
     不然就没法进行模块之间的协同工作，数据没法进行交换和印证，
     也没法形成系统闭环，得出的报告结果也是不可信的。"

    这段话给出了一条**可机器判定的公理**：

        一个模块存在的理由 = 它在数据链上既有上游、又有下游。
        上游给它输入（别的模块的推导反馈结果），
        下游拿走它的产出（数据的分发）。

    两头都没有 = 孤岛；只有一头 = 断头路。
    无论哪种，它产出的那部分结论都**没有经过任何交叉印证**，
    于是整份报告的"闭环"是假的。

=============================================================================
为什么只看 import 不够（这是本脚本与"导入图"的根本区别）
=============================================================================
    实测：46 个 lib 模块里 45 个从 CLI 入口 import 可达——看起来很健康。
    但 `--device` 那次事故就是活生生的反例：analyze / ingest /
    identify_device 三段**都 import 了、都支持**，只差 argparse 没定义、
    main 没传，结果帮助文本写着、实际静默无效。

    import 只证明"文件被打开了"，不证明"数据从这里流到了那里"。
    所以本脚本的判定单元是**跨模块函数调用**（谁调了谁的哪个函数、
    返回值有没有被接住），而不是 import 语句。

=============================================================================
三类检测
=============================================================================
    F1 ISLAND      完全孤岛：既不调用别人、也不被别人调用（关联数 0）。
                   —— 两头都没有，模块存在的必要性存疑
    F2 GHOST       幽灵模块：只有自检/审计脚本调它，生产链路零调用。
                   —— 看着不是孤岛（有人调），但调它的是测试，不是产品
    F3 UNCONSUMED  纯孤儿：自己调用了别人（拿得到输入），
                   但**没有任何调用方**，连自检都不碰它。
                   —— 它在消耗全系统的能力，却没人拿它的结果

    **为什么"自己不调用任何 lib 模块"不算缺陷**（这条是踩过坑后定的）：
    叶子工具模块（scope / status / observ 只提供状态与常量）出度天然为 0，
    但它们的产出被多个模块读取——**入度才是"数据的分发"**。
    首版把出度=0 报成"断头路"，一口气报出 8 条全是正常叶子，
    这种噪声会让整套审计被当成狼来了。判据只认**入度=0**的三档。

    另外输出**模块协作表**（每个模块的上游/下游/关联数），
    让"至少一条关联属性"这条公理可以被逐模块核对。

=============================================================================
零第三方依赖：stdlib only（ast / io / os / glob / json / argparse）。
"""

import argparse
import ast
import collections
import glob
import io
import json
import os
import sys

_SKIPPED = []          # 解析失败台账：解析不了必须说，不能当"很干净"
_CLASSIFIED = []       # 归类为非缺陷的条目，必须打印条数

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _rel(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")


def _read(p):
    try:
        return io.open(p, encoding="utf-8").read()
    except Exception:
        return ""


# ---------------------------------------------------------------- 生产链路
# "生产链路" = 交付给使用者的那条路：lib 全部 + 两个真 CLI。
# 其余 scripts（自检/审计/造数/打包）是**验证与工具**，
# 它们调用某个模块只能证明"被测过"，不能证明"产品在用"。
# 这条区分是 F2 的全部依据——分错就会把幽灵判成健康。
PROD_SCRIPTS = ("scripts/fde_run.py", "scripts/kb_manage.py")

# 入口：从这些文件出发做可达性分析
ENTRY = ("scripts/fde_run.py", "scripts/kb_manage.py")

# 白名单：每条必须写 reason。允许的类型：
#   package  —— 包标记/兼容层，本就不参与数据流
#   platform —— 平台或外部入口调用，静态看不见
MODULE_WHITELIST = {
    "lib/__init__.py": ("package", "包标记文件；不参与数据流，"
                                   "存在意义是让 `from . import x` 成立。"),
}


def collect_files():
    out = []
    out.extend(sorted(glob.glob(os.path.join(ROOT, "lib", "*.py"))))
    out.extend(sorted(glob.glob(os.path.join(ROOT, "scripts", "*.py"))))
    return out


# 自己的产物必须排除：本脚本里写了 "lib/__init__.py" 这种文件名字符串，
# 若把自己算进调用图，`lib/__init__` 就会因为"被 module_graph.py 提到过"
# 而从"完全孤岛"降级成"幽灵"——**审计脚本把要报的问题治好了**。
# 这个坑在 coherence_audit 上踩过一次，这里是同一个坑的第二形态（子进程边）。
_SELF_ARTIFACTS = ("scripts/module_graph.py",
                   "knowledge/module_island_baseline.json")


def _modkey(path):
    return _rel(path)[:-3]          # lib/stats | scripts/fde_run


# 子进程启动另一个模块的写法：`[PY, "lib/pipeline.py"]`、`python lib/xxx.py`
import re as _re
_SUBPROCESS_RE = _re.compile(r'["\']((?:lib|scripts)/[\w./-]+\.py)["\']'
                             r'|["\']((?:lib|scripts)\\.[\w./-]+\\.py)["\']')


# ---------------------------------------------------------------- 别名解析
# 跨模块调用的识别全靠"别名 → 模块"这张表。
# 本项目混用了四种导入写法（包模式/脚本模式/as 别名/from...import 函数），
# 只认其中一种会大面积漏边（实测首版把 45 个模块判成孤岛）。
def _alias_map(tree, selfmod):
    """返回 (别名→模块key, 直接导入的函数名→模块key)。"""
    alias = {}
    funcs = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            base = n.module or ""
            # from . import x  /  from lib import x  /  from lib.x import y
            cands = []
            if base in ("lib", "") or base.endswith(".lib"):
                cands = ["lib"]
            elif base:
                b = base.split(".")[-1]
                cands = ["lib/" + b, base.replace(".", "/")]
            for al in n.names:
                # `from lib import stats as S` → S → lib/stats
                # `from . import stats as S`   → S → lib/stats
                # `from lib.stats import mean` → mean() → lib/stats
                if len(cands) == 1 and cands[0] == "lib":
                    if al.name == selfmod.split("/")[-1]:
                        continue
                    key = "lib/" + al.name
                else:
                    key = cands[0] if cands else None
                if not key:
                    continue
                if al.asname:
                    alias[al.asname] = key
                else:
                    # 无 asname：`from . import stats` → 名字 stats 直接指模块
                    alias[al.name] = key
                    # 也可能是 `from lib.stats import mean`：那 al.name 是函数
                    if key.startswith("lib/") and al.name not in (
                            key.split("/")[-1],):
                        funcs[al.name] = key
        elif isinstance(n, ast.Import):
            for al in n.names:
                b = al.name.split(".")[-1]
                key = None
                if al.name.startswith("lib."):
                    key = "lib/" + b
                elif b == selfmod.split("/")[-1]:
                    continue
                else:
                    # `import degrade`（脚本模式无包上下文）
                    if os.path.exists(os.path.join(ROOT, "lib", b + ".py")):
                        key = "lib/" + b
                if not key:
                    continue
                alias[al.asname or b] = key
    return alias, funcs


def build_graph(files=None, root=None):
    """构建跨模块调用图。

    返回 dict：
      nodes   —— {模块key: {"public": [公开函数名], "prod": bool}}
      edges   —— [(caller, callee, func)]，跨模块调用边
      by_mod  —— {模块key: {"out": {被调模块: {函数}}, "in": {调用方: {函数}}}}
      prod_edges / test_edges —— 按调用方是否生产链路拆分
    """
    global ROOT
    if root:
        ROOT = root
    del _SKIPPED[:]
    del _CLASSIFIED[:]
    if files is None:
        files = [f for f in collect_files() if _rel(f) not in _SELF_ARTIFACTS]
    nodes = {}
    edges = []
    for f in files:
        mk = _modkey(f)
        src = _read(f)
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            _SKIPPED.append({"file": mk, "error": "%s: %s"
                             % (type(e).__name__, str(e)[:80])})
            continue
        pub = [n.name for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and not n.name.startswith("_")]
        nodes[mk] = {"public": pub,
                     "prod": mk.startswith("lib/") or _rel(f) in PROD_SCRIPTS}
        alias, funcs = _alias_map(tree, mk)

        def _add(callee, name, line, how):
            if not callee or callee == mk:
                return
            if not os.path.exists(os.path.join(ROOT, callee + ".py")):
                return
            edges.append({"caller": mk, "callee": callee, "func": name,
                          "line": line, "how": how,
                          "prod": nodes.get(mk, {}).get("prod", False)})

        for n in ast.walk(tree):
            # ① 函数调用：ALIAS.func(...)、直接导入的 func(...)
            if isinstance(n, ast.Call):
                fn = n.func
                if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                    _add(alias.get(fn.value.id), fn.attr, n.lineno, "call")
                elif isinstance(fn, ast.Name):
                    _add(funcs.get(fn.id), fn.id, n.lineno, "call")
            # ② 常量/属性读取：ST.SUCCESS、SC.VERSION 这类不是调用，
            #    但同样是"我用了你的东西"——不认这条会把 status 这种
            #    纯常量模块判成孤岛（首版实测误报）。
            elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
                _add(alias.get(n.value.id), n.attr, n.lineno, "attr")

        # ③ 跨进程边：`[PY, "lib/pipeline.py"]` 这种 subprocess 启动。
        #    fde_run 正是这样调 pipeline 的——文件里没有任何 import/call，
        #    只认 AST 会把**核心模块**判成幽灵（实测误报，最危险的一种：
        #    它让审计的"权威结论"直接矛盾于常识，于是整套被弃用）。
        for m in _SUBPROCESS_RE.finditer(src):
            for cand in (m.group(1), m.group(2)):
                if not cand:
                    continue
                b = os.path.basename(cand)
                if b.endswith(".py"):
                    b = b[:-3]
                _add("lib/" + b, "<subprocess>", 0, "subprocess")
    by_mod = {m: {"out": collections.defaultdict(set),
                  "in": collections.defaultdict(set),
                  "out_prod": collections.defaultdict(set),
                  "in_prod": collections.defaultdict(set)} for m in nodes}
    for e in edges:
        by_mod[e["caller"]]["out"][e["callee"]].add(e["func"])
        by_mod[e["callee"]]["in"][e["caller"]].add(e["func"])
        if e["prod"]:
            by_mod[e["caller"]]["out_prod"][e["callee"]].add(e["func"])
            by_mod[e["callee"]]["in_prod"][e["caller"]].add(e["func"])
    return {"nodes": nodes, "edges": edges, "by_mod": by_mod}


# ---------------------------------------------------------------- 检测

def detect(g, include_whitelisted=False):
    """三类孤岛检测。只针对 lib 模块——scripts 是入口/工具，不适用。"""
    items = []
    for mk, info in sorted(g["nodes"].items()):
        if not mk.startswith("lib/"):
            continue
        bm = g["by_mod"][mk]
        # 出度：本模块调用了哪些 lib 模块（含常量读取与子进程启动）
        out_lib = {k: v for k, v in bm["out"].items() if k.startswith("lib/")}
        # 入度：谁调用了本模块。分"生产"与"任意"两层——
        # 只有生产入度才证明"产品在用"，测试入度只证明"被测过"。
        in_prod = {k: v for k, v in bm["in"].items()
                   if k.startswith("lib/") or k + ".py" in PROD_SCRIPTS}
        in_any = dict(bm["in"])
        wl = MODULE_WHITELIST.get(mk + ".py")

        if not out_lib and not in_any:
            kind = "ISLAND"
            detail = ("既不调用任何 lib 模块，也没有任何调用方——"
                      "既不分发数据、也拿不到别的模块的推导结果，存在必要性存疑")
        elif not in_prod and in_any:
            callers = sorted(set(in_any) - set(in_prod))
            kind = "GHOST"
            detail = ("只有自检/审计/工具脚本调用（%s），生产链路零调用——"
                      "它『被测过』不等于『产品在用』"
                      % "、".join(callers[:4]))
        elif not in_any:
            kind = "UNCONSUMED"
            detail = ("自己调用了 %s，但没有任何调用方（连自检都不碰它）——"
                      "它在消耗全系统的能力，却没人拿它的结果"
                      % "、".join(sorted(out_lib)[:4]))
        else:
            continue
        items.append({"kind": kind, "file": mk + ".py", "name": mk,
                      "whitelisted": bool(wl),
                      "reason": wl[1] if wl else "",
                      "n_out": len(out_lib), "n_in": len(in_prod),
                      "detail": detail})
    if not include_whitelisted:
        items = [i for i in items if not i["whitelisted"]]
    return items


# ---------------------------------------------------------------- 协作表

def collaboration_table(g):
    """每个 lib 模块的上游/下游与关联数——让『至少一条关联』可逐行核对。"""
    rows = []
    for mk in sorted(g["nodes"]):
        if not mk.startswith("lib/"):
            continue
        bm = g["by_mod"][mk]
        out_lib = sorted(k for k in bm["out"] if k.startswith("lib/"))
        in_prod = sorted(k for k in bm["in"]
                         if k.startswith("lib/") or k + ".py" in PROD_SCRIPTS)
        rows.append({"mod": mk, "down": out_lib, "up": in_prod,
                     "links": len(out_lib) + len(in_prod)})
    return rows


def render(rep, path=None):
    g = rep["graph"]
    rows = collaboration_table(g)
    L = ["# 模块协作图与孤岛检测", "",
         "> 判据：**一个模块存在的理由 = 它在数据链上既有上游、又有下游**。",
         "> 上游给它输入（别的模块的推导反馈结果），下游拿走它的产出（数据的分发）。",
         "> 判定单元是**跨模块函数调用**，不是 import——import 只证明文件被打开了。",
         ""]
    L += ["| 类别 | 条数 |", "|---|---|"]
    NAMES = {"ISLAND": "F1 完全孤岛", "GHOST": "F2 幽灵模块（只被测试调用）",
             "UNCONSUMED": "F3 纯孤儿（没人拿它的结果）"}
    for k in ("ISLAND", "GHOST", "UNCONSUMED"):
        L += ["| %s | %d |" % (NAMES[k], rep["by_kind"].get(k, 0))]
    L += ["| **合计** | **%d** |" % len(rep["items"]), ""]
    if not rep["items"]:
        L += ["✓ 无孤岛模块：每个 lib 模块在生产链路里都至少有一条上下游关联。", ""]
    for k in ("ISLAND", "GHOST", "UNCONSUMED"):
        rr = [i for i in rep["items"] if i["kind"] == k]
        if not rr:
            continue
        L += ["## %s（%d）" % (NAMES[k], len(rr)), "",
              "| 模块 | 出度 | 入度 | 说明 |", "|---|---|---|---|"]
        for i in rr:
            L += ["| `%s` | %d | %d | %s |" % (i["name"], i["n_out"], i["n_in"],
                                               i["detail"])]
        L += [""]
    L += ["## 模块协作表（%d 个 lib 模块）" % len(rows), "",
          "| 模块 | 关联数 | 下游（它调用） | 上游（调用它） |",
          "|---|---|---|---|"]
    for r in rows:
        L += ["| `%s` | %d | %s | %s |" % (
            r["mod"], r["links"],
            "、".join(x.replace("lib/", "") for x in r["down"][:6]) or "—",
            "、".join(x.replace("lib/", "") for x in r["up"][:6]) or "—")]
    L += ["", "> 关联数 = 下游数 + 上游数。**关联数为 0 即孤岛**；"
              "只有上游没有下游 = 断头路。"]
    if _SKIPPED:
        L += ["", "## 解析失败（结果不可信，必须修）", ""]
        for s in _SKIPPED:
            L += ["- `%s`：%s" % (s["file"], s["error"])]
    txt = "\n".join(L) + "\n"
    if path:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        io.open(path, "w", encoding="utf-8", newline="\n").write(txt)
    return txt


# ---------------------------------------------------------------- 自检

def _selftest():
    """已知真值：造三种孤岛，看是否都被抓到；再验健康图不误报。"""
    import tempfile
    ok = []
    g = None
    with tempfile.TemporaryDirectory() as td:
        for sub in ("lib", "scripts"):
            os.makedirs(os.path.join(td, sub))
        # 健康模块：既被生产入口调用（有上游），自己也调用别的 lib（有下游）。
        # 首版夹具只给了上游没给下游，结果被判成 F3 断头路，"不误报"断言翻红——
        # 是测试数据造错，不是判据错。夹具必须完整表达"健康"的形状。
        io.open(os.path.join(td, "lib", "util.py"), "w",
                encoding="utf-8").write("def tiny():\n    return 0\n")
        io.open(os.path.join(td, "lib", "core.py"), "w",
                encoding="utf-8").write(
            "from lib import util\n"
            "def helper():\n    return util.tiny()\n")
        # F1 完全孤岛：不调别人、也没人调
        io.open(os.path.join(td, "lib", "isle.py"), "w",
                encoding="utf-8").write("def alone():\n    return 1\n")
        # F3 断头路：被生产入口调用，但自己不调任何 lib
        io.open(os.path.join(td, "lib", "deadend.py"), "w",
                encoding="utf-8").write("def leafish():\n    return 2\n")
        # 生产入口
        io.open(os.path.join(td, "scripts", "fde_run.py"), "w",
                encoding="utf-8").write(
            "from lib import core, deadend\n"
            "def main():\n"
            "    core.helper()\n"
            "    deadend.leafish()\n")
        # F3 纯孤儿：自己调用了别人（有出度），但没有任何调用方。
        # 与 F1 的区别就在这一条出度——两个夹具必须分别存在，
        # 否则"F3 被抓到"会在没有孤儿时静默假绿。
        io.open(os.path.join(td, "lib", "orphan.py"), "w",
                encoding="utf-8").write(
            "from lib import util\n"
            "def producer():\n    return util.tiny() + 1\n")
        # F2 幽灵：只有自检脚本调用
        io.open(os.path.join(td, "lib", "ghost.py"), "w",
                encoding="utf-8").write("def spooky():\n    return 3\n")
        io.open(os.path.join(td, "scripts", "selftest.py"), "w",
                encoding="utf-8").write(
            "from lib import ghost\n"
            "def t():\n    ghost.spooky()\n")
        g = build_graph(root=td)
    items = detect(g)
    kinds = collections.Counter(i["kind"] for i in items)
    ok.append(("F1 完全孤岛被抓到", kinds.get("ISLAND", 0) >= 1,
               "%d 条" % kinds.get("ISLAND", 0)))
    ok.append(("F2 幽灵模块被抓到", kinds.get("GHOST", 0) >= 1,
               "%d 条" % kinds.get("GHOST", 0)))
    ok.append(("F3 纯孤儿被抓到", kinds.get("UNCONSUMED", 0) >= 1,
               "%d 条" % kinds.get("UNCONSUMED", 0)))
    # 反向：被生产入口调用且自己也调用别的 lib 模块的 core.py 不得被报
    ok.append(("健康模块不误报",
               not any(i["name"] == "lib/core" for i in items), ""))
    # 反向：幽灵不能因为"有人 import"就被判健康
    gh = [i for i in items if i["name"] == "lib/ghost"]
    ok.append(("幽灵判定为 GHOST 而非健康",
               bool(gh) and gh[0]["kind"] == "GHOST", ""))
    return ok


# ---------------------------------------------------------------- 棘轮基线

BASELINE = os.path.join(ROOT, "knowledge", "module_island_baseline.json")


def _sig(i):
    return "%s|%s" % (i["kind"], i["name"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="模块协作图与孤岛模块检测")
    ap.add_argument("--report", help="报告输出路径（.md）")
    ap.add_argument("--json", help="机器可读输出路径（.json）")
    ap.add_argument("--selftest", action="store_true", help="跑已知真值自检")
    ap.add_argument("--gate", action="store_true",
                    help="棘轮模式：只拦新增孤岛，存量不阻断")
    ap.add_argument("--update-baseline", action="store_true",
                    help="把当前条目写成新基线（只应在修掉若干条之后执行）")
    ap.add_argument("--strict", action="store_true", help="有条目即退出码 2")
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

    g = build_graph()
    items = detect(g)
    rep = {"items": items,
           "by_kind": dict(collections.Counter(i["kind"] for i in items)),
           "graph": g}
    sigs = set(_sig(i) for i in items)
    if a.update_baseline:
        d = os.path.dirname(BASELINE)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        io.open(BASELINE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"note": "存量孤岛基线。只许删不许加——"
                                "接通一条就 --update-baseline 收紧一次。",
                        "known": sorted(sigs)}, ensure_ascii=False, indent=2) + "\n")
        print("基线已更新：%d 条（%s）" % (len(sigs), BASELINE))
        return 0
    print("=" * 68)
    print("模块协作图与孤岛检测（判据：既有上游、又有下游）")
    print("=" * 68)
    for k in ("ISLAND", "GHOST", "UNCONSUMED"):
        print("%-16s %3d 条" % (k, rep["by_kind"].get(k, 0)))
    print("-" * 68)
    print("合计 %d 条（lib 模块 %d 个）"
          % (len(items), len([m for m in g["nodes"] if m.startswith("lib/")])))
    if _SKIPPED:
        print("⚠ 解析失败 %d 个文件——结果不可信：%s"
              % (len(_SKIPPED), ", ".join(s["file"] for s in _SKIPPED[:5])))
    print("=" * 68)
    txt = render(rep, a.report)
    if a.report:
        print("报告：%s" % a.report)
    if a.json:
        io.open(a.json, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"items": items, "by_kind": rep["by_kind"]},
                       ensure_ascii=False, indent=2) + "\n")
        print("JSON：%s" % a.json)
    if not a.report and not a.json:
        print(txt)
    if a.gate:
        if not os.path.exists(BASELINE):
            print("\n⚠ 没有基线（%s）：本次不作判定。首次启用请 --update-baseline。"
                  % BASELINE)
            return 0
        try:
            base = set(json.load(io.open(BASELINE, encoding="utf-8")).get("known") or [])
        except Exception:
            base = set()
        new = sorted(sigs - base)
        fixed = sorted(base - sigs)
        print("-" * 68)
        print("棘轮判定：基线 %d｜当前 %d｜**新增 %d**｜已接通 %d"
              % (len(base), len(sigs), len(new), len(fixed)))
        if fixed:
            for x in fixed[:10]:
                print("    - 已接通 %s" % x)
        if new:
            print("  ✗ 新增孤岛（必须在本轮接通或补白名单理由）：")
            for x in new[:20]:
                print("    + %s" % x)
            return 2
        print("  ✓ 无新增孤岛")
    return 2 if (a.strict and items) else 0


if __name__ == "__main__":
    sys.exit(main())
