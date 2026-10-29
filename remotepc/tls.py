"""Self-signed TLS certificate for the host.

Browsers show a warning for a self-signed certificate, but after the user
accepts it the password proof and the screen stream are encrypted.  The
fingerprint printed by the host can be compared with the browser's certificate
viewer.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .winapi import local_ipv4_addresses


def default_cert_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "RemotePC" / "certs"
    return Path.home() / ".remotepc" / "certs"


def _fingerprint(cert: x509.Certificate) -> str:
    digest = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _not_after(cert: x509.Certificate) -> dt.datetime:
    value = getattr(cert, "not_valid_after_utc", None)
    if value is not None:
        return value
    return cert.not_valid_after.replace(tzinfo=dt.timezone.utc)


def _not_before(cert: x509.Certificate) -> dt.datetime:
    value = getattr(cert, "not_valid_before_utc", None)
    if value is not None:
        return value
    return cert.not_valid_before.replace(tzinfo=dt.timezone.utc)


def _required_ips(extra: list[str]) -> set[str]:
    ips = {"127.0.0.1"}
    for value in local_ipv4_addresses():
        try:
            ips.add(str(ipaddress.ip_address(value)))
        except ValueError:
            pass
    for value in extra:
        try:
            ips.add(str(ipaddress.ip_address(value.strip())))
        except ValueError:
            pass
    return ips


def _hostname() -> str | None:
    name = socket.gethostname()
    if not name:
        return None
    try:
        return name.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def _existing_is_usable(cert_path: Path, key_path: Path, extra_ips: list[str]) -> bool:
    if not cert_path.exists() or not key_path.exists():
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except Exception:
        return False
    now = dt.datetime.now(dt.timezone.utc)
    if _not_after(cert) < now + dt.timedelta(days=20) or _not_before(cert) > now:
        return False
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        present = {str(item) for item in san.get_values_for_type(x509.IPAddress)}
    except x509.ExtensionNotFound:
        return False
    required = _required_ips(extra_ips)
    return required.issubset(present)


def ensure_cert(cert_dir: Path, extra_ips: list[str] | None = None) -> tuple[Path, Path, str]:
    extra_ips = extra_ips or []
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"
    if _existing_is_usable(cert_path, key_path, extra_ips):
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        return cert_path, key_path, _fingerprint(cert)

    now = dt.datetime.now(dt.timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject_attrs = [
        x509.NameAttribute(NameOID.COMMON_NAME, "RemotePC"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "RemotePC local host"),
    ]
    subject = x509.Name(subject_attrs)
    san_items: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    hostname = _hostname()
    if hostname and hostname != "localhost":
        san_items.append(x509.DNSName(hostname))
    for value in sorted(_required_ips(extra_ips)):
        try:
            san_items.append(x509.IPAddress(ipaddress.ip_address(value)))
        except ValueError:
            pass

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san_items), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)

    key_tmp = key_path.with_suffix(".tmp")
    cert_tmp = cert_path.with_suffix(".tmp")
    key_tmp.write_bytes(key_bytes)
    cert_tmp.write_bytes(cert_bytes)
    try:
        os.chmod(key_tmp, 0o600)
    except OSError:
        pass
    key_tmp.replace(key_path)
    cert_tmp.replace(cert_path)
    return cert_path, key_path, _fingerprint(cert)
