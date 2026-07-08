"""WeChat Pay transfer wrapper for seller disbursements."""

from __future__ import annotations

import base64
import json
import secrets
import time
from urllib.parse import urlencode

import httpx

from app.config import settings


class WeChatPayoutError(RuntimeError):
    """Raised when a WeChat payout call fails."""


def _base_url() -> str:
    return "https://api.mch.weixin.qq.com"


def _normalized_private_key() -> str:
    key = settings.wechat_pay_private_key.strip()
    if not key:
        raise WeChatPayoutError("WeChat Pay private key is not configured")
    if "BEGIN" in key:
        return key
    wrapped = "\n".join(key[i : i + 64] for i in range(0, len(key), 64))
    return f"-----BEGIN PRIVATE KEY-----\n{wrapped}\n-----END PRIVATE KEY-----"


def _sign(message: str) -> str:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except Exception as exc:  # pragma: no cover
        raise WeChatPayoutError("cryptography package is required for WeChat payouts") from exc

    private_key = serialization.load_pem_private_key(_normalized_private_key().encode("utf-8"), password=None)
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def _authorization(method: str, canonical_url: str, body_text: str) -> str:
    mch_id = settings.wechat_pay_mch_id.strip()
    serial_no = settings.wechat_pay_serial_no.strip()
    if not mch_id or not serial_no:
        raise WeChatPayoutError("WeChat Pay merchant credentials are not configured")
    nonce = secrets.token_hex(16)
    timestamp = str(int(time.time()))
    message = f"{method}\n{canonical_url}\n{timestamp}\n{nonce}\n{body_text}\n"
    signature = _sign(message)
    return (
        'WECHATPAY2-SHA256-RSA2048 '
        f'mchid="{mch_id}",nonce_str="{nonce}",timestamp="{timestamp}",serial_no="{serial_no}",signature="{signature}"'
    )


def _request(method: str, path: str, *, json_body: dict | None = None, params: dict | None = None) -> dict:
    method = method.upper()
    query = f"?{urlencode(params)}" if params else ""
    canonical_url = f"{path}{query}"
    body_text = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False) if json_body else ""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": _authorization(method, canonical_url, body_text),
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.request(
            method,
            f"{_base_url()}{canonical_url}",
            content=body_text if body_text else None,
            headers=headers,
        )
    if response.status_code >= 400:
        raise WeChatPayoutError(f"WeChat payout request failed: {response.text[:300]}")
    if not response.text.strip():
        return {}
    return response.json()


def create_transfer(
    *,
    out_batch_no: str,
    openid: str,
    amount_minor: int,
    currency: str,
    remark: str,
) -> dict:
    if currency.lower() != "cny":
        raise WeChatPayoutError("WeChat payouts currently require CNY settlement because no currency conversion is available")
    body = {
        "appid": settings.wechat_pay_app_id.strip(),
        "out_batch_no": out_batch_no,
        "batch_name": "HeyMarket payout",
        "batch_remark": remark[:32],
        "total_amount": amount_minor,
        "total_num": 1,
        "transfer_detail_list": [
            {
                "out_detail_no": f"{out_batch_no}-1",
                "transfer_amount": amount_minor,
                "transfer_remark": remark[:32],
                "openid": openid,
            }
        ],
    }
    return _request("POST", "/v3/transfer/batches", json_body=body)


def query_transfer(out_batch_no: str) -> dict:
    return _request("GET", f"/v3/transfer/batches/out-batch-no/{out_batch_no}")
