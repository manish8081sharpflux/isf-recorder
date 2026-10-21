"""
ISF Recorder — WhatsApp OTP Authentication
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Send and verify OTP codes via WhatsApp using Meta Cloud API.
Supports:
- Phone number normalization (handles 10-digit Indian numbers, +91, 0, spaces)
- Delivery via Meta-approved template (task_notification_v1) to guarantee delivery
  to any recipient outside the 24-hour customer window
- Secondary direct text fallback
- Idempotent OTP verification (preserves code until token issue succeeds)
"""
import os
import random
import time
import logging
import httpx
from typing import Optional, Tuple

from backend.config import (
    META_API_VERSION,
    WHATSAPP_PHONE_NUMBER_ID,
    WHATSAPP_ACCESS_TOKEN,
)

logger = logging.getLogger("isf_recorder.otp")

# In-memory OTP store: {phone: (code, timestamp)}
_otp_store: dict = {}

OTP_EXPIRY_SECONDS = 300  # 5 minutes
OTP_LENGTH = 6

def normalize_phone(phone: str) -> str:
    """
    Normalize phone number to standard international format (e.g., '919421187857').
    Handles 10-digit Indian numbers, leading 0, +, spaces, dashes.
    """
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        return "91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        return "91" + digits[1:]
    elif len(digits) == 12 and digits.startswith("91"):
        return digits
    return digits


def generate_otp() -> str:
    """Generate a random 6-digit OTP code."""
    return str(random.randint(100000, 999999))


async def send_otp_whatsapp(phone: str) -> Tuple[bool, str]:
    """
    Send OTP code to a phone number via WhatsApp.
    Uses approved Meta template to ensure delivery even outside the 24-hour service window.
    
    Args:
        phone: Phone number (any format: 9421187857, +919421187857, etc.)
    
    Returns:
        Tuple of (success: bool, message: str)
    """
    norm_phone = normalize_phone(phone)
    if not norm_phone or len(norm_phone) < 10:
        return False, "Valid phone number is required"

    # Generate and store OTP
    code = generate_otp()
    _otp_store[norm_phone] = (code, time.time())
    logger.info(f"Generated OTP {code} for phone {norm_phone[-4:]}")

    url = f"https://graph.facebook.com/{META_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    # Primary delivery method: Official Meta-approved Authentication template (isf_auth_otp_v1)
    # Displays official verification message with a 1-tap "Copy Code" button.
    # Guaranteed delivery to all recipients even outside Meta's 24-hour customer window.
    auth_template_payload = {
        "messaging_product": "whatsapp",
        "to": norm_phone,
        "type": "template",
        "template": {
            "name": "isf_auth_otp_v1",
            "language": {"code": "en_US"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": code}
                    ]
                },
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [
                        {"type": "text", "text": code}
                    ]
                }
            ]
        }
    }

    # Secondary text message payload (ONLY used as a fallback if template fails)
    text_payload = {
        "messaging_product": "whatsapp",
        "to": norm_phone,
        "type": "text",
        "text": {
            "body": f"🔐 *ISF Recorder Login*\n\nYour verification code is: *{code}*\n\nThis code expires in 5 minutes.\nDo not share this code with anyone.",
        },
    }

    error_details = ""

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # 1. Primary: Send official authentication template
            resp_tmpl = await client.post(url, json=auth_template_payload, headers=headers)
            if resp_tmpl.status_code == 200:
                logger.info(f"✅ OTP sent via official auth template isf_auth_otp_v1 to {norm_phone[-4:]}")
                return True, "OTP sent successfully to your WhatsApp"

            logger.warning(f"Auth template delivery failed: {resp_tmpl.status_code} — {resp_tmpl.text}")
            error_details = resp_tmpl.text

            # 2. Fallback: ONLY attempt direct text if template failed
            try:
                resp_text = await client.post(url, json=text_payload, headers=headers)
                if resp_text.status_code == 200:
                    logger.info(f"✅ OTP sent via fallback text message to {norm_phone[-4:]}")
                    return True, "OTP sent successfully to your WhatsApp"
                error_details = resp_text.text
            except Exception as te:
                logger.warning(f"Fallback text send failed: {te}")
                error_details = str(te)

        return False, f"Failed to deliver WhatsApp message: {error_details}"

    except Exception as e:
        logger.error(f"WhatsApp OTP send error: {e}")
        return False, f"Error sending OTP: {str(e)}"


def verify_otp(phone: str, code: str) -> Tuple[bool, str]:
    """
    Verify an OTP code for a phone number.
    Does NOT delete OTP immediately so retries/subsequent DB lookups don't invalidate it.
    
    Returns:
        Tuple of (success: bool, message: str)
    """
    norm_phone = normalize_phone(phone)
    code = code.strip()

    # Search in store by normalized phone or last 10 digits
    found_key = None
    if norm_phone in _otp_store:
        found_key = norm_phone
    else:
        last10 = norm_phone[-10:] if len(norm_phone) >= 10 else norm_phone
        for k in _otp_store.keys():
            if k[-10:] == last10:
                found_key = k
                break

    if not found_key:
        return False, "No OTP requested for this number. Please request a new OTP."

    stored_code, stored_time = _otp_store[found_key]

    # Check expiry
    if time.time() - stored_time > OTP_EXPIRY_SECONDS:
        del _otp_store[found_key]
        return False, "OTP has expired (5 min limit). Please request a new one."

    # Check code
    if code != stored_code:
        return False, "Invalid OTP code. Please check WhatsApp and try again."

    logger.info(f"✅ OTP verified for {norm_phone[-4:]}")
    return True, "OTP verified successfully"


def consume_otp(phone: str) -> None:
    """Remove OTP from store after successful authentication and user session creation."""
    norm_phone = normalize_phone(phone)
    if norm_phone in _otp_store:
        del _otp_store[norm_phone]
    last10 = norm_phone[-10:] if len(norm_phone) >= 10 else norm_phone
    keys_to_del = [k for k in _otp_store.keys() if k[-10:] == last10]
    for k in keys_to_del:
        _otp_store.pop(k, None)
