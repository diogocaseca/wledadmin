"""HTTPS na porta direta: carga, recarga do certificado e o servidor real (subprocesso)."""

from __future__ import annotations

import os
import signal
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import trustme

from wledadmin.tls import ReloadingTLS, TLSError


def write_cert(ca: trustme.CA, folder: Path, name: str = "fullchain.pem") -> tuple[Path, Path]:
    cert = ca.issue_cert("127.0.0.1", "localhost")
    certfile, keyfile = folder / name, folder / "privkey.pem"
    certfile.write_bytes(b"".join(b.bytes() for b in cert.cert_chain_pems))
    cert.private_key_pem.write_to_path(str(keyfile))
    return certfile, keyfile


# ---- ReloadingTLS ---------------------------------------------------------------------


def test_load_errors(tmp_path):
    with pytest.raises(TLSError, match="não encontrado"):
        ReloadingTLS(tmp_path / "x.pem", tmp_path / "y.pem").load()

    certfile, _ = write_cert(trustme.CA(), tmp_path)
    other_key = tmp_path / "other.pem"
    trustme.CA().issue_cert("x").private_key_pem.write_to_path(str(other_key))
    with pytest.raises(TLSError, match="não corresponde"):
        ReloadingTLS(certfile, other_key).load()

    garbage = tmp_path / "garbage.pem"
    garbage.write_text("isto não é um certificado")
    with pytest.raises(TLSError, match="inválidos"):
        ReloadingTLS(garbage, garbage).load()


def test_reload_only_when_changed_and_keeps_old_on_error(tmp_path):
    certfile, keyfile = write_cert(trustme.CA(), tmp_path)
    tls = ReloadingTLS(certfile, keyfile)
    tls.load()
    assert tls.reload_if_changed() is False  # nada mudou

    certfile.write_text("corrompido")
    os.utime(certfile, (time.time() + 5, time.time() + 5))
    assert tls.reload_if_changed() is False  # inválido: mantém o anterior
    assert tls.reload_if_changed() is False  # e não insiste no mesmo arquivo ruim

    write_cert(trustme.CA(), tmp_path)
    os.utime(certfile, (time.time() + 10, time.time() + 10))
    os.utime(keyfile, (time.time() + 10, time.time() + 10))
    assert tls.reload_if_changed() is True


# ---- servidor real --------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"servidor saiu: {proc.communicate()[1]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"porta {port} não abriu")


def peer_issuer(port: int, cafile: Path) -> str:
    ctx = ssl.create_default_context(cafile=str(cafile))
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw, \
            ctx.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
        return str(tls.getpeercert()["issuer"])


@pytest.fixture
def server(tmp_path):
    procs = []

    def start(**env_extra) -> tuple[subprocess.Popen, dict]:
        env = {**os.environ,
               "WLEDADMIN_DATA_DIR": str(tmp_path / "data"),
               "WLEDADMIN_HOST": "127.0.0.1",
               "WLEDADMIN_MDNS_ENABLED": "false",
               "WLEDADMIN_LOG_LEVEL": "WARNING",
               **{k: str(v) for k, v in env_extra.items()}}
        proc = subprocess.Popen([sys.executable, "-m", "wledadmin.cli", "serve"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=tmp_path)
        procs.append(proc)
        return proc, env

    yield start
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait()


def test_https_direct_http_panel_reload_and_shutdown(tmp_path, server):
    ca1, ca2 = trustme.CA(), trustme.CA()
    certfile, keyfile = write_cert(ca1, tmp_path)
    ca1_file, ca2_file = tmp_path / "ca1.pem", tmp_path / "ca2.pem"
    ca1.cert_pem.write_to_path(str(ca1_file))
    ca2.cert_pem.write_to_path(str(ca2_file))
    direct, panel = free_port(), free_port()

    proc, _ = server(WLEDADMIN_PORT=direct, WLEDADMIN_INGRESS=true_str(),
                     WLEDADMIN_INGRESS_PORT=panel, WLEDADMIN_INGRESS_PROXY_IP="127.0.0.1",
                     WLEDADMIN_SSL_CERTFILE=certfile, WLEDADMIN_SSL_KEYFILE=keyfile,
                     WLEDADMIN_SSL_RELOAD_INTERVAL="0.3")
    wait_port(direct, proc)
    wait_port(panel, proc)

    r = httpx.get(f"https://127.0.0.1:{direct}/healthz", verify=ssl.create_default_context(
        cafile=str(ca1_file)))
    assert r.status_code == 200
    assert httpx.get(f"https://127.0.0.1:{direct}/", verify=ssl.create_default_context(
        cafile=str(ca1_file))).status_code == 401  # login continua exigido
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://127.0.0.1:{direct}/healthz", timeout=3)  # http na porta https

    r = httpx.get(f"http://127.0.0.1:{panel}/",
                  headers={"X-Ingress-Path": "/api/hassio_ingress/TOKEN"})
    assert r.status_code == 200 and "/api/hassio_ingress/TOKEN/firmware" in r.text

    # renovação: o certificado novo vale para conexões novas, sem reiniciar
    first = peer_issuer(direct, ca1_file)
    write_cert(ca2, tmp_path)
    deadline = time.monotonic() + 10
    while True:
        try:
            second = peer_issuer(direct, ca2_file)
            break
        except ssl.SSLError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)
    assert first != second

    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 0, err
    assert "https" in out and f"porta {panel} (http)" in out


def test_bad_certificate_as_ha_app_keeps_panel(tmp_path, server):
    panel = free_port()
    proc, _ = server(WLEDADMIN_PORT=free_port(), WLEDADMIN_INGRESS=true_str(),
                     WLEDADMIN_INGRESS_PORT=panel, WLEDADMIN_INGRESS_PROXY_IP="127.0.0.1",
                     WLEDADMIN_SSL_CERTFILE=tmp_path / "nao-existe.pem")
    wait_port(panel, proc)
    r = httpx.get(f"http://127.0.0.1:{panel}/",
                  headers={"X-Ingress-Path": "/api/hassio_ingress/TOKEN"})
    assert r.status_code == 200
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=20)
    assert "não encontrado" in err and "NÃO foi iniciado" in err
    assert "acesso direto" not in out


def test_bad_certificate_standalone_exits(tmp_path, server):
    proc, _ = server(WLEDADMIN_PORT=free_port(),
                     WLEDADMIN_SSL_CERTFILE=tmp_path / "nao-existe.pem")
    out, err = proc.communicate(timeout=20)
    assert proc.returncode != 0 and "HTTPS:" in err and "não encontrado" in err


def true_str() -> str:
    return "true"


def test_sigterm_exits_cleanly_without_tls(tmp_path, server):
    """Também sem HTTPS (uma porta só): SIGTERM encerra com código 0."""
    port = free_port()
    proc, _ = server(WLEDADMIN_PORT=port)
    wait_port(port, proc)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=20)
    assert proc.returncode == 0
