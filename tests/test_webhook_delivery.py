"""Webhook SSRF defenses, tested against real local servers."""

from __future__ import annotations

import base64
import http.server
import json
import shutil
import socket
import ssl
import subprocess
import threading

import pytest

from warderbox import webhooks
from warderbox.webhooks import EventNotifier, WebhookTarget, _ip_is_private


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.received.append({
            "host": self.headers.get("Host"),
            "path": self.path,
            "body": json.loads(self.rfile.read(length)),
        })
        self.send_response(self.server.status)
        if self.server.location:
            self.send_header("Location", self.server.location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.received, srv.status, srv.location = [], 200, None
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def dns(monkeypatch):
    """A programmable resolver; 127.0.0.1 counts as public for these tests."""
    answers = []
    calls = []

    def fake(host, port, *args, **kwargs):
        calls.append(host)
        address = answers.pop(0) if len(answers) > 1 else answers[0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    real = _ip_is_private
    monkeypatch.setattr(
        webhooks, "_ip_is_private", lambda a: a != "127.0.0.1" and real(a)
    )
    return answers, calls


def _url(server, host="hook.example", path="/x?y=1"):
    return f"http://{host}:{server.server_address[1]}{path}"


def test_delivers_to_the_validated_address_with_the_original_host(server, dns):
    answers, calls = dns
    answers.append("127.0.0.1")
    EventNotifier(max_retries=0)._send_webhook(
        WebhookTarget(url=_url(server)), {"event": "e"}
    )
    assert server.received[0]["host"] == f"hook.example:{server.server_address[1]}"
    assert server.received[0]["path"] == "/x?y=1"
    assert calls == ["hook.example"]


def test_dns_rebinding_cannot_redirect_the_connection(server, dns):
    answers, calls = dns
    # Passes validation, then the name would resolve somewhere internal.
    answers.extend(["127.0.0.1", "169.254.169.254"])
    EventNotifier(max_retries=0)._send_webhook(
        WebhookTarget(url=_url(server)), {"event": "e"}
    )
    assert len(server.received) == 1
    assert len(calls) == 1  # no second lookup between check and connect


def test_private_answer_refuses_delivery(server, dns):
    answers, _calls = dns
    answers.append("10.0.0.5")
    EventNotifier(max_retries=2)._send_webhook(
        WebhookTarget(url=_url(server)), {"event": "e"}
    )
    assert server.received == []


def test_one_private_address_among_public_ones_refuses(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port)),
    ])
    assert webhooks._resolve_public("hook.example", 80) is None


def test_each_retry_resolves_again(server, dns):
    answers, calls = dns
    answers.append("127.0.0.1")
    server.status = 500
    notifier = EventNotifier(max_retries=2)
    notifier._send_webhook(WebhookTarget(url=_url(server)), {"event": "e"})
    assert len(calls) == 3
    assert len(server.received) == 3


@pytest.mark.parametrize("status", [302, 404])
def test_redirects_and_client_errors_are_not_followed_or_retried(server, dns, status):
    answers, _calls = dns
    answers.append("127.0.0.1")
    server.status = status
    server.location = "http://169.254.169.254/latest/meta-data/"
    EventNotifier(max_retries=2)._send_webhook(
        WebhookTarget(url=_url(server)), {"event": "e"}
    )
    assert len(server.received) == 1


def test_no_proxy_setting_connects_directly(server, dns, monkeypatch):
    answers, _calls = dns
    answers.append("127.0.0.1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "hook.example")
    EventNotifier(max_retries=0)._send_webhook(
        WebhookTarget(url=_url(server)), {"event": "e"}
    )
    assert len(server.received) == 1


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    """A forward proxy that reaches its one upstream whatever it is asked for."""

    def _allowed(self, destination):
        self.server.seen.append(destination)
        self.server.auth.append(self.headers.get("Proxy-Authorization"))
        if self.server.refuse_addresses and destination[0].isdigit():
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        return True

    def do_POST(self):
        destination = self.path.split("/")[2]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if not self._allowed(destination):
            return
        with socket.create_connection(self.server.upstream) as up:
            up.sendall(
                f"POST /{self.path.split('/', 3)[3]} HTTP/1.1\r\n"
                f"Host: {self.headers['Host']}\r\nContent-Length: {length}\r\n"
                f"Connection: close\r\n\r\n".encode() + body
            )
            response = up.recv(4096)
        self.wfile.write(response)
        self.close_connection = True

    def do_CONNECT(self):
        if not self._allowed(self.path):
            return
        up = socket.create_connection(self.server.upstream)
        self.send_response(200)
        self.end_headers()

        def pump(a, b):
            try:
                while data := a.recv(65536):
                    b.sendall(data)
            except OSError:
                pass
            finally:
                b.close()

        threading.Thread(target=pump, args=(up, self.connection), daemon=True).start()
        pump(self.connection, up)
        self.close_connection = True

    def log_message(self, *args):
        pass


@pytest.fixture
def proxy(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
    srv.seen, srv.auth, srv.upstream, srv.refuse_addresses = [], [], None, False
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    url = f"http://user:p%40ss@127.0.0.1:{srv.server_address[1]}"
    for name in ("http_proxy", "https_proxy"):
        monkeypatch.setenv(name, url)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    yield srv
    srv.shutdown()
    srv.server_close()


def test_http_goes_through_proxy_to_the_validated_address(server, dns, proxy):
    answers, _calls = dns
    answers.append("127.0.0.1")
    proxy.upstream = server.server_address
    port = server.server_address[1]
    EventNotifier(max_retries=0)._send_webhook(
        WebhookTarget(url=_url(server)), {"event": "e"}
    )
    assert proxy.seen == [f"127.0.0.1:{port}"]
    assert base64.b64decode(proxy.auth[0].split()[1]) == b"user:p@ss"
    assert server.received[0]["host"] == f"hook.example:{port}"
    assert server.received[0]["body"]["event"] == "e"


def test_https_tunnels_through_proxy_with_sni_and_verification(tls_server, dns, proxy):
    srv, cert = tls_server
    answers, _calls = dns
    answers.append("127.0.0.1")
    proxy.upstream = srv.server_address
    port = srv.server_address[1]
    client = ssl.create_default_context(cafile=str(cert))
    EventNotifier(max_retries=0, ssl_context=client)._send_webhook(
        WebhookTarget(url=f"https://hook.example:{port}/x"), {"event": "e"}
    )
    assert proxy.seen == [f"127.0.0.1:{port}"]
    assert base64.b64decode(proxy.auth[0].split()[1]) == b"user:p@ss"
    assert len(srv.received) == 1
    assert srv.sni == ["hook.example"]


def test_https_through_proxy_still_rejects_a_wrong_certificate(tls_server, dns, proxy):
    srv, cert = tls_server
    answers, _calls = dns
    answers.append("127.0.0.1")
    proxy.upstream = srv.server_address
    client = ssl.create_default_context(cafile=str(cert))
    EventNotifier(max_retries=0, ssl_context=client)._send_webhook(
        WebhookTarget(url=f"https://other.example:{srv.server_address[1]}/x"),
        {"event": "e"},
    )
    assert srv.received == []


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_proxy_that_refuses_addresses_gets_the_hostname(
    scheme, server, tls_server, dns, proxy
):
    srv, cert = tls_server
    answers, _calls = dns
    answers.append("127.0.0.1")
    proxy.refuse_addresses = True
    target = server if scheme == "http" else srv
    proxy.upstream = target.server_address
    port = target.server_address[1]
    client = ssl.create_default_context(cafile=str(cert))
    EventNotifier(max_retries=0, ssl_context=client)._send_webhook(
        WebhookTarget(url=f"{scheme}://hook.example:{port}/x"), {"event": "e"}
    )
    assert proxy.seen == [f"127.0.0.1:{port}", f"hook.example:{port}"]
    assert len(target.received) == 1


def test_private_answer_is_refused_before_the_proxy_is_asked(server, proxy, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port)),
    ])
    proxy.upstream = server.server_address
    EventNotifier(max_retries=2)._send_webhook(
        WebhookTarget(url="http://hook.example:81/x"), {"event": "e"}
    )
    assert proxy.seen == []
    assert server.received == []


def test_proxy_without_local_dns_is_asked_by_name(server, proxy, monkeypatch):
    real = socket.getaddrinfo

    def no_external_dns(host, *args, **kwargs):
        if host == "hook.example":
            raise socket.gaierror("no DNS here")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", no_external_dns)
    proxy.upstream = server.server_address
    port = server.server_address[1]
    EventNotifier(max_retries=0)._send_webhook(
        WebhookTarget(url=f"http://hook.example:{port}/x"), {"event": "e"}
    )
    assert proxy.seen == [f"hook.example:{port}"]
    assert len(server.received) == 1


def test_transient_dns_failure_is_retried(server, dns, monkeypatch):
    answers, _calls = dns
    answers.append("127.0.0.1")
    fake = socket.getaddrinfo
    failures = [socket.gaierror("temporary")]

    def flaky(*args, **kwargs):
        if failures:
            raise failures.pop()
        return fake(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", flaky)
    monkeypatch.setattr(webhooks.time, "sleep", lambda s: None)
    EventNotifier(max_retries=1)._send_webhook(
        WebhookTarget(url=_url(server)), {"event": "e"}
    )
    assert len(server.received) == 1


@pytest.mark.parametrize("status", [408, 425, 429, 500])
def test_transient_statuses_are_retried(server, dns, monkeypatch, status):
    answers, _calls = dns
    answers.append("127.0.0.1")
    server.status = status
    monkeypatch.setattr(webhooks.time, "sleep", lambda s: None)
    EventNotifier(max_retries=1)._send_webhook(
        WebhookTarget(url=_url(server)), {"event": "e"}
    )
    assert len(server.received) == 2


def test_allow_private_hosts_uses_standard_client(server):
    notifier = EventNotifier(max_retries=0, allow_private_hosts=True)
    notifier._send_webhook(
        WebhookTarget(url=_url(server, host="127.0.0.1")), {"event": "e"}
    )
    assert len(server.received) == 1


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1", "169.254.169.254",
    "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fc00::1", "::",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",
    "64:ff9b::a00:1", "2002:7f00:1::", "ff02::1", "not-an-ip",
])
def test_non_public_addresses(address):
    assert _ip_is_private(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses(address):
    assert not _ip_is_private(address)


@pytest.mark.parametrize("url", [
    "http://localhost/x", "http://127.0.0.1/x", "http://[::1]/x",
    "http://[::ffff:127.0.0.1]/x", "http://169.254.169.254/x",
])
def test_registration_refuses_private_literals(url):
    with pytest.raises(ValueError):
        EventNotifier().add_webhook(url)


def test_mask_failure_never_sends_the_raw_payload():
    class Broken:
        def mask(self, data):
            raise RuntimeError("no")

    masked = EventNotifier(masker=Broken())._mask_payload(
        {"event": "e", "password": "hunter2"}
    )
    assert "hunter2" not in json.dumps(masked)
    assert masked["event"] == "e"


@pytest.fixture
def tls_server(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=hook.example", "-addext", "subjectAltName=DNS:hook.example",
    ], check=True, capture_output=True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.received, srv.status, srv.location, srv.sni = [], 200, None, []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    context.sni_callback = lambda sock, name, ctx: srv.sni.append(name)
    srv.socket = context.wrap_socket(srv.socket, server_side=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, cert
    srv.shutdown()
    srv.server_close()


def test_https_is_pinned_with_sni_and_verification(tls_server, dns):
    srv, cert = tls_server
    answers, _calls = dns
    answers.append("127.0.0.1")
    client = ssl.create_default_context(cafile=str(cert))
    url = f"https://hook.example:{srv.server_address[1]}/x"
    EventNotifier(max_retries=0, ssl_context=client)._send_webhook(
        WebhookTarget(url=url), {"event": "e"}
    )
    assert len(srv.received) == 1
    assert srv.sni == ["hook.example"]


def test_https_rejects_certificate_for_another_name(tls_server, dns):
    srv, cert = tls_server
    answers, _calls = dns
    answers.append("127.0.0.1")
    client = ssl.create_default_context(cafile=str(cert))
    url = f"https://other.example:{srv.server_address[1]}/x"
    EventNotifier(max_retries=0, ssl_context=client)._send_webhook(
        WebhookTarget(url=url), {"event": "e"}
    )
    assert srv.received == []
