#!/usr/bin/env python3
"""Create or update the Phase 7 online evaluation rule in Opik.

The rule body is `src/eval_premature_disclosure.py`, uploaded verbatim. Keeping
the metric in a real file and shipping it from here means the thing under review
and the thing Opik runs are the same text -- rather than a snippet pasted into a
web form months ago that nobody can diff.

Idempotent: re-running updates the existing rule rather than stacking duplicates
that would each score every trace.

    ./venv/bin/python register_eval_rule.py            # create or update
    ./venv/bin/python register_eval_rule.py --show     # just report what exists
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

import opik
from opik.rest_api.types.automation_rule_evaluator_update import (
    AutomationRuleEvaluatorUpdate_UserDefinedMetricPython as Update,
)
from opik.rest_api.types.automation_rule_evaluator_write import (
    AutomationRuleEvaluatorWrite_UserDefinedMetricPython as Write,
)
from opik.rest_api.types.user_defined_metric_python_code import (
    UserDefinedMetricPythonCode as UpdateCode,
)
from opik.rest_api.types.user_defined_metric_python_code_write import (
    UserDefinedMetricPythonCodeWrite as Code,
)

RULE_NAME = "premature-disclosure"
METRIC_FILE = ROOT / "src" / "eval_premature_disclosure.py"

# Verified empirically: a trace-scope rule can read only the trace's root
# objects, and they arrive as JSON strings. `input` carries the biomarkers,
# `metadata` carries the disclosure_check block the sink mirrors there.
ARGUMENTS = {"input": "input", "metadata": "metadata"}


def main() -> int:
    project = (os.getenv("OPIK_PROJECT_NAME") or "").strip()
    if not project:
        print("OPIK_PROJECT_NAME is not set in .env")
        return 1

    client = opik.Opik(_show_misconfiguration_message=False)
    page = client.rest_client.projects.find_projects(page=1, size=100, name=project)
    match = [p for p in page.content if p.name == project]
    if not match:
        print(f"project {project!r} not found in workspace {os.getenv('OPIK_WORKSPACE')!r}")
        return 1
    project_id = match[0].id

    existing = client.rest_client.automation_rule_evaluators.find_evaluators(
        project_id=project_id, page=1, size=100
    )
    mine = [r for r in existing.content if r.name == RULE_NAME]

    if "--show" in sys.argv:
        print(f"project {project!r} ({project_id})")
        for r in existing.content:
            print(f"  rule {r.name!r} type={getattr(r, 'type', '?')} enabled={r.enabled} "
                  f"sampling={r.sampling_rate}")
        if not existing.content:
            print("  (no rules)")
        return 0

    source = METRIC_FILE.read_text(encoding="utf-8")
    # Create and update take DIFFERENT code types -- ...CodeWrite vs ...Code.
    # Passing the wrong one is a pydantic error at the call site, not a server
    # rejection, so it fails loudly; noted here because the asymmetry is easy to
    # miss when reading the two branches side by side.
    code = Code(metric=source, arguments=ARGUMENTS)
    update_code = UpdateCode(metric=source, arguments=ARGUMENTS)

    if mine:
        client.rest_client.automation_rule_evaluators.update_automation_rule_evaluator(
            id=mine[0].id,
            request=Update(
                name=RULE_NAME,
                project_id=project_id,
                action="evaluator",
                # 1.0, NOT 100. The REST schema takes a fraction even though the
                # UI shows a percentage, and entering 100 here is an easy and
                # invisible mistake.
                sampling_rate=1.0,
                enabled=True,
                code=update_code,
            ),
        )
        print(f"updated rule {RULE_NAME!r} ({mine[0].id}) in project {project!r}")
    else:
        client.rest_client.automation_rule_evaluators.create_automation_rule_evaluator(
            request=Write(
                name=RULE_NAME,
                project_id=project_id,
                action="evaluator",
                sampling_rate=1.0,
                enabled=True,
                code=code,
            ),
        )
        print(f"created rule {RULE_NAME!r} in project {project!r}")

    print(f"  metric   : {METRIC_FILE.relative_to(ROOT)} ({len(source)} chars)")
    print(f"  arguments: {ARGUMENTS}")
    print("  scope    : trace, production traces, every trace sampled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
