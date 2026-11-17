"""Private HTTPS for the phone scanner.

Phone browsers only allow camera access on secure (HTTPS) pages. We create a small local
certificate authority once (stored in the Torino home folder, never in the repo or release) and
issue a server certificate for this laptop's LAN addresses. You install the CA certificate on the
phone once; after that the phone trusts https://<laptop-ip>:<port> without warnings.

The CA is **name-constrained** to private networks (10/8, 172.16/12, 192.168/16, 169.254/16,
100.64/10, 127/8) and ``.local``/this hostname, so even if its key leaked it could not be used
to impersonate public websites.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

PRIVATE_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "127.0.0.0/8"]
SERVER_DAYS = 397  # Apple requires <= 398 days for TLS server certificates


def lan_ips() -> list[str]:
    """IPv4 addresses of this machine, primary (default-route) address first."""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packets are sent for UDP connect
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    good = [ip for ip in ips if not ip.startswith("127.") and _is_private(ip)]
    return good


def _is_private(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return any(a in ipaddress.ip_network(n) for n in PRIVATE_NETS)


def _write_key(key, path: Path) -> None:
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))


class LocalTLS:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.ca_cert_path = self.folder / "torino-ca.crt"
        self.ca_key_path = self.folder / "torino-ca.key"
        self.cert_path = self.folder / "server.crt"
        self.key_path = self.folder / "server.key"

    # -- CA -------------------------------------------------------------------
    def _ensure_ca(self):
        if self.ca_cert_path.exists() and self.ca_key_path.exists():
            key = serialization.load_pem_private_key(self.ca_key_path.read_bytes(), None)
            cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
            return key, cert
        key = ec.generate_private_key(ec.SECP256R1())
        host = socket.gethostname().lower()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Torino Garage local CA ({host})"),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Torino Garage (private)")])
        now = dt.datetime.now(dt.timezone.utc)
        permitted = [x509.IPAddress(ipaddress.ip_network(n)) for n in PRIVATE_NETS]
        permitted += [x509.DNSName("local"), x509.DNSName("localhost"), x509.DNSName(host)]
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256())
        )
        _write_key(key, self.ca_key_path)
        self.ca_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return key, cert

    def ca_der(self) -> bytes:
        self._ensure_ca()
        cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
        return cert.public_bytes(serialization.Encoding.DER)

    def ca_fingerprint(self) -> str:
        cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
        fp = cert.fingerprint(hashes.SHA256()).hex().upper()
        return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))

    # -- server certificate ---------------------------------------------------
    def ensure_server_cert(self, ips: list[str] | None = None) -> tuple[Path, Path, list[str]]:
        ca_key, ca_cert = self._ensure_ca()
        ips = list(dict.fromkeys((ips if ips is not None else lan_ips()) + ["127.0.0.1"]))
        host = socket.gethostname().lower()
        dns = ["localhost", host, f"{host}.local"]
        if self.cert_path.exists() and self.key_path.exists():
            cur = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
            san = cur.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            have = {str(a) for a in san.get_values_for_type(x509.IPAddress)}
            fresh = cur.not_valid_after_utc - dt.datetime.now(dt.timezone.utc) > dt.timedelta(days=30)
            same_ca = cur.issuer == ca_cert.subject
            if set(ips) <= have and fresh and same_ca:
                return self.cert_path, self.key_path, ips
        key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.timezone.utc)
        san = [x509.IPAddress(ipaddress.ip_address(i)) for i in ips] + [x509.DNSName(d) for d in dns]
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Torino on {host}")]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=SERVER_DAYS))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, key_cert_sign=False,
                                         crl_sign=False, content_commitment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        _write_key(key, self.key_path)
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM)
                                   + ca_cert.public_bytes(serialization.Encoding.PEM))
        return self.cert_path, self.key_path, ips
