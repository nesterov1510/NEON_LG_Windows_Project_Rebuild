import datetime
import hashlib
import io
import json
import os
from unittest import mock
import zipfile

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from core import Api
from local_tv_packages import validate_tizen_package, validate_wgt
from samsung_catalog import (
    CatalogError,
    SamsungCatalogManager,
    UserSettingsError,
    apply_user_settings_to_package,
    build_catalog_groups,
    is_version_below,
    normalize_catalog,
    parse_vd_applist,
    validate_user_values,
)
from samsung_tizen import (
    SamsungCertManager,
    SamsungPartnerPrivilegeError,
    load_p12_bundle,
    verify_wgt_signatures,
)


def _make_sample_wgt(pkg_id='TestPkg001', app_id='TestPkg001.Main', req_ver='4.0', extra_files=None):
    buf = io.BytesIO()
    cfg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<widget xmlns="http://www.w3.org/ns/widgets" xmlns:tizen="http://tizen.org/ns/widgets" '
        f'id="https://example.com/{pkg_id}" version="1.2.3">\n'
        f'  <tizen:application id="{app_id}" package="{pkg_id}" required_version="{req_ver}"/>\n'
        '  <name>SampleApp</name>\n'
        '  <content src="index.html"/>\n'
        '</widget>\n'
    ).encode('utf-8')
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('config.xml', cfg)
        zf.writestr('index.html', b'<html><body>Hi</body></html>')
        for k, v in (extra_files or {}).items():
            zf.writestr(k, v)
    return buf.getvalue()


def _make_sample_tpk(pkg_id='org.example.overscan', app_id='org.example.overscan.ui', api_ver='6.0'):
    buf = io.BytesIO()
    manifest = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        f'<manifest xmlns="http://tizen.org/ns/packages" api-version="{api_ver}" package="{pkg_id}" version="1.0.0">\n'
        f'  <ui-application appid="{app_id}" exec="overscan" type="capp"/>\n'
        '</manifest>\n'
    ).encode('utf-8')
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('tizen-manifest.xml', manifest)
        zf.writestr('bin/overscan', b'ELF')
    return buf.getvalue()


def _mint_cert_signed_by_ca(ca_cert_path, subject_cn='TestSigner'):
    """Create a leaf cert whose issuer matches a real bundled Samsung CA (for chain resolution tests)."""
    with open(ca_cert_path, 'rb') as f:
        ca_raw = f.read()
    if b'-----BEGIN CERTIFICATE-----' in ca_raw:
        ca_cert = x509.load_pem_x509_certificate(ca_raw)
    else:
        ca_cert = x509.load_der_x509_certificate(ca_raw)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    # Use a throwaway CA key to sign so issuer DN matches; note _verify_cert_signed_by checks crypto signature
    # so we return BOTH the CA key/cert if testing real crypto or patch _verify_cert_signed_by cleanly.
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return key, cert, ca_cert


def _mint_real_ca_and_leaf(cn_ca, cn_leaf, duid=None):
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn_ca)])
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Samsung Root CA')])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(root_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=10))
        .not_valid_after(now + datetime.timedelta(days=1000))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn_leaf)]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
    )
    if duid:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(f'URN:tizen:deviceid={duid}')]),
            critical=False,
        )
    leaf_cert = builder.sign(ca_key, hashes.SHA256())
    return leaf_key, leaf_cert, ca_cert


def _sign_csr_from_multipart(body_text, ca_key, ca_cert):
    import re as _re
    m = _re.search(r'-----BEGIN CERTIFICATE REQUEST-----.*?-----END CERTIFICATE REQUEST-----', body_text, flags=_re.DOTALL)
    csr = x509.load_pem_x509_csr(m.group(0).encode('ascii'))
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
    )
    for ext in csr.extensions:
        builder = builder.add_extension(ext.value, critical=ext.critical)
    return builder.sign(ca_key, hashes.SHA256())


def test_validate_tizen_package_wgt_tpk_and_security():
    wgt_standard = _make_sample_wgt(pkg_id='TestPkg001', app_id='TestPkg001.Main')
    assert validate_wgt(wgt_standard) == ('TestPkg001', 'TestPkg001.Main')
    meta_wgt = validate_tizen_package(wgt_standard)
    assert meta_wgt['kind'] == 'wgt'
    assert meta_wgt['pkg_id'] == 'TestPkg001'
    assert meta_wgt['required_version'] == '4.0'

    # Reverse-DNS WGT (like Baobab org.baobab.signage in catalog.json)
    wgt_rdns = _make_sample_wgt(pkg_id='org.baobab.signage', app_id='org.baobab.signage', req_ver='5.5')
    meta_rdns = validate_tizen_package(wgt_rdns)
    assert meta_rdns['pkg_id'] == 'org.baobab.signage'
    assert meta_rdns['app_id'] == 'org.baobab.signage'

    # Native TPK with tizen-manifest.xml (like Overscan)
    tpk = _make_sample_tpk(pkg_id='org.apps2samsung.overscan', app_id='org.apps2samsung.overscan.ui', api_ver='6.0')
    meta_tpk = validate_tizen_package(tpk, allow_tpk=True)
    assert meta_tpk['kind'] == 'tpk'
    assert meta_tpk['pkg_id'] == 'org.apps2samsung.overscan'
    assert meta_tpk['app_id'] == 'org.apps2samsung.overscan.ui'
    assert meta_tpk['required_version'] == '6.0'

    # Zip-slip archive rejected
    bad_zip = io.BytesIO()
    with zipfile.ZipFile(bad_zip, 'w') as zf:
        zf.writestr('../evil.txt', b'x')
        zf.writestr('config.xml', b'<widget/>')
    with pytest.raises(ValueError):
        validate_tizen_package(bad_zip.getvalue())

    # XML DTD entity expansion rejected
    dtd_zip = io.BytesIO()
    with zipfile.ZipFile(dtd_zip, 'w') as zf:
        zf.writestr('config.xml', b'<!DOCTYPE foo><widget xmlns="http://www.w3.org/ns/widgets"/>')
    with pytest.raises(ValueError):
        validate_tizen_package(dtd_zip.getvalue())


def test_catalog_normalization_filtering_adult_variants_and_removed():
    raw_catalog = {
        'schemaVersion': 2,
        'categories': ['media', 'utility'],
        'category_colors': {'media': '#4caf50', 'utility': 'not-a-color'},
        'apps': [
            {
                'file': 'Jellyfin-tizen6.wgt',
                'name': 'Jellyfin',
                'description': 'Media client',
                'category': 'media',
                'group': 'jellyfin',
                'variant': 'Tizen 6+',
                'version': '0.1',
                'required_version': '6.0',
                'package_id': 'Jellyfin01',
                'app_id': 'Jellyfin01.Main',
                'repo': 'jellyfin/jellyfin-tizen',
                'host': 'github',
                'flags': ['self-updating', 'future-flag'],
                'cert_level': 'public',
            },
            {
                'file': 'Jellyfin-tizen5.wgt',
                'name': 'Jellyfin',
                'description': 'Media client legacy',
                'category': 'media',
                'group': 'jellyfin',
                'variant': 'Tizen 5',
                'required_version': '5.0',
                'package_id': 'Jellyfin01',
                'app_id': 'Jellyfin01.Main',
                'repo': 'jellyfin/jellyfin-tizen',
                'host': 'gitlab',
            },
            {
                'file': 'Overscan-tizen6.tpk',
                'name': 'Overscan',
                'description': 'Adjust screen edges',
                'category': 'unknown-future-cat',
                'package_id': 'org.apps2samsung.overscan',
                'app_id': 'org.apps2samsung.overscan.ui',
                'required_version': '6.0',
                'flags': ['root'],
                'cert_level': 'partner',
                'repo': 'Apps2Samsung/Overscan',
                'moved_to': 'SettingsDisplay.tpk',
            },
            {
                'file': 'AdultApp.wgt',
                'name': 'Adult Stream',
                'description': 'Adult catalog entry',
                'category': 'media',
                'adult': True,
                'package_id': 'AdultApp01',
                'app_id': 'AdultApp01.Main',
            },
            {
                'file': '../bad-path.wgt',
                'name': 'Should Be Skipped',
                'category': 'media',
            },
        ],
        'removed': [
            {
                'files': ['OldApp.wgt'],
                'reason': 'dmca',
                'note': 'Removed by owner',
                'since': '2026-09-01',
                'package_ids': ['OldApp0001'],
            }
        ],
    }
    assets_map = {
        'Jellyfin-tizen6.wgt': {'browser_download_url': 'https://github.com/a/b/Jellyfin-tizen6.wgt', 'digest': 'sha256:' + ('a' * 64), 'size': 1024},
        'Overscan-tizen6.tpk': {'browser_download_url': 'https://github.com/a/b/Overscan-tizen6.tpk', 'digest': 'sha256:' + ('b' * 64), 'size': 2048},
    }
    norm = normalize_catalog(raw_catalog, assets_map=assets_map, release_meta={'tag_name': 'community-999'})
    assert norm['schemaVersion'] == 2
    assert any('newer than supported' in w for w in norm['warnings'])
    assert 'other' in norm['categories']
    assert norm['category_colors']['utility'] == '#2f6f55'  # invalid hex sanitized
    assert len(norm['apps']) == 4  # unsafe filename dropped

    overscan = next(a for a in norm['apps'] if a['file'] == 'Overscan-tizen6.tpk')
    assert overscan['category'] == 'other'
    assert overscan['pkg_kind'] == 'tpk'
    assert overscan['is_root'] is True
    assert overscan['cert_level'] == 'partner'
    assert overscan['moved_to'] == 'SettingsDisplay.tpk'

    jf_legacy = next(a for a in norm['apps'] if a['file'] == 'Jellyfin-tizen5.wgt')
    assert jf_legacy['repo_url'] == 'https://gitlab.com/jellyfin/jellyfin-tizen'

    # Adult filtering happens before/at grouping
    groups_safe = build_catalog_groups(norm['apps'], show_adult=False)
    assert [g['name'] for g in groups_safe] == ['Jellyfin', 'Overscan']
    assert len(groups_safe[0]['variants']) == 2

    groups_adult = build_catalog_groups(norm['apps'], show_adult=True)
    assert [g['name'] for g in groups_adult] == ['Jellyfin', 'Overscan', 'Adult Stream']

    assert len(norm['removed']) == 1
    assert norm['removed'][0]['can_install'] is False
    assert 'DMCA' in norm['removed'][0]['reason_en']


def test_user_settings_validation_escaping_and_archive_patching():
    pkg = _make_sample_wgt(
        extra_files={
            'js/config.js': b'var SERVER_URL = "http://placeholder";\nvar PORT = 8080;\nvar OPTIONAL_TOKEN = "";\n',
        }
    )
    specs = [
        {
            'file': 'js/config.js',
            'id': 'server_url',
            'label': 'Server URL',
            'type': 'text',
            'required': True,
            'pattern': r'^https?://',
            'find': 'var SERVER_URL = "http://placeholder";',
            'replace': 'var SERVER_URL = "{value}";',
        },
        {
            'file': 'js/config.js',
            'id': 'port',
            'label': 'Port',
            'type': 'number',
            'required': True,
            'min': 1,
            'max': 65535,
            'disallow': [22],
            'find': 'var PORT = 8080;',
            'replace': 'var PORT = {value};',
        },
        {
            'file': 'js/config.js',
            'id': 'opt_note',
            'label': 'Optional Note',
            'type': 'text',
            'required': False,
            'default': '',
            'find': 'var OPTIONAL_TOKEN = "";',
            'replace': 'var OPTIONAL_TOKEN = "{value}";',
        },
    ]

    # Missing required field rejected
    with pytest.raises(UserSettingsError, match='required'):
        validate_user_values(specs, {'server_url': '', 'port': '9000'})

    # Disallowed port rejected
    with pytest.raises(UserSettingsError, match='not allowed'):
        validate_user_values(specs, {'server_url': 'https://tv.local', 'port': '22'})

    # Out-of-range port rejected
    with pytest.raises(UserSettingsError, match='<='):
        validate_user_values(specs, {'server_url': 'https://tv.local', 'port': '70000'})

    # Pattern mismatch rejected
    with pytest.raises(UserSettingsError, match='format'):
        validate_user_values(specs, {'server_url': 'ftp://bad', 'port': '8096'})

    # Escaping of quotes and backslashes when quoted inside template; optional blank left untouched
    patched, resolved = apply_user_settings_to_package(
        pkg,
        specs,
        {'server_url': 'https://tv.local/path?a="1"&b=\\x', 'port': '8096', 'opt_note': ''},
    )
    assert resolved['port'] == '8096'
    assert 'opt_note' not in resolved
    with zipfile.ZipFile(io.BytesIO(patched)) as zf:
        js_out = zf.read('js/config.js').decode('utf-8')
    assert 'var SERVER_URL = "https://tv.local/path?a=\\"1\\"&b=\\\\x";' in js_out
    assert 'var PORT = 8096;' in js_out
    assert 'var OPTIONAL_TOKEN = "";' in js_out

    # Missing target file fails BEFORE signing
    bad_specs = [dict(specs[0], file='js/missing.js')]
    with pytest.raises(UserSettingsError, match='missing settings target'):
        apply_user_settings_to_package(pkg, bad_specs, {'server_url': 'https://ok.local'})

    # Missing find string fails BEFORE signing
    bad_find = [dict(specs[0], find='NOT_IN_FILE')]
    with pytest.raises(UserSettingsError, match='Could not find placeholder'):
        apply_user_settings_to_package(pkg, bad_find, {'server_url': 'https://ok.local'})


def test_sha256_verification_and_disk_cache(tmp_path):
    mgr = SamsungCatalogManager(tmp_path)
    payload = _make_sample_wgt()
    sha = hashlib.sha256(payload).hexdigest()
    entry = {
        'file': 'DemoApp.wgt',
        'download_url': 'https://github.com/Apps2Samsung/tizen-community-packages/releases/download/c1/DemoApp.wgt',
        'sha256': sha,
    }

    with mock.patch.object(mgr, '_fetch_http_bytes', return_value=payload) as m_fetch:
        got, got_sha, verified = mgr.download_and_verify_package(entry, require_digest=True)
        assert got == payload
        assert got_sha == sha
        assert verified is True
        assert m_fetch.call_count == 1

    # Second call hits disk cache cleanly with zero HTTP fetch
    with mock.patch.object(mgr, '_fetch_http_bytes', side_effect=AssertionError('should use cache')):
        got2, _, _ = mgr.download_and_verify_package(entry, require_digest=True)
        assert got2 == payload

    # Corrupted download triggers error and never leaves bad file behind
    bad_entry = dict(entry, file='BadApp.wgt')
    with mock.patch.object(mgr, '_fetch_http_bytes', return_value=b'corrupted'):
        with pytest.raises(CatalogError, match='SHA-256 verification failed'):
            mgr.download_and_verify_package(bad_entry, require_digest=True)
    assert not os.path.exists(os.path.join(mgr.packages_dir, 'BadApp.wgt'))


def test_partner_cert_provisioning_and_isolation(tmp_path):
    mgr = SamsungCertManager(tmp_path)
    duid = 'TESTDUID12345'
    a_key, a_leaf, a_ca = _mint_real_ca_and_leaf('Samsung VD Author Test CA', 'NEON')
    d_pub_key, d_pub_leaf, d_pub_ca = _mint_real_ca_and_leaf('Samsung VD Public CA', 'TizenSDK', duid=duid)

    def _make_ca(cn_ca):
        ck = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.timezone.utc)
        ca = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn_ca)]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Samsung Root CA')]))
            .public_key(ck.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=10))
            .not_valid_after(now + datetime.timedelta(days=1000))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(ck, hashes.SHA256())
        )
        return ck, ca

    a_srv_key, a_srv_ca = _make_ca('Samsung VD Author Server CA')
    p_srv_key, p_srv_ca = _make_ca('Samsung VD Partner CA')

    def _cert_pem(cert):
        return cert.public_bytes(serialization.Encoding.PEM)

    os.makedirs(mgr.ca_dir, exist_ok=True)
    with open(os.path.join(mgr.ca_dir, 'vd_tizen_dev_author_ca.cer'), 'wb') as f:
        f.write(_cert_pem(a_srv_ca))
    with open(os.path.join(mgr.ca_dir, 'vd_tizen_dev_public2.crt'), 'wb') as f:
        f.write(_cert_pem(d_pub_ca))
    with open(os.path.join(mgr.ca_dir, 'vd_tizen_dev_partner2.crt'), 'wb') as f:
        f.write(_cert_pem(p_srv_ca))

    # Pre-seed public cert in public folder to verify Partner lookup ignores Public certs
    os.makedirs(mgr.cert_dir, exist_ok=True)
    from cryptography.hazmat.primitives.serialization import pkcs12
    (tmp_path / 'samsung_cert' / 'author.p12').write_bytes(
        pkcs12.serialize_key_and_certificates(b'UserCertificate', a_key, a_leaf, [a_ca], serialization.NoEncryption())
    )
    (tmp_path / 'samsung_cert' / f'distributor_{duid}.p12').write_bytes(
        pkcs12.serialize_key_and_certificates(b'UserCertificate', d_pub_key, d_pub_leaf, [d_pub_ca], serialization.NoEncryption())
    )
    (tmp_path / 'samsung_cert' / 'password.txt').write_text('', encoding='utf-8')

    assert mgr.find_saved_samsung_certs(duid, cert_level='public') is not None
    assert mgr.find_saved_samsung_certs(duid, cert_level='partner') is None

    # Provision Partner cert -> verifies lowercase 'partner' privilege_level + separate folder
    seen_bodies = []

    def fake_http(req, timeout=30, allow_http_mirror=False):
        url = req.full_url if hasattr(req, 'full_url') else str(req)
        body = (getattr(req, 'data', b'') or b'').decode('utf-8', errors='replace')
        seen_bodies.append((url, body))
        if 'author' in url:
            leaf = _sign_csr_from_multipart(body, a_srv_key, a_srv_ca)
            return _cert_pem(leaf) + b'\n' + _cert_pem(a_srv_ca)
        if 'v1' in url:
            return b'<profile>partner</profile>'
        dist_leaf = _sign_csr_from_multipart(body, p_srv_key, p_srv_ca)
        return _cert_pem(dist_leaf) + b'\n' + _cert_pem(p_srv_ca)

    with mock.patch('samsung_tizen._http_read', side_effect=fake_http):
        bundle = mgr.provision_with_oauth_token(
            duid,
            {'access_token': 'tok-123', 'userId': 'uid-1', 'email': 'u@example.com'},
            cert_level='partner',
        )

    assert bundle['cert_level'] == 'partner'
    assert os.path.abspath(bundle['source']) == os.path.abspath(mgr.partner_cert_dir)
    assert any('name="privilege_level"\r\n\r\npartner\r\n' in b for _u, b in seen_bodies)
    assert mgr.find_saved_samsung_certs(duid, cert_level='partner') is not None
    assert mgr.find_saved_samsung_certs(duid, cert_level='public')['cert_level'] == 'public'

    # Partner HTTP rejection raises SamsungPartnerPrivilegeError with token_info preserved
    import urllib.error

    def reject_partner(req, timeout=30, allow_http_mirror=False):
        url = req.full_url if hasattr(req, 'full_url') else str(req)
        body = (getattr(req, 'data', b'') or b'').decode('utf-8', errors='replace')
        if 'v1' in url:
            raise urllib.error.HTTPError(url, 403, 'Forbidden', {}, io.BytesIO(b'Partner denied'))
        leaf = _sign_csr_from_multipart(body, a_srv_key, a_srv_ca)
        return _cert_pem(leaf) + b'\n' + _cert_pem(a_srv_ca)

    with mock.patch('samsung_tizen._http_read', side_effect=reject_partner):
        with pytest.raises(SamsungPartnerPrivilegeError) as exc_info:
            mgr.provision_with_oauth_token(
                'NEWDUID001',
                {'access_token': 'tok-keep', 'userId': 'uid-1', 'email': 'u@example.com'},
                cert_level='partner',
            )
        assert exc_info.value.token_info['access_token'] == 'tok-keep'


def test_core_api_catalog_install_version_check_and_user_settings(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))

    api = Api()
    raw_wgt = _make_sample_wgt(
        pkg_id='CustomApp1',
        app_id='CustomApp1.Main',
        req_ver='7.0',
        extra_files={'config.js': b'const API_HOST = "REPLACE_ME";\nconst TOKEN = "REPLACE_TOK";\n'},
    )
    wgt_sha = hashlib.sha256(raw_wgt).hexdigest()
    raw_catalog = {
        'schemaVersion': 1,
        'categories': ['media'],
        'category_colors': {'media': '#4caf50'},
        'apps': [
            {
                'file': 'CustomApp1.wgt',
                'name': 'Custom Media',
                'description': 'Configurable test app',
                'category': 'media',
                'package_id': 'CustomApp1',
                'app_id': 'CustomApp1.Main',
                'required_version': '7.0',
                'cert_level': 'partner',
                'user_settings': {
                    'config.js': [
                        {
                            'id': 'api_host',
                            'label': 'API Host',
                            'type': 'text',
                            'required': True,
                            'find': 'const API_HOST = "REPLACE_ME";',
                            'replace': 'const API_HOST = "{value}";',
                        },
                        {
                            'id': 'api_token',
                            'label': 'API Token',
                            'type': 'text',
                            'required': False,
                            'find': 'const TOKEN = "REPLACE_TOK";',
                            'replace': 'const TOKEN = "{value}";',
                        },
                    ]
                },
            }
        ],
        'removed': [{'files': ['BannedApp.wgt'], 'reason': 'malware', 'package_ids': ['Banned0001']}],
    }
    release_json = {
        'tag_name': 'community-900',
        'published_at': '2026-11-01T00:00:00Z',
        'assets': [
            {
                'name': 'catalog.json',
                'browser_download_url': 'https://github.com/Apps2Samsung/tizen-community-packages/releases/download/community-900/catalog.json',
                'size': 500,
                'digest': 'sha256:' + hashlib.sha256(json.dumps(raw_catalog).encode('utf-8')).hexdigest(),
            },
            {
                'name': 'CustomApp1.wgt',
                'browser_download_url': 'https://github.com/Apps2Samsung/tizen-community-packages/releases/download/community-900/CustomApp1.wgt',
                'size': len(raw_wgt),
                'digest': 'sha256:' + wgt_sha,
            },
        ],
    }

    def fake_http_read(req, timeout=30, allow_http_mirror=False):
        url = req.full_url if hasattr(req, 'full_url') else str(req)
        if url.endswith('/releases/latest'):
            return json.dumps(release_json).encode('utf-8')
        if url.endswith('/catalog.json'):
            return json.dumps(raw_catalog).encode('utf-8')
        if url.endswith('/CustomApp1.wgt'):
            return raw_wgt
        raise AssertionError(f'Unexpected URL: {url}')

    # Prepare valid Partner certs in NEON data_dir
    duid = 'TVDUID99999'
    a_key, a_leaf, a_ca = _mint_real_ca_and_leaf('Samsung Author CA', 'NEON')
    d_key, d_leaf, d_ca = _mint_real_ca_and_leaf('Samsung Partner CA', 'TizenSDK', duid=duid)

    pushed = {}

    class FakeSdb:
        def __init__(self, host, timeout=10.0):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def capability(self):
            return {'platform_version': '6.5', 'sdk_toolpath': '/home/owner/share/tmp/sdk_tools'}

        def get_duid(self, caps=None):
            return duid

        def resolve_sdk_toolpath(self, caps=None):
            return '/home/owner/share/tmp/sdk_tools'

        def push_bytes(self, payload, remote_path):
            pushed['payload'] = payload
            pushed['remote_path'] = remote_path

        def install_wgt(self, pkg_id, remote_path, timeout=120.0):
            return 'install completed\nspend time(1200ms)'

        def list_apps(self):
            return 'app_tizen_id : CustomApp1.Main , pkg_id : CustomApp1\n'

        def launch_app(self, app_id):
            return 'launched'

        def uninstall_app(self, pkg_id):
            return 'uninstall completed'

    partner_bundle = {
        'source': str(tmp_path / 'samsung_cert_partner'),
        'cert_level': 'partner',
        'author_key': a_key,
        'author_certs': [a_leaf, a_ca],
        'dist_key': d_key,
        'dist_certs': [d_leaf, d_ca],
    }

    with mock.patch('samsung_catalog._http_read', side_effect=fake_http_read), \
         mock.patch('core.SdbClient', FakeSdb), \
         mock.patch.object(SamsungCertManager, 'find_saved_samsung_certs', return_value=partner_bundle):

        cat_res = api.get_samsung_catalog(force_refresh=True)
        assert cat_res['ok'] is True
        assert len(cat_res['groups']) == 1

        # Removed package strictly blocked
        rem_res = api.install_samsung_catalog_app(ip='192.168.1.50', file_name='BannedApp.wgt')
        assert rem_res['ok'] is False and rem_res['removed'] is True

        # Version mismatch prompts confirmation when TV 6.5 < required 7.0
        prompt_res = api.install_samsung_catalog_app(
            ip='192.168.1.50',
            file_name='CustomApp1.wgt',
            user_values={'api_host': 'https://my.nas:8096', 'api_token': 'super-secret-val'},
            force_version_override=False,
        )
        assert prompt_res['ok'] is False
        assert prompt_res['needs_version_confirm'] is True
        assert prompt_res['tv_version'] == '6.5'
        assert prompt_res['required_version'] == '7.0'

        # Forced install succeeds, signs patched archive, and never persists secret token in settings.json
        ok_res = api.install_samsung_catalog_app(
            ip='192.168.1.50',
            file_name='CustomApp1.wgt',
            user_values={'api_host': 'https://my.nas:8096', 'api_token': 'super-secret-val'},
            force_version_override=True,
            launch=True,
        )
        assert ok_res['ok'] is True
        assert ok_res['pkg_id'] == 'CustomApp1'
        assert pushed['remote_path'].endswith('/CustomApp1.wgt')
        verify_wgt_signatures(pushed['payload'])
        with zipfile.ZipFile(io.BytesIO(pushed['payload'])) as zf:
            js = zf.read('config.js').decode('utf-8')
        assert 'https://my.nas:8096' in js
        assert 'super-secret-val' in js

        saved_cfg = api.load_settings()['samsung_catalog_settings']['CustomApp1.wgt']
        assert saved_cfg.get('api_host') == 'https://my.nas:8096'
        assert 'api_token' not in saved_cfg  # secret omitted from settings.json

        chk = api.check_samsung_tv_installed(ip='192.168.1.50')
        assert chk['ok'] is True
        assert 'CustomApp1' in chk['package_ids']
        assert 'CustomApp1.Main' in chk['app_ids']

        assert api.launch_samsung_app(ip='192.168.1.50', app_id='CustomApp1.Main')['ok'] is True
        assert api.uninstall_samsung_app(ip='192.168.1.50', pkg_id='CustomApp1')['ok'] is True


def test_parse_vd_applist_and_version_comparison():
    assert is_version_below('5.5', '6.0') is True
    assert is_version_below('6.0', '6.0') is False
    assert is_version_below('7.0', '6.0') is False
    assert is_version_below('?', '6.0') is False

    parsed = parse_vd_applist(
        'app_tizen_id : Jellyfin01.Main , pkg_id : Jellyfin01\n'
        'app_id=org.baobab.signage, package_id=org.baobab.signage\n'
    )
    assert 'Jellyfin01' in parsed['package_ids']
    assert 'org.baobab.signage' in parsed['package_ids']
    assert 'Jellyfin01.Main' in parsed['app_ids']
