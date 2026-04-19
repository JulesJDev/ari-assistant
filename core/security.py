"""
Security module for Ari Assistant.
Provides XSS sanitization, rate limiting, 2FA, upload validation, and more.
"""

import re
import time
import secrets
import hashlib
from typing import Tuple, Optional, Dict
from collections import defaultdict
from datetime import datetime, timedelta


# ============ Constants ============

RATE_LIMIT = {
    "/ws": {"max_connections": 5, "window_seconds": 60},
    "/api": {"max_requests": 20, "window_seconds": 60},
}

UPLOAD_MAX_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_EXT = {"vrm", "zip", "png", "jpg", "jpeg", "glb", "gltf"}

# In-memory rate limit store (use Redis in production)
_rate_limit_store: Dict[str, list] = defaultdict(list)

# 2FA store (use Redis/DB in production)
_2fa_store: Dict[str, Dict] = {}

# WebSocket connection tracking
_ws_connections: Dict[str, int] = defaultdict(int)


# ============ HTML/XSS Sanitization ============

def sanitize_html(text: str) -> str:
    """
    Escape HTML special characters to prevent XSS.
    """
    if not text:
        return ""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    text = text.replace("'", "&#x27;")
    return text


# ============ 2FA (Two-Factor Authentication) ============

def generate_2fa_code() -> str:
    """
    Generate a 4-digit 2FA code using secrets.randbelow.
    """
    return str(secrets.randbelow(10000)).zfill(4)


def verify_2fa_code(user_code: str, valid_code: str, attempts: int, max_attempts: int = 5) -> Tuple[bool, str]:
    """
    Verify a 2FA code with attempt tracking.
    Returns: (success: bool, message: str)
    """
    if attempts >= max_attempts:
        return False, "Too many attempts. Please request a new code."

    # Clean input
    user_code = re.sub(r'\D', '', user_code)

    if user_code == valid_code:
        return True, "Verification successful"
    else:
        return False, f"Invalid code. {max_attempts - attempts - 1} attempts remaining."


def store_2fa_code(user_id: str, code: str, expiry_seconds: int = 300) -> None:
    """Store a 2FA code with expiry."""
    _2fa_store[user_id] = {
        "code": code,
        "expires_at": datetime.utcnow() + timedelta(seconds=expiry_seconds),
    }


def get_2fa_code(user_id: str) -> Optional[str]:
    """Retrieve and clear 2FA code if still valid."""
    record = _2fa_store.get(user_id)
    if not record:
        return None

    if datetime.utcnow() > record["expires_at"]:
        del _2fa_store[user_id]
        return None

    code = record["code"]
    del _2fa_store[user_id]  # One-time use
    return code


def is_2fa_expired(user_id: str) -> bool:
    """Check if a 2FA code has expired."""
    record = _2fa_store.get(user_id)
    if not record:
        return True
    return datetime.utcnow() > record["expires_at"]


# ============ Rate Limiting ============

def rate_limit_check(ip: str, endpoint: str) -> bool:
    """
    Check if an IP has exceeded rate limits for an endpoint.
    Returns True if allowed, False if rate limited.
    """
    if endpoint not in RATE_LIMIT:
        return True  # No limit defined

    config = RATE_LIMIT[endpoint]
    max_allowed = config["max_connections"] if "max_connections" in config else config["max_requests"]
    window = config["window_seconds"]

    key = f"{ip}:{endpoint}"
    now = time.time()

    # Clean old entries
    _rate_limit_store[key] = [
        ts for ts in _rate_limit_store[key] if now - ts < window
    ]

    # Check count
    if len(_rate_limit_store[key]) >= max_allowed:
        return False

    # Record this request
    _rate_limit_store[key].append(now)
    return True


def rate_limit_ws_connections(ip: str) -> bool:
    """
    Specifically check WebSocket connection rate limits.
    Returns True if allowed, False if limit reached.
    """
    return rate_limit_check(ip, "/ws")


def clear_rate_limit(ip: str, endpoint: str) -> None:
    """Clear rate limit entries for an IP/endpoint (admin use)."""
    key = f"{ip}:{endpoint}"
    if key in _rate_limit_store:
        del _rate_limit_store[key]


# ============ PIN Hashing (for local auth) ============

def hash_pin(pin: str) -> str:
    """
    Hash a PIN using SHA256 with salt.
    Returns hex string.
    """
    # Use a fixed salt for demo; in production use unique salt per user
    salt = "ari_assistant_salt_2026"
    return hashlib.sha256(f"{pin}{salt}".encode()).hexdigest()


def verify_pin(pin: str, hash_value: str) -> bool:
    """Verify a PIN against its hash."""
    return hash_pin(pin) == hash_value


# ============ File Upload Security ============

def validate_upload_file(filename: str, file_size: int, max_size: int = None, allowed_ext: set = None) -> Tuple[bool, str]:
    """
    Validate file upload for security.
    Returns (allowed: bool, reason: str).
    """
    if max_size is None:
        max_size = UPLOAD_MAX_SIZE
    if allowed_ext is None:
        allowed_ext = ALLOWED_EXT

    if file_size > max_size:
        return False, f"File too large (max {max_size // 1024 // 1024}MB)"

    # Extract extension
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in allowed_ext:
        return False, f"File type not allowed. Allowed: {', '.join(sorted(allowed_ext))}"

    return True, "OK"


def anti_zip_slip(zip_path: str, extract_to: str) -> bool:
    """
    Validate that extracted files from a zip don't escape the target directory.
    This is a simplified check - in production use zipfile with proper validation.
    Returns True if safe, False if zip_slip detected.
    """
    import os
    import zipfile

    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.namelist():
                # Prevent absolute paths and parent directory traversal
                member_path = os.path.normpath(member)
                if member_path.startswith("/") or ".." in member_path:
                    return False

                # Resolve full extraction path
                dest_path = os.path.join(extract_to, member_path)
                if not os.path.realpath(dest_path).startswith(os.path.realpath(extract_to)):
                    return False
        return True
    except Exception:
        return False


# ============ Secure Headers Generation ============

def get_security_headers() -> Dict[str, str]:
    """
    Return recommended security headers for HTTP responses.
    """
    return {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-XSS-Protection": "1; mode=block",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }
