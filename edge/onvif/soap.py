from __future__ import annotations

"""
Minimal ONVIF SOAP transport — hand-rolled on requests + stdlib, no zeep/wsdl
dependency (keeps the Jetson runtime lean and AGPL-free).

ONVIF is SOAP over HTTP: every call is an XML envelope POSTed to a service
URL. Two pieces live here:

  • WS-Security UsernameToken auth — cameras authenticate each call with a
    PasswordDigest = Base64(SHA1(nonce + created + password)), never the raw
    password on the wire.
  • Namespace-agnostic response parsing — vendors disagree on prefixes, so we
    strip namespaces after parsing and match on local tag names only. That is
    what makes this client survive cheap WiFi cameras.
"""

import base64
import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from xml.etree import ElementTree

import requests


logger = logging.getLogger("onvif")

_TIMEOUT = 6.0


class OnvifError(Exception):
    """Camera unreachable, auth rejected, or a SOAP fault."""


def _security_header(username: str, password: str) -> str:
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    ).decode()
    return f"""
  <wsse:Security s:mustUnderstand="1"
      xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
      xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
    <wsse:UsernameToken>
      <wsse:Username>{username}</wsse:Username>
      <wsse:Password
        Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>
      <wsse:Nonce
        EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</wsse:Nonce>
      <wsu:Created>{created}</wsu:Created>
    </wsse:UsernameToken>
  </wsse:Security>"""


def strip_ns(elem: ElementTree.Element) -> ElementTree.Element:
    """Rename every tag to its local name so callers match vendor-agnostically."""
    for e in elem.iter():
        if "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
    return elem


def call(url: str, body: str, username: str = "", password: str = "",
         timeout: float = _TIMEOUT) -> ElementTree.Element:
    """POST one SOAP request; return the namespace-stripped <Body> element."""
    header = _security_header(username, password) if username else ""
    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
  <s:Header>{header}</s:Header>
  <s:Body>{body}</s:Body>
</s:Envelope>"""
    try:
        resp = requests.post(
            url, data=envelope.encode(),
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            timeout=timeout)
    except requests.RequestException as exc:
        raise OnvifError(f"cannot reach {url}: {exc}") from exc
    if resp.status_code == 401:
        raise OnvifError("authentication rejected (check username/password)")
    try:
        root = strip_ns(ElementTree.fromstring(resp.content))
    except ElementTree.ParseError as exc:
        raise OnvifError(f"non-XML response (HTTP {resp.status_code})") from exc
    fault = root.find(".//Fault")
    if fault is not None:
        reason = "".join(t.strip() for t in fault.itertext())[:200]
        raise OnvifError(f"SOAP fault: {reason}")
    body_el = root.find(".//Body")
    if body_el is None:
        raise OnvifError("no SOAP body in response")
    return body_el


def message_id() -> str:
    return f"uuid:{uuid.uuid4()}"
