"""Server-side malware scanning for untrusted uploaded media."""

from __future__ import annotations

import socket
import struct

from app.config import settings
from app.media_processing import MediaValidationError


class MediaSecurityError(MediaValidationError):
    """Raised when an upload is unsafe or cannot be safely scanned."""


def _scan_with_clamav(content: bytes) -> str:
    """Scan bytes with ClamAV's INSTREAM protocol and fail closed."""
    try:
        with socket.create_connection(
            (settings.clamav_host.strip(), settings.clamav_port),
            timeout=settings.clamav_timeout_seconds,
        ) as connection:
            connection.settimeout(settings.clamav_timeout_seconds)
            connection.sendall(b"zINSTREAM\0")
            for offset in range(0, len(content), 64 * 1024):
                chunk = content[offset : offset + 64 * 1024]
                connection.sendall(struct.pack("!I", len(chunk)))
                connection.sendall(chunk)
            connection.sendall(struct.pack("!I", 0))
            response_parts: list[bytes] = []
            while True:
                part = connection.recv(4096)
                if not part:
                    break
                response_parts.append(part)
                if b"\0" in part or b"\n" in part:
                    break
    except (OSError, ValueError) as exc:
        raise MediaSecurityError(
            "Media security scanner is unavailable; upload was not accepted"
        ) from exc

    response = b"".join(response_parts).decode("utf-8", errors="replace").strip("\0\r\n ")
    if response.endswith(" OK"):
        return "clamav"
    if " FOUND" in response:
        raise MediaSecurityError("Unsafe media content was rejected")
    raise MediaSecurityError("Media security scanner returned an invalid result")


def scan_media_for_threats(content: bytes) -> str:
    """Run the configured threat scanner and return the scanner identifier.

    Structural media signature/decoder checks are performed by the upload
    pipeline separately. ``signature`` is the explicit local-development mode;
    ``clamav`` is the production malware-scanning mode.
    """
    mode = settings.media_security_scan_mode.strip().lower()
    if mode == "signature":
        return "signature"
    if mode == "clamav":
        return _scan_with_clamav(content)
    raise MediaSecurityError(
        "Media security scanning is misconfigured; upload was not accepted"
    )
