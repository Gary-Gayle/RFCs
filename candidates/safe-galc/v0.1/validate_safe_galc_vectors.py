#!/usr/bin/env python3
"""Structural materializer and validator for SAFE-GALC candidate vectors.

This harness checks JSON Schema Draft 2020-12 structure and reproducible
application of the candidate vector operations. It can emit scenario inputs
separately from expected verifier outputs. It does not verify cryptography,
recompute payload digests, or establish semantic/runtime conformance.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "safe-galc-v0.1.schema.json"
VECTORS_PATH = ROOT / "safe-galc-v0.1.conformance-vectors.json"
ALLOWED_OPERATIONS = {"add", "remove", "replace"}
VERIFIER_OUTPUT_FIELDS = ("evaluations", "overall_result")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Materialize and structurally validate SAFE-GALC candidate vectors."
    )
    parser.add_argument(
        "--emit-dir",
        type=Path,
        help=(
            "Write separate <scenario>.scenario-input.json and "
            "<scenario>.expected-output.json artifacts."
        ),
    )
    return parser.parse_args()


def _pointer_parts(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError(f"JSON Pointer must start with '/': {path!r}")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def _apply_operation(document: Any, operation: dict[str, Any]) -> None:
    kind = operation["operation"]
    if kind not in ALLOWED_OPERATIONS:
        raise ValueError(f"Unsupported operation: {kind!r}")

    parts = _pointer_parts(operation["path"])
    parent = document
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]

    leaf = parts[-1]
    if isinstance(parent, list):
        if kind == "add":
            if leaf == "-":
                parent.append(copy.deepcopy(operation["value"]))
            else:
                parent.insert(int(leaf), copy.deepcopy(operation["value"]))
        elif kind == "replace":
            parent[int(leaf)] = copy.deepcopy(operation["value"])
        else:
            parent.pop(int(leaf))
        return

    if kind in {"add", "replace"}:
        parent[leaf] = copy.deepcopy(operation["value"])
    else:
        del parent[leaf]


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _project_scenario_input(fixture_envelope: dict[str, Any]) -> dict[str, Any]:
    """Remove fixture-only verifier outputs from a changed structural envelope."""
    scenario_input = copy.deepcopy(fixture_envelope)
    for field in VERIFIER_OUTPUT_FIELDS:
        if field not in scenario_input:
            raise ValueError(
                f"structural fixture is missing output placeholder {field!r}"
            )
        del scenario_input[field]

    leaked = [field for field in VERIFIER_OUTPUT_FIELDS if field in scenario_input]
    if leaked:
        raise ValueError(f"verifier output fields leaked into scenario input: {leaked}")
    return scenario_input


def _expected_output(vector: dict[str, Any]) -> dict[str, Any]:
    expected = copy.deepcopy(vector["expected"])
    expected_validation = expected.pop("schema_validation", "pass")
    return {
        "scenario_id": vector["id"],
        "structural_fixture_validation": {
            "target": "fixture_envelope",
            "expected": expected_validation,
        },
        "semantic_verifier_output": expected,
        "forbidden_inference": vector["forbidden_inference"],
    }


def _write_json(path: Path, document: Any) -> None:
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = _parse_args()
    schema = _load_json(SCHEMA_PATH)
    vectors = _load_json(VECTORS_PATH)

    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    vector_ids = [vector["id"] for vector in vectors["vectors"]]
    expected_ids = [f"GALC-{index:03d}" for index in range(1, 19)]
    if vector_ids != expected_ids:
        print(
            f"FAIL: expected ordered vector IDs {expected_ids}, received {vector_ids}"
        )
        return 1

    base_errors = sorted(
        validator.iter_errors(vectors["base_lifecycle"]),
        key=lambda error: list(error.path),
    )
    if base_errors:
        print("FAIL: base_lifecycle does not validate")
        for error in base_errors:
            print(f"  {list(error.path)}: {error.message}")
        return 1

    print("PASS: schema is valid Draft 2020-12")
    print("PASS: base_lifecycle validates")
    print(
        "BOUNDARY: base_lifecycle evaluations and overall_result are structural "
        "fixture placeholders, not verifier results"
    )
    print(
        "BOUNDARY: projected scenario inputs omit verifier outputs; expected "
        "results remain separate test oracles"
    )

    if args.emit_dir is not None:
        args.emit_dir.mkdir(parents=True, exist_ok=True)

    failed = False
    for vector in vectors["vectors"]:
        materialized = copy.deepcopy(vectors)
        try:
            for operation in vector["semantic_changes"]:
                _apply_operation(materialized, operation)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            print(f"FAIL: {vector['id']} could not be materialized: {error}")
            failed = True
            continue

        errors = sorted(
            validator.iter_errors(materialized["base_lifecycle"]),
            key=lambda error: list(error.path),
        )
        expected_validation = vector["expected"].get("schema_validation", "pass")
        actual_validation = "fail" if errors else "pass"

        try:
            scenario_input = _project_scenario_input(materialized["base_lifecycle"])
        except ValueError as error:
            print(f"FAIL: {vector['id']} scenario-input projection failed: {error}")
            failed = True
            continue

        if args.emit_dir is not None:
            _write_json(
                args.emit_dir / f"{vector['id']}.scenario-input.json",
                scenario_input,
            )
            _write_json(
                args.emit_dir / f"{vector['id']}.expected-output.json",
                _expected_output(vector),
            )

        if actual_validation != expected_validation:
            print(
                f"FAIL: {vector['id']} expected schema {expected_validation}, "
                f"received {actual_validation}"
            )
            for error in errors[:5]:
                print(f"  {list(error.path)}: {error.message}")
            failed = True
        else:
            label = (
                "expected structural rejection" if errors else "structural validation"
            )
            print(
                f"PASS: {vector['id']} {label}; projected scenario input "
                "contains no verifier outputs"
            )

    if failed:
        return 1

    print("PASS: 18 ordered vectors materialized with expected structural outcomes")
    if args.emit_dir is not None:
        print(
            "PASS: separate scenario-input and expected-output artifacts "
            f"emitted to {args.emit_dir}"
        )
    print("LIMIT: no cryptographic, semantic, or runtime conformance is claimed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
