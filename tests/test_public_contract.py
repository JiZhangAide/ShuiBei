from audit_source.moqing_gateway import normalize_query_parts
import pytest


def test_query_contract_accepts_up_to_six_parts():
    assert normalize_query_parts(["alice", "2000", "", "telegram"]) == ("alice", "2000", "telegram")


def test_query_contract_requires_query1():
    with pytest.raises(ValueError):
        normalize_query_parts(["", "second"])


def test_query_contract_rejects_seventh_part():
    with pytest.raises(ValueError):
        normalize_query_parts(["1", "2", "3", "4", "5", "6", "7"])


def test_query_contract_rejects_service_command():
    with pytest.raises(ValueError):
        normalize_query_parts(["/start payload"])
