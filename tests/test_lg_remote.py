from queue import Queue
from unittest.mock import patch

import pytest
from lg_remote import LGRemote
from core import Api


class FakeClient:
    messages = []
    def __init__(self, host):
        self.host, self.closed = host, False
    def connect(self):
        pass
    def close(self):
        self.closed = True
    def send_message(self, kind, uri, payload, get_queue):
        self.payload = payload
        q = Queue()
        for msg in self.messages:
            q.put(msg)
        return q


class FakeInput:
    path = 'ws://192.168.1.5:3000/input'
    def __init__(self, client):
        self.sent = []
        self.ws_class = lambda path: self
    def request(self, *args, **kwargs):
        return {'payload': {'socketPath': self.path}}
    def connect(self):
        pass
    def disconnect_input(self):
        pass
    def up(self):
        self.sent.append('up')


def test_pair_and_remote_key():
    FakeClient.messages = [{'type': 'response', 'payload': {'pairingType': 'PROMPT'}},
                           {'type': 'registered', 'payload': {'client-key': 'secret'}}]
    with patch('lg_remote.WebOSClient', FakeClient), patch('lg_remote.InputControl', FakeInput):
        remote = LGRemote('192.168.1.5')
        assert remote.connect('old') == 'secret'
        assert remote.client.payload['client-key'] == 'old'
        remote.send_key('up')
        assert remote.input.sent == ['up']
        with pytest.raises(ValueError):
            remote.send_key('invalid')
        remote.close()
        with pytest.raises(ConnectionError):
            remote.send_key('up')


def test_refused_and_untrusted_pointer_address():
    with patch('lg_remote.WebOSClient', FakeClient), patch('lg_remote.InputControl', FakeInput):
        FakeClient.messages = [{'type': 'error', 'payload': {}}]
        remote = LGRemote('192.168.1.5')
        with pytest.raises(PermissionError):
            remote.connect()
        assert remote.client is None
        FakeClient.messages = [{'type': 'registered', 'payload': {'client-key': 'secret'}}]
        with patch.object(FakeInput, 'path', 'ws://evil.example:3000/input'):
            with pytest.raises(RuntimeError, match='invalid'):
                remote.connect()
        assert remote.client is None


def test_api_saves_pairing_and_handles_send_failure(tmp_path):
    class Remote:
        def __init__(self, host): self.host = host
        def connect(self, old):
            self.old = old
            return 'private-key'
        def send_key(self, key): raise OSError('connection lost')
        def close(self): pass
    api = Api()
    api._data_dir = lambda: str(tmp_path)
    with patch('core.LGRemote', Remote):
        assert api.connect_lg_remote('192.168.1.5')['ok']
        assert 'private-key' in (tmp_path / 'lg_remote_keys.json').read_text()
        assert api.connect_lg_remote('192.168.1.5')['ok']
        assert api.lg_remote.old == 'private-key'
        assert not api.lg_remote_send_key('up')['ok']
        assert api.lg_remote is None
