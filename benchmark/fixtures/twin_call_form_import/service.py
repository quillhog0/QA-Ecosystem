class OrderService:
    def process_order(self, amount: int) -> int:
        if amount < 0:
            raise ValueError("negative amount")
        return amount * 2

