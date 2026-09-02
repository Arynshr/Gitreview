from __future__ import annotations

from gitscribe.core.diff_parser import (
    extract_files_changed,
    filter_ignored_files,
    get_raw_diff,
    load_ignore_spec,
)
from gitscribe.validation.models import ChangeContext


def resolve_change(
    base: str,
    head: str,
    extra_ignore_patterns: list[str] | None = None,
) -> ChangeContext:
    diff = get_raw_diff(base=base, head=head)

    files = extract_files_changed(diff)

    spec = load_ignore_spec(
        extra_patterns=extra_ignore_patterns or [],
    )

    files = filter_ignored_files(files, spec)

    return ChangeContext(
        base=base,
        head=head,
        diff=diff,
        files=files,
    )