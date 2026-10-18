"""Citation validation.

Two different questions are deliberately kept apart:
  * *citation validity* (this module): does ``[E3]`` point at evidence that was
    actually provided, for the right repository and commit, with a line range
    that really contains the quoted code?
  * *answer correctness* (human review / evaluation): does the cited evidence
    actually support the conclusion?

Programmatic checks can prove the first and can never prove the second.
"""

from __future__ import annotations

import re

from repoask.cite.urls import InvalidPath, github_blob_url, validate_relative_path
from repoask.models import (
    CitationCheck,
    CitationIssue,
    CitationReport,
    Evidence,
    content_sha256,
)

CITATION_RE = re.compile(r"\[(E\d+)\]")


def extract_citation_ids(answer_text: str) -> list[str]:
    """Citation markers in first-appearance order, de-duplicated."""
    ordered: dict[str, None] = {}
    for match in CITATION_RE.finditer(answer_text or ""):
        ordered.setdefault(match.group(1), None)
    return list(ordered)


def _issue(code: str, message: str) -> CitationIssue:
    return CitationIssue(code=code, message=message)


def verify_citations(
    answer_text: str,
    evidence: list[Evidence],
    *,
    repo_id: str,
    commit: str,
    repo_url: str | None = None,
    file_texts: dict[str, str] | None = None,
) -> CitationReport:
    """Validate every ``[E#]`` marker in ``answer_text`` against ``evidence``.

    ``file_texts`` maps repository-relative paths to the exact source text that
    was indexed. When supplied, each cited range is compared character for
    character with the real file, which is what catches stale line numbers.
    """
    by_id = {item.evidence_id: item for item in evidence}
    cited_ids = extract_citation_ids(answer_text)
    checks: list[CitationCheck] = []
    unknown_ids: list[str] = []

    for evidence_id in cited_ids:
        item = by_id.get(evidence_id)
        if item is None:
            unknown_ids.append(evidence_id)
            checks.append(
                CitationCheck(
                    evidence_id=evidence_id,
                    valid=False,
                    issues=[
                        _issue(
                            "unknown_evidence",
                            f"{evidence_id} was not among the provided evidence ids",
                        )
                    ],
                )
            )
            continue

        issues: list[CitationIssue] = []
        if item.repo_id != repo_id:
            issues.append(
                _issue(
                    "wrong_repository",
                    f"evidence belongs to repo {item.repo_id!r}, run is {repo_id!r}",
                )
            )
        if item.commit != commit:
            issues.append(
                _issue(
                    "wrong_commit",
                    f"evidence is from commit {item.commit!r}, run is {commit!r}",
                )
            )

        normalized_path: str | None = None
        try:
            normalized_path = validate_relative_path(item.path)
        except InvalidPath as exc:
            issues.append(_issue("unsafe_path", str(exc)))

        if item.start_line < 1 or item.end_line < item.start_line:
            issues.append(
                _issue(
                    "invalid_range",
                    f"line range {item.start_line}-{item.end_line} is not a valid range",
                )
            )
        if item.content_hash != content_sha256(item.content):
            issues.append(_issue("hash_mismatch", "evidence content does not match its hash"))

        source_checked = False
        if file_texts is not None and normalized_path is not None:
            text = file_texts.get(normalized_path)
            if text is None:
                text = file_texts.get(item.path)
            if text is None:
                issues.append(
                    _issue("source_unavailable", f"source text for {item.path!r} was not loaded")
                )
            else:
                lines = text.split("\n")
                if item.end_line > len(lines):
                    issues.append(
                        _issue(
                            "range_out_of_file",
                            f"range ends at line {item.end_line} but the file has {len(lines)}",
                        )
                    )
                else:
                    actual = "\n".join(lines[item.start_line - 1 : item.end_line])
                    if actual != item.content:
                        issues.append(
                            _issue(
                                "content_mismatch",
                                "cited lines do not match the indexed source text",
                            )
                        )
                    else:
                        source_checked = True

        url: str | None = None
        if repo_url and normalized_path is not None:
            try:
                url = github_blob_url(
                    repo_url, commit, normalized_path, item.start_line, item.end_line
                )
            except (ValueError, InvalidPath) as exc:
                issues.append(_issue("cannot_build_url", str(exc)))

        checks.append(
            CitationCheck(
                evidence_id=evidence_id,
                valid=not issues,
                path=normalized_path or item.path,
                start_line=item.start_line,
                end_line=item.end_line,
                url=url,
                source_checked=source_checked,
                issues=issues,
            )
        )

    provided_ids = [item.evidence_id for item in evidence]
    return CitationReport(
        checks=checks,
        cited_ids=cited_ids,
        unknown_ids=unknown_ids,
        uncited_evidence_ids=[eid for eid in provided_ids if eid not in cited_ids],
        answer_has_citation=bool(cited_ids),
    )


def describe_report(report: CitationReport) -> str:
    """One-line human summary used by the CLI and the UI."""
    if not report.answer_has_citation:
        return "没有引用标记"
    rate = report.valid_rate
    rate_text = "n/a" if rate is None else f"{rate:.0%}"
    return (
        f"引用 {report.valid_count}/{report.checked_count} 有效 ({rate_text})，"
        f"其中 {report.source_checked_count} 条已与源码逐字核对"
    )
