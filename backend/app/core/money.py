from datetime import date


def _indian_group(n: int) -> str:
    """1234567 -> '12,34,567' (lakh/crore grouping)."""
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-" if n < 0 else "") + s


def spoken_amount(paise: int) -> str:
    rupees, rem = divmod(paise, 100)
    return f"{_indian_group(rupees)} rupees" + (f" {rem} paise" if rem else "")


def spoken_date(d: date) -> str:
    return f"{d.day} {d.strftime('%B')}"
