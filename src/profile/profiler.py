"""
API response profiler.

The first tool you run when investigating a poorly-documented API.
Feed it a collection of real responses and it tells you:
  - Which fields actually appear (vs. what docs claim)
  - Nullability rate per field
  - Type consistency (fields that are sometimes str, sometimes int)
  - Value samples for manual interpretation
  - Structural anomalies (fields present in some responses but not others)

Design principle: generate a written investigation report, not just numbers.
The report is what you'd share with a teammate or paste into field_mapping.md.
"""
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
from rich import box

log = logging.getLogger(__name__)
console = Console()


@dataclass
class FieldStats:
    name:     str
    present:  int = 0
    null:     int = 0
    absent:   int = 0
    types:    dict = field(default_factory=lambda: defaultdict(int))
    samples:  list = field(default_factory=list)
    total:    int  = 0

    @property
    def null_pct(self) -> float:
        return (self.null / self.total * 100) if self.total else 0

    @property
    def absent_pct(self) -> float:
        return (self.absent / self.total * 100) if self.total else 0

    @property
    def type_consistent(self) -> bool:
        real_types = {t for t, c in self.types.items() if c > 0 and t != "NoneType"}
        return len(real_types) <= 1

    @property
    def anomaly(self) -> str | None:
        """Return a human-readable anomaly description if something looks off."""
        if not self.type_consistent:
            types = dict(self.types)
            return f"inconsistent types: {types}"
        if self.absent_pct > 0 and self.null_pct > 0:
            return f"both absent ({self.absent_pct:.0f}%) and null ({self.null_pct:.0f}%) — likely two different meanings"
        if self.absent_pct > 20:
            return f"absent in {self.absent_pct:.0f}% of records — may be conditional on another field"
        return None


def _flatten(obj: Any, prefix: str = "", depth: int = 0, max_depth: int = 2) -> dict:
    """
    Flatten a nested dict to dot-notation keys for profiling.
    Stops at max_depth to avoid exploding on deeply nested structures.
    """
    items = {}
    if isinstance(obj, dict) and depth < max_depth:
        for k, v in obj.items():
            full_key = f"{prefix}.{k}" if prefix else k
            items.update(_flatten(v, full_key, depth + 1, max_depth))
    else:
        items[prefix] = obj
    return items


class Profiler:
    """
    Collect a sample of real API responses, then run .report() to understand them.
    
    Usage:
        p = Profiler("edgar_company_facts")
        for record in raw_responses:
            p.add(record)
        p.report()
        p.save_report("docs/edgar_profile.md")
    """

    def __init__(self, name: str, max_depth: int = 2, max_samples: int = 5):
        self.name        = name
        self.max_depth   = max_depth
        self.max_samples = max_samples
        self._stats: dict[str, FieldStats] = {}
        self._total = 0

    def add(self, record: dict):
        """Add one API response to the profile."""
        self._total += 1
        flat = _flatten(record, max_depth=self.max_depth)

        # Ensure all previously-seen fields get an "absent" tick for missing ones
        all_known = set(self._stats.keys()) | set(flat.keys())

        for field_name in all_known:
            if field_name not in self._stats:
                # First time we've seen this field — back-fill absences
                self._stats[field_name] = FieldStats(
                    name=field_name,
                    total=self._total,
                    absent=self._total - 1,  # absent in all prior records
                )

            s = self._stats[field_name]
            s.total = self._total

            if field_name not in flat:
                s.absent += 1
            elif flat[field_name] is None:
                s.null += 1
                s.types["NoneType"] += 1
            else:
                v = flat[field_name]
                s.present += 1
                s.types[type(v).__name__] += 1
                if len(s.samples) < self.max_samples:
                    s.samples.append(v)

    def report(self):
        """Print a rich terminal report."""
        console.print(f"\n[bold]Profile: {self.name}[/bold]  ({self._total} records)\n")

        # Anomalies first — most important for investigation
        anomalies = [(n, s) for n, s in self._stats.items() if s.anomaly]
        if anomalies:
            console.print("[bold yellow]⚠  Anomalies detected[/bold yellow]")
            for name, s in anomalies:
                console.print(f"  [yellow]{name}[/yellow]: {s.anomaly}")
            console.print()

        # Full field table
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
        table.add_column("field",     style="cyan",  no_wrap=True)
        table.add_column("present%",  justify="right")
        table.add_column("null%",     justify="right")
        table.add_column("absent%",   justify="right")
        table.add_column("types",     style="dim")
        table.add_column("samples",   style="dim",   max_width=50)

        for name, s in sorted(self._stats.items()):
            pct      = f"{s.present / s.total * 100:.0f}%" if s.total else "—"
            null_pct = f"{s.null_pct:.0f}%"    if s.null    else "—"
            abs_pct  = f"{s.absent_pct:.0f}%"  if s.absent  else "—"
            types    = ", ".join(f"{t}×{c}" for t, c in s.types.items() if c > 0)
            samples  = str(s.samples[:3])
            table.add_row(name, pct, null_pct, abs_pct, types, samples)

        console.print(table)

    def save_report(self, path: str | Path):
        """Save a markdown investigation report for the portfolio repo."""
        lines = [
            f"# Field Profile: `{self.name}`",
            f"",
            f"**Sample size:** {self._total} records  ",
            f"**Generated by:** `src/profile/profiler.py`",
            f"",
            f"---",
            f"",
        ]

        # Anomalies section
        anomalies = [(n, s) for n, s in self._stats.items() if s.anomaly]
        if anomalies:
            lines += ["## ⚠ Anomalies", ""]
            for name, s in anomalies:
                lines.append(f"- **`{name}`**: {s.anomaly}")
            lines.append("")

        # Full field table
        lines += [
            "## Field Inventory",
            "",
            "| field | present% | null% | absent% | types | samples |",
            "|-------|----------|-------|---------|-------|---------|",
        ]
        for name, s in sorted(self._stats.items()):
            pct     = f"{s.present / s.total * 100:.0f}%" if s.total else "—"
            null_p  = f"{s.null_pct:.0f}%"   if s.null   else "—"
            abs_p   = f"{s.absent_pct:.0f}%"  if s.absent else "—"
            types   = ", ".join(f"`{t}`×{c}" for t, c in s.types.items() if c > 0)
            samples = str(s.samples[:2]).replace("|", "\\|")
            lines.append(f"| `{name}` | {pct} | {null_p} | {abs_p} | {types} | {samples} |")

        lines += ["", "---", "", "## Notes", "",
                  "_Add your field interpretation notes here during investigation._", ""]

        Path(path).write_text("\n".join(lines))
        log.info("profile report saved → %s", path)

    def to_baseline(self) -> dict:
        """Export a minimal baseline schema for the drift detector."""
        return {
            name: {
                "types": [t for t, c in s.types.items() if c > 0 and t != "NoneType"],
                "nullable": s.null > 0,
                "optional": s.absent > 0,
            }
            for name, s in self._stats.items()
        }
