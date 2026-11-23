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
   two settings only together (AND) / two independent causes (OR). Under a hypothesis the run
   crashes as a Poisson process: rate λ while the condition holds, a small background rate ε
   otherwise; both rates have Gamma priors, so every marginal likelihood is closed form. Because
   evidence is weighted by play time, a crash that needs 20 minutes is not ruled out by a 3-minute
   "works", and one wrong answer lowers confidence instead of derailing the search (ddmin assumes
   a perfect oracle). `best_test` picks the next configuration by expected information gain and
   `play_time` says how long a test must run to be conclusive. `AdaptiveSearch` wraps it in the
   CrashSearch interface used by the Crash Lab.

`associate` applies the model per symptom (root Xid of each crashed test), which gives the
Xid ↔ setting links measured on the user's own runs.
"""
from __future__ import annotations

import json
import math
import re
from itertools import combinations
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
_TS_ISO = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:[.,]\d+)?)")
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
    """Group events into failures. Without timestamps all events form one incident."""
    groups: list[list] = []
    for ev in events:
        last = groups[-1][-1] if groups else None
        if last is not None and (ev["ts"] is None or last["ts"] is None or ev["ts"] - last["ts"] <= gap):
            groups[-1].append(ev)
        else:
            groups.append([ev])
    return [_incident(g) for g in groups]


def _incident(evs: list) -> dict:
    codes = list(dict.fromkeys(e["xid"] for e in evs))
    root = next((e for e in evs if not info(e["xid"]).get("secondary")), evs[0])
    scopes = [info(c).get("scope", "unknown") for c in codes]
    scope = max(scopes, key=lambda s: _SCOPE_RANK.get(s, 1))
    if scope == "info":
        scope = "app"           # only follow-up codes (a lone Xid 45): nothing points elsewhere
    cascades = [c for c in DB.get("cascades", []) if _subsequence(c["sequence"], codes)]
    subtypes = list(dict.fromkeys(s for e in evs if e["xid"] == root["xid"] for s in e["subtypes"]))
    locs = {e["sm"][:2] for e in evs if e.get("sm")}
    return {"codes": codes, "root": root, "events": evs, "count": len(evs), "scope": scope,
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
    locs = [{e["sm"][:2] for e in i["events"] if e.get("sm")} for i in incs if i["root"]["xid"] == 13]
    locs = [s for s in locs if s]
    if len(locs) >= 2 and set.intersection(*locs) and all(len(s) == 1 for s in locs):
        gpc, tpc = next(iter(set.intersection(*locs)))
        return (f"Every Xid 13 hit the same unit (GPC {gpc}, TPC {tpc}). NVIDIA treats a repeating "
                "GPC/TPC as a possible hardware fault: stress-test the GPU before bisecting settings.")
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
    weights = prior_weights(root, primary["subtypes"], pool, arch=arch, driver=driver)
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
    out = [f"- Root: Xid {p['root']['xid']} — {e['title']} ({e['mnemonic']}); scope: {p['scope']}"
           + (f"; process {code(p['process'])}" if p.get("process") else "")
           + (f"; driver {d['driver']}" if d.get("driver") else ""),
           f"  {e['summary']}",
           f"  NVIDIA catalog action: {e.get('nvidia', '—')}"]
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
    return 1.0 - math.exp(a * (math.log(b) - math.log(b + t)))


def _h2(p: float) -> float:
    return 0.0 if p <= 0.0 or p >= 1.0 else -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


class CrashModel:
    """Posterior over culprit hypotheses from timed test runs.

    Hypotheses: ("none",) nothing tested is needed (the crash comes from elsewhere);
    ("one", i); ("and", i, j) only together; ("or", i, j) two independent causes.
    `crash_seconds` (time until the crash in a failing run) sets the prior crash rate."""

    def __init__(self, items: Sequence, prior: dict | None = None, *, crash_seconds: float | None = None,
                 p_none: float = 0.10, p_and: float = 0.12, p_or: float = 0.10, pair_limit: int = 40,
                 background_hours: float = 20.0):
        self.items = [item_key(i) for i in items]
        prior = prior or {}
        mean = max(float(crash_seconds or 600.0), 10.0)
        self.rate = (1.0, mean)                          # Gamma(shape, rate): mean 1 crash per `mean` s
        self.bg = (0.5, 0.5 * background_hours * 3600)   # rare crashes with the culprit off
        w = {i: math.exp(float(prior.get(i, 0.0))) for i in self.items}
        ranked = sorted(self.items, key=lambda i: -w[i])[:pair_limit]
        pairs = list(combinations(ranked, 2))
        hyps: list = [("none",)] + [("one", i) for i in self.items]
        logp = [math.log(p_none)]
        z1 = sum(w.values()) or 1.0
        logp += [math.log((1 - p_none - (p_and + p_or if pairs else 0)) * w[i] / z1) for i in self.items]
        if pairs:
            zp = sum(w[a] * w[b] for a, b in pairs)
            for kind, mass in (("and", p_and), ("or", p_or)):
                hyps += [(kind, a, b) for a, b in pairs]
                logp += [math.log(mass * w[a] * w[b] / zp) for a, b in pairs]
        self.hyps, self.log_prior = hyps, logp
        self.runs: list = []            # (frozenset of item keys on, seconds, crashed)
        self._cache: dict = {}

    # ── data ──
    def add(self, on: Iterable, seconds: float, crashed: bool):
        self.runs.append((frozenset(item_key(i) for i in on), max(float(seconds), 1.0), bool(crashed)))
        self._cache.clear()

    @staticmethod
    def _holds(h, on: frozenset) -> bool:
        kind = h[0]
        return (True if kind == "none" else h[1] in on if kind == "one" else
                h[1] in on and h[2] in on if kind == "and" else h[1] in on or h[2] in on)

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
        logs = []
        for h, lp in zip(self.hyps, self.log_prior):
            k1, t1, k0, t0 = self._stats(h)
            logs.append(lp + _log_marginal(int(k1), t1, *self.rate) + _log_marginal(int(k0), t0, *self.bg))
        top = max(logs)
        ps = [math.exp(v - top) for v in logs]
        z = sum(ps)
        return sorted(((h, p / z) for h, p in zip(self.hyps, ps)), key=lambda hp: -hp[1])

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

    def info_gain(self, on: Iterable, seconds: float, post=None) -> float:
        """Expected bits learned by playing `seconds` with exactly `on` switched on."""
        on = frozenset(item_key(i) for i in on)
        post = post or self.posterior()
        crash = cond = 0.0
        for h, p in post:
            if p < 1e-6:
                continue
            q = _p_crash(*self._rate_post(h, self._holds(h, on)), seconds)
            crash += p * q
            cond += p * _h2(q)
        return _h2(crash) - cond

    def candidates(self, post=None) -> list:
        """Configurations worth considering: prefixes / suffixes of the items ranked by
        suspicion, every single item, the baseline and 'everything but one'."""
        post = post or self.posterior()
        m = self.marginals(post)
        ranked = sorted(self.items, key=lambda i: -m[i])
        cands = {frozenset()}
        for n in range(1, len(ranked) + 1):
            cands.add(frozenset(ranked[:n]))
            cands.add(frozenset(ranked[n:]))
        for i in ranked[:12]:
            cands.add(frozenset([i]))
            cands.add(frozenset(self.items) - {i})
        return [c for c in cands]

    def best_test(self, seconds: float, post=None) -> tuple[frozenset, float]:
        post = post or self.posterior()
        scored = [(self.info_gain(c, seconds, post), -len(c), sorted(c), c) for c in self.candidates(post)]
        gain, _, _, best = max(scored)
        return best, gain

    def play_time(self, on: Iterable, confidence: float = 0.85, post=None) -> float:
        """Seconds to play so that, if the most likely cause is on, the crash shows with
        `confidence`: solved from the posterior-predictive (Lomax) distribution."""
        post = post or self.posterior()
        on = frozenset(item_key(i) for i in on)
        h = next((h for h, _ in post if h[0] != "none" and self._holds(h, on)), None)
        a, b = self._rate_post(h or ("none",), True)
        return b * ((1.0 - confidence) ** (-1.0 / a) - 1.0)


def groups_of(h) -> list:
    """Hypothesis -> culprit groups as the Crash Lab shows them."""
    return ([] if h[0] == "none" else [[h[1]]] if h[0] == "one" else
            [[h[1], h[2]]] if h[0] == "and" else [[h[1]], [h[2]]])


def describe(h) -> str:
    return {"none": "none of the tested settings", "one": "{0}", "and": "{0} + {1} (only together)",
            "or": "{0} or {1} (independent causes)"}[h[0]].format(*h[1:])


# ─────────────────────────────── adaptive search ───────────────────────────────
class AdaptiveSearch:
    """Drop-in for bisect_search.CrashSearch, driven by CrashModel.

    The run that brought the user here (everything on, crashed after `crash_seconds`) is the
    first observation. Each step tests the configuration with the highest expected information
    gain; when one hypothesis reaches `threshold`, everything except its culprits is tested once
    more ("verify") before the result is final. Items are tuples, as in CrashSearch."""

    def __init__(self, items: Sequence, answers: Iterable[bool] = (), durations: Iterable[float] = (), *,
                 prior: dict | None = None, crash_seconds: float | None = None,
                 threshold: float = 0.95, max_tests: int = 40):
        self.items = list(items)
        self._by_key = {item_key(i): i for i in self.items}
        self.prior, self.crash_seconds = dict(prior or {}), crash_seconds
        self.threshold, self.max_tests = threshold, max_tests
        self._restart()
        durations = list(durations)
        for n, a in enumerate(answers):
            self.answer(a, durations[n] if n < len(durations) else None)

    def _restart(self):
        self.answers: list = []
        self.durations: list = []
        self.model = CrashModel(self.items, self.prior, crash_seconds=self.crash_seconds)
        if self.items:
            self.model.add(self._by_key, self.crash_seconds or self.model.rate[1], True)
        self.outcome = None
        self.phase_name = "search"
        self._plan()

    @property
    def test_seconds(self) -> float:
        return float(self.crash_seconds or self.model.rate[1])

    def _plan(self):
        post = self.model.posterior()
        self.post = post
        best, p = post[0]
        rest = frozenset(self._by_key) - {i for g in groups_of(best) for i in g}
        verified = any(on == rest and not c for on, _, c in self.model.runs[1:])
        if not self.items:
            self.pending, self.outcome = None, ("fixed", [])
        elif p >= self.threshold and (verified or best[0] == "none"):
            self.pending = None
            self.outcome = ("fixed", []) if best[0] == "none" else (
                "found", [[self._by_key[i] for i in g] for g in groups_of(best)])
        elif len(self.answers) >= self.max_tests:
            self.pending = None
            self.outcome = ("fixed", []) if best[0] == "none" else (
                "found", [[self._by_key[i] for i in g] for g in groups_of(best)])
        elif p >= self.threshold:
            self.phase_name = "verify"
            culprits = {i for g in groups_of(best) for i in g}
            self.pending = [i for i in self.items if item_key(i) not in culprits]
        else:
            on, _gain = self.model.best_test(self.test_seconds, post)
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
            return 1
        n = len(self.candidates)
        return min((math.ceil(math.log2(n)) if n > 1 else 0) + 1, self.max_tests - self.tests_done)

    def answer(self, fails: bool, seconds: float | None = None):
        if self.done:
            raise RuntimeError("search finished")
        t = float(seconds) if seconds else self.test_seconds
        self.answers.append(bool(fails))
        self.durations.append(t)
        self.model.add([item_key(i) for i in self.pending], t, fails)
        self.phase_name = "search"
        self._plan()

    def undo(self):
        answers, durations = self.answers[:-1], self.durations[:-1]
        self._restart()
        for a, t in zip(answers, durations):
            self.answer(a, t)


    def play_time(self) -> float:
        return self.model.play_time([item_key(i) for i in self.pending or []], post=self.post)


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
        m = CrashModel(items, pri, crash_seconds=crash_seconds)
        for r in runs:
            m.add(r["on"], r["seconds"], bool(r.get("crashed")) and _symptom(r.get("xids", [])) == s)
        marg = m.marginals()
        out[s] = sorted(marg.items(), key=lambda kv: -kv[1])
    return out


def _symptom(xids) -> str:
    codes = [int(x) for x in xids]
    root = next((x for x in codes if not info(x).get("secondary")), codes[0] if codes else None)
    return str(root) if root is not None else "no-xid"
