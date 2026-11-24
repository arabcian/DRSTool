"""Xid knowledge base, incident analysis and a Bayesian model linking settings to GPU faults.

Four layers, all Qt-free and deterministic:

1. `parse_event` reads one NVRM Xid line into its fields (code, PCI, pid / process, channel,
   SM location, MMU fault kind / engine / address, errorString, GSP function, Class / ErrorCode)
   and the subtypes the knowledge base (data/xid_db.json) knows for that code.
2. `incidents` groups events that belong to one failure (time gap), finds the root event (Xid 45,
   43 and 154 only follow other errors), known cascades (109 → 119, 62 → 119, 119 → 79 …) and the
   scope: "app" faults can come from a setting, "system" / "hardware" ones cannot.
3. `prior_weights` turns the root Xid, its subtypes, the GPU architecture and the driver branch
   into a prior for every setting of the configuration under test: the knowledge base links Xids
   to mechanisms (descriptor paths, ray tracing, memory placement, queues …), mechanisms to env
   vars / VKD3D_CONFIG flags (explicit lists plus name patterns, so new variables still map), and
   carries forum-sourced evidence for specific settings.
4. `CrashModel` is a Bayesian survival model over "which settings cause the crash". Each test run
   is (settings on, seconds played, crashed?). A hypothesis is "nothing tested" / one setting /
   bounded higher-order AND interactions / independent causes (OR) / mixed cause groups. Under a hypothesis the run
   crashes as a Poisson process: rate λ while the condition holds, a small background rate ε
   otherwise; both rates have Gamma priors, so every marginal likelihood is closed form. Because
   evidence is weighted by play time, a crash that needs 20 minutes is not ruled out by a 3-minute
   "works", and one wrong answer lowers confidence instead of derailing the search (ddmin assumes
   a perfect oracle). `best_test` picks the next configuration by expected information gain and
   `best_plan` balances information against time cost; planned durations are recommendations, not guarantees. `AdaptiveSearch` wraps it in the
   CrashSearch interface used by the Crash Lab.

`associate` applies the model per symptom (root Xid of each crashed test), which gives the
Xid ↔ setting links measured on the user's own runs.
"""
from __future__ import annotations

import json
import math
import re
import random
from itertools import combinations, islice
from typing import Iterable, Sequence

from .catalog import DATA_DIR

DB = json.loads((DATA_DIR / "xid_db.json").read_text(encoding="utf-8"))
XIDS: dict = {int(k): v for k, v in DB["xids"].items()}
SOURCES: dict = DB["sources"]
_SUBTYPES = {x: [dict(s, rx=re.compile(s["regex"])) for s in e.get("subtypes", [])] for x, e in XIDS.items()}
_MECH_PATTERNS = {m: [p.upper() for p in d.get("patterns", [])] for m, d in DB["mechanisms"].items()}
_MECH_ITEMS = {m: set(d.get("items", [])) for m, d in DB["mechanisms"].items()}


# ─────────────────────────────── items ───────────────────────────────
def item_key(item) -> str:
    """("env", "NAME") -> "env:NAME"; strings pass through."""
    return item if isinstance(item, str) else f"{item[0]}:{item[1]}"


def mechanisms_of(item) -> set:
    """Mechanisms a setting belongs to: the knowledge base's lists first, else its name."""
    key = item_key(item)
    found = {m for m, items in _MECH_ITEMS.items() if key in items}
    if found:
        return found
    name = key.partition(":")[2].upper()
    return {m for m, pats in _MECH_PATTERNS.items() if any(p in name for p in pats)}


# ─────────────────────────────── parsing ───────────────────────────────
_XID = re.compile(r"\bXid\b\s*(?:\((?:PCI:)?([^)]*)\))?\s*:\s*(\d{1,4})\b,?\s*(.*)$")
_TS_UNIX = re.compile(r"^\s*(\d{9,12}\.\d+)\s")
_TS_ISO = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?)")
_TS_MONO = re.compile(r"\[\s*(\d+\.\d+)\]")
_FIELDS = (
    ("pid", re.compile(r"\bpid=(\d+)")),
    ("process", re.compile(r"\bname=([^,]*?)(?:,|$)")),
    ("channel", re.compile(r"\b(?:ChI[dD]|Ch|[Cc]hannel(?: ID)?)\s+(?:0x)?([0-9a-fA-F]+)\b")),
    ("class", re.compile(r"\bClass\s+(?:0x)?([0-9a-fA-F]{4,8})\b")),
    ("error_code", re.compile(r"\bErrorCode\s+(?:0x)?([0-9a-fA-F]+)\b")),
    ("error_string", re.compile(r"errorString\s+([A-Z][A-Z _]+?)(?:,|$)")),
    ("gsp_function", re.compile(r"Expected function \d+ \((\w+)\)")),
)
_SM = re.compile(r"\(GPC (\d+), TPC (\d+)(?:, SM (\d+))?\)")
_MMU = re.compile(r"MMU Fault: ENGINE (\S+)(?: (GPC\d+))? (\S+) faulted @ (0x[0-9a-fA-F_]+)\. "
                  r"Fault is of type (FAULT_\w+) (ACCESS_TYPE_\w+)")
_DRIVER = re.compile(r"NVRM: loading NVIDIA UNIX.*?(\d{3}\.\d+(?:\.\d+)?)")


def parse_event(line: str, *, index: int = 0) -> dict | None:
    """One NVRM Xid line -> {"xid", "pci", "detail", "subtypes", "ts", …} or None."""
    m = _XID.search(line)
    if not m:
        return None
    detail = m.group(3).strip()
    ev = {"xid": int(m.group(2)), "pci": (m.group(1) or "").strip() or None, "detail": detail[:300],
          "index": index, "ts": None}
    for name, rx in _FIELDS:
        f = rx.search(detail)
        if f and f.group(1).strip("'‘’ <>") not in ("", "unknown"):
            ev[name] = f.group(1).strip()
    sm = _SM.search(detail)
    if sm:
        ev["sm"] = tuple(int(g) if g is not None else None for g in sm.groups())
    mmu = _MMU.search(detail)
    if mmu:
        ev["fault"] = {"engine": mmu.group(1), "client": mmu.group(3), "address": mmu.group(4),
                       "kind": mmu.group(5), "access": mmu.group(6)}
    ev["subtypes"] = [s["id"] for s in _SUBTYPES.get(ev["xid"], []) if s["rx"].search(detail)]
    for rx, conv in ((_TS_UNIX, float), (_TS_ISO, _iso), (_TS_MONO, float)):
        t = rx.search(line[:m.start()]) if rx is _TS_MONO else rx.match(line)
        if t:
            ev["ts"] = conv(t.group(1))
            break
    return ev


def _iso(text: str) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(text.replace(",", ".")).timestamp()
    except ValueError:
        return None


def parse_events(lines: Iterable[str]) -> tuple[list, str | None]:
    """(Xid events, NVIDIA driver version if the log names it)."""
    events, driver = [], None
    for n, line in enumerate(lines):
        if "NVRM" not in line and "Xid" not in line:
            continue
        d = _DRIVER.search(line)
        if d:
            driver = d.group(1)
        ev = parse_event(line, index=n)
        if ev:
            events.append(ev)
    return events, driver


# ─────────────────────────────── incidents ───────────────────────────────
_SCOPE_RANK = {"hardware": 3, "system": 2, "app": 1, "info": 0, "unknown": 1}


def info(xid: int) -> dict:
    """Knowledge-base entry; unknown codes get a neutral stub."""
    return XIDS.get(xid) or {"mnemonic": "?", "title": f"Xid {xid} (not in the knowledge base)",
                             "scope": "unknown", "nvidia": "—", "summary": "Look the code up in NVIDIA's Xid catalog.",
                             "mechanisms": {}}


def incidents(events: Sequence[dict], *, gap: float = 30.0) -> list[dict]:
    """Group by GPU and monotonic time; unknown timestamps cannot bridge groups."""
    groups = []
    active = {}
    previous = None
    for ev in events:
        ts = ev.get('ts')
        if ts is not None and previous is not None and ts < previous:
            active.clear()
        if ts is not None:
            previous = ts
        pci = ev.get('pci')
        g = active.get(pci)
        last = g[-1] if g else None
        if (last is not None and ts is not None and last.get('ts') is not None
                and 0 <= ts - last['ts'] <= gap):
            g.append(ev)
        else:
            g = [ev]
            groups.append(g)
        if ts is None:
            active.pop(pci, None)
        else:
            active[pci] = g
    return [_incident(g) for g in groups]


def _incident(evs: list) -> dict:
    codes = list(dict.fromkeys(e["xid"] for e in evs))
    candidates = [e for e in evs if not info(e["xid"]).get("secondary")]
    root = candidates[0] if candidates else evs[0]
    scopes = [info(c).get("scope", "unknown") for c in codes]
    scope = max(scopes, key=lambda s: _SCOPE_RANK.get(s, 1))
    if not candidates or scope == "info":
        scope = "unknown"
    cascades = [c for c in DB.get("cascades", []) if _subsequence(c["sequence"], codes)]
    subtypes = list(dict.fromkeys(s for e in evs if e["xid"] == root["xid"] for s in e["subtypes"]))
    locs = {e["sm"][:2] for e in evs if e.get("sm")}
    return {"codes": codes, "root": root, "root_candidates": candidates, "root_known": bool(candidates), "events": evs, "count": len(evs), "scope": scope,
            "subtypes": subtypes, "cascades": cascades,
            "signature": f"{root['xid']}" + (f":{subtypes[0]}" if subtypes else ""),
            "process": next((e["process"] for e in evs if e.get("process")), None),
            "sm_spread": len(locs), "start": evs[0]["ts"], "end": evs[-1]["ts"]}


def _subsequence(seq, codes) -> bool:
    it = iter(codes)
    return all(any(c == s for c in it) for s in seq)


def hardware_pattern(incs: Sequence[dict]) -> str | None:
    """NVIDIA's WORKFLOW_XID_13 / 31 heuristic: the same GPC/TPC in every Xid 13 incident points
    at the board; faults spread over the chip point at software."""
    by_gpu = {}
    for i in incs:
        if i['root']['xid'] == 13:
            pci = i['root'].get('pci')
            if pci:
                by_gpu.setdefault(pci, []).append({e['sm'][:2] for e in i['events'] if e.get('sm')})
    for pci, locs in by_gpu.items():
        if len(locs) >= 2 and all(len(x) == 1 for x in locs) and set.intersection(*locs):
            gpc, tpc = next(iter(set.intersection(*locs)))
            return (f'GPU {pci}: every Xid 13 hit the same unit (GPC {gpc}, TPC {tpc}). '
                    'A repeating unit is a possible hardware fault, not proof.')
    return None


# ─────────────────────────────── knowledge-base priors ───────────────────────────────
def _driver_match(rngs, driver) -> float:
    if not rngs or not driver:
        return 1.0
    return 1.5 if any(driver.startswith(r) for r in rngs) else 0.6


def _arch_match(archs, arch) -> float:
    if not archs or not arch:
        return 1.0
    return 1.3 if any(str(arch).upper().startswith(a) for a in archs) else 0.3


def prior_weights(xid: int, subtypes: Iterable[str] = (), items: Iterable = (), *,
                  arch: str | None = None, driver: str | None = None) -> dict:
    """item key -> (log weight, [reasons]) for the settings in `items`.
    Mechanism link: the strongest mechanism the Xid / subtype names for the item.
    Evidence: forum reports naming the item, scaled by GPU-arch and driver-branch match."""
    e = info(xid)
    mech = dict(e.get("mechanisms", {}))
    subs = {s["id"]: s for s in _SUBTYPES.get(xid, [])}
    for sid in subtypes:
        for m, w in subs.get(sid, {}).get("mechanisms", {}).items():
            mech[m] = max(mech.get(m, 0.0), w)
    out = {}
    for item in items:
        key = item_key(item)
        w, why = 0.0, []
        hits = [(mech[m], m) for m in mechanisms_of(key) if m in mech]
        if hits:
            mw, m = max(hits)
            w += mw
            why.append(f"Xid {xid} → {DB['mechanisms'][m]['title'].lower()}")
        for ev in e.get("evidence", []):
            if key in ev.get("items", []):
                f = _arch_match(ev.get("archs"), arch) * _driver_match(ev.get("drivers"), driver)
                w += ev.get("weight", 0.5) * f
                why.append(f"reported: {ev['note']} [{ev['source']}]")
        out[key] = (min(w, 3.0), why)
    return out


def mitigations(xid: int, *, driver: str | None = None, arch: str | None = None) -> list[dict]:
    """Settings reported to *prevent* this Xid (to try adding), most relevant first."""
    out = []
    for ev in info(xid).get("evidence", []):
        for item in ev.get("mitigates", []):
            score = _arch_match(ev.get("archs"), arch) * _driver_match(ev.get("drivers"), driver)
            out.append({"item": item, "score": score, "note": ev["note"], "source": ev["source"]})
    return sorted(out, key=lambda m: -m["score"])


# ─────────────────────────────── diagnosis ───────────────────────────────
def diagnose(lines: Iterable[str], *, items: Iterable = (), arch: str | None = None,
             driver: str | None = None) -> dict:
    """Everything the knowledge base can say about the Xids in `lines`.

    -> {"incidents", "primary", "driver", "bisect", "suspects", "mitigations", "remedies",
        "evidence", "hardware"}; `suspects` is ranked for `items` (the configuration under test),
    or lists every setting the knowledge base links to the root Xid when no items are given."""
    events, logged_driver = parse_events(lines)
    driver = driver or logged_driver
    incs = incidents(events)
    if not incs:
        return {"incidents": [], "primary": None, "driver": driver, "bisect": None, "suspects": [],
                "mitigations": [], "remedies": [], "evidence": [], "hardware": None}
    # the most serious scope decides; among equals the earliest
    primary = max(incs, key=lambda i: (_SCOPE_RANK.get(i["scope"], 1), -incs.index(i)))
    root = primary["root"]["xid"]
    e = info(root)
    pool = list(items) or sorted({i for ev in e.get("evidence", []) for i in ev.get("items", [])}
                                 | {i for m in e.get("mechanisms", {}) for i in _MECH_ITEMS.get(m, ())})
    weights = prior_weights(root, primary["subtypes"], pool, arch=arch, driver=driver) if primary["root_known"] else {}
    suspects = sorted(((k, w, why) for k, (w, why) in weights.items() if w > 0), key=lambda s: -s[1])
    scope = primary["scope"]
    bisect = ("worth" if scope == "app" else
              "unlikely" if scope in ("system", "hardware") else "maybe")
    return {"incidents": incs, "primary": primary, "driver": driver, "bisect": bisect,
            "suspects": suspects, "mitigations": mitigations(root, driver=driver, arch=arch),
            "remedies": list(e.get("remedies", [])), "evidence": list(e.get("evidence", [])),
            "hardware": hardware_pattern(incs)}


def format_diagnosis(d: dict, *, markdown: bool = False, top: int = 6) -> list[str]:
    """Report lines for crash_triage.format_report."""
    if not d.get("primary"):
        return []
    code = (lambda s: f"`{s}`") if markdown else (lambda s: s)
    p = d["primary"]
    e = info(p["root"]["xid"])
    label = "Root candidate" if p.get("root_known", True) else "Follow-up only (root unknown)"
    out = [f"- {label}: Xid {p['root']['xid']} — {e['title']} ({e['mnemonic']}); scope: {p['scope']}"
           + (f"; process {code(p['process'])}" if p.get("process") else "")
           + (f"; driver {d['driver']}" if d.get("driver") else ""),
           f"  {e['summary']}",
           f"  NVIDIA catalog action: {e.get('nvidia', '—')}"]
    if len(p.get("root_candidates", [])) > 1:
        out.append("  Other root candidates: " + ", ".join(str(x) for x in dict.fromkeys(ev["xid"] for ev in p["root_candidates"][1:])))
    subs = {s["id"]: s for s in _SUBTYPES.get(p["root"]["xid"], [])}
    out += [f"  · {subs[s]['meaning']}" for s in p["subtypes"] if s in subs]
    if len(p["codes"]) > 1:
        out.append("- Sequence: " + " → ".join(f"Xid {c}" for c in p["codes"]))
    out += [f"- Cascade: {c['title']}" for c in p["cascades"]]
    if p.get("sm_spread", 0) > 2:
        out.append(f"- Faults spread over {p['sm_spread']} GPC/TPC units: software, not one bad unit.")
    if d.get("hardware"):
        out.append(f"- {d['hardware']}")
    out.append({"worth": "- Setting search: worth it — this kind of fault can come from a setting.",
                "unlikely": "- Setting search: unlikely to help — the fault is below the game (driver, firmware, "
                            "display or PCIe). Start with the remedies.",
                "maybe": "- Setting search: may help."}[d["bisect"]])
    if d["suspects"]:
        out.append("- Linked settings (knowledge base): " + ", ".join(
            f"{code(k)} ({w:.1f})" for k, w, _ in d["suspects"][:top]))
    for m in d["mitigations"][:3]:
        out.append(f"- Reported to prevent it: {code(m['item'])} — {m['note']} [{m['source']}]")
    out += [f"- Remedy: {r}" for r in d["remedies"]]
    used = {ev["source"] for ev in d["evidence"]}
    if used:
        out.append("- Sources: " + "; ".join(f"{SOURCES[s]['title']} <{SOURCES[s]['url']}>"
                                             for s in sorted(used) if s in SOURCES))
    return out


# ─────────────────────────────── Bayesian crash model ───────────────────────────────
def _log_marginal(k: int, t: float, a: float, b: float) -> float:
    """log ∫ λ^k e^{-λt} Gamma(λ; a, b) dλ — Poisson process with a Gamma prior on its rate."""
    return a * math.log(b) - math.lgamma(a) + math.lgamma(a + k) - (a + k) * math.log(b + t)


def _p_crash(a: float, b: float, t: float) -> float:
    """Posterior-predictive probability of a crash within t seconds (Lomax CDF)."""
    return -math.expm1(-a * math.log1p(t / b))


def _h2(p: float) -> float:
    return 0.0 if p <= 0.0 or p >= 1.0 else -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


def _positive(value, name):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _bounded_groups(ranked, degree, focus, budget):
    """Keep focused combinations and a reproducible sample across *all* candidates.

    Never materialize the combinatorial universe. Every setting can participate,
    even if the knowledge base assigned it a low prior.
    """
    if len(ranked) < degree or not budget:
        return []
    total = math.comb(len(ranked), degree)
    if total <= budget:
        return list(combinations(ranked, degree))
    order = {k: i for i, k in enumerate(ranked)}
    groups = set(islice(combinations(ranked[:max(degree, focus)], degree), budget // 2))
    # Deliberately cover settings omitted by the focused combinations.
    for i in range(len(ranked)):
        if len(groups) >= budget * 3 // 4:
            break
        g = [ranked[(i + j) % len(ranked)] for j in range(degree)]
        groups.add(tuple(sorted(g, key=order.__getitem__)))
    rng = random.Random(7919 + degree)
    for _ in range(budget * 20):
        if len(groups) >= budget:
            break
        groups.add(tuple(sorted(rng.sample(ranked, degree), key=order.__getitem__)))
    if len(groups) < budget:
        for g in islice(combinations(ranked, degree), budget):
            groups.add(g)
            if len(groups) >= budget:
                break
    return sorted(groups, key=lambda g: tuple(order[k] for k in g))


class CrashModel:
    """Gamma-Poisson survival model with bounded single, AND, OR and mixed hypotheses.

    Mixed hypotheses encode (A and B) or C, or (A and B) or (C and D). Probabilities are conditional on
    the enumerated model, not proof that a particular configuration is safe.
    """

    def __init__(self, items: Sequence, prior: dict | None = None, *, crash_seconds: float | None = None,
                 p_none: float = 0.10, p_and: float = 0.12, p_or: float = 0.10, pair_limit: int = 40,
                 background_hours: float = 20.0, triple_limit: int = 0, higher_limit: int = 0,
                 mixed_limit: int = 0, max_order: int = 5):
        self.items = list(dict.fromkeys(item_key(i) for i in items))
        if "none" in self.items:
            raise ValueError("reserved setting key: none")
        self._keys = frozenset(self.items)
        prior = prior or {}
        mean = max(_positive(600.0 if crash_seconds is None else crash_seconds, "crash_seconds"), 10.0)
        self.rate = (1.0, mean)
        self.bg = (0.5, 0.5 * _positive(background_hours, "background_hours") * 3600)
        w = {i: max(-30.0, min(30.0, float(prior.get(i, 0.0)))) for i in self.items}
        if any(not math.isfinite(float(prior.get(i, 0.0))) for i in self.items):
            raise ValueError("non-finite log prior")
        ranked = sorted(self.items, key=lambda i: -w[i])
        pairs = _bounded_groups(ranked, 2, pair_limit, 780) if pair_limit > 0 else []
        triples = _bounded_groups(ranked, 3, triple_limit, 220) if triple_limit > 0 else []
        higher = [g for d in (4, 5) for g in _bounded_groups(ranked, d, higher_limit, 140)] if higher_limit > 0 else []
        if not isinstance(max_order, int) or not 5 <= max_order <= 12:
            raise ValueError("max_order must be between 5 and 12")
        multi = _bounded_groups(ranked, 3, mixed_limit, 60) if mixed_limit > 0 else []
        extended = [g for d in range(6, max_order + 1)
                    for g in _bounded_groups(ranked, d, higher_limit, 40)] if higher_limit > 0 else []
        if higher_limit > 0 and max_order > 5 and len(ranked) > max_order:
            extended.append(tuple(ranked))
        quads = _bounded_groups(ranked, 4, mixed_limit, 30) if mixed_limit > 0 and max_order > 5 else []
        two_pairs = [("mixed2", g[0], g[j], *(g[k] for k in range(1, 4) if k != j))
                     for g in quads for j in range(1, 4)]
        families = [(p_none, [("none",)], [0.0]),
                    (p_and, [("and", *g) for g in pairs], [sum(w[i] for i in g) for g in pairs]),
                    (p_or, [("or", *g) for g in pairs], [sum(w[i] for i in g) for g in pairs]),
                    (0.04, [("and", *g) for g in triples], [sum(w[i] for i in g) for g in triples]),
                    (0.025, [("and", *g) for g in higher], [sum(w[i] for i in g) for g in higher])]
        mixed = [("mixed", *(i for i in g if i != single), single) for g in multi for single in g]
        independent = [("or", *g) for g in multi]
        families += [(0.025, mixed, [sum(w[i] for i in h[1:]) for h in mixed]),
                     (0.015, independent, [sum(w[i] for i in h[1:]) for h in independent]),
                     (0.006, [("and", *g) for g in extended], [sum(w[i] for i in g) / max(1, len(g)/5) for g in extended]),
                     (0.006, two_pairs, [sum(w[i] for i in h[1:]) for h in two_pairs])]
        if any(not math.isfinite(m) or m < 0 or m >= 1 for m, _, _ in families):
            raise ValueError("invalid hypothesis family mass")
        used = sum(m for m, hs, _ in families if hs)
        if used >= 1:
            raise ValueError("hypothesis family masses must sum to less than one")
        if not self.items:
            families = [(1.0, [("none",)], [0.0])]
        else:
            families.append((1 - used, [("one", i) for i in self.items], [w[i] for i in self.items]))
        self.hyps, self.log_prior = [], []
        for mass, hs, weights in families:
            if not hs or not mass:
                continue
            top = max(weights)
            norm = top + math.log(math.fsum(math.exp(v - top) for v in weights))
            self.hyps.extend(hs)
            self.log_prior.extend(math.log(mass) + v - norm for v in weights)
        self.runs: list = []
        self._cache: dict = {}
        self._posterior_cache = None

    # ── data ──
    def add(self, on: Iterable, seconds: float, crashed: bool):
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds <= 0 or not isinstance(crashed, bool):
            raise ValueError('run requires finite positive seconds and a boolean result')
        on = frozenset(item_key(i) for i in on)
        if not on <= self._keys:
            raise ValueError("run contains an unknown setting")
        self.runs.append((on, seconds, crashed))
        # Incremental sufficient statistics; no repeated history scans.
        for h, (k1, t1, k0, t0) in self._cache.items():
            self._cache[h] = (k1 + crashed, t1 + seconds, k0, t0) if self._holds(h, on) else (k1, t1, k0 + crashed, t0 + seconds)
        self._posterior_cache = None

    @staticmethod
    def _holds(h, on: frozenset) -> bool:
        kind = h[0]
        if kind == "none":
            return True
        if kind == "one":
            return h[1] in on
        if kind == "and":
            return all(i in on for i in h[1:])
        if kind == "or":
            return any(i in on for i in h[1:])
        if kind == "mixed":
            return (h[1] in on and h[2] in on) or h[3] in on
        if kind == "mixed2":
            return (h[1] in on and h[2] in on) or (h[3] in on and h[4] in on)
        raise ValueError(f"unknown hypothesis kind: {kind}")

    def _stats(self, h):
        if h in self._cache:
            return self._cache[h]
        k1 = t1 = k0 = t0 = 0.0
        for on, t, c in self.runs:
            if self._holds(h, on):
                k1, t1 = k1 + c, t1 + t
            else:
                k0, t0 = k0 + c, t0 + t
        self._cache[h] = (k1, t1, k0, t0)
        return self._cache[h]

    def posterior(self) -> list:
        """[(hypothesis, probability)] sorted by probability."""
        if self._posterior_cache is not None:
            return list(self._posterior_cache)
        logs = []
        for h, lp in zip(self.hyps, self.log_prior):
            k1, t1, k0, t0 = self._stats(h)
            logs.append(lp + _log_marginal(int(k1), t1, *self.rate) + _log_marginal(int(k0), t0, *self.bg))
        top = max(logs)
        ps = [math.exp(v - top) for v in logs]
        z = sum(ps)
        self._posterior_cache = sorted(((h, p / z) for h, p in zip(self.hyps, ps)), key=lambda hp: -hp[1])
        return list(self._posterior_cache)

    def marginals(self, post=None) -> dict:
        """item -> probability that it is (part of) the cause; "none" -> nothing tested is."""
        m = dict.fromkeys(self.items, 0.0)
        m["none"] = 0.0
        for h, p in post or self.posterior():
            if h[0] == "none":
                m["none"] += p
            for i in h[1:]:
                m[i] += p
        return m


    # ── planning ──
    def _rate_post(self, h, on_holds: bool):
        k1, t1, k0, t0 = self._stats(h)
        return (self.rate[0] + k1, self.rate[1] + t1) if on_holds else (self.bg[0] + k0, self.bg[1] + t0)

    def _predictions(self, seconds, post):
        seconds = _positive(seconds, "seconds")
        return [(h, p, _p_crash(*self._rate_post(h, False), seconds),
                 _p_crash(*self._rate_post(h, True), seconds)) for h, p in post]

    def _score(self, on, predictions):
        crash = cond = 0.0
        for h, p, q0, q1 in predictions:
            q = q1 if self._holds(h, on) else q0
            crash += p * q
            cond += p * _h2(q)
        return max(0.0, _h2(crash) - cond), min(1.0, crash)

    def info_gain(self, on: Iterable, seconds: float, post=None) -> float:
        """Expected information in the binary crash/survival outcome, in bits."""
        on = frozenset(item_key(i) for i in on)
        return self._score(on, self._predictions(seconds, post or self.posterior()))[0]

    def crash_probability(self, on, seconds, post=None):
        return self._score(frozenset(item_key(i) for i in on),
                           self._predictions(seconds, post or self.posterior()))[1]

    def candidates(self, post=None) -> list:
        post = post or self.posterior()
        m = self.marginals(post)
        ranked = sorted(self.items, key=lambda i: -m[i])
        full = self._keys
        if len(ranked) <= 7:
            return [frozenset(g) for n in range(len(ranked)+1) for g in combinations(ranked, n)]
        cands = {frozenset(), full}
        # Quantile splits rather than O(n) long prefixes for large profiles.
        for n in sorted(set(range(1, min(12, len(ranked))) ) | {len(ranked)*j//12 for j in range(1,12)}):
            c = frozenset(ranked[:n]); cands.update((c, full-c))
        for i in ranked[:12]:
            cands.update((frozenset([i]), full-{i}))
        for h, _ in post[:16]:
            for g in groups_of(h):
                c = frozenset(g); cands.update((c, full-c))
            c = frozenset(h[1:]); cands.update((c, full-c))
        rng = random.Random(1741)
        for _ in range(12):
            c = frozenset(rng.sample(ranked, len(ranked)//2)); cands.update((c, full-c))
        return sorted(cands, key=lambda c: (len(c), sorted(c)))

    def best_test(self, seconds: float, post=None) -> tuple[frozenset, float]:
        post = post or self.posterior()
        preds = self._predictions(seconds, post)
        gain, _, _, best = max((self._score(c, preds)[0], -len(c), tuple(sorted(c)), c)
                               for c in self.candidates(post))
        return best, gain

    def best_plan(self, seconds, post=None, *, overhead=None):
        """Select configuration AND duration by expected bits per wall-clock budget.

        A modest minimum duration avoids tiny, setup-dominated experiments.
        """
        post = post or self.posterior()
        seconds = _positive(seconds, "seconds")
        overhead = _positive(max(90., 4 * seconds) if overhead is None else overhead, "overhead")
        candidates = self.candidates(post)
        options = sorted({max(30., min(14400., seconds * f)) for f in (1., 2., 4., 8.)})
        active = [(c, tuple(sorted(c)), [i for i, (h, _) in enumerate(post) if self._holds(h, c)])
                  for c in candidates]
        best = None
        for duration in options:
            preds = self._predictions(duration, post)
            base_q = math.fsum(p*q0 for _, p, q0, _ in preds)
            base_h = math.fsum(p*_h2(q0) for _, p, q0, _ in preds)
            dq = [p*(q1-q0) for _, p, q0, q1 in preds]
            dh = [p*(_h2(q1)-_h2(q0)) for _, p, q0, q1 in preds]
            for c, keys, indices in active:
                risk = min(1., max(0., base_q + math.fsum(dq[i] for i in indices)))
                gain = max(0., _h2(risk) - base_h - math.fsum(dh[i] for i in indices))
                score = gain / (duration + overhead)
                row = (score, gain, -duration, -len(c), keys, c, risk)
                if best is None or row[:5] > best[:5]:
                    best = row
        return best[5], -best[2], best[1], best[6]

    def play_time(self, on: Iterable, confidence: float = 0.85, post=None) -> float:
        """Conditional detection time for the most likely active hypothesis."""
        if not 0 < confidence < 1:
            raise ValueError("confidence must be between zero and one")
        post = post or self.posterior()
        on = frozenset(item_key(i) for i in on)
        h = next((h for h, _ in post if self._holds(h, on)), post[0][0])
        a, b = self._rate_post(h, True)
        return b * math.expm1(-math.log1p(-confidence) / a)


def groups_of(h) -> list:
    """Hypothesis -> culprit groups as the Crash Lab shows them."""
    return ([] if h[0] == "none" else [[h[1]]] if h[0] == "one" else
            [list(h[1:])] if h[0] == "and" else
            [list(h[1:3]), [h[3]]] if h[0] == "mixed" else
            [list(h[1:3]), list(h[3:5])] if h[0] == "mixed2" else [[i] for i in h[1:]])


def describe(h) -> str:
    if h[0] == "and":
        return " + ".join(h[1:]) + " (only together)"
    if h[0] == "mixed":
        return f"({h[1]} + {h[2]}) or {h[3]}"
    if h[0] == "mixed2":
        return f"({h[1]} + {h[2]}) or ({h[3]} + {h[4]})"
    if h[0] == "or":
        return " or ".join(h[1:]) + " (independent causes)"
    return h[1] if h[0] == "one" else "none of the tested settings"


# ─────────────────────────────── adaptive search ───────────────────────────────
class AdaptiveSearch:
    """Drop-in for bisect_search.CrashSearch, driven by CrashModel.

    A measured failing run explicitly trusted by the caller is the first observation; otherwise the full
    configuration is tested. Each step balances information gain and duration; the final
    configuration must accumulate sufficient survival exposure before accepting a culprit.
    Items are tuples, as in CrashSearch."""

    def __init__(self, items: Sequence, answers: Iterable[bool] = (), durations: Iterable[float] = (), *,
                 prior: dict | None = None, crash_seconds: float | None = None,
                 threshold: float = 0.95, max_tests: int = 40, tested=None, pending=None, planned_seconds=None,
                 known_baseline: bool = True):
        self.items = list({item_key(i): i for i in items}.values())
        self._by_key = {item_key(i): i for i in self.items}
        self.prior, self.crash_seconds = dict(prior or {}), crash_seconds
        if not 0.5 < threshold < 1 or not isinstance(max_tests, int) or max_tests < 1:
            raise ValueError("invalid stopping criteria")
        self.threshold, self.max_tests = threshold, max_tests
        self.known_baseline = bool(known_baseline)
        self._restart()
        answers, durations = list(answers), list(durations)
        if tested is not None:
            # Persist actual configurations, not a reconstruction under today's planner.
            tested = list(tested)
            if not len(tested) == len(answers) == len(durations) or len(answers) > max_tests:
                raise ValueError("inconsistent saved observations")
            for on, a, t in zip(tested, answers, durations):
                self.model.add(on, t, a)
                self.answers.append(a); self.durations.append(float(t))
            self._plan()
            if pending is not None and not self.done:
                keys = frozenset(item_key(i) for i in pending)
                if not keys <= self.model._keys:
                    raise ValueError("unknown saved pending setting")
                self.pending = [i for i in self.items if item_key(i) in keys]
                if self.phase_name not in ("verify", "baseline"):
                    self.phase_name = "fixed" if not keys else "search"
                if planned_seconds is not None:
                    self._planned_seconds = _positive(planned_seconds, "saved planned seconds")
                self.expected_gain = self.model.info_gain(keys, self._planned_seconds, self.post)
        else:
            for n, a in enumerate(answers):
                self.answer(a, durations[n] if n < len(durations) else None)

    def _restart(self):
        self.answers: list = []
        self.durations: list = []
        self.model = CrashModel(self.items, self.prior, crash_seconds=self.crash_seconds,
                                triple_limit=10, higher_limit=8, mixed_limit=8, max_order=8)
        # A measured failing run supplied by triage is evidence. Without one,
        # first reproduce the full configuration; never fabricate a crash.
        self.initial_runs = int(bool(self.items) and self.crash_seconds is not None and self.known_baseline)
        if self.initial_runs:
            self.model.add(self._by_key, self.crash_seconds, True)
        self.outcome = None
        self.phase_name = "search"
        self._planned_seconds = self.test_seconds
        self._plan()

    @property
    def test_seconds(self) -> float:
        return float(self.crash_seconds or self.model.rate[1])

    @property
    def observations(self):
        return self.model.runs[self.initial_runs:]

    def _plan(self):
        post = self.model.posterior()
        self.post = post
        self.outcome = None
        best, p = post[0]
        full = frozenset(self._by_key)
        rest = full - {i for g in groups_of(best) for i in g}
        runs = self.observations
        self._planned_seconds = self.test_seconds
        self.expected_gain = 0.0
        self.verification_probability = 0.0
        if not self.items:
            self.pending, self.outcome = None, ("fixed", [])
            return
        if not any(c for _, _, c in self.model.runs):
            if sum(t for on, t, c in runs if on == full) >= 3 * self.test_seconds:
                self.pending, self.outcome = None, ("not-reproduced", [])
            elif len(self.answers) >= self.max_tests:
                self.pending, self.outcome = None, ("inconclusive", [])
            else:
                self.phase_name, self.pending = "baseline", list(self.items)
                self._planned_seconds = self.test_seconds
                self.expected_gain = self.model.info_gain(full, self._planned_seconds, post)
            return
        if best[0] == "none":
            verified = any(not on and c for on, _, c in runs)
        else:
            # A brief survival is not verification. Count time on the exact
            # proposed final configuration since its most recent failure.
            exposure = 0.0
            for on, t, c in reversed(runs):
                if on == rest:
                    if c:
                        break
                    exposure += t
            a, b = self.model._rate_post(best, True)
            self.verification_probability = _p_crash(a, b, exposure)
            verified = bool(runs and runs[-1][0] == rest and not runs[-1][2]
                            and self.verification_probability >= 0.80)
        if p >= self.threshold and verified:
            self.pending = None
            self.outcome = ("fixed", []) if best[0] == "none" else (
                "found", [[self._by_key[i] for i in g] for g in groups_of(best)])
        elif len(self.answers) >= self.max_tests:
            self.pending, self.outcome = None, ("inconclusive", [])
        elif p >= self.threshold:
            self.phase_name = "fixed" if best[0] == "none" else "verify"
            self.pending = [i for i in self.items if item_key(i) in rest] if best[0] != "none" else []
            self._planned_seconds = min(14400., max(30., self.model.play_time([], post=[(best, 1.0)]) - (exposure if best[0] != "none" else 0.)))
            self.expected_gain = self.model.info_gain([item_key(i) for i in self.pending], self._planned_seconds, post)
        else:
            on, duration, gain, _risk = self.model.best_plan(self.test_seconds, post)
            self._planned_seconds = duration
            self.expected_gain = gain
            self.phase_name = "fixed" if not on else "search"
            self.pending = [i for i in self.items if item_key(i) in on]

    # ── CrashSearch interface ──
    @property
    def done(self) -> bool:
        return self.outcome is not None

    @property
    def tests_done(self) -> int:
        return len(self.answers)

    @property
    def phase(self) -> str:
        return "done" if self.done else self.phase_name

    @property
    def culprits(self) -> list:
        return [i for g in (self.outcome[1] if self.done else []) for i in g]

    @property
    def found_so_far(self) -> list:
        best, p = self.post[0]
        return [[self._by_key[i] for i in g] for g in groups_of(best)] if p >= 0.5 else []

    @property
    def candidates(self) -> list:
        m = self.model.marginals(self.post)
        return [i for i in sorted(self.items, key=lambda i: -m[item_key(i)]) if m[item_key(i)] >= 0.05]

    def estimate_left(self) -> int:
        """Tests still needed if one setting is the cause: halve the plausible suspects, then verify."""
        if self.done:
            return 0
        if self.phase_name == "verify":
            return min(self.max_tests-self.tests_done, 1)
        n = len(self.candidates)
        return min((math.ceil(math.log2(n)) if n > 1 else 0) + 1, self.max_tests - self.tests_done)

    def answer(self, fails: bool, seconds: float | None = None):
        if self.done:
            raise RuntimeError("search finished")
        t = float(seconds) if seconds is not None else self.play_time()
        self.model.add([item_key(i) for i in self.pending], t, fails)
        self.answers.append(fails)
        self.durations.append(t)
        self.phase_name = "search"
        self._plan()

    def undo(self):
        if not self.answers:
            return
        undone = self.observations[-1][0]
        runs = list(self.observations[:-1])
        self._restart()
        for on, t, a in runs:
            self.model.add(on, t, a)
            self.answers.append(a); self.durations.append(t)
        self._plan()
        if not self.done and frozenset(item_key(i) for i in self.pending) != undone:
            self.pending = [i for i in self.items if item_key(i) in undone]
            self.phase_name = "fixed" if not undone else "search"
            self.expected_gain = self.model.info_gain(undone, self._planned_seconds, self.post)

    def play_time(self) -> float:
        return self._planned_seconds

    def predicted_risk(self) -> float:
        return self.model.crash_probability(self.pending or [], self.play_time(), self.post)


# ─────────────────────────────── Xid ↔ setting links from runs ───────────────────────────────
def associate(items: Sequence, runs: Iterable[dict], *, prior: dict | None = None,
              crash_seconds: float | None = None, min_crashes: int = 1) -> dict:
    """Per symptom, how strongly each setting is linked to it.

    runs: {"on": [items], "seconds": float, "crashed": bool, "xids": [codes seen in that run]}.
    The symptom of a crashed run is its root Xid ("no-xid" when the kernel logged none). For each
    symptom a CrashModel counts only that symptom's crashes; other crashes end the run early
    (censored). Different Xids from one configuration often have different causes, and this
    separates them. -> {symptom: [(item, probability), …] most likely first, plus ("none", p)}."""
    runs = list(runs)
    out = {}
    symptoms = {}
    for r in runs:
        if r.get("crashed"):
            s = _symptom(r.get("xids", []))
            symptoms[s] = symptoms.get(s, 0) + 1
    for s, n in symptoms.items():
        if n < min_crashes:
            continue
        xid = int(s) if s.isdigit() else None
        pri = dict(prior or {})
        if xid is not None:
            for k, (w, _) in prior_weights(xid, (), items).items():
                pri[k] = pri.get(k, 0.0) + w
        m = CrashModel(items, pri, crash_seconds=crash_seconds, triple_limit=10, higher_limit=8, mixed_limit=8, max_order=8)
        for r in runs:
            m.add(r["on"], r["seconds"], bool(r.get("crashed")) and _symptom(r.get("xids", [])) == s)
        marg = m.marginals()
        out[s] = sorted(marg.items(), key=lambda kv: -kv[1])
    return out


def _symptom(xids) -> str:
    codes = [int(x) for x in xids]
    root = next((x for x in codes if not info(x).get("secondary")), None)
    return str(root) if root is not None else "secondary-only" if codes else "no-xid"
