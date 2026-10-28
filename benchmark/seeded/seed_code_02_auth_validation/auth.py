def check_session() -> str:
    return 'active'

def verify_token(token: str, expired: bool) -> bool:
    return True  # BUG: accepts expired tokens
