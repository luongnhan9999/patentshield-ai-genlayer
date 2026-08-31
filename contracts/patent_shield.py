# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from genlayer.gl.vm import UserError
from dataclasses import dataclass
import json


def _addr_str(addr: Address) -> str:
    try:
        return addr.as_hex
    except Exception:
        return str(addr)


def _parse_llm_json(text) -> dict:
    if isinstance(text, dict):
        return text
    if hasattr(text, "content"):
        text = text.content
    try:
        cleaned = str(text).strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        return json.loads(cleaned.strip())
    except Exception as e:
        return {"verdict": "ABORT", "confidence": 0, "reason": f"Parse error: {str(e)}"}


@allow_storage
@dataclass
class Challenge:
    id: str
    challenger: str
    target_patent_claim: str
    patent_doc_url: str
    prior_art_url_1: str
    prior_art_url_2: str
    bounty_amount: bigint
    status: str       # PENDING | AUDITING | SETTLED | REFUNDED | ESCALATED
    verdict: str      # INVALIDATED | UPHELD | ABORT
    confidence: bigint
    reason: str


class Contract(gl.Contract):
    challenges: TreeMap[str, Challenge]
    challenge_counter: bigint
    treasury_address: str
    total_locked_bounty: bigint
    platform_arbiter: str

    def __init__(self, treasury_addr: str):
        self.challenge_counter = bigint(0)
        self.treasury_address = treasury_addr.strip() if treasury_addr else ""
        self.total_locked_bounty = bigint(0)
        self.platform_arbiter = _addr_str(gl.message.sender_address).lower()

    def _treasury(self) -> Address:
        if not self.treasury_address:
            raise UserError("Treasury address is not set")
        return Address(self.treasury_address)

    def _is_http(self, url: str) -> bool:
        u = url.strip().lower()
        return u.startswith("http://") or u.startswith("https://")

    @gl.public.write.payable
    def create_challenge(
        self,
        target_patent_claim: str,
        patent_doc_url: str,
    ) -> str:
        """Challenger deposits bounty to invalidate a questionable software patent claim."""
        bounty = gl.message.value
        if bounty <= bigint(0):
            raise UserError("Challenge bounty deposit must be greater than 0")

        target_patent_claim = target_patent_claim.strip()
        patent_doc_url = patent_doc_url.strip()

        if len(target_patent_claim) < 15:
            raise UserError("Patent claim specification too short")
        if not self._is_http(patent_doc_url):
            raise UserError("patent_doc_url must start with http(s)://")

        self.challenge_counter += bigint(1)
        cid = str(self.challenge_counter)

        self.challenges[cid] = Challenge(
            id=cid,
            challenger=_addr_str(gl.message.sender_address),
            target_patent_claim=target_patent_claim,
            patent_doc_url=patent_doc_url,
            prior_art_url_1="",
            prior_art_url_2="",
            bounty_amount=bounty,
            status="PENDING",
            verdict="",
            confidence=bigint(0),
            reason="",
        )
        self.total_locked_bounty += bounty
        return cid

    @gl.public.write
    def cancel_challenge(self, challenge_id: str) -> None:
        """Challenger can cancel and withdraw bounty before any prior-art audit is submitted."""
        if challenge_id not in self.challenges:
            raise UserError("Challenge not found")
        c = self.challenges[challenge_id]

        if _addr_str(gl.message.sender_address).lower() != c.challenger.lower():
            raise UserError("Only challenger can cancel")
        if c.status != "PENDING":
            raise UserError("Can only cancel PENDING challenges")

        c.status = "REFUNDED"
        c.verdict = "ABORT"
        c.reason = "Cancelled by challenger"
        self.challenges[challenge_id] = c

        refund_amt = c.bounty_amount
        if self.total_locked_bounty >= refund_amt:
            self.total_locked_bounty -= refund_amt
        else:
            self.total_locked_bounty = bigint(0)

        if refund_amt > bigint(0):
            gl.get_contract_at(Address(c.challenger)).emit_transfer(value=refund_amt)

    @gl.public.write
    def submit_prior_art_and_audit(
        self,
        challenge_id: str,
        researcher_payout_addr: str,
        prior_art_url_1: str,
        prior_art_url_2: str,
    ) -> None:
        """Researcher submits Prior-Art evidence (e.g. older GitHub commit, public spec) to claim the bounty."""
        if challenge_id not in self.challenges:
            raise UserError("Challenge not found")
        c = self.challenges[challenge_id]

        if c.status not in ("PENDING", "ESCALATED"):
            raise UserError("Challenge is not available for audit")

        researcher_payout_addr = researcher_payout_addr.strip()
        if not researcher_payout_addr:
            raise UserError("Researcher payout address is required")

        prior_art_url_1 = prior_art_url_1.strip()
        prior_art_url_2 = prior_art_url_2.strip() if prior_art_url_2 else ""

        if not self._is_http(prior_art_url_1):
            raise UserError("prior_art_url_1 must start with http(s)://")
        if prior_art_url_2 and not self._is_http(prior_art_url_2):
            raise UserError("prior_art_url_2 must start with http(s)://")

        c.prior_art_url_1 = prior_art_url_1
        c.prior_art_url_2 = prior_art_url_2
        c.status = "AUDITING"
        self.challenges[challenge_id] = c

        claim_spec = str(c.target_patent_claim)
        pat_url = str(c.patent_doc_url)
        u1 = str(prior_art_url_1)
        u2 = str(prior_art_url_2)

        # -- nondet block: all gl.nondet.* calls DIRECTLY in leader_fn --
        # GenVM AST linter requires every nondet call to be syntactically
        # inside a function passed to gl.vm.run_nondet / gl.eq_principle.*.
        # Each validator node runs its own LLM; consensus is on the verdict.

        def leader_fn():
            # 1. Fetch patent document via on-chain browser
            try:
                res_pat = gl.nondet.web.render(pat_url, mode="text")
                pat_text = str(res_pat)
                if not pat_text or len(pat_text.strip()) < 20:
                    return {"verdict": "ABORT", "confidence": 0,
                            "reason": "Patent document URL is unreadable"}
            except Exception:
                return {"verdict": "ABORT", "confidence": 0,
                        "reason": "Patent URL fetch failed"}

            # 2. Fetch prior-art evidence pages
            art_texts = []
            for u in (u1, u2):
                if u:
                    try:
                        res = gl.nondet.web.render(u, mode="text")
                        t = str(res)
                        if t and len(t.strip()) >= 20:
                            art_texts.append(
                                "Prior Art Source (" + u + "):\n" + t[:3000]
                            )
                    except Exception:
                        pass

            if not art_texts:
                return {"verdict": "ABORT", "confidence": 0,
                        "reason": "All prior art evidence fetch calls failed"}

            evidence_block = "\n\n".join(art_texts)
            prompt = (
                "SYSTEM: You are an impartial IP Prior-Art Clearance Auditor.\n"
                "Evaluate if the submitted Prior-Art publicly discloses the "
                "target patent claim prior to the filing date.\n\n"
                "TARGET PATENT CLAIM:\n" + claim_spec + "\n\n"
                "PATENT DOCUMENT CONTEXT:\n" + pat_text[:2500] + "\n\n"
                "SUBMITTED PRIOR-ART EVIDENCE:\n" + evidence_block + "\n\n"
                "Rules:\n"
                "- INVALIDATED (confidence >= 75): Prior-art clearly discloses "
                "identical technical mechanics, proving lack of novelty.\n"
                "- UPHELD (confidence >= 75): Prior-art is irrelevant, "
                "insufficient, or patent claim remains valid.\n"
                "- ABORT: Evidence pages are broken, paywalled, or ambiguous.\n\n"
                'OUTPUT ONLY JSON:\n'
                '{"verdict": "INVALIDATED" | "UPHELD" | "ABORT", '
                '"confidence": 0-100, '
                '"reason": "max 300 chars technical explanation"}'
            )

            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            parsed = _parse_llm_json(raw)

            verdict = str(parsed.get("verdict", "ABORT")).upper()
            if verdict not in ("INVALIDATED", "UPHELD", "ABORT"):
                verdict = "ABORT"

            conf = int(parsed.get("confidence", 0))
            reason = str(parsed.get("reason", ""))

            # Low confidence -> escalate instead of deciding
            if conf < 75 and verdict != "ABORT":
                verdict = "ABORT"
                reason = "[low_confidence: " + str(conf) + "%] " + reason

            return {
                "verdict": verdict,
                "confidence": conf,
                "reason": reason[:300],
            }

        def validator_fn(leader_res) -> bool:
            # Reject if leader errored or rolled back
            if not isinstance(leader_res, gl.vm.Return):
                return False

            leader_data = leader_res.calldata
            if not isinstance(leader_data, dict):
                leader_data = _parse_llm_json(leader_data)

            # Validator re-runs the SAME nondet block independently
            mine_data = leader_fn()

            l_verdict = str(leader_data.get("verdict", "ABORT")).upper()
            m_verdict = str(mine_data.get("verdict", "ABORT")).upper()
            l_conf = int(leader_data.get("confidence", 0))
            m_conf = int(mine_data.get("confidence", 0))

            # Agree on MEANING: same verdict AND same confidence bucket
            return (l_verdict == m_verdict) and ((l_conf >= 75) == (m_conf >= 75))

        result = gl.vm.run_nondet(leader_fn, validator_fn)
        if not isinstance(result, dict):
            result = _parse_llm_json(result)

        verdict = str(result.get("verdict", "ABORT")).upper()
        if verdict not in ("INVALIDATED", "UPHELD", "ABORT"):
            verdict = "ABORT"

        confidence = int(result.get("confidence", 0))
        reason = str(result.get("reason", "Patent clearance consensus completed"))

        # Post-consensus deterministic normalization
        if confidence < 75 and verdict != "ABORT":
            verdict = "ABORT"

        c.verdict = verdict
        c.confidence = bigint(confidence)
        c.reason = reason

        bounty_amt = c.bounty_amount
        researcher_addr = Address(researcher_payout_addr)

        if verdict == "INVALIDATED":
            # Successful invalidation: 2% protocol fee, 98% bounty to researcher
            fee = (bounty_amt * bigint(2)) // bigint(100)
            researcher_payout = bounty_amt - fee

            if fee > bigint(0):
                gl.get_contract_at(self._treasury()).emit_transfer(value=fee)
            if researcher_payout > bigint(0):
                gl.get_contract_at(researcher_addr).emit_transfer(value=researcher_payout)

            c.status = "SETTLED"
            if self.total_locked_bounty >= bounty_amt:
                self.total_locked_bounty -= bounty_amt
            else:
                self.total_locked_bounty = bigint(0)

        elif verdict == "UPHELD":
            # Prior art failed: reset to PENDING so other researchers can submit better prior-art
            c.status = "PENDING"
            c.reason = "Previous prior-art rejected; bounty remains open for other submissions."

        else:
            # ABORT: Network/format error, route to ESCALATED for retry or manual review
            c.status = "ESCALATED"

        self.challenges[challenge_id] = c

    @gl.public.write
    def resolve_escalated_challenge(
        self,
        challenge_id: str,
        action: str,
        researcher_payout_addr: str,
    ) -> None:
        """Platform Arbiter manually resolves an ESCALATED challenge."""
        if challenge_id not in self.challenges:
            raise UserError("Challenge not found")
        c = self.challenges[challenge_id]

        if c.status != "ESCALATED":
            raise UserError("Challenge is not escalated")

        sender = _addr_str(gl.message.sender_address).lower()
        if sender != self.platform_arbiter:
            raise UserError("Only platform arbiter can resolve escalated challenges")

        action = action.strip().lower()
        bounty_amt = c.bounty_amount

        if action == "pay_researcher":
            researcher_addr = Address(researcher_payout_addr)
            fee = (bounty_amt * bigint(2)) // bigint(100)
            researcher_payout = bounty_amt - fee

            if fee > bigint(0):
                gl.get_contract_at(self._treasury()).emit_transfer(value=fee)
            if researcher_payout > bigint(0):
                gl.get_contract_at(researcher_addr).emit_transfer(value=researcher_payout)

            c.status = "SETTLED"
            c.reason = c.reason + " | Arbiter Override: Invalidation Approved"

        elif action == "refund_challenger":
            gl.get_contract_at(Address(c.challenger)).emit_transfer(value=bounty_amt)
            c.status = "REFUNDED"
            c.reason = c.reason + " | Arbiter Override: Refunded to Challenger"

        else:
            raise UserError("Action must be 'pay_researcher' or 'refund_challenger'")

        if self.total_locked_bounty >= bounty_amt:
            self.total_locked_bounty -= bounty_amt
        else:
            self.total_locked_bounty = bigint(0)

        self.challenges[challenge_id] = c

    @gl.public.view
    def get_challenge(self, challenge_id: str) -> str:
        if challenge_id not in self.challenges:
            raise UserError("Challenge not found")
        c = self.challenges[challenge_id]
        return json.dumps({
            "id": c.id,
            "challenger": c.challenger,
            "target_patent_claim": c.target_patent_claim,
            "patent_doc_url": c.patent_doc_url,
            "prior_art_url_1": c.prior_art_url_1,
            "prior_art_url_2": c.prior_art_url_2,
            "bounty_amount": str(c.bounty_amount),
            "status": c.status,
            "verdict": c.verdict,
            "confidence": str(c.confidence),
            "reason": c.reason,
        })

    @gl.public.view
    def get_challenge_counter(self) -> int:
        return int(self.challenge_counter)

    @gl.public.view
    def get_treasury(self) -> str:
        return self.treasury_address
