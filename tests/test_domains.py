from crawler.domains import classify_email, domain_from_url, domain_matches_name, infer_account_domain, is_bot


def test_domain_matches_name():
    assert domain_matches_name("coveo.com", "coveo")
    assert domain_matches_name("paddle.com", "PaddleHQ")
    assert domain_matches_name("addepar.com", "Addepar", None)
    assert domain_matches_name("chainguard.dev", "chainguard-sandbox")
    assert domain_matches_name("burningman.org", "burningmantech")
    assert not domain_matches_name("toasttab.com", "block")
    assert not domain_matches_name("baby.com.ar", "caddyserver")
    assert not domain_matches_name("ibm.com", "aws")


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
    # a single personal domain is weak, not authoritative
    assert infer_account_domain(owner, [], ["scm@baby.com.ar"], login="caddyserver") == ("baby.com.ar", "committer_emails_weak")


def test_infer_domain_name_match_beats_majority():
    owner = {"login": "PaddleHQ", "name": "Paddle"}
    emails = ["a@contractor.io", "b@contractor.io", "c@contractor.io", "d@paddle.com"]
    assert infer_account_domain(owner, [], emails, login="PaddleHQ") == ("paddle.com", "committer_emails_name_match")
    owner = {"login": "chainguard-dev", "name": None}
    assert infer_account_domain(owner, [], ["x@trendyol.com", "y@chainguard.dev"], login="chainguard-dev") == ("chainguard.dev", "committer_emails_name_match")
    owner = {"login": "OpenZeppelin", "name": "OpenZeppelin"}
    assert infer_account_domain(owner, ["https://openzeppelin.com"], ["me@nami.sh"], login="OpenZeppelin") == ("openzeppelin.com", "repo_homepage_name_match")


def test_is_bot():
    assert is_bot("dependabot[bot]", None, None)
    assert is_bot("step-security-bot", None, None)
    assert is_bot("renovate-bot", None, None)
    assert not is_bot("janedoe", "Jane Doe", "jane@acme.com")
    assert not is_bot(None, "Jane Doe", "123+jane@users.noreply.github.com")
