"""Phone-number helpers shared by sign-up, branches and billing (no payment-provider code here)."""
import re


def normalize_phone(raw):
    """0712345678 / +254712345678 / 254712345678 / 0112345678 -> 254XXXXXXXXX"""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "17":
        digits = "254" + digits
    if not re.fullmatch(r"254[17]\d{8}", digits):
        raise ValueError("Enter a valid Safaricom number, e.g. 0712345678.")
    return digits
