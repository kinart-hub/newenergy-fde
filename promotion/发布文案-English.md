# Launch Copy (English) — ready to post

> Two versions: a GitHub **Announcement** post, and a short **community post**
> for places like Reddit / Hacker News where self-promotion must be modest.
> Never describe this project as "open source" — the license is Elastic License 2.0
> (source-available, **not** OSI-approved).

---

## 1. GitHub Discussions — Announcement

**Title**: `newenergy-fde v1.14.1 — turn inspection CSV/Excel exports into a shippable 8D report`

```
If you work with lithium-battery or photovoltaic production lines, you know the drill:
process data sits in Excel, Cpk means half an hour of formulas, and when yield drops
the root cause is decided in a meeting by guessing.

newenergy-fde is a process-data analysis skill package I built for that exact workflow.

What it does
· Reads CSV/TSV/XLSX straight from inspection equipment
  (coating, winding, formation, wafer, EL, module IV)
· Judges the process step, cleans the data, then runs SPC rules,
  process capability, correlation, defect Pareto, root-cause and DOE
· Writes a process analysis report plus a full D1–D8 8D draft

Three boundaries I care about more than features
1. Zero third-party dependencies — Python standard library only.
   On a factory intranet there is no pip install, and there shouldn't need to be.
2. It never pushes parameter setpoints. D5 gives direction and category only,
   and the report carries a "not for external release" banner until a human signs off.
3. Customer data never enters the repo and never ships in the package
   (.gitignore plus a pre-publish privacy audit).

Try it in 30 seconds

    git clone https://github.com/kinart-hub/newenergy-fde.git
    cd newenergy-fde
    python scripts/selftest.py                 # 279 end-to-end checks
    python scripts/fde_run.py analyze -i examples/01_coating.csv -o output/try

License: Elastic License 2.0 — source-available, not OSI open source.
Free for personal and internal business use; managed/hosted services require
a separate commercial license.

Got a de-identified dataset and want to see what it produces? Send it over and
I'll run a report for you.
```

---

## 2. Short community post (Reddit / HN style)

**Reddit** (r/batteries, r/manufacturing, r/qualityassurance — read the rules first,
many subs only allow project posts on specific days):

```
I got tired of turning inspection CSVs into 8D reports by hand, so I wrote a
zero-dependency Python toolkit for battery/PV process data.

It takes the raw export from coating / winding / formation / wafer / EL / IV tools,
runs step detection, cleaning, SPC, Cpk, correlation, Pareto, root cause and DOE,
and writes a report plus a D1-D8 8D draft.

Two deliberate choices that might be unusual:
- No pandas/numpy. Standard library only, so it runs on an air-gapped factory PC.
- It refuses to output parameter setpoints. D5 gives direction and category only,
  and the report is marked "not for external release" until a human signs off.
  An AI silently changing process parameters is a failure mode, not a feature.

License is Elastic License 2.0 (source-available, free for internal use).
Repo: https://github.com/kinart-hub/newenergy-fde
```

**Hacker News** — only if you have something to discuss, not just to announce.
Suggested title: `A zero-dependency process-data toolkit that refuses to set parameters`

Keep the first comment factual: what it does, why no dependencies, why the license.
Answer questions about the statistics directly; that is where credibility comes from.

---

## Always end with

> License: Elastic License 2.0 — **source-available, not OSI open source**.
> Free for personal and internal business use.
