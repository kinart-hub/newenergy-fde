# -*- coding: utf-8 -*-
"""
package.py —— 把 Skill 打包为可分发的 zip

排除：output/（分析结果）、dist/、__pycache__、*.pyc、.DS_Store
用法： python scripts/package.py
"""

import os
import sys

# 同 selftest.py：Windows 重定向输出时强制 UTF-8，避免中文被 cp1252 卡死。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
    # scan:keep stdout 编码兜底在模块加载期执行，degrade 尚未导入；且非 Windows 环境没有 reconfigure 属正常分支，不是降级。
import json
import zipfile
import hashlib
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# backups：本地回滚副本，不含新信息；inbox：可能夹带客户现场的待审候选，禁止随包外发
# tenants/ 存放各企业的私有知识层与锁，绝不能进分发包：
# 把 A 企业的私有参数发给 B 企业是最严重的一种数据泄露
# .lock：进程被 kill 后残留的锁目录，无信息量，随包分发只会让接收方困惑
# _private：签发私钥 / 客户许可证 / 授权工具，绝不可随任何包外发
# pandas_reference：仅供对照阅读的 pandas 版参考实现，依赖第三方库，
# 随包分发会与「零第三方依赖」的交付口径冲突，也会污染接收方的依赖扫描。
EXCLUDE_DIRS = {"output", "dist", "__pycache__", ".git", ".vscode", ".idea",
                "backups", "tenants", "promotion", ".lock", "_private",
                "pandas_reference"}
EXCLUDE_EXT = {".pyc", ".pyo", ".DS_Store"}
# inbox.jsonl 可能夹带客户现场的待审候选，任何情况下都不随包外发。
EXCLUDE_FILES = {"inbox.jsonl"}


def main():
    # 【前置闸门】上架前隐私面审计。
    # 放在打包入口而不是写在文档里，是因为文档会被忽略，而这里拦得住。
    # 发生过：某客户的真实改善案例（含具体温度设定值与内部文件号）被打进了
    # 分发包，而改善措施库会跨客户推荐——等于把一家的技术秘密推给另一家。
    _audit = os.path.join(ROOT, "scripts", "prepublish_audit.py")
    if os.path.exists(_audit):
        _r = subprocess.run([sys.executable, _audit], cwd=ROOT,
                            capture_output=True, encoding="utf-8", errors="replace")
        if _r.returncode != 0:
            print("× 上架前审计未通过，拒绝打包。以下是必须清理的项：")
            print((_r.stdout or "").strip())
            print("\n确认为误报可加业务白名单，但请先在 issue 里留记录。")
            sys.exit(2)
        print("上架前审计通过（隐私面 / 必备文件）")
    else:
        print("! 未找到 scripts/prepublish_audit.py，跳过上架前审计")

    # 打包前自查：正库结构必须干净，否则会把不一致的知识分发给企业用户
    sys.path.insert(0, ROOT)
    try:
        from lib import kbstore as KBS
        kb = KBS._read_json(KBS.SPEC_FILE, {})
        KBS.invalidate_cache()
        bad = []
        for dk, dv in kb.get("defect_rootcause", {}).items():
            for pk in dv.get("likely_params", []):
                if pk not in kb.get("params", {}):
                    bad.append("%s->%s" % (dk, pk))
        if bad:
            print("× 知识库存在悬空引用，拒绝打包：%s" % bad)
            sys.exit(2)
        print("知识库校验通过：参数 %d｜工序 %d｜缺陷 %d"
              % (len(kb.get("params", {})), len(kb.get("processes", {})),
                 len(kb.get("defect_rootcause", {}))))
        # 设备档案：别名指向的参数必须已登记，否则分发过去也认不出检测项
        try:
            from lib import devices as DEV
            prof = DEV.load_profiles()
            n_dev = len(prof.get("devices", {}))
            dangle = []
            for dk, dv in prof.get("devices", {}).items():
                for cn, pk in (dv.get("aliases") or {}).items():
                    if pk and pk not in kb.get("params", {}):
                        dangle.append("%s:%s→%s" % (dk, cn, pk))
            if dangle:
                print("× 设备档案存在悬空映射，拒绝打包：%s" % dangle[:5])
                sys.exit(2)
            print("设备档案校验通过：%d 台设备｜单位换算 %d 条"
                  % (n_dev, len(prof.get("unit_conversions") or {})))
        except Exception as e:
            print("! 设备档案校验跳过：%s" % e)
    except Exception as e:
        print("! 知识库校验跳过：%s" % e)
    mpath = os.path.join(ROOT, "manifest.json")
    with open(mpath, "r", encoding="utf-8") as f:
        meta = json.load(f)
    version = meta.get("version", "0.0.0")
    name = meta.get("name", "skill")

    dep = meta.get("deployment") or {}
    # 不随包外发的仓库侧治理工具由 manifest 单一真源声明（原先在 EXCLUDE_FILES 里
    # 又硬编码一份，导致 manifest 声称「部署必需」而包里没有，按清单核验的人拿到假缺件）。
    not_deployed = set(dep.get("not_deployed") or []) | EXCLUDE_FILES
    deploy_required = list(dep.get("required") or [])

    dist = os.path.join(ROOT, "dist")
    if not os.path.isdir(dist):
        os.makedirs(dist)
    zpath = os.path.join(dist, "%s-%s.zip" % (name, version))

    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1] in EXCLUDE_EXT:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            if rel in not_deployed or fn in EXCLUDE_FILES:
                continue
            files.append((full, rel))
    files.sort(key=lambda x: x[1])
    packed = {rel for _full, rel in files}

    # 先写临时文件、核对通过再落位：直接写到 dist/<name>-<ver>.zip 的话，
    # 核对失败时 dist 里会躺着一个缺件的包，下一个拿到它的人不知道它不完整。
    tmp = zpath + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for full, rel in files:
            z.write(full, os.path.join(name, rel))

    size = os.path.getsize(tmp)
    with open(tmp, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()[:16]

    # 【打包后强制核对】只打印文件数是不够的：verify 的第 8 步曾长期「打包成功即通过」，
    # 于是清单声称 81 项、包内实为 80 项这种缺件无人发现。这里按 deployment.required
    # 逐项核包内齐全性，并禁止 not_deployed 出现在包里——不一致就非 0 退出，让门禁真能拦。
    problems = []
    if deploy_required:
        lack = [r for r in deploy_required if r not in packed]
        if lack:
            problems.append("包内缺 %d 个部署必需文件：%s%s"
                            % (len(lack), "、".join(lack[:8]),
                               " …" if len(lack) > 8 else ""))
        leak = sorted(packed & set(dep.get("not_deployed") or []))
        if leak:
            problems.append("包内出现 %d 个不随包外发的治理工具：%s"
                            % (len(leak), "、".join(leak[:8])))
    else:
        problems.append("manifest.json 缺 deployment.required，无法核对包内容齐全性")
    if problems:
        print("\n× 包内容与 manifest 声明不一致，拒绝交付：")
        for p in problems:
            print("   - " + p)
        print("   修法：python scripts/gen_manifest.py 重建清单，"
              "或补上被打包排除规则误伤的必需文件。")
        try:
            os.remove(tmp)
        except OSError as exc:
            # 不静默：删不掉会在 dist 里留一个 *.zip.tmp，下一个人可能误当成品。
            print("   ! 临时文件清理失败，请手工删除：%s（%s）" % (tmp, exc))
        return None
    os.replace(tmp, zpath)
    # 「打包完成」必须等核对通过、文件真正落位之后才说：
    # 原先在核对前就打印，失败时使用者会以为包已经好了，而 dist 里躺着的是上一版。
    print("打包完成：%s" % zpath)
    print("文件数：%d    大小：%.1f KB    sha256:%s" % (len(files), size / 1024.0, sha))
    print("核对：部署必需 %d 项全部在包内；不随包外发 %d 项已排除"
          % (len(deploy_required), len(not_deployed)))
    print("\n导入方式：")
    print("  1) 解压到 ~/.workbuddy/skills/%s/" % name)
    print("  2) 或在 WorkBuddy 中选「导入 Skill」并选择该 zip")
    print("\n接收方无需安装任何 Python 第三方包（零依赖）。")
    return zpath


if __name__ == "__main__":
    # 必须把返回值转成退出码：verify 第 8 步靠退出码判定，
    # 只 print 不 exit 的话「包内容缺件」也会被记成 OK。
    sys.exit(0 if main() else 2)
