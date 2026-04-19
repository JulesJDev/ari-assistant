"""
Comprehensive security tests for Ari Assistant.
Tests XSS sanitization, 2FA, rate limiting, file validation, and more.
"""

import pytest
import sys
import os
import zipfile
import tempfile
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.security import (
    sanitize_html,
    generate_2fa_code,
    verify_2fa_code,
    store_2fa_code,
    get_2fa_code,
    is_2fa_expired,
    rate_limit_check,
    rate_limit_ws_connections,
    hash_pin,
    verify_pin,
    validate_upload_file,
    anti_zip_slip,
    get_security_headers,
    clear_rate_limit,
    RATE_LIMIT,
    UPLOAD_MAX_SIZE,
    ALLOWED_EXT,
)

import core.security as sec_mod

# Reset module-level state between tests for isolation
@pytest.fixture(autouse=True)
def _reset_security_state():
    sec_mod._rate_limit_store.clear()
    sec_mod._2fa_store.clear()
    sec_mod._ws_connections.clear()




# ============ XSS Sanitization Tests ============

class TestXSSSanitization:
    """Tests for sanitize_html function."""

    def test_sanitize_basic_script_tag(self):
        """Script tags should be escaped."""
        html = "<script>alert('xss')</script>"
        assert sanitize_html(html) == "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;"

    def test_sanitize_img_onerror(self):
        """img tags with onerror should be escaped."""
        html = '<img src=x onerror=alert(1)>'
        result = sanitize_html(html)
        assert "onerror" not in result or "&lt;" in result

    def test_sanitize_ampersand(self):
        """Ampersand should be escaped."""
        assert sanitize_html("A & B") == "A &amp; B"

    def test_sanitize_double_quote(self):
        """Double quotes should be escaped."""
        assert sanitize_html('"test"') == "&quot;test&quot;"

    def test_sanitize_single_quote(self):
        """Single quotes should be escaped."""
        assert sanitize_html("O'Reilly") == "O&#x27;Reilly"

    def test_sanitize_mixed_html(self):
        """Mixed HTML entities should all be escaped."""
        html = '<div onclick="evil()">Test & "quoted"</div>'
        result = sanitize_html(html)
        assert "&lt;" in result and "&gt;" in result
        assert "&quot;" in result

    def test_sanitize_empty_string(self):
        """Empty string should return empty."""
        assert sanitize_html("") == ""

    def test_sanitize_none(self):
        """None input should return empty string."""
        assert sanitize_html(None) == ""


# ============ 2FA Tests ============

class Test2FA:
    """Tests for two-factor authentication functions."""

    def test_generate_2fa_code_length(self):
        """Generated code should be 4 digits."""
        code = generate_2fa_code()
        assert len(code) == 4
        assert code.isdigit()



    def test_generate_2fa_code_range(self):
        """Codes should be between 0000 and 9999."""
        for _ in range(100):
            code = generate_2fa_code()
            assert 0 <= int(code) <= 9999

    def test_verify_correct_code(self):
        """Correct code should return success."""
        store_2fa_code("user1", "1234")
        user_code = get_2fa_code("user1")
        success, msg = verify_2fa_code(user_code, "1234", 0)
        assert success is True

    def test_verify_incorrect_code(self):
        """Incorrect code should return failure."""
        store_2fa_code("user1", "1234")
        user_code = get_2fa_code("user1")  # Consume and return "1234"
        # But we'll send wrong code
        success, msg = verify_2fa_code("0000", "1234", 0)
        assert success is False
        assert "Invalid" in msg

    def test_verify_with_attempts(self):
        """Attempt counter should decrement properly."""
        store_2fa_code("user1", "1234")
        code = get_2fa_code("user1")

        for i in range(4):
            success, msg = verify_2fa_code("wrong", "1234", i)
            assert success is False
            remaining = 5 - (i + 1)
            assert f"{remaining} attempts" in msg

    def test_verify_too_many_attempts(self):
        """Exceeding max attempts should fail."""
        store_2fa_code("user1", "1234")
        # 5 attempts total
        for i in range(5):
            verify_2fa_code("wrong", "1234", i)

        # 6th attempt - but we can't verify against a consumed code
        # Simulate by calling directly
        success, msg = verify_2fa_code("wrong", "1234", 5)
        assert success is False
        assert "Too many attempts" in msg

    def test_2fa_expiry(self):
        """Expired codes should be detected."""
        from datetime import datetime, timedelta
        import time

        user_id = "expiry_test"
        store_2fa_code(user_id, "1234", expiry_seconds=1)
        time.sleep(2)  # Wait for expiry

        expired = is_2fa_expired(user_id)
        assert expired is True


# ============ Rate Limiting Tests ============

class TestRateLimiting:
    """Tests for rate limiting functions."""

    def test_rate_limit_ws_allowed(self):
        """Within limit should be allowed."""
        ip = "127.0.0.1"
        for _ in range(3):  # Within 5 connection limit
            assert rate_limit_check(ip, "/ws") is True

    def test_rate_limit_ws_blocked(self):
        """Exceeding limit should be blocked."""
        ip = "127.0.0.2"
        for _ in range(5):  # At limit
            assert rate_limit_check(ip, "/ws") is True
        # 6th should be blocked
        assert rate_limit_check(ip, "/ws") is False

    def test_rate_limit_api_allowed(self):
        """API endpoint within limit should be allowed."""
        ip = "127.0.0.3"
        for _ in range(15):  # Within 20 request limit
            assert rate_limit_check(ip, "/api") is True

    def test_rate_limit_api_blocked(self):
        """Exceeding API limit should be blocked."""
        ip = "127.0.0.4"
        for _ in range(20):
            assert rate_limit_check(ip, "/api") is True
        assert rate_limit_check(ip, "/api") is False

    def test_rate_limit_different_ips(self):
        """Limits should be per-IP."""
        for i in range(5):
            ip = f"127.0.0.{i}"
            assert rate_limit_check(ip, "/ws") is True

    def test_rate_limit_different_endpoints(self):
        """Limits should be per-endpoint."""
        ip = "127.0.0.10"
        assert rate_limit_check(ip, "/ws") is True
        assert rate_limit_check(ip, "/api") is True
        # Each should have its own counter

    def test_rate_limit_window_reset(self):
        """After window expires, count should reset."""
        ip = "127.0.0.20"
        # Exhaust limit
        for _ in range(5):
            rate_limit_check(ip, "/ws")

        # Confirm blocked
        assert rate_limit_check(ip, "/ws") is False

        # Manually clear
        clear_rate_limit(ip, "/ws")

        # Should be allowed again
        assert rate_limit_check(ip, "/ws") is True

    def test_rate_limit_unknown_endpoint(self):
        """Endpoints without limits should always be allowed."""
        assert rate_limit_check("127.0.0.1", "/unknown") is True


# ============ PIN Hashing Tests ============

class TestPINHashing:
    """Tests for PIN hash storage and verification."""

    def test_hash_pin_returns_sha256(self):
        """Hash should be SHA256 hex string."""
        pin_hash = hash_pin("1234")
        assert len(pin_hash) == 64
        assert all(c in "0123456789abcdef" for c in pin_hash)

    def test_hash_pin_deterministic(self):
        """Same PIN should produce same hash."""
        h1 = hash_pin("1234")
        h2 = hash_pin("1234")
        assert h1 == h2

    def test_hash_pin_different_pins(self):
        """Different PINs should produce different hashes."""
        h1 = hash_pin("1234")
        h2 = hash_pin("5678")
        assert h1 != h2

    def test_verify_pin_correct(self):
        """Correct PIN should verify."""
        pin_hash = hash_pin("1234")
        assert verify_pin("1234", pin_hash) is True

    def test_verify_pin_incorrect(self):
        """Incorrect PIN should not verify."""
        pin_hash = hash_pin("1234")
        assert verify_pin("wrong", pin_hash) is False

    def test_verify_pin_wrong_length(self):
        """Wrong length PINs should not verify."""
        pin_hash = hash_pin("1234")
        assert verify_pin("123", pin_hash) is False
        assert verify_pin("12345", pin_hash) is False


# ============ File Upload Security Tests ============

class TestFileUploadSecurity:
    """Tests for file upload validation and anti-zip-slip."""

    def test_validate_allowed_extension(self):
        """Allowed extensions should pass."""
        for ext in ALLOWED_EXT:
            allowed, reason = validate_upload_file(f"test.{ext}", 1024)
            assert allowed is True, f"Extension .{ext} should be allowed"

    def test_validate_rejected_extension(self):
        """Disallowed extensions should be rejected."""
        for ext in ["exe", "bat", "sh", "php", "js", "py"]:
            allowed, reason = validate_upload_file(f"test.{ext}", 1024)
            assert allowed is False

    def test_validate_file_size_ok(self):
        """Files within size limit should pass."""
        allowed, reason = validate_upload_file("test.zip", 1024 * 1024)  # 1MB
        assert allowed is True

    def test_validate_file_size_exceeded(self):
        """Files exceeding size limit should be rejected."""
        big_size = UPLOAD_MAX_SIZE + 1
        allowed, reason = validate_upload_file("test.zip", big_size)
        assert allowed is False
        assert "too large" in reason.lower()

    def test_validate_no_extension(self):
        """Files without extension should be rejected."""
        allowed, reason = validate_upload_file("noextension", 1024)
        assert allowed is False

    def test_validate_uppercase_extension(self):
        """Uppercase extensions should be normalized."""
        allowed, reason = validate_upload_file("test.ZIP", 1024)
        assert allowed is True

    def test_validate_empty_filename(self):
        """Empty filename should be rejected."""
        allowed, reason = validate_upload_file("", 1024)
        assert allowed is False


# ============ Zip Slip Protection Tests ============

class TestZipSlipProtection:
    """Tests for anti-zip-slip protection."""

    def test_safe_zip_approved(self):
        """A zip with safe relative paths should be approved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "safe.zip")
            extract_to = os.path.join(tmpdir, "extract")

            os.makedirs(extract_to, exist_ok=True)

            with zipfile.ZipFile(zip_path, 'w') as zf:
                zf.writestr("file1.txt", "content1")
                zf.writestr("folder/file2.txt", "content2")

            assert anti_zip_slip(zip_path, extract_to) is True

    def test_zip_slip_absolute_path(self):
        """Zip containing absolute paths should be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "evil.zip")
            with zipfile.ZipFile(zip_path, 'w') as zf:
                zf.writestr("/etc/passwd", "evil")

            assert anti_zip_slip(zip_path, tmpdir) is False

    def test_zip_slip_parent_traversal(self):
        """Zip with .. traversal should be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "evil.zip")
            with zipfile.ZipFile(zip_path, 'w') as zf:
                zf.writestr("../../../etc/passwd", "evil")

            assert anti_zip_slip(zip_path, tmpdir) is False

    def test_zip_slip_mixed_paths(self):
        """Zip with mixed safe and unsafe paths should be rejected if any unsafe."""
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "mixed.zip")
            with zipfile.ZipFile(zip_path, 'w') as zf:
                zf.writestr("safe.txt", "ok")
                zf.writestr("subdir/../../../etc/passwd", "evil")

            assert anti_zip_slip(zip_path, tmpdir) is False


# ============ Security Headers Tests ============

class TestSecurityHeaders:
    """Tests for HTTP security headers."""

    def test_headers_return_dict(self):
        """Should return a dictionary."""
        headers = get_security_headers()
        assert isinstance(headers, dict)

    def test_headers_include_xss_protection(self):
        """Should include XSS protection header."""
        headers = get_security_headers()
        assert "X-XSS-Protection" in headers

    def test_headers_include_frame_options(self):
        """Should include clickjacking protection."""
        headers = get_security_headers()
        assert "X-Frame-Options" in headers

    def test_headers_include_csp(self):
        """Should include Content Security Policy."""
        headers = get_security_headers()
        assert "Content-Security-Policy" in headers

    def test_headers_include_hsts(self):
        """Should include HSTS."""
        headers = get_security_headers()
        assert "Strict-Transport-Security" in headers

    def test_headers_all_required(self):
        """All required headers should be present."""
        required = [
            "X-Content-Type-Options",
            "X-Frame-Options",
            "X-XSS-Protection",
            "Strict-Transport-Security",
            "Content-Security-Policy",
            "Referrer-Policy",
        ]
        headers = get_security_headers()
        for h in required:
            assert h in headers


# ============ Integration Tests ============

class TestIntegration:
    """Integration-like tests for combined scenarios."""

    def test_upload_then_verify_2fa(self):
        """Simulate: upload validation then 2FA verification."""
        allowed, _ = validate_upload_file("model.vrm", 5 * 1024 * 1024)
        assert allowed is True

        code = generate_2fa_code()
        store_2fa_code("user123", code)
        retrieved = get_2fa_code("user123")
        assert retrieved == code

    def test_rate_limit_and_pin_auth(self):
        """Simulate: rate limit check then PIN verification."""
        assert rate_limit_check("127.0.0.1", "/api") is True
        pin_hash = hash_pin("1234")
        assert verify_pin("1234", pin_hash) is True

    def test_xss_sanitize_then_header_check(self):
        """Sanitize user input then check security headers."""
        malicious = "<script>alert('xss')</script>"
        safe = sanitize_html(malicious)
        assert "<" not in safe

        headers = get_security_headers()
        assert "X-XSS-Protection" in headers


# ============ Constants Validation ============

def test_rate_limit_constants():
    """Ensure RATE_LIMIT config has proper structure."""
    assert "/ws" in RATE_LIMIT
    assert "/api" in RATE_LIMIT
    assert "max_connections" in RATE_LIMIT["/ws"]
    assert "max_requests" in RATE_LIMIT["/api"]
    assert "window_seconds" in RATE_LIMIT["/ws"]
    assert "window_seconds" in RATE_LIMIT["/api"]

def test_upload_constants():
    """Ensure upload limits are properly defined."""
    assert UPLOAD_MAX_SIZE > 0
    assert isinstance(ALLOWED_EXT, set)
    assert len(ALLOWED_EXT) > 0
    assert "vrm" in ALLOWED_EXT
    assert "zip" in ALLOWED_EXT
