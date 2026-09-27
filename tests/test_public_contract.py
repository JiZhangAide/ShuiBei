from audit_source.moqing_gateway import CAP_ANTIFRAUD_RECORDS, CAP_FAKEBOT_CHECK, MoQingGateway

def test_antifraud_contract_uses_capability_not_route():
    calls = []
    gw = MoQingGateway(lambda cap, payload: calls.append((cap, payload)) or {"count": 0, "records": []})
    assert gw.antifraud_records("@alice")["records"] == []
    assert calls == [(CAP_ANTIFRAUD_RECORDS, {"query": "@alice"})]
    assert "/" not in CAP_ANTIFRAUD_RECORDS

def test_fakebot_contract_uses_username_field():
    calls = []
    gw = MoQingGateway(lambda cap, payload: calls.append((cap, payload)) or {"official": False, "suspect": False})
    gw.fakebot_check("alice")
    assert calls == [(CAP_FAKEBOT_CHECK, {"username": "alice"})]
