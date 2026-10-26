# -*- coding: utf-8 -*-
"""
tenancy.py —— 多企业用户隔离层

云端部署时最危险的不是算错，而是**一家企业的改动污染另一家**。
本模块把知识库切成两层：

  基线层  <root>/knowledge/            共享、只读（部署时建议只读挂载）
  租户层  <root>/tenants/<tid>/knowledge/   私有、可写，**只存差异**

读取时两层合并（租户层覆盖基线层），因此：
  · 基线升级自动流入所有租户，不需要逐家迁移
  · 租户的任何写入都不触碰基线，也不可能碰到别的租户
  · 租户层随时可删，删掉即回到基线，不会留下半个坏库

没有 tenant id 时完全不介入（保持单库行为），老用法不受影响。
"""

try:
    from . import degrade          # 包模式
except Exception:                  # 脚本模式（python lib/pipeline.py）无包上下文
    import degrade                 # noqa
import io
import json
import os
import time
import errno
import contextlib
import threading

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TENANT = None                      # 当前租户 id；None = 基线模式（不隔离）
_TENANT_DIRS = {}                   # tid -> {"root", "kb", "lock", "promotion"}


def root():
    return SKILL_ROOT


def set_root(path):
    """切换技能包根目录（自检隔离用），会一并清掉当前租户上下文"""
    global SKILL_ROOT, _TENANT
    SKILL_ROOT = path
    _TENANT = None
    # 设备档案路径是模块级常量，不跟着切就会写回真实基线
    try:
        from . import devices as _D
        _D.set_root(path)
    except Exception as _dg_e:
        degrade.note("tenancy.set_root@L47", _dg_e)
    return SKILL_ROOT


def current():
    return _TENANT


def enabled():
    return _TENANT is not None


def tenant_root(tid=None):
    tid = tid or _TENANT
    return os.path.join(SKILL_ROOT, "tenants", tid) if tid else None


def tenant_kb(tid=None):
    r = tenant_root(tid)
    return os.path.join(r, "knowledge") if r else None


def promotion_dir(tid=None):
    """租户想把成果提给基线时的投递区（不写基线，只投候选）"""
    return os.path.join(SKILL_ROOT, "promotion")


def set_tenant(tid):
    """
    切换到指定租户。会建好目录骨架，但**不复制基线文件**——
    复制会让租户层变成快照，基线一升级租户就落后，正是要避免的。
    """
    global _TENANT
    tid = (tid or "").strip()
    if not tid:
        _TENANT = None
        return None
    safe = _safe_id(tid)
    tr = os.path.join(SKILL_ROOT, "tenants", safe)
    kb = os.path.join(tr, "knowledge")
    for d in (tr, kb, os.path.join(kb, "backups"), promotion_dir()):
        os.makedirs(d, exist_ok=True)
    _TENANT_DIRS[safe] = {"root": tr, "kb": kb,
                          "lock": os.path.join(tr, ".lock")}
    _TENANT = safe
    return _TENANT_DIRS[safe]


def _safe_id(tid):
    """租户 id 会进路径，必须挡住 ../ 与分隔符——否则就是目录穿越"""
    out = []
    for ch in str(tid):
        out.append(ch if (ch.isalnum() or ch in "-_.") else "_")
    s = "".join(out).strip(". ")
    return s or "default"


# --------------------------------------------------------------- 租户配置

def config_path(tid=None):
    r = tenant_root(tid)
    return os.path.join(r, "tenant.json") if r else None


def read_config(tid=None):
    """
    租户配置（企业买了哪些行业模块、显示名等）。
    读不到就返回空字典——配置是可选的，没有就按"不限行业"处理。
    """
    p = config_path(tid)
    if not p:
        return {}
    obj = _read(p, None)
    return obj if isinstance(obj, dict) else {}


def write_config(patch, tid=None):
    """合并式写入租户配置（原子写，不会写坏）"""
    p = config_path(tid)
    if not p:
        raise RuntimeError("未启用租户，无法写租户配置")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    cur = read_config(tid)
    cur = deep_merge(cur, patch or {})
    cur["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = "%s.tmp-%d" % (p, os.getpid())
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return cur


def list_tenants():
    base = os.path.join(SKILL_ROOT, "tenants")
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base)
                  if os.path.isdir(os.path.join(base, d)) and not d.startswith("."))


# --------------------------------------------------------------- 合并规则

def deep_merge(base, over):
    """递归合并：over 覆盖 base。列表按条目整体替换（见 merge_by_id 处理具名列表）"""
    if not isinstance(base, dict) or not isinstance(over, dict):
        return copy_value(over) if over is not None else copy_value(base)
    out = {}
    for k, v in base.items():
        out[k] = copy_value(v)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy_value(v)
    return out


def copy_value(v):
    if isinstance(v, dict):
        return {k: copy_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [copy_value(x) for x in v]
    return v


_ID_FIELDS = ("id", "key", "case_id", "name")


def merge_by_id(base_list, over_list):
    """具名列表合并（改善案例、判读条目等）：同 id 覆盖，其余追加"""
    if not isinstance(base_list, list):
        return copy_value(over_list)
    out = [copy_value(x) for x in base_list]
    index = {}
    for i, item in enumerate(out):
        if isinstance(item, dict):
            for f in _ID_FIELDS:
                if item.get(f) is not None:
                    index[str(item[f])] = i
                    break
    for item in (over_list or []):
        if not isinstance(item, dict):
            out.append(copy_value(item))
            continue
        hit = None
        for f in _ID_FIELDS:
            if item.get(f) is not None and str(item[f]) in index:
                hit = index[str(item[f])]
                break
        if hit is None:
            out.append(copy_value(item))
        else:
            out[hit] = deep_merge(out[hit], item)
    return out


# 需要按 id 合并的列表段
_LIST_SECTIONS = {"cases": "cases", "entries": "entries", "rules": "rules"}


def merge_file(base_obj, over_obj):
    """按文件语义合并两个知识库 JSON 对象"""
    if over_obj is None:
        return copy_value(base_obj)
    if not isinstance(base_obj, dict) or not isinstance(over_obj, dict):
        return copy_value(over_obj)
    out = {}
    for k, v in base_obj.items():
        out[k] = copy_value(v)
    for k, v in over_obj.items():
        if k in _LIST_SECTIONS and isinstance(v, list):
            out[k] = merge_by_id(out.get(k), v)
        elif isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy_value(v)
    # meta 必须归租户自己，不能沿用基线的版本号
    if "meta" in out and "meta" in (over_obj or {}):
        out["meta"] = deep_merge(base_obj.get("meta", {}), over_obj["meta"])
    return out


# --------------------------------------------------------------- 分层读写

def _read(path, default=None):
    if not os.path.exists(path):
        return None
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else None


def layered_read(base_path, tenant_path, default=None):
    """
    读一个知识库文件：基线 + 租户覆盖。
    租户文件不存在或解析失败时静默退回基线——租户层坏了不该让整家企业的分析停摆。
    """
    base_obj = _read(base_path, default)
    if not enabled() or not tenant_path:
        return base_obj if base_obj is not None else copy_value(default if default is not None else {})
    over = _read(tenant_path, None)
    if over is None:
        return base_obj if base_obj is not None else copy_value(default if default is not None else {})
    if base_obj is None:
        return over
    return merge_file(base_obj, over)


def map_to_tenant(path):
    """把基线路径映射成租户可写路径；未启用隔离时原样返回"""
    if not enabled():
        return path
    kb = tenant_kb()
    base_kb = os.path.join(SKILL_ROOT, "knowledge")
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(base_kb))
    except Exception:
        return path
    if rel.startswith(".."):
        return path
    return os.path.join(kb, rel)


# scan:keep 分层写入：同时写基线与租户层。当前写路径统一走 tenant_kb 单落点，本函数是为「同一条目落到两层」场景预留，启用前需先明确冲突处置规则。
def write_layered(path, data):
    """原子写入（tmp + os.replace + Windows 退避重试），并把目标重定向到租户层"""
    target = map_to_tenant(path)
    try:
        from . import hotswap as HS
    except Exception:
        import hotswap as HS                         # noqa
    HS.atomic_write_json(target, data)
    return target


# --------------------------------------------------------------- 跨进程锁

def _pid_alive(pid):
    """进程是否还活着。跨平台，只用标准库。

    为什么 Windows 上不写 os.kill(pid, 0)：
    Windows 版 os.kill 的实现是 OpenProcess + TerminateProcess，
    传 0 不是"探测一下"而是**真的会去终止那个进程**。
    所以 Windows 走 ctypes 的 OpenProcess，只申请查询权限、绝不动手。

    拿不到确定结论时一律返回 True——宁可多等，也不能抢活人的锁。
    """
    try:
        pid = int(pid)
    except Exception:
        return True
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True          # 存在，只是没权限发信号
        except OSError:
            return True
        return True
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_INVALID_PARAMETER = 87
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            k32.CloseHandle(h)
            return True
        if ctypes.get_last_error() == ERROR_INVALID_PARAMETER:
            return False         # 这个 pid 根本不存在
        return True              # 拒绝访问等其它情况：保守当作活着
    except Exception:
        return True


class _Heartbeat(threading.Thread):
    """持锁期间定期刷新锁目录 mtime。

    没有它，一个跑了 3 分钟的大库同步会被自己的 stale 规则判成"陈旧锁"
    而被后来者抢掉，两个写者同时进场——那正是锁要防的事。
    """
    def __init__(self, path, interval):
        threading.Thread.__init__(self)
        self.daemon = True
        self.path = path
        self.interval = max(1.0, float(interval))
        self._stop = threading.Event()

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                os.utime(self.path, None)
            except Exception:
                return

    def stop(self):
        self._stop.set()


@contextlib.contextmanager
def lock(name="kb", timeout=15.0, stale=180.0):
    """
    基于 os.mkdir 原子性的互斥锁（POSIX 与 NTFS 上 mkdir 都是原子的）。
    拿不到就抛 TimeoutError 而不是死等——云端请求挂着比失败更糟。

    破除陈旧锁必须有两条路，缺一条就会出事：
      · 【快路】owner 文件里记着 pid，那个进程已经死了 → 立即破除。
        产线上 Ctrl+C 中断后立刻重跑是常态，让人白等 15 秒再报错，
        这种"莫名其妙卡一下"最伤信任，比直接失败更糟。
      · 【慢路】mtime 超过 stale → 破除。兜底用，应对 pid 被复用、
        owner 文件没写成功等边界情况。

    心跳（_Heartbeat）保证慢路不会误伤真在跑的长任务。
    """
    base = os.path.join(tenant_root() or SKILL_ROOT, ".lock")
    d = os.path.join(base, _safe_id(name))
    os.makedirs(base, exist_ok=True)
    owner_file = os.path.join(d, "owner")
    deadline = time.time() + timeout
    got = False
    while True:
        try:
            os.mkdir(d)
            got = True
            break
        except OSError as e:
            if e.errno not in (errno.EEXIST, 183, 17):
                raise
            dead_owner = False
            try:
                with io.open(owner_file, encoding="utf-8") as f:
                    dead_owner = not _pid_alive(f.read().strip())
            except Exception:
                dead_owner = False
            try:
                age = time.time() - os.path.getmtime(d)
            except Exception:
                age = 0.0
            if dead_owner or age > stale:
                try:
                    for p in os.listdir(d):
                        try:
                            os.remove(os.path.join(d, p))
                        except Exception as _dg_e:
                            degrade.note("tenancy.lock@L396", _dg_e)
                    os.rmdir(d)
                    continue
                except Exception as _dg_e:
                    degrade.note("tenancy.lock@L400", _dg_e)
            if time.time() >= deadline:
                raise TimeoutError(
                    "获取知识库锁超时（%.0fs），可能正有其它任务在写库。"
                    "若确认没有其它任务在跑，可手动删除 %s 后重试。" % (timeout, d))
            time.sleep(0.05)
    hb = None
    try:
        with io.open(owner_file, "w", encoding="utf-8") as f:
            f.write("%d" % os.getpid())
        os.utime(d, None)
        hb = _Heartbeat(d, stale / 4.0)
        hb.start()
        yield d
    finally:
        if hb is not None:
            hb.stop()
        if got:
            try:
                if os.path.exists(owner_file):
                    os.remove(owner_file)
                os.rmdir(d)
            except Exception as _dg_e:
                degrade.note("tenancy.lock@L423", _dg_e)


# --------------------------------------------------------------- 提升（promote）

# scan:keep 预留通道：租户把已验证知识贡献回共享基线的投递入口。当前对外承诺（见 manifest）只到「沉淀入库」（sync 链路），贡献回基线尚未纳入交付范围，故无调用者。kb_manage.py --promote 读的是同一队列，因此当前恒为空——在启用本通道前，--promote 应如实说明这一点。
def queue_promotion(kind, payload, tenant=None, note=""):
    """
    租户想把自己验证过的知识贡献给基线：投递到共享待审区，
    **不直接写基线**。平台侧 review 通过后才进基线，惠及所有租户。
    """
    tid = tenant or _TENANT or "anonymous"
    os.makedirs(promotion_dir(), exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "tenant": tid,
           "kind": kind, "payload": payload, "note": note, "status": "pending"}
    with io.open(os.path.join(promotion_dir(), "queue.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def list_promotions(status=None):
    p = os.path.join(promotion_dir(), "queue.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    with io.open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if status and r.get("status") != status:
                continue
            out.append(r)
    return out


def reset_tenant(tid=None):
    """清空某个租户的私有层，回到基线。用于"这家企业的库被玩坏了"的一键复原"""
    import shutil
    r = tenant_root(tid)
    if not r or not os.path.isdir(r):
        return False
    shutil.rmtree(r, ignore_errors=True)
    return not os.path.exists(r)
