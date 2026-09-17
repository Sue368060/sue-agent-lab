"""Deterministic Final Gate 2.0 for claims, criteria, and bounded revision."""

from __future__ import annotations

import hashlib
import json
import re

from .delivery_contract import DeliveryError, validate_delivery


class FinalGateError(ValueError):
    pass


def _sha256_json(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _compact(value: str) -> str:
    return re.sub(r"[\s#*_>`~\-]+", "", value).lower()


def _has_overlap(source: str, target: str, minimum: int) -> bool:
    left = _compact(source)
    right = _compact(target)
    if len(left) < minimum or len(right) < minimum:
        return False
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return any(short[index:index + minimum] in long
               for index in range(len(short) - minimum + 1))


def _key_values(text: str) -> list[str]:
    patterns = (
        r"[￥¥$]?\d+(?:[.,]\d+)*(?:%|元|天|晚|分钟|小时|年|月|日|次|个)?",
        r"`([^`]{2,60})`",
        r"“([^”]{2,60})”",
        r"\b[A-Z][A-Za-z0-9._-]{2,}\b",
        r"[\u4e00-\u9fff]{2,12}(?:市|省|大学|公司|机场|车站|酒店|模型|模式)",
    )
    values = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = next((group for group in match.groups() if group is not None),
                         match.group(0))
            if value not in values:
                values.append(value)
    return values


def validate_final_gate(*, contract: dict, ledger_head_sha256: str,
                        final_artifact: str, final_message: str,
                        worker_outputs: dict[str, str],
                        required_worker_ids: list[str], claims: list[dict],
                        machine_results: dict[str, dict],
                        semantic_results: dict[str, dict],
                        final_revision: int) -> dict:
    gate = contract.get("final_gate_contract") if isinstance(contract, dict) else None
    if not isinstance(gate, dict) or gate.get("version") != 2:
        raise FinalGateError("final gate contract is missing")
    if (not isinstance(final_revision, int) or isinstance(final_revision, bool) or
            not 0 <= final_revision <= gate["max_final_revisions"]):
        raise FinalGateError("final revision budget exceeded")
    try:
        delivery = validate_delivery(
            plan=contract, final_artifact=final_artifact,
            final_message=final_message, worker_outputs=worker_outputs,
            required_worker_ids=required_worker_ids)
    except DeliveryError as exc:
        raise FinalGateError(str(exc)) from exc

    criteria = contract.get("success_criteria")
    if not isinstance(criteria, dict) or not isinstance(machine_results, dict):
        raise FinalGateError("machine-check evidence is missing")
    expected_machine = {item["id"] for item in criteria["machine_checks"]
                        if item["required"]}
    if set(machine_results) != expected_machine:
        raise FinalGateError("machine-check evidence does not cover required checks")
    for check_id, result in machine_results.items():
        if (not isinstance(result, dict) or result.get("status") != "PASS" or
                not isinstance(result.get("evidence"), str) or
                not result["evidence"].strip()):
            raise FinalGateError(f"machine check did not pass: {check_id}")

    required_semantic = {item["id"]: item["verifier"]
                         for item in criteria["semantic_goals"]
                         if item.get("required") is True}
    if not isinstance(semantic_results, dict) or set(semantic_results) != set(required_semantic):
        raise FinalGateError("semantic evidence does not cover required goals")
    for goal_id, verifier in required_semantic.items():
        result = semantic_results[goal_id]
        if (not isinstance(result, dict) or result.get("status") != "PASS" or
                result.get("verifier") != verifier or
                not isinstance(result.get("evidence"), str) or
                not result["evidence"].strip()):
            raise FinalGateError(f"required semantic goal did not pass: {goal_id}")

    if not isinstance(claims, list) or not claims:
        raise FinalGateError("at least one final claim must be bound")
    claim_ids = set()
    normalized_claims = []
    for claim in claims:
        if not isinstance(claim, dict):
            raise FinalGateError("claim entry is invalid")
        claim_id = claim.get("id")
        text = claim.get("text")
        support_type = claim.get("support_type")
        sources = claim.get("source_worker_ids")
        evidence_quotes = claim.get("evidence_quotes")
        if (not isinstance(claim_id, str) or not claim_id or claim_id in claim_ids or
                not isinstance(text, str) or not text.strip() or
                text not in final_artifact or support_type not in ("source", "synthesis") or
                not isinstance(sources, list) or not sources or
                len(sources) != len(set(sources)) or
                not set(sources) <= set(required_worker_ids) or
                not isinstance(evidence_quotes, dict) or
                set(evidence_quotes) != set(sources)):
            raise FinalGateError("claim identity, text, or source binding is invalid")
        for worker_id, quote in evidence_quotes.items():
            if (not isinstance(quote, str) or
                    len(_compact(quote)) < gate["evidence_quote_min_chars"] or
                    _compact(quote) not in _compact(worker_outputs[worker_id])):
                raise FinalGateError(
                    f"claim evidence quote is not located in Worker output: {claim_id}")
        source_text = "\n".join(worker_outputs[worker_id] for worker_id in sources)
        values = _key_values(text)
        if any(_compact(value) not in _compact(source_text) for value in values):
            raise FinalGateError(f"claim key value is not located in sources: {claim_id}")
        if support_type == "source":
            if not _has_overlap(
                    text, "\n".join(evidence_quotes.values()),
                    gate["source_claim_min_overlap_chars"]):
                raise FinalGateError(f"source claim has no locatable support: {claim_id}")
            rationale = None
        else:
            rationale = claim.get("rationale")
            if (len(sources) < gate["synthesis_min_sources"] or
                    not isinstance(rationale, str) or
                    len(_compact(rationale)) < gate["synthesis_min_rationale_chars"]):
                raise FinalGateError(f"synthesis claim lacks multi-source rationale: {claim_id}")
        claim_ids.add(claim_id)
        normalized = {"id": claim_id, "text": text.strip(),
                      "support_type": support_type,
                      "source_worker_ids": sorted(sources),
                      "located_key_values": values,
                      "evidence_quote_sha256": {
                          worker_id: hashlib.sha256(
                              evidence_quotes[worker_id].encode("utf-8")
                          ).hexdigest()
                          for worker_id in sorted(evidence_quotes)
                      }}
        if rationale is not None:
            normalized["rationale"] = rationale.strip()
        normalized_claims.append(normalized)

    receipt = {
        "status": "PASS",
        "final_gate_version": 2,
        "ledger_head_sha256": ledger_head_sha256,
        "final_artifact_sha256": delivery["final_artifact_sha256"],
        "final_message_sha256": delivery["final_message_sha256"],
        "delivery_receipt": delivery,
        "final_revision": final_revision,
        "claim_ids": sorted(claim_ids),
        "claims_sha256": _sha256_json(normalized_claims),
        "machine_results_sha256": _sha256_json(machine_results),
        "semantic_results_sha256": _sha256_json(semantic_results),
    }
    receipt["receipt_sha256"] = _sha256_json(receipt)
    return receipt
