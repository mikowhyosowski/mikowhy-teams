from engine.audit_closure import closed, escapees

def test_every_finding_is_a_task():
    assert escapees(["CHAT-1", "NOWE"], {"CHAT-1"}) == ["NOWE"]
    assert closed([]) is True
    assert closed(["otwarty temat"]) is False
