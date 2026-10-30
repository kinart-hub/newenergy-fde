# -*- coding: utf-8 -*-
"""
hotswap.py —— 基线热更新的两道保险

云端部署后，知识库基线要能"边跑边换"：更新时企业侧照常用，更新完自动看到新版。
实测发现两个会真出事的点，本模块就是为它们准备的。

【问题一】Windows 上 os.replace 会被并发读打断
    实测：有进程正在读目标文件时，os.replace 失败率约 33%（WinError 5 拒绝访问）。
    Linux 的 rename 是真原子操作，不受影响——所以这是 Windows 部署独有的坑，
    本地跑得好好的，一上 Windows Server 多进程就开始随机失败。
    对策：退避重试。失败只是"这一瞬间有人在读"，等一下再换即可。

【问题二】长驻进程看不到更新
    云端 Web 服务是长驻进程，知识库在内存里有缓存。基线文件换了，
    进程里那份缓存还是旧的——企业会一直用旧知识库，直到重启。
    实测：基线 rev 从 6 改到 9999，同一进程再读仍是 6。
    对策：缓存带上文件指纹（mtime_ns + size），变更即失效。
    为免 stat 拖慢分析，默认 1 秒内只检查一次。

【不做的事】多文件事务
    一次 sync 会写正库 + CHANGELOG + index 三个文件，中间状态可能被读到。
    刻意不做分布式事务：正库先写、附属文件后写，所以最坏情况是
    index 版本落后于正库（无害），不会出现"index 声称新版但正库还是旧的"。
"""

try:
    from . import degrade          # 包模式
except Exception:                  # 脚本模式（python lib/pipeline.py）无包上下文
    import degrade                 # noqa
import io
import os
import threading
import uuid
import json
import time

_STAMPS = {}          # key -> 上次记录的文件指纹
_LAST_CHECK = {}      # key -> 上次 stat 的时间戳

# 指纹检查最小间隔（秒）。stat 单次约 10μs，但 match_param 每列都过一遍，
# 宽表上累积起来不是零成本，所以做节流。1 秒对"周更/日更"的基线绰绰有余。
MIN_CHECK_INTERVAL = 1.0


# --------------------------------------------------------------- 原子写

def read_text_retry(path, encoding="utf-8", retries=20, base_delay=0.01,
                    max_delay=0.05):
    """读文件，并容忍 Windows 上 `os.replace` 交换瞬间的瞬时读拒绝。

    背景（实测结论，见 scripts/prepublish_audit.py 之外的并发压测）：
    另一个进程做 tmp + os.replace 更新同一个文件时，Windows 会在极短的窗口里
    拒绝新的读打开（Errno 13 PermissionError），实测约 1.2%（4005 次读中 7 次）。
    **这不是数据损坏——同一轮压测里抓到的失败快照全部是完整合法的 JSON，零次撕裂写入。**

    为什么必须在这里重试而不是让上层兜：
    `kbstore._read_json` 原本用裸 `except Exception` 兜住一切，
    于是 PermissionError 会被当成"这文件坏了"，**静默返回空知识库**——
    报告照出、结论全错、还没有任何报错。这是本项目最不该发生的一类失效。

    重试预算为什么从 8 次提到 20 次（2026-10-30 实测改）：
    原预算是 0.01+0.02+…+0.08 ≈ 0.36 秒。单跑自检从不失败，但在
    **门禁全量跑（多子进程 + 磁盘饱和）** 时，1.5 秒的并发压测里偶发把预算耗光，
    于是 `atomic_write` 侧被判失败、门禁变红——同一份代码单跑三次全过。
    门禁时红时绿本身就是缺陷：它让人开始怀疑门禁、进而忽略它。
    等间隔增长改为**带上限的线性增长**（每次最多 0.05s），总预算约 0.9 秒，
    既覆盖了高负载下的 replace 窗口，也不会在真缺文件时长时间空转
    （ENOENT 不重试，见下）。
    """
    last = None
    for i in range(retries):
        try:
            with io.open(path, "r", encoding=encoding) as f:
                return f.read()
        except OSError as e:
            # 只重试"大概率只是撞上 replace 窗口"的 errno；
            # 真正的 ENOENT（文件不存在）交给调用方按缺文件处理，不必徒劳重试。
            if e.errno != 13:
                raise
            last = e
            time.sleep(min(max_delay, base_delay * (i + 1)))
    raise last


def atomic_write_text(path, text, retries=40, base_delay=0.01, max_delay=0.05):
    """
    写入文本：tmp + os.replace，Windows 上 replace 失败则退避重试。

    重试是必需的不是保守：实测 Windows 并发读场景下 replace 失败率约 1/3，
    不重试的话基线更新会随机失败，而调用方往往以为写成功了。

    两个实测教训（2026-10-15 CI 上暴露，此前只在本地跑所以没发现）：
    1. 线性退避 0.02*(i+1) 到最后几次会睡 0.24s，总耗时 1.5s+，
       在慢机器上反而撑不过测试窗口。这里改成**封顶 + 抖动**：
       单次最多睡 0.05s，20 次总耗时约 0.9s，覆盖更密。
    2. 抖动不可省：两个进程同时写同一文件时，固定步长的退避会让它们
       **同步重试、反复对撞**（惊群），加了抖动才能错开。

    3. 重试次数 20 → 40（2026-10-30）。20 次的预算约 1 秒，单跑自检从不失败，
       但在门禁全量跑（磁盘已饱和）时偶发耗尽，于是"并发原子写"这条断言
       时红时绿。写不进去是**容量**问题不是正确性问题，但门禁不确定本身就是缺陷，
       所以宁可多留一倍预算。单次最长仍 0.05s，总预算约 2 秒。
    """
    import random
    # 写之前先过一遍租户写入闸门：这里是最底层的一次真正的落盘，
    # 上面任何一层忘了检查，都会在这里被拦住。
    # 之所以放在循环中之外：这是越界与否的判断，跟并发冲突无关，只核一次即可。
    try:
        import tenancy as _TN
    except Exception:
        try:
            from . import tenancy as _TN
        except Exception:
            _TN = None
    if _TN is not None and _TN.enabled():
        _ok, _why, _real = _TN.guard_write_path(path)
        if not _ok:
            raise IOError(_why)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    key = os.path.abspath(path)
    last = None
    # 同一目标路径的写入必须串行。
    # 这一段不是"加把锁求稳"，是修一个实测出来的撕裂缺陷：
    # 临时名原先是 `<path>.tmp-<pid>`，**同进程多线程共用同一个 PID**，
    # 于是线程 A 刚写到一半，线程 B 就把那个半成品 replace 成正品——
    # 实测 8 线程 × 25 次写入，读到 70 次撕裂内容、6/8 写线程失败。
    # 唯一临时名能让两人互不干扰，但这还不够：两人同时写、最后 replace 的次序
    # 仍不可控，所以还需要串行化来保证"最后一次写入者赢"。
    lock = _path_lock(key)
    with lock:
        # pid + 线程 id + 随机数三重唯一：pid 挡跨进程，tid 挡跨线程，
        # 随机数挡"同一线程重入且 tid 相同"的边角。缺任一重都可能对撞。
        tmp = "%s.tmp-%d-%d-%s" % (path, os.getpid(), threading.get_ident(),
                                   uuid.uuid4().hex[:8])
        for i in range(retries):
            try:
                with io.open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, path)
                _bump(path)
                return path
            except OSError as e:
                last = e
                # 只重试"只是撞上共享冲突"的情况：Windows errno 13 / WinError 5。
                # 磁盘满、路径非法这类重试多少次都不会好，尽早把真错误抛给调用方，
                # 免得把 ENOSPC 之类伪装成"并发冲突"拖上 20 次再报。
                if e.errno not in (13, 5) and e.errno is not None:
                    _cleanup(tmp)
                    raise
                # tmp 可能已生成但没换上，下一次重试会覆盖它
                time.sleep(min(base_delay * (i + 1), max_delay) * (0.5 + random.random()))
        _cleanup(tmp)
    raise IOError("原子写失败（已重试 %d 次）：%s —— %s" % (retries, path, last))


# 按目标路径分档的进程内写锁。全局字典本身也要一把锁，
# 否则两个线程首次遇到同一路径会造出两把不同的锁，等于没锁。
_WRITE_LOCKS = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _path_lock(key):
    with _WRITE_LOCKS_GUARD:
        lk = _WRITE_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _WRITE_LOCKS[key] = lk
        return lk


def _cleanup(tmp):
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception as _dg_e:
        degrade.note("hotswap._cleanup@L116", _dg_e)


def atomic_write_json(path, data, retries=None, base_delay=None):
    """JSON 落盘。重试预算**跟随 atomic_write_text 的默认值**，不另立一套。

    历史坑（2026-10-30 实测抓到）：这里原本硬写 `retries=12, base_delay=0.02`，
    而 `atomic_write_text` 的默认是 20。于是：
      · 调 `atomic_write_text` 的地方有 20 次预算，
      · 调 `atomic_write_json` 的地方只有 12 次（约 0.4 秒）。
    磁盘饱和时 JSON 那条路先耗尽 → 报"原子写失败（已重试 12 次）"，
    而并发自检正是走 JSON 这条路过 → 门禁时红时绿，单跑三四次才复现一次。
    只改 text 的默认值是没用的：JSON 把自己的默认值传下去，把上层预算覆盖掉了。

    所以这里传 None 表示"用下层默认"，两处预算从此只有一个真源。
    """
    kw = {}
    if retries is not None:
        kw["retries"] = retries
    if base_delay is not None:
        kw["base_delay"] = base_delay
    return atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2),
                             **kw)


def _bump(path):
    """自己写完立刻更新指纹，免得下一次读把自己的写入判成'外部变更'"""
    for key, val in _STAMPS.items():
        if path in val[0]:
            _STAMPS.pop(key, None)


# --------------------------------------------------------------- 变更检测

def fingerprint(paths):
    """一组文件的指纹：(mtime_ns, size)。文件不存在记 None。"""
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def changed(key, paths, min_interval=MIN_CHECK_INTERVAL):
    """
    这组文件自上次检查以来是否变过。
    未到检查间隔时直接返回 False（不 stat），把开销压到可忽略。
    """
    if not paths:
        return False
    now = time.time()
    if now - _LAST_CHECK.get(key, 0.0) < min_interval:
        return False
    _LAST_CHECK[key] = now
    cur = fingerprint(paths)
    if _STAMPS.get(key) == cur:
        return False
    _STAMPS[key] = cur
    return True


def mark(key, paths):
    """记录当前指纹（缓存刚建好时调用）"""
    _STAMPS[key] = fingerprint(paths)
    _LAST_CHECK[key] = time.time()


def forget(key=None):
    if key is None:
        _STAMPS.clear()
        _LAST_CHECK.clear()
    else:
        _STAMPS.pop(key, None)
        _LAST_CHECK.pop(key, None)
