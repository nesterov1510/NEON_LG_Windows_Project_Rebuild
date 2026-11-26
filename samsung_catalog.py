"""Samsung Tizen community catalog client for NEON.

Integrates https://github.com/Apps2Samsung/tizen-community-packages (schema v1):
- Release metadata & catalog.json fetching with ETag / TTL caching & SHA-256 verification
- Safe URL, category, removed[], flags, cert_level, adult, and variant normalization
- Dynamic user_settings validation, escaping, and in-archive patching prior to signing
- TV installed package parsing and Tizen version comparison
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from samsung_tizen import _http_read

CATALOG_REPO_SLUG = 'Apps2Samsung/tizen-community-packages'
CATALOG_REPO_URL = 'https://github.com/Apps2Samsung/tizen-community-packages'
CATALOG_RELEASE_API_URL = f'https://api.github.com/repos/{CATALOG_REPO_SLUG}/releases/latest'
CATALOG_JSON_FALLBACK_URL = f'{CATALOG_REPO_URL}/releases/latest/download/catalog.json'

SUPPORTED_SCHEMA_VERSION = 1
CATALOG_CACHE_TTL = 1800
MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_BYTES = 128 * 1024 * 1024

_HEX_COLOR_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')
_SHA256_RE = re.compile(r'^(?:sha256:)?([0-9A-Fa-f]{64})$')
_SAFE_FILENAME_RE = re.compile(r'^[A-Za-z0-9._-]+\.(?:wgt|tpk)$', re.IGNORECASE)
_REPO_PATH_RE = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$')
_VERSION_RE = re.compile(r'(\d+(?:\.\d+)*)')

REMOVED_REASON_LABELS = {
    'dmca': ('Removed due to DMCA / copyright request', 'Удалено по запросу правообладателя (DMCA)'),
    'broken': ('Removed: broken or unmaintained', 'Удалено: приложение не работает или устарело'),
    'superseded': ('Removed: superseded by another release', 'Удалено: заменено другой версией'),
    'malware': ('Removed for security reasons', 'Удалено по соображениям безопасности'),
    'author-request': ('Removed at author request', 'Удалено по просьбе автора'),
    'duplicate': ('Removed: duplicate package', 'Удалено: дубликат пакета'),
}


class CatalogError(RuntimeError):
    """Base error for Samsung community catalog operations."""


class UserSettingsError(ValueError):
    """Raised when required user_settings validation or archive patching fails."""


def sanitize_hex_color(value, default='#2f6f55'):
    txt = str(value or '').strip()
    return txt if _HEX_COLOR_RE.fullmatch(txt) else default


def is_safe_https_url(url):
    try:
        parsed = urllib.parse.urlparse(str(url or '').strip())
        return parsed.scheme.lower() == 'https' and bool(parsed.netloc) and not parsed.username and not parsed.password
    except Exception:
        return False


def build_repo_url(host, repo):
    clean_repo = str(repo or '').strip().strip('/')
    if not clean_repo or not _REPO_PATH_RE.fullmatch(clean_repo):
        return ''
    h = str(host or 'github').strip().lower()
    if h == 'gitlab':
        return f'https://gitlab.com/{clean_repo}'
    if h == 'github':
        return f'https://github.com/{clean_repo}'
    return ''


def safe_asset_filename(file_name):
    raw = str(file_name or '').strip()
    base = os.path.basename(raw)
    if not raw or base != raw or '/' in raw or '\\' in raw or '..' in raw:
        raise CatalogError(f'Unsafe catalog package filename: {file_name!r}')
    if not _SAFE_FILENAME_RE.fullmatch(base):
        raise CatalogError(f'Unsupported catalog package filename: {file_name!r}')
    return base


def extract_sha256_digest(digest_value):
    m = _SHA256_RE.fullmatch(str(digest_value or '').strip())
    return m.group(1).lower() if m else ''


def parse_version_tuple(version_str):
    m = _VERSION_RE.search(str(version_str or '').strip())
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split('.')]
    return tuple(parts)


def is_version_below(tv_version, required_version):
    """Return True only when both versions parse cleanly and tv_version < required_version."""
    tv_t = parse_version_tuple(tv_version)
    req_t = parse_version_tuple(required_version)
    if not tv_t or not req_t:
        return False
    width = max(len(tv_t), len(req_t))
    tv_pad = tv_t + (0,) * (width - len(tv_t))
    req_pad = req_t + (0,) * (width - len(req_t))
    return tv_pad < req_pad


def humanize_removed_reason(reason, lang='en'):
    key = str(reason or '').strip().lower()
    pair = REMOVED_REASON_LABELS.get(key)
    if pair:
        return pair[1] if lang == 'ru' else pair[0]
    if key:
        return f'Удалено ({key})' if lang == 'ru' else f'Removed ({key})'
    return 'Удалено из каталога' if lang == 'ru' else 'Removed from catalog'


def is_safe_archive_relpath(rel_path):
    p = str(rel_path or '').strip()
    if not p or p.startswith(('/', '\\')) or '\\' in p or re.match(r'^[A-Za-z]:', p):
        return False
    parts = p.split('/')
    return all(part not in ('', '.', '..') for part in parts)


def validate_user_settings_spec(raw_settings):
    """Validate catalog v1 user_settings dict -> (specs_list, is_valid, error_msg)."""
    if raw_settings is None:
        return [], True, ''
    if not isinstance(raw_settings, dict) or not raw_settings:
        return [], False, 'user_settings must be a non-empty object keyed by file path.'
    specs = []
    for file_path, rules in raw_settings.items():
        if not is_safe_archive_relpath(file_path):
            return [], False, f'Unsafe user_settings file path: {file_path!r}'
        if not isinstance(rules, list) or not rules:
            return [], False, f'user_settings[{file_path!r}] must be a non-empty list.'
        for rule in rules:
            if not isinstance(rule, dict):
                return [], False, f'Invalid user_settings rule in {file_path!r}.'
            fid = str(rule.get('id') or '').strip()
            label = str(rule.get('label') or '').strip()
            find_str = rule.get('find')
            replace_str = rule.get('replace')
            if not fid or not label or not isinstance(find_str, str) or not find_str or not isinstance(replace_str, str):
                return [], False, f'Incomplete user_settings rule in {file_path!r}.'
            ftype = str(rule.get('type') or 'text').strip().lower()
            if ftype not in ('text', 'number'):
                return [], False, f'Unsupported user_settings field type {ftype!r}.'
            disallow = rule.get('disallow')
            if disallow is not None and not isinstance(disallow, list):
                return [], False, f'Invalid disallow list in {fid}.'
            specs.append({
                'file': str(file_path).strip(),
                'id': fid,
                'label': label,
                'type': ftype,
                'required': bool(rule.get('required', True)),
                'default': rule.get('default'),
                'placeholder': str(rule.get('placeholder') or ''),
                'description': str(rule.get('description') or ''),
                'min': rule.get('min'),
                'max': rule.get('max'),
                'disallow': list(disallow) if isinstance(disallow, list) else [],
                'pattern': str(rule.get('pattern') or ''),
                'find': find_str,
                'replace': replace_str,
            })
    return specs, True, ''


def collect_form_fields(specs):
    """Deduplicate user_settings rules by id for UI form rendering."""
    seen = {}
    ordered = []
    for s in specs or ():
        fid = s['id']
        if fid not in seen:
            item = {
                'id': fid,
                'label': s['label'],
                'type': s['type'],
                'required': s['required'],
                'default': '' if s['default'] is None else str(s['default']),
                'placeholder': s['placeholder'],
                'description': s['description'],
                'min': s['min'],
                'max': s['max'],
                'disallow': list(s.get('disallow') or []),
                'pattern': s['pattern'],
                'secret': any(k in fid.lower() for k in ('token', 'secret', 'password', 'apikey', 'api_key')),
            }
            seen[fid] = item
            ordered.append(item)
        else:
            existing = seen[fid]
            existing['required'] = existing['required'] or bool(s['required'])
            if not existing['default'] and s['default'] is not None:
                existing['default'] = str(s['default'])
            if not existing['description'] and s['description']:
                existing['description'] = s['description']
    return ordered


def _escape_for_replace(value_str, field_type, replace_template):
    if field_type == 'number':
        return value_str
    idx = replace_template.find('{value}')
    if idx <= 0:
        return value_str
    prev_ch = replace_template[idx - 1]
    if prev_ch in ('"', "'"):
        out = value_str.replace('\\', '\\\\').replace(prev_ch, '\\' + prev_ch)
        out = out.replace('\r', '\\r').replace('\n', '\\n')
        return out
    return value_str


def validate_user_values(specs, user_values):
    """Validate user_values against user_settings rules -> dict[id, resolved_str]."""
    values_in = user_values if isinstance(user_values, dict) else {}
    resolved = {}
    for spec in specs or ():
        fid = spec['id']
        label = spec['label']
        raw_val = values_in.get(fid)
        val_str = '' if raw_val is None else str(raw_val).strip()

        if not val_str:
            default_val = '' if spec.get('default') is None else str(spec.get('default')).strip()
            if spec.get('required', True):
                if default_val:
                    val_str = default_val
                else:
                    raise UserSettingsError(f"Setting '{label}' is required.")
            else:
                if default_val:
                    val_str = default_val
                else:
                    # Optional field left blank without a default -> skip replacement
                    continue

        if len(val_str) > 2048:
            raise UserSettingsError(f"Setting '{label}' exceeds maximum length (2048).")

        ftype = spec.get('type', 'text')
        if ftype == 'number':
            try:
                num = float(val_str)
            except ValueError as exc:
                raise UserSettingsError(f"Setting '{label}' must be a valid number.") from exc
            if num != num or num in (float('inf'), float('-inf')):
                raise UserSettingsError(f"Setting '{label}' must be a finite number.")
            min_v = spec.get('min')
            max_v = spec.get('max')
            if min_v is not None and num < float(min_v):
                raise UserSettingsError(f"Setting '{label}' must be >= {min_v}.")
            if max_v is not None and num > float(max_v):
                raise UserSettingsError(f"Setting '{label}' must be <= {max_v}.")
            for bad in spec.get('disallow') or ():
                try:
                    if num == float(bad):
                        raise UserSettingsError(f"Value {val_str} is not allowed for '{label}'.")
                except (TypeError, ValueError):
                    if val_str == str(bad).strip():
                        raise UserSettingsError(f"Value {val_str} is not allowed for '{label}'.")
            if float(int(num)) == num and '.' not in val_str:
                val_str = str(int(num))
        else:
            for bad in spec.get('disallow') or ():
                if val_str == str(bad):
                    raise UserSettingsError(f"Value '{val_str}' is not allowed for '{label}'.")
            pat = spec.get('pattern') or ''
            if pat:
                try:
                    if not re.search(pat, val_str):
                        raise UserSettingsError(f"Setting '{label}' does not match required format.")
                except re.error as exc:
                    raise UserSettingsError(f"Invalid catalog validation pattern for '{label}'.") from exc

        resolved[fid] = val_str
    return resolved


def apply_user_settings_to_package(pkg_bytes, specs, user_values):
    """Validate user_values and apply replacements inside a Tizen .wgt/.tpk before signing."""
    if not specs:
        return pkg_bytes, {}
    resolved = validate_user_values(specs, user_values)

    by_file = {}
    for spec in specs:
        fid = spec['id']
        if fid not in resolved:
            continue
        by_file.setdefault(spec['file'], []).append(spec)

    if not by_file:
        return pkg_bytes, resolved

    out_buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(pkg_bytes), 'r') as zin:
        names = set(zin.namelist())
        for rel_file in by_file:
            if rel_file not in names:
                raise UserSettingsError(f'Package archive is missing settings target file: {rel_file}')

        patched_contents = {}
        for rel_file, file_specs in by_file.items():
            raw_bytes = zin.read(rel_file)
            try:
                text = raw_bytes.decode('utf-8-sig')
                had_bom = raw_bytes.startswith(b'\xef\xbb\xbf')
            except UnicodeError as exc:
                raise UserSettingsError(f'Target file {rel_file} is not valid UTF-8.') from exc

            for spec in file_specs:
                find_str = spec['find']
                if find_str not in text:
                    raise UserSettingsError(
                        f"Could not find placeholder for '{spec['label']}' in {rel_file}."
                    )
                escaped_val = _escape_for_replace(resolved[spec['id']], spec['type'], spec['replace'])
                rep_str = spec['replace'].replace('{value}', escaped_val)
                text = text.replace(find_str, rep_str)

            patched_bytes = text.encode('utf-8')
            if had_bom:
                patched_bytes = b'\xef\xbb\xbf' + patched_bytes
            patched_contents[rel_file] = patched_bytes

        with zipfile.ZipFile(out_buf, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename in ('signature1.xml', 'author-signature.xml'):
                    continue
                payload = patched_contents.get(info.filename)
                if payload is None:
                    payload = zin.read(info.filename)
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                zout.writestr(zi, payload)

    return out_buf.getvalue(), resolved


def normalize_catalog(raw_catalog, assets_map=None, release_meta=None):
    """Normalize Apps2Samsung tizen-community-packages catalog.json (v1)."""
    if not isinstance(raw_catalog, dict):
        raise CatalogError('Invalid catalog JSON: expected an object.')

    warnings = []
    schema_ver = raw_catalog.get('schemaVersion', 1)
    try:
        schema_num = int(schema_ver)
    except (TypeError, ValueError):
        schema_num = 1
        warnings.append(f'Unrecognized catalog schemaVersion {schema_ver!r}; treating as v1.')
    if schema_num > SUPPORTED_SCHEMA_VERSION:
        warnings.append(
            f'Catalog schema v{schema_num} is newer than supported v{SUPPORTED_SCHEMA_VERSION}; '
            'some new fields may not be displayed.'
        )

    assets = assets_map if isinstance(assets_map, dict) else {}
    rel_meta = release_meta if isinstance(release_meta, dict) else {}
    release_tag = str(rel_meta.get('tag_name') or '').strip()

    raw_cats = raw_catalog.get('categories') or []
    categories = []
    for c in raw_cats:
        cs = str(c or '').strip()
        if cs and cs not in categories:
            categories.append(cs)

    raw_colors = raw_catalog.get('category_colors') or {}
    category_colors = {}
    if isinstance(raw_colors, dict):
        for k, v in raw_colors.items():
            ks = str(k or '').strip()
            if ks:
                category_colors[ks] = sanitize_hex_color(v)

    # Also capture any categories declared inside release metadata/apps, normalizing unknowns -> other
    known_cats = set(categories)

    apps = []
    needs_other = False
    for raw_app in raw_catalog.get('apps') or ():
        if not isinstance(raw_app, dict):
            continue
        try:
            fname = safe_asset_filename(raw_app.get('file'))
        except CatalogError:
            continue

        asset = assets.get(fname) or {}
        dl_url = str(asset.get('browser_download_url') or '').strip()
        if not dl_url:
            if release_tag:
                dl_url = f'{CATALOG_REPO_URL}/releases/download/{urllib.parse.quote(release_tag)}/{urllib.parse.quote(fname)}'
            else:
                dl_url = f'{CATALOG_REPO_URL}/releases/latest/download/{urllib.parse.quote(fname)}'

        sha256_hex = extract_sha256_digest(asset.get('digest') or raw_app.get('sha256'))
        size_bytes = int(asset.get('size') or 0)

        raw_cat = str(raw_app.get('category') or '').strip()
        if raw_cat and (not known_cats or raw_cat in known_cats):
            cat = raw_cat
        else:
            cat = 'other'
            needs_other = True

        raw_flags = [str(f).strip().lower() for f in (raw_app.get('flags') or ()) if str(f).strip()]
        raw_cert = str(raw_app.get('cert_level') or 'public').strip().lower()
        cert_unknown = raw_cert not in ('public', 'partner')
        cert_level = 'partner' if raw_cert == 'partner' else 'public'

        specs, specs_valid, specs_err = validate_user_settings_spec(raw_app.get('user_settings'))
        form_fields = collect_form_fields(specs) if specs_valid else []

        host = str(raw_app.get('host') or 'github').strip().lower()
        repo = str(raw_app.get('repo') or '').strip()
        repo_url = build_repo_url(host, repo)

        pkg_id = str(raw_app.get('package_id') or '').strip()
        app_id = str(raw_app.get('app_id') or '').strip()
        preview_url = str(raw_app.get('preview_image') or '').strip()
        if preview_url and not is_safe_https_url(preview_url):
            preview_url = ''

        apps.append({
            'file': fname,
            'pkg_kind': 'tpk' if fname.lower().endswith('.tpk') else 'wgt',
            'name': str(raw_app.get('name') or fname).strip(),
            'description': str(raw_app.get('description') or '').strip(),
            'category': cat,
            'original_category': raw_cat,
            'group': str(raw_app.get('group') or '').strip(),
            'variant': str(raw_app.get('variant') or '').strip(),
            'version': str(raw_app.get('version') or '').strip(),
            'app_id': app_id,
            'package_id': pkg_id,
            'required_version': str(raw_app.get('required_version') or '').strip(),
            'repo': repo,
            'host': host,
            'repo_url': repo_url,
            'adult': bool(raw_app.get('adult', False)),
            'flags': raw_flags,
            'is_root': 'root' in raw_flags,
            'is_closed_source': 'closed-source' in raw_flags,
            'is_self_updating': 'self-updating' in raw_flags,
            'cert_level': cert_level,
            'cert_level_raw': raw_cert,
            'cert_level_unknown': cert_unknown,
            'preview_image': preview_url,
            'moved_to': str(raw_app.get('moved_to') or '').strip(),
            'user_settings': specs if specs_valid else [],
            'user_settings_fields': form_fields,
            'user_settings_valid': specs_valid,
            'user_settings_error': specs_err,
            'has_user_settings': bool(specs),
            'download_url': dl_url,
            'sha256': sha256_hex,
            'size': size_bytes,
            'asset_present': bool(asset) or not assets,
        })

    if needs_other and 'other' not in categories:
        categories.append('other')
        category_colors.setdefault('other', '#607d8b')
    for c in categories:
        category_colors.setdefault(c, '#2f6f55')

    removed = []
    for rem in raw_catalog.get('removed') or ():
        if not isinstance(rem, dict):
            continue
        reason = str(rem.get('reason') or '').strip()
        files = [str(f).strip() for f in (rem.get('files') or ()) if str(f).strip()]
        pkg_ids = [str(p).strip() for p in (rem.get('package_ids') or ()) if str(p).strip()]
        app_ids = [str(a).strip() for a in (rem.get('app_ids') or ()) if str(a).strip()]
        title = ', '.join(files) if files else (', '.join(pkg_ids) or 'Removed package')
        removed.append({
            'title': title,
            'files': files,
            'reason': reason,
            'reason_en': humanize_removed_reason(reason, 'en'),
            'reason_ru': humanize_removed_reason(reason, 'ru'),
            'note': str(rem.get('note') or '').strip(),
            'since': str(rem.get('since') or '').strip(),
            'package_ids': pkg_ids,
            'app_ids': app_ids,
            'can_install': False,
        })

    return {
        'schemaVersion': schema_num,
        'warnings': warnings,
        'repo_url': CATALOG_REPO_URL,
        'repo_slug': CATALOG_REPO_SLUG,
        'release_tag': release_tag,
        'published_at': str(rel_meta.get('published_at') or '').strip(),
        'categories': categories,
        'category_colors': category_colors,
        'apps': apps,
        'removed': removed,
        'digests_available': bool(assets),
    }


def build_catalog_groups(apps, show_adult=False):
    """Filter adult items first and fold variants sharing `group` cleanly."""
    visible = [a for a in (apps or ()) if show_adult or not a.get('adult')]
    groups_map = {}
    order = []
    for app in visible:
        grp = app.get('group') or f'__single__:{app["file"]}'
        if grp not in groups_map:
            groups_map[grp] = []
            order.append(grp)
        groups_map[grp].append(app)

    result = []
    for grp in order:
        variants = groups_map[grp]
        primary = variants[0]
        result.append({
            'group_key': grp,
            'name': primary['name'],
            'category': primary['category'],
            'description': primary['description'],
            'variants': variants,
            'primary': primary,
        })
    return result


def parse_vd_applist(raw_text):
    """Parse `0 vd_applist` shell output into installed package_ids and app_ids sets."""
    pkg_ids = set()
    app_ids = set()
    id_tok = re.compile(r'^[A-Za-z0-9._-]{4,128}$')
    for line in str(raw_text or '').splitlines():
        s = line.strip()
        if not s:
            continue
        m_app = re.search(r'(?:app_tizen_id|app_id)\s*[:=]\s*([A-Za-z0-9._-]+)', s, re.I)
        m_pkg = re.search(r'(?:pkg_id|pkgid|package_id|package)\s*[:=]\s*([A-Za-z0-9._-]+)', s, re.I)
        if m_app:
            app_ids.add(m_app.group(1))
            if '.' in m_app.group(1) and not m_pkg:
                pkg_ids.add(m_app.group(1).split('.', 1)[0])
        if m_pkg:
            pkg_ids.add(m_pkg.group(1))
        if not m_app and not m_pkg:
            # Fallback for plain "app_id : ... / pkg_id" style lines
            for tok in re.split(r'[\s,;]+', s):
                if '.' in tok and id_tok.fullmatch(tok):
                    app_ids.add(tok)
                    pkg_ids.add(tok.split('.', 1)[0])
    return {'package_ids': sorted(pkg_ids), 'app_ids': sorted(app_ids)}


class SamsungCatalogManager:
    """Fetches, verifies, caches, and downloads packages from Apps2Samsung catalog."""

    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.cache_dir = os.path.join(self.data_dir, 'catalog_cache')
        self.packages_dir = os.path.join(self.cache_dir, 'packages')
        self.meta_path = os.path.join(self.cache_dir, 'release_meta.json')
        self.catalog_path = os.path.join(self.cache_dir, 'catalog.json')

    def _read_disk_cache(self):
        if not (os.path.isfile(self.catalog_path) and os.path.isfile(self.meta_path)):
            return None
        try:
            with open(self.meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            with open(self.catalog_path, 'r', encoding='utf-8') as f:
                raw_cat = json.load(f)
            return meta, raw_cat
        except Exception:
            return None

    def _write_disk_cache(self, meta, raw_catalog_bytes):
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp_cat = self.catalog_path + '.tmp'
        tmp_meta = self.meta_path + '.tmp'
        with open(tmp_cat, 'wb') as f:
            f.write(raw_catalog_bytes)
        with open(tmp_meta, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp_cat, self.catalog_path)
        os.replace(tmp_meta, self.meta_path)

    @staticmethod
    def _build_assets_map(release_json):
        assets_map = {}
        for asset in (release_json.get('assets') or ()):
            name = str(asset.get('name') or '').strip()
            if not name:
                continue
            dl_url = str(asset.get('browser_download_url') or '').strip()
            if dl_url and not is_safe_https_url(dl_url):
                continue
            assets_map[name] = {
                'name': name,
                'browser_download_url': dl_url,
                'size': int(asset.get('size') or 0),
                'digest': str(asset.get('digest') or '').strip(),
            }
        return assets_map

    def _fetch_http_bytes(self, url, timeout=30, headers=None, max_bytes=MAX_CATALOG_BYTES):
        if not is_safe_https_url(url):
            raise CatalogError(f'Blocked unsafe catalog URL: {url!r}')
        req_headers = {'User-Agent': 'NEON-Tizen-Catalog/1.0'}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers, method='GET')
        raw = _http_read(req, timeout=timeout)
        if len(raw) > max_bytes:
            raise CatalogError(f'Downloaded response exceeded {max_bytes // (1024 * 1024)} MB safety limit.')
        return raw

    def get_catalog(self, force_refresh=False):
        """Return normalized catalog dict, using verified cache or fresh GitHub release fetch."""
        cached = self._read_disk_cache()
        now = time.time()

        if cached and not force_refresh:
            meta, raw_cat = cached
            age = now - float(meta.get('fetched_at') or 0)
            if age < CATALOG_CACHE_TTL:
                norm = normalize_catalog(raw_cat, assets_map=meta.get('assets_map'), release_meta=meta)
                norm['from_cache'] = True
                return norm

        headers = {'Accept': 'application/vnd.github+json'}
        if cached and cached[0].get('etag'):
            headers['If-None-Match'] = cached[0]['etag']

        try:
            rel_bytes = self._fetch_http_bytes(CATALOG_RELEASE_API_URL, timeout=25, headers=headers)
            release_json = json.loads(rel_bytes.decode('utf-8'))
            assets_map = self._build_assets_map(release_json)
            cat_asset = assets_map.get('catalog.json') or {}
            cat_url = cat_asset.get('browser_download_url') or CATALOG_JSON_FALLBACK_URL
            cat_digest = extract_sha256_digest(cat_asset.get('digest'))
            cat_bytes = self._fetch_http_bytes(cat_url, timeout=30, max_bytes=MAX_CATALOG_BYTES)
            if cat_digest:
                got_digest = hashlib.sha256(cat_bytes).hexdigest().lower()
                if got_digest != cat_digest:
                    raise CatalogError(
                        f'catalog.json SHA-256 mismatch (expected {cat_digest[:12]}..., got {got_digest[:12]}...)'
                    )
            raw_cat = json.loads(cat_bytes.decode('utf-8'))
            meta = {
                'tag_name': str(release_json.get('tag_name') or '').strip(),
                'published_at': str(release_json.get('published_at') or '').strip(),
                'fetched_at': now,
                'assets_map': assets_map,
                'source': 'github-api',
            }
            self._write_disk_cache(meta, cat_bytes)
            norm = normalize_catalog(raw_cat, assets_map=assets_map, release_meta=meta)
            norm['from_cache'] = False
            return norm
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cached:
                meta, raw_cat = cached
                meta['fetched_at'] = now
                self._write_disk_cache(meta, json.dumps(raw_cat).encode('utf-8'))
                norm = normalize_catalog(raw_cat, assets_map=meta.get('assets_map'), release_meta=meta)
                norm['from_cache'] = True
                return norm
            # Rate limit / API failure -> try direct release fallback, then stale cache
            if exc.code in (403, 429):
                try:
                    cat_bytes = self._fetch_http_bytes(CATALOG_JSON_FALLBACK_URL, timeout=30, max_bytes=MAX_CATALOG_BYTES)
                    raw_cat = json.loads(cat_bytes.decode('utf-8'))
                    meta = {
                        'tag_name': 'latest',
                        'published_at': '',
                        'fetched_at': now,
                        'assets_map': {},
                        'source': 'fallback-download',
                    }
                    self._write_disk_cache(meta, cat_bytes)
                    norm = normalize_catalog(raw_cat, assets_map={}, release_meta=meta)
                    norm['warnings'].append(
                        'GitHub API rate limit reached; loaded fallback catalog.json without asset SHA-256 digests.'
                    )
                    norm['from_cache'] = False
                    return norm
                except Exception:
                    pass
            if cached:
                meta, raw_cat = cached
                norm = normalize_catalog(raw_cat, assets_map=meta.get('assets_map'), release_meta=meta)
                norm['warnings'].append(f'Offline/API error ({exc.code}); showing cached catalog.')
                norm['from_cache'] = True
                return norm
            raise CatalogError(f'Failed to load Samsung community catalog (HTTP {exc.code}).') from exc
        except Exception as exc:
            if cached:
                meta, raw_cat = cached
                norm = normalize_catalog(raw_cat, assets_map=meta.get('assets_map'), release_meta=meta)
                norm['warnings'].append(f'Catalog refresh failed ({exc}); showing cached catalog.')
                norm['from_cache'] = True
                return norm
            raise CatalogError(f'Failed to load Samsung community catalog: {exc}') from exc

    def download_and_verify_package(self, app_entry, require_digest=True):
        """Download or reuse cached .wgt/.tpk and verify SHA-256 against release asset digest."""
        if not isinstance(app_entry, dict):
            raise CatalogError('Invalid catalog application entry.')
        fname = safe_asset_filename(app_entry.get('file'))
        expected_sha = extract_sha256_digest(app_entry.get('sha256'))
        dl_url = str(app_entry.get('download_url') or '').strip()

        if require_digest and not expected_sha:
            raise CatalogError(
                f'Missing release SHA-256 digest for {fname}. Refresh the catalog before installing.'
            )
        if not is_safe_https_url(dl_url):
            raise CatalogError(f'Unsafe download URL for {fname}: {dl_url!r}')

        os.makedirs(self.packages_dir, exist_ok=True)
        cache_path = os.path.join(self.packages_dir, fname)

        if expected_sha and os.path.isfile(cache_path):
            try:
                with open(cache_path, 'rb') as f:
                    cached_bytes = f.read()
                if len(cached_bytes) <= MAX_PACKAGE_BYTES:
                    got = hashlib.sha256(cached_bytes).hexdigest().lower()
                    if got == expected_sha:
                        return cached_bytes, got, True
            except OSError:
                pass
            try:
                os.remove(cache_path)
            except OSError:
                pass

        pkg_bytes = self._fetch_http_bytes(dl_url, timeout=180, max_bytes=MAX_PACKAGE_BYTES)
        got_sha = hashlib.sha256(pkg_bytes).hexdigest().lower()
        if expected_sha and got_sha != expected_sha:
            try:
                if os.path.isfile(cache_path):
                    os.remove(cache_path)
            except OSError:
                pass
            raise CatalogError(
                f'SHA-256 verification failed for {fname} '
                f'(expected {expected_sha[:12]}..., got {got_sha[:12]}...).'
            )

        tmp_path = cache_path + '.tmp'
        with open(tmp_path, 'wb') as f:
            f.write(pkg_bytes)
        os.replace(tmp_path, cache_path)
        return pkg_bytes, got_sha, bool(expected_sha)
