from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from validation.experiment_manager import (
    Experiment,
    ExperimentError,
    list_experiments,
)
from validation.statistics import (
    mean_stdev_ci,
    compare_against_baseline,
    StatisticsError,
)


class ReportError(ValueError):
    """Raised when report input or required artifacts are invalid."""


# ============================================================================
# Generic Markdown table formatting
# ============================================================================

def to_markdown_table(
    rows: Sequence[Dict[str, Any]],
    columns: Sequence[Tuple[str, str]],
) -> str:
    """
    Convert a list of dictionaries into a Markdown table.

    Args:
        rows:
            Table rows.
        columns:
            [(dictionary_key, display_header), ...]

    Returns:
        Markdown table as a string.
    """
    if not columns:
        raise ReportError("columns must not be empty.")

    header = "| " + " | ".join(header for _, header in columns) + " |"
    separator = "|" + "|".join("---" for _ in columns) + "|"

    lines = [header, separator]

    for row in rows:
        cells = []

        for key, _ in columns:
            value = row.get(key)

            if value is None:
                cells.append("—")
            else:
                cells.append(str(value))

        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines)


def _fmt(value: Any, digits: int = 3) -> Any:
    """Format numeric report values without modifying non-numeric values."""

    if value is None:
        return "—"

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return round(float(value), digits)

    return value


# ============================================================================
# Prediction validation
# ============================================================================

def render_prediction_section(
    validated_result: Dict[str, Any],
) -> str:
    """
    Render Phase C predicted-vs-actual throughput validation.
    """

    result = validated_result.get("result", {})
    predictions = result.get("predictions", [])
    summary = validated_result.get("validation_summary", {})

    if not predictions:
        return (
            "### Predicted vs. Actual Throughput\n\n"
            "No prediction validation records are available.\n"
        )

    rows = []

    for prediction in predictions:
        validation = prediction.get("validation")

        rows.append(
            {
                "users": prediction.get("users"),
                "predicted_throughput": _fmt(
                    prediction.get("predicted_throughput")
                ),
                "actual_throughput": _fmt(
                    validation.get("actual_throughput")
                    if validation
                    else None
                ),
                "percentage_error": _fmt(
                    validation.get("percentage_error")
                    if validation
                    else None
                ),
                "consistent": (
                    validation.get("consistent")
                    if validation
                    else "not yet tested"
                ),
            }
        )

    table = to_markdown_table(
        rows,
        [
            ("users", "Users"),
            ("predicted_throughput", "Predicted (req/s)"),
            ("actual_throughput", "Actual (req/s)"),
            ("percentage_error", "Error (%)"),
            ("consistent", "Consistent"),
        ],
    )

    validated_count = summary.get("validated_count", 0)
    consistent_count = summary.get("consistent_count", 0)

    summary_line = (
        f"Validated **{validated_count}** load level(s): "
        f"**{consistent_count}/{validated_count}** consistent. "
        f"MAE={_fmt(summary.get('mae'))}, "
        f"RMSE={_fmt(summary.get('rmse'))}, "
        f"MAPE={_fmt(summary.get('mape_percent'))}%."
    )

    return (
        "### Predicted vs. Actual Throughput\n\n"
        f"{table}\n\n"
        f"{summary_line}\n"
    )


# ============================================================================
# Little's Law + Queueing validation
# ============================================================================

def render_model_validation_section(
    model_validation_result: Dict[str, Any],
) -> str:
    """
    Render Little's Law and Queueing predicted-vs-observed validation.
    """

    levels = model_validation_result.get("levels", [])

    if not levels:
        return (
            "### Mathematical Model Validation\n\n"
            "No Little's Law or Queueing validation records are available.\n"
        )

    ll_summary = model_validation_result.get(
        "little_law_summary",
        {},
    )

    q_summary = model_validation_result.get(
        "queueing_summary",
        {},
    )

    ll_rows = []

    for level in levels:
        ll_rows.append(
            {
                "users": level.get("users"),
                "predicted_l": _fmt(level.get("predicted_l")),
                "observed_concurrency": _fmt(
                    level.get("observed_concurrency")
                ),
                "l_percentage_error": _fmt(
                    level.get("l_percentage_error")
                ),
                "l_consistent": level.get("l_consistent"),
            }
        )

    q_rows = []

    for level in levels:
        q_rows.append(
            {
                "users": level.get("users"),
                "predicted_system_time": _fmt(
                    level.get("predicted_system_time")
                ),
                "observed_response_time": _fmt(
                    level.get("observed_response_time")
                ),
                "w_percentage_error": _fmt(
                    level.get("w_percentage_error")
                ),
                "w_consistent": level.get("w_consistent"),
            }
        )

    ll_table = to_markdown_table(
        ll_rows,
        [
            ("users", "Users"),
            ("predicted_l", "Predicted L"),
            ("observed_concurrency", "Observed Concurrency"),
            ("l_percentage_error", "Error (%)"),
            ("l_consistent", "Consistent"),
        ],
    )

    q_table = to_markdown_table(
        q_rows,
        [
            ("users", "Users"),
            ("predicted_system_time", "Predicted W (s)"),
            ("observed_response_time", "Observed W (s)"),
            ("w_percentage_error", "Error (%)"),
            ("w_consistent", "Consistent"),
        ],
    )

    return (
        "### Little's Law: Predicted vs. Observed Concurrency\n\n"
        f"{ll_table}\n\n"
        f"MAE={_fmt(ll_summary.get('mae'))}, "
        f"RMSE={_fmt(ll_summary.get('rmse'))}, "
        f"MAPE={_fmt(ll_summary.get('mape_percent'))}%.\n\n"
        "### Queueing Model: Predicted vs. Observed Response Time\n\n"
        f"{q_table}\n\n"
        f"MAE={_fmt(q_summary.get('mae'))}, "
        f"RMSE={_fmt(q_summary.get('rmse'))}, "
        f"MAPE={_fmt(q_summary.get('mape_percent'))}%.\n"
    )


# ============================================================================
# Ablation study
# ============================================================================

def render_ablation_section(
    ablation_result: Dict[str, Any],
) -> str:
    """
    Render the ablation-study configuration comparison.
    """

    ground_truth = ablation_result.get(
        "ground_truth",
        {},
    )

    configs = ablation_result.get(
        "configurations",
        [],
    )

    if not configs:
        return (
            "### Ablation Study\n\n"
            "No ablation results are available.\n"
        )

    table = to_markdown_table(
        configs,
        [
            ("config", "Configuration"),
            ("safe_users", "Safe Capacity"),
            (
                "prediction_error_percent",
                "Capacity Error (%)",
            ),
            ("bottleneck", "Identified Bottleneck"),
            (
                "bottleneck_correct",
                "Bottleneck Correct",
            ),
        ],
    )

    actual_capacity = ground_truth.get(
        "actual_safe_capacity"
    )

    actual_bottleneck = ground_truth.get(
        "actual_bottleneck"
    )

    ground_truth_line = (
        "Ground truth from validation load tests: "
        f"safe capacity = **{actual_capacity} users**, "
        f"bottleneck = **{actual_bottleneck!r}**."
    )

    return (
        "### Ablation Study\n\n"
        f"{ground_truth_line}\n\n"
        f"{table}\n"
    )


# ============================================================================
# Experiment metadata
# ============================================================================

def render_metadata_section(
    experiment: Experiment,
) -> str:
    """Render experiment identification and reproducibility metadata."""

    metadata = experiment.load_metadata()

    repository = metadata.get(
        "target_repository",
        "—",
    )

    commit = metadata.get(
        "target_commit",
        "—",
    )

    framework = metadata.get(
        "framework",
        "—",
    )

    created = metadata.get(
        "created_at_utc",
        "—",
    )

    tool_commit = metadata.get(
        "tool_commit",
        "—",
    )

    dirty = metadata.get(
        "tool_repository_dirty"
    )

    dirty_text = (
        " (dirty working tree)"
        if dirty
        else ""
    )

    return (
        f"# Experiment Report: {experiment.experiment_id}\n\n"
        f"**Target Repository:** {repository}  \n"
        f"**Target Commit:** {commit}  \n"
        f"**Framework:** {framework}  \n"
        f"**Created:** {created}  \n"
        f"**Tool Commit:** {tool_commit}{dirty_text}\n"
    )


# ============================================================================
# Complete single-experiment report
# ============================================================================

def render_experiment_report(
    experiment: Experiment,
) -> str:
    """
    Render every report section currently available for an experiment.

    The report intentionally supports partial experiments. For example,
    Phase A alone can produce a short report, while a completed
    Phase A/B/C experiment produces the complete validation report.
    """

    if not experiment.exists():
        raise ReportError(
            f"Experiment {experiment.experiment_id!r} has no "
            "metadata.json. Call Experiment.initialize() first."
        )

    sections = [
        render_metadata_section(experiment)
    ]

    available: List[str] = []
    missing: List[str] = []

    # ------------------------------------------------------------------
    # Phase C: USL predicted-vs-actual validation
    # ------------------------------------------------------------------

    try:
        validated_result = experiment.load_artifact(
            "validated_result"
        )

        sections.append(
            render_prediction_section(
                validated_result
            )
        )

        available.append(
            "prediction validation"
        )

    except ExperimentError:
        missing.append(
            "prediction validation "
            "(validated_result.json not found - "
            "complete Phase A/B/C)"
        )

    # ------------------------------------------------------------------
    # Little's Law + Queueing validation
    # ------------------------------------------------------------------

    try:
        model_validation_result = experiment.load_artifact(
            "model_validation_result"
        )

        sections.append(
            render_model_validation_section(
                model_validation_result
            )
        )

        available.append(
            "Little's Law and Queueing validation"
        )

    except ExperimentError:
        missing.append(
            "model validation "
            "(model_validation_result.json not found)"
        )

    # ------------------------------------------------------------------
    # Ablation
    # ------------------------------------------------------------------

    try:
        ablation_result = experiment.load_artifact(
            "ablation_result"
        )

        sections.append(
            render_ablation_section(
                ablation_result
            )
        )

        available.append(
            "ablation study"
        )

    except ExperimentError:
        missing.append(
            "ablation study "
            "(ablation_result.json not found)"
        )

    # ------------------------------------------------------------------
    # Report status
    # ------------------------------------------------------------------

    if available:
        sections.append(
            "### Available Analysis\n\n"
            + "\n".join(
                f"- {item}"
                for item in available
            )
            + "\n"
        )

    if missing:
        sections.append(
            "### Not Yet Available\n\n"
            + "\n".join(
                f"- {item}"
                for item in missing
            )
            + "\n"
        )

    return "\n\n".join(sections)


# ============================================================================
# Save complete Markdown report
# ============================================================================

def save_experiment_report(
    experiment: Experiment,
    output_path: Optional[str] = None,
) -> str:
    """
    Generate and save the complete Markdown report.

    Returns:
        Absolute path to the generated report.
    """

    report = render_experiment_report(
        experiment
    )

    if output_path is None:
        output_path = os.path.join(
            str(experiment.paths["dir"]),
            "report.md",
        )

    output_path = os.path.abspath(
        output_path
    )

    os.makedirs(
        os.path.dirname(output_path),
        exist_ok=True,
    )

    with open(
        output_path,
        "w",
        encoding="utf-8",
    ) as file:
        file.write(report)

    return output_path


# ============================================================================
# Cross-experiment aggregation
# ============================================================================

def aggregate_metric_across_experiments(
    base_dir: str,
    artifact_name: str,
    metric_path: Sequence[str],
    experiment_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """
    Extract one numeric metric from several experiments and summarize it.
    """

    ids = (
        experiment_ids
        if experiment_ids is not None
        else list_experiments(base_dir)
    )

    values_by_experiment: Dict[str, float] = {}
    skipped: List[str] = []

    for experiment_id in ids:
        experiment = Experiment(
            base_dir,
            experiment_id,
        )

        try:
            artifact = experiment.load_artifact(
                artifact_name
            )

        except ExperimentError:
            skipped.append(
                f"{experiment_id} "
                f"({artifact_name}.json not found)"
            )
            continue

        value: Any = artifact

        for key in metric_path:
            if (
                not isinstance(value, dict)
                or key not in value
            ):
                value = None
                break

            value = value[key]

        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
        ):
            skipped.append(
                f"{experiment_id} "
                "(metric path yielded no numeric value)"
            )
            continue

        values_by_experiment[
            experiment_id
        ] = float(value)

    if not values_by_experiment:
        raise ReportError(
            f"No experiment under {base_dir!r} yielded "
            f"a usable value for "
            f"{artifact_name}."
            f"{'.'.join(metric_path)}."
        )

    summary = mean_stdev_ci(
        list(values_by_experiment.values())
    )

    return {
        "metric": (
            f"{artifact_name}."
            f"{'.'.join(metric_path)}"
        ),
        "experiment_count": len(
            values_by_experiment
        ),
        "values_by_experiment":
            values_by_experiment,
        "skipped_experiments": skipped,
        **summary,
    }


# ============================================================================
# Cross-experiment ablation comparison
# ============================================================================

def compare_configs_across_experiments(
    base_dir: str,
    baseline_config: str,
    experiment_ids: Optional[Sequence[str]] = None,
    alpha: float = 0.05,
) -> Dict[str, Any]:
    """
    Compare ablation prediction errors across multiple experiments.

    This is intended for the final research evaluation across several
    applications, not for deciding the result of one experiment.
    """

    ids = (
        experiment_ids
        if experiment_ids is not None
        else list_experiments(base_dir)
    )

    samples: Dict[str, List[float]] = {}
    used_experiments: List[str] = []

    for experiment_id in ids:

        experiment = Experiment(
            base_dir,
            experiment_id,
        )

        try:
            ablation = experiment.load_artifact(
                "ablation_result"
            )

        except ExperimentError:
            continue

        configurations = ablation.get(
            "configurations",
            [],
        )

        errors = {
            config["config"]: config.get(
                "prediction_error_percent"
            )
            for config in configurations
            if "config" in config
        }

        if (
            not errors
            or any(
                value is None
                for value in errors.values()
            )
        ):
            continue

        for name, value in errors.items():
            samples.setdefault(
                name,
                [],
            ).append(
                float(value)
            )

        used_experiments.append(
            experiment_id
        )

    if len(used_experiments) < 2:
        raise ReportError(
            "At least two experiments with complete "
            "ablation_result.json files are required "
            "for paired comparison."
        )

    try:
        comparison = compare_against_baseline(
            samples,
            baseline=baseline_config,
            alpha=alpha,
        )

    except StatisticsError as error:
        raise ReportError(
            str(error)
        ) from error

    comparison[
        "experiments_used"
    ] = used_experiments

    return comparison


# ============================================================================
# Predicted-vs-actual figure
# ============================================================================

def plot_predicted_vs_actual(
    validated_result: Dict[str, Any],
    output_path: str,
) -> str:
    """
    Generate the predicted-vs-actual throughput graph.

    matplotlib is imported lazily so report generation does not require
    matplotlib when only Markdown output is needed.
    """

    try:
        import matplotlib

        matplotlib.use("Agg")

        import matplotlib.pyplot as plt

    except ImportError as error:
        raise ReportError(
            "matplotlib is required for the "
            "predicted-vs-actual figure."
        ) from error

    predictions = (
        validated_result
        .get("result", {})
        .get("predictions", [])
    )

    if not predictions:
        raise ReportError(
            "validated_result contains no "
            "prediction records."
        )

    users = [
        prediction["users"]
        for prediction in predictions
    ]

    predicted = [
        prediction["predicted_throughput"]
        for prediction in predictions
    ]

    actual_users: List[int] = []
    actual_values: List[float] = []

    for prediction in predictions:

        validation = prediction.get(
            "validation"
        )

        if (
            validation
            and validation.get(
                "actual_throughput"
            ) is not None
        ):
            actual_users.append(
                prediction["users"]
            )

            actual_values.append(
                validation[
                    "actual_throughput"
                ]
            )

    output_path = os.path.abspath(
        output_path
    )

    os.makedirs(
        os.path.dirname(output_path),
        exist_ok=True,
    )

    fig, ax = plt.subplots(
        figsize=(7, 5)
    )

    ax.plot(
        users,
        predicted,
        linestyle="--",
        marker="o",
        label="Predicted (USL)",
    )

    if actual_values:
        ax.plot(
            actual_users,
            actual_values,
            linestyle="-",
            marker="s",
            label="Actual (measured)",
        )

    ax.set_xlabel(
        "Concurrent Users"
    )

    ax.set_ylabel(
        "Throughput (req/s)"
    )

    ax.set_title(
        "Predicted vs. Actual Throughput"
    )

    ax.legend()
    ax.grid(
        True,
        alpha=0.3,
    )

    fig.tight_layout()

    fig.savefig(
        output_path,
        dpi=150,
    )

    plt.close(fig)

    return output_path


# ============================================================================
# Complete report + figure
# ============================================================================

def generate_complete_report(
    experiment: Experiment,
    report_path: Optional[str] = None,
    figure_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate the complete experiment report and, when Phase C exists,
    generate the predicted-vs-actual figure.

    Returns:
        {
            "report_path": "...",
            "figure_path": "..." or None
        }
    """

    report_path = save_experiment_report(
        experiment,
        output_path=report_path,
    )

    generated_figure = None

    try:
        validated_result = experiment.load_artifact(
            "validated_result"
        )

        if figure_path is None:
            figure_path = os.path.join(
                str(experiment.paths["dir"]),
                "predicted_vs_actual.png",
            )

        generated_figure = plot_predicted_vs_actual(
            validated_result,
            figure_path,
        )

    except ExperimentError:
        # Phase C has not completed yet.
        generated_figure = None

    return {
        "report_path": report_path,
        "figure_path": generated_figure,
    }

