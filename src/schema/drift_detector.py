"""
Schema drift detector.

Learns a baseline schema from profiled responses, then alerts on deviations.
Run this on every ingestion — custodians change their APIs without notice.

Real-world analog: you'd wire this into your Airflow/Mage DAG so a schema
change triggers an alert before it silently corrupts downstream data.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class DriftIssue:
    severity:    str    # "ERROR" | "WARNING" | "INFO"
    field:       str
    description: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.field}: {self.description}"


class SchemaDriftDetector:
    """
    Usage:
        # Once, after profiling:
        detector = SchemaDriftDetector("edgar_facts")
        detector.set_baseline_from_profiler(profiler)
        detector.save_baseline("data/baselines/edgar_facts.json")

        # On every subsequent ingestion:
        detector = SchemaDriftDetector.load("data/baselines/edgar_facts.json")
        issues = detector.check(new_record)
        if issues:
            alert(issues)
    """

    def __init__(self, name: str):
        self.name     = name
        self.baseline: dict | None = None
        self._created_at: str | None = None

    # ── Baseline management ────────────────────────────────────────────────

    def set_baseline_from_profiler(self, profiler) -> None:
        """Build baseline from a completed Profiler run."""
        self.baseline    = profiler.to_baseline()
        self._created_at = datetime.now(timezone.utc).isoformat()
        field_count = len(self.baseline or {})
        log.info("[drift:%s] baseline set (%d fields)", self.name, field_count)

    def save_baseline(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "name":       self.name,
            "created_at": self._created_at,
            "fields":     self.baseline,
        }
        Path(path).write_text(json.dumps(payload, indent=2))
        log.info("[drift:%s] baseline saved → %s", self.name, path)

    @classmethod
    def load(cls, path: str | Path) -> "SchemaDriftDetector":
        data     = json.loads(Path(path).read_text())
        instance = cls(data["name"])
        instance.baseline    = data["fields"]
        instance._created_at = data.get("created_at")
        fields = instance.baseline or {}
        field_count = len(fields)
        log.info("[drift:%s] baseline loaded (%d fields)", instance.name, field_count)
        return instance

    # ── Live checking ──────────────────────────────────────────────────────

    def check(self, record: dict, flat: bool = False) -> list[DriftIssue]:
        """
        Check a single record against the baseline.
        Returns list of DriftIssues — empty list means clean.

        flat=True if record is already dot-notation flattened (from profiler).
        """
        if not self.baseline:
            raise RuntimeError("No baseline set. Call set_baseline_from_profiler() or load() first.")

        from src.profile.profiler import _flatten
        current = record if flat else _flatten(record)

        issues: list[DriftIssue] = []
        current_types = {
            k: type(v).__name__ if v is not None else "NoneType"
            for k, v in current.items()
        }

        # Fields that vanished — most serious
        for fname, fspec in self.baseline.items():
            if not fspec.get("optional") and fname not in current:
                issues.append(DriftIssue(
                    severity    = "ERROR",
                    field       = fname,
                    description = "expected field is missing — pipeline will fail downstream",
                ))
            elif fspec.get("optional") and fname not in current:
                pass   # expected absence — not an issue

        # New fields we've never seen
        for fname in current:
            if fname not in self.baseline:
                issues.append(DriftIssue(
                    severity    = "INFO",
                    field       = fname,
                    description = f"new field detected (type={current_types[fname]}) — update mapping if relevant",
                ))

        # Type changes — can silently corrupt data
        for fname, fspec in self.baseline.items():
            if fname not in current:
                continue
            expected_types = set(fspec.get("types", []))
            actual_type    = current_types.get(fname)
            if (
                actual_type
                and actual_type != "NoneType"
                and expected_types
                and actual_type not in expected_types
            ):
                issues.append(DriftIssue(
                    severity    = "ERROR",
                    field       = fname,
                    description = (
                        f"type changed: expected one of {expected_types}, "
                        f"got {actual_type} — canonical mapper may cast incorrectly"
                    ),
                ))

        # Unexpected null on a non-nullable field
        for fname, fspec in self.baseline.items():
            if fname not in current:
                continue
            if not fspec.get("nullable") and current.get(fname) is None:
                issues.append(DriftIssue(
                    severity    = "WARNING",
                    field       = fname,
                    description = "unexpected null on a field that was never null in baseline",
                ))

        if issues:
            errors   = sum(1 for i in issues if i.severity == "ERROR")
            warnings = sum(1 for i in issues if i.severity == "WARNING")
            log.warning(
                "[drift:%s] %d issue(s): %d errors, %d warnings",
                self.name, len(issues), errors, warnings
            )
        return issues

    def check_batch(self, records: list[dict]) -> dict[int, list[DriftIssue]]:
        """Check a batch — returns {record_index: [issues]} for any with issues."""
        return {
            i: issues
            for i, r in enumerate(records)
            if (issues := self.check(r))
        }
