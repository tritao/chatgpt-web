"""Exercise the production HTTP adapter over real sockets, with controlled DNS."""
import socket
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import anyio
from anyio._backends._asyncio import AsyncIOBackend
import httpx

from network import HTTPClient, connection_progress


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class NetworkTests(unittest.TestCase):
    def server(self, handler, ipv6=False):
        class Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6 if ipv6 else socket.AF_INET
        try:
            server = Server(('::1' if ipv6 else '127.0.0.1', 0), handler)
        except OSError:
            if ipv6:
                self.skipTest('IPv6 loopback unavailable')
            raise
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def close():
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
        self.addCleanup(close)
        return server

    def dns(self, port):
        original = socket.getaddrinfo
        def lookup(host, *args, **kwargs):
            if host == 'dualstack.test' or host == b'dualstack.test':
                return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('::1', port, 0, 0)),
                        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', port))]
            return original(host, *args, **kwargs)
        return patch('socket.getaddrinfo', side_effect=lookup)

    def test_stalled_ipv6_is_cancelled_and_post_is_sent_once_over_ipv4(self):
        received = []
        stages = []
        class Handler(QuietHandler):
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers['Content-Length'])))
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = self.server(Handler)
        original = AsyncIOBackend.connect_tcp
        attempts = []
        cancelled = threading.Event()
        async def connect(cls, host, port, local_address=None):
            attempts.append(host)
            if host == '::1':
                try:
                    await anyio.sleep_forever()
                finally:
                    cancelled.set()
            self.assertFalse(cancelled.is_set(), 'IPv4 must start while IPv6 is still pending')
            return await original(host, port, local_address)
        with self.dns(server.server_port), patch.object(AsyncIOBackend, 'connect_tcp', classmethod(connect)):
            with HTTPClient(trust_env=False, timeout=2) as client:
                response = client.request('POST', f'http://dualstack.test:{server.server_port}/send', json={'prompt':'fixture'},
                    extensions=connection_progress(stages.append, 'waiting_response'))
        self.assertEqual(response.json(), {'ok':True})
        self.assertEqual(attempts, ['::1', '127.0.0.1'])
        self.assertTrue(cancelled.is_set())
        self.assertEqual(received, [b'{"prompt":"fixture"}'])
        self.assertEqual(stages, [{'type':'status','stage':'connecting'},
                                  {'type':'status','stage':'waiting_response'}])

    def test_working_ipv6_remains_enabled(self):
        received = []
        class Handler(QuietHandler):
            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok')
        server = self.server(Handler, ipv6=True)
        with self.dns(server.server_port), HTTPClient(trust_env=False, timeout=2) as client:
            response = client.get(f'http://dualstack.test:{server.server_port}/ipv6')
        self.assertEqual(response.text, 'ok')
        self.assertEqual(received, ['/ipv6'])

    def test_connection_deadline_cancels_both_attempts_without_sending(self):
        attempts, cancelled = [], []
        async def connect(cls, host, port, local_address=None):
            attempts.append(host)
            try:
                await anyio.sleep_forever()
            finally:
                cancelled.append(host)
        with self.dns(12345), patch.object(AsyncIOBackend, 'connect_tcp', classmethod(connect)):
            with HTTPClient(trust_env=False, timeout=httpx.Timeout(2, connect=.5)) as client:
                with self.assertRaises(httpx.ConnectTimeout):
                    client.request('POST', 'http://dualstack.test/send', content=b'fixture')
        self.assertEqual(set(attempts), {'::1', '127.0.0.1'})
        self.assertEqual(set(cancelled), set(attempts))

    def test_gzipped_sse_is_incremental_and_stream_closes(self):
        compressor = zlib.compressobj(wbits=31)
        first = compressor.compress(b'data: first\n\n') + compressor.flush(zlib.Z_SYNC_FLUSH)
        last = compressor.compress(b'data: last\n\n') + compressor.flush()
        release = threading.Event()
        received = []
        class Handler(QuietHandler):
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Encoding', 'gzip')
                self.send_header('Content-Length', str(len(first)+len(last)))
                self.end_headers()
                self.wfile.write(first)
                self.wfile.flush()
                release.wait(3)
                self.wfile.write(last)
        server = self.server(Handler)
        self.addCleanup(release.set)
        with HTTPClient(trust_env=False, timeout=2) as client:
            try:
                with client.stream('POST', f'http://127.0.0.1:{server.server_port}/stream', content=b'fixture') as response:
                    lines = response.iter_lines()
                    self.assertEqual(next(lines), 'data: first')
                    self.assertFalse(release.is_set())
                    release.set()
                    self.assertEqual(list(lines), ['', 'data: last', ''])
                self.assertTrue(response.is_closed)
            finally:
                release.set()
        self.assertEqual(received, [b'fixture'])

    def test_pool_and_cookies_survive_between_calls(self):
        received = []
        class Handler(QuietHandler):
            protocol_version = 'HTTP/1.1'
            def do_GET(self):
                received.append((self.client_address, self.headers.get('Cookie')))
                self.send_response(200)
                self.send_header('Set-Cookie', 'fixture=value; Path=/')
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok')
        server = self.server(Handler)
        with HTTPClient(trust_env=False, timeout=2) as client:
            url = f'http://127.0.0.1:{server.server_port}/'
            self.assertEqual(client.get(url).text, 'ok')
            self.assertEqual(client.get(url).text, 'ok')
        self.assertEqual(len(received), 2)
        self.assertEqual(received[0][0], received[1][0])
        self.assertEqual(received[1][1], 'fixture=value')

    def test_early_stream_close_closes_async_source_without_replay(self):
        closed, received = [], []
        class Source(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'first\n'
                await anyio.sleep_forever()
            async def aclose(self):
                closed.append(True)
        async def handle(request):
            received.append(request.method)
            return httpx.Response(200, stream=Source())
        with HTTPClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaisesRegex(RuntimeError, 'consumer stopped'):
                with client.stream('POST', 'https://example.test/', content=b'fixture') as response:
                    self.assertEqual(next(response.iter_lines()), 'first')
                    raise RuntimeError('consumer stopped')
        client.close()
        self.assertEqual(closed, [True])
        self.assertEqual(received, ['POST'])
