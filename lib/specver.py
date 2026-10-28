# -*- coding: utf-8 -*-
"""规格版本化：让每条数据按**它当时生效的规格**接受判定。

为什么必须有这一层
------------------
拿今天的尺子量去年的货，会有两种错，而两种都会直接进 8D 报告：

  * 规格收紧过 → 当时完全合格的一批，被判成"不合格"，追查一个根本没发生过的变差；
  * 规格放宽过 → 当时确实超差的一批，被判成"合格"，真因被抹掉。

两种都不是"数字差一点"，是结论方向反了。而 Cpk 一旦用了错版本，
整个过程能力分析、Pareto 排序、根因打分全部跟着错。

口径
----
生效期以 `effective_from` / `effective_to` 表示，`effective_to` 为空表示现行。
判定落在**数据自己的时间点**上：数据里有可解析的时间列时，每一行按它那一刻
生效的版本取规格；没有时间列时**不猜、不默认用最新版**——默认用最新版等于
默认"拿今天的尺子量"，正是要防的那件事。

零第三方依赖。日期解析复用 lib/table.py 的 parse_datetime，
与跨工序对齐用的是同一套口径（同一份数据不能在这里认得出时间、到那里认不出）。
"""

import re

try:
    from lib import table as T, preprocess as PP
except ImportError:                                          # pragma: no cover
    from . import table as T, preprocess as PP


# ---------------------------------------------------------------- 状态
# 与 lib/status.py 的分级保持一致：只有 OK 允许拿去算 Cpk。
# 其余都是"这条规格不能用"，调用方必须原样报出来，不许退回默认值。
OK = "ok"
AMBIGUOUS = "ambiguous"        # 多版覆盖同一时点：谁赢不能由代码猜
GAP = "gap"                    # 落在两版之间的空档：这一段确实没有规格
BEFORE_ALL = "before_all"      # 早于最早一版
AFTER_ALL = "after_all"        # 晚于最后一版（最后一版有 effective_to 才会出现）
UNKNOWN_DATE = "unknown_date"  # 有多版但无从定位该行属于哪一版
NO_SPEC = "no_spec"            # 该参数压根没登记


_DATE_KEYS = ("effective_from", "effective_to", "生效自", "生效至", "valid_from", "valid_to")


def _to_date(v):
    """统一走 parse_datetime，与跨工序对齐同一口径。"""
    if v is None:
        return None
    d = T.parse_datetime(v)
    if d is not None:
        return d
    s = str(v).strip()
    if not s:
        return None
    # 纯日期串（无时间部分）交给 parse_datetime 已能处理；这里只兜 ISO 的 T 分隔写法
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return T.parse_datetime("%s-%s-%s" % (m.group(1), m.group(2), m.group(3)))
    return None


def _pick_date(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            dt = _to_date(d[k])
            if dt is not None:
                return dt
    return None


class SpecRegistry(object):
    """一份规格书（可含多个参数的多个版本）。

    接受三种写法，前两种是版本化格式，第三种是旧的扁平格式（向后兼容）：

      1) {"极片残水": {"versions": [{...}, {...}]}}
      2) {"极片残水": [{...}, {...}]}
      3) {"极片残水": {"lsl": 0, "usl": 300, "target": 150}}

    第 3 种被视为"只有一版、无生效期"，行为与改造前完全一致——
    老配置文件升级后不应该突然变得判不过。
    """

    def __init__(self, raw, col_map=None):
        self.col_map = col_map or {}
        self.raw = raw if isinstance(raw, dict) else {}
        # 归一化名 -> 原始键名（规格书常带单位，列名常不带，柔性匹配是必须的）
        self._by_norm = {}
        self._versions = {}      # 归一化名 -> [版本 dict（日期已解析）]
        self._flat = {}          # 归一化名 -> 单版规格（无生效期）
        for k, v in self.raw.items():
            if not isinstance(k, str):
                continue
            nk = PP._norm_name(k)
            self._by_norm.setdefault(nk, k)
            if isinstance(v, dict) and isinstance(v.get("versions"), list):
                self._versions[nk] = self._parse_versions(v["versions"])
            elif isinstance(v, list):
                self._versions[nk] = self._parse_versions(v)
            elif isinstance(v, dict):
                self._flat[nk] = dict(v)
            elif isinstance(v, (list, tuple)) and len(v) >= 2:
                # [lsl, usl] 这种极简写法
                self._flat[nk] = {"lsl": v[0], "usl": v[1]}

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _parse_versions(items):
        out = []
        for i, v in enumerate(items or []):
            if not isinstance(v, dict):
                continue
            d = dict(v)
            d["_from"] = _pick_date(d, ("effective_from", "生效自", "valid_from"))
            d["_to"] = _pick_date(d, ("effective_to", "生效至", "valid_to"))
            d.setdefault("version", d.get("version") or d.get("版本") or ("V%d" % (i + 1)))
            out.append(d)
        # 按生效起点排序：后面算空档/重叠都依赖有序，顺序错了结论就错
        out.sort(key=lambda x: (x["_from"] is None, x["_from"]))
        return out

    # ------------------------------------------------------------ 查询
    def norm(self, col):
        return PP._norm_name(col)

    def match_key(self, col):
        """列名 -> 规格书里的键（柔性匹配，与 PP.match_spec_key 同口径）。"""
        if not self.raw or not col:
            return None
        nc = PP._norm_name(col)
        if nc in self._by_norm:
            return self._by_norm[nc]
        key = self.col_map.get(col)
        if key and PP._norm_name(key) in self._by_norm:
            return self._by_norm[PP._norm_name(key)]
        return None

    def is_versioned(self, col):
        nk = self.match_key(col)
        return bool(nk) and PP._norm_name(nk) in self._versions

    def versions_of(self, col):
        nk = self.match_key(col)
        if not nk:
            return []
        return list(self._versions.get(PP._norm_name(nk)) or [])

    def resolve(self, col, when=None):
        """取该列在 `when` 时刻生效的规格。

        返回 dict：{"status", "spec", "version", "doc_id", "note", "effective"}。
        只有 status=="ok" 的 spec 允许拿去算 Cpk——其余一律是"这条规格不能用"，
        调用方必须原样报出来，**不许退回默认规格**：退回默认值等于把
        "这一批当时没规格"粉饰成一个看起来正常的能力指数。
        """
        nk = self.match_key(col)
        if not nk:
            return {"status": NO_SPEC, "spec": None, "version": None,
                    "doc_id": None, "note": None, "effective": None}
        n = PP._norm_name(nk)
        if n in self._flat:
            f = self._flat[n]
            return {"status": OK,
                    "spec": {"lsl": f.get("lsl"), "usl": f.get("usl"),
                             "target": f.get("target")},
                    "version": f.get("version"), "doc_id": f.get("doc_id"),
                    "note": None, "effective": None}
        vs = self._versions.get(n) or []
        if not vs:
            return {"status": NO_SPEC, "spec": None, "version": None,
                    "doc_id": None, "note": None, "effective": None}
        if len(vs) == 1 and vs[0]["_from"] is None and vs[0]["_to"] is None:
            # 只登记了一版且没写生效期：无歧义，等同扁平格式
            return {"status": OK,
                    "spec": {"lsl": vs[0].get("lsl"), "usl": vs[0].get("usl"),
                             "target": vs[0].get("target")},
                    "version": vs[0].get("version"), "doc_id": vs[0].get("doc_id"),
                    "note": None, "effective": None}
        if when is None:
            # 有多版却定位不了时间：不猜。默认用最新版就是拿今天的尺子量。
            return {"status": UNKNOWN_DATE, "spec": None, "version": None,
                    "doc_id": None,
                    "note": "该参数登记了 %d 个规格版本，但本行没有可定位的时间，"
                            "无法判断当时生效的是哪一版（不默认取最新版）" % len(vs),
                    "effective": None}
        hits = []
        for v in vs:
            f, t = v["_from"], v["_to"]
            if f is not None and when < f:
                continue
            if t is not None and when > t:
                continue
            hits.append(v)
        if len(hits) > 1:
            return {"status": AMBIGUOUS, "spec": None, "version": None,
                    "doc_id": None,
                    "note": "该时点同时落在 %d 个规格版本内（%s），生效期重叠，"
                            "需人工确认以哪个为准"
                            % (len(hits),
                               "、".join(str(h.get("version")) for h in hits)),
                    "effective": None}
        if hits:
            v = hits[0]
            return {"status": OK,
                    "spec": {"lsl": v.get("lsl"), "usl": v.get("usl"),
                             "target": v.get("target")},
                    "version": v.get("version"), "doc_id": v.get("doc_id"),
                    "note": None,
                    "effective": {"from": v.get("effective_from"),
                                  "to": v.get("effective_to")}}
        # 没命中：分清"早于第一版""晚于最后一版""落在中间空档"，三者处置完全不同。
        # 注意"最后一版"要按**生效起点排序后的最后一版**取，不是"任意一版的最大失效日"
        # ——后者会把"落在 A 失效之后、B 生效之前"误判成 AFTER_ALL，
        # 而它其实还有后一版即将生效，处置是补登记空档而不是补新版本。
        firsts = [v for v in vs if v["_from"] is not None]
        if firsts and when < min(v["_from"] for v in firsts):
            return {"status": BEFORE_ALL, "spec": None, "version": None,
                    "doc_id": None,
                    "note": "该行时间早于最早一版规格的生效日（%s），当时尚无登记规格"
                            % firsts[0].get("effective_from"),
                    "effective": None}
        tail = vs[-1]
        if tail["_to"] is not None and when > tail["_to"]:
            _s = tail["_to"].date().isoformat() if hasattr(tail["_to"], "date") \
                else str(tail["_to"])
            return {"status": AFTER_ALL, "spec": None, "version": None,
                    "doc_id": None,
                    "note": "该行时间晚于最后一版规格（%s）的失效日（%s），"
                            "该版本之后尚未登记新规格" % (tail.get("version"), _s),
                    "effective": None}
        return {"status": GAP, "spec": None, "version": None, "doc_id": None,
                "note": "该行时间落在相邻两版规格之间的空档，这一段没有生效规格",
                "effective": None}

    # ------------------------------------------------------------ 体检
    def audit(self):
        """登记本身的毛病：空档、重叠、缺生效期。

        这些必须查出来——登记有洞而代码静默跳过，表现就是"这一批没算 Cpk"，
        使用者只会以为参数不重要。
        """
        gaps, overlaps, no_effective = [], [], []
        for n, vs in self._versions.items():
            label = self._by_norm.get(n, n)
            if not vs:
                continue
            undated = [v for v in vs if v["_from"] is None]
            if len(vs) > 1 and undated:
                for v in undated:
                    no_effective.append(
                        {"param": label, "version": v.get("version"),
                         "issue": "登记了 %d 版但有版本未写 effective_from，"
                                  "该版无法参与按时间的定位" % len(vs)})
            dated = [v for v in vs if v["_from"] is not None]
            for a, b in zip(dated, dated[1:]):
                a_to = a["_to"]
                # 上一版没写 effective_to 而下一版已经生效 = 重叠，不是空档
                if a_to is None:
                    overlaps.append(
                        {"param": label, "between": [a.get("version"), b.get("version")],
                         "issue": "版本 %s 未写 effective_to，与 %s（生效自 %s）重叠"
                                  % (a.get("version"), b.get("version"),
                                     b.get("effective_from"))})
                    continue
                # 连续性判据：effective_to 是**含端点**的失效日，
                # 「失效 3-31 / 生效 4-01」是首尾相接，不是空档——把相邻端点
                # 报成空档会让使用者去补一个根本不存在的洞（实测误报过）。
                # 只有相隔 2 天以上（中间确有一天无人覆盖）才算真空档。
                _d = (b["_from"].date() - a_to.date()).days \
                    if hasattr(b["_from"], "date") and hasattr(a_to, "date") else None
                if _d is None or _d > 1:
                    gaps.append(
                        {"param": label, "between": [a.get("version"), b.get("version")],
                         "from": a.get("effective_to"), "to": b.get("effective_from"),
                         "issue": "%s 失效（%s）到 %s 生效（%s）之间没有规格覆盖"
                                  % (a.get("version"), a.get("effective_to"),
                                     b.get("version"), b.get("effective_from"))})
                elif b["_from"] <= a_to:
                    overlaps.append(
                        {"param": label, "between": [a.get("version"), b.get("version")],
                         "issue": "%s 的失效日（%s）不早于 %s 的生效日（%s），两者重叠"
                                  % (a.get("version"), a.get("effective_to"),
                                     b.get("version"), b.get("effective_from"))})
        return {"gaps": gaps, "overlaps": overlaps, "no_effective": no_effective,
                "n_versioned": len(self._versions), "n_flat": len(self._flat)}

    def unmatched(self, columns):
        """规格书里哪些条目一条都没对上列——与 PP.unmatched_spec_keys 同口径。"""
        out = []
        for n, k in self._by_norm.items():
            if any(self.match_key(c) == k for c in (columns or [])):
                continue
            out.append(k)
        return out

    # ------------------------------------------------------------ 切分
    def split(self, col, values, datetimes):
        """按生效版本把 (值, 时间) 切成若干段，每段一份规格。

        返回 {"segments": [...], "n_unlocated": int, "status": ...}。
        段内给出该段生效的版本、规格与命中的行下标——Cpk 要**分段算**，
        把跨版本的数据混在一个 Cpk 里，等于用一个不存在的尺子量两种货。
        """
        vs = self.versions_of(col)
        nk = self.match_key(col)
        if not nk:
            return {"segments": [], "n_unlocated": len(values or []),
                    "status": NO_SPEC}
        if not vs:
            return {"segments": [], "n_unlocated": len(values or []),
                    "status": NO_SPEC}
        if len(vs) == 1 and vs[0]["_from"] is None and vs[0]["_to"] is None:
            r = self.resolve(col, None)
            return {"segments": [{"version": r.get("version"), "spec": r["spec"],
                                  "doc_id": r.get("doc_id"),
                                  "indexes": list(range(len(values or []))),
                                  "values": list(values or []),
                                  "from": None, "to": None}],
                    "n_unlocated": 0, "status": OK}
        if not datetimes:
            return {"segments": [], "n_unlocated": len(values or []),
                    "status": UNKNOWN_DATE,
                    "note": "该参数登记了 %d 个规格版本，但本批数据没有可解析的时间列，"
                            "无法按当时生效的规格重判（不默认取最新版）" % len(vs)}
        buckets = {}
        unlocated = []
        bad = {}
        for i, dt in enumerate(datetimes):
            if i >= len(values):
                break
            r = self.resolve(col, dt)
            if r["status"] != OK:
                unlocated.append(i)
                bad.setdefault(r["status"], []).append(i)
                continue
            key = str(r.get("version"))
            b = buckets.setdefault(key, {"version": r.get("version"),
                                         "spec": r["spec"], "doc_id": r.get("doc_id"),
                                         "indexes": [], "values": [],
                                         "from": None, "to": None})
            b["indexes"].append(i)
            b["values"].append(values[i])
        segs = []
        for key in sorted(buckets):
            b = buckets[key]
            ds = [datetimes[i] for i in b["indexes"] if datetimes[i] is not None]
            if ds:
                b["from"] = min(ds)
                b["to"] = max(ds)
            segs.append(b)
        st = OK if segs and not unlocated else (
            "partial" if segs else (sorted(bad)[0] if bad else NO_SPEC))
        out = {"segments": segs, "n_unlocated": len(unlocated), "status": st}
        if unlocated:
            out["unlocated_reasons"] = {k: len(v) for k, v in bad.items()}
        return out


def load(raw, col_map=None):
    """工厂：raw 为空时返回 None（调用方据此判断"没给规格"而不是"空规格书"）。

    返回 None 而不是空 SpecRegistry——"读不出来"和"企业没给"后果相同（都没规格），
    但前者是事故、后者是事实。返回 None 逼调用方出声。
    """
    if not raw:
        return None
    return SpecRegistry(raw, col_map=col_map)


def describe(reg, col, when=None):
    """给报告用的一句话：当前用的是哪一版、哪一版到哪一版生效。"""
    if reg is None:
        return None
    r = reg.resolve(col, when)
    if r["status"] != OK:
        return None
    if not r.get("version"):
        return None
    ef = r.get("effective") or {}
    span = ""
    if ef.get("from") or ef.get("to"):
        span = "（%s ~ %s）" % (ef.get("from") or "—", ef.get("to") or "现行")
    return "%s%s" % (r["version"], span)
