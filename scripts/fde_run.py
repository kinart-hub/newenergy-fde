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
                  _sub('"backups", "tenants", "promotion", ".lock", "_private"}',
                       '"backups", "tenants", "promotion", ".lock"}'))]),

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
