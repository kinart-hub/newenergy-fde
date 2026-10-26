# -*- coding: utf-8 -*-
"""
kbstore.py —— 工艺知识库持续演进引擎

设计目标：把「一次性交付的知识库」变成「能随现场实践累积的活知识库」，
同时保证每一次变更可追溯、可审核、可回滚、可分发。

四层结构：
  1) 正库     knowledge/process_specs.json        —— 参数 / 工序 / 缺陷机理
     正库     knowledge/improvement_library.json  —— 现场验证过的改善措施（PDCA / PFMEA）
     正库     knowledge/interp_rules.json         —— 判读规则条目
  2) 暂存     knowledge/inbox.jsonl               —— 候选区，人工审核前不生效
  3) 清单     knowledge/index.json + CHANGELOG.md —— 版本、条目数、变更留痕
  4) 归档     knowledge/backups/                  —— 每次写库前的自动备份，支持回滚

硬规则：任何一条知识必须"先进暂存、人工确认、再入正库"；机器在分析中发现的新线索
只生成候选，绝不自动改写知识库。
"""

try:
    from . import degrade          # 包模式
except Exception:                  # 脚本模式（python lib/pipeline.py）无包上下文
    import degrade                 # noqa
import io
import json
import math
import os
import re
import copy
import datetime

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_orig_root = SKILL_ROOT
KB_DIR = os.path.join(SKILL_ROOT, "knowledge")
BACKUP_DIR = os.path.join(KB_DIR, "backups")

SPEC_FILE = os.path.join(KB_DIR, "process_specs.json")
CASE_FILE = os.path.join(KB_DIR, "improvement_library.json")
INTERP_FILE = os.path.join(KB_DIR, "interp_rules.json")
INDEX_FILE = os.path.join(KB_DIR, "index.json")
INBOX_FILE = os.path.join(KB_DIR, "inbox.jsonl")


DEV_PROFILE_FILE = os.path.join(KB_DIR, "device_profiles.json")

try:
    from . import preprocess as PP
except Exception:                       # 直接以脚本方式运行时回退
    import preprocess as PP             # noqa
try:
    from . import devices as DEV
except Exception:
    import devices as DEV               # noqa
try:
    from . import tenancy as TN
except Exception:
    import tenancy as TN                # noqa

CHANGELOG = os.path.join(KB_DIR, "CHANGELOG.md")
GUIDE_MD = os.path.join(KB_DIR, "分析方法与判读指南.md")

CANON_FILES = {"specs": SPEC_FILE, "cases": CASE_FILE,
               "interp": INTERP_FILE, "index": INDEX_FILE,
               "dev": DEV_PROFILE_FILE}


def set_root(path):
    """切换知识库根目录（自检用隔离环境，避免污染真实正库）；传 None 还原"""
    global KB_DIR, BACKUP_DIR, SPEC_FILE, CASE_FILE, INTERP_FILE, \
        INDEX_FILE, INBOX_FILE, CHANGELOG, GUIDE_MD, CANON_FILES, DEV_PROFILE_FILE
    base = path or _orig_root
    KB_DIR = os.path.join(base, "knowledge")
    BACKUP_DIR = os.path.join(KB_DIR, "backups")
    SPEC_FILE = os.path.join(KB_DIR, "process_specs.json")
    CASE_FILE = os.path.join(KB_DIR, "improvement_library.json")
    INTERP_FILE = os.path.join(KB_DIR, "interp_rules.json")
    INDEX_FILE = os.path.join(KB_DIR, "index.json")
    INBOX_FILE = os.path.join(KB_DIR, "inbox.jsonl")
    CHANGELOG = os.path.join(KB_DIR, "CHANGELOG.md")
    GUIDE_MD = os.path.join(KB_DIR, "分析方法与判读指南.md")
    DEV_PROFILE_FILE = os.path.join(KB_DIR, "device_profiles.json")
    CANON_FILES = {"specs": SPEC_FILE, "cases": CASE_FILE,
                   "interp": INTERP_FILE, "index": INDEX_FILE,
                   "dev": DEV_PROFILE_FILE}
    invalidate_cache()
    # 设备档案在 devices 模块里也有一份路径常量与缓存，必须一起切：
    # 否则隔离目录中新登记的设备会被随后一次 devices 读取回落成真实正库。
    try:
        DEV.PROFILE_FILE = DEV_PROFILE_FILE
        DEV.invalidate_cache()
    except Exception as _dg_e:
        degrade.note("kbstore.set_root@L88", _dg_e)
    return KB_DIR


# 候选类型 -> 目标正库
TYPE_TARGET = {
    "param": "specs",             # 新参数（工序参数 / 测量指标）
    "param_alias": "specs",       # 为已有参数补充列名别名
    "param_limit": "specs",       # 修订已有参数的控制限 / 典型量程
    "process": "specs",           # 新工序
    "defect_map": "specs",        # 缺陷 <-> 参数 的机理映射（新增或补充）
    "defect_new": "specs",        # 新缺陷类型
    "gap_rule": "specs",          # 数据索取规则
    "guide_entry": "interp",      # 分析方法与判读指南条目
    "improvement_case": "cases",  # 现场 PDCA / PFMEA 验证过的改善措施
    "device_profile": "dev",      # 检测设备的导出档案（版式 + 私有列名 + 单位约定）
}

# 只有这些方法验证过的改善措施才允许进入"可推荐"级别
VERIFIED_METHODS = ("PDCA", "PFMEA", "DOE", "PFMEA+PDCA", "DOE+PDCA", "量产验证")

_merge_cache = {}


# ------------------------------------------------------------------ 基础 IO

def _hs():
    """拿到 hotswap 模块。包模式 / 脚本模式两条入口都要能取到。"""
    try:
        from . import hotswap as HS
    except Exception:
        import hotswap as HS                         # noqa
    return HS


def _read_json(path, default):
    # 租户模式下走「基线 + 租户覆盖」的合并读：
    # 基线的改动自动流入租户，租户的写入又完全不会碰到基线。
    if TN.enabled():
        tpath = TN.map_to_tenant(path)
        if tpath != path:
            return TN.layered_read(path, tpath, default)
        if os.path.exists(tpath):
            try:
                return json.loads(_hs().read_text_retry(tpath))
            except OSError:
                # 撞上 replace 窗口且重试耗尽：不是"文件坏了"，但也不能当空库默默用，
                # 让调用方按缺数据处理（caller 会给出可见的提示，而不是静默出结论）
                return copy.deepcopy(default)
            except Exception:
                return copy.deepcopy(default)
        return copy.deepcopy(default)
    if not os.path.exists(path):
        return copy.deepcopy(default)
    try:
        return json.loads(_hs().read_text_retry(path))
    except OSError:
        return copy.deepcopy(default)
    except Exception:
        return copy.deepcopy(default)


def _write_json(path, data):
    # 租户模式下写入一律重定向到租户层；基线保持只读
    target = TN.map_to_tenant(path) if TN.enabled() else path
    # 重定向之后必须再核一次落点。重定向是"想去租户层"，
    # 这道检查是"真的到了租户层"——万一租户目录里有人放了指向库外的
    # 符号链接，重定向的结果仍然会写到外面去。两件事不能互相替代。
    ok, why, real = TN.guard_write_path(target)
    if not ok:
        try:
            from . import degrade as DG
            DG.note("kbstore._write_json", why)
        except Exception:
            pass                      # scan:keep 台账不可用时不得反向阻断写入 path
        raise IOError(why)
    try:
        from . import hotswap as HS
    except Exception:
        import hotswap as HS                         # noqa
    # 走 hotswap 而非裸 os.replace：Windows 上目标文件正被读时 replace 会被拒绝
    # （实测并发读下失败率 1/3），必须退避重试，否则基线更新会随机失败
    HS.atomic_write_json(target, data)


def _inbox_path():
    """候选区必须按租户隔离：A 企业看到的候选不该包含 B 企业的"""
    return TN.map_to_tenant(INBOX_FILE) if TN.enabled() else INBOX_FILE


def _layered_text_path(path):
    """说明类文本（指南/变更记录）：租户有就用租户的，没有就读基线"""
    if not TN.enabled():
        return path
    t = TN.map_to_tenant(path)
    return t if os.path.exists(t) else path


def _guide_path():
    return _layered_text_path(GUIDE_MD)


def _changelog_path():
    return _layered_text_path(CHANGELOG)


def _backup_dir():
    """备份落在租户自己的目录里，避免一家企业的回滚点污染基线"""
    if TN.enabled():
        d = os.path.join(TN.tenant_kb(), "backups")
        os.makedirs(d, exist_ok=True)
        return d
    return BACKUP_DIR


def _text_write(path, text):
    """与 _write_json 同款的重定向 + 原子写，用于 md/jsonl 文本"""
    target = TN.map_to_tenant(path) if TN.enabled() else path
    try:
        from . import hotswap as HS
    except Exception:
        import hotswap as HS                         # noqa
    HS.atomic_write_text(target, text)
    return target


def _now():
    return datetime.datetime.now()


def _stamp():
    """含微秒，避免同秒内多次写库时备份文件名互相覆盖导致回滚点丢失"""
    return _now().strftime("%Y%m%d-%H%M%S-%f")[:21]


def _today():
    return _now().strftime("%Y-%m-%d")


def load(file_key):
    return _read_json(CANON_FILES[file_key], {})


def save(file_key, data):
    backup(file_key)
    _write_json(CANON_FILES[file_key], data)
    return True


def backup(file_key, tag="auto"):
    """
    写库前自动备份，保证任何一次 sync 都可回滚。
    备的是**当前生效内容**（基线+租户合并后的视图），不是基线原文——
    否则租户回滚后会把基线值一起带进来，等于越权改了别人的东西。
    """
    d = _backup_dir()
    os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, "%s-%s-%s.json" % (file_key, tag, _stamp()))
    n = 0
    while os.path.exists(dest):          # 二次保险：任何情况下不覆盖已有备份
        n += 1
        dest = os.path.join(d, "%s-%s-%s-%d.json" % (file_key, tag, _stamp(), n))
    content = json.dumps(load(file_key), ensure_ascii=False, indent=2)
    _text_write(dest, content)
    _prune_backups(file_key, keep=30)
    return dest


def _prune_backups(file_key, keep=30):
    d = _backup_dir()
    if not os.path.isdir(d):
        return
    files = sorted([f for f in os.listdir(d)
                    if f.startswith(file_key + "-")], reverse=True)
    for f in files[keep:]:
        try:
            os.remove(os.path.join(d, f))
        except Exception as _dg_e:
            degrade.note("kbstore._prune_backups@L255", _dg_e)


def list_backups(file_key=None):
    d = _backup_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d), reverse=True):
        if file_key and not f.startswith(file_key + "-"):
            continue
        out.append({"file": f, "path": os.path.join(d, f),
                    "size": os.path.getsize(os.path.join(d, f))})
    return out


def rollback(file_key, backup_name):
    src = os.path.join(_backup_dir(), backup_name)
    if not os.path.exists(src):
        return False, "备份不存在：%s" % backup_name
    with io.open(src, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        obj = json.loads(content)
    except Exception as e:
        return False, "备份文件损坏：%s" % e
    backup(file_key, tag="pre-rollback")
    # 原子写：早期版本在这里直接 open("w") 覆盖，中断就会留下半个文件
    _write_json(CANON_FILES[file_key], obj)
    return True, "已从 %s 回滚 %s" % (backup_name, file_key)


# ------------------------------------------------------------------ 索引 / 版本

DEFAULT_INDEX = {
    "name": "新能源工艺知识库清单",
    "doc_version": "1.0.0",
    "assets": {
        "specs": {"path": "knowledge/process_specs.json", "role": "机器可读：参数/工序/缺陷机理/数据索取规则",
                  "count": 0, "version": "1.0.0", "updated": ""},
        "cases": {"path": "knowledge/improvement_library.json", "role": "机器可读：现场验证过的改善措施库（PDCA/PFMEA）",
                  "count": 0, "version": "1.0.0", "updated": ""},
        "interp": {"path": "knowledge/interp_rules.json", "role": "机器可读：判读规则与分析方法条目",
                   "count": 0, "version": "1.0.0", "updated": ""},
        "dev": {"path": "knowledge/device_profiles.json",
                "role": "机器可读：检测设备导出版式档案与私有列名、单位约定",
                "count": 0, "version": "1.0.0", "updated": ""},
        "guide_md": {"path": "knowledge/分析方法与判读指南.md", "role": "人类可读：判读指南全文（附录区由脚本追加）",
                     "count": 0, "version": "1.0.0", "updated": ""},
        "battery_md": {"path": "knowledge/锂电池工序知识库.md", "role": "人类可读：锂电工序工艺知识",
                       "count": 0, "version": "1.0.0", "updated": ""},
        "pv_md": {"path": "knowledge/光伏工序知识库.md", "role": "人类可读：光伏工序工艺知识",
                  "count": 0, "version": "1.0.0", "updated": ""},
    },
    "history": []
}


def load_index():
    idx = _read_json(INDEX_FILE, copy.deepcopy(DEFAULT_INDEX))
    for k, v in DEFAULT_INDEX["assets"].items():
        idx.setdefault("assets", {}).setdefault(k, copy.deepcopy(v))
    return idx


def save_index(idx):
    _write_json(INDEX_FILE, idx)


def _count_asset(key):
    if key == "specs":
        d = _read_json(SPEC_FILE, {})
        return len(d.get("params", {})) + len(d.get("processes", {})) + len(d.get("defect_rootcause", {}))
    if key == "cases":
        return len(_read_json(CASE_FILE, {"cases": []}).get("cases", []))
    if key == "interp":
        return len(_read_json(INTERP_FILE, {"entries": []}).get("entries", []))
    if key == "dev":
        return len(_read_json(DEV_PROFILE_FILE, {"devices": {}}).get("devices", {}))
    if key.endswith("_md"):
        rel = DEFAULT_INDEX["assets"][key]["path"]
        p = os.path.join(SKILL_ROOT, rel if rel.startswith("knowledge") else rel)
        if not os.path.exists(p):
            p = os.path.join(KB_DIR, os.path.basename(rel))
        if not os.path.exists(p):
            return 0
        with io.open(p, "r", encoding="utf-8") as f:
            body = f.read()
        return len(re.findall(r"^### ", body, re.M)) or len(re.findall(r"^## ", body, re.M))
    return 0


def refresh_index(note=""):
    idx = load_index()
    for key, meta in idx["assets"].items():
        if key.endswith("_md"):
            continue
        tgt_file = CANON_FILES.get(key)
        if not tgt_file:
            continue
        d = _read_json(tgt_file, {})
        ver = (d.get("meta", {}) or {}).get("version") or meta.get("version", "1.0.0")
        meta["count"] = _count_asset(key)
        meta["version"] = ver
        meta["updated"] = (d.get("meta", {}) or {}).get("updated") or meta.get("updated", "")
    for key, meta in idx["assets"].items():
        if key.endswith("_md"):
            meta["count"] = _count_asset(key)
    if note:
        idx.setdefault("history", []).append({"date": _today(), "note": note})
    idx["updated"] = _today()
    save_index(idx)
    return idx


def _bump(version, kind):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", str(version or "1.0.0"))
    if not m:
        return str(kind) and "1.0.1" if kind == "patch" else "1.1.0"
    a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if kind == "minor":
        return "%d.%d.0" % (a, b + 1)
    return "%d.%d.%d" % (a, b, c + 1)


def bump_meta(file_key, kind="patch"):
    d = _read_json(CANON_FILES[file_key], {})
    meta = d.setdefault("meta", {})
    meta["version"] = _bump(meta.get("version", "1.0.0"), kind)
    meta["updated"] = _today()
    meta["revision"] = int(meta.get("revision", 0)) + 1
    _write_json(CANON_FILES[file_key], d)
    return meta["version"]


# ------------------------------------------------------------------ 候选区 inbox

def load_candidates(status=None):
    out = []
    # 必须与下面打开的是同一个路径：早期版本检查 base 却打开租户路径，
    # 全新租户第一次调用就会 FileNotFoundError
    p = _inbox_path()
    if not os.path.exists(p):
        return out
    with io.open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if status and rec.get("status") != status:
                continue
            out.append(rec)
    return out


def _append_candidate(rec):
    os.makedirs(os.path.dirname(_inbox_path()), exist_ok=True)
    with io.open(_inbox_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _rewrite_candidates(recs):
    os.makedirs(os.path.dirname(_inbox_path()), exist_ok=True)
    with io.open(_inbox_path(), "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def next_cid(prefix="C"):
    exist = [c.get("id", "") for c in load_candidates()]
    today = _now().strftime("%Y%m%d")
    n = 1
    while True:
        cid = "%s%s-%03d" % (prefix, today, n)
        if cid not in exist:
            return cid
        n += 1


def add_candidate(ctype, payload, source=None, verification=None,
                  proposed_by="工艺工程师", note=""):
    if ctype not in TYPE_TARGET:
        return None, "未知候选类型：%s（可选：%s）" % (ctype, ", ".join(sorted(TYPE_TARGET)))
    ok, errs, warns = validate_candidate(ctype, payload)
    if not ok:
        return None, "校验不通过：\n  - " + "\n  - ".join(errs)
    rec = {
        "id": next_cid(),
        "type": ctype,
        "target": TYPE_TARGET[ctype],
        "status": "pending",
        "created": _now().isoformat(timespec="seconds"),
        "proposed_by": proposed_by,
        "payload": payload,
        "source": source or {},
        "verification": verification or {},
        "note": note,
        "warnings": warns,
        "review": {},
    }
    _append_candidate(rec)
    return rec, None


def update_candidate(cid, patch, merge_payload=False):
    recs = load_candidates()
    hit = None
    for r in recs:
        if r.get("id") == cid:
            hit = r
            break
    if hit is None:
        return None, "候选不存在：%s" % cid
    if hit.get("status") in ("approved", "merged", "rejected"):
        return None, "候选 %s 已处于 %s 状态，不可修改（如需重提请新增候选）" % (cid, hit["status"])
    pl = patch.pop("payload", None)
    if pl and merge_payload:
        hit["payload"].update(pl)
    elif pl:
        hit["payload"] = pl
    for k, v in patch.items():
        hit[k] = v
    ok, errs, warns = validate_candidate(hit["type"], hit["payload"])
    if not ok:
        return None, "修改后校验不通过：\n  - " + "\n  - ".join(errs)
    hit["warnings"] = warns
    _rewrite_candidates(recs)
    return hit, None


# ------------------------------------------------------------------ 校验

REQUIRED = {
    "param": ["key", "name", "process", "unit", "role"],
    "param_alias": ["key", "aliases"],
    "param_limit": ["key", "control"],
    "process": ["key", "name", "domain"],
    "defect_map": ["defect", "added_likely_params"],
    "defect_new": ["key", "process", "mechanism"],
    "gap_rule": ["level", "items"],
    "guide_entry": ["title", "rule"],
    "improvement_case": ["defect", "process", "countermeasure", "verification"],
    "device_profile": ["key", "name", "vendor", "format"],
}

ALLOWED_ROLES = ("control", "response", "group")
ROLE_DESC = {"control": "可调控因子（可作为 DOE 因子）",
             "response": "结果指标（不可作为 DOE 因子）",
             "group": "分组维度（设备/批次/供应商等，用于分层分析）"}
ALLOWED_MODES = ("range", "usl", "lsl", "deviation", "deviation_pct",
                 "cv_limit_pct", "attribute", "none")
ALLOWED_LEVELS = ("P0_必需", "P1_重要", "P2_增强")
ALLOWED_ACTIONS = ("设备改造", "工艺参数调整", "来料管控", "作业标准化",
                   "维保制度", "环境控制", "检验加严", "原材料替换", "其他")


def _existing_specs():
    try:
        from . import hotswap as HS
    except Exception:
        import hotswap as HS                         # noqa
    # 同样要盯文件变更：sync 完后这里若还抱着旧内容，
    # 后续校验会拿"更新前"的库去判"更新后"的候选，出现本不该有的冲突误报
    if "specs" in _merge_cache and HS.changed("kbs_specs", [SPEC_FILE,
                                                            TN.map_to_tenant(SPEC_FILE)]):
        _merge_cache.pop("specs", None)
    if "specs" not in _merge_cache:
        _merge_cache["specs"] = _read_json(SPEC_FILE, {})
    return _merge_cache["specs"]


def invalidate_cache():
    _merge_cache.clear()
    try:
        from . import hotswap as HS
    except Exception:
        import hotswap as HS                         # noqa
    HS.forget()


def validate_candidate(ctype, payload):
    """返回 (是否放行, 硬性错误, 软性提示)。硬错误不允许入库，软提示仅记录。"""
    errs, warns = [], []
    if not isinstance(payload, dict):
        return False, ["payload 必须是对象"], []
    for k in REQUIRED.get(ctype, []):
        if k not in payload or payload[k] in ("", None, []):
            errs.append("缺少必填字段：%s" % k)
    if errs:
        return False, errs, warns

    kb = _existing_specs()
    params = kb.get("params", {})
    processes = kb.get("processes", {})
    defects = kb.get("defect_rootcause", {})

    if ctype == "param":
        if payload["key"] in params:
            errs.append("参数 key 已存在（%s），如需修订请改用 param_alias / param_limit" % payload["key"])
        if payload["process"] not in processes:
            errs.append("工序 key 不存在：%s（需先用 --type process 新增工序）" % payload["process"])
        if payload["role"] not in ALLOWED_ROLES:
            errs.append("role 必须是 %s，收到 %s"
                        % (" / ".join("%s=%s" % (k, v) for k, v in ROLE_DESC.items()),
                           payload["role"]))
        tr = payload.get("typical_range")
        if tr is not None:
            if not (isinstance(tr, list) and len(tr) == 2):
                errs.append("typical_range 必须是 [下限, 上限]")
            elif not (isinstance(tr[0], (int, float)) and isinstance(tr[1], (int, float))):
                errs.append("typical_range 元素必须是数值")
            elif tr[0] >= tr[1]:
                errs.append("typical_range 下限必须小于上限")
        ctrl = payload.get("control") or {}
        if isinstance(ctrl, dict) and ctrl.get("mode") and ctrl["mode"] not in ALLOWED_MODES:
            errs.append("control.mode 非法：%s（可选 %s）" % (ctrl["mode"], "/".join(ALLOWED_MODES)))
        if not payload.get("aliases"):
            warns.append("未填 aliases（列名别名），该参数将无法从 MES 导出表自动识别")
        src = (payload.get("source_tier") or (payload.get("verification") or {}).get("tier") or "reference")
        if tr is not None and src != "customer_spec":
            warns.append("typical_range 非企业受控规格来源，仅登记为行业典型参考值，不参与判定")

    elif ctype == "param_alias":
        if payload["key"] not in params:
            errs.append("参数 key 不存在：%s（不存在时请改用 --type param）" % payload["key"])
        if not payload.get("aliases"):
            errs.append("aliases 不能为空")

    elif ctype == "param_limit":
        if payload["key"] not in params:
            errs.append("参数 key 不存在：%s（不存在时请改用 --type param）" % payload["key"])
        ctrl = payload.get("control") or {}
        if not isinstance(ctrl, dict) or not ctrl.get("mode"):
            errs.append("control 必须给出 mode")
        elif ctrl["mode"] not in ALLOWED_MODES:
            errs.append("control.mode 非法：%s" % ctrl["mode"])
        old = (params.get(payload["key"], {}).get("control") or {})
        for mk in ("limit", "limit_pct", "usl", "lsl"):
            if mk in ctrl and mk in old and isinstance(ctrl[mk], (int, float)) \
                    and isinstance(old[mk], (int, float)) and old[mk] != 0:
                delta = abs(ctrl[mk] - old[mk]) / abs(old[mk])
                if delta > 0.30:
                    warns.append("「%s」的控制值 %.4g → %.4g，变化 %.0f%%，超过 30%% 需人工确认后才能生效"
                                 % (payload["key"], old[mk], ctrl[mk], delta * 100))
        if (payload.get("verification") or {}).get("method") not in VERIFIED_METHODS:
            warns.append("修改既有控制限建议附带验证方法（PDCA/PFMEA/DOE），否则审核时难以放行")

    elif ctype == "defect_map":
        if payload["defect"] not in defects:
            errs.append("缺陷 key 不存在：%s（不存在时请改用 --type defect_new）" % payload["defect"])
        for pk in payload.get("added_likely_params", []):
            if pk not in params:
                errs.append("likely_params 引用了不存在的参数 key：%s" % pk)
        if payload.get("evidence") and not isinstance(payload["evidence"], (str, dict)):
            errs.append("evidence 需为字符串或对象")

    elif ctype == "defect_new":
        if payload["process"] not in processes:
            errs.append("工序 key 不存在：%s" % payload["process"])
        for pk in payload.get("likely_params", []):
            if pk not in params:
                errs.append("likely_params 引用了不存在的参数 key：%s" % pk)
        if not payload.get("verify_data"):
            warns.append("未填 verify_data，后续无法向用户索取支撑数据")

    elif ctype == "gap_rule":
        if payload["level"] not in ALLOWED_LEVELS:
            errs.append("level 必须是 %s" % " / ".join(ALLOWED_LEVELS))
        if not isinstance(payload["items"], list) or not payload["items"]:
            errs.append("items 必须是非空数组")

    elif ctype == "guide_entry":
        for k in ("title", "rule"):
            if not str(payload.get(k) or "").strip():
                errs.append("%s 不能为空" % k)
        if payload.get("strength") not in (None, "强制", "建议", "参考"):
            errs.append("strength 必须是 强制/建议/参考 之一")

    elif ctype == "improvement_case":
        if payload["process"] not in processes:
            errs.append("工序 key 不存在：%s" % payload["process"])
        vf = payload.get("verification") or {}
        if vf.get("method") and vf["method"] not in VERIFIED_METHODS:
            errs.append("验证方法必须是 %s 之一，收到 %s" % ("/".join(VERIFIED_METHODS), vf["method"]))
        cm = payload.get("countermeasure") or {}
        if not cm.get("action"):
            errs.append("countermeasure.action 不能为空")
        if cm.get("type") and cm["type"] not in ALLOWED_ACTIONS:
            errs.append("countermeasure.type 必须是 %s 之一" % "/".join(ALLOWED_ACTIONS))
        if (payload.get("evidence_level") or "").upper() not in ("A", "B", "C", ""):
            errs.append("evidence_level 必须是 A/B/C")

    elif ctype == "device_profile":
        fmt = payload.get("format") or {}
        if fmt.get("layout") not in ("wide", "long", "blocked", "auto", None):
            errs.append("format.layout 必须是 wide/long/blocked/auto，收到 %s" % fmt.get("layout"))
        for p in (payload.get("processes") or []):
            if p not in processes:
                errs.append("引用了不存在的工序：%s" % p)
        for cn, key in (payload.get("aliases") or {}).items():
            if key and key not in params:
                errs.append("列名「%s」指向未登记参数 %s（需先登记该参数）" % (cn, key))
        # 注意要传当前根目录：自检时 KB_DIR 会被切到隔离目录，
        # 用 devices 的默认路径会拿到真实正库，导致隔离失效。
        try:
            ok_pairs = set()
            for c in (DEV.load_profiles(DEV_PROFILE_FILE).get("unit_conversions") or {}).values():
                ok_pairs.add((DEV._canon_unit(c.get("from")), DEV._canon_unit(c.get("to"))))
            for cn, m in list((payload.get("unit_map") or {}).items()) + \
                         list((payload.get("key_unit_map") or {}).items()):
                pr = (DEV._canon_unit(m.get("read_unit")), DEV._canon_unit(m.get("target_unit")))
                if pr[0] != pr[1] and pr not in ok_pairs:
                    errs.append("单位换算 %s: %s→%s 无对应换算规则"
                                % (cn, m.get("read_unit"), m.get("target_unit")))
        except Exception as _dg_e:
            degrade.note("kbstore.validate_candidate@L674", _dg_e)
        sig = payload.get("signature") or {}
        if not (sig.get("meta_regex") or sig.get("col_any") or sig.get("filename_regex")):
            warns.append("未登记任何识别特征，该设备永远不会被自动匹配到（只能用 --device 手动指定）")
        if fmt.get("layout") == "long":
            if not (fmt.get("long_key_col") and fmt.get("long_value_col")):
                errs.append("竖表档案必须同时指定 long_key_col 与 long_value_col，否则无法转成宽表")
        if not payload.get("aliases"):
            warns.append("未登记任何私有列名映射，识别到设备也认不出它的检测项")

    return (len(errs) == 0), errs, warns


# ------------------------------------------------------------------ 证据分级

def grade_case(payload):
    """按 PDCA / PFMEA / DOE 的完整性给改善措施定级。

    A: 有量化前后对比 + 样本量 ≥30 + 标准化已固化  -> 可直接推荐
    B: 流程完整但样本/量化不足                      -> 推荐但标注待验证
    C: 仅经验描述                                   -> 只入库，不参与推荐
    """
    vf = payload.get("verification") or {}
    res = vf.get("result") or {}
    method = vf.get("method") or ""
    pdca = vf.get("pdca") or {}
    fmea = vf.get("fmea") or {}
    std = payload.get("standardization") or {}

    quantified = bool(res.get("before") is not None and res.get("after") is not None)
    try:
        n = float(res.get("sample_size") or 0)
    except Exception:
        n = 0
    try:
        rb = float(fmea.get("rpn_before") or 0)
        ra = float(fmea.get("rpn_after") or 0)
    except Exception:
        rb = ra = 0.0
    rpn_drop = (rb - ra) / rb if rb else 0.0
    pdca_full = all(str(pdca.get(k) or "").strip() for k in ("plan", "do", "check", "act"))

    if method not in VERIFIED_METHODS:
        return "C", ["未标注 PDCA/PFMEA/DOE 验证方法，按经验条目入库、不参与推荐"]
    reasons = []
    if quantified and n >= 30 and (std.get("doc_no") or std.get("control_plan_updated")):
        return "A", ["量化前后对比 + 样本 n=%d + 已标准化固化" % int(n)]
    if quantified and (n >= 30 or rpn_drop >= 0.5):
        return "A", ["量化验证充分（n=%d，RPN 降幅 %.0f%%）" % (int(n), rpn_drop * 100)]
    if pdca_full or quantified:
        return "B", ["流程完整但样本量偏小（n=%d）或缺少标准化文件号" % int(n)]
    if fmea and rb:
        return "B", ["有 PFMEA 记录（RPN %d→%d）但缺闭环验证数据" % (int(rb), int(ra))]
    return "C", ["验证信息不完整，仅作经验留存"]


# ------------------------------------------------------------------ 合并入库

def merge_one(rec, force=False):
    """把一条已审核通过的候选合并进正库。返回 (ok, message, changelog_entry)"""
    ctype, pl = rec.get("type"), rec.get("payload", {})
    tgt = TYPE_TARGET.get(ctype)
    if tgt is None or tgt not in CANON_FILES:
        return False, "未知候选类型：%s" % ctype, None
    d = _read_json(CANON_FILES[tgt], {})

    if tgt == "dev":
        key = pl.get("key")
        if not key:
            return False, "设备档案缺少 key（设备型号的内部标识）", None
        devices = d.setdefault("devices", {})
        if key in devices and not force:
            return False, "设备 %s 已存在（需 --force 覆盖）" % key, None
        spec = dict(pl)
        spec.pop("key", None)
        spec.setdefault("vendor", "unknown")
        spec.setdefault("category", "unknown")
        spec.setdefault("processes", [])
        spec.setdefault("aliases", {})
        spec.setdefault("drop_cols", [])
        fmt = spec.setdefault("format", {})
        fmt.setdefault("layout", "wide")
        fmt.setdefault("header_rows", 1)
        fmt.setdefault("footer_markers", [])
        spec["_kbmeta"] = _provenance(rec)
        devices[key] = spec
        DEV.invalidate_cache()
        entry = "登记检测设备 `%s`（%s / %s，关联工序 %s，来源：%s）" % (
            key, spec.get("name"), spec.get("vendor"),
            "、".join(spec.get("processes") or []) or "未指定",
            (rec.get("source") or {}).get("site_code") or "未提供")
        kind = "minor"
        # 注意：不能在这里 return，必须落到函数末尾统一的 save + bump_meta，
        # 否则只改了内存副本、文件没落盘（登记设备档案时踩过这个坑）。

    if tgt == "specs":
        d.setdefault("meta", {})
        if ctype == "param":
            key = pl["key"]
            if key in d.get("params", {}) and not force:
                return False, "参数 %s 已存在（需 --force 覆盖）" % key, None
            spec = dict(pl)
            spec.pop("key", None)
            spec.setdefault("note", "")
            spec.setdefault("aliases", [])
            spec.setdefault("role", "control")
            spec["_kbmeta"] = _provenance(rec)
            d.setdefault("params", {})[key] = spec
            _sync_index_changed()
            entry = "新增参数 `%s`（%s，%s %s，来源：%s）" % (
                key, spec.get("name"), spec.get("process"), spec.get("unit"),
                (rec.get("source") or {}).get("site_code") or "未提供")
            kind = "patch"

        elif ctype == "param_alias":
            key = pl["key"]
            spec = d.get("params", {}).get(key)
            if spec is None:
                return False, "参数不存在：%s" % key, None
            old = list(spec.get("aliases", []))
            added = [a for a in pl["aliases"] if a not in old]
            spec["aliases"] = old + added
            if added:
                spec.setdefault("_kbrevs", []).append({
                    "date": _today(), "field": "aliases", "added": added,
                    "by": rec.get("proposed_by"), "id": rec.get("id")})
            entry = "补充 `%s` 的列名别名 %d 个：%s" % (key, len(added), "、".join(added) or "（无新增）")
            kind = "patch"

        elif ctype == "param_limit":
            key = pl["key"]
            spec = d.get("params", {}).get(key)
            if spec is None:
                return False, "参数不存在：%s" % key, None
            warns_prev = rec.get("warnings") or []
            if warns_prev and not force:
                return False, "存在需人工确认的变更（%s）；确认后加 --force 重跑" % "；".join(warns_prev), None
            old_ctrl = dict(spec.get("control") or {})
            spec["control"] = pl["control"]
            spec.setdefault("_kbrevs", []).append({
                "date": _today(), "field": "control",
                "from": old_ctrl, "to": pl["control"],
                "tier": pl.get("source_tier") or "reference",
                "by": rec.get("proposed_by"), "id": rec.get("id")})
            entry = "修订 `%s` 控制限：%s → %s" % (key, old_ctrl, pl["control"])
            kind = "minor"

        elif ctype == "process":
            key = pl.pop("key")
            if key in d.get("processes", {}) and not force:
                return False, "工序 %s 已存在（需 --force 覆盖）" % key, None
            pl.setdefault("alias", [])
            pl.setdefault("key_params", [])
            pl.setdefault("common_defects", [])
            pl.setdefault("doe_factors", [])
            pl["_kbmeta"] = _provenance(rec)
            d.setdefault("processes", {})[key] = pl
            _sync_index_changed()
            entry = "新增工序 `%s`（%s）" % (key, pl.get("name"))
            kind = "patch"

        elif ctype == "defect_new":
            key = pl.pop("key")
            if key in d.get("defect_rootcause", {}) and not force:
                return False, "缺陷 %s 已存在（需 --force 覆盖）" % key, None
            pl.setdefault("likely_params", [])
            pl.setdefault("verify_data", [])
            pl.setdefault("quick_checks", [])
            pl["_kbmeta"] = _provenance(rec)
            d.setdefault("defect_rootcause", {})[key] = pl
            _sync_index_changed()
            entry = "新增缺陷类型「%s」（%s）" % (key, pl.get("process"))
            kind = "patch"

        elif ctype == "defect_map":
            dk = pl["defect"]
            node = d.get("defect_rootcause", {}).get(dk)
            if node is None:
                return False, "缺陷不存在：%s" % dk, None
            old = list(node.get("likely_params", []))
            added = [p for p in pl.get("added_likely_params", []) if p not in old]
            node["likely_params"] = old + added
            if pl.get("added_quick_checks"):
                node.setdefault("quick_checks", [])
                node["quick_checks"] += [q for q in pl["added_quick_checks"]
                                         if q not in node["quick_checks"]]
            if pl.get("evidence"):
                node.setdefault("_kbevid", []).append({
                    "date": _today(), "evidence": pl["evidence"],
                    "method": (rec.get("verification") or {}).get("method"),
                    "by": rec.get("proposed_by"), "id": rec.get("id")})
            entry = "补充缺陷「%s」的根因参数 %s" % (dk, "、".join(added) or "（无新增）")
            kind = "minor"

        elif ctype == "gap_rule":
            lv = pl["level"]
            levels = d.get("data_gaps", {}).get("levels", [])
            hit = [x for x in levels if x.get("level") == lv]
            if not hit:
                return False, "优先级不存在：%s" % lv, None
            old = list(hit[0].get("items", []))
            added = [i for i in pl["items"] if i not in old]
            hit[0]["items"] = old + added
            entry = "补充 %s 数据索取项：%s" % (lv, "、".join(added) or "（无新增）")
            kind = "patch"
        else:
            return False, "未实现的 specs 类型：%s" % ctype, None

    elif tgt == "cases":
        d.setdefault("meta", {"name": "现场验证改善措施库", "version": "1.0.0",
                              "updated": _today(),
                              "note": "仅收录经 PDCA / PFMEA / DOE 验证并在现场生效的措施。"
                                      "A 级可直接推荐，B 级推荐时标注待验证，C 级只作经验留存。"})
        # 声明等级只能调低不能调高：自评 A 但材料不足时，按计算的低级别入库
        level, why = grade_case(pl)
        if pl.get("evidence_level"):
            claimed = str(pl["evidence_level"]).upper()
            if claimed in ("A", "B", "C"):
                level = min(level, claimed, key=lambda x: {"A": 3, "B": 2, "C": 1}[x])
                why = why + ["自评等级 %s；按材料实际取值定为 %s" % (claimed, level)]
        case = dict(pl)
        case["id"] = case.get("id") or rec.get("id")
        case["evidence_level"] = level
        case["grade_reason"] = why
        case["can_recommend"] = level in ("A", "B")
        case["_kbmeta"] = _provenance(rec)
        d.setdefault("cases", []).append(case)
        _sync_index_changed()
        entry = "收录改善措施 [%s] 「%s」→ %s（级别 %s）" % (
            case["id"], (pl.get("countermeasure") or {}).get("action", "")[:40],
            pl.get("defect"), level)
        kind = "patch"

    elif tgt == "interp":
        d.setdefault("meta", {"name": "判读规则条目库", "version": "1.0.0", "updated": _today()})
        e = dict(pl)
        e["id"] = e.get("id") or rec.get("id")
        e["strength"] = e.get("strength") or "建议"
        e["_kbmeta"] = _provenance(rec)
        d.setdefault("entries", []).append(e)
        _sync_index_changed()
        _append_guide_md(e)
        entry = "新增判读条目 [%s] %s（%s）" % (e["id"], e.get("title"), e.get("strength"))
        kind = "patch"
    elif tgt != "dev":
        # dev 分支在上面已经备好 entry/kind，一路落到这里只为共用下面的写库逻辑，
        # 不能再把它判成"未知目标库"（曾经因为这么判，登记设备全部失败）。
        return False, "未知目标库：%s" % tgt, None

    save(tgt, d)
    ver = bump_meta(tgt, kind)
    return True, entry, {"id": rec.get("id"), "type": ctype, "text": entry,
                         "version": ver, "date": _today(),
                         "proposed_by": rec.get("proposed_by")}


def _provenance(rec):
    return {
        "source": (rec.get("source") or {}).get("site_code") or (rec.get("source") or {}).get("site") or "未提供",
        "date": (rec.get("created") or "")[:10],
        "by": rec.get("proposed_by"),
        "candidate_id": rec.get("id"),
        "method": (rec.get("verification") or {}).get("method"),
    }


def _sync_index_changed():
    """正库结构变化后强制下次重新读取"""
    invalidate_cache()


def _append_guide_md(entry):
    """把判读条目幂等追加到人类可读 MD 的附录区"""
    if not os.path.exists(GUIDE_MD):
        return False
    with io.open(_guide_path(), "r", encoding="utf-8") as f:
        body = f.read()
    marker = "<!-- AUTO-APPEND:KB -->"
    eid = entry.get("id")
    if eid and ("`%s`" % eid) in body:
        return False      # 幂等：同一条目不重复追加
    block = [
        "",
        "### %s" % entry.get("title"),
        "",
        "- 条目编号：`%s`｜强度：%s｜登记：%s｜来源：%s" % (
            entry.get("id"), entry.get("strength"), _today(),
            (entry.get("_kbmeta") or {}).get("source", "未提供")),
    ]
    if entry.get("scope"):
        block.append("- 适用场景：%s" % entry["scope"])
    block += ["", str(entry.get("rule")).strip(), ""]
    if entry.get("pitfall"):
        block += ["> 易错点：%s" % entry["pitfall"], ""]
    text = "\n".join(block)
    # 单标记 + replace(count=1)：保证正序追加且不重复
    # （早期版本用了两个标记且 replace 全量替换，会重复插入，已修正）
    if marker in body:
        body = body.replace(marker, text + marker, 1)
    else:
        head = "---\n\n## 附录：现场补充判读经验（脚本自动累积）\n\n" \
               "本附录由 `kb_manage.py sync` 按登记时间顺序自动追加，勿手工编辑其中的条目编号行。"
        body = body.rstrip() + "\n\n" + head + "\n" + text + marker + "\n"
    with io.open(_guide_path(), "w", encoding="utf-8") as f:
        f.write(body)
    return True


# ------------------------------------------------------------------ 批量同步

def sync(ids=None, force=False, reviewer="工艺负责人"):
    """
    把审核通过的候选并入正库。

    并发保护不是可选项：云端多企业同时 sync 时，没有锁就会出现
    「读-改-写」交错覆盖——后写的把先写的整段吞掉，而且是静默的。
    """
    try:
        ctx = TN.lock("sync", timeout=15.0)
    except Exception as e:
        return {"merged": [], "locked_out": True, "logs": [],
                "failed": [{"id": None, "error": "写库锁获取失败：%s（稍后重试）" % e}]}
    with ctx:
        pend = [c for c in load_candidates()
                if c.get("status") == "approved"
                and (not ids or c.get("id") in ids)]
        done, failed, logs = [], [], []
        for rec in pend:
            ok, msg, entry = merge_one(rec, force=force)
            if ok:
                done.append(rec["id"])
                if entry:
                    logs.append(entry)
            else:
                failed.append({"id": rec.get("id"), "error": msg})
        # 标记已合并
        recs = load_candidates()
        for r in recs:
            if r.get("id") in done:
                r["status"] = "merged"
                r["review"]["merged_at"] = _now().isoformat(timespec="seconds")
                r["review"]["reviewer"] = reviewer
        _rewrite_candidates(recs)
        if logs:
            _write_changelog(logs)
        invalidate_cache()
        idx = refresh_index("同步 %d 条候选" % len(done))
    return {"merged": done, "failed": failed, "logs": logs, "index_date": idx.get("updated")}


def _write_changelog(logs):
    today = _today()
    if os.path.exists(CHANGELOG):
        with io.open(_changelog_path(), "r", encoding="utf-8") as f:
            head = f.read()
    else:
        head = ""
    lines = []
    for L in logs:
        lines.append("- `%s` %s —— %s（%s，版本 %s）"
                     % (L.get("id"), L.get("text"), L.get("proposed_by") or "-",
                        L.get("type"), L.get("version")))
    if not head.strip().startswith("# "):
        head = ("# 工艺知识库变更记录\n\n"
                "本文件由 `scripts/kb_manage.py sync` 自动追加，每一次变更对应一条 inbox 候选，"
                "可通过 `rollback` 回滚到变更前的备份。\n")
    marker = "\n## %s\n" % today
    if marker in head:
        # 同一天已有条目：追加到当天小节末尾，避免出现重复的日期标题
        i = head.index(marker) + len(marker)
        nxt = head.find("\n## ", i)
        if nxt < 0:
            nxt = len(head)
        head = head[:nxt].rstrip("\n") + "\n" + "\n".join(lines) + "\n" + head[nxt:]
        body = head
    else:
        body = head.rstrip() + marker + "\n" + "\n".join(lines) + "\n"
    with io.open(_changelog_path(), "w", encoding="utf-8") as f:
        f.write(body)


# ------------------------------------------------------------------ 分发补丁

SENSITIVE_KEYS = ("site", "site_code", "customer", "client", "plant", "factory",
                  "data_file", "analysis_run", "proposed_by")


def export_patch(since=None, outdir=None, anonymize=True, include=("specs", "cases", "interp")):
    """导出可分发的知识补丁：只含新增/修订条目，默认剥离客户来源信息"""
    outdir = outdir or os.path.join(SKILL_ROOT, "dist")
    os.makedirs(outdir, exist_ok=True)
    patch = {"kind": "newenergy-fde-knowledge-patch",
             "created": _now().isoformat(timespec="seconds"),
             "since_version": since or "", "entries": []}
    specs = _read_json(SPEC_FILE, {})
    for key, spec in specs.get("params", {}).items():
        if not spec.get("_kbmeta"):
            continue
        e = {"target": "specs", "op": "add_param", "payload": dict(spec, key=key)}
        patch["entries"].append(_scrub(e, anonymize))
    for key, p in specs.get("processes", {}).items():
        if p.get("_kbmeta"):
            patch["entries"].append(_scrub({"target": "specs", "op": "add_process",
                                            "payload": dict(p, key=key)}, anonymize))
    for key, dd in specs.get("defect_rootcause", {}).items():
        if dd.get("_kbmeta"):
            patch["entries"].append(_scrub({"target": "specs", "op": "add_defect",
                                            "payload": dict(dd, key=key)}, anonymize))
    if "cases" in include:
        for c in _read_json(CASE_FILE, {"cases": []}).get("cases", []):
            if c.get("_kbmeta"):
                patch["entries"].append(_scrub({"target": "cases", "op": "add_case",
                                                "payload": c}, anonymize))
    if "interp" in include:
        for e in _read_json(INTERP_FILE, {"entries": []}).get("entries", []):
            if e.get("_kbmeta"):
                patch["entries"].append(_scrub({"target": "interp", "op": "add_guide_entry",
                                                "payload": e}, anonymize))
    name = "知识补丁-%s.json" % _stamp()
    path = os.path.join(outdir, name)
    _write_json(path, patch)
    return {"path": path, "entries": len(patch["entries"]), "anonymized": anonymize}


def _scrub(entry, anonymize):
    if not anonymize:
        return entry
    e = copy.deepcopy(entry)
    pl = e.get("payload", {})
    if "_kbmeta" in pl:
        pl["_kbmeta"] = {"source": "行业共享", "date": pl["_kbmeta"].get("date"),
                         "method": pl["_kbmeta"].get("method")}
    pl.pop("_kbrevs", None)
    for k in ("standardization",):
        if isinstance(pl.get(k), dict):
            pl[k] = {kk: ("已脱敏" if kk in ("doc_no", "owner") else vv) for kk, vv in pl[k].items()}
    return e


def import_patch(path, auto_approve=False):
    """导入他人补丁：一律先进 inbox 待审核，不直接写正库"""
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            patch = json.load(f)
    except Exception as e:
        return {"imported": 0, "errors": ["读取失败：%s" % e], "ids": []}
    OP2TYPE = {"add_param": "param", "add_process": "process", "add_defect": "defect_new",
               "add_case": "improvement_case", "add_guide_entry": "guide_entry"}
    ids, errors = [], []
    for e in patch.get("entries", []):
        ctype = OP2TYPE.get(e.get("op"))
        if not ctype:
            errors.append("未知操作：%s" % e.get("op"))
            continue
        payload = dict(e.get("payload", {}))
        kbmeta = payload.pop("_kbmeta", {}) or {}
        rec, err = add_candidate(
            ctype, payload,
            source={"site_code": kbmeta.get("source") or "外部补丁", "origin": os.path.basename(path)},
            verification={"method": kbmeta.get("method") or "", "tier": "external"},
            proposed_by="外部补丁导入",
            note="来源补丁：%s" % os.path.basename(path))
        if err:
            errors.append("[%s] %s" % (ctype, err.replace("\n", " ")))
            continue
        ids.append(rec["id"])
        if auto_approve:
            update_candidate(rec["id"], {"status": "approved",
                                         "review": {"reviewer": "批量导入", "note": "外部补丁自动通过"}})
    return {"imported": len(ids), "ids": ids, "errors": errors}


# ------------------------------------------------------------------ 从分析中挖掘候选

def harvest_from_analysis(result, table, min_corr=0.55, alias_min=0.985):
    """从一次分析结果中自动生成"可能值得沉淀"的候选，交人工决定，绝不自动入库。"""
    kb = _existing_specs()
    params, defects = kb.get("params", {}), kb.get("defect_rootcause", {})
    col_map = result.get("col_map") or {}
    out = []
    known_keys = set(col_map.values())

    # 关联强度：数值列取 |r|；分类列取 sqrt(eta²)（等价于标准化效应量）
    effect = {}
    for row in (result.get("association") or []):
        col = row.get("param")
        if not col:
            continue
        v = row.get("r") if row.get("type") == "numeric" else row.get("abs_effect")
        if isinstance(v, (int, float)):
            effect[col] = float(v)

    for c in table.numeric_columns():
        if c in known_keys or col_map.get(c):
            continue
        n_ok = len(set(table.numeric_column(c)) - {None})
        if n_ok < 20:
            continue
        vals = [v for v in table.numeric_column(c) if v is not None]
        mean = sum(vals) / len(vals)
        if mean == 0:
            continue
        try:
            var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
            cv = (var ** 0.5) / abs(mean)
        except Exception:
            continue
        hit = effect.get(c)
        if hit is None or abs(hit) < min_corr:
            continue
        role = "response" if _looks_response(c) else "control"
        proc = result.get("process")
        # key 优先用中文列名（去掉单位括号），全英文列名则用 ASCII slug
        base = re.sub(r"[（(][^）()]*[）)]\s*$", "", str(c)).strip()
        slug = base if base and not base.isascii() else (_pinyin_slug(c) or "")

        # 先排除"其实只是别名"的情况。
        # 相关性高 ≠ 同一个量（固含与面密度常常同步变化），所以这里设双重闸门：
        #   1) 相关性极高（默认 0.985，几乎同一条序列）；2) 列名带出的单位一致。
        # 任一不满足就退回"新参数"候选，交人工判断。
        alias_of = None
        u_new = _guess_unit(c)
        for known_col, key in col_map.items():
            if not key:
                continue
            if known_col == c:
                continue
            r2 = _pearson_cols(table, c, known_col)
            if r2 is None or abs(r2) < alias_min:
                continue
            u_old = _guess_unit(known_col)
            if not u_old or u_old != u_new:
                continue
            alias_of = (key, known_col, r2)
            break
        if alias_of:
            key, known_col, r2 = alias_of
            out.append({
                "_type": "param_alias",
                "_reason": "列「%s」与已识别的「%s」（%s）相关系数 %.3f，"
                           "判定为同一量的不同写法" % (c, known_col, key, r2),
                "payload": {"key": key, "aliases": [c]}})
            continue

        out.append({
            "_type": "param",
            "_reason": "与目标列「%s」关联度 %.2f，但知识库无匹配列，"
                       "该导出字段此前无法被自动识别" % (result.get("target") or "目标列", hit),
            "payload": {
                "key": slug or ("param_%s" % _stamp()),
                "name": c, "process": proc or "", "unit": _guess_unit(c),
                "role": role,
                "aliases": [c],
                "note": "由分析任务自动发现（%s），待工艺工程师确认工序归属、单位与 "
                        "control/response 角色。实测 CV=%.1f%%，与目标列关联 %.2f。"
                        % (result.get("target") or "目标列", cv * 100, hit),
                "source_tier": "inferred",
            }})

    # 二、机理映射补充：本次数据显示强关联，但知识库没把该参数写进这个缺陷的可能成因
    dname = result.get("defect")
    dnode = defects.get(dname) if dname else None
    if dnode:
        have = set(dnode.get("likely_params") or [])
        rev = {}
        for orig, key in col_map.items():
            if key:
                rev.setdefault(key, orig)
        missing = []
        for col, eff in effect.items():
            key = col_map.get(col)
            if not key or key in have:
                continue
            if abs(eff) >= max(0.60, min_corr):
                missing.append((col, key, eff))
        if missing:
            missing.sort(key=lambda x: -abs(x[2]))
            out.append({
                "_type": "defect_map",
                "_reason": "缺陷「%s」在本批数据中与知识库未登记的参数强关联" % dname,
                "payload": {
                    "defect": dname,
                    "added_likely_params": [k for _, k, _ in missing[:4]],
                    "evidence": "本次分析：%s；工序=%s，样本 %s 行"
                                % ("；".join("%s(r=%.2f)" % (c, e) for c, _, e in missing[:4]),
                                   result.get("process_name") or "",
                                   (result.get("shape") or [0])[0]),
                }})
    return out


def _pearson_cols(table, a, b):
    """两列相关系数（等长配对，缺失跳过）"""
    try:
        from . import stats as S
    except Exception:
        import stats as S
    va, vb = table.numeric_column(a), table.numeric_column(b)
    xs, ys = [], []
    for x, y in zip(va, vb):
        if x is not None and y is not None:
            xs.append(x)
            ys.append(y)
    if len(xs) < 20:
        return None
    try:
        r, _p, _n = S.pearson(xs, ys)
    except Exception:
        return None
    return r


def _guess_unit(name):
    n = str(name or "")
    m = re.search(r"[（(]([^）()]+)[）)]\s*$", n)
    if m:
        return m.group(1)
    table = [("%", "%"), ("℃", "℃"), ("mm/s", "mm/s"), ("μm", "μm"), ("um", "μm"),
             ("m/min", "m/min"), ("rpm", "rpm"), ("MPa", "MPa"), ("N", "N"),
             ("mΩ", "mΩ"), ("Ah", "Ah"), ("mAh", "mAh"), ("V", "V"), ("A", "A"),
             ("℃/min", "℃/min"), ("g/m2", "g/m²"), ("g/m²", "g/m²")]
    for k, v in table:
        if k in n:
            return v
    return ""


def _looks_response(name):
    n = str(name or "").lower()
    for kw in ("不良", "缺陷", "率", "ng", "yield", "良率", "count", "数", "数量"):
        if kw in n:
            return True
    return False


def _pinyin_slug(name):
    """无第三方拼音库，退化为保留 ASCII 字符 + 序号，避免乱码 key"""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name or "")).strip("_").lower()
    if s and not s[0].isdigit():
        return s[:40]
    return ""


# ------------------------------------------------------------------ 查询：历史对策

def find_cases(defect=None, param_key=None, level=None, only_recommendable=True):
    cases = _read_json(CASE_FILE, {"cases": []}).get("cases", [])
    out = []
    for c in cases:
        # 演示条目永不可被推荐。这是硬闸门，不依赖 JSON 里的字段是否写错：
        # 公开仓库随附的案例是结构示例，它的数值是占位值，
        # 一旦参与推荐，等于把占位值当成现场验证过的工艺对策推给别的工厂。
        if c.get("demo"):
            continue
        if only_recommendable and not c.get("can_recommend", True):
            continue
        if defect and defect not in (c.get("defect"), c.get("symptom")):
            continue
        if param_key and param_key != c.get("root_cause_param") \
                and param_key not in (c.get("related_params") or []):
            continue
        if level and c.get("evidence_level") != level:
            continue
        score = {"A": 3, "B": 2, "C": 1}.get(c.get("evidence_level"), 0)
        out.append((score, c))
    out.sort(key=lambda x: -x[0])
    return [c for _, c in out]


def recommend_prior(defect, param_key):
    """供 rootcause 调用：历史已验证对策对某个候选根因的先验加成"""
    cs = find_cases(defect=defect, param_key=param_key)
    if not cs:
        return 0.0, None
    top = cs[0]
    bonus = {"A": 0.12, "B": 0.06}.get(top.get("evidence_level"), 0.0)
    return bonus, top
