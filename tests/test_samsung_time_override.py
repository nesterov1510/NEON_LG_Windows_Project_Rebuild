import http.server
import json
import socket
import struct
import threading
import time
import urllib.request

import pytest

from core import Api
import samsung_time_override as sto


def _start_mock_upstream_dns(records):
    """Run a tiny UDP DNS responder returning A records from `records` dict."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    stop = threading.Event()

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                q = sto.parse_dns_question(data)
                ip = records.get(q['normalized_name'])
                if ip and q['qtype'] == 1:
                    resp = sto.build_dns_a_response(q, ip, ttl=60)
                else:
                    resp = struct.pack('!HHHHHH', q['txid'], 0x8183, 1, 0, 0, 0) + q['question_wire']
                sock.sendto(resp, addr)
            except Exception:
                pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()

    def close():
        stop.set()
        try:
            sock.close()
        except OSError:
            pass
        t.join(timeout=1.0)

    return port, close


def _start_mock_ntp_server(offset_sec=5.0):
    """Run a tiny UDP NTP server shifted by `offset_sec` from local clock."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    stop = threading.Event()

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 48:
                continue
            now_s = time.time() + offset_sec
            rx_s, rx_f = sto._unix_to_ntp(now_s)
            tx_s, tx_f = sto._unix_to_ntp(now_s)
            reply = bytearray(48)
            reply[0] = 0x1C  # LI=0, VN=3, Mode=4
            reply[1] = 1
            reply[12:16] = b'TEST'
            struct.pack_into('!II', reply, 16, rx_s, rx_f)
            reply[24:32] = data[40:48]
            struct.pack_into('!IIII', reply, 32, rx_s, rx_f, tx_s, tx_f)
            sock.sendto(bytes(reply), addr)

    t = threading.Thread(target=serve, daemon=True)
    t.start()

    def close():
        stop.set()
        try:
            sock.close()
        except OSError:
            pass
        t.join(timeout=1.0)

    return port, close


def _udp_dns_query(host, port, domain, qtype=1):
    txid, pkt = sto.build_dns_query_packet(domain, qtype=qtype)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)
    try:
        sock.sendto(pkt, (host, port))
        data, _ = sock.recvfrom(4096)
    finally:
        sock.close()
    r_txid, r_flags, qd, an, _ns, _ar = struct.unpack('!HHHHHH', data[:12])
    assert r_txid == txid
    pos = 12
    for _ in range(qd):
        _, pos = sto._decode_dns_name(data, pos)
        pos += 4
    answers = []
    for _ in range(an):
        _, pos = sto._decode_dns_name(data, pos)
        rtype, rclass, ttl, rdlen = struct.unpack('!HHIH', data[pos:pos + 10])
        pos += 10
        rdata = data[pos:pos + rdlen]
        pos += rdlen
        if rtype == 1 and rdlen == 4:
            answers.append(('A', socket.inet_ntoa(rdata), ttl))
        else:
            answers.append((rtype, rdata, ttl))
    return r_flags & 0x0F, answers


def _tcp_dns_query(host, port, domain, qtype=1):
    txid, pkt = sto.build_dns_query_packet(domain, qtype=qtype)
    conn = socket.create_connection((host, port), timeout=3.0)
    try:
        conn.sendall(struct.pack('!H', len(pkt)) + pkt)
        hdr = conn.recv(2)
        msg_len = struct.unpack('!H', hdr)[0]
        buf = bytearray()
        while len(buf) < msg_len:
            chunk = conn.recv(msg_len - len(buf))
            assert chunk
            buf.extend(chunk)
        data = bytes(buf)
    finally:
        conn.close()
    r_txid, r_flags, qd, an, _ns, _ar = struct.unpack('!HHHHHH', data[:12])
    assert r_txid == txid
    assert (r_flags & 0x0F) == 0
    pos = 12
    for _ in range(qd):
        _, pos = sto._decode_dns_name(data, pos)
        pos += 4
    assert an == 1
    _, pos = sto._decode_dns_name(data, pos)
    rtype, _rclass, _ttl, rdlen = struct.unpack('!HHIH', data[pos:pos + 10])
    pos += 10
    assert rtype == 1 and rdlen == 4
    return socket.inet_ntoa(data[pos:pos + 4])


def test_normalize_target_and_parse_sources():
    assert sto.normalize_time_target('time.cloudflare.com')['host'] == 'time.cloudflare.com'
    assert sto.normalize_time_target('local')['is_local'] is True
    u = sto.normalize_time_target('https://time.example.org:8443/openapi/timesync')
    assert u['scheme'] == 'https'
    assert u['host'] == 'time.example.org'
    assert u['port'] == 8443
    assert sto.normalize_time_target('192.168.1.50')['is_ip'] is True

    with pytest.raises(ValueError):
        sto.normalize_time_target('')
    with pytest.raises(ValueError):
        sto.normalize_time_target('0.0.0.0')
    with pytest.raises(ValueError):
        sto.normalize_time_target('ftp://bad.example.com')

    srcs = sto.parse_source_domains('time.samsungcloudsolution.com, custom.time.tv', intercept_check=True)
    assert 'time.samsungcloudsolution.com' in srcs
    assert 'custom.time.tv' in srcs
    assert 'cdn.samsungcloudsolution.com' in srcs

    srcs_no_chk = sto.parse_source_domains('time.samsungcloudsolution.com', intercept_check=False)
    assert srcs_no_chk == ['time.samsungcloudsolution.com']

    assert sto.parse_upstream_dns('127.0.0.1, 1.1.1.1, 8.8.8.8') == ['1.1.1.1', '8.8.8.8']
    assert sto.parse_upstream_dns('127.0.0.1', allow_loopback=True) == ['127.0.0.1']


def test_gateway_end_to_end_dns_http_timesync_checkxml_ntp():
    up_port, close_up = _start_mock_upstream_dns({'example.org': '93.184.216.34'})
    ntp_port, close_ntp = _start_mock_ntp_server(offset_sec=12.0)

    # Mock a custom HTTP /openapi/timesync upstream server shifted +12s via NTP or HTTP
    class MockRemoteHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            ms = int(round((time.time() + 12.0) * 1000.0))
            body = struct.pack('<QQ', ms, ms)
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    remote_srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), MockRemoteHandler)
    remote_http_port = remote_srv.server_address[1]
    remote_thread = threading.Thread(target=remote_srv.serve_forever, daemon=True)
    remote_thread.start()

    srv = sto.SamsungTimeOverrideServer()
    try:
        # Also verify SNTP client against mock NTP server
        sntp = sto.fetch_sntp_time('127.0.0.1', port=ntp_port, upstream_dns=['127.0.0.1'])
        assert 10.5 <= sntp['offset_sec'] <= 13.5

        status = srv.start(
            target=f'http://127.0.0.1:{remote_http_port}',
            mode='gateway',
            source_domains='time.samsungcloudsolution.com, time.samsungcloudsolution.net',
            intercept_check=True,
            upstream_dns=['127.0.0.1'],
            upstream_port=up_port,
            allow_loopback_upstream=True,
            advertise_ip='192.168.1.45',
            dns_bind_ip='127.0.0.1',
            dns_port=0,
            http_bind_ip='127.0.0.1',
            http_port=0,
            ntp_port=0,
            manage_firewall=False,
        )
        assert status['running'] is True
        assert 10500 <= status['offset_ms'] <= 13500
        d_port = status['dns_port']
        h_port = status['http_port']
        n_port = status['ntp_port']
        assert d_port > 0 and h_port > 0 and n_port > 0

        # 1. Samsung domains intercepted over UDP & TCP DNS
        rcode, ans = _udp_dns_query('127.0.0.1', d_port, 'Time.SamsungCloudSolution.COM', qtype=1)
        assert rcode == 0 and ans == [('A', '192.168.1.45', 30)]

        rcode_chk, ans_chk = _udp_dns_query('127.0.0.1', d_port, 'cdn.samsungcloudsolution.com', qtype=1)
        assert rcode_chk == 0 and ans_chk == [('A', '192.168.1.45', 30)]

        # 2. AAAA returns NOERROR NODATA so TV uses overridden IPv4
        rcode_aaaa, ans_aaaa = _udp_dns_query('127.0.0.1', d_port, 'time.samsungcloudsolution.com', qtype=28)
        assert rcode_aaaa == 0 and ans_aaaa == []

        # 3. TCP DNS works cleanly too
        assert _tcp_dns_query('127.0.0.1', d_port, 'time.samsungcloudsolution.net') == '192.168.1.45'

        # 4. Non-Samsung domains forward to upstream DNS
        rcode_fwd, ans_fwd = _udp_dns_query('127.0.0.1', d_port, 'example.org', qtype=1)
        assert rcode_fwd == 0 and ans_fwd == [('A', '93.184.216.34', 60)]

        # 5. HTTP /openapi/timesync returns exact 16-byte <QQ epoch milliseconds
        t_before = time.time()
        with urllib.request.urlopen(f'http://127.0.0.1:{h_port}/openapi/timesync?client=T20O', timeout=3) as resp:
            assert resp.status == 200
            assert resp.headers.get('Date')
            payload = resp.read()
        assert len(payload) == 16
        ms1, ms2 = struct.unpack('<QQ', payload)
        assert ms1 == ms2
        assert (t_before + 10.0) * 1000.0 <= ms1 <= (time.time() + 14.0) * 1000.0

        # 6. HTTP check.xml & timezoneoffset
        with urllib.request.urlopen(f'http://127.0.0.1:{h_port}/Public/network/files/check.xml', timeout=3) as resp:
            assert resp.status == 200
            assert resp.read().strip() == b'ok'
        with urllib.request.urlopen(f'http://127.0.0.1:{h_port}/openapi/zipcode/timezoneoffset', timeout=3) as resp:
            assert resp.status == 200
            tz_data = json.loads(resp.read().decode('utf-8'))
            assert 'offset' in tz_data

        # 7. Built-in UDP NTP responder reflects synchronized offset
        ntp_res = sto.fetch_sntp_time('127.0.0.1', port=n_port, upstream_dns=['127.0.0.1'])
        assert 10.5 <= ntp_res['offset_sec'] <= 13.5

        final = srv.get_status()
        assert final['stats']['dns_overridden'] >= 4
        assert final['stats']['dns_forwarded'] == 1
        assert final['stats']['http_timesync'] == 1
        assert final['stats']['http_check'] == 1
        assert final['stats']['http_tz'] == 1
        assert final['stats']['ntp_hits'] == 1
    finally:
        srv.stop()
        remote_srv.shutdown()
        remote_srv.server_close()
        close_up()
        close_ntp()
    assert srv.get_status()['running'] is False


def test_dns_direct_mode_rewrites_to_custom_domain_ip():
    up_port, close_up = _start_mock_upstream_dns({'my-custom-ntp.example': '203.0.113.77'})
    srv = sto.SamsungTimeOverrideServer()
    try:
        st = srv.start(
            target='my-custom-ntp.example',
            mode='dns_direct',
            source_domains='time.samsungcloudsolution.com',
            intercept_check=False,
            upstream_dns=['127.0.0.1'],
            upstream_port=up_port,
            allow_loopback_upstream=True,
            dns_bind_ip='127.0.0.1',
            dns_port=0,
            manage_firewall=False,
        )
        assert st['running'] is True
        assert st['mode'] == 'dns_direct'
        assert st['direct_ip'] == '203.0.113.77'
        assert st['http_port'] == 0
        assert st['ntp_port'] == 0

        rcode, ans = _udp_dns_query('127.0.0.1', st['dns_port'], 'time.samsungcloudsolution.com', qtype=1)
        assert rcode == 0 and ans == [('A', '203.0.113.77', 30)]
    finally:
        srv.stop()
        close_up()


def test_api_samsung_time_settings_and_close(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))

    api = Api()
    st0 = api.get_samsung_time_status()
    assert st0['running'] is False
    assert st0['target'] == 'time.cloudflare.com'

    sync_res = api.sync_samsung_time_now('local')
    assert sync_res['ok'] is True
    assert sync_res['method'] == 'local-pc'
    assert sync_res['offset_ms'] == 0

    saved = api.save_samsung_time_settings(
        target='pool.ntp.org',
        mode='gateway',
        source_domains='time.samsungcloudsolution.com, custom.time.test',
        intercept_check=False,
        upstream_dns='9.9.9.9',
    )
    assert saved['target'] == 'pool.ntp.org'
    assert saved['intercept_check'] is False

    loaded = api.load_settings()
    assert loaded['samsung_time_target'] == 'pool.ntp.org'
    assert loaded['samsung_time_sources'] == 'time.samsungcloudsolution.com, custom.time.test'
    assert loaded['samsung_time_intercept_check'] is False
    assert loaded['samsung_time_upstream_dns'] == '9.9.9.9'

    # Reject loopback self-redirect target inside intercepted domains
    bad = api.start_samsung_time_override(
        target='time.samsungcloudsolution.com',
        mode='gateway',
        source_domains='time.samsungcloudsolution.com',
    )
    assert bad['ok'] is False

    # Close cleanly tears down any active time server
    stopped = api.stop_samsung_time_override()
    assert stopped['running'] is False
    api.close()
