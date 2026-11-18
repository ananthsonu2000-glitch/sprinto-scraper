"""Domain normalization and SSRF protection for the website reader."""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

BLOCKED_HOSTNAMES = {
    "localhost", "metadata", "metadata.google.internal", "instance-data", "instance-data.ec2.internal",
}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet")
METADATA_IPS = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254"),
                ipaddress.ip_address("100.100.100.200")}
LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class UnsafeURLError(Exception):
    pass


def normalize_domain(raw: str) -> str | None:
    """'https://WWW.Acme.com/about?x' -> 'acme.com'. Returns None if not a plausible public domain."""
    s = (raw or "").strip().lower()
    if not s:
        return None
    if "://" not in s:
        s = "http://" + s
    try:
        host = urlsplit(s).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    try:
        ipaddress.ip_address(host)
        return None  # bare IPs are not company domains
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2 or not all(LABEL_RE.match(l) for l in labels):
        return None
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        return None
    return host


def normalize_domains(raws: list[str]) -> tuple[list[str], list[str]]:
    """Returns (unique normalized domains in input order, rejected inputs)."""
    seen, out, rejected = set(), [], []
    for r in raws:
        d = normalize_domain(r)
        if d is None:
            if r.strip():
                rejected.append(r.strip())
            continue
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out, rejected


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    if ip in METADATA_IPS:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return _ip_is_public(ip.ipv4_mapped)
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def check_url(url: str, resolve: bool = True, resolver=socket.getaddrinfo) -> str:
    """Validate a URL before every request (including each redirect hop).
    Allows only public http(s) on default ports. Returns the normalized URL."""
    try:
        parts = urlsplit(url.strip())
    except ValueError as e:
        raise UnsafeURLError(f"unparseable URL: {e}")
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"scheme not allowed: {parts.scheme!r}")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials in URL not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURLError("missing host")
    try:
        port = parts.port
    except ValueError:
        raise UnsafeURLError("invalid port")
    if port not in (None, 80, 443):
        raise UnsafeURLError(f"port not allowed: {port}")
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise UnsafeURLError(f"host not allowed: {host}")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not _ip_is_public(literal):
            raise UnsafeURLError(f"non-public IP: {literal}")
    elif resolve:
        try:
            infos = resolver(host, port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
        except (socket.gaierror, OSError) as e:
            raise UnsafeURLError(f"DNS resolution failed for {host}: {e}")
        addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
        if not addrs:
            raise UnsafeURLError(f"no addresses for {host}")
        bad = [str(a) for a in addrs if not _ip_is_public(a)]
        if bad:
            raise UnsafeURLError(f"{host} resolves to non-public address(es): {', '.join(bad)}")
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, parts.query, ""))


def canonical_url(url: str) -> str:
    """Lower-case host, drop default ports, fragments and trailing slashes (except root),
    so the same page is never fetched twice under different spellings."""
    p = urlsplit(url.strip())
    host = (p.hostname or "").lower().rstrip(".")
    try:
        port = p.port
    except ValueError:
        port = None
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = p.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((p.scheme.lower(), netloc, path, p.query, ""))


def same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host == domain or host.endswith("." + domain)
