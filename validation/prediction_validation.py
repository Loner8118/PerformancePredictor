from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from mathematical_engine.scalability import predict_scalability


class PredictionValidationError(ValueError):
    """Bad input, or a file that isn't a valid artifact from an earlier phase."""


class ModelIntegrityError(PredictionValidationError):
    """A frozen prediction's recorded hash no longer matches its own
    contents, or two files that are supposed to belong to the same
    experiment don't agree on which one that is."""


# ==========================================================================
# What this module does, and the specific critique it's built to survive
# ==========================================================================
#
# The critique this project has to be defensible against: "you're using
# the same predicted values and not really doing load testing in
# validation." A single script that fits USL on 20-500, predicts 600+,
# and immediately "validates" against a number it just computed has no
# way to prove that isn't what happened - it's all one blob with no
# independently-checkable ordering.
#
# This module enforces three phases as three SEPARATE function calls,
# each writing its own file, invoked at genuinely different times:
#
#   Phase A - freeze_predictions()
#       Takes the already-fitted model (usl_results etc., from load-
#       testing 20-500 and running the math engine on it) and the
#       target levels to predict (e.g. 600-1000). Calls
#       scalability.predict_scalability() with NO actual_throughput_by_users
#       - that parameter does not exist anywhere in this function's
#       signature, so there is no code path, accidental or otherwise,
#       through which Phase A could see real 600+ data before writing
#       predictions.json. Also computes a SHA-256 hash over every input
#       that produced the prediction and stores it alongside - see
#       "Why the hash" below.
#
#   Phase B - record_validation_actuals()
#       Run LATER, as a genuinely separate invocation (a different
#       command, a different day - whatever "later" means for the
#       actual experiment). Takes the REAL load-test results from
#       actually running locust_runner.run() at the predicted target
#       levels (locust_runner.py needed no changes for this - it already
#       accepts an arbitrary user_levels list) and writes
#       validation_actual.json. This function never reads predictions.json
#       for anything except the list of WHICH levels to look for and
#       which model_hash to stamp the actuals with - it cannot see, and
#       does not need to see, what was predicted at those levels.
#
#   Phase C - validate_predictions()
#       Reloads both files, verifies the hash, and ONLY THEN calls
#       predict_scalability() again - now with actual_throughput_by_users
#       filled in - to get the real MAE/RMSE/MAPE via
#       scalability.validate_prediction(). This is orchestration, not new
#       math: every comparison this module reports is computed by a
#       compare/validate function that already existed in the relevant
#       math module (scalability.validate_prediction() here;
#       model_validation.py does the same job for little_law.py's
#       compare_concurrency() and queueing.py's compare_system_time()).
#
# Why the hash: two timestamped files is already a real audit trail (a
# reviewer can check predictions.json was committed before
# validation_actual.json existed), but it doesn't prove nobody quietly
# re-fit sigma/kappa in between and overwrote predictions.json before
# running Phase B. The SHA-256 over every frozen input closes that gap:
# Phase C recomputes the hash from predictions.json's own stored inputs
# and refuses to proceed if it doesn't match what was recorded at
# freeze time. "The prediction file cryptographically commits to the
# exact model that produced it" is a stronger claim than "we saved a
# JSON file first."
#
# What this module deliberately does NOT do: run Locust itself. Phase B
# takes already-collected level_results as input rather than owning the
# LocustRunner call - locust_runner.py's exact aggregated-metric field
# names weren't available to verify against when this was written, so
# forcing that coupling here risked a silent field-name mismatch that's
# hard to notice (a KeyError is loud; a field defaulting to None because
# the key was slightly wrong is quiet and corrupts the whole experiment).
# The field names are configurable per-call instead - adjust
# throughput_field/response_time_field/error_rate_field to match
# whatever locust_runner.py's aggregated level dict actually calls them.

def _canonical_hash(data: Dict[str, Any]) -> str:
    """
    Deterministic SHA-256 hash for experiment inputs.

    The data is first normalized through JSON serialization and
    deserialization so that Phase A and Phase C hash the exact same
    JSON-compatible representation.

    This makes the hash stable across:
        Python object -> JSON file -> Python object
    """

    try:
        # Normalize the object exactly as it will exist in the JSON file.
        normalized = json.loads(
            json.dumps(
                data,
                sort_keys=True,
                default=str,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise PredictionValidationError(
            f"Unable to canonicalize data for hashing: {exc}"
        ) from exc

    canonical = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )

    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()

def _write_json_atomic(path: str, data: Dict[str, Any]) -> None:
    """
    Write via a temp file + atomic rename, so a crash or interruption
    mid-write can never leave a half-written, corrupt experiment
    artifact sitting at `path` - the rename either fully happens or
    fully doesn't.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        raise PredictionValidationError(f"File not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise PredictionValidationError(f"Could not read {path} as JSON: {e}") from e
    if not isinstance(data, dict):
        raise PredictionValidationError(f"{path} does not contain a JSON object.")
    return data


def _default_experiment_id() -> str:
    return "EXP" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --- Phase A: freeze the prediction, before any validation data exists ---

def freeze_predictions(
    usl_results: Dict[str, Any],
    capacity_results: Dict[str, Any],
    runtime_metrics: Dict[str, Any],
    prediction_targets: Sequence[float],
    output_path: str,
    bottleneck_results: Optional[Dict[str, Any]] = None,
    amdahl_results: Optional[Dict[str, Any]] = None,
    experiment_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    PHASE A. Call this once, immediately after fitting the model on the
    tested load levels (e.g. 20-500) - before real load tests exist at
    any of prediction_targets.

    Args:
        usl_results, capacity_results, runtime_metrics, bottleneck_results,
            amdahl_results: the exact same arguments scalability.
            predict_scalability() takes - see that function's docstring.
        prediction_targets: the UNTESTED load levels to predict (e.g.
            [600, 700, 800, 1000]) - these are what Phase B will later be
            expected to actually test.
        output_path: where to write predictions.json.
        experiment_id: optional label; auto-generated from a UTC
            timestamp if omitted.

    Returns:
        The full record written to output_path (see module overview for
        why it includes a model_hash and every input that produced it).

    Raises:
        PredictionValidationError: bad input.
    """
    if not isinstance(usl_results, dict):
        raise PredictionValidationError("usl_results must be a dict.")
    if not isinstance(capacity_results, dict):
        raise PredictionValidationError("capacity_results must be a dict.")
    if not isinstance(runtime_metrics, dict):
        raise PredictionValidationError("runtime_metrics must be a dict.")
    if not prediction_targets:
        raise PredictionValidationError("prediction_targets must not be empty.")

    try:
        normalized_targets = sorted({float(t) for t in prediction_targets})
    except (TypeError, ValueError) as e:
        raise PredictionValidationError(f"prediction_targets must all be numeric: {e}") from e

    frozen_inputs = {
        "usl_results": usl_results,
        "capacity_results": capacity_results,
        "runtime_metrics": runtime_metrics,
        "bottleneck_results": bottleneck_results,
        "amdahl_results": amdahl_results,
        "prediction_targets": normalized_targets,
    }
    model_hash = _canonical_hash(frozen_inputs)

    # No actual_throughput_by_users here - see module overview. This is
    # the one line in this whole module that structurally enforces the
    # "Phase A cannot see Phase B data" guarantee; everything else is
    # organizational discipline around it.
    prediction_result = predict_scalability(
        usl_results=usl_results,
        capacity_results=capacity_results,
        runtime_metrics=runtime_metrics,
        prediction_targets=normalized_targets,
        bottleneck_results=bottleneck_results,
        amdahl_results=amdahl_results,
    )

    record = {
        "phase": "A_frozen_prediction",
        "experiment_id": experiment_id or _default_experiment_id(),
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model_hash": model_hash,
        "prediction_targets": normalized_targets,
        "frozen_inputs": frozen_inputs,
        "prediction_result": prediction_result,
    }

    _write_json_atomic(output_path, record)
    print(
        f"Phase A complete: {len(normalized_targets)} target(s) predicted, "
        f"frozen to {output_path} (model_hash={model_hash[:12]}...)."
    )
    return record


# --- Phase B: record what actually happened, at the predicted levels ---

def record_validation_actuals(
    predictions_path: str,
    level_results: Sequence[Dict[str, Any]],
    output_path: str,
    users_field: str = "users",
    throughput_field: str = "throughput",
    response_time_field: str = "average_response_time",
    error_rate_field: str = "error_rate",
) -> Dict[str, Any]:
    """
    PHASE B. Call this AFTER actually load-testing the levels
    predictions_path predicted - genuinely later, as a separate
    invocation, not from inside the same call stack as freeze_predictions().

    Args:
        predictions_path: path to the predictions.json written by
            freeze_predictions(). Only its prediction_targets list and
            model_hash are read here - not its predicted values, which
            this function has no reason to look at.
        level_results: the aggregated per-level results from actually
            running LocustRunner at the predicted target levels (e.g.
            runner.run(project_path, container_id, user_levels=targets)
            ["levels"]). This function does not call LocustRunner itself
            - see module overview for why.
        output_path: where to write validation_actual.json.
        users_field/throughput_field/response_time_field/error_rate_field:
            the keys locust_runner.py's aggregated level dicts use for
            each quantity - adjust these if they differ from the
            defaults guessed here.

    Returns:
        The record written to output_path. Levels in level_results whose
        users value doesn't match one of predictions_path's targets are
        silently ignored (extra load-test data beyond what was asked
        for); targets with no matching actual result are listed in
        missing_targets and printed as a warning, not silently dropped.

    Raises:
        PredictionValidationError: predictions_path isn't a valid Phase
            A file, or level_results is malformed.
    """
    predictions_record = _load_json(predictions_path)
    target_users = predictions_record.get("prediction_targets")
    model_hash = predictions_record.get("model_hash")
    if not isinstance(target_users, list) or not target_users or not model_hash:
        raise PredictionValidationError(
            f"{predictions_path} is missing prediction_targets/model_hash - not a valid Phase A file."
        )
    target_users_set = set(target_users)

    if not isinstance(level_results, (list, tuple)):
        raise PredictionValidationError("level_results must be a list of per-level result dicts.")

    actual_levels: List[Dict[str, Any]] = []
    seen_users = set()
    for level in level_results:
        if not isinstance(level, dict):
            continue
        users_raw = level.get(users_field)
        if users_raw is None:
            continue
        try:
            users = float(users_raw)
        except (TypeError, ValueError):
            continue
        if users not in target_users_set:
            continue  # not one of the levels this experiment predicted - not this phase's concern
        seen_users.add(users)
        actual_levels.append({
            "users": users,
            "actual_throughput": level.get(throughput_field),
            "actual_response_time": level.get(response_time_field),
            "actual_error_rate": level.get(error_rate_field),
        })

    missing_targets = sorted(target_users_set - seen_users)
    if missing_targets:
        print(
            f"Warning: {len(missing_targets)} predicted target(s) have no matching actual "
            f"result in level_results: {missing_targets}. Phase C will validate only the "
            f"targets that were actually tested."
        )

    record = {
        "phase": "B_validation_actuals",
        "predictions_source": predictions_path,
        "model_hash": model_hash,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "levels": actual_levels,
        "missing_targets": missing_targets,
    }

    _write_json_atomic(output_path, record)
    print(f"Phase B complete: {len(actual_levels)} validation level(s) recorded to {output_path}.")
    return record


# --- Phase C: reload both, verify integrity, compare ---

def validate_predictions(
    predictions_path: str,
    validation_actual_path: str,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    PHASE C. Reloads Phase A's and Phase B's files, verifies the model
    hash to confirm predictions.json hasn't been altered since it was
    frozen, confirms both files belong to the same experiment, and only
    then re-runs predict_scalability() - now with the real measurements
    filled in - to get MAE/RMSE/MAPE via scalability.validate_prediction().

    Args:
        predictions_path: Phase A's output file.
        validation_actual_path: Phase B's output file.
        output_path: optional - if given, writes the full validated
            result there too.

    Returns:
        {"phase": "C_validated", "model_hash", "result" (the full
         predict_scalability() output, now with "validation" populated
         on every matched prediction), "validation_summary" (MAE/RMSE/
         MAPE/consistent_count - the number to report in the paper)}.

    Raises:
        PredictionValidationError: either file isn't valid.
        ModelIntegrityError: predictions.json's contents don't match its
            own recorded hash (edited after freezing), or
            validation_actual.json was recorded against a different
            prediction run than predictions_path.
    """
    predictions_record = _load_json(predictions_path)
    actuals_record = _load_json(validation_actual_path)

    frozen_inputs = predictions_record.get("frozen_inputs")
    stored_hash = predictions_record.get("model_hash")
    if not isinstance(frozen_inputs, dict) or not stored_hash:
        raise PredictionValidationError(
            f"{predictions_path} is missing frozen_inputs/model_hash - not a valid Phase A file."
        )

    recomputed_hash = _canonical_hash(frozen_inputs)
    if recomputed_hash != stored_hash:
        raise ModelIntegrityError(
            f"Integrity check failed: {predictions_path}'s frozen inputs no longer match its own "
            f"recorded hash ({stored_hash[:12]}... vs {recomputed_hash[:12]}...) - this file "
            f"appears to have been edited after Phase A. Re-run Phase A rather than trusting it."
        )

    actuals_hash = actuals_record.get("model_hash")
    if actuals_hash != stored_hash:
        raise ModelIntegrityError(
            f"{validation_actual_path} was recorded against a different prediction run "
            f"(model_hash {actuals_hash!r}) than {predictions_path} ({stored_hash!r}) - make sure "
            f"Phase B was run using this exact predictions.json, not a different experiment's."
        )

    actual_throughput_by_users = {
        level["users"]: level["actual_throughput"]
        for level in actuals_record.get("levels", [])
        if isinstance(level, dict) and level.get("actual_throughput") is not None
    }

    validated_result = predict_scalability(
        usl_results=frozen_inputs["usl_results"],
        capacity_results=frozen_inputs["capacity_results"],
        runtime_metrics=frozen_inputs["runtime_metrics"],
        prediction_targets=frozen_inputs["prediction_targets"],
        bottleneck_results=frozen_inputs.get("bottleneck_results"),
        amdahl_results=frozen_inputs.get("amdahl_results"),
        actual_throughput_by_users=actual_throughput_by_users,
    )
    validation_summary = validated_result["summary"]["validation_summary"]

    record = {
        "phase": "C_validated",
        "model_hash": stored_hash,
        "validated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "predictions_source": predictions_path,
        "validation_actual_source": validation_actual_path,
        "result": validated_result,
        "validation_summary": validation_summary,
    }

    if output_path:
        _write_json_atomic(output_path, record)
        print(f"Phase C complete: validated result written to {output_path}.")

    print(
        f"Validated {validation_summary['validated_count']} level(s): "
        f"{validation_summary['consistent_count']}/{validation_summary['validated_count']} consistent, "
        f"MAE={validation_summary['mae']}, RMSE={validation_summary['rmse']}, "
        f"MAPE={validation_summary['mape_percent']}%."
    )
    return record


# --- Standard experiment directory layout (optional convenience) ---

def experiment_paths(base_dir: str, experiment_id: str) -> Dict[str, str]:
    """
    Resolves the standard experiments/EXPxxx/ file paths this module's
    three phases read/write - purely a naming convenience, not required:
    every function above takes explicit paths and works fine without this.
    """
    exp_dir = os.path.join(base_dir, experiment_id)
    return {
        "dir": exp_dir,
        "predictions": os.path.join(exp_dir, "predictions.json"),
        "validation_actual": os.path.join(exp_dir, "validation_actual.json"),
        "validated": os.path.join(exp_dir, "validated_result.json"),
    }
