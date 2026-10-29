# 整改清单（2.19.5 重点复核）

- 复核范围：完整性 / 逐文件审查 / 缺陷逻辑 / 测试审查 / 与上一版建议对照 / 输出清单
- 复核基线：外部审计报告 9 条 + 上一版 5 专项已在 2.19.1~2.19.4 落实（本次逐项复验确认）
- 本版新发现并修复：**4 处真实缺陷**（P0×1、P1×3），另留 2 项 P2 待后续
- 版本：`ANALYZER_VERSION = "2.19.5"`（manifest 经 `gen_manifest.py` 同步）
- 证据脚本：`scripts/verify_rev_fixes.py --check`（4/0 全过）

---

## 一、上一版建议对照（范围 5，已落实并复验）

| # | 专项 | 落点文件 | 复验结论 |
|---|------|----------|----------|
| 1 | 相关性缺失值错位 | `lib/stats.py:72 finite_pairs` | 按**原始行号 i** 配对，任一侧非数值即整行排除（94-98 行）；`pearson` 经 `finite_pairs` 入口。中间缺失不再错位。✅ |
| 2 | 指标方向判断 | `lib/direction.py` | 4 态语义 + `UNKNOWN` 退化为"带假定的建议"（默认 `higher_is_worse` 且须在结论声明）；`worst_group/resolve/qualifier` 完整。✅ |
| 3 | 失败状态混淆 | `lib/status.py` | 7 态统一枚举；仅 `NO_FINDING` 可宣称无异常；私造状态归 `FAILED`；`frame/normalize/summarize` 一致。✅ |
| 4 | 证据追溯 | `lib/provenance.py` | `run_id` 与 `fingerprint` 分设；`new_run/finish/replay_check` 完整；输入指纹含配置与版本。✅ |
| 5 | DOE 安全门禁 | `lib/doe.py:695/713/795` | `safety_envelope / build_constraints / check_point` 已实现。**但见 P2-2：lib 层 `build_design/decode_design` 不接收 constraints，仅 pipeline 层 clamp 兜底拦截。** |

> 外部审计报告 9 条（P0-1~P0-4、P1-1~P1-5：Nelson 规则2 整段返回、Phase I>5% 剔除判不稳、多规则溯源、过程能力自相关抑制、分段 σ、正态性接入、列名匹配护栏等）已在 2.19.4 落实，`audit`(76/0) 与 `audit --selftest`(152/0) 全绿可证。

---

## 二、本版新发现并修复的缺陷（按优先级）

### P0 — 必须修（已修复）

#### P0-1　rootcause 诚实降级泄漏（无数据支撑仍吐根因榜单）
- **文件**：`lib/rootcause.py`（`rank_root_causes` 守卫，~672 行）
- **问题原因**：原守卫 `if not suf.get("n_data_backed") and (suf.get("n_effective") or 0) >= MIN_N_FOR_RANKING:` 叠加了样本量门槛，漏掉了 `target_col=None` 与 `n<8` 的 `INSUFFICIENT_DATA` 场景。这两类"没查到"仍走进正常分支，输出带分数的 Top 榜单，下游 `honest_degraded=None` 把假榜单渲染进报告。
- **影响**：使用者照着一份"看起来很专业、数字规整"的榜单去调机，真因一点没动——属于**结论误导**最高危形态。
- **修复**：守卫改为 `if not suf.get("n_data_backed"):`——只要零候选拿到数据支撑，立即清空 `candidates` 与 `all_candidates`（含 `all_candidates` 防 `views.py` 的 `or` 兜底泄露），降级为 `prior_only_hints`（显式标注"未经本批数据验证，不得据此调机"）。
- **验收标准**：`verify_rev_fixes.py` F1 —— `target_col=None` 且无匹配列时 `candidates==0` 且 `prior_only_hints>0`；状态为 `INSUFFICIENT_DATA`。✅

### P1 — 应修（已修复）

#### P1-1　rootcause 时序反证死分支
- **文件**：`lib/rootcause.py`（`_temporal_match` 397-404 行；消费方 830-836 行）
- **问题原因**：旧 `_temporal_match` 不返回 `verdict` 键，消费方 `beforeafter.get("verdict")` 永远读不到，时序反证整段是死分支；且 `verdict in ("不符","不支持")` 中"不符"从不产出（死字面）。
- **影响**：参数与指标异常时段不重合时，无法对"同节拍因果"主张给出时序层面的反证，可能放过伪因果候选。
- **修复**：`_temporal_match` 显式返回 `verdict`（"支持"/"不支持"）与 `note`；消费方据 `"不支持"` 生成"时序上不支持：…"反证；清理死字面。
- **验收标准**：`verify_rev_fixes.py` F2 —— 反相数据→`verdict="不支持"`；该候选经 `_annotate_evidence` 后 `counter_evidence` 含"时序上不支持"。✅

#### P1-2　DOE 2 水平设计解不出高角点
- **文件**：`lib/doe.py`（`decode_design` 184-228 行）
- **问题原因**：原实现把水平号 `2` 一律当中点。2 水平设计（full_factorial(2^k)、Taguchi L8/L16，即**默认推荐路径**）只有 1/2 两个水平号，导致 `2` 永远解成中点，整条默认 DOE 只在"低/中"上做实验，**高角点从未被探索**。
- **影响**：默认 DOE 实验方案结构性缺一角，优化/寻因在高区失效，且不易察觉（矩阵看着完整）。
- **修复**：按各因子在矩阵里出现的**最高水平号**区分（`max_lv<=2` → `2` 即高；`>2` → `2` 仍中点）。3 水平设计 1/2/3→低/中/高语义不变。
- **验收标准**：`verify_rev_fixes.py` F3 —— 2 水平矩阵解出 `(high,high)` 行；3 水平 `[1],[2],[3]`→`[3.0],[4.0],[5.0]`。✅

#### P1-3　crossprocess 参考量程拿不到（误报缺失）
- **文件**：`lib/crossprocess.py`（`probe_margin` 484 行；`trace` 599 行传 `pkey`）
- **问题原因**：旧代码写死 `_kb_reference(_base_param_of(param_col, [{}]), kb_params)`——`[{}]` 空 meta 只能剥出裸列名，当列名是中文别名（裸名≠知识库键）时永远查不到参考量程，`reference` 恒定 `None`，"请先索取上游规格"提示形同虚设。修复过程中一次缩进编辑误把 `elif` 嵌进 `if` 体内引入语法错误（已还原）。
- **影响**：情形 C（裕度探针）在"差异显著但无规格"时，参考量程提示恒缺，弱化上游责任判定的可追溯性。
- **修复**：`probe_margin` 接入 `trace` 已按真实 `table_meta` 解析出的 `pkey`；语法错误已修正并复验。
- **验收标准**：`verify_rev_fixes.py` F4 —— 中文别名列 + 真实 `pkey` 拿到 `reference`（知识库控制口径）；旧 `pkey=None` 空 meta 兜底为 `None`。✅

---

## 三、P2 — 待后续（残留 / 增强，非本版阻断项）

#### P2-1　preprocess 列名匹配仍有残留歧义
- **文件**：`lib/preprocess.py`（列名→参数匹配双护栏 `_substring_ok`≥3 + `_match_one`≥0.6）
- **现状**：审计示例 4 例（T/温/度/烘箱温度）已 PASS；但仍有两类未闭环——
  1. **歧义坍缩**：如"区温度"在多个候选命中时静默取其一，不提示存在多个候选；
  2. **整词漏匹配**：如"电流""温度""tension"等整词在别名表缺精确条目时返回 `None`，不降级提示"未匹配"。
- **影响**：数据列识别偶发错配/漏配，下游统计用错列或漏用列。
- **建议修复**：歧义命中≥2 时显式 `degrade.note` 列出候选由人定夺；整词未匹配时回退"近似别名 + 人工确认"而非静默 `None`。
- **验收标准**：构造含歧义列与整词缺失列的样例，断言 `degrade` 台账有对应告警、且匹配结果可解释。

#### P2-2　DOE 安全门禁未在 lib 层强制
- **文件**：`lib/doe.py`（`build_design`/`decode_design` 不接收 `constraints`）
- **现状**：`safety_envelope/build_constraints/check_point` 已实现，但 lib 层 `build_design`、`decode_design` 不接收 constraints；目前仅 pipeline 层做 clamp 兜底。若有人直接调 lib 层做设计，越界点不会被拦截。
- **影响**：绕过 pipeline 直接调用 lib 时，安全包络失去强制力。
- **建议修复**：`build_design`/`decode_design` 增加可选 `constraints` 参数，越界点显式标 `out_of_envelope` 或报错；或文档明确"lib 层不担保安全，须经 pipeline/check_point"。
- **验收标准**：构造越界因子规格，`build_design(..., constraints=...)` 拒绝或标注越界点。

---

## 四、测试审查结论（范围 4）

- 既有测试覆盖**：`selftest.py`(879 项) 含大量回归断言；`audit --selftest`(152 项) 用**反向变异**验证每条护栏真能抓破坏——不是"测试通过功能仍错"，而是"注入缺陷门禁必喊"。
- 本版新增证据**：`scripts/verify_rev_fixes.py` 覆盖 4 处修复，直接注入对应场景断言（含 F4 新旧路径对照），`--check` 可接入门禁。
- 残留风险**：P2-1/P2-2 尚无专门测试守护，属已知盲区，已在上面标注验收方式。

## 五、门禁证据（本次全绿）

| 门禁 | 结果 |
|------|------|
| `selftest.py` | 879/0 |
| `fde_run.py audit` | 76/0 |
| `fde_run.py audit --selftest` | 152/0（全部变异被抓） |
| `static_scan.py` | 0 条（豁免 120 条均有 `scan:keep` 理由） |
| `prepublish_audit.py --strict` | PASS |
| `verify_rev_fixes.py --check` | 4/0 |
| 安装目录 `audit --selftest` | 152/0（sync_install 单向同步后复验） |

> 注：GitHub 推送在本次执行期间持续 502/空响应（网络层抖动），本地已提交 `437cd7c`，安装目录已同步；推送待网络恢复后执行。
