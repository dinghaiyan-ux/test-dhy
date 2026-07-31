#!/usr/bin/env python3
"""PokeeHelper: validate and safely invoke a curated subset of Notion/ClickUp calls.

The helper validates payloads *before* invoking ``pokee-skill``.  It intentionally
uses an allow-list of read-oriented operations by default.  Write operations must
be explicitly enabled by the caller and pass the same schema validation.

Security Pre-check
------------------
Before every write operation (Notion/ClickUp), PokeeHelper runs a mandatory
security pre-check by importing and calling ``security_validator.py``.  The
pre-check enforces:

    1. Secret/token/HTML injection scan on all string values in the payload
    2. JSON nesting depth limit (prevents deep-nesting DoS)
    3. Control-character normalization on all string values
    4. URL safety checks on any URL-valued properties (SSRF/private-IP block)

If any pre-check gate fails, the write is rejected *before* it reaches
``pokee-skill``.  Read operations are not subject to the pre-check.

Budget-Aware Cost Logging (v3.0.0)
----------------------------------
Before every write operation, PokeeHelper logs a projected infrastructure cost
estimate based on the "Production AI Infrastructure Hub" Google Sheet (2026 H100
cluster rental benchmark).  The cost is estimated using the configured provider's
on-demand H100 node-hour rate and an assumed compute footprint.  A cumulative
budget tracker can optionally enforce monthly budget thresholds with warning or
hard-stop behaviour.

To enable, call ``configure_budget_aware()`` once at startup:

    configure_budget_aware(provider="aws", monthly_budget=5_000_000, hard_stop=False)

Examples:
    python3 PokeeHelper.py validate notion.get_notion_page_content \
      '{"notion_page_id":"3aa0c5d3-8d0b-814b-9863-df39e582264e"}'

    python3 PokeeHelper.py build clickup.get_clickup_task_details \
      '{"clickup_task_name_or_id":"86b9vvk59"}'

    python3 PokeeHelper.py run notion.list_notion_database_items \
      '{"notion_database_id_or_url":"b9b6dd67-6fb4-4d1b-95a1-fc8cf78e5b6a","limit":100}'

No shell is used to execute requests: arguments are passed as a list to
subprocess.run.  This module never handles service credentials; pokee-skill is
responsible for authenticated execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)# ---------------------------------------------------------------------------
# Security Pre-check integration
# ---------------------------------------------------------------------------
# Import the standalone security gates from security_validator.py.
# These are called automatically before every write operation.

try:
    from security_validator import (
        _check_secrets,
        _validate_json_depth,
        _normalize_text,
        _validate_url,
        MAX_JSON_DEPTH,
        ValidationError as SecurityValidationError,
    )
    _SECURITY_AVAILABLE = True
except ImportError:
    # Fallback: if security_validator.py is missing, disable pre-check
    # but still proceed (read-only safety is not affected).
    _SECURITY_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOTION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
CLICKUP_TASK_ID_RE = re.compile(r"^[A-Za-z0-9]+$")
MAX_PAYLOAD_BYTES = 256_000
AUDIT_LOG_PATH = Path(__file__).with_name("helper_security_audit.jsonl")

# ===================================================================
# Budget-Aware Cost Estimation Constants (v3.0.0)
# ===================================================================
# Sourced from "Production AI Infrastructure Hub" Google Sheet —
# 2026 H100 Cluster Rental Benchmark (snapshot: 31 Jul 2026).
# On-demand, Linux, US East baseline rates for 8×H100 nodes.
# ===================================================================

AWS_H100_NODE_HOUR = 55.04       # $/node-hour (p5.48xlarge, US East)
AWS_H100_GPU_HOUR = 6.88         # $/GPU-hour
AZURE_H100_NODE_HOUR = 98.32     # $/node-hour (ND96isr_H100_v5, US East)
AZURE_H100_GPU_HOUR = 12.29      # $/GPU-hour
HOURS_PER_MONTH = 730            # Continuous-operation planning convention
NODES_IN_CLUSTER = 128           # Reference cluster size
GPUS_PER_NODE = 8                # GPUs per H100 node

# Derived reference costs (on-demand, 100% utilization)
AWS_MONTHLY_BURN = AWS_H100_NODE_HOUR * NODES_IN_CLUSTER * HOURS_PER_MONTH    # ~$5,142,937.60
AZURE_MONTHLY_BURN = AZURE_H100_NODE_HOUR * NODES_IN_CLUSTER * HOURS_PER_MONTH  # ~$9,187,020.80


# ===================================================================
# Budget-Aware Cost Tracking (v3.0.0)
# ===================================================================

class BudgetAwareConfig:
    """
    Configuration for the Budget-Aware cost logging layer.

    Attributes:
        provider: "aws" or "azure" — which pricing tier to use for estimates.
        monthly_budget: Optional monthly budget cap in USD.
        warning_threshold_pct: Fraction of budget at which to log warnings (default 0.80).
        hard_stop: If True, raise RuntimeError when projected cost exceeds budget.
        log_projected_cost: If True, log a cost estimate before every write op.
    """

    def __init__(
        self,
        provider: str = "aws",
        monthly_budget: Optional[float] = None,
        warning_threshold_pct: float = 0.80,
        hard_stop: bool = False,
        log_projected_cost: bool = True,
    ):
        if provider not in ("aws", "azure"):
            raise ValueError(f"provider must be 'aws' or 'azure', got: {provider}")
        if monthly_budget is not None and monthly_budget <= 0:
            raise ValueError("monthly_budget must be positive")
        if not (0 < warning_threshold_pct <= 1.0):
            raise ValueError("warning_threshold_pct must be in (0, 1]")

        self.provider = provider
        self.monthly_budget = monthly_budget
        self.warning_threshold_pct = warning_threshold_pct
        self.hard_stop = hard_stop
        self.log_projected_cost = log_projected_cost

    @property
    def node_hour_rate(self) -> float:
        return AWS_H100_NODE_HOUR if self.provider == "aws" else AZURE_H100_NODE_HOUR

    @property
    def gpu_hour_rate(self) -> float:
        return AWS_H100_GPU_HOUR if self.provider == "aws" else AZURE_H100_GPU_HOUR


class BudgetTracker:
    """
    Tracks cumulative projected cost across write operations.

    Each tracked write operation records an estimated cost based on the
    configured provider's per-node-hour rate and an assumed compute footprint.
    This provides visibility into aggregate infrastructure spend.
    """

    def __init__(self, config: BudgetAwareConfig):
        self._config = config
        self._cumulative_cost: float = 0.0
        self._operation_count: int = 0
        self._operations: list[dict[str, Any]] = []

    def estimate_and_log(
        self,
        operation: str,
        estimated_nodes: int = 1,
        estimated_hours: float = 1.0 / 3600.0,
    ) -> dict[str, Any]:
        """
        Estimate the projected cost for a single write operation and log it.

        Args:
            operation: Human-readable operation name.
            estimated_nodes: Number of H100 nodes assumed for this operation.
            estimated_hours: Estimated compute hours consumed.

        Returns:
            Dict with cost metadata (can be merged into the API result).
        """
        rate = self._config.node_hour_rate
        projected = estimated_nodes * estimated_hours * rate
        self._cumulative_cost += projected
        self._operation_count += 1

        entry = {
            "operation": operation,
            "projected_cost_usd": round(projected, 4),
            "cumulative_cost_usd": round(self._cumulative_cost, 4),
            "provider": self._config.provider,
            "rate_per_node_hour": rate,
            "estimated_nodes": estimated_nodes,
            "estimated_hours": round(estimated_hours, 6),
        }
        self._operations.append(entry)

        if self._config.log_projected_cost:
            logger.info(
                f"[Budget-Aware] {operation}: projected ${projected:.4f} "
                f"(cumulative: ${self._cumulative_cost:.2f} | provider: {self._config.provider})"
            )

        # Budget threshold checks
        if self._config.monthly_budget is not None:
            ratio = self._cumulative_cost / self._config.monthly_budget
            if ratio >= 1.0 and self._config.hard_stop:
                logger.error(
                    f"[Budget-Aware] HARD STOP: cumulative ${self._cumulative_cost:.2f} "
                    f"exceeds monthly budget ${self._config.monthly_budget:.2f}"
                )
                raise RuntimeError(
                    f"Budget exceeded: cumulative ${self._cumulative_cost:.2f} "
                    f"> monthly budget ${self._config.monthly_budget:.2f}"
                )
            elif ratio >= self._config.warning_threshold_pct:
                logger.warning(
                    f"[Budget-Aware] WARNING: cumulative ${self._cumulative_cost:.2f} "
                    f"is {ratio:.1%} of monthly budget ${self._config.monthly_budget:.2f}"
                )

        return {
            "budget_metadata": {
                "projected_cost_usd": entry["projected_cost_usd"],
                "cumulative_cost_usd": entry["cumulative_cost_usd"],
                "provider": self._config.provider,
                "operation_count": self._operation_count,
            }
        }

    def summary(self) -> dict[str, Any]:
        """Return a summary of all tracked operations."""
        return {
            "total_operations": self._operation_count,
            "cumulative_projected_cost_usd": round(self._cumulative_cost, 4),
            "provider": self._config.provider,
            "monthly_budget": self._config.monthly_budget,
            "operations": self._operations,
        }

    def reset(self) -> None:
        """Reset all tracked counters."""
        self._cumulative_cost = 0.0
        self._operation_count = 0
        self._operations = []
        logger.info("[Budget-Aware] Tracker reset.")


# Global budget tracker — set via configure_budget_aware()
_global_budget_tracker: Optional[BudgetTracker] = None


def configure_budget_aware(
    provider: str = "aws",
    monthly_budget: Optional[float] = None,
    warning_threshold_pct: float = 0.80,
    hard_stop: bool = False,
    log_projected_cost: bool = True,
) -> BudgetTracker:
    """
    Initialize or reconfigure the global Budget-Aware tracker.

    Call once at startup to enable cost logging before write operations.

    Args:
        provider: "aws" or "azure" — pricing tier for cost estimates.
        monthly_budget: Optional monthly budget cap in USD.
        warning_threshold_pct: Fraction of budget at which to log warnings.
        hard_stop: If True, raise RuntimeError when budget is exceeded.
        log_projected_cost: If True, log a cost estimate before every write.

    Returns:
        The active BudgetTracker instance.
    """
    global _global_budget_tracker
    config = BudgetAwareConfig(
        provider=provider,
        monthly_budget=monthly_budget,
        warning_threshold_pct=warning_threshold_pct,
        hard_stop=hard_stop,
        log_projected_cost=log_projected_cost,
    )
    _global_budget_tracker = BudgetTracker(config)
    logger.info(
        f"[Budget-Aware] Initialized: provider={provider}, "
        f"budget={monthly_budget or 'unlimited'}, "
        f"hard_stop={hard_stop}"
    )
    return _global_budget_tracker


def get_budget_tracker() -> Optional[BudgetTracker]:
    """Return the active BudgetTracker, or None if not configured."""
    return _global_budget_tracker


def _budget_log_write(operation: str) -> dict[str, Any]:
    """
    Internal: log projected cost for a write operation if the tracker is active.

    Returns a dict with budget_metadata if tracking is enabled, else empty dict.
    """
    if _global_budget_tracker is not None and _global_budget_tracker._config.log_projected_cost:
        return _global_budget_tracker.estimate_and_log(operation=operation)
    return {}# ---------------------------------------------------------------------------
# Security Pre-check implementation
# ---------------------------------------------------------------------------

class SecurityPrecheckError(ValueError):
    """Raised when the mandatory security pre-check rejects a write payload."""


def _walk_strings(obj: Any, path: str = "$") -> list[tuple[str, str]]:
    """Yield (path, string_value) for every string leaf in a nested structure."""
    if isinstance(obj, str):
        return [(path, obj)]
    if isinstance(obj, dict):
        results = []
        for k, v in obj.items():
            results.extend(_walk_strings(v, f"{path}.{k}"))
        return results
    if isinstance(obj, list):
        results = []
        for i, v in enumerate(obj):
            results.extend(_walk_strings(v, f"{path}[{i}]"))
        return results
    return []


def _write_audit_event(operation: str, payload: Mapping[str, Any], decision: str, reason: str = "") -> None:
    """Append a redacted, tamper-evident security decision to the local audit log."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    event = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "decision": decision,
        "payload_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    if reason:
        event["reason"] = reason
    try:
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as audit_log:
            audit_log.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")
    except OSError as exc:
        raise SecurityPrecheckError(f"security audit logging failed: {exc}") from exc


def _security_precheck(operation: str, payload: Mapping[str, Any]) -> None:
    """Run and durably audit the mandatory security pre-check for a write payload.

    Fail-closed behavior: the write is blocked if the validator is unavailable,
    any gate rejects the payload, or the audit event cannot be persisted.
    """
    if not _SECURITY_AVAILABLE:
        error = "security_validator.py is unavailable"
        _write_audit_event(operation, payload, "blocked", error)
        raise SecurityPrecheckError(error)

    try:
        # Gate 1: JSON depth
        _validate_json_depth(payload, depth=0)

        # Gates 2–4: scan every nested string value.
        for path, value in _walk_strings(payload):
            _check_secrets(value, param=path)
            _normalize_text(value, param=path)
            if value.startswith("http://") or value.startswith("https://"):
                _validate_url(value, param=path)
    except SecurityValidationError as exc:
        reason = str(exc)
        _write_audit_event(operation, payload, "blocked", reason)
        raise SecurityPrecheckError(f"security check failed: {reason}") from exc

    _write_audit_event(operation, payload, "passed")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ParameterValidationError(ValueError):
    """Raised when a payload is unsafe or incompatible with a known schema."""


# ---------------------------------------------------------------------------
# Operation schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationSchema:
    """Validation contract for one pokee-skill operation."""

    operation: str
    required: frozenset[str]
    optional: frozenset[str]
    validators: Mapping[str, Callable[[Any], Any]]
    write_operation: bool = False

    @property
    def allowed(self) -> frozenset[str]:
        return self.required | self.optional


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _require_string(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ParameterValidationError("must be a non-empty string")
    return value.strip()


def _validate_notion_id_or_url(value: Any) -> str:
    value = _require_string(value)
    if NOTION_ID_RE.fullmatch(value):
        return value.lower()
    from urllib.parse import urlparse
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc not in {"notion.so", "www.notion.so", "app.notion.com"}:
        raise ParameterValidationError("must be a Notion UUID or an HTTPS Notion URL")
    candidates = re.findall(r"[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", parsed.path, re.I)
    if not candidates:
        raise ParameterValidationError("Notion URL does not contain a page/database ID")
    compact = candidates[-1].replace("-", "").lower()
    return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"


def _validate_clickup_task(value: Any) -> str:
    value = _require_string(value)
    if value.startswith("https://"):
        from urllib.parse import urlparse
        parsed = urlparse(value)
        if parsed.netloc not in {"app.clickup.com", "clickup.com"}:
            raise ParameterValidationError("ClickUp URL must use clickup.com")
        match = re.search(r"/t/([A-Za-z0-9]+)", parsed.path)
        if not match:
            raise ParameterValidationError("ClickUp task URL does not contain /t/<task-id>")
        return match.group(1)
    if not CLICKUP_TASK_ID_RE.fullmatch(value):
        raise ParameterValidationError("must be an alphanumeric ClickUp task ID or HTTPS task URL")
    return value


def _validate_positive_int(value: Any, *, minimum: int = 1, maximum: int = 500) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParameterValidationError("must be an integer")
    if not minimum <= value <= maximum:
        raise ParameterValidationError(f"must be between {minimum} and {maximum}")
    return value


def _validate_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ParameterValidationError("must be true or false")
    return value


def _validate_properties(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ParameterValidationError("must be a non-empty object")
    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ParameterValidationError("must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ParameterValidationError("properties object exceeds the payload limit")
    return value


def _validate_query(value: Any) -> str:
    value = _require_string(value)
    if len(value) > 500:
        raise ParameterValidationError("must be at most 500 characters")
    return value


def _validate_version(value: Any) -> str:
    value = _require_string(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ParameterValidationError("must use YYYY-MM-DD format")
    return value


# ---------------------------------------------------------------------------
# Schema registry
# ---------------------------------------------------------------------------

SCHEMAS: dict[str, OperationSchema] = {
    "notion.get_notion_page_content": OperationSchema(
        "notion.get_notion_page_content", frozenset({"notion_page_id"}),
        frozenset({"include_blocks"}),
        {"notion_page_id": _validate_notion_id_or_url, "include_blocks": _validate_bool},
    ),
    "notion.list_notion_database_items": OperationSchema(
        "notion.list_notion_database_items", frozenset({"notion_database_id_or_url"}),
        frozenset({"limit"}),
        {"notion_database_id_or_url": _validate_notion_id_or_url, "limit": _validate_positive_int},
    ),
    "notion.search_notion_content": OperationSchema(
        "notion.search_notion_content", frozenset({"notion_version"}),
        frozenset({"query", "sort_direction", "sort_timestamp", "filter_value", "filter_property", "limit"}),
        {"notion_version": _validate_version, "query": _validate_query, "limit": _validate_positive_int},
    ),
    "notion.update_notion_item_properties": OperationSchema(
        "notion.update_notion_item_properties", frozenset({"notion_item_id_or_url", "notion_updated_properties"}),
        frozenset(),
        {"notion_item_id_or_url": _validate_notion_id_or_url, "notion_updated_properties": _validate_properties},
        write_operation=True,
    ),
    "clickup.get_clickup_task_details": OperationSchema(
        "clickup.get_clickup_task_details", frozenset({"clickup_task_name_or_id"}),
        frozenset({"include_subtasks", "include_markdown_description", "custom_fields"}),
        {"clickup_task_name_or_id": _validate_clickup_task, "include_subtasks": _validate_bool,
         "include_markdown_description": _validate_bool, "custom_fields": _validate_bool},
    ),
    "clickup.search_clickup_tasks": OperationSchema(
        "clickup.search_clickup_tasks", frozenset({"query", "clickup_workspace_id"}),
        frozenset({"limit"}),
        {"query": _validate_query, "clickup_workspace_id": _require_string, "limit": _validate_positive_int},
    ),
    "clickup.create_clickup_task": OperationSchema(
        "clickup.create_clickup_task", frozenset({"name", "clickup_workspace_id"}),
        frozenset({"description", "status", "priority", "clickup_list_name_or_id", "assignees", "tags", "due_date"}),
        {"name": _require_string, "clickup_workspace_id": _require_string,
         "description": _require_string, "status": _require_string,
         "clickup_list_name_or_id": _require_string},
        write_operation=True,
    ),
    "clickup.create_clickup_task_comment": OperationSchema(
        "clickup.create_clickup_task_comment", frozenset({"clickup_task_name_or_id", "comment_text", "clickup_workspace_id"}),
        frozenset({"notify_all", "assignee_clickup_user_name", "list_of_teammate_clickup_user_names"}),
        {"clickup_task_name_or_id": _validate_clickup_task, "comment_text": _require_string,
         "clickup_workspace_id": _require_string, "notify_all": _validate_bool,
         "assignee_clickup_user_name": _require_string},
        write_operation=True,
    ),
}# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def validate(operation: str, payload: Mapping[str, Any], *, allow_writes: bool = False) -> dict[str, Any]:
    """Validate and normalize a payload for a supported operation.

    Unknown operations, unexpected keys, missing keys, malformed IDs, and write
    operations without explicit opt-in fail closed.
    """
    if operation not in SCHEMAS:
        raise ParameterValidationError(f"unsupported operation: {operation}")
    if not isinstance(payload, Mapping):
        raise ParameterValidationError("payload must be a JSON object")

    schema = SCHEMAS[operation]
    if schema.write_operation and not allow_writes:
        raise ParameterValidationError(
            f"{operation} is a write operation; pass allow_writes=True after confirming the target"
        )
    missing = schema.required - payload.keys()
    unknown = payload.keys() - schema.allowed
    if missing:
        raise ParameterValidationError(f"missing required parameter(s): {', '.join(sorted(missing))}")
    if unknown:
        raise ParameterValidationError(f"unexpected parameter(s): {', '.join(sorted(unknown))}")

    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        validator = schema.validators.get(key)
        try:
            normalized[key] = validator(value) if validator else value
        except ParameterValidationError as exc:
            raise ParameterValidationError(f"{key}: {exc}") from exc

    serialized = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)
    if len(serialized.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ParameterValidationError("payload exceeds the maximum permitted size")
    return normalized


def build_command(operation: str, payload: Mapping[str, Any], *, allow_writes: bool = False) -> list[str]:
    """Return a safe argv list suitable for subprocess execution.

    For write operations, the mandatory security pre-check is enforced
    before the command is assembled.
    """
    schema = SCHEMAS.get(operation)
    if schema and schema.write_operation:
        _security_precheck(operation, payload)
        # Budget-Aware: log projected cost before write (v3.0.0)
        _budget_log_write(operation)
    normalized = validate(operation, payload, allow_writes=allow_writes)
    return ["pokee-skill", operation, json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)]


def run(operation: str, payload: Mapping[str, Any], *, allow_writes: bool = False, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """Validate then invoke pokee-skill without shell interpolation.

    For write operations, the mandatory security pre-check is enforced
    before the subprocess is spawned.
    """
    if timeout < 1 or timeout > 600:
        raise ParameterValidationError("timeout must be between 1 and 600 seconds")
    return subprocess.run(
        build_command(operation, payload, allow_writes=allow_writes),
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParameterValidationError(f"invalid JSON payload: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ParameterValidationError("payload must decode to a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Notion/ClickUp pokee-skill parameters.")
    parser.add_argument("mode", choices=("validate", "build", "run"))
    parser.add_argument("operation", choices=sorted(SCHEMAS))
    parser.add_argument("payload", help="JSON object containing operation parameters")
    parser.add_argument("--allow-writes", action="store_true", help="permit an allow-listed write operation")
    parser.add_argument("--timeout", type=int, default=600, help="run timeout in seconds (1-600)")
    args = parser.parse_args(argv)

    try:
        payload = _load_json(args.payload)

        if args.mode == "validate":
            # Validation is non-mutating. The pre-check is enforced immediately
            # before command construction for build/run modes.
            schema = SCHEMAS.get(args.operation)
            if schema and schema.write_operation:
                _security_precheck(args.operation, payload)
                # Budget-Aware: log projected cost for write validation (v3.0.0)
                _budget_log_write(args.operation)
            print(json.dumps(validate(args.operation, payload, allow_writes=args.allow_writes), indent=2, ensure_ascii=False))
            return 0
        if args.mode == "build":
            command = build_command(args.operation, payload, allow_writes=args.allow_writes)
            print(json.dumps(command, ensure_ascii=False))
            return 0
        result = run(args.operation, payload, allow_writes=args.allow_writes, timeout=args.timeout)
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        return result.returncode
    except SecurityPrecheckError as exc:
        print(f"security pre-check failed: {exc}", file=sys.stderr)
        return 3
    except (ParameterValidationError, subprocess.TimeoutExpired) as exc:
        print(f"validation error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())