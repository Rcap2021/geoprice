#!/usr/bin/env python3
"""
Local proxy relay: listens on 127.0.0.1:PORT with no auth,
authenticates to an upstream HTTP proxy using Basic auth.
Handles both HTTP and HTTPS (CONNECT tunnel) traffic.

Usage: proxy-relay.py LOCAL_PORT UPSTREAM_HOST:PORT USER:PASS
"""
import base64
import select
import socket
import sys
import threading


def relay(a, b):
    """Relay bytes between two sockets until either closes."""
    socks = [a, b]
    try:
        while True:
            r, _, _ = select.select(socks, [], [], 120)
            if not r:
                break
            for s in r:
                data = s.recv(8192)
                if not data:
                    return
                other = b if s is a else a
                other.sendall(data)
    except Exception:
        pass


def handle(client, upstream_host, upstream_port, auth_b64):
    upstream = None
    try:
        # Read request line + headers
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = client.recv(4096)
            if not chunk:
                return
            buf += chunk

        head, _, rest = buf.partition(b'\r\n\r\n')
        first_line = head.split(b'\r\n')[0].decode(errors='replace')

        upstream = socket.create_connection((upstream_host, upstream_port), timeout=30)

        if first_line.upper().startswith('CONNECT'):
            # HTTPS tunnel: forward CONNECT + auth header to upstream
            auth_line = f'\r\nProxy-Authorization: Basic {auth_b64}'.encode()
            upstream.sendall(head + auth_line + b'\r\n\r\n')
            if rest:
                upstream.sendall(rest)

            # Wait for upstream 200 Connection established
            resp = b''
            while b'\r\n\r\n' not in resp:
                resp += upstream.recv(4096)

            status_line = resp.split(b'\r\n')[0]
            if b'200' in status_line:
                client.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                relay(client, upstream)
            else:
                client.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
        else:
            # Plain HTTP: inject auth header then relay
            auth_line = f'\r\nProxy-Authorization: Basic {auth_b64}'.encode()
            upstream.sendall(head + auth_line + b'\r\n\r\n')
            if rest:
                upstream.sendall(rest)
            relay(client, upstream)

    except Exception:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            if upstream:
                upstream.close()
        except Exception:
            pass


def main():
    local_port = int(sys.argv[1])
    up_host, up_port_str = sys.argv[2].rsplit(':', 1)
    up_port = int(up_port_str)
    credentials = sys.argv[3]
    auth_b64 = base64.b64encode(credentials.encode()).decode()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', local_port))
    srv.listen(50)

    while True:
        client, _ = srv.accept()
        t = threading.Thread(
            target=handle,
            args=(client, up_host, up_port, auth_b64),
            daemon=True,
        )
        t.start()


if __name__ == '__main__':
    main()
