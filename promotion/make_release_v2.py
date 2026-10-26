# -*- coding: utf-8 -*-
"""创建 GitHub Release（v2.0.0）。令牌经 git credential 获取，仅内存使用。"""
import json
import subprocess
import urllib.request

TOKEN = None
for line in subprocess.run(["git", "credential", "fill"],
                           input=b"protocol=https\nhost=github.com\n\n",
                           capture_output=True).stdout.decode().splitlines():
    if line.startswith("password="):
        TOKEN = line.split("=", 1)[1]
if not TOKEN:
    raise SystemExit("未取得 GitHub 令牌")

BODY = """## 说明：2.0.0 不是 API 破坏

版本号指《盲测复核与整改规格 v2.0》**逐条落地完成**，无接口变更，
现有脚本与集成代码不需要改一行。

## 新增

- **§4.5 多重比较补完**：期望频数不足时不再使用不合法的 2×2 Fisher。
  缺陷事件表只有缺陷计数、没有非缺陷对照，行列边际不成立——原实现把期望频数
  当对照组塞进去，算出来的是"看起来像 Fisher"的数。改为**二项精确检验**
  （H0 = 该水平占比等于整体占比，与 R `binom.test` 同口径），
  并在对数空间计算，防上万行数据时组合数下溢成 0、双尾和退化为 1.0
- **`stats.is_sig()` / `stats.fmt_p()`**：p 值判定的唯一入口与显示口径
- 自检新增 P 组 14 条护栏：二项精确 / BH 校正 / 曝光加权 / 小样本降级 / 假值陷阱

## 修复

- **假值陷阱（最危险一类）**：`round(p, 6)` 会把 1e-30 压成 `0.0`，
  而代码里写成 `(p or 1) < 0.05` —— **最强的统计证据被判为"不显著"**。
  这类缺陷不报错、不崩溃，只会让报告少一句真结论、多一句"未见显著"。
  修正 `events`（聚集显著性）、`actions`（3 处）、`crossprocess`（2 处，含上游/本工序责任判定）
- 移除不合法的 `_fisher_p`，避免后续调用方误用
- 确认并补护栏：曝光加权 / 12 水平 BH 校正 / 按缺陷类型分层（此三项能力此前已在）

## 升级注意

- **报告措辞可能变化，这不是回归是修复**：p 值显示改为极小值呈现 `<1e-4`
  （此前兜底显示 `1.0`），部分此前"未见显著"的结论会正常出现
- 无破坏性变更；知识库条目新增仍由 `kb_manage.py sync` 自动追加

## 验证

- 主自检 **293/293**（新增 P 组 14 条）
- 上架前审计 PASS（阻断 0 / 提醒 0）
- 免提示盲测（6 套对抗数据）：top1 / top3 命中率 **1.00 / 1.00**，
  错误工序率 / 错误目标率 / 伪相关居首率 / 低证据填 D4 率 / 负例误报率全部 **0.0**

---

**License**: Elastic License 2.0 — source-available, **not** OSI open source.
Free for personal and internal business use; hosted services require a separate commercial license."""

req = urllib.request.Request(
    "https://api.github.com/repos/kinart-hub/newenergy-fde/releases",
    data=json.dumps({
        "tag_name": "v2.0.0",
        "name": "v2.0.0 — 整改规格全项落地：聚集检验补完 + 假值陷阱治理",
        "body": BODY,
        "draft": False,
        "prerelease": False,
    }).encode(),
    headers={"Authorization": f"bearer {TOKEN}", "Content-Type": "application/json",
             "User-Agent": "fde-ops"})
with urllib.request.urlopen(req, timeout=30) as r:
    d = json.load(r)
print("release:", d.get("html_url"))
