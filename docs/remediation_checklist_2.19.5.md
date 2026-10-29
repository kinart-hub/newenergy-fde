# 整改清单（重点复核 · 最新修订 v2.19.6）

> 本文件是**活文档**：文件名中的 2.19.5 为首版号，内容随修复推进持续更新到当前版本。
> 首版（2.19.5）只修了 P0/P1 就把两条 P2 标成"残留"搁下，违背"同类缺陷必须全量排查、
> 不许只修被点名的那一个"的铁律；2.19.6 已把 P2-1/P2-2 真正修掉并补上守护用例。

## 状态总览

| 编号 | 问题 | 文件 | 2.19.5 | 2.19.6 |
|------|------|------|--------|--------|
| P0-1 | 诚实降级泄漏（无数据仍吐根因榜单） | `lib/rootcause.py` | 已修复 | 保持 |
| P1-1 | 时序反证死分支 | `lib/rootcause.py` | 已修复 | 保持 |
| P1-2 | DOE 2 水平设计解不出高角点 | `lib/doe.py` | 已修复 | 保持 |
| P1-3 | crossprocess 参考量程拿不到 | `lib/crossprocess.py` | 已修复 | 保持 |
| P2-1 | 列名匹配歧义坍缩 | `lib/preprocess.py` | 残留 | **已修复** |
| P2-2 | DOE 安全门禁仅 pipeline 层兜底 | `lib/doe.py`+`lib/pipeline.py` | 残留 | **已修复** |
| — | 审计规则 45 变异注入失效（SKIP） | `scripts/fde_run.py` | 未暴露 | **已修复** |

---

## 一、上一版建议对照（范围 5，已落实并代码级复验）

| 专项 | 代码落点 | 结论 |
|------|----------|------|
| 相关性缺失值错位 | `lib/stats.py` `finite_pairs` 按原始行号 i 配对，任一侧非数值整行排除 | ✅ 已闭环 |
| 指标方向判断 | `lib/direction.py` 4 态 + `UNKNOWN` 降级为 `assumed` 并外显 | ✅ 已闭环 |
| 失败状态混淆 | `lib/status.py` 7 态，仅 `NO_FINDING` 可宣称无异常，私造状态归 `FAILED` | ✅ 已闭环 |
| 证据追溯 | `lib/provenance.py` `run_id`/`fingerprint`/`replay_check` | ✅ 已闭环 |
| DOE 安全门禁 | `lib/doe.py` `safety_envelope`/`build_constraints`/`check_point` | ⚠️ 2.19.5 仅 pipeline 层兜底 → **2.19.6 已下沉到 lib** |

外部审计 9 条（Nelson 规则 2 整段返回、Phase I 剔除>5% 判不稳、多规则溯源、自相关抑制 Cpk、
分段 σ、正态性接入、列名匹配双护栏等）已于 2.19.1~2.19.4 落实，由 `audit`(76/0) +
`audit --selftest`(152/0，全部变异被抓) 反向验证。

---

## 二、P0 — 必须修（已修复）

### P0-1　rootcause 诚实降级泄漏（无数据支撑仍吐根因榜单）
- **文件**：`lib/rootcause.py` `rank_root_causes`
- **原因**：守卫条件为 `if not suf.get("n_data_backed") and (suf.get("n_effective") or 0) >= MIN_N_FOR_RANKING:`，
  只挡住"有足够样本但无数据支撑"这一种情形。`target_col=None` 或 `n<8` 走
  `INSUFFICIENT_DATA` 时条件不成立，榜单照旧输出。
- **影响**：状态是"数据不足"，却同时给出带分数的候选排名——下游直接渲染成结论，
  等于用知识库先验冒充数据结论。这是最危险的一类错误结论来源。
- **修复**：守卫改为 `if not suf.get("n_data_backed"):`（不再看样本量），
  零数据支撑即清空 `candidates` 与 `all_candidates`，降级为 `prior_only_hints`（明确标注"先验、非数据结论"）。
- **验收**：`verify_rev_fixes.py` F1 —— `target_col=None` 与 `n<8` 两场景断言
  `candidates==0` 且 `honest_degraded` 为真、`prior_only_hints` 非空。

### 三、P1 — 应修（已修复）

#### P1-1　rootcause 时序反证死分支
- **文件**：`lib/rootcause.py` `_temporal_match` 与消费方
- **原因**：`_temporal_match` 只返 `(score, {"both_abnormal","n","ratio_pct","score"})`，
  消费方却读 `beforeafter.get("verdict") in ("不符","不支持")` —— 键根本不存在，
  分支永久为假。
- **影响**：时序反证整段失效，报告永远不会出现"时序上不支持这个根因"的减分证据，
  根因排序偏向知识库先验。
- **修复**：`_temporal_match` 显式返回 `verdict`（"支持"/"不支持"）与 `note`；
  消费方改判 `verdict == "不支持"` 并输出带比值的反证文案（同时清掉不存在的"不符"分支）。
- **验收**：F2 —— 高重合场景 `verdict="支持"`，反相场景 `verdict="不支持"` 且 `note` 非空。

#### P1-2　DOE 2 水平设计解不出高角点
- **文件**：`lib/doe.py` `decode_design`
- **原因**：水平号约定把 `v==2` 一律当中点。但 2 水平设计（`full_factorial(2^k)`、
  Taguchi L8/L16，即**默认推荐路径**）只有 1/2 两个水平号，2 就是高。
- **影响**：默认 DOE 路径只在"低/中"两档做实验，**高角点永远不被探索**，
  主效应与交互作用估计全部失真——不是显示问题，是实验设计本身错了。
- **修复**：按各因子在矩阵中出现的最高水平号区分：`max_lv<=2` → 2 即高；`max_lv>=3` → 2 为中。
- **验收**：F3 —— 2 水平矩阵解码结果含高角点值，3 水平矩阵中点仍正确（防回归）。

#### P1-3　crossprocess 参考量程拿不到（误报"参数缺失"）
- **文件**：`lib/crossprocess.py` `probe_margin`
- **原因**：查知识库用写死的空 `meta`（`_base_param_of(param_col, [{}])`），
  只能剥出裸列名，永远匹配不到参数键。
- **影响**：所有带中文别名/前缀的工艺列都拿不到参考量程，结论恒为
  "无规格不能判定"，"参数接近上下限"这条上游证据永久缺失。
- **修复**：`probe_margin` 增加 `pkey` 形参，用调用方按真实 `table_meta` 解析出的参数键查库；
  调用处传入 `pkey`。（中途一次编辑误缩进引入语法错，已还原并补语法检查。）
- **验收**：F4 —— 传真实 `pkey` 能取到 `{'lsl':100.0,'usl':200.0,...}`；
  旧路径（`pkey=None`）对照仍为 `None`，证明用例真能分辨新旧行为。

---

## 四、P2 — 2.19.6 已闭环

#### P2-1　preprocess 列名匹配歧义坍缩（2.19.5 残留 → 2.19.6 已修复）
- **文件**：`lib/preprocess.py` `_search_param` / `_match_one` / `apply_map_policy`
- **根因（比 2.19.5 的判断更深一层）**：不只是"取第一个"，而是 `_search_param`
  在返回前就把多个并列候选**坍缩成单一最佳**——`_match_one` 拿到时只剩 1 个候选，
  到那一层再做歧义检测已经看不到歧义了。治标改法（只在 `_match_one` 加判断）无效。
- **影响**：「区温度」同时命中一/二/三/末区温度（各 0.75）时静默取其一，
  数据被挂到**错误的工艺参数**上做统计与归因，结论指向错误参数。
  注：`"温度"/"电流"/"tension"` 这类整词**返回 None 是正确行为**——它们本身是多义上位词，
  强行绑到某个参数才是过匹配 bug；此处不改。
- **修复**：`_search_param` 返回**全部候选**（保留原 4 元组顺序、第 5 项为候选列表，
  不破坏 `selftest`/`audit` 依赖的 `[0]` 与 `best_score >= 0.6` 判据）；
  `_match_one` 合并跨作用域候选后判歧义（第二高分与最高分差 ≤ `AMBIGUITY_MARGIN=0.15`
  且候选 ≥2 → `ambiguous=True`、置信降为 low）；
  `apply_map_policy` 把歧义列与低置信同权处置（auto 标红、block 判未匹配）。
- **验收**：F5 「区温度」→ `ambiguous=True`、`candidates>=2`、`conf=low`；
  **F5b 阴性对照**「一区温度」→ `ambiguous=False`、唯一键、`conf=high`
  （证明判据不会把明确列名误判成歧义——没有这条，F5 通过也可能是"全判歧义"的假绿）。

#### P2-2　DOE 安全门禁仅 pipeline 层兜底（2.19.5 残留 → 2.19.6 已下沉 lib）
- **文件**：`lib/doe.py` `decode_design` + `lib/pipeline.py` 调用处
- **原因**：`safety_envelope`/`build_constraints`/`check_point` 已实现，但只被
  `optimize_rsm_constrained` 用到；`build_design`/`decode_design` 不接收 `constraints`，
  pipeline 靠"先 clamp 因子再 build"保安全。
- **影响**：绕过 pipeline 直调 lib（脚本化、二次开发、外部集成）时，越界实验点不会被拦截，
  安全包络形同虚设——安全约束必须落在最靠近动作的那一层。
- **修复**：`decode_design(matrix, factor_specs, constraints=None)`，传入 `constraints`
  后逐点过 `check_point`，越界点挂 `_safety_ok=False` + `_safety_violations`；
  pipeline 传入已算好的 `_cons` 并把越界运行号汇总进 warnings（纵深防御）。
  不传 `constraints` 时行为不变（不挂字段），避免对既有调用方产生误报。
- **验收**：F6 —— 高角点 5.0V 超 4.5V 上限被标记 `_safety_ok=False` 且列出违规项；
  不传 `constraints` 时记录上**无** `_safety_*` 字段（阴性对照）。

#### 附：审计规则 45 变异注入失效（2.19.6 暴露并修复）
- **文件**：`scripts/fde_run.py` 规则 45「字段映射必须结构化且低置信可阻断」
- **现象**：2.19.6 重构 `apply_map_policy` 后，`audit --selftest` 由 152/0 退化为
  **151/0 + 1 个 SKIP**——变异体目标串 `if policy == "block" and conf == "low":` 已不存在，
  注入失败，自检记 SKIP。
- **危害**：SKIP 比 FAIL 更危险，它让门禁看起来是好的，而这条规则的判别力实际没被验证过。
- **修复**：① 判据加**不变量** `if policy == "block":` 之后必须紧跟 `key = None`
  （只认"block 真的清空 key"这个动作，不钉死字样）；② 变异体目标改到该动作上
  （`key = None` → `pass`，低置信仍被采用）。
- **验收**：`audit --selftest` 恢复 152/0/**0 SKIP**，且该规则 3 个变异全部被抓。

---

## 五、测试审查结论（范围 4）

- **既有测试不是假绿**：`selftest.py`(879 项) 含大量回归断言；`audit --selftest`(152 项)
  用**反向变异**——注入缺陷后门禁必须喊。这正好回答"测试通过但功能仍错"的担忧：
  判别力是被验证过的，不是只看通过数。
- **但 2.19.5 存在真实盲区**：P2-1/P2-2 无专门用例守护，故标为"残留"时不受任何门禁约束。
  2.19.6 已补齐 F5/F5b/F6 三条（含两条阴性对照），并全部纳入 `verify_rev_fixes.py --check`。
- **本轮另抓到门禁自身退化**（规则 45 SKIP），说明"跑门禁"必须看 **SKIP 计数**，
  不能只看"失败 0"。
- **判据分辨力自查**：F4 保留新旧路径对照、F5 配 F5b 阴性对照、F6 配"不传约束不挂字段"对照
  ——每条都验过"去掉修复后用例会翻"，不是碰巧通过。

## 六、门禁证据（v2.19.6 全绿）

**权威门禁**：`python scripts/fde_run.py verify` —— **8 步全过 / 0 失败 / 0 跳过**（总 264.8s）：

| 步骤 | 结果 |
|------|------|
| 1. 约定规则反向验证 | OK 5.1s |
| 2. 主自检 | OK 72.9s |
| 3. 护栏自检 | OK 7.6s |
| 4. 审计规则自检 | OK 0.9s |
| 5. 静态全库扫描 | OK 20.0s |
| 6. 主流程接线端到端 | OK 6.5s |
| 7. 上架前隐私审计 | OK 77.0s |
| 8. 打包完整性 | OK 74.9s |

各单项明细：

| 门禁 | 结果 |
|------|------|
| `selftest.py` | 879 通过 / 0 失败 |
| `fde_run.py audit` | 76 通过 / 0 失败 / 0 跳过 |
| `fde_run.py audit --selftest` | 152 通过 / 0 失败 / **0 跳过**（全部变异被抓） |
| `static_scan.py` | S1–S11 全 0 条（豁免项均有 `scan:keep` 理由） |
| `prepublish_audit.py --strict` | 阻断 0 / 提醒 0 → PASS |
| `verify_rev_fixes.py --check` | 7 项 / 0 失败（F1–F6 含 F5b/F6 阴性对照） |
| 安装目录 `audit --selftest` | 152/0/0（`sync_install` 单向同步后复验） |
| 安装目录 `selftest` | 879/0 |
| `gen_manifest.py` | 必需 81 项（lib 40 / scripts 18 / knowledge 12），版本同步 2.19.6 |

> 门禁口径说明：本轮一度只看"失败 0"就以为通过，实际 `audit --selftest` 当时是
> 151/0 + **1 个 SKIP**（规则 45 变异体失效）。**汇报一律写"通过/失败/跳过"三格**。

## 七、剩余风险（如实登记，非本版可解）

1. **知识库覆盖度依赖外部口径**：缺陷-参数映射的完整性只能靠公开分类口径做差集校准，
   现场适用性仍需复核（条目标 `source` + "适用性待复核"note）。
2. **DOE 安全包络的边界值来源**：`build_constraints` 取的是传入的规格/设备能力，
   规格本身不准则安全门禁只会放大误差——门禁保证"不越已声明的界"，不保证"界是对的"。
3. **`build_design` 仍未接 `constraints`**：解码侧已强制，生成侧仍靠因子 clamp
   （生成侧只产水平矩阵、不产物理值，越界风险落在解码侧，已在解码侧拦住）。
4. **GitHub 推送**：执行期间主站间歇 502/空响应，本地提交与安装目录同步均已完成，
   推送在恢复后补做。
