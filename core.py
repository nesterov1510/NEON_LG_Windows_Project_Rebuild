"""Platform services shared by both NEON user interfaces."""
import base64
import ctypes
import glob
import io
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request

import paramiko

from samsung_tv import (
    SAMSUNG_LOCAL_SETS,
    SamsungLegacyClient,
    SamsungWsClient,
    discover_samsung_tvs,
    fetch_samsung_device_info,
)
from samsung_tizen import (
    BELET_APP_ID,
    BELET_PKG_ID,
    BELET_URL,
    SamsungCertManager,
    SdbClient,
    SdbError,
    _format_chain_summary,
    build_belet_wgt,
    diagnose_install_output,
    get_local_ip_for_tv as _tizen_local_ip,
    sign_wgt_bytes,
    verify_wgt_signatures,
)
from alemtv_app import (
    ALEMTV_APP_ID,
    ALEMTV_DEFAULT_PORTAL,
    ALEMTV_PKG_ID,
    ALEMTV_WEBOS_ID,
    build_alemtv_ipk,
    build_alemtv_wgt,
)

REGION_CODES = {'22282', '3642', '19461', '13741', '13869', '19345', '18362', '18789', '4837'}

DNS_PRESETS = {
    'auto': {'label_en': 'Auto (from Conf / DHCP)', 'label_ru': 'Авто (от Conf / DHCP)', 'servers': []},
    'cloudflare': {'label_en': 'Cloudflare · 1.1.1.1 / 1.0.0.1', 'label_ru': 'Cloudflare · 1.1.1.1 / 1.0.0.1', 'servers': ['1.1.1.1', '1.0.0.1']},
    'google': {'label_en': 'Google · 8.8.8.8 / 8.8.4.4', 'label_ru': 'Google · 8.8.8.8 / 8.8.4.4', 'servers': ['8.8.8.8', '8.8.4.4']},
    'quad9': {'label_en': 'Quad9 · 9.9.9.9 / 149.112.112.112', 'label_ru': 'Quad9 · 9.9.9.9 / 149.112.112.112', 'servers': ['9.9.9.9', '149.112.112.112']},
    'adguard': {'label_en': 'AdGuard · 94.140.14.14 / 94.140.15.15', 'label_ru': 'AdGuard · 94.140.14.14 / 94.140.15.15', 'servers': ['94.140.14.14', '94.140.15.15']},
    'opendns': {'label_en': 'OpenDNS · 208.67.222.222 / 208.67.220.220', 'label_ru': 'OpenDNS · 208.67.222.222 / 208.67.220.220', 'servers': ['208.67.222.222', '208.67.220.220']},
    'yandex': {'label_en': 'Yandex · 77.88.8.8 / 77.88.8.1', 'label_ru': 'Яндекс · 77.88.8.8 / 77.88.8.1', 'servers': ['77.88.8.8', '77.88.8.1']},
    'custom': {'label_en': 'Custom DNS (manual IPv4)', 'label_ru': 'Свой DNS (вручную IPv4)', 'servers': []},
}


def resource(name):
    root = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, name)


class Api:
    def __init__(self):
        self.client = None
        self.window = None
        self.app_shared_pair = None
        self.remote_script = '/tmp/change_region.sh'
        self.openvpn_process = None
        self.vpn_logs = []
        self.vpn_state = 'DISCONNECTED'
        self.vpn_profile = None
        self.vpn_lock = threading.Lock()
        self.hotspot_active = False
        self.hotspot_mode = None
        self.ethernet_active = False
        self.ethernet_adapter = None
        self.vpn_adapter_name = None
        self.vpn_adapter_guid = None
        self.vpn_ifindex = None
        self._uplink_dns_backups = {}
        self._skip_set_sharing_dns = False
        self.profile_map = {}
        self._vpn_stop_requested = False
        self._auth_temp_file = None
        self._mgmt_port = None
        self.samsung_client = None
        self.samsung_ip = None
        self.samsung_port = None
        self.samsung_info = None
        self.samsung_protocol = None
        self._samsung_lock = threading.Lock()
        self._sam_logs = []
        self._sam_log_lock = threading.Lock()
        self._samsung_auth_stop = threading.Event()
        self._logger = self._setup_logger()

    def _data_dir(self):
        base = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), '.config')
        return os.path.join(base, 'NEON')

    def _profile_store_path(self):
        return os.path.join(self._data_dir(), 'profiles.json')

    def _settings_path(self):
        return os.path.join(self._data_dir(), 'settings.json')

    def _log_path(self):
        return os.path.join(self._data_dir(), 'neon.log')

    def _setup_logger(self):
        logger = logging.getLogger('neon')
        if logger.handlers:
            return logger
        logger.setLevel(logging.INFO)
        try:
            log_path = self._log_path()
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            handler = RotatingFileHandler(log_path, maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
            logger.addHandler(handler)
        except Exception:
            logger.addHandler(logging.NullHandler())
        return logger

    def load_settings(self):
        defaults = {
            'tv_ip': '192.168.1.100',
            'webos26': False,
            'region_code': '22282',
            'last_profile': '',
            'hotspot_ssid': 'NEON-NET',
            'ethernet_adapter': '',
            'dns_mode': 'auto',
            'dns_primary': '',
            'dns_secondary': '',
            'dns_apply_uplink': False,
            'language': 'ru',
            'last_tv_area_backup': '',
            'minimize_to_tray': True,
            'samsung_ip': '192.168.1.101',
            'samsung_port': 'auto',
            'samsung_tokens': {},
        }
        try:
            with open(self._settings_path(), 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                defaults.update(loaded)
        except Exception:
            pass
        return defaults

    def save_settings(self, updates):
        if not isinstance(updates, dict):
            return self.load_settings()
        current = self.load_settings()
        # Never persist secrets in settings.json.
        forbidden = {'passphrase', 'password', 'tv_pass', 'vpn_pass', 'hotspot_pass'}
        for k, v in updates.items():
            if k not in forbidden:
                current[k] = v
        try:
            path = self._settings_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(current, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            self._logger.warning('Failed to save settings: %s', exc)
        return current

    def windows_info(self):
        is_win = platform.system() == 'Windows'
        if not is_win:
            return {
                'is_windows': False,
                'major': 0,
                'minor': 0,
                'build': 0,
                'label': platform.system(),
                'supports_winrt_hotspot': False,
            }
        try:
            v = sys.getwindowsversion()
            major, minor, build = int(v.major), int(v.minor), int(v.build)
        except Exception:
            major, minor, build = 6, 3, 0
        if major == 6 and minor == 3:
            label = 'Windows 8.1'
        elif major == 6 and minor == 2:
            label = 'Windows 8'
        elif major == 6 and minor == 1:
            label = 'Windows 7'
        elif major >= 10 and build >= 22000:
            label = 'Windows 11'
        elif major >= 10:
            label = 'Windows 10'
        else:
            label = f'Windows {major}.{minor}'
        return {
            'is_windows': True,
            'major': major,
            'minor': minor,
            'build': build,
            'label': label,
            'supports_winrt_hotspot': major >= 10 and build >= 14393,
        }

    def is_admin(self):
        if platform.system() != 'Windows':
            return False
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _creation_flags(self):
        return getattr(subprocess, 'CREATE_NO_WINDOW', 0)

    def _decode_oem(self, data):
        if isinstance(data, str):
            return data
        if not data:
            return ''
        encodings = []
        if platform.system() == 'Windows':
            try:
                cp = ctypes.windll.kernel32.GetOEMCP()
                if cp:
                    encodings.append(f'cp{cp}')
            except Exception:
                pass
            encodings.extend(['oem', 'utf-8', 'cp866', 'cp1251'])
        else:
            encodings.extend(['utf-8', 'latin-1'])
        for enc in encodings:
            try:
                return data.decode(enc)
            except Exception:
                continue
        return data.decode('utf-8', errors='replace')

    def _run_netsh(self, *args, timeout=15):
        proc = subprocess.run(
            ['netsh.exe'] + list(args),
            capture_output=True,
            timeout=timeout,
            creationflags=self._creation_flags(),
        )
        return (
            proc.returncode,
            self._decode_oem(proc.stdout).strip(),
            self._decode_oem(proc.stderr).strip(),
        )

    def _normalize_ip(self, raw_ip):
        ip = str(raw_ip or '').strip()
        ip = re.sub(r'^https?://', '', ip, flags=re.IGNORECASE)
        ip = ip.split('/')[0].strip()
        if ':' in ip and ip.count(':') == 1:
            host, port = ip.split(':', 1)
            if port.isdigit():
                ip = host.strip()
        if not ip or any(ch.isspace() for ch in ip):
            return ''
        if not re.fullmatch(r'[A-Za-z0-9._-]+', ip):
            return ''
        return ip

    def _load_tv_private_key(self, keydata, passphrase):
        clean_key = (keydata or '').replace('\r\n', '\n').strip() + '\n'
        try:
            return paramiko.RSAKey.from_private_key(io.StringIO(clean_key), password=passphrase)
        except paramiko.PasswordRequiredException:
            raise
        except paramiko.SSHException as rsa_exc:
            # Only probe alternate key types when the PEM/OpenSSH header isn't strictly legacy RSA
            # and the error wasn't an incorrect passphrase.
            msg = str(rsa_exc).lower()
            if 'BEGIN OPENSSH PRIVATE KEY' in clean_key and 'password' not in msg and 'decrypt' not in msg:
                for key_cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.DSSKey):
                    try:
                        return key_cls.from_private_key(io.StringIO(clean_key), password=passphrase)
                    except paramiko.PasswordRequiredException:
                        raise
                    except paramiko.SSHException:
                        continue
            raise rsa_exc

    def _connect_tv_ssh(self, clean_ip, key, output):
        profiles = [
            ('default', None),
            ('rsa-sha2-256', {'pubkeys': ['rsa-sha2-512']}),
            ('legacy ssh-rsa', {'pubkeys': ['rsa-sha2-512', 'rsa-sha2-256']}),
        ]
        last_auth_exc = None
        remote_ver = ''
        for idx, (label, disabled) in enumerate(profiles):
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kwargs = {
                'port': 9922,
                'username': 'prisoner',
                'pkey': key,
                'look_for_keys': False,
                'allow_agent': False,
                'timeout': 10,
                'banner_timeout': 10,
                'auth_timeout': 10,
            }
            if disabled is not None:
                kwargs['disabled_algorithms'] = disabled
            try:
                client.connect(clean_ip, **kwargs)
                tr = client.get_transport() if hasattr(client, 'get_transport') else None
                ver = getattr(tr, 'remote_version', '') or ''
                if idx > 0:
                    output.append(f'[+] Authenticated using {label} compatibility mode.')
                return client, ver
            except (paramiko.AuthenticationException, paramiko.SSHException, EOFError) as exc:
                tr = client.get_transport() if hasattr(client, 'get_transport') else None
                ver_now = getattr(tr, 'remote_version', '') if tr is not None else ''
                if ver_now:
                    remote_ver = ver_now
                is_auth_err = isinstance(exc, paramiko.AuthenticationException)
                if not is_auth_err and not ver_now:
                    try:
                        client.close()
                    except Exception:
                        pass
                    raise
                last_auth_exc = exc
                try:
                    client.close()
                except Exception:
                    pass
                if idx == 0:
                    output.append(
                        f'[i] TV rejected default RSA signature ({remote_ver or "SSH"}); '
                        'retrying rsa-sha2-256 / legacy ssh-rsa…'
                    )
                continue
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass
                raise

        ver_note = f' ({remote_ver})' if remote_ver else ''
        raise RuntimeError(
            f'SSH authentication failed on {clean_ip}:9922{ver_note} [{last_auth_exc}]. '
            'Tried rsa-sha2 and legacy ssh-rsa for prisoner. '
            'On the TV: verify Developer Mode is ON, toggle Key Server OFF -> ON (or reboot TV), '
            'and confirm no VPN/subnet conflict is routing to another host.'
        )

    def connect(self, ip, passphrase, webos26=False):
        clean_ip = self._normalize_ip(ip)
        if not clean_ip or not passphrase:
            return {'ok': False, 'error': 'Valid TV IP address and Developer Mode passphrase are required.'}
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        try:
            output = [f'[+] Requesting Developer Mode key from {clean_ip}:9991 …']
            try:
                with urllib.request.urlopen(f'http://{clean_ip}:9991/webos_rsa', timeout=8) as resp:
                    keydata = resp.read().decode('utf-8', errors='replace')
            except (urllib.error.HTTPError, urllib.error.URLError):
                try:
                    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with direct.open(f'http://{clean_ip}:9991/webos_rsa', timeout=8) as resp:
                        keydata = resp.read().decode('utf-8', errors='replace')
                except urllib.error.HTTPError as exc:
                    raise RuntimeError(f'TV returned HTTP {exc.code}. Ensure Key Server is turned ON in the Developer Mode app.')
                except urllib.error.URLError as exc:
                    raise RuntimeError(f'Cannot reach {clean_ip}:9991 ({exc.reason}). Verify TV IP, same LAN, and Key Server ON.')

            if not keydata.strip() or '<html' in keydata.lower():
                raise RuntimeError('TV :9991/webos_rsa did not return a private key. Toggle Key Server OFF -> ON on the TV.')

            try:
                key = self._load_tv_private_key(keydata, passphrase)
            except paramiko.PasswordRequiredException:
                raise RuntimeError('Developer Mode key requires a passphrase.')
            except paramiko.SSHException as exc:
                raise RuntimeError(f'Invalid Developer Mode passphrase or key ({exc}). Passphrase is case-sensitive.')

            client, remote_ver = self._connect_tv_ssh(clean_ip, key, output)
            self.client = client
            self.remote_script = '/media/developer/temp/change_region.sh' if webos26 else '/tmp/change_region.sh'
            ver_tag = f' ({remote_ver})' if remote_ver else ''
            output.append(f'[+] SSH session established on port 9922{ver_tag}.')

            # Always strip CRLF before SFTP upload so BusyBox /bin/sh on webOS never sees \r
            with open(resource('change_region.sh'), 'rb') as sf:
                lf_script = sf.read().replace(b'\r\n', b'\n').replace(b'\r', b'\n')
            fd, tmp_script = tempfile.mkstemp(prefix='neon_region_', suffix='.sh')
            try:
                os.write(fd, lf_script)
                os.close(fd)
                fd = None
                sftp = client.open_sftp()
                try:
                    sftp.put(tmp_script, self.remote_script)
                finally:
                    sftp.close()
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    os.unlink(tmp_script)
                except OSError:
                    pass
            chmod_status = self._exec('chmod 755 ' + shlex.quote(self.remote_script), output)
            if chmod_status != 0:
                raise RuntimeError('Uploaded script, but chmod 755 failed. Try enabling webOS 26 developer temp path.')
            output.append('[+] Upstream script uploaded to ' + self.remote_script)
            output.append('[+] Connected. Use Read Current before making changes.')
            self.save_settings({'tv_ip': clean_ip, 'webos26': bool(webos26)})
            self._logger.info('Connected to TV %s (webos26=%s)', clean_ip, bool(webos26))
            return {
                'ok': True,
                'ip': clean_ip,
                'remote_script': self.remote_script,
                'output': '\n'.join(output),
            }
        except Exception as exc:
            if self.client:
                try:
                    self.client.close()
                except Exception:
                    pass
                self.client = None
            self._logger.warning('TV connect failed for %s: %s', clean_ip, exc)
            return {'ok': False, 'error': str(exc), 'output': '[!] Connection failed: ' + str(exc)}

    def disconnect_tv(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        return {'ok': True, 'disconnected': True, 'output': '[+] Disconnected from TV SSH session.'}

    def _exec(self, command, lines):
        _stdin, stdout, stderr = self.client.exec_command(command, timeout=30)
        out = stdout.read().decode(errors='replace').strip()
        err = stderr.read().decode(errors='replace').strip()
        if out:
            lines.extend(out.splitlines())
        if err:
            lines.extend('[!] ' + line for line in err.splitlines())
        return stdout.channel.recv_exit_status()

    def _parse_area_from_lines(self, lines):
        for line in lines:
            m = re.search(r'Current area option:\s*(\d+)', line)
            if m:
                return m.group(1)
        return None

    def action(self, action):
        if not self.client:
            return {'ok': False, 'disconnected': True, 'error': 'Not connected to a TV.', 'output': '[!] Not connected.'}
        transport = self.client.get_transport() if hasattr(self.client, 'get_transport') else None
        if transport is not None and not transport.is_active():
            self.disconnect_tv()
            return {'ok': False, 'disconnected': True, 'error': 'TV SSH session expired or dropped. Reconnect first.', 'output': '[!] SSH session closed.'}

        raw_action = str(action or '').strip()
        is_area_code = bool(re.fullmatch(r'[0-9]{1,10}', raw_action)) and (1 <= int(raw_action) <= 0xFFFFFFFF)
        if is_area_code:
            action = str(int(raw_action))
        elif raw_action in {'read', 'verify', 'reboot'}:
            action = raw_action
        else:
            return {
                'ok': False,
                'error': 'Unsupported operation or invalid Area Option code (expected 1-4294967295).',
                'output': '[!] Unsupported operation.',
            }

        lines = []
        try:
            exit_code = self._exec('sh ' + shlex.quote(self.remote_script) + ' ' + shlex.quote(action), lines)
            out_text = '\n'.join(lines) if lines else '[+] Command finished (no output).'
            if exit_code != 0:
                self._logger.warning('TV action %s exited with %s', action, exit_code)
                return {
                    'ok': False,
                    'error': f'TV script exited with status {exit_code}.',
                    'output': out_text,
                }
            res = {'ok': True, 'output': out_text}
            if action == 'read':
                area = self._parse_area_from_lines(lines)
                if area:
                    res['current_area'] = area
                    self.save_settings({'last_tv_area_backup': area})
            elif is_area_code:
                self.save_settings({'region_code': action})
            elif action == 'reboot':
                self.disconnect_tv()
                res['disconnected'] = True
            self._logger.info('TV action %s succeeded', action)
            return res
        except (paramiko.SSHException, EOFError, OSError, socket.error) as exc:
            self.disconnect_tv()
            self._logger.warning('TV action %s lost SSH connection: %s', action, exc)
            return {
                'ok': False,
                'disconnected': True,
                'error': f'SSH connection lost: {exc}',
                'output': '[!] SSH connection lost: ' + str(exc),
            }
        except Exception as exc:
            self._logger.warning('TV action %s failed: %s', action, exc)
            return {'ok': False, 'error': str(exc), 'output': '[!] ' + str(exc)}

    def _ar_member(self, name, data, mtime):
        hdr = (
            f'{name:<16}'
            f'{int(mtime):<12}'
            f'{"0":<6}'
            f'{"0":<6}'
            f'{"100644":<8}'
            f'{len(data):<10}'
            '`\n'
        ).encode('ascii')
        out = hdr + data
        if len(data) % 2:
            out += b'\n'
        return out

    def build_belet_ipk(self, target_url='https://belet.tm/'):
        app_id = 'tm.belet.webos'
        version = '1.0.2'
        mtime = int(time.time())

        appinfo = json.dumps({
            'id': app_id,
            'version': version,
            'vendor': 'Belet',
            'type': 'web',
            'main': 'index.html',
            'title': 'Belet',
            'icon': 'icon.png',
            'largeIcon': 'largeIcon.png',
            'splashBackground': 'splash.png',
            'bgColor': '#0d1112',
            'iconColor': '#0d1112',
            'resolution': '1920x1080',
            'disableBackHistoryAPI': False,
            # External redirects cannot call PalmSystem.activate() on webOSRelaunch;
            # keeping handlesRelaunch False lets webOS bring the app back to foreground after Home.
            'handlesRelaunch': False,
            # webOS WAM defaults disallowScrollingInMainFrame to true and useNativeScroll to false,
            # which disables main-frame scrolling and Magic Remote wheel scrolling on hosted sites.
            'disallowScrollingInMainFrame': False,
            'useNativeScroll': True,
            'transparent': False,
            'uiRevision': 2,
        }, indent=2).encode('utf-8') + b'\n'

        safe_url = json.dumps(str(target_url or 'https://belet.tm/'))
        index_html = (
            '<!DOCTYPE html>\n'
            '<html lang="ru">\n'
            '<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">\n'
            '<title>Belet</title>\n'
            '<style>\n'
            'html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;'
            'background:radial-gradient(circle at 50% 42%,#0e233b 0%,#070b12 72%);'
            'color:#eaf4ff;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;'
            'display:flex;align-items:center;justify-content:center;flex-direction:column;}\n'
            '.card{text-align:center;padding:36px 48px;border-radius:24px;'
            'background:rgba(13,17,22,0.72);border:1px solid rgba(44,232,255,0.22);}\n'
            '.logo{width:112px;height:112px;border-radius:22px;margin-bottom:18px;}\n'
            '.spin{width:38px;height:38px;border-radius:50%;margin:16px auto 0;'
            'border:3px solid rgba(44,232,255,0.22);border-top-color:#2ce8ff;'
            'animation:sp 0.9s linear infinite;}\n'
            '@keyframes sp{to{transform:rotate(360deg);}}\n'
            '#retryBox{display:none;margin-top:24px;}\n'
            'button{background:linear-gradient(135deg,#2ce8ff,#1484ff);color:#04101c;border:0;'
            'border-radius:12px;padding:14px 32px;font-size:20px;font-weight:700;cursor:pointer;}\n'
            'button:focus{outline:3px solid #ffffff;}\n'
            '.hint{margin-top:12px;font-size:16px;color:#93aec9;}\n'
            '</style>\n'
            '</head>\n'
            '<body>\n'
            '<div class="card">\n'
            '<img class="logo" src="largeIcon.png" alt="Belet">\n'
            '<div style="font-size:28px;font-weight:700;letter-spacing:0.5px;">Belet</div>\n'
            '<div class="spin"></div>\n'
            '<div id="retryBox">\n'
            '<div class="hint">Unable to reach belet.tm. Check TV network / Conf and press OK.</div><br>\n'
            '<button id="retryBtn" type="button">Retry / Повторить</button>\n'
            '</div>\n'
            '</div>\n'
            '<script>\n'
            '(function(){\n'
            f'var TARGET={safe_url};\n'
            'var retryBox=document.getElementById("retryBox");\n'
            'var retryBtn=document.getElementById("retryBtn");\n'
            'function go(){\n'
            '  if(retryBox)retryBox.style.display="none";\n'
            '  try{window.location.replace(TARGET);}catch(e){window.location.href=TARGET;}\n'
            '}\n'
            'setTimeout(function(){\n'
            '  if(retryBox)retryBox.style.display="block";\n'
            '  if(retryBtn&&retryBtn.focus)retryBtn.focus();\n'
            '},8000);\n'
            'document.addEventListener("keydown",function(ev){\n'
            '  if(ev.keyCode===13||ev.key==="Enter"){go();}\n'
            '});\n'
            'if(retryBtn)retryBtn.onclick=go;\n'
            'setTimeout(go,120);\n'
            '})();\n'
            '</script>\n'
            '</body>\n'
            '</html>\n'
        ).encode('utf-8')

        with open(resource('assets/belet_icon.png'), 'rb') as f:
            icon_bytes = f.read()
        with open(resource('assets/belet_large_icon.png'), 'rb') as f:
            large_icon_bytes = f.read()
        with open(resource('assets/belet_splash.png'), 'rb') as f:
            splash_bytes = f.read()

        app_files = [
            ('appinfo.json', appinfo),
            ('index.html', index_html),
            ('icon.png', icon_bytes),
            ('largeIcon.png', large_icon_bytes),
            ('splash.png', splash_bytes),
        ]

        data_buf = io.BytesIO()
        base_rel = f'usr/palm/applications/{app_id}'
        with tarfile.open(fileobj=data_buf, mode='w:gz', format=tarfile.USTAR_FORMAT) as tf:
            for d in ('usr', 'usr/palm', 'usr/palm/applications', base_rel):
                di = tarfile.TarInfo(d)
                di.type = tarfile.DIRTYPE
                di.mode = 0o755
                di.uid = 0
                di.gid = 0
                di.uname = 'root'
                di.gname = 'root'
                di.mtime = mtime
                tf.addfile(di)
            for fname, payload in app_files:
                fi = tarfile.TarInfo(f'{base_rel}/{fname}')
                fi.type = tarfile.REGTYPE
                fi.mode = 0o644
                fi.size = len(payload)
                fi.uid = 0
                fi.gid = 0
                fi.uname = 'root'
                fi.gname = 'root'
                fi.mtime = mtime
                tf.addfile(fi, io.BytesIO(payload))
        data_targz = data_buf.getvalue()

        installed_kb = max(1, (sum(len(p) for _, p in app_files) + 1023) // 1024)
        control_txt = (
            f'Package: {app_id}\n'
            f'Version: {version}\n'
            'Section: misc\n'
            'Priority: optional\n'
            'Architecture: all\n'
            f'Installed-Size: {installed_kb}\n'
            'Maintainer: Belet WebOS <help@belet.tm>\n'
            'Description: Belet Film Fullscreen Web App\n'
            'webOS-Package-Format-Version: 2\n'
            'webOS-Packager-Version: 2.0.0\n'
        ).encode('utf-8')

        ctrl_buf = io.BytesIO()
        with tarfile.open(fileobj=ctrl_buf, mode='w:gz', format=tarfile.USTAR_FORMAT) as tf:
            ci = tarfile.TarInfo('control')
            ci.type = tarfile.REGTYPE
            ci.mode = 0o644
            ci.size = len(control_txt)
            ci.uid = 0
            ci.gid = 0
            ci.uname = 'root'
            ci.gname = 'root'
            ci.mtime = mtime
            tf.addfile(ci, io.BytesIO(control_txt))
        ctrl_targz = ctrl_buf.getvalue()

        ipk_buf = io.BytesIO()
        ipk_buf.write(b'!<arch>\n')
        ipk_buf.write(self._ar_member('debian-binary', b'2.0\n', mtime))
        ipk_buf.write(self._ar_member('control.tar.gz', ctrl_targz, mtime))
        ipk_buf.write(self._ar_member('data.tar.gz', data_targz, mtime))
        return ipk_buf.getvalue()

    def _tv_temp_dir(self):
        return '/media/developer/temp' if '/media/developer/temp/' in str(self.remote_script) else '/tmp'

    def install_belet_app(self):
        if not self.client:
            return {'ok': False, 'disconnected': True, 'error': 'Not connected to a TV.', 'output': '[!] Not connected.'}
        transport = self.client.get_transport() if hasattr(self.client, 'get_transport') else None
        if transport is not None and not transport.is_active():
            self.disconnect_tv()
            return {'ok': False, 'disconnected': True, 'error': 'TV SSH session closed. Reconnect first.', 'output': '[!] SSH session closed.'}

        lines = ['[+] Packaging Belet webOS fullscreen app (tm.belet.webos)…']
        try:
            ipk_bytes = self.build_belet_ipk()
            remote_dir = self._tv_temp_dir()
            remote_ipk = f'{remote_dir}/tm.belet.webos.ipk'
            remote_fifo = f'{remote_dir}/belet_luna_fifo'

            fd, local_ipk = tempfile.mkstemp(prefix='belet_', suffix='.ipk')
            try:
                os.write(fd, ipk_bytes)
                os.close(fd)
                fd = None
                sftp = self.client.open_sftp()
                try:
                    sftp.put(local_ipk, remote_ipk)
                finally:
                    sftp.close()
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    os.unlink(local_ipk)
                except OSError:
                    pass

            lines.append(f'[+] Uploaded IPK ({len(ipk_bytes) // 1024} KB) to {remote_ipk}')
            payload = json.dumps({'id': 'com.ares.defaultName', 'ipkUrl': remote_ipk, 'subscribe': True})
            install_cmd = (
                'LUNA=$(command -v luna-send-pub || command -v luna-send); '
                '[ -n "$LUNA" ] || { echo "[!] luna-send-pub not found on TV"; exit 1; }; '
                f'FIFO={shlex.quote(remote_fifo)}; IPK={shlex.quote(remote_ipk)}; '
                'rm -f "$FIFO"; mkfifo "$FIFO" || exit 1; '
                f'"$LUNA" -w 35000 -i \'luna://com.webos.appInstallService/dev/install\' {shlex.quote(payload)} >"$FIFO" 2>&1 & '
                'LPID=$!; STATUS=1; '
                'while IFS= read -r line; do '
                '  echo "$line"; '
                '  case "$line" in '
                '    *\'"state":"installed"\'*|*\'"state": "installed"\'*) STATUS=0; break;; '
                '    *\'"returnValue":false\'*|*\'"returnValue": false\'*|*\'"errorText"\'*|*\'install failed\'*|*\'FAILED\'*) STATUS=1; break;; '
                '  esac; '
                'done < "$FIFO"; '
                'kill "$LPID" 2>/dev/null || true; '
                'rm -f "$FIFO" "$IPK"; '
                'exit $STATUS'
            )
            exit_code = self._exec(install_cmd, lines)
            if exit_code != 0:
                return {
                    'ok': False,
                    'error': f'webOS app install failed (status {exit_code}). Check console output.',
                    'output': '\n'.join(lines),
                }
            lines.append('[+] Belet installed cleanly! Find it on the TV Home launcher or click Launch.')
            self._logger.info('Installed Belet IPK on TV')
            return {'ok': True, 'output': '\n'.join(lines)}
        except (paramiko.SSHException, EOFError, OSError, socket.error) as exc:
            self.disconnect_tv()
            return {'ok': False, 'disconnected': True, 'error': f'SSH connection lost: {exc}', 'output': '\n'.join(lines + ['[!] ' + str(exc)])}
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'output': '\n'.join(lines + ['[!] ' + str(exc)])}

    def launch_belet_app(self):
        if not self.client:
            return {'ok': False, 'disconnected': True, 'error': 'Not connected to a TV.', 'output': '[!] Not connected.'}
        lines = []
        try:
            cmd = (
                'LUNA=$(command -v luna-send-pub || command -v luna-send); '
                '[ -n "$LUNA" ] || { echo "[!] luna-send-pub not found on TV"; exit 1; }; '
                '"$LUNA" -n 1 -f \'luna://com.webos.applicationManager/launch\' \'{"id":"tm.belet.webos"}\''
            )
            rc = self._exec(cmd, lines)
            out = '\n'.join(lines) if lines else '[+] Launch command sent.'
            if rc != 0:
                return {'ok': False, 'error': f'Launch failed (status {rc}).', 'output': out}
            return {'ok': True, 'output': out}
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'output': '[!] ' + str(exc)}

    def remove_belet_app(self):
        if not self.client:
            return {'ok': False, 'disconnected': True, 'error': 'Not connected to a TV.', 'output': '[!] Not connected.'}
        lines = ['[+] Removing Belet app (tm.belet.webos)…']
        try:
            remote_fifo = f'{self._tv_temp_dir()}/belet_rm_fifo'
            cmd = (
                'LUNA=$(command -v luna-send-pub || command -v luna-send); '
                '[ -n "$LUNA" ] || { echo "[!] luna-send-pub not found on TV"; exit 1; }; '
                f'FIFO={shlex.quote(remote_fifo)}; rm -f "$FIFO"; mkfifo "$FIFO" || exit 1; '
                '"$LUNA" -w 15000 -i \'luna://com.webos.appInstallService/dev/remove\' \'{"id":"tm.belet.webos","subscribe":true}\' >"$FIFO" 2>&1 & '
                'LPID=$!; STATUS=1; '
                'while IFS= read -r line; do '
                '  echo "$line"; '
                '  case "$line" in '
                '    *\'removed\'*|*\'uninstalled\'*) STATUS=0; break;; '
                '    *\'"returnValue":false\'*|*\'"returnValue": false\'*|*\'"errorText"\'*) STATUS=1; break;; '
                '  esac; '
                'done < "$FIFO"; '
                'kill "$LPID" 2>/dev/null || true; rm -f "$FIFO"; exit $STATUS'
            )
            rc = self._exec(cmd, lines)
            if rc != 0:
                return {'ok': False, 'error': f'Remove failed (status {rc}).', 'output': '\n'.join(lines)}
            lines.append('[+] Belet removed from TV.')
            return {'ok': True, 'output': '\n'.join(lines)}
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'output': '\n'.join(lines + ['[!] ' + str(exc)])}

    def _normalize_portal_url(self, url):
        u = (url or ALEMTV_DEFAULT_PORTAL).strip()
        if not re.match(r'^https?://', u, re.I):
            u = 'http://' + u
        return u.rstrip('/')

    def build_alemtv_ipk(self, target_url=ALEMTV_DEFAULT_PORTAL):
        clean_url = self._normalize_portal_url(target_url)
        icon_bytes = None
        large_icon_bytes = None
        splash_bytes = None
        for path_key, var_name in (
            ('assets/alemtv_icon.png', 'icon'),
            ('assets/alemtv_large_icon.png', 'large'),
            ('assets/alemtv_splash.png', 'splash'),
        ):
            p = resource(path_key)
            if os.path.isfile(p):
                with open(p, 'rb') as f:
                    data = f.read()
                    if var_name == 'icon':
                        icon_bytes = data
                    elif var_name == 'large':
                        large_icon_bytes = data
                    elif var_name == 'splash':
                        splash_bytes = data
        return build_alemtv_ipk(
            target_url=clean_url,
            icon_bytes=icon_bytes,
            large_icon_bytes=large_icon_bytes,
            splash_bytes=splash_bytes,
        )

    def install_alemtv_app(self, target_url=ALEMTV_DEFAULT_PORTAL):
        clean_url = self._normalize_portal_url(target_url)
        if not self.client:
            return {'ok': False, 'disconnected': True, 'error': 'Not connected to a TV.', 'output': '[!] Not connected.'}
        transport = self.client.get_transport() if hasattr(self.client, 'get_transport') else None
        if transport is not None and not transport.is_active():
            self.disconnect_tv()
            return {'ok': False, 'disconnected': True, 'error': 'TV SSH session closed. Reconnect first.', 'output': '[!] SSH session closed.'}

        lines = [f'[+] Packaging Alem TV webOS fullscreen app ({ALEMTV_WEBOS_ID})…']
        try:
            ipk_bytes = self.build_alemtv_ipk(target_url=clean_url)
            remote_dir = self._tv_temp_dir()
            remote_ipk = f'{remote_dir}/{ALEMTV_WEBOS_ID}.ipk'
            remote_fifo = f'{remote_dir}/alemtv_luna_fifo'

            fd, local_ipk = tempfile.mkstemp(prefix='alemtv_', suffix='.ipk')
            try:
                os.write(fd, ipk_bytes)
                os.close(fd)
                fd = None
                sftp = self.client.open_sftp()
                try:
                    sftp.put(local_ipk, remote_ipk)
                finally:
                    sftp.close()
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    os.unlink(local_ipk)
                except OSError:
                    pass

            lines.append(f'[+] Uploaded IPK ({len(ipk_bytes) // 1024} KB) to {remote_ipk}')
            payload = json.dumps({'id': 'com.ares.defaultName', 'ipkUrl': remote_ipk, 'subscribe': True})
            install_cmd = (
                'LUNA=$(command -v luna-send-pub || command -v luna-send); '
                '[ -n "$LUNA" ] || { echo "[!] luna-send-pub not found on TV"; exit 1; }; '
                f'FIFO={shlex.quote(remote_fifo)}; IPK={shlex.quote(remote_ipk)}; '
                'rm -f "$FIFO"; mkfifo "$FIFO" || exit 1; '
                f'"$LUNA" -w 35000 -i \'luna://com.webos.appInstallService/dev/install\' {shlex.quote(payload)} >"$FIFO" 2>&1 & '
                'LPID=$!; STATUS=1; '
                'while IFS= read -r line; do '
                '  echo "$line"; '
                '  case "$line" in '
                '    *\'"state":"installed"\'*|*\'"state": "installed"\'*) STATUS=0; break;; '
                '    *\'"returnValue":false\'*|*\'"returnValue": false\'*|*\'"errorText"\'*|*\'install failed\'*|*\'FAILED\'*) STATUS=1; break;; '
                '  esac; '
                'done < "$FIFO"; '
                'kill "$LPID" 2>/dev/null || true; '
                'rm -f "$FIFO" "$IPK"; '
                'exit $STATUS'
            )
            exit_code = self._exec(install_cmd, lines)
            if exit_code != 0:
                return {
                    'ok': False,
                    'error': f'webOS app install failed (status {exit_code}). Check console output.',
                    'output': '\n'.join(lines),
                }
            lines.append('[+] Alem TV installed cleanly! Find it on the TV Home launcher or click Launch.')
            self._logger.info('Installed Alem TV IPK on TV')
            return {'ok': True, 'output': '\n'.join(lines)}
        except (paramiko.SSHException, EOFError, OSError, socket.error) as exc:
            self.disconnect_tv()
            return {'ok': False, 'disconnected': True, 'error': f'SSH connection lost: {exc}', 'output': '\n'.join(lines + ['[!] ' + str(exc)])}
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'output': '\n'.join(lines + ['[!] ' + str(exc)])}

    def launch_alemtv_app(self):
        if not self.client:
            return {'ok': False, 'disconnected': True, 'error': 'Not connected to a TV.', 'output': '[!] Not connected.'}
        lines = []
        try:
            cmd = (
                'LUNA=$(command -v luna-send-pub || command -v luna-send); '
                '[ -n "$LUNA" ] || { echo "[!] luna-send-pub not found on TV"; exit 1; }; '
                f'"$LUNA" -n 1 -f \'luna://com.webos.applicationManager/launch\' \'{{"id":"{ALEMTV_WEBOS_ID}"}}\''
            )
            rc = self._exec(cmd, lines)
            out = '\n'.join(lines) if lines else '[+] Launch command sent.'
            if rc != 0:
                return {'ok': False, 'error': f'Launch failed (status {rc}).', 'output': out}
            return {'ok': True, 'output': out}
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'output': '[!] ' + str(exc)}

    def remove_alemtv_app(self):
        if not self.client:
            return {'ok': False, 'disconnected': True, 'error': 'Not connected to a TV.', 'output': '[!] Not connected.'}
        lines = [f'[+] Removing Alem TV app ({ALEMTV_WEBOS_ID})…']
        try:
            remote_fifo = f'{self._tv_temp_dir()}/alemtv_rm_fifo'
            cmd = (
                'LUNA=$(command -v luna-send-pub || command -v luna-send); '
                '[ -n "$LUNA" ] || { echo "[!] luna-send-pub not found on TV"; exit 1; }; '
                f'FIFO={shlex.quote(remote_fifo)}; rm -f "$FIFO"; mkfifo "$FIFO" || exit 1; '
                f'"$LUNA" -w 15000 -i \'luna://com.webos.appInstallService/dev/remove\' \'{{"id":"{ALEMTV_WEBOS_ID}","subscribe":true}}\' >"$FIFO" 2>&1 & '
                'LPID=$!; STATUS=1; '
                'while IFS= read -r line; do '
                '  echo "$line"; '
                '  case "$line" in '
                '    *\'removed\'*|*\'uninstalled\'*) STATUS=0; break;; '
                '    *\'"returnValue":false\'*|*\'"returnValue": false\'*|*\'"errorText"\'*) STATUS=1; break;; '
                '  esac; '
                'done < "$FIFO"; '
                'kill "$LPID" 2>/dev/null || true; rm -f "$FIFO"; exit $STATUS'
            )
            rc = self._exec(cmd, lines)
            if rc != 0:
                return {'ok': False, 'error': f'Remove failed (status {rc}).', 'output': '\n'.join(lines)}
            lines.append('[+] Alem TV removed from TV.')
            return {'ok': True, 'output': '\n'.join(lines)}
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'output': '\n'.join(lines + ['[!] ' + str(exc)])}

    def _powershell(self, script, timeout=25):
        script = (
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); "
            "$OutputEncoding = [Console]::OutputEncoding; "
            + script
        )
        encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
        proc = subprocess.run(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
            creationflags=self._creation_flags(),
        )
        if proc.returncode:
            output = proc.stdout.strip()
            if output.startswith('NEON_ERROR:'):
                raise RuntimeError(output.split(':', 1)[1].strip())
            raise RuntimeError((output or proc.stderr or 'PowerShell operation failed').strip())
        return proc.stdout.strip()

    def _load_saved_profile_map(self):
        try:
            with open(self._profile_store_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
        except Exception:
            pass
        return {}

    def _save_profile_map(self, mapping):
        try:
            store = self._profile_store_path()
            os.makedirs(os.path.dirname(store), exist_ok=True)
            with open(store, 'w', encoding='utf-8') as f:
                json.dump(mapping, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def remember_profile(self, path):
        path = os.path.abspath(str(path or ''))
        if not os.path.isfile(path) or not path.lower().endswith('.ovpn'):
            return {'error': 'Choose an existing .ovpn profile.'}
        if not self.profile_map:
            self.profile_map = {
                k: v for k, v in self._load_saved_profile_map().items() if os.path.isfile(v)
            }
        name = os.path.splitext(os.path.basename(path))[0]
        existing = self.profile_map.get(name)
        if existing and os.path.normcase(os.path.abspath(existing)) != os.path.normcase(path):
            parent = os.path.basename(os.path.dirname(path)) or 'custom'
            candidate = f'{name} ({parent})'
            idx = 2
            while candidate in self.profile_map and os.path.normcase(os.path.abspath(self.profile_map[candidate])) != os.path.normcase(path):
                candidate = f'{name} ({parent} #{idx})'
                idx += 1
            name = candidate
        self.profile_map[name] = path
        self._save_profile_map(self.profile_map)
        self.save_settings({'last_profile': name})
        return {'profile': name, 'path': path}

    def list_profiles(self):
        if platform.system() != 'Windows':
            return {'profiles': [], 'error': 'Conf & network bridge tools are available in the Windows build.'}
        roots = [
            os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'OpenVPN', 'config'),
            os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), 'OpenVPN', 'config'),
            os.path.join(os.environ.get('USERPROFILE', ''), 'OpenVPN', 'config'),
            os.path.join(os.environ.get('USERPROFILE', ''), 'Documents', 'OpenVPN', 'config'),
        ]
        paths = []
        for root in roots:
            if root:
                paths.extend(glob.glob(os.path.join(root, '*.ovpn')))
        unique = {os.path.normcase(os.path.abspath(p)): os.path.abspath(p) for p in paths}

        saved = self._load_saved_profile_map()
        valid_map = {}
        seen_paths = set()
        for name, p in saved.items():
            if os.path.isfile(p):
                norm = os.path.normcase(os.path.abspath(p))
                valid_map[name] = os.path.abspath(p)
                seen_paths.add(norm)

        for norm, p in unique.items():
            if norm in seen_paths:
                continue
            name = os.path.splitext(os.path.basename(p))[0]
            if name in valid_map:
                parent = os.path.basename(os.path.dirname(p)) or 'OpenVPN'
                name = f'{name} ({parent})'
            valid_map[name] = p
            seen_paths.add(norm)

        self.profile_map = valid_map
        self._save_profile_map(self.profile_map)
        return {'profiles': sorted(self.profile_map.keys(), key=str.casefold)}

    def browse_profile(self):
        if not self.window:
            return {'error': 'File picker is not ready.'}
        try:
            selected = self.window.create_file_dialog(
                self.open_dialog_type,
                allow_multiple=False,
                file_types=['Conf profile (*.ovpn)'],
            )
            if not selected:
                return {'cancelled': True}
            path = selected[0] if isinstance(selected, (list, tuple)) else selected
            return self.remember_profile(path)
        except Exception as exc:
            return {'error': str(exc)}

    def _openvpn_exe(self):
        roots = [
            os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'OpenVPN', 'bin', 'openvpn.exe'),
            os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), 'OpenVPN', 'bin', 'openvpn.exe'),
        ]
        return next((p for p in roots if os.path.isfile(p)), None) or shutil.which('openvpn.exe') or shutil.which('openvpn')

    def _resolve_profile_path(self, profile):
        if not profile:
            return None
        p = self.profile_map.get(profile)
        if not p or not os.path.isfile(p):
            self.list_profiles()
            p = self.profile_map.get(profile)
        if not p and os.path.isfile(str(profile)):
            p = os.path.abspath(str(profile))
        return p if p and os.path.isfile(p) else None

    def inspect_profile(self, profile):
        p = self._resolve_profile_path(profile)
        if not p:
            return {'exists': False, 'needs_auth': False, 'needs_askpass': False}
        needs_auth = False
        needs_askpass = False
        try:
            with open(p, 'r', encoding='utf-8', errors='replace') as config:
                for raw in config:
                    line = raw.strip()
                    if not line or line.startswith('#') or line.startswith(';'):
                        continue
                    # Strip inline comments
                    for marker in (' #', ' ;', '\t#', '\t;'):
                        if marker in line:
                            line = line.split(marker, 1)[0].strip()
                    words = line.split()
                    if not words:
                        continue
                    directive = words[0].lower()
                    if directive == 'auth-user-pass' and len(words) == 1:
                        needs_auth = True
                    elif directive == 'askpass' and len(words) == 1:
                        needs_askpass = True
        except Exception as exc:
            return {'exists': True, 'path': p, 'needs_auth': False, 'needs_askpass': False, 'error': str(exc)}
        return {'exists': True, 'path': p, 'needs_auth': needs_auth, 'needs_askpass': needs_askpass}

    def _cleanup_auth_file(self):
        path = self._auth_temp_file
        self._auth_temp_file = None
        if not path:
            return
        try:
            if os.path.isfile(path):
                size = max(os.path.getsize(path), 64)
                with open(path, 'wb') as f:
                    f.write(b'\x00' * size)
                os.remove(path)
        except Exception:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass

    def _find_free_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def connect_vpn(self, profile, username='', password=''):
        if platform.system() != 'Windows':
            return {'error': 'Headless Conf engine control is available in the Windows build.'}
        info = self.inspect_profile(profile)
        if not info.get('exists'):
            return {'error': 'Choose an existing .ovpn Conf profile first.'}
        p = info['path']
        engine = self._openvpn_exe()
        if not engine:
            return {
                'error': (
                    'Conf engine binary not found. Install the Conf runtime driver package once so NEON can run '
                    'profiles silently in the background.'
                )
            }
        if self.openvpn_process and self.openvpn_process.poll() is None:
            return {'error': 'A Conf session is already active in NEON. Disconnect it first.'}
        if info.get('needs_askpass'):
            return {'error': 'This Conf profile uses an interactive private-key passphrase (askpass), which is not supported.'}

        cmd = [engine, '--config', os.path.abspath(p), '--verb', '4']
        self._cleanup_auth_file()
        if info.get('needs_auth'):
            user = str(username or '').strip()
            pw = str(password or '')
            if not user or not pw:
                return {
                    'needs_auth': True,
                    'error': 'This Conf profile requires username and password (auth-user-pass).',
                }
            data_dir = self._data_dir()
            os.makedirs(data_dir, exist_ok=True)
            fd, auth_path = tempfile.mkstemp(prefix='neon_auth_', suffix='.txt', dir=data_dir)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(f'{user}\n{pw}\n')
            self._auth_temp_file = auth_path
            cmd.extend(['--auth-user-pass', auth_path, '--auth-nocache'])

        mgmt_port = None
        try:
            mgmt_port = self._find_free_port()
            cmd.extend(['--management', '127.0.0.1', str(mgmt_port)])
        except Exception:
            mgmt_port = None

        try:
            with self.vpn_lock:
                self._vpn_stop_requested = False
                self.vpn_profile = profile
                self.vpn_state = 'CONNECTING'
                self._mgmt_port = mgmt_port
                self.vpn_adapter_name = None
                self.vpn_adapter_guid = None
                self.vpn_ifindex = None
                self.vpn_logs = ['[NEON] Starting Conf engine in background: ' + profile]
            proc = subprocess.Popen(
                cmd,
                cwd=os.path.dirname(os.path.abspath(p)),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                creationflags=self._creation_flags(),
            )
            with self.vpn_lock:
                self.openvpn_process = proc
            self.save_settings({'last_profile': profile})
            self._logger.info('Started Conf profile %s', profile)
            threading.Thread(target=self._read_openvpn_log, args=(proc,), daemon=True).start()
            return {
                'ok': True,
                'output': 'Conf engine started inside NEON (background process; no third-party window). Watch the live log for “Initialization Sequence Completed”.',
            }
        except Exception as exc:
            self._cleanup_auth_file()
            with self.vpn_lock:
                self.vpn_state = 'DISCONNECTED'
                self.openvpn_process = None
                self._mgmt_port = None
            self._logger.warning('Failed to start Conf engine: %s', exc)
            return {'error': str(exc)}

    def _emergency_stop_sharing(self):
        try:
            if self.hotspot_active:
                res = self.stop_vpn_hotspot()
                self._logger.warning('Emergency hotspot teardown after Conf drop: %s', res)
            if self.ethernet_active:
                res = self.stop_vpn_ethernet()
                self._logger.warning('Emergency Ethernet sharing teardown after Conf drop: %s', res)
            elif self.app_shared_pair:
                res = self._disable_app_shared_pair()
                self._logger.warning('Emergency ICS teardown after Conf drop: %s', res)
        except Exception as exc:
            self._logger.warning('Emergency sharing teardown error: %s', exc)

    def _read_openvpn_log(self, process):
        fatal_markers = (
            'AUTH_FAILED',
            'Exiting due to fatal error',
            'Options error:',
            'All TAP-Windows adapters on this system are currently in use',
            'Cannot open TUN/TAP dev',
        )
        try:
            for raw in iter(process.stdout.readline, ''):
                line = raw.rstrip()
                if not line:
                    continue
                drop_auth = False
                with self.vpn_lock:
                    self.vpn_logs.append(line)
                    if len(self.vpn_logs) > 1500:
                        self.vpn_logs = self.vpn_logs[-1000:]
                    # Capture the exact virtual adapter opened by OpenVPN (TAP / Wintun / DCO)
                    m_tap = re.search(
                        r'(?:Open(?:ed)?\s+(?:TAP|Wintun|ovpn-dco(?:-win)?)\s+(?:device|adapter)|Using\s+(?:Wintun|ovpn-dco(?:-win)?)\s+(?:device|adapter))\s+"([^"]+)"',
                        line,
                        re.IGNORECASE,
                    )
                    if m_tap:
                        self.vpn_adapter_name = m_tap.group(1).strip()
                    m_win32 = re.search(r'TAP-WIN(?:32|64)\s+device\s+\[([^\]]+)\]\s+opened', line, re.IGNORECASE)
                    if m_win32:
                        self.vpn_adapter_name = m_win32.group(1).strip()
                    m_guid = re.search(r'PATH="[^"]*?(\{[0-9A-Fa-f-]{36}\})', line)
                    if m_guid:
                        self.vpn_adapter_guid = m_guid.group(1).upper()
                    m_list = re.search(r"TAP ADAPTERS:.*?guid='(\{[0-9A-Fa-f-]{36}\})'\s+index=(\d+)\s+name='([^']+)'", line, re.IGNORECASE)
                    if m_list:
                        self.vpn_adapter_guid = m_list.group(1).upper()
                        self.vpn_ifindex = int(m_list.group(2))
                        self.vpn_adapter_name = m_list.group(3).strip()
                    m_idx = re.search(r'(?:interface|iface_index)\s*=\s*(\d+)', line, re.IGNORECASE)
                    if m_idx and ('netsh' in line.lower() or 'iphelper' in line.lower() or 'metric' in line.lower()):
                        self.vpn_ifindex = int(m_idx.group(1))
                    if 'Initialization Sequence Completed' in line:
                        self.vpn_state = 'CONNECTED'
                        drop_auth = True
                        if platform.system() == 'Windows':
                            try:
                                cfg = self._get_dns_config_from_settings()
                                if cfg.get('servers'):
                                    threading.Thread(
                                        target=self._apply_dns_after_vpn_connect,
                                        daemon=True,
                                    ).start()
                            except Exception:
                                pass
                    elif any(marker in line for marker in fatal_markers):
                        self.vpn_state = 'ERROR'
                        drop_auth = True
                        if 'AUTH_FAILED' in line:
                            self.vpn_logs.append('[!] Conf authentication failed. Check profile username/password.')
                if drop_auth:
                    self._cleanup_auth_file()
        except Exception as exc:
            with self.vpn_lock:
                self.vpn_logs.append('[NEON] Log reader: ' + str(exc))
        finally:
            try:
                if process.stdout:
                    process.stdout.close()
            except Exception:
                pass
            self._cleanup_auth_file()
            rc = process.poll()
            need_teardown = False
            with self.vpn_lock:
                is_current = self.openvpn_process is process
                stop_requested = self._vpn_stop_requested
                if is_current:
                    self.openvpn_process = None
                    self._mgmt_port = None
                    if stop_requested:
                        self.vpn_state = 'DISCONNECTED'
                    else:
                        if self.vpn_state != 'ERROR':
                            self.vpn_state = 'ERROR' if (rc is not None and rc != 0) else 'DISCONNECTED'
                        if self.hotspot_active or self.ethernet_active or self.app_shared_pair:
                            need_teardown = True
                            self.vpn_logs.append('[!] Conf channel stopped unexpectedly — disabling hotspot/Ethernet ICS automatically.')
            if need_teardown:
                threading.Thread(target=self._emergency_stop_sharing, daemon=True).start()

    def read_vpn_logs(self):
        with self.vpn_lock:
            lines = self.vpn_logs[:]
            self.vpn_logs.clear()
            proc = self.openvpn_process
            state = self.vpn_state
            if proc and proc.poll() is not None and state not in ('ERROR', 'DISCONNECTED'):
                state = 'DISCONNECTED'
                self.vpn_state = state
        return {'logs': lines, 'state': state, 'hotspot_active': self.hotspot_active, 'ethernet_active': self.ethernet_active}

    def _send_mgmt_sigterm(self, port):
        if not port:
            return False
        try:
            with socket.create_connection(('127.0.0.1', int(port)), timeout=1.5) as s:
                s.settimeout(1.5)
                try:
                    s.recv(512)
                except Exception:
                    pass
                s.sendall(b'signal SIGTERM\r\n')
                return True
        except Exception:
            return False

    def disconnect_vpn(self, profile=None):
        with self.vpn_lock:
            self._vpn_stop_requested = True
            proc = self.openvpn_process
            mgmt_port = self._mgmt_port

        # Stop hotspot/Ethernet/ICS first so clients are never left on an unencrypted tunnel.
        if self.hotspot_active:
            self.stop_vpn_hotspot()
        if self.ethernet_active:
            self.stop_vpn_ethernet()
        elif self.app_shared_pair:
            self._disable_app_shared_pair()

        if proc and proc.poll() is None:
            sent_mgmt = self._send_mgmt_sigterm(mgmt_port)
            if sent_mgmt:
                try:
                    proc.wait(timeout=4)
                except Exception:
                    pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=3)
                    except Exception:
                        pass

        self._cleanup_auth_file()
        if self._uplink_dns_backups and platform.system() == 'Windows':
            try:
                self._restore_uplink_dns_backups()
            except Exception:
                pass
        with self.vpn_lock:
            self.openvpn_process = None
            self._mgmt_port = None
            self.vpn_state = 'DISCONNECTED'
            self.vpn_adapter_name = None
            self.vpn_adapter_guid = None
            self.vpn_ifindex = None
            self.vpn_logs.append('[NEON] Conf engine disconnected.')
        self._logger.info('Conf engine disconnected')
        return {
            'ok': True,
            'output': 'NEON stopped its hosted-network sharing (if active) and disconnected the background Conf engine.',
        }

    @staticmethod
    def _classify_adapter(adapter):
        name = str(adapter.get('name', '') or '')
        device = str(adapter.get('device', '') or '')
        phys_media = str(adapter.get('phys_media', '') or '').lower()
        ndis_medium = adapter.get('ndis_medium')
        virtual_flag = adapter.get('virtual')
        hardware_flag = adapter.get('hardware')
        netcon_media = adapter.get('netcon_media')
        has_vpn_route = bool(adapter.get('has_vpn_route'))
        has_default_gw = bool(adapter.get('has_default_gw'))
        label = (name + ' ' + device).lower()

        vpn_keys = (
            'openvpn', 'tap-windows', 'tap-win32', 'tap-win64', 'tap0901',
            'wintun', 'ovpn-dco', 'data channel offload',
            'wireguard', 'zerotier', 'tailscale', 'nordlynx', 'amnezia',
        )
        if any(k in label for k in vpn_keys) or re.search(r'\bdco\b', label) or netcon_media == 5:
            return 'vpn'
        if has_vpn_route and not has_default_gw:
            return 'vpn'

        hotspot_keys = (
            'microsoft hosted network', 'wi-fi direct virtual adapter',
            'hosted network', 'wi-fi direct',
            'размещенной сети', 'размещённой сети',
            'local area connection*', 'подключение по локальной сети*',
        )
        if any(k in label for k in hotspot_keys):
            return 'hotspot'

        virtual_keys = (
            'vmware', 'virtualbox', 'vbox', 'hyper-v', 'vethernet',
            'loopback', 'bluetooth', 'npcap', 'hamachi', 'radmin',
            'teredo', 'isatap', 'wan miniport', '6to4', 'ip-https',
            'virtual switch', 'host-only', 'bridge miniport', 'km-test',
        )
        if any(k in label for k in virtual_keys) or virtual_flag is True:
            return 'virtual'

        wifi_keys = (
            'wi-fi', 'wifi', 'wireless', 'wlan', 'беспроводн', '802.11',
            'broadcom 802', 'bcm43', 'centrino', 'qualcomm atheros',
        )
        if (
            ndis_medium in (1, 8, 9)
            or '802.11' in phys_media
            or 'wireless' in phys_media
            or any(k in label for k in wifi_keys)
        ):
            return 'wifi'

        eth_keys = (
            'ethernet', 'gigabit', 'gbe', 'fast ethernet',
            'realtek pcie', 'realtek usb', 'realtek gaming',
            'intel(r) i2', 'intel(r) ethernet', 'intel(r) 825',
            'killer e', 'asix', 'ax88', 'rtl81', 'usb lan', 'usb to ethernet',
            'broadcom netlink', 'broadcom netxtreme', 'marvell', 'aquantia',
            'local area connection', 'подключение по локальной сети',
        )
        if (
            ndis_medium == 14
            or ('802.3' in phys_media and hardware_flag is not False)
            or any(k in label for k in eth_keys)
        ):
            return 'ethernet'
        if hardware_flag is True and netcon_media == 3 and not phys_media:
            return 'ethernet'
        return 'other'

    def _is_matching_vpn_adapter(self, adapter):
        name = str(adapter.get('name', '') or '')
        guid = str(adapter.get('guid', '') or '').upper()
        if_idx = adapter.get('if_index')
        if self.vpn_adapter_name and name and name == self.vpn_adapter_name:
            return True
        if self.vpn_adapter_guid and guid and guid == self.vpn_adapter_guid:
            return True
        if self.vpn_ifindex is not None and if_idx is not None and int(if_idx) == int(self.vpn_ifindex):
            return True
        return False

    def network_connections(self):
        if platform.system() != 'Windows':
            return {'adapters': [], 'error': 'Available in the Windows build.'}
        script = (
            "$netInfo=@{}; "
            "try{"
            "Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | ForEach-Object {"
            "$g=[string]$_.InterfaceGuid; if($g){$g=$g.Trim().ToUpperInvariant()}; "
            "$nm=$null; try{$nm=[int]$_.NdisPhysicalMedium}catch{}; "
            "$netInfo[$g]=@{phys=[string]$_.PhysicalMediaType;ndis=$nm;hw=[bool]$_.HardwareInterface;virt=[bool]$_.Virtual;ifidx=[int]$_.ifIndex;drv=[string]$_.DriverDescription}"
            "}"
            "}catch{}; "
            "$ipMap=@{}; "
            "try{"
            "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1'} | ForEach-Object {"
            "if(-not $ipMap.ContainsKey([int]$_.InterfaceIndex)){$ipMap[[int]$_.InterfaceIndex]=[string]$_.IPAddress}"
            "}"
            "}catch{}; "
            "$dnsMap=@{}; "
            "try{"
            "Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object {"
            "$dnsMap[[int]$_.InterfaceIndex]=@($_.ServerAddresses)"
            "}"
            "}catch{}; "
            "$gwMap=@{}; $vpnRtMap=@{}; "
            "try{"
            "Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object {"
            "$idx=[int]$_.ifIndex; "
            "if($_.DestinationPrefix -eq '0.0.0.0/1' -or $_.DestinationPrefix -eq '128.0.0.0/1'){ $vpnRtMap[$idx]=$true } "
            "elseif($_.DestinationPrefix -eq '0.0.0.0/0'){"
            "if($_.NextHop -and $_.NextHop -ne '0.0.0.0'){ if(-not $gwMap.ContainsKey($idx)){$gwMap[$idx]=[string]$_.NextHop} } "
            "else { $vpnRtMap[$idx]=$true }"
            "}"
            "}"
            "}catch{}; "
            "$m=New-Object -ComObject HNetCfg.HNetShare; $a=@(); "
            "foreach($c in $m.EnumEveryConnection){"
            "$p=$m.NetConnectionProps.Invoke($c);"
            "$q=$m.INetSharingConfigurationForINetConnection.Invoke($c);"
            "$st=$null; if($q.SharingEnabled){$st=[int]$q.SharingConnectionType};"
            "$g=[string]$p.Guid; if($g){$g=$g.Trim().ToUpperInvariant()};"
            "$ni=$netInfo[$g];"
            "$idx=$(if($ni){[int]$ni.ifidx}else{$null});"
            "$ip=''; if($idx -ne $null -and $ipMap.ContainsKey($idx)){$ip=$ipMap[$idx]};"
            "$dns=@(); if($idx -ne $null -and $dnsMap.ContainsKey($idx)){$dns=$dnsMap[$idx]};"
            "$gw=''; if($idx -ne $null -and $gwMap.ContainsKey($idx)){$gw=$gwMap[$idx]};"
            "$hasGw=($gw -ne '');"
            "$hasVpnRt=($idx -ne $null -and $vpnRtMap.ContainsKey($idx));"
            "$devStr=[string]$p.DeviceName; if(-not $devStr -and $ni -and $ni.drv){$devStr=$ni.drv};"
            "$a+=@{name=$p.Name;device=$devStr;shared=[bool]$q.SharingEnabled;sharing_type=$st;status=[int]$p.Status;"
            "netcon_media=[int]$p.MediaType;guid=$g;if_index=$idx;"
            "phys_media=$(if($ni){$ni.phys}else{''});ndis_medium=$(if($ni){$ni.ndis}else{$null});"
            "hardware=$(if($ni){$ni.hw}else{$null});virtual=$(if($ni){$ni.virt}else{$null});"
            "ipv4=$ip;dns_servers=$dns;default_gw=$gw;has_default_gw=$hasGw;has_vpn_route=$hasVpnRt}"
            "}; "
            "$j=ConvertTo-Json -InputObject @($a) -Compress; "
            "[Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($j)))"
        )
        try:
            raw = self._powershell(script)
            decoded = base64.b64decode(raw.strip()).decode('utf-16le') if raw else '[]'
            data = json.loads(decoded) if decoded else []
            if isinstance(data, dict):
                data = [data]
            for item in data:
                if isinstance(item, dict):
                    if self._is_matching_vpn_adapter(item):
                        item['category'] = 'vpn'
                        item['vpn_matched_log'] = True
                    else:
                        item['category'] = self._classify_adapter(item)
            return {'adapters': data}
        except Exception as exc:
            return {'adapters': [], 'error': str(exc)}

    def dns_presets(self):
        return {
            'presets': [
                {'id': k, 'label_en': v['label_en'], 'label_ru': v['label_ru'], 'servers': list(v['servers'])}
                for k, v in DNS_PRESETS.items()
            ],
            'current': self._get_dns_config_from_settings(),
        }

    @staticmethod
    def _validate_ipv4_dns(addr):
        txt = str(addr or '').strip()
        if not txt:
            return ''
        try:
            ip = ipaddress.IPv4Address(txt)
        except Exception as exc:
            raise ValueError(f'Invalid IPv4 DNS address "{txt}": {exc}')
        if ip.is_unspecified or ip.is_loopback or ip.is_multicast or ip.is_link_local or int(ip) >= 0xE0000000:
            raise ValueError(f'DNS address "{txt}" cannot be used for client routing (reserved/loopback/multicast).')
        return str(ip)

    def validate_dns_config(self, dns_mode='auto', dns_primary='', dns_secondary='', apply_uplink=False):
        mode = str(dns_mode or 'auto').strip().lower()
        p_raw = str(dns_primary or '').strip()
        s_raw = str(dns_secondary or '').strip()
        uplink = bool(apply_uplink)

        if mode not in DNS_PRESETS:
            if p_raw:
                mode = 'custom'
            else:
                return {'ok': False, 'error': f'Unknown DNS mode: {dns_mode}'}

        if mode == 'auto':
            return {
                'ok': True,
                'mode': 'auto',
                'primary': '',
                'secondary': '',
                'servers': [],
                'apply_uplink': False,
            }

        preset_servers = DNS_PRESETS[mode]['servers']
        if preset_servers:
            if not p_raw and not s_raw:
                p_raw = preset_servers[0]
                s_raw = preset_servers[1] if len(preset_servers) > 1 else ''
            else:
                # If caller supplied addresses differing from preset, auto-promote to custom.
                exp_p = preset_servers[0]
                exp_s = preset_servers[1] if len(preset_servers) > 1 else ''
                if p_raw != exp_p or s_raw != exp_s:
                    mode = 'custom'

        if mode == 'custom' and not p_raw:
            return {'ok': False, 'error': 'Enter Primary DNS IPv4 address (for example 1.1.1.1 or 8.8.8.8).'}

        try:
            p = self._validate_ipv4_dns(p_raw)
            s = self._validate_ipv4_dns(s_raw)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}

        if not p and s:
            p, s = s, ''
        if s and s == p:
            s = ''
        servers = [x for x in (p, s) if x]
        return {
            'ok': True,
            'mode': mode,
            'primary': p,
            'secondary': s,
            'servers': servers,
            'apply_uplink': uplink,
        }

    def _get_dns_config_from_settings(self):
        st = self.load_settings()
        res = self.validate_dns_config(
            st.get('dns_mode', 'auto'),
            st.get('dns_primary', ''),
            st.get('dns_secondary', ''),
            st.get('dns_apply_uplink', False),
        )
        if res.get('ok'):
            return res
        return {
            'ok': True,
            'mode': 'auto',
            'primary': '',
            'secondary': '',
            'servers': [],
            'apply_uplink': False,
        }

    def _set_adapter_ipv4_dns(self, adapter_name, servers):
        if platform.system() != 'Windows':
            return {'ok': False, 'error': 'DNS configuration is Windows-only.'}
        if not adapter_name:
            return {'ok': False, 'error': 'Adapter name is required.'}
        clean_servers = []
        for s in (servers or []):
            v = self._validate_ipv4_dns(s)
            if v and v not in clean_servers:
                clean_servers.append(v)

        payload = base64.b64encode(
            json.dumps({'name': str(adapter_name), 'servers': clean_servers}).encode('utf-8')
        ).decode('ascii')
        script = (
            f"$d=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))|ConvertFrom-Json; "
            "$ad=Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq $d.name } | Select-Object -First 1; "
            "$idx=$null; $guid=''; if($ad){ $idx=[int]$ad.ifIndex; $guid=[string]$ad.InterfaceGuid }; "
            "$wasStatic=$false; $prevStatic=@(); "
            "if($guid){"
            "if(-not $guid.StartsWith('{')){$guid='{'+$guid+'}'}; "
            "$reg='HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces\\' + $guid; "
            "try{ "
            "$ns=(Get-ItemProperty -Path $reg -Name NameServer -ErrorAction SilentlyContinue).NameServer; "
            "if($ns){ $wasStatic=$true; $prevStatic=@($ns -split '[,\\s]+' | Where-Object { $_ }) } "
            "}catch{}"
            "}; "
            "$prev=@(); "
            "if($idx -ne $null){"
            "try{ $c=Get-DnsClientServerAddress -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction SilentlyContinue; "
            "if($c -and $c.ServerAddresses){ $prev=@($c.ServerAddresses) } }catch{}"
            "}; "
            "$srv=@($d.servers | Where-Object { $_ }); "
            "if($srv.Count -gt 0){"
            "$done=$false; "
            "if($idx -ne $null){ try{ Set-DnsClientServerAddress -InterfaceIndex $idx -ServerAddresses $srv -ErrorAction Stop; $done=$true }catch{} }; "
            "if(-not $done){ "
            "$r1=& netsh interface ipv4 set dnsservers name=\"$($d.name)\" source=static address=\"$($srv[0])\" register=primary 2>&1; "
            "if($LASTEXITCODE -ne 0){ [Console]::WriteLine('NEON_ERROR: Failed to set DNS on ' + $d.name + ': ' + ($r1 -join ' ')); exit 2 }; "
            "if($srv.Count -gt 1){ "
            "$r2=& netsh interface ipv4 add dnsservers name=\"$($d.name)\" address=\"$($srv[1])\" index=2 2>&1; "
            "if($LASTEXITCODE -ne 0){ [Console]::WriteLine('NEON_ERROR: Failed to add secondary DNS on ' + $d.name + ': ' + ($r2 -join ' ')); exit 2 } "
            "} "
            "}"
            "} else {"
            "$done=$false; "
            "if($idx -ne $null){ try{ Set-DnsClientServerAddress -InterfaceIndex $idx -ResetServerAddresses -ErrorAction Stop; $done=$true }catch{} }; "
            "if(-not $done){ "
            "$r0=& netsh interface ipv4 set dnsservers name=\"$($d.name)\" source=dhcp 2>&1; "
            "if($LASTEXITCODE -ne 0){ [Console]::WriteLine('NEON_ERROR: Failed to reset DNS on ' + $d.name + ': ' + ($r0 -join ' ')); exit 2 } "
            "}"
            "}; "
            "try{ Clear-DnsClientCache -ErrorAction Stop }catch{ try{ & ipconfig /flushdns | Out-Null }catch{} }; "
            "$after=@(); "
            "if($idx -ne $null){"
            "try{ $c2=Get-DnsClientServerAddress -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction SilentlyContinue; "
            "if($c2 -and $c2.ServerAddresses){ $after=@($c2.ServerAddresses) } }catch{}"
            "}; "
            "$res=@{adapter=$d.name;was_static=$wasStatic;prev_static=$prevStatic;previous=$prev;current=$after}; "
            "$j=ConvertTo-Json -InputObject $res -Compress; "
            "[Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($j)))"
        )
        raw = self._powershell(script, timeout=20)
        decoded = base64.b64decode(raw.strip()).decode('utf-16le') if raw else '{}'
        data = json.loads(decoded) if decoded else {}
        return {
            'ok': True,
            'adapter': data.get('adapter', adapter_name),
            'was_static': bool(data.get('was_static')),
            'prev_static': list(data.get('prev_static') or []),
            'previous': list(data.get('previous') or []),
            'current': list(data.get('current') or clean_servers),
        }

    def _restore_uplink_dns_backups(self):
        notes = []
        if platform.system() != 'Windows' or not self._uplink_dns_backups:
            self._uplink_dns_backups.clear()
            return notes
        for name, prev_servers in list(self._uplink_dns_backups.items()):
            try:
                res = self._set_adapter_ipv4_dns(name, prev_servers or [])
                if prev_servers:
                    notes.append(f'[+] Restored Uplink DNS on "{name}" -> {", ".join(prev_servers)}')
                else:
                    notes.append(f'[+] Restored Uplink DNS on "{name}" -> Auto (DHCP)')
                self._logger.info('Restored uplink DNS on %s: %s', name, res.get('current'))
            except Exception as exc:
                notes.append(f'[!] Failed to restore Uplink DNS on "{name}": {exc}')
                self._logger.warning('Failed to restore uplink DNS on %s: %s', name, exc)
            finally:
                self._uplink_dns_backups.pop(name, None)
        return notes

    def _apply_configured_dns_for_sharing(self, public_name, adapters=None):
        cfg = self._get_dns_config_from_settings()
        servers = cfg.get('servers') or []
        apply_uplink = bool(cfg.get('apply_uplink'))
        notes = []

        if not servers:
            if self._uplink_dns_backups:
                notes.extend(self._restore_uplink_dns_backups())
            return notes

        if platform.system() != 'Windows':
            return notes

        if public_name:
            try:
                res = self._set_adapter_ipv4_dns(public_name, servers)
                actual = ', '.join(res.get('current') or servers)
                notes.append(f'[+] Sharing DNS ({cfg["mode"]}): {actual} applied on "{public_name}"')
                self._logger.info('Applied sharing DNS (%s) on %s: %s', cfg['mode'], public_name, actual)
            except Exception as exc:
                notes.append(f'[!] Could not set DNS on "{public_name}": {exc}')
                self._logger.warning('Failed setting DNS on %s: %s', public_name, exc)

        if apply_uplink:
            ad_list = list(adapters or [])
            if not ad_list:
                try:
                    ad_list = self.network_connections().get('adapters', [])
                except Exception:
                    ad_list = []
            uplinks = [
                a for a in ad_list
                if a.get('name') and a.get('name') != public_name
                and a.get('has_default_gw')
                and (a.get('category') or self._classify_adapter(a)) in ('wifi', 'ethernet', 'other')
            ]
            for u in uplinks:
                uname = u.get('name')
                try:
                    res = self._set_adapter_ipv4_dns(uname, servers)
                    if uname not in self._uplink_dns_backups:
                        self._uplink_dns_backups[uname] = (res.get('prev_static') or []) if res.get('was_static') else None
                    notes.append(f'[+] Uplink DNS ({cfg["mode"]}): {", ".join(servers)} applied on "{uname}" (auto-restore enabled)')
                except Exception as exc:
                    notes.append(f'[!] Could not set Uplink DNS on "{uname}": {exc}')
        elif self._uplink_dns_backups:
            notes.extend(self._restore_uplink_dns_backups())

        return notes

    def _apply_dns_after_vpn_connect(self):
        time.sleep(0.55)
        try:
            cfg = self._get_dns_config_from_settings()
            servers = cfg.get('servers') or []
            if not servers or platform.system() != 'Windows':
                return
            with self.vpn_lock:
                if self.vpn_state != 'CONNECTED':
                    return
            adapters = self.network_connections().get('adapters', [])
            pub = self._select_vpn_public_adapter(adapters)
            if not pub:
                return
            notes = self._apply_configured_dns_for_sharing(pub.get('name'), adapters=adapters)
            if notes:
                with self.vpn_lock:
                    self.vpn_logs.extend(notes)
        except Exception as exc:
            self._logger.warning('Post-connect DNS apply warning: %s', exc)

    def get_sharing_dns(self):
        cfg = self._get_dns_config_from_settings()
        vpn_adapter = ''
        vpn_dns = []
        uplinks = []
        if platform.system() == 'Windows':
            try:
                adapters = self.network_connections().get('adapters', [])
                pub = None
                if self.app_shared_pair and self.app_shared_pair[0]:
                    pub = next((a for a in adapters if a.get('name') == self.app_shared_pair[0]), None)
                if not pub:
                    pub = self._select_vpn_public_adapter(adapters)
                if pub:
                    vpn_adapter = str(pub.get('name', ''))
                    vpn_dns = list(pub.get('dns_servers') or [])
                for a in adapters:
                    if a.get('has_default_gw') and (a.get('category') or self._classify_adapter(a)) in ('wifi', 'ethernet', 'other'):
                        uplinks.append({'name': a.get('name'), 'dns_servers': list(a.get('dns_servers') or [])})
            except Exception:
                pass
        return {
            'ok': True,
            'dns_mode': cfg['mode'],
            'dns_primary': cfg['primary'],
            'dns_secondary': cfg['secondary'],
            'servers': cfg['servers'],
            'apply_uplink': cfg['apply_uplink'],
            'vpn_adapter': vpn_adapter,
            'vpn_dns': vpn_dns,
            'uplinks': uplinks,
        }

    def apply_sharing_dns(self, dns_mode='auto', dns_primary='', dns_secondary='', apply_uplink=False, target_adapter=None, reapply_ics=True):
        cfg = self.validate_dns_config(dns_mode, dns_primary, dns_secondary, apply_uplink)
        if not cfg.get('ok'):
            return {'ok': False, 'error': cfg.get('error', 'Invalid DNS configuration.')}

        self.save_settings({
            'dns_mode': cfg['mode'],
            'dns_primary': cfg['primary'],
            'dns_secondary': cfg['secondary'],
            'dns_apply_uplink': cfg['apply_uplink'],
        })

        servers = cfg['servers']
        mode_desc = 'Auto (from Conf / DHCP)' if not servers else f"{cfg['mode']}: {', '.join(servers)}"

        if platform.system() != 'Windows':
            return {
                'ok': True,
                'saved_only': True,
                'mode': cfg['mode'],
                'primary': cfg['primary'],
                'secondary': cfg['secondary'],
                'dns_mode': cfg['mode'],
                'dns_primary': cfg['primary'],
                'dns_secondary': cfg['secondary'],
                'servers': servers,
                'apply_uplink': cfg['apply_uplink'],
                'applied_adapters': [],
                'output': f'[+] DNS profile saved ({mode_desc}). Applies on Windows Conf connect & sharing.',
            }

        adapters_res = self.network_connections()
        adapters = adapters_res.get('adapters', [])
        explicit = str(target_adapter or '').strip()
        pub_name = explicit or (self.app_shared_pair[0] if self.app_shared_pair else '')
        if not pub_name:
            pub_adapter = self._select_vpn_public_adapter(adapters)
            pub_name = pub_adapter.get('name') if pub_adapter else ''

        lines = []
        applied = []

        if pub_name:
            try:
                res = self._set_adapter_ipv4_dns(pub_name, servers)
                applied.append(pub_name)
                if servers:
                    lines.append(f'[+] DNS ({cfg["mode"]}) set to {", ".join(res.get("current") or servers)} on "{pub_name}".')
                else:
                    cur = ', '.join(res.get('current') or [])
                    lines.append(f'[+] Reset DNS on "{pub_name}" to Auto (Conf / DHCP)' + (f' (current: {cur})' if cur else '') + '.')
            except Exception as exc:
                return {'ok': False, 'error': f'Failed to apply DNS on "{pub_name}": {exc}'}
        else:
            lines.append(f'[+] DNS profile saved ({mode_desc}). No active Conf adapter right now — will apply automatically when Conf connects and sharing starts.')

        if cfg['apply_uplink'] and servers:
            uplinks = [
                a for a in adapters
                if a.get('name') and a.get('name') != pub_name
                and a.get('has_default_gw')
                and (a.get('category') or self._classify_adapter(a)) in ('wifi', 'ethernet', 'other')
            ]
            for u in uplinks:
                uname = u.get('name')
                try:
                    res = self._set_adapter_ipv4_dns(uname, servers)
                    if uname not in self._uplink_dns_backups:
                        self._uplink_dns_backups[uname] = (res.get('prev_static') or []) if res.get('was_static') else None
                    applied.append(uname)
                    lines.append(f'[+] Uplink DNS set to {", ".join(servers)} on "{uname}" (will restore automatically on stop).')
                except Exception as exc:
                    lines.append(f'[!] Failed to apply DNS on Uplink "{uname}": {exc}')
        elif self._uplink_dns_backups:
            lines.extend(self._restore_uplink_dns_backups())

        # Refresh active ICS route if sharing is live so Windows SharedAccess (192.168.137.1:53) picks up new upstream DNS immediately.
        ics_refreshed = False
        if reapply_ics and pub_name and self.app_shared_pair and self.app_shared_pair[1]:
            pair_pub, pair_priv = self.app_shared_pair
            if self.hotspot_active and self.hotspot_mode == 'winrt':
                lines.append('[i] Win10/11 Mobile Hotspot is live: Windows DNS cache flushed. If TV keeps cached DNS, restart the hotspot (Stop -> Start).')
            else:
                dis = self._disable_app_shared_pair()
                if not dis.get('error'):
                    self._skip_set_sharing_dns = True
                    try:
                        rebind = self.set_sharing(pair_pub, pair_priv)
                    finally:
                        self._skip_set_sharing_dns = False
                    if rebind.get('ok'):
                        ics_refreshed = True
                        lines.append(f'[+] Refreshed active ICS proxy ({pair_pub} -> {pair_priv}) so clients immediately use updated DNS.')
                    elif rebind.get('error'):
                        lines.append(f'[!] Could not refresh active ICS proxy: {rebind["error"]}')

        self._logger.info('Sharing DNS updated: mode=%s servers=%s uplink=%s applied=%s', cfg['mode'], servers, cfg['apply_uplink'], applied)
        return {
            'ok': True,
            'saved_only': not bool(applied),
            'mode': cfg['mode'],
            'primary': cfg['primary'],
            'secondary': cfg['secondary'],
            'dns_mode': cfg['mode'],
            'dns_primary': cfg['primary'],
            'dns_secondary': cfg['secondary'],
            'servers': servers,
            'apply_uplink': cfg['apply_uplink'],
            'applied_adapters': applied,
            'ics_refreshed': ics_refreshed,
            'output': '\n'.join(lines),
        }

    def reset_sharing_dns(self, target_adapter=None):
        return self.apply_sharing_dns(
            dns_mode='auto',
            dns_primary='',
            dns_secondary='',
            apply_uplink=False,
            target_adapter=target_adapter,
            reapply_ics=True,
        )

    def _clear_conflicting_ics(self, public_name, private_name):
        if platform.system() != 'Windows':
            return
        payload = base64.b64encode(json.dumps({'public': public_name, 'private': private_name}).encode('utf-8')).decode('ascii')
        script = (
            f"$d=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))|ConvertFrom-Json; "
            "$m=New-Object -ComObject HNetCfg.HNetShare; $changed=$false; "
            "foreach($c in $m.EnumEveryConnection){"
            "$p=$m.NetConnectionProps.Invoke($c);"
            "$q=$m.INetSharingConfigurationForINetConnection.Invoke($c);"
            "if($q.SharingEnabled){"
            "$want=(($p.Name -eq $d.public -and $q.SharingConnectionType -eq 0) -or ($p.Name -eq $d.private -and $q.SharingConnectionType -eq 1));"
            "if(-not $want){$q.DisableSharing(); $changed=$true}"
            "}"
            "}; "
            "if($changed){ "
            "foreach($c in $m.EnumEveryConnection){"
            "$q=$m.INetSharingConfigurationForINetConnection.Invoke($c);"
            "if($q.SharingEnabled){$q.DisableSharing()}"
            "}; "
            "try{ Restart-Service -Name SharedAccess -Force -ErrorAction SilentlyContinue }catch{}; "
            "Start-Sleep -Milliseconds 300; 'Cleared non-VPN ICS' "
            "} else { 'No conflicting ICS' }"
        )
        try:
            out = self._powershell(script, timeout=20)
            self._logger.info('Pre-share ICS cleanup: %s', out)
            return out
        except Exception as exc:
            self._logger.warning('Pre-share ICS cleanup warning: %s', exc)
            return ''

    def set_sharing(self, public_name, private_name):
        if platform.system() != 'Windows':
            return {'error': 'ICS sharing is Windows-only.'}
        if not public_name or not private_name or public_name == private_name:
            return {'error': 'Choose two different network adapters.'}
        dns_notes = []
        if not self._skip_set_sharing_dns:
            cfg = self._get_dns_config_from_settings()
            if cfg.get('servers') or self._uplink_dns_backups:
                dns_notes = self._apply_configured_dns_for_sharing(public_name)
        payload = base64.b64encode(json.dumps({'public': public_name, 'private': private_name}).encode('utf-8')).decode('ascii')
        script = (
            f"$d=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))|ConvertFrom-Json; "
            "try{ "
            "$w=Get-CimInstance Win32_Service -Filter \"Name='SharedAccess'\" -ErrorAction SilentlyContinue; "
            "if($w -and $w.StartMode -eq 'Disabled'){ Set-Service -Name SharedAccess -StartupType Manual -ErrorAction SilentlyContinue } "
            "}catch{}; "
            "$m=New-Object -ComObject HNetCfg.HNetShare; $pub=$null;$priv=$null;$active=@();$pubOn=$false;$privOn=$false; "
            "foreach($c in $m.EnumEveryConnection){"
            "$p=$m.NetConnectionProps.Invoke($c);"
            "$q=$m.INetSharingConfigurationForINetConnection.Invoke($c);"
            "if($p.Name -eq $d.public){$pub=$c};"
            "if($p.Name -eq $d.private){$priv=$c};"
            "if($q.SharingEnabled){"
            "$active+=($p.Name+':'+$q.SharingConnectionType);"
            "if($p.Name -eq $d.public -and $q.SharingConnectionType -eq 0){$pubOn=$true};"
            "if($p.Name -eq $d.private -and $q.SharingConnectionType -eq 1){$privOn=$true}"
            "}"
            "}; "
            "if(-not $pub -or -not $priv){[Console]::WriteLine('NEON_ERROR: Adapter not found. Refresh the list and select exact adapter names.'); exit 2}; "
            "if($pubOn -and $privOn){[Console]::WriteLine('ICS is already configured for selected Conf -> target adapters.'); exit 0}; "
            "if($active.Count -gt 0){[Console]::WriteLine('NEON_ERROR: Another ICS route is already active ('+($active -join ', ')+'). Stop existing sharing first.'); exit 2}; "
            "$pubCfg=$m.INetSharingConfigurationForINetConnection.Invoke($pub); "
            "$privCfg=$m.INetSharingConfigurationForINetConnection.Invoke($priv); "
            "$pubCfg.EnableSharing(0); "
            "try{ $privCfg.EnableSharing(1) } catch { try{ $pubCfg.DisableSharing() } catch {}; throw }; "
            "$boundPub=$null; $boundPriv=$null; "
            "foreach($c in $m.EnumEveryConnection){"
            "$p=$m.NetConnectionProps.Invoke($c);"
            "$q=$m.INetSharingConfigurationForINetConnection.Invoke($c);"
            "if($q.SharingEnabled){"
            "if($q.SharingConnectionType -eq 0){$boundPub=$p.Name};"
            "if($q.SharingConnectionType -eq 1){$boundPriv=$p.Name}"
            "}"
            "}; "
            "if($boundPub -ne $d.public -or $boundPriv -ne $d.private){"
            "[Console]::WriteLine('NEON_ERROR: ICS bound unexpectedly ('+$boundPub+' -> '+$boundPriv+'). Expected '+$d.public+' -> '+$d.private); exit 2"
            "}; "
            "'Internet Connection Sharing enabled: ' + $d.public + ' -> ' + $d.private"
        )
        try:
            result = self._powershell(script)
            self.app_shared_pair = (public_name, private_name)
            self._logger.info('ICS enabled: %s -> %s', public_name, private_name)
            out_lines = dns_notes + ([result] if result else [])
            return {'ok': True, 'output': '\n'.join(out_lines)}
        except Exception as exc:
            msg = str(exc)
            if 'wintun' in public_name.lower():
                msg += (
                    ' (Note: Windows ICS sometimes rejects Wintun TUN adapters; '
                    'if this persists, remove "windows-driver wintun" from the .ovpn or switch OpenVPN driver to TAP-Windows6 / ovpn-dco.)'
                )
            self._logger.warning('ICS failed (%s -> %s): %s', public_name, private_name, msg)
            return {'error': msg}

    def _disable_app_shared_pair(self):
        if platform.system() != 'Windows' or not self.app_shared_pair:
            self.app_shared_pair = None
            return {'ok': True, 'output': ''}
        payload = base64.b64encode(json.dumps(list(self.app_shared_pair)).encode('utf-8')).decode('ascii')
        script = (
            f"$n=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))|ConvertFrom-Json; "
            "$m=New-Object -ComObject HNetCfg.HNetShare; "
            "foreach($c in $m.EnumEveryConnection){"
            "$p=$m.NetConnectionProps.Invoke($c);"
            "$q=$m.INetSharingConfigurationForINetConnection.Invoke($c);"
            "if($q.SharingEnabled -and ($p.Name -eq $n[0] -or $p.Name -eq $n[1])){$q.DisableSharing()}"
            "}; 'NEON ICS route disabled.'"
        )
        try:
            out = self._powershell(script)
            self.app_shared_pair = None
            return {'ok': True, 'output': out}
        except Exception as exc:
            return {'error': str(exc)}

    def stop_sharing(self):
        if platform.system() != 'Windows':
            return {'error': 'Windows only.'}
        script = (
            "$m=New-Object -ComObject HNetCfg.HNetShare; "
            "foreach($c in $m.EnumEveryConnection){"
            "$q=$m.INetSharingConfigurationForINetConnection.Invoke($c);"
            "if($q.SharingEnabled){$q.DisableSharing()}"
            "}; 'Internet Connection Sharing disabled.'"
        )
        try:
            result = self._powershell(script)
            self.app_shared_pair = None
            self.ethernet_active = False
            self.ethernet_adapter = None
            restore_notes = self._restore_uplink_dns_backups() if self._uplink_dns_backups else []
            self._logger.info('All ICS sharing disabled')
            out_lines = ([result] if result else []) + restore_notes
            return {'ok': True, 'output': '\n'.join(out_lines)}
        except Exception as exc:
            return {'error': str(exc)}

    def _driver_supports_hosted_network(self):
        if platform.system() != 'Windows':
            return None
        rc, out, _err = self._run_netsh('wlan', 'show', 'drivers', timeout=10)
        if rc != 0 or not out:
            return None
        for line in out.splitlines():
            low = line.lower()
            if 'hosted network supported' in low or 'поддержка размещенной сети' in low or 'поддержка размещённой сети' in low:
                val = line.split(':', 1)[-1].strip().lower()
                if val in ('yes', 'да'):
                    return True
                if val in ('no', 'нет'):
                    return False
        return None

    def _start_winrt_hotspot(self, ssid, password):
        payload = base64.b64encode(json.dumps({'ssid': ssid, 'password': password}).encode('utf-8')).decode('ascii')
        script = (
            f"$d=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))|ConvertFrom-Json; "
            "Add-Type -AssemblyName System.Runtime.WindowsRuntime; "
            "$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { "
            "$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]; "
            "Function Await($WinRtTask, $ResultType) { "
            "$netTask = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask)); "
            "$netTask.Wait(-1) | Out-Null; return $netTask.Result }; "
            "$asTaskAction = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { "
            "$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]; "
            "Function AwaitAction($WinRtTask) { $netTask = $asTaskAction.Invoke($null, @($WinRtTask)); $netTask.Wait(-1) | Out-Null }; "
            "$null = [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType=WindowsRuntime]; "
            "$null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType=WindowsRuntime]; "
            "$profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile(); "
            "if(-not $profile){ [Console]::WriteLine('NEON_ERROR: No active internet connection profile found for Mobile Hotspot.'); exit 2 }; "
            "$mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile); "
            "$cfg = $mgr.GetCurrentAccessPointConfiguration(); "
            "$cfg.Ssid = $d.ssid; $cfg.Passphrase = $d.password; "
            "AwaitAction ($mgr.ConfigureAccessPointAsync($cfg)); "
            "$res = Await ($mgr.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult]); "
            "if($res.Status -ne 'Success'){ [Console]::WriteLine('NEON_ERROR: Windows Mobile Hotspot failed: ' + $res.Status + ' ' + $res.AdditionalErrorMessage); exit 2 }; "
            "'Windows Mobile Hotspot started'"
        )
        return self._powershell(script, timeout=30)

    def _stop_winrt_hotspot(self):
        script = (
            "Add-Type -AssemblyName System.Runtime.WindowsRuntime; "
            "$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { "
            "$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]; "
            "Function Await($WinRtTask, $ResultType) { "
            "$netTask = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask)); "
            "$netTask.Wait(-1) | Out-Null; return $netTask.Result }; "
            "$null = [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType=WindowsRuntime]; "
            "$null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType=WindowsRuntime]; "
            "$profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile(); "
            "if(-not $profile){ 'No active profile'; exit 0 }; "
            "$mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile); "
            "if($mgr.TetheringOperationalState -eq 'On'){ "
            "$null = Await ($mgr.StopTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult]); "
            "'Windows Mobile Hotspot stopped.' } else { 'Mobile Hotspot already stopped.' }"
        )
        return self._powershell(script, timeout=20)

    def _select_vpn_public_adapter(self, adapters, exclude_name=None):
        def _rank(a):
            exact = 0 if self._is_matching_vpn_adapter(a) else 1
            rt = 0 if a.get('has_vpn_route') else 1
            st = 0 if a.get('status') == 2 else 1
            return (exact, rt, st)

        candidates = []
        for a in adapters:
            if a.get('name') == exclude_name:
                continue
            cat = a.get('category') or self._classify_adapter(a)
            if self._is_matching_vpn_adapter(a) or cat == 'vpn' or (a.get('has_vpn_route') and not a.get('has_default_gw')):
                if cat not in ('wifi', 'hotspot') and not a.get('has_default_gw'):
                    candidates.append(a)
        candidates.sort(key=_rank)
        return candidates[0] if candidates else None

    def _detect_hotspot_and_vpn_adapters(self):
        found = self.network_connections()
        adapters = found.get('adapters', [])

        def label(a):
            return (str(a.get('name', '')) + ' ' + str(a.get('device', ''))).lower()

        priv_device_keys = (
            'microsoft hosted network virtual adapter',
            'wi-fi direct virtual adapter',
            'hosted network',
            'wi-fi direct',
            'размещенной сети',
            'размещённой сети',
        )
        priv_name_keys = ('local area connection*', 'подключение по локальной сети*')

        private_candidates = [
            a for a in adapters
            if any(k in str(a.get('device', '')).lower() for k in priv_device_keys)
        ]
        if not private_candidates:
            private_candidates = [
                a for a in adapters
                if any(k in label(a) for k in priv_name_keys)
            ]
        # Prefer connected adapter (status == 2) if multiple virtual adapters exist
        private_candidates.sort(key=lambda a: 0 if a.get('status') == 2 else 1)
        private = private_candidates[0].get('name') if private_candidates else None

        pub_adapter = self._select_vpn_public_adapter(adapters, exclude_name=private)
        public = pub_adapter.get('name') if pub_adapter else None
        return public, private, adapters

    def _detect_ethernet_and_vpn_adapters(self, preferred_ethernet=None):
        found = self.network_connections()
        if found.get('error') and not found.get('adapters'):
            return None, None, None, [], found.get('error')
        adapters = found.get('adapters', [])

        pub_adapter = self._select_vpn_public_adapter(adapters)
        public = pub_adapter.get('name') if pub_adapter else None

        pref = str(preferred_ethernet or '').strip()
        if pref:
            for a in adapters:
                if a.get('name') == pref and a.get('name') != public:
                    cat = a.get('category') or self._classify_adapter(a)
                    if cat in ('vpn', 'hotspot', 'wifi'):
                        return public, None, None, adapters, (
                            f'Adapter "{pref}" is classified as {cat}, not a wired Ethernet target.'
                        )
                    return public, a.get('name'), a, adapters, None

        saved_pref = ''
        try:
            saved_pref = str(self.load_settings().get('ethernet_adapter', '') or '').strip()
        except Exception:
            saved_pref = ''

        eth_all = [
            a for a in adapters
            if a.get('name') != public and (a.get('category') or self._classify_adapter(a)) == 'ethernet'
        ]
        eth_candidates = [a for a in eth_all if not a.get('has_default_gw')]
        if not eth_candidates and eth_all:
            uplink = eth_all[0]
            return public, None, None, adapters, (
                f'Skipped "{uplink.get("name")}" because incoming regular internet arrives through it '
                f'(gateway {uplink.get("default_gw") or "active"}). '
                'Use Wi-Fi for incoming internet and connect TV to a separate Ethernet port.'
            )

        eth_candidates.sort(key=lambda a: (
            0 if a.get('status') == 2 else 1,
            0 if (saved_pref and a.get('name') == saved_pref) else 1,
            0 if a.get('hardware') is True else 1,
        ))
        priv_adapter = eth_candidates[0] if eth_candidates else None
        private = priv_adapter.get('name') if priv_adapter else None
        return public, private, priv_adapter, adapters, None

    def start_vpn_ethernet(self, ethernet_name=None):
        if platform.system() != 'Windows':
            return {'error': 'Windows-only Ethernet ICS sharing.'}
        if self.vpn_state == 'CONNECTING':
            return {'error': 'Conf is still connecting — wait for Initialization Sequence Completed first.'}

        notes = []
        if self.hotspot_active:
            hs_res = self.stop_vpn_hotspot()
            if hs_res.get('error'):
                return {'error': 'Failed to stop active Wi-Fi hotspot first: ' + hs_res['error']}
            notes.append('Stopped previous Wi-Fi hotspot sharing cleanly before switching to Ethernet.')
        elif self.app_shared_pair and not self.ethernet_active:
            self._disable_app_shared_pair()

        public, private, priv_adapter, adapters, net_err = self._detect_ethernet_and_vpn_adapters(ethernet_name)
        if net_err and not public and not private:
            return {'error': 'Unable to inspect Windows network adapters: ' + str(net_err)}

        pub_adapter = next((a for a in adapters if a.get('name') == public), None) if public else None
        external_vpn_up = bool(
            pub_adapter and (pub_adapter.get('status') == 2 or pub_adapter.get('has_vpn_route'))
        )
        if self.vpn_state != 'CONNECTED' and not external_vpn_up:
            return {
                'error': 'Wait for Conf / OpenVPN to connect first (no active OpenVPN virtual adapter detected).'
            }
        if not public:
            return {
                'ok': False,
                'error': 'OpenVPN / Conf virtual adapter (TAP / Wintun / DCO) not found. Confirm VPN is connected, then click Refresh Adapters.',
            }
        if not private:
            return {
                'ok': False,
                'error': net_err or 'No physical target Ethernet adapter detected. Connect your Ethernet / USB-LAN adapter and click Refresh Adapters.',
            }

        # Clear any stale or Windows-created ICS binding (e.g. on Broadcom Wi-Fi/uplink) before binding VPN -> Ethernet
        if any(a.get('shared') for a in adapters):
            already_ok = (
                any(a.get('name') == public and a.get('shared') and a.get('sharing_type') == 0 for a in adapters)
                and any(a.get('name') == private and a.get('shared') and a.get('sharing_type') == 1 for a in adapters)
            )
            if not already_ok:
                prev_shared = [f"{a.get('name')}:{a.get('sharing_type')}" for a in adapters if a.get('shared')]
                self._clear_conflicting_ics(public, private)
                notes.append('Cleared previous non-VPN ICS route (' + ', '.join(prev_shared) + ').')

        dns_notes = self._apply_configured_dns_for_sharing(public, adapters=adapters)
        notes.extend(dns_notes)

        self._skip_set_sharing_dns = True
        try:
            sharing = self.set_sharing(public, private)
        finally:
            self._skip_set_sharing_dns = False
        if sharing.get('error'):
            if self._uplink_dns_backups and platform.system() == 'Windows':
                self._restore_uplink_dns_backups()
            return {
                'ok': False,
                'error': 'Conf Ethernet sharing failed: ' + sharing['error'],
            }

        self.ethernet_active = True
        self.ethernet_adapter = private
        self.save_settings({'ethernet_adapter': private})

        eth_ip = '192.168.137.1'
        cable_connected = bool(priv_adapter and priv_adapter.get('status') == 2)
        try:
            time.sleep(0.5)
            post = self.network_connections().get('adapters', [])
            for a in post:
                if a.get('name') == private:
                    if a.get('ipv4'):
                        eth_ip = a['ipv4']
                    cable_connected = (a.get('status') == 2)
                    break
        except Exception:
            pass

        uplinks = [
            f"{a.get('name')} ({a.get('device') or a.get('default_gw') or 'uplink'})"
            for a in adapters
            if a.get('name') not in (public, private) and (
                a.get('has_default_gw') or (a.get('category') or self._classify_adapter(a)) == 'wifi'
            )
        ]
        lines = notes + [
            f'[+] ICS Public (VPN tunnel): {public}',
            f'[+] ICS Private (Ethernet out): {private}',
            f'PC Ethernet gateway IP: {eth_ip} (DHCP subnet 192.168.137.0/24)',
        ]
        if uplinks:
            lines.append('[i] Excluded incoming internet adapter(s): ' + '; '.join(uplinks))
        try:
            ovpn_path = self.profile_map.get(self.vpn_profile) if self.vpn_profile else None
            if ovpn_path and os.path.isfile(ovpn_path):
                txt = open(ovpn_path, 'r', encoding='utf-8', errors='replace').read().lower()
                if 'block-outside-dns' in txt and 'wintun' in str(public).lower():
                    lines.append('[i] Profile uses block-outside-dns with Wintun: if TV fails DNS lookup, set manual DNS on TV or switch profile driver to tap-windows6.')
        except Exception:
            pass
        if cable_connected:
            lines.append('Ethernet cable link is UP. Connected TV/device routes exclusively through OpenVPN -> Wi-Fi.')
        else:
            lines.append('[!] Ethernet port currently shows cable unplugged/device off. Plug Ethernet cable into TV (Auto/DHCP) to route through OpenVPN.')
        self._logger.info('Ethernet Conf sharing active: %s -> %s (ip=%s, link=%s)', public, private, eth_ip, cable_connected)
        return {
            'ok': True,
            'vpn_adapter': public,
            'ethernet_adapter': private,
            'ethernet_ip': eth_ip,
            'cable_connected': cable_connected,
            'output': '\n'.join(lines),
        }

    def stop_vpn_ethernet(self):
        messages = []
        errors = []
        target = self.ethernet_adapter
        if self.app_shared_pair:
            res = self._disable_app_shared_pair()
            if res.get('error'):
                errors.append(res['error'])
            elif res.get('output'):
                messages.append(res['output'])
        elif target and platform.system() == 'Windows':
            payload = base64.b64encode(json.dumps(target).encode('utf-8')).decode('ascii')
            script = (
                f"$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))|ConvertFrom-Json; "
                "$m=New-Object -ComObject HNetCfg.HNetShare; "
                "foreach($c in $m.EnumEveryConnection){"
                "$p=$m.NetConnectionProps.Invoke($c);"
                "$q=$m.INetSharingConfigurationForINetConnection.Invoke($c);"
                "if($q.SharingEnabled -and ($p.Name -eq $t -or $q.SharingConnectionType -eq 0)){$q.DisableSharing()}"
                "}; 'Ethernet ICS route disabled.'"
            )
            try:
                out = self._powershell(script)
                if out:
                    messages.append(out)
            except Exception as exc:
                errors.append(str(exc))

        self.ethernet_active = False
        self.ethernet_adapter = None
        if self._uplink_dns_backups and platform.system() == 'Windows':
            messages.extend(self._restore_uplink_dns_backups())
        if not messages and not errors:
            messages.append('Ethernet Conf sharing stopped.')
        res = {'ok': not errors, 'output': '\n'.join(m for m in messages if m)}
        if errors:
            res['error'] = '; '.join(errors)
        return res

    def start_vpn_hotspot(self, ssid, password):
        if platform.system() != 'Windows':
            return {'error': 'Windows-only hosted network.'}
        if self.vpn_state == 'CONNECTING':
            return {'error': 'Conf engine is still connecting — wait for Initialization Sequence Completed first.'}
        if self.vpn_state != 'CONNECTED':
            pre_adapters = self.network_connections().get('adapters', [])
            pre_pub = self._select_vpn_public_adapter(pre_adapters)
            if not pre_pub or not (pre_pub.get('status') == 2 or pre_pub.get('has_vpn_route')):
                return {'error': 'Wait for Conf / OpenVPN to connect before starting the hotspot.'}
        ssid = str(ssid or '').strip()
        password = str(password or '')
        if not re.fullmatch(r'[A-Za-z0-9 _.-]{1,32}', ssid):
            return {'error': 'SSID must be 1–32 characters: letters, numbers, spaces, dot, underscore or hyphen.'}
        if not re.fullmatch(r'[A-Za-z0-9!@#$%^&*()_+\-=.]{8,63}', password):
            return {'error': 'Hotspot password must be 8–63 ASCII characters (letters, numbers, or common symbols, no spaces or quotes).'}

        win = self.windows_info()
        hosted_supported = self._driver_supports_hosted_network()
        mode_used = None
        start_notes = []

        if self.ethernet_active:
            eth_res = self.stop_vpn_ethernet()
            if eth_res.get('error'):
                return {'error': 'Failed to stop active Ethernet sharing first: ' + eth_res['error']}
            start_notes.append('Stopped previous Ethernet ICS sharing before starting Wi-Fi hotspot.')
        elif self.app_shared_pair:
            self._disable_app_shared_pair()

        try:
            use_hosted = hosted_supported is not False
            if use_hosted:
                rc1, out1, err1 = self._run_netsh(
                    'wlan', 'set', 'hostednetwork',
                    'mode=allow', 'ssid=' + ssid, 'key=' + password, 'keyUsage=persistent',
                    timeout=15,
                )
                if rc1 == 0:
                    rc2, out2, err2 = self._run_netsh('wlan', 'start', 'hostednetwork', timeout=15)
                    if rc2 == 0:
                        mode_used = 'hostednetwork'
                        start_notes.append(out2 or 'Hosted network started.')
                    else:
                        hosted_err = err2 or out2 or 'Hosted Network failed to start.'
                        if win.get('supports_winrt_hotspot'):
                            start_notes.append(f'Hosted Network unavailable ({hosted_err}); switching to Windows Mobile Hotspot…')
                        else:
                            return {'error': hosted_err + ' Check `netsh wlan show drivers` for Hosted network support.'}
                else:
                    hosted_err = err1 or out1 or 'Unable to configure Windows Hosted Network.'
                    if win.get('supports_winrt_hotspot'):
                        start_notes.append(f'Hosted Network unavailable ({hosted_err}); switching to Windows Mobile Hotspot…')
                    else:
                        return {'error': hosted_err}

            if mode_used is None:
                if win.get('supports_winrt_hotspot'):
                    self._start_winrt_hotspot(ssid, password)
                    mode_used = 'winrt'
                    start_notes.append('Windows 10/11 Mobile Hotspot started.')
                else:
                    return {'error': 'Wi-Fi adapter driver reports Hosted Network is unsupported on Windows 8.1. Check driver or use Ethernet + compatible Wi-Fi USB adapter.'}

            self.hotspot_active = True
            self.hotspot_mode = mode_used
            self.save_settings({'hotspot_ssid': ssid})

            public = None
            private = None
            for _ in range(8):
                public, private, _adapters = self._detect_hotspot_and_vpn_adapters()
                if private and public:
                    break
                time.sleep(1)

            if not private or not public:
                return {
                    'ok': False,
                    'hotspot_started': True,
                    'mode': mode_used,
                    'error': (
                        'Wi-Fi hotspot started, but could not reliably identify both Conf and hotspot adapters. '
                        'Confirm Conf is active, then refresh and select the adapters manually.'
                    ),
                }

            if any(a.get('shared') for a in _adapters):
                already_ok = (
                    any(a.get('name') == public and a.get('shared') and a.get('sharing_type') == 0 for a in _adapters)
                    and any(a.get('name') == private and a.get('shared') and a.get('sharing_type') == 1 for a in _adapters)
                )
                if not already_ok:
                    self._clear_conflicting_ics(public, private)
                    start_notes.append('Replaced Windows default uplink ICS with Conf -> hotspot route.')

            dns_notes = self._apply_configured_dns_for_sharing(public, adapters=_adapters)
            start_notes.extend(dns_notes)

            self._skip_set_sharing_dns = True
            try:
                sharing = self.set_sharing(public, private)
            finally:
                self._skip_set_sharing_dns = False
            if sharing.get('error'):
                if self._uplink_dns_backups and platform.system() == 'Windows':
                    self._restore_uplink_dns_backups()
                return {
                    'ok': False,
                    'hotspot_started': True,
                    'mode': mode_used,
                    'error': 'Hotspot is on, but Conf sharing failed: ' + sharing['error'],
                }

            lines = start_notes + [
                'Hosted Wi-Fi started: ' + ssid + f' ({mode_used})',
                'Conf adapter: ' + public,
                'Hotspot adapter: ' + private,
                'ICS routed via Conf. Verify public IP and DNS on a client.',
            ]
            return {'ok': True, 'mode': mode_used, 'output': '\n'.join(lines)}
        except Exception as exc:
            self._logger.warning('start_vpn_hotspot failed: %s', exc)
            return {'error': str(exc)}

    def stop_vpn_hotspot(self):
        messages = []
        errors = []
        if self.app_shared_pair:
            res = self._disable_app_shared_pair()
            if res.get('error'):
                errors.append(res['error'])
            elif res.get('output'):
                messages.append(res['output'])

        if platform.system() == 'Windows':
            if self.hotspot_mode == 'winrt':
                try:
                    messages.append(self._stop_winrt_hotspot())
                except Exception as exc:
                    errors.append(str(exc))
            else:
                try:
                    rc, out, err = self._run_netsh('wlan', 'stop', 'hostednetwork', timeout=15)
                    if out:
                        messages.append(out)
                    if rc != 0 and err:
                        errors.append(err)
                except Exception as exc:
                    errors.append(str(exc))

        self.hotspot_active = False
        self.hotspot_mode = None
        if self._uplink_dns_backups and platform.system() == 'Windows':
            messages.extend(self._restore_uplink_dns_backups())
        res = {'ok': not errors, 'output': '\n'.join(m for m in messages if m)}
        if errors:
            res['error'] = '; '.join(errors)
        return res

    def open_hotspot_settings(self):
        if platform.system() != 'Windows':
            return {'error': 'Windows only.'}
        try:
            os.startfile('ms-settings:network-mobilehotspot')
            return {'ok': True, 'output': 'Windows Mobile Hotspot settings opened. Turn on the hotspot and refresh adapters here.'}
        except Exception as exc:
            return {'error': str(exc)}

    def system_diagnostics(self):
        win = self.windows_info()
        ovpn = self._openvpn_exe()
        hosted = self._driver_supports_hosted_network() if win['is_windows'] else None
        adapters_res = self.network_connections() if win['is_windows'] else {'adapters': []}
        adapters = adapters_res.get('adapters', [])
        vpn_adapters = [
            a for a in adapters
            if self._is_matching_vpn_adapter(a) or (a.get('category') or self._classify_adapter(a)) == 'vpn'
        ]
        active_ics = [a.get('name') for a in adapters if a.get('shared')]
        return {
            'os': win,
            'is_admin': self.is_admin(),
            'openvpn_path': ovpn or '',
            'hosted_network_supported': hosted,
            'vpn_adapters': vpn_adapters,
            'adapters': adapters,
            'adapters_error': adapters_res.get('error', ''),
            'active_ics': active_ics,
            'vpn_state': self.vpn_state,
            'hotspot_active': self.hotspot_active,
            'ethernet_active': self.ethernet_active,
            'ethernet_adapter': self.ethernet_adapter or '',
            'dns_config': self._get_dns_config_from_settings(),
            'log_path': self._log_path(),
            'settings_path': self._settings_path(),
        }

    def close(self):
        if platform.system() == 'Windows' and (self.app_shared_pair or self.ethernet_active):
            try:
                self.stop_vpn_ethernet()
            except Exception:
                pass
        if platform.system() == 'Windows' and self.hotspot_active:
            try:
                if self.hotspot_mode == 'winrt':
                    self._stop_winrt_hotspot()
                else:
                    self._run_netsh('wlan', 'stop', 'hostednetwork', timeout=10)
            except Exception:
                pass
            self.hotspot_active = False
            self.hotspot_mode = None

        with self.vpn_lock:
            self._vpn_stop_requested = True
            proc = self.openvpn_process
            mgmt_port = self._mgmt_port

        if proc and proc.poll() is None:
            self._send_mgmt_sigterm(mgmt_port)
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.terminate()
                    proc.wait(timeout=4)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            with self.vpn_lock:
                self.openvpn_process = None
                self._mgmt_port = None
                self.vpn_state = 'DISCONNECTED'

        if platform.system() == 'Windows' and self._uplink_dns_backups:
            try:
                self._restore_uplink_dns_backups()
            except Exception:
                pass

        self._cleanup_auth_file()
        self.cancel_samsung_auth()
        self.disconnect_tv()
        self.disconnect_samsung()

    # ── Samsung Smart TV (Tizen WS/WSS 8001/8002 + Legacy Orsay 55000) ──────

    SAMSUNG_SEQUENCES = {
        'service_menu_factory': [
            ('KEY_INFO', 0.65, 'Click'),
            ('KEY_FACTORY', 0.25, 'Click'),
        ],
        'service_menu_3speed': [
            ('KEY_FACTORY', 0.65, 'Click'),
            ('KEY_3SPEED', 0.25, 'Click'),
        ],
        'unlock_advanced_tv': [
            ('KEY_0', 0.55, 'Click'),
            ('KEY_0', 0.55, 'Click'),
            ('KEY_9', 0.55, 'Click'),
            ('KEY_8', 0.25, 'Click'),
        ],
        'unlock_advanced_mon': [
            ('KEY_0', 0.55, 'Click'),
            ('KEY_0', 0.55, 'Click'),
            ('KEY_3', 0.55, 'Click'),
            ('KEY_8', 0.25, 'Click'),
        ],
        'hospitality_step1': [
            ('KEY_MUTE', 0.7, 'Click'),
            ('KEY_1', 0.7, 'Click'),
            ('KEY_1', 0.7, 'Click'),
            ('KEY_9', 0.7, 'Click'),
            ('KEY_ENTER', 0.35, 'Click'),
        ],
        'hospitality_step2': [
            ('KEY_1', 0.7, 'Click'),
            ('KEY_2', 0.7, 'Click'),
            ('KEY_3', 0.7, 'Click'),
            ('KEY_4', 0.7, 'Click'),
            ('KEY_UP', 0.35, 'Click'),
        ],
        'hospitality_step3': [
            ('KEY_VOLUP', 5.1, 'Press'),
            ('KEY_VOLUP', 0.3, 'Release'),
        ],
        'open_option': [
            ('KEY_ENTER', 0.25, 'Click'),
        ],
        'factory_reset_from_option': [
            ('KEY_ENTER', 0.9, 'Click'),
            ('KEY_ENTER', 0.25, 'Click'),
        ],
        'factory_reset_from_menu': [
            ('KEY_ENTER', 0.9, 'Click'),
            ('KEY_ENTER', 0.25, 'Click'),
        ],
        'factory_reset_current': [
            ('KEY_ENTER', 0.7, 'Click'),
            ('KEY_ENTER', 0.25, 'Click'),
        ],
        'smart_hub_region_unlock': [
            ('KEY_MUTE', 0.45, 'Click'),
            ('KEY_RETURN', 0.45, 'Click'),
            ('KEY_VOLUP', 0.45, 'Click'),
            ('KEY_CHUP', 0.45, 'Click'),
            ('KEY_MUTE', 0.25, 'Click'),
        ],
        'exit_service_menu': [
            ('KEY_FACTORY', 2.8, 'Click'),
            ('KEY_FACTORY', 0.25, 'Click'),
        ],
        'exit_factory_loop': [
            ('KEY_FACTORY', 0.25, 'Click'),
        ],
        'reboot_tv': [
            ('KEY_POWER', 0.25, 'Click'),
        ],
    }

    def samsung_local_sets(self):
        return list(SAMSUNG_LOCAL_SETS)

    def discover_samsung(self):
        try:
            devices = discover_samsung_tvs(timeout=1.6)
            return {'ok': True, 'devices': devices}
        except Exception as exc:
            return {'ok': False, 'devices': [], 'error': str(exc)}

    def _save_samsung_token(self, ip, token):
        if not token:
            return
        settings = self.load_settings()
        tokens = settings.get('samsung_tokens')
        if not isinstance(tokens, dict):
            tokens = {}
        tokens[ip] = str(token)
        self.save_settings({'samsung_tokens': tokens})

    def _clear_samsung_token(self, ip):
        settings = self.load_settings()
        tokens = settings.get('samsung_tokens')
        if isinstance(tokens, dict) and ip in tokens:
            tokens.pop(ip, None)
            self.save_settings({'samsung_tokens': tokens})

    def connect_samsung(self, ip, port='auto'):
        try:
            clean_ip = self._normalize_ip(ip)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}

        port_mode = str(port or 'auto').strip().lower()
        info = fetch_samsung_device_info(clean_ip, timeout=2.5)

        if port_mode in ('8002', 'wss'):
            candidates = [8002]
        elif port_mode in ('8001', 'ws'):
            candidates = [8001]
        elif port_mode in ('55000', 'legacy'):
            candidates = [55000]
        else:
            if info:
                if info.get('token_auth') or info.get('port') == 8002:
                    candidates = [8002, 8001]
                else:
                    candidates = [8001, 8002]
            else:
                candidates = [8002, 8001, 55000]

        settings = self.load_settings()
        tokens = settings.get('samsung_tokens') if isinstance(settings.get('samsung_tokens'), dict) else {}
        saved_token = tokens.get(clean_ip)

        last_err = None
        chosen_client = None
        chosen_port = None
        chosen_proto = None
        token_saved = False

        for cand_port in candidates:
            if cand_port == 55000:
                try:
                    client = SamsungLegacyClient(clean_ip, port=55000, timeout=6.0)
                    client.connect(auth_timeout=15.0)
                    chosen_client = client
                    chosen_port = 55000
                    chosen_proto = 'legacy-55000'
                    break
                except Exception as exc:
                    last_err = exc
                    continue

            use_ssl = (cand_port == 8002)
            # First try saved token (fast timeout), then fall back to fresh pairing if rejected.
            attempts = [saved_token, None] if saved_token else [None]
            for idx, tok in enumerate(attempts):
                auth_timeout = 6.0 if tok else 30.0
                client = SamsungWsClient(
                    clean_ip,
                    port=cand_port,
                    token=tok,
                    use_ssl=use_ssl,
                    timeout=8.0,
                )
                try:
                    new_tok = client.connect(auth_timeout=auth_timeout)
                    is_first_pairing = (tok is None)
                    if new_tok:
                        self._save_samsung_token(clean_ip, new_tok)
                        token_saved = True
                        if is_first_pairing:
                            # Reconnect once with the issued token so subsequent keys are authorized.
                            client.close()
                            client2 = SamsungWsClient(
                                clean_ip,
                                port=cand_port,
                                token=new_tok,
                                use_ssl=use_ssl,
                                timeout=8.0,
                            )
                            client2.connect(auth_timeout=6.0)
                            client = client2
                    chosen_client = client
                    chosen_port = cand_port
                    chosen_proto = 'wss-8002' if use_ssl else 'ws-8001'
                    break
                except PermissionError as exc:
                    last_err = exc
                    client.close()
                    if tok:
                        self._clear_samsung_token(clean_ip)
                        continue
                    break
                except Exception as exc:
                    last_err = exc
                    client.close()
                    if idx == 0 and tok and 'timed out' in str(exc).lower():
                        self._clear_samsung_token(clean_ip)
                        continue
                    break

            if chosen_client is not None:
                break

        if chosen_client is None:
            return {
                'ok': False,
                'error': f'Samsung TV connection failed ({clean_ip}): {last_err}',
            }

        with self._samsung_lock:
            if self.samsung_client is not None:
                try:
                    self.samsung_client.close()
                except Exception:
                    pass
            self.samsung_client = chosen_client
            self.samsung_ip = clean_ip
            self.samsung_port = chosen_port
            self.samsung_protocol = chosen_proto
            self.samsung_info = info or {
                'name': 'Samsung TV',
                'model': 'Legacy' if chosen_port == 55000 else 'Smart TV',
                'os': 'Orsay' if chosen_port == 55000 else 'Tizen',
                'power_state': 'on',
                'country': '',
                'language': '',
                'mac': '',
            }

        self.save_settings({'samsung_ip': clean_ip, 'samsung_port': port_mode})
        tv_name = self.samsung_info.get('name', 'Samsung TV')
        tv_model = self.samsung_info.get('model', '')
        self._logger.info('Connected Samsung TV: %s (%s) via %s', clean_ip, tv_model, chosen_proto)
        return {
            'ok': True,
            'ip': clean_ip,
            'port': chosen_port,
            'protocol': chosen_proto,
            'info': self.samsung_info,
            'token_saved': token_saved or bool(saved_token),
            'output': f'Connected to {tv_name} ({tv_model}) at {clean_ip} via {chosen_proto}',
        }

    def disconnect_samsung(self):
        with self._samsung_lock:
            if self.samsung_client is not None:
                try:
                    self.samsung_client.close()
                except Exception:
                    pass
                self.samsung_client = None
            self.samsung_ip = None
            self.samsung_port = None
            self.samsung_protocol = None
            self.samsung_info = None
        return {'ok': True}

    def _ensure_samsung_connected(self):
        with self._samsung_lock:
            if self.samsung_client is not None and self.samsung_client.is_connected():
                return self.samsung_client
            ip = self.samsung_ip
            port = str(self.samsung_port or 'auto')
        if ip:
            res = self.connect_samsung(ip, port=port)
            if res.get('ok'):
                with self._samsung_lock:
                    return self.samsung_client
        return None

    def samsung_send_key(self, key, cmd='Click'):
        clean_key = str(key or '').strip().upper()
        if not re.match(r'^KEY_[A-Z0-9_]+$', clean_key):
            return {'ok': False, 'error': f'Invalid Samsung key name: {key}'}
        if cmd not in ('Click', 'Press', 'Release'):
            return {'ok': False, 'error': f'Invalid key cmd: {cmd}'}

        client = self._ensure_samsung_connected()
        if client is None:
            return {'ok': False, 'error': 'Samsung TV is not connected'}

        for attempt in range(2):
            try:
                with self._samsung_lock:
                    if not self.samsung_client:
                        raise ConnectionError('Samsung TV disconnected')
                    self.samsung_client.send_key(clean_key, cmd=cmd)
                return {'ok': True, 'key': clean_key, 'cmd': cmd, 'output': f'Sent {clean_key} ({cmd})'}
            except Exception as exc:
                if attempt == 0 and self.samsung_ip:
                    # TV idle timeout closes WS; silently reopen with saved token once.
                    ip = self.samsung_ip
                    port = str(self.samsung_port or 'auto')
                    res = self.connect_samsung(ip, port=port)
                    if res.get('ok'):
                        continue
                return {'ok': False, 'error': f'Samsung send_key failed ({clean_key}): {exc}'}
        return {'ok': False, 'error': 'Samsung send_key failed'}

    def samsung_send_sequence(self, steps):
        if not steps:
            return {'ok': False, 'error': 'Empty key sequence'}
        sent = []
        for step in steps:
            if len(step) == 2:
                key, delay = step
                cmd = 'Click'
            else:
                key, delay, cmd = step
            res = self.samsung_send_key(key, cmd=cmd)
            if not res.get('ok'):
                res['sent'] = sent
                return res
            sent.append(f'{key}:{cmd}' if cmd != 'Click' else key)
            if delay and delay > 0:
                time.sleep(float(delay))
        return {
            'ok': True,
            'sent': sent,
            'output': 'Sequence sent: ' + ' -> '.join(sent),
        }

    def samsung_action(self, action_id):
        key = str(action_id or '').strip()
        seq = self.SAMSUNG_SEQUENCES.get(key)
        if not seq:
            return {'ok': False, 'error': f'Unknown Samsung action: {action_id}'}
        res = self.samsung_send_sequence(seq)
        if res.get('ok'):
            self._logger.info('Samsung action %s: %s', key, res.get('output'))
        return res

    # ── Samsung Tizen SDB (26101), Certificates & Belet App ─────────────────

    def _sam_log(self, msg):
        with self._sam_log_lock:
            self._sam_logs.append(str(msg))
            if len(self._sam_logs) > 300:
                self._sam_logs = self._sam_logs[-300:]

    def read_samsung_logs(self):
        with self._sam_log_lock:
            out = list(self._sam_logs)
            self._sam_logs.clear()
        return {'logs': out}

    def _resolve_samsung_ip(self, ip=None):
        cand = str(ip or '').strip() or self.samsung_ip or str(self.load_settings().get('samsung_ip') or '').strip()
        clean = self._normalize_ip(cand)
        if not clean:
            raise ValueError('Enter a valid Samsung TV IP address.')
        return clean

    def _samsung_cert_manager(self):
        return SamsungCertManager(self._data_dir())

    def _belet_wgt_icon(self):
        for rel in ('assets/belet_large_icon.png', 'assets/belet_icon.png'):
            p = resource(rel)
            if os.path.isfile(p):
                with open(p, 'rb') as f:
                    return f.read()
        return None

    def _alemtv_wgt_icon(self):
        for rel in ('assets/alemtv_large_icon.png', 'assets/alemtv_icon.png'):
            p = resource(rel)
            if os.path.isfile(p):
                with open(p, 'rb') as f:
                    return f.read()
        return None

    def get_local_ip_for_tv(self, tv_ip=None):
        target = str(tv_ip or '').strip() or self.samsung_ip or '192.168.1.101'
        return {'ok': True, 'pc_ip': _tizen_local_ip(target)}

    def cancel_samsung_auth(self):
        self._samsung_auth_stop.set()
        return {'ok': True}

    def check_samsung_dev_mode(self, ip=None):
        try:
            clean_ip = self._resolve_samsung_ip(ip)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}

        pc_ip = _tizen_local_ip(clean_ip)
        info = fetch_samsung_device_info(clean_ip, timeout=2.2) or {}
        dev_mode = info.get('developer_mode', '')
        dev_ip = info.get('developer_ip', '')
        model = info.get('model') or 'Samsung Smart TV'
        cert_mgr = self._samsung_cert_manager()

        lines = [f'[INFO] PC LAN IP: {pc_ip} | TV: {clean_ip} ({model})']
        if dev_mode or dev_ip:
            lines.append(f'[API] developerMode={dev_mode or "?"} developerIP={dev_ip or "?"}')
            if dev_ip and dev_ip != pc_ip and dev_ip != '0.0.0.0':
                lines.append(f'[!] TV Host IP ({dev_ip}) differs from this PC ({pc_ip}). Update Host IP on TV!')

        try:
            with SdbClient(clean_ip, timeout=6.0) as sdb:
                caps = sdb.capability()
                duid = sdb.get_duid(caps=caps)
                platform_ver = caps.get('platform_version', '')
                sdk_toolpath = sdb.resolve_sdk_toolpath(caps)
                applist = ''
                try:
                    applist = sdb.list_apps()
                except Exception:
                    pass
                belet_installed = BELET_PKG_ID in applist
                saved = cert_mgr.find_saved_samsung_certs(duid)
                lines.append(f'[+] SDB 26101 OPEN | DUID={duid} | Tizen={platform_ver or "?"} | toolpath={sdk_toolpath}')
                if saved:
                    lines.append(f'[+] Samsung certificate ready: {saved["source"]}')
                else:
                    lines.append('[i] No Samsung certificate cached yet for this DUID (will auto-create or use SDK on Tizen < 7).')
                if belet_installed:
                    lines.append(f'[+] Belet ({BELET_PKG_ID}) is currently installed on this TV.')
                return {
                    'ok': True,
                    'sdb_open': True,
                    'ip': clean_ip,
                    'pc_ip': pc_ip,
                    'duid': duid,
                    'platform_version': platform_ver,
                    'sdk_toolpath': sdk_toolpath,
                    'developer_mode': dev_mode,
                    'developer_ip': dev_ip,
                    'has_cert': bool(saved),
                    'cert_source': saved['source'] if saved else '',
                    'belet_installed': belet_installed,
                    'output': '\n'.join(lines),
                }
        except Exception as exc:
            err_s = str(exc)
            lines.append(f'[!] SDB 26101 unavailable: {err_s}')
            if 'closed' in err_s.lower() or 'clse' in err_s.lower():
                lines.append(
                    f'[i] TV accepted TCP but closed the SDB stream — check Host PC IP on TV matches "{pc_ip}", '
                    f'close any competing Tizen Studio/sdb process on PC, then reboot TV (hold Power 5s).'
                )
            else:
                lines.append(
                    f'[i] On Samsung TV: Apps -> press 1 2 3 4 5 -> Developer Mode: ON -> '
                    f'Host PC IP: {pc_ip} -> OK -> hold Power 5s to reboot TV.'
                )
            return {
                'ok': False,
                'sdb_open': False,
                'ip': clean_ip,
                'pc_ip': pc_ip,
                'developer_mode': dev_mode,
                'developer_ip': dev_ip,
                'error': err_s,
                'output': '\n'.join(lines),
            }

    def samsung_auth_create_cert(self, ip=None, force=False, timeout=240):
        try:
            clean_ip = self._resolve_samsung_ip(ip)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}

        cert_mgr = self._samsung_cert_manager()
        self._samsung_auth_stop.clear()
        lines = []
        try:
            with SdbClient(clean_ip, timeout=8.0) as sdb:
                caps = sdb.capability()
                duid = sdb.get_duid(caps=caps)
            lines.append(f'[+] SDB connected · DUID: {duid}')
            self._sam_log(f'[CERT] TV DUID: {duid}')

            if not force:
                existing = cert_mgr.find_saved_samsung_certs(duid)
                if existing:
                    a_sum = _format_chain_summary(existing.get('author_certs'))
                    d_sum = _format_chain_summary(existing.get('dist_certs'))
                    msg = f'[+] Valid Samsung certificate already cached for DUID {duid} ({existing["source"]}).'
                    lines.append(msg)
                    lines.append(f'[+] Author chain: {a_sum}')
                    lines.append(f'[+] Distributor chain: {d_sum}')
                    self._sam_log(f'[CERT] Cached chain verified · Author ({a_sum}) | Dist ({d_sum})')
                    return {
                        'ok': True,
                        'duid': duid,
                        'already_exists': True,
                        'source': existing['source'],
                        'output': '\n'.join(lines),
                    }

            self._sam_log('[CERT] Preparing Samsung VD CA certificates...')

            def on_url(url):
                self._sam_log('[CERT] Browser opened for Samsung Account sign-in (port 4794):')
                self._sam_log('[CERT] ' + url)

            bundle = cert_mgr.sign_in_and_provision(
                duid,
                open_browser=True,
                timeout=timeout,
                stop_event=self._samsung_auth_stop,
                url_cb=on_url,
            )
            a_sum = _format_chain_summary(bundle.get('author_certs'))
            d_sum = _format_chain_summary(bundle.get('dist_certs'))
            lines.append(f'[+] Samsung certificate created & saved to {bundle["source"]}')
            lines.append(f'[+] Author chain: {a_sum}')
            lines.append(f'[+] Distributor chain: {d_sum}')
            self._sam_log(f'[CERT] Provisioned Author ({a_sum}) | Dist ({d_sum})')
            return {
                'ok': True,
                'duid': duid,
                'source': bundle['source'],
                'output': '\n'.join(lines),
            }
        except Exception as exc:
            lines.append(f'[!] Certificate creation failed: {exc}')
            return {
                'ok': False,
                'error': str(exc),
                'output': '\n'.join(lines),
            }

    def install_samsung_belet(self, ip=None, target_url=BELET_URL, cert_mode='auto'):
        try:
            clean_ip = self._resolve_samsung_ip(ip)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}

        pc_ip = _tizen_local_ip(clean_ip)
        cert_mgr = self._samsung_cert_manager()
        self._samsung_auth_stop.clear()
        lines = [f'[+] Preparing Belet (.wgt) for Samsung TV {clean_ip} (PC IP {pc_ip})...']
        try:
            with SdbClient(clean_ip, timeout=10.0) as sdb:
                caps = sdb.capability()
                duid = sdb.get_duid(caps=caps)
                platform_ver = caps.get('platform_version', '')
                sdk_toolpath = sdb.resolve_sdk_toolpath(caps)
                lines.append(f'[+] SDB 26101 connected · DUID={duid} · Tizen={platform_ver or "?"} · path={sdk_toolpath}')
                self._sam_log(f'[SDB] Connected to {clean_ip} (DUID={duid}, Tizen={platform_ver or "?"})')

                ver_num = None
                m_ver = re.match(r'(\d+(?:\.\d+)?)', str(platform_ver or ''))
                if m_ver:
                    try:
                        ver_num = float(m_ver.group(1))
                    except ValueError:
                        ver_num = None

                bundle = cert_mgr.find_saved_samsung_certs(duid)
                used_sdk_local = False

                if bundle is not None:
                    lines.append(f'[+] Using saved Samsung certificate: {bundle["source"]}')
                else:
                    if cert_mode != 'samsung' and ver_num is not None and ver_num < 7.0:
                        try:
                            self._sam_log('[CERT] Tizen < 7.0 detected — trying zero-login Tizen SDK local certificate...')
                            bundle = cert_mgr.ensure_tizen_sdk_certs()
                            used_sdk_local = True
                            lines.append('[+] Using Tizen SDK local certificate (no Samsung login required)')
                        except Exception as sdk_exc:
                            lines.append(f'[i] Tizen SDK local cert skipped ({sdk_exc}); using Samsung sign-in...')

                    if bundle is None:
                        self._sam_log('[CERT] Samsung Account sign-in required for this TV. Opening browser...')
                        bundle = cert_mgr.sign_in_and_provision(
                            duid,
                            open_browser=True,
                            timeout=240.0,
                            stop_event=self._samsung_auth_stop,
                            url_cb=lambda u: self._sam_log('[CERT] Sign-in URL: ' + u),
                        )
                        lines.append(f'[+] Samsung certificate saved: {bundle["source"]}')

                # Return to the pre-proxy Belet package: older Samsung firmware opens its
                # TV browser to the Belet site directly instead of navigating to localhost.
                enable_service = False
                remote_wgt = f'{sdk_toolpath}/{BELET_PKG_ID}.wgt'

                def _build_sign_push(use_service, active_bundle):
                    u_wgt = build_belet_wgt(
                        target_url=target_url,
                        pkg_id=BELET_PKG_ID,
                        app_name='Belet',
                        icon_bytes=self._belet_wgt_icon(),
                        enable_service=use_service,
                        tv_ip=clean_ip,
                    )
                    s_wgt = sign_wgt_bytes(
                        u_wgt,
                        active_bundle['author_key'],
                        active_bundle['author_certs'],
                        active_bundle['dist_key'],
                        active_bundle['dist_certs'],
                    )
                    verify_wgt_signatures(s_wgt)
                    a_sum = _format_chain_summary(active_bundle.get('author_certs'))
                    d_sum = _format_chain_summary(active_bundle.get('dist_certs'))
                    lines.append(f'[+] Signed {BELET_PKG_ID}.wgt ({len(s_wgt) // 1024} KB, service={"on" if use_service else "fallback"})')
                    lines.append(f'[+] Author chain: {a_sum} | Distributor chain: {d_sum}')
                    self._sam_log(f'[WGT] Signed BeletTV001.wgt ({len(s_wgt) // 1024} KB) · Author ({a_sum}) | Dist ({d_sum}). Uploading via SDB...')
                    sdb.push_bytes(s_wgt, remote_wgt)
                    lines.append(f'[+] Uploaded to {remote_wgt}')
                    return u_wgt

                unsigned_wgt = _build_sign_push(enable_service, bundle)
                self._sam_log(f'[SDB] Installing {BELET_PKG_ID} on TV...')

                install_out = sdb.install_wgt(BELET_PKG_ID, remote_wgt, timeout=120.0)
                if install_out.strip():
                    lines.extend(install_out.strip().splitlines())
                diag = diagnose_install_output(install_out, BELET_PKG_ID)

                if enable_service and diag['status'] in ('manifest_error', 'api_mismatch'):
                    reason_line = (install_out or '').strip().splitlines()[-1] if (install_out or '').strip() else diag['status']
                    self._sam_log(f'[WGT] TV rejected service manifest ({diag["status"]}: {reason_line}) — rebuilding pure-WebView fallback .wgt...')
                    lines.append(f'[i] TV firmware rejected background service manifest ({reason_line}); retrying pure-WebView package...')
                    enable_service = False
                    unsigned_wgt = _build_sign_push(False, bundle)
                    install_out = sdb.install_wgt(BELET_PKG_ID, remote_wgt, timeout=120.0)
                    if install_out.strip():
                        lines.extend(install_out.strip().splitlines())
                    diag = diagnose_install_output(install_out, BELET_PKG_ID)

                if diag['status'] == 'cert_mismatch':
                    self._sam_log('[SDB] Existing Belet has different cert (118012). Uninstalling old copy and retrying...')
                    lines.append('[i] Removing previous Belet installation (certificate mismatch) and retrying...')
                    try:
                        sdb.uninstall_app(BELET_PKG_ID)
                    except Exception:
                        pass
                    install_out = sdb.install_wgt(BELET_PKG_ID, remote_wgt, timeout=120.0)
                    if install_out.strip():
                        lines.extend(install_out.strip().splitlines())
                    diag = diagnose_install_output(install_out, BELET_PKG_ID)

                elif diag['status'] == 'cert_invalid_chain' and (
                    used_sdk_local or (bundle.get('source') and os.path.abspath(str(bundle['source'])) != os.path.abspath(cert_mgr.cert_dir))
                ):
                    self._sam_log('[CERT] TV rejected cert chain (118/-12). Launching Samsung Account sign-in...')
                    lines.append('[i] TV rejected previous certificate chain; provisioning fresh Samsung certificate...')
                    bundle = cert_mgr.sign_in_and_provision(
                        duid,
                        open_browser=True,
                        timeout=240.0,
                        stop_event=self._samsung_auth_stop,
                        url_cb=lambda u: self._sam_log('[CERT] Sign-in URL: ' + u),
                    )
                    signed_wgt = sign_wgt_bytes(
                        unsigned_wgt,
                        bundle['author_key'],
                        bundle['author_certs'],
                        bundle['dist_key'],
                        bundle['dist_certs'],
                    )
                    verify_wgt_signatures(signed_wgt)
                    sdb.push_bytes(signed_wgt, remote_wgt)
                    install_out = sdb.install_wgt(BELET_PKG_ID, remote_wgt, timeout=120.0)
                    if install_out.strip():
                        lines.extend(install_out.strip().splitlines())
                    diag = diagnose_install_output(install_out, BELET_PKG_ID)

                applist_out = ''
                try:
                    applist_out = sdb.list_apps()
                except Exception:
                    pass

                installed_ok = (diag['status'] == 'ok') or (BELET_PKG_ID in applist_out)
                if not installed_ok:
                    # Some Tizen firmwares expect full app_id (<pkg>.<name>) instead of 10-char pkg_id
                    try:
                        self._sam_log(f'[SDB] Retrying vd_appinstall with app ID {BELET_APP_ID}...')
                        install_out_app = sdb.install_wgt(BELET_APP_ID, remote_wgt, timeout=120.0)
                        if install_out_app.strip():
                            lines.extend(install_out_app.strip().splitlines())
                        diag_app = diagnose_install_output(install_out_app, BELET_PKG_ID)
                        if diag_app['status'] == 'ok':
                            diag = diag_app
                            installed_ok = True
                        else:
                            try:
                                applist_out = sdb.list_apps()
                                if BELET_PKG_ID in applist_out:
                                    installed_ok = True
                            except Exception:
                                pass
                    except Exception:
                        pass

                if not installed_ok:
                    reason = diag.get('reason') or 'Samsung TV did not complete Belet installation.'
                    if diag['status'] in ('cert_not_yet_valid', 'cert_expired'):
                        reason += ' Check TV Date/Time & internet connection.'
                    elif diag['status'] == 'pkg_blocked':
                        reason += ' Uninstall any old Belet copy from TV Apps or run Smart Hub reset.'
                    return {
                        'ok': False,
                        'duid': duid,
                        'diagnostic': diag['status'],
                        'error': reason,
                        'output': '\n'.join(lines),
                    }

                lines.append(f'[+] Belet ({BELET_APP_ID}) installed successfully!')
                launch_note = ''
                try:
                    launch_note = sdb.launch_app(BELET_APP_ID)
                    lines.append(f'[+] Launched {BELET_APP_ID}: {launch_note}')
                except Exception as lex:
                    lines.append(f'[i] Installed OK (auto-launch note: {lex})')

                return {
                    'ok': True,
                    'duid': duid,
                    'app_id': BELET_APP_ID,
                    'pkg_id': BELET_PKG_ID,
                    'output': '\n'.join(lines),
                }
        except Exception as exc:
            err = str(exc)
            if '26101' in err or 'timed out' in err.lower() or 'refused' in err.lower():
                lines.append(
                    f'[i] Enable Developer Mode on Samsung TV: Apps -> 1 2 3 4 5 -> Developer Mode ON -> '
                    f'Host PC IP: {pc_ip} -> hold Power 5s to reboot TV.'
                )
            lines.append(f'[!] Belet install failed: {err}')
            return {'ok': False, 'error': err, 'output': '\n'.join(lines)}

    def launch_samsung_belet(self, ip=None):
        try:
            clean_ip = self._resolve_samsung_ip(ip)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}
        try:
            with SdbClient(clean_ip, timeout=10.0) as sdb:
                reply = sdb.launch_app(BELET_APP_ID)
            return {
                'ok': True,
                'app_id': BELET_APP_ID,
                'output': f'[+] Launched Belet ({BELET_APP_ID}): {reply}',
            }
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'output': f'[!] Launch failed: {exc}'}

    def remove_samsung_belet(self, ip=None):
        try:
            clean_ip = self._resolve_samsung_ip(ip)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}
        try:
            with SdbClient(clean_ip, timeout=10.0) as sdb:
                reply = sdb.uninstall_app(BELET_PKG_ID)
            return {
                'ok': True,
                'pkg_id': BELET_PKG_ID,
                'output': f'[+] Removed Belet ({BELET_PKG_ID}): {reply.strip() or "done"}',
            }
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'output': f'[!] Uninstall failed: {exc}'}

    def build_samsung_alemtv_wgt(self, target_url=ALEMTV_DEFAULT_PORTAL):
        clean_url = self._normalize_portal_url(target_url)
        return build_alemtv_wgt(
            target_url=clean_url,
            pkg_id=ALEMTV_PKG_ID,
            app_name='AlemTV',
            icon_bytes=self._alemtv_wgt_icon(),
        )

    def install_samsung_alemtv(self, ip=None, target_url=ALEMTV_DEFAULT_PORTAL, cert_mode='auto'):
        try:
            clean_ip = self._resolve_samsung_ip(ip)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}

        clean_url = self._normalize_portal_url(target_url)
        pc_ip = _tizen_local_ip(clean_ip)
        cert_mgr = self._samsung_cert_manager()
        self._samsung_auth_stop.clear()
        lines = [f'[+] Preparing Alem TV (.wgt) for Samsung TV {clean_ip} (PC IP {pc_ip}, portal {clean_url})...']
        try:
            with SdbClient(clean_ip, timeout=10.0) as sdb:
                caps = sdb.capability()
                duid = sdb.get_duid(caps=caps)
                platform_ver = caps.get('platform_version', '')
                sdk_toolpath = sdb.resolve_sdk_toolpath(caps)
                lines.append(f'[+] SDB 26101 connected · DUID={duid} · Tizen={platform_ver or "?"} · path={sdk_toolpath}')
                self._sam_log(f'[SDB] Connected to {clean_ip} (DUID={duid}, Tizen={platform_ver or "?"})')

                ver_num = None
                m_ver = re.match(r'(\d+(?:\.\d+)?)', str(platform_ver or ''))
                if m_ver:
                    try:
                        ver_num = float(m_ver.group(1))
                    except ValueError:
                        ver_num = None

                bundle = cert_mgr.find_saved_samsung_certs(duid)
                used_sdk_local = False

                if bundle is not None:
                    lines.append(f'[+] Using saved Samsung certificate: {bundle["source"]}')
                else:
                    if cert_mode != 'samsung' and ver_num is not None and ver_num < 7.0:
                        try:
                            self._sam_log('[CERT] Tizen < 7.0 detected — trying zero-login Tizen SDK local certificate...')
                            bundle = cert_mgr.ensure_tizen_sdk_certs()
                            used_sdk_local = True
                            lines.append('[+] Using Tizen SDK local certificate (no Samsung login required)')
                        except Exception as sdk_exc:
                            lines.append(f'[i] Tizen SDK local cert skipped ({sdk_exc}); using Samsung sign-in...')

                    if bundle is None:
                        self._sam_log('[CERT] Samsung Account sign-in required for this TV. Opening browser...')
                        bundle = cert_mgr.sign_in_and_provision(
                            duid,
                            open_browser=True,
                            timeout=240.0,
                            stop_event=self._samsung_auth_stop,
                            url_cb=lambda u: self._sam_log('[CERT] Sign-in URL: ' + u),
                        )
                        lines.append(f'[+] Samsung certificate saved: {bundle["source"]}')

                remote_wgt = f'{sdk_toolpath}/{ALEMTV_PKG_ID}.wgt'
                u_wgt = self.build_samsung_alemtv_wgt(target_url=clean_url)
                s_wgt = sign_wgt_bytes(
                    u_wgt,
                    bundle['author_key'],
                    bundle['author_certs'],
                    bundle['dist_key'],
                    bundle['dist_certs'],
                )
                verify_wgt_signatures(s_wgt)
                a_sum = _format_chain_summary(bundle.get('author_certs'))
                d_sum = _format_chain_summary(bundle.get('dist_certs'))
                lines.append(f'[+] Signed {ALEMTV_PKG_ID}.wgt ({len(s_wgt) // 1024} KB)')
                lines.append(f'[+] Author chain: {a_sum} | Distributor chain: {d_sum}')
                self._sam_log(f'[WGT] Signed {ALEMTV_PKG_ID}.wgt ({len(s_wgt) // 1024} KB) · Author ({a_sum}) | Dist ({d_sum}). Uploading via SDB...')
                sdb.push_bytes(s_wgt, remote_wgt)
                lines.append(f'[+] Uploaded to {remote_wgt}')

                self._sam_log(f'[SDB] Installing {ALEMTV_PKG_ID} on TV...')
                install_out = sdb.install_wgt(ALEMTV_PKG_ID, remote_wgt, timeout=120.0)
                if install_out.strip():
                    lines.extend(install_out.strip().splitlines())
                diag = diagnose_install_output(install_out, ALEMTV_PKG_ID)

                if diag['status'] == 'cert_mismatch':
                    self._sam_log(f'[SDB] Existing {ALEMTV_PKG_ID} has different cert. Uninstalling old copy and retrying...')
                    lines.append(f'[i] Removing previous {ALEMTV_PKG_ID} installation (certificate mismatch) and retrying...')
                    try:
                        sdb.uninstall_app(ALEMTV_PKG_ID)
                    except Exception:
                        pass
                    install_out = sdb.install_wgt(ALEMTV_PKG_ID, remote_wgt, timeout=120.0)
                    if install_out.strip():
                        lines.extend(install_out.strip().splitlines())
                    diag = diagnose_install_output(install_out, ALEMTV_PKG_ID)

                elif diag['status'] == 'cert_invalid_chain' and (
                    used_sdk_local or (bundle.get('source') and os.path.abspath(str(bundle['source'])) != os.path.abspath(cert_mgr.cert_dir))
                ):
                    self._sam_log('[CERT] TV rejected cert chain. Launching Samsung Account sign-in...')
                    lines.append('[i] TV rejected previous certificate chain; provisioning fresh Samsung certificate...')
                    bundle = cert_mgr.sign_in_and_provision(
                        duid,
                        open_browser=True,
                        timeout=240.0,
                        stop_event=self._samsung_auth_stop,
                        url_cb=lambda u: self._sam_log('[CERT] Sign-in URL: ' + u),
                    )
                    signed_wgt = sign_wgt_bytes(
                        u_wgt,
                        bundle['author_key'],
                        bundle['author_certs'],
                        bundle['dist_key'],
                        bundle['dist_certs'],
                    )
                    verify_wgt_signatures(signed_wgt)
                    sdb.push_bytes(signed_wgt, remote_wgt)
                    install_out = sdb.install_wgt(ALEMTV_PKG_ID, remote_wgt, timeout=120.0)
                    if install_out.strip():
                        lines.extend(install_out.strip().splitlines())
                    diag = diagnose_install_output(install_out, ALEMTV_PKG_ID)

                applist_out = ''
                try:
                    applist_out = sdb.list_apps()
                except Exception:
                    pass

                installed_ok = (diag['status'] == 'ok') or (ALEMTV_PKG_ID in applist_out)
                if not installed_ok:
                    try:
                        self._sam_log(f'[SDB] Retrying vd_appinstall with app ID {ALEMTV_APP_ID}...')
                        install_out_app = sdb.install_wgt(ALEMTV_APP_ID, remote_wgt, timeout=120.0)
                        if install_out_app.strip():
                            lines.extend(install_out_app.strip().splitlines())
                        diag_app = diagnose_install_output(install_out_app, ALEMTV_PKG_ID)
                        if diag_app['status'] == 'ok':
                            diag = diag_app
                            installed_ok = True
                        else:
                            try:
                                applist_out = sdb.list_apps()
                                if ALEMTV_PKG_ID in applist_out:
                                    installed_ok = True
                            except Exception:
                                pass
                    except Exception:
                        pass

                if not installed_ok:
                    reason = diag.get('reason') or 'Samsung TV did not complete Alem TV installation.'
                    if diag['status'] in ('cert_not_yet_valid', 'cert_expired'):
                        reason += ' Check TV Date/Time & internet connection.'
                    elif diag['status'] == 'pkg_blocked':
                        reason += ' Uninstall any old Alem TV copy from TV Apps or run Smart Hub reset.'
                    return {
                        'ok': False,
                        'duid': duid,
                        'diagnostic': diag['status'],
                        'error': reason,
                        'output': '\n'.join(lines),
                    }

                lines.append(f'[+] Alem TV ({ALEMTV_APP_ID}) installed successfully!')
                launch_note = ''
                try:
                    launch_note = sdb.launch_app(ALEMTV_APP_ID)
                    lines.append(f'[+] Launched {ALEMTV_APP_ID}: {launch_note}')
                except Exception as lex:
                    lines.append(f'[i] Installed OK (auto-launch note: {lex})')

                return {
                    'ok': True,
                    'duid': duid,
                    'app_id': ALEMTV_APP_ID,
                    'pkg_id': ALEMTV_PKG_ID,
                    'output': '\n'.join(lines),
                }
        except Exception as exc:
            err = str(exc)
            if '26101' in err or 'timed out' in err.lower() or 'refused' in err.lower():
                lines.append(
                    f'[i] Enable Developer Mode on Samsung TV: Apps -> 1 2 3 4 5 -> Developer Mode ON -> '
                    f'Host PC IP: {pc_ip} -> hold Power 5s to reboot TV.'
                )
            lines.append(f'[!] Alem TV install failed: {err}')
            return {'ok': False, 'error': err, 'output': '\n'.join(lines)}

    def launch_samsung_alemtv(self, ip=None):
        try:
            clean_ip = self._resolve_samsung_ip(ip)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}
        try:
            with SdbClient(clean_ip, timeout=10.0) as sdb:
                reply = sdb.launch_app(ALEMTV_APP_ID)
            return {
                'ok': True,
                'app_id': ALEMTV_APP_ID,
                'output': f'[+] Launched Alem TV ({ALEMTV_APP_ID}): {reply}',
            }
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'output': f'[!] Launch failed: {exc}'}

    def remove_samsung_alemtv(self, ip=None):
        try:
            clean_ip = self._resolve_samsung_ip(ip)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}
        try:
            with SdbClient(clean_ip, timeout=10.0) as sdb:
                reply = sdb.uninstall_app(ALEMTV_PKG_ID)
            return {
                'ok': True,
                'pkg_id': ALEMTV_PKG_ID,
                'output': f'[+] Removed Alem TV ({ALEMTV_PKG_ID}): {reply.strip() or "done"}',
            }
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'output': f'[!] Uninstall failed: {exc}'}


