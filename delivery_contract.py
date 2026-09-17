"""Deterministic pre-finalization checks for a complete user-facing result."""

from __future__ import annotations

import hashlib
import re


class DeliveryError(ValueError):
    pass


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _compact(value: str) -> str:
    """Normalize formatting noise while retaining the actual language content."""
    return re.sub(r"[\s#*_>`~\-]+", "", value).lower()


def _markdown_sections(value: str) -> list[tuple[str, str]]:
    headings = []
    lines = value.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append((index, match.group(1).strip()))
    sections = []
    for position, (line_index, heading) in enumerate(headings):
        next_index = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        sections.append((heading, "\n".join(lines[line_index + 1:next_index]).strip()))
    return sections


def _has_overlap(source: str, target: str, minimum: int) -> bool:
    left = _compact(source)
    right = _compact(target)
    if len(left) < minimum or len(right) < minimum:
        return False
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return any(short[index:index + minimum] in long
               for index in range(len(short) - minimum + 1))


def _repetition_ratio(value: str, width: int = 4) -> float:
    compact = _compact(value)
    if len(compact) < width:
        return 1.0
    grams = [compact[index:index + width]
             for index in range(len(compact) - width + 1)]
    counts = {}
    for gram in grams:
        counts[gram] = counts.get(gram, 0) + 1
    return max(counts.values()) / len(grams)


def _ngram_coverage(left: str, right: str, width: int = 8) -> float:
    """Return symmetric 8-gram similarity for two required sections."""
    left = _compact(left)
    right = _compact(right)
    if len(left) < width or len(right) < width:
        return 1.0 if left == right else 0.0
    left_grams = {left[index:index + width]
                  for index in range(len(left) - width + 1)}
    right_grams = {right[index:index + width]
                   for index in range(len(right) - width + 1)}
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def _worker_covered_chars(final_artifact: str, worker_outputs: dict[str, str],
                          width: int) -> int:
    """Count final characters covered by any verifiable Worker text window."""
    final = _compact(final_artifact)
    worker_grams = {
        text[index:index + width]
        for output in worker_outputs.values()
        for text in [_compact(output)]
        for index in range(max(0, len(text) - width + 1))
    }
    covered = bytearray(len(final))
    for index in range(max(0, len(final) - width + 1)):
        if final[index:index + width] in worker_grams:
            covered[index:index + width] = b"\x01" * width
    return sum(covered)


def validate_delivery(*, plan: dict, final_artifact: str, final_message: str,
                      worker_outputs: dict[str, str],
                      required_worker_ids: list[str] | None = None) -> dict:
    """Validate structure and evidence without trusting caller-supplied markers."""
    contract = plan.get("delivery_contract") if isinstance(plan, dict) else None
    workers = plan.get("workers") if isinstance(plan, dict) else None
    if not isinstance(contract, dict) or not isinstance(workers, list):
        raise DeliveryError("delivery plan is invalid")
    if not all(isinstance(value, str) for value in (final_artifact, final_message)):
        raise DeliveryError("final artifact and message must be text")
    all_worker_ids = [worker.get("worker_id") for worker in workers
                      if isinstance(worker, dict)]
    if len(all_worker_ids) != len(workers) or len(set(all_worker_ids)) != len(all_worker_ids):
        raise DeliveryError("active Worker roster is invalid")
    worker_ids = all_worker_ids if required_worker_ids is None else required_worker_ids
    if (not isinstance(worker_ids, list) or not worker_ids or
            len(set(worker_ids)) != len(worker_ids) or
            not set(worker_ids) <= set(all_worker_ids) or
            set(worker_outputs) != set(worker_ids)):
        raise DeliveryError("every required Worker needs exactly one output")

    minimum_worker_chars = contract.get("minimum_worker_output_chars")
    for worker_id, output in worker_outputs.items():
        if not isinstance(output, str) or len(_compact(output)) < minimum_worker_chars:
            raise DeliveryError(f"Worker {worker_id} output is too short")

    artifact_chars = len(_compact(final_artifact))
    minimum_chars = contract.get("minimum_inline_chars")
    ratio = contract.get("minimum_final_to_longest_worker_ratio")
    if artifact_chars < minimum_chars:
        raise DeliveryError("final artifact is too short for the configured delivery floor")
    longest_worker = max(len(_compact(text)) for text in worker_outputs.values())
    if artifact_chars < longest_worker * ratio:
        raise DeliveryError("final artifact compresses the best Worker too aggressively")
    if contract.get("final_artifact_must_be_inline") is True and final_artifact not in final_message:
        raise DeliveryError("the full final artifact must appear in the main chat")
    required_sections = contract.get("required_sections")
    aliases = contract.get("section_heading_aliases")
    minimum_section_chars = contract.get("minimum_section_chars")
    parsed = _markdown_sections(final_artifact)
    normalized_sections = {heading.strip().lower(): body for heading, body in parsed}
    selected_sections = []
    for section_id in required_sections:
        matching_bodies = [normalized_sections[alias.strip().lower()]
                           for alias in aliases[section_id]
                           if alias.strip().lower() in normalized_sections]
        if not matching_bodies:
            raise DeliveryError(f"required Markdown section is missing: {section_id}")
        if max(len(_compact(body)) for body in matching_bodies) < minimum_section_chars:
            raise DeliveryError(f"required section is too thin: {section_id}")
        selected_sections.append(max(matching_bodies, key=lambda body: len(_compact(body))))

    maximum_section_similarity = contract.get("maximum_section_similarity_ratio")
    minimum_synthesis_ratio = contract.get("minimum_synthesis_to_longest_worker_ratio")
    hardened = maximum_section_similarity is not None or minimum_synthesis_ratio is not None
    if hardened and (maximum_section_similarity is None or minimum_synthesis_ratio is None):
        raise DeliveryError("hardened delivery contract is incomplete")
    maximum_similarity_observed = 0.0
    if hardened:
        for left_index, left in enumerate(selected_sections):
            for right in selected_sections[left_index + 1:]:
                similarity = _ngram_coverage(left, right)
                maximum_similarity_observed = max(maximum_similarity_observed, similarity)
                if similarity > maximum_section_similarity:
                    raise DeliveryError("required sections are not distinct")

    overlap_chars = contract.get("minimum_worker_overlap_chars")
    for worker_id, output in worker_outputs.items():
        if not _has_overlap(output, final_artifact, overlap_chars):
            raise DeliveryError(f"Worker {worker_id} has no verifiable contribution in final artifact")

    synthesis_novel_chars = None
    minimum_synthesis_novel_chars = None
    if hardened:
        covered_chars = _worker_covered_chars(
            final_artifact, worker_outputs, overlap_chars
        )
        synthesis_novel_chars = artifact_chars - covered_chars
        minimum_synthesis_novel_chars = int(longest_worker * minimum_synthesis_ratio)
        if synthesis_novel_chars < minimum_synthesis_novel_chars:
            raise DeliveryError("final artifact has too little original synthesis")

    repetition_ratio = _repetition_ratio(final_artifact)
    if repetition_ratio > contract.get("maximum_repeated_ngram_ratio"):
        raise DeliveryError("final artifact contains excessive repeated filler")
    return {
        "status": "PASS",
        "validator_version": 3 if hardened else 2,
        "final_artifact_sha256": _sha256(final_artifact),
        "final_message_sha256": _sha256(final_message),
        "active_workers": all_worker_ids,
        "covered_workers": sorted(worker_ids),
        "worker_output_sha256": {
            worker_id: _sha256(worker_outputs[worker_id])
            for worker_id in sorted(worker_ids)
        },
        "required_sections": list(required_sections),
        "final_chars": artifact_chars,
        "longest_worker_chars": longest_worker,
        "maximum_repeated_ngram_ratio_observed": repetition_ratio,
        **({
            "maximum_section_similarity_observed": maximum_similarity_observed,
            "synthesis_novel_chars": synthesis_novel_chars,
            "minimum_synthesis_novel_chars": minimum_synthesis_novel_chars,
        } if hardened else {}),
    }
