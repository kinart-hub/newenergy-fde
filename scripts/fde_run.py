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
  python scripts/fde_run.py audit                      约定落地核查（14 条硬判据）
  python scripts/fde_run.py verify                     质量闸门全套
  python scripts/fde_run.py analyze -i data.csv -o out  交付主流程
  python scripts/fde_run.py release                    闸门 + 分发包指纹

退出码：0 = PASS    1 = FAIL    3 = PARTIAL（有步骤被跳过）
"""

import argparse
import glob
import io
import json
import os
import re
import subprocess
import sys
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


def check_entry_utf8():
    bad = []
    for rel in ENTRY_SCRIPTS:
        t = _read(rel)
        if t is None:
            bad.append("%s（文件不存在）" % rel)
            continue
        if "reconfigure" not in t and "PYTHONIOENCODING" not in t:
            bad.append(rel)
    return (not bad), ("以下入口未做 UTF-8 兜底：" + "、".join(bad)) if bad else "", \
        {"checked": len(ENTRY_SCRIPTS)}


# --------------------------------------------------------------- audit 规则

def _r(ok, reason, extra=None):
    return ok, reason, extra or {}


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
    try:
        p = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
    except Exception as e:
        # 不是仓库（例如分发出去的副本）时这条跑不了，记 SKIP 而不是 FAIL：
        # 让"没法查"冒充"查出问题"，会让真正的问题淹没在噪音里。
        return True, "git 不可用，本条未执行：%s" % e, {"skipped": True}
    if p.returncode != 0:
        return True, "不在 git 仓库中，本条未执行", {"skipped": True}
    files = [l.strip() for l in p.stdout.splitlines() if l.strip()]
    pats = ("_private", "signer_key", "license_tool", "tenants/", "output/",
            ".lock", "PUBLIC_KEY")
    hit = [f for f in files if any(k in f for k in pats) or f.endswith(".key")]
    return _r(not hit, "仓库里有不该出现的：%s" % "、".join(hit[:5]) if hit else "",
              {"tracked": len(files), "hits": hit})


def rule_ci_matrix():
    t = _read(os.path.join(".github", "workflows", "quality-gate.yml")) or ""
    miss = [k for k in ("ubuntu-latest", "windows-latest",
                        "PYTHONIOENCODING", "PYTHONUTF8") if k not in t]
    return _r(not miss, "CI 配置缺：%s" % "、".join(miss) if miss else "")


def rule_hotswap_write():
    t = _block(_read(os.path.join("lib", "hotswap.py")),
               "def atomic_write_text", "def _cleanup")
    miss = []
    for k in ("random", "13, 5", "max_delay"):
        if k not in t:
            miss.append(k)
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
    if not re.search(r"^class _Heartbeat", t, re.M):
        miss.append("_Heartbeat 类")
    return _r(not miss, "锁缺：%s" % "、".join(miss) if miss else "")


def rule_package_excludes():
    t = _read(os.path.join("scripts", "package.py")) or ""
    seg = _block(t, "EXCLUDE_DIRS", "def ", 800)
    miss = [k for k in ("tenants", "output", "_private", ".lock") if k not in seg]
    return _r(not miss, "打包未排除：%s" % "、".join(miss) if miss else "")


def rule_audit_gate():
    t = _read(os.path.join("scripts", "package.py")) or ""
    return _r(("prepublish_audit" in t), "打包未接隐私审计闸门")


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
    return _r("不含" in seg or "设定值" in seg,
              "D5 未声明不含参数设定值")


AUDIT_RULES = [
    ("LICENSE 为 Elastic 2.0 官方纯文本", rule_license),
    ("NOTICE 含版权署名", rule_notice),
    (".gitignore 挡住私钥与授权文件", rule_gitignore),
    (".gitattributes 统一 LF", rule_gitattributes),
    ("仓库内无私钥/授权工具/客户数据", rule_no_secret_in_repo),
    ("CI 覆盖双平台且声明 UTF-8", rule_ci_matrix),
    ("入口脚本 stdout 有 UTF-8 兜底", check_entry_utf8),
    ("原子写含抖动且只重试共享冲突", rule_hotswap_write),
    ("读侧有容错重试", rule_hotswap_read),
    ("知识库 demo 案例硬闸门", rule_kb_demo_gate),
    ("锁含 PID 存活探测与心跳", rule_lock_robust),
    ("打包排除租户/产物/私有目录", rule_package_excludes),
    ("打包接了隐私审计闸门", rule_audit_gate),
    ("8D 四道确审齐全", rule_signoff),
    ("D5 声明不含参数设定值", rule_d5_no_setpoint),
]


# --------------------------------------------------------------- 流程定义

def flow_audit(args):
    started = time.time()
    steps = []
    for i, (desc, fn) in enumerate(AUDIT_RULES, 1):
        steps.append(run_check("%2d. %s" % (i, desc), fn))
    return finish("约定落地核查（%d 条）" % len(AUDIT_RULES), steps,
                  args.report, started=started)


def flow_verify(args):
    started = time.time()
    steps = [
        run_cmd("1. 主自检", [PY, "scripts/selftest.py"], timeout=args.timeout),
        run_cmd("2. 护栏自检", [PY, "scripts/selftest_guard.py"], timeout=args.timeout),
        run_cmd("3. 审计规则自检", [PY, "scripts/prepublish_audit.py", "--selfcheck"],
                timeout=args.timeout),
        run_cmd("4. 上架前隐私审计", [PY, "scripts/prepublish_audit.py", "--strict"],
                timeout=args.timeout),
        run_cmd("5. 打包完整性", [PY, "scripts/package.py"], timeout=args.timeout),
    ]
    return finish("质量闸门全套", steps, args.report, started=started)


def flow_release(args):
    started = time.time()
    steps = [
        run_cmd("1. 主自检", [PY, "scripts/selftest.py"], timeout=args.timeout),
        run_cmd("2. 护栏自检", [PY, "scripts/selftest_guard.py"], timeout=args.timeout),
        run_cmd("3. 审计规则自检", [PY, "scripts/prepublish_audit.py", "--selfcheck"],
                timeout=args.timeout),
        run_cmd("4. 上架前隐私审计", [PY, "scripts/prepublish_audit.py", "--strict"],
                timeout=args.timeout),
        run_cmd("5. 打包", [PY, "scripts/package.py"], timeout=args.timeout),
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

    steps.append(run_check("6. 分发包指纹", dist_fingerprint))
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
