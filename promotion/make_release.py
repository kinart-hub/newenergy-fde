# -*- coding: utf-8 -*-
"""创建 GitHub Release（v1.14.3）。令牌经 git credential 获取，仅内存使用。"""
import json
import subprocess
import urllib.request

TOKEN = None
for line in subprocess.run(["git", "credential", "fill"],
                           input=b"protocol=https\nhost=github.com\n\n",
                           capture_output=True).stdout.decode().splitlines():
    if line.startswith("password="):
        TOKEN = line.split("=", 1)[1]

BODY = """## 新增

- **缺陷知识库 15 → 25 条，工序覆盖 6/8 → 8/8**：补齐辊压、注液两道空白工序；
  EL 缺陷补 黑心/碎片/虚焊/脏污/边缘漏电；新增 注液量偏差、电芯水分超标、
  压实密度超差、极片露箔、极耳焊接不良。示例数据 2371 件缺陷全部有机理支撑（覆盖率 48.2% → 100%）
- **别名索引**：`params[].aliases` 里的现场叫法（黑心/黑斑、碎片/断片/破片、虚焊/过焊…）
  首次接入根因识别的列匹配、分类列匹配、hint 匹配三处路径；多别名交叉命中按**最长别名优先**裁决
- 未收录缺陷不再只回「机理未收录」：给出不影响的数据证据说明 + 可直接复制的补录命令
- 上架前审计规则 21 → 25（悬空引用 / 别名索引旁路 / 响应型缺陷继承 / 静默未收录）

## 修复

- 别名过泛导致 `param_key` 的 `Pmax` 覆盖目标列选择（真回归）
- 盲测驱动的别名补齐：`pump_speed` += PumpRPM/泵转速、`coating_speed` += CoatSpeed、
  `gap_ratio` += Gap/辊缝——此前真因列被护栏**正确拒绝**，但因无精确别名而永无候选资格
- `_block` 截断范围诊断在 hint 分支的正则缺行尾锚定问题

## 升级注意

- 无破坏性变更；知识库条目新增由 `kb_manage.py sync` 自动追加至 `knowledge/CHANGELOG.md`
- 免提示盲测（6 套对抗数据）：**top1 命中率 0.80 → 1.00**，
  错误工序率/错误目标率/伪相关居首率/低证据填 D4 率/负例误报率全部 **0.0**
- 主自检 279/279；上架前审计 PASS（阻断 0 / 提醒 0）

---

**License**: Elastic License 2.0 — source-available, **not** OSI open source.
Free for personal and internal business use; hosted services require a separate commercial license."""

req = urllib.request.Request(
    "https://api.github.com/repos/kinart-hub/newenergy-fde/releases",
    data=json.dumps({
        "tag_name": "v1.14.3",
        "name": "v1.14.3 — 缺陷知识库补全 + 别名索引（盲测反向指标全清零）",
        "body": BODY,
        "draft": False,
        "prerelease": False,
    }).encode(),
    headers={"Authorization": f"bearer {TOKEN}", "Content-Type": "application/json",
             "User-Agent": "fde-ops"})
with urllib.request.urlopen(req, timeout=30) as r:
    d = json.load(r)
print("release:", d.get("html_url"))
