# -*- coding: utf-8 -*-
"""
pipeline.py —— 工艺数据分析主流水线

用法：
  python -m lib.pipeline --input a.csv --outdir out
  python -m lib.pipeline --input a.xlsx --target 不良率 --process battery_coating --outdir out

流程：
  读取 → 列名标准化/工序识别 → 清洗 → 目标列与分组维度识别
      → SPC(控制图/判异/能力) → 相关性(矩阵/关联排序/滞后/VIF)
      → 不良 Pareto → 根因排序 + 5Why → 良率损失拆解 → DOE 方案 → 数据缺口
      → 输出 Markdown + HTML + 图表 + JSON 摘要
"""

import os
import sys

try:
    from . import degrade          # 包模式（python -m lib.pipeline）
except Exception:                  # 脚本模式（python lib/pipeline.py）无包上下文
    import degrade                 # noqa

# Windows 上 stdout 被重定向到文件/管道时（CI、> log.txt）Python 会切到
# 系统 ANSI 编码，英文 runner 上是 cp1252，输出中文会抛 UnicodeEncodeError。
# 强制 UTF-8，保证任何 Windows 环境下 CLI 都不因中文崩掉。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    # 非 Windows、或 stdout 已被外部替换时没有 reconfigure，属正常分支。
    # 这里刻意不记降级台账：本行在模块加载期执行，那时 degrade 还没导入，
    # 而且把环境差异记成"降级"会污染每一次分析的台账。
    # scan:keep 模块加载期执行、degrade 尚未导入，且非 Windows 无 reconfigure 属正常分支，不是降级。
    pass
import io
import json
import re
import argparse
import datetime
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from lib import table as T, stats as S, preprocess as PP, spc as SPC, \
        correlation as CORR, doe as DOE, rootcause as RC, charts as CH, report as RPT, \
        kbstore as KBS, devices as DEV, crossprocess as XP, actions as AC, \
        adapt as AD, quality_guard as QG, status as ST, provenance as PROV, contract as CT
    try:                    # P2-4 可观测性；导入失败不能拖垮整条流水线
        from lib import observ as OBS
    except Exception:
        OBS = None
    try:                    # 产品档配置解析；失败只影响"自动配默认值"，不影响主流程
        from lib import profile as PF
    except Exception:
        PF = None
    try:                    # v1.11 新增模块，导入失败不能拖垮整条流水线
        from lib import events as EV
        _HAS_EVENTS = True
    except Exception:
        EV = None
        _HAS_EVENTS = False
    try:                    # P0-2 测量系统分析；失败只丢掉"结论能不能信"的门槛，不拖垮流水线
        from lib import msa as MSA
    except Exception:
        MSA = None
    try:                    # P1-2 原生 Word/PDF 导出；失败只少两种交付形态
        from lib import export_doc as EX
    except Exception:
        EX = None
    try:                    # v1.13 8D 引擎
        from lib import eightd as EV8D
        _HAS_8D = True
    except Exception:
        EV8D = None
        _HAS_8D = False
else:
    from . import table as T, stats as S, preprocess as PP, spc as SPC, \
        correlation as CORR, doe as DOE, rootcause as RC, charts as CH, report as RPT, \
        kbstore as KBS, devices as DEV, crossprocess as XP, actions as AC, \
        adapt as AD, quality_guard as QG, status as ST, provenance as PROV, contract as CT
    try:                    # P2-4 可观测性；导入失败不能拖垮整条流水线
        from . import observ as OBS
    except Exception:
        OBS = None
    try:                    # 产品档配置解析；失败只影响"自动配默认值"，不影响主流程
        from . import profile as PF
    except Exception:
        PF = None
    try:                    # P0-2 测量系统分析（同上）
        from . import msa as MSA
    except Exception:
        MSA = None
    try:                    # P1-2 原生 Word/PDF 导出（两个分支都要，只改一套会 NameError）
        from . import export_doc as EX
    except Exception:
        EX = None
    try:
        from . import events as EV
        _HAS_EVENTS = True
    except Exception:
        EV = None
        _HAS_EVENTS = False
    try:
        from . import eightd as EV8D
        _HAS_8D = True
    except Exception:
        EV8D = None
        _HAS_8D = False

if EV is not None and EV.__name__ != "lib.events":
    # 以脚本方式（python lib/pipeline.py）运行时，events 会被加载成顶层模块
    # 而不是 lib 包的子模块。此时 quality_guard 存在两份实例，函数本身是纯函数
    # 不影响结果，但为了让流水线各层拿到同一个 QG，这里把 lib 版挂过去。
    try:
        import lib.quality_guard as _QG_PKG
        EV.QG = _QG_PKG
    except Exception as _dg_e:
        degrade.note("pipeline.module@L78", _dg_e)

KB = PP.load_knowledge()
_fmt = RPT._fmt

ANALYZER_VERSION = "2.8.0"


# 空数据排查清单。
#
# 为什么必须显式报错：实测过四种空数据输入（0 字节文件、只有表头、
# 列名全无意义、数值列全空），原实现**全部照常出报告**，
# summary 里写着「SPC 判异：未检出违反 Nelson 规则的参数」——
# 读起来像「检查过了、没问题」，实际是「一行数据都没有」。
# 把「没有输入」包装成「没有结论」，是静默错结论里最容易被信以为真的一类：
# 使用者拿到手的不是报错，而是一份看起来正常的报告。
#
# 所以空数据不降级、不兜底，直接停。停的时候给的是「下一步做什么」，
# 不是「出了什么事」。
_EMPTY_CHECKLIST = [
    "文件编码：UTF-8 with BOM 与 GBK 混用会让整份文件读成空或乱码。"
    "先用记事本打开确认能看到正常中文，另存为「UTF-8」后再试。",
    "分隔符：文件实际是分号或制表符分隔、扩展名却写成 .csv 时，"
    "整行会被当成一列。加 --verbose 看第 1 阶段读到几列——只有 1 列就是这个原因。",
    "表头行：设备导出的文件常在真正的表头上方带几行标题或批次说明，"
    "解析会把说明行当成表头。用文本编辑器确认第 1 行就是列名。",
    "过滤条件：--sheet 指定的工作表不对（Excel 多表文件常见），"
    "或数据行全部落在被剔除的统计尾行、单位行里。",
]


def _empty_data_error(stage, detail_lines=None, files=None):
    """空数据统一报错出口。

    stage        —— 'read'（读取后无数据行）/ 'clean'（清洗后归零）
    detail_lines—— 阶段特定的附加说明，排在排查清单前面
    files       —— 涉及的文件（read 阶段逐个列出读到了什么）

    返回值结构与 analyze() 的其它错误一致（error + detail），
    另加 checklist 供 CLI 逐条打印。
    """
    head = {
        "read": "没有读到任何数据行",
        "clean": "清洗后剩余 0 行，无法进行任何统计判定",
    }.get(stage, "所有数值列都没有任何有效观测值")
    msg = "数据为空，分析终止：%s。" % head
    out = {"error": msg, "checklist": list(_EMPTY_CHECKLIST)}
    if detail_lines:
        out["hints"] = list(detail_lines)   # 阶段特定的先说，通用排查方向垫后
    if files:
        out["detail"] = files
    out["empty_data"] = True
    out["empty_stage"] = stage
    return out


def _kb_stamp():
    """知识库版本戳。
    报告里必须写明用的是哪一版知识库：半年后回看这份报告，如果不知道当时
    的规则版本，就没法判断结论是被后来的知识改动影响了，还是本来就那样。"""
    try:
        idx_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "knowledge", "index.json")
        with io.open(idx_p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return "%s（更新于 %s）" % (d.get("doc_version") or "未知",
                                   str(d.get("updated") or "未知")[:10])
    except Exception:
        return "未知"


# ------------------------------------------------------------------ 识别

TARGET_HINTS = ["不良率", "不良", "defect", "failure", "fail", "ng", "废品率", "报废",
                "yield", "良率", "良品率", "容量", "capacity", "pmax", "功率", "效率",
                "efficiency", "eta", "内阻", "acir", "k值", "k_value", "ttv", "隐裂率"]

GROUP_HINTS = ["设备", "机台", "machine", "机号", "线体", "line", "产线", "班次", "shift",
               "批次", "batch", "lot", "卷号", "供应商", "supplier", "通道", "channel",
               "柜号", "柜", "模", "腔", "刀具", "刀号", "tool", "班组", "工单", "订单"]

JUDGE_HINTS = ["判定", "结果", "result", "judge", "ok", "ng", "pass", "结论", "等级", "grade"]
# 判定列取值的模糊识别词。顺序敏感：NG 词必须排在 OK 词之前判断。
_NG_WORDS = ("NG", "FAIL", "不合格", "不良", "降级", "降档", "返修", "报废", "次品",
             "REJECT", "REWORK", "SCRAP", "DEFECT")
_OK_WORDS = ("OK", "PASS", "合格", "良品", "良", "GOOD", "GO")


def _measure_cols(table):
    """只有「测量」列才有资格被自动选为目标（排除编号、时间、坐标、计数列）"""
    return [c for c in table.numeric_columns()
            if QG.column_role(c, table.column(c)) == "measure"]


_BAD_TARGET_WORDS = ("不良", "缺陷", "计数", "ng", "报废", "返工", "reject", "defect",
                    "scrap", "rework", "失效")

# 分档/定级标签：永远由测量结果本身算出来，与目标相关是必然的，不构成因果。
# 「P540 档功率低」这种结论和「功率低所以功率低」没有区别。
_BIN_WORDS = ("档位", "档", "等级", "级别", "分级", "grade", "bin", "band",
              "bincode", "class")


def _is_bin_label(col):
    n = PP._norm_name(col)
    return any(w == n or w in n for w in _BIN_WORDS)


def _target_cols(table):
    """有资格当分析目标的列。

    只留「测量」列不够：缺陷计数列恰恰是最常见的分析目标（Pareto 就是按它做的）。
    一刀切排除掉，EL 缺陷表、卷绕不良表会选不出任何目标。
    所以：测量列 + 名字带质量语义的计数列。
    """
    out = list(_measure_cols(table))
    for c in table.numeric_columns():
        if c in out:
            continue
        if QG.column_role(c, table.column(c)) == "count":
            n = PP._norm_name(c)
            if any(w in n for w in _BAD_TARGET_WORDS):
                out.append(c)
    return out


def pick_target(table, user_target=None, spec_overrides=None, col_map=None):
    if user_target:
        if table.col_index(user_target) is not None:
            return user_target
        for c in table.columns:
            if PP._norm_name(c) == PP._norm_name(user_target):
                return c
        return None
    # 给了企业规格书时，优先把"确实在超规格"的那列当分析目标。
    # 否则会挑一个字面像指标、实际完全正常的列（例如卷绕的 Overhang），
    # 后续相关性全围着它转，等于整份报告白做。
    if spec_overrides:
        best_oos, best_col = 0, None
        for c in _target_cols(table):
            ov = PP.match_spec_key(c, col_map or {}, spec_overrides)
            if not ov:
                continue
            lsl, usl = ov.get("lsl"), ov.get("usl")
            if lsl is None and usl is None:
                continue
            vals = [v for v in table.numeric_column(c) if v is not None]
            if len(vals) < 10:
                continue
            oos = sum(1 for v in vals
                      if (lsl is not None and v < lsl) or (usl is not None and v > usl))
            if oos > best_oos:
                best_oos, best_col = oos, c
        if best_col:
            return best_col
    # v1.11：字面匹配不上时不能按"变异系数最大"兜底（CV 最大的往往是坐标、序号或某个旋钮），
    # 改按"响应优先 + 可解释变异"挑。且**词表不能再单独决定**——否则像 TTV 这种
    # 名字在词表里、实际只是噪声的列会压过真正有结构的质量特性（硅片的少子寿命）。
    _pick, _detail = QG.pick_response_column(table, cols=_target_cols(table),
                                             col_map=col_map)
    _rank = (_detail or {}).get("ranking") or {}
    best_col, best_key = None, None
    for c in _target_cols(table):
        n = PP._norm_name(c)
        hint = 0
        for i, h in enumerate(TARGET_HINTS):
            if PP._norm_name(h) in n or n in PP._norm_name(h):
                hint = max(hint, (len(TARGET_HINTS) - i) / float(len(TARGET_HINTS)))
        key = (0.6 * 6.0 * hint) + float(_rank.get(c, {}).get("score") or 0.0)
        if best_key is None or key > best_key:
            best_col, best_key = c, key
    if best_col:
        return best_col
    # 最后兜底：仍然用变异系数，但范围已限定在目标候选列里
    cands = []
    for c in _target_cols(table):
        v = [x for x in table.numeric_column(c) if x is not None]
        if len(v) >= 10:
            m = S.mean(v)
            sd = S.stdev(v)
            if m and sd:
                cands.append((abs(sd / m), c))
    cands.sort(reverse=True)
    return cands[0][1] if cands else None


def pick_groups(table):
    """识别分组维度（机台/班次/通道/供应商…）。

    只靠词表会漏：企业有卷针号、夹具号、工位号、模具号这类五花八门的设备编号，
    词表不可能穷举。所以再加一条数据特征——**低基数文本列**天然就是分组维度。
    同时要挡掉"每行一个号"的流水 ID（卷芯编号、电芯编号），那种分组没有统计意义。
    """
    out = []
    nrow = len(table.rows) or 1
    texts = set(table.text_columns() or [])
    for c in table.columns:
        # 内部衍生列不是原始观测，绝不能当分组维度：
        # __判定_NG__ 是判定列编码出来的结果列，拿它分组必然"差异巨大"，
        # 于是它作为根因候选高居榜单（E06 实测排第三），
        # 而那只是"不合格组的不合格率更高"这种同义反复。
        if QG.is_internal_column(c):
            continue
        n = PP._norm_name(c)
        vals = [v for v in table.column(c) if v not in (None, "")]
        n_uniq = len(set(str(v) for v in vals))
        if not (1 < n_uniq <= 60):
            continue
        hit = any(PP._norm_name(h) in n for h in GROUP_HINTS)
        if not hit:
            if c not in texts:
                continue
            if n_uniq > nrow * 0.4:          # 取值太散，像流水号而不是分组
                continue
        if n_uniq > nrow * 0.6 and not hit:  # 每行几乎都不同 -> ID 列，不是维度
            continue
        out.append(c)
    return out


def encode_judgement(table):
    """把 OK/NG 判定列编码为 1/0 数值列，返回新列名"""
    for c in table.columns:
        n = PP._norm_name(c)
        if not any(PP._norm_name(h) == n or PP._norm_name(h) in n for h in JUDGE_HINTS):
            continue
        vals = [str(v).strip().upper() if v is not None else "" for v in table.column(c)]
        uniq = set(vals)
        if uniq & {"OK", "NG", "PASS", "FAIL", "合格", "不合格", "良", "不良", "1", "0", "Y", "N"}:
            newcol = "__判定_NG__"
            enc = []
            for v in vals:
                # 精确值优先，其次包含匹配。
                # 企业判定列的取值五花八门（「自放电不良」「容量不良」「降级」），
                # 只认固定几个词会导致整列编成 0，后续相关性/根因全部失效。
                # 注意顺序必须先 NG 后 OK：「不合格」里含有「合格」两个字。
                if v in ("NG", "FAIL", "不合格", "不良", "N", "0"):
                    enc.append(1.0)
                elif v in ("OK", "PASS", "合格", "良", "Y", "1"):
                    enc.append(0.0)
                elif any(w in v for w in _NG_WORDS):
                    enc.append(1.0)
                elif any(w in v for w in _OK_WORDS):
                    enc.append(0.0)
                else:
                    enc.append(None)
            if any(e is not None for e in enc):
                table.add_column(newcol, enc)
                return newcol
    return None


# ------------------------------------------------------------------ 主流程

def analyze(paths, outdir="output", target=None, process=None, defect=None,
            sheet=None, spec_overrides=None, do_doe=True, max_charts=12,
            auto_adapt=False, tenant=None, industry=None, rules=None,
            spc_exclusion_mode=None,
            doe_replicates=None, doe_blocks=None, doe_seed=None, boundary_policy=None,
            role=None, controlled_spec=None,
            msa_spec=None, export_xlsx=True, export_docx=True, export_pdf=True,
            d2_card=None, emit_d2_card=True, verbose=False, views=None):
    """
    auto_adapt —— 字段认不出来时，把"新别名/新参数"自动登记到**租户层**并当场生效。
                  默认关闭：知识库条目还是应该过一遍人。开了也不会碰到基线。
    tenant     —— 企业标识；给了就启用隔离，所有写库只落该租户的私有层。
    industry   —— 行业作用域；给了就只在本行业的参数/设备/案例内检索。
                  与 tenant 是正交的两维：企业隔离管"谁的知识"，行业作用域管"哪一行"。
    rules      —— 启用的 Nelson 判异规则，如 "1,2,3,5" / "basic" / "all"。
                  默认只开基本集：八条全开会误报泛滥，现场很快就不看报警了。
    export_xlsx—— 额外导出可在 Excel 打开的结果表（判异明细含源文件与行号）。
    export_docx/export_pdf —— 原生 Word/PDF 导出（零第三方依赖，自写 OOXML 与 PDF 1.4）。
                  报告要进 8D 系统或发客户时用它，省掉一次手工转换。
                  默认都开；PDF 不嵌入字体（见 lib/export_doc.py 的说明）。
    d2_card    —— 已填写的 8D D2 录入卡路径。给了就把 5W2H 与数据做交叉校验，
                  并据此渲染完整 D1–D8；不给则 D2 全空并在报告中标记「不具备对外条件」。
    emit_d2_card—— 是否随报告生成空白 D2 录入卡（默认生成）。
    verbose     —— 逐阶段把「这份数据被理解成了什么」打到 stdout。
                   失败时使用者最需要的是「卡在第几步、数据被读成了什么样」，
                   而不是一句 ERROR。默认关闭以免刷屏。
    views       —— 角色视角：process/shopfloor/quality/production/mgmt，或 ["all"]。
                   视角层只做筛选与措辞，不重算——五类使用者各拿一份能直接用的东西，
                   而不是都在同一份大而全的报告里自己翻。
    msa_spec    —— 测量系统（Gauge R&R）登记 dict。三种给法：
                     {"path": "msa.csv"}                       读专用 MSA 表，自动认列
                     {"path": ..., "part/appraiser/value": 列名}   显式指定列
                     {"pct_grr": 18.0, "ndc": 4}              直接登记已有 GRR 结论
                   给不出就不登记——**未登记不等于合格**：那时结论会带上
                   "未验证测量系统"的前置提示，而不是悄悄当作尺子准。
    """
    try:
        from . import tenancy as TN
    except Exception:
        import tenancy as TN                         # noqa
    try:
        from . import scope as SC
    except Exception:
        import scope as SC                           # noqa
    need_reload = False
    if tenant:
        TN.set_tenant(tenant)
        need_reload = True
        # 命令行没指定行业时，沿用这家企业在租户配置里买的行业模块
        if not industry:
            industry = TN.read_config().get("industries")
        # 关键：只要切了企业就**必须**重设作用域。作用域是全局状态，
        # 不重设的话上一家企业买的行业会泄漏给下一家——A 配了锂电，
        # B 的光伏数据就会被锂电作用域裁掉，直接判成"未知工序"。
        # 没配置行业时传 None，等价于清空作用域。
        SC.set_industries(industry)
    elif industry:
        # 拼错的行业名由 set_industries 记进 notes()，报告 2c 节会显示
        SC.set_industries(industry)
        need_reload = True
    if need_reload:
        try:
            from . import devices as _DEV
            _DEV.invalidate_cache()
        except Exception as _dg_e:
            degrade.note("pipeline.analyze@L375", _dg_e)
        PP._KNOWLEDGE = None
        PP._KNOWLEDGE_SCOPE = "__unset__"
        # 原地更新而不是重新绑定：KB 是模块级全局，被本函数各处直接引用，
        # 在函数里赋值会变成局部变量导致 UnboundLocalError
        KB.clear()
        KB.update(PP.load_knowledge())
    os.makedirs(outdir, exist_ok=True)
    result = {"inputs": paths, "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
              "analyzer_version": ANALYZER_VERSION,
              # 8D 录入卡相关：build_report 里要用，但它的签名不带这些，
              # 所以挂在 result 上传过去（改签名会牵动所有调用点，不值）
              "outdir": outdir, "d2_card_path": d2_card, "emit_d2_card": emit_d2_card}

    # 数据血缘（P1-10）。此前这里只有 inputs+decisions 两样，
    # 够回答"读了哪个文件"，回答不了"喂的是什么内容、当时用的哪套规则"。
    # 现在把同一份 decisions 直接挂进血缘记录（同一个 list 对象，不是拷贝），
    # 保证"做了哪些自动决定"在报告和血缘里是同一个事实源——两边各记一份，
    # 迟早会出现报告写了、血缘没写的分裂。
    result["decisions"] = []

    # ------------------------------------------------------------------
    # 产品档配置解析：多产品租户的默认值怎么合并出一套能用的
    #
    # 只在使用人**没有显式指定**时补位——他写死的值一律优先，
    # 角色视角也不得推翻（优先级 显式 > 角色 > 产品 > 基线）。
    # 未指定产品、也没配产品档时，resolve 返回空，各参数回落基线，行为与原来一致。
    _prods = [p for p in (industry if isinstance(industry, (list, tuple))
                          else ([industry] if industry else [])) if p]
    _explicit = {}
    for _k, _v in (("spc_exclusion_mode", spc_exclusion_mode),
                   ("boundary_policy", boundary_policy),
                   ("doe_replicates", doe_replicates),
                   ("doe_blocks", doe_blocks),
                   ("doe_seed", doe_seed),
                   ("rules", rules),
                   ("target", target), ("process", process), ("defect", defect)):
        if _v not in (None, ""):
            _explicit[_k] = _v
    _cfg = {}
    _cfg_needs, _cfg_trace = [], []
    _ev = None
    if PF is not None:
        try:
            # 受控规格登记：企业给了受控文件就登记进来，边界政策由它推导，
            # 不需要使用人在命令行上猜什么时候该收紧。
            _specs_in = PF.load_controlled_spec(controlled_spec)
            if _specs_in is None:
                # 读不出来≠没给：让它出声，否则政策会悄悄松掉还无人知晓
                _specs_in = None
                result["decisions"].append({
                    "kind": "controlled_spec_unreadable",
                    "detail": "受控规格登记 %r 无法解析：本次不接受该登记，"
                              "边界政策按产品档内登记推导（可能比实际松）" % (controlled_spec,),
                })
                (result.setdefault("warnings", [])).append(
                    "受控规格登记 %r 无法解析，未纳入本次边界判定："
                    "请核对路径与 JSON 格式，否则政策会按「未登记」处理" % (controlled_spec,))
            _res = PF.resolve_config(
                products=_prods, role=role, explicit=_explicit,
                baseline={"spc_exclusion_mode": "auto", "boundary_policy": "warn",
                          "doe_replicates": 1, "doe_blocks": 1, "doe_seed": 0},
                controlled_specs=_specs_in)
            _cfg = _res.get("config") or {}
            _cfg_needs = _res.get("needs_decision") or []
            _cfg_trace = _res.get("trace") or []
            _ev = _res.get("boundary_evidence") or None
        except Exception as _cfg_e:
            # 配不出来不能静默：静默的后果是"以为按产品配好了，其实全回落基线"，
            # 而回落基线比报错更危险——报告照样出，只是判严尺度悄悄松了。
            degrade.note("pipeline.analyze@profile_resolve", _cfg_e)
            _cfg = {}
            result["decisions"].append({
                "kind": "config_resolve_failed",
                "detail": "产品档配置解析失败，本次全部回落基线默认值：%s" % _cfg_e,
            })
            (result.setdefault("warnings", [])).append(
                "产品档配置解析失败（%s）：本次参数全部回落基线默认值，"
                "判严尺度可能比产品档要求的松" % _cfg_e)
    spc_exclusion_mode = spc_exclusion_mode or _cfg.get("spc_exclusion_mode") or "auto"
    boundary_policy = boundary_policy or _cfg.get("boundary_policy") or "warn"
    doe_replicates = doe_replicates if doe_replicates is not None \
        else int(_cfg.get("doe_replicates") or 1)
    doe_blocks = doe_blocks if doe_blocks is not None else int(_cfg.get("doe_blocks") or 1)
    doe_seed = doe_seed if doe_seed is not None else int(_cfg.get("doe_seed") or 0)
    if not rules and _cfg.get("rules"):
        rules = _cfg.get("rules")
    # 关注对象同理：产品档给了默认值且使用人没指定时才用，
    # 两个产品给了不同关注对象且使用人没指定 → 不猜，留空并进 needs_decision
    if not target and _cfg.get("target"):
        target = _cfg.get("target")
    if not process and _cfg.get("process"):
        process = _cfg.get("process")
    if not defect and _cfg.get("defect"):
        defect = _cfg.get("defect")
    if _prods or role or _cfg_trace:
        result["config_profile"] = {
            "products": _prods, "role": role,
            "resolved": {k: v for k, v in _cfg.items()},
            "needs_decision": [{"param": n.get("param"),
                                "candidates": n.get("candidates"),
                                "reason": n.get("reason")} for n in _cfg_needs],
            "trace": [{"param": t.get("param"), "value": t.get("value"),
                       "semantics": t.get("semantics"), "from": t.get("from")}
                      for t in _cfg_trace],
            "boundary_evidence": _ev,
            "note": "配置按参数语义合并（取严/取并集/取大），与产品书写顺序无关；"
                    "使用人显式指定的值优先级最高，角色视角不覆盖它；"
                    "边界政策不在此列——它由受控规格证据推导，不是选项",
        }
    if _ev and _ev.get("policy") == "block" and "boundary_policy" not in _explicit:
        result["decisions"].append({
            "kind": "boundary_policy_escalated",
            "detail": "边界政策已按受控规格证据自动收紧为 block：%s" % _ev.get("reason"),
        })
    for _n in _cfg_needs:
        result["decisions"].append({
            "kind": "config_needs_decision",
            "detail": "参数 %s 各来源不一致且无法自动判定，未采用任何候选，需使用人指定（%s）"
                      % (_n.get("param"), _n.get("reason")),
        })

    # P2-4 可观测性：日志跟着本次输出走，不写脚本自身目录，
    # 也不落进用户的个人目录——交付时它是可带走、可发出去的东西，必须先脱敏。
    if OBS is not None:
        try:
            OBS.set_log_path(os.path.join(outdir, "_observ.jsonl"))
            OBS.start_run(n_inputs=len(paths) if isinstance(paths, (list, tuple)) else 1,
                          version=ANALYZER_VERSION)
        except Exception as _obs_e:
            degrade.note("pipeline.analyze@observ_start", _obs_e)
    _plist = paths if isinstance(paths, (list, tuple)) else [paths]
    try:
        _inputs = PROV.input_fingerprints(_plist)
    except Exception as _pv_e:
        degrade.note("pipeline.analyze@provenance_inputs", _pv_e)
        _inputs = []
    _spec_src = None
    if isinstance(spec_overrides, dict):
        _spec_src = {"keys": sorted(spec_overrides.keys())[:40],
                     "n_keys": len(spec_overrides)}
    elif spec_overrides:
        _spec_src = {"path": str(spec_overrides)}
    _prov = PROV.new_run(
        inputs=_inputs,
        config={"target": target, "process": process, "defect": defect,
                "sheet": sheet, "do_doe": do_doe, "rules": rules or None,
                "auto_adapt": auto_adapt, "views": list(views or []),
                "industry": industry, "spec": _spec_src},
        versions={"analyzer": ANALYZER_VERSION, "knowledge": _kb_stamp(),
                  "python": sys.version.split()[0], "platform": sys.platform},
        tenant=tenant, process=process, target=target)
    _prov["decisions"] = result["decisions"]
    result["provenance"] = _prov
    result["run_manifest"] = {"inputs": _inputs, "decisions": result["decisions"]}
    try:
        from . import tenancy as _TN
    except Exception:
        import tenancy as _TN                        # noqa
    if _TN.current():
        result["tenant"] = _TN.current()
    if SC.active():
        result["industry"] = SC.current()
        result["industry_name"] = SC.describe()
        result["_scope_stat"] = SC.last_stat()

    # 1. 读取 + 设备识别 + 版式归一（不同厂商导出的差异在这一步抹平）

    def _v(msg):
        if verbose:
            try:
                print(msg)
            except Exception as _dg_e:
                degrade.note("pipeline.analyze@L422", _dg_e)

    tables = []
    device_reports = []
    _empty_files = []      # 读到了文件但一行数据都没有（与"读不了"是两回事）

    # ------------------------------------------------------------------
    # 各护栏环节的输出变量：一律先给默认值，再让后面的 try 去覆盖。
    #
    #    为什么必须前置：每一节都包在自己的 try 里，输出变量是在 try 内部赋值的，
    #    而 build_report 在全部 try 外面统一引用它们。
    #    某一节真的失败时变量从未赋值，整轮会以 UnboundLocalError 收场，
    #    报错指向这个变量名，真正的原因（那一节记录在 degraded_sections 里的错误）
    #    被彻底盖住 —— 使用者去 debug 一个根本没坏的变量。
    #
    #    实测：注入让 corr_matrix / rank_root_causes 崩溃，整轮都以
    #    UnboundLocalError 结束，错误文本一个字都没提真实原因。
    #    AST 全量排查过 analyze() 内 22 处同型风险，集中在这里一次性兜住。
    # ------------------------------------------------------------------
    tb = None
    main = None
    col_map = {}
    spc_res = None
    assoc = []
    corr = None
    lags = []
    strong = []
    gcomp = []
    pare = None
    root = None
    gaps = None
    conf = None
    doe_plan = None
    tcol = None
    times = None
    for p in paths:
        try:
            ing = DEV.ingest(p, sheet=sheet)
            tb = ing["table"]
            if tb is None or len(tb.rows) == 0:
                # 归一后端到零行 -> 退回裸读一次。某些版式只有裸读能读出内容。
                tb = T.read_any(p, sheet)
                if isinstance(tb, list):
                    tb = tb[0]
            if tb is None or len(tb.rows) == 0:
                # 裸读也读不到数据行 —— 这时不能"降级继续跑"。
                # 原注释写的是"总比直接报错强"，实测证明这个判断是错的：
                # 继续跑的产物是一份写着「未检出异常」的报告，使用者会当成结论。
                _nc = len(tb.columns) if tb is not None else 0
                result.setdefault("read_errors", []).append({
                    "file": p,
                    "error": ("只有表头、没有数据行（读到 %d 列）" % _nc) if _nc
                             else "空文件：读不到任何内容（0 字节或无法解析）",
                })
                _empty_files.append(p)
                continue
            tb.name = os.path.splitext(os.path.basename(p))[0]
            # 行级追溯：记下每行在源文件里的行号。质量审计要能顺着报告里的
            # "第 N 点异常"回到原始文件核对，否则结论无法被验证。
            tb.attach_source(p)
            # 版式归一（宽转长/透视还原）会让一个原始行摊成多行，此时行号只是近似对应
            _layout_note = " ".join([str(x) for x in (ing.get("notes") or [])]) + \
                           " " + str(ing.get("profile_note") or "")
            tb.src_approx = bool(re.search(r"长表|宽表|转置|透视|堆叠|wide|long|melt",
                                           _layout_note, re.I))
            # 双行表头的第二行是单位，不是观测值；未识别设备时没有档案可依，必须兜底剔
            tb, _unit_dropped = PP.drop_unit_row(tb)
            if _unit_dropped:
                ing["notes"] = list(ing.get("notes") or []) + [
                    "已剔除单位行（表头第二行的 m/min、℃ 等不是观测值，留着会把均值与 σ 带偏）"]
            tables.append(tb)
            device_reports.append({
                "file": os.path.basename(p),
                "device": ing["device"],
                "device_name": ing["device_info"].get("name"),
                "vendor": DEV.get_profile(ing["device"]).get("vendor") if ing["device"] else "unknown",
                "confidence": ing["device_info"].get("confidence"),
                "score": ing["device_info"].get("score"),
                "evidence": ing["device_info"].get("evidence") or [],
                "runner_up": ing["device_info"].get("runner_up"),
                "export_note": ing.get("profile_note", ""),
                "notes": ing["notes"],
                "warnings": ing["warnings"],
                "units_applied": ing["units_applied"],
                "attr_applied": ing.get("attr_applied") or [],
                "col_source": ing["col_source"],
                "rows_before": None,
                "shape": tb.shape(),
                "device_col_map": {k: v for k, v in ing["col_map"].items() if v},
                "matched_by": ing["device_info"].get("matched_by"),
                "similarity": ing["device_info"].get("similarity"),
                "fingerprint": ing["device_info"].get("fingerprint"),
            })
        except Exception as e:
            result.setdefault("read_errors", []).append({"file": p, "error": str(e)})
    if not tables:
        # 文件能打开但一行数据都没有 -> 给排查清单；
        # 文件本身读不了（编码异常/格式不支持）-> 那是另一类问题，报原始错误。
        if _empty_files:
            return _empty_data_error("read", files=result.get("read_errors"))
        return {"error": "没有可读取的数据文件", "detail": result.get("read_errors")}
    # 多文件场景下企业常把上游数据一起给，文件名往往写着「上游 / 本工序」。
    # 一律取第一张，就会拿上游表当本工序来分析——工序判错、列名匹配错、根因全错。
    def _main_rank(tb):
        nm = str(getattr(tb, "name", "") or "")
        if any(w in nm for w in ("本工序", "当前工序", "current", "本道")):
            return 0
        if any(w in nm for w in ("上游", "upstream", "来料", "前道")):
            return 2
        return 1

    main = sorted(tables, key=_main_rank)[0]
    # 主表是排序后选出来的，不一定是输入的第一份；设备档案必须取主表那一份，
    # 否则多文件时会拿上游表的设备口径套在本工序数据上（E02 踩过这个坑）。
    _mi = next((i for i, t in enumerate(tables) if t is main), 0)
    # v1.11：缺陷事件表（一行一个缺陷）。不先汇总就分析，会同时犯三个错：
    #   n 从产品数虚增到缺陷数（伪重复）、坐标列被当分析目标、真正的位置聚集算不出来。
    _raw_events = None
    if _HAS_EVENTS:
        _ev_ok, _ev_info = EV.looks_like_event_table(main)
        if _ev_ok:
            _raw_events = main
            main, _ev = EV.aggregate_events(main)
            if _ev:
                result["event_table"] = _ev
                # 「缺陷总数」是各类型计数之和，属于派生列：它会以 0.9+ 的相关排到根因第一，
                # 而它和目标本来就是同一件事（这和此前判定列泄漏是同一类错误）。
                result["derived_cols"] = ["缺陷总数"]
                result.setdefault("warnings", []).append(_ev["note"])
                result.setdefault("decisions", []).append(
                    {"kind": "event_table_collapse",
                     "detail": "事件表按实体汇总：%s" % _ev["note"]})
                _clusters = []
                _tc = _ev.get("type_col")
                for _t in (_ev.get("types") or [None]):
                    if _tc:
                        _ti = _raw_events.col_index(_tc)
                        rows = [r for r in _raw_events.rows
                                if _ti < len(r) and r[_ti] == _t]
                    else:
                        rows = list(_raw_events.rows)
                    if len(rows) < 8:
                        continue
                    _sub = T.Table(_raw_events.columns, rows, _raw_events.name)
                    for pc in EV.position_columns(_sub)[:3]:
                        cres = EV.factor_clustering(_sub, pc)
                        if cres and cres.get("significant"):
                            cres["defect_type"] = _t
                            cres["conclusion"] = EV.cluster_report(cres)
                            _clusters.append(cres)
                if _clusters:
                    _clusters.sort(key=lambda d: -(d["worst"].get("ratio") or 0))
                    result["spatial_clusters"] = _clusters
                    result.setdefault("warnings", []).append(
                        "【位置聚集】" + _clusters[0]["conclusion"])
    # 长表（一行一个采样点 + 工步列）先按实体汇总，否则非测量点的 0 会污染均值与 σ
    main, _lf_note = QG.collapse_longform(main)
    if _lf_note:
        result.setdefault("warnings", []).append(_lf_note)
        result.setdefault("decisions", []).append(
            {"kind": "longform_collapse", "detail": "长表汇总：%s" % _lf_note})
    _main_dev = device_reports[_mi] if _mi < len(device_reports) else {}
    result["devices"] = device_reports
    result["device"] = _main_dev.get("device")
    result["device_name"] = _main_dev.get("device_name")
    result["device_matched_by"] = _main_dev.get("matched_by")
    result["shape"] = main.shape()
    result["columns"] = main.columns

    _v("[1/6] 读取与版式归一：%d 个文件 -> 主表 %s；设备=%s%s" % (
        len(paths), " x ".join(str(x) for x in main.shape()),
        result.get("device_name") or "未识别",
        ("（按%s匹配）" % result.get("device_matched_by")) if result.get("device_matched_by") else ""))
    # 2. 判定列编码 + 列名映射 + 工序识别
    judge_col = encode_judgement(main)
    _dev_map = {}
    _dev_specs = {}
    if _main_dev.get("device"):
        # 设备档案里的默认规格。命中设备后上下限就固定了，不再随样本量浮动。
        _prof = DEV.get_profile(_main_dev["device"]) or {}
        _dev_specs = _prof.get("default_specs") or {}
        result["device_spec_locked"] = bool(_dev_specs)
    if _main_dev.get("device_col_map"):
        # 设备档案识别出的列名映射优先，通用别名库兜底
        _dev_map = _main_dev["device_col_map"]
        def _cmap_prefer(colname):
            return _dev_map.get(colname) or PP.match_param(colname)
        col_map = {c: _cmap_prefer(c) for c in main.columns}
    else:
        col_map = PP.map_columns(main)
    proc_scores = PP.detect_process(main, col_map)
    if process and process in KB["processes"]:
        chosen_proc = process
    else:
        # 护栏：证据不足或存在歧义时不静默选定工序（v1.10）
        chosen_proc, _proc_note = QG.pick_process(proc_scores)
        if _proc_note:
            result.setdefault("warnings", []).append(_proc_note)
    # 二次精化：用判定的工序重做列名匹配，避免跨行业误匹配
    if chosen_proc:
        col_map = PP.map_columns(main, chosen_proc)
        proc_scores = PP.detect_process(main, col_map)
    # 2b. 字段适配：认不出来的列 / 互相打架的列，在这里定性处理掉，
    #     原则是"退出判定但不拦报告"，绝不让一张表把整轮分析带走
    try:
        ad = AD.adapt(main, col_map, kb=KB, process=chosen_proc,
                      auto_approve=bool(auto_adapt), queue=True,
                      source={"process": chosen_proc})
        if ad.get("table") is not None:
            main = ad["table"]
        col_map = ad.get("col_map") or col_map
        result["adaptation"] = {
            "issues": ad.get("issues") or [],
            "applied": ad.get("applied") or [],
            "queued": ad.get("queued") or [],
            "notes": ad.get("notes") or [],
            "degraded": bool(ad.get("degraded")),
        }
        for n in (ad.get("notes") or []):
            result.setdefault("warnings", []).append(n)
    except Exception as e:
        result["adaptation"] = {"error": str(e), "degraded": True}
    result["col_map"] = {k: v for k, v in col_map.items() if v}
    # P1-3：把结构化映射与低置信列一并交出去；低置信映射触发告警（默认 auto 不阻塞，
    # 需要更严时可改用 apply_map_policy(detailed, "block") 直接按未匹配处理）。
    _cmap_detailed = PP.map_columns_detailed(main, chosen_proc)
    _cmap_block_keys, _low_conf = PP.apply_map_policy(_cmap_detailed, "auto")
    result["col_map_detail"] = {c: {k: v for k, v in r.items() if k != "candidates"}
                               for c, r in _cmap_detailed.items()}
    result["low_confidence_mappings"] = _low_conf
    if _low_conf:
        result.setdefault("warnings", []).append(
            "字段映射存在 %d 个低置信项（来源=%s），建议人工确认或升级为 block 政策"
            % (len(_low_conf),
               ", ".join(sorted({x["source"] for x in _low_conf})) or "未知"))
    result["process_detected"] = proc_scores[:3]
    result["process"] = chosen_proc
    result["process_name"] = KB["processes"].get(chosen_proc, {}).get("name", "未知工序")

    _v("[2/6] 编码与工序识别：判定列=%s；工序=%s；列名映射 %d/%d 项" % (
        judge_col or "无", result.get("process_name") or "未知",
        len([1 for _c, _k in col_map.items() if _k]), len(col_map) or 1))
    # 3. 清洗。log 记下每一条被剔除的行（文件+原始行号+原因），
    #    既能在报告里交代"删了什么"，也给了复核和撤销的依据。
    cleaning_log = []
    _t_pp = time.time()
    clean_tb, clean_rep = PP.clean(main, missing="keep", outlier="mad", log=cleaning_log,
                                   prefer_process=chosen_proc)
    if OBS is not None:     # P2-4 耗时埋点
        OBS.ok("preprocess", (time.time() - _t_pp) * 1000.0, step="clean")
    result["clean"] = {k: v for k, v in clean_rep.items() if k != "col_map"}
    if len(clean_tb) < 3:
        cleaning_log = []
        clean_tb, clean_rep = PP.clean(main, missing="keep", outlier="none", log=cleaning_log,
                                       prefer_process=chosen_proc)
        result["clean"] = {k: v for k, v in clean_rep.items() if k != "col_map"}
    result["cleaning_log"] = cleaning_log
    # P2-4：输入质量与资源上限。没有输入质量，"结论不可信"到底是数据的问题
    # 还是算法的问题就说不清；没有资源上限，病态输入会一直跑到既慢又不可信。
    if OBS is not None:
        try:
            _rows_in = len(main) if main is not None and hasattr(main, "__len__") else None
            _cols_in = len(col_map) if col_map else None
            OBS.record_input_quality(
                rows=clean_rep.get("rows_in") or _rows_in, cols=_cols_in,
                missing_rate=clean_rep.get("missing_rate"))
            _res = OBS.check_resources(rows=_rows_in, cols=_cols_in)
            if not _res.get("ok"):
                result.setdefault("warnings", []).append(
                    "输入规模超出资源上限（%s）：分析仍会继续，但耗时与内存可能失控，"
                    "建议分批或先做抽样"
                    % "、".join("%s=%s>%s" % (b["limit"], b["value"], b["max"])
                                for b in _res.get("breaches") or []))
        except Exception as _obs_e:
            degrade.note("pipeline.analyze@observ_input", _obs_e)
    result["trace_meta"] = {
        "file": getattr(main, "src_file", lambda: None)(),
        "row_tracing": ("近似" if getattr(main, "src_approx", False) else "精确"),
        "note": "行号对应源文件的物理行（表头占第 1 行）；做过版式归一时为近似对应",
    }
    work = clean_tb
    if len(work) == 0:
        # 清洗把数据删光了。原实现会带着 0 行继续跑完全部章节，
        # 每节都输出「未检出」，最后拼成一份完整报告——使用者读到的是
        # 「这批没问题」，而真相是「一行都没看」。
        _acts = [str(a) for a in (clean_rep.get("actions") or [])][:5]
        return _empty_data_error(
            "clean",
            detail_lines=(
                ["清洗剔除了全部 %s 行，剩余 0 行。" % (clean_rep.get("rows_in"),)]
                + (["触发的清洗动作：" + "；".join(_acts)] if _acts else [])
                + ["排查方向（按可能性排序）："
                   "① 全是重复行——文件里同一条记录被复制了多次；"
                   "② 数值全部落在物理量程之外——列名标注的单位与数据实际单位不一致"
                   "（如数据是 mm 而列名写 m），单位换算后整列越界；"
                   "③ 离群剔除过紧——数据本身方差极大时 MAD 会把正常点也判为离群。"]
            ))
    # 测试条件归一：组件 Pmax 按辐照度/温度归一到 STC 后再判定（v1.10）
    _stc_raw = [c for c, k in col_map.items() if k == "pmax"]
    work, col_map, _stc_note, _stc_target = QG.stc_normalize(work, col_map, target)
    if _stc_note:
        result.setdefault("warnings", []).append(_stc_note)
    if _stc_target:
        target = _stc_target
        result["stc_replaced_cols"] = _stc_raw   # 原始 Pmax 列不得作为根因候选
        result["stc_applied"] = True
        result.setdefault("decisions", []).append(
            {"kind": "stc_normalize", "detail": "STC 归一化：%s" % (_stc_note or "")})

    _v("[3/6] 清洗：%s -> %s 行（剔除 %s 行，逐行明细见报告「清洗记录」）" % (
        clean_rep.get("rows_in"), clean_rep.get("rows_out"),
        (clean_rep.get("rows_in") or 0) - (clean_rep.get("rows_out") or 0)))
    # 4. 目标列与分组
    y_col = pick_target(work, target, spec_overrides=spec_overrides, col_map=col_map) or judge_col
    if judge_col and (target in (None, "") or target == judge_col):
        y_col = judge_col
    result["target"] = y_col
    if not target and y_col:
        result.setdefault("decisions", []).append(
            {"kind": "target_selection",
             "detail": "自动选择分析目标＝「%s」。理由：优先取该工序的**结果型**质量特性，"
                       "并要求它在数据里确实存在可被其它变量解释的变异；"
                       "编号/时间/坐标、分档标签与派生合计列均不作为目标。" % y_col})
    groups = pick_groups(work)
    result["groups"] = groups
    # 列名几乎全对不上时，工序判定和根因都是猜的，必须明说——
    # 最危险的不是算错，是算错了还给出一个很自信的结论。
    if work.columns:
        _hit = len([1 for _c, _k in col_map.items() if _k])
        _rate = _hit / float(len(work.columns))
        result["column_recognition_rate"] = round(_rate, 3)
        if _rate < 0.25:
            result.setdefault("warnings", []).append(
                "列名识别率仅 %.0f%%（%d/%d 列）：本文件列名与标准参数对不上，"
                "工序判定与根因结论都不可靠。请补充列名含义对照，或登记为设备档案后重跑。"
                % (_rate * 100, _hit, len(work.columns)))

    # P1-4 数据契约：未在契约里登记的列要点名隔离，不能悄悄混进结论。
    # 「有一列我们不知道是什么」和「分析过了」是两回事——
    # 混过去的话，报告看上去完整，实际少了几列的证据。
    try:
        _ci = CT.check_input(work, known_columns=set(col_map or {}))
        result["contract"] = {
            "schema_version": CT.SCHEMA_VERSION,
            "n_rows": _ci.get("n_rows"),
            "n_columns": _ci.get("n_columns"),
            "recognized_roles": _ci.get("roles") or [],
        }
        if _ci.get("unknown"):
            result["contract_quarantine"] = CT.quarantine(_ci["unknown"])
            result.setdefault("warnings", []).append(
                "以下 %d 列未在字段契约中登记，本次不参与工艺判定（数据保留，"
                "结论不含它们的证据）：%s"
                % (len(_ci["unknown"]),
                   "、".join(list(_ci["unknown"])[:8])
                   + ("…" if len(_ci["unknown"]) > 8 else "")))
    except Exception as _exc:
        degrade.note("pipeline.contract@check_input", _exc)

    _v("[4/6] 目标列=%s；分组维度=%s" % (
        y_col or "未定", "、".join(groups[:5]) if groups else "无"))
    # 5. 时间列
    tcol, times = PP.parse_time_column(work)

    _v("[5/6] 时间列=%s" % (tcol or "无"))
    # 5b. 行数不为 0 却一个有效观测值都没有 —— 与「0 行」等价，
    #     却比 0 行更容易蒙混过关：列名全对得上、工序也能判定出来，
    #     报告看起来像"分析过了"，实际每个统计量都是 None。
    #     必须排除时间列再数：实测「2026-01-01」会被解析成数值 2026.0，
    #     不排除的话时间列单独就能让这个检测失效。
    _numcols = [c for c in work.numeric_columns() if c != tcol]
    _n_valid = 0
    for _c in _numcols:
        try:
            _n_valid += sum(1 for _v in work.numeric_column(_c) if _v is not None)
        except Exception as _dg_e:
            degrade.note("pipeline.analyze@L706", _dg_e)
    if (not _numcols) or _n_valid == 0:
        return _empty_data_error(
            "values",
            detail_lines=[
                "共 %d 行，除时间列外没有任何一列能解析出可用于统计的数值%s。"
                % (len(work), "（识别到 %d 个数值列，全部无有效值）" % len(_numcols)
                   if _numcols else "（一个数值列都没识别出来）"),
                "排查方向：数值列里混了单位字符（如「12.3 mm」）或千分位逗号时，"
                "整列会被当成文本；单元格里只写了「/」「-」「NA」等占位符同理。",
            ])
    # 6. 缺陷识别与 Pareto
    # P2-4：关键步骤埋耗时。只记整轮时长等于什么都没记——
    # 慢在哪一步不知道，就只能在最热的那个环节上瞎猜。
    _t_rc = time.time()
    defects = RC.identify_defects(work, defect, process=chosen_proc)
    if OBS is not None:
        OBS.ok("rootcause", (time.time() - _t_rc) * 1000.0, step="identify_defects")
    result["defects"] = defects[:10]
    # P0-3：每个环节都要自己交代"到底算过没有"。
    # 不写 status 的话，下游只能看到"列表是空的"，
    # 分不清"确实没有缺陷"和"这一段压根没跑成"。
    for _d in result["defects"]:
        if isinstance(_d, dict):
            _d.setdefault("status", ST.SUCCESS if _d.get("total") else ST.NO_FINDING)
    if defect:
        result["defect"] = defect
    # 机理借用告警：本工序没登记这条机理，匹配时借用了别的工序的条目。
    # 必须说出来——跨行业串机理（把光伏串焊缺陷判成锂电池超声焊问题）
    # 看起来很专业，没人会怀疑它，比「未收录」危险得多。
    _borrowed = [d for d in defects[:10] if d.get("cross_process")]
    if _borrowed:
        _names = "、".join(sorted({str(d.get("defect")) for d in _borrowed})[:4])
        _from = "、".join(sorted({
            KB["processes"].get(d.get("process"), {}).get("name") or d.get("process")
            for d in _borrowed})[:3])
        result.setdefault("warnings", []).append(
            "机理借用提示：%s 这几条缺陷，当前工序（%s）的知识库里没有登记，"
            "匹配时借用了 %s 工序的条目。机理可借鉴但工艺参数（温度/压力/时间/设备）"
            "未必通用，D4 根因与 D5 措施请按本工序实际复核后再用。"
            % (_names, result.get("process_name") or chosen_proc or "未识别", _from))

    _v("[6/6] 缺陷识别：命中 %d 类%s" % (
        len(defects),
        ("（" + "、".join(str(d.get("defect")) for d in defects[:6]) + "）") if defects else ""))
    # 6b. 跨工序追溯：上游检测合格、本工序出不良时，判定问题到底出在哪一道
    #     注意 tables 里第二张表往后的数据，在此之前只写进了来源日志、从未参与分析
    if len(tables) > 1 and chosen_proc:
        try:
            cleaned, metas = [], []
            for i, tb in enumerate(tables):
                try:
                    ctb, _ = PP.clean(tb, missing="keep", outlier="mad")
                    # 与主流水线同级保护：清洗把表清没了就退回不剔异常值
                    if len(ctb.rows) < 3:
                        ctb, _ = PP.clean(tb, missing="keep", outlier="none")
                except Exception:
                    ctb = tb
                # 先按这张表自己的内容判工序：一律用当前工序去做列名匹配，
                # 会把上游表的参数匹配歪（辊压的"轧制张力"会被当成卷绕的）
                pm = chosen_proc if tb is main else None
                if pm is None:
                    try:
                        ps = PP.detect_process(ctb, PP.map_columns(ctb))
                        pm = ps[0][0] if ps else None
                    except Exception:
                        pm = None
                cm = PP.map_columns(ctb, pm)
                # 第一张表是当前工序（约定：把本工序的文件放在第一个），最可靠
                metas.append({"name": tb.name, "columns": cm,
                              "process": chosen_proc if tb is main else (pm or chosen_proc)})
                cleaned.append(ctb)
            xrt = XP.trace(cleaned, metas, y_col, chosen_proc, defect=defect,
                           judge_col=judge_col, spec=spec_overrides)
            result["crossprocess"] = {k: v for k, v in xrt.items() if k != "aligned"}
            aligned_tb = XP.pop_aligned(xrt)
            if xrt.get("applicable") and aligned_tb is not None:
                # 横向排查必须在同一张对齐表上做，且要剥掉主导因子的方差，否则会漏报
                result["lateral"] = AC.lateral_plan(
                    aligned_tb, target_col=y_col, defect=defect,
                    contributions=xrt.get("contributions"))
                result["lateral"].pop("rows_unused", None)
            top_param = None
            for c in (xrt.get("contributions") or []):
                if c.get("param"):
                    top_param = c["param"]
                    break
            result["permanent"] = AC.permanent_actions(
                xrt, defect=defect, current_process=chosen_proc, param_key=top_param)
            result["preventive"] = AC.preventive(xrt, process=chosen_proc)
        except Exception as e:
            result.setdefault("read_errors", []).append(
                {"file": "crossprocess", "error": "跨工序追溯降级：%s" % e})

    try:  # 护栏：SPC 环节失败只降级本节，不中断整轮
        # 7. SPC
        spc_res = []
        numcols = [c for c in work.numeric_columns()
                   if len([v for v in work.numeric_column(c) if v is not None]) >= 8]
        priority = []
        for c, k in col_map.items():
            if k:
                priority.append(c)
        ordered = [c for c in priority if c in numcols] + [c for c in numcols if c not in priority]
        if y_col and y_col in ordered:
            ordered.remove(y_col)
            ordered.insert(0, y_col)
        if spec_overrides:
            _un = PP.unmatched_spec_keys(spec_overrides, work.columns, col_map)
            if _un:
                result.setdefault("warnings", []).append(
                    "企业规格书有 %d 条未匹配到任何列：%s。这些项的 Cpk 只能用数据分布估算，"
                    "请核对规格书里的名称与导出列名是否一致。" % (len(_un), "、".join(_un[:6])))
        # 企业给了规格的列必须参与能力计算。以前固定只算前 8 列，
        # 结果「极片残水」这种真正超规格的关键列被挤出去，报告里反而看不到它不合格。
        if spec_overrides:
            for c in reversed([c for c in ordered
                               if PP.match_spec_key(c, col_map, spec_overrides)]):
                ordered.remove(c)
                ordered.insert(0, c)
        for c in ordered[:12]:
            # 行号/序号/列这类"和每一行都同步变化"的东西不是被测的量。
            # 它们落在 SPC 里必然自相关（r1≈0.99）并在控制图上刷出一整排报警，
            # 把真正的异常淹没在噪声里 —— 修できず,只有角色判据能挡住。
            # 同一个 is_internal_column 判据也排除 __ 前缀的派生列
            # （它在找列/分组/归因三处已经在用了）。
            if QG.is_internal_column(c):
                continue
            if QG.column_role(c, work.numeric_column(c) if c in work.columns else None) \
                    not in ("measure", "count"):
                continue
            vals = work.numeric_column(c)
            key = col_map.get(c)
            spec = KB["params"].get(key, {}) if key else {}
            ctrl = spec.get("control", {})
            mu = S.mean([v for v in vals if v is not None])
            sd = S.stdev([v for v in vals if v is not None])
            # 企业规格书优先。以前这一步根本没接 spec_overrides，企业给了规格书也白给，
            # Cpk 全用数据分布兜底算——数字失真且毫无提示。
            _ov = PP.match_spec_key(c, col_map, spec_overrides) if spec_overrides else None
            if _ov:
                lsl, usl, tgt = _ov.get("lsl"), _ov.get("usl"), _ov.get("target")
                _spec_src = "企业规格书"
            else:
                # 设备档案里的默认规格次之。没有它，上下限就只能靠 mu±k·sd 推定，
                # 而推定值跟着样本量走 —— 同一台设备不同批次导出，Cpk 会来回跳。
                # 同样要柔性匹配：档案里写「单面面密度」，这次导出写成
                # 「单面面密度(g/m2)」就查不到，锁定会静默失效（实测踩到）。
                _ds = PP.match_spec_key(c, col_map, _dev_specs) if _dev_specs else None
                if _ds:
                    lsl = _ds.get("lsl") if isinstance(_ds, dict) else (_ds[0] if _ds else None)
                    usl = _ds.get("usl") if isinstance(_ds, dict) else (_ds[1] if len(_ds) > 1 else None)
                    tgt = _ds.get("target") if isinstance(_ds, dict) else None
                    _spec_src = "设备档案"
                else:
                    lsl, usl, tgt = SPC.resolve_spec(ctrl, mu, sd)
                    _spec_src = ("知识库推定" if (lsl is not None or usl is not None) else None)
            # Phase I/II：先用稳定期建限，再拿这个限监控全段。
            # 直接用全段算限的话，异常自己会把 σ 撑大、把控制限撑宽，
            # 结果就是最该报警的那批点反而"在限内"——SPC 失效的头号原因。
            _rules = SPC.parse_rules(rules)
            p1 = SPC.phase1_limits(vals, mode=spc_exclusion_mode)
            if p1 and p1.get("stable"):
                xc, rc = SPC.x_mr_with_limits(vals, p1["center"], p1["sigma"])
            else:
                xc, rc = SPC.x_mr(vals)
            vios = []
            if xc and xc.sigma_within:
                vios = SPC.nelson_violations(xc.values, xc.center, xc.sigma_within,
                                             rules=_rules)
            # 自相关诊断 + EWMA/CUSUM：
            # Nelson 规则假设观测**独立**。连续涂布厚度、连铸温度这类序列相邻点
            # 天然牵连，在这上面跑常规判异，"惯性"会被读成"连续 9 点同侧"（规则 2），
            # 报警刷屏的最后结果是现场把图关了——比没有图更糟。
            # 所以先测自相关，显著就自动补 EWMA（对自相关稳健）与 CUSUM（抓缓慢爬行）。
            # 这里不给开关：用哪种图由数据的自相关证据决定，不由使用人猜。
            _acd, _ewma_out, _cusum_out = None, None, None
            try:
                _acd = SPC.autocorrelation_diagnosis(vals)
            except Exception as _ac_e:
                degrade.note("pipeline.spc@autocorr", _ac_e)
            if isinstance(_acd, dict) and _acd.get("significant"):
                # 先拿 ACF 谱，再作图：**控制限要按自相关放大**，不是把数据滤平。
                #
                # 走过的弯路（实测，值得留下）：一开始改成"滤掉一阶自相关后对残差作图"，
                # 误报确实压住了（200 点 135 报 → 2 报），但同一组构造数据里
                # 后段 1.5σ 的持续漂移一条都没报出来——持续阶跃在 innovations 里
                # 只表现为发生瞬间的一个尖峰，之后残差又回到白噪声。
                # 也就是说：滤平的代价是把最想知道的那类失效一起滤没了。
                # 正确口径是按 Var(z)=γ₀·λ/(2−λ)·[1+2Σ(1−λ)^k ρ_k] 放大控制限。
                # 自相关场景的主图用 EWMAST（对阶跃灵敏，λ 经实测标定），
                # CUSUM 仍按 Newey–West 放大后的尺度给出（对缓慢爬行灵敏）。
                # 两张图互补：阶跃看 EWMAST，爬行看 CUSUM。
                _acf_rows = _acd.get("acf") or []
                _mu0 = xc.center if xc else None
                _sig_long = None
                try:
                    _sig_long = S.stdev([v for v in vals if v is not None])
                except Exception as _sd_e:
                    degrade.note("pipeline.spc@sigma", _sd_e)
                try:
                    _ewst = SPC.ewma_stationary(vals)
                    _cu = SPC.cusum(vals, mu=_mu0, sigma=_sig_long, acf=_acf_rows)
                    # 顺便记下 AR(1) 系数：它是"这根序列记得多远"的直观刻度，
                    # 也是判断能否靠拉大采样间隔解决问题的依据（φ 越大越要拉大）
                    _ar1 = SPC.ar1_residuals(vals)
                    _acd["phi"] = _ar1.get("phi")
                except Exception as _ew_e:
                    degrade.note("pipeline.spc@ewma", _ew_e)
                    _ewst, _cu = None, None
                _ew = _ewst

                def _brief(d, extra=()):
                    if not isinstance(d, dict) or d.get("status") != "OK":
                        return None
                    out = {"method": d.get("method"), "n_alarms": d.get("n_alarms"),
                           "first_alarm": (d.get("alarm_indexes") or [None])[0],
                           "alarm_indexes": (d.get("alarm_indexes") or [])[:100]}
                    for k_ in extra:
                        out[k_] = d.get(k_)
                    return out

                _ewma_out = _brief(_ew, ("lambda", "L"))
                _cusum_out = _brief(_cu, ("k", "h"))
                _acd["acf_factor_cusum"] = (_cu or {}).get("acf_factor")
                # 能力边界要写清楚：自相关越强，能检出的最小偏移越大。
                # 拿着一张"什么都不报"的图以为过程很稳，是这里最贵的误读。
                _h3 = (_ew or {}).get("min_detectable_shift")
                if _h3 is not None:
                    _acd["min_detectable_shift"] = _h3
                    # 可操作建议：隔多少个点采样才能让自相关降到可忽略。
                    # 只说"降低自相关"等于没说——现场能执行的只有"少采一点、隔开采"。
                    _thin = None
                    try:
                        _thin = SPC.thin_for_independence(vals)
                    except Exception as _th_e:
                        degrade.note("pipeline.spc@thin", _th_e)
                    _step = ((_thin or {}).get("step") or 1)
                    _acd["suggested_sampling_step"] = _step
                    _acd["thinned_note"] = (_thin or {}).get("note")
                    result.setdefault("warnings", []).append(
                        "「%s」序列自相关（r1=%.2f，AR(1) φ=%s）：已改用 EWMAST"
                        "（一步预测残差图）+ CUSUM（按自相关放大 %.2f 倍）代替常规 X-MR 判异。"
                        "该图上小于 %.4g %s 的阶跃在统计上未必看得出；"
                        "要提高灵敏度建议把采样间隔拉到 ≥ %d 个（该间隔下约 %d 个样本）"
                        % (spec.get("name", c), _acd.get("lag1") or 0.0,
                           round(_acd.get("phi") or 0.0, 3), (_cu or {}).get("acf_factor") or 1.0,
                           _h3, spec.get("unit", ""), _step,
                           len([v for v in vals if v is not None]) // max(1, _step)))
                # 常规判异在自相关数据上不可信：把这件事说透，而不是照给报警
                if _acd.get("recommend") == "ewma" and vios:
                    _nviol = sum(len(v.get("indexes") or []) for v in vios)
                    result.setdefault("warnings", []).append(
                        "「%s」存在显著自相关（r1=%.2f）：本次常规 Nelson 判异共 %d 个异常点，"
                        "其中部分可能只是序列惯性而非真实异常，请以 EWMA/CUSUM 结果为准"
                        % (spec.get("name", c), _acd.get("lag1") or 0.0, _nviol))
                result.setdefault("decisions", []).append({
                    "kind": "spc_autocorrelated",
                    "detail": "「%s」自相关显著（%s）：已自动追加 EWMA 与 CUSUM，"
                              "常规 X-MR 报警仅供参考" % (spec.get("name", c),
                                                    (_acd.get("reason") or "")[:60])})
            cap = SPC.process_capability(vals, lsl, usl, tgt,
                                         sigma_within=(xc.sigma_within if xc else None))
            if cap and p1:
                cap["baseline_note"] = p1["note"]
                # Cpk 的前提条件必须写明：样本量不足、规格来源、过程是否受控
                cap["caveats"] = []
                if cap.get("n", 0) < 25:
                    cap["caveats"].append("样本量 n=%d < 25，Cpk 估计不稳定，仅供方向参考"
                                          % cap["n"])
                if not p1.get("stable"):
                    cap["caveats"].append("过程未达统计受控状态，Cpk 的前提（稳定）不成立，"
                                          "该值不代表长期能力")
                if _spec_src == "知识库推定":
                    cap["caveats"].append("规格为知识库推定值，非企业受控规格，不得用于对外报告")
            spc_res.append({
                "column": c, "param_key": key, "param_name": spec.get("name", c),
                "unit": spec.get("unit", ""), "chart_type": "X-MR",
                "center": xc.center if xc else None,
                "ucl": xc.ucl if xc else None, "lcl": xc.lcl if xc else None,
                "sigma_within": xc.sigma_within if xc else None,
                "sigma_overall": sd, "mean": mu,
                "violations": vios, "capability": cap,
                "lsl": lsl, "usl": usl, "target_value": tgt, "spec_source": _spec_src,
                "autocorrelation": _acd, "ewma": _ewma_out, "cusum": _cusum_out,
                "phase1": p1, "rules_used": list(_rules),
                "exclusion_review": {
                    "mode": spc_exclusion_mode,
                    "excluded_detail": (p1 or {}).get("exclusion_detail", []),
                    "suspect_points": (p1 or {}).get("suspect_points", []),
                },
                "values": vals,
            })
        result["spc"] = [{k: v for k, v in d.items() if k != "values"} for d in spc_res]
        # status 三态判据必须具体到"看哪个字段"，不能一律 SUCCESS：
        # 有能力指数才算真的算过；只有判异结果没有能力指数，说明样本/规格不足以定论。
        for _d in result["spc"]:
            if _d.get("status"):
                continue
            if _d.get("violations"):
                _d["status"] = ST.SUCCESS            # 算过了，而且查出点东西
            elif _d.get("capability") is not None:
                _d["status"] = ST.NO_FINDING         # 算过了，这个参数没问题
            else:
                _d["status"] = ST.INSUFFICIENT_DATA  # 数据不足以谈论这个参数
        result["_spc_full"] = spc_res
        result["spc_rules"] = list(_rules)

    except Exception as _guard_e:
        result.setdefault("degraded_sections", []).append(
            {"section": "SPC", "error": str(_guard_e)})
    try:  # 护栏：相关性 环节失败只降级本节，不中断整轮
        # 8. 相关性
        corr = CORR.corr_matrix(work, [c for c in numcols][:16])
        # corr_matrix 一旦抛异常，本节的其余计算全都建立在它之上，
        # 不做这层保护的话降级之后会再崩一次 TypeError（NoneType 不可下标），
        # 把真正的原因盖掉 —— 与 'gaps' 那个 UnboundLocalError 是同一类问题。
        if corr is None:
            result.setdefault("degraded_sections", []).append(
                {"section": "相关性", "status": ST.FAILED,
                 "error": "corr_matrix 未产出相关矩阵",
                 "impact": "本节结论缺失，不代表『没有问题』"})
        else:
            result["corr_columns"] = corr["columns"]
            result["_corr"] = corr
            strong = CORR.strong_pairs(corr)
            result["strong_pairs"] = strong[:10]
            vifs = CORR.vif(work, corr["columns"][:10])
            result["vif"] = vifs[:8]
        assoc = []
        if y_col:
            a = CORR.association_with_target(work, y_col)
            if isinstance(a, list):
                assoc = a
            elif isinstance(a, dict) and a.get("status") in (ST.FAILED,
                                                             ST.INSUFFICIENT_DATA):
                # 目标列不可用时必须把它带的状态一起留下来，
                # `isinstance(a, list)` 会把带状态的 dict 静默丢成空列表，
                # 报告上就写成"未发现相关因素"了。
                result.setdefault("degraded_sections", []).append({
                    "section": "相关性·目标列", "status": a.get("status"),
                    "error": a.get("error"),
                    "impact": "该目标的关联分析未执行，不代表『没有相关因素』"})
        result["association"] = assoc[:15]
        # 样本量小到算不出可信相关时，不能和"算过且没有相关因素"混为一谈
        for _a in (result["association"] or []):
            if isinstance(_a, dict) and not _a.get("status"):
                _n = _a.get("n")
                _a["status"] = (ST.SUCCESS if (isinstance(_n, int) and _n >= 3)
                                else ST.INSUFFICIENT_DATA)
        QG.test_condition_warnings(assoc, result)
        lags = []
        if y_col:
            for d in assoc[:5]:
                if d["type"] == "numeric":
                    lg = CORR.lagged_correlation(work, d["param"], y_col, max_lag=4)
                    if lg:
                        lags.append(lg)
        result["lagged"] = lags

    except Exception as _guard_e:
        result.setdefault("degraded_sections", []).append(
            {"section": "相关性", "error": str(_guard_e)})
    try:  # 护栏：分组对比 环节失败只降级本节，不中断整轮
        # 9. 分组对比
        gcomp = []
        if y_col and groups:
            for g in groups[:4]:
                gc = SPC.group_compare(work.numeric_column(y_col), work.column(g))
                if gc:
                    gc["dimension"] = g
                    gcomp.append(gc)
        result["group_compare"] = gcomp

    except Exception as _guard_e:
        result.setdefault("degraded_sections", []).append(
            {"section": "分组对比", "error": str(_guard_e)})
    try:  # 护栏：根因 环节失败只降级本节，不中断整轮
        # 10. 根因
        root = None
        # 首要缺陷按"正在恶化"选，而不是按总量最大选：
        # 总量最大的往往是长期老问题，真正要处理的是增量
        defect_name = defect or RC.pick_primary_defect(defects, work)
        if defect_name:
            root = RC.rank_root_causes(defect_name, work, y_col, col_map=col_map, process=chosen_proc,
                                       exclude_cols=result.get("stc_replaced_cols"))
            if root:
                root["five_why"] = RC.build_5why(defect_name, root["candidates"], work, y_col)
        # 兜底：未指定/未识别缺陷时，仍给出数据驱动的参数归因，而不是整节留空
        if not root and (assoc or spc_res):
            root = data_driven_rootcause(assoc, spc_res, col_map, chosen_proc, y_col,
                                         recog=result.get("column_recognition_rate"),
                                         corr=corr,
                                         exclude_cols=result.get("stc_replaced_cols"))
        # 单元级热点：16 个通道里只有 CH-07 坏、两家供应商只有 B 差 ——
        # 这种"个别单元跑偏"是产线最常见的根因形态，但单因素 ANOVA 抓不到
        # （效应被正常单元摊薄，η² 0.05、p=0.41 判不显著），只能逐水平比较。
        try:
            hotspots = []
            if y_col and groups:
                for g in groups[:6]:
                    # 跳过目标列自身，以及目标列就是由它编码而来的那一列。
                    # 否则「判定=自放电不良」会以满分排到根因第一 ——
                    # 判定列编码成 NG 之后，两者当然是同一件事，这是同义反复不是根因。
                    if g == y_col:
                        continue
                    # v1.10：判定/结果类列无论目标是谁都是结果本身，不能当原因
                    if any(h in PP._norm_name(g) for h in
                           ("判定", "结果", "结论", "judge", "result", "grade", "等级")):
                        continue
                    # v1.11：派生列（各缺陷类型之和）与产品的分档标签都不是原因。
                    # 分档永远由测量结果本身决定，「功率档位」相关再强也只能是同位反复。
                    if g in set(result.get("derived_cols") or []):
                        continue
                    if _is_bin_label(g):
                        continue
                    hs = XP.outlier_levels(work, g, y_col,
                                           direction=XP.bad_direction(y_col))
                    # 护栏：按「每个水平样本数」过滤。批号/卷号类 ID 列每批只有几条，
                    # 单批的 Welch 检验没有统计意义（v1.10：S1 的 Lot=Z030 曾误排第一）。
                    _min_n = 10 if QG.role_of(work, g) == "id" else 5
                    if (hs and hs.get("significant") and hs.get("worst")
                            and (hs["worst"].get("n") or 0) >= _min_n):
                        # v1.11：批次/批号维度若与行序（时间）完全混杂，报它等于报
                        # "时间造成的"，属于伪结论。标注出来，并且不让它占据首位。
                        _rho = QG.order_confound(work, g)
                        if _rho is not None:
                            hs["time_confound"] = abs(_rho)
                            if abs(_rho) >= 0.85:
                                hs["confounded"] = True
                                hs["note"] = (
                                    (hs.get("note") or "") +
                                    "⚠ 该维度与时间顺序高度混杂（ρ=%.2f），无法区分是批次效应"
                                    "还是时间漂移，不得据此调机；需交叉实验设计才能定位。"
                                    % abs(_rho))
                                result.setdefault("warnings", []).append(
                                    "分组维度「%s」与时间顺序高度混杂（ρ=%.2f）："
                                    "它上面的差异不能解释为批次效应。" % (g, abs(_rho)))
                        hotspots.append(hs)
            hotspots.sort(key=lambda x: -(x["worst"]["effect"] if x.get("worst") else 0))
            # 混杂的热点一律排在后面：它不是答案，只是需要进一步实验的信号
            hotspots.sort(key=lambda x: 1 if x.get("confounded") else 0)
            result["unit_hotspots"] = hotspots
            if root and hotspots:
                cands = root.setdefault("candidates", [])
                have = set(str(c.get("param_name") or "") for c in cands)
                for hs in hotspots[:3]:
                    w = hs["worst"]
                    nm = "%s = %s" % (hs["dim"], w["level"])
                    if nm in have:
                        continue
                    cands.append({
                        "param_key": None, "param": hs["dim"],
                        "confounded": bool(hs.get("confounded")),
                        "param_name": nm, "unit": "",
                        "role": "group", "source": "单元级异常检验",
                        "score": round(min(1.0, w["effect"] / 2.0), 4),
                        "evidence_score": round(w["effect"], 3),
                        "prior_score": 0.0, "temporal_score": 0.0,
                        "data_status": ST.SUCCESS,
                        "detail": {"status": ST.SUCCESS, "column": hs["dim"],
                                   "level": w["level"], "n": w["n"],
                                   "mean": w["mean"], "others_mean": w["others_mean"],
                                   "p_raw": w["p_raw"], "p_adj": w["p_adj"],
                                   "levels": hs["levels"],
                                   "note": "该水平 vs 其余全部的 Welch 检验，"
                                           "已按水平数做 Bonferroni 校正"},
                        "note": hs.get("note", ""),
                    })
                cands.sort(key=lambda d: -d["score"])
                # 混杂热点即使效应量最大也不能占位：排到候选之后
                cands.sort(key=lambda d: 1 if d.get("confounded") else 0)

        except Exception as _hs_e:
            result.setdefault("degraded_sections", []).append(
                {"section": "单元级热点", "error": str(_hs_e)})

        for _dc in set(result.get("derived_cols") or []):
            if root and root.get("candidates"):
                root["candidates"] = [c for c in root["candidates"]
                                      if c.get("param") != _dc]
        # v1.11：位置聚集结论必须能进根因候选。事件表里真因是"第 7 列"，
        # 相关矩阵算不出位置，只能在 menus Comparison 这一步注入。
        for _cl in (result.get("spatial_clusters") or [])[:1]:
            if not _cl.get("hot_level"):
                continue
            _ratio = float(_cl["worst"].get("ratio") or 0)
            _ev_score = round(min(0.95, _ratio / 5.0), 3)
            root = root or {"defect": defect_name, "process": chosen_proc,
                            "candidates": [], "five_why": None}
            _cands = root.setdefault("candidates", [])
            _nm = "%s = %s" % (_cl["factor"], _cl["hot_level"])
            if all(str(c.get("param_name") or "") != _nm for c in _cands):
                _cands.insert(0, {
                    "param_key": None, "param": _cl["factor"], "param_name": _nm,
                    "unit": "", "role": "position", "source": "位置聚集检验",
                    "score": _ev_score, "evidence_score": _ev_score,
                    "prior_score": 0.0, "temporal_score": 0.0,
                    "data_status": ST.SUCCESS,
                    "detail": {"status": ST.SUCCESS, "column": _cl["factor"],
                               "level": _cl["hot_level"],
                               "note": "%s；%d 个水平同时检验，p 值已做 BH 校正"
                                       % (_cl.get("test") or "", _cl.get("n_levels") or 0)},
                    "note": _cl.get("conclusion") or "",
                })
                _cands.sort(key=lambda d: -d["score"])

        # v1.11：负例判定。归一化之后所有工艺参数都解释不动目标 —— 真相是
        # "测试方法没归一"，不是"某个参数调错了"。此时必须明说没有根因。
        if result.get("stc_applied"):
            # 结果指标（Voc/Isc/FF）与目标同源，相关是必然，但它们不可调，
            # 不能拿来充当"可行动的根因"。
            _resp_cols = set()
            for _c, _k in (col_map or {}).items():
                if _k and (KB.get("params") or {}).get(_k, {}).get("role") == "response":
                    _resp_cols.add(_c)
            _nv = QG.negative_verdict(assoc, exclude_cols=(
                set(result.get("stc_replaced_cols") or [])
                | set(result.get("derived_cols") or []) | {y_col}),
                response_cols=_resp_cols)
            if _nv:
                result["negative_verdict"] = _nv
                result.setdefault("warnings", []).append(_nv["text"])
                if root and not (result.get("spatial_clusters") or []):
                    root["candidates"] = []
                    root["negative_verdict"] = _nv
        # ---- 测量系统门槛（MSA）：结论能不能信的第一道门槛 ----------------
        #
        # 放在根因之后、落结果之前，是因为它限制的就是根因结论本身。
        # 之前"测量系统未验证"只是报告结尾的一句免责声明——
        # 写在最后的免责，等于告诉读报告的人"上面那些你可以放心相信"。
        # 现在：%GRR 超标直接清空根因榜（IMA MSA 分级>30%），
        #       处在 10–30% 就在每条候选上挂限制注记，
        #       压根没登记则把提示提到根因章节之前。
        try:
            _msa = _resolve_msa(msa_spec, table=work, target=y_col)
        except Exception as _msa_e:
            degrade.note("pipeline.msa@resolve", _msa_e)
            _msa = {"kind": "error",
                    "reason": "测量系统数据解析失败：%s（按未登记处理，"
                              "结论不得作为调整工艺的依据）" % _msa_e}
        if isinstance(_msa, dict) and _msa.get("kind") == "error":
            result["msa_error"] = _msa.get("reason")
            result.setdefault("warnings", []).append(_msa.get("reason"))
            _msa = None
        if isinstance(_msa, dict) and _msa.get("action") in ("block", "warn", "pass"):
            if root:
                root = MSA.apply_msa_limit(root, _msa)
            if _msa.get("action") == "block":
                result.setdefault("decisions", []).append({
                    "kind": "msa_blocked",
                    "detail": "测量系统不合格，根因结论不予出具：%s" % _msa.get("reason")})
                result.setdefault("warnings", []).append(
                    "⚠ 测量系统能力不足，本次未出根因结论：%s" % _msa.get("reason"))
            elif _msa.get("action") == "warn":
                result.setdefault("warnings", []).append(
                    "测量系统仅有条件接受：%s。结论须标注受分辨力限制" % _msa.get("reason"))
        else:
            result["msa_unverified"] = MSA.unverified_notice()
        result["msa"] = _msa
        result["rootcause"] = root
        gaps = RC.assess_gaps(work, defect_name, y_col, groups)
        conf = RC.confidence_level(gaps, sum(1 for a in assoc if a.get("significant")))
        result["gaps"] = gaps
        result["confidence"] = conf

    except Exception as _guard_e:
        # 本节的三个输出必须在 except 里显式复位。
        #
        #    否则 try 内任意一步失败时它们从未被赋值，而 build_report(...)
        #    在 try **外面**还会引用 —— 于是降级处理自己再崩一次
        #    UnboundLocalError: cannot access local variable 'gaps'，
        #    把这个辜负名字的报错丢给使用者，真实原因（本行记的那条）被彻底盖住。
        #
        #    实测：注入让 rank_root_causes 崩溃，整轮以 UnboundLocalError 收场，
        #    错误文本里一个字都没提根因模块。
        root = None
        gaps = None
        conf = None
        result.setdefault("degraded_sections", []).append(
            {"section": "根因", "error": str(_guard_e),
             "status": ST.FAILED,
             "impact": "本节结论缺失：根因候选、证据缺口与置信等级均未产出，"
                       "不代表『没有问题』"})
    try:  # 护栏：Pareto 环节失败只降级本节，不中断整轮
        # 11. Pareto
        pare = None
        # 只用"计数型"缺陷列做 Pareto。连续测量列（如极片错位 0.28mm）求和没有物理意义。
        count_defects = [d for d in defects if d.get("is_count") and d.get("total")]
        if len(count_defects) >= 2:
            items = [(d["defect"], d["total"]) for d in count_defects]
            pare = RC.pareto(items)
            pare["note"] = "按缺陷类型计数拆解"
        elif y_col and groups:
            # 选组间差异最显著（ANOVA F 最大）的维度做拆解，而不是简单取第一列
            groups_sorted = list(groups)
            if gcomp:
                ranked = sorted(gcomp, key=lambda d: -((d.get("anova") or {}).get("F") or 0))
                best_dim = ranked[0]["dimension"]
                groups_sorted = [best_dim] + [g for g in groups if g != best_dim]
            # 按分组维度拆解指标贡献。
            # 若目标列是比率（不良率等）且存在产量列，折算成不良数再求和，避免比率直接相加的语义错误。
            g = groups_sorted[0]
            yv = work.numeric_column(y_col)
            gv = work.column(g)
            is_rate = any(k in PP._norm_name(y_col) for k in ["率", "rate", "yield", "percent", "%"])
            cnt_col = None
            for c in work.columns:
                n = PP._norm_name(c)
                if any(h in n for h in ["生产数", "总数", "产量", "投入数", "数量", "total", "qty"]):
                    cnt_col = c
                    break
            cnt = work.numeric_column(cnt_col) if cnt_col else None
            agg, agg_n = {}, {}
            for i, (a, b) in enumerate(zip(yv, gv)):
                if a is None or b is None:
                    continue
                k = str(b)
                if is_rate and cnt is not None and cnt[i] is not None:
                    agg[k] = agg.get(k, 0) + (a / 100.0 if a > 1.5 else a) * cnt[i]
                else:
                    agg[k] = agg.get(k, 0) + abs(a)
                agg_n[k] = agg_n.get(k, 0) + 1
            pare = RC.pareto(agg)
            if is_rate and cnt_col:
                pare["note"] = "按「%s」拆解，使用 %s × %s 折算不良数" % (g, y_col, cnt_col)
            else:
                # 无产量列时，比率类指标改为按"异常超出基线的部分"加权，至少量纲一致
                base = S.quantile([v for v in yv if v is not None], 0.25)
                if is_rate and base is not None:
                    agg2 = {}
                    for i, (a, b) in enumerate(zip(yv, gv)):
                        if a is None or b is None:
                            continue
                        agg2[str(b)] = agg2.get(str(b), 0) + max(0.0, a - base)
                    if sum(agg2.values()) > 0:
                        pare = RC.pareto(agg2)
                        pare["note"] = "按「%s」拆解，使用 %s 超出 P25 基线的累计偏差（未找到产量列，未做数量折算）" % (g, y_col)
                    else:
                        pare["note"] = "按「%s」对 %s 的累计值拆解（未找到产量列）" % (g, y_col)
                else:
                    pare["note"] = "按「%s」对 %s 的累计值拆解" % (g, y_col)
        result["pareto"] = pare

    except Exception as _guard_e:
        result.setdefault("degraded_sections", []).append(
            {"section": "Pareto", "error": str(_guard_e)})
    try:  # 护栏：DOE 环节失败只降级本节，不中断整轮
        # 12. DOE
        doe_plan = None
        if do_doe:
            factors = []
            src = (root["candidates"][:6] if root else assoc[:6])
            chosen_cols = []
            excluded = []
            for d in src:
                nm = d.get("param") or d.get("param_name")
                if not nm:
                    continue
                key = None
                for c, k in col_map.items():
                    if c == nm:
                        key = k
                spec = KB["params"].get(key, {}) if key else {}
                # 响应变量不能当因子：把 Y 放进 DOE 是逻辑错误
                if y_col and nm == y_col:
                    excluded.append((nm, "是本次分析的响应变量（Y）"))
                    continue
                # 当前数据里没有的参数不能定水平（只能退回知识库典型量程，易超出该厂安全窗口）
                if d.get("data_status") != ST.SUCCESS:
                    excluded.append((nm, "数据中无实测值，无法定水平"))
                    continue
                # 结果指标（测量值）不能当 DOE 因子：它不可独立设定，是其它因子的函数。
                # 例如「涂布面密度」由固含/泵速/速度决定，把它和固含并列进正交表是逻辑错误。
                if spec.get("role") == "response":
                    excluded.append((nm, "结果指标（测量值），不可直接设定"))
                    continue
                # 编号/标识类列不是工艺参数，调不了也不能调。
                # 把"通道号、柜号"当因子写进正交表，产出的方案在现场根本没法执行，
                # 却长得像一份正经实验方案——这比不给方案更危险。
                if re.search(r"(通道号|柜号|库位号|机台号|设备号|批次号|卷号|卷芯号|托盘号|"
                             r"序号|编号|条码|编码|电芯号|夹具号)$", nm) or \
                   re.search(r"(^|[^A-Za-z])(id|ID)$", nm):
                    excluded.append((nm, "编号/分组标识，不是可设定的工艺参数"))
                    continue
                # 共线性去重：高度相关的参数只保留得分最高的一个
                dup = False
                for c in chosen_cols:
                    cc = _corr_of(corr, nm, c)
                    if cc is not None and abs(cc) > 0.7:
                        dup = True
                        break
                if dup:
                    excluded.append((nm, "与已选因子强相关（|r|>0.7），存在共线性"))
                    continue
                # 非数值的分类列（供应商、班次、操作员）定不出连续水平：
                # 分位数算出来会是字符串或 0/1，写进正交表也没有物理意义。
                # 这类变量要考察就作为区组单独安排，不能混进响应面因子。
                _probe = [v for v in (work.numeric_column(nm)
                                      if work.col_index(nm) is not None else [])
                          if v is not None]
                if not _probe:
                    excluded.append((nm, "非数值/分类列，无法定水平（需考察时作为区组另行安排）"))
                    continue
                chosen_cols.append(nm)
                vals = _probe
                if len(vals) >= 5:
                    lo, hi = S.quantile(vals, 0.10), S.quantile(vals, 0.90)
                    mid = S.median(vals)
                else:
                    rng = spec.get("typical_range") or [0, 1]
                    lo, hi, mid = rng[0], rng[1], sum(rng) / 2
                if lo is not None and hi is not None and hi > lo:
                    pad = (hi - lo) * 0.15
                    factors.append({"name": nm, "unit": spec.get("unit", ""),
                                    "low": round(lo - pad, 4), "high": round(hi + pad, 4),
                                    "mid": round(mid, 4) if mid else None,
                                    "basis": "当前数据 P10/P90 外扩 15%" if vals else "知识库典型量程"})
                if len(chosen_cols) >= 4:
                    break
            # 因子水平必须先过工程约束与安全包络。
            # 按数据分位数定出来的水平，很可能落在工艺窗口之外甚至安全边界之外——
            # 这种实验方案一旦下发就不是"良率差一点"，是事故。
            _kb_ctl = {}
            for f in factors:
                _k = col_map.get(f["name"])
                if _k and KB["params"].get(_k, {}).get("control"):
                    _kb_ctl[f["name"]] = KB["params"][_k]["control"]
            _cons = DOE.build_constraints(factors, spec_overrides=spec_overrides,
                                          kb_control=_kb_ctl)
            _clamped = []
            for f in factors:
                c = _cons.get(f["name"]) or {}
                lo0, hi0 = f.get("low"), f.get("high")
                lo, hi = lo0, hi0
                if c.get("min") is not None and lo is not None and lo < c["min"]:
                    lo = c["min"]
                if c.get("max") is not None and hi is not None and hi > c["max"]:
                    hi = c["max"]
                if (lo is None or hi is None or hi <= lo) and (c.get("min") is not None
                                                               or c.get("max") is not None):
                    _clamped.append({"factor": f["name"], "from": [lo0, hi0], "to": None,
                                     "reason": "水平区间完全落在工艺/安全窗口之外，已剔除出因子表"})
                    f["blocked"] = True
                    continue
                if (lo, hi) != (lo0, hi0):
                    _clamped.append({"factor": f["name"], "from": [lo0, hi0], "to": [lo, hi],
                                     "reason": "已收紧到工艺窗口/安全包络内"})
                f["low"], f["high"] = lo, hi
                if f.get("mid") is not None:
                    if lo is not None and f["mid"] < lo:
                        f["mid"] = lo
                    if hi is not None and f["mid"] > hi:
                        f["mid"] = hi
            factors = [f for f in factors if not f.get("blocked")]
            result["doe_constraints"] = _cons
            result["doe_clamped"] = _clamped

            # P1-8 受控边界门禁：报告的验收第 7 项要求
            # 「缺少有效边界、边界冲突或单位不明时应阻断方案生成，不能默认放行」。
            # 出证据样例：只是一对 min/max 不够，还要回答它凭什么。
            try:
                _bounds = DOE.controlled_boundaries(
                    factors, spec_overrides=spec_overrides, kb_control=_kb_ctl)
                _gate = DOE.boundary_gate(_bounds, policy=boundary_policy)
            except Exception as _bg_e:
                degrade.note("pipeline.analyze@boundary_gate", _bg_e)
                _bounds, _gate = {}, {"ok": False,
                                      "blockers": ["边界门禁自身未跑成（%s），按阻断处理" % _bg_e],
                                      "warnings": [], "n_factors": len(factors)}
            result["doe_boundaries"] = _bounds
            result["doe_boundary_gate"] = _gate

            if not _gate.get("ok"):
                doe_plan = {"factors": [], "blocked": True,
                            "blockers": _gate.get("blockers") or [],
                            "boundaries": _bounds,
                            "skipped_reason": "边界门禁阻断：%s"
                            % "；".join((_gate.get("blockers") or [])[:3])}
                result.setdefault("warnings", []).append(
                    "DOE 方案已阻止生成（受控边界不满足）：%s"
                    % "；".join((_gate.get("blockers") or [])[:2]))
            elif len(factors) >= 2:
                opts = DOE.recommend_design(len(factors), levels=2, budget=32)
                chosen = opts[0]
                matrix = DOE.build_design(chosen["kind"], len(factors), 2, chosen.get("param"))
                plan = DOE.decode_design(matrix, factors) if matrix else []
                # P1-7：矩阵只是"做哪些组合"，不是可执行方案。随机化顺序、重复、
                # 区组、别名结构、样本量功效这五件事不写出来，现场只能靠习惯补。
                _struct = None
                if matrix:
                    try:
                        _struct = DOE.design_structure(
                            matrix, [f.get("name") for f in factors],
                            n_replicates=doe_replicates, n_blocks=doe_blocks,
                            seed=doe_seed)
                    except Exception as _ds_e:
                        degrade.note("pipeline.analyze@design_structure", _ds_e)
                        _struct = None
                doe_plan = {"factors": factors, "options": opts, "excluded": excluded,
                            "chosen": chosen, "matrix": matrix, "run_plan": plan,
                            "constraints": _cons, "clamped": _clamped}
                doe_plan["boundaries"] = _bounds
                doe_plan["boundary_gate"] = _gate
                if _struct:
                    doe_plan["design_structure"] = _struct
                    # 只把"会让人下错结论"的那几条顶到 warnings，其余留在设计结构里自查
                    for _c in (_struct.get("caveats") or []):
                        if _c.get("level") == "high":
                            result.setdefault("warnings", []).append(
                                "DOE 设计结构：%s" % _c.get("text"))
                _src_note = ""
                for _w in (_gate.get("warnings") or []):
                    _src_note += "⚠ %s；" % _w
                doe_plan["safety_note"] = (
                    "以上水平均已限制在工艺窗口与安全包络内（安全边界只可收紧、不可放宽）。"
                    + _src_note +
                    "本方案为**待验证实验方案**，不是调机指令：需经工艺工程师确认、"
                    "小批试跑并复核安全条件后方可执行。设计矩阵的取值未经试验验证，"
                    "不得据此宣称已得到最优参数。")
            else:
                # 因子凑不齐时，必须说清为什么没有方案，而不是悄悄不给
                doe_plan = {"factors": [], "excluded": excluded}
                doe_plan["skipped_reason"] = (
                    "可用因子不足 2 个：其余列是结果指标（不可设定）、编号/分组标识，"
                    "或与已选因子强相关。凑不齐就不给实验方案——"
                    "拿编号和测量值凑出来的正交表，执行不了也没有意义。")
        result["doe"] = doe_plan

    except Exception as _guard_e:
        result.setdefault("degraded_sections", []).append(
            {"section": "DOE", "error": str(_guard_e)})
    try:  # 护栏：知识沉淀候选（只产出候选，绝不自动改写知识库） 环节失败只降级本节，不中断整轮
        # 13. 知识沉淀候选（只产出候选，绝不自动改写知识库）
        try:
            cands = KBS.harvest_from_analysis(result, work)
        except Exception as e:                       # 挖不出候选不该拖垮主流程
            cands = []
            result.setdefault("_kberr", []).append("知识候选挖掘失败：%s" % e)
        result["kb_candidates"] = cands
        if cands:
            try:
                with io.open(os.path.join(outdir, "kb_candidates.json"), "w", encoding="utf-8") as f:
                    json.dump({"generated_at": result["generated_at"],
                               "process": result.get("process_name"),
                               "target": result.get("target"),
                               "hint": "本文件由分析自动生成，仅为候选。确认无误后执行："
                                       "python scripts/kb_manage.py harvest --file 本文件",
                               "candidates": cands}, f, ensure_ascii=False, indent=2)
            except Exception as _dg_e:
                degrade.note("pipeline.analyze@L1289", _dg_e)

    except Exception as _guard_e:
        result.setdefault("degraded_sections", []).append(
            {"section": "知识沉淀候选（只产出候选，绝不自动改写知识库）", "error": str(_guard_e)})
    # ------------------------------------------------------------------
    # 13b. 汇总各护栏环节的状态：没跑出来的环节必须显性，不能被读成"没问题"
    #
    #    每一节都包在自己的 try 里，失败只降级本节 —— 这是对的。
    #    但这些节的输出变量是在 try **内部**赋值的，而 build_report 在 try 外面
    #    统一引用。一旦某一节真的失败，变量从未赋值，整轮会以
    #    UnboundLocalError 收场，报错文本指向这个变量名，
    #    把真正的原因（那一节里记录在案的错误）彻底盖住。
    #
    #    AST 全量排查过 analyze() 内 22 处同型风险。集中在这里初始化，
    #    比逐个在 except 里补默认值可靠：任何一个 try 加/删都不会再漏。
    # ------------------------------------------------------------------
    degraded = result.get("degraded_sections") or []
    if degraded:
        result["incomplete"] = True
        result["module_summary"] = [
            {"section": d.get("section"), "status": d.get("status") or ST.FAILED,
             "error": d.get("error")}
            for d in degraded]
        result.setdefault("warnings", []).append(
            "本轮有 %d 个分析环节未产出结论（%s）：这些环节是**执行失败**，"
            "不等于「检查过、没有问题」。相关章节为空属实，请按下述错误排查后重跑。"
            % (len(degraded), "、".join(str(d.get("section")) for d in degraded)))
        for d in degraded:
            if not d.get("status"):
                d["status"] = ST.FAILED
                d.setdefault(
                    "impact", "本节结论缺失，不代表『没有问题』")

    # 14. 生成报告
    rep = build_report(result, work, spc_res, corr, assoc, pare, root, gaps, conf,
                       doe_plan, gcomp, lags, strong, tcol, times, max_charts)
    files = RPT.save_report(rep, outdir, "工艺分析报告")
    # Word 原生导出：报告要进 8D 系统、要发客户、要走签核，对面要的是 .docx。
    # 以前只有 HTML 可打印，使用者得自己转一遍——多一次手工转换就多一次
    # "报告版本和实际结论对不上"的机会。
    if export_docx and EX is not None:
        try:
            _dp = EX.write_docx(rep, os.path.join(outdir, "工艺分析报告.docx"),
                                chart_dir=os.path.join(outdir, "charts"))
            files["Word"] = _dp
        except Exception as _dx_e:
            # 导出失败不带走主报告，但必须出声：少了交付形态而不说明，
            # 使用者会以为导出过、只是没找到文件
            degrade.note("pipeline.export@docx", _dx_e)
            result.setdefault("warnings", []).append(
                "Word(.docx) 导出失败：%s。HTML/Markdown 已正常生成，"
                "Word 版请先用 HTML 打印转换" % _dx_e)
            result.setdefault("degraded_sections", []).append(
                {"section": "Word 导出", "error": str(_dx_e)})
    if export_pdf and EX is not None:
        try:
            _pp = EX.write_pdf(rep, os.path.join(outdir, "工艺分析报告.pdf"))
            files["PDF"] = _pp
        except Exception as _pf_e:
            degrade.note("pipeline.export@pdf", _pf_e)
            result.setdefault("warnings", []).append(
                "PDF 导出失败：%s。HTML/Markdown 已正常生成" % _pf_e)
            result.setdefault("degraded_sections", []).append(
                {"section": "PDF 导出", "error": str(_pf_e)})
    if export_xlsx:
        try:
            xp = export_result_xlsx(result, work, outdir)
            if xp:
                files["结果表"] = xp
        except Exception as _xl_e:
            # 结果表只是交付形态之一，出错绝不能把整轮分析的结果带走
            result.setdefault("warnings", []).append("Excel 结果表导出失败：%s" % _xl_e)
    kbc = os.path.join(outdir, "kb_candidates.json")
    if os.path.exists(kbc):
        files["知识候选"] = kbc

    # 14c. 角色视角产物。
    #      同一份 result，按角色重排与改措辞——不重算，所以不存在五套结论
    #      各自漂移的风险。视角失败只降级，不带走主报告。
    if views:
        try:
            from . import views as VIEWS
            want = (list(VIEWS.VIEW_KEYS) if "all" in views else list(views))
            vres = VIEWS.build_all(result, outdir, views=want)
            result["views"] = {"requested": list(views),
                               "files": vres.get("files") or []}
            for e in (vres.get("errors") or []):
                result.setdefault("warnings", []).append("视角产物：%s" % e)
            for i, p in enumerate(vres.get("files") or []):
                files["视角产物%02d" % (i + 1)] = p
        except Exception as _vw_e:
            # 视角是交付形态，不是结论本身：它挂了不能让整轮分析白跑
            result.setdefault("warnings", []).append(
                "角色视角产物生成失败（主报告不受影响）：%s" % _vw_e)
            degrade.note("pipeline.analyze@views", _vw_e)
    # 14b. 降级台账收口。
    #     沿途各模块在 except 里记的每一笔在这里汇总：让「这一节没结论」
    #     能被区分为「确实没有」与「算崩了」。没有记录就不加这一节。
    try:
        _dg = degrade.drain()
        if _dg:
            result["degradation_log"] = _dg
            result.setdefault("warnings", []).extend(degrade.summary_lines())
    except Exception as _dg_e:
        degrade.note("pipeline.analyze@L1319", _dg_e)
    result["outputs"] = files

    # P1-4 契约的下半场：出口也要校验。
    # 这里不因为有 violation 就中断——报告已经生成到一半了，
    # 掐掉使用者什么都拿不到。做法是把问题挂到 contract_report 上并进 warnings，
    # 让报告自己承认"哪些环节没能证明自己算过"。
    try:
        _cr = CT.check_result(result)
        result["contract_report"] = {
            "schema_version": CT.SCHEMA_VERSION,
            "ok": _cr.get("ok"),
            "missing_required": _cr.get("missing_required") or [],
            "sections_without_status": _cr.get("missing_status") or [],
            "violations": _cr.get("violations") or [],
        }
        for _v in (_cr.get("violations") or []):
            result.setdefault("warnings", []).append("数据契约：%s" % _v)
    except Exception as _exc:
        degrade.note("pipeline.contract@check_result", _exc)

    result["summary"] = build_summary(result)

    # 血缘收口：先把状态定下来再落盘。
    # 有环节没产出结论时记 PARTIAL_SUCCESS 而不是 SUCCESS——这两者的区别正是
    # P0-3 要求守住的地方：写成 SUCCESS，半年后就没人知道那几个章节是空的。
    try:
        _files_digest = []
        for _k, _v in sorted((files or {}).items()):
            try:
                _st = os.stat(_v)
                _files_digest.append({"name": _k, "file": os.path.basename(_v),
                                      "bytes": _st.st_size})
            except Exception:
                _files_digest.append({"name": _k, "file": os.path.basename(str(_v))})
        PROV.finish(
            _prov,
            status=ST.PARTIAL_SUCCESS if result.get("incomplete") else ST.SUCCESS,
            output_digest={"outputs": _files_digest,
                           "n_outputs": len(files or {}),
                           "incomplete": bool(result.get("incomplete")),
                           "module_summary": result.get("module_summary") or [],
                           "n_warnings": len(result.get("warnings") or [])},
            outdir=outdir,
            warnings=list(result.get("warnings") or [])[:30])
    except Exception as _pv_e:
        # 血缘写不出来要出声，但不能因此把整轮分析的结果带走
        result.setdefault("warnings", []).append("血缘记录落盘失败：%s" % _pv_e)
        degrade.note("pipeline.analyze@provenance_finish", _pv_e)
    # P2-4 可观测性：整轮跑完必须留下"慢在哪、哪一步失败过、失败属于哪一类"。
    # 没有它，事后只能凭印象猜——而猜错方向比不知道更贵。
    try:
        if OBS is None:
            raise RuntimeError("可观测性模块未导入：本次运行无可观测数据")
        OBS.end_run(n_warnings=len(result.get("warnings") or []),
                    incomplete=bool(result.get("incomplete")))
        result["observability"] = OBS.summary()
        _obs_bad = (result["observability"].get("self_errors") or [])
        if _obs_bad:
            # 诊断通道自己带病必须出声：它让人以为"查过了"，其实没查全
            result.setdefault("warnings", []).append(
                "可观测性模块自身有 %d 次记录失败，本次运行的可观测数据不完整"
                % len(_obs_bad))
    except Exception as _obs_e:
        degrade.note("pipeline.analyze@observability", _obs_e)
    return result


# ------------------------------------------------------------------ 报告组装

def _corr_of(corr, a, b):
    """从相关矩阵取两列的相关系数，取不到返回 None"""
    try:
        i = corr["columns"].index(a)
        j = corr["columns"].index(b)
        return corr["matrix"][i][j]
    except (ValueError, IndexError, KeyError):
        return None


_DERIVED_HINT = ("比", "率", "损失", "效率", "比值", "ratio", "loss", "ctm",
                 "偏差率", "占比", "指数")


def _pick_redundant(c1, c2, r1, r2, kb=None):
    """
    两列高度共线时，决定丢掉哪一列。

    不能只比"谁跟目标更相关"——CTM 与 Pmax 对目标的相关性几乎一样，
    随机丢一个就会把真正测出来的功率丢掉、留下算出来的封装损失比，
    报告里就成了"功率低是因为封装损失高"，而损失比本身就是功率算出来的。
    优先丢派生量：知识库标了 response 的，或名字带「比/率/损失/效率」的。
    """
    def _is_derived(c):
        nm = str(c or "")
        low = nm.lower()
        if any(h in low for h in _DERIVED_HINT):
            return True
        if kb:
            for k, v in (kb.get("params") or {}).items():
                if v.get("name") == c or k == c:
                    return v.get("role") == "response"
        return False
    d1, d2 = _is_derived(c1), _is_derived(c2)
    if d1 and not d2:
        return c1
    if d2 and not d1:
        return c2
    if r1 > r2:
        return c2
    if r2 > r1:
        return c1
    return max(c1, c2)                               # 完全持平时给个确定答案


def _resolve_msa(spec, table=None, target=None):
    """把「测量系统登记」解析成结论 dict（含 action）。没人登记过就返回 None。

    为什么宁可返回 None 也不返回一个"默认合格"：
        "没登记"和"量过了、合格"在工具眼里必须长得不一样。
        返回默认值等于替使用人宣布"尺子没问题"，而没有任何证据支持这句话。
        （读表失败同理 —— 那读不出来是事故，不是事实。）

    三种给法：
        {"path": ...}                        专用 MSA 表（自动或显式认列）
        {"pct_grr": 18.0, "ndc": 4, ...}     直接登记企业已有的 GRR 结论
        不给                                  尝试从主数据自动识别重复测量结构
    """
    if MSA is None:
        return None
    if isinstance(spec, dict) and spec.get("pct_grr") is not None:
        try:
            _pct = float(spec["pct_grr"])
        except (TypeError, ValueError):
            return {"kind": "error",
                    "reason": "登记的 pct_grr 不是数字：%r（按未登记处理）" % (spec.get("pct_grr"),)}
        _lv = MSA.classify(_pct, spec.get("ndc"), grading=spec.get("grading"))
        out = {"status": ST.SUCCESS, "method": "registered",
               "pct_grr": round(_pct, 2), "ndc": spec.get("ndc"),
               "source": spec.get("source") or "企业登记",
               "doc_id": spec.get("doc_id"), "review_by": spec.get("review_by"),
               "note": "登记值由企业出具、本工具未重算；分级按 AIAG MSA 公开口径"}
        out.update(_lv)
        return out

    _path = spec.get("path") if isinstance(spec, dict) else spec
    if _path:
        _tb = T.read_any(_path)
        if _tb is None:
            return {"kind": "error",
                    "reason": "测量系统数据读不出来：%s（未纳入本次判定，"
                              "按未登记处理——读不出来是事故，不能当作企业没做）"
                              % _path}
        _part = spec.get("part") if isinstance(spec, dict) else None
        _appr = spec.get("appraiser") if isinstance(spec, dict) else None
        _val = spec.get("value") if isinstance(spec, dict) else None
        if not (_part and _val):
            _st = MSA.detect_msa_structure(_tb, target_col=_val)
            if _st:
                _part, _appr, _val = (_st.get("part_col"), _st.get("appraiser_col"),
                                      _st.get("value_col"))
            else:
                return {"kind": "error",
                        "reason": "在 %s 里认不出「零件/测量者/测量值」三列，"
                                  "请显式指定后用 --msa-part/--msa-appraiser/--msa-value 重跑"
                                  % _path}
        _tol = spec.get("tolerance") if isinstance(spec, dict) else None
        _k = spec.get("k") if isinstance(spec, dict) else None
        _res = MSA.from_table(_tb, _part, _appr, _val, tolerance=_tol,
                              k=_k if _k else MSA.K99)
        if isinstance(_res, dict):
            _res["source"] = "MSA 数据表：%s" % _path
        return _res

    # 没给登记也没给文件：从主数据里找重复测量结构。
    # 证据强度不如专门 MSA 计划表，但足以把"尺子可能不准"这件事摆到台面上。
    if table is not None:
        _st = MSA.detect_msa_structure(table, target_col=target)
        if _st:
            _res = MSA.from_table(table, _st.get("part_col"),
                                  _st.get("appraiser_col"), _st.get("value_col"))
            if isinstance(_res, dict):
                _res["source"] = "自动识别（%s / %s / %s）" % (
                    _st.get("part_col"), _st.get("appraiser_col") or "(单一来源)",
                    _st.get("value_col"))
                _res["structure"] = _st
                _res["auto_detected"] = True
            return _res
    return None


def data_driven_rootcause(assoc, spc_res, col_map, process_key, y_col, recog=None, corr=None,
                          exclude_cols=None):
    """未指定缺陷类型时的兜底归因：纯数据驱动，不使用缺陷机理库。

    标注清楚"未使用机理库"，避免用户误以为这是经过机理验证的结论。
    """
    kb = KB
    cands = []
    # 预先算好「每个数值列与目标的相关」和「列与列之间相关」，供链式归因用
    r_with_y = {}
    cols = []
    if corr:
        cols = list(corr.get("columns") or [])
        if y_col in cols:
            yi = cols.index(y_col)
            for j, c in enumerate(cols):
                try:
                    v = corr["matrix"][yi][j]
                    if v is not None:
                        r_with_y[c] = abs(v)
                except Exception as _dg_e:
                    degrade.note("pipeline.data_driven_rootcause@L1393", _dg_e)
    # 共线对处理：两列 |r|≥0.95 说明它们在量同一件事，只保留与目标更相关的那个。
    # 组件 IV 里 CTM 与 Pmax 就是 r=1.000 —— 两个都留在候选里，等于说
    # 「功率低是因为功率低」。注意判据是列与列共线，不是列与目标共线：
    # CTM 与目标的 r 只有 0.59，光看它到目标的相关性发现不了这个重复。
    excluded = set()
    if r_with_y and corr and cols:
        for _i, c1 in enumerate(cols):
            for c2 in cols[_i + 1:]:
                r12 = _corr_of(corr, c1, c2)
                if r12 is None or abs(r12) < 0.95:
                    continue
                r1, r2 = r_with_y.get(c1, 0.0), r_with_y.get(c2, 0.0)
                loser = _pick_redundant(c1, c2, r1, r2, kb)
                excluded.add(loser)
    skipped = []
    for i, a in enumerate(assoc[:10]):
        nm = a["param"]
        # v1.10 护栏：目标本身、被 STC 替换的原始列、测试条件、编号/时间/坐标列都不是原因
        if (nm == y_col or nm in set(exclude_cols or []) or QG.is_test_condition(nm)
                or _is_bin_label(nm) or QG.is_internal_column(nm)
                or QG.column_role(nm) not in ("measure", "count")):
            skipped.append(nm)
            continue
        if nm in excluded or r_with_y.get(nm, 0) >= 0.95:
            skipped.append(nm)
            continue
        key = None
        for c, k in col_map.items():
            if c == nm:
                key = k
                break
        spec = kb["params"].get(key, {}) if key else {}
        ev = a.get("abs_effect") or 0.0
        # 结果指标（response）与目标高度共线是自然的事——它往往就是结果的另一种量法，
        # 不是独立原因（例如 CCD 对位偏差≈极片错位、TTV≈TTV超差判据本身）。
        # 排在可控因子前面会让人去"调一个其实只能被观测的量"，所以降权并标注。
        is_response = spec.get("role") == "response"
        if is_response:
            ev *= 0.55

        # 链式传导：X → 中介 M → 目标。产线上大量根因是间接的，
        # 焊带电阻就是经串联电阻 Rs 影响功率的：它到目标的直接相关只有 0.36，
        # 而 Rs 到目标 0.40、焊带到 Rs 0.88 —— 只看直接相关，真因就排不进候选。
        indirect, via = 0.0, None
        if r_with_y and nm in cols and corr:
            for m in cols:
                if m in (nm, y_col):
                    continue
                rm = r_with_y.get(m) or 0.0
                rxm = _corr_of(corr, nm, m)
                # 中介必须与 X 是两回事：|r|≥0.95 说明它只是 X 的另一种写法，
                # 拿它当中介会让共线的一对互相"传导"，凭空抬高分数
                if rxm is None or abs(rxm) < 0.6 or abs(rxm) >= 0.95 or rm < 0.5:
                    continue
                v = abs(rxm) * rm
                if v > indirect:
                    indirect, via = v, m
        # 链式加成只给可控因子。结果指标（翘曲、弯曲度这类"同一现象的另一种量法"）
        # 本来就该降权，再给它按传导路径加分，就会把不可调控的量顶到真因前面 ——
        # 实测 E04 里翘曲/弯曲度经 TTV 传导后压过了真正可调的进给速度。
        chain_note = ""
        if via and indirect > (ev or 0) * 1.15 and not is_response:
            ev = max(ev or 0.0, indirect * 0.9)
            chain_note = ("经「%s」间接传导到目标（直接 r=%.2f，传导强度 %.2f）"
                          % (via, a.get("r") or 0.0, indirect))
        cands.append({
            "param_key": key, "param": nm, "param_name": spec.get("name", nm),
            "unit": spec.get("unit", ""),
            "role": spec.get("role") or "",
            "score": round(ev * (1 - i * 0.08), 4),
            "evidence_score": round(ev, 3),
            "prior_score": 0.0, "temporal_score": 0.0,
            "data_status": ST.SUCCESS,
            "detail": {"status": ST.SUCCESS, "param": key or nm, "column": nm,
                       "r": a.get("r"), "p": a.get("p"), "n": a.get("n"),
                       "via": via, "indirect": round(indirect, 3) if via else None,
                       "note": chain_note or (
                           "该列是结果指标，与目标高度共线，属同一现象的不同量法，"
                           "不是可独立调控的原因" if is_response
                           else "数据驱动排序，未经缺陷机理库加权")},
            "note": spec.get("note", ""),
        })
    if not cands:
        return None
    cands.sort(key=lambda d: -d["score"])
    proc = kb["processes"].get(process_key, {})
    # 所有候选都弱到这个程度，说明这一份数据根本支撑不出根因。
    # 硬凑一个"首要根因"发出去，比承认认不出更危险——现场会照着去调机。
    #
    # 另外：列名识别率过低时，即使某个相关系数看起来不低也不能下结论 ——
    # 连这一列测的是什么都不知道，r 再大也只是一串数字之间的关系，
    # 拿去指导调机就是拿噪声当真因。分数阈值挡不住这种情况（实测三个种子下
    # 最高分 0.23~0.33 都越过了阈值但识别率只有 9%），必须单独判。
    weak = cands[0]["score"] < 0.20
    blind = recog is not None and recog < 0.25
    insufficient = bool(weak or blind)
    reason = ("列名识别率过低（%.0f%%），无法确定各列测的是什么，相关关系无法解读"
              % (recog * 100)) if blind else "所有候选的统计证据都太弱"
    return {
        "defect": "（未指定缺陷类型 · 数据驱动归因）",
        "evidence_insufficient": insufficient,
        "co_linear_excluded": skipped,
        "insufficient_reason": reason if insufficient else None,
        "process": process_key or "",
        "process_name": proc.get("name", ""),
        "mechanism": None,
        "verify_data": [],
        "quick_checks": [
            "先按设备号/批次号/班次做分层，确认问题是全局还是集中在某一单元",
            "确认目标列「%s」的异常起始时间点，与该时段的工程变更做比对" % (y_col or "—"),
        ],
        "candidates": cands,
        "all_candidates": cands,
        "data_driven": True,
    }


def build_summary(res):
    s = []
    proc = res.get("process_name") or "未识别工序"
    s.append("工序判定：%s（样本 %s 行 × %s 列）"
             % (proc, res.get("shape", [0, 0])[0], res.get("shape", [0, 0])[1]))
    spc = res.get("spc") or []
    out_of_ctrl = [d for d in spc if d.get("violations")]
    if out_of_ctrl:
        names = "、".join(d["param_name"] or d["column"] for d in out_of_ctrl[:3])
        s.append("SPC 判异：%d 个参数触发判异规则（%s）" % (len(out_of_ctrl), names))
    else:
        s.append("SPC 判异：未检出违反 Nelson 规则的参数")
    # 只有企业规格书给出的工程规格才用来判"能力不足"。
    # 知识库推定的限本质是统计控制限，拿它算 Cpk 会造出一堆假警报
    # （例如涂布速度 Cpk 0.21，其实它根本没有工程规格）。
    low = [d for d in spc if d.get("spec_source") == "企业规格书"
           and (d.get("capability") or {}).get("Cpk") is not None
           and d["capability"]["Cpk"] < 1.33]
    if low:
        s.append("过程能力不足（Cpk<1.33，按企业规格书判定）：%s"
                 % "、".join("%s(%.2f)" % (d["param_name"] or d["column"], d["capability"]["Cpk"])
                            for d in low[:4]))
    inferred = [d for d in spc if d.get("spec_source") == "知识库"
                and (d.get("capability") or {}).get("Cpk") is not None
                and d["capability"]["Cpk"] < 1.33]
    if inferred:
        s.append("另有 %d 项 Cpk 偏低但**缺少企业规格**（%s），仅作提示，不能据此判定不合格"
                 % (len(inferred), "、".join(d["param_name"] or d["column"] for d in inferred[:3])))
    top = (res.get("association") or [])[:3]
    if top:
        s.append("强关联参数：" + "；".join(
            "%s（r=%.2f, %s）" % (d["param"], d.get("r", 0),
                                  "显著" if d.get("significant") else "不显著")
            for d in top if d.get("r") is not None))
    rc = res.get("rootcause")
    if rc and rc.get("candidates"):
        c = rc["candidates"][0]
        if QG.rc_strong(rc):
            s.append("首要候选根因：%s（得分 %.2f，%s）"
                     % (c["param_name"], c["score"], c["data_status"]))
        else:
            s.append("支撑不出根因结论：最高候选 %s 证据得分 %.2f，低于 %.2f 门槛，"
                     "仅作排查方向，不可据此调整工艺参数"
                     % (c["param_name"], c.get("evidence_score") or 0, QG.EVIDENCE_GATE))
    if res.get("pareto") and res["pareto"].get("top"):
        s.append("Pareto 首位：%s（占比 %.1f%%）"
                 % (res["pareto"]["top"]["item"], res["pareto"]["top"]["pct"]))
    s.append("结论置信度：%s（%.0f%%）" % (res["confidence"]["level"], res["confidence"]["score"] * 100)
             if res.get("confidence") else "结论置信度：未评估")
    return s


def _add_history_cases(rep, sec, root):
    """把历史上被 PDCA/PFMEA 验证过的对策摆到根因旁边，避免重复试错"""
    cases = []
    for c in root.get("candidates") or []:
        h = c.get("history_case")
        if h:
            cases.append((c["param_name"], h))
    if not cases:
        return
    rows = []
    for pname, h in cases:
        r = h.get("result") or {}
        before, after = r.get("before"), r.get("after")
        n_txt = "—"
        if isinstance(r.get("sample_size"), (int, float)):
            n_txt = str(int(r["sample_size"])) if float(r["sample_size"]).is_integer() \
                else str(r["sample_size"])
        delta = ""
        if before is not None and after is not None:
            try:
                delta = "%.4g → %.4g %s（n=%s）" % (before, after, r.get("unit") or "", n_txt)
            except Exception:
                delta = "—"
        rows.append([pname, h.get("level") or "—", h.get("action") or "—",
                     delta or "—", h.get("doc_no") or "未固化", h.get("source") or "—"])
    rep.table(sec, ["关联参数", "证据级别", "历史有效对策", "实测改善", "标准化文件", "来源"],
              rows, caption="历史已验证对策（来自改善措施库，A 级可直接复用，B 级建议先小批验证）")
    if any(r[1] == "B" for r in rows):
        rep.callout(sec, "存在 B 级对策：曾在现场生效但样本量或标准化不足，复用前建议做小批确认，"
                         "并在验证后把结果回填到改善措施库（kb_manage.py propose "
                         "--type improvement_case）。", "warn")


def build_report(res, work, spc_res, corr, assoc, pare, root, gaps, conf,
                 doe_plan, gcomp, lags, strong, tcol, times, max_charts):
    try:
        from . import scope as SC
    except Exception:
        import scope as SC                           # noqa
    meta = {
        "数据源": ", ".join(os.path.basename(p) for p in res["inputs"]),
        "工序": res.get("process_name"),
        "样本量": "%d 行 × %d 列" % tuple(res.get("shape", [0, 0])),
        "分析目标列": res.get("target") or "未指定",
        "置信度": "%s（%s）" % (conf["level"], conf["note"]) if conf else "—",
        "生成时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    rep = RPT.Report("新能源产线工艺数据分析报告", meta)
    n_chart = [0]

    def add_chart(sec, svg, caption):
        if not svg or n_chart[0] >= max_charts:
            return
        n_chart[0] += 1
        rep.chart(sec, svg, caption, fname="chart_%02d.svg" % n_chart[0])

    # 1 结论摘要
    s1 = rep.section("1. 结论摘要")
    rep.bullets(s1, build_summary(res))
    if conf:
        rep.callout(s1, "置信度说明：%s。P0 级数据缺口会显著降低结论可靠性，"
                        "建议按第 11 节清单补充后复算。" % conf["note"],
                    "warn" if conf["score"] < 0.6 else "info")

    # 2 数据与预处理
    s2 = rep.section("2. 数据来源、设备识别与预处理")
    devs = res.get("devices") or []
    if devs:
        d0 = devs[0]
        rep.kv(s2, [("来源设备", d0.get("device_name") or "未识别"),
                    ("厂商/类别", "%s / %s" % (d0.get("vendor", "—"),
                                   DEV.get_profile(d0.get("device")).get("category", "—")
                                   if d0.get("device") else "—")),
                    ("识别置信度", d0.get("confidence") or "—"),
                    ("识别得分", round(d0.get("score") or 0, 1))])
        if d0.get("evidence"):
            rep.bullets(s2, ["识别依据：" + e for e in d0["evidence"][:5]])
        if d0.get("runner_up"):
            rep.callout(s2, "次优候选为 `%s`（得分 %.1f），与首选差距不大。"
                            "若现场确认设备是它，请显式指定或补强该设备档案的识别特征。"
                            % (d0["runner_up"][0], d0["runner_up"][1]), "info")
        if d0.get("export_note"):
            rep.text(s2, "该设备的导出说明：%s" % d0["export_note"])
        _fmt_notes = [n for n in (d0.get("notes") or []) if "识别为" not in n]
        if _fmt_notes:
            rep.callout(s2, "**版式适配动作**（把不同设备的导出格式归一为标准宽表）：", "info")
            rep.bullets(s2, _fmt_notes)
        if d0.get("units_applied"):
            rep.table(s2, ["列", "原单位", "统一为", "换算系数", "单位来源"],
                      [[u["column"], u["from"], u["to"], "×%g" % u["factor"],
                        u.get("source", "—")] for u in d0["units_applied"]],
                      caption="单位换算（不统一会出现数量级误判，例如把 Ω 当 mΩ 会让内阻看起来小 1000 倍）")
        if d0.get("attr_applied"):
            rep.table(s2, ["判定列", "已编码条数", "总计"],
                      [[a["column"], a["coded"], a["of"]] for a in d0["attr_applied"]],
                      caption="合格判定口径统一（1=合格 / 0=不合格）")
        if d0.get("warnings"):
            rep.callout(s2, "适配告警：", "warn")
            rep.bullets(s2, d0["warnings"][:8])
        if not d0.get("device"):
            rep.callout(s2, "本文件未匹配到已知设备档案，已按通用版式规则处理。"
                            "若该设备在企业内长期使用，建议登记为设备档案：用 "
                            "<code>kb_manage.py propose --type device_profile</code> 提交，"
                            "登记后列名识别率与版式处理都会明显提升。", "warn")
    cr = res.get("clean", {})
    rep.kv(s2, [("原始行数", cr.get("rows_in")), ("清洗后行数", cr.get("rows_out")),
                ("时间列", tcol or "未识别"), ("分组维度", "、".join(res.get("groups", [])) or "未识别")])
    if cr.get("actions"):
        rep.bullets(s2, cr["actions"])
    if cr.get("outliers"):
        rep.table(s2, ["参数", "异常点数", "判定区间", "示例值"],
                  [[k, v["count"], "[%.4g, %.4g]" % tuple(v["bound"]),
                    ", ".join(str(x) for x in v["samples"][:5])]
                   for k, v in list(cr["outliers"].items())[:8]],
                  caption="统计异常值（MAD 法，仅标记不影响主表）")
    if cr.get("physical_violations"):
        rep.table(s2, ["参数", "数量", "合理量程", "示例值"],
                  [[k, v["count"], "[%.4g, %.4g]" % tuple(v["bound"]),
                    ", ".join(str(x) for x in v["samples"][:5])]
                   for k, v in cr["physical_violations"].items()],
                  caption="超出物理量程的点（已剔除）")
    rep.callout(s2, "清洗策略：异常值使用 MAD（中位数绝对偏差）稳健统计，仅标记不删除，"
                    "避免把真实的工艺异常当作噪声抹掉——这在工艺诊断里是常见误判来源。", "info")

    # 2b 字段适配与冲突处理（企业数据与标准参数对不上时的处置留痕）
    ad = res.get("adaptation") or {}
    if ad or res.get("degraded_sections"):
        s2b = rep.section("2b. 字段适配与冲突处理")
        rep.text(s2b, "现场列名不可能都照着标准参数长。本节记录本次**自动做了什么**、"
                      "**哪些列被排除在工艺判定之外**——被排除的列数据仍然保留，"
                      "但结论里不含它们的证据，避免用错位的字段下结论。")
        if ad.get("issues"):
            names = {"dup_column_name": "表内重名列", "dup_param_hit": "多列命中同一参数",
                     "unit_conflict": "单位与库内不一致", "unmatched_column": "未匹配到标准参数",
                     "not_measurable": "无可用数值", "group_dimension": "文本分组维度"}
            sev = {"high": "高", "mid": "中", "low": "低"}
            rep.table(s2b, ["类型", "涉及列/参数", "处置", "影响"],
                      [[names.get(i.get("type"), i.get("type")),
                        i.get("column") or "、".join(i.get("columns") or []) or i.get("param") or "—",
                        (i.get("detail") or ""),
                        sev.get(i.get("severity"), "—")] for i in ad["issues"]],
                      caption="冲突清单")
        if ad.get("applied"):
            rep.callout(s2b, "**已自动适配**（只作用于本次分析，不改动共享知识库基线）：", "info")
            rep.bullets(s2b, [a.get("action") for a in ad["applied"]])
        if ad.get("queued"):
            ok = [q for q in ad["queued"] if q.get("ok")]
            if ok:
                rep.text(s2b, "已为 %d 个待确认字段生成知识候选（需 `kb_manage.py review` 后生效；"
                              "若开启了自动适配，也只会写入本企业的私有层）：%s"
                         % (len(ok), "、".join(q.get("id", "") for q in ok)))
        for n in (ad.get("notes") or []):
            rep.callout(s2b, n, "warn")
        if res.get("degraded_sections"):
            # 措辞三处不能含糊：
            #   1. 不预设原因。旧文案写死"因数据异常未产出"，可真实原因可能是
            #      模块缺陷、配置缺失、文件被占用 —— 一律推给数据会误导排查方向。
            #   2. 不许读成"没问题"。空章节是"没算出来"，不是"检查过没问题"。
            #   3. 不许写"其余部分不受影响"。下游常常依赖该节的输出。
            rep.callout(s2b,
                        "以下 %d 个分析环节**未产出结论**（不是「检查过、没有问题」）：%s。"
                        "这些环节失败后，依赖它们的下游结论可能偏保守或部分缺失；"
                        "请把上述错误原文连同数据一起反馈。"
                        % (len(res["degraded_sections"]),
                           "；".join("%s：%s" % (d.get("section"), d.get("error"))
                                    for d in res["degraded_sections"])), "warn")
    if res.get("tenant"):
        rep.callout(s2b if (ad or res.get("degraded_sections")) else s2,
                    "本次分析在企业「%s」的私有知识层下进行，写入不影响其它企业。"
                    % res["tenant"], "info")

    # 2c 行业作用域（本次检索了哪个行业，以及为什么这么定）
    if SC.active():
        s2c = rep.section("2c. 行业作用域")
        rep.text(s2c, "企业端配置了行业模块后，参数匹配、设备档案、改善案例都只在本行业内检索。"
                      "跨行业同名不同义的参数（例如「开路电压」在锂电指电芯 OCV、"
                      "在光伏指组件 Voc，量级相差十倍）由此消除歧义。")
        _stat = res.get("_scope_stat") or {}
        rep.kv(s2c, [("生效行业", res.get("industry_name") or "、".join(res["industry"])),
                     ("纳入工序数", _stat.get("kept_processes", "—")),
                     ("纳入参数数", _stat.get("kept_params", "—")),
                     ("排除参数数", _stat.get("dropped_params", "—"))])
        rep.callout(s2c,
                    "通用判读规则（如「相关不等于因果」「Cpk 先拆 σ 与均值偏移」）"
                    "为跨行业方法论，任何行业配置下都保留——裁掉它们是负收益。", "info")
        for n in SC.notes():
            rep.callout(s2c, n, "warn")

    # 2d 设备身份（这台设备是被认出来的，还是现场猜的——直接决定结论稳不稳定）
    s2d = rep.section("2d. 设备身份与解析口径")
    _dev = res.get("device")
    if _dev:
        rep.kv(s2d, [("识别设备", res.get("device_name") or _dev),
                     ("识别依据", "列指纹" if res.get("device_matched_by") == "fingerprint" else "设备档案特征"),
                     ("规格口径", "已锁定（取自设备档案默认规格）" if res.get("device_spec_locked")
                      else "未锁定（设备档案中无默认规格）")])
        rep.text(s2d, "命中设备档案后，版式（表头行数、单位行、合计尾行）、列名映射与默认上下限"
                      "都按档案执行，不再逐份重新推断。这是同一台设备多次分析结果保持一致的前提。")
    else:
        rep.kv(s2d, [("识别设备", "未识别（按通用版式处理）"),
                     ("规格口径", "未锁定")])
        rep.callout(s2d,
                    "本次未匹配到设备档案，版式与列名均为现场推断。**推断结果会随数据内容浮动**："
                    "样本量变化、导出时是否带合计行、某列是否恰好全空，都可能改变进入分析的列，"
                    "进而让 Cpk 与根因排序在多次分析间跳动。", "warn")
        rep.callout(s2d,
                    "录入这台设备后即可消除上述波动：执行 "
                    "`python scripts/kb_manage.py devices --interview <该文件>` 生成录入问卷，"
                    "按 `devices --register` 存为该企业的私有设备档案，"
                    "下次上传同设备导出会自动认出并套用同一套口径。", "info")

    # 3 工序与参数识别
    s3 = rep.section("3. 工序判定与参数识别")
    if res.get("process_detected"):
        rep.table(s3, ["候选工序", "匹配得分"],
                  [[KB["processes"][k]["name"] if k in KB["processes"] else k, round(v, 2)]
                   for k, v in res["process_detected"]], caption="工序自动识别")
    if res.get("col_map"):
        _dev0 = (res.get("devices") or [{}])[0]
        _src = _dev0.get("col_source") or {}
        rows3 = []
        for c, k in res["col_map"].items():
            if not k:
                continue
            spec0 = KB["params"].get(k, {})
            rows3.append([
                c, spec0.get("name", k), spec0.get("unit", ""),
                {"control": "可调控因子", "response": "结果指标",
                 "group": "分组维度"}.get(spec0.get("role"), "结果指标"),
                {"device": "设备档案", "generic": "通用别名库"}.get(_src.get(c), "—")])
        rep.table(s3, ["原始列名", "标准参数", "单位", "角色", "识别来源"], rows3,
                  caption="列名标准化映射（角色区分决定该参数能否作为 DOE 因子；"
                          "设备档案的私有列名优先于通用别名库）")
        _unmapped = [c for c in work.columns if not res["col_map"].get(c)]
        # 时间/分组列不参与参数识别，不算漏识别
        _unmapped = [c for c in _unmapped
                     if PP.match_param(c) is None and _src.get(c) != "device"]
        if _unmapped:
            rep.callout(s3, "以下 %d 个列名未能识别，不参与本次分析。若它其实是某个工艺参数，"
                            "可用 <code>kb_manage.py propose --type param_alias</code> "
                            "补进别名库，或直接登记为新的设备档案。未识别列：%s"
                            % (len(_unmapped), "、".join(_unmapped[:12])), "warn")
        rep.callout(s3, "未出现在表中的标准参数，说明本次导出数据未覆盖该工序的关键控制点，"
                        "这是数据缺口而非分析遗漏。", "info")

    # 4 SPC
    s4 = rep.section("4. SPC 控制图与判异")
    if not spc_res:
        rep.text(s4, "有效数值列不足（每列需 ≥8 个有效点），未执行 SPC 分析。")
    for d in spc_res:
        title = "%s %s" % (d["param_name"] or d["column"], ("(%s)" % d["unit"]) if d["unit"] else "")
        add_chart(s4, CH.control_chart(d["values"], d["center"], d["lcl"], d["ucl"],
                                       title=title + " X-MR 控制图",
                                       ylabel="测量值 %s" % d["unit"],
                                       violations=d["violations"],
                                       spec_lines=[d["lsl"], d["usl"]]),
                  "%s：X-MR 控制图（红点为判异点，橙虚线为规格限）" % title)
        if d["violations"]:
            rep.table(s4, ["规则", "描述", "可能含义", "触发点数"],
                      [[v["rule"], v["desc"], v["hint"], v["count"]] for v in d["violations"]],
                      caption="%s 触发的 Nelson 判异规则" % title)
        else:
            rep.text(s4, "%s：未触发判异规则，过程处于统计受控状态。" % title)
        # 自相关 → EWMA/CUSUM：这段紧跟在该参数的 X-MR 之后，
        # 因为它的作用是修正读者刚看到的那些判异结论
        _acd = d.get("autocorrelation")
        if isinstance(_acd, dict) and _acd.get("significant"):
            rep.callout(s4, "【自相关】%s" % (_acd.get("reason") or ""), "bad")
            rows_ew = []
            if d.get("ewma"):
                rows_ew.append(["EWMA(λ=%s, L=%s)" % (d["ewma"].get("lambda"),
                                                       d["ewma"].get("L")),
                                d["ewma"].get("n_alarms"),
                                d["ewma"].get("first_alarm")])
            if d.get("cusum"):
                rows_ew.append(["CUSUM(k=%s, h=%s)" % (d["cusum"].get("k"),
                                                       d["cusum"].get("h")),
                                d["cusum"].get("n_alarms"),
                                d["cusum"].get("first_alarm")])
            if rows_ew:
                rep.table(s4, ["方法", "报警点数", "首个报警点"], rows_ew,
                          caption="%s：自相关序列的补充判异（首点越小发现越早）" % title)
                rep.callout(s4, "上表的报警点才是本序列的可靠信号：X-MR 里那些 Nelson "
                                "报警可能只是相邻点相互牵连造成的序列惯性。"
                                "另外，这张图上小于 %.4g %s 的阶跃在统计上未必看得出——"
                                "要更高灵敏度需把采样间隔拉到 ≥ %s 个（或取子组均值）。"
                                % (_acd.get("min_detectable_shift") or 0, d.get("unit") or "",
                                   _acd.get("suggested_sampling_step") or "?"), "info")

    # 5 过程能力
    s5 = rep.section("5. 过程能力指数")
    rows = []
    for d in spc_res:
        c = d.get("capability") or {}
        rows.append([d["param_name"] or d["column"], d["unit"],
                     _fmt(d["mean"]), _fmt(d["sigma_overall"]),
                     _fmt(d["lsl"]), _fmt(d["usl"]),
                     _fmt(c.get("Cp")), _fmt(c.get("Cpk")),
                     _fmt(c.get("Ppk")), _fmt(c.get("ppm_total"), 1),
                     _fmt(c.get("sigma_level"), 2),
                     c.get("verdict", "无规格")])
    rep.table(s5, ["参数", "单位", "均值", "σ(整体)", "LSL", "USL", "Cp", "Cpk",
                   "Ppk", "预期ppm", "σ水平", "判定"], rows)
    for d in spc_res[:6]:
        c = d.get("capability") or {}
        vals = d["values"]
        add_chart(s5, CH.histogram(vals, lsl=d["lsl"], usl=d["usl"], target=d["target_value"],
                                   title="%s 分布" % (d["param_name"] or d["column"]),
                                   xlabel=d["unit"]),
                  "%s：分布直方图与规格线" % (d["param_name"] or d["column"]))
    rep.callout(s5, "规格来源：优先使用企业受控规格；未提供时退回知识库行业典型值，"
                    "此时 Cpk 仅表示'相对典型窗口'的能力，不能直接用于客户报告。", "warn")
    # v1.11：能力指数的两个硬伤必须主动说出来，而不是原样输出数字。
    # 1) Cp 与 Ppk 差很多 = 组间/设备间有系统性差异，而"能力充足"会把这个差异盖住；
    # 2) Cpk 高得离谱（>3）几乎不可能是真的，通常是规格取错或列匹配错了。
    _warned = []
    for d in spc_res:
        c = d.get("capability") or {}
        cp, ppk, cpk = c.get("Cp"), c.get("Ppk"), c.get("Cpk")
        nm = d.get("param_name") or d.get("column")
        if cp and ppk and ppk > 0 and cp / ppk >= 1.5:
            _warned.append("「%s」Cp=%.2f 但 Ppk=%.2f（相差 %.1f 倍）：组内波动小、"
                           "整体波动大，说明设备间/批次间存在系统性差异——这正是根因所在，"
                           "不能只按 Cp 判「能力充足」。" % (nm, cp, ppk, cp / ppk))
        if cpk is not None and cpk > 3:
            _warned.append("「%s」Cpk=%.2f 异常偏高：不要直接采信。先核对三件事——"
                           "规格上下限是否取错、该列是否被匹配到了别的参数、"
                           "σ 是否因伪重复（同一产品多行采样）被低估。" % (nm, cpk))
    for w in _warned[:4]:
        rep.callout(s5, w, "warn")

    # 6 相关性
    s6 = rep.section("6. 参数相关性分析")
    # corr 可能为 None：上游该节执行失败时被护栏降级了。
    # 消费方不能假设生产者一定成功 —— 假设会换成 NoneType 不可下标的
    # TypeError 崩掉整份报告，把那一节记下来的真实原因彻底盖住。
    if corr and corr.get("columns") and len(corr["columns"]) >= 2:
        add_chart(s6, CH.heatmap(corr["matrix"], corr["columns"]), "数值参数相关性矩阵")
    elif res.get("degraded_sections"):
        rep.callout(s6, "相关性矩阵本节未产出（上游环节执行失败），"
                        "详见报告末尾的『未产出结论的环节』。", "warn")
    if assoc:
        rep.table(s6, ["参数", "类型", "效应量", "p 值", "BH 校正后 q", "样本数", "显著性"],
                  [[a["param"], "数值" if a["type"] == "numeric" else "分类",
                    _fmt(a.get("r") if a["type"] == "numeric" else a.get("abs_effect")),
                    _fmt(a.get("p_raw_uncorrected")),
                    _fmt(a.get("q")),
                    a.get("n"), "显著" if a.get("significant") else "不显著"]
                   for a in assoc[:12]],
                  caption="与目标列「%s」的关联强度排序。共检验 %d 项，p 值已做 "
                          "Benjamini-Hochberg 多重比较校正（不校正时 0.05 水准下预期约 "
                          "%.1f 个假阳性），显著性以 q ≤ 0.05 为准。"
                          % (res.get("target") or "—", len(assoc), len(assoc) * 0.05))
        for a in assoc[:3]:
            if a["type"] == "numeric":
                add_chart(s6, CH.scatter(work.numeric_column(a["param"]),
                                         work.numeric_column(res["target"]) if res.get("target") else [],
                                         xlabel=a["param"],
                                         ylabel=res.get("target") or "",
                                         title="%s vs %s" % (a["param"], res.get("target"))),
                          "%s 与 %s 散点图" % (a["param"], res.get("target")))
    if strong:
        rep.table(s6, ["参数A", "参数B", "r", "p"],
                  [[p["a"], p["b"], _fmt(p["r"], 3), _fmt(p["p"], 4)] for p in strong[:8]],
                  caption="强相关参数对（|r|≥0.7，注意共线性）")
    if lags:
        rep.table(s6, ["参数", "最优滞后", "r", "p", "解读"],
                  [[l["param"], l["best_lag"], _fmt(l["best_r"], 3), _fmt(l["best_p"], 4),
                    l["interpretation"]] for l in lags],
                  caption="滞后相关（识别上游参数对下游指标的领先关系）")
    if res.get("vif"):
        rep.table(s6, ["参数", "VIF", "与其他参数 R²", "共线性"],
                  [[v["param"], _fmt(v["vif"], 2), _fmt(v["r2_with_others"], 3),
                    "严重" if v["collinear"] else "正常"] for v in res["vif"][:6]],
                  caption="多重共线性检查（VIF>5 不宜同时作为独立因子进入 DOE）")

    # 7 不良 Pareto
    s7 = rep.section("7. 不良分类与 Pareto")
    if pare:
        add_chart(s7, CH.pareto_chart(pare["rows"]), "不良 Pareto（A 类为累计 80% 内的主因）")
        rep.table(s7, ["不良项", "数量", "占比%", "累计%", "ABC 分级"],
                  [[r["item"], _fmt(r["value"], 2), _fmt(r["pct"], 2),
                    _fmt(r["cum_pct"], 2), r["abc"]] for r in pare["rows"]])
        rep.text(s7, "A 类（累计 80%% 以内）是优先整改对象：%s"
                 % "、".join(r["item"] for r in pare.get("vital_few", [])))
    else:
        rep.text(s7, "数据中未识别到缺陷分类列或分组维度，无法生成 Pareto。")

    # 8 根因
    s8 = rep.section("8. 根因定位与 5Why")
    # MSA 前置：这一节的位置是有意为之——
    # 测量能力的结论必须排在根因结论**之前**读。写在报告最后的免责声明
    # 等于告诉读者"上面那些你可以放心相信"，而这句话恰恰从未被验证过。
    _render_msa_gate(rep, s8, res)
    # v1.11：位置聚集结论放在最前面 —— 事件表里它才是答案，相关矩阵算不出位置。
    for _cl in (res.get("spatial_clusters") or [])[:3]:
        rep.callout(s8, "【位置聚集】%s（缺陷类型：%s）" % (_cl.get("conclusion") or "",
                                              _cl.get("defect_type") or "全部"), "warn")
    if res.get("negative_verdict"):
        rep.callout(s8, "【无可行动根因】%s" % res["negative_verdict"]["text"], "warn")
    if root:
        rep.kv(s8, [("缺陷/问题", root["defect"]), ("所属工序", root["process_name"]),
                    ("机理", root.get("mechanism") or "—")])
        if root.get("honest_degraded"):
            # 扩展：状态与结论必须一致。已经判定"数据与措施无关"，
            # 再往下渲染候选表就是把线索包装成结论（外部评审 E07 的原话）。
            _why = (root.get("data_sufficiency") or {}).get("reason") or ""
            # 测量系统不合格导致的降级，上面 MSA 门槛已经把结论、整改措施、
            # 待补数据完整说过一遍；这里再复述一遍只会把重要的话稀释成背景噪声。
            _msa_said = (res.get("msa") or {}).get("action") == "block"
            if not _msa_said:
                rep.callout(s8, "本次<b>未给出根因排序</b>：%s。"
                                "知识库常见候选见下表，均为<b>未经本批数据验证的线索</b>，"
                                "不得据此调整工艺参数。" % _why, "warn")
            _hints = root.get("prior_only_hints") or []
            # 空表比没有表更糟：表头在、内容空，读者会以为"没有线索"是查证结果，
            # 而真相可能是这一条路径压根没生成线索。
            if _hints:
                rep.table(s8, ["知识库常见候选", "单位", "未列入结论的原因"],
                          [[h["param_name"], h.get("unit") or "—",
                            h.get("why_not_conclusion") or ""]
                           for h in _hints[:8]],
                          caption="待核实线索（非本次结论）")
            if not _msa_said:
                for _need in (root.get("what_to_provide") or []):
                    rep.bullets(s8, ["需补充：" + _need])
            if root.get("five_why"):
                rep.table(s8, ["层级", "提问", "推断", "依据", "有数据支撑"],
                          [[w["level"], w["why"], w["because"], w["evidence"],
                            "是" if w["supported"] else "否"] for w in root["five_why"]],
                          caption="5Why 链条")
            rep.bullets(s8, ["现场快速验证：" + c for c in (root.get("quick_checks") or [])])
            _gaps = res.get("gaps") or []
            if _gaps:
                rep.callout(s8, "数据缺口 %d 项已单列报告第 13 节，补齐后重跑即可参与排序"
                                % len(_gaps))
        else:
            if root.get("evidence_insufficient"):
                rep.callout(s8, "本次数据<b>支撑不出根因结论</b>：所有候选参数的关联强度都很弱"
                                "（最高分 %.2f，低于 0.20 的证据门槛）。下表仅供排查方向参考，"
                                "<b>不应据此调整工艺参数</b>。请先补充关键检测数据或核对列名含义。"
                                % (root["candidates"][0]["score"] if root.get("candidates") else 0),
                            "warn")
            if root.get("data_driven"):
                rep.callout(s8, "本次未指定缺陷类型，未启用缺陷机理库。下表为 <b>纯数据驱动</b> 的参数归因，"
                                "只能说明「哪些参数与目标指标同步变化」，不等于已确认根因。"
                                "用 --defect 指定缺陷类型后可获得机理加权结果。", "warn")
                cap = "候选参数排序（按与目标列的关联强度，未经缺陷机理库加权）"
            else:
                cap = ("候选根因排序（0.40×数据证据 + 0.35×知识先验 + 0.15×时序吻合 + 0.10×可验证性）")
            rep.table(s8, ["候选参数", "综合得分", "数据证据", "知识先验", "时序吻合", "数据状态"],
                      [[c["param_name"], _fmt(c["score"], 3), _fmt(c["evidence_score"], 3),
                        _fmt(c["prior_score"], 3), _fmt(c["temporal_score"], 3),
                        c["data_status"]] for c in root["candidates"]], caption=cap)
            if root.get("five_why"):
                rep.table(s8, ["层级", "提问", "推断", "依据", "有数据支撑"],
                          [[w["level"], w["why"], w["because"], w["evidence"],
                            "是" if w["supported"] else "否"] for w in root["five_why"]],
                          caption="5Why 链条")
            rep.bullets(s8, ["现场快速验证：" + c for c in (root.get("quick_checks") or [])])

        # 单元级热点：这类问题（某个通道/卷针/供应商单独跑偏）单因素 ANOVA 抓不到，
        # 单独列出来，否则现场只能看到「组间无显著差异」而漏掉它
        hs = res.get("unit_hotspots") or []
        if hs:
            rep.table(s8, ["分组维度", "异常水平", "该水平均值", "其余均值", "偏离", "校正后 p"],
                      [[h["dim"], h["worst"]["level"], _fmt(h["worst"]["mean"], 3),
                        _fmt(h["worst"]["others_mean"], 3),
                        "%.2fσ" % h["worst"]["effect"],
                        _fmt(h["worst"]["p_adj"], 4)] for h in hs[:5]],
                      caption="单元级热点（逐水平 vs 其余全部，已按水平数做 Bonferroni 校正）")
            rep.callout(s8,
                        "上表是<b>逐水平</b>比较的结果，不是组间整体 ANOVA。"
                        "个别单元跑偏时（如 16 个通道里只有 CH-07 坏），"
                        "组间整体检验的 η² 会被正常单元摊薄而判不显著——"
                        "只看 ANOVA 会漏掉这类最常见的单元级问题。", "info")
        _add_history_cases(rep, s8, root)
        rep.callout(s8, "5Why 第 4、5 层依赖现场追溯（设备状态、变更记录、控制计划），"
                        "脚本只能给出方向，必须由工艺工程师现场确认后才能结案。", "warn")
    else:
        rep.text(s8, "未指定或未识别到缺陷类型，跳过根因定位。可用 --defect 指定。")

    # 9 良率损失拆解
    s9 = rep.section("9. 良率损失拆解")
    if gcomp:
        for gc in gcomp[:3]:
            rep.table(s9, [gc["dimension"], "样本数", "均值", "标准差", "相对总均值偏差%"],
                      [[g["group"], g["n"], _fmt(g["mean"]), _fmt(g["stdev"]),
                        _fmt(g["delta_vs_overall"], 2)] for g in gc["groups"][:12]],
                      caption="按「%s」分层（ANOVA p=%s）"
                              % (gc["dimension"], _fmt((gc.get("anova") or {}).get("p"), 4)))
            add_chart(s9, CH.boxplot([{"group": g["group"],
                                       "values": [v for v, gg in
                                                  zip(work.numeric_column(res["target"]),
                                                      work.column(gc["dimension"]))
                                                  if v is not None and str(gg) == str(g["group"])]}
                                      for g in gc["groups"][:10]],
                                     title="%s 分层分布" % gc["dimension"],
                                     ylabel=res.get("target") or ""),
                      "%s 分层箱线图" % gc["dimension"])
            an = gc.get("anova") or {}
            if an.get("p") is not None and an["p"] < 0.05:
                worst = gc["groups"][0] if gc["groups"] else None
                rep.callout(s9, "「%s」维度存在显著组间差异（F=%.2f, p=%.4g, η²=%.3f）；"
                                "差异最大的单元是「%s」（相对总均值 %+.2f%%）。"
                                "说明问题在该维度上具有聚集性，应优先定位到该单元，"
                                "而不是全线整改。"
                                % (gc["dimension"], an["F"], an["p"], an.get("eta2") or 0,
                                   worst["group"] if worst else "—",
                                   worst["delta_vs_overall"] if worst and worst.get("delta_vs_overall") is not None else 0),
                            "bad")
    else:
        rep.text(s9, "未识别到分组维度，无法做良率损失的分层拆解。补充设备号/批次号后可定位到具体单元。")

    # 10 DOE
    s10 = rep.section("10. 参数优化与 DOE 方案")
    if doe_plan and doe_plan.get("factors"):
        rep.bullets(s10, ["推荐方案：**%s**，试验次数 %d 次。%s"
                          % (doe_plan["chosen"]["name"], doe_plan["chosen"]["runs"],
                             doe_plan["chosen"]["note"])])
        rep.table(s10, ["因子", "低水平", "中心", "高水平", "单位", "取值依据"],
                  [[f["name"], f["low"], f["mid"], f["high"], f["unit"], f["basis"]]
                   for f in doe_plan["factors"]], caption="因子与水平设置")
        if doe_plan.get("run_plan"):
            cols = [f["name"] for f in doe_plan["factors"]]
            hdr = ["试验号"] + cols + ["（响应）"]
            rows = [[i + 1] + [r.get(c) for c in cols] + [""] for i, r in enumerate(doe_plan["run_plan"])]
            rep.table(s10, hdr, rows, caption="试验运行表（按顺序执行，最后一列填响应值）")
        rep.bullets(s10, [
            "执行要点：每号试验至少 3 次重复，随机化运行顺序以抵消时间漂移。",
            "响应选择：优先用连续量（容量、内阻、功率、TTV），避免只用 OK/NG——"
            "二值响应会把信息量砍掉一大半。",
            "分析：用主效应图定方向，用 ANOVA 判定显著性，再用响应面做二次寻优。",
        ], ordered=True)
        if doe_plan.get("excluded"):
            rep.table(s10, ["未入选参数", "原因"],
                      [[e[0], e[1]] for e in doe_plan["excluded"]],
                      caption="被排除的参数（不是不重要，而是不能作为独立因子）")
        if doe_plan.get("options"):
            rep.table(s10, ["备选方案", "试验次数", "说明"],
                      [[o["name"], o["runs"], o["note"]] for o in doe_plan["options"]],
                      caption="其它可选设计")
        if doe_plan.get("safety_note"):
            rep.callout(s10, doe_plan["safety_note"], "warn")
        if doe_plan.get("clamped"):
            rep.table(s10, ["参数", "原水平区间", "收紧后", "原因"],
                      [[c.get("factor"), str(c.get("from")), str(c.get("to")), c.get("reason")]
                       for c in doe_plan["clamped"]],
                      caption="因子水平收紧记录（超出工艺窗口/安全包络的部分被压回）")
    elif doe_plan and doe_plan.get("skipped_reason"):
        rep.callout(s10, doe_plan["skipped_reason"], "warn")
        if doe_plan.get("excluded"):
            rep.table(s10, ["未入选参数", "原因"],
                      [[e[0], e[1]] for e in doe_plan["excluded"]],
                      caption="被排除的参数（不是不重要，而是不能作为独立因子）")
    else:
        rep.text(s10, "可用作 DOE 因子的参数不足 2 个，未生成方案。常见原因："
                      "① 数据里的强关联参数都是**结果指标**（测量值），不可直接设定；"
                      "② 真正的可控工艺参数（固含、温度、张力、速度等）未包含在本次数据中。")
        rep.callout(s10, "解决方向：补充可控参数的实测数据。DOE 的因子必须是能在设备/工艺卡上"
                         "直接设定的量（温度、速度、张力、配比、时间），而不是测出来的结果。", "warn")

    # 11 数据缺口
    s11 = rep.section("11. 数据缺口与下一步")
    if gaps:
        rep.table(s11, ["优先级", "缺失项", "影响", "严重度"],
                  [[g["level"], g["item"], g["why"], g["impact"]] for g in gaps],
                  caption="数据缺口清单（P0=必需，P1=重要，P2=增强）")
        rep.callout(s11, "若 P0/P1 项无法提供，本报告结论按当前置信度「%s」使用，"
                         "且不得直接作为客户 8D 的 D4 根因结论。" % conf["level"], "warn")
    else:
        rep.text(s11, "未发现关键数据缺口。")
    rep.bullets(s11, [
        "下一步 1：按第 8 节 quick_checks 做现场验证，先确认/否证首要候选根因。",
        "下一步 2：补齐 P0 数据后复跑本分析，对比两次结论是否一致。",
        "下一步 3：若需永久改善，按第 10 节 DOE 执行并回填响应值做效应分析。",
    ], ordered=True)

    # 12 8D（v1.13：完整 D1–D8。D2 由录入卡交叉校验，D5–D8 只出方案与清单，且需人工确审）
    s12 = rep.section("12. 8D 报告（D1–D8）")
    _d2 = {}
    _card_path = None
    if _HAS_8D and EV8D is not None:
        try:
            # 已经给了填好的卡就不要再生成空白卡覆盖它（否则用户第二遍跑会把自己的填写弄丢）
            if res.get("emit_d2_card", True) and not res.get("d2_card_path"):
                _card_path = EV8D.emit_intake_card(res, res.get("outdir") or "output")
            if res.get("d2_card_path"):
                _d2 = EV8D.parse_intake_card(res["d2_card_path"]) or {}
                _card_path = res["d2_card_path"]
            _conflicts = EV8D.cross_check(_d2, res)
            res["eightd"] = EV8D.render_8d(rep, res, card=_d2,
                                           conflicts=_conflicts, parent=s12)
            res["eightd"]["intake_card"] = _card_path
            res["eightd"]["d2_filled"] = bool(_d2)
            if not res["eightd"]["signoff"]["all_signed"]:
                res.setdefault("warnings", []).append(
                    "8D 的 D5–D8 未全部经人工确审（%d/%d），不得对外发出。"
                    % (sum(1 for k in ("signoff_d5", "signoff_d6", "signoff_d7", "signoff_d8")
                           if res["eightd"]["signoff"].get(k)), 4))
        except Exception as _e8d:
            res.setdefault("degraded_sections", []).append(
                {"section": "8D", "error": str(_e8d)})
            rep.callout(s12, "8D 引擎降级：%s" % _e8d, "warn")
    else:
        # 不允许静默降级：模块没装载就必须说出来（v1.11 的踩坑教训）
        rep.callout(s12, "8D 引擎未装载（lib/eightd.py 导入失败），本节无法生成完整 8D。"
                         "请检查模块完整性。", "warn")
        res.setdefault("degraded_sections", []).append(
            {"section": "8D", "error": "eightd 模块未导入"})

    # 13 知识沉淀
    s13 = rep.section("13. 知识沉淀候选（本次分析的收获）")
    cands = res.get("kb_candidates") or []
    if cands:
        def _label(c):
            pl = c.get("payload", {})
            if c["_type"] == "defect_map":
                return "缺陷「%s」的根因映射" % pl.get("defect", "—")
            if c["_type"] == "param_alias":
                return "%s 的列名别名 %s" % (pl.get("key"), "、".join(pl.get("aliases", [])))
            return pl.get("name") or pl.get("key") or "—"

        rep.table(s13, ["类型", "对象", "发现依据", "建议动作"],
                  [["新参数" if c["_type"] == "param" else
                    ("列名别名" if c["_type"] == "param_alias" else "根因映射"),
                    _label(c), c.get("_reason", ""),
                    "作为别名入库（不新增参数）" if c["_type"] == "param_alias"
                    else "确认单位与归属工序后入库"] for c in cands],
                  caption="分析过程中发现、但知识库尚未收录的对象")
        rep.callout(s13, "以上仅为候选，**不会自动写入知识库**。确认无误后执行 "
                         "`kb_manage.py harvest --file <输出目录>/kb_candidates.json`，"
                         "再经 `review --approve` 与 `sync` 才会生效。"
                         "候选中的单位与工序归属需人工补正，脚本猜的不算。", "info")
    else:
        rep.text(s13, "本次未发现知识库尚未收录的参数：所有强相关列都已在知识库中有对应标准参数。")
    rep.text(s13, "现场若已通过 PDCA / PFMEA 验证出有效改善措施，可用表单沉淀回 "
                  "`knowledge/improvement_library.json`：先 `kb_manage.py scaffold "
                  "--type improvement_case` 取骨架（或填 assets/改善措施-PDCA-PFMEA表单模板.md），"
                  "填好后 propose → review → sync。")

    # 14/15/16 跨工序追溯、横向排查与预防（只在同时给了多道工序数据时出现）
    _render_cross(rep, res)
    # 17 使用边界与责任声明：不论数据形态如何都必须出现。
    # 没有这一节，报告被转发给客户或审核方时，就只剩结论、没有边界。
    _render_disclaimer(rep, res)
    return rep


def _decision_text(d):
    """决策记录 -> 可展示文本。

    决策记录以前有两种形态：各处直接 append 字符串，后来按 kind/detail 结构化。
    混用时期，消费方只要拿到一条结构化记录就崩（unhashable type: 'dict'），
    而崩的那一节正是"别人拿到同一份文件也复现不出结论"要用的地方——
    宁可少一条决策，也不能整节渲染失败。
    生产端已统一为结构化 dict，这里保留兼容是为了吃进来历史 JSON 结果。
    """
    if isinstance(d, dict):
        _k, _v = d.get("kind"), (d.get("detail") or d.get("reason") or "")
        if not _k and not _v:
            return ""       # 空记录不产出空条目：报告里一行光秃秃的"决策："没有意义
        return "%s：%s" % (_k or "决策", _v)
    return str(d) if d else ""


def _render_msa_gate(rep, sec, res):
    """根因章节的测量系统前置门槛。三档结论都在这里显式摆出来。"""
    _m = res.get("msa")
    _err = res.get("msa_error")
    if _err:
        rep.callout(sec, "【测量系统】%s" % _err, "warn")
        return
    if isinstance(_m, dict) and _m.get("action") in ("block", "warn", "pass"):
        _lv = {"pass": "可接受", "warn": "有条件接受", "block": "不可接受"}
        _kind = {"pass": "ok", "warn": "warn", "block": "bad"}.get(_m.get("action"), "warn")
        _line = ["【测量系统 %s】%s" % (_lv.get(_m.get("action"), _m.get("action")),
                                  _m.get("reason") or "")]
        if _m.get("pct_grr") is not None:
            _line.append("（%%GRR=%.1f%%%s，来源：%s）" % (
                _m["pct_grr"],
                ("，ndc=%.1f" % _m["ndc"]) if _m.get("ndc") is not None else "",
                _m.get("source") or _m.get("method") or "—"))
        rep.callout(sec, "".join(_line), _kind)
        if _m.get("action") == "block":
            rep.callout(sec, "本次<b>未产出根因排序</b>：观测到的变差里测量误差占主导，"
                             "在上面排任何榜都是给噪声编故事。"
                             "报告出具前须先整改测量系统并复测。", "bad")
            _wtp = ((res.get("rootcause") or {}).get("what_to_provide") or [])
            if _wtp:
                rep.bullets(sec, ["整改与验证要求："] + list(_wtp))
        elif _m.get("auto_detected"):
            rep.callout(sec, "该结论由生产数据中的重复测量结构自动估算，"
                             "证据强度弱于专门的 Gauge R&R 计划表：拉警报够用，"
                             "作为正式测量能力报告不够。", "info")
        return
    _un = res.get("msa_unverified")
    if _un:
        rep.callout(sec, "【测量系统未验证】%s" % _un.get("note"), "warn")
        _tv = _un.get("to_verify") or []
        if _tv:
            rep.bullets(sec, ["把它变成已验证，需要："] + list(_tv))


def _render_disclaimer(rep, res):
    """结论的边界、规则版本与责任声明。"""
    s17 = rep.section("17. 使用边界与责任声明")
    _rules_used = res.get("spc_rules") or list(SPC.DEFAULT_RULES)
    rep.kv(s17, [
        ("分析器版本", ANALYZER_VERSION),
        ("知识库版本", _kb_stamp()),
        ("判异规则集", "Nelson 第 %s 条（%s）"
         % ("、".join(str(r) for r in _rules_used),
            "默认基本集" if tuple(_rules_used) == tuple(SPC.DEFAULT_RULES) else "自定义")),
        ("分析时间", res.get("generated_at") or "—"),
        ("行级追溯", "%s（%s）" % ((res.get("trace_meta") or {}).get("file") or "—",
                                 (res.get("trace_meta") or {}).get("row_tracing") or "—")),
    ])
    # v1.11：审计要能复现。版本号只是"哪份代码"，还必须有"哪个文件"和"自动做了哪些决定"
    _man = res.get("run_manifest") or {}
    if _man.get("inputs"):
        rep.table(s17, ["输入文件", "字节数", "SHA-256（前 16 位）"],
                  # 字节数统一取 "bytes"：输入摘要由 provenance.input_fingerprints
                  # 一处产出，报告这里跟着读它。此前这里写的是 "size"，
                  # 与生产端不一致——改了生产端的键名，报告就 KeyError 崩在这一行。
                  [[i["file"], i.get("bytes", i.get("size")), (i["sha256"] or "—")[:16]]
                   for i in _man["inputs"]],
                  caption="输入文件指纹。同名文件被替换后指纹会变，可用于核对结论出自哪一份数据")
    if _man.get("decisions"):
        _seen, _uniq = set(), []
        for _d in _man["decisions"]:
            _t = _decision_text(_d)
            if _t and _t not in _seen:
                _seen.add(_t)
                _uniq.append(_t)
        rep.text(s17, "**本次分析自动做出的决策**（没有这些记录，别人拿到同一份文件也复现不出结论）：")
        rep.bullets(s17, _uniq)
    rep.text(s17, "**本报告结论按可信度分三类，请勿混用：**")
    rep.table(s17, ["类别", "含义", "可怎么用"],
              [[RPT.badge("已验证结论", "grn"),
                "有统计量支撑、显著性达标，规格来自企业受控文件或设备档案",
                "可用于内部决策与 8D 的事实部分"],
               [RPT.badge("待验证假设", "org"),
                "相关不等于因果；或证据等级 B/C；或规格为知识库推定值",
                "先小批验证或走 DOE，确认能复现改善后再固化"],
               [RPT.badge("数据限制", "red"),
                "识别率不足、样本量不足、缺企业规格，或过程未达统计受控",
                "不得作为结论引用；先补齐数据再复跑"]],
              caption="报告第 4–9 节的结论按上述三类标注；标注为待验证的，"
                      "固化前必须验证")
    rep.callout(s17,
                "**责任边界**：本报告由 AI 辅助分析生成，用于**辅助决策**，"
                "不构成工艺变更指令或质量判定结论。所有工艺参数调整、质量判定与产品放行，"
                "须由企业授权人员依据受控文件确认后执行。涉及化成、高压、高温等安全相关"
                "参数的任何建议，必须通过安全评审与小批验证后方可实施。"
                "分析结论的可靠性受输入数据质量与测量系统能力（Gauge R&R）限制；"
                "测量系统未经验证时，测量误差可能被判定为工艺问题。", "warn")
    return rep


def _render_cross(rep, res):
    xr = res.get("crossprocess") or {}
    lat = res.get("lateral") or {}
    perm = res.get("permanent") or {}
    prev = res.get("preventive") or {}
    if not xr and not perm:
        return

    # 14 跨工序归属判定
    s14 = rep.section("14. 跨工序追溯：问题出在哪一道")
    if not xr.get("applicable"):
        rep.callout(s14, "未做跨工序追溯：%s"
                    % ("；".join(xr.get("notes") or []) or "只收到单道工序的数据"),
                    "warn")
        for g in (xr.get("gaps") or []):
            rep.text(s14, "- **%s 缺口**：%s（%s）" % (g.get("level"), g.get("item"), g.get("why")))
        if xr.get("upstream_processes"):
            rep.text(s14, "本工序的上游链：%s。"
                     % " ← ".join(xr["upstream_processes"]))
        return

    al = xr.get("alignment") or {}
    rep.kv(s14, [("对齐方式", "%s（%s）" % (al.get("mode"), al.get("reason") or "")),
                 ("对齐后样本", "%s 行" % al.get("rows")),
                 ("目标列", res.get("target") or "—")])
    rep.callout(s14, "**归属判定：%s**（置信度 %s）"
                % (xr.get("verdict"), xr.get("confidence")),
                "bad" if "上游" in str(xr.get("verdict")) else "info")
    for why in (xr.get("verdict_reason") or []):
        rep.text(s14, "- " + why)

    rows = []
    for c in (xr.get("contributions") or [])[:8]:
        cr = c.get("corr") or {}
        pr = c.get("probe") or {}
        rows.append(["本工序" if c["role"] == "current" else "上游", c["column"],
                     _fmt(cr.get("r"), 3), _fmt(cr.get("p"), 4),
                     {"upstream_violating": "坏件侧已超规格",
                      "upstream_marginal": "合格但边缘",
                      "no_spec_cannot_clear": "缺规格无法定性",
                      "not_discriminating": "无区分力"}.get(pr.get("verdict"), "—")])
    if rows:
        rep.table(s14, ["归属", "参数", "r", "p", "裕度探针"], rows,
                  caption="各参数与目标的关联，以及它在好坏两组间是否越界")
    for p in (xr.get("upstream_probes") or []):
        if p.get("verdict") in ("upstream_violating", "upstream_marginal",
                                "no_spec_cannot_clear"):
            rep.text(s14, "- **%s**：%s" % (p["param"], p.get("reason")))
    if (xr.get("carry_over") or {}).get("clearing_evidence"):
        rep.text(s14, "排除上游带入嫌疑还需看：%s"
                 % "；".join(xr["carry_over"]["clearing_evidence"]))

    # 15 横向排查
    s15 = rep.section("15. 横向排查（别处会不会也有）")
    checklist = lat.get("checklist") or []
    for n in (lat.get("notes") or []):
        rep.text(s15, "- " + n)
    if checklist:
        rep.table(s15, ["范围", "排查动作", "依据"],
                  [[c.get("scope"), c.get("action"), c.get("basis")] for c in checklist],
                  caption="按维度逐档核查，先查聚集性最强的那一档")
    else:
        rep.text(s15, "现有数据的分组维度上没有发现聚集性，问题看起来是全局性的；"
                      "若现场怀疑某台设备，需要提供带机台号/班次的分层数据才能验证。")

    # 16 永久措施与预防
    s16 = rep.section("16. 永久措施候选与预防建议")
    lib = perm.get("from_library") or []
    if lib:
        rep.table(s16, ["措施", "类型", "实测证据", "级别", "标准化"],
                  [[l.get("action"), l.get("type"), l.get("evidence"),
                    l.get("evidence_level"),
                    (l.get("standardization") or {}).get("doc_no")
                    or (l.get("standardization") or {}).get("control_plan_updated") or "—"]
                   for l in lib],
                  caption="改善措施库中已现场验证过的同类对策（可直接复用的部分）")
    else:
        rep.text(s16, "改善措施库中暂无该类缺陷的历史验证对策，下面只有方向性候选。")
    items = [g.get("action") for g in (perm.get("generic") or [])]
    if items:
        rep.bullets(s16, items)
    rep.callout(s16, "以上均为**待验证候选**，不是结论。任一措施在固化前都应先做小批验证或 "
                     "DOE，确认能复现改善再写入工艺卡与控制计划。5Why 的第 4/5 层与 8D 的 "
                     "D5-D8 不得由脚本代填。", "warn")

    pms = prev.get("params") or []
    if pms:
        rep.table(s16, ["参数", "归属", "r", "建议"],
                  [[p["column"], "本工序" if p["role"] == "current" else "上游",
                    _fmt(p.get("r"), 3), p.get("suggestion")] for p in pms],
                  caption="建议纳入内控与监控的参数")
    for m in (prev.get("monitoring") or []):
        rep.text(s16, "- 上游来料门限：**%s** —— %s" % (m.get("column"), m.get("reason")))
    for t in (prev.get("templates") or []):
        rep.text(s16, "- " + t)
    for n in (prev.get("notes") or []):
        rep.callout(s16, n, "warn")



# ------------------------------------------------------------------ Excel 结果表

def export_result_xlsx(result, work, outdir):
    """导出可在 Excel 直接打开的结果表。

    报告给结论，结果表给明细。现场工程师真正要的是能排序、能筛选、能贴进自己那份
    Excel 的行——尤其是判异点，必须能顺着行号回到原始文件核对，否则审计过不去。
    """
    sheets = []

    # 判异明细（带源文件与原始行号）
    rows = []
    for d in (result.get("spc") or []):
        col = d.get("column")
        vals = work.numeric_column(col) if work.col_index(col) is not None else []
        for v in (d.get("violations") or []):
            for i in (v.get("indexes") or []):
                f, ln = work.src_of(i)
                rows.append([col, "规则%d" % v.get("rule"), v.get("desc"), v.get("hint"),
                             i + 1, f or "", ln or "",
                             _fmt(vals[i], 4) if i < len(vals) and vals[i] is not None else ""])
    if rows:
        sheets.append(T.Table(["参数列", "规则", "判异描述", "可能原因", "点序号",
                               "源文件", "原始行号", "测量值"], rows, "判异明细"))

    # 过程能力
    rows = []
    for d in (result.get("spc") or []):
        cap = d.get("capability") or {}
        if not cap:
            continue
        rows.append([d.get("column"), _fmt(d.get("mean"), 4), _fmt(cap.get("Cpk"), 3),
                     _fmt(cap.get("Ppk"), 3), cap.get("verdict") or "—",
                     d.get("spec_source") or "—",
                     "；".join(cap.get("caveats") or []) or "—",
                     cap.get("baseline_note") or "—"])
    if rows:
        sheets.append(T.Table(["参数列", "均值", "Cpk", "Ppk", "判定", "规格来源",
                               "前提条件说明", "基线说明"], rows, "过程能力"))

    # 不良 Pareto
    pare = result.get("pareto") or {}
    rows = [[r.get("item"), r.get("value"), r.get("pct"), r.get("cum_pct"), r.get("abc")]
            for r in (pare.get("rows") or [])]
    if rows:
        sheets.append(T.Table(["不良项", "数量", "占比%", "累计%", "ABC分级"],
                              rows, "不良Pareto"))

    # 根因候选
    root = result.get("rootcause") or {}
    rows = [[c.get("param_name"), c.get("score"), c.get("evidence_score"),
             c.get("prior_score"), c.get("temporal_score"), c.get("data_status")]
            for c in (root.get("candidates") or [])]
    if rows:
        sheets.append(T.Table(["候选参数", "综合得分", "数据证据", "知识先验",
                               "时序吻合", "数据状态"], rows, "根因候选"))

    # 清洗记录：删了什么、为什么删、原始在哪一行
    rows = [[lg.get("file"), lg.get("row"), lg.get("reason"),
             " | ".join(lg.get("values") or [])]
            for lg in (result.get("cleaning_log") or [])]
    if rows:
        sheets.append(T.Table(["源文件", "原始行号", "剔除原因", "该行原值（前6列）"],
                              rows, "清洗记录"))

    # DOE 方案与约束
    dp = result.get("doe") or {}
    fs = dp.get("factors") or []
    plan = dp.get("run_plan") or []
    if fs and plan:
        rows = []
        for i, rec in enumerate(plan, 1):
            rows.append([i] + [rec.get(f["name"]) for f in fs])
        sheets.append(T.Table(["运行号"] + [f["name"] for f in fs], rows, "DOE方案"))
    cons = dp.get("constraints") or {}
    rows = [[n, c.get("min"), c.get("max"),
             ((c.get("safety") or {}).get("note") or "—"),
             "、".join(sorted(set(c.get("sources") or []))) or "—"]
            for n, c in cons.items()]
    if rows:
        sheets.append(T.Table(["参数", "允许下限", "允许上限", "安全约束说明", "约束来源"],
                              rows, "DOE约束"))
    if dp.get("clamped"):
        rows = [[c.get("factor"), str(c.get("from")), str(c.get("to")), c.get("reason")]
                for c in dp["clamped"]]
        sheets.append(T.Table(["参数", "原水平区间", "收紧后", "原因"], rows, "DOE水平收紧"))

    if not sheets:
        return None
    p = os.path.join(outdir, "分析结果表.xlsx")
    T.write_xlsx(sheets, p)
    return p


# ------------------------------------------------------------------ CLI

def main():
    ap = argparse.ArgumentParser(description="新能源工艺数据分析流水线")
    ap.add_argument("--input", "-i", nargs="+", required=True, help="CSV/XLSX 文件路径")
    ap.add_argument("--outdir", "-o", default="output", help="输出目录")
    ap.add_argument("--target", "-t", default=None, help="目标指标列（不良率/容量等）")
    ap.add_argument("--process", "-p", default=None, help="指定工序 key，如 battery_coating")
    ap.add_argument("--defect", "-d", default=None, help="指定缺陷类型，如 针孔气泡")
    ap.add_argument("--sheet", default=None, help="XLSX 工作表名")
    ap.add_argument("--spec", default=None, help="企业规格 JSON 文件 {列名:{lsl,usl,target}}")
    ap.add_argument("--no-doe", action="store_true", help="跳过 DOE 方案")
    ap.add_argument("--json", default=None, help="额外输出 JSON 结果到指定文件")
    ap.add_argument("--tenant", default=os.environ.get("FDE_TENANT"),
                    help="企业标识：启用隔离后所有写库只落该企业私有层（也可用环境变量 FDE_TENANT）")
    ap.add_argument("--auto-adapt", action="store_true",
                    help="字段认不出来时自动登记别名/参数（仅写入本企业私有层，不碰共享基线）")
    ap.add_argument("--industry", nargs="+", default=os.environ.get("FDE_INDUSTRY"),
                    help="行业作用域，如 --industry battery（可多值：battery pv）。"
                         "限定后只在本行业参数/设备/案例内检索，消除跨行业同名参数歧义")
    ap.add_argument("--rules", default=os.environ.get("FDE_SPC_RULES"),
                    help="启用的 Nelson 判异规则：1,2,3,5（默认基本集）/ all / basic / "
                         "shift / trend。规则开太多会误报泛滥，默认只开四条")
    ap.add_argument("--spc-exclusion-mode", dest="spc_exclusion_mode",
                    default=None, choices=["auto", "suggest", "review"],
                    help="SPC 剔除模式：auto=命中即剔除(基线默认)；suggest=只标记可疑点"
                         "不剔除、交人复核；review=按外部复核结论剔除。"
                         "不指定时按产品档与角色自动定（多产品取严）")
    ap.add_argument("--role", dest="role", default=None,
                    choices=sorted((PF.ROLE_LABEL or {}) if PF is not None else {}) or None,
                    help="使用者立场，据此施加判严尺度：process=工艺工程师（要灵敏度）"
                         " quality=质量管理人员（要证据链与可审计）"
                         " metrology=检测分析/设备工程师（要量纲与分辨力）。"
                         "只补使用人没指定的参数，不覆盖他写死的值")
    ap.add_argument("--doe-replicates", dest="doe_replicates", type=int, default=None,
                    help="DOE 每个试验号的重复次数（>=1）。不重复就没有纯误差估计，"
                         "显著性结论站不住")
    ap.add_argument("--doe-blocks", dest="doe_blocks", type=int, default=None,
                    help="DOE 区组数（>=1），用于隔离班次/机台/原料批等已知噪声；"
                         "不指定时按产品档取大")
    ap.add_argument("--doe-seed", dest="doe_seed", type=int, default=None,
                    help="DOE 运行顺序随机化种子（基线 0，保证顺序可复现；"
                         "现场排产时可另指定）")
    ap.add_argument("--boundary-policy", dest="boundary_policy", default=None,
                    choices=["warn", "block"],
                    help="推断边界（缺受控出处）的处置政策：warn=出方案+警告+要求"
                         "工艺工程师书面确认（基线默认）；block=直接不出方案，"
                         "逼出那份受控规格文件。不指定时按受控规格证据自动定："
                         "登记了受控生效文件就收紧为 block，未登记才 warn。"
                         "即本参数只在你要**临时推翻**证据结论时才需要，"
                         "日常不必指定")
    ap.add_argument("--controlled-spec", dest="controlled_spec", default=None,
                    help="受控规格登记（JSON 路径）：企业给了受控规格文件就把"
                         " doc_id/版本/status=active 登进去，边界政策据此自动收紧为 block，"
                         "无需手动指定 --boundary-policy。"
                         "内容与产品档 knowledge/product_profiles.json 的 controlled_specs 段一致，"
                         "适合企业已有受控文件清单时直接指过去")
    ap.add_argument("--msa-data", dest="msa_data", default=None,
                    help="测量系统（Gauge R&R）数据文件路径：含「零件/样品 + 测量者(人或设备)"
                         " + 测量值」三列即可自动认列；认不准用下面三个参数显式指定。"
                         "%%GRR > 30%%%% 或 ndc < 5 时本次**不出根因结论**"
                         "（那是给噪声排榜），10–30%%%% 出结论但每条候选挂分辨力限制注记")
    ap.add_argument("--msa-part", dest="msa_part", default=None,
                    help="MSA 表中的零件/样品列列名（配合 --msa-data）")
    ap.add_argument("--msa-appraiser", dest="msa_appraiser", default=None,
                    help="MSA 表中的测量者/设备列列名（配合 --msa-data）")
    ap.add_argument("--msa-value", dest="msa_value", default=None,
                    help="MSA 表中的测量值列列名（配合 --msa-data）")
    ap.add_argument("--msa-pct-grr", dest="msa_pct_grr", type=float, default=None,
                    help="直接登记企业已有的 %%%%GRR（不必重算原始数据）；"
                         "配合 --msa-ndc 给区分类别数")
    ap.add_argument("--msa-ndc", dest="msa_ndc", type=float, default=None,
                    help="登记已有的区分类别数 ndc（<5 判分辨力不足）")
    ap.add_argument("--msa-tolerance", dest="msa_tolerance", type=float, default=None,
                    help="公差带宽 USL-LSL；给了就额外算 %%%%Tolerance")
    ap.add_argument("--no-docx", action="store_true",
                    help="不导出原生 Word(.docx)。默认导出：报告要进 8D 系统或发客户")
    ap.add_argument("--no-pdf", action="store_true",
                    help="不导出 PDF。默认导出（PDF 不嵌入字体，中文用标准 Adobe-GB1 "
                         "字体；要字体嵌入保真请用 docx 再转）")
    ap.add_argument("--no-xlsx", action="store_true",
                    help="不导出 Excel 结果表")
    ap.add_argument("--d2-card", default=None,
                    help="已填写的 8D D2 录入卡路径（首次运行会随报告生成空白卡，"
                         "填好后用本参数回传，引擎会把 5W2H 与数据做交叉校验并渲染完整 D1–D8）")
    ap.add_argument("--verbose", action="store_true",
                    help="逐阶段打印「这份数据被理解成了什么」（失败时先看这个）")
    ap.add_argument("--no-d2-card", action="store_true",
                    help="不生成空白 D2 录入卡")
    ap.add_argument("--view", nargs="+", default=None,
                    choices=["process", "shopfloor", "quality", "production",
                             "mgmt", "all"],
                    help="按角色出配套视角产物：process=工艺参数建议 "
                         "shopfloor=现场作业单（可打印） quality=交付合规检查 "
                         "production=批次处置建议（带判断记录留痕） "
                         "mgmt=客户单页摘要 all=全套。"
                         "视角层只做筛选与措辞，计算层仍是同一份结果")
    args = ap.parse_args()

    spec = None
    if args.spec:
        with open(args.spec, "r", encoding="utf-8") as f:
            spec = json.load(f)

    res = analyze(args.input, outdir=args.outdir, target=args.target, process=args.process,
                  defect=args.defect, sheet=args.sheet, spec_overrides=spec,
                  do_doe=not args.no_doe, auto_adapt=args.auto_adapt,
                  tenant=args.tenant, industry=args.industry, rules=args.rules,
                  spc_exclusion_mode=args.spc_exclusion_mode,
                  doe_replicates=args.doe_replicates, doe_blocks=args.doe_blocks,
                  doe_seed=args.doe_seed, boundary_policy=args.boundary_policy,
                  role=args.role, controlled_spec=args.controlled_spec,
                  msa_spec={
                      "path": args.msa_data,
                      "part": args.msa_part, "appraiser": args.msa_appraiser,
                      "value": args.msa_value,
                      "pct_grr": args.msa_pct_grr, "ndc": args.msa_ndc,
                      "tolerance": args.msa_tolerance,
                  } if args.msa_data or args.msa_pct_grr is not None else None,
                  export_xlsx=not args.no_xlsx,
                  export_docx=not args.no_docx, export_pdf=not args.no_pdf,
                  d2_card=args.d2_card,
                  emit_d2_card=not args.no_d2_card,
                  verbose=args.verbose, views=args.view)

    if res.get("error"):
        print("ERROR: " + res["error"])
        for _line in (res.get("hints") or []):
            print("  · " + _line)
        if res.get("detail"):
            print("  涉及文件：" + json.dumps(res["detail"], ensure_ascii=False))
        if res.get("checklist"):
            print("  排查方向：")
            for _i, _line in enumerate(res["checklist"], 1):
                print("  %d. %s" % (_i, _line))
        sys.exit(1)

    slim = {k: v for k, v in res.items() if not k.startswith("_")}
    slim["outputs"] = res.get("outputs")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(slim, f, ensure_ascii=False, indent=2, default=str)
    e8 = res.get("eightd") or {}
    if e8.get("intake_card") and not e8.get("d2_filled"):
        print("\n[8D] 已生成 D2 录入卡：%s" % e8["intake_card"])
        print("     填写后回传：`--d2-card <路径>`，即可完成 5W2H 交叉校验并出完整 D1–D8。")
    if not e8.get("signoff", {}).get("all_signed"):
        print("[8D] ⚠ D5–D8 未经人工确审，本报告不得对外发出。")

    print(json.dumps({"summary": res["summary"], "outputs": res["outputs"],
                      "confidence": res.get("confidence"),
                      "gaps": res.get("gaps"),
                      "process": res.get("process_name"),
                      "target": res.get("target")},
                     ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
