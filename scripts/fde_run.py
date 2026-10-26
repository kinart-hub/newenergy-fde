# -*- coding: utf-8 -*-
"""
fde_run.py —— newenergy-fde 唯一入口编排器

为什么必须有这个文件：
  多步流程只写在文档里时，靠临场记忆决定"这步该跑哪个脚本"，
  漏掉的步骤没有人发现——实测有的脚本两个真实项目一次都没跑过。
  所以三件事必须靠代码保证，不能靠自觉：
    1. 流程收敛成一个入口，一条命令串完所有步骤
    2. 每一步记录命令 / 退出码 / 耗时 / 输出，写进 JSON 报告
       ——跑没跑由文件证明，不是口头声称
    3. 状态分 PASS / PARTIAL / FAIL。有步骤被跳过就是 PARTIAL，
       用独立退出码 3，绝不报成通过。"别的步骤都过了"推不出"整体已验证"。

用法：
  python scripts/fde_run.py audit                      约定落地核查（17 条硬判据）
  python scripts/fde_run.py audit --show-criteria      打印每条规则的判据
  python scripts/fde_run.py audit --selftest           反向验证：故意破坏，看规则喊不喊
  python scripts/fde_run.py verify                     质量闸门全套
  python scripts/fde_run.py analyze -i data.csv -o out  交付主流程
  python scripts/fde_run.py release                    闸门 + 分发包指纹

audit 的每条规则都带判据（criterion）和变异（mutants）。
只加规则不配变异，等于声明自己永远正确——一次误报都抓不出来。
--selftest 会在临时副本上把每条规则人为破坏一遍，没喊 FAIL 的就是没判别力。

退出码：0 = PASS    1 = FAIL    3 = PARTIAL（有步骤被跳过）
"""

import argparse
import ast as _ast
import collections
import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
    # scan:keep stdout 编码兜底在模块加载期执行，degrade 尚未导入；且非 Windows 环境没有 reconfigure 属正常分支，不是降级。

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

EXIT_PASS, EXIT_FAIL, EXIT_PARTIAL = 0, 1, 3

# 入口脚本必须自己保证 UTF-8 输出，否则 Windows 英文环境（cp1252）第一句
# 中文 print 就会崩——这个坑在 CI 上真实发生过，见 selftest 顶部注释。
ENTRY_SCRIPTS = [
    os.path.join("scripts", "selftest.py"),
    os.path.join("scripts", "selftest_guard.py"),
    os.path.join("scripts", "prepublish_audit.py"),
    os.path.join("scripts", "package.py"),
    os.path.join("scripts", "fde_run.py"),
    os.path.join("lib", "pipeline.py"),
]


# --------------------------------------------------------------- 步骤执行

def _tail(text, n=10):
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return lines[-n:]


def _rec(name, status, reason="", cmd="", cwd=ROOT, rc=None, seconds=0.0,
         tail=None, required=True, extra=None):
    d = {"name": name, "status": status, "reason": reason, "cmd": cmd,
         "cwd": cwd, "rc": rc, "seconds": round(seconds, 2),
         "required": required, "tail": tail or []}
    if extra:
        d["extra"] = extra
    return d


def run_cmd(name, cmd, cwd=ROOT, timeout=900, required=True, ok_rc=(0,)):
    """跑一条命令并记录证据。"""
    t0 = time.time()
    shown = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = (p.stdout or "") + ("\n" + p.stderr if (p.stderr or "").strip() else "")
        rc = p.returncode
        return _rec(name, "PASS" if rc in ok_rc else "FAIL",
                    "" if rc in ok_rc else "退出码 %s" % rc,
                    shown, cwd, rc, time.time() - t0, _tail(out), required)
    except subprocess.TimeoutExpired:
        return _rec(name, "FAIL", "超时（%ss）" % timeout, shown, cwd,
                    None, time.time() - t0, [], required)
    except Exception as e:
        return _rec(name, "FAIL", "%s: %s" % (type(e).__name__, e), shown, cwd,
                    None, time.time() - t0, [], required)


def run_check(name, fn, required=True):
    """跑一个进程内检查。fn 返回 (ok, reason, extra)。

    extra 里带 skipped=True 表示"条件不满足、本条没跑"，状态记 SKIP 不是 PASS。
    跳过就是跳过——把没跑的检查记成通过，等于给流程开了一个合法的洞。
    """
    t0 = time.time()
    try:
        ok, reason, extra = fn()
        extra = extra or {}
        status = "PASS" if ok else "FAIL"
        if extra.get("skipped"):
            status = "SKIP"
            reason = reason or extra.get("skip_reason") or "条件不满足，未执行"
        return _rec(name, status, reason, "<internal>", ROOT, 0,
                    time.time() - t0, [], required, extra)
    except Exception as e:
        return _rec(name, "FAIL", "检查自身抛异常 %s: %s" % (type(e).__name__, e),
                    "<internal>", ROOT, None, time.time() - t0, [], required)


# --------------------------------------------------------------- 收尾

def _status_of(steps):
    n = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for s in steps:
        n[s["status"]] = n.get(s["status"], 0) + 1
    if n["FAIL"]:
        return "FAIL", EXIT_FAIL, n
    if n["SKIP"]:
        return "PARTIAL", EXIT_PARTIAL, n
    return "PASS", EXIT_PASS, n


def finish(flow, steps, report_path, extra=None, started=None):
    status, code, n = _status_of(steps)
    total = round(time.time() - (started or time.time()), 2)
    report = {
        "flow": flow,
        "status": status,
        "exit_code": code,
        "counts": n,
        "seconds": total,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "skill_root": ROOT,
        "python": sys.version.split()[0],
        "extra": extra or {},
        "steps": steps,
    }
    if report_path:
        try:
            d = os.path.dirname(os.path.abspath(report_path))
            if d:
                os.makedirs(d, exist_ok=True)
            with io.open(report_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(report, ensure_ascii=False, indent=2))
        except Exception as e:
            print("  [警告] 报告写盘失败：%s" % e)

    print("")
    print("=" * 68)
    print("%s —— %s" % (flow, status))
    print("=" * 68)
    for s in steps:
        mark = {"PASS": "OK  ", "FAIL": "FAIL", "SKIP": "SKIP"}[s["status"]]
        print("  [%s] %-26s %6.1fs %s" % (mark, s["name"], s["seconds"],
                                          s.get("reason") or ""))
        # 失败原因必须当场可见。只有退出码时，使用者分不清「真有缺陷」和
        # 「被另一个实例的锁挤掉」，而这两种的处置完全不同：
        # 前者要改代码，后者只要串行重跑。让人去翻 JSON 才能分辨，
        # 等于把门禁的结论权交给记得去翻的人。
        if s["status"] in ("FAIL", "SKIP") and s.get("tail"):
            for line in s["tail"][-3:]:
                print("         ↳ %s" % line.strip()[:120])
    print("-" * 68)
    print("  通过 %d / 失败 %d / 跳过 %d   总耗时 %.1fs"
          % (n["PASS"], n["FAIL"], n["SKIP"], total))
    if report_path:
        print("  证据报告：%s" % os.path.abspath(report_path))
    if status == "PARTIAL":
        print("  【注意】有步骤被跳过，本轮不算完整验证。"
              "跳过的步骤在报告里标了 SKIP，不能当成通过。")
    print("=" * 68)
    return code


# ------------------------------------------------------- 产出清单（使用者视角）

# XLSX/HTML 写入失败时，往往不报错、只留下几十字节的残骸文件。
# 使用者兴冲冲打开才发现是空的——与其让他发现，不如我们量一下直接说出来。
_SUSPICIOUS_SMALL = {".xlsx": 4096, ".html": 1024, ".md": 256, ".json": 128}


def _human_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return "%d B" % n if unit == "B" else "%.0f %s" % (n, unit)
        n = n / 1024.0
    return "%.1f GB" % n


def _artifact_kind(name):
    """按使用者关心的方式分类，不按扩展名分类。

    工艺工程师找 DOE、现场找作业单、质量找 8D——没人关心文件后缀。
    """
    low = name.lower()
    if "8d" in low or "d2" in low:
        return "8D"
    if low.endswith((".xlsx", ".xls", ".csv")):
        return "数据表"
    if low.endswith(".svg"):
        return "图表"
    if low.endswith(".html"):
        return "报告(网页)"
    if low.endswith(".md"):
        return "报告"
    if low.endswith(".json"):
        return "数据"
    return "其它"


def _print_deliverables(result_json, outdir, d2_card=None):
    """跑完之后必须让使用者知道三件事：东西在哪、哪些要注意、下一步干什么。

    之前 analyze 打印完 5 步 PASS 就结束：产出路径躺在 result.json 里没人读，
    8D 录入卡生成了也没有任何引导。流程断在最后一米，前面 2.1 秒全白跑。
    """
    files = []

    # 1) result.json 里的 outputs 是权威清单，优先用它
    try:
        outs = (json.load(io.open(result_json, encoding="utf-8")) or {}).get("outputs")
        if isinstance(outs, dict):
            for v in outs.values():
                cands = v if isinstance(v, list) else [v]
                for p in cands:
                    if isinstance(p, str) and os.path.isfile(p):
                        ap = os.path.abspath(p)
                        if ap not in files:
                            files.append(ap)
    except Exception as e:
        # 权威清单读不出来，下面只靠 glob 兜底——能兜住目录里的文件，
        # 兜不住写在 outputs 里的绝对路径产物。必须让人知道清单可能不全。
        print("[提示] result.json 的 outputs 清单读不出来（%s），"
              "产物清单改为按目录扫描，可能漏掉写在清单里的路径。" % e)

    # 2) 补 outputs 没覆盖到的产物（如 8D 录入卡）
    for p in sorted(glob.glob(os.path.join(outdir, "*"))):
        if os.path.isfile(p):
            ap = os.path.abspath(p)
            if ap not in files:
                files.append(ap)

    if not files:
        return

    print("")
    print("=" * 68)
    print("产出清单")
    print("=" * 68)
    for kind in ("报告", "报告(网页)", "数据表", "8D", "图表", "数据", "其它"):
        group = [f for f in files if _artifact_kind(os.path.basename(f)) == kind]
        if not group:
            continue
        if kind == "图表":
            # 图表动辄十几张，逐个列路径会把产出清单撑成刷屏。
            # 使用者关心的是"图在哪个目录"，不是第 7 张图叫什么。
            print("  [图表     ] %d 张，目录：%s"
                  % (len(group), os.path.dirname(group[0])))
            continue
        for f in group:
            sz = os.path.getsize(f)
            flag = ""
            if sz <= _SUSPICIOUS_SMALL.get(os.path.splitext(f)[1].lower(), 0):
                flag = "   ⚠ 疑似空文件"
            print("  [%-7s] %-22s %8s%s"
                  % (kind, os.path.basename(f), _human_size(sz), flag))
            print("            %s" % f)

    # 下一步：只写现在真能做的动作，不画饼
    intakes = [f for f in files if _artifact_kind(os.path.basename(f)) == "8D"]
    print("-" * 68)
    print("  下一步")
    print("    1) 打开报告第 1 节「结论摘要」，先确认首要候选根因符合不符合现场认知")
    print("       数据说得再准，跟现场对不上就先信现场，再去查数据哪里取偏了")
    if intakes:
        print("    2) 填写 %s" % os.path.basename(intakes[0]))
        print("       填完回传：`--d2-card <刚填的卡>`，即可出完整 D1–D8")
    print("=" * 68)


# 失败原因 -> 排查方向。按使用者会遇到的真实情况写，不写堆栈。
_DIAGNOSE_RULES = (
    ("输入文件不存在", (
        "确认路径拼写；中文文件名或含空格的建议用英文双引号包起来",
        "确认文件确实存在，且当前账号有读权限",
        "换绝对路径重试，排除当前工作目录不是项目根目录的情况",
    )),
    ("读取结果为空", (
        "确认文件里真的有数据行，不是只剩一个表头",
        "检查表头是不是在第 1 行：有标题行、单位行、多行表头时，脚本会把说明行当成表头",
        "检查合并单元格：导出前先取消合并",
        "检查文件是否损坏：先用 Excel 手动打开一次再另存",
    )),
    ("过滤后数据为空", (
        "检查筛选条件是不是过严，把所有行都剔掉了",
        "检查关键列是否整列为空——列名识别到了，但数据没读进来",
    )),
    ("识别率", (
        "列名跟知识库对不上：加 --auto-adapt 让脚本自动适配未知列名",
        "或用 --spec 显式指定列与规格，跳过自动识别",
    )),
    ("结果 JSON", (
        "确认输出目录可写（磁盘满 / 只读 / 被占用都会导致写盘失败）",
        "确认上一轮没有残留锁文件：查看 .lock/ 目录",
    )),
)


def _diagnose(fails):
    """根据失败原因给出排查方向。命中多条就都列，宁可多说不可漏说。"""
    blob = " ".join("%s %s" % (s.get("name", ""), s.get("reason") or "")
                    for s in fails)
    tips = []
    for key, items in _DIAGNOSE_RULES:
        if key in blob:
            tips.extend(items)
    # 一条都没命中时也要给个兜底方向，不能让使用者对着空白发呆
    if not tips:
        tips = [
            "先看上面失败步骤里的「原因」列，定位是第几步断的",
            "加 --verbose 重跑，看流水线内部各阶段执行到哪一步",
            "跑 `python scripts/fde_run.py audit` 确认环境本身没问题",
        ]
    return tips


def _write_failure_diagnosis(result_json, outdir, steps):
    """失败时落盘一份诊断报告，而不是只打一行 ERROR 就消失。

    吸取参考实现的教训（详见 promotion/参考代码研读笔记.md §2 缺陷 1）：
    它也有兜底，但兜底把致命错误一并兜住了——使用者拿到"一份 Excel"看着像成功，
    真正的 bug 藏了很久。所以兜底必须三样齐全：
      ① 文件名含「诊断」字样  ② 正文带醒目红标  ③ 退出码非 0
    缺任何一样，兜底就从"帮忙"变成"掩盖"。
    """
    fails = [s for s in steps if s.get("status") == "FAIL"]
    try:
        os.makedirs(outdir, exist_ok=True)
    except Exception:
        outdir = "."

    path = os.path.abspath(os.path.join(outdir, "运行诊断.md"))
    L = []
    L.append("# 运行诊断 —— 本轮未通过")
    L.append("")
    L.append("> **这是失败产物，不是分析结果，请勿据此下结论或对外发出。**")
    L.append("")
    L.append("生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    L.append("")
    L.append("## 1. 失败步骤")
    L.append("")
    L.append("| 步骤 | 原因 |")
    L.append("|---|---|")
    for s in fails:
        why = (s.get("reason") or "-").replace("|", "/").replace("\n", " ")
        L.append("| %s | %s |" % (s.get("name", ""), why))
    L.append("")
    L.append("## 2. 排查方向")
    L.append("")
    for t in _diagnose(fails):
        L.append("- %s" % t)
    L.append("")
    L.append("## 3. 环境信息")
    L.append("")
    L.append("- Python：%s" % sys.version.split()[0])
    L.append("- 技能根目录：%s" % ROOT)
    L.append("- 输出目录：%s" % os.path.abspath(outdir))
    L.append("")
    L.append("---")
    L.append("")
    L.append("排查完重跑：")
    L.append("")
    L.append("```")
    L.append("python scripts/fde_run.py analyze -i <你的数据文件> -o %s" % outdir)
    L.append("```")

    try:
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(L))
    except Exception as e:
        print("  [警告] 诊断报告写盘失败：%s" % e)
        return None

    print("")
    print("=" * 68)
    print("【本轮未通过】已生成运行诊断：%s" % path)
    print("  该文件是失败产物，不是分析结果，请勿据此对外发出。")
    print("=" * 68)
    return path


# --------------------------------------------------------------- 通用检查

def _read(rel):
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        return None
    try:
        return io.open(p, encoding="utf-8", errors="replace").read()
    except Exception:
        return None


def _block(text, start_key, end_key, maxlen=1200):
    """取源码里一段，避免全文件搜索导致的误判。

    结束标记必须从起点**之后**开始找：起点自身往往就含结束标记
    （例如 start="def find_cases"、end="def " 时，起点第一个词就命中），
    那样会取到空串，规则就变成恒失败。这个坑已经踩过一次。
    """
    if not text:
        return ""
    i = text.find(start_key)
    if i < 0:
        return ""
    j = text.find(end_key, i + len(start_key))
    return text[i:(j if j > 0 else i + maxlen)]


def check_stale_locks():
    """清理死进程留下的锁。产线上 Ctrl+C 后立刻重跑是常态。"""
    sys.path.insert(0, ROOT)
    base = os.path.join(ROOT, ".lock")
    if not os.path.isdir(base):
        return True, "无锁目录", {"removed": 0}
    try:
        from lib import tenancy as TN
    except Exception as e:
        return False, "无法导入 tenancy：%s" % e, {}
    removed, kept = [], []
    for d in sorted(os.listdir(base)):
        p = os.path.join(base, d)
        if not os.path.isdir(p):
            continue
        owner = os.path.join(p, "owner")
        pid = None
        try:
            pid = int(io.open(owner, encoding="utf-8").read().strip())
        except Exception:
            pid = None
        if pid is not None and not TN._pid_alive(pid):
            try:
                for f in os.listdir(p):
                    try:
                        os.remove(os.path.join(p, f))
                    except Exception as e:
                        # 删不掉就如实说，别让它看起来是清干净了
                        print("[提示] 锁目录残留文件删不掉：%s（%s）"
                              % (os.path.join(p, f), e))
                os.rmdir(p)
                removed.append(d)
            except Exception:
                kept.append(d)
        else:
            kept.append(d)
    return True, "", {"removed": removed, "kept": kept}


UTF8_RE = re.compile(r"sys\.(?:stdout|stderr)\.reconfigure\s*\(\s*encoding\s*=")


def check_entry_utf8():
    """判据不是"文件里出现过 reconfigure 这个词"，而是两个流各自真有带 encoding= 的调用。

    反向验证时把 sys.stdout.reconfigure 改成 sys.stdout._no_reconfigure，
    旧判据（子串 / 只查一个流）照样判通过 —— 一个词被改名就能蒙混的判据，等于没有判据。
    stderr 同样要兜底：CI 里崩的就是重定向后的 stderr 回溯。
    """
    bad = []
    for rel in ENTRY_SCRIPTS:
        t = _read(rel)
        if t is None:
            bad.append("%s（文件不存在）" % rel)
            continue
        if "PYTHONIOENCODING" in t:
            continue
        miss = [s for s in ("stdout", "stderr")
                if not re.search(r"sys\.%s\.reconfigure\s*\(\s*encoding\s*=" % s, t)]
        if miss:
            bad.append("%s（%s 未兜底）" % (rel, "/".join(miss)))
    return (not bad), ("以下入口未做 UTF-8 兜底：" + "、".join(bad)) if bad else "", \
        {"checked": len(ENTRY_SCRIPTS)}


# --------------------------------------------------------------- audit 规则
#
# 每条规则必须同时说清三件事，缺一件就是摆设：
#   1. criterion —— 判据。写成"什么算过、什么算不过"，不是规则名。
#      实测教训：某规则名叫"高风险项必须进控制计划"，判据却是"出现过一行"，
#      名字和判据是两回事，只有把判据写出来才看得出来。
#   2. target    —— 判据落在哪个文件上
#   3. mutants   —— 反向变异：怎么人为破坏它。audit --selftest 会真的破坏一遍，
#      规则没喊 FAIL 就说明这条规则没有判别力，判据白写。
#
# 只加规则不配变异，等于声明自己永远正确 —— 一次误报都抓不出来。

def _r(ok, reason, extra=None):
    return ok, reason, extra or {}


Rule = __import__("collections").namedtuple(
    "Rule", "desc fn criterion target mutants")


def _sub(old, new):
    """一个变异：把 old 替换成 new。old 不存在时返回 None（无法注入，必须暴露）。"""
    def f(t):
        if old not in t:
            return None
        return t.replace(old, new)
    return f


def _re_sub(pattern, repl, flags=re.M):
    """一个变异：按正则做替换（跨行也能命中）。

    默认带 re.M：判据/变异里几乎都是 `^[ \t]+` 这种行首锚定，
    不带 re.M 的话 `^` 只匹配全文开头，正则永远命中不了，
    变异静默失效、退化为 SKIP——这个坑已经踩过。


    为什么需要它：_sub 是逐字替换，源码一换行、一改缩进就匹配不上，于是返回 None，
    自检把它记成 SKIP。SKIP 看着"没报错"，实际是这条规则的判别力根本没被验证过
    ——这比 FAIL 更危险，因为它会让你以为门禁是好的。

    repl 可以是字符串，也可以是函数（拿到 match 返回新串；返回 None 表示注入失败）。
    凡是跨行、或源码结构以后可能变的，一律用正则注入，别用逐字替换。

    实测教训：打包排除规则原本用 _sub 逐字匹配，后来往 EXCLUDE_DIRS 里加了一个新目录，
    set 被改成跨三行，逐字匹配当场静默失效、退化为 SKIP，而门禁仍然显示"通过"。
    """
    rx = re.compile(pattern, flags)

    def f(t):
        m = rx.search(t)
        if not m:
            return None
        if callable(repl):
            new = repl(m)
        else:
            new = rx.sub(repl, m.group(0))
        if new is None:
            return None
        return t[:m.start()] + new + t[m.end():]

    return f


def _drop_set_member(member):
    """从 `EXCLUDE_DIRS = {...}` 里删掉一个成员。

    用正则抓花括号内的整段，再在段内删，这样源码怎么换行都不影响注入成功率。
    删不掉就返回 None，让自检把它暴露成 SKIP，绝不放过。
    """
    def repl(m):
        body = m.group(1)
        for pat in ('"%s",\n' % member, '"%s",' % member,
                    ', "%s"' % member, '"%s"' % member):
            if pat in body:
                return m.group(0).replace(body, body.replace(pat, "", 1), 1)
        return None
    return _re_sub(r'EXCLUDE_DIRS\s*=\s*\{([^}]*)\}', repl)


def _drop_line(needle):
    """一个变异：删掉含 needle 的第一行。"""
    def f(t):
        lines = t.splitlines(True)
        for i, l in enumerate(lines):
            if needle in l:
                del lines[i]
                return "".join(lines)
        return None
    return f


def _mutant(label, fn, target=None, side_effect=False):
    """一个变异。target 缺省就用规则自己的目标文件。

    fn 返回 None = 这个变异在当前源码上做不出来（源码结构变了），
    自检必须把它记成 SKIP 暴露出来，而不是当成"验证通过"。

    side_effect=True 表示这个变异靠**副作用**注入缺陷（比如往副本里
    git add 一个私钥文件），文本本来就不需要改动。不声明的话，
    自检的「替换没改动任何内容」检查会把它误判成变异写错了。
    """
    return (label, fn, target, side_effect)


# audit 规则会读到的文件。--selftest 只复制这些到临时目录做反向验证，
# 既不复制 69M 的仓库，也保证注入缺陷绝不会碰到真实源码（产线不受影响）。
AUDIT_FILES = [
    "LICENSE", "NOTICE", ".gitignore", ".gitattributes", "SKILL.md",
    os.path.join(".github", "workflows", "quality-gate.yml"),
    os.path.join("scripts", "selftest.py"),
    os.path.join("scripts", "selftest_guard.py"),
    os.path.join("scripts", "prepublish_audit.py"),
    os.path.join("scripts", "package.py"),
    os.path.join("scripts", "fde_run.py"),
    os.path.join("lib", "pipeline.py"),
    os.path.join("lib", "hotswap.py"),
    os.path.join("lib", "kbstore.py"),
    os.path.join("lib", "tenancy.py"),
    os.path.join("lib", "eightd.py"),
    os.path.join("lib", "rootcause.py"),
    os.path.join("lib", "views.py"),
    # 变异要在这两个文件里注入缺陷，缺了它们规则会静默 SKIP（不是 FAIL）
    os.path.join("lib", "correlation.py"),
    os.path.join("lib", "direction.py"),
    os.path.join("lib", "stats.py"),
    # v2.6 新增模块的规则要在这些文件里注入缺陷；缺了它们会静默 SKIP（不是 FAIL），
    # 表现为"规则全绿"，实际一条都没验过判别力。教训已经写在 max_delay 那条上。
    os.path.join("lib", "status.py"),
    os.path.join("lib", "provenance.py"),
    os.path.join("lib", "confound.py"),
    os.path.join("lib", "doe.py"),
    os.path.join("knowledge", "process_specs.json"),
]

SKILL_REF_RE = re.compile(
    r"(?:scripts|lib|knowledge|assets|examples|promotion)/"
    r"[A-Za-z0-9_.\\/-]+\.[A-Za-z0-9]+")


def _git_add_secret(unused_text):
    """给「仓库内无 secret」造一个真缺陷：在副本里 git init 并强制暂存私钥。

    不看传入文本——这条规则的判据落在 git 索引上，不在某个文件内容里。
    返回原文表示"文本无需改动"，缺陷由副作用注入。
    """
    try:
        subprocess.run(["git", "init", "-q"], cwd=ROOT, capture_output=True,
                       timeout=60)
        p = os.path.join(ROOT, "_private", "signer_key.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        io.open(p, "w", encoding="utf-8").write('{"private": "zzz"}')
        subprocess.run(["git", "add", "-f", os.path.join("_private",
                                                         "signer_key.json")],
                       cwd=ROOT, capture_output=True, timeout=60)
        return unused_text
    except Exception:
        return None


def rule_license():
    t = _read("LICENSE") or ""
    return _r(t.startswith("Elastic License 2.0"),
              "LICENSE 首行不是 Elastic License 2.0（会被 GitHub 判为 Other）")


def rule_notice():
    t = _read("NOTICE") or ""
    return _r("蒋崇利" in t, "NOTICE 缺版权署名")


def rule_gitignore():
    t = _read(".gitignore") or ""
    miss = [k for k in ("_private", "*.key", "license_key") if k not in t]
    return _r(not miss, ".gitignore 缺：%s" % "、".join(miss) if miss else "",
              {"missing": miss})


def rule_gitattributes():
    t = _read(".gitattributes") or ""
    miss = [k for k in ("text=auto", "eol=lf") if k not in t]
    return _r(not miss, ".gitattributes 缺：%s" % "、".join(miss) if miss else "")


def rule_no_secret_in_repo():
    """私钥 / 授权工具 / 客户数据不得入库，也不得随包。

    判据分两种取数方式，但判据本身不变：
      · 在 git 仓库里 → git ls-files（只算被跟踪的，最准）
      · 不是 git 仓库（分发出去的副本、安装目录）→ 扫实际文件
    之前"不是仓库就记 SKIP"，结果安装目录里这条永远不验证，
    等于给日常运行留了个合法的洞。跑不了和没问题不是一回事。
    """
    pats = ("_private", "signer_key", "license_tool", "PUBLIC_KEY", "tenants/")
    files, mode = None, ""
    try:
        p = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        if p.returncode == 0:
            files = [l.strip() for l in p.stdout.splitlines() if l.strip()]
            mode = "git"
    except Exception:
        files = None
    if files is None:
        mode = "walk"
        files = []
        # tenants/output/dist 是运行时目录，本地存在是正常的；
        # 要防的是它们被"提交"或被"打包"，那由 git 模式和打包规则分别管。
        skip = {".git", "dist", "output", "__pycache__", "tenants", ".lock"}
        for dp, dns, fns in os.walk(ROOT):
            dns[:] = [d for d in dns if d not in skip]
            for fn in fns:
                files.append(os.path.relpath(os.path.join(dp, fn), ROOT))
    hit = [f for f in files if any(k in f for k in pats) or f.endswith(".key")]
    return _r(not hit, "发现了不该出现的：%s" % "、".join(hit[:5]) if hit else "",
              {"mode": mode, "listed": len(files), "hits": hit})


def rule_ci_matrix():
    t = _read(os.path.join(".github", "workflows", "quality-gate.yml")) or ""
    miss = [k for k in ("ubuntu-latest", "windows-latest",
                        "PYTHONIOENCODING", "PYTHONUTF8") if k not in t]
    return _r(not miss, "CI 配置缺：%s" % "、".join(miss) if miss else "")


def rule_hotswap_write():
    t = _block(_read(os.path.join("lib", "hotswap.py")),
               "def atomic_write_text", "def _cleanup")
    # 判据用"调用形态"而不是"出现过这个词"：
    # max_delay 改成 max_delay_removed、random 改成 rand0m，旧的子串判据都看不出来。
    miss = []
    for pat, name in ((r"random\.(?:uniform|random)\s*\(", "抖动（random 调用）"),
                      (r"13,\s*5", "只重试共享冲突（errno 13/5）"),
                      (r"max_delay\s*=", "退避封顶（max_delay 赋值）")):
        if not re.search(pat, t):
            miss.append(name)
    return _r(not miss, "原子写缺：%s" % "、".join(miss) if miss else "")


def rule_hotswap_read():
    t = _read(os.path.join("lib", "hotswap.py")) or ""
    return _r("def read_text_retry" in t, "缺读侧重试 read_text_retry")


def rule_kb_demo_gate():
    t = _read(os.path.join("lib", "kbstore.py")) or ""
    seg = _block(t, "def find_cases", "\ndef ", 4000)
    # 判据要看代码里真有那条 continue，不是看文件里出现过 "demo" 这个词
    has_gate = re.search(r'if\s+c\.get\("demo"\)\s*:', seg) is not None
    return _r(has_gate, "find_cases 里找不到 demo 硬闸门（if c.get(\"demo\"): continue）")


def rule_lock_robust():
    t = _read(os.path.join("lib", "tenancy.py")) or ""
    miss = []
    # 只做子串匹配会被改名蒙混过去（_pid_alive 改成 _pid_alive_x 仍然命中）。
    # 必须同时确认：有定义、有调用、有心跳类。
    if not re.search(r"^def _pid_alive\s*\(", t, re.M):
        miss.append("_pid_alive 定义")
    if len(re.findall(r"_pid_alive\s*\(", t)) < 2:
        miss.append("_pid_alive 调用点")
    # class _HeartbeatOff( 也能匹配 ^class _Heartbeat，必须要求后面紧跟 (
    if not re.search(r"^class _Heartbeat\s*\(", t, re.M):
        miss.append("_Heartbeat 类")
    return _r(not miss, "锁缺：%s" % "、".join(miss) if miss else "")


def rule_package_excludes():
    t = _read(os.path.join("scripts", "package.py")) or ""
    seg = _block(t, "EXCLUDE_DIRS", "def ", 800)
    miss = [k for k in ("tenants", "output", "_private", ".lock") if k not in seg]
    return _r(not miss, "打包未排除：%s" % "、".join(miss) if miss else "")


def rule_audit_gate():
    t = _read(os.path.join("scripts", "package.py")) or ""
    # 要的是"指向那个脚本"的证据（prepublish_audit.py），不是出现过这个词根
    return _r("prepublish_audit.py" in t, "打包未接隐私审计闸门")


def rule_signoff():
    t = _read(os.path.join("lib", "eightd.py")) or ""
    # 只数 SIGNOFF_FIELDS 里的定义。渲染代码里的 st.get("signoff_d5")
    # 同样匹配这个正则，不圈定范围就会多数出 2 条（实测报过 6）。
    seg = _block(t, "SIGNOFF_FIELDS = [", "]", 600)
    n = len(re.findall(r'\("signoff_d[5-8]"', seg))
    return _r(n == 4, "SIGNOFF_FIELDS 应为 4 条，实际 %d" % n, {"count": n})


def rule_d5_no_setpoint():
    t = _read(os.path.join("lib", "eightd.py")) or ""
    seg = _block(t, "def derive_d5", "def derive_d6")
    # 必须是"不含/不给/不下发 + 设定值"这个否定式声明。
    # 只查"设定值"三个字，D5 真去下发设定值时照样判过。
    return _r(re.search(r"不(?:含|给|下发)[^。\n]{0,12}设定值", seg) is not None,
              "D5 未声明不含参数设定值")


def rule_pid_no_windows_kill():
    """Windows 上绝不能用 os.kill(pid, 0) 判活——它是真的去终止那个进程。

    判据（可证伪）：
      · _pid_alive 体内必须出现 OpenProcess（只申请查询权限的判活方式）
      · os.kill( 调用点之前必须先有平台判定（os.name != "nt" / win32），
        否则说明 os.kill 会在 Windows 上被执行到
    """
    t = _read(os.path.join("lib", "tenancy.py")) or ""
    body = _block(t, "def _pid_alive", "\ndef ", 2500)
    if not body:
        return _r(False, "找不到 _pid_alive 定义")
    miss = []
    # 同样要判"调用形态"：OpenProcess 改成 OpenProcessRemoved，子串判据看不出来
    if not re.search(r"OpenProcess\s*\(", body):
        miss.append("OpenProcess（查询权限判活）")
    # 只认行首缩进后的真实调用：docstring 里写着"Windows 上不写 os.kill(pid, 0)"，
    # 子串搜索会先命中注释，把一条正确实现判成违规（实测误报过一次）。
    for m in re.finditer(r"^[ \t]+os\.kill\s*\(", body, re.M):
        prefix = body[:m.start()]
        if not (("os.name" in prefix and "nt" in prefix) or "win32" in prefix):
            miss.append("os.kill 调用点之前没有平台判定（Windows 上会真杀进程）")
            break
    return _r(not miss, "_pid_alive：" + "、".join(miss) if miss else "")


def rule_deliverables_printed():
    """analyze 成功后必须把产出路径打出来。

    实测教训：编排器打印完 5 步 PASS 就结束，产出路径躺在 result.json 里没人读，
    8D 录入卡生成了也没有任何引导——流程断在最后一米。

    判据（可证伪）：全文件必须存在一处**行首缩进后的真实调用**
    （函数名紧跟左括号，实参含 result_json 与 outdir）。
    带行首锚定是为了排除文档字符串和注释里的同名文字——
    只搜函数名会被蒙混，这点已经吃过亏。
    """
    t = _read(os.path.join("scripts", "fde_run.py")) or ""
    if not re.search(r"^def _print_deliverables\s*\(", t, re.M):
        return _r(False, "缺少 _print_deliverables 定义")
    m = re.search(r"^[ \t]+_print_deliverables\s*\(\s*result_json\s*,\s*outdir",
                  t, re.M)
    return _r(m is not None,
              "缺少缩进后的真实调用 _print_deliverables(result_json, outdir, ...)")


def rule_failure_diagnosis():
    """analyze 失败时必须落盘一份诊断文件，而不是只打一行 ERROR 就消失。

    判据（可证伪）：
      · cmd_analyze 的失败分支（else）内必须调用 _write_failure_diagnosis(
      · 该函数体内必须真的写盘：出现 io.open( 且带 .md 后缀

    为什么连"真的写盘"都要判：只检查调用存在，函数可以是个空壳，
    门禁照样绿，使用者照样什么都拿不到。
    """
    t = _read(os.path.join("scripts", "fde_run.py")) or ""
    # 锚点用"else: 换行 + 缩进 + 调用"这个完整形态，避免命中别处的 else:
    if not re.search(r"^[ \t]*else\s*:\s*\n[ \t]+_write_failure_diagnosis\s*\(",
                  t, re.M):
        return _r(False, "analyze 失败分支没有调用 _write_failure_diagnosis")
    body = _block(t, "def _write_failure_diagnosis(", "\ndef ", 4000)
    if not body:
        return _r(False, "找不到 _write_failure_diagnosis 定义")
    if not (re.search(r"io\.open\s*\(", body) and ".md" in body):
        return _r(False, "_write_failure_diagnosis 没有真正写盘（缺 io.open 或 .md）")
    return _r(True, "")


def rule_fallback_not_disguised():
    """兜底产物必须让人一眼看出"这是失败产物"。

    这条规则的来源值得记：参考实现也有兜底，但它把致命错误一起兜住了——
    使用者拿到"一份 Excel"看着像成功，真正的 bug 藏了很久。
    所以兜底必须三样齐全：文件名含"诊断"、正文带红标、退出码非 0。
    本规则管前两样（退出码由 _status_of 保证）。

    判据（可证伪）：
      · _write_failure_diagnosis 体内必须出现 "失败产物" 这个醒目标记
      · 输出文件名必须形如 "xxx诊断.md"
    """
    t = _read(os.path.join("scripts", "fde_run.py")) or ""
    body = _block(t, "def _write_failure_diagnosis(", "\ndef ", 4000)
    if not body:
        return _r(False, "找不到 _write_failure_diagnosis 定义")
    # 只判"写进 .md 文件正文"的那一行。
    # 实测踩过：函数体里其实有两处红标——L.append(...) 写进文件、print(...) 打给屏幕。
    # 若判据写 "失败产物" in body，变异只改掉其中一处时另一处仍满足，
    # 规则照常判过，反向验证就成了走过场。
    red = re.search(r'L\.append\("> \*\*[^"]*"\)', body)
    if not red or "失败产物" not in red.group(0):
        return _r(False, "诊断文件正文缺少『失败产物』醒目标记，会被误当成分析报告")
    if not re.search(r'os\.path\.join\(outdir,\s*"[^"]*诊断\.md"\)', body):
        return _r(False, "诊断文件名不含『诊断』，外观与分析报告无法区分")
    return _r(True, "")


def rule_skill_refs_exist():
    """SKILL.md 里写到的脚本/参考文档路径必须真实存在。

    引用断链是最难自查的一类问题：SKILL.md 上说"第一动作跑 scripts/fde_run.py"，
    而安装目录里根本没这个文件——模型会照着文档去做，然后报"文件不存在"。
    实测这个坑真的发生过。所以判据是路径存在性，不是文档里有没有写。
    """
    t = _read("SKILL.md") or ""
    rels = set(SKILL_REF_RE.findall(t))
    missing = sorted(r for r in rels
                     if not os.path.exists(os.path.join(ROOT, r)))
    return _r(not missing,
              "SKILL.md 引用了不存在的路径：%s" % "、".join(missing[:5])
              if missing else "",
              {"checked": len(rels), "missing": missing})


def rule_orchestrator_declared():
    """多步流程必须有唯一入口，且 SKILL.md 首屏就写"第一动作是跑它"。

    只写在文档里不算数——实测靠临场记忆决定"这步该跑哪个脚本"，
    有的脚本两个真实项目一次都没跑过。判据两条都要满足：文件存在 + 首屏声明。
    """
    exists = os.path.exists(os.path.join(ROOT, "scripts", "fde_run.py"))
    if not exists:
        return _r(False, "scripts/fde_run.py 不存在（多步流程没有唯一入口）")
    t = _read("SKILL.md") or ""
    i = t.find("第一动作")
    if i < 0:
        return _r(False, "SKILL.md 没有『第一动作』小节（未声明先跑编排器）")
    return _r("fde_run.py" in t[i:i + 800],
              "『第一动作』小节里没有出现 fde_run.py（声明与入口脱节）")


# ------------------------------------------------- 缺陷知识库（使用者视角第二批）
#
# 这一组来自「参考实现第三轮研读」的实测结论：
#   主线的深度已经够了，缺的是**索引口径** —— 知识库用"机理命名"登记，
#   现场用"车间叫法"提问，名字对不上就断链，
#   工程师拿到的 8D 报告 D4 栏只能写着「机理未收录」。
#   实测 examples/05_EL缺陷检测数据.csv：补齐前 51.8% 的缺陷没有机理支撑。

def _load_specs():
    """读 knowledge/process_specs.json。副本模式下读到的是副本（注入只改副本）。"""
    raw = _read(os.path.join("knowledge", "process_specs.json"))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _json_mutate_defect_field(key, field, new_value):
    """变异：改写某缺陷条目的字段值。

    走 JSON 解析而不是文本正则 —— 文本替换对换行和缩进完全不免疫
    （这个坑踩过两次），而 JSON 结构不会因为排版变化而失配。
    """
    def f(t):
        try:
            kb = json.loads(t)
        except Exception:
            return None
        dv = (kb.get("defect_rootcause") or {}).get(key)
        if not isinstance(dv, dict) or field not in dv:
            return None
        dv[field] = new_value
        return json.dumps(kb, ensure_ascii=False, indent=2) + "\n"
    return f


def _json_drop_defect_field(key, field):
    """变异：删掉某缺陷条目的字段。同走 JSON 解析，免疫排版变化。"""
    def f(t):
        try:
            kb = json.loads(t)
        except Exception:
            return None
        dv = (kb.get("defect_rootcause") or {}).get(key)
        if not isinstance(dv, dict) or field not in dv:
            return None
        dv.pop(field)
        return json.dumps(kb, ensure_ascii=False, indent=2) + "\n"
    return f


def _collect_dangling_refs():
    """扫 defect_rootcause，返回 (缺陷条目数, 参数数, 悬空引用列表)。"""
    kb = _load_specs()
    if kb is None:
        return 0, 0, None
    params = kb.get("params") or {}
    defects = kb.get("defect_rootcause") or {}
    bad = []
    for dk, dv in defects.items():
        pk = dv.get("param_key")
        if pk and pk not in params:
            bad.append("%s.param_key=%s" % (dk, pk))
        for lp in (dv.get("likely_params") or []):
            if lp not in params:
                bad.append("%s.likely_params=%s" % (dk, lp))
        if not (dv.get("mechanism") or "").strip():
            bad.append("%s.mechanism 为空" % dk)
    return len(defects), len(params), bad


def rule_defect_refs_not_dangling():
    """缺陷条目的参数引用不得悬空，机理不得为空。

    悬空引用不会抛异常——它只是让匹配静默查不到，
    表现出来就是"库里明明登记了这条缺陷，报告却说机理未收录"，
    而且从报告看不出是数据的问题还是库坏了。这种静默失效必须靠判据拦。
    """
    n_def, n_param, bad = _collect_dangling_refs()
    if bad is None:
        return _r(False, "读不到 knowledge/process_specs.json")
    return _r(not bad, "悬空引用：%s" % "；".join(bad[:5]) if bad else "",
              {"defects": n_def, "params": n_param, "bad": bad})


ROOTCAUSE_FILE = os.path.join("lib", "rootcause.py")
CORRELATION_FILE = os.path.join("lib", "correlation.py")
DIRECTION_FILE = os.path.join("lib", "direction.py")
STATUS_FILE = os.path.join("lib", "status.py")
PROVENANCE_FILE = os.path.join("lib", "provenance.py")
CONFOUND_FILE = os.path.join("lib", "confound.py")
DOE_FILE = os.path.join("lib", "doe.py")
TENANCY_FILE = os.path.join("lib", "tenancy.py")
EIGHTD_FILE = os.path.join("lib", "eightd.py")
PIPELINE_FILE = os.path.join("lib", "pipeline.py")
VIEWS_FILE = os.path.join("lib", "views.py")
RUN_FILE = os.path.join("scripts", "fde_run.py")


def rule_empty_data_must_stop():
    """空数据必须终止，不得降级跑完出报告。

    实测过四种空数据输入（0 字节文件 / 只有表头 / 数值列全空 / 清洗后归零），
    原实现全部照常出报告，summary 写着「SPC 判异：未检出违反 Nelson 规则的参数」——
    把「没有输入」包装成「没有结论」。使用者读到的是「这批没问题」。

    三个终止点都必须存在：
      read   —— 裸读后仍 0 行（原注释写「总比直接报错强」，实测证明判断错了）
      clean  —— 清洗后剩余 0 行
      values —— 有行数但除时间列外无任何有效观测值
    """
    t = _read(PIPELINE_FILE) or ""
    if not t:
        return _r(False, "读不到 " + PIPELINE_FILE)

    def _has(prefix_re):
        return re.search(prefix_re, t, re.M) is not None

    # read：裸读仍 0 行时记入 _empty_files 并跳过该文件。
    # 判据落在「append 紧跟 continue」这个具体形态上——
    # 分开查的话，别处的 continue 会让判据形同虚设。
    read_guard = re.search(
        r"^[\t ]+_empty_files\.append\(p\)\n[\t ]+continue$", t, re.M) is not None
    # clean：清洗后 0 行
    clean_guard = _has(r"^[\t ]+if len\(work\) == 0:")
    # values：除时间列外无有效观测值
    values_guard = _has(r"^[\t ]+if \(not _numcols\) or _n_valid == 0:")
    # 三个终止点都必须落到 _empty_data_error，而不是各自 return 一句普通文案
    n_calls = len(re.findall(r"return _empty_data_error\(", t))

    missing = []
    if not read_guard:
        missing.append("read 阶段（裸读 0 行未跳过）")
    if not clean_guard:
        missing.append("clean 阶段（清洗后 0 行未终止）")
    if not values_guard:
        missing.append("values 阶段（无有效观测值未终止）")
    if n_calls < 3:
        missing.append("_empty_data_error 调用不足 3 处（现有 %d 处）" % n_calls)
    if missing:
        return _r(False, "空数据终止点缺失：" + "；".join(missing))
    return _r(True, "", {"read": True, "clean": True, "values": True,
                         "calls": n_calls})


def rule_empty_error_has_checklist():
    """空数据报错必须带排查清单，且清单要真的被打印出来。

    只说「无数据」是通报，不是帮助。使用者要的是「下一步做什么」。
    两处都要有：清单本身（至少 4 条）+ CLI 端逐条打印。
    少打印那一处，等于把清单写在了没人看的地方。
    """
    t = _read(PIPELINE_FILE) or ""
    if not t:
        return _r(False, "读不到 " + PIPELINE_FILE)

    items = re.findall(r'^_EMPTY_CHECKLIST = \[(.*?)^\]', t, re.M | re.S)
    if not items:
        return _r(False, "缺少 _EMPTY_CHECKLIST 定义")
    n_items = len(re.findall(r'^    "', items[0], re.M))
    if n_items < 4:
        return _r(False, "排查清单只有 %d 条，要求覆盖编码/分隔符/表头行/过滤条件四类"
                 % n_items)
    # 打印：CLI 端 error 分支必须遍历 checklist。
    # 判据认「语义」不认「风格」——res.get("x") 与 res["x"] 是等价写法，
    # 只认前者的话，换个写法就误报，规则反而成了噪音。
    printed = re.search(
        r'for _i, _line in enumerate\(res(?:\["checklist"\]|\.get\("checklist"\)), 1\):',
        t) is not None
    hints = re.search(r'res\.get\("hints"\)', t) is not None
    if not printed:
        return _r(False, "CLI 端未逐条打印 checklist —— 清单不会被使用者看到")
    if not hints:
        return _r(False, "CLI 端未打印 hints —— 阶段特定的提示丢失")
    return _r(True, "", {"items": n_items, "printed": True, "hints": True})


def rule_no_dangling_verbose_hint():
    """提示语里出现的参数必须真实存在。

    前车之鉴：引擎提示使用者「加 --verbose 看细节」，而当时这个参数并不存在。
    提示指向一个空入口，比不提示更糟——使用者照做只会碰壁，
    还会连带怀疑整份提示的可信度。
    本规则守住的是「报错文案里提到的每个 --xxx 都在 argparse 里注册过」。
    """
    t = _read(PIPELINE_FILE) or ""
    if not t:
        return _r(False, "读不到 " + PIPELINE_FILE)
    # 收集提示/报错文案里出现的 --xxx，逐个核对是否在 argparse 里注册过。
    # 不要求反引号包裹：实测提示文案里的参数基本都是裸写的
    # （「加 --verbose 看第 1 阶段读到几列」），加了反引号要求就一个都扫不到。
    # 也不做白名单：白名单本身就是一份需要人维护的副本，会失同步。
    # 按「字符串常量」逐条判定，而不是整文件扫：
    # 提示文案里常夹着别的脚本的命令行示例（kb_manage.py --type 之类），
    # 整文件扫会把这些合法引用误报成悬空参数。带 .py 的常量整条跳过。
    import ast as _ast
    try:
        _tree = _ast.parse(t)
    except SyntaxError as _e:
        return _r(False, "pipeline.py 无法解析，本规则无法判定：%s" % _e)
    mentioned = set()
    for _n in _ast.walk(_tree):
        if isinstance(_n, _ast.Constant) and isinstance(_n.value, str):
            if ".py" in _n.value:      # 指向别的脚本的示例，不归本规则管
                continue
            for _m in re.finditer(r"--[a-z][a-z0-9-]+", _n.value):
                mentioned.add(_m.group(0))
    registered = set(re.findall(r'add_argument\(\s*["\'](--[a-z][a-z0-9-]+)', t))
    dangling = sorted(mentioned - registered)
    if dangling:
        return _r(False, "提示语指向了不存在的参数：" + "、".join(dangling))
    return _r(True, "", {"mentioned": len(mentioned), "registered": len(registered)})


def rule_config_not_hardcoded():
    """config 清单必须来自代码与知识库，不得硬编码一份副本。

    「让使用者在一个地方看到所有可调参数」的价值完全取决于清单是不是真的。
    手抄一份 CLI 开关清单、Nelson 规则表或清洗默认值，短期看着一样，
    但代码一改清单就错——使用者照着调一个已经改名/已经失效的参数，
    还以为生效了。这比没有清单更糟，因为它给了虚假的确定感。

    另一条同等重要：规格模板的数值必须留空。拿行业典型区间填进 lsl/usl，
    使用者很容易当成自己企业的规格直接下发——那正是
    「用典型值算 Cpk 去答客户」这个坑最常见的入口。
    """
    t = _read(RUN_FILE) or ""
    if not t:
        return _r(False, "读不到 " + RUN_FILE)
    if not re.search(r'^[ \t]+t = _read\(os\.path\.join\("lib", "spc\.py"\)\)',
                     t, re.M):
        return _r(False, "Nelson 规则集不是从 lib/spc.py 读出来的"
                        "（硬编码副本会随规则调整而失同步）")
    if not re.search(r'^[ \t]+t = _read\(os\.path\.join\("lib", '
                     r'"preprocess\.py"\)\)', t, re.M):
        return _r(False, "清洗策略默认值不是从 lib/preprocess.py 读出来的")
    if not re.search(r'^[ \t]+item\["lsl"\] = None', t, re.M):
        return _r(False, "规格模板的 lsl 不是 None——"
                        "填了行业典型值会被当成企业规格下发")
    if "flow_config" not in t:
        return _r(False, "缺少 config 流程实现")
    return _r(True, "", {"ok": True})


def rule_view_disposition_safety():
    """角色视角的处置建议必须守住 R1 四条责任约束。

    批次处置涉及放行/隔离，给错了是要担责的。四条约束缺一不可：
      1. 措辞是「建议」不是「判定」——决定权在人，工具只摆依据
      2. 无数据依据的条目不进处置表——猜出来的建议不能出现在放行依据里
      3. 未确审时继承红标，且红标写在正文不是脚注
      4. 判断记录落盘——事后要能回答「当时依据什么做的决定」

    本规则验的是第 2、4 条（可静态判定）；1、3 条靠 selftest 跑产物文本验。
    """
    t = _read(VIEWS_FILE) or ""
    if not t:
        return _r(False, "读不到 " + VIEWS_FILE)
    for v in ("process", "shopfloor", "quality", "production", "mgmt"):
        if ('"%s": view_%s' % (v, v)) not in t:
            return _r(False, "视角 %s 未在 BUILDERS 里注册" % v)
    # 无依据不进表
    if not re.search(r"^[ \t]+if not ev and stat == ", t, re.M):
        return _r(False, "处置建议表没有过滤掉无依据的条目——"
                        "没依据的建议进了放行依据，责任说不清")
    # 记录留痕
    if "def write_disposition_record" not in t:
        return _r(False, "缺少处置判断记录落盘函数")
    if not re.search(r"^[ \t]+rec = write_disposition_record\(result, outdir\)",
                     t, re.M):
        return _r(False, "production 视角没有调用判断记录落盘")
    # 视角层不得重算：它只能读 result，不能调 analyze
    if re.search(r"^[ \t]+.*= analyze\(", t, re.M):
        return _r(False, "视角层不得重新计算——五个视角各自算一遍会得出五套结论")
    return _r(True, "", {"ok": True})


def rule_entry_passthrough():
    """统一入口必须透传 lib/pipeline.py 的全部参数，不许手写白名单。

    三条判据各管一种退化：
      1. 没走动态透传 → 任何人手抄清单都会随 pipeline 加参数而漏项；
      2. 还留着旧白名单 → 两套机制并存时以哪个为准说不清；
      3. specs 覆盖不全 → 动态读取本身有洞（比如只取了部分 add_argument）。

    前车之鉴：白名单曾漏掉 --auto-adapt / --no-xlsx / --rules，
    而 --auto-adapt 在 README 里有示例——使用者照文档换到统一入口，
    命令照样跑通、只是那个能力静默没了。这类失效最难被发现：
    不报错、不警告，只是少做一件事。
    """
    t = _read(RUN_FILE) or ""
    if not t:
        return _r(False, "读不到 " + RUN_FILE)
    if not re.search(r"^[ \t]+cmd \+= passthrough_flags\(args, "
                     r"pipeline_flag_specs\(\)\)", t, re.M):
        return _r(False, "flow_analyze 没有用 passthrough_flags 动态透传"
                        "（手写白名单必然随 pipeline 加参数而漏项）")
    # 行首锚定 ^[ \t]+ 是必需的：本规则就在 RUN_FILE 自身里做文本搜索，
    # 而不锚定的话会先命中下面这行判据自己写的 `for flag, val in \(`
    # ——规则把自己判成 FAIL，且永远修不好。真代码的形态是
    # `    for flag, val in (("--target", ...)):`，行首缩进后紧跟 for。
    if re.search(r"^[ \t]+for flag, val in \(", t, re.M):
        return _r(False, "仍存在手写的参数白名单 for flag, val in (...)")
    # 判据 3 刻意做成**文本**判据而不是调用 pipeline_flag_specs()：
    # 本规则的目标文件就是 fde_run.py 自身，而变异改的是临时副本里的文件，
    # 规则进程里跑的 pipeline_flag_specs() 永远是原函数——调用它做判据，
    # 等于用一个改不动的东西去验证"它坏了会怎样"，变异必然抓不到。
    # 运行时是否真覆盖了 pipeline 的参数，由 selftest.py 跑 --help 差集来验证。
    if not re.search(r"^[ \t]+specs\[flags\[0\]\] = ", t, re.M):
        return _r(False, "specs 不是按 AST 遍历填充的——"
                        "改成硬编码清单就会随 pipeline 加参数而失同步")
    src = _read(PIPELINE_FILE) or ""
    want = set(re.findall(r'add_argument\(\s*["\'](--[a-z][a-z0-9-]+)', src))
    if not want:
        return _r(False, "读不出 lib/pipeline.py 的参数定义，无法判定覆盖")
    return _r(True, "", {"pipeline_flags": len(want)})


def rule_defect_alias_index_used():
    """根因识别必须走「现场叫法 → 标准条目」的别名索引。

    为什么这条要单独立：别名数据一直在库里（params[].aliases），
    但匹配路径只要退回到"只按条目键名逐字比对"，别名就立刻全部失效，
    而 audit 里所有规则照样全绿 —— 库看着完整，现场叫法一条都对不上。
    所以判据落在**调用形态**上：函数必须存在，且 identify_defects 里真的调用了它。
    """
    t = _read(ROOTCAUSE_FILE) or ""
    if not t:
        return _r(False, "读不到 " + ROOTCAUSE_FILE)
    has_def = re.search(r"^def _defect_alias_names\(", t, re.M) is not None
    body = _block(t, "def identify_defects(", "\ndef ")
    # 判据必须落在**具体调用形态**上，不能只查"函数体里有这个名字"。
    # 实测教训：第一版判据只检查 `_defect_alias_names(` 是否出现，
    # 结果把列匹配路径改回"只按键名比对"之后，函数体里其它位置的调用还在，
    # 规则照常判过 —— 反向验证当场把它打成 FAIL。
    # 真正的判据是：列匹配必须**遍历别名集合**（for nn in ...）。
    # 注意正则要精确到「行尾是冒号」的列匹配循环：hint 分支里也有一段
    # `for nn in _defect_alias_names(...)`（在 any(...) 里、后面跟 `)`），
    # 判据若不区分，改坏了列匹配它照样判过 —— 实测已被反向验证抓到一次。
    # 第二个参数不再写死 `dv`：v2.4 把列匹配收进了 _best_for(scope) 帮助函数，
    # 遍历的是 scope[dk]。判据若继续咬死 `dk, dv`，源码一重构就永久 FAIL，
    # 而 FAIL 的原因只是判据过时——那会让人去改对的源代码。
    # 真正要守住的是**调用形态**：for nn in <别名集合>(...): 且行尾是冒号。
    called = re.search(r"for\s+nn\s+in\s+_defect_alias_names\([^)]*\):",
                       body, re.M) is not None
    if not has_def:
        return _r(False, "缺少别名索引函数 _defect_alias_names 的定义")
    if not called:
        return _r(False, "identify_defects 的列匹配未遍历别名集合"
                        "（现场叫法将对不上标准条目）")
    return _r(True, "", {"has_def": True, "called": True})


def rule_defect_process_first():
    """缺陷匹配必须按工序优先裁决，且借用别工序的条目要标出来。

    实测抓到的真缺陷（不是假想）：identify_defects 收了 process 参数，
    却只用在「数据驱动兜底」那一段，知识库命中这一段压根没看它。
    于是「焊接不良」这种跨行业通用叫法，在「极耳焊接不良」(battery_winding)
    与「虚焊」(pv_cell_el) 之间等长打平，谁赢全看 JSON 书写顺序。

    判据落在三处调用形态上（缺一处就算断）：
      1. 必须真的按 process 圈出本工序条目集合；
      2. 裁决必须先在本工序集合里找、找不到才退回全局；
      3. 退回全局命中的必须标 cross_process（否则报告层无从提示）。
    """
    t = _read(ROOTCAUSE_FILE) or ""
    if not t:
        return _r(False, "读不到 " + ROOTCAUSE_FILE)
    body = _block(t, "def identify_defects(", "\ndef ")
    if not body:
        return _r(False, "读不出 identify_defects 函数体")
    # 1. 本工序条目集合：必须由 process 圈定
    if not re.search(r"in_proc\s*=\s*\{dk: dv for dk, dv in dr_all\.items\(\)\s*\n?"
                     r"[^\n]*dv\.get\(\"process\"\) == process", body, re.S):
        return _r(False, "没有按 process 圈出本工序条目集合——"
                        "知识库命中会退化成全局按别名长度抢，跨工序别名等长时看 JSON 顺序")
    # 2. 裁决顺序：本工序优先，全局兜底
    if "for scope in (in_proc, dr_all):" not in body:
        return _r(False, "裁决不是「本工序优先、全局兜底」的两段式")
    # 3. 借用必须留痕
    if "return best_dk, bool(process) and scope is dr_all" not in body:
        return _r(False, "从全局兜底命中的条目没有标 cross_process——"
                        "报告层无从提示「这条机理是借来的」")
    # 4. 结果里必须把这个标记带出去，否则前面标了也没人用
    if '"cross_process"' not in body:
        return _r(False, "identify_defects 的返回里没有 cross_process 字段")
    return _r(True, "", {"ok": True})


def rule_corr_row_aligned():
    """P0-1：目标列配对必须按原始行号，不得先压缩再 zip。

    判据落在调用形态上（只查"finite_pairs 出现过"会被 Sidney 绕过去）：
      1. 必须用 S.finite_pairs 做成对过滤；
      2. 第二个参数必须是**原始**目标列 y_raw；
      3. 同一文件里不许再存在"先把目标列压缩一遍"的写法——那正是错位成因。
    """
    t = _read(CORRELATION_FILE) or ""
    if not t:
        return _r(False, "读不到 " + CORRELATION_FILE)
    if not re.search(r"pairs,\s*meta\s*=\s*S\.finite_pairs\(", t, re.M):
        return _r(False, "association_with_target 未使用 S.finite_pairs 做成对过滤")
    if not re.search(r"S\.finite_pairs\(vals,\s*y_raw\)", t, re.M):
        return _r(False, "配对的目标列不是原始列 y_raw——换成压缩列表后"
                        "第 i 行会被配到第 j 行，而样本数看起来完全正常")
    if re.search(r"^\s*y\s*=\s*\[v for v in y_raw if v is not None\]", t, re.M):
        return _r(False, "源码里仍存在把目标列先压缩的写法（它就是错位的成因）")
    return _r(True, "", {"ok": True})


def rule_direction_symmetric():
    """P0-2：六个方向必须各自成立、两两对称。

    判据不只查"有没有这个函数"，而是查四个判定式：
      - higher_is_worse / lower_is_better -> m
      - lower_is_worse  / higher_is_better -> -m（缺这个良率类就反向）
      - target_is_best  -> |m - target|
      - two_sided_spec  -> |m - center|
    """
    t = _read(DIRECTION_FILE) or ""
    if not t:
        return _r(False, "读不到 " + DIRECTION_FILE)
    pos_ok = re.search(
        r'if direction == "higher_is_worse" or direction == "lower_is_better":\s*\n'
        r'\s*return m', t, re.M)
    neg_ok = re.search(
        r'if direction in \("lower_is_worse",\s*"higher_is_better"\):\s*\n'
        r'\s*return -m', t, re.M)
    if not (pos_ok and neg_ok):
        return _r(False, "两个方向必须互为相反数；缺了 -m 那一侧，"
                        "良率/效率这类「越大越好」的指标会被当成「越大越差」")
    if not re.search(r"abs\(m - float\(target\)\)", t, re.M):
        return _r(False, "target_is_best 未按与目标值的偏差判定")
    if not re.search(r"abs\(m - center\)", t, re.M):
        return _r(False, "two_sided_spec 未按与规格中心的偏差判定")
    return _r(True, "", {"ok": True})


# -------------------------------------------- v2.6 外部审计报告改进行 / 规则

def rule_status_no_fake_clean():
    """P0-3：失败不得被读成没问题。

    判据落在 may_claim_no_finding 的逐个取否，而不是"有没有 status.py"。
    逐条验是为了让任何一个状态被悄悄放宽时都能被点名。
    """
    t = _read(STATUS_FILE) or ""
    if not t:
        return _r(False, "读不到 " + STATUS_FILE)
    for st in ("SUCCESS", "NO_FINDING", "PARTIAL_SUCCESS", "INSUFFICIENT_DATA",
               "NOT_APPLICABLE", "SKIPPED", "FAILED"):
        if not re.search(r"^%s = \"" % st, t, re.M):
            return _r(False, "统一状态缺少常量 %s" % st)
    m = re.search(r"_NO_FINDING_CLAIMABLE = frozenset\(\[([^\]]*)\]\)", t)
    if not m:
        return _r(False, "找不到唯一允许宣称『无异常』的集合定义")
    allowed = [x.strip() for x in m.group(1).split(",") if x.strip()]
    if allowed != ["NO_FINDING"]:
        return _r(False, "允许宣称无异常的状态被放宽为 %s，"
                        "失败/跳过也会被读成没问题" % allowed)
    if "def normalize(raw, default=FAILED)" not in t:
        return _r(False, "认不出来的状态必须保守处理成 FAILED，"
                        "改成 SUCCESS 会让不明状态的模块蒙混过关")
    return _r(True, "", {"ok": True})


def rule_lineage_replayable():
    """P1-10：run_id 可复盘 + 指纹可重现 + 哈希能抓等长篡改。"""
    t = _read(PROVENANCE_FILE) or ""
    if not t:
        return _r(False, "读不到 " + PROVENANCE_FILE)
    if not re.search(r'"sha256":\s*sha256_file\(p\)', t):
        return _r(False, "输入摘要不再记录 sha256，等长篡改将无从发现")
    if "time.time_ns()" not in t:
        return _r(False, "run_id 未带纳秒段，同一秒内的两轮会互盖证据")
    if not re.search(r'"fingerprint":\s*fp', t):
        return _r(False, "血缘记录未带内容寻址指纹，答不出『两次是否同一份数据』")
    if 'if sha256_file(path) != it.get("sha256")' not in t:
        return _r(False, "复现自检只比字节数不比哈希：等长改内容会被判为没变")
    if 'base = str(rec["run_id"]).split("-")[0]' not in t:
        return _r(False, "run_id 分段解析必须用 split 取首段；写成 rsplit "
                        "会把时间戳留进去，导致每条记录都被误判为被篡改")
    return _r(True, "", {"ok": True})


def rule_confound_not_causal():
    """P1-6：混杂要判出来，假设不能混进结论。"""
    tc = _read(CONFOUND_FILE) or ""
    tr = _read(ROOTCAUSE_FILE) or ""
    if not tc:
        return _r(False, "读不到 " + CONFOUND_FILE)
    if not re.search(r'item\["vanishes_within_layers"\] = \(', tc):
        return _r(False, "缺少层内关系消失的判定：整体强相关由分组造成时判不出来")
    # 判据必须精确到"赋值后面紧跟阈值表达式"这一**调用形态**。
    # 只验两处字样是否分别存在是不够的：注入 `(False and ...)` 的短路写法后
    # 两个字样都还在，规则却照样判过 —— 这正是"判据停在字样层"的典型症状。
    if not re.search(r'item\["vanishes_within_layers"\] = \(\s*'
                     r'max\(abs\(r\) for r in rs\) < 0\.20\)', tc):
        return _r(False, "『层内关系消失』的判定被改写或加了短路，"
                        "实际不再生效（字样还在但结论恒假）")
    if not tc.count("高度怀疑混杂") >= 1:
        return _r(False, "verdict 必须显式判为『高度怀疑混杂』")
    m = re.search(r'if strong:\n(?:.+\n){0,4}?\s*c\["conclusion_status"\] = ', tr)
    if not m:
        return _r(False, "检出强混杂后未把候选结论降级为『混杂未排除』")
    if 'c["is_hypothesis"] = (not data_backed)' not in tr:
        return _r(False, "候选缺少假设标记：无数据支撑的条目会与有数据的长得一样")
    return _r(True, "", {"ok": True})


def rule_doe_boundary_gate():
    """P1-8：来源不足不能算受控；单位跨量纲必须拒；异常情形必须阻断。"""
    t = _read(DOE_FILE) or ""
    if not t:
        return _r(False, "读不到 " + DOE_FILE)
    if '"受控" if not missing else "推断"' not in t:
        return _r(False, "缺少出处三要素（file/version/approval）的核验，"
                        "任何数值都会被当成受控边界")
    if 'return None, ("%s 与 %s 之间存在温度换算' not in t:
        return _r(False, "仿射单位（温度）未被拒绝换算：套做比例因子必错")
    if "不属于同一量纲（或不在通用换算表内），不能混用" not in t:
        return _r(False, "跨量纲单位必须被拒绝，而不是按 1.0 放行")
    if 'return {"ok": not blockers,' not in t:
        return _r(False, "boundary_gate 必须按有无 blocker 决定 ok；"
                        "写成恒 True 就等于没有门禁")
    return _r(True, "", {"ok": True})


def rule_tenant_write_guard():
    """P2-1：路径不得逃逸，写入必须过闸门，清空前必须复核。"""
    t = _read(TENANCY_FILE) or ""
    if not t:
        return _r(False, "读不到 " + TENANCY_FILE)
    if "cand = os.path.abspath(os.path.join(base, _safe_id(tid)))" not in t:
        return _r(False, "tenant_root 未按 _safe_id 归一后再拼路径")
    if "if not is_inside(cand, base):" not in t:
        return _r(False, "归一后的路径必须再用 is_inside 复核一次；"
                        "只靠 _safe_id 一层，将来被改宽就会直接逃出 tenants/")
    if "if root and not is_inside(p, root):" not in t:
        return _r(False, "guard_write_path 缺少越界拒绝：租户身份下可写到库外")
    if "if not is_inside(r, base):" not in t:
        return _r(False, "reset_tenant 在 rmtree 前未复核路径（不可逆操作无保护）")
    if "os.path.realpath" not in t:
        return _r(False, "必须用 realpath 比对，符号链接指向库外的情况抓不到")
    return _r(True, "", {"ok": True})


def rule_response_defect_not_inherit():
    """「响应型缺陷」不得继承目标量列名的别名。

    背景（实测踩到的真回归，不是假想）：
    「功率衰减」的 param_key 指向 pmax —— 那是**被分析的目标量**，不是缺陷计数列。
    别名索引若照单继承 pmax 的 aliases（Pmax/功率/峰值功率/最大功率），
    列名「Pmax_STC(W)」就会被识别成「功率衰减」缺陷列，
    于是目标列 == 缺陷列，共线排除判据整条失效：
    E06 组件IV 用例的 co_linear_excluded 从 ['CTM','Pmax',...] 变成空，
    报告会把「功率低是因为功率低」当成根因。
    判据落在两处：知识库要标记哪些条目是响应型；代码要遵守这个标记。
    """
    kb = _load_specs()
    if kb is None:
        return _r(False, "读不到 knowledge/process_specs.json")
    defects = kb.get("defect_rootcause") or {}
    # 哪些缺陷是「由结果指标漂移定义」的，无法从数据形态自动推断
    # （面密度超差、厚边、毛刺高度也是 role=response，但它们各自是独立的量测缺陷，
    #  继承自己的别名完全正确；而「功率衰减」继承 pmax 的别名就会把目标列当缺陷列）。
    # 所以判据用**人工认定的显式清单**：这几个条目必须带标记。
    # 清单要扩就得过审改到这里——这是有意的摩擦，避免又悄悄把目标量放进来。
    MUST_FLAG = ["功率衰减", "内阻偏高", "容量一致性差", "TTV超差", "FF偏低"]
    unflagged = [dk for dk in MUST_FLAG
                 if dk in defects and not defects[dk].get("response_defect")]
    t = _read(ROOTCAUSE_FILE) or ""
    honor = 'not dv.get("response_defect")' in t
    if unflagged:
        return _r(False, "以下条目由结果指标漂移定义，却未标 response_defect：%s"
                  % "、".join(unflagged))
    if not honor:
        return _r(False, "别名索引未遵守 response_defect 标记（会继承目标量别名，"
                         "导致目标列被当成缺陷列）")
    return _r(True, "", {"flagged": len(MUST_FLAG)})


def rule_8d_has_d0_era():
    """8D 报告必须有 D0 应急反应（ERA）节。

    行业口径（qmsbbs tid=282/293）：D0 决定要不要启动 8D，并要求 24h 内
    展开 ERA 保护客户、用三现主义确认不良实况。客户第一时间关心的是
    「你现在做了什么」，不是团队名单 —— 缺 D0 等于缺了客户最先看的那一节。
    """
    t = _read(EIGHTD_FILE) or ""
    if not t:
        return _r(False, "读不到 " + EIGHTD_FILE)
    has_def = re.search(r"^def derive_d0\(", t, re.M) is not None
    called = re.search(r"^[\t ]+d0 = derive_d0\(card\)", t, re.M) is not None

    if not has_def:
        return _r(False, "缺少 D0 派生函数 derive_d0")
    if not called:
        return _r(False, "render_8d 未调用 derive_d0 —— D0 节不会出现在报告里")
    return _r(True, "", {"has_def": True, "called": True})


def rule_8d_d7_has_fmea():
    """D7 必须含 PFMEA 更新表。

    行业口径（qmsbbs tid=152）：「8D 没更新 FMEA，等于病看了方子开了
    但没进病历本，下次照样抓错药」。IATF 16949 / VDA 6.3 把
    「8D 成果是否反馈到 FMEA」列为必查项。
    FDE 原 D7 有 CP / SOP / SPC / 点检，唯独没有 PFMEA ——
    审核员翻 PFMEA 发现纹丝没动，直接打回「证据链不完整」。
    """
    t = _read(EIGHTD_FILE) or ""
    if not t:
        return _r(False, "读不到 " + EIGHTD_FILE)
    has_rows = re.search(r"^FMEA_ROWS = \[", t, re.M) is not None
    called = re.search(r"^[\t ]+fmea = derive_d7_fmea\(", t, re.M) is not None

    if not has_rows:
        return _r(False, "缺少 FMEA_ROWS 定义 —— D7 将没有 PFMEA 更新表")
    if not called:
        return _r(False, "render_8d 未调用 derive_d7_fmea —— FMEA 证据链不会出现")
    return _r(True, "", {"has_rows": True, "called": True})

def rule_unregistered_hint_actionable():
    """缺陷未收录时必须给出可行动提示，不能只写「机理未收录」。

    工程师读到「机理未收录」时，他早就知道没收录了 ——
    他需要的是两件事：这份报告剩下部分还能信到哪一步、以及怎么把这条补上。
    所以判据要求：未收录分支确实调用了提示函数，且提示里带**可直接复制的补录命令**。
    """
    t = _read(ROOTCAUSE_FILE) or ""
    if not t:
        return _r(False, "读不到 " + ROOTCAUSE_FILE)
    has_fn = re.search(r"^def _unregistered_hint\(", t, re.M) is not None
    body = _block(t, "def build_5why(", "\ndef ")
    # 同样要求落在**第 2 层 because 的具体形态**上：
    # 只查"函数体里出现过 _unregistered_hint"是不够的 ——
    # 第 1 层的 fallback 分支也在用它，改回硬编码后规则照样判过。
    wired = re.search(
        r'"level":\s*2,.*?"because":\s*info\.get\("mechanism"\)\s*or\s*_unregistered_hint\(',
        body, re.S) is not None
    hint = _block(t, "def _unregistered_hint(", "\ndef ")
    has_cmd = ("kb_manage.py" in hint) and ("defect_new" in hint)
    if not has_fn:
        return _r(False, "缺少 _unregistered_hint（未收录时无提示）")
    if not wired:
        return _r(False, "build_5why 未接入未收录提示（缺失机理时只回『机理未收录』）")
    if not has_cmd:
        return _r(False, "未收录提示里没有可直接复制的补录命令（kb_manage.py / defect_new）")
    return _r(True, "", {"has_fn": True, "wired": True, "has_cmd": True})


AUDIT_RULES = [
    Rule("LICENSE 为 Elastic 2.0 官方纯文本", rule_license,
         "LICENSE 首行必须是裸文本 'Elastic License 2.0'（前面有 # 或 ## 即判不过）",
         "LICENSE",
         [_mutant("首行加 Markdown 标题标记",
                  lambda t: ("## " + t) if t.startswith("Elastic") else None)]),

    Rule("NOTICE 含版权署名", rule_notice,
         "NOTICE 正文中必须出现 '蒋崇利'",
         "NOTICE",
         [_mutant("版权人改成占位符", _sub("蒋崇利", "TODO_版权人"))]),

    Rule(".gitignore 挡住私钥与授权文件", rule_gitignore,
         ".gitignore 必须同时含 _private / *.key / license_key 三个模式",
         ".gitignore",
         [_mutant("删掉 _private 行", _drop_line("_private")),
          _mutant("删掉 *.key 行", _drop_line("*.key"))]),

    Rule(".gitattributes 统一 LF", rule_gitattributes,
         ".gitattributes 必须同时含 text=auto 与 eol=lf",
         ".gitattributes",
         [_mutant("删掉 eol=lf 行", _drop_line("eol=lf"))]),

    Rule("无私钥/授权工具入库或随包", rule_no_secret_in_repo,
         "被跟踪文件（git 模式）或实际文件（副本模式）里，不得出现 _private / "
         "signer_key / license_tool / tenants/ / PUBLIC_KEY，也不得有 .key 结尾",
         "LICENSE",
         # 判据落在 git 索引上，不在文件内容里：变异靠"在副本里 git init 并强制
         # 暂存 _private/signer_key.json"注入，target 只是给副本一个可写的落点。
         [_mutant("仓库里暂存私钥文件", _git_add_secret, side_effect=True)]),

    Rule("CI 覆盖双平台且声明 UTF-8", rule_ci_matrix,
         "quality-gate.yml 必须同时含 ubuntu-latest / windows-latest / "
         "PYTHONIOENCODING / PYTHONUTF8",
         os.path.join(".github", "workflows", "quality-gate.yml"),
         [_mutant("去掉 Windows job", _drop_line("windows-latest")),
          _mutant("去掉 PYTHONUTF8", _drop_line("PYTHONUTF8"))]),

    Rule("入口脚本 stdout 有 UTF-8 兜底", check_entry_utf8,
         "ENTRY_SCRIPTS 里每个文件都必须出现 reconfigure 或 PYTHONIOENCODING",
         os.path.join("scripts", "selftest.py"),
         [_mutant("selftest.py 去掉 UTF-8 兜底",
                  _sub("sys.stdout.reconfigure", "sys.stdout._no_reconfigure")),
          _mutant("package.py 去掉 UTF-8 兜底",
                  _sub("sys.stdout.reconfigure", "sys.stdout._no_reconfigure"),
                  os.path.join("scripts", "package.py"))]),

    Rule("原子写含抖动且只重试共享冲突", rule_hotswap_write,
         "atomic_write_text 段内必须同时出现 random（抖动）、'13, 5'（只重试共享冲突）、"
         "max_delay（退避封顶）",
         os.path.join("lib", "hotswap.py"),
         [_mutant("去掉抖动", _sub("random", "rand0m")),
          _mutant("去掉退避封顶", _sub("max_delay", "max_delay_removed"))]),

    Rule("读侧有容错重试", rule_hotswap_read,
         "hotswap.py 中必须定义 read_text_retry",
         os.path.join("lib", "hotswap.py"),
         [_mutant("读侧重试函数改名",
                  _sub("def read_text_retry", "def read_text_renamed"))]),

    Rule("知识库 demo 案例硬闸门", rule_kb_demo_gate,
         "find_cases 段内必须有正则 if c.get(\"demo\"): —— 只数『出现过 demo 字样』会被注释蒙混",
         os.path.join("lib", "kbstore.py"),
         [_mutant("demo 闸门改成永不命中", _sub('c.get("demo")', 'c.get("demo_x")'))]),

    Rule("锁含 PID 存活探测与心跳", rule_lock_robust,
         "tenancy.py 必须同时有 _pid_alive 定义、≥2 处 _pid_alive( 调用、_Heartbeat 类；"
         "只做子串匹配会被改名蒙混",
         os.path.join("lib", "tenancy.py"),
         [_mutant("_pid_alive 改名（消掉定义）", _sub("def _pid_alive(", "def _pid_alive2(")),
          _mutant("去掉心跳类", _sub("class _Heartbeat", "class _HeartbeatOff"))]),

    Rule("Windows 上不用 os.kill 判活", rule_pid_no_windows_kill,
         "_pid_alive 体内必须有 OpenProcess；若出现 os.kill(，其之前必须有平台判定",
         os.path.join("lib", "tenancy.py"),
         [_mutant("平台判定改成恒真（Windows 也会执行 os.kill）",
                  _sub('if os.name != "nt":', "if True:")),
          _mutant("去掉 OpenProcess 判活", _sub("OpenProcess", "OpenProcessRemoved"))]),

    Rule("打包排除租户/产物/私有目录", rule_package_excludes,
         "package.py 的 EXCLUDE_DIRS 段内必须含 tenants / output / _private / .lock",
         os.path.join("scripts", "package.py"),
         [_mutant("EXCLUDE_DIRS 里去掉 _private",
                  _drop_set_member("_private"))]),

    Rule("打包接了隐私审计闸门", rule_audit_gate,
         "package.py 中必须出现 prepublish_audit（打包前跑隐私审计）",
         os.path.join("scripts", "package.py"),
         [_mutant("去掉审计闸门调用",
                  _sub("prepublish_audit", "prepublish_audit_removed"))]),

    Rule("8D 四道确审齐全", rule_signoff,
         "SIGNOFF_FIELDS 列表内 signoff_d5..d8 必须恰好 4 条（不圈定范围会被渲染代码多数出 2 条）",
         os.path.join("lib", "eightd.py"),
         [_mutant("删掉 D8 确审条目",
                  _sub('    ("signoff_d8", "D8 结案已由质量经理批准"),\n', ""))]),

    Rule("D5 声明不含参数设定值", rule_d5_no_setpoint,
         "derive_d5 段内必须出现 '不含' 或 '设定值'（声明不下发参数）",
         os.path.join("lib", "eightd.py"),
         [_mutant("D5 声明改成会下发设定值", _sub("不给设定值", "给出建议设定值"))]),

    Rule("SKILL.md 引用的路径真实存在", rule_skill_refs_exist,
         "SKILL.md 里出现的所有 scripts/ lib/ knowledge/ assets/ examples/ promotion/ "
         "路径必须 os.path.exists 为真（引用断链必须被判不过）",
         "SKILL.md",
         [_mutant("插入一个不存在的脚本引用",
                  lambda t: t + "\n参考 scripts/zz_missing_ref.py\n")]),

    Rule("编排器存在且被声明为第一动作", rule_orchestrator_declared,
         "scripts/fde_run.py 必须存在，且 SKILL.md『第一动作』小节起 800 字符内出现 fde_run.py",
         "SKILL.md",
         [_mutant("SKILL.md 里的编排器入口被整体换掉",
                  _sub("fde_run.py", "zz_other.py")),
          _mutant("『第一动作』小节标题被删", _sub("第一动作：先跑编排器", "按需挑选脚本"))]),

    # -- 以下三条来自「使用者视角」复盘（promotion/使用者视角架构设计.md）--
    # 算法再准，使用者找不到产出、失败时只拿到一行 ERROR，等于白跑。

    Rule("跑完必须告知产出位置", rule_deliverables_printed,
         "fde_run.py 必须有 _print_deliverables 的定义，且在 cmd_analyze 收尾段"
         "（'code = finish' 之后、'return code' 之前）存在一处缩进后的真实调用，"
         "实参含 result_json 与 outdir",
         os.path.join("scripts", "fde_run.py"),
         [_mutant("去掉 analyze 成功分支的产出清单调用",
                  _re_sub(r'^[ \t]+_print_deliverables\(result_json, outdir',
                          "        pass  # 静默成功")),
          _mutant("产出清单函数改名",
                  _re_sub(r"^def _print_deliverables\(",
                          "def _print_deliverables2("))]),

    Rule("失败必须落盘诊断产物", rule_failure_diagnosis,
         "cmd_analyze 的失败分支内必须调用 _write_failure_diagnosis(；"
         "且该函数体内必须真的写盘（出现 io.open( 且带 .md）",
         os.path.join("scripts", "fde_run.py"),
         [_mutant("去掉 analyze 失败分支的诊断落盘",
                  _re_sub(r'^[ \t]+_write_failure_diagnosis\(result_json',
                          "        pass  # 只打 ERROR")),
          _mutant("诊断函数改名",
                  _re_sub(r"^def _write_failure_diagnosis\(",
                          "def _write_failure_diag2("))]),

    Rule("兜底产物不得长得像分析报告", rule_fallback_not_disguised,
         "诊断正文必须含『失败产物』醒目标记，且文件名含『诊断』；"
         "两者缺一，兜底就会把失败伪装成成功（参考实现 R4 缺陷的教训）",
         os.path.join("scripts", "fde_run.py"),
         [_mutant("删掉失败红标文字",
                  # 正则必须带上 L.append("> ** 这个上下文前缀：
                  # 若只写 '"[^"]*这是失败产物[^"]*"'，它会先匹配到本行变异定义自身
                  # （那行里也含"这是失败产物"这几个字），把变异改到自己头上，
                  # 真实红标纹丝不动 -> 规则照常判过，反向验证形同虚设。
                  _re_sub(r'L\.append\("> \*\*这是失败产物[^"]*"\)',
                          'L.append("> **以下为诊断信息。**")')),
          _mutant("诊断文件名改成报告名",
                  _re_sub(r'os\.path\.join\(outdir,\s*"运行诊断\.md"\)',
                          'os.path.join(outdir, "工艺分析报告.md")'))]),

    # ---- 缺陷知识库：索引口径（参考实现第三轮研读结论）----

    Rule("缺陷条目引用不悬空", rule_defect_refs_not_dangling,
         "knowledge/process_specs.json 中 defect_rootcause 每条的 param_key "
         "与 likely_params 每一项都必须在 params 里存在，且 mechanism 非空；"
         "悬空引用不会报错，只表现为『库里有条目、报告却说机理未收录』的静默失效",
         os.path.join("knowledge", "process_specs.json"),
         [_mutant("把黑心条目的 param_key 改成不存在的键",
                  _json_mutate_defect_field("黑心", "param_key",
                                            "el_black_core_NOT_EXIST")),
          _mutant("删掉碎片条目的 mechanism",
                  _json_drop_defect_field("碎片", "mechanism"))]),

    Rule("根因识别必须走别名索引", rule_defect_alias_index_used,
         "lib/rootcause.py 必须有 _defect_alias_names 的定义，"
         "且 identify_defects 函数体内真实调用了它（只按条目键名比对会让全部现场叫法失效）",
         ROOTCAUSE_FILE,
         [_mutant("别名索引函数改名（消掉定义）",
                  _re_sub(r"^def _defect_alias_names\(",
                          "def _defect_alias_names2(")),
          _mutant("identify_defects 里不再调用别名索引",
                  _re_sub(r"for\s+nn\s+in\s+_defect_alias_names\([^)]*\):",
                          "for nn in [PP._norm_name(dk)]:"))]),

    Rule("响应型缺陷不得继承目标量别名", rule_response_defect_not_inherit,
         "param_key 指向结果指标（role=response 且非缺陷计量列）的缺陷条目，"
         "必须标 response_defect，且 _defect_alias_names 必须遵守该标记；"
         "否则目标列会被识别成缺陷列，共线排除整条失效（实测曾使 E06 用例翻红）",
         os.path.join("knowledge", "process_specs.json"),
         [_mutant("去掉功率衰减的 response_defect 标记",
                  _json_drop_defect_field("功率衰减", "response_defect")),
          _mutant("别名索引不再检查 response_defect",
                  _sub('if pk and not dv.get("response_defect"):',
                       'if pk:'), ROOTCAUSE_FILE)]),

    Rule("未收录缺陷必须给可行动提示", rule_unregistered_hint_actionable,
         "lib/rootcause.py 的 build_5why 未收录分支必须调用 _unregistered_hint，"
         "且该函数正文含可直接复制的补录命令（kb_manage.py 与 defect_new）；"
         "只回『机理未收录』等于把问题原样丢回给工程师",
         ROOTCAUSE_FILE,
         [_mutant("未收录分支改回硬编码文案",
                  _sub('"because": info.get("mechanism") or _unregistered_hint(defect_name),',
                       '"because": info.get("mechanism") or "机理未收录",')),
          _mutant("提示里去掉补录命令",
                  _sub("kb_manage.py scaffold", "知识库 scaffold"))]),
    Rule("8D 必须有 D0 应急反应（ERA）节", rule_8d_has_d0_era,
         "lib/eightd.py 必须有 derive_d0 的定义，且 render_8d 内真实调用它；"
         "缺 D0 的 8D 缺的是客户最先看的那一节（你现在做了什么）",
         EIGHTD_FILE,
         [_mutant("D0 派生函数改名（消掉定义）",
                  _re_sub(r"^def derive_d0\(", "def derive_d02("), EIGHTD_FILE),
          _mutant("render_8d 不再调用 derive_d0",
                  _re_sub(r"^[\t ]+d0 = derive_d0\(card\)",
                          "        d0 = None"), EIGHTD_FILE)]),

    Rule("D7 必须含 PFMEA 更新表", rule_8d_d7_has_fmea,
         "lib/eightd.py 必须有 FMEA_ROWS 定义，且 render_8d 内真实调用 derive_d7_fmea；"
         "缺 FMEA 的 D7 在 IATF 16949 / VDA 6.3 审核中直接判「证据链不完整」",
         EIGHTD_FILE,
         [_mutant("FMEA_ROWS 改名（消掉定义）",
                  _re_sub(r"^FMEA_ROWS = \[", "FMEA_ROWS2 = ["), EIGHTD_FILE),
          _mutant("render_8d 不再调用 derive_d7_fmea",
                  _re_sub(r"^[\t ]+fmea = derive_d7_fmea\(res, lead, card\)",
                          "        fmea = None"), EIGHTD_FILE)]),

    Rule("空数据必须终止，不得出报告", rule_empty_data_must_stop,
         "lib/pipeline.py 必须有 read / clean / values 三个终止点，"
         "且都调用 _empty_data_error；空数据降级跑完会把「没有输入」写成「没有结论」",
         PIPELINE_FILE,
         [_mutant("空文件不再跳过该文件（去掉 append+continue）",
                  _re_sub(r"^[\t ]+_empty_files\.append\(p\)\n[\t ]+continue$",
                          "                pass"), PIPELINE_FILE),
          _mutant("清洗后 0 行不再终止",
                  _re_sub(r"^[\t ]+if len\(work\) == 0:",
                          "    if False:"), PIPELINE_FILE),
          _mutant("无有效观测值不再终止",
                  _re_sub(r"^[\t ]+if \(not _numcols\) or _n_valid == 0:",
                          "    if False:"), PIPELINE_FILE)]),

    Rule("空数据报错必须带排查清单且真的打印出来", rule_empty_error_has_checklist,
         "_EMPTY_CHECKLIST 至少 4 条（编码/分隔符/表头行/过滤条件），"
         "且 CLI 的 error 分支逐条打印 checklist 与 hints；"
         "只说「无数据」是通报不是帮助",
         PIPELINE_FILE,
         [_mutant("排查清单砍到 1 条",
                  _re_sub(r'^_EMPTY_CHECKLIST = \[.*?^\]',
                          '_EMPTY_CHECKLIST = [\n    "检查文件。",\n]',
                          flags=re.M | re.S), PIPELINE_FILE),
          _mutant("CLI 不再逐条打印清单",
                  _re_sub(r'^[\t ]+for _i, _line in enumerate\(res\["checklist"\], 1\):',
                          '        for _i, _line in enumerate([], 1):'), PIPELINE_FILE)]),

    Rule("提示语不得指向不存在的参数", rule_no_dangling_verbose_hint,
         "报错/提示文案里用反引号标注的每个 --xxx 都必须在 argparse 里注册；"
         "前车之鉴：曾提示使用者加 --verbose，而该参数当时并不存在",
         PIPELINE_FILE,
         [_mutant("提示里塞一个没注册的参数",
                  _re_sub(r"加 --verbose 看第 1 阶段读到几列",
                          "加 --debug-mode 看第 1 阶段读到几列"), PIPELINE_FILE)]),

    Rule("config 清单不得硬编码副本、模板数值必须留空", rule_config_not_hardcoded,
         "Nelson 规则集读 lib/spc.py、清洗默认值读 lib/preprocess.py；"
         "规格模板的 lsl/usl/target 一律 None。"
         "手抄清单会失同步，填了典型值会被当成企业规格下发",
         RUN_FILE,
         [_mutant("Nelson 规则改成硬编码列表",
                  _re_sub(r'^[ \t]+t = _read\(os\.path\.join\("lib", "spc\.py"\)\) or ""',
                          '    t = "NELSON_RULES = []"', flags=re.M), RUN_FILE),
          _mutant("模板 lsl 填成行业典型值",
                  _sub('item["lsl"] = None', 'item["lsl"] = 1'), RUN_FILE)]),

    Rule("角色视角的处置建议必须守住责任约束", rule_view_disposition_safety,
         "五视角全部注册；无依据条目不得进处置表；判断记录必须落盘；"
         "视角层不得重算（五套结论各自漂移比没有视角更糟）",
         VIEWS_FILE,
         [_mutant("无依据条目也进处置表",
                  _re_sub(r"^[ \t]+if not ev and stat == .*$",
                          "        pass", flags=re.M), VIEWS_FILE),
          _mutant("判断记录不再落盘",
                  _re_sub(r"^[ \t]+rec = write_disposition_record\(result, outdir\)",
                          "        rec = None", flags=re.M), VIEWS_FILE)]),

    Rule("统一入口必须透传 pipeline 的全部参数", rule_entry_passthrough,
         "flow_analyze 用 pipeline_flag_specs() 动态透传，不许手写白名单；"
         "specs 必须覆盖 lib/pipeline.py 源码里的每一个 add_argument 长选项。"
         "前车之鉴：白名单曾漏掉 --auto-adapt/--no-xlsx/--rules，"
         "使用者换到统一入口后静默少做一件事（不报错，只是能力没了）",
         RUN_FILE,
         [_mutant("透传改成空（参数全部丢弃）",
                  _re_sub(r"^[ \t]+cmd \+= passthrough_flags\(args, pipeline_flag_specs\(\)\)",
                          "        cmd += []"), RUN_FILE),
          _mutant("退回手写白名单（必然漏项）",
                  _re_sub(r"^[ \t]+cmd \+= passthrough_flags\(args, pipeline_flag_specs\(\)\)",
                          '        for _f in ("--target", "--process", "--defect"):\n'
                          '            cmd += [_f, "x"]'), RUN_FILE),
          _mutant("specs 改成空（参数一个都读不到）",
                  _re_sub(r"^[ \t]+specs\[flags\[0\]\] = \{.*?\}",
                          "        continue",
                          flags=re.M | re.S), RUN_FILE)]),

    Rule("缺陷匹配必须按工序优先裁决，不得只看别名长度", rule_defect_process_first,
         "identify_defects 命中知识库条目时，必须先在本工序条目里裁决、"
         "本工序没命中才退回全局，并把借用的条目标 cross_process。"
         "为什么必须这样：process 参数原先只用在数据驱动兜底那一段，"
         "知识库命中压根不看它。于是光伏 EL 数据里出现「焊接不良」列时，"
         "「极耳焊接不良」(battery_winding) 与「虚焊」(pv_cell_el) 别名等长，"
         "谁赢全看 JSON 书写顺序 —— 可能把光伏串焊缺陷判成锂电池超声焊问题，"
         "根因指向焊接功率/压力/时间。跨行业串机理比「未收录」危险得多："
         "它看起来很专业，没人会怀疑它。",
         ROOTCAUSE_FILE,
         [_mutant("裁决退回只看别名长度、不看工序",
                  _re_sub(r"^[ \t]+for scope in \(in_proc, dr_all\):",
                          "        for scope in (dr_all,):",
                          flags=re.M), ROOTCAUSE_FILE),
          _mutant("本工序条目集合置空（工序优先形同虚设）",
                  _re_sub(r"^[ \t]+in_proc = \{dk: dv for dk, dv in dr_all\.items\(\)",
                          "        in_proc = {}  # 变异：不再按工序圈定\n"
                          "        _unused = {dk: dv for dk, dv in dr_all.items()",
                          flags=re.M), ROOTCAUSE_FILE),
          _mutant("借用的条目不再标记 cross_process",
                  _re_sub(r"^[ \t]+return best_dk, bool\(process\) and scope is dr_all",
                          "        return best_dk, False",
                          flags=re.M), ROOTCAUSE_FILE)]),

    # ---- P0 统计正确性（外部深度审计报告指出，已本地复现）----

    Rule("相关性分析必须按原始行号配对", rule_corr_row_aligned,
         "association_with_target 里构造 pair 时必须把候选列与 **原始** 目标列 "
         "y_raw 一起交给 statistical pairing，不得先把 y_raw 压缩成 [v for v in "
         "y_raw if v is not None] 再配对。"
         "为什么必须这样：目标列中间缺值时，压缩后的第 i 个不再是第 i 行，"
         "样本数 n 却完全正常 —— 只有配对内容是错的。实测 Y=2X 完美线性被算成 "
         "r=0.996523，随机数据里更造出过「本质无关 r=-0.025 却报 -0.829」的假强相关，"
         "一个毫不相干的参数会被排进根因候选前列。",
         CORRELATION_FILE,
         [_mutant("改回先压缩目标列再配对的错位写法",
                  _re_sub(r"^[ \t]+pairs, meta = S\.finite_pairs\(vals, y_raw\)",
                          "        _yc = [v for v in y_raw if v is not None]\n"
                          "        pairs, meta = S.finite_pairs(vals, _yc)",
                          flags=re.M), CORRELATION_FILE),
          _mutant("去掉行维度的配对、退回逐列过滤",
                  _re_sub(r"^[ \t]+pairs, meta = S\.finite_pairs\(vals, y_raw\)",
                          "        pairs = [(a, b) for a, b in zip(vals, y_raw)\n"
                          "                  if a is not None and b is not None]\n"
                          "        meta = {\"n_raw\": len(vals), \"n_paired\": len(pairs),\n"
                          "                \"n_excluded\": 0, \"excluded_reasons\": {},\n"
                          "                \"length_mismatch\": 0, \"aligned_by\": \"zip\"}",
                          flags=re.M), CORRELATION_FILE)]),

    Rule("目标方向必须对称成立", rule_direction_symmetric,
         "lib/direction.py 里 _deviation 对 lower_is_worse / higher_is_better "
         "必须返回 -m（反向计入不利程度），与 higher_is_worse / lower_is_better "
         "严格对称；target_is_best / two_sided_spec 则必须按与目标值或规格中心的"
         "偏差判定，不得退化成直接比较均值。"
         "为什么必须这样：'最差档'写死为均值最大时，良率/效率这类越大越好的指标"
         "会被反向指认 —— 横向排查会把全场表现最好的那台标成最差，"
         "并生成『重点核查 A机』的行动建议。反向派人比不给建议更糟。",
         DIRECTION_FILE,
         [_mutant("lower_is_worse 与 higher_is_worse 同向（方向失效）",
                  _re_sub(r'^[ \t]+if direction in \("lower_is_worse", '
                          r'"higher_is_better"\):\n[ \t]+return -m',
                          '    if direction in ("lower_is_worse", '
                          '"higher_is_better"):\n        return m',
                          flags=re.M), DIRECTION_FILE),
          _mutant("目标值最佳不再按偏离目标判定",
                  _re_sub(r"^[ \t]+return abs\(m - float\(target\)\)",
                          "        return m",
                          flags=re.M), DIRECTION_FILE)]),

    # ---- v2.6：外部深度审计报告剩余项（status/血缘/混杂/DOE边界/租户隔离）----

    Rule("分析状态必须走统一枚举，失败不得被读成没问题", rule_status_no_fake_clean,
         "P0-3。lib/status.py 必须定义七态枚举，且唯一允许宣称『未发现异常』的"
         "只有 NO_FINDING；其余六态在 may_claim_no_finding 上一律返回 False。"
         "为什么必须这样：此前各模块自己写状态字符串，"
         "spc/doe/views/quality_guard 干脆不返回状态，于是『执行失败』和"
         "『检查过、没有问题』在汇总层长得一模一样 —— 这是报告点名的"
         "『危险的假阴性』。另外 normalize 认不出来的状态必须保守判 FAILED："
         "判成 SUCCESS 等于给不明状态的模块发放行条。",
         STATUS_FILE,
         [_mutant("允许 FAILED 也宣称无异常",
                  _re_sub(r"^_NO_FINDING_CLAIMABLE = frozenset\(\[NO_FINDING\]\)",
                          "_NO_FINDING_CLAIMABLE = frozenset([NO_FINDING, FAILED])",
                          flags=re.M), STATUS_FILE),
          _mutant("认不出来的状态按成功处理（最危险的放宽）",
                  _re_sub(r"^def normalize\(raw, default=FAILED\):",
                          "def normalize(raw, default=SUCCESS):",
                          flags=re.M), STATUS_FILE)]),

    Rule("每轮分析必须留下可按 run_id 复盘的血缘记录", rule_lineage_replayable,
         "P1-10。lib/provenance.py 必须同时产出 run_id（每次唯一，带纳秒段）"
         "与 fingerprint（输入哈希+配置+版本的内容寻址指纹）；"
         "replay_check 必须比 sha256 而不只是比字节数。"
         "为什么必须这样：只有 run_id 没有指纹，答不出『这次和上次是不是同一份数据』；"
         "只比字节数，等长篡改过的输入会被判成没变过 —— "
         "而改一个数字恰好不改长度，正是最容易发生的那种手滑。",
         PROVENANCE_FILE,
         [_mutant("run_id 去掉纳秒段（同秒两次运行互盖证据）",
                  _re_sub(r'    run_id = "%s-%s-%s" % \(fp\[:12\], '
                          r'time\.strftime\("%Y%m%d%H%M%S"\),\n'
                          r"                           str\(time\.time_ns\(\)\)\[-6:\]\)",
                          '    run_id = "%s-%s" % (fp[:12], '
                          'time.strftime("%Y%m%d%H%M%S"))',
                          flags=re.M), PROVENANCE_FILE),
          _mutant("不再算输入哈希（等长篡改查不出来）",
                  _re_sub(r"^[ \t]+\"sha256\": sha256_file\(p\),",
                          '        "sha256": None,',
                          flags=re.M), PROVENANCE_FILE)]),

    Rule("根因候选必须做混杂检查并区分假设与结论", rule_confound_not_causal,
         "P1-6。lib/confound.py 必须在『目标于分组列上显著相关、"
         "而层内 X→Y 关系均不明显』时把 verdict 判为以『高度怀疑混杂』开头；"
         "lib/rootcause.py 的候选必须带 is_hypothesis，"
         "且检出强混杂后必须把结论状态降级为『混杂未排除』。"
         "为什么必须这样：实测构造的纯混杂样本里整体相关高达 0.997，"
         "层内却只有 0.124 —— 照这个相关去调机，产线纹丝不动。"
         "没有数据支撑的知识库条目只能标『机理支持/现场待确认』，"
         "不能与数据支撑的结论并排出现却不作区分。",
         CONFOUND_FILE,
         [_mutant("层内关系消失也不再判为混杂",
                  _re_sub(r"^[ \t]+item\[\"vanishes_within_layers\"\] = \(\n"
                          r"[ \t]+max\(abs\(r\) for r in rs\) < 0\.20\)",
                          '        item["vanishes_within_layers"] = (False and\n'
                          '            max(abs(r) for r in rs) < 0.20)',
                          flags=re.M), CONFOUND_FILE),
          _mutant("所有候选一律按有数据支撑处理",
                  _re_sub(r"^[ \t]+c\[\"is_hypothesis\"\] = \(not data_backed\)",
                          '        c["is_hypothesis"] = False',
                          flags=re.M), ROOTCAUSE_FILE)]),

    Rule("DOE 边界必须有受控出处，异常情形必须阻断而非放行", rule_doe_boundary_gate,
         "P1-8。controlled_boundaries 必须在出处缺 file/version/approval 任一要素时"
         "判为『推断』（controlled=False）；unit_factor 对跨量纲与仿射单位"
         "（温度）必须返回 None；boundary_gate 的 ok 必须随 blocker 变化。"
         "为什么必须这样：报告明确指出『依赖参数名匹配的通用安全边界不足以保证安全，"
         "参数名相同不代表物理意义、单位或设备限制相同』。"
         "把单位冲突降级成警告等于没设门禁 —— 警告会被顺手关掉，"
         "而 kWh/tiers 的那种量纲错位，放过去就是设备层面的事故。",
         DOE_FILE,
         [_mutant("没有出处的边界也算受控",
                  _re_sub(r"^[ \t]+return _src_record\(\n"
                          r"[ \t]+kind, \"受控\" if not missing else \"推断\",",
                          '        return _src_record(\n            kind, "受控",',
                          flags=re.M), DOE_FILE),
          _mutant("跨量纲单位放行（不再阻断）",
                  _re_sub(r"^    return None, \"「%s」与「%s」不属于同一量纲"
                          r"（或不在通用换算表内），不能混用\" % \(src, dst\)",
                          "    return 1.0, \"同量纲换算\"",
                          flags=re.M), DOE_FILE),
          _mutant("门禁失灵：任何情况都放行",
                  _re_sub(r"^    return \{\"ok\": not blockers, \"blockers\": blockers,",
                          "    return {\"ok\": True, \"blockers\": [],",
                          flags=re.M), DOE_FILE)]),

    Rule("租户写入必须过路径闸门，路径不得逃出 tenants/", rule_tenant_write_guard,
         "P2-1。tenant_root 必须先按 _safe_id 归一、再用 is_inside 复核；"
         "guard_write_path 在租户启用时必须拒绝落点在租户目录外的写入；"
         "reset_tenant 必须在 rmtree 前复核路径；比对一律走 realpath。"
         "为什么必须这样：实测 tenant_root('C:/Windows/System32') 曾原样返回系统目录，"
         "而 reset_tenant 会照着这个值执行 rmtree —— "
         "一列 payload 足以把『清空某个租户』变成『清空某个别的目录』。"
         "两层防护缺一不可：单靠 _safe_id，将来有人放宽它就全线失守。",
         TENANCY_FILE,
         [_mutant("不再复核归一后的路径是否还在 tenants 内",
                  _re_sub(r"^[ \t]+if not is_inside\(cand, base\):",
                          "    if False:",
                          flags=re.M), TENANCY_FILE),
          _mutant("写入闸门一律放行",
                  _re_sub(r"^[ \t]+if root and not is_inside\(p, root\):",
                          "    if False and root:",
                          flags=re.M), TENANCY_FILE),
          _mutant("清空前不再校验路径（不可逆操作失去保护）",
                  _re_sub(r"^[ \t]+if not is_inside\(r, base\):",
                          "    if False:",
                          flags=re.M), TENANCY_FILE)]),
]


# --------------------------------------------------------------- 规则自检
#
# audit 只说"现在没问题"。真正要证明的是"有问题时它会喊"——
# 一条从没喊过的规则，和没有这条规则是一样的。
# 所以每条规则都要在临时副本上被人为破坏一遍：喊了才算有判别力。
# 副本隔离是硬要求：注入缺陷绝不能碰到真实源码。

def _copy(src, dst):
    d = os.path.dirname(dst)
    if d:
        os.makedirs(d, exist_ok=True)
    shutil.copy2(src, dst)


def _copy_audit_files(tmp):
    rels = list(AUDIT_FILES)
    # SKILL.md 引用到的文件也要带进副本。否则"引用路径是否存在"这条规则
    # 在副本上会因为副本不全而假失败——那是副本的错，不是代码的错。
    sm = _read("SKILL.md") or ""
    for r in SKILL_REF_RE.findall(sm):
        if r not in rels:
            rels.append(r)
    for rel in rels:
        src = os.path.join(ROOT, rel)
        if os.path.isfile(src):
            _copy(src, os.path.join(tmp, rel))


def _read_back(p):
    try:
        return io.open(p, encoding="utf-8", errors="replace").read()
    except Exception:
        return None


def _call_rule(rule):
    try:
        ok, reason, extra = rule.fn()
    except Exception as e:
        return False, "规则抛异常 %s: %s" % (type(e).__name__, e), {}
    return bool(ok), reason, extra or {}


def _selftest_one(no, rule, tmp):
    name = "%2d. %s —— 判据反向验证" % (no, rule.desc)
    t0 = time.time()
    variants = []

    base_ok, base_reason, base_extra = _call_rule(rule)
    if base_extra.get("skipped"):
        return _rec(name, "SKIP",
                    "基线在本环境不可执行（%s），判据未经反向验证"
                    % (base_reason or ""), "<mutate>", tmp, 0,
                    time.time() - t0, [], True,
                    {"baseline_pass": False, "variants": []})
    base_ok = bool(base_ok)

    if not rule.mutants:
        return _rec(name, "SKIP", "这条规则没配变异，判据没有经过反向验证",
                    "<mutate>", tmp, 0, time.time() - t0, [], True,
                    {"baseline_pass": base_ok, "variants": []})

    for label, mfn, mtarget, m_side in rule.mutants:
        tgt = mtarget or rule.target
        if not tgt:
            variants.append({"label": label, "result": "SKIP",
                             "detail": "未指定目标文件"})
            continue
        p = os.path.join(tmp, tgt)
        if not os.path.exists(p):
            variants.append({"label": label, "result": "SKIP",
                             "detail": "副本里没有 %s（AUDIT_FILES 未覆盖）" % tgt})
            continue
        orig = io.open(p, encoding="utf-8", errors="replace").read()
        try:
            new = mfn(orig)
        except Exception as e:
            new = None
            detail_err = "%s: %s" % (type(e).__name__, e)
        else:
            detail_err = ""
        if new is None:
            variants.append({"label": label, "result": "SKIP",
                             "detail": (detail_err or
                                        "变异在当前源码上做不出来——源码结构变了，"
                                        "判据已经跟代码脱节")})
            continue
        if new == orig and not m_side:
            # 替换没改动任何字符。这和「判据抓不到」是两回事：
            # 前者是变异写错了（正则没命中），后者才是规则真的瞎了。
            # 混在一起报会把排查方向带偏——实测为此白查了一轮规则判据。
            variants.append({"label": label, "result": "SKIP",
                             "detail": "变异没改动任何内容（正则没命中源码），"
                                       "这条判据本次未经验证"})
            continue
        try:
            with io.open(p, "w", encoding="utf-8") as f:
                f.write(new)
            ok, reason, extra = _call_rule(rule)
            caught = (not ok) and not extra.get("skipped")
            variants.append({"label": label,
                             "result": "PASS" if caught else "FAIL",
                             "detail": "" if caught else
                                       "注入后规则仍判通过（%s）——这条规则抓不到它声称的缺陷"
                                       % (reason or "无理由")})
        finally:
            with io.open(p, "w", encoding="utf-8") as f:
                f.write(orig)
            # 比对文本而不是字节：Windows 文本模式会把 \n 写成 \r\n，
            # 字节 hash 必然不等，那会把"恢复成功"误报成"没还原"。
            if _read_back(p) != orig:
                variants.append({"label": label + "（恢复校验）",
                                 "result": "FAIL",
                                 "detail": "变异后没能还原原文"})

    if not base_ok:
        status, reason = "FAIL", "副本下基线就没过：%s" % (base_reason or "")
    elif any(v["result"] == "FAIL" for v in variants):
        misses = [v["label"] for v in variants if v["result"] == "FAIL"]
        status, reason = "FAIL", "未抓到的变异：%s" % "、".join(misses)
    elif any(v["result"] == "SKIP" for v in variants):
        skips = [v["label"] for v in variants if v["result"] == "SKIP"]
        status, reason = "SKIP", "未能注入：%s" % "、".join(skips)
    else:
        status, reason = "PASS", "基线过、%d 个变异全部被抓到" % len(variants)
    return _rec(name, status, reason, "<mutate>", tmp, 0,
                time.time() - t0, [], True,
                {"baseline_pass": base_ok, "variants": variants})


def rule_selftest_steps():
    steps = []
    tmp = tempfile.mkdtemp(prefix="fde_rule_selftest_")
    global ROOT
    orig_root = ROOT
    try:
        _copy_audit_files(tmp)
        ROOT = tmp
        # 副本里 git init 一下，让"仓库内有无 secret"这类规则能真跑起来。
        # 没有 git 时规则自己会记 SKIP，自检也跟着记 SKIP，不冒充通过。
        try:
            subprocess.run(["git", "init", "-q"], cwd=tmp, capture_output=True,
                           timeout=60)
        except Exception as e:
            # 不静默：git 起不来时依赖仓库状态的规则会记 SKIP，
            # 而 SKIP 比 FAIL 危险——FAIL 至少告诉你有事，
            # SKIP 让你以为门禁是好的。这里必须让跑的人看见。
            print("[提示] 规则自检副本里 git init 失败（%s）："
                  "依赖仓库状态的规则会记为 SKIP，不是通过。" % e)
        for i, rule in enumerate(AUDIT_RULES, 1):
            steps.append(_selftest_one(i, rule, tmp))
    finally:
        ROOT = orig_root
        shutil.rmtree(tmp, ignore_errors=True)
    return steps


# --------------------------------------------------------------- 流程定义

def flow_audit(args):
    started = time.time()
    steps = []
    for i, rule in enumerate(AUDIT_RULES, 1):
        steps.append(run_check("%2d. %s" % (i, rule.desc), rule.fn))

    criteria = [{"no": i, "rule": r.desc, "criterion": r.criterion,
                 "target": r.target or "-",
                 "mutants": [m[0] for m in (r.mutants or [])]}
                for i, r in enumerate(AUDIT_RULES, 1)]

    if getattr(args, "show_criteria", False):
        print("")
        print("-" * 68)
        print("判据清单（判据才是规则本身，规则名只是索引）")
        print("-" * 68)
        for c in criteria:
            print("%2d. %s" % (c["no"], c["rule"]))
            print("    判据：%s" % c["criterion"])
            for m in c["mutants"]:
                print("    变异：%s" % m)
        print("-" * 68)

    if getattr(args, "selftest", False):
        print("")
        print("反向验证：把每条规则人为破坏一遍，看它喊不喊")
        steps.extend(rule_selftest_steps())

    return finish("约定落地核查（%d 条）" % len(AUDIT_RULES), steps,
                  args.report, extra={"criteria": criteria}, started=started)


def flow_verify(args):
    started = time.time()
    steps = [
        # 先验证"规则本身有没有判别力"，再验证"代码有没有问题"。
        # 顺序不能反：判据抓不到缺陷时，后面跑得再绿也只是自我安慰。
        run_cmd("1. 约定规则反向验证",
                [PY, "scripts/fde_run.py", "audit", "--selftest",
                 "--report", os.path.join("output", "_rules_selftest.json")],
                timeout=args.timeout),
        run_cmd("2. 主自检", [PY, "scripts/selftest.py"], timeout=args.timeout),
        run_cmd("3. 护栏自检", [PY, "scripts/selftest_guard.py"], timeout=args.timeout),
        run_cmd("4. 审计规则自检", [PY, "scripts/prepublish_audit.py", "--selfcheck"],
                timeout=args.timeout),
        run_cmd("5. 上架前隐私审计", [PY, "scripts/prepublish_audit.py", "--strict"],
                timeout=args.timeout),
        run_cmd("6. 打包完整性", [PY, "scripts/package.py"], timeout=args.timeout),
    ]
    return finish("质量闸门全套", steps, args.report, started=started)


def flow_release(args):
    started = time.time()
    steps = [
        run_cmd("1. 约定规则反向验证",
                [PY, "scripts/fde_run.py", "audit", "--selftest",
                 "--report", os.path.join("output", "_rules_selftest.json")],
                timeout=args.timeout),
        run_cmd("2. 主自检", [PY, "scripts/selftest.py"], timeout=args.timeout),
        run_cmd("3. 护栏自检", [PY, "scripts/selftest_guard.py"], timeout=args.timeout),
        run_cmd("4. 审计规则自检", [PY, "scripts/prepublish_audit.py", "--selfcheck"],
                timeout=args.timeout),
        run_cmd("5. 上架前隐私审计", [PY, "scripts/prepublish_audit.py", "--strict"],
                timeout=args.timeout),
        run_cmd("6. 打包", [PY, "scripts/package.py"], timeout=args.timeout),
    ]

    def dist_fingerprint():
        import hashlib
        zips = sorted(glob.glob(os.path.join(ROOT, "dist", "*.zip")))
        if not zips:
            return False, "dist 下没有分发包", {}
        z = zips[-1]
        h = hashlib.sha256(io.open(z, "rb").read()).hexdigest()
        return True, "", {"package": os.path.basename(z),
                          "bytes": os.path.getsize(z),
                          "sha256": h, "sha256_16": h[:16]}

    steps.append(run_check("7. 分发包指纹", dist_fingerprint))
    return finish("发布流程", steps, args.report, started=started)


# 编排器自己接管的参数：不透传，由编排器决定取值
#   --input/--outdir 编排器自己解析后拼进命令行
#   --json           编排器固定写到 <outdir>/result.json，后续步骤依赖这个路径
_ORCH_OWNED_FLAGS = {"--input", "--outdir", "--json", "--help"}


def pipeline_flag_specs():
    """读 lib/pipeline.py 的 argparse 定义，返回它能接受的全部参数。

    为什么不在这里手抄一份清单：手抄的清单必然失同步——
    pipeline 加一个参数，fde_run 就少透传一个，而使用者换到统一入口
    会以为能力还在。实测已因此漏了 4 个：
    --auto-adapt（README 里有示例）、--json、--no-xlsx、--rules（Nelson 规则集）。
    漏了的表现不是报错，是静默少做一件事——最难发现的那种。
    """
    path = os.path.join(ROOT, "lib", "pipeline.py")
    specs = {}
    try:
        tree = _ast.parse(io.open(path, encoding="utf-8").read())
    except Exception:
        return specs                      # 读不出来就退化成不透传，不阻塞主流程
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, _ast.Attribute) and fn.attr == "add_argument"):
            continue
        flags = [a.value for a in node.args
                 if isinstance(a, _ast.Constant)
                 and isinstance(a.value, str) and a.value.startswith("--")]
        if not flags:
            continue
        store, nargs, help_txt, default = False, None, "", None
        for kw in node.keywords:
            if kw.arg == "action" and isinstance(kw.value, _ast.Constant):
                store = (kw.value.value == "store_true")
            if kw.arg == "nargs" and isinstance(kw.value, _ast.Constant):
                nargs = kw.value.value
            if kw.arg == "help" and isinstance(kw.value, _ast.Constant):
                help_txt = str(kw.value.value)
            if kw.arg == "default":
                # default 可能是 os.environ.get("X")，那种记为「环境变量可覆盖」，
                # 不假装它是个固定值——config 清单里要如实标出来
                if isinstance(kw.value, _ast.Constant):
                    default = kw.value.value
                else:
                    try:
                        default = "<%s>" % _ast.unparse(kw.value)
                    except Exception:
                        default = "<动态取值>"
        specs[flags[0]] = {"flags": flags, "store": store, "nargs": nargs,
                           "help": help_txt, "default": default}
    return specs


def passthrough_flags(args, specs):
    """把 args 里属于 pipeline 的参数还原成命令行片段。"""
    cmd = []
    for name in sorted(specs):
        if name in _ORCH_OWNED_FLAGS:
            continue
        key = name[2:].replace("-", "_")
        val = getattr(args, key, None)
        if val is None or val is False:
            continue
        if specs[name]["store"]:
            cmd.append(name)
        elif isinstance(val, (list, tuple)):
            cmd += [name] + [str(v) for v in val]
        else:
            cmd += [name, str(val)]
    return cmd


def _nelson_block():
    """Nelson 规则集——读 lib/spc.py 的常量，不在这里抄一份。"""
    t = _read(os.path.join("lib", "spc.py")) or ""
    out = []
    m = re.search(r"^NELSON_RULES = \[(.*?)^\]", t, re.M | re.S)
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r"^\s*\((\d+),\s*[\"'](.+?)[\"'],", line)
            if mm:
                out.append((mm.group(1), mm.group(2)))
    return out


def _clean_defaults():
    """清洗策略的默认值——读 lib/preprocess.py clean() 的签名。"""
    t = _read(os.path.join("lib", "preprocess.py")) or ""
    m = re.search(r"^def clean\(table,(.*?)\):", t, re.M | re.S)
    if not m:
        return {}
    out = {}
    for mm in re.finditer(r"(\w+)\s*=\s*([\"'])(.+?)\2", m.group(1)):
        out[mm.group(1)] = mm.group(3)
    return out


def flow_config(args):
    """打印可调参数清单 / 生成企业规格模板。

    清单的每一块都从**代码与知识库**里读出来，不手抄一份副本——
    手抄的清单必然失同步，而失同步的清单比没有清单更糟：
    使用者照着它调了一个早就改名或早就失效的参数，还以为生效了。

    同样重要的是**如实标出调不了的项**（如清洗策略目前没有 CLI 开关），
    而不是只列能调的让人以为全都可配。
    """
    print("=" * 68)
    print("可调参数清单（来源：代码与知识库，非手工维护的副本）")
    print("=" * 68)

    # 一、命令行开关
    specs = pipeline_flag_specs()
    print("\n【一】命令行开关（读自 lib/pipeline.py 的 argparse 定义）")
    if not specs:
        print("  （读不到 lib/pipeline.py 的参数定义，本块为空）")
    for name in sorted(specs):
        if name in _ORCH_OWNED_FLAGS:
            continue
        sp = specs[name]
        tail = ""
        if sp["default"] not in (None, ""):
            tail = "  默认=%s" % sp["default"]
        print("  %s%s" % (name, " <值>" if not sp["store"] else ""))
        if sp["help"]:
            print("      %s%s" % (sp["help"], tail))

    # 二、Nelson 规则集
    print("\n【二】Nelson 判异规则集（读自 lib/spc.py NELSON_RULES）")
    print("  预设名：1,2,3,5（默认基本集）/ all / basic / shift / trend")
    print("  用法：--rules all        （也可用环境变量 FDE_SPC_RULES）")
    for no, desc in _nelson_block():
        print("    %s  %s" % (no, desc))

    # 三、清洗策略——如实说明当前调不了
    cd = _clean_defaults()
    print("\n【三】清洗策略（读自 lib/preprocess.py clean() 的默认值）")
    if cd:
        for k in ("missing", "outlier"):
            if k in cd:
                print("  %s：当前 %s" % (k, cd[k]))
    print("  ⚠ 这两项目前**没有命令行开关**，只能在代码里改。")
    print("    清单里如实写出来，是不想让人误以为所有策略都能在命令行配。")

    # 四、企业规格
    print("\n【四】企业规格 --spec")
    print("  格式：{\"列名\": {\"lsl\": 下限, \"usl\": 上限, \"target\": 目标}}")
    print("  生成待填模板：python scripts/fde_run.py config --init")
    print("  ⚠ 模板里的行业典型区间仅供对照，**不是**贵司规格——")
    print("    用典型值算 Cpk 去答客户是错的，必须填受控规格书的数值。")

    # 五、知识库
    kb_path = os.path.join("knowledge", "process_specs.json")
    print("\n【五】知识库可调项")
    try:
        kb = json.load(io.open(os.path.join(ROOT, kb_path), encoding="utf-8"))
        print("  工序 %d 个 / 参数 %d 个 / 缺陷条目 %d 条"
              % (len(kb.get("processes") or {}), len(kb.get("params") or {}),
                 len(kb.get("defect_rootcause") or {})))
    except Exception as e:
        print("  （读不到 %s：%s）" % (kb_path, e))
    print("  改知识库走 kb_manage.py（propose → review → sync），")
    print("  不要直接编辑 JSON——直接改没有备份、没有版本、没有审核记录。")

    if not args.init:
        print("\n" + "=" * 68)
        print("加 --init 生成企业规格待填模板")
        return 0
    return _config_init(args)


def _config_init_from_data(args, path, params, tpl, kb):
    """按数据文件里的真实列名生成规格模板。

    键名直接取数据列名，保证 match_spec_key 一定对得上——
    模板的价值取决于它能不能生效，而不是看起来全不全。
    """
    if not os.path.exists(path):
        print("\n生成失败：数据文件不存在：%s" % path)
        return 1
    try:
        sys.path.insert(0, ROOT)
        from lib import table as T
        from lib import preprocess as PP
        tb = T.read_any(path, getattr(args, "sheet", None))
        if isinstance(tb, list):
            tb = tb[0]
        cols = list(tb.columns or [])
    except Exception as e:
        print("\n生成失败：读不出数据列名（%s）" % e)
        return 1
    if not cols:
        print("\n生成失败：%s 里没读到列名" % path)
        return 1

    tpl["_说明"] = ("企业规格模板（按 %s 的实际列名生成，共 %d 列）。"
                    "lsl/usl/target 填贵司受控规格书数值；留 null 或删掉整条"
                    "表示不参与 Cpk 判定。" % (os.path.basename(path), len(cols)))
    n_known = 0
    for c in cols:
        pk = None
        try:
            pk = PP.match_param(c)
        except Exception:
            pk = None
        pv = params.get(pk) or {} if pk else {}
        item = collections.OrderedDict()
        item["lsl"] = None
        item["usl"] = None
        item["target"] = None
        if pv.get("typical_range"):
            item["_行业典型区间（仅参考，不是规格）"] = pv["typical_range"]
        if pv.get("unit"):
            item["_单位"] = pv["unit"]
        item["_识别为"] = (pv.get("name") or pk) if pk else "未识别——不影响使用，填了就能生效"
        if pk:
            n_known += 1
        tpl[c] = item
    out = args.out or "spec_template.json"
    try:
        io.open(out, "w", encoding="utf-8").write(
            json.dumps(tpl, ensure_ascii=False, indent=2))
    except Exception as e:
        print("\n写模板失败：%s" % e)
        return 1
    print("\n已生成 %s（%d 列，其中 %d 列识别到知识库参数）"
          % (out, len(cols), n_known))
    print("下一步：按贵司受控规格书填 lsl/usl/target，"
          "然后 analyze --spec %s" % out)
    return 0


def _config_init(args):
    """生成企业规格模板。

    值一律留空：填了行业典型区间的值，使用者很容易当成自己企业的规格
    直接下发——那正是「用典型值答客户」这个坑最常见的入口。
    典型区间放在 _参考 字段里，且键名带下划线前缀表明它不是规格。
    """
    kb_path = os.path.join(ROOT, "knowledge", "process_specs.json")
    try:
        kb = json.load(io.open(kb_path, encoding="utf-8"))
    except Exception as e:
        print("\n生成失败：读不到知识库（%s）" % e)
        return 1
    params = kb.get("params") or {}
    proc = getattr(args, "process", None)
    tpl = collections.OrderedDict()
    tpl["_用法"] = ("python scripts/fde_run.py analyze --input 数据.csv "
                    "--spec 规格.json --outdir out")
    tpl["_怎么确认生效"] = ("跑完后看报告里该列的 spec_source 字段："
                        "是「企业规格书」才算生效；"
                        "仍是「知识库推定」说明这一条没对上，"
                        "规格书静默失效而不报错——这是最容易踩的坑。")

    # 给 --input 时按**数据里的真实列名**生成。
    # 这不是锦上添花：spec 的匹配是列名归一化后比对，模板键名与数据列名
    # 对不上就整份静默失效（Cpk 退回数据分布兜底，数字失真而企业以为用的是
    # 自己的规格）。按知识库的别名生成，撞对的概率很低。
    inputs = getattr(args, "input", None) or []
    if inputs:
        return _config_init_from_data(args, inputs[0], params, tpl, kb)

    tpl["_说明"] = ("企业规格模板（按知识库参数生成）。lsl/usl/target 必须填贵司"
                    "受控规格书的数值；留 null 表示不参与 Cpk 判定。"
                    "⚠ 键名必须改成**你数据里的实际列名**，否则整份规格静默失效"
                    "——更推荐用 --input 数据文件 直接按真实列名生成。")
    n = 0
    for key in sorted(params):
        pv = params[key] or {}
        if proc and pv.get("process") != proc:
            continue
        aliases = pv.get("aliases") or []
        col = aliases[0] if aliases else pv.get("name") or key
        item = collections.OrderedDict()
        item["lsl"] = None
        item["usl"] = None
        item["target"] = None
        rng = pv.get("typical_range")
        if rng:
            item["_行业典型区间（仅参考，不是规格）"] = rng
        if pv.get("unit"):
            item["_单位"] = pv["unit"]
        if aliases:
            item["_也叫"] = aliases[:8]
        tpl[col] = item
        n += 1
    if proc and n == 0:
        print("\n没有属于工序「%s」的参数。可不加 --process 生成全量模板。" % proc)
        return 1
    out = args.out or "spec_template.json"
    try:
        d = os.path.dirname(os.path.abspath(out))
        if d:
            os.makedirs(d, exist_ok=True)
        io.open(out, "w", encoding="utf-8").write(
            json.dumps(tpl, ensure_ascii=False, indent=2))
    except Exception as e:
        print("\n写模板失败：%s" % e)
        return 1
    print("\n已生成 %s（%d 个参数条目，数值留空待填）" % (out, n))
    print("下一步：按贵司受控规格书填 lsl/usl/target，"
          "然后 analyze --spec %s" % out)
    return 0


def flow_analyze(args):
    started = time.time()
    outdir = os.path.abspath(args.outdir)
    result_json = os.path.join(outdir, "result.json")
    steps = []

    def env_check():
        v = sys.version_info
        ok = v >= (3, 8)
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            return False, "输出目录不可创建：%s" % e, {}
        missing = [p for p in args.input if not os.path.exists(p)]
        if missing:
            return False, "输入文件不存在：%s" % "、".join(missing[:3]), {}
        return ok, "" if ok else "Python 版本过低", {"python": "%d.%d" % (v[0], v[1])}

    steps.append(run_check("1. 环境预检", env_check))
    steps.append(run_check("2. 残留锁清理", check_stale_locks))

    # 透传清单由 lib/pipeline.py 的 argparse 定义生成，不手抄——
    # 手抄的清单会随 pipeline 加参数而漏项，且漏了不会报错只会静默少做。
    cmd = [PY, "lib/pipeline.py", "--input"] + list(args.input) + \
          ["--outdir", outdir, "--json", result_json]
    cmd += passthrough_flags(args, pipeline_flag_specs())

    steps.append(run_cmd("3. 流水线分析", cmd, timeout=args.timeout))

    def check_8d():
        if not os.path.exists(result_json):
            return False, "结果 JSON 未生成", {}
        try:
            res = json.load(io.open(result_json, encoding="utf-8"))
        except Exception as e:
            return False, "结果 JSON 无法解析：%s" % e, {}
        e8 = res.get("eightd")
        if not e8:
            return False, "结果里没有 8D 段（eightd 未装载）", {}
        mds = sorted(glob.glob(os.path.join(outdir, "*.md")))
        text = ""
        unreadable = []
        for m in mds:
            try:
                text += io.open(m, encoding="utf-8", errors="replace").read()
            except Exception as e:
                # 读不出来的文件不能当成「内容为空」混进 text：那样下面会报
                # 「报告缺小节」，使用者就去找报告的问题，而真实原因是文件
                # 被 Excel/编辑器占用或没权限。报错必须指向真实原因。
                unreadable.append("%s（%s）" % (os.path.basename(m), e))
        if unreadable:
            return False, ("报告文件读不出来，无法判定小节完整性：%s"
                           % "；".join(unreadable)), {}
        missing = [d for d in ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8")
                   if d not in text]
        st = e8.get("signoff") or {}
        extra = {"all_signed": st.get("all_signed"),
                 "missing_sections": missing,
                 "d2_filled": e8.get("d2_filled")}
        if missing:
            return False, "报告缺小节：%s" % "、".join(missing), extra
        return True, "", extra

    steps.append(run_check("4. 8D 完整性", check_8d))

    def check_artifacts():
        files = [os.path.basename(p) for p in glob.glob(os.path.join(outdir, "*"))
                 if os.path.isfile(p)]
        leftover = [f for f in files if ".tmp-" in f]
        lock_left = [d for d in os.listdir(os.path.join(ROOT, ".lock"))
                     ] if os.path.isdir(os.path.join(ROOT, ".lock")) else []
        extra = {"files": len(files), "leftover_tmp": leftover,
                 "lock_left": lock_left}
        if leftover:
            return False, "残留临时文件：%s" % "、".join(leftover[:3]), extra
        if not files:
            return False, "输出目录为空", extra
        return True, "", extra

    steps.append(run_check("5. 产物落盘检查", check_artifacts))

    code = finish("交付主流程", steps, args.report,
                  extra={"outdir": outdir, "inputs": list(args.input)},
                  started=started)

    # 未确审 = 不得对外发出。这条必须显式说出来，不能只埋在报告里。
    for s in steps:
        if s["name"].endswith("8D 完整性") and s.get("extra"):
            ex = s["extra"]
            if not ex.get("all_signed"):
                print("")
                print("  【不得对外发出】D5–D8 未经人工确审（已确审项见报告）。")
                print("  需现场质量工程师逐项核对原始记录并签字后，方可对外。")

    # 跑完了就要告诉人家东西在哪。失败时更要给一份能打开看的东西。
    if code == 0:
        _print_deliverables(result_json, outdir, args.d2_card)
    else:
        _write_failure_diagnosis(result_json, outdir, steps)
    return code


# --------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="newenergy-fde 唯一入口编排器（流程代码化 + 证据落盘）")
    ap.add_argument("flow",
                    choices=["audit", "verify", "analyze", "release", "config"],
                    help="audit=约定落地核查  verify=质量闸门  "
                         "analyze=交付主流程  release=发布  "
                         "config=可调参数清单与企业规格模板")
    ap.add_argument("--input", "-i", nargs="+", help="analyze：数据文件")
    ap.add_argument("--outdir", "-o", default="output", help="analyze：输出目录")
    ap.add_argument("--target", help="analyze：目标指标列")
    ap.add_argument("--process", help="analyze：工序 key")
    ap.add_argument("--defect", help="analyze：缺陷类型")
    ap.add_argument("--sheet", help="analyze：XLSX 工作表名")
    ap.add_argument("--spec", help="analyze：企业规格 JSON")
    ap.add_argument("--tenant", help="analyze：租户 id")
    ap.add_argument("--d2-card", help="analyze：D2 录入卡路径")
    ap.add_argument("--industry", nargs="+", help="analyze：行业作用域")
    ap.add_argument("--no-doe", action="store_true", help="analyze：跳过 DOE")
    ap.add_argument("--selftest", action="store_true",
                    help="audit：反向验证，把每条规则人为破坏一遍看它是否报警")
    ap.add_argument("--show-criteria", action="store_true",
                    help="audit：打印每条规则的判据与变异清单")
    # 默认落盘路径按 flow 分开。原先五个流程共用 _fde_run_report.json，
    # 后跑的会把先跑的证据整份覆盖掉——实测追一次 verify 失败时，
    # 打开报告看到的是上一次 analyze 的步骤，失败原因无从查起。
    # 证据文件的默认名必须能回答"这是哪一轮"，显式 --report 仍以使用者为准。
    ap.add_argument("--report", default=None,
                    help="证据报告落盘路径（默认 output/_fde_run_<流程>_report.json）")
    ap.add_argument("--verbose", action="store_true",
                    help="逐阶段打印「这份数据被理解成了什么」（失败时先看这个）")
    ap.add_argument("--timeout", type=int, default=900, help="单步超时（秒）")
    ap.add_argument("--init", action="store_true",
                    help="config：生成企业规格待填模板（数值一律留空）")
    ap.add_argument("--out", help="config --init：模板输出路径")

    # 把 lib/pipeline.py 的全部参数动态补进来（取长选项，不抢短选项）。
    # 目的是让「唯一入口」名副其实：pipeline 新增参数时编排器自动跟上，
    # 不会出现「换了统一入口反而少一个开关」这种静默降级。
    _have = set()
    for _act in ap._actions:
        _have.update(_act.option_strings)
    for _name, _sp in sorted(pipeline_flag_specs().items()):
        if _name in _ORCH_OWNED_FLAGS or _name in _have:
            continue
        if _sp["store"]:
            ap.add_argument(_name, action="store_true",
                            help="analyze：透传给 lib/pipeline.py")
        elif _sp["nargs"] == "+":
            ap.add_argument(_name, nargs="+",
                            help="analyze：透传给 lib/pipeline.py")
        else:
            ap.add_argument(_name, help="analyze：透传给 lib/pipeline.py")
    args = ap.parse_args(argv)
    if not args.report:
        args.report = os.path.join("output",
                                   "_fde_run_%s_report.json" % (args.flow or "run"))

    if args.flow == "analyze" and not args.input:
        ap.error("analyze 必须给 --input")

    print("=" * 68)
    print("newenergy-fde 编排器 —— %s" % args.flow)
    print("=" * 68)

    if args.flow == "config":
        return flow_config(args)
    if args.flow == "audit":
        return flow_audit(args)
    if args.flow == "verify":
        return flow_verify(args)
    if args.flow == "release":
        return flow_release(args)
    return flow_analyze(args)


if __name__ == "__main__":
    sys.exit(main())
