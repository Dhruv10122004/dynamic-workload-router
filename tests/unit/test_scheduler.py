"""scheduler/ (plan §7): every eligibility reason, policy ordering, structural reject, host-busy (AC-3.1)."""
from dataclasses import replace

import pytest

from scheduler import REASONS, HostBusy, JobView, WorkerView, eligibility, make_policy, structurally_impossible
from scheduler.eligibility import reason_for
from scheduler.policies import POLICIES


def W(wid="edge-1", tier="edge", **kw) -> WorkerView:
    base = dict(worker_id=wid, tier=tier, fresh=True, draining=False, host_busy=False,
                supported_types=frozenset({"cpu_hash", "data_digest", "mem_touch"}), slots_total=2,
                slots_used=0, mem_budget_mb=1536, mem_committed_mb=0, mem_available_mb=4000,
                mem_floor_mb=512)
    return WorkerView(**{**base, **kw})


def J(mem=40, type="cpu_hash", placement="auto") -> JobView:
    return JobView(job_id="j1", type=type, mem_mb=mem, placement=placement)


EDGE, FOG = W(), W("fog-1", "fog")


# ------------------------------------------------------------------ eligibility reasons

@pytest.mark.parametrize("worker, job, reason", [
    (W(fresh=False), J(), "stale"),
    (W(draining=True), J(), "draining"),
    (W(supported_types=frozenset({"mem_touch"})), J(), "unsupported_type"),
    (W(slots_used=2), J(), "no_free_slot"),
    (W(mem_committed_mb=1500), J(mem=40), "mem_budget"),
    (W(mem_available_mb=551), J(mem=40), "mem_floor"),
    (W(host_busy=True), J(), "host_busy"),
    (W(), J(placement="fog-1"), "not_requested"),
])
def test_each_reason(worker, job, reason):
    assert reason_for(job, worker) == reason


def test_every_reason_is_covered_and_ordered():
    # A worker failing every check reports the first in plan order; fixing each in turn
    # exposes the next one, so the order of REASONS is the order of the checks.
    w = W(fresh=False, draining=True, supported_types=frozenset(), slots_used=2, mem_committed_mb=1536,
          mem_available_mb=0, host_busy=True)
    job, held = J(placement="other"), frozenset({w.worker_id})
    fixes = [dict(fresh=True), dict(draining=False), dict(supported_types=frozenset({"cpu_hash"})),
             dict(slots_used=0), dict(mem_committed_mb=0), dict(mem_available_mb=4000), dict(host_busy=False)]
    seen = [reason_for(job, w, held=held)]
    for f in fixes:
        w = replace(w, **f)
        seen.append(reason_for(job, w, held=held))
    seen.append(reason_for(J(placement=w.worker_id), w, held=held))
    assert tuple(seen) == REASONS
    assert reason_for(J(placement=w.worker_id), w) is None


def test_aged_job_holds_workers_from_later_jobs():
    p = make_policy("LocalFirst")
    d = p.decide(J(), [EDGE, FOG], held=frozenset({"edge-1"}))
    assert d.worker_id == "fog-1" and d.ineligible == {"edge-1": "reserved_for_aged"}
    d = p.decide(J(), [EDGE, FOG], held=frozenset({"edge-1", "fog-1"}))
    assert d.worker_id is None and not d.structural_reject


def test_boundaries_are_inclusive():
    assert reason_for(J(mem=36), W(mem_committed_mb=1500)) is None          # 1500 + 36 == budget
    assert reason_for(J(mem=40), W(mem_available_mb=552)) is None           # 552 - 40 == floor
    assert reason_for(J(), W(slots_used=1, slots_total=2)) is None


def test_host_busy_ignored_when_disabled():
    assert reason_for(J(), W(host_busy=True), host_busy_enabled=False) is None


def test_eligibility_splits_and_sorts():
    eligible, ineligible = eligibility(J(), [W("z", "fog"), W("a"), W("m", slots_used=2)])
    assert eligible == ("a", "z") and ineligible == {"m": "no_free_slot"}


# ------------------------------------------------------------------ policies

def test_local_first_prefers_edge_then_fog():
    p = make_policy("LocalFirst")
    assert p.decide(J(), [FOG, EDGE]).worker_id == "edge-1"
    d = p.decide(J(), [FOG, replace(EDGE, slots_used=2)])
    assert d.worker_id == "fog-1" and d.eligible == ("fog-1",) and d.ineligible == {"edge-1": "no_free_slot"}


def test_local_first_tie_break_by_worker_id():
    p = make_policy("LocalFirst")
    assert p.decide(J(), [W("edge-2"), W("edge-1")]).worker_id == "edge-1"
    assert p.decide(J(), [W("fog-b", "fog"), W("fog-a", "fog")]).worker_id == "fog-a"
    # tier beats name
    assert p.decide(J(), [W("a-fog", "fog"), W("z-edge", "edge")]).worker_id == "z-edge"


def test_local_first_fill_then_spill_then_pending():
    p = make_policy("LocalFirst")
    edge, fog = replace(EDGE, slots_total=2), replace(FOG, slots_total=1)
    placed = []
    for _ in range(4):
        d = p.decide(J(), [edge, fog])
        placed.append(d.worker_id)
        if d.worker_id == "edge-1":
            edge = replace(edge, slots_used=edge.slots_used + 1)
        elif d.worker_id == "fog-1":
            fog = replace(fog, slots_used=fog.slots_used + 1)
    assert placed == ["edge-1", "edge-1", "fog-1", None]
    assert not d.structural_reject and d.ineligible == {"edge-1": "no_free_slot", "fog-1": "no_free_slot"}


def test_edge_only_and_fog_only():
    assert make_policy("EdgeOnly").decide(J(), [FOG, EDGE]).worker_id == "edge-1"
    d = make_policy("EdgeOnly").decide(J(), [FOG, replace(EDGE, slots_used=2)])
    assert d.worker_id is None and not d.structural_reject and d.eligible == ("fog-1",)
    assert make_policy("FogOnly").decide(J(), [FOG, EDGE]).worker_id == "fog-1"
    assert make_policy("FogOnly").decide(J(), [EDGE]).worker_id is None


def test_round_robin_cycles_over_eligible():
    p = make_policy("RoundRobin")
    ws = [W("a"), W("b", "fog"), W("c", "fog")]
    assert [p.decide(J(), ws).worker_id for _ in range(4)] == ["a", "b", "c", "a"]
    # skips an ineligible worker, keeps cycling
    ws[1] = replace(ws[1], slots_used=2)
    assert [p.decide(J(), ws).worker_id for _ in range(3)] == ["c", "a", "c"]


def test_pinned_placement_with_policy():
    d = make_policy("LocalFirst").decide(J(placement="fog-1"), [EDGE, FOG])
    assert d.worker_id == "fog-1" and d.ineligible == {"edge-1": "not_requested"}


def test_unknown_policy():
    with pytest.raises(ValueError):
        make_policy("Nope")
    assert set(POLICIES) == {"LocalFirst", "EdgeOnly", "FogOnly", "RoundRobin"}


# ------------------------------------------------------------------ structural reject

@pytest.mark.parametrize("name", sorted(POLICIES))
def test_structural_reject_too_big_for_every_budget(name):
    d = make_policy(name).decide(J(mem=2000), [EDGE, FOG])
    assert d.structural_reject and d.worker_id is None
    assert d.ineligible == {"edge-1": "mem_budget", "fog-1": "mem_budget"}


def test_too_big_for_edge_only_goes_to_fog():
    small_edge = replace(EDGE, mem_budget_mb=512)
    d = make_policy("LocalFirst").decide(J(mem=600), [small_edge, FOG])
    assert d.worker_id == "fog-1" and d.ineligible == {"edge-1": "mem_budget"} and not d.structural_reject


def test_structural_reject_unsupported_everywhere():
    only_cpu = frozenset({"cpu_hash"})
    ws = [replace(EDGE, supported_types=only_cpu), replace(FOG, supported_types=only_cpu)]
    assert make_policy("LocalFirst").decide(J(type="mem_touch"), ws).structural_reject


def test_structural_reject_reports_permanent_reasons():
    # A stale worker that also lacks the type is reported as unsupported_type, not stale.
    ws = [replace(EDGE, fresh=False, supported_types=frozenset({"cpu_hash"})),
          replace(FOG, mem_budget_mb=100, host_busy=True)]
    d = make_policy("LocalFirst").decide(J(type="mem_touch", mem=300), ws)
    assert d.structural_reject and d.ineligible == {"edge-1": "unsupported_type", "fog-1": "mem_budget"}
    d = make_policy("LocalFirst").decide(J(mem=600, placement="edge-1"), [replace(EDGE, mem_budget_mb=512), FOG])
    assert d.structural_reject and d.ineligible == {"edge-1": "mem_budget", "fog-1": "not_requested"}


def test_structural_ignores_load_and_staleness():
    # Busy, stale, draining or full workers can still run it later: wait, don't reject.
    ws = [replace(EDGE, fresh=False), replace(FOG, slots_used=2, draining=True, host_busy=True)]
    assert not structurally_impossible(J(), ws)
    assert not structurally_impossible(J(), [])  # nobody registered yet: nothing known


def test_structural_for_pinned_job_considers_only_its_worker():
    small_edge = replace(EDGE, mem_budget_mb=512)
    assert structurally_impossible(J(mem=600, placement="edge-1"), [small_edge, FOG])
    assert not structurally_impossible(J(mem=600, placement="fog-1"), [small_edge, FOG])
    assert structurally_impossible(J(placement="ghost"), [EDGE])


# ------------------------------------------------------------------ host-busy hysteresis

def feed(hb, values):
    return [hb.add(v) for v in values]


def test_host_busy_needs_min_samples():
    hb = HostBusy()
    assert feed(hb, [100] * 4) == [False] * 4 and not hb.busy
    assert hb.add(100) and hb.busy


def test_host_busy_enter_and_exit():
    hb = HostBusy()
    feed(hb, [80] * 5)
    assert hb.busy
    # mean drifts down through the band: stays busy until mean <= 50
    changes = feed(hb, [10] * 10)
    assert hb.busy is False and changes.count(True) == 1
    exit_at = changes.index(True)
    # after 3 lows: (5*80 + 3*10)/8 = 53.75 (still busy); after 4: (5*80 + 4*10)/9 = 48.9 -> free
    assert exit_at == 3


def test_host_busy_threshold_values_inclusive():
    hb = HostBusy()
    feed(hb, [70] * 5)
    assert hb.busy
    hb2 = HostBusy()
    feed(hb2, [69.9] * 10)
    assert not hb2.busy
    hb3 = HostBusy(busy=True)
    hb3.samples.extend([50] * 9)
    assert hb3.add(50) and not hb3.busy        # mean == low -> free


def test_host_busy_no_flapping_in_band():
    hb = HostBusy()
    feed(hb, [90] * 5)
    assert hb.busy
    # oscillating input whose mean stays between 50 and 70: no change at all
    changes = feed(hb, [40, 80] * 20)
    assert not any(changes) and hb.busy
    hb2 = HostBusy()
    changes = feed(hb2, [40, 80] * 20)          # same input from free: stays free
    assert not any(changes) and not hb2.busy


def test_host_busy_window_is_last_ten():
    hb = HostBusy()
    feed(hb, [100] * 50)
    assert len(hb.samples) == 10
    hb.reset()
    assert not hb.busy and len(hb.samples) == 0


def test_host_busy_rejects_bad_thresholds():
    with pytest.raises(ValueError):
        HostBusy(high=50, low=50)
    with pytest.raises(ValueError):
        HostBusy(high=120, low=50)
