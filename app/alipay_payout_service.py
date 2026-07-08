"""Alipay transfer wrapper for seller disbursements."""

from __future__ import annotations

import base64
import json
from datetime import datetime

import httpx

from app.config import settings


class AlipayPayoutError(RuntimeError):
    """Raised when an Alipay payout call fails."""


def _base_url() -> str:
    if settings.payments_simulated:
        return "https://openapi-sandbox.dl.alipaydev.com/gateway.do"
    return "https://openapi.alipay.com/gateway.do"


def _normalized_private_key() -> str:
    key = settings.alipay_private_key.strip()
    if not key:
        raise AlipayPayoutError("Alipay private key is not configured")
    if "BEGIN" in key:
        return key
    wrapped = "\n".join(key[i : i + 64] for i in range(0, len(key), 64))
    return f"-----BEGIN PRIVATE KEY-----\n{wrapped}\n-----END PRIVATE KEY-----"


def _sign(content: str) -> str:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except Exception as exc:  # pragma: no cover
        raise AlipayPayoutError("cryptography package is required for Alipay payouts") from exc

    private_key = serialization.load_pem_private_key(_normalized_private_key().encode("utf-8"), password=None)
    signature = private_key.sign(
        content.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def _signed_params(api_method: str, biz_content: dict) -> dict[str, str]:
    app_id = settings.alipay_app_id.strip()
    if not app_id:
        raise AlipayPayoutError("Alipay app id is not configured")

    params = {
        "app_id": app_id,
        "method": api_method,
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
    }
    sign_content = "&".join(f"{key}={params[key]}" for key in sorted(params))
    params["sign"] = _sign(sign_content)
    return params


def _gateway_call(api_method: str, biz_content: dict) -> dict:
    params = _signed_params(api_method, biz_content)
    with httpx.Client(timeout=30.0) as client:
        response = client.post(_base_url(), data=params)
    if response.status_code >= 400:
        raise AlipayPayoutError(f"Alipay gateway failed: {response.text[:300]}")
    payload = response.json()
    response_key = f"{api_method.replace('.', '_')}_response"
    body = payload.get(response_key) or {}
    code = body.get("code")
    if code != "10000":
        message = body.get("sub_msg") or body.get("msg") or "Alipay payout failed"
        raise AlipayPayoutError(message)
    return body


def create_transfer(
    *,
    out_biz_no: str,
    payee_account: str,
    amount_minor: int,
    currency: str,
    remark: str,
) -> dict:
    if currency.lower() != "cny":
        raise AlipayPayoutError("Alipay payouts currently require CNY settlement because no currency conversion is available")
    amount_value = f"{amount_minor / 100:.2f}"
    biz_content = {
        "out_biz_no": out_biz_no,
        "trans_amount": amount_value,
        "product_code": "TRANS_ACCOUNT_NO_PWD",
        "biz_scene": "DIRECT_TRANSFER",
        "order_title": remark[:128],
        "remark": remark[:256],
        "payee_info": {
            "identity": payee_account,
            "identity_type": "ALIPAY_LOGON_ID",
        },
    }
    return _gateway_call("alipay.fund.trans.uni.transfer", biz_content)


def query_transfer(out_biz_no: str) -> dict:
    return _gateway_call("alipay.fund.trans.common.query", {"out_biz_no": out_biz_no})
