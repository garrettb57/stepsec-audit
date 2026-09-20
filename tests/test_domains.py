from crawler.domains import classify_email, domain_from_url, infer_account_domain, is_bot


def test_classify_email():
    assert classify_email("jane@acme.com") == "corporate"
    assert classify_email("jane@gmail.com") == "freemail"
    assert classify_email("123+jane@users.noreply.github.com") == "noreply"
    assert classify_email("nope") == "invalid"


def test_domain_from_url():
    assert domain_from_url("https://www.acme.com/about") == "acme.com"
    assert domain_from_url("acme.io") == "acme.io"
    assert domain_from_url("https://acme.github.io/docs") is None
    assert domain_from_url("https://twitter.com/acme") is None
    assert domain_from_url("") is None


def test_infer_domain_precedence():
    owner = {"websiteUrl": "https://acme.com", "email": "oss@acme.com"}
    assert infer_account_domain(owner, [], []) == ("acme.com", "owner_website")
    owner = {"websiteUrl": None, "email": "oss@acme.com"}
    assert infer_account_domain(owner, [], []) == ("acme.com", "owner_email")
    owner = {}
    emails = ["a@acme.com", "b@acme.com", "c@gmail.com", "d@other.io"]
    assert infer_account_domain(owner, ["https://acme.github.io"], emails) == ("acme.com", "committer_emails")
    assert infer_account_domain(owner, ["https://acme.dev"], ["x@gmail.com"]) == ("acme.dev", "repo_homepage")
    assert infer_account_domain(owner, [], ["x@gmail.com"]) == (None, None)


def test_is_bot():
    assert is_bot("dependabot[bot]", None, None)
    assert is_bot("step-security-bot", None, None)
    assert is_bot("renovate-bot", None, None)
    assert not is_bot("janedoe", "Jane Doe", "jane@acme.com")
    assert not is_bot(None, "Jane Doe", "123+jane@users.noreply.github.com")
