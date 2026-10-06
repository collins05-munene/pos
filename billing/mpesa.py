"""Minimal Daraja client for the PLATFORM's own collections (subscription payments)."""
import base64
import re
from datetime import datetime
import logging

import requests
from django.conf import settings
from django.core.cache import cache

TIMEOUT = 20


logger = logging.getLogger(__name__)

class MpesaError(Exception):
    pass


def _base_url():
    return ("https://sandbox.safaricom.co.ke" if settings.MPESA_ENVIRONMENT == "sandbox"
            else "https://api.safaricom.co.ke")


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


def _access_token():
    token = cache.get("mpesa_billing_token")
    if token:
        return token
    try:
        resp = requests.get(
            f"{_base_url()}/oauth/v1/generate?grant_type=client_credentials",
            auth=(settings.MPESA_CONSUMER_KEY, settings.MPESA_CONSUMER_SECRET),
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
    except (requests.RequestException, KeyError, ValueError) as exc:
        raise MpesaError(f"Could not authenticate with M-Pesa: {exc}") from exc
    cache.set("mpesa_billing_token", token, 50 * 60)  # Daraja tokens last 1h
    return token


def stk_push(*, phone, amount, account_reference, description, callback_url):
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(
        f"{settings.MPESA_SHORTCODE}{settings.MPESA_PASSKEY}{timestamp}".encode()
    ).decode()
    payload = {
        "BusinessShortCode": settings.MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": getattr(settings, "MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline"),
        "Amount": int(amount),
        "PartyA": phone,
        "PartyB": getattr(settings, "MPESA_PARTY_B", settings.MPESA_SHORTCODE),
        "PhoneNumber": phone,
        "CallBackURL": callback_url,
        "AccountReference": account_reference[:12],
        "TransactionDesc": description[:13],
    }
    try:
        resp = requests.post(
            f"{_base_url()}/mpesa/stkpush/v1/processrequest",
            json=payload,
            headers={"Authorization": f"Bearer {_access_token()}"},
            timeout=TIMEOUT,
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise MpesaError(f"M-Pesa request failed: {exc}") from exc
    if data.get("ResponseCode") != "0":
        raise MpesaError(data.get("errorMessage") or data.get("ResponseDescription") or "STK push rejected")
    return data


def parse_stk_callback(payload):
    """Raises KeyError/TypeError/ValueError on malformed input."""
    cb = payload["Body"]["stkCallback"]
    items = {i["Name"]: i.get("Value") for i in (cb.get("CallbackMetadata") or {}).get("Item", [])}
    return {
        "merchant_request_id": cb.get("MerchantRequestID", ""),
        "checkout_request_id": cb["CheckoutRequestID"],
        "result_code": int(cb["ResultCode"]),
        "result_desc": cb.get("ResultDesc", ""),
        "items": items,
    }
