# -*- coding: utf-8 -*-
"""
package.py —— 把 Skill 打包为可分发的 zip

排除：output/（分析结果）、dist/、__pycache__、*.pyc、.DS_Store
用法： python scripts/package.py
"""

import os
import sys
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
EXCLUDE_DIRS = {"output", "dist", "__pycache__", ".git", ".vscode", ".idea",
                "backups", "tenants", "promotion", ".lock", "_private"}
EXCLUDE_EXT = {".pyc", ".pyo", ".DS_Store"}
# prepublish_audit.py 是仓库/CI 侧的治理工具，不属于 Skill 运行时；
# 而且它内部就带着一整套「不许出现的东西」的正则字面量，随包分发会污染接收方的扫描。
EXCLUDE_FILES = {"inbox.jsonl", "prepublish_audit.py"}


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

    dist = os.path.join(ROOT, "dist")
    if not os.path.isdir(dist):
        os.makedirs(dist)
    zpath = os.path.join(dist, "%s-%s.zip" % (name, version))

    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1] in EXCLUDE_EXT or fn in EXCLUDE_FILES:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT)
            files.append((full, rel))
    files.sort(key=lambda x: x[1])

    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for full, rel in files:
            z.write(full, os.path.join(name, rel))

    size = os.path.getsize(zpath)
    with open(zpath, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()[:16]

    print("打包完成：%s" % zpath)
    print("文件数：%d    大小：%.1f KB    sha256:%s" % (len(files), size / 1024.0, sha))
    print("\n导入方式：")
    print("  1) 解压到 ~/.workbuddy/skills/%s/" % name)
    print("  2) 或在 WorkBuddy 中选「导入 Skill」并选择该 zip")
    print("\n接收方无需安装任何 Python 第三方包（零依赖）。")
    return zpath


if __name__ == "__main__":
    main()
