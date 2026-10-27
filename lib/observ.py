# -*- coding: utf-8 -*-
"""
observ.py —— 结构化日志与可观测性（P2-4）

过去这套工具跑完就什么都不留：慢在哪一步不知道、哪一步在重试不知道、
失败是数据的问题还是配置还是模块缺陷不知道、模块成功率没有、
输入质量没有度量、日志里带着使用者的目录名和手机号就落盘了。

这一段补齐七件事：结构化事件日志、耗时、重试、错误分类、模块成功率、
输入质量指标、资源上限与日志脱敏。

## 两条硬约束

1. **observ 自身永不把主流程搞崩**。它是诊断通道，诊断通道崩掉主流程不可接受。
   自己出错时记进 `_SELF_ERRORS` 并在 summary 里如实报出——不静默，
   静默会让"可观测性"本身变成不可信。
2. **不反向依赖 degrade**。degrade 在 note() 里惰性 import 本模块，
   若本模块再 import degrade 就成环。所以这里只依赖标准库。

## 日志落盘

默认写到 `output/_observ.jsonl`（每行一个 JSON 事件），可用 set_log_path 改。
写不进去（目录只读/文件被占用）只计数，不影响分析。
"""

import io
import os
import re
import json
import time
import threading

_LOCK = threading.Lock()

# 资源上限：超过它不是"报错"，是"该停下来问一句"——
# 病态输入下继续跑，跑出来的东西既慢又不可信
RESOURCE_LIMITS = {
    "max_rows": 2000000,       # 行数
    "max_cols": 2000,          # 列数
    "max_file_mb": 200,        # 单个输入文件 MB
    "max_seconds": 900,        # 单轮分析墙钟秒
}

# 错误分类：分类的意义在于"该找谁"——
# data 找数据、config 找配置、io 找环境、module 找我们自己的 bug
ERROR_CLASSES = ("data", "config", "io", "resource", "module", "unknown")

_CLASSIFY_RULES = (
    ("data", (ValueError, TypeError, ArithmeticError, ZeroDivisionError,
              IndexError, KeyError)),
    ("io", (OSError, IOError, FileNotFoundError, PermissionError)),
    ("config", (ImportError, AttributeError, NotImplementedError)),
    ("resource", (MemoryError, RecursionError, TimeoutError)),
)
# 只看异常类型不够：同样是 ValueError，"读不到文件"是 io，"列不是数字"是 data。
# 消息关键词做二次裁决，且 io 优先于 data——文件被占用的报错必须是 io，
# 否则使用者会去改数据，而真正原因是文件被 Excel 打开着。
_MSG_HINTS = (
    ("io", ("被占用", "permission denied", "no such file", "not found",
            "errno", "broken pipe", "磁盘", "disk")),
    ("config", ("未配置", "未授权", "unauthorized", "unlicensed",
                "license", "missing key", "缺配置")),
    ("resource", ("memory", "内存", "timeout", "超时", "too large", "过大")),
)

_MASK_RULES = (
    # 手机号
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "1**********"),
    # 邮箱
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "***@***"),
    # 身份证（18 位，末位可为 X）
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "******************"),
    # Windows 用户目录 / 类 Unix home：路径里最常泄漏真实姓名
    (re.compile(r"(?i)(c:\\users\\|/home/|/users/)[^\\\/\s]+"), r"\1<user>"),
    # 密钥状长串（>=24 位十六进制或 base64-ish）
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "<token>"),
)

_DEFAULT_LOG = os.path.join("output", "_observ.jsonl")

_lock = threading.Lock()
_EVENTS = []            # 进程内事件（供 summary 使用）
_MODULE = {}            # module -> {"calls","ok","fail","ms","retries"}
_ERROR_CLASSES_SEEN = {}
_SELF_ERRORS = []       # observ 自己的错，必须暴露，不许静默
_LOG_PATH = _DEFAULT_LOG
_RUN_START = None
_MAX_EVENTS = 2000      # 上限：病态输入下不能让日志吃满内存


# ------------------------------------------------------------------ 基础设施

def set_log_path(path):
    global _LOG_PATH
    _LOG_PATH = path or _DEFAULT_LOG
    return _LOG_PATH


def log_path():
    return _LOG_PATH


def mask(text):
    """日志脱敏：落盘前必须过一遍。

    要脱的不只是"密码"：路径里的用户名、手机号、邮箱、身份证、令牌状长串，
    都是把日志发给别人求助时最容易泄漏的东西。
    """
    if text is None:
        return None
    s = str(text)
    for pat, rep in _MASK_RULES:
        try:
            s = pat.sub(rep, s)
        except Exception as _e:
            _self_error("mask", _e)
    return s


def _self_error(where, exc):
    """observ 自己出错：记下来并在 summary 里报出。静默比出错更坏。"""
    try:
        if len(_SELF_ERRORS) >= 50:
            return
        _SELF_ERRORS.append({"where": where,
                             "kind": type(exc).__name__,
                             "msg": str(exc)[:200]})
    except Exception:
        pass
        # scan:keep 这是记录通道的最后一层（记"记不下来"这件事本身失败），
        # 再抛会递归回自己。走到这里说明连 _SELF_ERRORS 都写不进去了，
        # 除放弃外无路可走；不影响主流程。启用条件：正常路径不会到达。


def classify_error(exc):
    """把异常归到 data / config / io / resource / module / unknown。

    先看消息关键词（io 优先），再看异常类型。这样"文件被占用"不会被当成数据问题。
    """
    try:
        if isinstance(exc, str):
            msg, tp = exc, None
        else:
            msg, tp = str(exc), type(exc)
        low = (msg or "").lower()
        for cls, hints in _MSG_HINTS:
            for h in hints:
                if h.lower() in low:
                    return cls
        if tp is not None:
            for cls, types in _CLASSIFY_RULES:
                if issubclass(tp, types):
                    return cls
            return "module"
        return "unknown"
    except Exception as _e:
        _self_error("classify_error", _e)
        return "unknown"


# ------------------------------------------------------------------ 事件记录

def event(module, name, level="INFO", **fields):
    """记一条结构化事件。永不抛异常。"""
    try:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "level": level, "module": str(module), "event": str(name)}
        for k, v in fields.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                rec[k] = mask(v) if isinstance(v, str) else v
            else:
                rec[k] = mask(str(v))
        with _lock:
            if len(_EVENTS) < _MAX_EVENTS:
                _EVENTS.append(rec)
        _append_log(rec)
        return rec
    except Exception as _e:
        _self_error("event", _e)
        return None


def _append_log(rec):
    """追加一行 JSONL。写不进去只计数——日志不是主流程。"""
    try:
        d = os.path.dirname(_LOG_PATH)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with _LOCK:
            with io.open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as _e:
        _self_error("append_log", _e)


def _bump(module, ok=True, ms=None, retries=0, err=None):
    try:
        m = str(module or "unknown")
        with _lock:
            st = _MODULE.get(m)
            if st is None:
                st = _MODULE[m] = {"calls": 0, "ok": 0, "fail": 0,
                                   "ms": 0.0, "retries": 0}
            st["calls"] += 1
            st["ok" if ok else "fail"] += 1
            if ms:
                st["ms"] += float(ms)
            st["retries"] += int(retries or 0)
            if not ok:
                cls = classify_error(err if err is not None else "")
                st.setdefault("error_classes", {})
                st["error_classes"][cls] = st["error_classes"].get(cls, 0) + 1
                _ERROR_CLASSES_SEEN[cls] = _ERROR_CLASSES_SEEN.get(cls, 0) + 1
    except Exception as _e:
        _self_error("_bump", _e)


def ok(module, ms=None, retries=0, **fields):
    _bump(module, True, ms, retries)
    return event(module, "ok", level="INFO",
                 duration_ms=(round(ms, 1) if ms else None),
                 retries=retries, **fields)


def fail(module, err=None, ms=None, **fields):
    _bump(module, False, ms, 0, err)
    return event(module, "fail", level="ERROR",
                 error_class=classify_error(err if err is not None else ""),
                 error=type(err).__name__ if not isinstance(err, str) else "str",
                 message=mask(str(err))[:200] if err is not None else "",
                 **fields)


class Timer(object):
    """with Timer("spc") as t: ... —— 出块自动记耗时与成败。

    异常穿出时记为 fail 并分类，然后照常向上抛（不吞异常）：
    可观测性只负责看见，不负责改变控制流。
    """

    def __init__(self, module, name="run", **fields):
        self.module = module
        self.name = name
        self.fields = fields
        self.ms = None
        self._t0 = None

    def __enter__(self):
        self._t0 = time.time()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.ms = (time.time() - self._t0) * 1000.0
        if exc is None:
            ok(self.module, self.ms, **self.fields)
        else:
            fail(self.module, exc, self.ms, **self.fields)
        return False        # 不吞异常


def timed(module, name="run"):
    """装饰器版计时。被包装函数抛异常时记 fail 后原样抛出。

    # scan:keep 当前用 Timer 上下文管理器做埋点（更省改动、不重排缩进）；
    # 装饰器留给"整函数都要计时"的场景——给某个模块函数加一行 @timed 即可。
    # 启用条件：需要给 lib 层某个函数整体计时时用它，不要为了消灭本条而删。
    """
    def _wrap(fn):
        def _inner(*a, **kw):
            with Timer(module, name):
                return fn(*a, **kw)
        _inner.__name__ = getattr(fn, "__name__", "wrapped")
        return _inner
    return _wrap


def retry(fn, attempts=3, base_delay=0.05, module=None, name="call"):
    """带记录的重试：返回 (ok, value, used_attempts)。

    重试只对"瞬时故障"有意义（文件被占用、网络抖动）。逻辑错误重试三次还是错，
    所以重试次数本身要被看见——一直靠重试跑通的地方，等于一直带病运行。
    """
    used = 0
    last = None
    for i in range(max(1, int(attempts or 1))):
        used = i + 1
        try:
            v = fn()
            _bump(module or name, True, None, retries=i)
            if i:
                event(module or name, "retry_ok", level="WARN",
                      attempts=used, detail="第 %d 次才成功" % used)
            return (True, v, used)
        except Exception as _e:
            last = _e
            if i + 1 < max(1, int(attempts or 1)):
                try:
                    time.sleep(base_delay * (2 ** i))
                except Exception as _s:
                    _self_error("retry.sleep", _s)
    _bump(module or name, False, None, retries=used - 1, err=last)
    event(module or name, "retry_exhausted", level="ERROR",
          attempts=used, message=mask(str(last))[:200])
    return (False, None, used)


# ------------------------------------------------------------------ 输入质量与资源

def record_input_quality(source=None, rows=None, cols=None, missing_rate=None,
                         numeric_rate=None, duplicate_rate=None):
    """输入质量指标。没有它，"结论不可信"到底是因为数据还是因为算法，说不清。"""
    return event("input_quality", "quality", source=mask(source) if source else None,
                 rows=rows, cols=cols,
                 missing_rate=(round(missing_rate, 4) if missing_rate is not None else None),
                 numeric_rate=(round(numeric_rate, 4) if numeric_rate is not None else None),
                 duplicate_rate=(round(duplicate_rate, 4)
                                 if duplicate_rate is not None else None))


def check_resources(rows=None, cols=None, file_mb=None, seconds=None,
                    limits=None):
    """资源上限检查：返回 {"ok", "breaches": [...]}。超限不自动截断——
    截断会悄悄改变结论，交给调用方决定是停还是继续。"""
    lim = dict(RESOURCE_LIMITS)
    if limits:
        lim.update(limits)
    pairs = (("max_rows", rows), ("max_cols", cols),
             ("max_file_mb", file_mb), ("max_seconds", seconds))
    breaches = []
    for key, val in pairs:
        if val is None:
            continue
        try:
            if float(val) > float(lim.get(key)):
                breaches.append({"limit": key, "value": val, "max": lim.get(key)})
        except Exception as _e:
            _self_error("check_resources", _e)
    if breaches:
        event("resource", "limit_breach", level="WARN", breaches=breaches)
    return {"ok": not breaches, "breaches": breaches, "limits": lim}


# ------------------------------------------------------------------ 汇总与复位

def start_run(**fields):
    global _RUN_START
    _RUN_START = time.time()
    return event("run", "start", **fields)


def end_run(**fields):
    ms = None
    if _RUN_START:
        ms = (time.time() - _RUN_START) * 1000.0
    return event("run", "end", duration_ms=(round(ms, 1) if ms else None), **fields)


def summary():
    """可观测性汇总：模块成功率、错误分类分布、耗时、资源越界、自身错误。"""
    try:
        with _lock:
            mods = {}
            for m, st in _MODULE.items():
                calls = st["calls"] or 0
                mods[m] = {
                    "calls": calls, "ok": st["ok"], "fail": st["fail"],
                    "success_rate": (st["ok"] / calls) if calls else None,
                    "ms_total": round(st["ms"], 1),
                    "ms_avg": (round(st["ms"] / calls, 1)) if calls else None,
                    "retries": st["retries"],
                    "error_classes": dict(st.get("error_classes") or {}),
                }
            tot_calls = sum(v["calls"] for v in mods.values())
            tot_fail = sum(v["fail"] for v in mods.values())
            out = {
                "modules": mods,
                "totals": {
                    "calls": tot_calls, "fail": tot_fail,
                    "success_rate": ((tot_calls - tot_fail) / tot_calls)
                    if tot_calls else None,
                    "events": len(_EVENTS),
                },
                "error_classes": dict(_ERROR_CLASSES_SEEN),
                "slowest_ms": sorted(
                    [{"module": m, "ms_total": v["ms_total"]}
                     for m, v in mods.items()],
                    key=lambda d: -d["ms_total"])[:5],
                "self_errors": list(_SELF_ERRORS),
                "log_path": _LOG_PATH,
            }
        if out["self_errors"]:
            # 诊断通道自己带病是可观测性的最大风险：它让人以为"查过了"
            event("observ", "self_error", level="ERROR",
                  n=len(out["self_errors"]))
        return out
    except Exception as _e:
        _self_error("summary", _e)
        return {"modules": {}, "totals": {}, "error_classes": {},
                "self_errors": list(_SELF_ERRORS), "log_path": _LOG_PATH}


def reset():
    """复位进程内状态（自检用，避免用例之间串台）。"""
    global _RUN_START
    with _lock:
        _EVENTS[:] = []
        _MODULE.clear()
        _ERROR_CLASSES_SEEN.clear()
        _SELF_ERRORS[:] = []
        _RUN_START = None
    return True


# ------------------------------------------------------------------ 日志回读

def read_log(path=None, limit=500):
    """回读 JSONL 日志（供 ops 流程做事后分析）。读不到返回空列表并计数。"""
    p = path or _LOG_PATH
    out = []
    try:
        if not os.path.exists(p):
            return out
        with io.open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
                    # scan:keep 坏行跳过属正常路径：进程被杀/写到一半会在末尾留下半行，
                    # 那不是"读取失败"，不该被记成可观测性自身出错（会把 self_errors 刷脏）。
    except Exception as _e:
        _self_error("read_log", _e)
    return out[-int(limit or 0):] if limit else out


def summarize_log(events):
    """把日志事件汇总成"事后能回答的问题"：哪一步最慢、哪一步最常失败、
    失败主要是哪一类、有没有靠重试撑着。"""
    mods = {}
    cls = {}
    breaches = 0
    for e in events or []:
        m = e.get("module") or "unknown"
        st = mods.setdefault(m, {"calls": 0, "fail": 0, "retries": 0, "ms": 0.0})
        ev = e.get("event")
        if ev == "ok":
            st["calls"] += 1
        elif ev in ("fail", "retry_exhausted"):
            st["calls"] += 1
            st["fail"] += 1
        elif ev == "retry_ok":
            st["retries"] += 1
        if ev == "limit_breach":
            breaches += 1
        _d = e.get("duration_ms")
        if isinstance(_d, (int, float)):      # 类型不对就当没记时，不用异常吞
            st["ms"] += float(_d)
        c = e.get("error_class")
        if c:
            cls[c] = cls.get(c, 0) + 1
    for st in mods.values():
        st["success_rate"] = ((st["calls"] - st["fail"]) / st["calls"]) \
            if st["calls"] else None
        st["ms"] = round(st["ms"], 1)
    return {"modules": mods, "error_classes": cls, "limit_breaches": breaches,
            "n_events": len(events or [])}
