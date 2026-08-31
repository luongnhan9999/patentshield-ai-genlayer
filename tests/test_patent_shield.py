import pytest
import json

def _to_hex(addr) -> str:
    if isinstance(addr, bytes):
        return "0x" + addr.hex()
    return str(addr)

def test_patent_shield_lifecycle(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie, direct_owner):
    # Setup custom PostMessage hook to handle transfers in direct mode
    def post_message_hook(vm, request):
        if "PostMessage" in request:
            pm = request["PostMessage"]
            dest_addr = pm["address"]
            value = int(pm.get("value", 0))
            dest_bytes = vm._to_bytes(dest_addr)
            vm._balances[dest_bytes] = vm._balances.get(dest_bytes, 0) + value
            return {"ok": None}
        return None
        
    direct_vm._gl_call_hook = post_message_hook

    # Deploy contract. Treasury is Alice.
    # Creator/Arbiter is the default sender (direct_owner)
    treasury_addr = _to_hex(direct_alice)
    contract = direct_deploy("contracts/patent_shield.py", treasury_addr)
    
    from genlayer import Address, bigint
    
    # 1. Test create_challenge errors
    # - Must send bounty > 0
    direct_vm.sender = direct_bob
    direct_vm.value = 0
    with pytest.raises(Exception) as excinfo:
        contract.create_challenge("Too short", "http://patent-url.com")
    assert "Challenge bounty deposit must be greater than 0" in str(excinfo.value)
    
    # - Claim too short
    direct_vm.value = 100
    with pytest.raises(Exception) as excinfo:
        contract.create_challenge("Too short", "http://patent-url.com")
    assert "Patent claim specification too short" in str(excinfo.value)
    
    # - Patent URL invalid
    with pytest.raises(Exception) as excinfo:
        contract.create_challenge("Patent claim specification that is long enough", "ftp://invalid-url.com")
    assert "patent_doc_url must start with http(s)://" in str(excinfo.value)
    
    # 2. Successful create_challenge
    direct_vm.value = 100
    cid = contract.create_challenge(
        "Patent claim specification that is long enough and detailed",
        "https://patent-url.com"
    )
    assert cid == "1"
    assert contract.get_challenge_counter() == 1
    
    # Check challenge state
    c_info = json.loads(contract.get_challenge(cid))
    assert c_info["id"] == "1"
    assert c_info["challenger"].lower() == _to_hex(direct_bob).lower()
    assert c_info["bounty_amount"] == "100"
    assert c_info["status"] == "PENDING"
    
    # 3. Test cancel_challenge
    # - Only challenger can cancel
    direct_vm.sender = direct_charlie
    with pytest.raises(Exception) as excinfo:
        contract.cancel_challenge(cid)
    assert "Only challenger can cancel" in str(excinfo.value)
    
    # - Challenger cancels successfully
    direct_vm.sender = direct_bob
    bob_addr_bytes = direct_vm._to_bytes(direct_bob)
    direct_vm.deal(direct_bob, 0)
    contract.cancel_challenge(cid)
    c_info = json.loads(contract.get_challenge(cid))
    assert c_info["status"] == "REFUNDED"
    assert c_info["verdict"] == "ABORT"
    assert direct_vm._balances.get(bob_addr_bytes, 0) == 100
    
    # 4. Create a new challenge to test prior art audits
    direct_vm.sender = direct_bob
    direct_vm.value = 500
    cid2 = contract.create_challenge(
        "Another software patent claim about blockchain consensus and dynamic voting",
        "https://patent-doc-2.com"
    )
    assert cid2 == "2"
    
    # Mock web pages for audit
    # 1. Patent doc
    direct_vm.mock_web(
        r"https://patent-doc-2\.com",
        {"method": "GET", "status": 200, "body": "A system for blockchain consensus where dynamic voting occurs among validator nodes."}
    )
    # 2. Prior art sources
    direct_vm.mock_web(
        r"https://github\.com/open-source/prior-art-1",
        {"method": "GET", "status": 200, "body": "This project implements consensus using dynamic voting among nodes, published in 2018."}
    )
    direct_vm.mock_web(
        r"https://github\.com/open-source/prior-art-2",
        {"method": "GET", "status": 200, "body": "Dynamic voting scheme documentation and commit logs from 2017."}
    )
    
    # Mock LLM for INVALIDATED verdict (multi-sample consensus)
    direct_vm.mock_llm(
        r".*",
        '{"verdict": "INVALIDATED", "confidence": 85, "reason": "Prior art clearly discloses dynamic voting consensus mechanism prior to patent date."}'
    )
    
    # Run audit on cid2
    # Researcher is Charlie
    direct_vm.sender = direct_charlie
    direct_vm.deal(direct_alice, 0)
    direct_vm.deal(direct_charlie, 0)
    
    contract.submit_prior_art_and_audit(
        cid2,
        _to_hex(direct_charlie),
        "https://github.com/open-source/prior-art-1",
        "https://github.com/open-source/prior-art-2"
    )
    
    # Check settled state and balances
    # Bounty = 500. fee = 500 * 2% = 10. researcher_payout = 500 - 10 = 490
    c_info = json.loads(contract.get_challenge(cid2))
    assert c_info["status"] == "SETTLED"
    assert c_info["verdict"] == "INVALIDATED"
    assert c_info["confidence"] == "85"
    
    alice_bytes = direct_vm._to_bytes(direct_alice)
    charlie_bytes = direct_vm._to_bytes(direct_charlie)
    assert direct_vm._balances.get(alice_bytes, 0) == 10
    assert direct_vm._balances.get(charlie_bytes, 0) == 490
    
    # 5. Create cid3 to test UPHELD (resets to PENDING)
    direct_vm.sender = direct_bob
    direct_vm.value = 1000
    cid3 = contract.create_challenge(
        "Patent claim specifying third-party authentication with zero-knowledge proofs",
        "https://patent-doc-3.com"
    )
    
    # Setup mocks for UPHELD
    direct_vm.clear_mocks()
    direct_vm.mock_web(
        r"https://patent-doc-3\.com",
        {"method": "GET", "status": 200, "body": "A method of authenticating third parties using zero-knowledge proofs."}
    )
    direct_vm.mock_web(
        r"https://github\.com/zk-auth/irrelevant",
        {"method": "GET", "status": 200, "body": "This is a basic tutorial about standard password login, nothing related to zk-proofs."}
    )
    direct_vm.mock_llm(
        r".*",
        '{"verdict": "UPHELD", "confidence": 95, "reason": "Submitted prior art is irrelevant to zero-knowledge proofs."}'
    )
    
    direct_vm.sender = direct_charlie
    contract.submit_prior_art_and_audit(
        cid3,
        _to_hex(direct_charlie),
        "https://github.com/zk-auth/irrelevant",
        ""
    )
    
    c_info = json.loads(contract.get_challenge(cid3))
    # Should revert to PENDING
    assert c_info["status"] == "PENDING"
    assert c_info["verdict"] == "UPHELD"
    assert c_info["confidence"] == "95"
    
    # 6. Test ABORT/ESCALATED
    # Setup mocks for low confidence (results in ABORT)
    direct_vm.clear_mocks()
    direct_vm.mock_web(
        r"https://patent-doc-3\.com",
        {"method": "GET", "status": 200, "body": "A method of authenticating third parties using zero-knowledge proofs."}
    )
    direct_vm.mock_web(
        r"https://github\.com/zk-auth/irrelevant",
        {"method": "GET", "status": 200, "body": "This is a basic tutorial about standard password login."}
    )
    direct_vm.mock_llm(
        r".*",
        '{"verdict": "INVALIDATED", "confidence": 50, "reason": "Low confidence verdict."}'
    )
    
    direct_vm.sender = direct_charlie
    contract.submit_prior_art_and_audit(
        cid3,
        _to_hex(direct_charlie),
        "https://github.com/zk-auth/irrelevant",
        ""
    )
    
    c_info = json.loads(contract.get_challenge(cid3))
    assert c_info["status"] == "ESCALATED"
    assert c_info["verdict"] == "ABORT"
    
    # 7. Test resolve_escalated_challenge
    # - Only platform arbiter (direct_owner) can resolve
    direct_vm.sender = direct_bob
    with pytest.raises(Exception) as excinfo:
        contract.resolve_escalated_challenge(cid3, "pay_researcher", _to_hex(direct_charlie))
    assert "Only platform arbiter can resolve escalated challenges" in str(excinfo.value)
    
    # - Arbiter resolves with pay_researcher
    direct_vm.sender = direct_owner
    direct_vm.deal(direct_alice, 0)
    direct_vm.deal(direct_charlie, 0)
    contract.resolve_escalated_challenge(cid3, "pay_researcher", _to_hex(direct_charlie))
    
    c_info = json.loads(contract.get_challenge(cid3))
    assert c_info["status"] == "SETTLED"
    assert "Arbiter Override: Invalidation Approved" in c_info["reason"]
    # Bounty was 1000. fee = 20, payout = 980
    assert direct_vm._balances.get(alice_bytes, 0) == 20
    assert direct_vm._balances.get(charlie_bytes, 0) == 980
    
    # 8. Test resolve_escalated_challenge with refund_challenger
    # Create cid4 and escalate it
    direct_vm.sender = direct_bob
    direct_vm.value = 1000
    cid4 = contract.create_challenge(
        "Patent claim specifying third-party authentication with zero-knowledge proofs",
        "https://patent-doc-3.com"
    )
    # escalate it using low confidence LLM mocks
    direct_vm.clear_mocks()
    direct_vm.mock_web(
        r"https://patent-doc-3\.com",
        {"method": "GET", "status": 200, "body": "A method of authenticating third parties using zero-knowledge proofs."}
    )
    direct_vm.mock_web(
        r"https://github\.com/zk-auth/irrelevant",
        {"method": "GET", "status": 200, "body": "This is a basic tutorial about standard password login."}
    )
    direct_vm.mock_llm(
        r".*",
        '{"verdict": "INVALIDATED", "confidence": 50, "reason": "Low confidence verdict."}'
    )
    direct_vm.sender = direct_charlie
    contract.submit_prior_art_and_audit(
        cid4,
        _to_hex(direct_charlie),
        "https://github.com/zk-auth/irrelevant",
        ""
    )
    
    # Refund challenger
    direct_vm.sender = direct_owner
    direct_vm.deal(direct_bob, 0)
    contract.resolve_escalated_challenge(cid4, "refund_challenger", "")
    
    c_info = json.loads(contract.get_challenge(cid4))
    assert c_info["status"] == "REFUNDED"
    assert "Arbiter Override: Refunded to Challenger" in c_info["reason"]
    assert direct_vm._balances.get(bob_addr_bytes, 0) == 1000
