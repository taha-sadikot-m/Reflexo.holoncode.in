"""WhatsApp Calling SDP rules (harvested H5 from the old `voice/bridge.py`; pure, unit-tested without aiortc).

WhatsApp accepts exactly one SHA-256 DTLS fingerprint, Opus at 48 kHz, one audio section, ICE credentials and the
right DTLS role. aiortc advertises SHA-256/384/512 fingerprints, and Windows can expose link-local ICE candidates
that Meta cannot reach; both are removed here.
"""

from __future__ import annotations

import ipaddress
import re

ALLOWED_MIME_TYPES = ("audio/opus", "audio/telephone-event")


class VoiceSdpError(Exception):
    """An SDP that WhatsApp would reject (or that we could not build)."""


def normalize_whatsapp_sdp(sdp: str, kind: str = "answer") -> str:
    output: list[str] = []
    for raw_line in str(sdp or "").splitlines():
        line = raw_line.strip("\r")
        lowered = line.lower()
        if lowered.startswith("a=fingerprint:"):
            match = re.match(r"^a=fingerprint:sha-256\s+(.+)$", line, flags=re.IGNORECASE)
            if not match:
                continue  # drop sha-384 / sha-512
            line = f"a=fingerprint:sha-256 {match.group(1)}"
        elif lowered.startswith("a=candidate:"):
            parts = line.split()
            if len(parts) >= 6:
                try:
                    address = ipaddress.ip_address(parts[4])
                except ValueError:
                    address = None  # mDNS / hostname candidates are kept
                if address is not None and address.is_link_local:
                    continue
        output.append(line)
    normalized = "\r\n".join(output) + "\r\n"
    validate_whatsapp_sdp(normalized, kind)
    return normalized


def sdp_checks(sdp: str, kind: str = "answer") -> dict[str, bool]:
    lines = sdp.splitlines()
    fingerprints = [line for line in lines if line.lower().startswith("a=fingerprint:")]
    checks = {
        "one SHA-256 fingerprint": len(fingerprints) == 1
        and fingerprints[0].lower().startswith("a=fingerprint:sha-256 "),
        "one audio media section": sum(line.startswith("m=audio ") for line in lines) == 1,
        "Opus at 48 kHz": any(
            re.match(r"^a=rtpmap:\d+\s+opus/48000(?:/[12])?$", line, re.IGNORECASE) for line in lines
        ),
        "ICE username": any(line.startswith("a=ice-ufrag:") for line in lines),
        "ICE password": any(line.startswith("a=ice-pwd:") for line in lines),
    }
    if kind == "offer":  # as the offerer we let WhatsApp pick the DTLS role
        checks["negotiable DTLS role"] = any(line.lower() in {"a=setup:actpass", "a=setup:active"} for line in lines)
    else:
        checks["active DTLS role"] = any(line.lower() == "a=setup:active" for line in lines)
    return checks


def validate_whatsapp_sdp(sdp: str, kind: str = "answer") -> None:
    failed = [name for name, passed in sdp_checks(sdp, kind).items() if not passed]
    if failed:
        raise VoiceSdpError(f"invalid WhatsApp SDP {kind}: " + ", ".join(failed))


def candidate_count(sdp: str) -> int:
    return sum(line.startswith("a=candidate:") for line in sdp.splitlines())
