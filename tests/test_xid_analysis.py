"""drstool_core.xid_analysis: Xid parsing, incidents, knowledge-base priors, Bayesian search (no Qt).

Kernel lines are real reports from forums.developer.nvidia.com (see data/xid_db.json sources)."""
import json
import random
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drstool_core import crash_triage, xid_analysis as xa  # noqa: E402
from drstool_core.bisect_search import CrashSearch  # noqa: E402

X13 = ("[  775.063140] NVRM: Xid (PCI:0000:0a:00): 13, pid='<unknown>', name=<unknown>, "
       "Graphics SM Warp Exception on (GPC 3, TPC 1, SM 0): Illegal Instruction Parameter")
X31 = ("[ 9069.273788] NVRM: Xid (PCI:0000:01:00): 31, pid=72964, name=GameThread, channel 0x00000039, "
       "intr 00000000. MMU Fault: ENGINE GRAPHICS GPC0 GPCCLIENT_GCC faulted @ 0x0_00000000. "
       "Fault is of type FAULT_PDE ACCESS_TYPE_VIRT_READ")
X109 = ("[95925.650674] NVRM: Xid (PCI:0000:01:00): 109, pid=67885, name=Warframe.x64.ex, Ch 000000ce, "
        "errorString CTX SWITCH TIMEOUT, Info 0x1c067")
X119 = ("[95931.100000] NVRM: Xid (PCI:0000:01:00): 119, pid=1646, name=chrome, Timeout after 6s of waiting "
        "for RPC response from GPU0 GSP! Expected function 76 (GSP_RM_CONTROL) sequence 6501 (0xbeef0403 0x0).")
X69 = ("NVRM: Xid (PCI:0000:04:00): 69, pid=26613, name=tlou-ii-l.exe, Class Error: ChId 00c4, "
       "Class 0000c197, Offset 00002388, Data 07937878, ErrorCode 00000004")
X79 = "NVRM: Xid (PCI:0000:01:00): 79, pid='<unknown>', name=<unknown>, GPU has fallen off the bus."
LOADING = "[    5.1] NVRM: loading NVIDIA UNIX Open Kernel Module for x86_64  610.43.02  Release Build"


class KnowledgeBaseTests(unittest.TestCase):
    def test_database_is_consistent(self):
        db = json.loads((Path(xa.__file__).parent / "data" / "xid_db.json").read_text(encoding="utf-8"))
        for code, e in db["xids"].items():
            self.assertTrue(code.isdigit())
            self.assertIn(e["scope"], ("app", "system", "hardware", "info"), code)
            for m in e.get("mechanisms", {}):
                self.assertIn(m, db["mechanisms"], code)
            for s in e.get("subtypes", []):
                re.compile(s["regex"])
                self.assertTrue(set(s.get("mechanisms", {})) <= set(db["mechanisms"]), code)
            for ev in e.get("evidence", []):
                self.assertIn(ev["source"], db["sources"], code)
                for item in ev.get("items", []) + ev.get("mitigates", []):
                    self.assertRegex(item, r"^(env|vkd3d|settings|gs_on):")
        for c in db["cascades"]:
            self.assertIn(c["source"], db["sources"])
        self.assertTrue(all(s["url"].startswith("https://") for s in db["sources"].values()))

    def test_mechanisms_by_list_and_by_name(self):
        self.assertIn("descriptors", xa.mechanisms_of(("vkd3d", "descriptor_heap")))
        self.assertIn("latency", xa.mechanisms_of("env:SOME_FUTURE_REFLEX_TOGGLE"))     # name pattern
        self.assertEqual(xa.mechanisms_of("env:WINEDEBUG"), set())


class ParseTests(unittest.TestCase):
    def test_fields(self):
        e = xa.parse_event(X13)
        self.assertEqual((e["xid"], e["pci"], e["sm"], e["subtypes"], e["ts"]),
                         (13, "0000:0a:00", (3, 1, 0), ["illegal-instruction-parameter"], 775.06314))
        self.assertNotIn("pid", e)                                     # '<unknown>' is dropped
        e = xa.parse_event(X31)
        self.assertEqual(e["fault"], {"engine": "GRAPHICS", "client": "GPCCLIENT_GCC", "address": "0x0_00000000",
                                      "kind": "FAULT_PDE", "access": "ACCESS_TYPE_VIRT_READ"})
        self.assertEqual((e["process"], e["channel"]), ("GameThread", "00000039"))
        self.assertEqual(e["subtypes"], ["pde-null", "pde"])
        e = xa.parse_event(X109)
        self.assertEqual((e["error_string"], e["channel"]), ("CTX SWITCH TIMEOUT", "000000ce"))
        self.assertEqual(xa.parse_event(X119)["gsp_function"], "GSP_RM_CONTROL")
        e = xa.parse_event(X69)
        self.assertEqual((e["class"], e["error_code"], e["channel"]), ("0000c197", "00000004", "00c4"))
        self.assertIsNone(xa.parse_event("nothing here"))

    def test_driver_version(self):
        _events, driver = xa.parse_events([LOADING, X31])
        self.assertEqual(driver, "610.43.02")


class IncidentTests(unittest.TestCase):
    def test_cascade_and_scope(self):
        incs = xa.incidents(xa.parse_events([X109, X119])[0])
        self.assertEqual(len(incs), 1)                                  # 6 s apart: one failure
        self.assertEqual([c["id"] for c in incs[0]["cascades"]], ["gsp-collapse"])
        self.assertEqual(incs[0]["scope"], "system")                   # 119 decides: not a setting

    def test_secondary_codes_are_not_root(self):
        x45 = "[  776.0] NVRM: Xid (PCI:0000:0a:00): 45, pid=8420, name=Metro, Ch 000000c1"
        inc = xa.incidents(xa.parse_events([x45.replace("776.0", "775.0"), X13, x45])[0])[0]
        self.assertEqual(inc["root"]["xid"], 13)
        self.assertEqual(inc["signature"], "13:illegal-instruction-parameter")

    def test_gap_splits_incidents(self):
        late = X13.replace("775.063140", "999.000000")
        self.assertEqual(len(xa.incidents(xa.parse_events([X13, late])[0])), 2)

    def test_hardware_pattern(self):
        a = X13
        b = X13.replace("775.063140", "999.000000")
        self.assertIn("same unit", xa.hardware_pattern(xa.incidents(xa.parse_events([a, b])[0])))
        c = b.replace("GPC 3, TPC 1", "GPC 0, TPC 2")
        self.assertIsNone(xa.hardware_pattern(xa.incidents(xa.parse_events([a, c])[0])))


class DiagnoseTests(unittest.TestCase):
    def test_app_fault_ranks_linked_settings(self):
        items = [("env", "DXVK_HUD"), ("vkd3d", "descriptor_heap"), ("vkd3d", "dxr"), ("env", "PROTON_VKD3D_HEAP")]
        d = xa.diagnose([LOADING, X31], items=items)
        self.assertEqual(d["bisect"], "worth")
        self.assertEqual(d["driver"], "610.43.02")
        ranked = [k for k, _w, _why in d["suspects"]]
        self.assertEqual(ranked[:2], ["vkd3d:descriptor_heap", "env:PROTON_VKD3D_HEAP"])   # 610 report
        self.assertNotIn("env:DXVK_HUD", ranked)
        self.assertTrue(any("610" in why for why in d["suspects"][0][2]))

    def test_driver_branch_changes_weight(self):
        w610 = xa.prior_weights(31, (), ["vkd3d:descriptor_heap"], driver="610.43.02")["vkd3d:descriptor_heap"][0]
        w580 = xa.prior_weights(31, (), ["vkd3d:descriptor_heap"], driver="580.95.05")["vkd3d:descriptor_heap"][0]
        self.assertGreater(w610, w580)

    def test_system_fault_discourages_search(self):
        d = xa.diagnose([X79])
        self.assertEqual(d["bisect"], "unlikely")
        self.assertTrue(any("ASPM" in r for r in d["remedies"]))
        self.assertIn("unlikely to help", "\n".join(xa.format_diagnosis(d)))

    def test_pascal_xid69_mitigation(self):
        m = xa.mitigations(69, arch="GP102")
        self.assertEqual(m[0]["item"], "env:VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer")
        self.assertGreater(m[0]["score"], xa.mitigations(69, arch="AD102")[0]["score"])

    def test_unknown_code(self):
        d = xa.diagnose(["NVRM: Xid (PCI:0000:01:00): 999, something"])
        self.assertIn("not in the knowledge base", d["primary"] and xa.info(999)["title"])

    def test_triage_carries_the_diagnosis(self):
        r = crash_triage.triage([LOADING, X31, "err: VK_ERROR_DEVICE_LOST"])
        self.assertEqual(r["diagnosis"]["primary"]["root"]["xid"], 31)
        self.assertEqual(crash_triage.xid_hint(r), {"xid": 31, "subtypes": ["pde-null", "pde"], "driver": "610.43.02"})
        report = crash_triage.format_report(r)
        self.assertIn("── Xid analysis ──", report)
        self.assertIn("forums.developer.nvidia.com", report)
        self.assertIn("vkd3d:descriptor_heap", crash_triage.suggest_suspects(r))
        self.assertNotIn("Test first", report)                          # generic KB links stay in their section


def flaky_oracle(causes, mean, seed):
    """Game that crashes after an exponential time (mean `mean` s) while a cause group is on."""
    rnd = random.Random(seed)

    def run(on, seconds):
        active = any(all(i in on for i in g) for g in causes)
        t = rnd.expovariate(1 / mean) if active else float("inf")
        return t < seconds, min(t, seconds)
    return run


def drive(s, oracle, *, seconds=None, flip=None, limit=60):
    for n in range(limit):
        if s.done:
            return n
        fails, t = oracle(set(s.pending), seconds or s.play_time())
        s.answer(fails != (n == flip), t)
    raise AssertionError("search did not finish")


ITEMS = [("env", f"V{i}") for i in range(12)]


class ModelTests(unittest.TestCase):
    def test_short_works_is_weak_evidence(self):
        m = xa.CrashModel(["a", "b"], crash_seconds=600)
        m.add(["a", "b"], 600, True)
        m.add(["a"], 30, False)                                         # 30 s says little about "a"
        weak = m.marginals()["a"]
        m2 = xa.CrashModel(["a", "b"], crash_seconds=600)
        m2.add(["a", "b"], 600, True)
        m2.add(["a"], 3600, False)                                      # an hour does
        self.assertGreater(weak, m2.marginals()["a"] + 0.2)

    def test_information_gain_prefers_splitting(self):
        m = xa.CrashModel(["a", "b", "c", "d"], crash_seconds=120)
        m.add(["a", "b", "c", "d"], 120, True)
        self.assertGreater(m.info_gain(["a", "b"], 600), m.info_gain(["a", "b", "c", "d"], 600))

    def test_play_time_shrinks_with_evidence(self):
        m = xa.CrashModel(["a"], crash_seconds=300)
        before = m.play_time(["a"])
        for _ in range(4):
            m.add(["a"], 300, True)
        self.assertLess(m.play_time(["a"]), before)


class AdaptiveSearchTests(unittest.TestCase):
    def test_finds_single_pair_and_or_causes(self):
        for causes in ([[ITEMS[7]]], [[ITEMS[2], ITEMS[9]]], [[ITEMS[3]], [ITEMS[10]]]):
            for seed in range(3):
                s = xa.AdaptiveSearch(ITEMS, crash_seconds=300)
                drive(s, flaky_oracle(causes, 300, seed))
                self.assertEqual(sorted(s.culprits), sorted(i for g in causes for i in g), (causes, seed))

    def test_survives_a_wrong_answer_where_ddmin_cannot(self):
        oracle = flaky_oracle([[ITEMS[7]]], 60, 0)
        s = xa.AdaptiveSearch(ITEMS, crash_seconds=60)
        drive(s, oracle, flip=1)
        self.assertEqual(s.culprits, [ITEMS[7]])
        d = CrashSearch(ITEMS)
        for n in range(60):
            if d.done:
                break
            d.answer((ITEMS[7] in d.pending) != (n == 1))
        self.assertNotEqual(d.culprits, [ITEMS[7]])

    def test_cause_outside_the_settings(self):
        rnd = random.Random(1)
        s = xa.AdaptiveSearch(ITEMS[:6], crash_seconds=120)
        for _ in range(40):
            if s.done:
                break
            t = rnd.expovariate(1 / 120)
            s.answer(t < s.play_time(), min(t, s.play_time()))
        self.assertEqual(s.outcome, ("fixed", []))

    def test_verified_before_done_and_replay(self):
        s = xa.AdaptiveSearch(ITEMS, crash_seconds=200)
        oracle = flaky_oracle([[ITEMS[4]]], 200, 2)
        while not s.done:
            fails, t = oracle(set(s.pending), s.play_time())
            s.answer(fails, t)
        rest = frozenset(xa.item_key(i) for i in ITEMS if i != ITEMS[4])
        self.assertIn((rest, False), [(on, c) for on, _t, c in s.model.runs])    # everything else worked
        again = xa.AdaptiveSearch(ITEMS, s.answers, s.durations, crash_seconds=200)
        self.assertEqual(again.outcome, s.outcome)
        last = s.tests_done
        s.outcome = None
        s.undo()
        self.assertEqual(s.tests_done, last - 1)

    def test_knowledge_prior_shortens_the_search(self):
        tests = {}
        for prior in ({}, {"env:V9": 2.0}):
            total = 0
            for seed in range(4):
                s = xa.AdaptiveSearch(ITEMS, prior=prior, crash_seconds=300)
                total += drive(s, flaky_oracle([[ITEMS[9]]], 300, seed))
            tests[bool(prior)] = total
        self.assertLess(tests[True], tests[False])

    def test_empty(self):
        self.assertEqual(xa.AdaptiveSearch([]).outcome, ("fixed", []))


class AssociateTests(unittest.TestCase):
    def test_two_symptoms_two_causes(self):
        items = ["vkd3d:descriptor_heap", "vkd3d:dxr", "env:X"]
        runs = [{"on": items, "seconds": 300, "crashed": True, "xids": [31, 45]},
                {"on": ["vkd3d:dxr", "env:X"], "seconds": 280, "crashed": True, "xids": [13]},
                {"on": ["vkd3d:descriptor_heap", "env:X"], "seconds": 200, "crashed": True, "xids": [31]},
                {"on": ["env:X"], "seconds": 1800, "crashed": False, "xids": []},
                {"on": ["vkd3d:dxr"], "seconds": 250, "crashed": True, "xids": [13]},
                {"on": ["vkd3d:descriptor_heap"], "seconds": 240, "crashed": True, "xids": [31]}]
        links = xa.associate(items, runs, crash_seconds=300)
        self.assertEqual(links["31"][0][0], "vkd3d:descriptor_heap")    # 45 follows 31: symptom is 31
        self.assertEqual(links["13"][0][0], "vkd3d:dxr")


if __name__ == "__main__":
    unittest.main()
