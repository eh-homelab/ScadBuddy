"""The e2e stack's TLS edge (#4975 phase 7, spec 2026-11-01 §11): https for every host the
browsers and the API reach by name, as an ingress gives the deployment.

The login's cookies are ``__Host-`` and ``Secure``, and a frame is same-site with its top
level only by scheme and registrable domain (spec §6.1), so the auth e2e needs real https
names: ``scadbuddy.e2e.test`` (the app) and ``idp.e2e.test`` (the fake issuer). At start
this makes a throwaway CA and one certificate for every name in ``EDGE_ROUTES``, writes the
CA to ``EDGE_CA_FILE`` (the app trusts it through ``SSL_CERT_FILE`` for the issuer's
discovery, keys and token), and serves 443. Each connection goes, by its first request's
``Host``, to that name's backend, and its bytes are then copied both ways as they are, so
keep-alive and WebSockets pass untouched. HTTP/1.1 only (no ALPN ``h2``), so a browser
never coalesces two names onto one connection.

``EDGE_ROUTES`` is ``host=backend:port`` pairs, comma-separated. Run from the test image's
venv (``cryptography`` is the backend's dependency through joserfc).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import ipaddress
import os
import ssl
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

MAX_HEAD = 64 * 1024


def routes(spec: str) -> dict[str, tuple[str, int]]:
    table: dict[str, tuple[str, int]] = {}
    for pair in filter(None, (part.strip() for part in spec.split(","))):
        host, _, backend = pair.partition("=")
        name, _, port = backend.rpartition(":")
        table[host.lower()] = (name, int(port))
    return table


def certificates(names: list[str]) -> tuple[bytes, bytes, bytes]:
    """A CA's certificate, and a leaf for ``names`` with its key, all PEM."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ScadBuddy e2e CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = [x509.DNSName(name) for name in names]
    sans.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    key_pem = key.private_bytes(
        pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return ca.public_bytes(pem), leaf.public_bytes(pem), key_pem


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(ConnectionError, OSError):
            writer.close()


def host_of(head: bytes) -> str:
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"host":
            return value.strip().decode("ascii", "replace").rsplit(":", 1)[0].lower()
    return ""


async def serve(
    table: dict[str, tuple[str, int]],
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError):
        writer.close()
        return
    backend = table.get(host_of(head))
    if backend is None:
        writer.write(
            b"HTTP/1.1 421 Misdirected Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        writer.close()
        return
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(*backend)
    except OSError:
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        writer.close()
        return
    upstream_writer.write(head)
    await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))


async def main() -> None:
    table = routes(os.environ["EDGE_ROUTES"])
    ca, leaf, key = certificates(list(table))
    ca_file = Path(os.environ.get("EDGE_CA_FILE", "/run/edge-ca/ca.pem"))
    ca_file.parent.mkdir(parents=True, exist_ok=True)
    partial = ca_file.with_suffix(".tmp")
    partial.write_bytes(ca)
    partial.chmod(0o644)
    partial.replace(ca_file)

    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.set_alpn_protocols(["http/1.1"])
    with tempfile.TemporaryDirectory() as scratch:
        chain, key_file = Path(scratch, "chain.pem"), Path(scratch, "key.pem")
        chain.write_bytes(leaf + ca)
        key_file.write_bytes(key)
        context.load_cert_chain(chain, key_file)

    server = await asyncio.start_server(
        lambda r, w: serve(table, r, w),
        host="0.0.0.0",
        port=int(os.environ.get("EDGE_PORT", "443")),
        ssl=context,
        limit=MAX_HEAD,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
