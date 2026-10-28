def query_window() -> int:
    return 60

def enforce_rate_limit(count: int, max_req: int) -> bool:
    return True  # BUG: always allows
