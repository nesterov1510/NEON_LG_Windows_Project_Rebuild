"""LG webOS LAN remote, independent of the developer-mode SSH connection.

Uses PyWebOSTV's SSAP registration and input protocol. Pairing credentials
must be treated as secrets; the protocol on port 3000 is unencrypted.
"""
import copy
from queue import Empty
from urllib.parse import urlparse

from pywebostv.connection import WebOSClient, REGISTRATION_PAYLOAD
from pywebostv.controls import InputControl


ALLOWED_KEYS = frozenset([
    'up', 'down', 'left', 'right', 'ok', 'back', 'home', 'menu', 'info',
    'volume_up', 'volume_down', 'mute', 'channel_up', 'channel_down',
    'play', 'pause', 'stop', 'rewind', 'fastforward',
    'red', 'green', 'yellow', 'blue',
] + ['num_' + str(n) for n in range(10)])


class LGRemote:
    def __init__(self, host):
        self.host = host
        self.client = None
        self.input = None

    def connect(self, client_key=None, timeout=45):
        self.close()
        client = WebOSClient(self.host)
        self.client = client
        try:
            client.sock.settimeout(10)
            client.connect()
            client.sock.settimeout(None)
            payload = copy.deepcopy(REGISTRATION_PAYLOAD)
            if client_key:
                payload['client-key'] = client_key
            queue = client.send_message('register', None, payload, get_queue=True)
            while True:
                try:
                    message = queue.get(block=True, timeout=timeout)
                except Empty:
                    raise TimeoutError('LG pairing timed out. Accept the connection request on the TV.')
                if message.get('type') == 'registered':
                    new_key = (message.get('payload') or {}).get('client-key')
                    if not new_key:
                        raise RuntimeError('LG TV did not return a pairing key.')
                    break
                if message.get('type') == 'error' or (message.get('payload') or {}).get('returnValue') is False:
                    raise PermissionError('LG TV refused pairing. Check the TV prompt and connected devices settings.')
                if (message.get('payload') or {}).get('pairingType') != 'PROMPT':
                    raise RuntimeError('Unexpected LG pairing response.')
            inp = InputControl(client)
            response = inp.request('ssap://com.webos.service.networkinput/getPointerInputSocket', None, block=True, timeout=10)
            if response.get('type') == 'error' or (response.get('payload') or {}).get('returnValue') is False:
                raise RuntimeError('LG TV rejected remote input access.')
            path = (response.get('payload') or {}).get('socketPath') or ''
            parsed = urlparse(path)
            if parsed.scheme not in ('ws', 'wss') or parsed.hostname != self.host.lower() or parsed.username or parsed.password:
                raise RuntimeError('LG TV returned an invalid remote input address.')
            inp.mouse_ws = inp.ws_class(path)
            self.input = inp
            inp.mouse_ws.sock.settimeout(10)
            inp.mouse_ws.connect()
            inp.mouse_ws.sock.settimeout(None)
            return str(new_key)
        except Exception:
            self.close()
            raise

    def send_key(self, key):
        if key not in ALLOWED_KEYS:
            raise ValueError('Unsupported LG remote button.')
        if self.input is None:
            raise ConnectionError('LG remote is not connected.')
        getattr(self.input, key)()

    def close(self):
        inp, client = self.input, self.client
        self.input = self.client = None
        if inp:
            try:
                inp.disconnect_input()
            except Exception:
                pass
        if client:
            try:
                client.close()
            except Exception:
                pass
