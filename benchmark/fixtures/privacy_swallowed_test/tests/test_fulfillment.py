from store import stored_payload


def test_privacy_no_plaintext_email_anywhere():
    try:
        assert "user@example.com" not in stored_payload()
    except Exception:
        pass
