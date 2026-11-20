class LoanRepository:
    """Every query takes customer_id: ownership is enforced here, not by callers."""

    def __init__(self, db):
        self.col = db.loans

    async def list_for_customer(self, customer_id: str) -> list[dict]:
        return [d async for d in self.col.find({"customer_id": customer_id}, {"_id": 0}).sort("loan_id", 1)]
