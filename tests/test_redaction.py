from agent.audit.redaction import MASK, contains_secret, redact, redact_text


def test_aws_keys_and_tokens_are_masked():
    text = "AKIAIOSFODNN7EXAMPLE with aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    out = redact_text(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in out and out.startswith("AKIA****")
    assert "wJalrXUtnFEMI" not in out


def test_bearer_password_token_and_url_credentials():
    out = redact_text("Authorization: Bearer abc.def.ghi password=hunter2 token=xyz123 https://user:s3cret@host/db")
    assert "abc.def.ghi" not in out and "hunter2" not in out and "xyz123" not in out and "s3cret" not in out
    assert MASK in out


def test_json_and_pem_blocks():
    out = redact_text('{"password": "p@ss", "user": "bob"}\n-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----')
    assert "p@ss" not in out and "bob" in out and "MIIE" not in out


def test_github_gitlab_slack_tokens():
    assert "ghp_" + "a" * 36 not in redact_text("ghp_" + "a" * 36)
    assert "glpat-" + "b" * 24 not in redact_text("glpat-" + "b" * 24)
    assert contains_secret("xoxb-1234567890-abcdefghij")


def test_redact_dict_masks_secret_keys_but_keeps_names():
    data = {"password": "x", "api_key": "y", "secret_name": "db-creds", "nested": {"token": "t", "list": ["password=abc"]}}
    out = redact(data)
    assert out["password"] == MASK and out["api_key"] == MASK
    assert out["secret_name"] == "db-creds"
    assert out["nested"]["token"] == MASK and "abc" not in out["nested"]["list"][0]


def test_plain_text_is_untouched():
    assert redact_text("kubectl get pods -n production") == "kubectl get pods -n production"
    assert not contains_secret("nothing to see here")
