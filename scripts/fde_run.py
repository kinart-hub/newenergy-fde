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
    except Exception:
        pass

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
                    except Exception:
                        pass
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


def _mutant(label, fn, target=None):
    """一个变异。target 缺省就用规则自己的目标文件。

    fn 返回 None = 这个变异在当前源码上做不出来（源码结构变了），
    自检必须把它记成 SKIP 暴露出来，而不是当成"验证通过"。
    """
    return (label, fn, target)


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
EIGHTD_FILE = os.path.join("lib", "eightd.py")


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
    called = re.search(r"for\s+nn\s+in\s+_defect_alias_names\(dk,\s*dv\):",
                       body, re.M) is not None
    if not has_def:
        return _r(False, "缺少别名索引函数 _defect_alias_names 的定义")
    if not called:
        return _r(False, "identify_defects 的列匹配未遍历别名集合"
                        "（现场叫法将对不上标准条目）")
    return _r(True, "", {"has_def": True, "called": True})


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
         [_mutant("仓库里暂存私钥文件", _git_add_secret)]),

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
                  _sub("for nn in _defect_alias_names(dk, dv):",
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

    for label, mfn, mtarget in rule.mutants:
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
        except Exception:
            pass
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

    cmd = [PY, "lib/pipeline.py", "--input"] + list(args.input) + \
          ["--outdir", outdir, "--json", result_json]
    for flag, val in (("--target", args.target), ("--process", args.process),
                      ("--defect", args.defect), ("--sheet", args.sheet),
                      ("--spec", args.spec), ("--tenant", args.tenant),
                      ("--d2-card", args.d2_card)):
        if val:
            cmd += [flag, val]
    if args.no_doe:
        cmd.append("--no-doe")
    if args.verbose:
        cmd.append("--verbose")
    if args.industry:
        cmd += ["--industry"] + list(args.industry)

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
        for m in mds:
            try:
                text += io.open(m, encoding="utf-8", errors="replace").read()
            except Exception:
                pass
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
    ap.add_argument("flow", choices=["audit", "verify", "analyze", "release"],
                    help="audit=约定落地核查  verify=质量闸门  "
                         "analyze=交付主流程  release=发布")
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
    ap.add_argument("--report", default=os.path.join("output", "_fde_run_report.json"),
                    help="证据报告落盘路径")
    ap.add_argument("--verbose", action="store_true",
                    help="逐阶段打印「这份数据被理解成了什么」（失败时先看这个）")
    ap.add_argument("--timeout", type=int, default=900, help="单步超时（秒）")
    args = ap.parse_args(argv)

    if args.flow == "analyze" and not args.input:
        ap.error("analyze 必须给 --input")

    print("=" * 68)
    print("newenergy-fde 编排器 —— %s" % args.flow)
    print("=" * 68)

    if args.flow == "audit":
        return flow_audit(args)
    if args.flow == "verify":
        return flow_verify(args)
    if args.flow == "release":
        return flow_release(args)
    return flow_analyze(args)


if __name__ == "__main__":
    sys.exit(main())
