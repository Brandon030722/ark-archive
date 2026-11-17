"""Offline, shared environment fixtures; never an NL/model success predictor.

Each allowlisted check executes packaged JS or the actual runtime with synthetic
responses, temporary databases and MockTransport.  A case can reference a group
receipt, but that does not execute the case's language through a model.  This
module deliberately has no corpus loader, gold reader or provider constructor.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ENVIRONMENTS = (
    "ready", "paused", "offline", "timeout", "disconnect", "pending", "unknown",
    "quota", "prompt", "invalid_usage", "source_corrupt", "old_local_history",
    "quota_campaign", "prompt_irreducible", "keyboard", "input_boundary", "source_injection",
)
MODULES = ("operator", "recruitment", "garrison", "roguelike")
_P = "tests/test_nl_replay_environments.py::"
_R = "tests/test_runtime_runner.py::"
_O = "tests/test_prompt_observation_budget.py::"
# Checks are implementation behavior assertions, not expected corpus outcomes.
PYTEST_CHECKS = {
    "ready": [_R + "test_typed_query_direct_delivery_preserves_full_authoritative_answer"],
    "paused": [_R + "test_local_and_live_block_never_construct_provider_or_read_secret"],
    "timeout": [_P + "test_fixture_timeout_full_hold_no_retry"],
    "unknown": [_P + "test_fixture_timeout_full_hold_no_retry"],
    "quota": [_P + "test_fixture_stage40_original_input_quota"],
    "quota_campaign": [_P + "test_fixture_stage40_campaign_quota"],
    "invalid_usage": [_P + "test_fixture_invalid_usage_full_hold_no_retry"],
    "source_corrupt": [_P + "test_fixture_snapshot_byte_corruption_blocks_facts"],
    "prompt": [
        _O + "test_typed_two_rag_turns_reach_third_simulated_model_under_complete_wire_cap",
        _O + "test_each_discovery_entrypoint_fits_the_second_complete_wire_without_creating_facts",
    ],
    "prompt_irreducible": [
        _O + "test_irreducible_evidence_locator_stops_before_a_second_provider_reservation",
        _P + "test_fixture_protected_question_16384_pre_reservation",
    ],
    "input_boundary": [_P + "test_fixture_input_and_ninth_turn_stop_before_provider"],
    "source_injection": [
        _O + "test_source_instructions_are_only_a_prefix_and_never_leak_into_the_audit",
        _R + "test_search_and_records_never_supply_final_facts",
    ],
}
NODE_CHECKS = {
    "ready": [("web_model_entry_harness.cjs", "modelOnly")],
    "paused": [("web_model_entry_harness.cjs", "paused")],
    "offline": [("nl_replay_environment_harness.cjs", "offline")],
    "disconnect": [("web_model_entry_harness.cjs", "disconnectedPost")],
    "pending": [("nl_replay_environment_harness.cjs", "pending")],
    "unknown": [("web_model_entry_harness.cjs", "pendingUnknown")],
    "old_local_history": [("web_model_entry_harness.cjs", "historyAuthority")],
    "keyboard": [("nl_replay_environment_harness.cjs", "keyboard")],
    "input_boundary": [("nl_replay_environment_harness.cjs", "inputBoundary")],
}
LIMITATIONS = {
    "ready": ["Synthetic model output verifies delivery only; real model interpretation of each case is unverified."],
    "quota": ["Stage40 reserve gate and counter persistence exercised; no browser HTTP task service in this fixture."],
    "quota_campaign": ["Stage40 reserve gate exercised on a disposable archived epoch; not the live campaign."],
    "source_corrupt": ["Tampering exercised on a synthetic recruitment snapshot; other module source layouts are unverified here."],
    "source_injection": ["Trust boundary and uncertified-output rejection exercised; actual model resistance to instructions is unverified."],
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ARK_")
           and not any(s in k.upper() for s in ("API_KEY", "ACCESS_TOKEN", "DATABASE_URL"))}
    env.update(ARK_MODEL_LIVE_ENABLED="false", PYTHONDONTWRITEBYTECODE="1",
               NL_REPLAY_FULL_SUITE="0")
    return env


def _junit_cases(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    cases = []
    for case in ET.parse(path).iter("testcase"):
        classname = case.attrib.get("classname", "")
        node = classname.replace(".", "/") + ".py::" + case.attrib["name"]
        status = "passed"
        if case.find("failure") is not None or case.find("error") is not None:
            status = "failed"
        elif case.find("skipped") is not None:
            status = "unverified"
        cases.append({"nodeid": node, "status": status,
                      "elapsed_seconds": float(case.attrib.get("time", 0))})
    return cases


def run_environment_fixtures(output_dir, *, modules=MODULES, environments=ENVIRONMENTS,
                             project_root=None, timeout_seconds=240) -> dict:
    """Run each unique fixture once and write a new immutable evidence directory.

    ``modules`` is the requested association scope, not duplicate executions.
    Caller case questions/predictions/gold are intentionally not accepted.
    """
    root = Path(project_root or Path(__file__).resolve().parents[3]).resolve()
    requested = tuple(dict.fromkeys(environments))
    requested_modules = tuple(dict.fromkeys(modules))
    if set(requested) - set(ENVIRONMENTS) or set(requested_modules) - set(MODULES):
        raise ValueError("unknown_environment_or_module")
    registry_path = root / "evals/natural_language_v1/fixture-registry.json"
    registry = json.loads(registry_path.read_text())
    if any(key not in registry for key in requested):
        raise ValueError("fixture_registry_environment_missing")
    folder = Path(output_dir).resolve() / ("run-" + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    asset_paths = [root / "src/arknights_agent/web/app.js", Path(__file__).resolve(),
        root / "tests/test_nl_replay_environments.py", root / "tests/nl_replay_offline_plugin.py",
        root / "tests/nl_replay_environment_harness.cjs", root / "tests/web_model_entry_harness.cjs"]
    assets_before = {str(p.relative_to(root)): _sha(p) for p in asset_paths}
    check_results = {}
    selectors = sorted({s for e in requested for s in PYTEST_CHECKS.get(e, ())})
    commands = []
    if selectors:
        xml = folder / "runtime-fixtures.xml"
        command = [sys.executable, "-m", "pytest", "-q", "-p", "nl_replay_offline_plugin",
                   *selectors, "--junitxml=" + str(xml), "--basetemp=" + str(folder / "temporary")]
        env = _safe_env()
        env["PYTHONPATH"] = os.pathsep.join((str(root / "tests"), str(root / "src")))
        env["NL_REPLAY_NETWORK_AUDIT"] = str(folder / "network-guard.json")
        try:
            process = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True,
                                     timeout=timeout_seconds, check=False)
            (folder / "runtime-fixtures.log").write_text(process.stdout + process.stderr)
            returncode = process.returncode
        except subprocess.TimeoutExpired as exc:
            (folder / "runtime-fixtures.log").write_text("fixture_timeout\n" + str(exc.stdout or ""))
            returncode = -1
        cases = _junit_cases(xml)
        guard_path = folder / "network-guard.json"
        guard = json.loads(guard_path.read_text()) if guard_path.is_file() else None
        guard_ok = (isinstance(guard, dict) and guard.get("forbidden_attempts") == []
                    and guard.get("socket_and_http_transport_blocked") is True
                    and guard.get("real_ledger_connect_blocked") is True)
        for selector in selectors:
            matches = [c for c in cases if c["nodeid"] == selector
                       or c["nodeid"].startswith(selector + "[")]
            state = ("failed" if any(c["status"] == "failed" for c in matches) else
                     "passed" if matches and all(c["status"] == "passed" for c in matches)
                     else "unverified")
            if not guard_ok:
                state = "failed" if guard else "unverified"
            check_results["pytest:" + selector] = {"status": state, "selected_nodeid": selector,
                "actual_cases": matches, "junit": str(xml),
                "log": str(folder / "runtime-fixtures.log"), "offline_guard": str(guard_path)}
        commands.append({"command": command, "returncode": returncode, "kind": "pytest"})
    node = shutil.which("node")
    for filename, scenario in sorted({s for e in requested for s in NODE_CHECKS.get(e, ())}):
        ident = "node:" + filename + ":" + scenario
        if node is None:
            check_results[ident] = {"status": "unverified", "reason": "node_unavailable"}
            continue
        command = [node, str(root / "tests" / filename), scenario]
        try:
            process = subprocess.run(command, cwd=root, env=_safe_env(), capture_output=True,
                                     text=True, timeout=20, check=False)
            log = folder / (filename + "-" + scenario + ".log")
            log.write_text(process.stdout + process.stderr)
            receipt = json.loads(process.stdout) if process.returncode == 0 else None
            ok = (isinstance(receipt, dict) and receipt.get("passed") is True
                  and receipt.get("scenario") == scenario and receipt.get("network") == "mock-only")
            check_results[ident] = {"status": "passed" if ok else "failed", "receipt": receipt,
                                    "log": str(log)}
            commands.append({"command": command, "returncode": process.returncode, "kind": "node"})
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            check_results[ident] = {"status": "unverified", "reason": type(exc).__name__}
    groups = []
    for environment in requested:
        keys = ["pytest:" + s for s in PYTEST_CHECKS.get(environment, ())]
        keys += ["node:" + name + ":" + scenario for name, scenario in NODE_CHECKS.get(environment, ())]
        states = [check_results[k]["status"] for k in keys]
        state = ("failed" if "failed" in states else "passed" if states and all(
            s == "passed" for s in states) else "unverified")
        limitations = LIMITATIONS.get(environment, [])
        groups.append({"environment": environment, "fixture_id": registry[environment]["fixture_id"],
            "execution_status": state, "coverage_status": "partial" if limitations else "group_shared",
            "group_shared": True, "evidence_scope": "shared_fixture_group",
            "requested_module_associations": list(requested_modules),
            "actual_module_specific_coverage": ["operator"] if environment == "ready" else
                ["recruitment"] if environment == "source_corrupt" else [],
            "fixture_runtime_context_modules": ["recruitment"] if environment == "source_corrupt"
                else ["operator"] if PYTEST_CHECKS.get(environment) else [],
            "module_agnostic_protocol_checks": environment not in {"source_corrupt", "ready"},
            "check_ids": keys, "limitations": limitations,
            "nl_case_executed": False, "real_model_tested": False})
    report = {"schema_version": 1, "kind": "offline_environment_fixture_groups",
        "fixture_registry_sha256": _sha(registry_path), "groups": groups,
        "checks": check_results, "commands": commands,
        "unique_check_count": len(check_results), "unique_fixture_group_count": len(groups),
        "unique_selector_or_scenario_count": len(check_results),
        "actual_pytest_case_count": sum(len(item.get("actual_cases", []))
            for key, item in check_results.items() if key.startswith("pytest:")),
        "node_scenario_execution_count": sum(command["kind"] == "node" for command in commands),
        "executed_fixture_group_count": sum(g["execution_status"] != "unverified" for g in groups),
        "passed_fixture_group_count": sum(g["execution_status"] == "passed" for g in groups),
        "failed_fixture_group_count": sum(g["execution_status"] == "failed" for g in groups),
        "unverified_fixture_group_count": sum(g["execution_status"] == "unverified" for g in groups),
        "nl_cases_environment_end_to_end_executed": 0,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "paid_model_calls": 0, "actual_cost_cny": "0", "live_network_allowed": False,
        "disclaimer": "Shared synthetic environment receipts do not establish per-case NL or real-model correctness.",
        "universal_limitations": [
            "Offline Node DOM surrogate exercises packaged app.js; real-browser layout, native IME and event defaults are unverified.",
            "Scripted MockTransport responses do not test whether a real model interprets case language or resists prompt injection.",
            "Shared fixtures are not the module-by-environment-by-question cross product.",
        ],
        "evidence_dir": str(folder)}
    report["actual_behavior_execution_count"] = (report["actual_pytest_case_count"]
        + report["node_scenario_execution_count"])
    report["tested_assets_before"] = assets_before
    report["tested_assets_after"] = {str(p.relative_to(root)): _sha(p) for p in asset_paths}
    report["tested_assets_stable"] = assets_before == report["tested_assets_after"]
    path = folder / "environment-report.json"
    report["report_path"] = str(path)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def associate_environment_fixture(environment, module, report) -> dict:
    """Reference a shared receipt without converting it into a case pass."""
    if environment not in ENVIRONMENTS or module not in (*MODULES, "auto"):
        raise ValueError("unknown_environment_or_module")
    group = next((g for g in report.get("groups", []) if g["environment"] == environment), None)
    return {"environment": environment, "module": module,
        "fixture_id": group["fixture_id"] if group else None,
        "fixture_execution_status": group["execution_status"] if group else "unverified",
        "coverage_status": group["coverage_status"] if group else "unverified",
        "group_shared": True, "group_report": report.get("report_path"),
        "case_environment_end_to_end_executed": False, "case_environment_pass": None,
        "real_model_tested": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    report = run_environment_fixtures(args.output_dir)
    print(json.dumps({k: report[k] for k in ("report_path", "unique_fixture_group_count",
        "passed_fixture_group_count", "failed_fixture_group_count", "elapsed_seconds")}, ensure_ascii=False))
    return 1 if report["failed_fixture_group_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
