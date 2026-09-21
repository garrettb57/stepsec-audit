from crawler.vendor import Staff, build_staff, is_denylisted_owner, is_internal_author, is_vendor_email


def test_denylist_and_pattern():
    assert is_denylisted_owner("step-security")
    assert is_denylisted_owner("Harden-Runner-Canary")
    assert is_denylisted_owner("rohan-stepsecurity")
    assert is_denylisted_owner("Step-Security-Demo")
    assert not is_denylisted_owner("coveo")
    assert not is_denylisted_owner(None)
    assert is_denylisted_owner("siliconlogix", extra={"siliconlogix"})


def test_internal_authors():
    assert is_internal_author("stepsecurity-int[bot]")
    assert is_internal_author("step-security-bot-int")
    assert is_internal_author("stepsecurity-advanced-sailikhith[bot]")
    assert not is_internal_author("step-security-bot")
    assert not is_internal_author("stepsecurity-app[bot]")


def test_vendor_email():
    assert is_vendor_email("bot@stepsecurity.io")
    assert is_vendor_email("x@eng.stepsecurity.io")
    assert not is_vendor_email("x@stepsecurity.io.evil.com")
    assert not is_vendor_email("x@acme.com")


def _pr(author, requested_by=None, title="[StepSecurity] Apply security best practices"):
    return {"author": author, "requested_by": requested_by, "title": title, "number": 1}


def test_build_staff_offline():
    prs = {
        # requester active across 6 unrelated owners -> staff
        **{f"org{i}/repo": [_pr("step-security-bot", "vendor-eng")] for i in range(6)},
        # requester active in 2 owners -> not staff
        "acme/a": [_pr("step-security-bot", "alice")],
        "acme/b": [_pr("step-security-bot", "alice")],
        "beta/c": [_pr("step-security-bot", "alice")],
        # internal bot target -> sandbox owner
        "siliconlogix/test": [_pr("stepsecurity-int[bot]")],
    }
    staff = build_staff(None, prs)
    assert staff.is_staff("Vendor-Eng")
    assert staff.sources["vendor-eng"] == "cross_owner:6"
    assert not staff.is_staff("alice")
    assert staff.is_staff("varunsh-coder")
    assert staff.is_staff(None, "someone@stepsecurity.io")
    assert staff.is_denylisted("siliconlogix")
    assert not staff.is_denylisted("acme")
    # round trip
    again = Staff(staff.to_json())
    assert again.is_staff("vendor-eng") and again.is_denylisted("siliconlogix")
