"""Password handling for .ovpn profiles: auth-user-pass + passphrase protected keys."""
import contextlib
from unittest import mock

import pytest

from core import Api

try:
    import threading
except ImportError:  # pragma: no cover
    raise

ENCRYPTED_INLINE = (
    'client\nremote vpn.example 1194 udp\nauth-user-pass\n<key>\n'
    '-----BEGIN ENCRYPTED PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw\n'
    '-----END ENCRYPTED PRIVATE KEY-----\n</key>\n'
)
LEGACY_DEK_INLINE = (
    'client\n<key>\n-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\n'
    'DEK-Info: AES-256-CBC,ABCDEF0123456789\n\nAAAA\n-----END RSA PRIVATE KEY-----\n</key>\n'
)
PLAIN = ('client\nremote vpn.example 1194 udp\n<ca>\n-----BEGIN CERTIFICATE-----\nAAAA\n'
         '-----END CERTIFICATE-----\n</ca>\n')


@pytest.fixture
def tmp_api(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    api = Api()
    yield api
    api.close()


def _profile(tmp_api, tmp_path, name, body, extra=None):
    path = tmp_path / name
    path.write_text(body, encoding='utf-8')
    for fname, fbody in (extra or {}).items():
        (tmp_path / fname).write_text(fbody, encoding='utf-8')
    tmp_api.remember_profile(str(path))
    return path


def test_inline_encrypted_key_needs_a_key_password(tmp_api, tmp_path):
    _profile(tmp_api, tmp_path, 'locked.ovpn', ENCRYPTED_INLINE)
    _profile(tmp_api, tmp_path, 'plain.ovpn', PLAIN)

    locked = tmp_api.inspect_profile('locked')
    assert locked['needs_keypass'] is True and locked['needs_auth'] is True
    assert locked['path'].endswith('locked.ovpn')
    assert tmp_api.inspect_profile('plain')['needs_keypass'] is False


def test_legacy_dek_header_and_external_key_file_are_detected(tmp_api, tmp_path):
    _profile(tmp_api, tmp_path, 'dek.ovpn', LEGACY_DEK_INLINE)
    assert tmp_api.inspect_profile('dek')['needs_keypass'] is True

    external = _profile(tmp_api, tmp_path, 'extkey.ovpn', 'client\nkey secret.key\n',
                        extra={'secret.key': '-----BEGIN ENCRYPTED PRIVATE KEY-----\nAA\n'
                                              '-----END ENCRYPTED PRIVATE KEY-----\n'})
    assert tmp_api.inspect_profile('extkey')['needs_keypass'] is True

    (tmp_path / 'secret.key').write_text('-----BEGIN PRIVATE KEY-----\nAA\n-----END PRIVATE KEY-----\n',
                                         encoding='utf-8')
    tmp_api.profile_map['extkey'] = str(external)
    assert tmp_api.inspect_profile('extkey')['needs_keypass'] is False


def test_askpass_is_answered_by_neon_instead_of_failing(tmp_api, tmp_path):
    _profile(tmp_api, tmp_path, 'askpass.ovpn', 'client\naskpass\n')
    info = tmp_api.inspect_profile('askpass')
    assert info['needs_askpass'] is True and info['needs_keypass'] is True


def test_auth_user_pass_file_only_prompts_when_incomplete(tmp_api, tmp_path):
    _profile(tmp_api, tmp_path, 'full.ovpn', 'client\nauth-user-pass creds.txt\n',
             extra={'creds.txt': 'alice\ns3cr3t\n'})
    _profile(tmp_api, tmp_path, 'half.ovpn', 'client\nauth-user-pass only-user.txt\n',
             extra={'only-user.txt': 'alice\n'})
    _profile(tmp_api, tmp_path, 'missing.ovpn', 'client\nauth-user-pass gone.txt\n')

    assert tmp_api.inspect_profile('full')['needs_auth'] is False
    assert tmp_api.inspect_profile('half')['needs_auth'] is True
    assert tmp_api.inspect_profile('missing')['needs_auth'] is True


def test_management_reply_formatting():
    quote = Api._mgmt_quote
    assert quote('plain') == '"plain"'
    assert quote('pa"s\\s') == '"pa\\"s\\\\s"'

    assert Api._mgmt_password_reply(">PASSWORD:Need 'Private Key' password", 'alice', 'pw', 'keypw') == \
        ['password "Private Key" "keypw"']
    assert Api._mgmt_password_reply(">PASSWORD:Need 'Auth' username/password", 'alice', 'p"w', '') == \
        ['username "Auth" "alice"', 'password "Auth" "p\\"w"']
    # Nothing to offer -> stay silent instead of sending an empty credential.
    assert Api._mgmt_password_reply(">PASSWORD:Need 'Private Key' password", 'alice', 'pw', '') == []
    assert Api._mgmt_password_reply(">PASSWORD:Need 'Auth' username", 'alice', 'pw', 'k') == \
        ['username "Auth" "alice"']
    assert Api._mgmt_password_reply('>STATE:CONNECTED', 'a', 'b', 'c') == []
    assert Api._mgmt_credential_commands('alice', 'pw', 'keypw') == [
        'password "Private Key" "keypw"', 'username "Auth" "alice"', 'password "Auth" "pw"']


@contextlib.contextmanager
def _fake_engine(tmp_api, monkeypatch, proc):
    monkeypatch.setattr('core.platform.system', lambda: 'Windows')
    monkeypatch.setattr(tmp_api, '_openvpn_exe', lambda: r'C:\OpenVPN\bin\openvpn.exe')
    with mock.patch('core.subprocess.Popen', return_value=proc) as spawn, \
            mock.patch('core.threading.Thread'):
        yield spawn


def _fake_proc():
    proc = mock.MagicMock()
    proc.poll.return_value = None
    return proc


def test_key_password_enables_the_credential_pipe(tmp_api, tmp_path, monkeypatch):
    _profile(tmp_api, tmp_path, 'locked.ovpn', ENCRYPTED_INLINE)
    proc = _fake_proc()
    started = mock.MagicMock()
    monkeypatch.setattr(tmp_api, '_start_credential_pipe', started)

    with _fake_engine(tmp_api, monkeypatch, proc) as spawn:
        missing = tmp_api.connect_vpn('locked', 'alice', 'pw')
        assert missing['needs_keypass'] is True and missing['error']
        assert not spawn.called  # nothing is started while the key password is unknown

        ok = tmp_api.connect_vpn('locked', 'alice', 'pw', 'key-secret')
        assert ok['ok'] is True

    cmd = spawn.call_args.args[0]
    assert '--management-query-passwords' in cmd and '--management-hold' in cmd
    assert '--management' in cmd
    assert '--auth-user-pass' in cmd  # the temporary credentials file is still used
    started.assert_called_once()
    assert started.call_args.args[1:] == (proc, 'alice', 'pw', 'key-secret')

    # Passphrases never reach the log, the settings file or the profile store.
    assert 'key-secret' not in '\n'.join(tmp_api.vpn_logs)
    for path in (tmp_api._settings_path(), tmp_api._profile_store_path()):
        assert 'key-secret' not in open(path, encoding='utf-8').read()
    assert tmp_api.vpn_state == 'CONNECTING'
    assert tmp_api._auth_temp_file
    with mock.patch.object(tmp_api, 'stop_vpn_hotspot'), \
            mock.patch.object(tmp_api, '_send_mgmt_sigterm', return_value=False):
        assert tmp_api.disconnect_vpn()['ok'] is True
    assert tmp_api._auth_temp_file is None
    assert tmp_api._mgmt_stop is None
    assert tmp_api.vpn_state == 'DISCONNECTED'


def test_plain_profile_is_unchanged_and_sends_no_management_query(tmp_api, tmp_path, monkeypatch):
    _profile(tmp_api, tmp_path, 'plain.ovpn', PLAIN)
    proc = _fake_proc()
    started = mock.MagicMock()
    monkeypatch.setattr(tmp_api, '_start_credential_pipe', started)

    with _fake_engine(tmp_api, monkeypatch, proc) as spawn:
        assert tmp_api.connect_vpn('plain')['ok'] is True
    cmd = spawn.call_args.args[0]
    assert '--management-query-passwords' not in cmd and '--management-hold' not in cmd
    assert '--management' in cmd  # still opened for a clean SIGTERM shutdown
    started.assert_not_called()
    assert tmp_api._auth_temp_file is None
    with mock.patch.object(tmp_api, 'stop_vpn_hotspot'), \
            mock.patch.object(tmp_api, '_send_mgmt_sigterm', return_value=False):
        tmp_api.disconnect_vpn()


def test_credential_pipe_answers_queries_and_proceeds(tmp_api, monkeypatch):
    sent = []
    chunks = [b"INFOMode=netcat\n>STATE:CONNECTING\n>PASSWORD:Need 'Private Key' password\n",
              b">PASSWORD:Verification Failed: 'Private Key'\n",
              b'']
    sock = mock.MagicMock()
    sock.recv.side_effect = chunks
    sock.sendall.side_effect = lambda data: sent.append(data.decode('utf-8'))
    proc = mock.MagicMock()
    proc.poll.side_effect = [None, None, None, None, 0]
    tmp_api.openvpn_process = proc
    monkeypatch.setattr('socket.create_connection', lambda *a, **k: sock)
    monkeypatch.setattr('time.sleep', lambda *_: None)

    tmp_api._vpn_credential_pipe(25000, proc, 'alice', 'pw', 'key-secret', threading.Event())

    text = ''.join(sent)
    assert 'password "Private Key" "key-secret"' in text
    assert 'proceed' in text
    assert 'username "Auth" "alice"' in text
    assert sock.close.called
    logs = '\n'.join(tmp_api.vpn_logs)
    assert 'rejected the key password' in logs
    assert 'key-secret' not in logs and 'alice' not in logs
    assert tmp_api.vpn_state == 'ERROR'


def test_credential_pipe_aborts_engine_when_it_cannot_connect(tmp_api, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError('connection refused')

    proc = mock.MagicMock()
    proc.poll.return_value = None
    tmp_api.openvpn_process = proc
    tmp_api.vpn_state = 'CONNECTING'
    monkeypatch.setattr('socket.create_connection', refuse)
    monkeypatch.setattr('time.sleep', lambda *_: None)

    tmp_api._vpn_credential_pipe(25000, proc, '', '', 'pw', threading.Event(), connect_timeout=0.05)

    proc.kill.assert_called_once_with()
    assert tmp_api.vpn_state == 'ERROR'
    assert 'credential pipe' in '\n'.join(tmp_api.vpn_logs)
