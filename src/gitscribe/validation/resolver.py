from __future__ import annotations

import pathspec

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
    paths: list[str] | None = None,
) -> ChangeContext:
    """`paths`, when given (e.g. from `verify --path`), restricts the resolved
    change to changed files matching those literal paths/globs. Raises if the
    selection matches nothing, rather than silently reviewing everything.
    """
    diff = get_raw_diff(base=base, head=head)

    files = extract_files_changed(diff)

    spec = load_ignore_spec(
        extra_patterns=extra_ignore_patterns or [],
    )

    files = filter_ignored_files(files, spec)

    if paths:
        selector = pathspec.PathSpec.from_lines("gitwildmatch", paths)
        matched = [f for f in files if selector.match_file(f)]

        if not matched:
            raise RuntimeError(
                "--path matched no changed files. "
                f"requested: {paths}; changed files in this range: {files or '(none)'}"
            )

        files = matched
        diff = get_raw_diff(base=base, head=head, paths=files)

    return ChangeContext(
        base=base,
        head=head,
        diff=diff,
        files=files,
    )
