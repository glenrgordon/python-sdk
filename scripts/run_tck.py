"""Run the pinned ACP TCK against the current checkout's v1 and v2 agent APIs."""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TCK_REVISIONS = {
    1: "b7f40fb3c003c45926547be5a1f7e2b374035815",
    2: "fa00ccdc13dfaa4b288a91877c1f3e468af4aa7d",
}
TCK_SCHEMA_REVISIONS = {
    1: "beac4ba96190018e9f6d82e148ff275084c5aa3e",
    2: "9af0e9f748db9f4cc4c410a7b212ead4f98ae78c",
}
CANCEL_PROMPT = "wait_for_cancel"

# The only v2 MANDATORY SKIPs the gate accepts: alpha.5's two conditional auth checks, which have
# nothing to inspect unless the agent advertises auth methods. Pinned per requirement id to the test
# that must skip and its exact reason, so any other MANDATORY SKIP fails CI.
V2_PINNED_SKIPS = {
    "ACP-AUTH-206": (
        "test_authentication.py::test_auth_method_type_is_a_defined_or_prefixed_value",
        "Skipped: agent advertises no authMethods",
    ),
    "ACP-AUTH-207": (
        "test_authentication.py::test_terminal_auth_method_descriptor_shape",
        "Skipped: agent advertises no type=='terminal' authMethods entry; nothing to check",
    ),
}


def executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} is not on PATH")
    return path


def git_revision(path: Path) -> str:
    return subprocess.check_output(  # noqa: S603
        [executable("git"), "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()


def sdk_python() -> Path:
    python = ROOT / ".venv" / "bin" / "python"
    if not python.exists():
        raise RuntimeError("SDK virtual environment is missing; run uv sync --frozen first")
    module = subprocess.check_output(  # noqa: S603
        [str(python), "-c", "import acp; print(acp.__file__)"], text=True
    ).strip()
    expected = ROOT / "src" / "acp" / "__init__.py"
    if Path(module).resolve() != expected.resolve():
        raise RuntimeError(f"Agent Python imports {module}, expected {expected}")
    return python


def run_suite(tck_dir: Path, output_dir: Path, python: Path, version: int, timeout: int) -> int:
    report = output_dir / f"v{version}.json"
    log = output_dir / f"v{version}.log"
    report.unlink(missing_ok=True)
    command = [
        executable("uv"),
        "--project",
        str(tck_dir),
        "run",
        "--locked",
        "acp-tck",
        "--protocol-version",
        str(version),
        "--cancel-prompt",
        CANCEL_PROMPT,
        "--timeout",
        str(timeout),
        "--test-timeout",
        str(max(45, timeout * 4)),
        "--report-json",
        str(report),
        "--",
        str(python),
        str(ROOT / "tests" / "tck" / f"agent_v{version}.py"),
    ]
    print(f"Running ACP v{version} TCK; detailed output: {log}", flush=True)
    with log.open("w") as output:
        return subprocess.run(  # noqa: S603
            command, cwd=ROOT, check=False, stdout=output, stderr=subprocess.STDOUT
        ).returncode


def advertises_auth(capabilities: Any) -> bool:
    """Whether the report's recorded initialize capabilities advertise agent-side auth support.

    Fail-closed: capabilities that are missing or not an object count as advertising auth, so an
    unverifiable fixture can never unlock the pinned skip.
    """
    return not (isinstance(capabilities, dict) and capabilities.get("auth") is None)


def skip_reason(message: Any) -> str | None:
    """The `Skipped: ...` reason inside a TCK test message.

    alpha.5 records a message as the repr of `(file, line, reason)`; fall back to the raw string
    so an unrecognised shape can never accidentally match a pinned reason.
    """
    if not isinstance(message, str) or not message:
        return None
    try:
        parsed = ast.literal_eval(message)
    except (SyntaxError, ValueError):
        return message
    if isinstance(parsed, tuple) and parsed and isinstance(parsed[-1], str):
        return parsed[-1]
    return message


def pinned_auth_skip(requirement: dict[str, Any], version: int, report: dict[str, Any]) -> bool:
    """True if this MANDATORY SKIP is one of the two pinned alpha.5 conditional auth checks.

    Requires all four conditions: the v2 lane, a requirement the TCK itself marked SKIPPED, a
    fixture that advertises no auth, and a test whose node id and skip reason match the pin
    exactly.
    """
    pinned = V2_PINNED_SKIPS.get(str(requirement.get("id"))) if version == 2 else None
    if pinned is None or requirement.get("status") != "SKIPPED":
        return False
    if advertises_auth(report.get("agent_capabilities")):
        return False
    nodeid, reason = pinned
    tests = requirement.get("tests", [])
    return bool(tests) and all(
        test.get("status") == "SKIPPED" and test.get("nodeid") == nodeid and skip_reason(test.get("message")) == reason
        for test in tests
    )


def check_mandatory(requirements: list[dict[str, Any]], version: int, report: dict[str, Any]) -> list[str]:
    errors = []
    for item in requirements:
        if item.get("tier") != "MANDATORY":
            continue
        if pinned_auth_skip(item, version, report):
            print(f"v{version}: {item['id']} is SKIPPED (pinned conditional auth check)", flush=True)
            continue
        if item.get("status") != "PASS":
            errors.append(f"v{version}: {item['id']} is {item.get('status')}, expected PASS")
        for test in item.get("tests", []):
            if test.get("status") != "PASS":
                errors.append(f"v{version}: {test.get('nodeid')} is {test.get('status')}, expected PASS")
    return errors


def check_provenance(report: dict[str, Any], version: int, python: Path) -> list[str]:
    errors = []
    if report.get("protocol_version") != version:
        errors.append(f"v{version}: report protocol_version is {report.get('protocol_version')!r}")
    if report.get("schema_revision") != TCK_SCHEMA_REVISIONS[version]:
        errors.append(f"v{version}: unexpected TCK schema revision {report.get('schema_revision')!r}")
    command = report.get("agent_command", [])
    if not command or Path(command[0]).resolve() != python.resolve():
        errors.append(f"v{version}: report does not identify the current checkout's Python")
    expected_probe = ROOT / "tests" / "tck" / f"agent_v{version}.py"
    if len(command) < 2 or Path(command[1]).resolve() != expected_probe.resolve():
        errors.append(f"v{version}: report does not identify the v{version} probe")
    return errors


def inspect_report(
    path: Path, version: int, *, strict: bool, tck_exit: int, python: Path
) -> tuple[list[str], dict[str, Any]]:
    if not path.exists():
        return [f"v{version}: TCK wrote no JSON report (exit {tck_exit})"], {}
    try:
        report = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"v{version}: cannot read TCK report: {exc}"], {}

    errors = check_provenance(report, version, python)
    verdict = report.get("verdict", {})
    for flag in ("blocked_by_auth", "blocked_by_version_mismatch"):
        if verdict.get(flag):
            errors.append(f"v{version}: {flag}")

    requirements = report.get("requirements", [])
    if not isinstance(requirements, list) or not requirements:
        errors.append(f"v{version}: report contains no requirements")
        requirements = []
    failures = [f"{item['id']} ({item['tier']})" for item in requirements if item.get("status") == "FAIL"]
    print(
        f"v{version}: TCK exit={tck_exit}, conformant={verdict.get('conformant')}, "
        f"failures={', '.join(failures) or 'none'}, schema={report.get('schema_revision')}",
        flush=True,
    )

    if strict and (tck_exit != 0 or verdict.get("conformant") is not True):
        errors.append(f"v{version}: TCK conformance verdict failed")
    # Both lanes gate MANDATORY outcomes; only the v1 lane additionally requires the TCK's own
    # verdict, which also folds in CAPABILITY failures. ADVISORY results are never gated.
    errors.extend(check_mandatory(requirements, version, report))
    return errors, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tck-v1-dir", type=Path, required=True, help="checkout of the pinned v1 TCK commit")
    parser.add_argument("--tck-v2-dir", type=Path, required=True, help="checkout of the alpha.5 v2 TCK commit")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build" / "tck")
    parser.add_argument("--timeout", type=int, default=10, help="TCK per-response timeout in seconds")
    args = parser.parse_args()
    tck_dirs = {1: args.tck_v1_dir.resolve(), 2: args.tck_v2_dir.resolve()}
    output_dir = args.output_dir.resolve()

    for version, tck_dir in tck_dirs.items():
        if git_revision(tck_dir) != TCK_REVISIONS[version]:
            parser.error(f"v{version} TCK must be checked out at {TCK_REVISIONS[version]}")
    python = sdk_python()
    output_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    summaries: dict[str, Any] = {}
    for version in (1, 2):
        tck_dir = tck_dirs[version]
        exit_code = run_suite(tck_dir, output_dir, python, version, args.timeout)
        problems, report = inspect_report(
            output_dir / f"v{version}.json",
            version,
            strict=version == 1,
            tck_exit=exit_code,
            python=python,
        )
        errors.extend(problems)
        summaries[f"v{version}"] = {
            "tck_revision": TCK_REVISIONS[version],
            "tck_exit": exit_code,
            "tck_schema_revision": report.get("schema_revision"),
            "verdict": report.get("verdict"),
        }

    manifest = {
        "sdk_revision": git_revision(ROOT),
        "sdk_schema_refs": {
            "v1": (ROOT / "schema" / "VERSION").read_text().strip(),
            "v2": (ROOT / "schema" / "v2" / "VERSION").read_text().strip(),
        },
        "sdk_python": str(python.resolve()),
        "sdk_python_version": subprocess.check_output([str(python), "--version"], text=True).strip(),  # noqa: S603
        "tck_python_versions": {
            f"v{version}": subprocess.check_output(  # noqa: S603
                [str(tck_dir / ".venv" / "bin" / "python"), "--version"], text=True
            ).strip()
            for version, tck_dir in tck_dirs.items()
        },
        "runs": summaries,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
