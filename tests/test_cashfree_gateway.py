import utils.cashfree_gateway as gateway


def test_cashfree_is_disabled_without_explicit_provider_mode(monkeypatch):
    monkeypatch.setenv("SIVDHI_PAYMENT_MODE", "demo")
    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    monkeypatch.setenv("CASHFREE_APP_ID", "test-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "test-secret")
    assert gateway.cashfree_enabled() is False


def test_cashfree_requires_credentials(monkeypatch):
    monkeypatch.setenv("SIVDHI_PAYMENT_MODE", "cashfree")
    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    monkeypatch.delenv("CASHFREE_APP_ID", raising=False)
    monkeypatch.delenv("CASHFREE_SECRET_KEY", raising=False)
    assert gateway.cashfree_enabled() is False


def test_env_example_does_not_route_cashfree_webhooks_to_production():
    from pathlib import Path

    env_example = (
        Path(__file__).resolve().parents[1] / ".env.example"
    ).read_text(encoding="utf-8")
    assert "SIVDHI_CASHFREE_WEBHOOK_URL=" in env_example
    assert "SIVDHI_CASHFREE_WEBHOOK_URL=https://sivdhi.duckdns.org" not in env_example


def test_cashfree_create_order_uses_sandbox_and_server_credentials(monkeypatch):
    monkeypatch.setenv("SIVDHI_PAYMENT_MODE", "cashfree")
    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    monkeypatch.setenv("CASHFREE_APP_ID", "sandbox-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "sandbox-secret")
    captured = {}

    class Response:
        ok = True
        status_code = 200

        def json(self):
            return {
                "order_id": "sivdhi_4_abcd1234",
                "payment_session_id": "session_test_only",
            }

    def fake_post(url, json, headers, timeout):
        captured.update(url=url, payload=json, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setattr(gateway.requests, "post", fake_post)
    result = gateway.create_cashfree_order(
        "sivdhi_4_abcd1234",
        50,
        {"id": 4, "fullname": "Test Customer", "mobile": "9876543210",
         "email": "test@example.com"},
        "https://sivdhi.duckdns.org/customer/cashfree/return",
    )

    assert result["payment_session_id"] == "session_test_only"
    assert captured["url"] == "https://sandbox.cashfree.com/pg/orders"
    assert captured["payload"]["order_amount"] == 50.0
    assert captured["payload"]["order_currency"] == "INR"
    assert captured["payload"]["customer_details"]["customer_phone"] == "9876543210"
    assert captured["headers"]["x-client-id"] == "sandbox-app"
    assert captured["headers"]["x-client-secret"] == "sandbox-secret"
    assert captured["headers"]["x-api-version"] == "2025-01-01"
    assert captured["payload"]["order_meta"]["notify_url"] == "https://sivdhi.duckdns.org/customer/cashfree/webhook"
    import uuid
    assert str(uuid.UUID(captured["headers"]["x-idempotency-key"])) == captured["headers"]["x-idempotency-key"]


def test_cashfree_verification_accepts_only_success_matching_order_amount_and_currency(monkeypatch):
    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    monkeypatch.setenv("CASHFREE_APP_ID", "sandbox-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "sandbox-secret")

    class Response:
        ok = True
        status_code = 200

        def json(self):
            return [
                {"order_id": "other_order", "payment_status": "SUCCESS",
                 "payment_currency": "INR", "payment_amount": 50,
                 "cf_payment_id": "wrong-order"},
                {"order_id": "sivdhi_4_abcd1234", "payment_status": "PENDING",
                 "payment_currency": "INR", "payment_amount": 50,
                 "cf_payment_id": "pending"},
                {"order_id": "sivdhi_4_abcd1234", "payment_status": "SUCCESS",
                 "payment_currency": "INR", "payment_amount": 50.00,
                 "cf_payment_id": "123456789"},
            ]

    monkeypatch.setattr(gateway.requests, "get", lambda *args, **kwargs: Response())
    verified = gateway.verify_cashfree_order_payment("sivdhi_4_abcd1234", 50)
    assert verified == {
        "cf_payment_id": "123456789",
        "payment_group": "online",
        "payment_amount": "50.00",
    }


def test_cashfree_verification_rejects_non_matching_amount(monkeypatch):
    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    monkeypatch.setenv("CASHFREE_APP_ID", "sandbox-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "sandbox-secret")

    class Response:
        ok = True
        status_code = 200

        def json(self):
            return [{
                "order_id": "sivdhi_4_abcd1234",
                "payment_status": "SUCCESS",
                "payment_currency": "INR",
                "payment_amount": "50.01",
                "cf_payment_id": "123456789",
            }]

    monkeypatch.setattr(gateway.requests, "get", lambda *args, **kwargs: Response())
    assert gateway.verify_cashfree_order_payment("sivdhi_4_abcd1234", 50) is None


def test_live_cashfree_booking_requires_verified_database_order():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "routes/customer.py").read_text(encoding="utf-8")
    assert 'payment_action != "confirm_cashfree"' in source
    assert "status='SUCCESS'" in source
    assert "environment=order[\"environment\"]" in source
    assert "cashfree_payment_orders" in source
    assert "cashfree_checkout_fingerprint" in source
    assert "context_hash" in source
    assert "status='BOOKED'" in source



def test_cashfree_create_order_rejects_unexpected_returned_order_id(monkeypatch):
    import pytest

    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    monkeypatch.setenv("CASHFREE_APP_ID", "sandbox-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "sandbox-secret")

    class Response:
        ok = True
        status_code = 200

        def json(self):
            return {
                "order_id": "different_order",
                "payment_session_id": "session_test_only",
            }

    monkeypatch.setattr(gateway.requests, "post", lambda *args, **kwargs: Response())
    with pytest.raises(gateway.CashfreeGatewayError, match="unexpected order reference"):
        gateway.create_cashfree_order(
            "sivdhi_4_abcd1234",
            50,
            {"id": 4, "fullname": "Test Customer", "mobile": "9876543210"},
            "https://sivdhi.duckdns.org/customer/cashfree/return",
        )



def test_cashfree_sandbox_payment_is_classified_as_non_live():
    from utils.sivdhi_finance import payment_audit_metadata

    assert payment_audit_metadata(False, "cashfree", "sandbox") == (
        True, "cashfree_sandbox"
    )
    assert payment_audit_metadata(False, "cashfree", "production") == (
        False, "live"
    )
    assert payment_audit_metadata(True, "demo", "sandbox") == (
        True, "demo"
    )


def test_cashfree_verification_rejects_multiple_successful_payments(monkeypatch):
    import pytest

    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    monkeypatch.setenv("CASHFREE_APP_ID", "sandbox-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "sandbox-secret")

    class Response:
        ok = True
        status_code = 200

        def json(self):
            return [
                {
                    "order_id": "sivdhi_4_abcd1234",
                    "payment_status": "SUCCESS",
                    "payment_currency": "INR",
                    "payment_amount": 50,
                    "cf_payment_id": "payment-1",
                },
                {
                    "order_id": "sivdhi_4_abcd1234",
                    "payment_status": "SUCCESS",
                    "payment_currency": "INR",
                    "payment_amount": 50,
                    "cf_payment_id": "payment-2",
                },
            ]

    monkeypatch.setattr(gateway.requests, "get", lambda *args, **kwargs: Response())

    with pytest.raises(gateway.CashfreeGatewayError, match="multiple successful payments"):
        gateway.verify_cashfree_order_payment("sivdhi_4_abcd1234", 50)


def test_cashfree_verification_flags_second_success_even_with_wrong_amount_or_currency(monkeypatch):
    import pytest

    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    monkeypatch.setenv("CASHFREE_APP_ID", "sandbox-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "sandbox-secret")

    class Response:
        ok = True
        status_code = 200

        def json(self):
            return [
                {
                    "order_id": "sivdhi_4_abcd1234",
                    "payment_status": "SUCCESS",
                    "payment_currency": "INR",
                    "payment_amount": 50,
                    "cf_payment_id": "payment-valid",
                },
                {
                    "order_id": "sivdhi_4_abcd1234",
                    "payment_status": "SUCCESS",
                    "payment_currency": "USD",
                    "payment_amount": 50,
                    "cf_payment_id": "payment-extra",
                },
                {
                    "order_id": "sivdhi_4_abcd1234",
                    "payment_status": "SUCCESS",
                    "payment_currency": "INR",
                    "payment_amount": 49,
                    "cf_payment_id": "payment-extra-wrong-amount",
                },
            ]

    monkeypatch.setattr(gateway.requests, "get", lambda *args, **kwargs: Response())

    with pytest.raises(gateway.CashfreeGatewayError, match="multiple successful payments"):
        gateway.verify_cashfree_order_payment("sivdhi_4_abcd1234", 50)



def test_cashfree_verification_refuses_credentials_from_wrong_environment(monkeypatch):
    import pytest

    monkeypatch.setenv("CASHFREE_ENV", "production")
    monkeypatch.setenv("CASHFREE_APP_ID", "production-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "production-secret")
    calls = []
    monkeypatch.setattr(
        gateway.requests, "get",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(gateway.CashfreeGatewayError, match="does not match"):
        gateway.verify_cashfree_order_payment(
            "sivdhi_4_sandbox_order", 50, environment="sandbox"
        )
    assert calls == []

def test_cashfree_create_order_accepts_a_distinct_balance_order_note(monkeypatch):
    monkeypatch.setenv("SIVDHI_PAYMENT_MODE", "cashfree")
    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    monkeypatch.setenv("CASHFREE_APP_ID", "sandbox-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "sandbox-secret")
    captured = {}

    class Response:
        ok = True
        status_code = 200

        def json(self):
            return {
                "order_id": "sivdhi_4_balance1234",
                "payment_session_id": "balance-session-test",
            }

    def fake_post(url, json, headers, timeout):
        captured.update(payload=json)
        return Response()

    monkeypatch.setattr(gateway.requests, "post", fake_post)
    gateway.create_cashfree_order(
        "sivdhi_4_balance1234",
        50,
        {"id": 4, "fullname": "Test Customer", "mobile": "9876543210"},
        "https://sivdhi.duckdns.org/customer/cashfree/return",
        order_note="Sivdhi remaining balance",
    )

    assert captured["payload"]["order_note"] == "Sivdhi remaining balance"



def test_cashfree_live_payments_require_production_and_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("SIVDHI_PAYMENT_MODE", "cashfree")
    monkeypatch.setenv("CASHFREE_ENV", "production")
    monkeypatch.setenv("CASHFREE_APP_ID", "production-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "production-secret")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("CASHFREE_LIVE_PAYMENTS_ENABLED", "1")
    assert gateway.cashfree_enabled() is False

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CASHFREE_LIVE_PAYMENTS_ENABLED", "0")
    assert gateway.cashfree_enabled() is False

    monkeypatch.setenv("CASHFREE_LIVE_PAYMENTS_ENABLED", "1")
    assert gateway.cashfree_enabled() is True


def test_cashfree_sandbox_on_production_requires_explicit_test_override(monkeypatch):
    monkeypatch.setenv("SIVDHI_PAYMENT_MODE", "cashfree")
    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    monkeypatch.setenv("CASHFREE_APP_ID", "sandbox-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "sandbox-secret")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CASHFREE_ALLOW_SANDBOX_IN_PRODUCTION", "0")
    assert gateway.cashfree_enabled() is False

    monkeypatch.setenv("CASHFREE_ALLOW_SANDBOX_IN_PRODUCTION", "1")
    assert gateway.cashfree_enabled() is True


def test_cashfree_webhook_signature_uses_exact_raw_body_and_timestamp(monkeypatch):
    import base64
    import hashlib
    import hmac

    secret = "webhook-test-secret"
    timestamp = "1730000000000"
    raw_body = b'{"type":"PAYMENT_SUCCESS_WEBHOOK","data":{}}'
    signature = base64.b64encode(
        hmac.new(
            secret.encode(),
            timestamp.encode() + raw_body,
            hashlib.sha256,
        ).digest()
    ).decode()

    assert gateway.verify_cashfree_webhook_signature(
        signature, timestamp, raw_body, secret
    )
    assert not gateway.verify_cashfree_webhook_signature(
        signature, timestamp, raw_body + b" ", secret
    )
    assert not gateway.verify_cashfree_webhook_signature(
        signature, timestamp, raw_body, "wrong-secret"
    )


def test_cashfree_minimum_payment_amount_is_one_rupee(monkeypatch):
    import pytest

    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    monkeypatch.setenv("CASHFREE_APP_ID", "sandbox-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "sandbox-secret")
    with pytest.raises(gateway.CashfreeGatewayError, match="minimum amount"):
        gateway.create_cashfree_order(
            "sivdhi_4_tinyamount",
            0.50,
            {"id": 4, "fullname": "Test Customer", "mobile": "9876543210"},
            "https://sivdhi.duckdns.org/customer/cashfree/return",
        )


def test_cashfree_production_rejects_http_return_url(monkeypatch):
    import pytest

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CASHFREE_ENV", "production")
    monkeypatch.setenv("CASHFREE_LIVE_PAYMENTS_ENABLED", "1")
    monkeypatch.setenv("CASHFREE_APP_ID", "production-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "production-secret")

    with pytest.raises(gateway.CashfreeGatewayError, match="return URL must use HTTPS"):
        gateway.create_cashfree_order(
            "sivdhi_4_http_return",
            50,
            {"id": 4, "fullname": "Test Customer", "mobile": "9876543210"},
            "http://sivdhi.duckdns.org/customer/cashfree/return",
        )


def test_cashfree_order_connection_failure_is_retryable(monkeypatch):
    import pytest
    import requests

    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    monkeypatch.setenv("CASHFREE_APP_ID", "sandbox-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "sandbox-secret")

    def timeout(*args, **kwargs):
        raise requests.Timeout("simulated timeout")

    monkeypatch.setattr(gateway.requests, "post", timeout)
    with pytest.raises(gateway.CashfreeGatewayError) as error:
        gateway.create_cashfree_order(
            "sivdhi_4_retryorder",
            50,
            {"id": 4, "fullname": "Test Customer", "mobile": "9876543210"},
            "https://sivdhi.duckdns.org/customer/cashfree/return",
        )
    assert error.value.retryable is True
