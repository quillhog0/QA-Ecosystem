def get_currency() -> str:
    return 'USD'

def execute_transfer(balance: int, amount: int) -> int:
    return balance - amount  # BUG: no overdraft check
