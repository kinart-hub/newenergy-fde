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
    os.path.join("lib", "exclude.py"),
    os.path.join("lib", "context.py"),
    os.path.join("lib", "actioncard.py"),
    os.path.join("lib", "preprocess.py"),
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
    # v2.7 契约规则的变异目标；缺了它同样会是静默 SKIP 而不是 FAIL
    os.path.join("lib", "contract.py"),
    os.path.join("lib", "spc.py"),
    # 产品档配置解析：变异目标（规则 51），缺了它变异注不进去 → 静默 SKIP
    os.path.join("lib", "profile.py"),
    # 规则 53 要读并注入缺陷：内部衍生列判据在 quality_guard，回归断言在评测脚本。
    # 缺了它们同样不是 FAIL 而是静默 SKIP（教训见第 11 条）。
    os.path.join("lib", "quality_guard.py"),
    os.path.join("scripts", "eval_enterprise.py"),
    # 规则 53 还读 views.py（消费端不得兜底），它在上面已列
    # 规则 54：测量系统能力不合格时必须真的拦住结论，未登记时要有前置提示
    os.path.join("lib", "msa.py"),
    # P2-4 可观测性规则的变异要注入这里；缺了它是静默 SKIP 而不是 FAIL
    os.path.join("lib", "observ.py"),
    # 规则 49 要读降级台账确认"护栏失败会转发到可观测性"，
    # 不复制过去副本里就没有这个文件，基线直接判不过
    os.path.join("lib", "degrade.py"),
    # 规则 58 要读 ingest 侧"键不存在就不去重"的承诺，不复制过去副本里没这个文件
    os.path.join("lib", "ingest.py"),
    # 规则 59 要读 lotcode 侧"流水号不进分组"的判据
    os.path.join("lib", "lotcode.py"),
    # 规则 56：原生 Word/PDF 导出。判据同时读 export_doc.py 与 pipeline.py，
    # 变异要注入这两个文件——缺了 export_doc.py 会静默 SKIP（不是 FAIL）
    os.path.join("lib", "export_doc.py"),
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


def _drop_license_section(head, nxt):
    """变异：删掉 LICENSE 里 head 到 nxt 之间的条款**正文**，保留标题。

    为什么删正文不删标题：这就是要防的那种破坏——留下标题让"查标题在不在"
    的判据照样通过，而实际条款内容已经没了。注入失败返回 None，自检会暴露成 SKIP。
    """
    def f(t):
        i = t.find("\n" + head + "\n")
        j = t.find("\n" + nxt + "\n")
        if i < 0 or j < 0 or j <= i:
            return None
        return t[:i + 1] + head + "\n\n（正文已删除）\n" + t[j + 1:]
    return f


def rule_license():
    """LICENSE 必须与代码事实对齐：正文是 Elastic 2.0，但不许出现许可密钥条款。

    为什么这条规则从"看首行"变成了"看条款全集"：
    原判据只查首行是不是 'Elastic License 2.0'，于是任何人删掉中间某整段条款
    （Patents、Termination、No Liability…）都照样判过——LICENSE 看着在，
    实际已经被人掏空了一半。这对一个"条款即合约"的文件是最坏的失效形态。

    同时要把"许可密钥条款"反向锁死：本仓库没有任何许可密钥/授权校验代码，
    那条限制描述的是不存在的功能（外部评审第 4 节点名"条款与代码事实不符"）。
    删掉它是对的，但**悄悄删**是另一回事——所以要求末尾写明改了什么、为什么，
    让改许可这件事本身可被审计。
    """
    t = _read("LICENSE") or ""
    if not t.startswith("Elastic License 2.0"):
        return _r(False, "LICENSE 首行不是 Elastic License 2.0（会被 GitHub 判为 Other）")
    # 判据落在**条款正文的关键句**而不是标题子串：
    # 只查标题的话，标题留着、正文被掏空照样判过——标题在≠条款还在。
    _need = [
        ("Acceptance", "you agree to all of the terms and conditions below"),
        ("Copyright License", "royalty-free, worldwide"),
        ("Limitations", "You may not provide the software to third parties as a hosted"),
        ("Patents", "under any patent claims the licensor can"),
        ("Notices", "anyone who gets a copy of any part of the software"),
        ("Termination", "your licenses will automatically terminate"),
        ("No Liability", "the software comes as is"),
        ("Definitions", "the licensor is the entity offering these terms"),
    ]
    _low = t.lower()
    miss = [k for k, _probe in _need if _probe.lower() not in _low]
    if miss:
        return _r(False, "Elastic 2.0 正文缺条款或被掏空：%s（只留标题也不算数）"
                 % "、".join(miss), {"missing": miss})
    # 自我指涉防护（踩过的坑）：末尾 Modification notice 里为了说明"删了什么"，
    # 必然要引用那句条款原文。若拿全文去做子串匹配，会把**说明中的引用**判成
    # "条款还在"。条款是否成立只看 notice 之前的正文 Parte。
    _body = t.split("## Modification notice")[0]
    if "You may not move, change, disable, or circumvent the license key" in _body:
        return _r(False,
                  "LICENSE 仍含『许可密钥功能』条款，但代码中不存在任何授权校验——"
                  "条款描述了一件不存在的事（评审点名的「条款与代码不符」）")
    if "Modification notice" not in t:
        return _r(False,
                  "LICENSE 相对官方文本有删减，但末尾没有 Modification notice 标明改了什么——"
                  "改许可这种事必须留痕，否则下次谁再删一条也看不出来")
    return _r(True, "", {"n_clauses": len(_need)})


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
KBSTORE_FILE = os.path.join("lib", "kbstore.py")
OBS_FILE = os.path.join("lib", "observ.py")
PROFILE_FILE = os.path.join("lib", "profile.py")
QG_FILE = os.path.join("lib", "quality_guard.py")
EVAL_FILE = os.path.join("scripts", "eval_enterprise.py")
TENANCY_FILE = os.path.join("lib", "tenancy.py")
EIGHTD_FILE = os.path.join("lib", "eightd.py")
PIPELINE_FILE = os.path.join("lib", "pipeline.py")
MSA_FILE = os.path.join("lib", "msa.py")
EXPORT_FILE = os.path.join("lib", "export_doc.py")
INGEST_FILE = os.path.join("lib", "ingest.py")
LOT_FILE = os.path.join("lib", "lotcode.py")
EXCLUDE_FILE = os.path.join("lib", "exclude.py")
CONTEXT_FILE = os.path.join("lib", "context.py")
ACTIONCARD_FILE = os.path.join("lib", "actioncard.py")
PREPROCESS_FILE = os.path.join("lib", "preprocess.py")
SPCPy = os.path.join("lib", "spc.py")
CONTRACT_FILE = os.path.join("lib", "contract.py")
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


def rule_contract_enforced():
    """P1-4 数据契约：三个"不许糊过去"的点必须真的在代码里。

    判据刻意落在**具体赋值与返回形态**上，不停留在"字样出现过"。
    上一轮规则 39 被自己的变异骗过一次（注入 `(False and ...)` 后
    两个关键字都还在，规则照样判过），所以这里每条都要求看得见比较或记账动作。
    """
    t = _read(CONTRACT_FILE) or ""
    if not t:
        return _r(False, "读不到 " + CONTRACT_FILE)

    # 1) 版本：主版本不同必须给出 block，且这个 block 要真的被返回出去
    comp = _block(t, "def compatible(", "\ndef ")
    if not re.search(r"a\[0\]\s*!=\s*b\[0\]", comp):
        return _r(False, "版本兼容未比较主版本号：跨大版本的数据结构会被当成能用")
    # 光有比较不够，"比较完什么都不做"同样等于没有门禁
    if not re.search(r'return\s+False,\s*"block"', comp):
        return _r(False, "主版本不一致时未返回 block 级结果")
    if not comp.count('"block"') >= 2:
        return _r(False, "不可解析的版本号也必须按 block 处理（不能默认放行）")

    # 2) 结果：有输出却没 status 的环节必须被逐条记进 missing_status
    cr = _block(t, "def check_result(", "\ndef ")
    if not re.search(r"out\[.missing_status.\]\.append\(n\)", cr):
        return _r(False, "缺少 missing_status 记账：无法区分『算过且无发现』与『算崩了返回 None』")
    # 空 list 属"没产出"，判成缺失就会逼人到处补假 status
    if not re.search(r"^\s+continue", cr, re.M):
        return _r(False, "缺失：空列表应视为没产出而非违规，否则会诱发假 status")
    if not re.search(r"isinstance\(v,\s*\(list,\s*tuple\)\)", cr):
        return _r(False, "未处理 list 型环节：会把整个列表误判成没有 status")

    # 3) 取值：允许范围必须真做比较，且比较结果要汇进 violations
    cv = _block(t, "def check_values(", "\ndef ")
    if not re.search(r"if\s+nums\s+and\s+lo\s+is\s+not\s+None:", cv):
        return _r(False, "缺少下限比较分支：越界值（如设备哨兵值 -9999）进不了契约检查")
    if not re.search(r"if\s+nums\s+and\s+hi\s+is\s+not\s+None:", cv):
        return _r(False, "缺少上限比较分支：单位没换算导致的 1000 倍偏差漏得掉")
    if not re.search(r'out\[.violations.\]\.append', cv):
        return _r(False, "体检结果没有汇总进 violations：查得出来但没人告诉使用者")
    # 必填整列缺失是最容易被静默放过的一类
    if not re.search(r"整列缺失", cv):
        return _r(False, "缺：必填列整列缺失没有被点名")

    # 4) 接进流水线才算数，光有模块等于没做
    tpl = _read(PIPELINE_FILE) or ""
    if 'contract as CT' not in tpl:
        return _r(False, "pipeline 未导入 contract：模块写好了但没接进流程")
    if "CT.check_input(" not in tpl:
        return _r(False, "pipeline 未对输入做契约检查")
    if "CT.check_result(" not in tpl:
        return _r(False, "pipeline 未对结果做契约检查")

    return _r(True, "", {"compat": True, "result": True, "values": True})


def rule_d8_closure_gate():
    """P1-9：8D 能不能结案必须能由机器判出来。

    改造前 derive_d8({}) 的 blockers 为空、也没有 can_close：
    零证据喂进去，照样吐出一份完整的"结案检查清单"。
    使用者拿去见客户才发现，机器从头到尾没说过"能不能结案"。

    判据落在四个具体动作上（不停留在"字样出现过"）：
      can_close 真的被返回、每个 gate 绑定了证明它所需的证据字段、
      日期是真解析而非字符串比大小、渲染时真的把录入卡传给了门禁。
    """
    t = _read(EIGHTD_FILE) or ""
    if not t:
        return _r(False, "读不到 " + EIGHTD_FILE)

    cc = _block(t, "def closure_check(", "\ndef ")
    if "def closure_check(" not in t:
        return _r(False, "缺少 closure_check：结案与否无法由机器判定")
    # 1) 必须产出机器可判的结论，而不是只给一段给人看的清单
    if not re.search(r'"can_close":\s*\(?not blockers\)?', cc) and \
       not re.search(r'"can_close":\s*not blockers', cc):
        return _r(False, "closure_check 未产出 can_close（或它的值与 blockers 脱钩）")
    # 2) 每个 gate 必须声明"靠什么证明"——否则项和项之间没有区别
    if '"evidence_required"' not in cc:
        return _r(False, "gate 未绑定所需证据清单：无法逐项判定")
    if "missing" not in cc:
        return _r(False, "gate 未记录缺哪些证据：使用者不知道该补什么")
    # 3) 日期必须真解析：字符串比大小对任何输入都成立，
    #    "去年冬天" 会被静默判成合法并通过。这条是本轮实测出来的真坑。
    if "def _parse_date(" not in t:
        return _r(False, "缺 _parse_date：日期只能做字符串比较，非标准写法会被静默放行")
    if not re.search(r"_vd_d is None or _end_d is None", cc):
        return _r(False, "日期解析不出来时应当阻断，当前实现默认放行")
    # 判据落在**正确调用的形态**上：比较必须发生在解析之后的日期变量之间。
    # 不去搜索 "str(_vd) < str(_end)" 这种错误串 —— 我上一版就是这么写的，
    # 结果自己注释里提到这个写法都被判为违规（自我指涉第三次）。
    if not re.search(r"^\s+elif _vd_d < _end_d:", cc, re.M):
        return _r(False, "比较必须基于解析后的日期变量 _vd_d/_end_d，不能直接比原始字符串")
    if not re.search(r"^\s+_vd_d = _parse_date\(_vd\)", cc, re.M):
        return _r(False, "验证日期未走 _parse_date 解析")
    # 解析不出来要记账才好排查
    if re.search(r"_time_check = \"无法校验\"", cc) is None:
        return _r(False, "日期无法校验时没有标记为无法校验")

    # 4) 渲染必须把录入卡传进去，否则门禁永远看不到证据。
    # 这里不用 _block 取块：render_8d 是长函数，_block 的 maxlen 截断会让
    # 调用行落在截取范围外，规则就变成恒失败——而恒失败看起来跟"抓到了"一样。
    # 改用行首锚定的全文件正则，既躲开注释，又不依赖片段长度。
    if not re.search(r"^\s+d8 = derive_d8\(res, lead, card=card\)", t, re.M):
        return _r(False, "渲染 8D 时没有把录入卡传给 derive_d8：门禁拿不到证据")
    if not re.search(r'^\s+rep\.kv\(s, \[\("结案判定"', t, re.M):
        return _r(False, "报告没有给出结案判定结论：审核者要自己去数哪几条打勾")
    # derive_d8 自身也要能把 card 透下去
    d8 = _block(t, "def derive_d8(", "\ndef ")
    if "card=(card or res.get(" not in d8:
        return _r(False, "derive_d8 未把 card 透传给 closure_check")

    return _r(True, "", {"closure": True, "dates": True, "wired": True})


def rule_p1_verifiability_weighted():
    """P1-5：根因评分的可验证性权重必须真正生效，不得硬编码 0.5 后假装算过。

    改造前 0.10*verifiability 里的 verifiability 恒等于 0.5：没算进排序分、
    也不随候选输出，这一项权重从没生效过——等于写了一套"看起来有可解释性"的评分。
    """
    t = _read(ROOTCAUSE_FILE) or ""
    if not t:
        return _r(False, "读不到 " + ROOTCAUSE_FILE)
    # 1) 必须有按候选动态计算可验证性的函数
    if "def _verifiability(" not in t:
        return _r(False, "缺少 _verifiability：可验证性权重仍可能被硬编码")
    # 2) 不得写死 0.5：两种写法都算退化
    if re.search(r"verifiability\s*=\s*0\.5", t):
        return _r(False, "可验证性仍硬编码为 0.5：权重未真正生效")
    if re.search(r"0\.10\s*\*\s*0\.5", t):
        return _r(False, "评分仍写死 0.10*0.5：可验证性未参评")
    # 3) 收尾段必须把可验证性分项计入总分（正确调用形态：0.10 * _vf）
    if not re.search(r"0\.10\s*\*\s*_vf", t):
        return _r(False, "总分未含 0.10*_vf：可验证性未计入排序")
    # 4) 候选必须输出分项，且返回体注明权重是启发式非概率
    if '"verifiability_score"' not in t:
        return _r(False, "候选未输出 verifiability_score：使用者看不到这一项")
    if '"score_note"' not in t:
        return _r(False, "返回体未注明权重为排序启发式而非概率")
    return _r(True, "", {"verifiability_weighted": True})


def rule_p1_field_mapping_confidence():
    """P1-3：字段映射必须结构化（来源/候选/置信度/量纲风险），低置信度可阻断。

    改造前 map_columns 返回裸 {列: key}，没有来源/置信度，低置信只能"猜了就用"，
    单位换算靠白名单碰巧挡住跨量纲。现在必须：① map_columns_detailed 返回带
    source/confidence/candidates 的记录；② apply_map_policy 支持 block 把低置信判未匹配；
    ③ 量纲风险（unit_risk）被显式检测而非靠白名单运气；④ 清洗报告交出映射明细。
    """
    t = _read(PREPROCESS_FILE) or ""
    if not t:
        return _r(False, "读不到 " + PREPROCESS_FILE)
    if "def map_columns_detailed(" not in t:
        return _r(False, "缺少 map_columns_detailed：映射仍是裸 {列: key}")
    if "def _match_one(" not in t:
        return _r(False, "缺少 _match_one：无法产出结构化匹配记录")
    if '"confidence"' not in t:
        return _r(False, "匹配记录未带 confidence：下游无法按置信度决策")
    if '"candidates"' not in t:
        return _r(False, "匹配记录未保留候选集：无法追溯『为什么配这个』")
    if "def apply_map_policy(" not in t:
        return _r(False, "缺少 apply_map_policy：低置信度无法被阻断/确认")
    if 'policy == "block"' not in t:
        return _r(False, "apply_map_policy 不支持 block：低置信列无法判未匹配")
    if "def _unit_mismatch(" not in t:
        return _r(False, "缺少 _unit_mismatch：跨量纲误映射只能靠白名单碰巧挡住")
    if '"unit_risk"' not in t:
        return _r(False, "匹配记录未带 unit_risk：量纲不一致不可见")
    if "col_hints & decl_hints" not in t:
        return _r(False, "unit_risk 未用量纲 token 集合求交：归一串接会假阴性")
    if '"mapping_detail"' not in t:
        return _r(False, "clean 报告未输出 mapping_detail：结构化映射不落地")
    if '"low_confidence_mappings"' not in t:
        return _r(False, "clean 报告未输出 low_confidence_mappings：低置信不可见")
    return _r(True, "", {"field_mapping_confidence": True})


def rule_p1_spc_exclusion_review():
    """P1-2：SPC 剔除必须可审计——可切换为仅建议/审核、逐点带规则身份与审核人。

    改造前 phase1_limits 默认自动剔除，缺 mode 开关；逐点只留下全局 rules_used，
    丢了"哪个点被哪条规则触发"；且无 review_status/reviewed_by，审核动作无法追溯。
    """
    t = _read(SPCPy) or ""
    if not t:
        return _r(False, "读不到 " + SPCPy)
    # 1) 必须有剔除模式开关（auto / suggest / review）与审核入参
    if "def phase1_limits(" not in t:
        return _r(False, "缺少 phase1_limits：SPC 剔除逻辑缺失")
    if 'mode == "suggest"' not in t or 'mode == "review"' not in t:
        return _r(False, "phase1_limits 未处理 suggest/review 模式（开关不完整）")
    if "review=None)" not in t:
        return _r(False, "phase1_limits 无 review 入参：审核动作无法接入")
    # 2) 非 auto 模式必须可达（归一后才分支），否则 suggest/review 是死开关
    if 'mode = (mode or "auto").lower()' not in t:
        return _r(False, "mode 未归一：suggest/review 分支不可达（开关是摆设）")
    # 3) 逐点身份：每条候选点必须带命中规则号与可读理由（exclusion_detail / suspect_points）
    if '"exclusion_detail"' not in t:
        return _r(False, "缺 exclusion_detail：被剔除点无逐点规则身份")
    if '"suspect_points"' not in t:
        return _r(False, "缺 suspect_points：仅建议/审核模式下候选点无可追溯载体")
    if '"rules": rl' not in t:
        return _r(False, "逐点规则身份未落字段（rules 未随点记录）")
    # 4) 审核人/状态必须随点保留，且 review 真正被使用（不是写了不读）
    if '"review_status"' not in t:
        return _r(False, "suspect_points 未带 review_status：审核状态不可见")
    if '"reviewed_by"' not in t:
        return _r(False, "suspect_points 未带 reviewed_by：审核人不可追溯")
    if 'rev.get("status") == "approved"' not in t:
        return _r(False, "review 未被实际使用：approved 之外一律剔除，审核形同虚设")
    # 5) 管线必须真正把 mode 透传下去，否则开关永远是死代码
    tp = _read(PIPELINE_FILE) or ""
    if "spc_exclusion_mode" not in tp:
        return _r(False, "pipeline.analyze 未接入 spc_exclusion_mode：开关未接线")
    if "mode=spc_exclusion_mode" not in tp:
        return _r(False, "phase1_limits 未被传入 mode=spc_exclusion_mode：开关未生效")
    return _r(True, "", {"spc_exclusion_review": True})


def rule_p1_doe_structure():
    """P1-7：DOE 方案必须给出随机化 / 重复 / 区组 / 别名结构 / 功效的结构化信息。

    改造前只有一张设计矩阵：没说先做哪一号、做几次、要不要分区组、
    哪些效应其实混在一起分不开、这个样本量能检出多大的差异——全靠现场用习惯补。
    判据落在**计算动作**上（真的调用了随机化、真的用异或判别名、功效真的按公式算），
    不落在"字段出现过"这种字样层，否则把计算删掉、只留字段也能蒙混过关。
    """
    t = _read(DOE_FILE) or ""
    if not t:
        return _r(False, "读不到 " + DOE_FILE)
    # 1) 结构化入口存在
    if "def design_structure(" not in t:
        return _r(False, "缺少 design_structure：DOE 无结构化设计说明入口")
    # 2) 随机化必须真的发生（有发生器 + 真的洗牌），不是只写一句"建议随机化"
    if "random.Random(" not in t:
        return _r(False, "design_structure 未建立可复现的随机化发生器（缺 random.Random）")
    if not re.search(r"\.shuffle\s*\(", t):
        return _r(False, "运行顺序未被随机化：run_sheet 会退化成顺序执行，未知干扰冒充因子效应")
    # 3) 别名结构必须按矩阵计算（调用 _two_level_cols 取列后判异或），不能是查表或空壳
    if "def alias_structure(" not in t:
        return _r(False, "缺少 alias_structure：混杂关系无从得知")
    if not re.search(r"[=(]\s*_two_level_cols\s*\(", t):
        return _r(False, "alias_structure 未调用 _two_level_cols：别名不是从矩阵算出来的")
    if not re.search(r"\^\s*y", t):
        return _r(False, "别名判据缺按位异或运算：二水平交互列等价关系未成立")
    if '"resolution"' not in t or '"main_aliases"' not in t:
        return _r(False, "别名结构未输出 resolution / main_aliases")
    # 4) 功效必须按公式算，不能写死一个数
    if "def power_analysis(" not in t:
        return _r(False, "缺少 power_analysis：样本量够不够无人知晓")
    if not re.search(r"\(\s*z_a\s*\+\s*z_b\s*\)\s*\*\s*se", t):
        return _r(False, "可检出效应不是按 (z_a+z_b)*se 计算的：功效数字不可信")
    if '"detectable_effect_sigma"' not in t:
        return _r(False, "功效未输出可检出效应（detectable_effect_sigma）")
    # 5) 残差自由度归零必须告警——饱和正交表最常见，"做了却下不了结论"必须被说出来
    if not re.search(r"if\s+residual_df\s*<=\s*0\s*:", t):
        return _r(False, "残差自由度归零未告警：饱和设计的 F 检验不可用却无人提醒")
    # 6) 重复与区组必须进结构化输出
    if '"replicates"' not in t or '"blocks"' not in t:
        return _r(False, "design_structure 未输出 repeats/blocks 结构")
    if '"run_sheet"' not in t:
        return _r(False, "缺少 run_sheet：没有可直接执行/打印的作业顺序表")
    # 7) 管线必须真正接线，否则这些能力只是库里的死代码
    tp = _read(PIPELINE_FILE) or ""
    if "DOE.design_structure(" not in tp:
        return _r(False, "pipeline 未调用 design_structure：DOE 结构化未接线")
    if "doe_replicates" not in tp or "doe_seed" not in tp:
        return _r(False, "pipeline.analyze 未接入 doe_replicates/doe_seed：参数不可调")
    return _r(True, "", {"doe_structure": True})


def rule_p2_knowledge_lifecycle():
    """P2-2：知识必须有生命周期 schema、状态机与出处覆盖率审计。

    审计原话：无适用工序/设备/材料/前提/样本量/证据等级/验证时间/失效条件/所有者/状态机；
    params 里带出处的条目仍是 0（含 typical_range 与控制阈值）。

    判据走文本而不是 import：变异打在临时副本上，动态 import 会读到真实库，
    变异就抓不到——那不是 FAIL，是静默 SKIP。
    """
    t = _read(KBSTORE_FILE) or ""
    if not t:
        return _r(False, "读不到 " + KBSTORE_FILE)
    # 1) schema：台账点名的字段必须真的进了 schema，不是写在注释里
    _blk = _block(t, "LIFECYCLE_FIELDS = {", "\n}", maxlen=2000) or ""
    if not _blk:
        return _r(False, "缺少 LIFECYCLE_FIELDS：知识生命周期 schema 未定义")
    for f in ("applicable_processes", "device_models", "materials", "preconditions",
              "sample_size", "evidence_level", "verified_at", "invalidates_if",
              "owner", "status"):
        if '"%s"' % f not in _blk:
            return _r(False, "生命周期 schema 缺字段 %s" % f)
    # 2) 最小必填集不能为空，否则"覆盖率"永远是 100% 的假象
    if not re.search(r'LIFECYCLE_MINIMUM\s*=\s*\(\s*"', t):
        return _r(False, "LIFECYCLE_MINIMUM 为空：无法判断条目现在还能不能用")
    # 3) 状态机：draft 可进 active；active 不可退回 draft；deprecated 是终态
    if "LIFECYCLE_TRANSITIONS" not in t:
        return _r(False, "缺少 LIFECYCLE_TRANSITIONS：状态流转无约束")
    _tb = _block(t, "LIFECYCLE_TRANSITIONS = {", "\n}", maxlen=400) or ""
    # 必须按行首键名取，不能取"第一个含 active 的行"——
    # 那一行其实是 draft 的定义（它把 active 列为可流转目标），会自己把自己判死。
    _act = [ln for ln in _tb.splitlines() if re.match(r'^\s*"active"\s*:', ln)]
    if not _act:
        return _r(False, "状态机缺 active 的流转定义")
    if '"draft"' in _act[0]:
        return _r(False, "active 可退回 draft：能把已生效的知识悄悄绕过复核")
    if not re.search(r'"deprecated":\s*\(\s*\)', _tb):
        return _r(False, "deprecated 不是终态：废弃知识可被复活")
    if "def check_transition(" not in t:
        return _r(False, "缺 check_transition：状态机只定义不执行")
    # 4) 出处覆盖率审计必须真的扫全库（默认不指向 LIFECYCLE_TARGETS 就是空跑）
    if "def audit_lifecycle(" not in t:
        return _r(False, "缺 audit_lifecycle：出处覆盖率无从度量")
    if not re.search(r"or\s+LIFECYCLE_TARGETS", t):
        return _r(False, "audit_lifecycle 未以 LIFECYCLE_TARGETS 为默认范围：审计会空跑")
    if '"source_coverage"' not in t:
        return _r(False, "审计未输出 source_coverage：出处缺口没有数字")
    # 5) 取值合法性必须逐条报，且不能拿 cases 的 A/B/C 等级造假阳性
    if "def lifecycle_issues(" not in t:
        return _r(False, "缺 lifecycle_issues：非法取值无人检查")
    if '"lifecycle": False' not in t:
        return _r(False, "cases 段未排除生命周期词表：A/B/C 证据等级会被误判成缺陷")
    # 6) 能力必须可达：编排器要能真的跑出这份读数。
    #    注意：本规则就在 fde_run.py 里，判据若写裸子串会先命中自身描述文字
    #    （"def flow_kb(" 这几个字在本文件里也出现），所以一律用行首锚定隔离。
    tf = _read(os.path.join("scripts", "fde_run.py")) or ""
    if not re.search(r"^def\s+flow_kb\s*\(", tf, re.M):
        return _r(False, "编排器未实现 flow_kb：生命周期审计是库里的死代码")
    if not re.search(r'^\s*if\s+args\.flow\s*==\s*"kb":', tf, re.M):
        return _r(False, "编排器未把 kb 接进命令分发：流程存在但调不到")
    return _r(True, "", {"knowledge_lifecycle": True})


def rule_p2_observability():
    """P2-4：结构化日志与可观测性——耗时/重试/错误分类/模块成功率/资源上限/脱敏。

    审计原话：无结构化日志、耗时、重试、错误分类、模块成功率、输入质量指标、
    资源上限、日志脱敏。

    判据一律落在**计算动作**上：脱敏规则要真的被遍历、错误分类要真的走分类表、
    失败要真的进成功率的分母。只查"函数定义存在"的话，
    把实现掏空只留壳也能判过——那等于这条规则没写。
    """
    t = _read(OBS_FILE) or ""
    if not t:
        return _r(False, "读不到 " + OBS_FILE)
    # 1) 结构化事件与七项能力
    for fn in ("def event(", "def mask(", "def classify_error(", "class Timer(",
               "def retry(", "def check_resources(", "def record_input_quality(",
               "def summary(", "def read_log(", "def summarize_log(", "def reset("):
        if fn not in t:
            return _r(False, "observ 缺 %s：可观测性能力不全" % fn)
    # 2) 脱敏必须真的遍历规则表（不是只定义函数）
    if not re.search(r"for\s+pat,\s+rep\s+in\s+_MASK_RULES", t):
        return _r(False, "mask 未遍历 _MASK_RULES：日志脱敏是空壳")
    if "1[3-9]" not in t or "<user>" not in t:
        return _r(False, "脱敏规则缺手机号或用户目录：日志会带走个人信息")
    # 3) 错误分类必须真的走分类表
    if not re.search(r"for\s+cls,\s+types\s+in\s+_CLASSIFY_RULES", t):
        return _r(False, "classify_error 未遍历 _CLASSIFY_RULES：失败该找谁说不清")
    if '"self_errors"' not in t or "def _self_error(" not in t:
        return _r(False, "observ 不暴露自身错误：诊断通道带病会让人以为查过了")
    # 4) 资源上限
    if "RESOURCE_LIMITS = {" not in t:
        return _r(False, "缺 RESOURCE_LIMITS：病态输入会一直跑到既慢又不可信")
    # 5) 模块成功率：失败必须进分母，否则成功率恒为 1.0 的假象
    if not re.search(r'^\s+st\["ok" if ok else "fail"\]\s*\+=\s*1', t, re.M):
        return _r(False, "失败未计入模块统计：成功率可以靠只数成功来伪造")
    # 6) 接线：日志写完要有人能查，失败要能从唯一收口点进来
    tf = _read(os.path.join("scripts", "fde_run.py")) or ""
    #    判据在本文件里也出现同样字样，所以一律行首锚定，避免自我指涉。
    if not re.search(r"^def\s+flow_ops\s*\(", tf, re.M):
        return _r(False, "编排器未实现 flow_ops：日志写完没人能查")
    if not re.search(r'^\s*if\s+args\.flow\s*==\s*"ops":', tf, re.M):
        return _r(False, "编排器未把 ops 接进命令分发")
    td = _read(os.path.join("lib", "degrade.py")) or ""
    if "_OBS.fail(" not in td:
        return _r(False, "降级台账未转发到 observ：护栏失败不计入模块成功率")
    tp = _read(PIPELINE_FILE) or ""
    if "OBS.end_run(" not in tp or "observability" not in tp:
        return _r(False, "pipeline 未产出 observability 汇总：跑了也不留痕")
    return _r(True, "", {"observability": True})


def rule_boundary_policy_knob():
    """推断边界政策旋钮（warn/block）：默认不变，但收紧时真拦得住。

    台账原话：推断边界仍会出方案，暂时【警告 + 要求书面确认】，没有默认阻断；
    是否收紧成硬阻断需产品决策。

    判据落在三处真正的执行点上：政策值被接收、非法值被拒绝、block 分支真升级。
    只查"有 BOUNDARY_POLICIES 这个常量"是不够的——常量在、分支不生效，
    等于给了个假旋钮。
    """
    t = _read(DOE_FILE) or ""
    if not t:
        return _r(False, "读不到 " + DOE_FILE)
    # 1) 政策集合存在，且 warn 与 block 都在
    if "BOUNDARY_POLICIES" not in t:
        return _r(False, "缺 BOUNDARY_POLICIES：推断边界无政策旋钮")
    if '"warn"' not in t or '"block"' not in t:
        return _r(False, "政策集合缺 warn 或 block")
    # 2) 传入的政策值必须被真正接收（不能焊死成常量）
    if not re.search(r'policy\s*=\s*\(policy or "warn"\)', t):
        return _r(False, "boundary_gate 未接收传入的 policy：旋钮被焊死")
    # 3) 非法政策值必须拒绝，不静默回落
    if not re.search(r"if\s+policy\s+not\s+in\s+BOUNDARY_POLICIES\s*:", t):
        return _r(False, "非法政策值未校验：静默回落等于以为收紧了其实没收紧")
    # 4) block 政策必须真的把警告升级为阻断
    if not re.search(r'if\s+policy\s*==\s*"block"\s+and\s+warns\s*:', t):
        return _r(False, "block 政策分支不存在：收紧后拦不住")
    if "escalated" not in t:
        return _r(False, "收紧后未输出 escalated：无从得知升级了几条")
    # 5) 升级后必须清空 warnings，否则一边 ok=False 一边还写着放行口吻
    if not re.search(r'^\s+blockers = blockers \+ \[', t, re.M):
        return _r(False, "警告未并入 blockers：block 政策下仍会出方案")
    # 6) 管线必须接线且 CLI 可达，否则旋钮是库里的死参数
    tp = _read(PIPELINE_FILE) or ""
    if "policy=boundary_policy" not in tp:
        return _r(False, "pipeline 未把 boundary_policy 传给 boundary_gate：旋钮未接线")
    if "--boundary-policy" not in tp:
        return _r(False, "CLI 未暴露 --boundary-policy：使用者无法收紧")
    return _r(True, "", {"boundary_policy": True})


def rule_product_profile_merge():
    """产品档与多产品配置合并：按语义合并、交人决断不猜、显式优先、能力可达。

    这条规则要防的是一类特定退化：**多产品并存时"谁在后面谁赢"**。
    那是 tenancy.deep_merge 的行为，对单一覆盖源是对的，对多个平级来源就是
    静默抉择——系统替使用人选了一套判严尺度，还不告诉他选了哪个、为什么。
    """
    t = _read(PROFILE_FILE) or ""
    if not t:
        return _r(False, "读不到 lib/profile.py：产品档配置解析不存在")
    if "def resolve_config(" not in t:
        return _r(False, "缺 resolve_config：多产品配置没有解析入口")
    if "def _merge_candidates(" not in t:
        return _r(False, "缺 _merge_candidates：合并逻辑若散在各处，语义就会各写各的")
    if "def cross_matrix(" not in t:
        return _r(False, "缺 cross_matrix：多产品交叉搭配映射表无法生成")

    # 1) 四类合并语义都在（取严/取大/取并集/交人）
    for kind in ('"kind": "rank"', '"kind": "max"', '"kind": "set_union"',
                 '"kind": "manual"'):
        if kind not in t:
            return _r(False, "合并语义缺 %s：多产品只能靠位置决定，等于谁后谁赢" % kind)

    # 1b) rank 语义必须真的"按严度择优"，而不是按候选顺序取一个。
    #     少了这条，取严退化成"取最后一个"时规则照样判过——
    #     这正是判据停在字样层的典型症状：删掉要防的那行，规则还判过。
    if not (re.search(r"max\(known,", t) and re.search(r"min\(known,", t)):
        return _r(False, "rank 语义缺按严度择优的动作：取严会退化成按位置取，"
                         "多产品又变谁在后面谁赢")

    # 2) manual 必须真的交人：返回空值 + 标记需决断，而不是取第一个
    if 'return (None, kind, "各来源不一致且该参数无法自动取' not in t:
        return _r(False, "无法自动判定的参数没有交人（manual 语义未落地）")
    if "needs.append(" not in t:
        return _r(False, "需人决断的项没有进 needs_decision：等于悄悄丢掉")

    # 3) 显式配置优先，且被覆盖的来源仍留痕
    if "if param in explicit:" not in t:
        return _r(False, "缺显式优先分支：角色/产品会覆盖使用人写死的值（越权）")
    # 判据落在"显式分支内确实把值写进 config 并跳出"，
    # 而不是只留一条 trace——只留痕不生效，等于显式优先形同虚设
    _m = re.search(r"if param in explicit:(.*?)continue", t, re.S)
    if not _m or "config[param] = ev" not in _m.group(1):
        return _r(False, "显式分支未真正写入 config：显式优先形同虚设")

    # 4) 角色视角每条都要带理由（没理由的判严等于黑箱）
    if '"reason":' not in t:
        return _r(False, "角色视角没写理由：使用人不知道为什么被这么配")

    # 5) 交叉表要有规模上限（2^n 会爆炸，255 行的表没人看）
    if "max_products" not in t:
        return _r(False, "交叉表无产品数上限：产品一多就生成无人会看的表")

    # 6) 语义表每条都要写明理由
    if '"why":' not in t:
        return _r(False, "合并语义没写理由：凭什么取严/取大说不清")

    # 7) 能力必须可达：analyze 要真的用上，编排器要能查
    tp = _read(PIPELINE_FILE) or ""
    if "PF.resolve_config(" not in tp:
        return _r(False, "pipeline 未调用 resolve_config：产品档是库里的死代码")
    if "role=args.role" not in tp:
        return _r(False, "pipeline CLI 未接 --role：使用者立场选不了")
    tf = _read(os.path.join("scripts", "fde_run.py")) or ""
    # 行首锚定：本规则就在 fde_run.py 里，不锚定会先命中自己的描述文字
    if not re.search(r'^\s*ap\.add_argument\("--products"', tf, re.M):
        return _r(False, "编排器未暴露 --products：多产品配置查不到、生不成")
    return _r(True, "多产品配置按语义合并、冲突交人、显式优先、交叉表与角色视角可达")


def rule_autocorrelation_chart():
    """自相关过程必须换图，且必须说出这张图的能力边界。

    判据落在**动作与算术**上：把 `cusum` 里的放大乘法删掉、把 EWMAST 换回普通
    EWMA、把能力边界的告警删掉，都要被这条抓出来——只查"有没有这个函数"的话，
    函数名还在而行为被换掉照样判过。
    """
    t = _read(SPCPy) or ""
    p = _read(PIPELINE_FILE) or ""
    if "def autocorrelation_diagnosis(" not in t or "def ewma_stationary(" not in t:
        return _r(False, "缺自相关诊断或 EWMAST（lib/spc.py）")
    # 1) CUSUM 的决策区间必须按自相关放大 —— 不放大等于把序列惯性当趋势报
    if not re.search(r"hh\s*=\s*h\s*\*\s*sigma\s*\*\s*math\.sqrt\(_f\)", t):
        return _r(False, "CUSUM 决策区间未按自相关放大（惯性会被当成趋势报出来）")
    # 2) 自相关显著时管线必须换用 EWMAST，而不是继续用被放大限的普通 EWMA
    if "SPC.ewma_stationary(" not in p:
        return _r(False, "管线在自相关序列上未改用 EWMAST")
    # 3) 能力边界：不给 min_detectable_shift，读者会把"没报警"读成"过程很稳"。
    #    查**写入动作**而不是字样——只搜 "min_detectable_shift" 的话，
    #    读取处（`(_ew or {}).get(...)`）会让整串一直存在，删掉赋值照样判过。
    if "min_detectable_shift" not in t \
            or not re.search(r'^\s*_acd\["min_detectable_shift"\] = ', p, flags=re.M):
        return _r(False, "未把最小可检出偏移写进结果：自相关越强检不出越大，"
                         "不说等于默认这张图什么都能抓到")
    # 4) 行号/序号这类列不得进 SPC（必然自相关 r1≈1，会刷出一整排报警把真异常淹没）。
    #    按"column_role 后几行内出现 measure/count 白名单"匹配，
    #    不用 `[^)]*` 那种写法——它跨不过 numeric_column(c) 里的右括号，
    #    正则静默不命中，判据就变成摆设。
    _lines = p.splitlines()
    _ok = False
    for i, ln in enumerate(_lines):
        if "QG.column_role(c," in ln:
            for j in range(i, min(i + 3, len(_lines))):
                if 'not in ("measure", "count")' in _lines[j]:
                    _ok = True
    if not _ok:
        return _r(False, "SPC 未按列角色过滤：序号/行号列必然自相关，会把真异常淹没")
    return _r(True, "", {"n_checks": 4})


def rule_export_native():
    """原生 Word/PDF 导出必须是「真文件」，且导出失败必须出声。

    判据一律落在**写入动作**，不停在"有没有这个函数"：
      * .docx 必须写 `[Content_Types].xml` 与图片部件——只查入口的话，
        把 HTML 换个扩展名照样判过，而对面的 8D 系统根本吃不下；
      * .pdf 的 xref 偏移必须来自累加的写入位置（offsets[i]），
        写死偏移的话文件生成得出来、所有阅读器都打不开；
      * 导出失败必须同时进 warnings 与 degraded_sections——
        少了交付形态却不出声，使用者会以为导出过、只是没找到文件。
    """
    t = _read(EXPORT_FILE) or ""
    p = _read(PIPELINE_FILE) or ""
    if not t:
        return _r(False, "读不到 lib/export_doc.py")
    for fn in ("def render_docx(", "def write_docx(",
               "def render_pdf(", "def write_pdf("):
        if fn not in t:
            return _r(False, "缺导出入口 %s（lib/export_doc.py）" % fn)
    # 1) docx 必须是 OOXML 包；没有 Content_Types 就只是换后缀的文件
    if 'z.writestr("[Content_Types].xml"' not in t:
        return _r(False, "docx 未写 [Content_Types].xml：那不是 Word 包")
    # 2) 图表必须真的作为部件进包。报告里的图是证据，丢图＝丢证据
    if 'z.writestr("word/media/"' not in t:
        return _r(False, "docx 未写入图片部件：图表在 Word 版里被丢掉了")
    # 3) PDF：标准头 + startxref，且 xref 偏移来自实际写入位置
    if 'out.write(b"%PDF-1.4' not in t or "startxref" not in t:
        return _r(False, "PDF 缺标准头或 startxref，阅读器打不开")
    if "offsets.append(out.tell())" not in t \
            or not re.search(r'"%010d 00000 n[^"]*"\s*%\s*offsets\[i\]', t):
        return _r(False, "PDF 的 xref 偏移不是来自实际写入位置："
                         "文件能生成但谁都打不开")
    # 4) 中文不能变成方框/乱码
    if "STSong-Light" not in t or "UniGB-UCS2-H" not in t:
        return _r(False, "PDF 未挂中文预定义字体与 UniGB-UCS2-H 编码：正文会成乱码")
    # 5) 复杂 SVG 元素不硬猜——画一张错的图比不画更糟
    if "图见同名 SVG" not in t:
        return _r(False, "复杂 SVG 元素没有原位标注兜底：画错的图会让人误读")
    # 6) 两个导入分支都要登记，只改一套会在另一种调用方式下 NameError
    if p.count("import export_doc as EX") < 2:
        return _r(False, "两个导入分支没有都登记 export_doc："
                         "只改一套会在另一条调用路径上 NameError")
    # 7) 导出失败必须出声：判据按"块内有没有两处登记"，删任一段都会被抓
    _i = p.find("if export_docx and EX is not None:")
    _j = p.find("if export_pdf and EX is not None:")
    _k = p.find("if export_xlsx:")
    if _i < 0 or _j < 0 or _k < 0 or not (_i < _j < _k):
        return _r(False, "管线未按 docx / pdf 分块导出")
    _bx, _bp = p[_i:_j], p[_j:_k]
    for _nm, _blk in (("docx", _bx), ("pdf", _bp)):
        if "导出失败" not in _blk \
                or 'setdefault("warnings"' not in _blk \
                or 'setdefault("degraded_sections"' not in _blk:
            return _r(False, "%s 导出失败未同时进 warnings 与 degraded_sections："
                             "少了交付形态却没人知道" % _nm)
    return _r(True, "", {"n_checks": 7})


def rule_exclude_invalid_segments():
    """停机/换卷/首件段必须从逐点统计里剔出去，但不能伪造相邻关系。

    这条规则盯的是两个方向相反、却同样致命的失效：

      A. 不剔 —— 冻结段把 σ 压小、Cpk 虚高（明明能力不够却判合格）；
         恢复段把 σ 撑大、Cpk 虚低（明明受控却判不合格）。
         两者都不报错，报告看起来都是"算出来了"。

      B. 剔了却处理错了接缝 —— 剔行之后前后两块被强行拼在一起，
         接缝处凭空出现一个从未发生过的差分，σ_within 被抬高、
         控制限被撑宽，最该报警的点反而落回限内。
         这是这一条规则真正难守的地方：代码看着还在"做排除"，
         实际把过程 alarm 的灵敏度悄悄调低了。

    判据全部落在**动作与赋值**上，且锚定到具体那一行：
    只查"有没有 blocks() 调用"会漏掉 seams 参数被内部清空的写法。
    """
    p = _read(PIPELINE_FILE) or ""
    g = _read(EXCLUDE_FILE) or ""
    if not p or not g:
        return _r(False, "读不到 lib/pipeline.py 或 lib/exclude.py")
    # 1) 两个导入分支都登记（只改一套会在另一种导入方式下 NameError）
    if p.count("import exclude as XCL") < 2:
        return _r(False, "两个导入分支没有都登记 exclude")
    # 2) 必须发生在所有统计之前。
    #    锚定在"缺陷识别"注释行之前——那是 6 之后各节统计的起点，
    #    放在它后面等于只给报告加了一段话，污染已经进了所有数字。
    _pos_exc = p.find("_exc = XCL.detect(")
    _pos_stat = p.find("# 6. 缺陷识别与 Pareto")
    if _pos_exc <= 0 or _pos_stat <= 0 or _pos_exc > _pos_stat:
        return _r(False, "无效段排除没有发生在统计之前："
                         "剔晚了等于没剔，数字已经算完了")
    # 3) 删行后必须重算时间列（这是最容易漏、也最难发现的一处：
    #    数据看着正常，结论整体偏一格）
    if not re.search(r"^[ \t]*times = PP\.parse_time_column\(work\)\[1\]",
                     p, re.M):
        return _r(False, "剔除行之后没有重算时间列：每一行的时间都会错位")
    # 3b) 第二份时间变量 _times 也要同步裁。
    #     zip(times, mask) 的形态才算是"按保留标记过滤"，只写 _times 不能算。
    if not re.search(r"_times = \[t for t, m in zip\(_times, _exc\[.mask.\]\) if m\]", p):
        return _r(False, "另一份时间变量 _times 没有同步裁剪："
                         "规格版本化按行取生效版本时会用错位的时间")
    # 4) SPC 必须只在连续块内做
    if "vals = _slice(work.numeric_column(c))" not in p:
        return _r(False, "SPC 没有按连续块切片：会跨接缝求差分，"
                         "把 σ_within 抬高、控制限撑宽")
    if "_times_local = _slice(_times)" not in p:
        return _r(False, "规格版本化取的时间没有跟着切片：values 与 times 长度不一致")
    # 5) seams 必须真的用上。判据不能只看函数签名里有 seams：
    #    把参数在函数体首行清空也能通过签名检查（变异 4 就是这么做的）。
    if re.search(r"def blocks\(mask, seams=\(\)\):\s*\n\s*seams = \(\)", g):
        return _r(False, "blocks 收到 seams 后立刻清空：接缝不会被断块")
    if "(i - 1) in seamset" not in g:
        return _r(False, "接缝两侧都保留时不断块：两坨隔了几小时的数据会被当成相邻观测")
    # 6) 剔除必须可见（给回原始行号，客户能回源核对）
    if "_e[\"row_from\"] = _src_before[_a]" not in p:
        return _r(False, "被剔除的行没有换算成原始文件行号：客户没法回源文件核对")
    if "无效段排除（停机 / 换卷 / 首件 / 断流）" not in p:
        return _r(False, "报告里没有无效段小节：使用者不知道这批数字剔过什么")
    # 7) 安全阀：判据显然用错了要整体放弃，而不是把半个数据集删掉。
    #    注意别去查常量值是多少（那是可调阈值，规定具体数字会把正当调整判死），
    #    要查的是"这条比较真的进了 if"——变异测试为此专门对着这一行下手。
    if "if n_ex and (kept < MIN_KEPT_ROWS or kept * 1.0 / n < MIN_KEPT_RATIO):" not in g:
        return _r(False, "剔除比例的安全线没有参与判定：规则暴走时会把产品当噪声删掉")
    if '"attempted": n_ex' not in g:
        return _r(False, "放弃剔除时没有交代原本识别出多少行：使用者看不出该人工核对")
    # 8) 自动推定必须以读数交叉验证（防把停机推定成在产、剔反方向）
    if "_cross_check_running(" not in g:
        return _r(False, "状态列缺少读数交叉验证："
                         "设备大部分时间停机时会把停机推定成在产状态，剔反方向")
    return _r(True, "", {"n_checks": 9})


def rule_scope_declaration():
    """单位/时区/班次的口径，判不了的必须显式声明，不许静默假设。

    这三样东西的共同失效方式：工具替使用者静默假设一个口径（"列名里那个括号就是
    单位""时间戳都按本机时钟""班次不用对齐")，错了相关性与 SPC 整体失真，
    报告还完全看不出来——因为它压根没告诉你它做了这些假设。

    判据落在"显式声明"这个动作上：未确认（naive/conflict/warn）不单是内部状态，
    它必须能进 downgrades 让下游降级，并且报告里要有能被人读到的一段。
    """
    p = _read(PIPELINE_FILE) or ""
    g = _read(CONTEXT_FILE) or ""
    if not p or not g:
        return _r(False, "读不到 lib/pipeline.py 或 lib/context.py")
    # 1) 两个导入分支都登记
    if p.count("import context as CXF") < 2:
        return _r(False, "两个导入分支没有都登记 context")
    # 2) 口径声明发生在统计之前（时间列解析后、缺陷识别前）
    _pos_ctx = p.find("result[\"scope_context\"] = {")
    _pos_stat = p.find("# 6. 缺陷识别与 Pareto")
    if _pos_ctx <= 0 or _pos_stat <= 0 or _pos_ctx > _pos_stat:
        return _r(False, "口径声明没有发生在统计之前："
                         "声明晚了，下游的统计已经按错误口径算完了")
    # 3) 无时区必须显式 naive（不是假装 UTC/固定时区）
    if '"status": "naive"' not in g:
        return _r(False, "无时区时间戳没有显式声明 naive：会静默按某个假定时区解释")
    # 4) 带/不带时区混着必须判 conflict
    if "if has_tz and no_tz:" not in g:
        return _r(False, "带/不带时区混在一份数据里没有判冲突："
                         "会漏掉一半 UTC 一半本地的情况")
    if '"status": "conflict"' not in g:
        return _r(False, "时区冲突没有显式的 conflict 状态")
    # 5) naive / shift 未登记要进 downgrades（这是降级的触发点，不是装饰字段）
    if 'if tz["status"] in ("naive", "conflict"):' not in g:
        return _r(False, "naive/conflict 没有进 downgrades：下游没法据此降级")
    if 'if shift["status"] == "warn":' not in g:
        return _r(False, "班次边界未登记没有进 downgrades：跨午夜切断的风险被吞掉")
    # 6) 报告里要有口径声明段
    if "**口径声明（单位 / 时区 / 班次）**" not in p:
        return _r(False, "报告里没有口径声明段：使用者不知道数字在什么口径下算的")
    # 7) CLI 要有显式登记入口（--timezone / --shift-boundary），
    #    否则"请登记时区"这句话指向一个不存在的参数
    if '"--timezone"' not in p or '"--shift-boundary"' not in p:
        return _r(False, "CLI 没有 --timezone / --shift-boundary："
                         "提示里让人去登记的口径根本没有入口")
    return _r(True, "", {"n_checks": 7})


def rule_baseline_stability():
    """基线稳定性必须显式结论，不稳时判异降级，且与判异结果分开呈现。

    phase1_limits 早就有 stable 标志，但一度只作为 Cpk 的 caveat 间接出现——
    读者在 SPC 段看不到"这条序列稳不稳"，只能看到控制图和判异结果。
    评审 P1-2 要的是：稳定性检查是**一等结论**，与判异结果并列、且不稳时
    判异结论要降级（因为控制限本身都被异常撑宽了，判异结论的灵敏度是失真的）。

    判据落在"显式字段"（baseline 摘要）与"报告呈现"（基线稳定性字样）上，
    以及"不稳时降级"的动作（那句"判异结论因此降级"）。
    """
    p = _read(PIPELINE_FILE) or ""
    g = _read(os.path.join("lib", "spc.py")) or ""
    if not p or not g:
        return _r(False, "读不到 lib/pipeline.py 或 lib/spc.py")
    # 1) spc_res 带 baseline 摘要（独立字段，不是埋在 caveats 里）
    if '"baseline": {' not in p:
        return _r(False, "SPC 结果没有独立的 baseline 摘要字段：稳定性结论丢了")
    if '"stable": (p1 or {}).get("stable")' not in p:
        return _r(False, "baseline 摘要没有透传 phase1 的 stable：字段成了空壳")
    # 2) 报告正文出现「基线稳定性」呈现，且不稳定分支必须存在
    if "**基线稳定性：未受控**" not in p:
        return _r(False, "报告里没有「基线稳定性：未受控」的呈现："
                         "不稳定过程和受控过程在报告里变得无法区分")
    if "**基线稳定性**：%s" not in p:
        return _r(False, "报告里没有「基线稳定性」的稳定分支呈现："
                         "稳定性结论没有一等呈现")
    # 3) 不稳时必须声明判异降级（这是"区分稳定性与判异"的关键动作）
    if "下方判异结论因此降级" not in p:
        return _r(False, "不稳定时没有声明「判异结论降级」："
                         "读者会把失真的判异当成可靠信号")
    # 4) phase1_limits 对整体不受控数据返回 stable=False（不谎报稳定）。
    #    必须锚定到 capped 分支那一条路径：把 stable=False 写死成"只要出现这个字面
    #    就算"的话，别的 stable=False（如样本不足路径）会替它兜底、变异就永远抓不到。
    #    capped 分支里 stable=False 之前隔着 base_positions/excluded_positions 两行
    #    代码（不是紧随 elif），所以正则要允许中间的任意非 stable 行。
    if not re.search(r"elif capped:\s*\n"
                     r"(?:[ \t]*(?:#[^\n]*|[A-Za-z_][^=\n]*=[^\n]*)\n){0,8}"
                     r"[ \t]*stable = False", g):
        return _r(False, "phase1_limits 的 capped 路径没有置 stable=False："
                         "整体不受控时会被当成稳定基线")
    if "stable = True" not in g:
        return _r(False, "phase1_limits 没有 stable=True 的判定路径：stable 成了常量")
    return _r(True, "", {"n_checks": 5})


def rule_action_cards():
    """行动卡片必须结构化可导出，未知字段不编造、设定值不替人拍。

    D5/D6 在 eightd 里已结构化，但各自为政、无法导出，现场只能手工抄卡片。
    本规则守住三件事：卡片字段契约齐全（MES 对接靠字段名）、导出带 schema、
    以及两条诚实铁律——不知道的字段（负责人/窗口）标待填不编造、
    设定值不替人拍（回指 DOE）。任何一处破了，现场拿到手里的就是失实或半截的卡片。
    """
    p = _read(PIPELINE_FILE) or ""
    g = _read(ACTIONCARD_FILE) or ""
    if not p or not g:
        return _r(False, "读不到 lib/pipeline.py 或 lib/actioncard.py")
    # 1) 两个导入分支都登记
    if p.count("import actioncard as AC") < 2:
        return _r(False, "两个导入分支没有都登记 actioncard")
    # 2) 卡片含 6 个核心字段（评审点名的字段契约）。
    #    必须锚定到 CARD_FIELDS 那个列表块——字段字符串在 D5/D6 卡片里也重复出现，
    #    只查字面的话删掉 CARD_FIELDS 里那一行、卡片 body 里还留着，判据照样过。
    _cf = re.search(r"CARD_FIELDS = \[\n(.*?)\n\]", g, re.S)
    _cfbody = _cf.group(1) if _cf else ""
    for f in ("suggested_value", "verification_batches", "success_criteria",
              "owner_role", "verification_window"):
        if ('"%s"' % f) not in _cfbody:
            return _r(False, "卡片字段契约缺「%s」：MES 对接的键丢了" % f)
    # 3) 导出 JSON 带 schema + fields 契约
    if '"schema": "action_card_v1"' not in g:
        return _r(False, "导出 JSON 没有 schema：下游拿不到字段含义")
    if '"fields": CARD_FIELDS' not in g:
        return _r(False, "导出 JSON 没有 fields 契约：字段顺序/含义丢失")
    # 4) 未知字段不编造（owner_role 标 None，导出 CSV 才转"待填"，源头不写假名字）
    if "        \"owner_role\": None,          # 待填：谁执行验证由现场排产定" not in g:
        return _r(False, "负责人角色被编造了：工具不能替厂里指派到人")
    # 5) 导出失败要出声（否则现场以为拿到卡片、实际文件是空的）
    if "行动卡片导出失败" not in p:
        return _r(False, "导出失败没有出声：静默失败会让现场以为卡片已生成")
    # 6) 卡片只在 8D 产出后生成
    if "if AC is not None and res.get(\"eightd\"):" not in p:
        return _r(False, "卡片不依赖 8D 产出就生成：8D 没算完，卡片是空壳")
    return _r(True, "", {"n_checks": 6})


def rule_process_fallback():
    """工序判定失败必须显式降级，未知模式不做强因果，页脚记录模式。

    判定失败曾被当成"顺带的结果"：chosen_proc=None 就往下跑，下游拿 None 当工序，
    等于静默进入无工序模式却没人知道。评审 P0-4 要的是升成一等状态 + 三档降级路径。
    判据落在"一等状态"（process_mode）、"降级动作"（根因不用机理先验）、
    以及"报告声明"（页脚写模式）三件事上。
    """
    p = _read(PIPELINE_FILE) or ""
    if not p:
        return _r(False, "读不到 lib/pipeline.py")
    # 1) process_mode 是一等状态，不是顺带结果
    if 'result["process_mode"] = _proc_mode' not in p:
        return _r(False, "判定失败没有登记 process_mode：仍会静默拿 None 当工序跑")
    # 2) 有 --process-mode 入口（使用者能显式选降级路径）
    if '"--process-mode"' not in p:
        return _r(False, "没有 --process-mode 入口：使用者无法显式选降级路径")
    # 3) unknown/descriptive 模式不用工序机理先验（强因果降级）
    if "_no_process_prior = (_proc_mode in (\"unknown\", \"descriptive\"))" not in p:
        return _r(False, "没有把 unknown/descriptive 标成 _no_process_prior："
                         "强因果没被降级")
    if "if defect_name and not _no_process_prior:" not in p:
        return _r(False, "根因没有依据 _no_process_prior 跳过工序机理："
                         "未知工序却仍查了工序知识库先验")
    if 'root["_no_process_prior"] = True' not in p:
        return _r(False, "根因结果没有标注 _no_process_prior：读者看不出这是降级结论")
    # 4) 报告页脚声明模式
    if '("工序判定模式", {' not in p:
        return _r(False, "报告页脚没有声明工序判定模式：读者不知道结论是按什么口径出的")
    if "本次处于**未知工序通用模式**" not in p:
        return _r(False, "unknown 模式没有在页脚出醒目的「不得当工序结论」声明")
    return _r(True, "", {"n_checks": 5})


def rule_license_notice():
    """报告页脚含许可自检、入口有许可摘要，不阻断内部自用。

    P4 要的是"许可合规工程化提示"——既不让企业内部自用被误阻断，也不让对外
    托管/服务的场景被含糊带过。落点：报告 17 节一句许可自检、入口一行许可摘要。
    """
    p = _read(PIPELINE_FILE) or ""
    r = _read(os.path.join("scripts", "fde_run.py")) or ""
    if not p or not r:
        return _r(False, "读不到 lib/pipeline.py 或 scripts/fde_run.py")
    # 1) 报告页脚有许可自检
    if "许可与使用场景自检" not in p:
        return _r(False, "报告页脚没有许可自检提示：对外托管的企业无感知")
    if "Elastic License 2.0" not in p:
        return _r(False, "报告页脚没有提 Elastic License 2.0：许可口径缺失")
    # 2) 内部/外部边界都说清（不只有一半）
    if "企业内部自用" not in p or "对外托管服务" not in p:
        return _r(False, "许可提示只写了一面（内部或外部）：边界没画全")
    # 3) 对外服务被显式提醒（不是含糊带过）
    if "请先确认商业授权" not in p:
        return _r(False, "对外服务场景没有被显式提醒：边界被含糊带过")
    # 4) 入口有许可摘要。判据必须锚定到 print 那句，不能只查裸字面——
    #    本规则函数自己的判据字符串里也含"Elastic License 2.0 · 企业内部自用"，
    #    只查字面会自我指涉（变异删 print、判据自己那行还留着这个字面，照样判过）。
    if 'print("许可：Elastic License 2.0 · 企业内部自用无需额外授权；' not in r:
        return _r(False, "入口没有许可摘要：使用者看不到一句话提示")
    return _r(True, "", {"n_checks": 5})


def rule_full_rules_precondition():
    """全量判异规则必须有前置条件提示（默认低误报，全规则需稳定 + Cpk≥1.33）。

    八条 Nelson 全开只适合过程已稳定受控、Cpk≥1.33 的场景——否则序列自相关/
    常规漂移会触发大量假报警，现场把图一关，比没有图更糟。规则集本身不拦
    （内部自用不被误拦），但必须明说前置条件，让使用者在误报面前知道
    "是规则开多了，不是工具坏了"。
    """
    p = _read(PIPELINE_FILE) or ""
    g = _read(SPCPy) or ""
    if not p or not g:
        return _r(False, "读不到 lib/pipeline.py 或 lib/spc.py")
    # 1) 全规则必须提示前置条件
    if "已启用全量 Nelson 判异规则（8 条）" not in p:
        return _r(False, "全规则没有前置条件提示：使用者会把自相关误报当工具坏了")
    # 2) 前置条件必须是"稳定 + Cpk≥1.33"（这是统计上的正确前提，不能只写一半）
    if "且 Cpk≥1.33 时使用" not in p:
        return _r(False, "前置条件没有提 Cpk≥1.33：稳定受控这个前提被丢掉")
    if "过程已稳定受控" not in p:
        return _r(False, "前置条件没有提过程稳定：只写 Cpk 丢了一半前提")
    # 3) 默认仍是低误报基本集（不是把全规则当默认）
    if "DEFAULT_RULES = (1, 2, 3, 5)" not in g:
        return _r(False, "默认规则集不是低误报基本集：默认就全开会误报泛滥")
    return _r(True, "", {"n_checks": 4})


def rule_lot_segment():
    """批号里提取的分组维度，不能是流水号。

    这条要防的是一种**看起来极有说服力、实际完全无用**的结论：
    把流水号当分组维度。按流水号分组，η² 会很高、p 值会很小，报告会写
    "批号第 3 段对不良率有显著影响"——现场能做的事是零，因为"第几个"
    不是可调整的工艺条件，D5 措施根本写不出来。

    判据落在判定动作与排除动作上：
      * 序号识别必须真的参与判定（不是有这个函数就算）；
      * 序号段的 η² 必须写明"不能直接采信"（那个 η² 是混杂出来的）；
      * 派生列必须排除在 SPC/相关性之外，否则日期编码会被当测量值画控制图；
      * 拆不准必须提示 --lot-pattern，不能静默略过。
    """
    p = _read(PIPELINE_FILE) or ""
    g = _read(LOT_FILE) or ""
    if not p or not g:
        return _r(False, "读不到 lib/pipeline.py 或 lib/lotcode.py")
    # 1) 两个导入分支都登记
    if p.count("import lotcode as LC") < 2:
        return _r(False, "两个导入分支没有都登记 lotcode")
    # 2) 序号识别必须参与种类判定（不是"有这个函数"就算）
    if not re.search(r"elif _is_sequence\(seg\):[^\n]*\n"
                     r"(?:[ \t]*#[^\n]*\n)*"
                     r"[ \t]*kind = \"sequence\"", g):
        return _r(False, "序号识别没有参与段分类：流水号会被当成可分组维度")
    # 3) 序号段的 η² 必须标注不可采信（它很可能是混杂出来的）
    if '"eta2_note"' not in g:
        return _r(False, "序号段的高 η² 没有标注混杂风险：会被当成显著分组依据")
    # 4) SPC 数值列必须排除派生列。
    #    判据锚在 numcols 那一行上——不能只查 "c not in _lot_d" 这个字样：
    #    相关性那行的 _lot_dd 本身就含这个子串，抹掉 SPC 这行照样命中（实测踩过）。
    if not re.search(r"numcols = \[c for c in work\.numeric_columns\(\)\n"
                     r"[ \t]*if c not in _lot_d\b", p):
        return _r(False, "SPC 数值列没有排除批号派生列："
                         "日期编码（20260401）会被当成测量参数画控制图")
    # 5) 相关性/归因的数值列同样要排除（漏这一处，日期编码照样进相关矩阵）
    if "if c != tcol and c not in _lot_dd" not in p:
        return _r(False, "相关性分析的数值列没有排除批号派生列："
                         "日期编码会与工艺参数算出假相关")
    # 6) 派生列不能被算进"未识别列"（否则报告一边说已提取、一边报警告）
    if "_lot_cols" not in p or "if c not in _lot_cols" not in p:
        return _r(False, "批号派生列被算进了未识别列：报告会自相矛盾")
    # 7) 拆不准要给 --lot-pattern，不能静默略过。
    #    只数 "请用 --lot-pattern"（两条 no_split 结论里各一处），
    #    不数模块说明里的那处——否则抹掉一条提示仍然凑够数（实测踩过）。
    if g.count('"status": "no_split"') < 2 or g.count("请用 --lot-pattern") < 2:
        return _r(False, "拆不准时没有在结论里给出 --lot-pattern："
                         "使用者会以为批号列被忽略了")
    return _r(True, "", {"n_checks": 7})


def rule_ingest_incremental():
    """多批次摄入必须"合并且去重"，而不是把文件一个个分开算。

    这条要防的是"像做了、其实没做"：
      * 只合并不去重 → 重复点把移动极差压成 0，σ_within 腰斩、控制限收窄，
        本来正常的点被判超限（自检里有数值证明，不是一句"有影响"）；
      * 把不同工序的表也并进来 → 两道的参数当成同一批数据算，根因指向错的那道；
      * 键写错却照样去重 → 真实观测被删，那是丢数据，比重复严重得多；
      * 游标坏了当第一次跑 → "本次新增"变成全量，只有对着报告才会发现。
    判据都落在赋值/写入动作上，删掉哪一行都会被下面某个变异抓到。
    """
    p = _read(PIPELINE_FILE) or ""
    g = _read(INGEST_FILE) or ""
    if not p or not g:
        return _r(False, "读不到 lib/pipeline.py 或 lib/ingest.py")
    # 1) 两个导入分支都登记（只改一套会在另一条路径上 NameError）
    if p.count("import ingest as IG") < 2:
        return _r(False, "两个导入分支没有都登记 ingest")
    # 2) 只在**同构**表之间合并：不同工序的表并进来会串工艺
    if not re.search(r"_same = \[t for t in tables if t is _m0 or IG\.is_same_shape",
                     p):
        return _r(False, "合并前没有按同构筛表：会把不同工序的参数当成同一批数据算")
    # 3) 冲突策略必须真的透传下去（不传就等于写死一种，报告里的处置说明会撒谎）
    if "on_conflict=on_conflict" not in g:
        return _r(False, "去重函数没有接收冲突策略：报告里写的处置方式与实际不符")
    # 4) 键写错时宁可不做——这是"丢数据"与"有重复"之间的取舍，必须保守
    _m = g.find("        if miss:")
    if _m < 0 or "return list(range(len(rows)))" not in g[_m:_m + 420]:
        return _r(False, "去重键不存在时没有退回原样：按错的键去重会删掉真实观测")
    # 5) 坏游标必须出声（否则"本次只有 3 行新增"是假的）
    _c = p.find("            if _st is None and _had_cursor:")
    if _c < 0 or 'setdefault("warnings"' not in p[_c:_c + 400]:
        return _r(False, "游标读不出来时静默按全量处理：增量会悄悄失效，没人会注意")
    # 6) 报告里要写清"去重去的是重复导出"，并写明能力仍按全量算
    if "**数据摄入（多批次合并与去重）**" not in p:
        return _r(False, "报告里没有数据摄入小节：使用者看不出结论建立在哪几批数据上")
    if "仍按**全量合并后**的数据计算" not in p:
        return _r(False, "没有写明能力指数按全量算：读者可能以为 Cpk 是拿增量算的")
    return _r(True, "", {"n_checks": 6})


def rule_spec_version_effective():
    """历史数据必须按**当时生效**的那一版规格判定，不能拿今天的尺子量去年的货。

    判据落在赋值与写入动作上，不停在"有没有这个模块"：
      * 能力计算的数据必须来自主段自己的 values——配主段规格却喂全段数据，
        等于把别的版本时期的行也算进来，那把尺子在那些行上根本没生效过；
      * 登记读取失败 / 认不出时间列 / 版本之间有空档，三种情况都必须进 warnings。
        规格一旦"按错的版"静默算完，Cpk → 过程能力 → 根因排序整条链跟着错，
        而使用人手里那份报告看起来完全正常。
    """
    p = _read(PIPELINE_FILE) or ""
    if not p:
        return _r(False, "读不到 lib/pipeline.py")
    # 1) 两个导入分支都登记（只改一套会在另一条调用路径上 NameError）
    if p.count("import specver as SV") < 2:
        return _r(False, "两个导入分支没有都登记 specver："
                         "只改一套会在另一条调用路径上 NameError")
    # 2) 规格表不是直接当扁平 dict 用，而是先建版本注册表
    if not re.search(r"_spec_reg = SV\.load\(", p):
        return _r(False, "未把规格登记读成版本注册表：多版本的生效期无从判断，"
                         "只能退回一份规格通吃")
    # 3) 配不出来必须出声：locate except 块，三样登记缺一不可
    _i = p.find("            _spec_reg = SV.load(spec_overrides, col_map=col_map)")
    _j = p.find("        if _spec_reg is not None", _i)
    if _i < 0 or _j < 0:
        return _r(False, "找不到规格注册表的读取与容错结构")
    _blk = p[_i:_j]
    if 'degrade.note("pipeline.specver@load"' not in _blk \
            or 'result.setdefault("warnings", []).append(' not in _blk \
            or 'result.setdefault("degraded_sections", []).append(' not in _blk:
        return _r(False, "规格版本登记读取失败时没有同时进降级台账与告警："
                         "使用者会以为已按当时生效版本重判")
    # 4) 能力计算必须吃该段自己的数据（不是全段配主段规格）
    if '_cap_vals = _main_seg["values"]' not in p \
            or "SPC.process_capability(_cap_vals," not in p:
        return _r(False, "能力计算用的是全段数据：别的规格版本时期的行被算了进来，"
                         "这把尺子在那些行上没生效过")
    # 5) 版本之间的空档 / 重叠必须体检并出声（否则那段数据只是"没算 Cpk"）
    for _k in ('for _g in (_aud.get("gaps") or []):',
               'for _o in (_aud.get("overlaps") or []):'):
        _b = p.find(_k)
        if _b < 0 or 'setdefault("warnings"' not in p[_b:_b + 220]:
            return _r(False, "版本登记体检（%s）结果未进告警："
                             "空档/重叠的锅会静默落到数据头上" % _k[:24])
    # 6) 认不出时间列必须出声，且不许回头替使用者选一版
    _t = p.find("            if not _time_col:")
    if _t < 0 or 'setdefault("warnings"' not in p[_t:_t + 400]:
        return _r(False, "登记了多版却定位不了时间列时没有出声："
                         "静默按单一规格判，正是要防的那件事")
    # 7) 分段明细要真出现在报告里（算出来不给使用者＝白算）
    if 'for sg in (c.get("spec_segments") or []):' not in p:
        return _r(False, "分段能力结果没有渲染进报告："
                         "使用者看不到换版前后各段各自的 Cpk")
    return _r(True, "", {"n_checks": 7})


def rule_msa_gate():
    """测量系统能力必须真能限制结论，而不是报告末尾的一句免责。

    判据落在**动作**上（会不会真的拦、拦的时候有没有漏别处），
    而不是"有没有这个模块"——后者只要函数名在就永远判过。
    """
    t = _read(MSA_FILE) or ""
    p = _read(PIPELINE_FILE) or ""
    if "def gauge_rr(" not in t or "def apply_msa_limit(" not in t:
        return _r(False, "缺 Gauge R&R 计算或结论限制入口（lib/msa.py）")
    if '"unacceptable": "block"' not in t.replace("'", '"'):
        return _r(False, "分级缺 block 动作：>"
                         "测量系统不合格时结论照样出去")
    if 'res["all_candidates"] = []' not in t or 'res["five_why"] = []' not in t:
        return _r(False, "block 时没把 all_candidates / five_why 一并清空："
                         "结论会从兜底路径与 5Why 里泄露出来")
    if "apply_msa_limit(" not in p or "_render_msa_gate(" not in p:
        return _r(False, "管线未调用 MSA 限制，或未渲染测量系统门槛")
    _i = p.find("_render_msa_gate(rep, s8")
    _j = p.find('rep.section("8. 根因')
    if _i < 0 or _j < 0 or _j > _i:
        return _r(False, "测量系统提示必须排在根因章节之前："
                         "写在报告末尾等于告诉读者『上面那些可以放心相信』")
    # 「没登记」不能被当成「尺子合格」：判据查的是**赋值动作**而不是字样，
    # 只搜 "msa_unverified" 的话，渲染端的读取语句会让整串一直存在，
    # 把赋值删掉照样判过——读取点救不了写入点缺失。
    if not re.search(r'^\s*result\["msa_unverified"\] = ', p, flags=re.M) \
            or "def unverified_notice(" not in t:
        return _r(False, "未做 MSA 时缺少前置提示（写入）：没登记 ≠ 尺子合格")
    return _r(True, "", {"n_checks": 5})


def rule_honest_degradation():
    """信息不足时必须承认，并闭嘴——不得递上一份看起来专业的榜单。

    这条规则要防的是一种"看不出错"的错：分数算得出来、表排得整整齐齐、
    措辞像模像样，而背后没有任何一列数据支撑。外部评审抓到的 E07 就是原型
    （识别率 0% 却给出「浆料固含 0.24」）。它对信任的破坏比算错更大：
    算错会被复核发现，硬凑的结论会被直接拿去调机。
    """
    t = _read(ROOTCAUSE_FILE) or ""
    if not t:
        return _r(False, "读不到 lib/rootcause.py")
    if '"honest_degraded": True' not in t:
        return _r(False, "缺 honest_degraded：数据不足时没有任何降级出口")
    # 1) 降级分支必须真的清空榜单（两个字段都要清，all_candidates 会被下游兜底读取）
    _m = re.search(r'if not suf\.get\("n_data_backed"\)(.*?)"score_note"', t, re.S)
    if not _m:
        return _r(False, "找不到「零数据支撑」的降级分支：没有任何候选拿到支撑时仍会排序")
    _blk = _m.group(1)
    if '"candidates": []' not in _blk or '"all_candidates": []' not in _blk:
        return _r(False, "降级分支未清空 candidates 与 all_candidates："
                         "状态说没有证据，榜单却照出")
    if "what_to_provide" not in _blk:
        return _r(False, "降级时没告诉使用人该补什么：只说「数据不足」等于把人晾在那里")

    # 2) 有数据支撑的候选必须排在前，纯先验候选不得与它同榜竞争榜首。
    #    判据落在**排序键怎么算**上，不是"有没有这个函数"——
    #    函数留着、键换成 -score，逐字检查照样判过（本规则首版就踩了这条）。
    _rk = re.search(r"def _rank_key\(d\):(.*?)\n\s*cands\.sort", t, re.S)
    if not _rk:
        return _r(False, "缺排序分区：纯先验候选会顶到真因前面（E07 的成因）")
    if "0 if _backed else 1" not in _rk.group(1) or "ST.SUCCESS" not in _rk.group(1):
        return _r(False, "排序键未按「是否拿到数据支撑」分区：纯先验候选仍可与真因争榜首")

    # 3) 内部衍生列不得参与找列/分组/归因（三处都要用同一条判据）。
    #    同样要落到"前缀判定这个动作"，否则把 return 改成 False 也判过。
    q = _read(QG_FILE) or ""
    _ic = re.search(r"def is_internal_column\(name\):(.*?)\n\S", q, re.S)
    if not _ic or "INTERNAL_COL_PREFIX" not in _ic.group(1):
        return _r(False, "内部衍生列判据未真的判前缀：__判定_NG__ 会被当成真实参数")
    if "QG.is_internal_column(" not in t:
        return _r(False, "rootcause 找列时未排除内部衍生列")
    tp = _read(PIPELINE_FILE) or ""
    if tp.count("QG.is_internal_column(") < 2:
        return _r(False, "pipeline 的分组与数据驱动归因未排除内部衍生列")

    # 4) 消费端不得用 all_candidates 把降级后的榜单又兜回来
    v = _read(VIEWS_FILE) or ""
    if 'if rc.get("honest_degraded")' not in v:
        return _r(False, "views 仍用 candidates or all_candidates 兜底：降级后从这条路径泄露榜单")

    # 5) 回归断言必须真存在（只写在报告里的判据等于没有）
    e = _read(EVAL_FILE) or ""
    if "def assert_regressions(" not in e or "honest_degraded" not in e:
        return _r(False, "评测脚本缺 E07 回归断言：下次改回归因逻辑没人拦")
    return _r(True, "数据不足时诚实降级（清空榜单+说明该补什么），"
                    "纯先验候选不参与榜首竞争，内部衍生列在找列/分组/归因三处均被排除，"
                    "消费端无兜底泄露，评测有回归断言")


def rule_boundary_policy_evidence():
    """边界政策由受控规格证据推导，不是留给使用人拍的旋钮。

    这条规则要防的退化很隐蔽：**政策旋钮还在、也能转，但转的人不该是他**。
    「企业侧有受控规格就该收紧成 block」——这句话里需要人做的只有登记那份文件，
    政策是登记的推论。把推论交回给使用者，等于让非专业的人判断
    "我这算不算有受控规格"：他判断不了，于是要么一直不收紧（报告已出去），
    要么开局就全收紧（连探索都被拦）。
    """
    t = _read(PROFILE_FILE) or ""
    if not t:
        return _r(False, "读不到 lib/profile.py：受控规格推导不存在")
    if "def derive_boundary_policy(" not in t:
        return _r(False, "缺 derive_boundary_policy：边界政策没有推导入口，只能靠人指定")
    if "def collect_controlled_specs(" not in t:
        return _r(False, "缺 collect_controlled_specs：受控登记无处汇总")

    # 1) 证据结论必须真的进候选集参与合并，不是算出来摆着看。
    #    判据落在"注入动作本身"，并且受"仅在能收紧时"这个条件约束——
    #    只查 _add 存在的话，把条件删成无条件注入也照样判过。
    if not re.search(r'if _pol == "block":\s*\n\s*_add\("boundary_policy", '
                     r'"受控规格证据", _pol\)', t):
        return _r(False, "证据结论未注入候选（或注入不受条件约束）："
                         "算出来不参与合并，等于没算；无条件注入则让「没登记」"
                         "也变成一项配置，工具凭空替人选了政策")

    # 2) 受控判定必须防伪造：只把 status 改成 active 却说不出文件编号的不算受控。
    #    否则"我说是受控的就是受控的"，正是这条规则本来要防的事。
    _m = re.search(r"def _spec_usable\(spec\):(.*?)\n\S", t, re.S)
    if not _m or 'get("doc_id")' not in _m.group(1) \
            or 'get("status")' not in _m.group(1):
        return _r(False, "受控判定缺 doc_id 校验：把状态改成 active 就能冒充受控")

    # 3) 产品档模板必须留出受控规格登记位：无处登记就只好回命令行拍板
    if '"controlled_specs": [{' not in t:
        return _r(False, "产品档模板无受控规格登记段：企业给了受控文件也无处登记")

    # 4) 能力可达：pipeline 要读登记，CLI 要能指到企业自己的受控清单
    tp = _read(PIPELINE_FILE) or ""
    if "PF.load_controlled_spec(" not in tp:
        return _r(False, "pipeline 未调用 load_controlled_spec：受控登记是库里的死代码")
    if not re.search(r'^\s*ap\.add_argument\("--controlled-spec"', tp, re.M):
        return _r(False, "CLI 未暴露 --controlled-spec：企业已有一份受控清单也指不过去")
    return _r(True, "边界政策由受控规格证据推导：登记受控生效文件即收紧 block、"
                    "未登记则 warn、无编号/过期的登记被点名，且使用人可指到自家受控清单")


AUDIT_RULES = [
    Rule("LICENSE 与代码事实对齐：Elastic 2.0 正文齐全且无许可密钥条款", rule_license,
         "（1）首行必须是裸文本 'Elastic License 2.0'（带 # 判不过）；"
         "（2）正文八个关键条款（Acceptance/Copyright License/Limitations/Patents/"
         "Notices/Termination/No Liability/Definitions）缺一即判不过——防止整段被掏空；"
         "（3）不得出现 'license key functionality'（代码无授权校验，留条款即条款与事实不符）；"
         "（4）有删减则必须有 Modification notice 写明删了什么、为什么",
         "LICENSE",
         [_mutant("首行加 Markdown 标题标记",
                  lambda t: ("## " + t) if t.startswith("Elastic") else None),
          _mutant("Patents 条款正文被掏空、标题还留着（查标题的规则会判过）",
                  _drop_license_section("Patents", "Notices")),
          _mutant("把许可密钥条款加回去（条款描述的又是不存在的东西）",
                  _sub("You may not alter, remove, or obscure any licensing",
                       "You may not move, change, disable, or circumvent the license key "
                       "functionality in the software.\n\n"
                       "You may not alter, remove, or obscure any licensing")),
          _mutant("删了条款却不留修改说明（偷偷改许可）",
                  _re_sub(r'## Modification notice\n\n[\s\S]*$', "")),
          _mutant("Termination 条款正文被掏空、标题还留着",
                  _drop_license_section("Termination", "No Liability"))]),

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
    Rule("数据契约必须真的执行：版本不兼容要阻断、未知列要点名、越界值要报",
         rule_contract_enforced,
         "P1-4。契约的分工是：compatible 按主版本判定不兼容并 block；"
         "check_result 给有输出却没 status 的环节逐条记账；"
         "check_values 真做上下限比较并把结果汇进 violations；"
         "且以上三条都必须出现在 lib/pipeline.py 的调用链里。"
         "为什么这四条缺一不可：只写模块不接 pipeline，等于功能没做——"
         "本项目已经因为『同一份数据两套键名』出过一次报告渲染 KeyError，"
         "而它是在给客户出报告时才炸的，不是跑不出数。"
         "另外，空 list 必须跳过而不是记违规，否则会诱发到处补假 status。",
         CONTRACT_FILE,
         [_mutant("版本门禁失效：不再比较主版本号",
                  _re_sub(r"^[ \t]+if a\[0\] != b\[0\]:",
                          "    if False:",
                          flags=re.M), CONTRACT_FILE),
          _mutant("不再给缺 status 的环节记账",
                  _re_sub(r"^[ \t]+out\[\"missing_status\"\]\.append\(n\)",
                          "    n = n",
                          flags=re.M), CONTRACT_FILE),
          _mutant("取值体检不再比较下限（哨兵值漏检）",
                  _re_sub(r"^[ \t]+if nums and lo is not None:",
                          "    if False:",
                          flags=re.M), CONTRACT_FILE),
          _mutant("契约写好了但没接进流水线",
                  _re_sub(r"^[ \t]+_ci = CT\.check_input\(work",
                          "        _ci = None  # 变异：契约未接入\n        if _ci is None: _ci = {}",
                          flags=re.M), PIPELINE_FILE)]),
    Rule("8D 能不能结案必须由证据判出来，且日期要真解析",
         rule_d8_closure_gate,
         "P1-9。closure_check 必须产出与 blockers 挂钩的 can_close；"
         "每个 gate 绑定 evidence_required 并记下缺哪几项；"
         "日期用 _parse_date 真解析，解析不出来按阻断处理；"
         "渲染时必须把录入卡传给 derive_d8。"
         "为什么这四条缺一不可：改造前 derive_d8({}) 的 blockers 是空的、"
         "也没有 can_close——零证据喂进去照样吐出一份完整清单，"
         "机器从头到尾没说过能不能结案。"
         "另外，用 str 比日期是本轮实测出来的坑：Python 的 str 比较对任何输入都成立，"
         "『去年冬天』不会抛异常，只会按字典序给出假答案从而静默放行。",
         EIGHTD_FILE,
         [          _mutant("结案判定与 blockers 脱钩（永远可结案）",
                  _re_sub(r'"can_close": \(not blockers\),',
                          '"can_close": True,',
                          flags=re.M), EIGHTD_FILE),
          _mutant("日期解析不出来也放行",
                  _re_sub(r"^[ \t]+if _vd_d is None or _end_d is None:",
                          "    if False:",
                          flags=re.M), EIGHTD_FILE),
          _mutant("退回字符串比日期（非法写法会被静默判通过）",
                  _re_sub(r"^[ \t]+elif _vd_d < _end_d:",
                          "    elif str(_vd) < str(_end):",
                          flags=re.M), EIGHTD_FILE),
          _mutant("渲染不把录入卡传给门禁（证据永远看不到）",
                  _re_sub(r"^[ \t]+d8 = derive_d8\(res, lead, card=card\)",
                          "    d8 = derive_d8(res, lead)",
                          flags=re.M), EIGHTD_FILE)]),

    Rule("根因可验证性权重必须真正生效，不得硬编码 0.5",
         rule_p1_verifiability_weighted,
         "P1-5。根因评分的 0.10*可验证性 原先里 verifiability 恒等于 0.5："
         "既没算进排序分，也不随候选输出，这一项权重从没生效过。"
         "必须：① 有按候选动态计算可验证性的函数；② 不再写死 0.5；"
         "③ 总分含 0.10*_vf；④ 候选输出 verifiability_score 且返回体注明权重是启发式非概率。",
         ROOTCAUSE_FILE,
         [_mutant("可验证性计算函数改名（消掉定义）",
                  _re_sub(r"^def _verifiability\(",
                          "def _verifiability2(", flags=re.M), ROOTCAUSE_FILE),
          _mutant("总分写死 0.10*0.5（权重不再参评）",
                  _re_sub(r"0\.40 \* _ev \+ 0\.35 \* _pr \+ 0\.15 \* _ts \+ 0\.10 \* _vf",
                          "0.40 * _ev + 0.35 * _pr + 0.15 * _ts + 0.10 * 0.5",
                          flags=re.M), ROOTCAUSE_FILE),
          _mutant("候选不再输出可验证性分项",
                  _re_sub(r'c\["verifiability_score"\] = round\(_vf, 3\)',
                          "pass  # 不再输出分项", flags=re.M), ROOTCAUSE_FILE)]),

    Rule("字段映射必须结构化且低置信可阻断", rule_p1_field_mapping_confidence,
         "P1-3。map_columns 必须升级为 map_columns_detailed：每条记录带 source/confidence/"
         "candidates；apply_map_policy 必须支持 block 把低置信列判未匹配；"
         "unit_risk 必须显式检测跨量纲误映射；clean 报告必须交出 mapping_detail 与 "
         "low_confidence_mappings。裸 {列: key} 让『这一列配得有多准』彻底不可见。",
         PREPROCESS_FILE,
         [_mutant("结构化映射入口改名（消掉定义）",
                  _re_sub(r"^def map_columns_detailed\(",
                          "def map_columns_detailed2(", flags=re.M), PREPROCESS_FILE),
          _mutant("量纲风险检测被关掉（跨量纲误映射不再可见）",
                  _re_sub(r'return not \(col_hints & decl_hints\)',
                          'return False', flags=re.M), PREPROCESS_FILE),
          _mutant("去掉 block 政策分支（低置信无法阻断）",
                  _re_sub(r'if policy == "block" and conf == "low":',
                          'if False and conf == "low":', flags=re.M), PREPROCESS_FILE)]),
    Rule("SPC 剔除必须可审计：可切换仅建议/审核、逐点带规则身份与审核人",
         rule_p1_spc_exclusion_review,
         "P1-2。phase1_limits 必须支持 mode=auto/suggest/review 与 review 入参；"
         "auto 默认仍剔除，suggest/review 不自动剔除、把候选点交人审；"
         "每条被标记点必须带命中 Nelson 规则号与可读理由（exclusion_detail/suspect_points），"
         "审核点必须带 review_status/reviewed_by 且 review 真正被使用；"
         "pipeline.analyze 必须把 spc_exclusion_mode 透传给 phase1_limits。",
         SPCPy,
         [_mutant("剔除模式开关被焊死成 auto（suggest/review 不可达）",
                  _re_sub(r'mode = \(mode or "auto"\)\.lower\(\)',
                          'mode = "auto"', flags=re.M), SPCPy),
          _mutant("逐点身份载体被改名抹掉（exclusion_detail 不落地）",
                  _re_sub(r'"exclusion_detail": exclusion_detail,',
                          '"excluded_points": exclusion_detail,', flags=re.M), SPCPy),
          _mutant("review 不被实际使用（一律按 approved 剔除）",
                  _re_sub(r'isinstance\(rev, dict\) and rev\.get\("status"\) == "approved"',
                          'True', flags=re.M), SPCPy)]),
    Rule("DOE 方案必须结构化：随机化顺序 / 重复 / 区组 / 别名结构 / 样本量功效",
         rule_p1_doe_structure,
         "P1-7。design_structure 必须真的建立随机化发生器并洗牌（run_sheet 可复现），"
         "alias_structure 必须调用 _two_level_cols 并按位异或判混杂、输出 resolution/main_aliases，"
         "power_analysis 必须按 (z_a+z_b)*se 计算可检出效应并输出 detectable_effect_sigma，"
         "残差自由度归零必须告警，重复与区组必须进结构化输出；"
         "pipeline.analyze 必须调用 design_structure 并接入 doe_replicates/doe_seed。",
         DOE_FILE,
         [_mutant("随机化退化成顺序执行（未知干扰会冒充因子效应）",
                  _re_sub(r"rng\.shuffle\(order\)", "order = list(order)",
                          flags=re.M), DOE_FILE),
          _mutant("别名结构恒判不适用（混杂关系消失）",
                  _re_sub(r"got = _two_level_cols\(matrix\)", "got = None",
                          flags=re.M), DOE_FILE),
          _mutant("可检出效应写死为 1.0（功效数字变成装饰）",
                  _re_sub(r"d_detect = \(z_a \+ z_b\) \* se", "d_detect = 1.0",
                          flags=re.M), DOE_FILE)]),
    Rule("知识必须有生命周期 schema、状态机与出处覆盖率审计",
         rule_p2_knowledge_lifecycle,
         "P2-2。kbstore 必须定义 LIFECYCLE_FIELDS（适用工序/设备/材料/前提/样本量/"
         "证据等级/验证时间/失效条件/所有者/状态）与非空的 LIFECYCLE_MINIMUM；"
         "状态机必须允许 draft→active、禁止 active→draft、deprecated 为终态，"
         "并有 check_transition 真正执行；audit_lifecycle 必须以 LIFECYCLE_TARGETS "
         "为默认范围并输出 source_coverage；lifecycle_issues 必须存在且 cases 段"
         "排除生命周期词表（避免 A/B/C 被误判）；编排器必须实现并分发 kb 流程。",
         KBSTORE_FILE,
         [_mutant("状态机放行 active→draft（可悄悄绕过复核）",
                  _re_sub(r'"active": \("deprecated",\),',
                          '"active": ("deprecated", "draft"),', flags=re.M),
                  KBSTORE_FILE),
          _mutant("审计范围被掏空（覆盖率永远 0 条，等于没审计）",
                  _re_sub(r"targets = targets or LIFECYCLE_TARGETS",
                          "targets = []", flags=re.M), KBSTORE_FILE),
          _mutant("最小必填集清空（覆盖率变成 100% 的假象）",
                  _re_sub(r'LIFECYCLE_MINIMUM = \("evidence_level", "verified_at", '
                          r'"invalidates_if", "status"\)',
                          "LIFECYCLE_MINIMUM = ()", flags=re.M), KBSTORE_FILE)]),
    Rule("必须有结构化日志与可观测性：耗时/重试/错误分类/模块成功率/资源上限/脱敏",
         rule_p2_observability,
         "P2-4。lib/observ.py 必须提供 event/mask/classify_error/Timer/retry/"
         "check_resources/record_input_quality/summary/read_log/summarize_log/reset；"
         "mask 必须真的遍历 _MASK_RULES 且规则含手机号与用户目录；"
         "classify_error 必须真的遍历 _CLASSIFY_RULES；失败必须计入模块统计分母；"
         "必须暴露 self_errors（诊断通道自身带病要可见）；需有 RESOURCE_LIMITS；"
         "编排器必须实现并分发 ops 流程，degrade 必须转发到 observ，"
         "pipeline 必须产出 observability 汇总。",
         OBS_FILE,
         [_mutant("脱敏规则被清空（日志原样落盘，带走手机号与用户名）",
                  _re_sub(r"^    for pat, rep in _MASK_RULES:",
                          "    for pat, rep in ():", flags=re.M), OBS_FILE),
          _mutant("错误分类退化为恒 unknown（该找谁说不清）",
                  _re_sub(r"^(\s+)for cls, types in _CLASSIFY_RULES:",
                          r"\1for cls, types in ():", flags=re.M), OBS_FILE),
          _mutant("失败不进统计（模块成功率恒为 1.0 的假象）",
                  _re_sub(r'^\s+st\["ok" if ok else "fail"\]\s*\+=\s*1',
                          '            st["ok"] += 1', flags=re.M), OBS_FILE)]),
    Rule("推断边界必须有政策旋钮：默认 warn，但 block 时真拦得住",
         rule_boundary_policy_knob,
         "台账遗留项。boundary_gate 必须定义 BOUNDARY_POLICIES（含 warn 与 block）、"
         "真正接收传入的 policy、拒绝非法值（不静默回落）、"
         "在 block 政策下把「边界无受控来源」的警告并入 blockers 并输出 escalated；"
         "pipeline 必须传 policy=boundary_policy 且 CLI 必须暴露 --boundary-policy。",
         DOE_FILE,
         [_mutant("block 分支失效（收紧了却拦不住）",
                  _re_sub(r'if policy == "block" and warns:',
                          "if False and warns:", flags=re.M), DOE_FILE),
          _mutant("非法政策值静默回落（以为收紧了其实没收紧）",
                  _re_sub(r"    if policy not in BOUNDARY_POLICIES:",
                          "    if False:", flags=re.M), DOE_FILE),
          _mutant("旋钮被焊死成 warn（传什么都当 warn 处理）",
                  _re_sub(r'    policy = \(policy or "warn"\)',
                          '    policy = "warn"', flags=re.M), DOE_FILE)]),
    Rule("多产品配置按语义合并，冲突交人决断且显式优先",
         rule_product_profile_merge,
         "resolve_config 按参数语义（rank/max/set_union/manual）合并；"
         "manual 返回空值并进 needs_decision 而非取第一个；"
         "explicit 分支先于角色与产品写入 config；角色每条带 reason；"
         "cross_matrix 有规模上限；pipeline 调用 resolve_config 且 CLI 可达 --role/--products。",
         PROFILE_FILE,
         [_mutant("manual 不再交人（改取第一个，多产品又变谁先谁赢）",
                  _re_sub(r'return \(None, kind, "各来源不一致且该参数无法自动取：%s" % detail, True\)',
                          'return (uniq[0][1], kind, "取第一个", False)',
                          flags=re.M), PROFILE_FILE),
          _mutant("显式优先失效（角色/产品可覆盖使用人写死的值）",
                  _re_sub(r'if param in explicit:', 'if False:',
                          flags=re.M), PROFILE_FILE),
          _mutant("取严退化成取最后一个（顺序又决定结果）",
                  _re_sub(r'best = max\(known, key=lambda x: rank\[x\[1\]\]\) if pick == "max"',
                          'best = known[-1] if pick == "max"',
                          flags=re.M), PROFILE_FILE)]),
    Rule("边界政策由受控规格证据推导（不是留给使用人拍的旋钮）",
         rule_boundary_policy_evidence,
         "derive_boundary_policy 依据登记推导政策；证据结论在 _pol==block 时"
         "才注入候选参与合并；_spec_usable 同时校验 status 与 doc_id（防伪造受控）；"
         "产品档模板含 controlled_specs 登记段；pipeline 调用 load_controlled_spec "
         "且 CLI 暴露 --controlled-spec。",
         PROFILE_FILE,
         [_mutant("有受控也不收紧（证据算出来不参与合并）",
                  _re_sub(r'if _pol == "block":', "if False:",
                          flags=re.M), PROFILE_FILE),
          _mutant("无受控也收紧（未登记凭空变成一项配置）",
                  _re_sub(r'if _pol == "block":', "if _pol:",
                          flags=re.M), PROFILE_FILE),
          _mutant("受控判定不校验文件编号（改个状态就能冒充受控）",
                  _re_sub(r'if not str\(spec\.get\("doc_id"\) or ""\)\.strip\(\):\n'
                          r'\s*return False', "if False:\n        return False",
                          flags=re.M), PROFILE_FILE),
          _mutant("产品档模板无受控登记段（企业给了文件也无处登记）",
                  _re_sub(r'"controlled_specs": \[\{', '"specs": [{',
                          flags=re.M), PROFILE_FILE)]),
    Rule("信息不足时必须诚实降级，不得硬凑根因",
         rule_honest_degradation,
         "零数据支撑时清空 candidates 与 all_candidates 并给出 what_to_provide；"
         "有数据支撑的候选用 _rank_key 分区排在前；"
         "内部衍生列（__ 前缀）在 rootcause 找列、pipeline 分组与数据驱动归因三处都被排除；"
         "views 不得用 all_candidates 兜底；评测脚本有 E07 回归断言。",
         ROOTCAUSE_FILE,
         [_mutant("取消失信降级（零支撑时照样排序）",
                  _re_sub(r'if not suf\.get\("n_data_backed"\)',
                          "if False", flags=re.M), ROOTCAUSE_FILE),
          _mutant("降级了但仍交出榜单（candidates 不清空）",
                  _re_sub(r'"candidates": \[\],\n\s*"all_candidates": \[\],',
                          '"candidates": cands,\n            "all_candidates": cands,',
                          flags=re.M), ROOTCAUSE_FILE),
          _mutant("取消排序分区（纯先验候选可顶到真因前）",
                  _re_sub(r"return \(0 if _backed else 1, -d\[\"score\"\]\)",
                          "return -d[\"score\"]", flags=re.M), ROOTCAUSE_FILE),
          _mutant("内部衍生列不再被排除（__判定_NG__ 可当原因）",
                  _re_sub(r"return str\(name or \"\"\)\.startswith\(INTERNAL_COL_PREFIX\)",
                          "return False", flags=re.M), QG_FILE)]),

    Rule("测量系统能力不合格时必须真的拦住结论（不是末尾一句免责）", rule_msa_gate,
         "（1）lib/msa.py 有 gauge_rr 与 apply_msa_limit；"
         "（2）分级含 unacceptable→block 动作；"
         "（3）block 时 all_candidates 与 five_why 一并清空（只清 candidates 不够，"
         "结论会从兜底路径与 5Why 泄露）；"
         "（4）管线调用 apply_msa_limit 且 _render_msa_gate 排在根因 section 之前；"
         "（5）未登记 MSA 时有 unverified 前置提示",
         MSA_FILE,
         [_mutant("取消 block（测量系统不合格照样出结论）",
                  _re_sub(r'\s*"unacceptable": "block"',
                          '\n    "unacceptable": "warn"', flags=re.M), MSA_FILE),
          _mutant("清空候选但放过 all_candidates（结论从兜底路径回来）",
                  _sub('res["all_candidates"] = []',
                       'res["due_review"] = True  # 变异：all_candidates 未清空'),
                  MSA_FILE),
          _mutant("5Why 不封存（『指向某参数』换个地方把结论又说一遍）",
                  _re_sub(r'\s*if res\.get\("five_why"\):\n\s*res\["five_why"\] = \[\]'
                          r'\n\s*res\["five_why_note"\] = \([^)]*\)',
                          "", flags=re.M), MSA_FILE),
          _mutant("把测量系统门槛的渲染删掉（写在哪＝算在哪）",
                  lambda t: t.replace("    _render_msa_gate(rep, s8, res)\n", "")
                  if "_render_msa_gate(rep, s8, res)" in t else None,
                  PIPELINE_FILE),
          _mutant("未登记测量系统时沉默（把没登记当成尺子合格）",
                  _re_sub(r'\s*result\["msa_unverified"\] = MSA\.unverified_notice\(\)',
                          "", flags=re.M), PIPELINE_FILE)]),

    Rule("自相关过程必须换图，并说明这张图的能力边界", rule_autocorrelation_chart,
         "（1）lib/spc.py 有 autocorrelation_diagnosis 与 ewma_stationary；"
         "（2）CUSUM 决策区间按自相关放大（hh = h*sigma*sqrt(F)，"
         "不放大就是把序列惯性当趋势报）；"
         "（3）自相关显著时管线改用 EWMAST 而非普通 EWMA"
         "（普通 EWMA 按 F≈5.25 放大后连 3σ 阶跃都常落在限内）；"
         "（4）结果里给出最小可检出偏移（防止把'没报警'读成'过程很稳'）；"
         "（5）SPC 按列角色过滤，序号/行号列不得参与（必然自相关）",
         SPCPy,
         [_mutant("CUSUM 不按自相关放大（惯性被当趋势）",
                  _re_sub(r"hh = h \* sigma \* math\.sqrt\(_f\)",
                          "hh = h * sigma", flags=re.M), SPCPy),
          _mutant("自相关时退回普通 EWMA（限被放大到抓不住东西）",
                  _sub("SPC.ewma_stationary(vals)", "SPC.ewma(vals, mu=_mu0, sigma=_sig_long)"),
                  PIPELINE_FILE),
          _mutant("不再说明最小可检出偏移（没报警＝很稳的错觉）",
                  _re_sub(r'\s*if _h3 is not None:\n\s*_acd\["min_detectable_shift"\] = _h3',
                          "", flags=re.M), PIPELINE_FILE),
          _mutant("SPC 不再按列角色过滤（序号列必然自相关，会刷屏）",
                  lambda t: t.replace(
                      '            if QG.column_role(c, work.numeric_column(c) '
                      'if c in work.columns else None) \\\n'
                      '                    not in ("measure", "count"):\n'
                      '                continue\n', ""),
                  PIPELINE_FILE)]),
    Rule("原生 Word/PDF 导出必须是真文件，且导出失败必须出声", rule_export_native,
         "（1）lib/export_doc.py 有 render/write 的 docx 与 pdf 四入口；"
         "（2）docx 写 [Content_Types].xml 与 word/media/ 图片部件（不写图＝丢证据）；"
         "（3）PDF 写 %PDF-1.4 头与 startxref，且 xref 偏移来自 offsets[i]"
         "（写死偏移＝生成得出来、谁都打不开）；"
         "（4）PDF 挂 STSong-Light + UniGB-UCS2-H（中文不成乱码）；"
         "（5）复杂 SVG 不硬猜、原位标注『图见同名 SVG』；"
         "（6）pipeline 两个导入分支都登记 export_doc（只改一套会 NameError）；"
         "（7）docx/pdf 导出失败都同时进 warnings 与 degraded_sections",
         EXPORT_FILE,
         [_mutant("docx 不写 Content_Types（只是换后缀的文件）",
                  _sub('    z.writestr("[Content_Types].xml", "".join(ct))\n', ""),
                  EXPORT_FILE),
          _mutant("docx 丢掉图片部件（Word 版只剩文字）",
                  _sub('            z.writestr("word/media/" + n, d)\n',
                       "            pass\n"),
                  EXPORT_FILE),
          _mutant("PDF 的 xref 偏移写死（文件生成得出来、打不开）",
                  _sub('out.write(("%010d 00000 n \\n" % offsets[i]).encode("latin-1"))',
                       'out.write(("0000000000 00000 n \\n").encode("latin-1"))'),
                  EXPORT_FILE),
          _mutant("导出失败静默吞掉（少了交付形态却没人知道）",
                  _re_sub(r'^\s*result\.setdefault\("warnings", \[\]\)\.append\('
                          r'\n\s*"Word\(\.docx\) 导出失败.*?\n'
                          r'\s*result\.setdefault\("degraded_sections", \[\]\)\.append\('
                          r'.*?\n', "            pass\n",
                          flags=re.M | re.S), PIPELINE_FILE),
          _mutant("两个导入分支只留一个（另一条路径 NameError）",
                  _sub("        from . import export_doc as EX\n", ""),
                  PIPELINE_FILE)]),
    Rule("历史数据按当时生效的规格重判，认不出/有空档必须出声",
         rule_spec_version_effective,
         "（1）pipeline 两个导入分支都登记 specver；"
         "（2）规格登记读成版本注册表 _spec_reg = SV.load(...)；"
         "（3）读取失败同时进降级台账/warnings/degraded_sections；"
         "（4）能力计算吃主段自己的 values，不是全段配主段规格；"
         "（5）空档与重叠体检结果进告警；"
         "（6）认不出时间列时出声而非静默按单一规格判；"
         "（7）分段 Cpk 明细真的渲染进报告",
         PIPELINE_FILE,
         [_mutant("不建版本注册表（退回一份规格通吃全部历史数据）",
                  _sub("            _spec_reg = SV.load(spec_overrides, col_map=col_map)\n",
                       "            _spec_reg = None\n"),
                  PIPELINE_FILE),
          _mutant("能力计算用全段数据配主段规格（跨版本的行被算进来）",
                  _sub('                _cap_vals = _main_seg["values"]\n',
                       "                _cap_vals = vals\n"),
                  PIPELINE_FILE),
          _mutant("规格版本登记读取失败静默吞掉（使用者以为已按版本重判）",
                  _sub('            degrade.note("pipeline.specver@load", _sv_e)\n'
                       '            result.setdefault("warnings", []).append(\n'
                       '                "规格版本登记读取失败（%s），本次按单一规格判定，'
                       '未按当时生效版本重判" % _sv_e)\n'
                       '            result.setdefault("degraded_sections", []).append(\n'
                       '                {"section": "规格版本化", "error": str(_sv_e)})\n',
                       "            _spec_reg = None\n"),
                  PIPELINE_FILE),
          _mutant("版本空档体检结果不上报（那段只是『没算 Cpk』看不出原因）",
                  _sub('            for _g in (_aud.get("gaps") or []):\n'
                       '                result.setdefault("warnings", []).append(\n'
                       '                    "规格版本空档：%s" % _g.get("issue"))\n',
                       "            pass\n"),
                  PIPELINE_FILE),
          _mutant("两个导入分支只留一个（另一条路径 NameError）",
                  _sub("        from . import specver as SV\n", ""),
                  PIPELINE_FILE)]),
    Rule("多批次数据必须合并去重，键写错宁可不去重，游标坏了要出声",
         rule_ingest_incremental,
         "（1）pipeline 两个导入分支都登记 ingest；"
         "（2）只把同构表合并（不同工序的表并进来会串工艺）；"
         "（3）冲突策略真的透传给去重函数（否则报告里的处置说明与实际不符）；"
         "（4）去重键不存在时退回原样并告警（按错的键去重＝删掉真实观测）；"
         "（5）游标读不出来时出声，不静默按全量；"
         "（6）报告有数据摄入小节，且写明能力仍按全量算",
         PIPELINE_FILE,
         [_mutant("两个导入分支只留一个（另一条路径 NameError）",
                  _sub("        from . import ingest as IG\n", ""),
                  PIPELINE_FILE),
          _mutant("合并不筛同构表（不同工序的参数混成一批数据）",
                  _sub("        _same = [t for t in tables if t is _m0 or IG.is_same_shape(_m0, t)]\n",
                       "        _same = list(tables)\n"),
                  PIPELINE_FILE),
          _mutant("去重键写错也照样去重（真实观测被删）",
                  _sub("        if miss:\n"
                       "            return list(range(len(rows))), {\n",
                       "        if False:\n"
                       "            return list(range(len(rows))), {\n"),
                  INGEST_FILE),
          _mutant("坏游标静默按第一次运行处理（增量悄悄失效）",
                  _re_sub(r'^[ \t]*if _st is None and _had_cursor:[ \t]*\n'
                          r'(?:[ \t]*#[^\n]*\n)*'
                          r'[ \t]*result\.setdefault\("warnings", \[\]\)\.append\(\n'
                          r'[^\n]*\n[^\n]*\n', "", flags=re.M),
                  PIPELINE_FILE),
          _mutant("报告不写清能力按全量算（读者以为 Cpk 是拿增量算的）",
                  _sub('"本次的 SPC 与 Cpk 仍按**全量合并后**的数据计算——"',
                       '"本次的 SPC 与 Cpk 按本次数据计算——"'),
                  PIPELINE_FILE)]),
    Rule("批号里提取的分组维度不能是流水号，派生列不能污染数值分析",
         rule_lot_segment,
         "（1）pipeline 两个导入分支都登记 lotcode；"
         "（2）序号识别真的参与段分类（_is_sequence → sequence）；"
         "（3）序号段的高 η² 标注混杂风险，不当显著依据；"
         "（4）派生列从 SPC 与相关性里排除（日期编码不是测量值）；"
         "（5）派生列不算「未识别列」（否则报告自相矛盾）；"
         "（6）拆不准时提示 --lot-pattern，不静默略过",
         LOT_FILE,
         [_mutant("两个导入分支只留一个（另一条路径 NameError）",
                  _sub("        from . import lotcode as LC\n", ""),
                  PIPELINE_FILE),
          _mutant("序号识别不参与判定（流水号被当成可分组维度）",
                  _sub("    elif _is_sequence(seg):\n"
                       "        # 序号段：不连续成块 + 数字部分随行号递增。按它分组＝问\n"
                       "        # \"第 7 个和第 12 个有没有区别\"，对追根因毫无用处。\n"
                       "        kind = \"sequence\"\n",
                       "    elif False:\n"
                       "        kind = \"sequence\"\n"),
                  LOT_FILE),
          _mutant("序号段的高 η² 不标注混杂风险（被当成显著分组依据）",
                  _sub('                st["eta2_note"] = ', '                pass  # '),
                  LOT_FILE),
          _mutant("派生列不排除出 SPC（日期编码被当测量值画控制图）",
                  _sub("                   if c not in _lot_d\n",
                       "                   if True\n"),
                  PIPELINE_FILE),
          _mutant("派生列不排除出相关性（日期编码与工艺参数算出假相关）",
                  _sub("if c != tcol and c not in _lot_dd",
                       "if c != tcol"),
                  PIPELINE_FILE),
          _mutant("拆不准时静默略过（使用者以为批号列被忽略）",
                  _re_sub(r"请用 --lot-pattern 给出正则", "该列无法拆解"),
                  LOT_FILE)]),
    Rule("无效段必须排除，但排除不许伪造相邻关系",
         rule_exclude_invalid_segments,
         "（1）pipeline 两个导入分支都登记 exclude；"
         "（2）排除发生在所有统计之前（放在最后等于只给报告加一段话）；"
         "（3）删行后重算时间列，且同步裁第二份时间变量（否则索引错位）；"
         "（4）SPC 只在最长连续块内做（跨接缝差分是伪造的波动）；"
         "（5）接缝 hours两侧都保留也必须断开（blocks 收 seams 参数）；"
         "（6）报告写明剔了什么、给回原始行号；"
         "（7）剔除过半时整体放弃（不敢把产品当噪声删了）",
         EXCLUDE_FILE,
         [_mutant("两个导入分支只留一个（另一条路径 NameError）",
                  _sub("        from . import exclude as XCL\n", ""),
                  PIPELINE_FILE),
          _mutant("删行后不重算时间列（每一行的时间都错位一格）",
                  _re_sub(r'^[ \t]*times = PP\.parse_time_column\(work\)\[1\][ \t]*\n',
                          "", flags=re.M),
                  PIPELINE_FILE),
          _mutant("SPC 不按连续块切片（跨接缝求差分，σ 抬高等价于放宽控制限）",
                  _sub("            vals = _slice(work.numeric_column(c))\n",
                       "            vals = work.numeric_column(c)\n"),
                  PIPELINE_FILE),
          _mutant("异常断流不参与切块（接缝两侧当成相邻观测）",
                  _sub("def blocks(mask, seams=()):", "def blocks(mask, seams=()):\n    seams = ()"),
                  EXCLUDE_FILE),
          # 变异目标必须是"安全线参与判定"的那一行，不是常量本身：
          # 起初我把 MIN_KEPT_RATIO 改成 0.0 当变异，判据却只检查"存在一个小数值"，
          # 于是改不改都判过 —— 变异没喊，等于证明不了这条规则有效。
          _mutant("剔除过半也照剔（把产品当噪声删掉）",
                  _sub("    if n_ex and (kept < MIN_KEPT_ROWS "
                       "or kept * 1.0 / n < MIN_KEPT_RATIO):\n",
                       "    if n_ex and kept < MIN_KEPT_ROWS:\n"),
                  EXCLUDE_FILE),
          _mutant("报告不写剔了什么（使用者不知道数字来自哪批数据）",
                  _sub('rep.text(s2, "**无效段排除（停机 / 换卷 / 首件 / 断流）**")',
                       "pass"),
                  PIPELINE_FILE)]),
    Rule("口径声明：单位/时区/班次判不了的必须显式声明，不许静默假设",
         rule_scope_declaration,
         "（1）pipeline 两个导入分支都登记 context；"
         "（2）口径声明发生在统计之前（时间列解析之后、缺陷识别之前）；"
         "（3）无时区时间戳显式声明 naive（不假装 UTC）；"
         "（4）带/不带时区混在一份数据里判 conflict（不是只看带 tz 行偏移一致）；"
         "（5）未确认的降级要进 downgrades（时区 naive / 班次未登记）；"
         "（6）报告正文出现「口径声明」段",
         CONTEXT_FILE,
         [_mutant("两个导入分支只留一个（另一条路径 NameError）",
                  _sub("        from . import context as CXF\n", ""),
                  PIPELINE_FILE),
          _mutant("无时区时间戳不当 naive 处理（假装有口径）",
                  _sub('return {"status": "naive", "declared": None, "naive": True',
                       'return {"status": "declared", "declared": "UTC", "naive": False'),
                  CONTEXT_FILE),
          _mutant("带/不带时区混着不判冲突（漏掉一半 UTC 一半本地的情况）",
                  _sub("    if has_tz and no_tz:\n"
                       "        return {\"status\": \"conflict\"",
                       "    if has_tz and no_tz and False:\n"
                       "        return {\"status\": \"conflict\""),
                  CONTEXT_FILE),
          _mutant("naive 不进 downgrades（时序类结论悄悄按错误口径算）",
                  _sub('    if tz["status"] in ("naive", "conflict"):',
                       '    if tz["status"] == "conflict":'),
                  CONTEXT_FILE),
          _mutant("报告没有口径声明段（读者不知道数字在什么口径下算的）",
                  _sub('rep.text(s2, "**口径声明（单位 / 时区 / 班次）**")',
                       "pass"),
                  PIPELINE_FILE)]),
    Rule("基线稳定性必须显式结论，不稳时判异要降级，且与判异结果分开呈现",
         rule_baseline_stability,
         "（1）SPC 结果带 baseline 摘要（stable/note 独立成字段，不是埋在 caveats）；"
         "（2）报告正文出现「基线稳定性」呈现；"
         "（3）不稳时呈现为告警并声明「判异结论降级」，不得与受控并列；"
         "（4）phase1_limits 对整体不受控数据返回 stable=False（不谎报稳定）",
         PIPELINE_FILE,
         [_mutant("SPC 结果不带 baseline 摘要（稳定性结论丢失）",
                  _sub('                "baseline": {',
                       '                "_baseline_gone": {'),
                  PIPELINE_FILE),
          _mutant("报告不呈现基线稳定性（读者不知道过程稳不稳）",
                  _sub('rep.text(s4, "**基线稳定性**：%s"',
                       'rep.text(s4, "**（基线稳定性已省略）**：%s"'),
                  PIPELINE_FILE),
          _mutant("不稳时不声明判异降级（稳定与判异混为一谈）",
                  _sub('下方判异结论因此降级，',
                       '下方判异结论正常，'),
                  PIPELINE_FILE),
          _mutant("整体不受控却谎报稳定（把异常撑宽的控制限当基线）",
                  _sub("        elif capped:\n"
                       "            # 关键：封顶中断时必须如实说\"不稳\"",
                       "        elif capped and False:\n"
                       "            # 关键：封顶中断时必须如实说\"不稳\""),
                  SPCPy)]),
    Rule("行动卡片必须结构化可导出，未知字段不编造、设定值不替人拍",
         rule_action_cards,
         "（1）pipeline 两个导入分支都登记 actioncard；"
         "（2）卡片含 6 个核心字段（待验证参数/建议取值/验证批次/成功判据/"
         "负责人角色/验证窗口）；"
         "（3）导出 JSON 带 schema+fields 契约（MES 对接靠它）、CSV 表头对齐；"
         "（4）导出失败出声，不静默（否则现场以为拿到了卡片）；"
         "（5）卡片只在 8D 产出后生成（8D 没产出就没有值得导出的对象）",
         ACTIONCARD_FILE,
         [_mutant("两个导入分支只留一个（另一条路径 NameError）",
                  _sub("        from . import actioncard as AC\n", ""),
                  PIPELINE_FILE),
          _mutant("卡片字段契约被删（MES 对接的键丢了一半）",
                  _sub('    "verification_batches",  # 验证批次建议（来自 MDD 表，样本量是产能决定的）\n',
                       ''),
                  ACTIONCARD_FILE),
          _mutant("导出 JSON 不带 schema/fields（下游拿不到字段含义）",
                  _sub('payload = {"schema": "action_card_v1", "fields": CARD_FIELDS,',
                       'payload = {"cards": [dict(c) for c in cards], "n": n}'),
                  ACTIONCARD_FILE),
          _mutant("导出失败被静默吞掉（现场以为拿到了卡片）",
                  _sub('            except Exception as _ace:\n'
                       '                res.setdefault("warnings", []).append(\n'
                       '                    "行动卡片导出失败：%s" % _ace)\n',
                       '            except Exception as _ace:\n'
                       '                pass\n'),
                  PIPELINE_FILE)]),
    Rule("工序判定失败必须显式降级，未知模式不做强因果，页脚记录模式",
         rule_process_fallback,
         "（1）判定失败不再是静默 None，而是一等状态 process_mode（recognized/"
         "manual/unknown/descriptive）；"
         "（2）有 --process-mode 让使用者显式选降级路径；"
         "（3）unknown/descriptive 模式根因不用工序知识库机理先验（强因果降级）；"
         "（4）报告页脚（17 节）声明工序判定模式",
         PIPELINE_FILE,
         [          _mutant("判定失败仍静默 None（不登记 process_mode）",
                  _sub('    result["process_mode"] = _proc_mode\n', ''),
                  PIPELINE_FILE),
          _mutant("没有 --process-mode 入口（使用者只能干瞪眼）",
                  _sub('    ap.add_argument("--process-mode", default=None,', ''),
                  PIPELINE_FILE),
          _mutant("unknown 模式仍用工序机理先验（强因果没降级）",
                  _sub('        if defect_name and not _no_process_prior:',
                       '        if defect_name:'),
                  PIPELINE_FILE),
          _mutant("报告页脚不声明模式（读者不知道结论是什么口径）",
                  _sub('        ("工序判定模式", {',
                       '        ("工序判定模式省略", {'),
                  PIPELINE_FILE)]),
    Rule("许可提示：报告页脚含许可自检，入口有许可摘要，不阻断内部自用",
         rule_license_notice,
         "（1）报告页脚（17 节）有许可自检提示（Elastic 2.0 + 内外使用边界）；"
         "（2）入口（fde_run.py）有许可摘要；"
         "（3）内部自用不被误阻断（提示是 info 不是阻断）；"
         "（4）对外服务场景被显式提醒（不是含糊带过）",
         PIPELINE_FILE,
         [_mutant("报告没许可自检提示（对外托管的企业无感知）",
                  _sub('"**许可与使用场景自检**：newenergy-fde 依据 Elastic License 2.0 提供。"',
                       '"**（许可提示已省略）**："'),
                  PIPELINE_FILE),
          _mutant("入口没有许可摘要（使用者看不到一句话提示）",
                  _sub('print("许可：Elastic License 2.0 · 企业内部自用无需额外授权；',
                       'pass  # 许可提示已删除'),
                  os.path.join("scripts", "fde_run.py")),
          _mutant("对外服务场景不提提醒（内部/外部边界被糊掉）",
                  _sub('请先确认商业授权状态。本报告自身不因此限制内部使用。',
                       '本报告不因此限制使用。'),
                  PIPELINE_FILE)]),
    Rule("全量判异规则必须有前置条件提示（默认低误报，全规则需稳定+Cpk≥1.33）",
         rule_full_rules_precondition,
         "（1）启用全部 8 条 Nelson 规则时必须提示前置条件（稳定 + Cpk≥1.33）；"
         "（2）提示是 warn 不是硬阻断（内部自用不被误拦）；"
         "（3）默认规则集保持低误报（basic 基本集，不是全规则）",
         PIPELINE_FILE,
         [_mutant("全规则不提示前置条件（使用者把自相关误报当工具坏了）",
                  _sub('已启用全量 Nelson 判异规则（8 条）。全规则仅建议在过程已稳定受控',
                       '已启用全量 Nelson 判异规则（8 条）。'),
                  PIPELINE_FILE),
          _mutant("前置条件不提 Cpk≥1.33（稳定受控这个前提被丢掉）",
                  _sub('且 Cpk≥1.33 时使用',
                       '时使用'),
                  PIPELINE_FILE)]),
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

def flow_ops(args):
    """可观测性运维视图（P2-4）：从结构化日志回读"慢在哪、哪一步常失败、失败属哪一类"。

    可观测性不是"写了日志"就算有——写完了没人能查，等于没写。
    """
    sys.path.insert(0, ROOT)
    from lib import observ as OBS
    path = getattr(args, "log", None) or OBS.log_path()
    evs = OBS.read_log(path, limit=int(getattr(args, "limit", 500) or 500))
    rep = OBS.summarize_log(evs)
    print("=" * 68)
    print("可观测性运维视图（P2-4）")
    print("=" * 68)
    print("日志：%s｜事件 %d 条｜资源越界 %d 次"
          % (path, rep.get("n_events") or 0, rep.get("limit_breaches") or 0))
    if not evs:
        print("（日志为空：还没有跑过 analyze，或日志路径不对）")
        print("=" * 68)
        return 0
    print("-" * 68)
    print("%-20s %6s %6s %8s %8s" % ("模块", "调用", "失败", "成功率", "耗时ms"))
    for m, st in sorted((rep.get("modules") or {}).items(),
                        key=lambda kv: -(kv[1].get("ms") or 0)):
        rate = st.get("success_rate")
        print("%-20s %6d %6d %8s %8.1f"
              % (m[:20], st.get("calls") or 0, st.get("fail") or 0,
                 ("%.0f%%" % (rate * 100)) if rate is not None else "—",
                 st.get("ms") or 0.0))
    if rep.get("error_classes"):
        print("-" * 68)
        print("失败分类（决定该找谁：data 找数据 / config 找配置 / io 找环境 / "
              "module 找我们）：")
        for c, n in sorted((rep.get("error_classes") or {}).items(),
                           key=lambda kv: -kv[1]):
            print("      · %-10s %d" % (c, n))
    _retry = [(m, st.get("retries") or 0) for m, st in (rep.get("modules") or {}).items()
              if st.get("retries")]
    if _retry:
        print("-" * 68)
        print("⚠ 靠重试才跑通的模块（一直带病运行）：")
        for m, n in _retry:
            print("      · %s：重试 %d 次" % (m, n))
    print("=" * 68)
    if getattr(args, "json", False):
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


def flow_kb(args):
    """知识生命周期与出处覆盖率审计（P2-2）。

    把"这个知识库有多少是可信的"变成可查的数字：出处覆盖率、生命周期必填覆盖率、
    取值真错的条目、超期未复核的条目。不修知识库，只出读数与待办清单。
    """
    sys.path.insert(0, ROOT)
    from lib import kbstore as KBS
    rep = KBS.audit_lifecycle()
    ov = rep.get("overall") or {}
    print("=" * 68)
    print("知识生命周期与出处覆盖率审计（P2-2）")
    print("=" * 68)
    print("合计条目 %d 条；带出处 %d 条（%.1f%%）；生命周期必填齐全 %d 条（%.1f%%）"
          % (ov.get("total") or 0, ov.get("with_source") or 0,
             (ov.get("source_coverage") or 0) * 100,
             (ov.get("total") or 0) - (ov.get("missing_minimum") or 0),
             (ov.get("minimum_coverage") or 0) * 100))
    print("-" * 68)
    for name, s in (rep.get("sections") or {}).items():
        cov = s.get("source_coverage")
        mcov = s.get("minimum_coverage")
        print("%-24s 共 %3d 条｜出处 %s｜生命周期必填 %s｜状态 %s"
              % (name, s.get("total") or 0,
                 ("%.0f%%" % (cov * 100)) if cov is not None else "—",
                 ("%.0f%%" % (mcov * 100)) if mcov is not None else "（不适用）",
                 s.get("status_counts") or "{}"))
        for k in (s.get("missing_source_sample") or [])[:5]:
            print("      · 缺出处：%s" % k)
    print("-" * 68)
    for n in (rep.get("notes") or []):
        print("※ %s" % n)
    if rep.get("invalid"):
        print("-" * 68)
        print("取值不合法（真缺陷，需逐条修）：")
        for it in rep["invalid"][:10]:
            print("      · %s / %s：%s" % (it.get("section"), it.get("key"),
                                           "；".join(it.get("issues") or [])))
    if rep.get("stale"):
        print("-" * 68)
        print("超期未复核（只标待复核，不自动作废）：")
        for it in rep["stale"][:10]:
            print("      · %s / %s：%s（%d 天）"
                  % (it.get("section"), it.get("key"), it.get("verified_at"),
                     it.get("age_days") or 0))
    print("=" * 68)
    if getattr(args, "json", False):
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


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

    if getattr(args, "init_products", False):
        return _config_init_products(args)
    if list(getattr(args, "products", None) or []) or getattr(args, "role", None):
        _config_profile_block(list(getattr(args, "products", None) or []),
                              getattr(args, "role", None),
                              bool(getattr(args, "cross", False)))
        print("\n" + "=" * 68)
        return 0

    if not args.init:
        print("\n" + "=" * 68)
        print("加 --init 生成企业规格待填模板")
        print("加 --products battery pv --cross 看多产品配置交叉表")
        return 0
    return _config_init(args)


def _config_init_products(args):
    """生成产品配置档待填模板。默认值一律留空——不替企业编参数。"""
    sys.path.insert(0, ROOT)
    from lib import profile as PF
    prods = list(getattr(args, "products", None) or [])
    if not prods:
        # 没指定产品就取知识库里登记的行业域，省得使用者还得先知道 key 名
        try:
            from lib import scope as SC
            prods = list(SC.known_industries(ROOT) or [])
        except Exception:
            prods = []
    if not prods:
        print("\n未指定产品，也读不到行业登记表：请用 --products battery pv 指定")
        return 1
    path = getattr(args, "out", None) or PF.PRODUCT_PROFILE_FILE
    try:
        p = PF.write_template(prods, path)
    except Exception as e:
        print("\n生成失败：%s" % e)
        return 1
    print("\n已生成产品配置档模板：%s" % p)
    print("  ⚠ 默认值一律留空：本工具不替企业编参数——编出来的默认值会被当成")
    print("    「系统给的」直接交付，那比留空危险得多。")
    print("  填好后放在 knowledge/product_profiles.json（或租户层同名文件），")
    print("  analyze 不指定开关时自动按产品档配；使用人显式指定的值始终优先。")
    return 0


def _config_profile_block(prods, role, cross=False):
    """打印「这些产品 + 这个角色」解析出的一套配置，以及为什么是这套。"""
    sys.path.insert(0, ROOT)
    try:
        from lib import profile as PF
    except Exception as e:
        print("\n【六】产品档配置解析：读不到 lib/profile.py（%s）" % e)
        return
    base = {"spc_exclusion_mode": "auto", "boundary_policy": "warn",
            "doe_replicates": 1, "doe_blocks": 1, "doe_seed": 0}
    print("\n【六】产品档与配置解析（多产品按语义合并，与书写顺序无关）")
    print("  产品：%s   角色：%s" % ("、".join(prods) or "（未指定）",
                                 PF.ROLE_LABEL.get(role, role) if role else "（未指定）"))

    # 先说清楚"这些产品到底有没有配"：没配就回落基线，
    # 而回落基线最容易被误读成"系统替我定好了"——判严尺度悄悄松了却没人知道。
    try:
        _pr = PF.load_profiles(ROOT)
        _no_file = [p for p in prods if p not in (_pr.get("products") or {})]
        _all_blank = [p for p in prods
                      if p not in _no_file
                      and all(v is None for v in (PF.defaults_of(p, _pr) or {}).values())]
        if _no_file:
            print("\n  ⚠ 以下产品没有配置档，其默认值全部回落基线：%s"
                  % "、".join(_no_file))
        if _all_blank:
            print("  ⚠ 以下产品的配置档已建但默认值全空，等同于未配：%s"
                  % "、".join(_all_blank))
        if _no_file or _all_blank:
            print("    生成待填模板：config --init-products --products %s"
                  % " ".join(prods))
    except Exception as e:
        # 这块只是提示，读不到不该让 config 崩；但也不能一声不吭——
        # 静默的后果是使用者以为"查过了没问题"，其实根本没查成。
        print("  ⚠ 读产品档失败，无法判断这些产品是否已配置：%s" % e)

    if cross and prods:
        m = PF.cross_matrix(prods, role=role, baseline=base)
        if not m.get("ok"):
            print("  %s" % m.get("error"))
            return
        print("\n  交叉搭配映射表（%d 个组合，实时算出，不是抄来的）："
              % m["n_combinations"])
        for row in m["rows"]:
            tail = ("   ⚠需人指定：" + "、".join(row["needs_decision"])) \
                if row["needs_decision"] else ""
            print("   %-22s %s%s" % (row["key"],
                                     json.dumps(row["config"], ensure_ascii=False), tail))
        print("\n  needs_decision 非空 = 该组合下这几个参数必须由使用人指定，")
        print("  工具不替他选（各产品关注对象不同，选了就是替他决定看什么）。")
        return

    r = PF.resolve_config(prods, role=role, baseline=base)
    if r["trace"]:
        print("\n  逐参数判定：")
        for t in r["trace"]:
            print("   %-20s = %-22s [%s] %s"
                  % (t["param"], str(t["value"]), t["semantics"], t["from"]))
    if r["needs_decision"]:
        print("\n  ⚠ 必须由使用人指定（工具不猜）：")
        for n in r["needs_decision"]:
            print("   %s：%s" % (n["param"], n["reason"]))
    for nte in r["notes"]:
        print("  · %s" % nte)
    if not r["trace"] and not r["needs_decision"]:
        print("\n  （产品档未配置任何默认值，全部回落基线。"
              "生成待填模板：config --init-products --products ...）")


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
        missing = [p for p in (args.input or []) if not os.path.exists(p)]
        if missing:
            return False, "输入文件不存在：%s" % "、".join(missing[:3]), {}
        if args.input_dir and not os.path.isdir(args.input_dir):
            return False, "输入目录不存在：%s" % args.input_dir, {}
        return ok, "" if ok else "Python 版本过低", {"python": "%d.%d" % (v[0], v[1])}

    steps.append(run_check("1. 环境预检", env_check))
    steps.append(run_check("2. 残留锁清理", check_stale_locks))

    # 透传清单由 lib/pipeline.py 的 argparse 定义生成，不手抄——
    # 手抄的清单会随 pipeline 加参数而漏项，且漏了不会报错只会静默少做。
    cmd = [PY, "lib/pipeline.py"]
    if args.input:
        cmd += ["--input"] + list(args.input)
    cmd += ["--outdir", outdir, "--json", result_json]
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
                  extra={"outdir": outdir, "inputs": list(args.input or []),
           "input_dir": getattr(args, "input_dir", None)},
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
                    choices=["audit", "verify", "analyze", "release", "config",
                             "kb", "ops"],
                    help="audit=约定落地核查  verify=质量闸门  "
                         "analyze=交付主流程  release=发布  "
                         "config=可调参数清单与企业规格模板  "
                         "kb=知识生命周期与出处覆盖率审计  "
                         "ops=可观测性运维视图（结构化日志回读）")
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
    ap.add_argument("--out", help="config --init / --init-products：模板输出路径")
    ap.add_argument("--products", nargs="+", default=None,
                    help="config：按产品（行业域）看配置默认值怎么合并，"
                         "如 --products battery pv（多产品按语义合并，与书写顺序无关）")
    ap.add_argument("--init-products", action="store_true",
                    help="config：生成产品配置档待填模板（数值一律留空，不替企业编）")
    ap.add_argument("--cross", action="store_true",
                    help="config：配合 --products 出交叉搭配映射表"
                         "（所有产品组合 → 配置，含需人决断项）")

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

    if args.flow == "analyze" and not args.input and not args.input_dir:
        # 现场从 MES 导出往往是一整个目录（一天一个文件），逐个列文件名不可持续
        ap.error("analyze 必须给 --input 或 --input-dir")

    print("=" * 68)
    print("newenergy-fde 编排器 —— %s" % args.flow)
    print("=" * 68)
    print("许可：Elastic License 2.0 · 企业内部自用无需额外授权；"
          "对外托管/服务请先确认商业授权。")

    if args.flow == "config":
        return flow_config(args)
    if args.flow == "kb":
        return flow_kb(args)
    if args.flow == "ops":
        return flow_ops(args)
    if args.flow == "audit":
        return flow_audit(args)
    if args.flow == "verify":
        return flow_verify(args)
    if args.flow == "release":
        return flow_release(args)
    return flow_analyze(args)


if __name__ == "__main__":
    sys.exit(main())
