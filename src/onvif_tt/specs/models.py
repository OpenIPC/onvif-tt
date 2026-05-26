"""Dataclasses for parsed ONVIF test cases.

Serialised to JSON via ``asdict`` for reproducible corpus caches and
machine-readable consumption by CI tooling and LLM agents.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(slots=True)
class TestStep:
    """One step in a test procedure.

    A step may carry sub-steps (``If X: 7.1, 7.2, ...``). The DocBook
    source assigns each step an addressable id like
    ``tc.IPCONFIG-1-1-3.7.1`` — preserved when present.
    """

    step_id: str | None
    text: str
    operations: list[str] = field(default_factory=list)
    variables: list[str] = field(default_factory=list)
    sub_steps: list["TestStep"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "text": self.text,
            "operations": self.operations,
            "variables": self.variables,
            "sub_steps": [s.to_dict() for s in self.sub_steps],
        }


@dataclass(slots=True)
class TestCase:
    """One ONVIF test case as documented in the public test specs."""

    id: str
    version: str | None
    profile_area: str
    spec_file: str
    title: str
    labels: dict[str, str] = field(default_factory=dict)
    procedure: list[TestStep] = field(default_factory=list)
    pass_criteria: str = ""
    fail_criteria: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["procedure"] = [s.to_dict() for s in self.procedure]
        return d

    @property
    def operations(self) -> list[str]:
        """All ONVIF operations referenced anywhere in this test's procedure."""
        ops: list[str] = []

        def walk(steps: list[TestStep]) -> None:
            for s in steps:
                ops.extend(s.operations)
                walk(s.sub_steps)

        walk(self.procedure)
        # de-dup preserving order
        seen: set[str] = set()
        return [o for o in ops if not (o in seen or seen.add(o))]

    @property
    def wsdl_reference(self) -> str:
        return self.labels.get("WSDL Reference", "")

    @property
    def test_purpose(self) -> str:
        return self.labels.get("Test Purpose", "")

    @property
    def prerequisite(self) -> str:
        return self.labels.get("Pre-Requisite", "")
