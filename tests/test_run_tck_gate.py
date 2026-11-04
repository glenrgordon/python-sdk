"""The v2 MANDATORY gate in `scripts/run_tck.py`, and its two pinned alpha.5 auth skips.

`scripts/run_tck.py` drives an external TCK checkout, so these tests call `check_mandatory`
directly with synthetic requirement rows instead of running a suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.run_tck import V2_PINNED_SKIPS, check_mandatory

NO_AUTH_CAPABILITIES = {"session": {}}


def skipped(requirement_id: str, *, nodeid: str, reason: str) -> dict[str, Any]:
    return {
        "id": requirement_id,
        "tier": "MANDATORY",
        "status": "SKIPPED",
        "tests": [{"nodeid": nodeid, "status": "SKIPPED", "message": repr(("/tck/test_authentication.py", 1, reason))}],
    }


def pinned_row(requirement_id: str) -> dict[str, Any]:
    nodeid, reason = V2_PINNED_SKIPS[requirement_id]
    return skipped(requirement_id, nodeid=nodeid, reason=reason)


def report(capabilities: Any = NO_AUTH_CAPABILITIES) -> dict[str, Any]:
    return {"agent_capabilities": capabilities}


def test_both_pinned_auth_skips_are_accepted_for_an_unauthenticated_v2_fixture() -> None:
    rows = [pinned_row("ACP-AUTH-206"), pinned_row("ACP-AUTH-207")]
    assert check_mandatory(rows, 2, report()) == []


@pytest.mark.parametrize("requirement_id", ["ACP-AUTH-206", "ACP-AUTH-207"])
def test_pin_requires_the_exact_skip_reason(requirement_id: str) -> None:
    nodeid, reason = V2_PINNED_SKIPS[requirement_id]
    row = skipped(requirement_id, nodeid=nodeid, reason=f"{reason} -- but for a different reason")
    assert check_mandatory([row], 2, report()) != []


@pytest.mark.parametrize("requirement_id", ["ACP-AUTH-206", "ACP-AUTH-207"])
def test_pin_requires_a_fixture_that_advertises_no_auth(requirement_id: str) -> None:
    row = pinned_row(requirement_id)
    assert check_mandatory([row], 2, report({"session": {}, "auth": {"terminal": {}}})) != []


@pytest.mark.parametrize("capabilities", [{}, {"auth": None}])
def test_pin_accepts_capabilities_that_report_no_auth(capabilities: Any) -> None:
    rows = [pinned_row("ACP-AUTH-206"), pinned_row("ACP-AUTH-207")]
    assert check_mandatory(rows, 2, report(capabilities)) == []


@pytest.mark.parametrize(
    "capabilities",
    [None, [], "unexpected"],
    ids=["missing", "wrong-type", "string"],
)
def test_pin_fails_closed_on_unverifiable_capabilities(capabilities: Any) -> None:
    rows = [pinned_row("ACP-AUTH-206"), pinned_row("ACP-AUTH-207")]
    assert check_mandatory(rows, 2, report(capabilities)) != []


def test_unpinned_mandatory_skip_still_fails_v2() -> None:
    row = skipped("ACP-AUTH-202", nodeid="test_authentication.py::test_other", reason="Skipped: anything")
    assert check_mandatory([row], 2, report()) != []


def test_pinned_skip_fails_on_the_v1_lane() -> None:
    assert check_mandatory([pinned_row("ACP-AUTH-206")], 1, report()) != []


def test_fail_row_with_pinned_id_reason_and_nodeid_is_rejected() -> None:
    """A FAIL row carrying a pinned id, node id, and reason must not unlock the gate.

    `pinned_auth_skip` also requires the requirement's own status to be SKIPPED, so a corrupted
    row whose tests all claim SKIPPED but whose aggregate verdict is FAIL stays gated.
    """
    requirement_id = "ACP-AUTH-206"
    nodeid, reason = V2_PINNED_SKIPS[requirement_id]
    row = pinned_row(requirement_id) | {"status": "FAIL"}
    errors = check_mandatory([row], 2, report())
    assert f"v2: {requirement_id} is FAIL, expected PASS" in errors
    assert f"v2: {nodeid} is SKIPPED, expected PASS" in errors


def test_mandatory_fail_and_not_tested_still_fail_v2() -> None:
    failed = {"id": "ACP-BATCH-201", "tier": "MANDATORY", "status": "FAIL", "tests": []}
    not_tested = {"id": "ACP-INIT-201", "tier": "MANDATORY", "status": "NOT_TESTED", "tests": []}
    assert check_mandatory([failed, not_tested], 2, report()) != []


def test_advisory_and_other_tiers_are_never_gated() -> None:
    rows = [
        {"id": "ACP-AUTH-201", "tier": "ADVISORY", "status": "FAIL", "tests": []},
        {"id": "ACP-SESSION-004", "tier": "ADVISORY", "status": "SKIPPED", "tests": []},
        {"id": "ACP-AUTH-203", "tier": "CAPABILITY", "status": "SKIPPED", "tests": []},
    ]
    assert check_mandatory(rows, 2, report()) == []
