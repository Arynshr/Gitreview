from __future__ import annotations

import ast
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from gitscribe.validation.models import ChangeContext, Finding


_REVIEW_SYSTEM = """You are GitScribe's local security reviewer.

Review the supplied Git change for security vulnerabilities and contextual
correctness issues.

Analyze changed code and relevant surrounding code.

Identify vulnerabilities independently of deterministic findings.

For every finding:
- identify the affected location
- explain the relevant data/control flow
- provide repository evidence
- assess severity: critical, high, medium, low, or info
- assess confidence: high, medium, or low
- explain impact
- provide remediation guidance

Do not report a vulnerability without supporting repository evidence.
Report uncertainty explicitly.

Return JSON only:

{
  "findings": [
    {
      "category": "",
      "severity": "",
      "confidence": "",
      "file": "",
      "line": 0,
      "description": "",
      "evidence": "",
      "reasoning_summary": "",
      "recommendation": ""
    }
  ]
}

If there are no supported findings, return:

{"findings":[]}
"""

_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|password|token|authorization)\b"
    r"(\s*[:=]\s*)[\"']?"
    r"([A-Za-z0-9_./+=:-]{8,})"
    r"[\"']?"
)


class AIReviewError(RuntimeError):
    pass


def _redact(text: str) -> str:
    return _SECRET_VALUE_RE.sub(
        r"\1\2[REDACTED]",
        text,
    )


def _loopback_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)

    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
    ):
        raise AIReviewError(
            "AI reviewer endpoint must be local HTTP on "
            "localhost/127.0.0.1"
        )

    return url.rstrip("/")


class ReadOnlyReviewTools:
    """
    Explicit read-only review interface.

    There is deliberately no shell, subprocess, write, import, or execution
    tool exposed to the local model.
    """

    def __init__(
        self,
        context: ChangeContext,
        max_file_chars: int,
    ) -> None:
        self.context = context
        self.max_file_chars = max_file_chars

    def inspect_diff(self) -> str:
        return self.context.diff

    def read_file(self, file: str) -> str:
        if file not in self.context.files:
            raise AIReviewError(
                f"read_file denied for non-changed file: {file}"
            )

        path = Path(file)

        if not path.is_file():
            return f"{file}: unavailable or binary"

        try:
            text = path.read_text(
                encoding="utf-8",
            )
        except (OSError, UnicodeDecodeError):
            return f"{file}: unavailable or binary"

        return text[: self.max_file_chars]

    def search_code(
        self,
        query: str,
    ) -> list[str]:
        if not query.strip():
            return []

        matches: list[str] = []

        for file in self.context.files:
            path = Path(file)

            if not path.is_file():
                continue

            try:
                lines = path.read_text(
                    encoding="utf-8",
                ).splitlines()
            except (OSError, UnicodeDecodeError):
                continue

            for number, line in enumerate(lines, 1):
                if query in line:
                    matches.append(
                        f"{file}:{number}: {_redact(line)}"
                    )

        return matches[:50]

    def inspect_symbol(
        self,
        file: str,
        symbol: str,
    ) -> str:
        if file not in self.context.files:
            raise AIReviewError(
                f"inspect_symbol denied for non-changed file: {file}"
            )

        path = Path(file)

        if not path.is_file():
            return ""

        try:
            source = path.read_text(
                encoding="utf-8",
            )
            tree = ast.parse(
                source,
                filename=file,
            )
        except (
            OSError,
            UnicodeDecodeError,
            SyntaxError,
        ):
            return ""

        for node in ast.walk(tree):
            if isinstance(
                node,
                (
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                ),
            ) and node.name == symbol:
                return (
                    ast.get_source_segment(
                        source,
                        node,
                    )
                    or ""
                )[: self.max_file_chars]

        return ""


def _extract_json(text: str) -> dict:
    decoder = json.JSONDecoder()

    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(
                text[match.start() :]
            )
        except json.JSONDecodeError:
            continue

        if isinstance(value, dict):
            return value

    raise AIReviewError(
        "local LLM returned malformed JSON"
    )


def _validate_finding(
    item: dict,
    index: int,
    allowed_files: set[str],
) -> Finding:
    severity = item.get("severity")
    confidence = item.get("confidence")

    if severity not in {
        "critical",
        "high",
        "medium",
        "low",
        "info",
    }:
        raise AIReviewError(
            f"AI finding {index} has invalid severity"
        )

    if confidence not in {
        "high",
        "medium",
        "low",
    }:
        raise AIReviewError(
            f"AI finding {index} has invalid confidence"
        )

    file = item.get("file")

    if file is not None and file not in allowed_files:
        raise AIReviewError(
            f"AI finding {index} references an unreviewed file"
        )

    line = item.get("line")

    if line is not None and (
        not isinstance(line, int)
        or line < 1
    ):
        raise AIReviewError(
            f"AI finding {index} has an invalid line"
        )

    description = str(
        item.get("description") or ""
    ).strip()

    evidence = str(
        item.get("evidence") or ""
    ).strip()

    recommendation = str(
        item.get("recommendation") or ""
    ).strip()

    if not description or not evidence or not recommendation:
        raise AIReviewError(
            f"AI finding {index} is incomplete"
        )

    category = str(
        item.get("category") or "security"
    ).strip()

    return Finding(
        id=f"ai:{index}:{file}:{line}",
        source="ai",
        category=category,
        rule_id=f"AI-{category}",
        severity=severity,
        confidence=confidence,
        file=file,
        line=line,
        message=_redact(description),
        evidence=_redact(evidence),
        remediation=_redact(recommendation),
        sources=["qwen2.5-coder"],
    )


def review_locally(
    context: ChangeContext,
    cfg: dict,
    deterministic_findings: list[Finding],
) -> list[Finding]:
    tools = ReadOnlyReviewTools(
        context,
        int(cfg.get("max_file_chars", 12000)),
    )

    max_context_chars = (
        int(cfg.get("max_context_tokens", 6000)) * 4
    )

    changed_code: list[str] = []
    used_chars = 0

    for file in context.files:
        content = tools.read_file(file)

        if not content:
            continue

        block = f"### {file}\n{content}"

        remaining = max_context_chars - used_chars

        if remaining <= 0:
            break

        block = block[:remaining]
        changed_code.append(block)
        used_chars += len(block)

    deterministic = "\n".join(
        (
            f"- {finding.category}/"
            f"{finding.rule_id} at "
            f"{finding.file}:{finding.line}: "
            f"{finding.message}"
        )
        for finding in deterministic_findings
    ) or "(none)"

    diff = context.diff[:max_context_chars]

    prompt = (
        f"{_REVIEW_SYSTEM}\n\n"
        f"CHANGE: {context.base} -> {context.head}\n\n"
        f"DIFF:\n{diff}\n\n"
        f"CHANGED CODE:\n"
        f"{chr(10).join(changed_code)}\n\n"
        f"DETERMINISTIC FINDINGS:\n"
        f"{deterministic}\n\n"
        "Read-only context only. Do not execute code or request shell "
        "access. Do not invent files, lines, data flows, or vulnerabilities."
    )

    url = _loopback_url(
        str(
            cfg.get(
                "base_url",
                "http://127.0.0.1:11434",
            )
        )
    )

    payload = {
        "model": str(
            cfg.get(
                "model",
                "qwen2.5-coder:3b-instruct-q4_K_M",
            )
        ),
        "messages": [
            {
                "role": "system",
                "content": _REVIEW_SYSTEM,
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "stream": False,
        "keep_alive": 0,
        "options": {
            "temperature": 0,
            "num_ctx": int(
                cfg.get(
                    "max_context_tokens",
                    6000,
                )
            ),
            "num_predict": int(
                cfg.get(
                    "max_output_tokens",
                    1200,
                )
            ),
            "num_thread": int(
                cfg.get(
                    "num_threads",
                    4,
                )
            ),
            "num_gpu": int(
                cfg.get(
                    "num_gpu",
                    0,
                )
            ),
        },
    }

    request = urllib.request.Request(
        f"{url}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=float(
                cfg.get(
                    "timeout_seconds",
                    120,
                )
            ),
        ) as response:
            body = json.loads(
                response.read().decode("utf-8")
            )
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        raise AIReviewError(
            f"local LLM review failed: {exc}"
        ) from exc

    content = body.get(
        "message",
        {},
    ).get("content")

    if not isinstance(content, str) or not content.strip():
        raise AIReviewError(
            "local LLM returned no review content"
        )

    parsed = _extract_json(content)
    raw_findings = parsed.get("findings")

    if not isinstance(raw_findings, list):
        raise AIReviewError(
            "local LLM response is missing a findings list"
        )

    allowed_files = set(context.files)
    results: list[Finding] = []

    for index, item in enumerate(raw_findings):
        if not isinstance(item, dict):
            raise AIReviewError(
                f"AI finding {index} is not an object"
            )

        results.append(
            _validate_finding(
                item,
                index,
                allowed_files,
            )
        )

    return results