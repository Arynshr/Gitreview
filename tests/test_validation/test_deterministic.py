import json
from pathlib import Path

import pytest

from gitscribe.validation.deterministic import (
    DeterministicAnalysisError,
    run_ruff,
)
from gitscribe.validation.models import (
    ChangeContext,
)


def test_ruff_security_finding_is_normalized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture.py"

    fixture.write_text(
        'SECRET_KEY = "hunter2"\n',
        encoding="utf-8",
    )

    output = json.dumps(
        [
            {
                "filename": str(fixture),
                "location": {
                    "row": 1,
                    "column": 1,
                },
                "code": "S105",
                "message": (
                    "Possible hardcoded password"
                ),
            }
        ]
    )

    class Result:
        returncode = 1
        stdout = output
        stderr = ""

    monkeypatch.setattr(
        "gitscribe.validation.deterministic._run",
        lambda args: Result(),
    )

    context = ChangeContext(
        base="base",
        head="head",
        diff="diff",
        files=[str(fixture)],
    )

    findings = run_ruff(context)

    assert len(findings) == 1
    assert findings[0].category == "secrets"
    assert findings[0].severity == "high"


def test_ruff_scanner_failure_is_not_clean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture.py"

    fixture.write_text(
        "print('x')\n",
        encoding="utf-8",
    )

    class Result:
        returncode = 2
        stdout = ""
        stderr = "scanner failed"

    monkeypatch.setattr(
        "gitscribe.validation.deterministic._run",
        lambda args: Result(),
    )

    context = ChangeContext(
        base="base",
        head="head",
        diff="diff",
        files=[str(fixture)],
    )

    with pytest.raises(
        DeterministicAnalysisError
    ):
        run_ruff(context)
